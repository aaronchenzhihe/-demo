# -*- coding: UTF-8 -*-
"""摄像头服务层：封装 camCapture 拍照流程。

关键依赖（方案文档 & 测试验证）：
  1. LCD 必须先初始化（否则 camCapture 回调不触发）
  2. camCapture 默认保存到 U:/，回调中移动到 U:/photos/ 目录
  3. 循环存储：最多保留 CAM_MAX_PHOTOS 张

API（MCP 核实 retriever_api_reference_docs）：
  from camera import camCapture
  cap = camCapture(model=0, cam_w=640, cam_h=480, perview_level=1, lcd_w=240, lcd_h=240)
  cap.open()                         → 使能拍照（返回 0=成功）
  cap.callback(func)                 → 注册回调 func(result)
  cap.start(w, h, name)              → 拍照，自动加 .jpg，保存到 U:/
  cap.close()                        → 关闭

  os.listdir(path) → 文件列表
  os.remove(path)  → 删除文件
  os.rename(old, new) → 重命名
  uos.statvfs(path) → 获取存储信息
"""
import uos
import utime
from camera import camCapture
from usr.logging import getLogger
from usr.config import (
    CAM_MODEL,
    CAM_PREVIEW_LEVEL,
    CAM_SENSOR_WIDTH,
    CAM_SENSOR_HEIGHT,
    CAM_CAPTURE_WIDTH,
    CAM_CAPTURE_HEIGHT,
    CAM_PHOTO_DIR,
    CAM_MAX_PHOTOS,
    TIMED_CAM_W,
    TIMED_CAM_H,
)

logger = getLogger("camera")


def _is_photo_name(fname):
    """判断文件名是否为照片（QuecPython 的 endswith 不支持 tuple 参数，需逐个判断）。"""
    low = fname.lower()
    return low.endswith(".jpg") or low.endswith(".jpeg")


# 定时照片云端最大编号持久化文件（本地帧删了但云端还在，下次命名从这个继续避免重名）
TIMED_STATE_FILE = "/usr/_timed_count.txt"


def _read_persisted_max(prefix):
    """读持久化的云端最大编号（仅定时帧有效）。

    上传成功后 _main.py 会把最大编号写到此文件，下次命名先读它再结合本地扫描。
    普通帧（photo_）不走持久化，返回 0。
    """
    if not prefix.startswith("timed_"):
        return 0
    try:
        with open(TIMED_STATE_FILE, "r") as f:
            content = f.read().strip()
        if content.isdigit():
            return int(content)
    except Exception:
        pass
    return 0


def _write_persisted_max(prefix, photo_name):
    """持久化云端最大编号（定时帧上传成功后调用）。

    Args:
        prefix: 文件名前缀（如 "timed_photo_"）
        photo_name: 完整文件名（如 "timed_photo_001.jpg"），内部提取编号
    """
    if not prefix.startswith("timed_") or not photo_name:
        return
    # 从文件名里提取编号（复用 CameraService._photo_number 的静态方法）
    num = CameraService._photo_number(photo_name, prefix)
    if num <= 0:
        return
    try:
        with open(TIMED_STATE_FILE, "w") as f:
            # 用 %d 格式化（QuecPython 文件 write 对 str() 转换可能有兼容性问题）
            f.write("%d" % num)
    except Exception as e:
        logger.warn("持久化 timed 编号失败: %s" % e)


