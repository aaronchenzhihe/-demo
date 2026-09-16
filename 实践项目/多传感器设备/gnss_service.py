# -*- coding: utf-8 -*-
"""GNSS 定位服务（外接 GNSS 模块，基于 QuecPython 官方 gnss 接口）。

采用官方外置 GNSS 库 `gnss.GnssGetData`（内部已封装串口读取 + NMEA 解析，
支持 GNGGA/GNRMC/GPGSV），比手动解析更规范。
参考文档：API_reference/zh/gnsslib/gnss.html
"""

import utime

try:
    from gnss import GnssGetData
    _HAS_GNSS = True
except ImportError:
    GnssGetData = None
    _HAS_GNSS = False


class GnssService(object):

    def __init__(self, uart_port=2, baudrate=9600,
                 databits=8, parity=0, stopbits=1, flowctl=0):
        """
        :param uart_port: uartn 通道号，外接 GNSS 用串口 2（MAIN PORT）
        :param baudrate: GNSS 模块波特率，多数默认 9600
        :param databits/parity/stopbits/flowctl: 串口参数，默认 8N1 无流控
        """
        self.uart_port = uart_port
        self.baudrate = baudrate
        self.databits = databits
        self.parity = parity
        self.stopbits = stopbits
        self.flowctl = flowctl
        self.gnss = None
        self.available = False
        self.location = None      # dict: lat/lng/alt/sats/valid/time

    def init(self):
        """创建 GNSS 对象，成功返回 True。"""
        if not _HAS_GNSS:
            print("[GNSS] 当前固件不含 gnss 模块（外置 GNSS 接口不可用）")
            return False
        try:
            self.gnss = GnssGetData(
                self.uart_port, self.baudrate,
                self.databits, self.parity, self.stopbits, self.flowctl)
            self.available = True
            print("[GNSS] 外接 GNSS 已初始化 uart{} @ {}bps".format(
                self.uart_port, self.baudrate))
            return True
        except Exception as e:
            print("[GNSS] 初始化失败:", e)
            self.available = False
            return False

    def get_state(self):
        """兼容接口：0=未就绪，2=就绪（可读数据）。"""
        return 2 if self.available else 0

    @staticmethod
    def _signed(value, direction):
        """按方向（S/W 为负）给经纬度加符号。"""
        try:
            v = float(value)
        except (ValueError, TypeError):
            return None
        if direction in ("S", "W"):
            v = -v
        return round(v, 6)

    def update(self):
        """读取并解析一包 GNSS 数据。定位成功返回 dict，否则返回 None。"""
        if not self.available or self.gnss is None:
            return None

        try:
            if not self.gnss.read_gnss_data(max_retry=1, debug=0):
                return None
            if self.gnss.isFix() != 1:
                return None

            loc = self.gnss.getLocation()
            if loc == -1 or not isinstance(loc, (tuple, list)) or len(loc) < 4:
                return None

            longitude, lon_dir, latitude, lat_dir = loc[:4]
            lng = self._signed(longitude, lon_dir)
            lat = self._signed(latitude, lat_dir)
            if lat is None or lng is None:
                return None

            info = {
                "valid": True,
                "lat": lat,
                "lng": lng,
                "alt": None,
                "sats": 0,
                "time": None,
            }

            try:
                info["sats"] = self.gnss.getUsedSateCnt()
            except Exception:
                pass
            try:
                info["alt"] = self.gnss.getGeodeticHeight()
            except Exception:
                pass
            try:
                info["time"] = self.gnss.getUtcTime()
            except Exception:
                pass

            self.location = info
            return info
        except Exception as e:
            print("[GNSS] update error:", e)
            return None



