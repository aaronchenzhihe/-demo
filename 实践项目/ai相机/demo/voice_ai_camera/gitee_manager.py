# -*- coding: UTF-8 -*-
"""Gitee 仓库管理：照片列表查询 & 删除。

使用 Gitee v5 Contents API。

API 端点：
  - GET    /repos/{owner}/{repo}/contents/{path}   → 列出目录内容
  - DELETE /repos/{owner}/{repo}/contents/{path}   → 删除文件

删除流程：
  1. 先 GET 列出目录内容，获取每个文件的 SHA
  2. 对每个文件 DELETE（需提供 SHA）

注意事项：
  - QuecPython 的 request 库不支持 DELETE 方法，所以使用 POST 并添加
    _method=DELETE 参数（Gitee API 支持此方式）
  - 响应体 resp.text 是生成器，需要用 for chunk in resp.text 读取
"""
import sys
import request
import ujson
from usr.logging import getLogger
from usr.config import (
    GITEE_OWNER,
    GITEE_REPO,
    GITEE_BRANCH,
    GITEE_PHOTO_PATH,
    GITEE_ACCESS_TOKEN,
    GITEE_API_CREATE,
    GITEE_RAW_BASE,
)

logger = getLogger("gitee")

# Gitee API 基础 URL
_GITEE_API_BASE = "https://gitee.com/api/v5/repos/{owner}/{repo}/contents/{path}"
_GITEE_DELETE_URL = "https://gitee.com/api/v5/repos/{owner}/{repo}/contents/{path}"


def _to_bytes(data):
    """把 resp.content 的各种可能类型统一转成 bytes。

    QuecPython 不同固件版本 resp.content 可能返回：bytes / str / bytearray / memoryview。
    str 必须按 latin-1 还原原始字节（避免 UTF-8 解码损坏 JPEG）。

    Returns:
        bytes 成功；None 无法转换
    """
    try:
        if isinstance(data, str):
            return data.encode("latin-1", "ignore")
        if isinstance(data, (bytearray, memoryview)):
            return bytes(data)
        if isinstance(data, bytes):
            return data
    except Exception as e:
        logger.warn("转换 content 为 bytes 异常: %s" % e)
    return None


def _chunk_to_bytes(chunk):
    """把生成器吐出的单个块转成 bytes（chunk 通常为 str 或 bytes）。"""
    try:
        if isinstance(chunk, str):
            return chunk.encode("latin-1", "ignore")
        if isinstance(chunk, (bytearray, memoryview)):
            return bytes(chunk)
        if isinstance(chunk, bytes):
            return chunk
    except Exception:
        pass
    return None


def _write_bytes(dest, buf):
    """把 bytes 写入文件，成功返回 True。"""
    try:
        with open(dest, "wb") as f:
            f.write(buf)
        return True
    except Exception as e:
        logger.error("写入文件失败 %s: %s" % (dest, e))
        return False


