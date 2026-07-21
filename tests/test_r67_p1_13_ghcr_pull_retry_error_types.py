"""R67 P1-13: GHCR pull retry 错误类型限制 — 单元测试。

R67 P1-13 整改要求:
    "仅对 404/manifest unknown/429/5xx 等瞬态错误重试;401/403、
     digest mismatch、TLS/签名错误应立即 fail"

测试覆盖:
    A. 瞬态错误(允许重试)
        - 404 / manifest unknown
        - 429 (rate limit)
        - 5xx (server error)
        - 网络瞬态(timeout/reset/EOF/unreachable/no route)
    B. 非瞬态错误(立即失败,attempts=1)
        - 401 / unauthorized
        - 403 / forbidden
        - permission/access denied
        - TLS / x509 / certificate 错误
        - digest mismatch
        - signature mismatch / verification failed
        - malformed manifest
    C. 未知错误(fail-closed,立即失败)
    D. _pull_with_retry 行为验证
        - 每种非瞬态错误 → attempts=1, fatal_error 非空
        - 每种瞬态错误 → attempts > 1 (重试),最终成功或耗尽
    E. release-gates.yml 工作流含分类重试策略
        - 含 401/403/unauthorized/forbidden 立即失败
        - 含 TLS/x509/certificate 立即失败
        - 含 digest/signature mismatch 立即失败
        - 含 malformed manifest 立即失败
        - 含瞬态错误重试(manifest unknown/429/5xx/timeout)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# A. 瞬态错误(允许重试)
# ════════════════════════════════════════════════════════════════


class TestTransientErrors:
    """R67 P1-13: 瞬态错误应被识别为可重试。"""

    @pytest.mark.parametrize("error_msg", [
        "manifest unknown",
        "Error: 404 Not Found",
        "429 Too Many Requests",
        "rate limit exceeded",
        "500 Internal Server Error",
        "502 Bad Gateway",
        "503 Service Unavailable",
        "504 Gateway Timeout",
        "timeout after 30s",
        "timed out waiting for connection",
        "connection reset by peer",
        "EOF during read",
        "temporary failure in name resolution",
        "network is unreachable",
        "no route to host",
        "registry is busy",
    ])
    def test_transient_error_classified_as_transient(self, error_msg):
        """每种瞬态错误应被 _is_transient_error 识别为 True。"""
        from scripts.verify_rc_3x import _is_transient_error
        assert _is_transient_error(error_msg), (
            f"R67 P1-13: {error_msg!r} 应被分类为瞬态错误(允许重试)"
        )

    @pytest.mark.parametrize("error_msg", [
        "manifest unknown",
        "Error: 404 Not Found",
        "429 Too Many Requests",
        "500 Internal Server Error",
        "502 Bad Gateway",
        "503 Service Unavailable",
        "timeout after 30s",
        "connection reset by peer",
    ])
    def test_transient_error_not_fatal(self, error_msg):
        """瞬态错误不应被 _is_fatal_error 识别为 True。"""
        from scripts.verify_rc_3x import _is_fatal_error
        assert not _is_fatal_error(error_msg), (
            f"R67 P1-13: {error_msg!r} 不应被分类为非瞬态(致命)错误"
        )


# ════════════════════════════════════════════════════════════════
# B. 非瞬态错误(立即失败)
# ════════════════════════════════════════════════════════════════


class TestFatalErrors:
    """R67 P1-13: 非瞬态错误应被识别为致命(立即失败)。"""

    @pytest.mark.parametrize("error_msg", [
        # 401 / unauthorized
        "401 Unauthorized",
        "authentication required",
        "unauthorized: authentication required",
        # 403 / forbidden
        "403 Forbidden",
        "forbidden: access denied",
        # permission / access denied
        "permission denied",
        "access denied",
    ])
    def test_auth_permission_errors_are_fatal(self, error_msg):
        """401/403/auth/permission 错误应被 _is_fatal_error 识别为 True。"""
        from scripts.verify_rc_3x import _is_fatal_error
        assert _is_fatal_error(error_msg), (
            f"R67 P1-13: {error_msg!r} 应被分类为非瞬态错误(auth/permission 立即失败)"
        )

    @pytest.mark.parametrize("error_msg", [
        "401 Unauthorized",
        "403 Forbidden",
        "permission denied",
        "access denied",
    ])
    def test_auth_permission_errors_not_transient(self, error_msg):
        """401/403/auth/permission 错误不应被 _is_transient_error 识别为 True。"""
        from scripts.verify_rc_3x import _is_transient_error
        assert not _is_transient_error(error_msg), (
            f"R67 P1-13: {error_msg!r} 不应被分类为瞬态(auth/permission 不可重试)"
        )

    @pytest.mark.parametrize("error_msg", [
        "TLS certificate verification failed",
        "x509: certificate signed by unknown authority",
        "certificate has expired",
        "certificate is invalid",
        "x509 certificate expired",
        "TLS handshake error",
    ])
    def test_tls_certificate_errors_are_fatal(self, error_msg):
        """TLS/证书错误应被 _is_fatal_error 识别为 True。"""
        from scripts.verify_rc_3x import _is_fatal_error
        assert _is_fatal_error(error_msg), (
            f"R67 P1-13: {error_msg!r} 应被分类为非瞬态错误(TLS/证书 立即失败)"
        )

    @pytest.mark.parametrize("error_msg", [
        "TLS certificate verification failed",
        "x509: certificate signed by unknown authority",
        "certificate has expired",
    ])
    def test_tls_certificate_errors_not_transient(self, error_msg):
        """TLS/证书错误不应被 _is_transient_error 识别为 True。"""
        from scripts.verify_rc_3x import _is_transient_error
        assert not _is_transient_error(error_msg), (
            f"R67 P1-13: {error_msg!r} 不应被分类为瞬态(TLS/证书 不可重试)"
        )

    @pytest.mark.parametrize("error_msg", [
        "digest mismatch: expected sha256:abc, got sha256:def",
        "signature mismatch",
        "signature verification failed",
        "invalid signature",
    ])
    def test_digest_signature_errors_are_fatal(self, error_msg):
        """digest/signature mismatch 错误应被 _is_fatal_error 识别为 True。"""
        from scripts.verify_rc_3x import _is_fatal_error
        assert _is_fatal_error(error_msg), (
            f"R67 P1-13: {error_msg!r} 应被分类为非瞬态错误(digest/signature 立即失败)"
        )

    @pytest.mark.parametrize("error_msg", [
        "digest mismatch",
        "signature mismatch",
        "signature verification failed",
    ])
    def test_digest_signature_errors_not_transient(self, error_msg):
        """digest/signature mismatch 错误不应被 _is_transient_error 识别为 True。"""
        from scripts.verify_rc_3x import _is_transient_error
        assert not _is_transient_error(error_msg), (
            f"R67 P1-13: {error_msg!r} 不应被分类为瞬态(digest/signature 不可重试)"
        )

    @pytest.mark.parametrize("error_msg", [
        "malformed manifest",
        "malformed manifest: invalid format",
    ])
    def test_malformed_manifest_errors_are_fatal(self, error_msg):
        """malformed manifest 错误应被 _is_fatal_error 识别为 True。"""
        from scripts.verify_rc_3x import _is_fatal_error
        assert _is_fatal_error(error_msg), (
            f"R67 P1-13: {error_msg!r} 应被分类为非瞬态错误(malformed manifest 立即失败)"
        )


# ════════════════════════════════════════════════════════════════
# C. 未知错误(fail-closed,立即失败)
# ════════════════════════════════════════════════════════════════


class TestUnknownErrorsFailClosed:
    """R67 P1-13: 未知错误应 fail-closed(立即失败,不重试)。"""

    @pytest.mark.parametrize("error_msg", [
        "some weird unknown error",
        "unexpected panic in registry",
        "completely unrecognized failure mode",
    ])
    def test_unknown_error_not_transient(self, error_msg):
        """未知错误不应被 _is_transient_error 识别为 True(fail-closed)。"""
        from scripts.verify_rc_3x import _is_transient_error
        assert not _is_transient_error(error_msg), (
            f"R67 P1-13: {error_msg!r} 不应被分类为瞬态(未知错误 fail-closed)"
        )


# ════════════════════════════════════════════════════════════════
# D. _pull_with_retry 行为验证 — 每种错误类型的实际重试行为
# ════════════════════════════════════════════════════════════════


class TestPullWithRetryBehavior:
    """R67 P1-13: _pull_with_retry 对每种错误类型的实际重试行为。"""

    @pytest.mark.parametrize("fatal_msg", [
        "401 Unauthorized",
        "403 Forbidden",
        "permission denied",
        "access denied",
        "TLS certificate verification failed",
        "x509 certificate expired",
        "digest mismatch",
        "signature mismatch",
        "signature verification failed",
        "malformed manifest",
        "invalid signature",
    ])
    def test_fatal_error_immediate_failure_attempts_1(self, fatal_msg):
        """每种非瞬态错误 → attempts=1(立即失败,不重试)。"""
        from scripts.verify_rc_3x import _pull_with_retry
        with patch("scripts.verify_rc_3x._run_cmd") as mock_run:
            mock_run.return_value = (1, "", fatal_msg)
            result = _pull_with_retry("ghcr.io/test@sha256:abc", max_attempts=5)
        assert not result["success"], f"{fatal_msg!r} 应导致失败"
        assert result["attempts"] == 1, (
            f"R67 P1-13: {fatal_msg!r} 应立即失败(attempts=1),实际 attempts={result['attempts']}"
        )
        assert result["fatal_error"] is not None
        assert "fatal" in result["fatal_error"].lower(), (
            f"fatal_error 应含 'fatal' 标记,实际: {result['fatal_error']!r}"
        )

    @pytest.mark.parametrize("transient_msg", [
        "manifest unknown",
        "404 Not Found",
        "429 Too Many Requests",
        "500 Internal Server Error",
        "503 Service Unavailable",
        "timeout after 30s",
        "connection reset by peer",
    ])
    def test_transient_error_retries_multiple_attempts(self, transient_msg):
        """每种瞬态错误 → attempts > 1(重试),最终 max_attempts_exceeded。"""
        from scripts.verify_rc_3x import _pull_with_retry
        with patch("scripts.verify_rc_3x._run_cmd") as mock_run:
            mock_run.return_value = (1, "", transient_msg)
            with patch("scripts.verify_rc_3x.time.sleep"):  # 加速测试
                result = _pull_with_retry(
                    "ghcr.io/test@sha256:abc",
                    max_attempts=3,
                    total_budget=3600,
                )
        assert not result["success"]
        assert result["attempts"] == 3, (
            f"R67 P1-13: {transient_msg!r} 应重试至 max_attempts=3,"
            f"实际 attempts={result['attempts']}"
        )
        assert result["fatal_error"] == "max_attempts_exceeded"

    def test_unknown_error_immediate_failure_attempts_1(self):
        """未知错误 → attempts=1(fail-closed,立即失败)。"""
        from scripts.verify_rc_3x import _pull_with_retry
        with patch("scripts.verify_rc_3x._run_cmd") as mock_run:
            mock_run.return_value = (1, "", "completely unrecognized error message")
            result = _pull_with_retry("ghcr.io/test@sha256:abc", max_attempts=5)
        assert not result["success"]
        assert result["attempts"] == 1, (
            "R67 P1-13: 未知错误应 fail-closed(attempts=1),"
            f"实际 attempts={result['attempts']}"
        )
        assert "unknown" in result["fatal_error"].lower() or "fail-closed" in result["fatal_error"].lower()


# ════════════════════════════════════════════════════════════════
# E. release-gates.yml 工作流含分类重试策略
# ════════════════════════════════════════════════════════════════


class TestReleaseGatesWorkflowClassification:
    """R67 P1-13: release-gates.yml 必须含分类重试策略。"""

    @pytest.fixture
    def workflow_content(self):
        """加载 release-gates.yml 工作流内容。"""
        workflow_path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        if not workflow_path.exists():
            pytest.skip("release-gates.yml 不存在")
        return workflow_path.read_text(encoding="utf-8")

    def test_workflow_contains_401_403_immediate_fail(self, workflow_content):
        """工作流应含 401/403/unauthorized/forbidden 立即失败检查。"""
        # 至少包含 401 或 unauthorized
        has_auth = (
            "401" in workflow_content
            or "unauthorized" in workflow_content.lower()
        )
        assert has_auth, (
            "R67 P1-13: release-gates.yml 应含 401/unauthorized 立即失败检查"
        )
        # 至少包含 403 或 forbidden
        has_forbidden = (
            "403" in workflow_content
            or "forbidden" in workflow_content.lower()
        )
        assert has_forbidden, (
            "R67 P1-13: release-gates.yml 应含 403/forbidden 立即失败检查"
        )

    def test_workflow_contains_tls_certificate_immediate_fail(self, workflow_content):
        """工作流应含 TLS/x509/certificate 立即失败检查。"""
        has_tls = (
            "TLS" in workflow_content
            or "tls" in workflow_content.lower()
            or "x509" in workflow_content.lower()
            or "certificate" in workflow_content.lower()
        )
        assert has_tls, (
            "R67 P1-13: release-gates.yml 应含 TLS/certificate 立即失败检查"
        )

    def test_workflow_contains_digest_signature_mismatch_immediate_fail(self, workflow_content):
        """工作流应含 digest/signature mismatch 立即失败检查。"""
        content_lower = workflow_content.lower()
        has_digest_mismatch = "digest mismatch" in content_lower
        has_sig_mismatch = (
            "signature mismatch" in content_lower
            or "signature verification failed" in content_lower
        )
        assert has_digest_mismatch or has_sig_mismatch, (
            "R67 P1-13: release-gates.yml 应含 digest/signature mismatch 立即失败检查"
        )

    def test_workflow_contains_malformed_manifest_immediate_fail(self, workflow_content):
        """工作流应含 malformed manifest 立即失败检查。"""
        content_lower = workflow_content.lower()
        has_malformed = (
            "malformed manifest" in content_lower
            or "invalid manifest" in content_lower
        )
        assert has_malformed, (
            "R67 P1-13: release-gates.yml 应含 malformed manifest 立即失败检查"
        )

    def test_workflow_contains_transient_retry(self, workflow_content):
        """工作流应含瞬态错误重试逻辑(manifest unknown/429/5xx/timeout)。"""
        content_lower = workflow_content.lower()
        # 至少一个瞬态错误模式
        has_transient = (
            "manifest unknown" in content_lower
            or "429" in workflow_content
            or "5xx" in content_lower
            or "timeout" in content_lower
        )
        assert has_transient, (
            "R67 P1-13: release-gates.yml 应含瞬态错误重试逻辑"
        )

    def test_workflow_contains_retry_max_attempts(self, workflow_content):
        """工作流应含 MAX_ATTEMPTS 限制(避免无限重试)。"""
        has_max_attempts = (
            "MAX_ATTEMPTS" in workflow_content
            or "max_attempts" in workflow_content.lower()
            or "max retries" in workflow_content.lower()
        )
        assert has_max_attempts, (
            "R67 P1-13: release-gates.yml 应含 MAX_ATTEMPTS 限制"
        )


# ════════════════════════════════════════════════════════════════
# F. 错误模式覆盖矩阵(确保 P1-13 audit 每条要求都被覆盖)
# ════════════════════════════════════════════════════════════════


class TestP1_13AuditCoverageMatrix:
    """R67 P1-13: 验证 audit 每条要求都被代码覆盖。"""

    def test_transient_error_set_includes_404_manifest_unknown(self):
        """audit 要求:404/manifest unknown 应为瞬态。"""
        from scripts.verify_rc_3x import _is_transient_error
        assert _is_transient_error("manifest unknown")
        assert _is_transient_error("404 Not Found")

    def test_transient_error_set_includes_429(self):
        """audit 要求:429 应为瞬态。"""
        from scripts.verify_rc_3x import _is_transient_error
        assert _is_transient_error("429 Too Many Requests")

    def test_transient_error_set_includes_5xx(self):
        """audit 要求:5xx 应为瞬态。"""
        from scripts.verify_rc_3x import _is_transient_error
        assert _is_transient_error("500 Internal Server Error")
        assert _is_transient_error("502 Bad Gateway")
        assert _is_transient_error("503 Service Unavailable")

    def test_fatal_error_set_includes_401(self):
        """audit 要求:401 应立即失败。"""
        from scripts.verify_rc_3x import _is_fatal_error, _is_transient_error
        assert _is_fatal_error("401 Unauthorized")
        assert not _is_transient_error("401 Unauthorized")

    def test_fatal_error_set_includes_403(self):
        """audit 要求:403 应立即失败。"""
        from scripts.verify_rc_3x import _is_fatal_error, _is_transient_error
        assert _is_fatal_error("403 Forbidden")
        assert not _is_transient_error("403 Forbidden")

    def test_fatal_error_set_includes_digest_mismatch(self):
        """audit 要求:digest mismatch 应立即失败。"""
        from scripts.verify_rc_3x import _is_fatal_error, _is_transient_error
        assert _is_fatal_error("digest mismatch")
        assert not _is_transient_error("digest mismatch")

    def test_fatal_error_set_includes_tls_errors(self):
        """audit 要求:TLS 错误应立即失败。"""
        from scripts.verify_rc_3x import _is_fatal_error, _is_transient_error
        assert _is_fatal_error("TLS certificate verification failed")
        assert not _is_transient_error("TLS certificate verification failed")

    def test_fatal_error_set_includes_signature_errors(self):
        """audit 要求:签名错误应立即失败。"""
        from scripts.verify_rc_3x import _is_fatal_error, _is_transient_error
        assert _is_fatal_error("signature mismatch")
        assert _is_fatal_error("signature verification failed")
        assert not _is_transient_error("signature mismatch")
