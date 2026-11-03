# -*- coding: UTF-8 -*-
"""LVGL 显示层：LCD + LVGL 初始化、界面创建、UI 按键导航状态机。

界面采用 GUI Guider 设计（色彩/字体/控件位置），高亮改用“直接设置背景色 +
强制刷新”驱动，绕开本固件 CHECKED 状态不触发重绘的问题。

职责：
  1. LCD + LVGL 初始化（v8 API，已验证）
  2. 创建主菜单 / 删除照片页 / 确认对话框
  3. 管理 UI 按键导航状态（menu_idx / bottom_idx / list_idx / dialog_idx）
  4. 提供线程安全的 LCD 所有权切换（lvgl / camera）

不包含：摄像头流程、业务模块逻辑（交给 camera_flow 和业务模块）。
"""
import utime
import sys
import _thread
import lvgl as lv
from machine import LCD
from usr.logging import getLogger

logger = getLogger("ui")

LCD_WIDTH, LCD_HEIGHT = 240, 240

from usr.config import LCD_CLK, LCD_DATA_LINE, LCD_LINE_NUM, LCD_TYPE, LCD_DRAW_BUF_LINES

# ---- ST7789 初始化序列（与 Portable_music 一致，已验证）----
INIT_DATA = (
    2, 0, 120,
    0, 0, 0x11,
    0, 1, 0x36, 1, 1, 0x00,
    0, 1, 0x3A, 1, 1, 0x05,
    0, 0, 0x21,
    0, 5, 0xB2,
    1, 1, 0x05, 1, 1, 0x05, 1, 1, 0x00, 1, 1, 0x33, 1, 1, 0x33,
    0, 1, 0xB7, 1, 1, 0x23,
    0, 1, 0xBB, 1, 1, 0x22,
    0, 1, 0xC0, 1, 1, 0x2C,
    0, 1, 0xC2, 1, 1, 0x01,
    0, 1, 0xC3, 1, 1, 0x13,
    0, 1, 0xC4, 1, 1, 0x20,
    0, 1, 0xC6, 1, 1, 0x0F,
    0, 2, 0xD0, 1, 1, 0xA4, 1, 1, 0xA1,
    0, 1, 0xD6, 1, 1, 0xA1,
    0, 14, 0xE0,
    1, 1, 0x70, 1, 1, 0x06, 1, 1, 0x0C, 1, 1, 0x08, 1, 1, 0x09,
    1, 1, 0x27, 1, 1, 0x2E, 1, 1, 0x34, 1, 1, 0x46, 1, 1, 0x37,
    1, 1, 0x13, 1, 1, 0x13, 1, 1, 0x25, 1, 1, 0x2A,
    0, 14, 0xE1,
    1, 1, 0x70, 1, 1, 0x04, 1, 1, 0x08, 1, 1, 0x09, 1, 1, 0x07,
    1, 1, 0x03, 1, 1, 0x2C, 1, 1, 0x42, 1, 1, 0x42, 1, 1, 0x38,
    1, 1, 0x14, 1, 1, 0x14, 1, 1, 0x27, 1, 1, 0x2C,
    0, 0, 0x29,
    0, 1, 0x36, 1, 1, 0x00,
    0, 4, 0x2A, 1, 1, 0x00, 1, 1, 0x00, 1, 1, 0x00, 1, 1, 0xEF,
    0, 4, 0x2B, 1, 1, 0x00, 1, 1, 0x00, 1, 1, 0x00, 1, 1, 0xEF,
    0, 0, 0x2C,
)
INVALID = (
    0, 4, 0x2A, 1, 1, 0xF0, 1, 1, 0xF1, 1, 1, 0xE0, 1, 1, 0xE1,
    0, 4, 0x2B, 1, 1, 0xF2, 1, 1, 0xF3, 1, 1, 0xE2, 1, 1, 0xE3,
    0, 0, 0x2C,
)
DISPLAY_ON = (0, 0, 0x11, 2, 0, 20, 0, 0, 0x29)
DISPLAY_OFF = (0, 0, 0x28, 2, 0, 120, 0, 0, 0x10)

# LCD 实例（供 flush_cb 用）
_lcd = None


# ============================================================
# 1. LCD + LVGL 初始化
# ============================================================

def init():
    """初始化 LCD + LVGL。返回 True=成功。"""
    global _lcd
    logger.info("初始化 LCD + LVGL...")
    _lcd = LCD()
    ret = _lcd.lcd_init(
        bytearray(INIT_DATA),
        LCD_WIDTH, LCD_HEIGHT,
        LCD_CLK, LCD_DATA_LINE, LCD_LINE_NUM, LCD_TYPE,
        bytearray(INVALID),
        bytearray(DISPLAY_ON),
        bytearray(DISPLAY_OFF),
        None,
    )
    if ret != 0:
        raise RuntimeError("LCD init fail: ret=%d" % ret)
    _lcd.lcd_clear(0x0000)

    # LVGL v8 初始化
    lv.init()

    def flush_cb(drv, area, buf):
        x1, y1 = area.x1, area.y1
        x2, y2 = area.x2, area.y2
        try:
            n = (x2 - x1 + 1) * (y2 - y1 + 1) * 4
            _lcd.lcd_write(x1, y1, x2, y2, buf.__dereference__(n))
        except Exception:
            try:
                _lcd.lcd_write(buf)
            except Exception:
                pass
        lv.disp_flush_ready()

    # 分块缓冲替代全屏缓冲：只分配 LCD_DRAW_BUF_LINES 行显存，降低 RAM 占用。
    # LVGL 会自动走多段 flush，静态界面显示不受影响。
    buf1 = bytearray(LCD_WIDTH * LCD_DRAW_BUF_LINES * 4)
    disp_buf = lv.disp_draw_buf_t()
    disp_buf.init(buf1, None, LCD_WIDTH * LCD_DRAW_BUF_LINES)

    disp_drv = lv.disp_drv_t()
    disp_drv.init()
    disp_drv.draw_buf = disp_buf
    disp_drv.flush_cb = flush_cb
    disp_drv.hor_res = LCD_WIDTH
    disp_drv.ver_res = LCD_HEIGHT
    disp_drv.register()
    logger.info("LCD + LVGL OK")
    return True


def _font(font_family, font_size):
    """取字体；保证 montserrat_14 一定有，否则回退默认。"""
    try:
        return lv.font_montserrat_14
    except AttributeError:
        return lv.font_default


# ============================================================
# 2. 界面对象（模块级引用）
# ============================================================

screen_menu = None
screen_menu_btns = []
screen_delete = None
screen_delete_list = None
screen_delete_bottom_btns = []
screen_analyze = None
screen_analyze_list = None
screen_analyze_bottom_btns = []

_menu_idx = 0
_bottom_idx = 0
_list_items = []
_list_names = []      # 与 _list_items 一一对应的照片名
_list_sources = []    # 与 _list_items 一一对应的来源: "local"/"cloud"/"none"
_list_urls = []       # 与 _list_items 一一对应的云端照片 download_url（云端才有）
_list_idx = 0

# 识别页：来源选择状态
screen_analyze_src = None
screen_analyze_src_btns = []
_analyze_src_idx = 0
_analyze_filter = "all"    # "local"/"cloud"/"all"

