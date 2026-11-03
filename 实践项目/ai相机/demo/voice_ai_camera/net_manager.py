# -*- coding: UTF-8 -*-
"""网络管理：驻网等待 / 连接状态检测 / SIM 热插拔检测 / 网络断开恢复。

QuecPython API（MCP AIotCoder 已验证 EC600MCN_LE 支持）：
  - sim.setSimDet(1, 0)          开启 SIM 热插拔检测（立即生效，掉电保存）
  - sim.setCallback(usrFun)      SIM 插拔回调：args=1 插入, args=2 拔出
  - dataCall.setCallback(fun)    网络 PDP 回调：args[1]=0 断开, args[1]=1 连接
  - checkNet.waitNetworkReady    (stage, state)=(3,1) 就绪
  - dataCall.getInfo             code=1 有公网 IP
  - sim.getStatus()              1=就绪

双保险策略：
  - sim.setCallback 响应物理插拔（根因）
  - dataCall.setCallback 响应网络实际断连（更可靠，不管是不是 SIM 导致的断网都能捕获）
    - 可注入网络断开/恢复处理器，供业务层按需处理网络变化
"""
try:
    import sim
    import net
    import utime
    import dataCall
    import checkNet
    from usr.logging import getLogger
    from usr.config import SIM_HOTPLUG_TRIGGER_LEVEL
except Exception:
    # 本地调试桩
    sim = net = dataCall = checkNet = utime = None
    SIM_HOTPLUG_TRIGGER_LEVEL = 1
    class _L:
        def info(self, m): print("[INFO][net] " + m)
        def warn(self, m): print("[WARN][net] " + m)
        def error(self, m): print("[ERR ][net] " + m)
    def getLogger(_): return _L()

logger = getLogger("net")

NET_TIMEOUT_PER_ROUND = 15      # 每次 checkNet 超时秒数
NET_MAX_RETRIES = 3             # 最大重试次数


