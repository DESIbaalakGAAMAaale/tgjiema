"""回归测试 11 —— MonBot 接入全局 stop_event（P1-15）。

P1-15: MonBot 构造接收 stop_event 并存为 _stop_event；run_mon 创建并经由
run_all._set_stop_event 注册全局停止事件，确保 run_all 触发停止时 Mon 优雅退出。
"""

import asyncio
import sys
import types

from unittest.mock import AsyncMock

from bots.mon_bot import MonBot, run_mon


def test_mon_stop_event_registration(monkeypatch):
    # 1) 构造阶段：stop_event 必须被保存
    ev = asyncio.Event()
    mon = MonBot(stop_event=ev)
    assert mon._stop_event is ev
    # 未注册时默认为 None（独立运行仍可靠 self._running 退出）
    assert MonBot()._stop_event is None

    # 2) run_mon 注册阶段：注入假 run_all，避免导入/执行真实 run_all 多进程逻辑
    captured = []
    constructed = []
    fake_run_all = types.ModuleType("run_all")

    def _set_stop_event(e):
        captured.append(e)

    fake_run_all._set_stop_event = _set_stop_event
    monkeypatch.setitem(sys.modules, "run_all", fake_run_all)

    real_init = MonBot.__init__

    def _init(self, stop_event=None):
        constructed.append(stop_event)
        real_init(self, stop_event)

    monkeypatch.setattr(MonBot, "__init__", _init)
    monkeypatch.setattr(MonBot, "start", AsyncMock())
    monkeypatch.setattr(MonBot, "stop", AsyncMock())

    asyncio.run(run_mon())

    # run_mon 确实创建了 stop 事件并注册
    assert len(captured) == 1
    assert isinstance(captured[0], asyncio.Event)
    # run_mon 把同一事件传给 MonBot 构造（stop_event 接线正确）
    assert constructed and constructed[0] is captured[0]