# 相册预览页状态
screen_album_src = None
screen_album_src_btns = []
_album_src_idx = 0
screen_album = None
screen_album_img = None
screen_album_title = None
screen_album_bottom_btns = []
_album_idx = 0
_album_source = "local"   # "local" / "cloud"
_album_photos = []        # [(name, source, url), ...]

# 视觉分析遮罩（Analyzing...）
_analyze_overlay = None

# 对话框状态
_dialog_obj = None
_dialog_btns = []
_dialog_idx = 0
_dialog_photo = ""

# GUI Guider 配色
_C_GRAY = 0xa4a4a4            # 按钮默认底色
_C_BLUE = 0x2195f6            # 选中高亮蓝
_C_LIST_BG = 0xffffff         # List 项默认底色
_C_LIST_TEXT = 0x0D3055       # List 项文字（深蓝）
_C_LIST_SEL_BG = 0x2195f6     # List 项选中底
_C_LIST_SEL_TEXT = 0xffffff   # List 项选中文字


# ============================================================
# 3. 界面创建（GUI Guider 设计）
# ============================================================

def _mk_scr():
    scr = lv.obj()
    scr.set_size(LCD_WIDTH, LCD_HEIGHT)
    scr.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    scr.set_style_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    scr.set_style_bg_color(lv.color_hex(0x000000), lv.PART.MAIN | lv.STATE.DEFAULT)
    scr.set_style_bg_grad_dir(lv.GRAD_DIR.NONE, lv.PART.MAIN | lv.STATE.DEFAULT)
    return scr


