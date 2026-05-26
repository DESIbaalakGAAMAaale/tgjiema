import asyncio
import json
import re

import httpx
from loguru import logger

from config import settings

_SYSTEM_PROMPT = """分析 Telegram 机器人回复，判断下一步操作。返回 JSON:
{"action":"click_button|wait|finish|error","target_button_row":int|null,"target_button_col":int|null,"target_button_text":"str|null","reason":"str","wait_seconds":int|null}

规则:
- 翻页按钮: "next"/"下一页"/">>"/"▶"/"→"等方向性按钮, 或无文字按钮靠右侧位置推断
- 数字页码"1 2 3 4": 点击下一个数字
- 纯图标无文字按钮: 最右侧通常是下一页
- 第N/N页或翻页按钮消失: finish
- 错误消息"未找到"/"已过期": error
- 消息未收齐: wait

按钮格式: row:col:data:text"""


def _parse_json_from_response(text: str) -> dict:
    text = text.strip()
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error(f"[AI Agent] 无法解析 AI 返回的 JSON: {text[:200]}")
        return {"action": "finish", "target_button_row": None, "target_button_col": None, "target_button_text": None, "reason": "AI返回无法解析", "wait_seconds": None}


class AIAgent:
    def __init__(self):
        self._api_base = ""
        self._api_key = ""
        self._model = ""
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def configure(self):
        self._api_base = (settings.AI_API_BASE_URL or "").strip().rstrip("/")
        if self._api_base.endswith("/chat/completions"):
            self._api_base = self._api_base[:-len("/chat/completions")]
        self._api_key = (settings.AI_API_KEY or "").strip()
        self._model = (settings.AI_MODEL or "gpt-4o-mini").strip()
        self._enabled = bool(self._api_base and self._api_key)
        if self._enabled:
            logger.info(f"[AI Agent] 已启用: base={self._api_base}, model={self._model}")
        else:
            logger.warning("[AI Agent] 未配置 AI_API_BASE_URL / AI_API_KEY，AI 决策已禁用")

    @staticmethod
    def _describe_buttons(reply_markup) -> list[str]:
        buttons = []
        if not reply_markup or not hasattr(reply_markup, "rows"):
            return buttons
        for row_idx, row in enumerate(reply_markup.rows):
            for col_idx, btn in enumerate(row.buttons):
                data = getattr(btn, "data", None) or ""
                text = (getattr(btn, "text", None) or "").strip()
                buttons.append(f"{row_idx}:{col_idx}:{data}:{text}")
        return buttons

    @staticmethod
    def _describe_message(msg) -> dict:
        info: dict = {}
        if getattr(msg, "photo", None):
            info["type"] = "photo"
        elif getattr(msg, "video", None):
            info["type"] = "video"
        elif getattr(msg, "voice", None):
            info["type"] = "voice"
        elif getattr(msg, "audio", None):
            info["type"] = "audio"
        elif getattr(msg, "gif", None):
            info["type"] = "animation(gif)"
        elif getattr(msg, "document", None):
            info["type"] = "document"
        elif getattr(msg, "message", None):
            info["type"] = "text"
        else:
            info["type"] = "text"

        text = getattr(msg, "message", None) or ""
        if text:
            info["caption"] = text[:200]

        if getattr(msg, "animated", None):
            info["animated"] = True
        if getattr(msg, "sticker", None):
            info["type"] = "sticker"

        if msg.reply_markup:
            info["buttons"] = AIAgent._describe_buttons(msg.reply_markup)

        return info

    def _build_context(self, exchange_data: dict) -> str:
        bot_username = exchange_data.get("bot_username", "unknown_bot")
        msg_events = exchange_data.get("events", [])

        descriptions = []
        for ev in msg_events:
            d = self._describe_message(ev.message)
            descriptions.append(d)

        return json.dumps(
            {
                "bot_username": bot_username,
                "total_messages_received": len(descriptions),
                "message_sequence": descriptions,
            },
            ensure_ascii=False,
            indent=2,
        )

    async def decide(self, exchange_data: dict) -> dict:
        if not self._enabled:
            logger.warning("[AI Agent] AI 未启用，返回代码内置默认行为")
            return self._fallback_decision(exchange_data)

        ctx = self._build_context(exchange_data)
        logger.info(f"[AI Agent] 发送决策请求, context length={len(ctx)}")

        max_retries = 2
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(30)) as client:
                    resp = await client.post(
                        f"{self._api_base}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self._model,
                            "messages": [
                                {"role": "system", "content": _SYSTEM_PROMPT},
                                {"role": "user", "content": ctx},
                            ],
                            "temperature": 0.1,
                            "max_tokens": 800,
                        },
                    )

                    if resp.status_code == 429:
                        logger.warning("[AI Agent] 429 限流，直接回退到内置决策")
                        return self._fallback_decision(exchange_data)

                    if resp.status_code >= 400:
                        body = resp.text[:500]
                        logger.error(
                            f"[AI Agent] HTTP {resp.status_code}: {body}"
                        )

                    resp.raise_for_status()
                    data = resp.json()
                    logger.debug(f"[AI Agent] 完整响应: {json.dumps(data, ensure_ascii=False)[:1000]}")

                    ai_text = ""
                    choices = data.get("choices", [])
                    if choices:
                        msg = choices[0].get("message") or {}
                        ai_text = msg.get("content") or ""
                        if not ai_text:
                            ai_text = choices[0].get("text") or ""
                        if not ai_text:
                            ai_text = msg.get("reasoning") or ""
                    if not ai_text:
                        logger.error(f"[AI Agent] AI 返回空内容, 原始: {json.dumps(data, ensure_ascii=False)[:500]}")
                        return self._fallback_decision(exchange_data)

                    logger.info(f"[AI Agent] AI 回复: {ai_text[:200]}")

                    decision = _parse_json_from_response(ai_text)
                    logger.info(f"[AI Agent] 决策: action={decision.get('action')}, reason={decision.get('reason')}")
                    return decision

            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"[AI Agent] 调用失败 ({type(e).__name__})，"
                        f"第{attempt+1}/{max_retries}次重试"
                    )
                    await asyncio.sleep(3)
                else:
                    logger.error(f"[AI Agent] 调用失败，回退到默认行为: {type(e).__name__}: {e}")
                    return self._fallback_decision(exchange_data)

        return self._fallback_decision(exchange_data)

    def _fallback_decision(self, exchange_data: dict) -> dict:
        msg_events = exchange_data.get("events", [])
        if not msg_events:
            return {"action": "finish", "target_button_row": None, "target_button_col": None, "target_button_text": None, "reason": "回退: 无消息", "wait_seconds": None}

        _NEXT_KW = ("next", "下一页", "下一頁", "\u2192", "\u25b6", "\u27a1", ">>", "\u00bb")

        for ev in msg_events:
            msg = ev.message
            if not (msg.reply_markup and hasattr(msg.reply_markup, "rows") and msg.reply_markup.rows):
                continue

            rows = msg.reply_markup.rows

            # Phase 1: text-based "next" detection
            for row_idx, row in enumerate(rows):
                for col_idx, btn in enumerate(row.buttons):
                    text = (getattr(btn, "text", None) or "").lower().strip()
                    if any(kw in text for kw in _NEXT_KW):
                        return {
                            "action": "click_button",
                            "target_button_row": row_idx,
                            "target_button_col": col_idx,
                            "target_button_text": getattr(btn, "text", None) or "",
                            "reason": "回退: 检测到下一页按钮",
                            "wait_seconds": None,
                        }

            # Phase 2: number pagination
            for row_idx, row in enumerate(rows):
                btn_texts = [(getattr(b, "text", None) or "").strip() for b in row.buttons]
                digits = [t for t in btn_texts if t.isdigit()]
                if len(digits) >= 3:
                    # All-digit row: "1 2 3 4 5" → click "2"
                    sorted_digits = sorted(digits, key=int)
                    if "2" in btn_texts:
                        col_idx = btn_texts.index("2")
                        return {
                            "action": "click_button",
                            "target_button_row": row_idx,
                            "target_button_col": col_idx,
                            "target_button_text": "2",
                            "reason": "回退: 数字页码，点击第2页",
                            "wait_seconds": None,
                        }
                    # Click the second smallest
                    if len(sorted_digits) >= 2:
                        second = sorted_digits[1]
                        if second in btn_texts:
                            col_idx = btn_texts.index(second)
                            return {
                                "action": "click_button",
                                "target_button_row": row_idx,
                                "target_button_col": col_idx,
                                "target_button_text": second,
                                "reason": f"回退: 数字页码，点击第{second}页",
                                "wait_seconds": None,
                            }

            # Phase 3: icon-only — click rightmost button with callback_data
            for row_idx in range(len(rows) - 1, -1, -1):
                row = rows[row_idx]
                if not row.buttons:
                    continue
                btn_texts = [(getattr(b, "text", None) or "").strip() for b in row.buttons]
                all_empty = all(not t for t in btn_texts)
                if all_empty:
                    last_btn = row.buttons[-1]
                    if getattr(last_btn, "data", None):
                        return {
                            "action": "click_button",
                            "target_button_row": row_idx,
                            "target_button_col": len(row.buttons) - 1,
                            "target_button_text": "",
                            "reason": "回退: 纯图标，点击最右侧按钮",
                            "wait_seconds": None,
                        }

        return {"action": "finish", "target_button_row": None, "target_button_col": None, "target_button_text": None, "reason": "回退: 无翻页按钮", "wait_seconds": None}


ai_agent = AIAgent()