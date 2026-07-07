import hashlib
import os
import re
import secrets
import string
import time

from loguru import logger

from config import settings
from utils.file_utils import MEDIA_TYPE

CODE_ALPHABET = string.ascii_lowercase + string.digits
# P1-16:媒体类型词表键直接引用 file_utils.MEDIA_TYPE 规范字符串,消除各模块类型字符串漂移。
# 值(缩写)用于内部码后缀:photo/video/document/audio/animation/voice/sticker
#   → p / v / d / a / g / o / s
FILE_TYPE_LABELS = {
    MEDIA_TYPE["PHOTO"]: "p",
    MEDIA_TYPE["VIDEO"]: "v",
    MEDIA_TYPE["DOCUMENT"]: "d",
    MEDIA_TYPE["AUDIO"]: "a",
    MEDIA_TYPE["ANIMATION"]: "g",
    MEDIA_TYPE["VOICE"]: "o",
    MEDIA_TYPE["STICKER"]: "s",
}

_BOT_PATTERN = re.compile(r"^[a-zA-Z0-9_]+bot", re.IGNORECASE)
_BOT_USERNAME_IN_MESSAGE = re.compile(r"([a-zA-Z0-9_]+bot)", re.IGNORECASE)
# 内部文件码后缀格式: {12位base36}_{类型后缀}
# 例: a1b2c3d4e5f6_3p_2v_1d
# 前缀由 settings.FILE_CODE_PREFIX 动态校验，不在此正则中硬编码
# 字符集 [pvdagos] 对应 FILE_TYPE_LABELS 的缩写: photo/video/document/audio/animation/voice/sticker
_INTERNAL_CODE_SUFFIX_PATTERN = re.compile(r"^[a-z0-9]{12}(?:_\d+[pvdagos])+$")


def _generate_deterministic_id(length: int = 12) -> str:
    """密码学安全随机 ID，零 DB 往返。

    S-5: 改用 secrets.randbelow 替代 SHA256(time_ns+PID)，
    消除确定性派生带来的可预测性风险，碰撞概率极低（36^12 ≈ 4.7×10^18）。
    DB PRIMARY KEY 作为最后防线。
    """
    result = []
    for _ in range(length):
        result.append(CODE_ALPHABET[secrets.randbelow(36)])
    return ''.join(result)


def build_file_code(file_types: dict) -> str:
    prefix = settings.FILE_CODE_PREFIX
    random_part = _generate_deterministic_id(12)
    type_parts = []
    logger.info(f"[build_file_code] input file_types={file_types!r}, type={type(file_types).__name__}, keys={list(file_types.keys()) if isinstance(file_types, dict) else 'N/A'}, repr_bytes={str(file_types).encode('utf-8')!r}")
    for label, abbr in FILE_TYPE_LABELS.items():
        if isinstance(file_types, dict):
            count = file_types.get(label, 0)
            # 详细排查 key 不匹配问题
            for actual_key, actual_val in file_types.items():
                if actual_key == label:
                    count = actual_val
                    logger.info(f"[build_file_code] EXACT key match: label={label!r}, actual_key={actual_key!r}, actual_key_bytes={actual_key.encode('utf-8')!r}, val={actual_val!r}")
                    break
        else:
            count = 0
        logger.info(f"[build_file_code] label={label!r}, abbr={abbr!r}, count={count!r}, count>0={count > 0}, type(count)={type(count).__name__}")
        if count > 0:
            type_parts.append(f"{count}{abbr}")
    if not type_parts:
        logger.error(f"[build_file_code] file_types 为空或无已知类型! file_types={file_types!r}, 将使用 0d 兜底")
        suffix = "0d"
    else:
        suffix = "_".join(type_parts)
    result = f"{prefix}_{random_part}_{suffix}"
    logger.info(f"[build_file_code] FINAL code={result!r}")
    return result


def is_valid_code_format(code: str) -> bool:
    """判断文本是否为有效文件码格式。
    
    内部码格式: {prefix}_{12位base36}_{类型后缀}，如 tgwenjian_a1b2c3d4e5f6_3p_2v_1d
    外部码格式: {botname}bot 开头，如 ccmarkbotutheigh1231gg1f4
    不在消息中随意匹配，防止误判普通文本。
    """
    code = code.strip()
    if not code:
        return False
    # 内部码：前缀严格匹配 settings.FILE_CODE_PREFIX，后缀校验格式
    prefix = settings.FILE_CODE_PREFIX
    if code.startswith(prefix + "_"):
        suffix = code[len(prefix) + 1:]
        if _INTERNAL_CODE_SUFFIX_PATTERN.match(suffix):
            return True
    # 外部码：bot 名称开头，且不含空格/换行
    if _BOT_PATTERN.match(code) and '\n' not in code and ' ' not in code:
        return True
    return False


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
        # B8: 仅提取第一个空白分隔的 token 作为文件码，不把含 bot 名的整条消息当码。
        # 外部码是单个 token（以 bot 名开头，不含空格/换行），如 "ccmarkbotutheigh1231gg1f4"。
        # 整条消息可能是 "ccmarkbotutheigh1231gg1f4  解码器ccmarkbot"，仅取首段。
        first_token = text.split()[0] if text.split() else text
        return first_token, bot

    normalized = re.sub(r'@([a-zA-Z0-9_]+bot)', r'\1', text, flags=re.IGNORECASE)

    all_bots = list(set(_BOT_USERNAME_IN_MESSAGE.findall(normalized)))
    if not all_bots:
        return "", ""

    # 排除系统自己的三个 bot 用户名，避免用户转发/复制文件码消息时误识别为第三方码
    # 同时排除文件码前缀(可能以 "bot" 结尾,如 mfilebot),否则会被误识别为外部 bot
    system_bots = {
        settings.UPLOAD_BOT_USERNAME.lower(),
        settings.DECODER_BOT_USERNAME.lower(),
        settings.SENDER_BOT_USERNAME.lower(),
        settings.FILE_CODE_PREFIX.lower(),
    }
    external_bots = [b for b in all_bots if b.lower() not in system_bots]
    if not external_bots:
        return "", ""

    bot_username = external_bots[0]

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
    """生成文件码。碰撞概率极低，数据库 PRIMARY KEY 是最后防线。"""
    return build_file_code(file_types)