# -*- coding: utf-8 -*-
"""Sensorhub 实践项目主程序（EC600X 开发板）。

功能：
  1. 温湿度检测   —— 板载 AHT20（I2C1）
  2. 光照强度检测 —— 板载光敏电阻 GT36528（ADC0）
  3. GNSS 定位    —— 外接 GNSS 模块（UART 读取 NMEA），无数据时显示 NO FIX
  4. 屏幕数据显示 —— 板载 ST7789 彩色屏（SPI）
"""

import utime
from usr.st7789 import ST7789, BLACK, CYAN, GRAY
from usr.modules import TempHumiModule, LightModule, GnssModule
from usr.qth_cloud import QthCloudClient


# 屏幕分辨率：默认 240x240；若为 240x320 套餐请改成 320（并见 st7789.py 说明）
LCD_WIDTH = 240
LCD_HEIGHT = 240

# 采集周期（秒）
SAMPLE_INTERVAL = 2

# ==================================================================== #
MODULES = [
    (TempHumiModule, {}),                                   # 温湿度 AHT20
    (LightModule,    {}),                                   # 光照 光敏电阻
    (GnssModule,     {"uart_port": 2, "baudrate": 9600}),   # 外接 GNSS（串口2）
]

# 布局参数（scale=2 时每行约 16 像素高，行距 26）
ROW_TITLE = 6      # 标题 Y
ROW_START = 40     # 首个数据行 Y
ROW_STEP = 26      # 行距
ROW_FOOT = 214     # 底部计数行 Y


class Sensorhub(object):

    def __init__(self):
        self.lcd = ST7789(LCD_WIDTH, LCD_HEIGHT)
        self.lcd.clear(BLACK)
        self.modules = []
        self.cloud = QthCloudClient("/usr/cloud_config.json")

    def setup(self):
        # 标题 + 分隔线
        self.lcd.draw_string(8, ROW_TITLE, "Sensorhub", fg=CYAN, bg=BLACK, scale=2)
        self.lcd.fill_rect(0, ROW_START - 6, LCD_WIDTH, 2, GRAY)

        # 按配置表逐个实例化并初始化，单个失败不影响其它
        for cls, cfg in MODULES:
            module = cls(**cfg)
            module.init()
            self.modules.append(module)
            print("[INIT] {} {}".format(module.NAME, "OK" if module.ok else "SKIP"))

        # 上云初始化失败不影响本地显示与采集
        self.cloud.init()

    def _upload_to_cloud(self):
        """按协议上传: 温度/湿度/光照/经纬度。"""
        if self.cloud is None:
            return
        data = {
            "temp": None,
            "humi": None,
            "light": None,
            "lon": None,
            "lat": None,
        }
        for module in self.modules:
            if isinstance(module, TempHumiModule):
                data["temp"] = module.last_temp
                data["humi"] = module.last_humi
            elif isinstance(module, LightModule):
                data["light"] = module.last_pct
            elif isinstance(module, GnssModule) and module.last_valid:
                data["lon"] = module.last_lng
                data["lat"] = module.last_lat
        self.cloud.upload_properties(data)

    def run(self):
        self.setup()
        count = 0
        while True:
            count += 1
            print("---- sample #{} ----".format(count))
            # 遍历所有启用的模块，采样并自动排布显示行
            y = ROW_START
            for module in self.modules:
                try:
                    rows = module.sample()
                except Exception as e:
                    print("[{}] sample err: {}".format(module.NAME, e))
                    rows = []
                for text, color in rows:
                    self.lcd.draw_string(6, y, text, fg=color, bg=BLACK, scale=2)
                    y += ROW_STEP

            self._upload_to_cloud()
            self.lcd.draw_string(6, ROW_FOOT, "update #{:<5d}".format(count),
                                 fg=CYAN, bg=BLACK, scale=2)
            utime.sleep(SAMPLE_INTERVAL)


if __name__ == "__main__":
    Sensorhub().run()
