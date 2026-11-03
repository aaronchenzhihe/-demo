# -*- coding: UTF-8 -*-
"""语音控制 AI 相机 — 主入口。

只做装配：
  1. 初始化 LCD + LVGL（lvgl_ui.init）
    2. 初始化业务模块（camera_srv / net_manager / voice_ai / gitee_manager / photo_upload）
  3. 创建界面（主菜单 + 删除页）
  4. 启动线程：LVGL / Camera 按键循环 / 定时帧后台上传线程

关键架构：
  - 普通拍照帧：cam 回调 → on_photo_done 入 _pending_photo → 主循环 drain 上传上报
  - 定时拍照帧：cam 回调 → on_photo_done 分流入 _timed_queue → 专属后台线程 drain
                HTTP 上传阻塞不影响 camera 连拍节奏；退出 interval 时 drain 完再返回
  - 定时帧上传成功后立即删本地，不留存存储（用户明确需求）
  - 两类帧分流互不干扰，普通帧链路零改动
"""
import _thread
import gc
import sys
import uos
import utime
import lvgl as lv

from usr import camera_flow, camera_srv, lvgl_ui
from usr.camera_srv import CameraService
from usr.config import GITEE_TIMED_PHOTO_PATH, LOG_LEVEL
from usr.gitee_manager import GiteeManager
from usr.input import InputDriver
from usr.logging import getLogger, BasicConfig
from usr.net_manager import NetManager
from usr.photo_upload import PhotoUploader
from usr.vision import BaiduVisionAnalyzer
from usr.voice_ai import VoiceAI

logger = getLogger("main")


# ---------------------------------------------------------------------------
# 定时照片专属后台上传线程的模块级状态
# camera_flow 退出 interval 模式时需要等待队列 drain 完，必须在 main() 外可访问
# ---------------------------------------------------------------------------
_timed_queue = []           # FIFO: [(photo_name, status), ...]
_timed_lock = None          # 在 main() 里初始化为 _thread.allocate_lock()
_timed_thread = None        # 后台线程引用（QuecPython start_new_thread 返回 None，别用它判断启动状态）
_timed_thread_started = False   # ★ 独立 flag：后台线程是否已启动（drain 用这个判断）


def _remove_local_photo(photo_name):
    """删除本地照片（上传成功后调用，不留存存储）。

    与 CameraService._locate_photo 候选路径一致，遍历尝试删除。
    Returns:
        True 有路径成功删除，False 所有路径都删失败
    """
    candidates = [
        "/usr/" + photo_name,
        "/" + photo_name,
        "U:/" + photo_name,
    ]
    try:
        for p in candidates:
            try:
                uos.remove(p)
                logger.info("[timed] 已删除本地文件: %s" % p)
                return True
            except OSError:
                continue
            except Exception:
                continue
    except Exception as e:
        logger.warn("[timed] _remove_local_photo 异常: %s" % e)
    logger.warn("[timed] 未找到可删除的本地文件: %s" % photo_name)
    return False


def _run_timed_upload_loop(cam, uploader):
    """定时照片专属后台上传线程主循环（始终运行，永不退出）。

    职责：
    - drain _timed_queue：逐帧上传 + 上传成功后删本地
      - 队列为空时 sleep_ms(100) 轻轮询，不占 CPU
      - interval 退出时由 drain_timed_queue_for_interval_exit() 阻塞等队列为空即可

    Args:
        cam/uploader: main() 闭包里的业务对象引用（通过线程参数传入）
    """
    logger.info("[timed] 后台上传线程启动（始终运行）")
    drain_count = 0
    while True:
        # 取队头（加锁）
        photo_name = None
        pstatus = None
        try:
            _timed_lock.acquire()
            if _timed_queue:
                photo_name, pstatus = _timed_queue.pop(0)
        finally:
            _timed_lock.release()

        if photo_name is None:
            # 队列为空：轻轮询等待新帧入队
            utime.sleep_ms(100)
            continue

        # 处理这张定时帧的上传 + 清理
        drain_count += 1
        logger.info("[timed] 处理拍照后续 #%d: %s status=%s" % (drain_count, photo_name, pstatus))
        try:
            if pstatus == cam.STATUS_OK:
                if uploader.enabled:
                    logger.info("[timed] 开始上传 %s" % photo_name)
                    ok, url = uploader.upload(photo_name)
                    if ok and url:
                        # 本地帧会在上传后删除，持久化编号以避免重启后覆盖 Gitee 文件。
                        try:
                            camera_srv._write_persisted_max("timed_photo_", photo_name)
                        except Exception as e:
                            logger.warn("[timed] 定时帧编号持久化异常: %s" % e)
                    if ok:
                        _remove_local_photo(photo_name)
                    else:
                        logger.warn("[timed] 上传失败，保留本地兜底: %s" % photo_name)
        except Exception as e:
            logger.error("[timed] 拍照后续处理异常（已忽略）: %s" % repr(e))

        # 每处理完一张定时帧就 gc — HTTP + base64 会产生大量临时对象
        try:
            gc.collect()
        except Exception:
            pass