class CameraService(object):
    """摄像头服务。

    用法：
        cam = CameraService()
        cam.init()
        cam.take_photo()
    """

    STATUS_IDLE = 0
    STATUS_BUSY = 1
    STATUS_OK = 2
    STATUS_ERROR = -1

    def __init__(self):
        self._cap = None
        self._status = self.STATUS_IDLE
        self._last_photo = None     # 最近一次拍照的文件名（不含路径）
        self._photo_count = 0
        self._callback = None        # 拍照完成回调

    def set_callback(self, cb):
        """注册拍照完成回调：cb(photo_name, status)"""
        self._callback = cb

    # ---- 初始化 ----
    def init(self):
        """创建 camCapture 实例 + open()。camCapture.open() 后自动在 LCD 推取景预览画面。

        必须在 LCD 初始化之后调用。
        返回 True=成功, False=失败。
        """
        try:
            self._ensure_photo_dir()
            self._cap = camCapture(
                model=CAM_MODEL,
                cam_w=CAM_SENSOR_WIDTH,
                cam_h=CAM_SENSOR_HEIGHT,
                perview_level=CAM_PREVIEW_LEVEL,
                lcd_w=240,
                lcd_h=240,
            )
            self._cap.callback(self._on_capture)
            res = self._cap.open()
            if res != 0:
                logger.error("camCapture.open 失败: {}".format(res))
                return False
            self._photo_count = self._count_photos()
            logger.info("摄像头初始化完成，当前照片: {} 张".format(self._photo_count))
            return True
        except Exception as e:
            logger.error("摄像头初始化异常: {}".format(e))
            self._cap = None
            return False

    def _ensure_photo_dir(self):
        """确保照片目录存在（/usr/ 是内部 Flash，通常已存在）。"""
        try:
            # 检测目录是否存在，忽略元组格式问题
            uos.listdir(CAM_PHOTO_DIR)
        except OSError:
            try:
                uos.mkdir(CAM_PHOTO_DIR)
                logger.info("创建照片目录: {}".format(CAM_PHOTO_DIR))
            except Exception as e:
                logger.warn("创建目录失败: {}".format(e))

    @staticmethod
    def _to_name(entry):
        """把 uos.listdir 返回的元素统一转为字符串文件名。

        QuecPython 不同版本可能返回：str / bytes / int / (name, type, size) tuple
        返回空字符串表示无法识别。
        """
        try:
            if isinstance(entry, (tuple, list)):
                entry = entry[0] if len(entry) > 0 else b""
            if isinstance(entry, bytes):
                return entry.decode("utf-8", "ignore")
            if isinstance(entry, str):
                return entry
            # int 等其他类型直接忽略
            return ""
        except Exception:
            return ""

    def _count_photos(self):
        """统计照片目录下 photo_*.jpg 文件数量。"""
        count = 0
        try:
            for entry in uos.listdir(CAM_PHOTO_DIR):
                fname = self._to_name(entry)
                if fname and _is_photo_name(fname) and fname.lower().startswith("photo_"):
                    count += 1
        except OSError:
            pass
        except Exception:
            pass
        return count

    @staticmethod
    def _photo_number(fname, prefix="photo_"):
        """从文件名提取编号。

        新格式 photo_001.jpg → 1；timed_photo_001.jpg → 1（前缀参数化）；
        旧格式 photo_YYYYMMDD_HHMMSS.jpg（非纯数字）→ 返回 0（视为最旧，排最前）。

        Args:
            fname: 文件名
            prefix: 文件名前缀（默认 "photo_"），按该前缀长度截取编号部分

        Returns:
            int 编号；无法解析返回 0
        """
        low = fname.lower()
        base = fname[:-4] if (low.endswith(".jpg") or low.endswith(".jpeg")) else fname
        if base.lower().startswith(prefix):
            num_str = base[len(prefix):]
            if num_str.isdigit():
                return int(num_str)
        return 0

    def _next_photo_name(self, prefix="photo_"):
        """生成下一个照片编号名：扫描现有指定前缀的 photo_NNN.jpg 的最大编号 + 1。

        Args:
            prefix: 文件名前缀（默认 "photo_"；定时拍摄传 "timed_photo_"）

        Returns:
            "<prefix><编号>" 格式（如 "photo_001" / "timed_photo_001"，超过 999 自动变 4 位）
        """
        max_num = 0
        # 定时帧：先读持久化的云端最大编号（本地帧删了但云端还在，重拍不会重名）
        persisted = _read_persisted_max(prefix)
        if persisted > max_num:
            max_num = persisted
        # 再扫本地（定时帧还没上传/上传中可能还在本地）
        for base in ([CAM_PHOTO_DIR, "/", "/usr", "U:/"]):
            try:
                for entry in uos.listdir(base):
                    fname = self._to_name(entry)
                    if fname and _is_photo_name(fname) and fname.lower().startswith(prefix):
                        n = self._photo_number(fname, prefix)
                        if n > max_num:
                            max_num = n
            except Exception:
                continue
        return "{}{:03d}".format(prefix, max_num + 1)

    # ---- 拍照（单张）----
    def take_photo(self, prefix="photo_", save_w=None, save_h=None):
        """触发拍照。

        Args:
            prefix: 文件名前缀（默认 "photo_"；定时拍摄用 "timed_photo_"）
            save_w, save_h: 保存尺寸（默认 None 用 config 的 CAM_CAPTURE_WIDTH/HEIGHT）
                            定时拍摄传 TIMED_CAM_W/H（640×480 sensor 原生）
        """
        if self._status == self.STATUS_BUSY:
            logger.warn("正在拍照中，忽略重复请求")
            return False
        if not self._cap:
            logger.warn("摄像头未初始化")
            return False

        real_w = save_w if save_w is not None else CAM_CAPTURE_WIDTH
        real_h = save_h if save_h is not None else CAM_CAPTURE_HEIGHT

        self._status = self.STATUS_BUSY
        photo_name = self._next_photo_name(prefix)
        try:
            self._cap.start(real_w, real_h, photo_name)
            logger.info("拍照触发: %s (%dx%d)" % (photo_name, real_w, real_h))
            return True
        except Exception as e:
            logger.error("拍照触发异常: %s" % e)
            self._status = self.STATUS_IDLE
            return False

    def take_timed_photo(self):
        """定时拍摄：sensor 原生 640×480 出图，定时专属后台上传线程处理。"""
        return self.take_photo("timed_photo_", TIMED_CAM_W, TIMED_CAM_H)

    # ---- 拍照回调 ----
    def _on_capture(self, result):
        """camCapture 拍照完成回调（单张）。

        result[0] 状态码（0=成功）
        result[1] 完整路径，如 U:/photo_001.jpg
        """
        logger.info("拍照回调: %s" % (result,))
        code = result[0] if isinstance(result, (list, tuple)) else -1
        if code == 0:
            src_path = result[1] if len(result) > 1 else "unknown"
            filename = src_path.split("/")[-1] if "/" in src_path else src_path
            cam_src = self._locate_photo(src_path, filename)
            if cam_src is None:
                logger.error("找不到拍照文件: %s / %s" % (src_path, filename))
                self._status = self.STATUS_ERROR
                if self._callback:
                    self._callback(None, self.STATUS_ERROR)
                return

            self._last_photo = filename
            # photo_count 只统计普通照片（特定时照片不污染云端 PhotoCount 上报）
            if not filename.lower().startswith("timed_"):
                self._photo_count += 1
            self._status = self.STATUS_OK
            logger.info("拍照成功: 文件名=%s 位置=%s" % (filename, cam_src))
            self._cleanup_old_photos()
            if self._callback:
                self._callback(filename, self.STATUS_OK)
        else:
            self._status = self.STATUS_ERROR
            logger.error("拍照失败: code=%s" % code)
            if self._callback:
                self._callback(None, self.STATUS_ERROR)

    @staticmethod
    def _locate_photo(src_path, filename):
        """在候选位置中查找拍照文件真实路径（POSIX 路径优先，U:/ 盘符可能 ENODEV）。"""
        candidates = [
            CAM_PHOTO_DIR + "/" + filename,   # /usr/photo_001.jpg（U:/ = /usr/）
            "/" + filename,
        ]
        if src_path and src_path != "unknown":
            candidates.append(src_path)
        candidates.append("U:/" + filename)
        for cand in candidates:
            try:
                uos.stat(cand)
                return cand
            except Exception:
                continue
        return None

    @staticmethod
    def _copy_file(src, dst):
        """复制文件（逐块读取写入，适配小内存设备）。"""
        buf_size = 512
        with open(src, "rb") as f_src:
            with open(dst, "wb") as f_dst:
                while True:
                    buf = f_src.read(buf_size)
                    if not buf:
                        break
                    f_dst.write(buf)

    # ---- 相册预览辅助 ----

    def get_photo_path(self, filename):
        """给定照片文件名，返回可供 lv.img.set_src 使用的路径。

        Args:
            filename: 如 "photo_001.jpg"

        Returns:
            "U:/photo_001.jpg" 或 None（找不到）
            返回 U:/ 格式，因为 QuecPython LVGL 的文件访问层可能不认纯 POSIX。
        """
        posix = self._locate_photo("", filename)
        if posix:
            return "U:/" + filename
        return None

    # ---- 关闭摄像头 ----

    def close(self):
        """关闭摄像头，停止预览，释放 LCD 供 UI 使用。"""
        if self._cap is not None:
            try:
                self._cap.close()
                logger.info("摄像头已关闭（预览停止）")
            except Exception as e:
                logger.warn("关闭摄像头异常: {}".format(e))
        self._cap = None
        self._status = self.STATUS_IDLE

    # ---- 照片列表 ----

    def list_photos(self):
        """列出本地照片。

        照片直接放在 U:/（=/usr/）根目录下，扫描 /usr 根目录的 photo_*.jpg。

        Returns:
            [photo_name, ...] 文件名列表（去重，按编号排序，最新的在前）
        """
        files = []
        bases = [CAM_PHOTO_DIR]
        for base in bases:
            try:
                for entry in uos.listdir(base):
                    try:
                        fname = self._to_name(entry)
                        if not fname:
                            continue
                        if _is_photo_name(fname) and fname.lower().startswith("photo_"):
                            files.append(fname)
                    except Exception:
                        pass
            except OSError:
                continue
            except Exception as e:
                logger.warn("列出照片异常 %s: %s" % (base, e))
        # 去重（多目录可能重复，保持原名）
        seen = set()
        unique = []
        for fname in files:
            if fname not in seen:
                unique.append(fname)
                seen.add(fname)
        # 按编号数字排序（逆序=最新在前；旧格式时间戳照片编号=0 排最前）
        unique.sort(key=self._photo_number, reverse=True)
        return unique

    # ---- 删除照片 ----

    def delete_photo(self, photo_name):
        """删除本地指定照片文件。

        会在照片目录和根目录候选位置中查找并删除（兼容旧固件文件未移入照片目录）。

        Args:
            photo_name: 文件名（如 "photo_xxx.jpg"）

        Returns:
            True=删除成功, False=失败
        """
        candidates = ["{}/{}".format(CAM_PHOTO_DIR, photo_name)]
        candidates.extend([
            "/usr/" + photo_name,
            "/" + photo_name,
            "U:/" + photo_name,
        ])
        for path in candidates:
            try:
                uos.remove(path)
                logger.info("删除本地照片: {}".format(path))
                self._photo_count = self._count_photos()
                return True
            except Exception:
                continue
        logger.warn("删除本地照片失败(找不到文件): {}".format(photo_name))
        return False

    def delete_all_photos(self):
        """删除本地所有照片。

        Returns:
            (deleted_count, failed_count)
        """
        photos = self.list_photos()
        deleted = 0
        failed = 0
        for fname in photos:
            if self.delete_photo(fname):
                deleted += 1
            else:
                failed += 1
        logger.info("本地删除完成: 成功={} 失败={}".format(deleted, failed))
        return (deleted, failed)

    # ---- 状态查询 ----
    @property
    def status(self):
        return self._status

    @property
    def is_ready(self):
        """摄像头是否已初始化并可用。"""
        return self._cap is not None

    @property
    def last_photo(self):
        return self._last_photo

    @property
    def photo_count(self):
        return self._photo_count

    def get_status_dict(self):
        """返回状态字典（供云端上报用）。"""
        return {
            "photo_count": self._photo_count,
            "last_photo": self._last_photo or "",
        }

    def _cleanup_old_photos(self):
        """删除最旧的照片（photo_ 与 timed_photo_ 前缀），直到不超过 CAM_MAX_PHOTOS。

        定时照片（timed_*）虽不进相册/不计数，但本地也纳入循环清理，避免延时摄影撑爆 Flash。
        只处理 photo_ / timed_photo_ 前缀文件，避免误删 /usr 下的其他文件。
        """
        try:
            all_entries = uos.listdir(CAM_PHOTO_DIR)
            files = []   # [(name, 编号), ...]
            for entry in all_entries:
                try:
                    fname = self._to_name(entry)
                    if fname and _is_photo_name(fname):
                        low = fname.lower()
                        if low.startswith("photo_") or low.startswith("timed_photo_"):
                            prefix = "timed_photo_" if low.startswith("timed_photo_") else "photo_"
                            files.append((fname, self._photo_number(fname, prefix)))
                except Exception:
                    pass
            if len(files) <= CAM_MAX_PHOTOS:
                return
            files.sort(key=lambda x: x[1])   # 按编号升序=最旧在前
            to_delete = len(files) - CAM_MAX_PHOTOS
            for f, _ in files[:to_delete]:
                try:
                    uos.remove("{}/{}".format(CAM_PHOTO_DIR, f))
                    logger.info("删除旧照片: {}".format(f))
                except Exception as e:
                    logger.warn("删除 {} 失败: {}".format(f, e))
        except Exception as e:
            logger.warn("清理旧照片异常: {}".format(e))