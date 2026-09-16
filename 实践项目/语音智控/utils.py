import sim
import net
import Opus
import audio
import utime
import dataCall
import checkNet
import sys_bus
from machine import Pin
from machine import ExtInt
import osTimer
from usr.threading import PriorityQueue, Thread
from usr.logging import getLogger


logger = getLogger(__name__)
volume = 7
name = '_xiao_yuan_xiao_yuan'
# ==================== 音频管理 ====================


class AudioManager(object):

    def __init__(self, channel=0, volume=7, pa_number=9):
        # NS4160 功放 CTRL 接 PIN40 = GPIO9, 高电平使能(见原理图 AUDIO 页)。
        self.aud = audio.Audio(channel)  # 初始化音频播放通道(0=听筒->功放->喇叭)
        # 展锐平台 set_pa 只在 audio 播放接口的回调里翻转 PA, opus/PCM 流式播放不触发该回调,
        # PA 会一直停在初始低电平(关闭)导致无声; 这里直接把 CTRL 拉高常开。
        self.__pa = Pin(getattr(Pin, "GPIO{}".format(pa_number)), Pin.OUT, Pin.PULL_PD, 1)
        self.aud.setVolume(volume)  # 设置音量
        self.aud.setCallback(self.audio_cb)
        self.rec = audio.Record(channel)
        self.rec.gain_set(3, 9)  # 保留录音增益设置(EC600M 支持 audio.Record)
        
    def setvolume_down(self):
        global volume
        volume -= 1
        if volume < 0: volume = 0
        self.aud.setVolume(volume)
        return volume
        
    def setvolume_up(self):
        global volume
        volume += 1
        if volume > 11: volume = 11
        self.aud.setVolume(volume)
        return volume
    
    def setvolume_close(self):
        global volume
        self.aud.setVolume(0)
        volume = 0
        return volume
    
    def setvolume(self,data):
        global volume
        self.aud.setVolume(data)
        volume = data
        return volume

    # ========== 音频文件 ====================

    def audio_cb(self, event):
        if event == 0:
            # logger.info('audio play start.')
            pass
        elif event == 7:
            # logger.info('audio play finish.')
            pass
        else:
            pass

    def pa_on(self):
        self.__pa.write(1)

    def play(self, file):
        self.aud.play(0, 1, file)
        
    def stop(self):
        self.aud.stopAll()

    # ========= opus ====================

    def open_opus(self):
        self.pcm = audio.Audio.PCM(0, 1, 16000, 2, 1, 15)  # 5 -> 25
        self.opus = Opus(self.pcm, 0, 60000)  # 6000 ~ 128000

    def close_opus(self):
        self.opus.close()
        self.pcm.close()
        del self.opus
        del self.pcm

    def opus_read(self):
        return self.opus.read(60)

    def opus_write(self, data):
        return self.opus.write(data)

    # ========= 唤醒词设置(供 MCP 语音指令修改用) ====================

    def new_name(self, data):
        global name
        name = data
        # print("当前唤醒词：", name)
        return name


# ==================== 充电管理 ====================


class ChargeManager(object):

    def __init__(self, GPIOn=3):
        self.charge_pin = Pin(getattr(Pin, "GPIO{}".format(GPIOn)), Pin.OUT, Pin.PULL_PU)
    
    def enable_charge(self):
        self.charge_pin.write(1)
    
    def disable_charge(self):
        self.charge_pin.write(0)


# ==================== 网络管理 ====================


