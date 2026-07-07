"""回归测试 10 —— 媒体类型词表统一（P1-16）。

file_utils / code_generator / relay_instance 必须引用同一组 MEDIA_TYPE 规范字符串，
杜绝 voice vs audio 等漂移；relay_instance._detect_media_type 对 voice 返回 "voice"。
"""

from types import SimpleNamespace

from services.code_generator import FILE_TYPE_LABELS
from services.relay_instance import RelayInstance
from utils.file_utils import MEDIA_TYPE


def test_media_type_constants():
    # P1-16 关键：voice 必须返回 "voice" 而非 "audio"
    assert MEDIA_TYPE["VOICE"] == "voice"
    assert MEDIA_TYPE["AUDIO"] == "audio"
    # 7 种类型齐全且值唯一
    assert set(MEDIA_TYPE.keys()) == {
        "PHOTO", "VIDEO", "AUDIO", "VOICE", "DOCUMENT", "STICKER", "ANIMATION"
    }
    assert len(set(MEDIA_TYPE.values())) == 7


def test_file_type_labels_reference_media_type():
    # code_generator 的缩写直接引用 file_utils.MEDIA_TYPE 规范字符串
    assert FILE_TYPE_LABELS[MEDIA_TYPE["VOICE"]] == "o"
    assert FILE_TYPE_LABELS[MEDIA_TYPE["STICKER"]] == "s"
    # 字符集 [pvdagos] 与缩写一致
    assert set(FILE_TYPE_LABELS.values()) == {"p", "v", "d", "a", "g", "o", "s"}


def test_detect_media_type_returns_voice():
    # relay_instance._detect_media_type 统一引用 MEDIA_TYPE（P1-16）
    assert (
        RelayInstance._detect_media_type(SimpleNamespace(voice=SimpleNamespace()))
        == MEDIA_TYPE["VOICE"]
    )
    assert (
        RelayInstance._detect_media_type(SimpleNamespace(photo=SimpleNamespace()))
        == MEDIA_TYPE["PHOTO"]
    )
    assert (
        RelayInstance._detect_media_type(SimpleNamespace(sticker=SimpleNamespace()))
        == MEDIA_TYPE["STICKER"]
    )
    # document 且 mime 含 video -> video
    doc_video = SimpleNamespace(document=SimpleNamespace(mime_type="video/mp4"))
    assert RelayInstance._detect_media_type(doc_video) == MEDIA_TYPE["VIDEO"]
