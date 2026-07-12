"""安全工具模块。

提供凭证指纹化、安全日志输出等通用能力。
所有需要在日志中标识敏感凭证(如 api_hash、r2_secret_key)的场景,
必须通过 hash_api_credential 输出不可逆指纹,禁止输出原值或前 N 位。
"""
from __future__ import annotations

import hashlib


def hash_api_credential(value: str) -> str:
    """返回凭证的不可逆指纹(SHA-256 前 8 位)。

    用途:
    - 日志中标识某个 api_hash / r2_secret_key 的指纹,便于排障又不会泄露原值。
    - 相同原值必返回相同指纹(便于关联同一账号的多次日志)。
    - 不可逆:仅凭指纹无法还原原值(8 位 SHA-256 共 32 bit,碰撞概率极低,
      仅用于日志标识而非安全校验)。

    空值返回 "empty",便于在日志中区分"未配置"与"已配置但指纹化失败"。

    Args:
        value: 原始凭证字符串(如 api_hash)

    Returns:
        8 位十六进制指纹,或 "empty"(空值时)
    """
    if not value:
        return "empty"
    # SHA-256 前 8 位(32 bit),足够日志辨识;原值不出现在任何返回中
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
