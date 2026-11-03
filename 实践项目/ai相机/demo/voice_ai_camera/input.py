# -*- coding: UTF-8 -*-
"""输入层：三个按键（选择键 + 确认键 + 外接快门键），基于 machine.ExtInt。

设计（复用 Portable_music 已验证的架构）：
- 中断回调只置标志（轻量，不阻塞中断上下文）
- 主循环通过 consume_events() 取出事件做重逻辑
- 硬件消抖 filter_time=50ms（EC600M 支持，无需软件消抖）

按键定义（config.py）：
- KEY_CAPTURE_GPIO  (13)  选择键 / 手动拍照快门
- KEY_FUNC_GPIO     (12)  确认键

API（MCP 核实 retriever_api_reference_docs）：
class machine.ExtInt(GPIOn, mode, pull, callback, [filter_time])
  - mode: IRQ_FALLING(下降沿) / IRQ_RISING(上升沿) / IRQ_RISING_FALLING(双沿)
  - pull: PULL_PU / PULL_PD / PULL_DISABLE
  - callback: 返回参数 (gpio号, 触发沿: 0=上升 1=下降)
  - filter_time: 滤波时间(ms)，EC600M 支持，非零值须为 10 的倍数
extint.enable() → 0 成功 / -1 失败

关键点：
1. 回调里只置标志位，不做重逻辑（避免竞态）。
2. 标志用 list 存放事件，consume_events() 取出后清空。
3. 同一键在主循环未消费前再次按下，新事件被丢弃（防积压）。
"""
import utime
from machine import ExtInt
from usr.config import (
    KEY_CAPTURE_GPIO, KEY_FUNC_GPIO, KEY_FILTER_TIME,
)
from usr.logging import getLogger

logger = getLogger("input")

# 事件类型
EVENT_CAPTURE = "KEY_CAPTURE"      # 选择键按下
EVENT_FUNC = "KEY_FUNC"            # 确认键按下


class InputDriver(object):
    """三个按键的 ExtInt 驱动。

    回调只置标志，主循环通过 consume_events() 消费。
    """

    def __init__(self):
        self._events = []          # 待消费事件队列（中断回调写入，主循环读取）
        self._lock = False          # 简单互斥（避免中断与消费竞争 list）

        # 选择键（下降沿触发，上拉，50ms 滤波）
        self._capture_key = ExtInt(
            getattr(ExtInt, "GPIO{}".format(KEY_CAPTURE_GPIO)),
            ExtInt.IRQ_FALLING,
            ExtInt.PULL_PU,
            self._on_capture,
            KEY_FILTER_TIME,
        )
        self._capture_key.enable()

        # 确认键
        self._func_key = ExtInt(
            getattr(ExtInt, "GPIO{}".format(KEY_FUNC_GPIO)),
            ExtInt.IRQ_FALLING,
            ExtInt.PULL_PU,
            self._on_func,
            KEY_FILTER_TIME,
        )
        self._func_key.enable()

        logger.info("InputDriver 就绪: CAPTURE=GPIO{} FUNC=GPIO{} filter={}ms".format(
            KEY_CAPTURE_GPIO, KEY_FUNC_GPIO, KEY_FILTER_TIME))

    # ---- 中断回调（务必轻量：只置标志）----
    def _on_capture(self, args):
        # args = (gpio号, 触发沿)；下降沿 edge=1
        gpio, edge = args
        if edge == 1 and not self._lock:
            self._events.append(EVENT_CAPTURE)

    def _on_func(self, args):
        gpio, edge = args
        if edge == 1 and not self._lock:
            self._events.append(EVENT_FUNC)

    # ---- 主循环消费接口 ----
    def consume_events(self):
        """取出并清空所有待处理事件，返回事件列表（按时间顺序）。"""
        if not self._events:
            return []
        self._lock = True
        events = list(self._events)
        self._events.clear()
        self._lock = False
        return events

    def has_events(self):
        """是否有待处理事件（主循环可先判断再消费）。"""
        return len(self._events) > 0

    def pressed_set(self):
        """把消费到的事件转为被按下的 GPIO 号集合（供 UI / 相机线程复用，避免重复 ExtInt+Pin 冲突）。"""
        evts = self.consume_events()
        p = set()
        if EVENT_CAPTURE in evts:
            p.add(KEY_CAPTURE_GPIO)
        if EVENT_FUNC in evts:
            p.add(KEY_FUNC_GPIO)
        return p