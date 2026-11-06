# -*- coding: UTF-8 -*-
"""
小智 AI 对讲 —— EC600M 开发板按键触发版

按键说明(S4 = KEY1 = GPIO13):
  - 按下      : 开始讲话(录音并上传)
  - 松开      : 讲话完成(停止上传, 等待 AI 回复)
  - AI 讲话时按下: 打断并停止 AI 讲话
"""
import utime
from machine import Pin
from misc import PWM_V2
from usr.protocol import WebSocketClient
from usr.utils import AudioManager, NetManager
from usr.camera_tools import CameraTools
from usr.threading import Thread, Event, Condition
from usr.logging import getLogger
import ujson as json
from audio import TTS


logger = getLogger(__name__)



class Application(object):

    def __init__(self):
        # 初始化音频管理(功放 CTRL = PIN40 = GPIO9)
        self.audio_manager = AudioManager()

        # 初始化网络管理, 并阻塞等待驻网就绪
        # (WebSocketClient 构造时立即发起 OTA HTTPS 请求, 未联网会 ETIMEDOUT)
        self.net_manager = NetManager()
        logger.info("waiting for network ready ...")
        self.net_manager.wait_network_ready()

        # 初始化协议(构造时请求 OTA, 联网瞬时抖动则重试)
        self.__protocol = self.__init_protocol()
        self.__protocol.set_callback(
            audio_message_handler=self.on_audio_message,
            json_message_handler=self.on_json_message
        )

        # 按键触发相关状态
        self.__ai_speaking = False    # AI 是否正在播报
        self.__talk_worker = None

        # S4 = KEY1 = PIN60 = GPIO13: 按下讲话 / 松开结束 / AI 讲话时按下打断
        # 按键另一端接 GND(原理图第 6 页), 上拉输入: 按下读 0、松开读 1
        self.key1 = Pin(Pin.GPIO13, Pin.IN, Pin.PULL_PU)
        self.tts = TTS(0)
        self.light_pwm = PWM_V2(PWM_V2.PWM2, 100.0, 15)

        # 摄像头 MCP 工具(监控/拍照)，懒加载，首次调用时才上电初始化
        self.camera_tools = CameraTools()

    def light_off(self):
        # 按用户给定参数: 关灯 -> 占空比10
        self.light_pwm.open(100.0, 10)
        logger.info("light off")

    def light_on(self):
        # 按用户给定参数: 开灯 -> 占空比20
        self.light_pwm.open(100.0, 20)
        logger.info("light on")

    def __init_protocol(self, retry=3):
        """构造 WebSocketClient(内部请求 OTA), 失败重试。"""
        for i in range(retry):
            try:
                return WebSocketClient()
            except Exception as e:
                logger.error("OTA init failed ({}/{}): {}".format(i + 1, retry, repr(e)))
                utime.sleep(2)
        # 多次失败: 做一次联网自检, 帮助定位原因(数据链路/定向卡/APN)
        self.net_manager.diagnose()
        raise RuntimeError("OTA/protocol init failed, please check network")

    # ==================== 按键触发(S4 = KEY1) ====================

    def __key1_pressed(self):
        # S4/KEY1 另一端接 GND, 低电平有效: 按下返回 True
        return self.key1.read() == 0

    def __is_connected(self):
        try:
            return self.__protocol.connect_flag and self.__protocol.is_state_ok()
        except Exception:
            return False

    def __ensure_connected(self):
        """确保 websocket 已连接, 未连接则重连并握手。"""
        if self.__is_connected():
            return True
        try:
            self.__protocol.disconnect()
        except Exception:
            pass
        try:
            self.__protocol.connect()
            self.__protocol.connect_flag = True
            self.__protocol.hello()
        except Exception as e:
            logger.error("connect failed: {}".format(repr(e)))
            return False
        return self.__is_connected()

    def __talk_worker_handler(self):
        """常驻线程: 轮询 KEY1 状态, 按住时录音上传, 松开时结束。"""
        logger.debug("talk worker enter")
        while True:
            # 等待按下(KEY1 低电平有效)
            while not self.__key1_pressed():
                utime.sleep_ms(30)
            if not self.__ensure_connected():
                logger.warn("network not ready, ignore this talk")
                while self.__key1_pressed():  # 等松开, 避免重复触发
                    utime.sleep_ms(30)
                continue
            try:
                # 按下即打断可能正在播报的 AI, 再开始新的监听(manual: 松开才结束)
                self.__protocol.abort()
                self.__ai_speaking = False
                self.__protocol.listen("start", mode="manual")
                logger.info("talk start")
                while self.__key1_pressed():
                    if not self.__is_connected():
                        break
                    data = self.audio_manager.opus_read()
                    self.__protocol.send(data)
                    utime.sleep_ms(5)
                self.__protocol.listen("stop")
                logger.info("talk stop")
            except Exception as e:
                logger.warn("talk cycle error: {}".format(repr(e)))

    def on_audio_message(self, raw):
        # 收到 AI 音频(opus), 解码播放
        self.audio_manager.opus_write(raw)

    def on_json_message(self, msg):
        # 服务器可能下发协议里定义的任意类型(stt/tts/llm/mcp/system/alert/...)，
        # 缺少对应 handle_xxx_message 时不应崩溃，否则异常会被 protocol 层以 debug 吞掉。
        handler = getattr(self, "handle_{}_message".format(msg["type"]), None)
        if handler is None:
            logger.warn("unhandled message type '{}': {}".format(msg["type"], msg))
            return
        return handler(msg)

    def handle_stt_message(self, msg):
        # 语音识别结果(用户说的话)
        logger.info("STT: {}".format(msg.kwargs.get("text", "")))

    def handle_tts_message(self, msg):
        state = msg["state"]
        if state == "start":
            self.__ai_speaking = True
        elif state == "stop":
            self.__ai_speaking = False
        else:
            pass

