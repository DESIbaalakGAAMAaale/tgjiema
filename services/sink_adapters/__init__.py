"""R64 P1-06: 用户面出口 typed adapter 包。

整改背景(R64 终审报告 P1-06):
    业务模块(bots/、services/、admin/)此前直接调用第三方 send/edit/response API
    (telegram.Bot.send_message / query.edit_message_text / JSONResponse 等),
    绕过 ``UserMessage | ErrorEnvelope`` 类型边界,导致:
        1. 裸 ``str(e)`` / ``str(result.error)`` 直接进入用户面消息 params,
           暴露内部异常细节;
        2. ``query.message.text + render_for_send(...)`` 模式继承旧消息语言,
           破坏 locale 隔离;
        3. 100+ 处直调无法被现有 scanner 静态约束。

    本包为唯一允许调用第三方 sink 的边界层:
        - 输入类型强制 ``UserMessage | ErrorEnvelope``(拒绝裸 str);
        - 内部经 ``render_for_send`` 渲染为本地化字符串后调用原生 API;
        - 不拼接旧消息文本(避免语言继承)。

子模块:
    - ``telegram_adapter``: Telegram send/edit API 的 typed adapter
    - ``web_adapter``: FastAPI JSONResponse 的 typed adapter

迁移策略:
    现有 100+ 直调点通过 baseline 机制逐步迁移(见
    ``scripts/check_sink_import_boundary.py``);新增直调由 AST 门禁阻断。
"""
from services.sink_adapters.telegram_adapter import (
    safe_reply_text,
    safe_send_message,
    safe_edit_message_text,
)
from services.sink_adapters.web_adapter import json_response

__all__ = [
    "safe_reply_text",
    "safe_send_message",
    "safe_edit_message_text",
    "json_response",
]