class NetManager(object):
    
    def __init__(self):
        # 注册网络回调
        dataCall.setCallback(self.__net_callback)

    def __net_callback(self, args):
        if args[1] == 0:
            sys_bus.publish("NET_STATE_CHANGE", dict(state="net_disconnect"))
            Thread(target=self.wait_network_ready).start()
        else:
            sys_bus.publish("NET_STATE_CHANGE", dict(state="net_connected"))

    @staticmethod
    def make_cfun():
        net.setModemFun(0, 0)
        utime.sleep_ms(200)
        net.setModemFun(1, 0)

    def wait_network_ready(self):
        while True:
            if sim.getStatus() != 1:
                logger.debug('no sim card.')
                sys_bus.publish("NET_STATE_CHANGE", dict(state="no_sim_card"))
            else:
                logger.debug('sim card ready.')
                sys_bus.publish("NET_STATE_CHANGE", dict(state="net_connecting"))
            code = checkNet.waitNetworkReady(10)
            if code == (3, 1):
                logger.info('network ready.')
                break
            else:
                if net.csqQueryPoll() < 18:
                    sys_bus.publish("NET_STATE_CHANGE", dict(state="no_signal"))
                logger.debug('make cfun.')
                self.make_cfun()

    def net_status(self):
        """返回当前网络状态摘要(基本不阻塞), 供未绑定循环等场景检查。"""
        try:
            sim_ok = sim.getStatus() == 1
        except Exception:
            sim_ok = False
        try:
            csq = net.csqQueryPoll()
        except Exception:
            csq = 0
        ip = ""
        try:
            info = dataCall.getInfo(1, 0)
            if isinstance(info, tuple) and info[2][0] == 1:
                ip = info[2][2]
        except Exception:
            pass
        return "SIM={}, CSQ={}, 数据={}".format(
            "就绪" if sim_ok else "异常",
            csq,
            ("已连接 IP=" + ip) if ip else "未连接"
        )

    def diagnose(self, host="api.tenclass.net", port=443):
        """联网自检: 打印 IP / 公网连通性, 定位 OTA 超时原因(数据链路/定向卡/APN)。"""
        try:
            info = dataCall.getInfo(1, 0)
            logger.info("dataCall.getInfo: {}".format(info))
            if isinstance(info, tuple) and info[2][0] == 1:
                logger.info("PDP active, IP = {}".format(info[2][2]))
            else:
                logger.warn("PDP 未激活或未获取到 IP(拨号未成功)")
        except Exception as e:
            logger.warn("dataCall.getInfo failed: {}".format(repr(e)))
        pub = self._probe("www.baidu.com", 443)
        logger.info("public internet (baidu:443) reachable: {}".format(pub))
        tgt = self._probe(host, port)
        logger.info("target {}:{} reachable: {}".format(host, port, tgt))
        if pub and not tgt:
            logger.warn("公网可达但目标不可达: 可能是定向物联网卡(仅白名单) 或服务器问题")
        elif not pub:
            logger.warn("公网不可达: 排查 SIM 流量/欠费 或 APN(dataCall.setPDPContext)")

    @staticmethod
    def _probe(host, port, timeout=5):
        import usocket
        try:
            addr = usocket.getaddrinfo(host, port)[0][-1]
        except Exception as e:
            logger.warn("DNS resolve {} failed: {}".format(host, repr(e)))
            return False
        s = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        try:
            s.settimeout(timeout)
        except Exception:
            pass
        try:
            s.connect(addr)
            return True
        except Exception as e:
            logger.warn("connect {}:{} failed: {}".format(host, port, repr(e)))
            return False
        finally:
            try:
                s.close()
            except Exception:
                pass


# ==================== 任务调度 ====================


class _Task(object):

    def __init__(self, target, args=(), kwargs={}, priority=0, sync=True, title="anon"):
        self.__target = target
        self.args = args
        self.kwargs = kwargs
        self.priority = priority
        self.sync = sync
        self.title = title
    
    def __str__(self):
        return "<Task: {}>".format(self.title)
        
    def __lt__(self, other):
        # 小顶堆优先级排序
        return self.priority < other.priority
    
    def __gt__(self, other):
        return self.priority > other.priority
    
    def __eq__(self, other):
        return self.priority == other.priority
        
    def run(self):
        if self.sync:
            self.__target(*self.args, **self.kwargs)
        else:
            Thread(target=self.__target, args=self.args, kwargs=self.kwargs).start()


class TaskManager(object):

    def __init__(self):
        self.__q = PriorityQueue()
        self.__main_thread = Thread(target=self.__main_loop)
        
    def __main_loop(self):
        while True:
            task = self.__q.get()
            try:
                task.run()
            except Exception as e:
                logger.error("{} run failed, Exception details: {}".format(task, repr(e)))
            else:
                pass
    
    def run_forever(self):
        logger.info('task manager run forever.')
        self.__main_thread.start()

    def submit(self, func, args=(), kwargs={}, priority=0, title="anon"):
        self.__q.put(_Task(target=func, args=args, kwargs=kwargs, priority=priority, title=title))
        
       
       



class Button(object):

    def __init__(self, gpio_number, delay=3000, long_press_callback=lambda: None, short_press_callback=lambda: None):
        self.key = ExtInt(getattr(ExtInt, "GPIO{}".format(gpio_number)), ExtInt.IRQ_RISING_FALLING, ExtInt.PULL_PU, self.__callback, 150)
        self.key.enable()
        self.delay = delay
        self.timer = osTimer()
        self.start_time = None
        self.end_time = None
        self.long_press_callback = long_press_callback
        self.short_press_callback = short_press_callback
        self.is_pressed = False  # 状态变量：按键是否被按下
        
        
    def __callback(self, args):
        gpio_number, mode = args
        if not self.is_pressed:
            # 下降沿，按下
            print("按键按下")
            self.is_pressed = True
            self.start_time = utime.ticks_ms()
            self.timer.start(self.delay, 0, lambda args: self.long_press_callback())
        elif self.is_pressed:
            # 上升沿，释放
            print("按键释放")
            self.is_pressed = False
            self.timer.stop()
            self.end_time = utime.ticks_ms()
            duration = utime.ticks_diff(self.end_time, self.start_time)
            if duration < self.delay:
                self.short_press_callback()
