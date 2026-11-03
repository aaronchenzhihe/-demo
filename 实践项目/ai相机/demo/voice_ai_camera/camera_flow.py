# -*- coding: UTF-8 -*-
"""相机流程：菜单选择拍照模式后，进出摄像头模式 + 模式循环。

职责：
  1. 进入摄像头模式：cam.init() → 记录模式 → 切 LCD 所有权给 camera
    2. 模式循环（由 camera 线程调用）：manual(拍照/返回)、voice(返回)
  3. 退出：cam.close() → 切回 LVGL

依赖：
  - camera_srv.CameraService
  - lvgl_ui（LCD 所有权 + 界面恢复）
"""
import utime
import _thread
from usr.threading import Thread
from usr.logging import getLogger

logger = getLogger("camera_flow")

# LCD 所有权（和 lvgl_ui 共享，但出于模块解耦，这里单独管理）
_lcd_owner = "lvgl"   # "lvgl" / "camera"
_state_lock = _thread.allocate_lock()
_camera_mode = None   # "manual" / "voice" / "interval" / None
_restore_menu_pending = False   # 退出相机后，等待 LVGL 线程恢复主菜单
_stop = False

# interval（延时摄影）连拍计时状态：进入 interval 模式时重置
_interval_last_ms = 0    # 上次成功触发 take_photo 的时刻(ms)，0=尚未触发过
_interval_taken = 0      # 已连拍张数
_interval_active = False # 是否正在连拍（GPIO13 短按切换 True/False）
_gpio12_hold_ms = 0      # GPIO12（返回键）按住累计时长 ms
_interval_clear_triggered = False  # 长按清理是否已触发
INTERVAL_HOLD_MS = 1500  # 长按阈值：按住超过 1.5s 触发一键清理

# 语音 AI run() 线程（voice 模式下懒启动，退出时置 None）
_voice_thread = None


def set_lcd_owner(owner):
    global _lcd_owner
    _state_lock.acquire()
    _lcd_owner = owner
    _state_lock.release()
    logger.info("LCD owner 切换为: %s" % owner)


def get_lcd_owner():
    _state_lock.acquire()
    v = _lcd_owner
    _state_lock.release()
    return v


def request_stop():
    global _stop
    _stop = True


def mark_restore_menu():
    """退出相机后置位：由 LVGL 线程负责恢复主菜单界面。"""
    global _restore_menu_pending
    _restore_menu_pending = True


def consume_restore_menu():
    """LVGL 线程取走恢复标志（取走即清空）。"""
    global _restore_menu_pending
    v = _restore_menu_pending
    _restore_menu_pending = False
    return v


def enter_camera_mode(mode, cam):
    """切换到摄像头模式。cam 必须是已初始化的 CameraService。"""
    global _camera_mode
    if not cam.is_ready:
        logger.info("初始化摄像头...")
        try:
            ok = cam.init()
        except Exception as e:
            logger.error("摄像头 init 异常: %s" % e)
            return False
        if not ok:
            logger.error("摄像头 init 失败")
            return False
        logger.info("摄像头就绪")
    _camera_mode = mode
    if mode == "interval":
        global _interval_last_ms, _interval_taken, _interval_active, _gpio12_hold_ms, _interval_clear_triggered
        _interval_last_ms = 0
        _interval_taken = 0
        _interval_active = False   # 待命状态，等 GPIO13 按了才开始拍
        _gpio12_hold_ms = 0
        _interval_clear_triggered = False
    set_lcd_owner("camera")
    logger.info("摄像头模式 %s 启动. GPIO12=返回" % mode)
    return True


def exit_camera_mode(cam):
    """退出摄像头模式：关预览 + 切回 LVGL。

    界面恢复（回主菜单）由 LVGL 线程负责，避免跨线程操作 LVGL 导致重绘丢失。
    """
    global _camera_mode
    _camera_mode = None
    logger.info("退出摄像头模式，关闭预览")
    try:
        if cam is not None:
            try:
                cam.close()
            except Exception as e:
                logger.warn("cam.close 异常: %s" % e)
    except Exception:
        pass
    utime.sleep_ms(50)
    set_lcd_owner("lvgl")
    mark_restore_menu()
    logger.info("LVGL 恢复，等待 LVGL 线程回到主菜单")


# ==================================================================
# 语音 AI 接入：voice 模式下懒启动 VoiceAI.run() 线程，GPIO13=通话，
# GPIO12=退出（置退出标志并等待线程结束，防止残留录音/连接）
# ==================================================================

