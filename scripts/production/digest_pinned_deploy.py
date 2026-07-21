"""R67 Wave 4: digest 锁定部署验证(digest-pinned deploy verifier)。

R67 审计要求(Wave 4 — RC tag 正式演练):
    "tag workflow、environment approval、production evidence、digest-pinned
     deploy、rollback 全通过"

digest-pinned deploy 确保部署使用不可变 image digest 而非可变 tag:
    1. 部署引用必须是 digest(image_ref 含 @sha256:...)
    2. digest 必须与 release manifest 一致
    3. digest 必须与 attestation subject 一致
    4. digest 必须与 verify-only-3x 验证的 digest 一致
    5. 禁止 tag 漂移(deploy_ref 不能仅是 :tag)

使用方法:
    verifier = DigestPinnedDeployVerifier(
        release_manifest_digest="sha256:abc...",
        attestation_subject_digest="sha256:abc...",
        verify_only_3x_digest="sha256:abc...",
    )
    result = verifier.verify_deploy_ref(
        deploy_ref="ghcr.io/owner/repo@sha256:abc...",
    )
"""
from __future__ import annotations

import re
from typing import Any

# digest 正则(sha256: 后跟 64 个 hex 字符)
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


def _extract_digest(ref: str) -> str | None:
    """从镜像引用中提取 digest(sha256:...)。

    支持以下格式:
        - ghcr.io/owner/repo@sha256:abc...
        - sha256:abc...
        - ghcr.io/owner/repo:tag@sha256:abc...

    Returns:
        digest 字符串(含 sha256: 前缀),无 digest 返回 None
    """
    if not ref:
        return None
    if "@" not in ref:
        return None
    digest_part = ref.split("@", 1)[1]
    if _DIGEST_PATTERN.match(digest_part):
        return digest_part
    return None


def _normalize_digest(digest: str) -> str:
    """标准化 digest(确保含 sha256: 前缀)。"""
    if not digest:
        return ""
    digest = digest.strip()
    if digest.startswith("sha256:"):
        return digest
    # 仅 hex 字符 — 添加 sha256: 前缀
    if re.match(r"^[0-9a-fA-F]{64}$", digest):
        return f"sha256:{digest}"
    return digest


class DigestPinnedDeployVerifier:
    """R67 Wave 4: digest 锁定部署验证器。

    确保部署使用不可变 image digest,且 digest 在 release manifest、
    attestation subject、verify-only-3x 验证记录中一致。
    """

    def __init__(
        self,
        *,
        release_manifest_digest: str,
        attestation_subject_digest: str = "",
        verify_only_3x_digest: str = "",
    ) -> None:
        """初始化验证器。

        Args:
            release_manifest_digest: release manifest 中的 image digest
            attestation_subject_digest: attestation subject[0].digest.sha256
            verify_only_3x_digest: verify-only-3x 验证记录中的 image digest
        """
        if not release_manifest_digest:
            raise ValueError("release_manifest_digest 不能为空")

        self.release_manifest_digest = _normalize_digest(release_manifest_digest)
        self.attestation_subject_digest = _normalize_digest(attestation_subject_digest)
        self.verify_only_3x_digest = _normalize_digest(verify_only_3x_digest)

    def verify_deploy_ref(
        self,
        deploy_ref: str,
    ) -> dict[str, Any]:
        """验证部署引用是否为 digest 锁定且与各来源一致。

        Args:
            deploy_ref: 部署引用(如 "ghcr.io/owner/repo@sha256:abc...")

        Returns:
            {
                "verified": bool,
                "reason": str,
                "deploy_digest": str,
                "release_manifest_digest": str,
                "attestation_subject_digest": str,
                "verify_only_3x_digest": str,
                "all_digests_match": bool,
            }
        """
        if not deploy_ref:
            return self._failure("deploy_ref 为空")

        # 1. 部署引用必须含 digest
        deploy_digest = _extract_digest(deploy_ref)
        if not deploy_digest:
            return self._failure(
                f"deploy_ref 不含有效 digest(必须是 @sha256:... 形式): "
                f"{deploy_ref!r} — 禁止仅使用 :tag 部署(tag 漂移风险)",
                deploy_digest="",
            )

        deploy_digest = _normalize_digest(deploy_digest)

        # 2. digest 格式校验
        if not _DIGEST_PATTERN.match(deploy_digest):
            return self._failure(
                f"deploy_digest 格式无效: {deploy_digest!r}",
                deploy_digest=deploy_digest,
            )

        # 3. digest 必须与 release manifest 一致
        if deploy_digest != self.release_manifest_digest:
            return self._failure(
                f"deploy_digest={deploy_digest!r} 与 release_manifest_digest="
                f"{self.release_manifest_digest!r} 不一致",
                deploy_digest=deploy_digest,
            )

        # 4. digest 必须与 attestation subject 一致(若提供)
        if (
            self.attestation_subject_digest
            and deploy_digest != self.attestation_subject_digest
        ):
            return self._failure(
                f"deploy_digest={deploy_digest!r} 与 attestation_subject_digest="
                f"{self.attestation_subject_digest!r} 不一致",
                deploy_digest=deploy_digest,
            )

        # 5. digest 必须与 verify-only-3x 验证记录一致(若提供)
        if (
            self.verify_only_3x_digest
            and deploy_digest != self.verify_only_3x_digest
        ):
            return self._failure(
                f"deploy_digest={deploy_digest!r} 与 verify_only_3x_digest="
                f"{self.verify_only_3x_digest!r} 不一致 "
                f"(verify-only-3x 未验证此 digest)",
                deploy_digest=deploy_digest,
            )

        return {
            "verified": True,
            "reason": "",
            "deploy_digest": deploy_digest,
            "release_manifest_digest": self.release_manifest_digest,
            "attestation_subject_digest": self.attestation_subject_digest,
            "verify_only_3x_digest": self.verify_only_3x_digest,
            "all_digests_match": True,
        }

    def _failure(
        self,
        reason: str,
        deploy_digest: str = "",
    ) -> dict[str, Any]:
        return {
            "verified": False,
            "reason": reason,
            "deploy_digest": deploy_digest,
            "release_manifest_digest": self.release_manifest_digest,
            "attestation_subject_digest": self.attestation_subject_digest,
            "verify_only_3x_digest": self.verify_only_3x_digest,
            "all_digests_match": False,
        }


def verify_digest_pinned_deploy(
    deploy_ref: str,
    *,
    release_manifest_digest: str,
    attestation_subject_digest: str = "",
    verify_only_3x_digest: str = "",
) -> dict[str, Any]:
    """便捷函数:验证 digest 锁定部署(无需实例化 verifier)。"""
    verifier = DigestPinnedDeployVerifier(
        release_manifest_digest=release_manifest_digest,
        attestation_subject_digest=attestation_subject_digest,
        verify_only_3x_digest=verify_only_3x_digest,
    )
    return verifier.verify_deploy_ref(deploy_ref)
