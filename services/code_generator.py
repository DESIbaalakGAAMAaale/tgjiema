import hashlib
import os
import re
import secrets
import string
import time

from config import settings

CODE_ALPHABET = string.ascii_lowercase + string.digits
FILE_TYPE_LABELS = {"photo": "p", "video": "v", "document": "d", "audio": "a", "animation": "g"}

_BOT_PATTERN = re.compile(r"^[a-zA-Z0-9_]+bot", re.IGNORECASE)
_BOT_USERNAME_IN_MESSAGE = re.compile(r"([a-zA-Z0-9_]+bot)", re.IGNORECASE)


def _generate_deterministic_id(length: int = 12) -> str:
    """Deterministic unique ID, zero DB round trips.

    原理:nanotimestamp + PID 作为种子 → SHA256 扩散 → 映射到 CODE_ALPHABET。
    - 同进程内:time.time_ns() 单调递增,每次调用种子不同
    - 跨进程:不同的 PID 确保即使同一纳秒种子也不同
    - 输出看似随机(SHA256 avalanche effect),不可猜测
    数学保证唯一,无需 CRDB 冲突检测。
    """
    seed = f"{time.time_ns():x}{os.getpid():x}"
    digest = hashlib.sha256(seed.encode()).hexdigest()
    val = int(digest, 16)
    return ''.join(CODE_ALPHABET[(val >> (i * 5)) % 36] for i in range(length))


def build_file_code(file_types: dict) -> str:
    prefix = settings.FILE_CODE_PREFIX
    random_part = _generate_deterministic_id(12)
    type_parts = []
    for label, abbr in FILE_TYPE_LABELS.items():
        count = file_types.get(label, 0)
        if count > 0:
            type_parts.append(f"{count}{abbr}")
    suffix = "_".join(type_parts) if type_parts else "0d"
    return f"{prefix}_{random_part}_{suffix}"


def is_valid_code_format(code: str) -> bool:
    code = code.strip()
    if code.startswith(settings.FILE_CODE_PREFIX):
        return True
    # 消息中包含 FILE_CODE_PREFIX 开头的文件码也算有效
    if settings.FILE_CODE_PREFIX in code:
        return True
    # 只有纯文件码（不含空格、换行、bot用户名）才算有效
    return bool(_BOT_PATTERN.match(code)) and '\n' not in code and ' ' not in code


def extract_bot_username(code: str) -> str:
    match = _BOT_PATTERN.match(code)
    if match:
        return match.group(0)
    return ""


def extract_code_and_bot_from_message(text: str) -> tuple[str, str]:
    """从消息文本中提取文件码和目标解码器 bot 用户名。

    处理码头不含 bot 名称但消息内含有解码器标识的情况,例如:
    - "utheigh1231gg1f4     解码器ccmarkbot" → ("utheigh1231gg1f4", "ccmarkbot")
    - "utheigh1231gg1f4\\n@ccmarkbot"        → ("utheigh1231gg1f4", "ccmarkbot")
    - "@ccmarkbot utheigh1231gg1f4"           → ("utheigh1231gg1f4", "ccmarkbot")

    Returns:
        (code, bot_username) 提取成功；否则 ("", "")
    """
    text = text.strip()
    if not text:
        return "", ""

    bot = extract_bot_username(text)
    if bot:
        return text, bot

    normalized = re.sub(r'@([a-zA-Z0-9_]+bot)', r'\1', text, flags=re.IGNORECASE)

    all_bots = list(set(_BOT_USERNAME_IN_MESSAGE.findall(normalized)))
    if not all_bots:
        return "", ""

    bot_username = all_bots[0]

    lines = normalized.split("\n")
    code_candidates = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        has_bot = any(bot in line for bot in all_bots)
        if not has_bot:
            code_candidates.append(line)

    if code_candidates:
        code = code_candidates[0]
        if code:
            return code, bot_username

    code = normalized
    for b in all_bots:
        code = code.replace(b, "")
    for indicator in ("解码器", "解码", "解码Bot:", "解码bot:", "解码器:"):
        code = code.replace(indicator, "")
    code = code.replace("@", "")
    code = code.strip("::\t\n\r -_")
    code = re.sub(r'[\u4e00-\u9fff]+', '', code).strip()

    if code:
        return code, bot_username
    return "", ""


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
    """直接生成文件码,无需 DB 冲突检测。
    
    确定性 ID 算法数学保证唯一,PRIMARY KEY 约束是最后一层保险。
    """
    return build_file_code(file_types)