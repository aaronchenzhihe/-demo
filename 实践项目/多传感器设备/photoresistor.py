# -*- coding: utf-8 -*-
"""光照强度检测（EC600X 开发板板载光敏电阻 GT36528，ADC 接口）。"""

from misc import ADC
import utime


class Photoresistor(object):

    def __init__(self, channel=ADC.ADC0, dark_mv=356, bright_mv=177):
        """
        :param channel: ADC 通道，EC600X 光敏电阻默认 ADC0
        :param dark_mv: 全暗时的参考电压（mV），对应 0%
        :param bright_mv: 强光时的参考电压（mV），对应 100%
        """
        self.channel = channel
        self.dark_mv = dark_mv
        self.bright_mv = bright_mv
        self.adc = ADC()

    def read_voltage(self, samples=5):
        """读取平均电压（mV）。"""
        total = 0
        n = 0
        for _ in range(samples):
            v = self.adc.read(self.channel)
            if v is not None and v >= 0:
                total += v
                n += 1
            utime.sleep_ms(5)
        if n == 0:
            return 0
        return total // n

    def read_percent(self):
        """把电压映射为 0~100 的相对光照百分比。"""
        mv = self.read_voltage()
        span = self.dark_mv - self.bright_mv
        if span == 0:
            return 0
        pct = (self.dark_mv - mv) * 100 // span
        if pct < 0:
            pct = 0
        elif pct > 100:
            pct = 100
        return pct

    def read(self):
        """返回 (电压mV,相对光照百分比)。"""
        mv = self.read_voltage()
        span = self.dark_mv - self.bright_mv
        pct = 0 if span == 0 else (self.dark_mv - mv) * 100 // span
        if pct < 0:
            pct = 0
        elif pct > 100:
            pct = 100
        return mv, pct


if __name__ == "__main__":
    dev = Photoresistor()
    for _ in range(10):
        mv, pct = dev.read()
        print("Light: {} mV  ({}%)".format(mv, pct))
        utime.sleep(1)