#"happy" "cool"  "angry"  "think"
    def handle_llm_message(self, msg):
        # 表情/情感消息, 本板无屏幕, 忽略
        pass
    
    def handle_mcp_message(self, msg):
        print("msg: ", msg)
        data=msg.to_bytes()

        # 解析JSON字符串为字典
        data_dict = json.loads(data)
        id=1
        # 提取method内容
        method = data_dict['payload']['method']
        # arguments = data_dict['payload']["arguments"]
        if 'id' in data_dict['payload']:
            id=data_dict['payload']['id']
        print("MCP请求: ",method)
        if method == "initialize":
            self.__protocol.mcp_initialize()
        elif method == "tools/list":
            self.__protocol.mcp_tools_list()
        elif method =="tools/call":
            handle =data_dict['payload']['params']['name']
            tool_args = data_dict['payload']["params"].get("arguments", {})
            result_text = None

            if handle == "self.setvolume_down()":     
                print("当前音量大小",self.audio_manager.setvolume_down())
            elif handle == "self.setvolume_up()":
                print("当前音量大小",self.audio_manager.setvolume_up())
            elif handle == "self.setvolume_close()":
                print("当前音量大小",self.audio_manager.setvolume_close())
            elif handle == "self.setvolume()":
                arguments = data_dict['payload']["params"]["arguments"]["volume"]
                print("当前音量大小",arguments,self.audio_manager.setvolume(arguments))
            elif handle == "self.new_name()":
                arguments = data_dict['payload']["params"]["arguments"]["name"]
                print("neme:",self.audio_manager.new_name(arguments))
            elif handle == "self.light_off()":
                self.light_off()
                print("已关灯")
            elif handle == "self.light_on()":
                self.light_on()
                print("已开灯")
            elif handle in CameraTools.NAMES:
                result_text = self.camera_tools.handle(handle, tool_args)
                print("摄像头工具:", handle, "->", result_text)
            self.__protocol.mcp_tools_call(tool_name=handle, req_id=id, args=result_text)
        # raise NotImplementedError("handle_mcp_message not implemented")
        
    def handle_iot_message(data, msg):
        pass
        # raise NotImplementedError("handle_iot_message not implemented")
    
    def handle_error_message(data, msg):
        pass
        # raise NotImplementedError("handle_error_message not implemented")

    def handle_alert_message(self, msg):
        # 服务器下发的告警/提示(设备未激活、额度不足、内容审核、服务异常等都走这里)。
        # 之前缺少此方法导致 AttributeError, 告警内容被 debug 吞掉, 表现为"服务器无回应"。
        status = msg.kwargs.get("status", "")
        message = msg.kwargs.get("message", "")
        emotion = msg.kwargs.get("emotion", "")
        logger.warn("server ALERT -> status={}, message={}, emotion={}".format(status, message, emotion))

    def __get_activation_code(self):
        """设备未绑定时返回设备码, 已绑定返回 None。"""
        try:
            resp = getattr(self.__protocol.ota, "response", None)
            if isinstance(resp, dict) and resp.get("activation"):
                return resp["activation"].get("code", "")
        except Exception as e:
            logger.warn("get activation code failed: {}".format(repr(e)))
        return None

    def __show_activation(self, code):
        """突出打印设备码与绑定流程(需到 xiaozhi.me 绑定)。"""
        star = "*" * 48
        logger.info(star)
        logger.info("设备未绑定! 请登录小智控制台绑定设备码:")
        logger.info("1. 网页登录小智控制台 XiaoZhi AI - 小智AI: https://xiaozhi.me/")
        logger.info("2. 点击进入控制台")
        logger.info("3. 点击添加设备, 输入设备码:")
        logger.info(">>>>>>>>>>  {}  <<<<<<<<<<".format(code))
        logger.info("绑定成功后重新运行本程序即可对讲")
        logger.info(star)

    def __wait_for_activation(self, code):
        """设备未绑定: 卡在此处死循环, 每 5 秒提示一次绑定并检查网络状态。"""
        while True:
            self.__show_activation(code)
            logger.info("网络状态: {}".format(self.net_manager.net_status()))
            utime.sleep(5)

    def play_tts(self, text):
        self.audio_manager.pa_on()
        self.tts.play(2, 0, 2, text)

    def run(self):
        self.audio_manager.open_opus()
        # 未绑定则卡在绑定提示, 不连接对话服务、不进入讲话流程
        code = self.__get_activation_code()
        if code:
            self.__wait_for_activation(code)
            return
        # 建立并保持 websocket 长连接
        self.__ensure_connected()
        # 启动按键录音上传常驻线程
        self.__talk_worker = Thread(target=self.__talk_worker_handler)
        self.__talk_worker.start(stack_size=64)
        logger.info("ready: 按住 S4(KEY1) 讲话, 松开结束; AI 播报时按下可打断")
        self.play_tts("网络连接成功")



if __name__ == "__main__":
    app = Application()
    app.run()