# -*- coding: UTF-8 -*-
"""协议层：WebSocket 连接 / 鉴权 / 消息收发（小智平台）。

整合自原 protocol.py / OTA_test.py / uuid.py 三个文件：
- WebSocket 连接小智 AI 平台
- OTA 换参获取 WebSocket 地址和 Token（原 OTA_test.py）
- UUID 工具（原 uuid.py，RFC 4122 v4）
- 接收线程区分 JSON 文本帧和二进制音频帧
- 会话控制：listen(start/stop)、abort
- MCP 工具交互：initialize、tools/list、tools/call
"""
import modem
import sim
import net
import urandom
import ujson as json
import request
import uwebsocket as ws
from usr.threading import Thread, Condition
from usr.logging import getLogger
from usr.config import (
    WSS_DEBUG,
    OTA_URL, BOARD_TYPE, BOARD_NAME, CARRIER,
    APP_VERSION, APP_SHA256,
)

logger = getLogger("protocol")

# ============================================================
# UUID 工具（原 uuid.py，RFC 4122 v4）
# ============================================================

_int = int      # The built-in int type
_bytes = bytes  # The built-in bytes type


class UUID(object):

    def __init__(self, bytes=None, version=None, int=None):
        if bytes is not None:
            if len(bytes) != 16:
                raise ValueError('bytes is not a 16-char string')
            assert isinstance(bytes, _bytes), repr(bytes)
            int = _int.from_bytes(bytes, "big")
        if int is not None:
            if not 0 <= int < (1 << 128):
                raise ValueError('int is out of range (need a 128-bit value)')
        if version is not None:
            if not 1 <= version <= 5:
                raise ValueError('illegal version number')
            # Set the variant to RFC 4122.
            int &= ~(0xc000 << 48)
            int |= 0x8000 << 48
            # Set the version number.
            int &= ~(0xf000 << 64)
            int |= version << 76
        self.int = int

    def __str__(self):
        hex = '%032x' % self.int
        return '%s-%s-%s-%s-%s' % (hex[:8], hex[8:12], hex[12:16], hex[16:20], hex[20:])


def uuid4():
    return UUID(bytes=bytes([urandom.getrandbits(8) for _ in range(16)]), version=4)


# ============================================================
# OTA 换参（原 OTA_test.py）
# ============================================================

class OTA(object):
    """POST 到小智平台 OTA 接口，换取 WebSocket 地址 / token / 协议版本。

    首次绑定设备时，返回里的 activation.code 就是 6 位验证码，这里会单独打印出来。
    """

    def __init__(self, mac, imei="", iccid="", csq=""):
        self.mac = mac
        self.imei = imei
        self.iccid = iccid
        self.csq = csq
        self.response = None

    def run(self):
        head = {
            "Accept-Language": "zh-CN",
            "Content-Type": "application/json",
            "User-Agent": "{}/{}".format(BOARD_NAME, APP_VERSION),
            "Device-Id": self.mac,
            "Client-Id": str(uuid4()),
        }
        ota_data = {
            "application": {"version": APP_VERSION, "elf_sha256": APP_SHA256},
            "board": {
                "type": BOARD_TYPE,
                "name": BOARD_NAME,
                "carrier": CARRIER,
                "csq": str(self.csq),
                "imei": self.imei,
                "iccid": self.iccid,
            },
        }
        logger.info("POST {} ...".format(OTA_URL))
        resp = request.post(OTA_URL, data=json.dumps(ota_data), headers=head)
        self.response = resp.json()
        act = self.response.get("activation", {})
        if act:
            logger.info("==== 首次绑定验证码: {} ====".format(act.get("code")))
        return self.response


# ============================================================
# WebSocket 协议
# ============================================================

class JsonMessage(object):

    def __init__(self, kwargs):
        self.kwargs = kwargs

    def __str__(self):
        return str(self.kwargs)

    def to_bytes(self):
        return json.dumps(self.kwargs)

    @classmethod
    def from_bytes(cls, data):
        return cls(json.loads(data))

    def __getitem__(self, key):
        return self.kwargs[key]


