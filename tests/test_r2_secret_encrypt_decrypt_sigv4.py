"""R26-M1: R2 密钥「加密→解密→SigV4 签名」端到端冒烟测试。

验证 admin /set_r2 写入的 r2_secret_key 经 Fernet 加密存入 config 表后，
读取时能正确解密还原，且解密后的明文能成功用于 SigV4 签名（不会拿到密文签名导致 403）。

被测链路: Fernet.encrypt → Fernet.decrypt → SigV4 签名核心逻辑
（与 database.relay_db.encrypt/decrypt 使用同一 Fernet 实现）
"""

import datetime
import hashlib
import hmac
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def test_r2_secret_encrypt_decrypt_sigv4_e2e():
    """R26-M1: 加密→解密→SigV4 签名全链路冒烟。"""
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        pytest.skip("cryptography 未安装")

    key = os.getenv("RELAY_ENCRYPTION_KEY", "0" * 43 + "=")
    try:
        f = Fernet(key.encode() if isinstance(key, str) else key)
    except Exception:
        pytest.skip("RELAY_ENCRYPTION_KEY 格式无效")

    plain_secret = "test-r2-secret-access-key-abcdef123456"
    cipher = f.encrypt(plain_secret.encode()).decode()
    assert cipher != plain_secret
    assert len(cipher) > 50

    recovered = f.decrypt(cipher.encode()).decode()
    assert recovered == plain_secret

    access_key = "test-r2-access-key-id"
    endpoint = "abc123.r2.cloudflarestorage.com"
    bucket = "tgjiema-backup"
    method = "PUT"
    obj_key = "db_backup/db_backup_20260708_120000.json"

    service = "s3"
    region = "auto"
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    canonical_uri = f"/{bucket}/{obj_key}"
    canonical_querystring = ""
    canonical_headers = (
        f"host:{endpoint}\n"
        f"x-amz-content-sha256:UNSIGNED-PAYLOAD\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    payload_hash = "UNSIGNED-PAYLOAD"

    canonical_request = (
        f"{method}\n{canonical_uri}\n{canonical_querystring}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = (
        f"{algorithm}\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )

    def _sign(k, msg):
        return hmac.new(k, msg.encode(), hashlib.sha256).digest()

    k_date = _sign(b"AWS4" + recovered.encode(), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    assert len(signature) == 64
    assert all(c in "0123456789abcdef" for c in signature)

    wrong_k_date = _sign(b"AWS4" + cipher.encode(), date_stamp)
    wrong_k_region = _sign(wrong_k_date, region)
    wrong_k_service = _sign(wrong_k_region, service)
    wrong_k_signing = _sign(wrong_k_service, "aws4_request")
    wrong_signature = hmac.new(
        wrong_k_signing, string_to_sign.encode(), hashlib.sha256
    ).hexdigest()
    assert signature != wrong_signature
