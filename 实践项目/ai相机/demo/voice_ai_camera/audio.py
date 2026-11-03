# -*- coding: UTF-8 -*-
"""音频层：AudioManager（上行录音 + Opus 编解码；下行播放）。

参考 AI_chatbot 项目的 AudioManager，适配相机项目。
- PA 功放使能引脚 GPIO9（官方 EC800M 例程）。
- Opus 模块是 AI 固件专属，标准固件 import Opus 失败 → open_opus() 报错。
"""
import utime
import audio
from machine import Pin

try:
    import Opus
except ImportError:
    Opus = None

from usr.config import PA_GPIO
from usr.logging import getLogger

logger = getLogger("audio")


class AudioManager(object):

    def __init__(self, channel=0, volume=7, pa_gpio=PA_GPIO):
        self.pa = None
        self._pa_enabled = False
        self._setup_pa_gpio(pa_gpio)
        self.aud = audio.Audio(channel)          # 播放通道
        self.aud.set_pa(pa_gpio, 2)              # 配置 PA（gpio, 上电脉冲数）
        self.aud.setVolume(volume)
        self.aud.setCallback(self.audio_cb)
        self.rec = audio.Record(channel)         # 录音通道
        self.rec.gain_set(3, 9)                  # 麦克风增益
        self.volume = volume

    def _setup_pa_gpio(self, pa_gpio):
        """构造 PA 使能引脚对象，初始低电平（NS4160 CTRL 关断态）。

        NS4160 的 CTRL 是「一线脉冲协议」而非电平使能：持续高电平无效，
        需发 2 个上升沿脉冲进入 D 类才出声。真正使能动作在 _ns4160_enable()。
        """
        try:
            self.pa = Pin(getattr(Pin, "GPIO{}".format(pa_gpio)), Pin.OUT, Pin.PULL_DISABLE, 0)
            logger.info("[PA] GPIO{} 初始化（低，关断态）".format(pa_gpio))
        except Exception as e:
            self.pa = None
            logger.error("[PA] GPIO{} setup failed: {}".format(pa_gpio, repr(e)))

    def _ns4160_enable(self, width_us=10):
        """NS4160 一线脉冲协议：发 2 个上升沿 → D 类工作，最后保持高电平。

        datasheet：1 个上升沿→AB 类；2 个上升沿→D 类；低电平>100us→关断；
        脉冲高/低电平宽 1~12us。test_09 实测 3~50us 均可使 opus.write 出声。
        """
        if self.pa is None:
            logger.warning("[PA] 无引脚，跳过使能")
            return
        pa = self.pa
        pa.write(0)
        utime.sleep_us(30)        # 稳定低电平（<100us 不关断）
        pa.write(1)               # 上升沿 1
        utime.sleep_us(width_us)
        pa.write(0)               # 下降沿
        utime.sleep_us(width_us)
        pa.write(1)               # 上升沿 2，保持高 → D 类
        logger.info("[PA] NS4160 enable（2 脉冲 → D 类）")
        self._pa_enabled = True

    def _ns4160_disable(self):
        """关断 PA：CTRL 拉低 >100us。"""
        if self.pa is None:
            return
        self.pa.write(0)
        utime.sleep_us(200)
        self._pa_enabled = False
        logger.info("[PA] NS4160 disable")

    # ---- 音量（0~11）----
    def set_volume(self, v):
        if v < 0:
            v = 0
        elif v > 11:
            v = 11
        self.aud.setVolume(v)
        self.volume = v
        return v

    def volume_up(self):
        return self.set_volume(self.volume + 1)

    def volume_down(self):
        return self.set_volume(self.volume - 1)

    def mute(self):
        self.aud.setVolume(0)
        self.volume = 0
        return 0

    # ---- 播放（下行，功能3 用）----
    def audio_cb(self, event):
        if event == 0:
            logger.info("[AUDIO] play start")
        elif event == 7:
            logger.info("[AUDIO] play finish")

    def play(self, file):
        self.aud.play(0, 1, file)

    def stop(self):
        self.aud.stopAll()

    # ---- Opus 编解码（AI 固件专属）----
    def open_opus(self):
        if Opus is None:
            raise RuntimeError("Opus 模块不存在，需 EC600M AI 固件")
        # PCM 通道：channel=0, 模式=1, 采样率=16000, 位宽=2, ?, 每帧长度=15
        self.pcm = audio.Audio.PCM(0, 1, 16000, 2, 1, 15)
        # Opus 编解码器：绑定 PCM, channel=0, 码率=60000（6000~128000）
        self.opus = Opus(self.pcm, 0, 60000)

    def close_opus(self):
        self.opus.close()
        self.pcm.close()

    def opus_read(self):
        return self.opus.read(60)        # 读 60ms 的 Opus 帧

    def opus_write(self, data):
        # Opus.write/PCM.write 不触发 PA 回调，需手动发脉冲使能 NS4160（仅首次）
        if not self._pa_enabled:
            self._ns4160_enable()
            self._pa_enabled = True
        return self.opus.write(data)     # 写 Opus → 解码播放（功能3 用）
