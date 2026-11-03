# -*- coding: UTF-8 -*-
"""语音 AI 交互管理模块：状态机 + WebSocket 通信 + 音频录制/播放 + MCP 拍照。

状态机：
  IDLE        → 空闲，等待按键/打断
  LISTENING   → 录音中，opus_read 上行
  THINKING    → 等待 AI 回复
  SPEAKING    → TTS 播放中，opus_write 下行
  RECONNECTING→ 断线重连（指数退避）

MCP 工具：
  camera.take_photo  → 拍照 → Gitee 上传 → 云端上报 → 回复 AI

与 _main.py 的关系：
  - VoiceAI 负责语音交互全流程
  - 拍照完成后通过回调通知 _main.py 做状态显示等
"""
import utime
import ujson as json
import gc
import net
from usr.protocol import WebSocketClient
from usr.audio import AudioManager
from usr.threading import Thread
from usr.config import (
    VOICE_TALK_KEY_GPIO, VOICE_FUNC_GPIO,
    RECONNECT_BACKOFF_MIN, RECONNECT_BACKOFF_MAX,
)
from usr.logging import getLogger

logger = getLogger("voice")


class VoiceAI(object):
    """语音 AI 交互管理。

    用法：
        voice = VoiceAI(camera_service, photo_uploader)
        voice.set_callbacks(on_photo_done=handler)
        voice.run()
    """

    def __init__(self, camera_service=None, photo_uploader=None, vision_service=None):
        self.camera = camera_service
        self.uploader = photo_uploader
        self.vision = vision_service  # 视觉分析器（百度图像理解）

        # 状态机
        self.state = "IDLE"          # IDLE / LISTENING / THINKING / SPEAKING / RECONNECTING

        # 组件（在 run() 中初始化）
        self.__protocol = None
        self.__audio = None
        self.__opus_ready = False

        # 录音线程
        self.__recording = False
        self.__record_thread = None

        # 音频播放统计
        self.__audio_frames = 0
        self.__audio_error_count = 0

        # 按键标志（中断回调置标志，主循环处理）
        self.__key_down_flag = False
        self.__key_up_flag = False
        self.__func_key_flag = False

        # 外部回调
        self.__on_photo_done = None

        # 拍照状态（防止重复触发）
        self.__photo_in_progress = False

        # 菜单模式退出标志（按 GPIO12 退出语音模式返回主菜单）
        self._exit_flag = False

    def set_callbacks(self, on_photo_done=None):
        """注册外部回调。"""
        if on_photo_done is not None:
            self.__on_photo_done = on_photo_done

    # ==================================================================
    # 对外按键接口（按键统一由 UI / Camera 线程按界面分发，
    #  VoiceAI 不直接消费全局按键，仅暴露方法供外部在语音拍照界面内驱动）
    # ==================================================================
    def on_talk_key(self):
        """通话键按下（GPIO13）：切换录音状态。

        - 当前 IDLE/THINKING/SPEAKING → 开始录音
        - 当前 LISTENING → 停止录音
        """
        if self.state == "LISTENING":
            self.__key_up_flag = True
        else:
            self.__key_down_flag = True

    def on_func_key(self):
        """功能键按下（GPIO12）：退出语音模式返回主菜单。"""
        self._exit_flag = True
        logger.info("功能键按下，退出语音模式")

    # ==================================================================
    # 状态机动作
    # ==================================================================
    def _start_listening(self):
        """开始录音上行。"""
        if not self.__opus_ready:
            logger.error("音频未就绪（无 Opus），无法上行")
            return
        if self.state == "LISTENING":
            logger.warn("已在录音，忽略按下")
            return

        # 打断：SPEAKING/THINKING 时按下，先 abort
        if self.state in ("SPEAKING", "THINKING"):
            self.__protocol.abort()
            logger.info("打断（{} → abort）".format(self.state))

        self.__protocol.listen("start", mode="manual")
        self.state = "LISTENING"
        self.__recording = True
        self.__record_thread = Thread(target=self.__record_loop)
        self.__record_thread.start(stack_size=64)
        logger.info("LISTENING start")

    def _stop_listening(self):
        """停止录音上行。"""
        if self.state != "LISTENING":
            return
        self.__recording = False
        if self.__record_thread is not None:
            self.__record_thread.join()
            self.__record_thread = None
        self.__protocol.listen("stop", mode="manual")
        self.state = "THINKING"
        logger.info("LISTENING stop → THINKING")

    def _reconnect(self):
        """断线重连：指数退避 1s/2s/4s/8s… 上限 30s。"""
        if self.state == "RECONNECTING":
            return
        self.state = "RECONNECTING"
        logger.warn("连接断开，开始重连")
        self.__recording = False
        if self.__record_thread is not None:
            self.__record_thread.join()
            self.__record_thread = None

        backoff = RECONNECT_BACKOFF_MIN
        while not self._exit_flag:
            utime.sleep(backoff)
            try:
                self.__protocol.reconnect()
            except Exception as e:
                logger.error("重连失败: {}，{}s 后重试".format(repr(e), backoff))
                if backoff * 2 <= RECONNECT_BACKOFF_MAX:
                    backoff = backoff * 2
                else:
                    backoff = RECONNECT_BACKOFF_MAX
                continue
            self.state = "IDLE"
            gc.collect()
            logger.info("重连成功，回到 IDLE")
            return
        logger.info("重连中断（用户退出），退出语音模式")

    def _rebuild_audio(self):
        """音频异常重建。"""
        self.__audio_error_count = 0
        logger.warn("音频层异常，重建 AudioManager")
        if self.__audio is not None:
            try:
                self.__audio.close_opus()
            except Exception:
                pass
        try:
            self.__audio = AudioManager()
            self.__audio.open_opus()
            self.__opus_ready = True
            self.__audio_frames = 0
            logger.info("音频重建成功")
        except Exception as e:
            self.__opus_ready = False
            logger.error("音频重建失败: {}".format(repr(e)))

    # ==================================================================
    # 录音线程
    # ==================================================================
    def __record_loop(self):
        """录音上传线程：LISTENING 期间持续 opus_read → 二进制帧上行。"""
        while self.__recording:
            if not self.__protocol.connect_flag:
                logger.warn("连接断开，停止上行")
                break
            try:
                data = self.__audio.opus_read()
                if data:
                    self.__protocol.send(data)
            except Exception as e:
                logger.error("录音异常: {}".format(repr(e)))
                break
            utime.sleep_ms(5)
        logger.info("录音线程退出")

    # ==================================================================
    # 消息回调（recv 线程调用）
    # ==================================================================
    def on_audio_message(self, raw):
        """下行二进制音频帧：Opus 解码 → 播放。"""
        if not self.__opus_ready:
            return
        if self.state != "SPEAKING":
            return
        try:
            ret = self.__audio.opus_write(raw)
            self.__audio_error_count = 0
            if self.__audio_frames < 3:
                logger.info("音频帧 len={} write ret={}".format(len(raw), ret))
                self.__audio_frames += 1
            elif ret == -1:
                logger.warn("音频帧 write ret=-1 len={}".format(len(raw)))
        except Exception as e:
            logger.error("播放失败: {}".format(repr(e)))
            self.__audio_error_count += 1
            if self.__audio_error_count >= 3:
                self._rebuild_audio()

    def on_json_message(self, msg):
        """JSON 文本帧：按 type 分发。"""
        handler = getattr(self, "handle_{}_message".format(msg["type"]), None)
        if handler is None:
            logger.warn("未知类型 {}: {}".format(msg["type"], msg.kwargs))
            return
        try:
            handler(msg)
        except Exception as e:
            logger.error("handle {} failed: {}".format(msg["type"], repr(e)))

    # ==================================================================
    # JSON 消息 Handler
    # ==================================================================
    def handle_tts_message(self, msg):
        state = msg["state"]
        if state == "start":
            if self.state == "LISTENING":
                logger.info("TTS start 忽略（打断后录音中）")
                return
            self.state = "SPEAKING"
            self.__audio_frames = 0
            logger.info("TTS start")
        elif state == "stop":
            if self.state != "SPEAKING":
                logger.info("TTS stop 忽略（当前 {}）".format(self.state))
                return
            self.state = "IDLE"
            gc.collect()
            logger.info("TTS stop → IDLE")

    def handle_stt_message(self, msg):
        text = msg.kwargs.get("text", "")
        if text:
            logger.info("STT: {}".format(text))

    def handle_llm_message(self, msg):
        text = msg.kwargs.get("text", "")
        emotion = msg.kwargs.get("emotion", "")
        logger.info("LLM: {} emotion={}".format(text, emotion))

    def handle_mcp_message(self, msg):
        """MCP 工具调用（JSON-RPC 2.0）：initialize / tools/list / tools/call。"""
        payload = msg.kwargs.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception as e:
                logger.warn("MCP payload 解析失败: {}".format(repr(e)))
                return
        method = payload.get("method", "")
        req_id = payload.get("id", 1)
        logger.info("MCP method={}".format(method))

        if method == "initialize":
            self.__protocol.mcp_initialize(req_id=req_id)
        elif method == "tools/list":
            self.__protocol.mcp_tools_list(req_id=req_id)
        elif method == "tools/call":
            params = payload.get("params", {})
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            self._handle_mcp_tools_call(name, req_id, arguments)
        else:
            logger.warn("MCP 未知 method: {}".format(method))

    def _handle_mcp_tools_call(self, tool_name, req_id, arguments):
        """处理 MCP tools/call：拍照指令。"""
        if tool_name == "camera.take_photo":
            if self.__photo_in_progress:
                logger.warn("拍照已在进行中，忽略")
                self.__protocol.mcp_tools_call_error(
                    req_id=req_id, error_msg="拍照中，请稍后")
                return
            if self.camera is None:
                logger.warn("摄像头未就绪")
                self.__protocol.mcp_tools_call_error(
                    req_id=req_id, error_msg="摄像头未就绪")
                return

            self.__photo_in_progress = True
            logger.info("MCP 指令: 拍照")
            self.__take_photo_with_mcp_reply(req_id)
        elif tool_name == "camera.analyze_photo":
            if self.camera is None or self.vision is None:
                logger.warn("视觉分析不可用（摄像头或视觉服务未就绪）")
                self.__protocol.mcp_tools_call_error(
                    req_id=req_id, error_msg="视觉分析服务不可用")
                return
            # 分析最近一张照片
            logger.info("MCP 指令: 分析最近照片")
            t = Thread(target=self.__analyze_photo_worker,
                       args=(req_id, arguments))
            t.start(stack_size=64)
        elif tool_name == "self.setvolume_down()":
            if self.__audio:
                vol = self.__audio.volume_down()
                logger.info("音量-: {}".format(vol))
        elif tool_name == "self.setvolume_up()":
            if self.__audio:
                vol = self.__audio.volume_up()
                logger.info("音量+: {}".format(vol))
        elif tool_name == "self.setvolume_close()":
            if self.__audio:
                self.__audio.mute()
                logger.info("静音")
        elif tool_name == "self.setvolume()":
            if self.__audio:
                try:
                    v = int(arguments.get("volume", self.__audio.volume))
                except Exception:
                    v = self.__audio.volume
                vol = self.__audio.set_volume(v)
                logger.info("音量设置: {}".format(vol))
        else:
            logger.warn("MCP 未知工具: {}".format(tool_name))

    def __take_photo_with_mcp_reply(self, req_id):
        """拍照 + 上传 + 回复 MCP。在新线程中执行以避免阻塞语音交互。"""
        t = Thread(target=self.__photo_worker, args=(req_id,))
        t.start(stack_size=64)

    def __photo_worker(self, req_id):
        """拍照工作线程：拍照 → Gitee 上传 → 云端上报 → MCP 回复。"""
        photo_name = None
        success = False
        try:
            # 1. 拍照
            logger.info("拍照中...")
            ok = self.camera.take_photo()
            if not ok:
                logger.error("拍照触发失败")
                self.__protocol.mcp_tools_call_error(
                    req_id=req_id, error_msg="拍照触发失败")
                self.__photo_in_progress = False
                return

            # 等待拍照完成（最多 10s），用 last_photo 判断是否成功
            # 注意：_on_capture 回调中 _main.on_photo_done 会把 status 重置为 IDLE
            # 所以不能用 status==OK 判断，改用 last_photo 是否有值
            for _ in range(100):
                if self.camera.last_photo is not None:
                    break
                utime.sleep_ms(100)
            else:
                logger.error("拍照超时")
                self.__protocol.mcp_tools_call_error(
                    req_id=req_id, error_msg="拍照超时")
                self.__photo_in_progress = False
                return

            photo_name = self.camera.last_photo
            logger.info("拍照成功: {}".format(photo_name))

            # 上传和云端上报由 _main.py 的 on_photo_done 回调处理
            # 这里只回复 MCP 成功

            # 2. MCP 回复
            self.__protocol.mcp_tools_call(tool_name="camera.take_photo",
                                           req_id=req_id)
            success = True

        except Exception as e:
            logger.error("拍照流程异常: {}".format(repr(e)))
            self.__protocol.mcp_tools_call_error(
                req_id=req_id, error_msg=str(e))
        finally:
            self.__photo_in_progress = False
            # 通知外部回调（如 LCD 显示）
            if self.__on_photo_done:
                try:
                    self.__on_photo_done(photo_name, success)
                except Exception as e:
                    logger.error("外部回调异常: {}".format(repr(e)))

    def __analyze_photo_worker(self, req_id, arguments):
        """视觉分析工作线程：读取最近照片 → 百度图像理解 → MCP 回执。"""
        try:
            photo_name = self.camera.last_photo
            if not photo_name:
                logger.error("无最近照片可分析")
                self.__protocol.mcp_tools_call_error(
                    req_id=req_id, error_msg="暂无照片，请先拍照")
                return

            question = "这张图片里有什么？"
            if isinstance(arguments, dict) and arguments.get("question"):
                question = arguments["question"]
            # 百度接口要求 ≤100 字符
            question = question[:100]

            logger.info("视觉分析开始: 照片={} 问题={}".format(photo_name, question))
            desc = self.vision.analyze(photo_name, question)
            if not desc:
                logger.error("视觉分析失败: {}".format(photo_name))
                self.__protocol.mcp_tools_call_error(
                    req_id=req_id, error_msg="视觉分析失败，请稍后重试")
                return

            self.__protocol.mcp_tools_call(
                tool_name="camera.analyze_photo", req_id=req_id,
                args={"text": desc})
        except Exception as e:
            logger.error("视觉分析异常: {}".format(repr(e)))
            self.__protocol.mcp_tools_call_error(
                req_id=req_id, error_msg=str(e))

    def handle_iot_message(self, msg):
        logger.info("IoT: {}".format(msg.kwargs))

    def handle_alert_message(self, msg):
        alert = msg.kwargs.get("alert", {})
        if isinstance(alert, dict):
            text = alert.get("text", "")
        else:
            text = msg.kwargs.get("text", "")
        logger.info("ALERT: {}".format(text if text else msg.kwargs))

    # ==================================================================
    # 启动
    # ==================================================================
    def run(self):
        """启动语音 AI 交互。

        1. WebSocket 连接 + hello 握手
        2. 打开 Opus 编解码
        3. 注册按键回调
        4. 进入主循环
        """
        # 1. WebSocket 连接
        self.__protocol = WebSocketClient()
        self.__protocol.set_callback(
            audio_message_handler=self.on_audio_message,
            json_message_handler=self.on_json_message)

        try:
            self.__protocol.connect()
            resp = self.__protocol.hello()
        except Exception as e:
            logger.error("连接/握手异常: {}".format(repr(e)))
            return False

        if resp is not None:
            logger.info("WebSocket 连接成功，hello 握手 OK")
        else:
            logger.error("hello 握手失败（10s 超时）")
            return False

        # 2. 音频：打开 Opus
        self.__audio = AudioManager()
        try:
            self.__audio.open_opus()
            self.__opus_ready = True
            logger.info("open_opus OK（AI 固件）")
        except Exception as e:
            logger.error("open_opus 失败：{}（标准固件无 Opus，上行语音不可用）".format(repr(e)))

        # 3. 主循环
        # 注意：不在这里自消费全局按键，按键统一由 UI / Camera 线程按界面分发，
        #       VoiceAI 仅暴露 on_talk_key() / on_func_key() 供外部（语音拍照界面内）驱动。
        logger.info("语音 AI 主循环开始")
        self._exit_flag = False
        while not self._exit_flag:

            # ---- 处理状态机按键标志 ----
            if self.__key_down_flag:
                self.__key_down_flag = False
                self._start_listening()
            if self.__key_up_flag:
                self.__key_up_flag = False
                self._stop_listening()
            if self.__func_key_flag:
                self.__func_key_flag = False
                logger.info("功能键按下")

            # ---- 断线检测 ----
            if not self.__protocol.connect_flag:
                self._reconnect()

            utime.sleep_ms(10)

        # 退出语音模式：清理状态
        logger.info("语音 AI 主循环退出")
        # 如果正在录音，先停止
        if self.state == "LISTENING":
            self._stop_listening()
        # 重置状态
        self.state = "IDLE"
        self.__recording = False
        return True