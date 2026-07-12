import re

# Telegram Bot API file_id 标准前缀(6 项)
# 移除非标准 AAQ/AAOF(无文档来源,可能误判)
_SUPPORTED_FILE_ID_PREFIXES = (
    "CAAC", "AgAD", "BAAC", "CQAC", "AwAC", "BQAC",
)

# file_id 字符集:Base64URL 变体(A-Za-z0-9_-)
_FILE_ID_CHARSET = re.compile(r'^[A-Za-z0-9_-]+$')
# file_id 最小长度(前缀 4 + 至少若干 payload 字符)
_MIN_FILE_ID_LEN = 10


def is_likely_bot_api_file_id(text: str) -> bool:
    """判断字符串是否可能是 Telegram Bot API file_id。

    P3: 添加最小长度 + 字符集校验,避免短前缀子串误判。
    """
    if not text or not isinstance(text, str):
        return False
    cleaned = text.strip().strip('"').strip("'")
    # P3: 长度校验(标准 file_id 远长于 4 字符前缀)
    if len(cleaned) < _MIN_FILE_ID_LEN:
        return False
    # P3: 字符集校验(Base64URL 变体,排除含空格/中文/特殊符号的误判)
    if not _FILE_ID_CHARSET.match(cleaned):
        return False
    return cleaned.startswith(_SUPPORTED_FILE_ID_PREFIXES)