def drain_timed_queue_for_interval_exit(timeout_ms=30000):
    """等待定时帧后台线程把队列 drain 完后再返回（interval 模式退出时调用）。

    camera_flow 退出 interval 模式（GPIO12 手动返回 / TIMED_CAPTURE_COUNT 拍满自动停）时调用。
    后台线程始终运行，这里只轮询队列长度直到 0（或超时），确保最后一张定时帧也传完。

    Args:
        timeout_ms: 最长等待时间毫秒，超时返回 False 说明仍有残留

    Returns:
        True=队列已 drain 完，False=超时仍有残留
    """
    if not _timed_thread_started:
        logger.info("[timed] 后台线程未启动，跳过 drain")
        return True

    logger.info("[timed] 等待后台线程 drain 队列（最长 %dms）..." % timeout_ms)
    deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms)
    while True:
        try:
            _timed_lock.acquire()
            remaining = len(_timed_queue)
        finally:
            _timed_lock.release()

        if remaining == 0:
            # 多等一小段确认后台线程真的把最后一张处理完（sleep 后最后一次 pop 已经拿到）
            utime.sleep_ms(1500)
            try:
                _timed_lock.acquire()
                remaining = len(_timed_queue)
            finally:
                _timed_lock.release()
            if remaining == 0:
                logger.info("[timed] 队列已 drain 完，interval 模式可安全退出")
                return True

        if utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
            logger.warn("[timed] drain 超时，队列仍有 %d 张未处理" % remaining)
            return False

        utime.sleep_ms(200)


def clear_timed_photos(gitee, cam):
    """一键清理定时连拍照片（本地 + 云端 + 持久化计数）。

    camera_flow interval 模式下 GPIO13 按下时调用。

    Returns:
        (local_deleted, cloud_deleted)
    """
    logger.info("[INTERVAL] ★ 一键清理定时连拍照片 ★")
    # 1. 先 drain 后台队列（让上传中的帧先传完，避免被删冲突）
    try:
        drain_timed_queue_for_interval_exit(timeout_ms=10000)
    except Exception as e:
        logger.warn("[INTERVAL] drain 队列异常（忽略）: %s" % e)

    # 2. 清本地 timed_photo_* 文件
    local_del = 0
    try:
        candidates = ["/usr/", "/", "U:/"]
        for root in candidates:
            try:
                files = uos.listdir(root)
            except Exception:
                continue
            for name in files:
                if name.startswith("timed_photo_") and (
                        name.endswith(".jpg") or name.endswith(".jpeg")):
                    full = root + name
                    try:
                        uos.remove(full)
                        local_del += 1
                    except Exception as e:
                        logger.warn("[INTERVAL] 删本地 %s 失败: %s" % (full, e))
    except Exception as e:
        logger.warn("[INTERVAL] 本地清理异常: %s" % e)

    logger.info("[INTERVAL] 本地删除定时帧 %d 张" % local_del)

    # 3. 清 Gitee photos/timed/ 目录
    cloud_del = 0
    try:
        if gitee and gitee.enabled:
            d, f = gitee.delete_all_photos(sub_path=GITEE_TIMED_PHOTO_PATH)
            cloud_del = d
            logger.info("[INTERVAL] Gitee 定时帧删除: 成功=%d 失败=%d" % (d, f))
    except Exception as e:
        logger.warn("[INTERVAL] Gitee 清理异常: %s" % e)

    # 4. 重置持久化计数文件（写 0 → 下次编号从 001 重新开始）
    try:
        persist_path = "/usr/_timed_count.txt"
        try:
            uos.remove(persist_path)
        except Exception:
            pass
        logger.info("[INTERVAL] 定时帧持久化计数已重置")
    except Exception as e:
        logger.warn("[INTERVAL] 持久化重置异常: %s" % e)

    logger.info("[INTERVAL] ★ 清理完成: 本地=%d 云端=%d ★" % (local_del, cloud_del))
    return (local_del, cloud_del)