class NetManager(object):

    def __init__(self):
        # 网络状态变化回调，由业务层按需注入
        self._on_network_disconnected = None   # 网络断开时调用
        self._on_network_reconnected = None    # 网络恢复时调用
        self._last_network_state = None        # dataCall 上一次的网络状态
        self._disconnect_cooldown_until = 0    # 断开处理冷却（ms），防抖动

    # ---- 事件注入 ----
    def set_disconnect_handler(self, handler):
        """注册网络断开处理器。"""
        self._on_network_disconnected = handler

    def set_reconnect_handler(self, handler):
        """注册网络恢复处理器。"""
        self._on_network_reconnected = handler

    # ------------------------------------------------------------------
    @staticmethod
    def _toggle_modem():
        """关/开射频，重置基带（应对信号弱/未驻网）。"""
        try:
            net.setModemFun(0, 0)
            utime.sleep_ms(500)
            net.setModemFun(1, 0)
            utime.sleep(2)
        except Exception as e:
            logger.warn("toggle_modem 异常: {}".format(repr(e)))

    def is_ready(self):
        """非阻塞：PDP 激活且有 IP → True。"""
        try:
            info = dataCall.getInfo(1, 0)
            code = info[1]
            ips = info[2] if len(info) >= 3 else None
            if code == 1 and ips and ips[0] != "0.0.0.0":
                return True
        except Exception:
            pass
        return False

    def get_signal(self):
        """获取当前信号强度（dBm）。"""
        try:
            if net is None:
                return None
            sig = net.getSignal(0)
            if isinstance(sig, (list, tuple)) and len(sig) > 1:
                lte = sig[1]
                if isinstance(lte, (list, tuple)) and len(lte) > 1:
                    rsrp = lte[1]
                    if isinstance(rsrp, int) and rsrp != 99:
                        return rsrp
            csq = net.csqQueryPoll()
            if isinstance(csq, int) and csq not in (-1, 99):
                return csq - 111
            return None
        except Exception as e:
            logger.warn("获取信号强度异常: {}".format(repr(e)))
            return None

    # ------------------------------------------------------------------
    # SIM 热插拔 + dataCall 网络回调注册（EC600MCN_LE 已验证支持）
    # ------------------------------------------------------------------
    def enable_hot_plug_detection(self):
        """开启 SIM 热插拔 + dataCall 网络回调（双保险）。

        调用时机：联网成功后（main 初始化完成时）。
        EC600MCN_LE 支持 sim.setSimDet + sim.setCallback（MCP 已核实不在不支持黑名单）。
        dataCall.setCallback 覆盖所有系列，更可靠（网络真断就回调）。
        """
        if sim is None or dataCall is None:
            logger.warn("非 QuecPython 环境：跳过热插拔检测注册")
            return

        # 1) 开启 SIM 热插拔功能（必须先开才能触发 setCallback）
        #    ★ triggerLevel 关键：多数板子 SIM 在位时是高电平 → 设 1；低电平 → 设 0
        try:
            ret = sim.setSimDet(1, SIM_HOTPLUG_TRIGGER_LEVEL)
            logger.info("sim.setSimDet(1,{}) → {}".format(SIM_HOTPLUG_TRIGGER_LEVEL, ret))
        except Exception as e:
            logger.warn("sim.setSimDet 异常: {}（可能此固件不支持，忽略）".format(e))

        # 2) 注册 SIM 热插拔回调
        try:
            def _sim_hotplug_cb(args):
                # args: 1=SIM 插入, 2=SIM 拔出
                try:
                    if args == 2:
                        logger.warn("[SIM] SIM 卡拔出")
                        # 幂等保护：dataCall 之后可能再次触发断开 handler。
                        self._on_network_disconnected()
                    elif args == 1:
                        logger.info("[SIM] SIM 卡插入，等待网络恢复...")
                        # SIM 插回后等待 dataCall state=1 确认网络恢复。
                except Exception as e:
                    logger.warn("[SIM] 热插拔回调异常: {}".format(e))
            ret = sim.setCallback(_sim_hotplug_cb)
            logger.info("sim.setCallback 注册 → {}".format(ret))
        except Exception as e:
            logger.warn("sim.setCallback 异常: {}".format(e))

        # 3) 注册 dataCall 网络断开/恢复回调（更可靠，核心断开处理走这里）
        try:
            def _data_call_cb(args):
                # args: (pdp_id, state)  state=0 网络断开, state=1 网络重连
                try:
                    pdp_id, state = args[0], args[1]
                    logger.info("[dataCall] pdp={} state={}".format(pdp_id, state))
                    if state == 0:
                        self._on_network_disconnected()
                    elif state == 1:
                        self._on_network_reconnected()
                except Exception as e:
                    logger.warn("[dataCall] 回调异常: {}".format(e))
            ret = dataCall.setCallback(_data_call_cb)
            logger.info("dataCall.setCallback 注册 → {}".format(ret))
        except Exception as e:
            logger.warn("dataCall.setCallback 异常: {}".format(e))

    # ------------------------------------------------------------------
    # 断开处理 + 重连处理（调用者需注入）
    # ------------------------------------------------------------------
    def _on_network_disconnected(self):
        """网络断开统一入口（带冷却防抖，避免频繁重复断链）。"""
        now = utime.ticks_ms()
        if now < self._disconnect_cooldown_until:
            logger.info("[net] 断开冷却中，跳过重复处理")
            return
        self._disconnect_cooldown_until = now + 3000   # 3s 冷却
        logger.warn("[net] ★ 网络断开 ★")
        if self._on_network_disconnected:
            try:
                self._on_network_disconnected()
            except Exception as e:
                logger.warn("[net] disconnect handler 异常: {}".format(e))

    def _on_network_reconnected(self):
        """网络恢复统一入口。"""
        logger.info("[net] ★ 网络恢复 ★")
        if self._on_network_reconnected:
            try:
                self._on_network_reconnected()
            except Exception as e:
                logger.warn("[net] reconnect handler 异常: {}".format(e))

    # ------------------------------------------------------------------
    def wait_network_ready(self, timeout_per_round=None, max_tries=None):
        """阻塞直到网络就绪。返回 True=成功 False=失败。"""
        if timeout_per_round is None:
            timeout_per_round = NET_TIMEOUT_PER_ROUND
        if max_tries is None:
            max_tries = NET_MAX_RETRIES

        for attempt in range(1, max_tries + 1):
            logger.info("等待联网 ({}/{}) ...".format(attempt, max_tries))
            if sim is not None and sim.getStatus() != 1:
                logger.warn("SIM 卡未就绪 (status={})".format(sim.getStatus()))
            if checkNet is None:
                logger.warn("非 QuecPython 环境：跳过 waitNetworkReady")
                return True
            code = checkNet.waitNetworkReady(timeout_per_round)
            if code == (3, 1):
                logger.info("网络就绪: stage=3 state=1, code={}".format(code))
                return True
            logger.warn("联网超时 code={} 尝试 toggle_modem".format(code))
            self._toggle_modem()
        logger.error("联网失败，经过 {} 次尝试".format(max_tries))
        return False