class GiteeManager(object):
    """Gitee 仓库管理：照片列表查询 & 删除。"""

    def __init__(self):
        self._token = GITEE_ACCESS_TOKEN
        self._enabled = bool(self._token) and "your-" not in self._token

    @property
    def enabled(self):
        return self._enabled

    # ---- 查询 ----

    def list_photos(self, sub_path=None):
        """列出 Gitee 仓库 photos 目录下的所有照片。

        Args:
            sub_path: 可选，子目录（如 "photos/timed/"）。None 时用默认 photos/。

        Returns:
            [(name, sha, download_url), ...] 成功时
            [] 失败或无照片时
        """
        if not self._enabled:
            logger.warn("Gitee 未配置，跳过列表查询")
            return []

        path = (sub_path or GITEE_PHOTO_PATH).rstrip("/")  # 去掉末尾斜杠
        api_url = _GITEE_API_BASE.format(
            owner=GITEE_OWNER,
            repo=GITEE_REPO,
            path=path,
        )
        query_url = "{}?access_token={}&ref={}".format(api_url, self._token, GITEE_BRANCH)

        logger.info("查询 Gitee 照片列表: {}".format(path))

        try:
            # 显式带 UA 和 Accept 头，避免部分服务端/库默认头处理异常
            resp = request.get(
                query_url,
                headers={"Accept": "application/json", "User-Agent": "quecpython"},
                timeout=15,
            )
            status = getattr(resp, "status_code", 0)

            if status != 200:
                logger.error("查询照片列表失败: status={}".format(status))
                self._read_response(resp)
                try:
                    resp.close()
                except Exception:
                    pass
                return []

            # 读取响应体
            body = self._read_response(resp)
            try:
                resp.close()
            except Exception:
                pass

            if not body:
                logger.error("查询照片列表: 响应体为空")
                return []

            # 解析 JSON
            try:
                items = ujson.loads(body)
            except Exception as e:
                logger.error("JSON 解析失败: {} body={}".format(e, body[:200]))
                return []

            if not isinstance(items, list):
                logger.warn("查询结果不是列表: {}".format(type(items)))
                return []

            # 筛选照片文件（QuecPython 的 endswith 不支持 tuple 参数，需逐个判断）
            photos = []
            for item in items:
                name = item.get("name", "")
                low = name.lower()
                if low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".png"):
                    photos.append((
                        name,
                        item.get("sha", ""),
                        item.get("download_url", ""),
                    ))

            logger.info("Gitee 照片列表: {} 张 (共 {} 项)".format(len(photos), len(items)))
            return photos

        except Exception as e:
            logger.error("查询照片列表异常: {}".format(e))
            sys.print_exception(e)
            return []

    # ---- 删除 ----

    def download_photo(self, photo_name, download_url, dest):
        """下载 Gitee 照片到本地文件。

        Args:
            photo_name: 文件名（日志用）
            download_url: 照片下载直链（list_photos 返回的 download_url）
            dest: 本地目标路径（如 /usr/_preview.jpg）

        Returns:
            dest 成功，None 失败
        """
        url = download_url
        if not url:
            logger.warn("下载照片 %s: 无 download_url" % photo_name)
            return None

        # 优先用 raw 直链（raw.giteeusercontent.com，不会 302）。
        # Gitee API 的 download_url 会重定向到该地址，而 QuecPython request.get
        # 默认不跟随重定向，302 会被误判为失败。
        raw_url = GITEE_RAW_BASE.format(
            owner=GITEE_OWNER, repo=GITEE_REPO, branch=GITEE_BRANCH,
        ) + GITEE_PHOTO_PATH + photo_name

        try:
            # 关键：decode=False 时 resp.content 返回二进制。
            # 注意：此固件 resp.content 是生成器（与 resp.text 同型），必须流式遍历，
            # 不能直接 len() 或一次性 bytes()。
            resp = request.get(raw_url, timeout=20, decode=False)
            status = getattr(resp, "status_code", 0)
            if status in (200, 302):
                # 流式写入（在 finally 前收集字节）
                ok, size = self._drain_content(resp, dest)
                try:
                    resp.close()
                except Exception:
                    pass
                if ok:
                    logger.info("下载照片 %s (raw直链) -> %s (%d bytes)" % (
                        photo_name, dest, size))
                    return dest

            # 直链失败则退回 download_url 并追加 token
            try:
                resp.close()
            except Exception:
                pass
            if self._token:
                url = url + ("&" if "?" in url else "?") + "access_token=" + self._token
            resp = request.get(url, timeout=20, decode=False)
            status = getattr(resp, "status_code", 0)
            if status != 200:
                logger.error("下载照片 %s: status=%s" % (photo_name, status))
                try:
                    resp.close()
                except Exception:
                    pass
                return None
            ok, size = self._drain_content(resp, dest)
            try:
                resp.close()
            except Exception:
                pass
            if ok:
                logger.info("下载照片 %s -> %s (%d bytes)" % (photo_name, dest, size))
                return dest
            return None
        except Exception as e:
            logger.error("下载照片 %s 异常: %s" % (photo_name, e))
            sys.print_exception(e)
            return None

    def _drain_content(self, resp, dest):
        """从 resp 读取响应体并流式写入文件，返回 (ok, size)。

        兼容 resp.content / resp.text 的多种形态：
          - 生成器（本固件实际形态）：逐块写入，chunk 可能为 str 或 bytes
          - bytes / bytearray / memoryview / str：一次性写入
        """
        try:
            data = resp.content
        except Exception:
            # 部分固件 content 不可读，回退 resp.text
            try:
                data = resp.text
            except Exception as e:
                logger.warn("读取响应体失败: %s" % e)
                return False, 0

        size = 0
        is_iter = hasattr(data, "__next__")
        with open(dest, "wb") as f:
            if is_iter:
                for chunk in data:
                    if chunk:
                        b = _chunk_to_bytes(chunk)
                        if b is not None:
                            f.write(b)
                            size += len(b)
            else:
                b = _to_bytes(data)
                if b is None:
                    logger.error("下载照片: content 无法转成 bytes (type=%s)" % type(data).__name__)
                    return False, 0
                f.write(b)
                size = len(b)
        if size == 0:
            logger.error("下载照片: 响应体为空 (type=%s)" % type(data).__name__)
            return False, 0
        return True, size

    def delete_photo(self, photo_name, sha, sub_path=None):
        """删除 Gitee 仓库中的一张照片。

        Gitee API 原为 DELETE 方法，但 QuecPython request 不支持 DELETE，
        使用 POST 并在 body 中添加 _method=DELETE 参数。

        Args:
            photo_name: 文件名（如 "photo_xxx.jpg"）
            sha: 文件的 SHA（通过 list_photos 获取）
            sub_path: 可选，子目录（如 "photos/timed/"）。None 时用默认 photos/。

        Returns:
            True=删除成功, False=失败
        """
        if not self._enabled:
            logger.warn("Gitee 未配置，跳过删除")
            return False

        prefix = sub_path if sub_path else GITEE_PHOTO_PATH
        object_path = "{}{}".format(prefix, photo_name)
        api_url = _GITEE_DELETE_URL.format(
            owner=GITEE_OWNER,
            repo=GITEE_REPO,
            path=object_path,
        )

        # 构造 form-encoded 请求体
        # Gitee API 支持 POST + _method=DELETE 方式
        payload = "access_token={}&_method=DELETE&message={}&sha={}&branch={}".format(
            self._token,
            "delete photo: {}".format(photo_name).replace(" ", "%20"),
            sha,
            GITEE_BRANCH,
        )

        logger.info("删除 Gitee 照片: {} SHA={}".format(photo_name, sha[:8]))

        try:
            resp = request.post(
                api_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=30,
            )
            status = getattr(resp, "status_code", 0)

            body = self._read_response(resp)
            try:
                resp.close()
            except Exception:
                pass

            if status in (200, 201, 204):
                logger.info("删除成功: {} (status={})".format(photo_name, status))
                return True
            else:
                logger.error("删除失败: {} status={} body={}".format(
                    photo_name, status, body[:200] if body else ""))
                return False

        except Exception as e:
            logger.error("删除照片异常: {} {}".format(photo_name, e))
            return False

    def delete_all_photos(self, sub_path=None):
        """删除 Gitee 仓库某个目录下所有照片。

        Args:
            sub_path: 可选，子目录（如 "photos/timed/"）。None 时用默认 photos/。

        Returns:
            (deleted_count, failed_count)
        """
        photos = self.list_photos(sub_path=sub_path)
        if not photos:
            logger.info("没有照片需要删除")
            return (0, 0)

        deleted = 0
        failed = 0
        for name, sha, _ in photos:
            ok = self.delete_photo(name, sha, sub_path=sub_path)
            if ok:
                deleted += 1
            else:
                failed += 1

        logger.info("Gitee 删除完成: 成功={} 失败={}".format(deleted, failed))
        return (deleted, failed)

    # ---- 工具 ----

    @staticmethod
    def _read_response(resp):
        """读取响应体（QuecPython resp.text 是生成器，需遍历）。"""
        try:
            chunks = []
            for chunk in resp.text:
                if chunk:
                    chunks.append(chunk)
            return "".join(chunks)
        except Exception as e:
            logger.warn("读取响应体失败: {}".format(e))
            return ""