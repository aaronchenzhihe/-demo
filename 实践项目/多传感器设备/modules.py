# -*- coding: utf-8 -*-
"""可插拔功能模块层：统一接口，便于快速启用 / 去除功能。

每个模块只需实现三件事：
  - NAME             : 模块名（打印/调试用）
  - _setup() -> bool : 创建底层驱动，成功返回 True
  - sample() -> list : 返回若干显示行 [(text, color), ...]

主程序只依赖 SensorModule 这套接口，不关心内部实现。
新增功能 = 写一个子类；启用/去除功能 = 改 _main.py 的 MODULES 列表。
"""

from usr.st7789 import WHITE, GRAY, RED, YELLOW, ORANGE, GREEN


class SensorModule(object):
    """所有功能模块的基类，统一 init / sample 流程。"""

    NAME = "module"

    def __init__(self, **cfg):
        self.cfg = cfg          # 该模块的配置参数
        self.ok = False         # 初始化是否成功
        self.driver = None      # 底层驱动实例

    def init(self):
        """统一初始化入口：捕获异常，单模块失败不影响其它。"""
        try:
            self.ok = bool(self._setup())
        except Exception as e:
            print("[INIT] {} FAIL: {}".format(self.NAME, e))
            self.ok = False
        return self.ok

    def _setup(self):
        """子类实现：创建驱动并返回 True/False。"""
        raise NotImplementedError

    def sample(self):
        """子类实现：返回 [(text, color), ...] 显示行列表。"""
        raise NotImplementedError


class TempHumiModule(SensorModule):
    """温湿度（AHT20 / I2C1）。"""

    NAME = "AHT20"

    def __init__(self, **cfg):
        super().__init__(**cfg)
        self.last_temp = None
        self.last_humi = None

    def _setup(self):
        from usr.aht20 import AHT20
        self.driver = AHT20()
        return True

    def sample(self):
        if not self.ok:
            self.last_temp = None
            self.last_humi = None
            return [("Temp: N/A", GRAY), ("Humi: N/A", GRAY)]
        t, h = self.driver.read()
        if t is None:
            self.last_temp = None
            self.last_humi = None
            return [("Temp: err ", RED), ("Humi: err ", RED)]
        self.last_temp = float(t)
        self.last_humi = float(h)
        print("[DATA] Temp={}C Humi={}%".format(t, h))
        return [("Temp:{:>5.1f}C ".format(t), YELLOW),
                ("Humi:{:>5.1f}% ".format(h), YELLOW)]


class LightModule(SensorModule):
    """光照强度（光敏电阻 GT36528 / ADC0）。"""

    NAME = "Light"

    def __init__(self, **cfg):
        super().__init__(**cfg)
        self.last_mv = None
        self.last_pct = None

    def _setup(self):
        from usr.photoresistor import Photoresistor
        self.driver = Photoresistor()
        return True

    def sample(self):
        if not self.ok:
            self.last_mv = None
            self.last_pct = None
            return [("Lux : N/A", GRAY)]
        mv, pct = self.driver.read()
        self.last_mv = mv
        self.last_pct = pct
        print("[DATA] Light={}mV ({}%)".format(mv, pct))
        return [("Lux :{:>4d}% ".format(pct), ORANGE)]


class GnssModule(SensorModule):
    """GNSS 定位（外接 GNSS 模块 / 官方 gnss 接口）。"""

    NAME = "GNSS"

    def __init__(self, **cfg):
        super().__init__(**cfg)
        self.last_lat = None
        self.last_lng = None
        self.last_valid = False

    def _setup(self):
        from usr.gnss_service import GnssService
        self.driver = GnssService(
            uart_port=self.cfg.get("uart_port", 2),
            baudrate=self.cfg.get("baudrate", 9600))
        return self.driver.init()

    def sample(self):
        if not self.ok or not self.driver.available:
            self.last_lat = None
            self.last_lng = None
            self.last_valid = False
            return [("GNSS: N/A ", GRAY),
                    ("Lat : ---- ", GRAY),
                    ("Lon : ---- ", GRAY)]
        loc = None
        try:
            loc = self.driver.update()
        except Exception as e:
            print("[GNSS] update err:", e)
        if loc and loc.get("valid"):
            self.last_lat = loc["lat"]
            self.last_lng = loc["lng"]
            self.last_valid = True
            print("[DATA] GNSS lat={} lng={} sats={}".format(
                loc["lat"], loc["lng"], loc["sats"]))
            return [("GNSS: FIX({}) ".format(loc["sats"]), GREEN),
                    ("Lat :{:>10.5f}".format(loc["lat"]), WHITE),
                    ("Lon :{:>10.5f}".format(loc["lng"]), WHITE)]
        self.last_lat = None
        self.last_lng = None
        self.last_valid = False
        return [("GNSS: NO FIX", RED),
                ("Lat : ---- ", GRAY),
                ("Lon : ---- ", GRAY)]
