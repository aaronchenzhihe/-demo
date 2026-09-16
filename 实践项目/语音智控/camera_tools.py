# -*- coding: UTF-8 -*-
"""摄像头 MCP 工具封装（按键小智 AI）。

把 camera_driver.Camera 封装成小智 MCP 可调用的两类工具：
  - 监控工具：打开/关闭摄像头实时预览（画面显示到 LCD）
  - 拍照工具：拍一张照片保存为 JPEG 到 /usr/，并把文件名回给 AI 播报

用法（在 _main.py 中）：
  self.camera_tools = CameraTools()              # 懒加载，首次调用才上电
  ...
  # tools/list 时：protocol 侧把 CameraTools.TOOLS 拼进工具列表
  # tools/call 时：text = self.camera_tools.handle(tool_name)

注意：摄像头拍照/预览都依赖 LCD 已初始化（见 camera_driver 说明）。
     若板子未接屏，监控无意义、拍照回调也可能不触发。
"""

from usr.camera_driver import Camera
from usr.logging import getLogger

logger = getLogger(__name__)


# MCP tools/list 中登记的工具定义（名字沿用本工程 "self.xxx()" 约定）
TOOLS = [
    {
        "name": "self.camera_monitor_on()",
        "description": "打开摄像头实时监控预览，把摄像头画面实时显示到 LCD 屏幕上",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "self.camera_monitor_off()",
        "description": "关闭摄像头实时监控预览",
        "inputSchema": {"type": "object", "properties": {}}
    },
    {
        "name": "self.take_photo()",
        "description": "用摄像头拍一张照片并保存为 JPEG 到 /usr/ 目录，接收到回应后会告知保存的文件名",
        "inputSchema": {"type": "object", "properties": {}}
    },
]


class CameraTools(object):
    """摄像头 MCP 工具集：监控开关 + 拍照。摄像头懒加载，首次调用时才初始化。"""

    # 供 _main.py 判断某个 tools/call 是否属于摄像头工具
    NAMES = (
        "self.camera_monitor_on()",
        "self.camera_monitor_off()",
        "self.take_photo()",
    )

    TOOLS = TOOLS

    def __init__(self, model=Camera.GC032A, cam_w=640, cam_h=480,
                 lcd_w=240, lcd_h=240, preview_level=1):
        self._cfg = {
            "model": model,
            "cam_w": cam_w,
            "cam_h": cam_h,
            "lcd_w": lcd_w,
            "lcd_h": lcd_h,
            "preview_level": preview_level,
        }
        self.cam = None
        self._monitoring = False

    def _ensure_camera(self):
        """首次使用时才创建 Camera（会初始化 LCD 并做供电复位）。"""
        if self.cam is None:
            logger.info("init camera on first use ...")
            self.cam = Camera(**self._cfg)
        return self.cam

    # ---------------- 监控工具 ---------------- #
    def monitor_on(self):
        try:
            cam = self._ensure_camera()
            ret = cam.start_preview()
            if ret == 0:
                self._monitoring = True
                return "摄像头监控已打开"
            return "摄像头监控打开失败(ret={})".format(ret)
        except Exception as e:
            logger.error("monitor_on error: {}".format(repr(e)))
            return "摄像头监控打开异常: {}".format(e)

    def monitor_off(self):
        try:
            if self.cam is not None and self._monitoring:
                self.cam.stop_preview()
            self._monitoring = False
            # 关闭监控时同步清屏（黑屏），不再显示最后一帧画面
            if self.cam is not None and self.cam.lcd is not None:
                self.cam.lcd.clear(0x0000)
            return "摄像头监控已关闭"
        except Exception as e:
            logger.error("monitor_off error: {}".format(repr(e)))
            return "摄像头监控关闭异常: {}".format(e)

    # ---------------- 拍照工具 ---------------- #
    def take_photo(self, pic_name="photo"):
        try:
            cam = self._ensure_camera()
            name = cam.take_photo(pic_name)
            self._monitoring = False  # take_photo 内部会先停预览
            if name:
                return "拍照成功，已保存 {}".format(name)
            return "拍照失败"
        except Exception as e:
            logger.error("take_photo error: {}".format(repr(e)))
            return "拍照异常: {}".format(e)

    # ---------------- MCP 分发 ---------------- #
    def handle(self, tool_name, args=None):
        """按工具名分发，返回给 AI 播报的文本。"""
        if tool_name == "self.camera_monitor_on()":
            return self.monitor_on()
        if tool_name == "self.camera_monitor_off()":
            return self.monitor_off()
        if tool_name == "self.take_photo()":
            return self.take_photo()
        return "未知摄像头工具: {}".format(tool_name)
