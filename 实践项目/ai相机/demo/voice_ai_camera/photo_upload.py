# -*- coding: UTF-8 -*-
"""照片上传：拍照完成后上传到 Gitee 仓库。

使用 Gitee v5 Contents API 上传照片文件（Base64 编码后 PUT 到仓库）。
上传后返回 Gitee 直链，供云端相册和视觉分析使用。

API:
  from usr.photo_upload import PhotoUploader
  uploader = PhotoUploader()
  ok = uploader.upload(photo_path)
"""
import request
import ubinascii
import uos
import ujson
import gc
from usr.logging import getLogger
from usr.config import (
    PHOTO_UPLOAD_GITEE_ENABLED,
    GITEE_OWNER,
    GITEE_REPO,
    GITEE_BRANCH,
    GITEE_PHOTO_PATH,
    GITEE_TIMED_PHOTO_PATH,
    GITEE_ACCESS_TOKEN,
    GITEE_RAW_BASE,
    GITEE_API_CREATE,
    PHOTO_UPLOAD_TIMEOUT,
)

logger = getLogger("upload")


class PhotoUploader(object):
    """照片上传器（Gitee）。"""

    def __init__(self):
        self._enabled = PHOTO_UPLOAD_GITEE_ENABLED and "your-" not in GITEE_ACCESS_TOKEN

    @property
    def enabled(self):
        return self._enabled

    @property
    def upload_method(self):
        return "gitee" if self._enabled else "none"

    def upload(self, photo_name):
        """上传照片到 Gitee 仓库（单张）。

        Args:
            photo_name: 照片文件名，如 "photo_001.jpg"

        Returns:
            (success, download_url) 元组；失败时 download_url = None
        """
        if not self._enabled:
            logger.info("Gitee 上传未启用（请配置 GITEE_ACCESS_TOKEN）")
            return False, None

        return self._upload_file(photo_name)

    def _upload_file(self, file_name):
        """上传单个文件到 Gitee。

        Args:
            file_name: 文件名（如 "photo_001.jpg"）

        Returns:
            (success, raw_url)
        """
        try:
            # 尝试多种路径读取照片文件（照片直接存 /usr/ 根目录）
            content = None
            for try_path in [
                "/usr/{}".format(file_name),
                "U:/{}".format(file_name),
            ]:
                try:
                    with open(try_path, "rb") as f:
                        content = f.read()
                    if content:
                        logger.info("从 {} 读取照片成功".format(try_path))
                        break
                except OSError as e:
                    logger.debug("从 {} 读取失败: {}".format(try_path, e))
                except Exception:
                    pass

            if not content:
                logger.info("{} 照片本地不存在，跳过上传".format(file_name))
                return False, None

            size = len(content)
            # 定时照片（timed_*）与普通照片在云端分开目录存储
            if file_name.lower().startswith("timed_"):
                sub_path = GITEE_TIMED_PHOTO_PATH   # photos/timed/
            else:
                sub_path = GITEE_PHOTO_PATH         # photos/
            object_path = sub_path + file_name
            logger.info("Gitee 上传照片: {} → {} ({} bytes)".format(
                file_name, object_path, size))

            b64_data = ubinascii.b2a_base64(content)
            b64_str = b64_data.decode("ascii").replace("\n", "").replace("\r", "")
            b64_encoded = b64_str.replace("+", "%2B").replace("/", "%2F").replace("=", "%3D")

            api_url = GITEE_API_CREATE.format(
                owner=GITEE_OWNER,
                repo=GITEE_REPO,
                path=object_path,
            )

            payload = "access_token={}&content={}&message={}&branch={}".format(
                GITEE_ACCESS_TOKEN,
                b64_encoded,
                "upload photo: {}".format(file_name).replace(" ", "%20"),
                GITEE_BRANCH,
            )

            logger.info("发送 POST 请求: {} 文件大小: {} bytes".format(object_path, size))

            status, body = self._post_form(api_url, payload)
            logger.info("Gitee 响应: 状态码 {}".format(status))

            if status == 400:
                logger.warn("创建返回 400（多为文件已存在），尝试覆盖更新")
                sha = self._get_sha(object_path)
                if sha:
                    payload = (
                        "access_token={}&content={}&message={}&branch={}"
                        "&sha={}&_method=PUT"
                    ).format(
                        GITEE_ACCESS_TOKEN,
                        b64_encoded,
                        "update photo: {}".format(file_name).replace(" ", "%20"),
                        GITEE_BRANCH,
                        sha,
                    )
                    status, body = self._post_form(api_url, payload)
                    logger.info("Gitee 覆盖更新响应: 状态码 {}".format(status))

            if status in (201, 200):
                raw_url = GITEE_RAW_BASE.format(
                    owner=GITEE_OWNER,
                    repo=GITEE_REPO,
                    branch=GITEE_BRANCH,
                ) + object_path
                logger.info("[OK] Gitee 上传照片成功: {}".format(raw_url))
                return True, raw_url
            else:
                logger.error("Gitee 上传照片失败: 状态码 {} body={}".format(
                    status, (body or "")[:200]))
                return False, None

        except Exception as e:
            logger.error("Gitee 上传照片异常: {}".format(e))
            return False, None
        finally:
            # 上传链路产生大量临时对象（content → b64_data → replace×3 → payload）
            # 手动 del 大对象让 gc 能及时回收，避免累积 OOM
            try:
                del content, b64_data, b64_str, b64_encoded, payload
            except Exception:
                pass
            try:
                gc.collect()
            except Exception:
                pass

    # ---- 工具 ----

    def _post_form(self, api_url, payload):
        """发送 form-encoded POST，返回 (status_code, body)。

        内部消化异常：网络异常返回 (0, "")，由调用方按失败处理。
        """
        resp = None
        try:
            resp = request.post(
                api_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=PHOTO_UPLOAD_TIMEOUT,
            )
            status = getattr(resp, "status_code", 0)
            body = self._read_body(resp)
            return status, body
        except Exception as e:
            logger.error("Gitee POST 异常: {}".format(e))
            return 0, ""
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

    def _get_sha(self, object_path):
        """查询 Gitee 上已有文件的 sha（覆盖更新时必须携带）。

        Returns:
            sha 字符串（文件存在）；None（不存在或查询失败）
        """
        try:
            api_url = GITEE_API_CREATE.format(
                owner=GITEE_OWNER,
                repo=GITEE_REPO,
                path=object_path,
            )
            url = "{}?access_token={}&ref={}".format(
                api_url, GITEE_ACCESS_TOKEN, GITEE_BRANCH)
            resp = request.get(
                url,
                headers={"Accept": "application/json", "User-Agent": "quecpython"},
                timeout=15,
            )
            status = getattr(resp, "status_code", 0)
            body = self._read_body(resp)
            try:
                resp.close()
            except Exception:
                pass
            if status == 200 and body:
                try:
                    info = ujson.loads(body)
                    sha = info.get("sha", "")
                    if sha:
                        logger.info("Gitee 已存在 {}，改为覆盖更新 (sha={})".format(
                            object_path, sha[:8]))
                        return sha
                except Exception as e:
                    logger.warn("解析文件信息失败: {}".format(e))
            return None
        except Exception as e:
            logger.warn("查询文件 sha 异常: {}".format(e))
            return None

    @staticmethod
    def _read_body(resp):
        """读取响应体文本（QuecPython resp.text 是生成器，需遍历）。"""
        try:
            chunks = []
            for chunk in resp.text:
                if chunk:
                    chunks.append(chunk)
            return "".join(chunks)
        except Exception as e:
            logger.warn("读取响应体失败: {}".format(e))
            return ""