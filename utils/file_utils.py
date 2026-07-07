"""文件相关工具函数 — 检测文件类型、提取文件元信息、判断媒体消息

P1-16 整改:统一媒体类型词表。
所有媒体类型字符串集中定义在 MEDIA_TYPE 常量集中，避免 voice/audio 等
字符串在各模块间不一致（file_utils / relay_instance / code_generator 必须
引用同一组字符串）。新增 sticker 类型。
"""

from telegram import Update

# ─── 规范媒体类型词表(P1-16) ───────────────────────────────
# 全局唯一来源:file_utils / relay_instance / code_generator 均引用此处常量，
# 杜绝 voice vs audio 等字符串分歧。键为逻辑类别,值为规范类型字符串。
MEDIA_TYPE = {
    "PHOTO": "photo",
    "VIDEO": "video",
    "AUDIO": "audio",
    "VOICE": "voice",
    "DOCUMENT": "document",
    "STICKER": "sticker",
    "ANIMATION": "animation",
}

# 便捷别名
PHOTO = MEDIA_TYPE["PHOTO"]
VIDEO = MEDIA_TYPE["VIDEO"]
AUDIO = MEDIA_TYPE["AUDIO"]
VOICE = MEDIA_TYPE["VOICE"]
DOCUMENT = MEDIA_TYPE["DOCUMENT"]
STICKER = MEDIA_TYPE["STICKER"]
ANIMATION = MEDIA_TYPE["ANIMATION"]


def detect_file_type(update: Update) -> str:
    if not update.message:
        return DOCUMENT
    if update.message.photo:
        return PHOTO
    if update.message.video:
        return VIDEO
    if update.message.document:
        return DOCUMENT
    if update.message.audio:
        return AUDIO
    if update.message.voice:
        return VOICE
    if update.message.sticker:
        return STICKER
    if update.message.animation:
        return ANIMATION
    return DOCUMENT


def extract_file_meta(update: Update) -> dict:
    if not update.message:
        return {"type": DOCUMENT, "file_id": ""}
    msg = update.message
    if msg.photo:
        return {"type": PHOTO, "file_id": msg.photo[-1].file_id}
    if msg.video:
        return {"type": VIDEO, "file_id": msg.video.file_id}
    if msg.document:
        return {"type": DOCUMENT, "file_id": msg.document.file_id}
    if msg.audio:
        return {"type": AUDIO, "file_id": msg.audio.file_id}
    if msg.voice:
        return {"type": VOICE, "file_id": msg.voice.file_id}
    if msg.sticker:
        return {"type": STICKER, "file_id": msg.sticker.file_id}
    if msg.animation:
        return {"type": ANIMATION, "file_id": msg.animation.file_id}
    return {"type": DOCUMENT, "file_id": ""}


def extract_media_info(msg):
    if msg is None:
        return None, DOCUMENT
    if msg.photo:
        return msg.photo[-1].file_id, PHOTO
    if msg.video:
        return msg.video.file_id, VIDEO
    if msg.audio:
        return msg.audio.file_id, AUDIO
    if msg.voice:
        return msg.voice.file_id, VOICE
    if msg.animation:
        return msg.animation.file_id, ANIMATION
    if msg.sticker:
        return msg.sticker.file_id, STICKER
    if msg.document:
        return msg.document.file_id, DOCUMENT
    return None, DOCUMENT


def is_media_message(msg) -> bool:
    if msg is None:
        return False
    return any((
        msg.photo, msg.video, msg.document,
        msg.audio, msg.voice, msg.sticker, msg.animation,
    ))
