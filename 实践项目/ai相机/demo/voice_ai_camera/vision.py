# -*- coding: UTF-8 -*-
"""AI 视觉分析：百度智能云 图像内容理解（image-understanding）。

流程（两步式异步接口）：
  1. OAuth2 获取 access_token（有效期 30 天，进程内缓存复用）
  2. 提交理解任务：base64 图片 + 提问 → 返回 task_id
  3. 轮询获取结果：task_id → 返回图片描述文本（description / description_to_llm）

API:
  from usr.vision import BaiduVisionAnalyzer
  analyzer = BaiduVisionAnalyzer()
  desc = analyzer.analyze("photo_001.jpg", "这张图片里有什么？")
"""
import utime
import request
import ubinascii
import ujson
from usr.logging import getLogger
from usr.config import (
    AI_VISION_BAIDU_ENABLED,
    BAIDU_AI_API_KEY,
    BAIDU_AI_SECRET_KEY,
    BAIDU_AI_TOKEN_URL,
    BAIDU_AI_REQUEST_URL,
    BAIDU_AI_RESULT_URL,
    BAIDU_AI_TIMEOUT,
    BAIDU_AI_POLL_INTERVAL,
)

logger = getLogger("vision")


class BaiduVisionAnalyzer(object):
    """百度图像理解客户端。"""

    def __init__(self):
        self._enabled = (
            AI_VISION_BAIDU_ENABLED
            and "your-" not in BAIDU_AI_API_KEY
            and "your-" not in BAIDU_AI_SECRET_KEY
        )
        self._access_token = None

    @property
    def enabled(self):
        return self._enabled

    # ==================================================================
    # 1. Access Token（OAuth2，有效期 30 天，进程内缓存）
    # ==================================================================
    def get_access_token(self):
        if self._access_token:
            return self._access_token
        if not self._enabled:
            logger.info("百度图像理解未启用（请配置 BAIDU_AI_API_KEY / BAIDU_AI_SECRET_KEY）")
            return None
        url = "{}?grant_type=client_credentials&client_id={}&client_secret={}".format(
            BAIDU_AI_TOKEN_URL, BAIDU_AI_API_KEY, BAIDU_AI_SECRET_KEY)
        try:
            resp = request.get(url, timeout=15)
            data = resp.json()
            try:
                resp.close()
            except Exception:
                pass
            token = data.get("access_token")
            if token:
                self._access_token = token
                logger.info("百度 access_token 获取成功")
            else:
                logger.error("百度 token 获取失败: {}".format(
                    data.get("error_description", data)))
            return token
        except Exception as e:
            logger.error("百度 token 获取异常: {}".format(repr(e)))
            return None

    # ==================================================================
    # 2. 提交理解任务
    # ==================================================================
    def _submit(self, image_b64, question):
        """提交图片理解任务（本地 base64），返回 task_id；失败返回 None。"""
        payload = ujson.dumps({
            "image": image_b64,          # base64 后 urlencode
            "question": question,
            "output_CHN": True,          # 中文输出描述
        })
        return self._post_and_task(payload)

    def _submit_url(self, image_url, question):
        """提交图片理解任务（云端 URL，百度服务端拉取），返回 task_id；失败返回 None。"""
        payload = ujson.dumps({
            "url": image_url,            # 图片完整 url（公开可访问）
            "question": question,
            "output_CHN": True,
        })
        return self._post_and_task(payload)

    def _post_and_task(self, payload):
        """POST 提交任务并提取 task_id（_submit/_submit_url 共用）。"""
        token = self.get_access_token()
        if not token:
            return None
        url = "{}?access_token={}".format(BAIDU_AI_REQUEST_URL, token)
        try:
            resp = request.post(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                timeout=30)
            data = resp.json()
            try:
                resp.close()
            except Exception:
                pass
            result = data.get("result") or {}
            task_id = result.get("task_id")
            if task_id:
                logger.info("图像理解任务已提交: {}".format(task_id))
                return task_id
            logger.error("百度提交失败: {}".format(data))
            return None
        except Exception as e:
            logger.error("百度提交异常: {}".format(repr(e)))
            return None

    # ==================================================================
    # 3. 轮询获取结果
    # ==================================================================
    def _get_result(self, task_id):
        """轮询任务结果，返回描述文本；失败返回 None。"""
        token = self._access_token
        url = "{}?access_token={}".format(BAIDU_AI_RESULT_URL, token)
        payload = ujson.dumps({"task_id": task_id})
        deadline = utime.time() + BAIDU_AI_TIMEOUT
        while utime.time() < deadline:
            try:
                resp = request.post(
                    url, data=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=15)
                data = resp.json()
                try:
                    resp.close()
                except Exception:
                    pass
                result = data.get("result") or {}
                # 结果就绪：description 非空即为最终描述
                desc = result.get("description") or result.get("description_to_llm")
                if desc:
                    return desc
                # ret_code 语义（百度文档）：0=成功完成，1=处理中，其它=失败
                ret_code = result.get("ret_code", data.get("ret_code", -1))
                if ret_code == 0:
                    # 完成但无 description（异常），不再轮询
                    logger.error("百度任务完成但无 description: {}".format(data))
                    return None
                if ret_code == 1:
                    logger.debug("任务处理中，继续轮询...")
                else:
                    logger.error("百度任务失败: ret_code={} {}".format(
                        ret_code, data))
                    return None
            except Exception as e:
                logger.error("百度查询异常: {}".format(repr(e)))
            utime.sleep(BAIDU_AI_POLL_INTERVAL)
        logger.error("百度任务超时（{}s）".format(BAIDU_AI_TIMEOUT))
        return None

    # ==================================================================
    # 对外主入口
    # ==================================================================
    def analyze(self, photo_name, question="这张图片里有什么？", url=None):
        """分析照片，返回描述文本；失败返回 None。

        Args:
            photo_name: 照片文件名，如 "photo_001.jpg"
            question:   对照片的提问（≤100 字符）
            url:        照片的公开 URL。若提供，则直接用百度拉取该 URL（设备无需下载/落盘），
                        photo_name 仅用作日志标识。
        """
        if not self._enabled:
            return None

        # 0) 云端 URL 模式：直接交给百度抓取，设备零下载
        if url:
            task_id = self._submit_url(url, question)
            if not task_id:
                return None
            desc = self._get_result(task_id)
            if desc:
                logger.info("图像理解结果(云端URL): {}".format(desc))
            return desc

        # 1. 读取照片（候选路径兼容 U:/ 与 POSIX）
        content = None
        read_path = None
        for try_path in [
            "/usr/{}".format(photo_name),
            "U:/{}".format(photo_name),
        ]:
            try:
                with open(try_path, "rb") as f:
                    content = f.read()
                if content:
                    read_path = try_path
                    break
            except Exception:
                pass
        if not content:
            logger.error("无法读取照片: {}".format(photo_name))
            return None
        logger.info("从 {} 读取照片成功 ({} bytes)".format(read_path, len(content)))

        # 文件头校验（JPEG 应为 FFD8，便于排查 image format error）
        logger.info("文件头: {}".format(ubinascii.hexlify(content[:4])))

        # 2. base64 编码（标准 base64，无换行）。百度接口直接在 JSON 里收 base64，
        #    不能再 URL 转义（+ / = 若被转义成 %2B/%2F/%3D，服务器解不出正确字节 → image format error）
        b64 = ubinascii.b2a_base64(content)
        b64_encoded = b64.decode("ascii").replace("\n", "").replace("\r", "")

        # 3. 提交 + 轮询
        task_id = self._submit(b64_encoded, question)
        if not task_id:
            return None
        desc = self._get_result(task_id)
        if desc:
            logger.info("图像理解结果: {}".format(desc))
        return desc

    def tts_play(self, text, tmp_path='/usr/_tts.amr'):
        """文字→本地 QuecPython TTS 直接播放（不依赖百度云 TTS）。

        使用 audio.TTS.play(priority, breakin, mode, str)：
          - mode=2 表示文本按 UTF-8 编码解释（中文明文直接播）
          - 与文件播放共用同一个播放队列

        前置条件（必须先做，否则 TTS 无声）：
          1) audio.Audio(0) 通道初始化 + set_pa 配置（告诉硬件 PA 引脚）
          2) audio.Audio(0).setVolume 设置音量（默认 0 没声音）
          3) GPIO9 手动发 NS4160 一线脉冲（audio.TTS.play 不触发 set_pa 回调）

        返回是否成功加入播放队列。
        """
        if not text:
            return False
        try:
            import audio as _amod
            # === 0) 关键：确保 audio channel 0 已初始化 + PA + 音量 ===
            # voice 模式会创建 AudioManager() 做好这些，但 Analyze Photo 不会，
            # 所以这里必须自己做一遍——否则 TTS 无声（通道冷启动 / 音量默认 0）
            try:
                from usr.config import PA_GPIO
                _aud = _amod.Audio(0)
                _aud.set_pa(PA_GPIO, 2)     # 硬件自动 PA 脉冲（对 Audio.play 生效）
                _aud.setVolume(7)           # 设置音量（0=无声，11=最大）
                logger.info("TTS Audio channel init: PA_GPIO=%s volume=7" % PA_GPIO)
            except Exception as e:
                logger.warn("TTS Audio channel init 跳过: %s" % repr(e))
            # === 1) 手动使能 NS4160 功放（GPIO9 一线脉冲 → 2 上升沿 → D 类）===
            # TTS.play 不触发 set_pa 回调（与 opus.write 同理），必须手动双保险
            try:
                from machine import Pin
                from usr.config import PA_GPIO
                _pa = Pin(getattr(Pin, "GPIO{}".format(PA_GPIO)), Pin.OUT, Pin.PULL_DISABLE, 0)
                _pa.write(0)
                utime.sleep_us(30)
                _pa.write(1)
                utime.sleep_us(10)
                _pa.write(0)
                utime.sleep_us(10)
                _pa.write(1)          # 保持高 → D 类工作
                logger.info("TTS PA 已使能(GPIO%s)" % PA_GPIO)
            except Exception as e:
                logger.warn("TTS PA 使能跳过: %s" % repr(e))
            # === 2) EC600M 本地 TTS 仅支持 device=0，mode=2=UTF-8 中文明文 ===
            _tts = _amod.TTS(0)
            ret = _tts.play(3, 1, 2, text)
            logger.info("TTS(本地) 提交: ret=%s text_len=%d" % (ret, len(text)))
            return ret == 0 or ret == 1
        except Exception as e:
            logger.error("TTS(本地) 播放异常: %s" % repr(e))
            return False

