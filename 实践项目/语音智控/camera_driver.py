"""EC600M + GC032A 摄像头驱动（单文件：LCD 初始化 + 预览 + 拍照）。

从测试脚本提取整合：
  - LCD 初始化：ST7789V 240x240，走原生 machine.LCD，内置初始化序列（无需字库依赖）
  - 摄像头供电复位：misc.Power.camVDD2V8Enable（关→开，做一次上电复位）
  - 预览：camera.camPreview(model, cam_w, cam_h, lcd_w, lcd_h, preview_level)
  - 拍照：camera.camCapture(model, cam_w, cam_h, preview_level, lcd_w, lcd_h)

关键坑（来自移远官方文档 + 实测）：
  1. 用摄像头前【必须先初始化 LCD】，否则拍照后回调不触发。
  2. GC032A 对应 model=0（BF3901 为 1）。
  3. camCapture 构造函数里 preview_level 在【第 4 位】，与 camPreview 顺序不同，勿混淆！
  4. 预览用完必须 close()，否则摄像头被占用，下次 open() 返回 -2。
  5. start(width, height, pic_name) 的 pic_name 不用加 .jpeg，会自动添加。

硬件：EC600M-CNLE + GC032A(SPI, model=0) + ST7789V 240x240 SPI 屏
"""

import camera
import utime
import uos
from machine import LCD
from misc import Power


# ===========================================================================
# LCD：ST7789V 240x240（自包含，仅依赖原生 machine.LCD）
# ===========================================================================
class ST7789V:
    """ST7789V 240x240 SPI 屏驱动（接口1：模块内置 LCD 控制器）。"""

    INVALID_OP = -2

    def __init__(self, width=240, height=240):
        self.lcd = LCD()
        self.width = width
        self.height = height

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
            bytearray(init_data), width, height, 13000, 1, 4, 0,
            bytearray(invalid_data), bytearray(display_on_data),
            bytearray(display_off_data), None)
        if ret == self.INVALID_OP:
            raise Exception("ST7789V lcd_init failed: %d" % ret)

    def clear(self, color=0x0000):
        """清屏（默认黑）。上电后 GRAM 是随机值，务必清一次避免雪花点。"""
        return self.lcd.lcd_clear(color)

    def write(self, color_buffer, start_x, start_y, end_x, end_y):
        return self.lcd.lcd_write(color_buffer, start_x, start_y, end_x, end_y)

    def show_jpg(self, file_name, start_x=0, start_y=0):
        """读文件方式显示 JPEG 图片（用于拍照后预览）。返回 0 成功。

        注意：据官方文档，部分平台（如 EC600M）可能不支持 lcd_show_jpg。
        """
        return self.lcd.lcd_show_jpg(file_name, start_x, start_y)


