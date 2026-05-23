import re

_SUPPORTED_FILE_ID_PREFIXES = (
    "CAAC", "AgAD", "BAAC", "CQAC", "AwAC", "BQAC",
    "AAQ", "AAOF",
)


def is_likely_bot_api_file_id(text: str) -> bool:
    cleaned = text.strip().strip('"').strip("'")
    return cleaned.startswith(_SUPPORTED_FILE_ID_PREFIXES)


def looks_like_external_code(text: str) -> bool:
    return re.match(r'^@?\w+?[:_]\w+', text.strip()) is not None


def parse_external_code(text: str) -> tuple[str, str]:
    text = text.strip()
    parts = re.split(r'[:_]', text, maxsplit=1)
    if len(parts) == 2:
        bot_part = parts[0].lstrip("@")
        code_part = parts[1]
        return bot_part, code_part
    return "", text
