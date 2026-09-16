# -*- coding: utf-8 -*-
"""ST7789 SPI 彩色 LCD 驱动（EC600X 开发板板载屏，含屏套餐）。"""

from machine import LCD
from usr.font8x8 import get_glyph


# 常用 RGB565 颜色
BLACK = 0x0000
WHITE = 0xFFFF
RED = 0xF800
GREEN = 0x07E0
BLUE = 0x001F
YELLOW = 0xFFE0
CYAN = 0x07FF
MAGENTA = 0xF81F
GRAY = 0x8410
ORANGE = 0xFD20


def rgb(r, g, b):
    """把 8bit RGB 转成 RGB565。"""
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


class ST7789(object):
    """ST7789 240x240 彩色屏驱动（接口1：模块内置 LCD 控制器）。"""

    INVALID_OP = -2

    def __init__(self, width=240, height=240, clk=13000):
        self.width = width
        self.height = height
        self.lcd = LCD()

        init_data = (
            2, 0, 120,
            0, 0, 0x11,
            0, 1, 0x36,
            1, 1, 0x00,
            0, 1, 0x3A,
            1, 1, 0x05,
            0, 0, 0x21,
            0, 5, 0xB2,
            1, 1, 0x05,
            1, 1, 0x05,
            1, 1, 0x00,
            1, 1, 0x33,
            1, 1, 0x33,
            0, 1, 0xB7,
            1, 1, 0x23,
            0, 1, 0xBB,
            1, 1, 0x22,
            0, 1, 0xC0,
            1, 1, 0x2C,
            0, 1, 0xC2,
            1, 1, 0x01,
            0, 1, 0xC3,
            1, 1, 0x13,
            0, 1, 0xC4, 
            1, 1, 0x20,
            0, 1, 0xC6,
            1, 1, 0x0F,
            0, 2, 0xD0,
            1, 1, 0xA4,
            1, 1, 0xA1,
            0, 1, 0xD6,
            1, 1, 0xA1,
            0, 14, 0xE0,
            1, 1, 0x70,
            1, 1, 0x06,
            1, 1, 0x0C,
            1, 1, 0x08,
            1, 1, 0x09,
            1, 1, 0x27,
            1, 1, 0x2E,
            1, 1, 0x34,
            1, 1, 0x46,
            1, 1, 0x37,
            1, 1, 0x13,
            1, 1, 0x13,
            1, 1, 0x25,
            1, 1, 0x2A,
            0, 14, 0xE1,
            1, 1, 0x70,
            1, 1, 0x04,
            1, 1, 0x08,
            1, 1, 0x09,
            1, 1, 0x07,
            1, 1, 0x03,
            1, 1, 0x2C,
            1, 1, 0x42,
            1, 1, 0x42,
            1, 1, 0x38,
            1, 1, 0x14,
            1, 1, 0x14,
            1, 1, 0x27,
            1, 1, 0x2C,
            0, 0, 0x29,
            0, 1, 0x36,
            1, 1, 0x00,
            0, 4, 0x2a,
            1, 1, 0x00,
            1, 1, 0x00,
            1, 1, 0x00,
            1, 1, 0xef,
            0, 4, 0x2b,
            1, 1, 0x00,
            1, 1, 0x00,
            1, 1, 0x00,
            1, 1, 0xef,
            0, 0, 0x2c,
        )
        invalid_data = (
            0, 4, 0x2a,
            1, 1, 0xf0,
            1, 1, 0xf1,
            1, 1, 0xE0,
            1, 1, 0xE1,
            0, 4, 0x2b,
            1, 1, 0xf2,
            1, 1, 0xf3,
            1, 1, 0xE2,
            1, 1, 0xE3,
            0, 0, 0x2c,
        )
        display_on_data = (
            0, 0, 0x11,
            2, 0, 20,
            0, 0, 0x29,
        )
        display_off_data = (
            0, 0, 0x28,
            2, 0, 120,
            0, 0, 0x10,
        )

        ret = self.lcd.lcd_init(
            bytearray(init_data), width, height, clk, 1, 4, 0,
            bytearray(invalid_data), bytearray(display_on_data),
            bytearray(display_off_data), None)
        if ret == self.INVALID_OP:
            raise Exception("ST7789 lcd_init failed: {}".format(ret))

    # ------------------------------------------------------------------ #
    # 基础绘制
    # ------------------------------------------------------------------ #
    def clear(self, color=BLACK):
        """整屏填充颜色（默认黑）。"""
        return self.lcd.lcd_clear(color)

    def fill_rect(self, x, y, w, h, color):
        """填充矩形区域。"""
        if w <= 0 or h <= 0:
            return
        hi = (color >> 8) & 0xFF
        lo = color & 0xFF
        # 注意：该固件的 bytearray 不支持切片赋值，用 bytes 乘法构造纯色缓冲
        buf = bytearray(bytes([hi, lo]) * (w * h))
        self.lcd.lcd_write(buf, x, y, x + w - 1, y + h - 1)

    # ------------------------------------------------------------------ #
    # 文字渲染（8x8 ASCII 字库）
    # ------------------------------------------------------------------ #
    def draw_string(self, x, y, text, fg=WHITE, bg=BLACK, scale=2):
        """在 (x, y) 处绘制一行字符串。

        :param scale: 放大倍数，1=8x8 像素，2=16x16 像素，以此类推
        """
        if not text:
            return
        cw = 8 * scale               # 单字符像素宽
        box_w = cw * len(text)       # 整行像素宽
        box_h = 8 * scale            # 行高
        # 越界裁剪：避免超出屏幕导致写入失败
        if x + box_w > self.width:
            box_w = self.width - x
            if box_w <= 0:
                return
        if y + box_h > self.height:
            box_h = self.height - y
            if box_h <= 0:
                return

        fg_hi, fg_lo = (fg >> 8) & 0xFF, fg & 0xFF
        bg_hi, bg_lo = (bg >> 8) & 0xFF, bg & 0xFF
        buf = bytearray(box_w * box_h * 2)

        for k in range(len(text)):
            glyph = get_glyph(text[k])
            base_x = k * cw
            for r in range(8):
                row_byte = glyph[r]
                for c in range(8):
                    on = (row_byte >> c) & 1
                    if on:
                        hi, lo = fg_hi, fg_lo
                    else:
                        hi, lo = bg_hi, bg_lo
                    # 放大成 scale x scale 像素块
                    for dy in range(scale):
                        py = r * scale + dy
                        if py >= box_h:
                            break
                        row_off = py * box_w * 2
                        for dx in range(scale):
                            px = base_x + c * scale + dx
                            if px >= box_w:
                                break
                            idx = row_off + px * 2
                            buf[idx] = hi
                            buf[idx + 1] = lo

        self.lcd.lcd_write(buf, x, y, x + box_w - 1, y + box_h - 1)

    def display_on(self):
        self.lcd.lcd_display_on()

    def display_off(self):
        self.lcd.lcd_display_off()