# ===========================================================================
# 摄像头驱动：预览 + 拍照
# ===========================================================================
class Camera:
    """GC032A 摄像头驱动，封装预览与拍照，自动完成 LCD 初始化与供电复位。"""

    GC032A = 0
    BF3901 = 1

    def __init__(
        self,
        model=GC032A,
        cam_w=640,
        cam_h=480,
        lcd_w=240,
        lcd_h=240,
        preview_level=1,
        lcd=None,
        auto_lcd=True,
    ):
        """
        :param model: 摄像头型号，0=gc032a，1=bf3901
        :param cam_w/cam_h: 摄像头分辨率（gc032a 为 640x480）
        :param lcd_w/lcd_h: LCD 分辨率
        :param preview_level: 预览等级，EC600M 可填 1 或 2
        :param lcd: 已初始化的 LCD 对象（有 clear 方法）；不传则自动创建 ST7789V
        :param auto_lcd: True 时若未传 lcd 则自动初始化 ST7789V 并清屏
        """
        self.model = model
        self.cam_w = cam_w
        self.cam_h = cam_h
        self.lcd_w = lcd_w
        self.lcd_h = lcd_h
        self.preview_level = preview_level

        self._preview = None
        self._photo_done = False
        self._photo_ok = False
        self._photo_name = None

        # 用摄像头前必须先初始化 LCD
        if lcd is None and auto_lcd:
            lcd = ST7789V(lcd_w, lcd_h)
            lcd.clear(0x0000)
        self.lcd = lcd

    # --------------------------------------------------------------- #
    # 供电复位：2.8V 先关再开，做一次上电复位
    # --------------------------------------------------------------- #
    @staticmethod
    def power_reset():
        Power.camVDD2V8Enable(0)
        utime.sleep(0.2)
        Power.camVDD2V8Enable(1)
        utime.sleep(1)  # 等 2.8V 稳定 + 传感器上电复位完成

    @staticmethod
    def power_off():
        Power.camVDD2V8Enable(0)

    # --------------------------------------------------------------- #
    # 预览
    # --------------------------------------------------------------- #
    def start_preview(self):
        """打开实时预览（显示到 LCD）。返回 0 表示成功。"""
        if self.lcd is None:
            raise Exception("预览需要先初始化 LCD")
        self.power_reset()
        # 注意参数顺序：model, cam_w, cam_h, lcd_w, lcd_h, preview_level
        self._preview = camera.camPreview(
            self.model, self.cam_w, self.cam_h,
            self.lcd_w, self.lcd_h, self.preview_level)
        return self._preview.open()

    def stop_preview(self):
        """关闭预览（用完必须关，否则占用摄像头导致下次 open 返回 -2）。"""
        if self._preview is not None:
            self._preview.close()
            self._preview = None
        self.power_off()

    # --------------------------------------------------------------- #
    # 拍照
    # --------------------------------------------------------------- #
    def _on_capture(self, para):
        # para[0]=结果码(0成功)，para[1]=文件名
        try:
            self._photo_ok = (para[0] == 0)
            self._photo_name = para[1] if len(para) > 1 else None
        except Exception:
            self._photo_ok = False
            self._photo_name = None
        self._photo_done = True

    def take_photo(self, pic_name="photo", width=None, height=None, timeout=8,
                   show_after=False, show_xy=(0, 0)):
        """拍照并保存为 JPEG（pic_name 不用带后缀）。

        :param show_after: True 时拍照成功后自动把照片显示到 LCD（拍照后预览）
        :param show_xy: 预览显示的起始坐标 (x, y)
        :return: 成功返回保存的文件名，失败返回 None
        """
        if self.lcd is None:
            raise Exception("拍照需要先初始化 LCD")

        # 预览若开着会占用摄像头，先关掉
        if self._preview is not None:
            self.stop_preview()

        width = width or self.cam_w
        height = height or self.cam_h

        self.power_reset()

        # 注意参数顺序：model, cam_w, cam_h, preview_level, lcd_w, lcd_h
        cam = camera.camCapture(
            self.model, self.cam_w, self.cam_h,
            self.preview_level, self.lcd_w, self.lcd_h)

        if cam.open() != 0:
            self.power_off()
            raise Exception("camCapture.open 失败")
        cam.callback(self._on_capture)

        self._photo_done = False
        self._photo_ok = False
        self._photo_name = None

        utime.sleep(1)
        if cam.start(width, height, pic_name) != 0:
            cam.close()
            self.power_off()
            raise Exception("camCapture.start 失败")

        # 等待回调返回结果
        waited = 0
        limit = timeout * 1000
        while (not self._photo_done) and (waited < limit):
            utime.sleep_ms(50)
            waited += 50

        cam.close()
        self.power_off()

        result = self._photo_name if self._photo_ok else None
        if show_after and result:
            self.show_photo(result, show_xy[0], show_xy[1])
        return result

    # --------------------------------------------------------------- #
    # 拍照后预览：把保存的照片显示到 LCD
    # --------------------------------------------------------------- #
    def show_photo(self, pic_name=None, x=0, y=0):
        """把拍好的照片显示到 LCD（拍照后预览）。

        :param pic_name: 图片名；默认用最近一次拍照返回的文件名
        :param x/y: 显示起始坐标
        :return: 成功返回实际显示的文件路径，失败返回 None
        """
        if self.lcd is None:
            raise Exception("显示照片需要先初始化 LCD")
        name = pic_name or self._photo_name
        if not name:
            print("[show_photo] 没有可显示的照片")
            return None
        if not name.endswith(".jpeg"):
            name = name + ".jpeg"
        if not hasattr(self.lcd, "show_jpg"):
            print("[show_photo] 当前 LCD 对象不支持 show_jpg")
            return None
        # 照片默认保存在 /usr/，兼容传入纯文件名或带路径
        candidates = [name]
        if not name.startswith("/"):
            candidates.append("/usr/" + name)
        for f in candidates:
            try:
                r = self.lcd.show_jpg(f, x, y)
                if r == 0:
                    print("[show_photo] 已显示：", f)
                    return f
                print("[show_photo] lcd_show_jpg 返回", r,
                      "（EC600M 据官方文档可能不支持屏上预览 JPEG）")
            except Exception as e:
                print("[show_photo] 显示异常：", e)
        return None

    def close(self):
        """释放资源。"""
        self.stop_preview()
        try:
            self.power_off()
        except Exception:
            pass


# ===========================================================================
# 独立自测：预览 10 秒 → 拍照保存到 /usr/
# ===========================================================================
def _demo():
    print("[camera_driver] init LCD + camera ...")
    cam = Camera(model=Camera.GC032A, cam_w=640, cam_h=480,
                 lcd_w=240, lcd_h=240, preview_level=2)
    try:
        print("start preview:", cam.start_preview())
        utime.sleep(10)
        cam.stop_preview()

        # 拍照后自动在屏上预览照片（capture 分辨率取屏幕尺寸以便全屏显示）
        name = cam.take_photo("test_photo", width=240, height=240, show_after=True)
        if name:
            print("[OK] 拍照成功，已保存：", name)
            utime.sleep(5)  # 保持照片显示 5 秒
        else:
            print("[FAIL] 拍照失败")

        print("jpeg in /usr:",
              [f for f in uos.listdir("/usr") if f.endswith(".jpeg")])
    finally:
        cam.close()
        print("[camera_driver] done")


if __name__ == "__main__":
    _demo()
