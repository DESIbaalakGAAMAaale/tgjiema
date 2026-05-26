import asyncio
import json
import re

import httpx
from loguru import logger

from config import settings

_SYSTEM_PROMPT = """你是一个 Telegram 机器人交互分析器。你的任务是分析外部 Telegram 机器人对文件码请求的回复，然后给出明确的下一步操作指令。

## 背景
用户通过我们的系统向第三方 Telegram 机器人发送了一个文件码。现在第三方机器人回复了一些消息，你需要分析这些回复并决定下一步该做什么。

## 你的能力
1. 检测是否有翻页按钮（内联键盘）。翻页按钮可能以各种形式出现：纯文字、emoji、纯图标（无文字）。你需要根据按钮位置和上下文推断其含义。
2. 判断媒体组是否完整，是否需要等待更多消息。
3. 判断当前状态：是否所有文件已返回、是否有错误提示需要处理。

## 输出格式
你必须严格返回以下 JSON 格式，不要包含任何其他内容：

```json
{
  "action": "click_button | wait | finish | error",
  "target_button_row": 数字或null,
  "target_button_col": 数字或null,
  "target_button_text": "按钮文字或null",
  "reason": "你的判断理由，简短说明",
  "wait_seconds": 数字或null
}
```

## action 含义
- **click_button**: 发现了翻页按钮，需要点击它来获取更多内容
- **wait**: 没有更多操作，但可能还有消息在路上，建议等待
- **finish**: 所有文件已接收完毕，可以结束本次交互
- **error**: 对方返回了错误消息，无法获取文件

## 判断规则
- 如果键盘有">>"、"next"、"下一页"、"下一頁"、向右箭头、▶ 等方向性按钮，那是翻页按钮
- 如果按钮没有文字（纯图标），根据它在键盘中的位置判断：通常右侧/最后的按钮是"下一页"，左侧的是"上一页"
- 如果当前页的媒体组已经完整发送，且还有下一页按钮，点击按钮获取下一页
- 如果消息包含"没有找到"、"文件不存在"、"已过期"、"not found" 等错误信息，返回 error
- 如果所有页面已获取完毕（翻页按钮消失），返回 finish
- 如果消息刚到达不久（还有更多媒体组在路上），返回 wait

## 按钮数据结构
按钮以 `row:col:data:text` 格式描述，例如：
- `0:0::下一页 ▶` — 第0行第0列，无callback_data，文字"下一页 ▶"
- `0:2::` — 第0行第2列，无文字（纯图标按钮）
- `1:0:SOMEDATA:确认` — 第1行第0列，callback_data为"SOMEDATA"，文字"确认"
"""


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

        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as client:
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
                        retry_after = 5 * (2 ** attempt)
                        logger.warning(
                            f"[AI Agent] 429 限流，第{attempt+1}/{max_retries}次重试，"
                            f"等待 {retry_after}s"
                        )
                        await asyncio.sleep(retry_after)
                        continue

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
                    wait = 3 * (2 ** attempt)
                    logger.warning(
                        f"[AI Agent] 调用失败 ({type(e).__name__})，"
                        f"第{attempt+1}/{max_retries}次重试，等待 {wait}s"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"[AI Agent] 调用失败，回退到默认行为: {type(e).__name__}: {e}")
                    return self._fallback_decision(exchange_data)

        return self._fallback_decision(exchange_data)

    def _fallback_decision(self, exchange_data: dict) -> dict:
        msg_events = exchange_data.get("events", [])
        if not msg_events:
            return {"action": "finish", "target_button_row": None, "target_button_col": None, "target_button_text": None, "reason": "无消息，直接结束", "wait_seconds": None}

        for ev in msg_events:
            msg = ev.message
            if msg.reply_markup and hasattr(msg.reply_markup, "rows") and msg.reply_markup.rows:
                for row_idx, row in enumerate(msg.reply_markup.rows):
                    for col_idx, btn in enumerate(row.buttons):
                        text = (getattr(btn, "text", None) or "").lower().strip()
                        if any(kw in text for kw in ("next", "下一页", "下一頁", "\u2192", "\u25b6", "\u27a1")):
                            return {
                                "action": "click_button",
                                "target_button_row": row_idx,
                                "target_button_col": col_idx,
                                "target_button_text": getattr(btn, "text", None) or "",
                                "reason": "回退模式: 检测到下一页按钮",
                                "wait_seconds": None,
                            }

        return {"action": "finish", "target_button_row": None, "target_button_col": None, "target_button_text": None, "reason": "回退模式: 无翻页按钮", "wait_seconds": None}


ai_agent = AIAgent()