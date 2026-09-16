# -*- coding: utf-8 -*-
"""QTH 上云客户端：按功能 ID 上传传感器属性。"""

import utime

try:
    import ujson as json
except Exception:
    import json

try:
    import dataCall
except Exception:
    dataCall = None

try:
    from usr import Qth
    _HAS_QTH = True
except Exception:
    try:
        import Qth
        _HAS_QTH = True
    except Exception:
        try:
            from usr.Qth import __init__ as Qth
            _HAS_QTH = True
        except Exception:
            Qth = None
            _HAS_QTH = False


class QthCloudClient(object):
    """QTH 云连接与属性上报。"""

    def __init__(self, config_path="/usr/cloud_config.json"):
        self.config_path = config_path
        self.cfg = {}
        self.ready = False
        self.last_upload_tick = 0

    def _load_config(self):
        with open(self.config_path, "r") as f:
            self.cfg = json.loads(f.read())

    def _event_callback(self, event, result):
        print("[QTH] event={} result={}".format(event, result))
        self.ready = self._is_online()

    def _is_online(self):
        try:
            return bool(hasattr(Qth, "state") and Qth.state())
        except Exception:
            return False

    def _ensure_network(self):
        """尽量确保 PDP 已激活。"""
        if dataCall is None:
            return True
        max_retry = int(self.cfg.get("NET_RETRY", 8))
        for _ in range(max_retry):
            try:
                info = dataCall.getInfo(1, 0)
                if info and len(info) > 2 and info[2] and info[2][0] == 1:
                    return True
                dataCall.activate(1)
            except Exception:
                pass
            utime.sleep(1)
        return False

    @staticmethod
    def _ensure_file(path, content):
        """缺失时创建占位文件，避免底层库打印 ENOENT。"""
        try:
            f = open(path, "r")
            f.close()
        except Exception:
            try:
                f = open(path, "w")
                f.write(content)
                f.close()
            except Exception:
                pass

    def _ensure_runtime_files(self):
        # 某些 Qth 版本会读取这些文件，缺失时会输出 read file fail/ENOENT
        self._ensure_file("/usr/secret.json", "{}")
        self._ensure_file("/usr/qth_config.ini", "[qth]\n")

    def _wait_online(self):
        """等待云连接建立。"""
        wait_sec = int(self.cfg.get("QTH_CONNECT_WAIT_SEC", 30))
        for _ in range(wait_sec):
            if self._is_online():
                return True
            utime.sleep(1)
        return False

    def _reconnect(self):
        """离线时尝试重连。"""
        try:
            Qth.start()
        except Exception:
            pass
        self.ready = self._wait_online()
        return self.ready

    def init(self):
        """初始化并启动 QTH。"""
        # 1) 先确认 Qth 库可导入（固件或 /usr 下必须存在）
        if not _HAS_QTH:
            print("[QTH] Qth module not found in firmware")
            return False
        try:
            # 2) 读取云配置（产品信息、功能ID、上报间隔等）
            self._load_config()

            # 3) 创建必要运行时文件，避免底层 SDK 报 ENOENT 噪声
            self._ensure_runtime_files()

            # 4) 确保蜂窝数据链路已就绪（PDP active）
            if not self._ensure_network():
                print("[QTH] network not ready")
                self.ready = False
                return False

            # 5) 初始化 SDK，并设置产品 PK/PS
            Qth.init()
            Qth.setProductInfo(self.cfg["QTH_PRODUCT_KEY"], self.cfg["QTH_PRODUCT_SECRET"])

            # 6) 注册设备事件回调，用于感知连接状态变化
            Qth.setEventCb({"devEvent": self._event_callback})

            # 7) 服务器策略：
            #    - USE_DEFAULT_QTH_SERVER=True：走 SDK 默认服务器
            #    - 否则使用 cloud_config.json 里的 QTH_SERVER
            use_default_server = bool(self.cfg.get("USE_DEFAULT_QTH_SERVER", False))
            server = self.cfg.get("QTH_SERVER", "")
            if use_default_server or not server:
                print("[QTH] use SDK default server")
            else:
                Qth.setServer(server)
                print("[QTH] use custom server={}".format(server))

            # 8) 发起连接并等待 online，只有在线才认为 init 成功
            Qth.start()
            self.ready = self._wait_online()
            print("[QTH] started, online={}".format(self.ready))
            return self.ready
        except Exception as e:
            print("[QTH] init err:", e)
            self.ready = False
            return False

    def upload_properties(self, values):
        """按功能 ID 上传属性。

        约定:
        - 12 温度 float
        - 15 湿度 float
        - 16 光照强度 int
        - 17 经度 double
        - 18 纬度 double
        """
        if not values:
            return False

        # 每次上报前检查在线状态，避免本地假成功
        self.ready = self._is_online()
        if not self.ready:
            self.ready = self._reconnect()
            if not self.ready:
                print("[QTH] skip upload: offline")
                return False

        now = utime.ticks_ms()
        interval_sec = int(self.cfg.get("UPLOAD_INTERVAL_SEC", 5))
        if utime.ticks_diff(now, self.last_upload_tick) < interval_sec * 1000:
            return False

        payload = {}
        if values.get("temp") is not None:
            payload[int(self.cfg.get("TSL_TEMP_FUNC_ID", 12))] = float(values.get("temp"))
        if values.get("humi") is not None:
            payload[int(self.cfg.get("TSL_HUMI_FUNC_ID", 15))] = float(values.get("humi"))
        if values.get("light") is not None:
            payload[int(self.cfg.get("TSL_LIGHT_FUNC_ID", 16))] = int(values.get("light"))
        if values.get("lon") is not None:
            payload[int(self.cfg.get("TSL_LON_FUNC_ID", 17))] = float(values.get("lon"))
        if values.get("lat") is not None:
            payload[int(self.cfg.get("TSL_LAT_FUNC_ID", 18))] = float(values.get("lat"))

        if not payload:
            return False
        
        # 获取上报模式，默认为 1 (立即上报)
        # 其他值：不需要确认的普通上报
        mode = int(self.cfg.get("TSL_MODE", 1))

        try:
            ok = Qth.sendTsl(mode, payload)
            if ok:
                self.last_upload_tick = now
                print("[QTH] upload ok payload={}".format(payload))
            else:
                print("[QTH] upload failed")
            return bool(ok)
        except Exception as e:
            print("[QTH] upload err:", e)
            return False