def _title(parent, text):
    lbl = lv.label(parent)
    lbl.set_text(text)
    lbl.set_long_mode(lv.label.LONG.WRAP)
    lbl.set_width(lv.pct(100))
    lbl.set_pos(0, 0)
    lbl.set_size(240, 25)
    lbl.set_style_border_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_radius(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_text_color(lv.color_hex(0xf1f1f1), lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_text_font(_font("montserratMedium", 16), lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_text_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_text_letter_space(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_text_line_space(10, lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_bg_color(lv.color_hex(0x000000), lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_pad_all(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_shadow_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    return lbl


def _mk_btn(parent, text, w, h):
    """创建 GUI Guider 风格按钮（灰色底、黑字，CHECKABLE）。"""
    b = lv.btn(parent)
    b.add_flag(lv.obj.FLAG.CHECKABLE)
    b.set_style_pad_all(0, lv.STATE.DEFAULT)
    b.set_size(w, h)
    b.set_style_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    b.set_style_bg_color(lv.color_hex(_C_GRAY), lv.PART.MAIN | lv.STATE.DEFAULT)
    b.set_style_border_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    b.set_style_radius(5, lv.PART.MAIN | lv.STATE.DEFAULT)
    b.set_style_shadow_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl = lv.label(b)
    lbl.set_text(text)
    lbl.set_long_mode(lv.label.LONG.WRAP)
    lbl.set_width(lv.pct(100))
    lbl.align(lv.ALIGN.LEFT_MID, 0, 0)
    lbl.set_style_text_color(lv.color_hex(0xffffff), lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_text_font(_font("montserratMedium", 12), lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_text_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    lbl.set_style_text_align(lv.TEXT_ALIGN.LEFT, lv.PART.MAIN | lv.STATE.DEFAULT)
    return b


def create_menu():
    """创建主菜单界面（4 项，Analyze Photo 已并入 Photo Preview）。"""
    global screen_menu, screen_menu_btns
    screen_menu = _mk_scr()
    _title(screen_menu, "CAMERA Menu")

    menu_items = [
        ("Voice AI",       40),
        ("Manual Photo",   68),
        ("Photo Preview",  96),
        ("Interval Photo", 124),
    ]
    screen_menu_btns = []
    for text, y_off in menu_items:
        b = _mk_btn(screen_menu, text, 176, 26)
        b.set_pos(32, y_off)
        screen_menu_btns.append(b)
    screen_menu.update_layout()
    return screen_menu, screen_menu_btns


def create_delete():
    """创建删除照片页界面。"""
    global screen_delete, screen_delete_list, screen_delete_bottom_btns
    screen_delete = _mk_scr()
    _title(screen_delete, "Delete Photos")

    screen_delete_list = lv.list(screen_delete)
    screen_delete_list.set_pos(9, 46)
    screen_delete_list.set_size(220, 117)
    screen_delete_list.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    screen_delete_list.set_style_pad_top(5, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_delete_list.set_style_pad_left(5, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_delete_list.set_style_pad_right(5, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_delete_list.set_style_pad_bottom(5, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_delete_list.set_style_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_delete_list.set_style_bg_color(lv.color_hex(0xffffff), lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_delete_list.set_style_border_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_delete_list.set_style_radius(3, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_delete_list.set_style_shadow_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)

    # 底部 4 个导航按钮
    btn_cfg = [
        (lv.SYMBOL.HOME + " ", 9),
        (lv.SYMBOL.UP + " ", 69),
        (lv.SYMBOL.DOWN + " ", 128),
        (lv.SYMBOL.OK + " ", 187),
    ]
    screen_delete_bottom_btns = []
    for symbol, x_off in btn_cfg:
        b = _mk_btn(screen_delete, symbol, 43, 25)
        b.set_pos(x_off, 207)
        # 符号按钮文本居中
        _lbl = b.get_child(0)
        if _lbl is not None:
            try:
                _lbl.align(lv.ALIGN.CENTER, 0, 0)
                _lbl.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN | lv.STATE.DEFAULT)
            except Exception:
                pass
        screen_delete_bottom_btns.append(b)
    screen_delete.update_layout()
    return screen_delete, screen_delete_list, screen_delete_bottom_btns


def create_analyze():
    """创建图像识别入口：先来源选择页（Local/Cloud/All），OK 后进入照片列表页。"""
    global screen_analyze_src, screen_analyze_src_btns
    global screen_analyze, screen_analyze_list, screen_analyze_bottom_btns

    # ---- 来源选择页 ----
    screen_analyze_src = _mk_scr()
    _title(screen_analyze_src, "Analyze Source")
    src_items = [
        ("Local Photos", 30),
        ("Cloud Photos", 65),
        ("Back", 100),
    ]
    screen_analyze_src_btns = []
    for text, y_off in src_items:
        b = _mk_btn(screen_analyze_src, text, 176, 28)
        b.set_pos(32, y_off)
        screen_analyze_src_btns.append(b)
    screen_analyze_src.update_layout()

    # ---- 照片列表页 ----
    screen_analyze = _mk_scr()
    _title(screen_analyze, "Analyze Photo")

    screen_analyze_list = lv.list(screen_analyze)
    screen_analyze_list.set_pos(9, 46)
    screen_analyze_list.set_size(220, 117)
    screen_analyze_list.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    for attr, val in [('pad_top',5),('pad_left',5),('pad_right',5),('pad_bottom',5),('bg_opa',255),('border_width',0),('radius',3),('shadow_width',0)]:
        try: getattr(screen_analyze_list, 'set_style_'+attr)(val, lv.PART.MAIN | lv.STATE.DEFAULT)
        except Exception: pass
    try: screen_analyze_list.set_style_bg_color(lv.color_hex(0xffffff), lv.PART.MAIN | lv.STATE.DEFAULT)
    except Exception: pass

    btn_cfg = [(lv.SYMBOL.HOME + " ", 9), (lv.SYMBOL.UP + " ", 69),
               (lv.SYMBOL.DOWN + " ", 128), (lv.SYMBOL.OK + " ", 187)]
    screen_analyze_bottom_btns = []
    for symbol, x_off in btn_cfg:
        b = _mk_btn(screen_analyze, symbol, 43, 25)
        b.set_pos(x_off, 207)
        _lbl = b.get_child(0)
        if _lbl is not None:
            try:
                _lbl.align(lv.ALIGN.CENTER, 0, 0)
                _lbl.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN | lv.STATE.DEFAULT)
            except Exception:
                pass
        screen_analyze_bottom_btns.append(b)
    screen_analyze.update_layout()
    return screen_analyze, screen_analyze_list, screen_analyze_bottom_btns


def create_album():
    """创建相册预览界面：来源选择页 + 预览浏览页。"""
    global screen_album_src, screen_album_src_btns
    global screen_album, screen_album_img, screen_album_title, screen_album_bottom_btns

    # ---- 来源选择页 ----
    screen_album_src = _mk_scr()
    _title(screen_album_src, "Photo Preview")
    src_items = [
        ("Local Photos", 30),
        ("Cloud Photos", 65),
        ("Back", 100),
    ]
    screen_album_src_btns = []
    for text, y_off in src_items:
        b = _mk_btn(screen_album_src, text, 176, 28)
        b.set_pos(32, y_off)
        screen_album_src_btns.append(b)
    screen_album_src.update_layout()

    # ---- 预览浏览页 ----
    screen_album = _mk_scr()
    screen_album_title = lv.label(screen_album)
    screen_album_title.set_text("--")
    screen_album_title.set_pos(0, 0)
    screen_album_title.set_size(240, 22)
    screen_album_title.set_style_text_color(lv.color_hex(0xffffff), lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_album_title.set_style_text_font(_font("montserratMedium", 12), lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_album_title.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_album_title.set_style_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_album_title.set_style_bg_color(lv.color_hex(0x000000), lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_album_title.set_style_pad_all(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_album_title.set_style_border_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_album_title.set_style_radius(0, lv.PART.MAIN | lv.STATE.DEFAULT)

    # 预览图占位（240x180，y=22，留给底部按钮 207+）
    screen_album_img = lv.img(screen_album)
    screen_album_img.set_pos(0, 22)
    screen_album_img.set_size(240, 180)
    screen_album_img.set_style_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_album_img.set_style_bg_color(lv.color_hex(0x222222), lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_album_img.set_style_border_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    screen_album_img.set_style_radius(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    try: screen_album_img.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    except Exception: pass

    # 底部 5 按钮（PREV / NEXT / DEL / ANALYZE / BACK）
    # SEARCH 符号兜底：若当前 LVGL 版本没有则用文字 "AI "
    _analyze_symbol = getattr(lv.SYMBOL, "SEARCH", "AI ")
    btn_cfg = [
        (lv.SYMBOL.LEFT + " ", 9),       # PREV
        (lv.SYMBOL.RIGHT + " ", 56),     # NEXT
        (lv.SYMBOL.CLOSE + " ", 103),    # DEL
        (_analyze_symbol + " ", 150),    # ANALYZE（放大镜 / AI 兜底）
        (lv.SYMBOL.HOME + " ", 197),     # BACK
    ]
    screen_album_bottom_btns = []
    for symbol, x_off in btn_cfg:
        b = _mk_btn(screen_album, symbol, 43, 25)
        b.set_pos(x_off, 207)
        # 符号按钮文本居中
        _lbl = b.get_child(0)
        if _lbl is not None:
            try:
                _lbl.align(lv.ALIGN.CENTER, 0, 0)
                _lbl.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN | lv.STATE.DEFAULT)
            except Exception:
                pass
        screen_album_bottom_btns.append(b)
    screen_album.update_layout()


def refresh_album_src_btns():
    for i, b in enumerate(screen_album_src_btns):
        _style_btn(b, i == _album_src_idx)
    _flush()


def refresh_album_btns():
    for i, b in enumerate(screen_album_bottom_btns):
        _style_btn(b, i == _bottom_idx)
    _flush()


def update_album_image():
    """刷新相册预览页的图和标题（安全版：stat 检查 + 从不 set_src(None)）。"""
    global screen_album_img, screen_album_title, _album_idx
    if screen_album_img is None or screen_album_title is None:
        logger.warn("[album] img/title 未初始化")
        return

    # 若上一轮因空列表隐藏了图片，这里先解除隐藏，恢复可显示
    try:
        screen_album_img.clear_flag(lv.obj.FLAG.HIDDEN)
    except Exception:
        pass

    if not _album_photos:
        screen_album_title.set_text("No Photos")
        # 隐藏图片对象，避免残留最后一张被删照片
        try:
            screen_album_img.add_flag(lv.obj.FLAG.HIDDEN)
        except Exception:
            # 兜底：若 flag API 不可用，置灰图片源的引用
            try:
                screen_album_img.set_style_bg_color(
                    lv.color_hex(0x222222), lv.PART.MAIN | lv.STATE.DEFAULT)
                screen_album_img.set_style_bg_opa(255, lv.PART.MAIN | lv.STATE.DEFAULT)
            except Exception:
                pass
            # 强刷一行覆盖残留
            try:
                screen_album_img.set_size(240, 180)
            except Exception:
                pass
        _flush()
        return
    if _album_idx >= len(_album_photos):
        _album_idx = 0
    entry = _album_photos[_album_idx]
    name = entry[0]
    source = entry[1] if len(entry) > 1 else "local"
    photo_url = entry[2] if len(entry) > 2 else ""

    # 标题: "3/5 photo_003.jpg"
    screen_album_title.set_text("{}/{} {}".format(
        _album_idx + 1, len(_album_photos), name))

    # 清图（先刷新标题，再尝试换图）
    _flush()

    target_path = None
    try:
        if source == "local":
            if _on_get_photo_path is not None:
                target_path = _on_get_photo_path(name)
                logger.info("[album] get_photo_path(%s) → %s" % (name, target_path))
        else:
            if _on_prepare_cloud_photo is not None:
                target_path = _on_prepare_cloud_photo(name, photo_url)
                logger.info("[album] prepare_cloud(%s) → %s" % (name, target_path))
    except Exception as e:
        logger.error("[album] 获取路径异常: %s" % e)

    if target_path is None:
        logger.warn("[album] %s 无可用路径，跳过载图" % name)
        _flush()
        return

    # stat 检查文件确实存在（防止 set_src 不存在的文件导致 C 层崩溃）
    real_path = target_path.replace("U:/", "/usr/")
    try:
        import uos
        uos.stat(real_path)
        logger.info("[album] stat OK: %s → %s" % (target_path, real_path))
    except Exception as e:
        logger.error("[album] 文件不存在: %s (%s)" % (target_path, e))
        return

    # set_src — 这个可能触发 LVGL C 层 JPEG 解码，必须在 stat 通过后才调
    try:
        screen_album_img.set_src(target_path)
        logger.info("[album] set_src OK: %s" % target_path)
    except Exception as e:
        logger.error("[album] set_src Python 异常: %s" % e)
        import sys
        try: sys.print_exception(e)
        except Exception: pass
    _flush()


def cleanup_prev_files():
    """清理云端相册的临时预览文件 /usr/_prev_*.jpg（进入云端源时调用，防止堆积）。"""
    try:
        import uos
        removed = 0
        for name in uos.listdir("/usr"):
            try:
                low = str(name).lower()
                if low.startswith("_prev_") and (low.endswith(".jpg") or low.endswith(".jpeg")):
                    uos.remove("/usr/" + str(name))
                    removed += 1
            except Exception:
                pass
        if removed:
            logger.info("[album] 已清理 %d 个云端预览临时文件" % removed)
    except Exception as e:
        logger.warn("[album] 清理预览临时文件失败: %s" % e)


def populate_album_photos(photos, source="local"):
    """填充相册照片列表。

    Args:
        photos: [(name, source, url), ...] 或 [name, ...] 或 [(name, source), ...]
        source: 默认来源（photos 未带 source 时用）
    """
    global _album_photos, _album_source, _album_idx
    _album_source = source
    result = []
    for entry in photos or []:
        if isinstance(entry, (tuple, list)):
            n = entry[0]
            s = entry[1] if len(entry) > 1 else source
            u = entry[2] if len(entry) > 2 else ""
        else:
            n, s, u = entry, source, ""
        if n:
            result.append((n, s, u))
    _album_photos = result
    _album_idx = 0


def refresh_analyze_btns():
    for i, b in enumerate(screen_analyze_bottom_btns):
        _style_btn(b, i == _bottom_idx)
    _flush()


def _style_analyze_src_btns():
    for i, b in enumerate(screen_analyze_src_btns):
        _style_btn(b, i == _analyze_src_idx)
    _flush()


def analyze_cursor_reset():
    global _bottom_idx
    _bottom_idx = 0


# ============================================================
# 4. 界面刷新（直接设背景色 + 强制刷新，绕开 CHECKED 坑）
# ============================================================

def _flush():
    """强制 LVGL 立即重绘当前屏幕（refr_now 需要显示驱动对象，不是屏幕对象）。"""
    try:
        disp = lv.disp_get_default()
        lv.refr_now(disp)
    except Exception as e:
        logger.warn("refr_now 异常: %s" % e)


def _get_btn_text(btn):
    """取按钮内 label 的文本（安全，任何异常返回空串）。"""
    try:
        lbl = btn.get_child(0)
        if lbl is not None:
            return lbl.get_text() or ""
    except Exception:
        pass
    return ""


def _style_btn(b, checked):
    """直接设置按钮背景色。"""
    b.set_style_bg_color(lv.color_hex(_C_BLUE if checked else _C_GRAY), lv.PART.MAIN)


def _style_list_item(item, checked):
    """直接设置 List 项背景色 + 文字色。"""
    item.set_style_bg_color(lv.color_hex(_C_LIST_SEL_BG if checked else _C_LIST_BG), lv.PART.MAIN)
    item.set_style_text_color(lv.color_hex(_C_LIST_SEL_TEXT if checked else _C_LIST_TEXT), lv.PART.MAIN)


def refresh_menu():
    for i, b in enumerate(screen_menu_btns):
        _style_btn(b, i == _menu_idx)
    _flush()


def refresh_bottom():
    for i, b in enumerate(screen_delete_bottom_btns):
        _style_btn(b, i == _bottom_idx)
    _flush()


def refresh_list_check():
    """刷新列表高亮，并让选中项滚入可视区（照片多时列表跟随滚动）。

    用 LVGL 原生的 scroll_to_view：内部自动计算坐标，保证选中项可见。
    照片多、列表显示不全时，用户按 UP/DOWN 选择到可视区外的照片，
    列表会自动滚动把选中项带进来，用户视觉上能看到超出列表的其他照片。
    """
    for i, item in enumerate(_list_items):
        _style_list_item(item, i == _list_idx)
    if _list_items and 0 <= _list_idx < len(_list_items):
        sel = _list_items[_list_idx]
        try:
            # ANIM.OFF 立即生效，避免动画过程中被 task_handler 的其他重绘覆盖
            sel.scroll_to_view(lv.ANIM.OFF)
        except Exception as e:
            logger.warn("scroll_to_view 失败: %s" % e)
    _flush()


def _populate_list_to(target_list, photo_names):
    """把照片列表填充到指定 lv.list 控件（populate_list 写死用 screen_delete_list，这里更通用）。"""
    items = []
    names = []
    sources = []
    if not photo_names:
        it = target_list.add_btn(lv.SYMBOL.WARNING, "No photos")
        items.append(it); names.append(""); sources.append("none")
    else:
        for entry in photo_names:
            if isinstance(entry, (tuple, list)):
                n, s = entry[0], entry[1] if len(entry) > 1 else "local"
            else:
                n, s = entry, "local"
            it = target_list.add_btn(lv.SYMBOL.IMAGE, n)
            items.append(it); names.append(n); sources.append(s)
    return items, names, sources


def populate_analyze_list(photo_names):
    global _list_items, _list_names, _list_sources, _list_urls, _list_idx
    # 先清旧（但不删控件，复用同一个 list 的 add_btn 会叠加！—— 用 delete 方案）
    for item in _list_items:
        try: item.delete()
        except Exception: pass
    _list_items = []; _list_names = []; _list_sources = []; _list_urls = []
    if photo_names:
        for entry in photo_names:
            if isinstance(entry, (tuple, list)):
                n = entry[0]
                s = entry[1] if len(entry) > 1 else "local"
                u = entry[2] if len(entry) > 2 else ""
            else:
                n, s, u = entry, "local", ""
            it = screen_analyze_list.add_btn(lv.SYMBOL.IMAGE, n)
            _list_items.append(it)
            _list_names.append(n)
            _list_sources.append(s)
            _list_urls.append(u)
    else:
        it = screen_analyze_list.add_btn(lv.SYMBOL.WARNING, "No photos")
        _list_items.append(it)
        _list_names.append("")
        _list_sources.append("none")
        _list_urls.append("")
    _list_idx = 0


def populate_list(photo_names):
    """用真实照片名刷新 List。

    Args:
        photo_names: [(name, source), ...] 或 [name, ...]（默认来源 local）
    """
    global _list_items, _list_names, _list_sources, _list_idx
    for item in _list_items:
        try:
            item.delete()
        except Exception:
            pass
    _list_items = []
    _list_names = []
    _list_sources = []
    if not photo_names:
        item = screen_delete_list.add_btn(lv.SYMBOL.WARNING, "No photos")
        _list_items.append(item)
        _list_names.append("")
        _list_sources.append("none")
        _list_idx = 0
        refresh_list_check()
        return
    for entry in photo_names:
        if isinstance(entry, (tuple, list)):
            name, src = entry[0], entry[1] if len(entry) > 1 else "local"
        else:
            name, src = entry, "local"
        item = screen_delete_list.add_btn(lv.SYMBOL.IMAGE, name)
        _list_items.append(item)
        _list_names.append(name)
        _list_sources.append(src)
    _list_idx = 0
    refresh_list_check()


# ============================================================
# 5. 确认对话框
# ============================================================

def show_confirm_dialog(photo_name):
    global _dialog_obj, _dialog_btns, _dialog_idx, _dialog_photo
    if _dialog_obj is not None:
        return
    _dialog_photo = photo_name

    mask = lv.obj(lv.scr_act())
    mask.set_size(240, 240)
    mask.set_pos(0, 0)
    mask.set_style_bg_opa(200, lv.PART.MAIN | lv.STATE.DEFAULT)
    mask.set_style_bg_color(lv.color_hex(0x000000), lv.PART.MAIN | lv.STATE.DEFAULT)
    mask.set_style_border_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    mask.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)

    w, h = 180, 145
    box = lv.obj(mask)
    box.set_size(w, h)
    box.align(lv.ALIGN.CENTER, 0, 0)
    box.set_style_bg_color(lv.color_hex(0xffffff), lv.PART.MAIN | lv.STATE.DEFAULT)
    box.set_style_border_width(2, lv.PART.MAIN | lv.STATE.DEFAULT)
    box.set_style_border_color(lv.color_hex(_C_BLUE), lv.PART.MAIN | lv.STATE.DEFAULT)
    box.set_style_radius(8, lv.PART.MAIN | lv.STATE.DEFAULT)
    box.set_style_pad_all(8, lv.PART.MAIN | lv.STATE.DEFAULT)

    title = lv.label(box)
    title.set_text("Delete?")
    title.align(lv.ALIGN.TOP_MID, 0, -2)
    title.set_style_text_color(lv.color_hex(0x000000), lv.PART.MAIN | lv.STATE.DEFAULT)
    title.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN | lv.STATE.DEFAULT)

    name_lbl = lv.label(box)
    name_lbl.set_text(photo_name)
    name_lbl.align(lv.ALIGN.TOP_MID, 0, 12)
    name_lbl.set_style_text_color(lv.color_hex(0x333333), lv.PART.MAIN | lv.STATE.DEFAULT)
    name_lbl.set_style_text_align(lv.TEXT_ALIGN.CENTER, lv.PART.MAIN | lv.STATE.DEFAULT)
    name_lbl.set_long_mode(lv.label.LONG.DOT)
    name_lbl.set_width(w - 20)

    btn_cfg = [("Local Only", 32), ("Cloud Only", 57), ("Both", 82), ("Cancel", 107)]
    dlg_btns = []
    for text, y_off in btn_cfg:
        b = _mk_btn(box, text, w - 20, 22)
        b.align(lv.ALIGN.TOP_MID, 0, y_off)
        dlg_btns.append(b)

    _dialog_obj = mask
    _dialog_btns = dlg_btns
    _dialog_idx = 0
    for i, b in enumerate(_dialog_btns):
        _style_btn(b, i == _dialog_idx)
    _flush()
    logger.info("对话框打开: %s" % photo_name)


def close_confirm_dialog():
    global _dialog_obj, _dialog_btns, _dialog_idx, _dialog_photo
    if _dialog_obj is not None:
        _dialog_obj.delete()
    _dialog_obj = None
    _dialog_btns = []
    _dialog_idx = 0
    _dialog_photo = ""


def dialog_is_open():
    return _dialog_obj is not None


def get_dialog_photo():
    return _dialog_photo


def get_dialog_choice():
    """返回当前对话框选中的目标: local/cloud/both/cancel"""
    if _dialog_idx in (0, 1, 2):
        return ["local", "cloud", "both"][_dialog_idx]
    return "cancel"


# ============================================================
# 6. 状态访问器
# ============================================================

def menu_cursor_reset():
    global _menu_idx
    _menu_idx = 0


def delete_cursor_reset():
    global _bottom_idx
    _bottom_idx = 0


def list_idx_max():
    return len(_list_items) - 1


# ============================================================
# 7. 业务回调注入（由 _main 装配，解耦业务依赖）
# ============================================================

_on_load_photos = None
_on_delete_photo = None
_on_enter_mode = None
_on_analyze = None
_on_analyze_speak = None
_on_get_photo_path = None      # 相册预览: 文件名 → POSIX 路径
_on_prepare_cloud_photo = None  # 相册预览: (name, url) → 下载到本地的 POSIX 路径
_on_delete_album_photo = None   # 相册预览删除: (name, source) → void
_photos_local = []
_photos_cloud = []


def set_callbacks(load_photos=None, delete_photo=None, enter_mode=None, analyze=None, analyze_speak=None,
                  get_photo_path=None, prepare_cloud_photo=None, delete_album_photo=None):
    global _on_load_photos, _on_delete_photo, _on_enter_mode, _on_analyze, _on_analyze_speak
    global _on_get_photo_path, _on_prepare_cloud_photo, _on_delete_album_photo
    _on_load_photos = load_photos
    _on_delete_photo = delete_photo
    _on_enter_mode = enter_mode
    _on_analyze = analyze
    _on_analyze_speak = analyze_speak
    _on_get_photo_path = get_photo_path
    _on_prepare_cloud_photo = prepare_cloud_photo
    _on_delete_album_photo = delete_album_photo


def set_photos(local, cloud):
    global _photos_local, _photos_cloud
    _photos_local = local or []
    _photos_cloud = cloud or []


# ============================================================
# 8. 线程锁与 LCD 所有权（复用 camera_flow 的单一状态）
# ============================================================

_cf = None


def _use_camera_flow():
    """懒导入 camera_flow，拿到共享的锁和 owner 访问器。"""
    global _cf
    if _cf is None:
        try:
            from usr import camera_flow as _cf
        except Exception as e:
            logger.warn("导入 camera_flow 失败: %s" % e)
    return _cf


def lcd_owner():
    cf = _use_camera_flow()
    if cf is not None:
        return cf.get_lcd_owner()
    return "lvgl"


def _lock_acquire():
    cf = _use_camera_flow()
    try:
        cf._state_lock.acquire()
    except Exception:
        pass


def _lock_release():
    cf = _use_camera_flow()
    try:
        cf._state_lock.release()
    except Exception:
        pass


# ============================================================
# 9. LVGL 线程主循环 + 按键处理
# ============================================================

_stop = False


def request_stop():
    global _stop
    _stop = True


def _handle_keys(pressed):
    global _menu_idx, _bottom_idx, _list_idx, _dialog_idx
    global _analyze_src_idx, _analyze_filter
    global _album_src_idx, _album_idx
    if not pressed:
        return

    # 对话框优先
    if dialog_is_open():
        if 13 in pressed:
            _dialog_idx = (_dialog_idx + 1) % len(_dialog_btns)
            for i, b in enumerate(_dialog_btns):
                _style_btn(b, i == _dialog_idx)
            _flush()
        if 12 in pressed:
            choice = get_dialog_choice()
            photo = get_dialog_photo()
            close_confirm_dialog()
            if choice != "cancel" and _on_delete_photo is not None:
                _on_delete_photo(photo, choice)
        return

    cur_scr = lv.scr_act()

    if cur_scr == screen_menu:
        if 13 in pressed:
            _menu_idx = (_menu_idx + 1) % len(screen_menu_btns)
            refresh_menu()
        if 12 in pressed:
            mode_map = ["voice", "manual"]
            if _menu_idx < 2:
                if _on_enter_mode is not None:
                    _on_enter_mode(mode_map[_menu_idx])
            elif _menu_idx == 2:       # Photo Preview
                lv.scr_load(screen_album_src)
                _album_src_idx = 0
                refresh_album_src_btns()
                logger.info("→ 相册来源选择")
            elif _menu_idx == 3:       # Interval Photo
                if _on_enter_mode is not None:
                    _on_enter_mode("interval")
        return

    if cur_scr == screen_delete:
        if 13 in pressed:
            _bottom_idx = (_bottom_idx + 1) % len(screen_delete_bottom_btns)
            refresh_bottom()
        if 12 in pressed:
            if _bottom_idx == 0:          # HOME 返回主菜单
                lv.scr_load(screen_menu)
                menu_cursor_reset()
                refresh_menu()
                logger.info("← 回主菜单")
            elif _bottom_idx == 1:        # UP（循环：到头回尾）
                if _list_items:
                    _list_idx = (_list_idx - 1) % len(_list_items)
                refresh_list_check()
                logger.info("List up -> %d" % _list_idx)
            elif _bottom_idx == 2:        # DOWN（循环：到尾回头）
                if _list_items:
                    _list_idx = (_list_idx + 1) % len(_list_items)
                refresh_list_check()
                logger.info("List down -> %d" % _list_idx)
            elif _bottom_idx == 3:        # OK
                if _list_idx < len(_list_names) and _list_names[_list_idx]:
                    show_confirm_dialog(_list_names[_list_idx])
                else:
                    logger.warn("OK: 没有选中的照片")

    if cur_scr == screen_analyze_src:
        if 13 in pressed:
            _analyze_src_idx = (_analyze_src_idx + 1) % len(screen_analyze_src_btns)
            _style_analyze_src_btns()
        if 12 in pressed:
            # Back 项（最后一项 -> 文本以 "Back" 开头）
            is_back = False
            if 0 <= _analyze_src_idx < len(screen_analyze_src_btns):
                _btxt = ""
                _bl = screen_analyze_src_btns[_analyze_src_idx].get_child(0)
                if _bl is not None:
                    try: _btxt = _bl.get_text() or ""
                    except Exception: pass
                is_back = ("Back" in _btxt)
            if is_back:          # Back
                lv.scr_load(screen_menu)
                menu_cursor_reset()
                refresh_menu()
                logger.info("← 回主菜单")
            else:
                _analyze_filter = ["local", "cloud"][_analyze_src_idx] if _analyze_src_idx < 2 else "local"
                analyze_cursor_reset()
                _list_idx = 0
                lv.scr_load(screen_analyze)
                logger.info("→ 图像识别页 (source=%s)" % _analyze_filter)
                refresh_analyze_btns()
                if _on_load_photos is not None:
                    _on_load_photos(is_analyze=True)
        return

    if cur_scr == screen_analyze:
        if 13 in pressed:
            _bottom_idx = (_bottom_idx + 1) % len(screen_analyze_bottom_btns)
            refresh_analyze_btns()
        if 12 in pressed:
            if _bottom_idx == 0:          # HOME → 返回来源选择页
                lv.scr_load(screen_analyze_src)
                _analyze_src_idx = 0
                _style_analyze_src_btns()
                logger.info("← 回来源选择页")
            elif _bottom_idx == 1:        # UP
                if _list_items:
                    _list_idx = (_list_idx - 1) % len(_list_items)
                refresh_list_check()
            elif _bottom_idx == 2:        # DOWN
                if _list_items:
                    _list_idx = (_list_idx + 1) % len(_list_items)
                refresh_list_check()
            elif _bottom_idx == 3:        # OK → 触发分析
                if _list_idx < len(_list_names) and _list_names[_list_idx]:
                    photo = _list_names[_list_idx]
                    src_tag = _list_sources[_list_idx] if _list_idx < len(_list_sources) else "local"
                    photo_url = _list_urls[_list_idx] if _list_idx < len(_list_urls) else ""
                    _trigger_analyze(photo, src_tag, photo_url)
                else:
                    logger.warn("OK: 没有选中的照片")

    if cur_scr == screen_album_src:
        if 13 in pressed:
            _album_src_idx = (_album_src_idx + 1) % len(screen_album_src_btns)
            refresh_album_src_btns()
        if 12 in pressed:
            logger.info("[album_src] OK idx=%d" % _album_src_idx)
            is_back = ("Back" in _get_btn_text(screen_album_src_btns[_album_src_idx]))
            if is_back:
                lv.scr_load(screen_menu)
                menu_cursor_reset()
                refresh_menu()
                logger.info("← 回主菜单（来自相册来源选择）")
            else:
                source = ["local", "cloud"][_album_src_idx] if _album_src_idx < 2 else "local"
                logger.info("[album_src] source=%s, 开始加载列表..." % source)
                _bottom_idx = 0
                photos = []
                if source == "local" and _on_load_photos is not None:
                    try:
                        logger.info("[album_src] 调用 _on_load_photos()...")
                        _on_load_photos()
                        logger.info("[album_src] _on_load_photos() 返回, local=%d" % len(_photos_local))
                    except Exception as e:
                        logger.error("[album_src] _on_load_photos 异常: %s" % e)
                    for n in _photos_local:
                        photos.append((n, "local", ""))
                elif source == "cloud" and _on_load_photos is not None:
                    # 进入云端相册前清理旧预览临时文件，防止设备堆积
                    cleanup_prev_files()
                    try:
                        _on_load_photos()
                    except Exception as e:
                        logger.error("[album_src] _on_load_photos 异常: %s" % e)
                    for item in _photos_cloud:
                        if isinstance(item, (tuple, list)):
                            n = item[0]
                            u = item[2] if len(item) > 2 else ""
                        else:
                            n, u = item, ""
                        photos.append((n, "cloud", u))
                logger.info("[album_src] populate_album_photos(%d items)" % len(photos))
                populate_album_photos(photos, source)
                logger.info("[album_src] lv.scr_load(screen_album)...")
                lv.scr_load(screen_album)
                refresh_album_btns()
                logger.info("[album_src] update_album_image()...")
                update_album_image()
                logger.info("[album_src] 加载完成, count=%d" % len(photos))
        return

    if cur_scr == screen_album:
        if 13 in pressed:
            _bottom_idx = (_bottom_idx + 1) % len(screen_album_bottom_btns)
            refresh_album_btns()
        if 12 in pressed:
            if _bottom_idx == 0:          # PREV
                if _album_photos:
                    _album_idx = (_album_idx - 1) % len(_album_photos)
                update_album_image()
                logger.info("[album] PREV idx=%d" % _album_idx)
            elif _bottom_idx == 1:        # NEXT
                if _album_photos:
                    _album_idx = (_album_idx + 1) % len(_album_photos)
                update_album_image()
                logger.info("[album] NEXT idx=%d" % _album_idx)
            elif _bottom_idx == 2:        # DEL
                if _album_photos and _album_idx < len(_album_photos):
                    entry = _album_photos[_album_idx]
                    name = entry[0]
                    src = entry[1] if len(entry) > 1 else "local"
                    deleted = False
                    if _on_delete_album_photo is not None:
                        try:
                            deleted = _on_delete_album_photo(name, src)
                            logger.info("[album] DELETE %s (%s) -> %s" % (name, src, deleted))
                        except Exception as e:
                            logger.error("[album] DELETE 异常: %s" % e)
                    # 删除成功后从列表移除并刷新
                    if deleted:
                        del _album_photos[_album_idx]
                        if _album_idx >= len(_album_photos):
                            _album_idx = max(0, len(_album_photos) - 1)
                    update_album_image()
                else:
                    logger.warn("[album] DELETE: 没有照片可删")
            elif _bottom_idx == 3:        # ANALYZE
                if _album_photos and _album_idx < len(_album_photos):
                    entry = _album_photos[_album_idx]
                    name = entry[0]
                    src = entry[1] if len(entry) > 1 else "local"
                    photo_url = entry[2] if len(entry) > 2 else ""
                    logger.info("[album] ANALYZE %s (%s)" % (name, src))
                    # 先释放 lv.img 内存（关键！分析要占 ~100KB RAM）
                    try:
                        if screen_album_img is not None:
                            screen_album_img.add_flag(lv.obj.FLAG.HIDDEN)
                    except Exception:
                        pass
                    _flush()
                    try:
                        import gc
                        gc.collect()
                    except Exception:
                        pass
                    # 复用已有分析函数（遮罩 + 后台线程 + TTS 播报）
                    try:
                        _trigger_analyze(name, src, photo_url)
                    except Exception as e:
                        logger.error("[album] ANALYZE 异常: %s" % e)
                else:
                    logger.warn("[album] ANALYZE: 没有照片可分析")
            elif _bottom_idx == 4:        # BACK → 返回来源选择页
                lv.scr_load(screen_album_src)
                _album_src_idx = 0
                refresh_album_src_btns()
                logger.info("← 回相册来源选择页")
        return

def _show_analyzing():
    """显示"Analyzing..."遮罩，防止重复点击。"""
    global _analyze_overlay
    _hide_analyzing()
    mask = lv.obj(lv.scr_act())
    mask.set_size(240, 240)
    mask.set_pos(0, 0)
    mask.set_style_bg_opa(200, lv.PART.MAIN | lv.STATE.DEFAULT)
    mask.set_style_bg_color(lv.color_hex(0x000000), lv.PART.MAIN | lv.STATE.DEFAULT)
    mask.set_style_border_width(0, lv.PART.MAIN | lv.STATE.DEFAULT)
    try: mask.set_scrollbar_mode(lv.SCROLLBAR_MODE.OFF)
    except Exception: pass
    box = lv.obj(mask)
    box.set_size(180, 60)
    box.align(lv.ALIGN.CENTER, 0, 0)
    box.set_style_bg_color(lv.color_hex(0xffffff), lv.PART.MAIN | lv.STATE.DEFAULT)
    box.set_style_border_width(2, lv.PART.MAIN | lv.STATE.DEFAULT)
    box.set_style_border_color(lv.color_hex(_C_BLUE), lv.PART.MAIN | lv.STATE.DEFAULT)
    box.set_style_radius(8, lv.PART.MAIN | lv.STATE.DEFAULT)
    title = lv.label(box)
    title.set_text("Analyzing...")
    title.align(lv.ALIGN.CENTER, 0, -8)
    title.set_style_text_color(lv.color_hex(0x000000), lv.PART.MAIN | lv.STATE.DEFAULT)
    tip = lv.label(box)
    tip.set_text("Wait please")
    tip.align(lv.ALIGN.CENTER, 0, 12)
    tip.set_style_text_color(lv.color_hex(0x666666), lv.PART.MAIN | lv.STATE.DEFAULT)
    _analyze_overlay = mask
    _flush()


def _hide_analyzing():
    global _analyze_overlay
    if _analyze_overlay is not None:
        try: _analyze_overlay.delete()
        except Exception: pass
        _analyze_overlay = None
        _flush()


def _trigger_analyze(photo_name, source, photo_url=""):
    """后台线程执行视觉分析（不阻塞 LVGL）。"""
    if _on_analyze is None:
        logger.error("未注入 analyze 回调")
        return
    _show_analyzing()
    logger.info("启动图像分析: %s (src=%s url=%s)" % (
        photo_name, source, photo_url or "-"))

    def _worker():
        desc = None
        try:
            desc = _on_analyze(photo_name, photo_url)
        except Exception as e:
            logger.error("图像分析 worker 异常: %s" % repr(e))
        # 回到 LVGL 线程关遮罩（用 LVGL 线程的定时器也可以，但简单起见直接 try）
        try:
            _hide_analyzing()
        except Exception as e:
            logger.error("关遮罩异常: %s" % e)
        if desc:
            logger.info("===== 图像识别结果 =====")
            logger.info("照片: %s" % photo_name)
            logger.info("结果: %s" % desc)
            logger.info("=======================")
            # 语音播报识别结果
            try:
                if _on_analyze_speak is not None:
                    _on_analyze_speak(desc)
                else:
                    logger.warn("未注入播报回调，跳过 TTS")
            except Exception as e:
                logger.error("TTS 播报异常: %s" % repr(e))
        else:
            logger.error("图像分析失败: %s" % photo_name)

    try:
        _thread.start_new_thread(_worker, ())
    except Exception as e:
        logger.error("启动分析线程失败: %s" % repr(e))
        _hide_analyzing()


def run_lvgl_thread(cam, gitee, enter_mode, events_fn, vision=None):
    """LVGL 线程主循环。

    参数:
        cam, gitee: 业务对象（用于删除时注入回调）
        enter_mode: 进入摄像头模式函数
        events_fn: 返回被按下 GPIO 号集合的函数（复用 InputDriver 的按键事件）
    """
    logger.info("[LVGL thread] 启动")
    _setup_business_bridge(cam, gitee, enter_mode, vision)

    while not _stop:
        if lcd_owner() == "lvgl":
            _lock_acquire()
            try:
                # 退出相机后需要恢复主菜单：在 LVGL 线程内串行执行，避免跨线程重绘竞争
                cf = _use_camera_flow()
                if cf is not None and cf.consume_restore_menu():
                    menu_cursor_reset()
                    lv.scr_load(screen_menu)
                    refresh_menu()
                    logger.info("LVGL 恢复，回到主菜单")
                lv.task_handler()
            except Exception as e:
                logger.error("[LVGL] task_handler 异常: %s" % e)
            finally:
                _lock_release()

            try:
                pressed = events_fn()
                if pressed:
                    logger.info("[UI] key pressed: %s" % sorted(list(pressed)))
                _handle_keys(pressed)
            except Exception as e:
                logger.error("[LVGL] 按键处理异常: %s" % e)
                sys.print_exception(e)
        utime.sleep_ms(10)
    logger.info("[LVGL thread] 退出")


def _setup_business_bridge(cam, gitee, enter_mode, vision=None):
    """在 ui 层注入业务回调，让 UI 能加载/删除照片、进入相机、图像识别。"""

    def load_photos(is_analyze=False):
        local = []
        cloud = []
        if cam is not None:
            try:
                local = cam.list_photos() or []
            except Exception as e:
                logger.warn("本地列表异常: %s" % e)
        if gitee is not None and gitee.enabled:
            try:
                cloud = gitee.list_photos() or []
            except Exception as e:
                logger.warn("云端列表异常: %s" % e)
        set_photos(local, cloud)
        if is_analyze:
            # 识别页：按来源过滤（Local/Cloud/All）。
            # 本地照片直接读文件；云端照片用其 download_url（Gitee 公开仓库）
            # 交百度服务端拉取，设备零下载、不落盘，烧录不会冲掉。
            shown = []
            seen = set()
            flt = _analyze_filter if _analyze_filter in ("local", "cloud", "all") else "all"
            if flt in ("local", "all"):
                for n in local:
                    if n not in seen:
                        shown.append((n, "local", ""))
                        seen.add(n)
            if flt in ("cloud", "all"):
                for item in cloud:
                    if isinstance(item, (tuple, list)) and len(item) >= 1:
                        cname = item[0]
                        curl = item[2] if len(item) > 2 else ""
                    else:
                        cname, curl = item, ""
                    if cname not in seen:
                        shown.append((cname, "cloud", curl))
                        seen.add(cname)
            populate_analyze_list(shown)
            logger.info("识别页: filter=%s 本地=%d 云端=%d 显示=%d" % (
                flt, len(local), len(cloud), len(shown)))
            return
        # 删除页：合并显示本地+云端
        shown = []
        seen = set()
        for name in local:
            if name not in seen:
                shown.append((name, "local"))
                seen.add(name)
        for item in cloud:
            name = item[0] if isinstance(item, (tuple, list)) else item
            if name not in seen:
                shown.append((name, "cloud"))
                seen.add(name)
        populate_list(shown)
        logger.info("照片列表: 本地=%d 云端=%d 显示=%d %s" % (
            len(local), len(cloud), len(shown),
            ("(本地: %s)" % ",".join(local[:3])) if local else "(本地为空)",
        ))

    def delete_photo(photo_name, target):
        deleted_any = False
        if target in ("local", "both") and cam is not None:
            try:
                ok = cam.delete_photo(photo_name)
                logger.info("本地删除 %s -> %s" % (photo_name, "OK" if ok else "FAIL"))
                if ok:
                    deleted_any = True
            except Exception as e:
                logger.error("本地删除异常: %s" % e)
        if target in ("cloud", "both") and gitee is not None and gitee.enabled:
            sha = ""
            for item in _photos_cloud:
                if item[0] == photo_name:
                    sha = item[1] if len(item) > 1 else ""
                    break
            if sha:
                try:
                    ok = gitee.delete_photo(photo_name, sha)
                    logger.info("云端删除 %s -> %s" % (photo_name, "OK" if ok else "FAIL"))
                    if ok:
                        deleted_any = True
                except Exception as e:
                    logger.error("云端删除异常: %s" % e)
        load_photos()

    def enter_mode_packed(mode):
        """把 enter_mode(mode, cam) 包成单参，供 UI 回调调用。"""
        try:
            return enter_mode(mode, cam)
        except Exception as e:
            logger.error("进入相机模式失败: %s" % e)
            return False

    def analyze_wrapper(photo_name, photo_url=""):
        if vision is None:
            logger.error("vision not injected")
            return None
        return vision.analyze(photo_name, url=(photo_url or None))

    def analyze_speak_wrapper(text):
        if vision is None:
            logger.error("vision not injected")
            return None
        return vision.tts_play(text)

    def get_photo_path_wrapper(filename):
        """相册预览: 文件名 → POSIX 路径"""
        if cam is None:
            return None
        try:
            return cam.get_photo_path(filename)
        except Exception as e:
            logger.warn("get_photo_path 失败: %s" % e)
            return None

    def prepare_cloud_photo_wrapper(name, photo_url):
        """相册预览: 下载云端照片到本地供 lv.img 显示。

        照片原生 240x240，lv.img 可零变换干净显示。
        下载到按照片名区分的唯一临时路径（_prev_ 前缀不会进相册列表），
        避免 LVGL 对相同 set_src 源字符串的去重缓存导致停在上一张。
        """
        if gitee is None or not gitee.enabled:
            return None
        try:
            base = name[:-4] if name.lower().endswith(".jpg") else name
            dest = "/usr/_prev_{}.jpg".format(base)
            local_path = gitee.download_photo(name, photo_url, dest)
            if local_path:
                return "U:/_prev_{}.jpg".format(base)
            return None
        except Exception as e:
            logger.warn("prepare_cloud_photo 失败: %s" % e)
            return None

    def delete_album_photo_wrapper(name, source):
        """相册预览删除回调：删除本地或 Gitee 照片，返回是否删除成功。"""
        deleted_any = False
        if source == "local":
            if cam is not None:
                try:
                    ok = cam.delete_photo(name)
                    if ok:
                        deleted_any = True
                    logger.info("[album] 本地删除 %s -> %s" % (name, "OK" if ok else "FAIL"))
                except Exception as e:
                    logger.error("[album] 本地删除异常: %s" % e)
        elif source == "cloud":
            if gitee is not None and gitee.enabled:
                sha = ""
                for item in _photos_cloud:
                    if isinstance(item, (tuple, list)) and len(item) > 1 and item[0] == name:
                        sha = item[1]
                        break
                if sha:
                    try:
                        ok = gitee.delete_photo(name, sha)
                        if ok:
                            deleted_any = True
                        logger.info("[album] 云端删除 %s -> %s" % (name, "OK" if ok else "FAIL"))
                    except Exception as e:
                        logger.error("[album] 云端删除异常: %s" % e)
                else:
                    logger.warn("[album] 云端删除: 找不到 %s 的 sha" % name)
        # 顺带清理该照片的云端预览临时文件 /usr/_prev_{name}.jpg（避免残留）
        if deleted_any:
            try:
                import uos
                base = name[:-4] if name.lower().endswith(".jpg") else name
                for cand in ("/usr/_prev_{}.jpg".format(base),
                             "/usr/_prev_{}.jpeg".format(base)):
                    try:
                        uos.remove(cand)
                        logger.info("[album] 已清理临时预览: %s" % cand)
                    except OSError:
                        pass
                    except Exception:
                        pass
            except Exception as e:
                logger.warn("[album] 清理临时预览失败: %s" % e)

        return deleted_any

    set_callbacks(load_photos=load_photos, delete_photo=delete_photo, enter_mode=enter_mode_packed,
                  analyze=analyze_wrapper, analyze_speak=analyze_speak_wrapper,
                  get_photo_path=get_photo_path_wrapper,
                  prepare_cloud_photo=prepare_cloud_photo_wrapper,
                  delete_album_photo=delete_album_photo_wrapper)