def _start_voice(voice):
    """懒启动语音 AI run() 线程（已在运行则忽略，避免重复线程）。"""
    global _voice_thread
    if voice is None:
        return
    if _voice_thread is not None and _voice_thread.is_running():
        return
    _voice_thread = Thread(target=voice.run)
    try:
        _voice_thread.start(stack_size=64)
        logger.info("[VOICE] 语音 AI 线程启动")
    except Exception as e:
        logger.error("[VOICE] 语音线程启动异常: %s" % e)


def _stop_voice(voice):
    """停止语音 AI run() 线程：置退出标志，等待线程结束（最多 3s）。

    run() 主循环检测到退出标志后自行清理（停止录音/重置状态）并返回。
    若 join 超时（如正阻塞在 connect 阶段），保留线程引用，等其自然结束后
    下次进入 voice 模式才会重新启动，避免同一 VoiceAI 出现并发 run()。
    """
    global _voice_thread
    if voice is not None:
        try:
            voice.on_func_key()   # 置 _exit_flag，run() 检测后退出
        except Exception:
            pass
    t = _voice_thread
    if t is not None:
        try:
            done = t.join(3)
        except Exception:
            done = False
        if done:
            _voice_thread = None
        else:
            logger.warn("[VOICE] 语音线程未在 3s 内退出（可能阻塞在 connect），等待其自然结束")
    logger.info("[VOICE] 语音 AI 线程已退出")