def main():
    BasicConfig.set("level", LOG_LEVEL)
    logger.info("=== 语音控制 AI 相机 ===")
    _main_start_ms = utime.ticks_ms()   # 主循环启动时间，用于周期性 RAM 监控

    # 1. 显示层
    lvgl_ui.init()
    lvgl_ui.create_menu()
    lvgl_ui.create_delete()
    lvgl_ui.create_analyze()
    lvgl_ui.create_album()  # ← 相册预览（来源选择 + 预览浏览）
    lv.scr_load(lvgl_ui.screen_menu)
    lvgl_ui.refresh_menu()

    # 2. 业务模块（摄像头暂不 init，进入模式时才 init）
    cam = CameraService()

    logger.info("连接网络...")
    net = NetManager()
    net_ok = net.wait_network_ready()

    if net_ok:
        net.enable_hot_plug_detection()
        logger.info("[net] SIM 热插拔 + dataCall 网络状态监测已注册")

    uploader = PhotoUploader()

    # 3. 拍照回调分流：普通帧入主循环队列，定时帧入定时专属队列
    #    camCapture 回调线程只做 append，立即返回，HTTP 阻塞不会污染摄像头内部线程
    global _timed_lock
    _timed_lock = _thread.allocate_lock()

    _pending_photo = []         # 普通帧 FIFO（主循环 drain）
    _pending_lock = _thread.allocate_lock()

    def on_photo_done(photo_name, status):
        """camCapture 拍照完成回调——分流入对应队列。"""
        lower = (photo_name or "").lower()
        if lower.startswith("timed_"):
            # 定时帧 → 定时专属后台上传线程（从主循环解耦，HTTP 阻塞不影响 camera 连拍节奏）
            try:
                _timed_lock.acquire()
                _timed_queue.append((photo_name, status))
            finally:
                _timed_lock.release()
            try:
                qlen = len(_timed_queue)
            except Exception:
                qlen = -1
            logger.info("[timed] 拍照完成，入定时队列: %s status=%s (队列 len=%d)" % (
                photo_name, status, qlen))
        else:
            # 普通帧 → 主循环队列（保持原有链路零改动）
            try:
                _pending_lock.acquire()
                _pending_photo.append((photo_name, status))
            finally:
                _pending_lock.release()
            try:
                qlen = len(_pending_photo)
            except Exception:
                qlen = -1
            logger.info("[main] 拍照完成，入普通队列: %s status=%s (队列 len=%d)" % (
                photo_name, status, qlen))

    cam.set_callback(on_photo_done)

    input_drv = InputDriver()
    vision = BaiduVisionAnalyzer()
    voice = VoiceAI(
        camera_service=cam,
        photo_uploader=uploader,
        vision_service=vision,
    )
    # 注：VoiceAI.run() 不在此启动，由 camera_flow 在进入 "voice" 模式时启动

    gitee = GiteeManager()
    logger.info("业务模块初始化完成. net=%s" % net_ok)

    # 4. 启动定时帧后台上传线程（始终运行，队列为空时 sleep_ms(100) 轻轮询）
    global _timed_thread, _timed_thread_started
    try:
        _timed_lock = _thread.allocate_lock()
        _timed_thread = _thread.start_new_thread(
            _run_timed_upload_loop, (cam, uploader))
        _timed_thread_started = True
        logger.info("[timed] 后台上传线程已启动")
    except Exception as e:
        logger.error("[timed] 后台上传线程启动失败: %s" % e)
        _timed_thread_started = False

    # 5. 启动 UI / Camera 按键线程
    def on_enter_mode(mode, cam_arg=None):
        return camera_flow.enter_camera_mode(mode, cam)

    tid_ui = _thread.start_new_thread(
        lvgl_ui.run_lvgl_thread,
        (cam, gitee, on_enter_mode, input_drv.pressed_set, vision),
    )
    logger.info("[main] LVGL 线程已启动, tid=%d" % tid_ui)

    tid_cam = _thread.start_new_thread(
        camera_flow.camera_thread_run,
        (cam, input_drv.pressed_set, voice, gitee))
    logger.info("[main] Camera 线程已启动, tid=%d" % tid_cam)

    # 6. 主线程：每 5s drain 普通帧队列
    #    定时帧已交由专属后台线程处理，主循环不再 drain _timed_queue
    logger.info("所有线程已启动，进入等待主循环...")
    while True:
        try:
            # drain 普通帧队列（定时帧已分流到 _timed_queue 由后台线程处理）
            while True:
                try:
                    _pending_lock.acquire()
                    if not _pending_photo:
                        break
                    photo_name, pstatus = _pending_photo.pop(0)
                finally:
                    _pending_lock.release()

                logger.info("[main] 处理普通帧后续: %s status=%s" % (photo_name, pstatus))
                try:
                    if pstatus == CameraService.STATUS_OK:
                        if uploader.enabled:
                            logger.info("[main] 开始上传 %s" % photo_name)
                            uploader.upload(photo_name)
                except Exception as e:
                    logger.error("[main] 普通帧后续处理异常（已忽略）: %s" % repr(e))

            utime.sleep(5)
            # 每 5 秒做一次 gc（主循环里 HTTP 上传 + base64 同样产生大量临时对象）
            try:
                gc.collect()
            except Exception:
                pass
            # 每 30 秒打一次 RAM watermark（观察内存趋势，提前发现 OOM）
            if utime.ticks_diff(utime.ticks_ms(), _main_start_ms) % 30000 < 5000:
                try:
                    free = sys.getfree() if hasattr(sys, "getfree") else -1
                    if free > 0:
                        logger.info("[main] RAM free=%d bytes" % free)
                except Exception:
                    pass
        except Exception as e:
            logger.error("[main] 主循环异常（已忽略，继续运行）: %s" % repr(e))
            try:
                utime.sleep(1)
            except Exception:
                pass


if __name__ == "__main__":
    main()