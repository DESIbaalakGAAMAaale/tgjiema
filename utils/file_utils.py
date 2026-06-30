"""文件相关工具函数 — 检测文件类型、提取文件元信息、判断媒体消息"""

from telegram import Update


def detect_file_type(update: Update) -> str:
    if not update.message:
        return "document"
    if update.message.photo:
        return "photo"
    if update.message.video:
        return "video"
    if update.message.document:
        return "document"
    if update.message.audio:
        return "audio"
    if update.message.voice:
        return "audio"
    if update.message.animation:
        return "animation"
    return "document"


def extract_file_meta(update: Update) -> dict:
    if not update.message:
        return {"type": "document", "file_id": ""}
    msg = update.message
    if msg.photo:
        return {"type": "photo", "file_id": msg.photo[-1].file_id}
    if msg.video:
        return {"type": "video", "file_id": msg.video.file_id}
    if msg.document:
        return {"type": "document", "file_id": msg.document.file_id}
    if msg.audio:
        return {"type": "audio", "file_id": msg.audio.file_id}
    if msg.voice:
        return {"type": "audio", "file_id": msg.voice.file_id}
    if msg.animation:
        return {"type": "animation", "file_id": msg.animation.file_id}
    return {"type": "document", "file_id": ""}


def extract_media_info(msg):
    if msg.photo:
        return msg.photo[-1].file_id, "photo"
    if msg.video:
        return msg.video.file_id, "video"
    if msg.audio:
        return msg.audio.file_id, "audio"
    if msg.voice:
        return msg.voice.file_id, "voice"
    if msg.animation:
        return msg.animation.file_id, "animation"
    if msg.document:
        return msg.document.file_id, "document"
    return None, "document"


def is_media_message(msg) -> bool:
    return any((
        msg.photo, msg.video, msg.document,
        msg.audio, msg.voice, msg.animation,
    ))