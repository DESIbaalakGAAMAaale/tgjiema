import re
import secrets
import string

from database import get_file_records_col

CODE_ALPHABET = string.ascii_lowercase + string.digits
FILE_TYPE_LABELS = {"photo": "p", "video": "v", "document": "d", "audio": "a", "animation": "g"}

_BOT_PATTERN = re.compile(r"^[a-zA-Z0-9_]+bot", re.IGNORECASE)


def _generate_random_part(length: int = 12) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def build_file_code(file_types: dict) -> str:
    from config import settings

    prefix = settings.FILE_CODE_PREFIX
    random_part = _generate_random_part(12)
    type_parts = []
    for label, abbr in FILE_TYPE_LABELS.items():
        count = file_types.get(label, 0)
        if count > 0:
            type_parts.append(f"{count}{abbr}")
    suffix = "_".join(type_parts) if type_parts else "0d"
    return f"{prefix}_{random_part}_{suffix}"


def is_valid_code_format(code: str) -> bool:
    from config import settings

    if code.startswith(settings.FILE_CODE_PREFIX):
        return True
    return bool(_BOT_PATTERN.match(code))


def extract_bot_username(code: str) -> str:
    match = _BOT_PATTERN.match(code)
    if match:
        return match.group(0)
    return ""


def parse_file_types_from_code(code: str) -> dict:
    abbr_to_label = {v: k for k, v in FILE_TYPE_LABELS.items()}
    parts = code.split("_")
    type_parts = parts[2:]
    result = {}
    for tp in type_parts:
        abbr = tp[-1]
        count = tp[:-1]
        if abbr in abbr_to_label and count.isdigit():
            result[abbr_to_label[abbr]] = int(count)
    return result


async def generate_unique_code(file_types: dict) -> str:
    col = get_file_records_col()
    for _ in range(100):
        code = build_file_code(file_types)
        existing = await col.find_one({"file_code": code})
        if existing is None:
            return code
    raise RuntimeError("无法生成唯一文件码，请稍后重试")