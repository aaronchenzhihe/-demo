# -*- coding: utf-8 -*-
"""AHT20 温湿度传感器驱动。

硬件连接（EC600X 开发板）：
- SDA: 引脚 56 / GPIO16
- SCL: 引脚 57 / GPIO15
- VCC: 3.3V
- GND: GND

接口：
- init() -> bool
- read() -> (temperature_c, humidity_percent)
"""

from machine import I2C
import utime


class AHT20(object):
	"""AHT20 驱动。"""

	def __init__(self, i2c=None, addr=0x38):
		self.addr = addr
		self.i2c = i2c if i2c is not None else I2C(I2C.I2C1, I2C.STANDARD_MODE)
		self._reg = bytearray([0x00])

	@staticmethod
	def _crc8(data):
		"""AHT20 CRC8 校验，多项式 0x31。"""
		crc = 0xFF
		for byte in data:
			crc ^= byte
			for _ in range(8):
				if crc & 0x80:
					crc = ((crc << 1) ^ 0x31) & 0xFF
				else:
					crc = (crc << 1) & 0xFF
		return crc

	def init(self):
		"""初始化 AHT20。成功返回 True。"""
		try:
			# 软复位
			self.i2c.write(self.addr, self._reg, 0, bytearray([0xBA]), 1)
			utime.sleep_ms(20)
			# 初始化校准
			self.i2c.write(self.addr, self._reg, 0, bytearray([0xBE, 0x08, 0x00]), 3)
			utime.sleep_ms(10)
			return True
		except Exception as e:
			print("[AHT20] init err:", e)
			return False

	def _read_raw(self):
		"""触发一次测量并读取原始 7 字节数据。"""
		self.i2c.write(self.addr, self._reg, 0, bytearray([0xAC, 0x33, 0x00]), 3)
		utime.sleep_ms(85)
		data = bytearray(7)
		ret = self.i2c.read(self.addr, self._reg, 0, data, 7, 0)
		if ret != 0:
			return None
		return data

	def read(self, retry=3):
		"""读取温湿度。

		:return: (temperature_c, humidity_percent)；失败时 (None, None)
		"""
		for attempt in range(retry):
			try:
				data = self._read_raw()
				if not data or len(data) < 7:
					continue

				# 数据有效位：bit7=0 表示忙碌结束
				if data[0] & 0x80:
					utime.sleep_ms(10)
					continue

				# 校验 CRC
				if self._crc8(data[:6]) != data[6]:
					utime.sleep_ms(10)
					continue

				humi_raw = (data[1] << 12) | (data[2] << 4) | (data[3] >> 4)
				temp_raw = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]

				humidity = (humi_raw / 1048576.0) * 100.0
				temperature = (temp_raw / 1048576.0) * 200.0 - 50.0
				return (round(temperature, 1), round(humidity, 1))
			except Exception as e:
				if attempt == retry - 1:
					print("[AHT20] read err:", e)
				utime.sleep_ms(10)

		return (None, None)