class RespHelper(Condition):

    def __init__(self):
        self.__ack_items = {}
        super().__init__()

    def get(self, request, timeout=None):
        """发送请求后调用，阻塞等待匹配响应；超时返回 None。"""
        self.__ack_items[request] = None
        self.wait_for(lambda: self.__ack_items[request] is not None, timeout=timeout)
        return self.__ack_items.pop(request)

    def put(self, response):
        """收到响应后调用，按 type 匹配到挂起的请求并唤醒。"""
        for request in self.__ack_items.keys():
            if not self.validate(request, response):
                continue
            self.__ack_items[request] = response
            self.notify_all()
            break

    @staticmethod
    def validate(request, response):
        return request["type"] == response["type"]


class WebSocketClient(object):

    def __init__(self):
        # 读设备信息
        self.mac = self.get_mac_address()
        imei = modem.getDevImei()
        iccid = sim.getIccid()
        csq = net.csqQueryPoll()
        # OTA 换参：拿 ws 地址 / token / 协议版本（首次绑定含 6 位验证码）
        ota = OTA(self.mac, imei=imei, iccid=iccid, csq=csq)
        resp = ota.run()
        self.host = resp["websocket"]["url"]
        self.token = resp["websocket"]["token"]
        self.protocol_version = resp["firmware"]["version"]

        self.__resp_helper = RespHelper()
        self.__recv_thread = None
        self.__audio_message_handler = None
        self.__json_message_handler = None
        self.connect_flag = False

    def __str__(self):
        return "{}(host={})".format(type(self).__name__, self.host)

    @staticmethod
    def get_mac_address():
        mac = str(UUID(int=int(modem.getDevImei())))[-12:]
        return ":".join(mac[i:i + 2] for i in range(0, 12, 2))

    @staticmethod
    def generate_uuid():
        return str(uuid4())

    @property
    def cli(self):
        c = getattr(self, "__client__", None)
        if c is None:
            raise RuntimeError("{} not connected".format(self))
        return c

    def is_state_ok(self):
        return self.cli.sock.getsocketsta() == 4

    def set_callback(self, audio_message_handler=None, json_message_handler=None):
        if audio_message_handler is not None:
            self.__audio_message_handler = audio_message_handler
        if json_message_handler is not None:
            self.__json_message_handler = json_message_handler

    def connect(self):
        c = ws.Client.connect(
            self.host,
            headers={
                "Authorization": "Bearer {}".format(self.token),
                "Protocol-Version": self.protocol_version,
                "Device-Id": self.mac,
                "Client-Id": self.generate_uuid(),
            },
            debug=WSS_DEBUG,
        )
        self.__recv_thread = Thread(target=self.__recv_thread_worker)
        self.__recv_thread.start(stack_size=64)
        setattr(self, "__client__", c)
        self.connect_flag = True
        return c

    def disconnect(self):
        c = getattr(self, "__client__", None)
        if c is not None:
            try:
                c.close()
            except Exception as e:
                logger.info("close 异常（可能已断开）: {}".format(repr(e)))
            del self.__client__
        if self.__recv_thread is not None:
            try:
                self.__recv_thread.join()
            except Exception as e:
                logger.info("join recv 异常: {}".format(repr(e)))
            self.__recv_thread = None
        self.connect_flag = False

    def reconnect(self):
        """断线重连：清理旧连接 → 重新 connect → hello 握手。

        连接异常或 hello 超时都抛异常，由调用方做指数退避重试。
        """
        self.disconnect()
        self.connect()
        resp = self.hello()
        if resp is None:
            raise RuntimeError("hello 握手超时")
        return resp

    def send(self, data):
        self.cli.send(data)

    def recv(self):
        return self.cli.recv()

    def __recv_thread_worker(self):
        while True:
            try:
                raw = self.recv()
            except Exception as e:
                logger.info("recv thread break: {}".format(repr(e)))
                self.connect_flag = False
                break
            if raw is None or raw == "":
                logger.info("websocket 断开")
                self.connect_flag = False
                break
            if isinstance(raw, str):
                # JSON 文本帧
                try:
                    msg = JsonMessage.from_bytes(raw)
                except Exception:
                    logger.warn("JSON 解析失败: {}".format(raw))
                    continue
                if msg["type"] == "hello":
                    with self.__resp_helper:
                        self.__resp_helper.put(msg)
                else:
                    self.__handle_json_message(msg)
            else:
                # 二进制音频帧
                self.__handle_audio_message(raw)

    def __handle_audio_message(self, raw):
        if self.__audio_message_handler is None:
            logger.warn("收到音频帧但未注册 audio handler")
            return
        try:
            self.__audio_message_handler(raw)
        except Exception as e:
            logger.error("handle audio failed: {}".format(repr(e)))

    def __handle_json_message(self, msg):
        if self.__json_message_handler is None:
            logger.warn("收到 JSON 消息但未注册 json handler: {}".format(msg))
            return
        try:
            self.__json_message_handler(msg)
        except Exception as e:
            logger.error("handle json failed: {}".format(repr(e)))

    def hello(self):
        req = JsonMessage({
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 100,
            },
            "features": {
                "consistent_sample_rate": True,
                "mcp": True,     # 启用 MCP 工具调用
            },
        })
        with self.__resp_helper:
            self.send(req.to_bytes())
            resp = self.__resp_helper.get(req, timeout=10)
        return resp

    def listen(self, state, mode="auto", session_id=""):
        """type=listen：state=start/stop/detect，mode=auto/manual/realtime（按键场景用 manual）。"""
        self.send(JsonMessage({
            "session_id": session_id,
            "type": "listen",
            "state": state,
            "mode": mode,
        }).to_bytes())

    def wakeword_detected(self, wakeword, session_id=""):
        """type=listen + state=detect：上报唤醒词。"""
        self.send(JsonMessage({
            "session_id": session_id,
            "type": "listen",
            "state": "detect",
            "text": wakeword,
        }).to_bytes())

    def abort(self, reason="", session_id=""):
        """type=abort：打断上一次会话/播报。"""
        self.send(JsonMessage({
            "session_id": session_id,
            "type": "abort",
            "reason": reason,
        }).to_bytes())

    # ---- MCP 工具交互 ----
    def send_mcp(self, payload, session_id=""):
        """发送 type=mcp 消息，payload 为 JSON-RPC 2.0 格式字典。"""
        self.send(JsonMessage({
            "session_id": session_id,
            "type": "mcp",
            "payload": payload,
        }).to_bytes())

    def mcp_initialize(self, req_id=1, session_id=""):
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-9-03",
                "capabilities": {"tools": {}, "notifications": {}},
                "serverInfo": {"name": "xiaozhi-camera", "version": "1.0.0"},
            },
        }
        self.send_mcp(payload, session_id)

    def mcp_tools_list(self, req_id=2, session_id=""):
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "camera.take_photo",
                        "description": "拍照并上传到云端",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                        },
                    },
                ],
            },
        }
        self.send_mcp(payload, session_id)

    def mcp_tools_call(self, tool_name, req_id="", args=None, session_id=""):
        """发送 MCP tools/call 回执（成功）。
        args 可传 dict，携带工具特定文本（如视觉分析结果）。"""
        text = {
            "camera.take_photo": "已拍照并上传到云端",
        }.get(tool_name, "操作成功")
        # 支持工具自定义回执内容（如 camera.analyze_photo 的分析结果）
        if isinstance(args, dict) and args.get("text"):
            text = args["text"]
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            },
        }
        self.send_mcp(payload, session_id)

    def mcp_tools_call_error(self, req_id="", error_msg="", session_id=""):
        """发送 MCP tools/call 回执（失败）。"""
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -1,
                "message": error_msg,
            },
        }
        self.send_mcp(payload, session_id)