def camera_thread_run(cam, events_fn, voice=None, gitee=None):
    """Camera 线程主循环：处理相机模式下的按键。

    events_fn: 返回被按下 GPIO 号集合的函数（复用 InputDriver 的按键事件）。
    voice: VoiceAI 实例（voice 模式下懒启动其 run() 线程，GPIO13=通话、GPIO12=退出）。
    gitee: GiteeManager 实例（interval 模式下长按 GPIO12 清理时删云端）。
    manual 模式：GPIO13=快门拍照、GPIO12=返回。
    interval 模式：GPIO13 空闲、GPIO12 短按=返回、GPIO12 长按>=1.5s=一键清理。
    """
    logger.info("[Camera thread] 启动")
    global _camera_mode
    import usr.config as cfg      # 定时拍摄间隔/张数配置（模块已缓存，仅首次加载）
    # 长按检测：Pin 读 GPIO12 实时电平（上拉输入，按住=0，松开=1）
    # ExtInt 只管下降沿（按下瞬间），实时电平靠 Pin.read()
    from machine import Pin
    _pin_gpio12 = None
    while not _stop:
        try:
            cam_ready = (cam is not None and cam.is_ready)
            if get_lcd_owner() == "camera" and cam_ready:
                # 读按键（复用 InputDriver 事件，避免与 ExtInt 冲突）
                pressed = events_fn()
                if _camera_mode == "manual":
                    # 板载 GPIO13 = 快门拍照（事件驱动）；GPIO12=返回
                    _status = cam.status if cam is not None else "no-cam"
                    if 13 in pressed:
                        # 只有 STATUS_BUSY(1) 才是真忙；STATUS_IDLE/OK/ERROR 都可触发（take_photo 内自带 BUSY 保护）
                        if _status != 1:
                            logger.info("[MANUAL] 快门按下(13)，拍照")
                            cam.take_photo()
                        else:
                            logger.warn("[MANUAL] 快门按下但相机忙, status=%s" % _status)
                    if 12 in pressed:
                        logger.info("[MANUAL] 用户返回")
                        exit_camera_mode(cam)
                elif _camera_mode == "interval":
                    # 延时摄影：GPIO13 短按切换开始/停止连拍，
                    # GPIO12 短按=返回主菜单（连拍中会先 drain 队列），
                    # GPIO12 长按>=1.5s=一键清理定时帧（不退出，清理后可继续）。
                    # 进入 interval 模式默认待命，等 GPIO13 按了才开始拍。
                    global _interval_last_ms, _interval_taken, _interval_active, _gpio12_hold_ms, _interval_clear_triggered
                    now = utime.ticks_ms()
                    # GPIO13 短按：切换开始/停止
                    if 13 in pressed:
                        if not _interval_active:
                            # 待命 → 开始连拍
                            _interval_active = True
                            _interval_last_ms = 0   # 让下一个 tick 立即触发第一拍
                            logger.info("[INTERVAL] GPIO13 → 开始定时连拍")
                        else:
                            # 连拍中 → 停止（drain 队列但不退出 interval）
                            _interval_active = False
                            logger.info("[INTERVAL] GPIO13 → 停止定时连拍（drain 队列）")
                            try:
                                from usr._main import drain_timed_queue_for_interval_exit
                                drain_timed_queue_for_interval_exit(timeout_ms=15000)
                            except Exception as e:
                                logger.warn("[INTERVAL] drain 队列异常: %s" % e)
                    # 自动连拍逻辑（只在 _interval_active=True 时才跑）
                    if _interval_active:
                        if (cfg.TIMED_CAPTURE_COUNT == 0 or
                                _interval_taken < cfg.TIMED_CAPTURE_COUNT):
                            if (_interval_last_ms == 0 or
                                    utime.ticks_diff(now, _interval_last_ms) >=
                                    cfg.TIMED_CAPTURE_INTERVAL * 1000):
                                if cam.status != cam.STATUS_BUSY:
                                    ok = cam.take_timed_photo()
                                    if ok:
                                        _interval_last_ms = now
                                        _interval_taken += 1
                        # 拍满指定张数后自动停止（不退出，等用户操作）
                        if (cfg.TIMED_CAPTURE_COUNT > 0 and
                                _interval_taken >= cfg.TIMED_CAPTURE_COUNT):
                            _interval_active = False
                            logger.info("[INTERVAL] 已连拍 %d 张，自动停止（drain 队列）" % _interval_taken)
                            try:
                                from usr._main import drain_timed_queue_for_interval_exit
                                drain_timed_queue_for_interval_exit(timeout_ms=30000)
                            except Exception as e:
                                logger.warn("[INTERVAL] drain 队列异常: %s" % e)
                    # GPIO12 长按检测（Pin 读实时电平：上拉，按住=0 松开=1）
                    if _pin_gpio12 is None:
                        _pin_gpio12 = Pin(
                            getattr(Pin, "GPIO{}".format(cfg.KEY_FUNC_GPIO)),
                            Pin.IN, Pin.PULL_PU)
                    gpio12_held = (_pin_gpio12.read() == 0)
                    if gpio12_held:
                        _gpio12_hold_ms += 30  # tick=30ms
                    else:
                        # 松开：如果按住时间不够长 → 短按返回
                        if _gpio12_hold_ms > 0 and _gpio12_hold_ms < INTERVAL_HOLD_MS:
                            logger.info("[INTERVAL] GPIO12 短按(hold=%dms) → 返回主菜单" % _gpio12_hold_ms)
                            # 如果正在连拍，先 drain 队列再退出
                            if _interval_active:
                                _interval_active = False
                                try:
                                    from usr._main import drain_timed_queue_for_interval_exit
                                    drain_timed_queue_for_interval_exit(timeout_ms=30000)
                                except Exception as e:
                                    logger.warn("[INTERVAL] drain 队列异常: %s" % e)
                            exit_camera_mode(cam)
                        # 重置
                        _gpio12_hold_ms = 0
                        _interval_clear_triggered = False
                    # 长按触发清理（阈值 1.5s，触发后本轮按住期间只触发一次）
                    if (_gpio12_hold_ms >= INTERVAL_HOLD_MS and
                            not _interval_clear_triggered):
                        _interval_clear_triggered = True
                        logger.info("[INTERVAL] GPIO12 长按(hold=%dms) → 一键清理定时帧" %
                                    _gpio12_hold_ms)
                        try:
                            from usr._main import clear_timed_photos
                            # 清理时先停拍 + drain 队列
                            _interval_active = False
                            clear_timed_photos(gitee, cam)
                            # 清理后重置计数，让编号从 001 重新开始
                            _interval_taken = 0
                            _interval_last_ms = 0
                            # 清理完自动继续待命（用户再按 GPIO13 开始）
                        except Exception as e:
                            logger.error("[INTERVAL] 清理异常: %s" % e)
                elif _camera_mode == "voice":
                    # 懒启动语音 AI 线程（已在运行则忽略）
                    _start_voice(voice)
                    if 13 in pressed:
                        logger.info("[VOICE] 通话键: 开始/停止录音")
                        voice.on_talk_key()
                    if 12 in pressed:
                        logger.info("[VOICE] 用户返回")
                        _stop_voice(voice)
                        exit_camera_mode(cam)
        except Exception as e:
            import sys
            logger.error("[CAM] 相机线程异常: %s" % repr(e))
            sys.print_exception(e)
            utime.sleep_ms(30)
        utime.sleep_ms(30)
    logger.info("[Camera thread] 退出")