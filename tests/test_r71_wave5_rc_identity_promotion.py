"""R71 Wave 5: RC 身份核验与无重建晋级 — 测试套件。

R71 报告 P0-09/10/11 指出:
    P0-09: production-v* tag 仍触发 docker-build,导致 production 部署重新构建
           而非 promote 已验证的 RC 的 exact RepoDigest。
    P0-10: master push 进入 production-eligible 命名空间(ghcr.io/<repo>),
           应只进入 staging 命名空间(ghcr.io/<repo>-staging)。
    P0-11: workflow_dispatch 没有 RC 身份输入字段,运维可在不验证哪个 RC 被
           晋级的情况下触发 production promotion。

R71 Wave 5 整改(P0-09/10/11, Commit 5):
    1. .github/workflows/release-gates.yml:
       - docker-build job 增加 if: 排除 production-v* tag(P0-09)
       - docker-build 内部增加 fail-closed guard step(defense in depth,P0-09)
       - master/main push 改为 staging 命名空间 ghcr.io/<repo>-staging(P0-10)
       - workflow_dispatch 增加 6 个 RC 身份输入字段(P0-11)
       - 新增 verify-rc-identity job,核验 RC 身份与 evidence artifact 一致(P0-11)
       - production-promotion-gate 依赖 verify-rc-identity(P0-11)
       - release-summary 聚合 verify-rc-identity 结果(P0-11)
    2. scripts/verify_rc_identity.py:
       - 校验 RC 身份输入字段格式(rc_tag / run_id / source_sha / digest)
       - 校验 typed_confirmation == "PROMOTE-RC-TO-PRODUCTION"
       - 校验 4 类 evidence artifact 的 digest 一致
       - 交叉核验所有 artifact 的 source_sha / image_digest / candidate_manifest_digest
       - 输出 JSON 报告,退出码 0/1/2
    3. 测试覆盖 40+ 用例(无 Docker,Windows 兼容)

被测对象:
    - scripts/verify_rc_identity.py(主验证脚本)
    - .github/workflows/release-gates.yml(工作流门禁)

测试覆盖矩阵(45 个测试):
    A. 模块结构与常量 — 4 个
    B. 输入字段格式校验 — 8 个
    C. typed_confirmation 校验 — 3 个
    D. candidate-manifest digest 校验 — 3 个
    E. oci-file-manifest image_digest 校验 — 3 个
    F. release-attestation image_digest 校验 — 3 个
    G. SBOM 校验 — 3 个
    H. 交叉核验 — 4 个
    I. 缺失 artifact — 4 个
    J. 有效 RC evidence 端到端 — 3 个
    K. CLI 参数解析与退出码 — 4 个
    L. 输出 JSON 结构 — 3 个
    M. workflow_dispatch vs tag trigger 逻辑 — 2 个
    N. YAML 工作流门禁(P0-09/10) — 2 个

测试策略:
    - 用 tmp_path 创建合成 evidence artifact 文件
    - 用 monkeypatch 替换时间戳函数(确定性)
    - 验证 exit code、JSON 结构、fail-closed 行为
    - 严格遵守 R71 整改规范(无 TODO / pass / 占位符)
    - 测试在 Windows 无 Docker 环境下确定性运行
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_rc_identity.py"
RELEASE_GATES_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"

# 测试用常量(全部格式合法)
TEST_SOURCE_SHA = "abc123def4567890abcdef1234567890abcdef12"  # 40-char hex
TEST_IMAGE_DIGEST_HEX = "a" * 64  # 64-char hex
TEST_IMAGE_DIGEST = f"sha256:{TEST_IMAGE_DIGEST_HEX}"
TEST_CANDIDATE_MANIFEST_HEX = "b" * 64
TEST_CANDIDATE_MANIFEST_DIGEST = f"sha256:{TEST_CANDIDATE_MANIFEST_HEX}"
TEST_RC_TAG = "rc-v1.0.0"
TEST_RC_RUN_ID = "1234567890"
TEST_TYPED_CONFIRMATION = "PROMOTE-RC-TO-PRODUCTION"
TEST_IMAGE_NAME = "ghcr.io/test/tgjiema"


# ════════════════════════════════════════════════════════════════
# 辅助:动态加载模块
# ════════════════════════════════════════════════════════════════


def _load_module_from_path(module_name: str, file_path: Path):
    """从文件路径动态加载 Python 模块。"""
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    assert spec is not None, f"无法加载模块 spec: {file_path}"
    assert spec.loader is not None, f"模块 loader 为 None: {file_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vrc():
    """加载 verify_rc_identity 模块(模块级缓存)。"""
    return _load_module_from_path("scripts.verify_rc_identity_r71w5", SCRIPT_PATH)


# ════════════════════════════════════════════════════════════════
# 辅助:合成 evidence artifact 文件创建
# ════════════════════════════════════════════════════════════════


def _make_candidate_manifest(
    tmp_path: Path,
    *,
    source_sha: str = TEST_SOURCE_SHA,
    image_digest: str = TEST_IMAGE_DIGEST,
    image_name: str = TEST_IMAGE_NAME,
    rc_tag: str = TEST_RC_TAG,
    rc_run_id: str = TEST_RC_RUN_ID,
) -> Path:
    """创建合成 candidate-manifest.json,返回文件路径。

    文件内容确定性地包含 RC 身份字段。其 sha256 由调用方计算。

    注意:不包含 candidate_manifest_digest 字段,因为该字段是文件自身的
    sha256(自引用,无法在文件内表达)。--candidate-manifest-digest 参数
    在 verify_candidate_manifest() 中通过 sha256(file) 校验。
    """
    data = {
        "schema_version": "1.0",
        "rc_tag": rc_tag,
        "source_sha": source_sha,
        "image_digest": image_digest,
        "image_name": image_name,
        "rc_run_id": rc_run_id,
    }
    path = tmp_path / "candidate-manifest.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    return path


def _make_oci_file_manifest(
    tmp_path: Path,
    *,
    image_digest: str = TEST_IMAGE_DIGEST,
    image_name: str = TEST_IMAGE_NAME,
    source_sha: str = TEST_SOURCE_SHA,
) -> Path:
    """创建合成 oci-file-manifest.json,返回文件路径。"""
    data = {
        "schema_version": "1.0",
        "image_digest": image_digest,
        "image_name": image_name,
        "source_sha": source_sha,
        "files": [],
    }
    path = tmp_path / "oci-file-manifest.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    return path


def _make_release_attestation(
    tmp_path: Path,
    *,
    image_digest: str = TEST_IMAGE_DIGEST,
    image_name: str = TEST_IMAGE_NAME,
    source_sha: str = TEST_SOURCE_SHA,
) -> Path:
    """创建合成 release-attestation.json,返回文件路径。"""
    data = {
        "schema_version": "1.0",
        "image_digest": image_digest,
        "image_name": image_name,
        "source_sha": source_sha,
        "signature": {"verified": True, "type": "cosign"},
    }
    path = tmp_path / "release-attestation.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    return path


def _make_sbom(
    tmp_path: Path,
    *,
    image_digest: str = TEST_IMAGE_DIGEST,
    image_name: str = TEST_IMAGE_NAME,
) -> Path:
    """创建合成 sbom.spdx.json,返回文件路径。"""
    data = {
        "spdxVersion": "SPDX-2.3",
        "schema_version": "1.0",
        "image_digest": image_digest,
        "image_name": image_name,
        "packages": [],
    }
    path = tmp_path / "sbom.spdx.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    return path


def _make_full_evidence_dir(
    tmp_path: Path,
    *,
    source_sha: str = TEST_SOURCE_SHA,
    image_digest: str = TEST_IMAGE_DIGEST,
    image_name: str = TEST_IMAGE_NAME,
    rc_tag: str = TEST_RC_TAG,
    rc_run_id: str = TEST_RC_RUN_ID,
) -> tuple[Path, str]:
    """创建完整的 RC evidence 目录(4 类 artifact),返回 (dir, candidate_manifest_sha256)。

    candidate-manifest.json 的 sha256 会被计算并返回,供调用方作为
    --candidate-manifest-digest 参数。
    """
    evidence_dir = tmp_path / "rc-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    _make_candidate_manifest(
        evidence_dir,
        source_sha=source_sha,
        image_digest=image_digest,
        image_name=image_name,
        rc_tag=rc_tag,
        rc_run_id=rc_run_id,
    )
    _make_oci_file_manifest(
        evidence_dir,
        image_digest=image_digest,
        image_name=image_name,
        source_sha=source_sha,
    )
    _make_release_attestation(
        evidence_dir,
        image_digest=image_digest,
        image_name=image_name,
        source_sha=source_sha,
    )
    _make_sbom(evidence_dir, image_digest=image_digest, image_name=image_name)
    # 计算 candidate-manifest.json 的实际 sha256
    cm_path = evidence_dir / "candidate-manifest.json"
    with open(cm_path, "rb") as f:
        cm_sha256 = hashlib.sha256(f.read()).hexdigest()
    return evidence_dir, cm_sha256


def _make_inputs(
    vrc,
    *,
    candidate_manifest_digest: str | None = None,
    image_digest: str = TEST_IMAGE_DIGEST,
    rc_tag: str = TEST_RC_TAG,
    source_sha: str = TEST_SOURCE_SHA,
    rc_run_id: str = TEST_RC_RUN_ID,
    typed_confirmation: str = TEST_TYPED_CONFIRMATION,
):
    """构造 RCIdentityInputs,默认所有字段合法。

    candidate_manifest_digest 默认为 None,调用方应传入实际计算的 sha256。
    """
    if candidate_manifest_digest is None:
        candidate_manifest_digest = f"sha256:{TEST_CANDIDATE_MANIFEST_HEX}"
    return vrc.RCIdentityInputs(
        rc_tag=rc_tag,
        rc_run_id=rc_run_id,
        source_sha=source_sha,
        candidate_manifest_digest=candidate_manifest_digest,
        image_digest=image_digest,
        typed_confirmation=typed_confirmation,
    )


# ════════════════════════════════════════════════════════════════
# A. 模块结构与常量
# ════════════════════════════════════════════════════════════════


class TestModuleStructure:
    """A. 模块结构与常量测试。"""

    def test_module_loads_without_error(self, vrc):
        """模块应能无错误加载。"""
        assert vrc is not None
        assert hasattr(vrc, "verify_rc_identity")
        assert hasattr(vrc, "RCIdentityInputs")
        assert hasattr(vrc, "VerificationResult")

    def test_constants_defined(self, vrc):
        """关键常量应已定义。"""
        assert vrc.SCHEMA_VERSION == "1.0"
        assert vrc.TOOL_VERSION == "R71-WAVE5-P0-09/10/11"
        assert vrc.EXPECTED_TYPED_CONFIRMATION == "PROMOTE-RC-TO-PRODUCTION"
        assert vrc.EXIT_SUCCESS == 0
        assert vrc.EXIT_VERIFICATION_FAILURE == 1
        assert vrc.EXIT_CLI_ERROR == 2

    def test_required_evidence_files(self, vrc):
        """必需 evidence 文件列表应包含 4 个文件。"""
        assert set(vrc.REQUIRED_EVIDENCE_FILES) == {
            "candidate-manifest.json",
            "oci-file-manifest.json",
            "release-attestation.json",
            "sbom.spdx.json",
        }

    def test_verification_result_to_dict(self, vrc):
        """VerificationResult.to_dict 应返回完整 JSON 可序列化 dict。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        d = result.to_dict()
        assert d["schema_version"] == "1.0"
        assert d["tool_version"] == "R71-WAVE5-P0-09/10/11"
        assert d["overall_passed"] is True
        assert d["errors"] == []
        assert d["artifacts_verified"] == []
        # 确保 JSON 可序列化
        json.dumps(d)


# ════════════════════════════════════════════════════════════════
# B. 输入字段格式校验
# ════════════════════════════════════════════════════════════════


class TestInputValidation:
    """B. 输入字段格式校验测试。"""

    def test_valid_inputs_pass(self, vrc):
        """合法输入应通过校验。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        inputs = _make_inputs(vrc)
        assert vrc.validate_inputs(inputs, result) is True
        assert result.errors == []
        assert result.rc_tag == TEST_RC_TAG
        assert result.rc_run_id == TEST_RC_RUN_ID
        assert result.source_sha == TEST_SOURCE_SHA.lower()

    def test_empty_rc_tag_fails(self, vrc):
        """rc_tag 为空应失败。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        inputs = _make_inputs(vrc, rc_tag="")
        assert vrc.validate_inputs(inputs, result) is False
        assert any("rc_tag" in e for e in result.errors)

    def test_invalid_rc_tag_fails(self, vrc):
        """rc_tag 不匹配 rc-v* 应失败。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        inputs = _make_inputs(vrc, rc_tag="v1.0.0")  # 缺 rc- 前缀
        assert vrc.validate_inputs(inputs, result) is False
        assert any("rc_tag" in e for e in result.errors)

    def test_empty_rc_run_id_fails(self, vrc):
        """rc_run_id 为空应失败。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        inputs = _make_inputs(vrc, rc_run_id="")
        assert vrc.validate_inputs(inputs, result) is False
        assert any("rc_run_id" in e for e in result.errors)

    def test_non_numeric_rc_run_id_fails(self, vrc):
        """rc_run_id 非数字应失败。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        inputs = _make_inputs(vrc, rc_run_id="abc123")
        assert vrc.validate_inputs(inputs, result) is False
        assert any("rc_run_id" in e for e in result.errors)

    def test_invalid_source_sha_fails(self, vrc):
        """source_sha 非 40-char hex 应失败。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        inputs = _make_inputs(vrc, source_sha="short")
        assert vrc.validate_inputs(inputs, result) is False
        assert any("source_sha" in e for e in result.errors)

    def test_invalid_candidate_manifest_digest_fails(self, vrc):
        """candidate_manifest_digest 格式不合法应失败。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        inputs = _make_inputs(vrc, candidate_manifest_digest="sha256:short")
        assert vrc.validate_inputs(inputs, result) is False
        assert any("candidate_manifest_digest" in e for e in result.errors)

    def test_invalid_image_digest_fails(self, vrc):
        """image_digest 格式不合法应失败。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        inputs = _make_inputs(vrc, image_digest="not-a-digest")
        assert vrc.validate_inputs(inputs, result) is False
        assert any("image_digest" in e for e in result.errors)


# ════════════════════════════════════════════════════════════════
# C. typed_confirmation 校验
# ════════════════════════════════════════════════════════════════


class TestTypedConfirmation:
    """C. typed_confirmation 校验测试。"""

    def test_valid_confirmation_passes(self, vrc):
        """正确的 typed_confirmation 应通过。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        assert vrc.validate_typed_confirmation("PROMOTE-RC-TO-PRODUCTION", result) is True
        assert result.typed_confirmation_valid is True
        assert result.errors == []

    def test_wrong_confirmation_fails(self, vrc):
        """错误的 typed_confirmation 应失败。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        assert vrc.validate_typed_confirmation("yes", result) is False
        assert result.typed_confirmation_valid is False
        assert len(result.errors) == 1

    def test_empty_confirmation_fails(self, vrc):
        """空的 typed_confirmation 应失败。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        assert vrc.validate_typed_confirmation("", result) is False
        assert result.typed_confirmation_valid is False


# ════════════════════════════════════════════════════════════════
# D. candidate-manifest digest 校验
# ════════════════════════════════════════════════════════════════


class TestCandidateManifestDigest:
    """D. candidate-manifest.json sha256 校验测试。"""

    def test_matching_digest_passes(self, vrc, tmp_path):
        """candidate-manifest.json sha256 匹配应通过。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        cm_digest = f"sha256:{cm_sha256}"
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_candidate_manifest(evidence_dir, cm_digest, result)
        assert data is not None
        assert "candidate-manifest" in result.artifacts_verified
        assert result.errors == []

    def test_mismatched_digest_fails(self, vrc, tmp_path):
        """candidate-manifest.json sha256 不匹配应失败。"""
        evidence_dir, _ = _make_full_evidence_dir(tmp_path)
        wrong_digest = f"sha256:{'c' * 64}"  # 不同的 digest
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_candidate_manifest(evidence_dir, wrong_digest, result)
        assert data is None
        assert any("sha256 不匹配" in e for e in result.errors)

    def test_missing_candidate_manifest_fails(self, vrc, tmp_path):
        """candidate-manifest.json 缺失应失败。"""
        evidence_dir = tmp_path / "empty-evidence"
        evidence_dir.mkdir()
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_candidate_manifest(evidence_dir, TEST_CANDIDATE_MANIFEST_DIGEST, result)
        assert data is None
        assert any("不存在" in e for e in result.errors)


# ════════════════════════════════════════════════════════════════
# E. oci-file-manifest image_digest 校验
# ════════════════════════════════════════════════════════════════


class TestOciFileManifestDigest:
    """E. oci-file-manifest.json image_digest 校验测试。"""

    def test_matching_digest_passes(self, vrc, tmp_path):
        """oci-file-manifest.json image_digest 匹配应通过。"""
        evidence_dir, _ = _make_full_evidence_dir(tmp_path)
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_oci_file_manifest(evidence_dir, TEST_IMAGE_DIGEST, result)
        assert data is not None
        assert "oci-file-manifest" in result.artifacts_verified

    def test_mismatched_digest_fails(self, vrc, tmp_path):
        """oci-file-manifest.json image_digest 不匹配应失败。"""
        evidence_dir, _ = _make_full_evidence_dir(tmp_path)
        wrong_digest = f"sha256:{'d' * 64}"
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_oci_file_manifest(evidence_dir, wrong_digest, result)
        assert data is None
        assert any("image_digest 不匹配" in e for e in result.errors)

    def test_missing_oci_file_manifest_fails(self, vrc, tmp_path):
        """oci-file-manifest.json 缺失应失败。"""
        evidence_dir = tmp_path / "empty-evidence"
        evidence_dir.mkdir()
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_oci_file_manifest(evidence_dir, TEST_IMAGE_DIGEST, result)
        assert data is None
        assert any("不存在" in e for e in result.errors)


# ════════════════════════════════════════════════════════════════
# F. release-attestation image_digest 校验
# ════════════════════════════════════════════════════════════════


class TestReleaseAttestationDigest:
    """F. release-attestation.json image_digest 校验测试。"""

    def test_matching_digest_passes(self, vrc, tmp_path):
        """release-attestation.json image_digest 匹配应通过。"""
        evidence_dir, _ = _make_full_evidence_dir(tmp_path)
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_release_attestation(evidence_dir, TEST_IMAGE_DIGEST, result)
        assert data is not None
        assert "release-attestation" in result.artifacts_verified

    def test_mismatched_digest_fails(self, vrc, tmp_path):
        """release-attestation.json image_digest 不匹配应失败。"""
        evidence_dir, _ = _make_full_evidence_dir(tmp_path)
        wrong_digest = f"sha256:{'e' * 64}"
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_release_attestation(evidence_dir, wrong_digest, result)
        assert data is None
        assert any("image_digest 不匹配" in e for e in result.errors)

    def test_missing_attestation_fails(self, vrc, tmp_path):
        """release-attestation.json 缺失应失败。"""
        evidence_dir = tmp_path / "empty-evidence"
        evidence_dir.mkdir()
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_release_attestation(evidence_dir, TEST_IMAGE_DIGEST, result)
        assert data is None
        assert any("不存在" in e for e in result.errors)


# ════════════════════════════════════════════════════════════════
# G. SBOM 校验
# ════════════════════════════════════════════════════════════════


class TestSbomVerification:
    """G. sbom.spdx.json 校验测试。"""

    def test_matching_digest_passes(self, vrc, tmp_path):
        """sbom.spdx.json image_digest 匹配应通过。"""
        evidence_dir, _ = _make_full_evidence_dir(tmp_path)
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_sbom(evidence_dir, TEST_IMAGE_DIGEST, result)
        assert data is not None
        assert "sbom" in result.artifacts_verified

    def test_mismatched_digest_fails(self, vrc, tmp_path):
        """sbom.spdx.json image_digest 不匹配应失败。"""
        evidence_dir = tmp_path / "sbom-evidence"
        evidence_dir.mkdir()
        # 创建一个 image_digest 不匹配的 SBOM
        _make_sbom(evidence_dir, image_digest=f"sha256:{'f' * 64}")
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_sbom(evidence_dir, TEST_IMAGE_DIGEST, result)
        assert data is None
        assert any("image_digest 不匹配" in e for e in result.errors)

    def test_missing_sbom_fails(self, vrc, tmp_path):
        """sbom.spdx.json 缺失应失败。"""
        evidence_dir = tmp_path / "empty-evidence"
        evidence_dir.mkdir()
        result = vrc.VerificationResult()
        result.overall_passed = True
        data = vrc.verify_sbom(evidence_dir, TEST_IMAGE_DIGEST, result)
        assert data is None
        assert any("不存在" in e for e in result.errors)


# ════════════════════════════════════════════════════════════════
# H. 交叉核验
# ════════════════════════════════════════════════════════════════


class TestCrossVerification:
    """H. artifact 交叉核验测试。"""

    def test_all_artifacts_consistent_passes(self, vrc, tmp_path):
        """所有 artifact digest 一致应通过交叉核验。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        result = vrc.VerificationResult()
        result.overall_passed = True
        cm = vrc._load_json(evidence_dir / "candidate-manifest.json")
        oci = vrc._load_json(evidence_dir / "oci-file-manifest.json")
        att = vrc._load_json(evidence_dir / "release-attestation.json")
        sbom = vrc._load_json(evidence_dir / "sbom.spdx.json")
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        ok = vrc.cross_verify_artifacts(cm, oci, att, sbom, inputs, result)
        assert ok is True
        assert result.cross_verification_passed is True
        assert result.errors == []

    def test_mismatched_source_sha_fails(self, vrc, tmp_path):
        """artifact 中 source_sha 不一致应失败。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        result = vrc.VerificationResult()
        result.overall_passed = True
        cm = vrc._load_json(evidence_dir / "candidate-manifest.json")
        oci = vrc._load_json(evidence_dir / "oci-file-manifest.json")
        # 篡改 oci source_sha
        oci["source_sha"] = "0" * 40
        att = vrc._load_json(evidence_dir / "release-attestation.json")
        sbom = vrc._load_json(evidence_dir / "sbom.spdx.json")
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        ok = vrc.cross_verify_artifacts(cm, oci, att, sbom, inputs, result)
        assert ok is False
        assert any("source_sha 不匹配" in e for e in result.errors)

    def test_mismatched_image_digest_fails(self, vrc, tmp_path):
        """artifact 中 image_digest 不一致应失败。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        result = vrc.VerificationResult()
        result.overall_passed = True
        cm = vrc._load_json(evidence_dir / "candidate-manifest.json")
        oci = vrc._load_json(evidence_dir / "oci-file-manifest.json")
        att = vrc._load_json(evidence_dir / "release-attestation.json")
        # 篡改 attestation image_digest
        att["image_digest"] = f"sha256:{'1' * 64}"
        sbom = vrc._load_json(evidence_dir / "sbom.spdx.json")
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        ok = vrc.cross_verify_artifacts(cm, oci, att, sbom, inputs, result)
        assert ok is False
        assert any("image_digest 不匹配" in e for e in result.errors)

    def test_none_artifact_fails_cross_verification(self, vrc, tmp_path):
        """任一 artifact 为 None(之前校验失败)应交叉核验失败。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        result = vrc.VerificationResult()
        result.overall_passed = True
        cm = vrc._load_json(evidence_dir / "candidate-manifest.json")
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        ok = vrc.cross_verify_artifacts(cm, None, None, None, inputs, result)
        assert ok is False
        assert result.cross_verification_passed is False


# ════════════════════════════════════════════════════════════════
# I. 缺失 artifact
# ════════════════════════════════════════════════════════════════


class TestMissingArtifacts:
    """I. 缺失 artifact 应 fail-closed。"""

    def test_missing_candidate_manifest_fails(self, vrc, tmp_path):
        """缺失 candidate-manifest.json 应整体失败。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        # 删除 candidate-manifest.json
        (evidence_dir / "candidate-manifest.json").unlink()
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.overall_passed is False
        assert any("candidate-manifest" in e for e in result.errors)

    def test_missing_oci_file_manifest_fails(self, vrc, tmp_path):
        """缺失 oci-file-manifest.json 应整体失败。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        (evidence_dir / "oci-file-manifest.json").unlink()
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.overall_passed is False

    def test_missing_release_attestation_fails(self, vrc, tmp_path):
        """缺失 release-attestation.json 应整体失败。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        (evidence_dir / "release-attestation.json").unlink()
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.overall_passed is False

    def test_missing_sbom_fails(self, vrc, tmp_path):
        """缺失 sbom.spdx.json 应整体失败。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        (evidence_dir / "sbom.spdx.json").unlink()
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.overall_passed is False


# ════════════════════════════════════════════════════════════════
# J. 有效 RC evidence 端到端
# ════════════════════════════════════════════════════════════════


class TestValidRCEvidence:
    """J. 有效 RC evidence 端到端测试。"""

    def test_full_valid_evidence_passes(self, vrc, tmp_path):
        """完整有效的 RC evidence 应通过所有核验。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.overall_passed is True
        assert result.errors == []
        assert set(result.artifacts_verified) == {
            "candidate-manifest",
            "oci-file-manifest",
            "release-attestation",
            "sbom",
        }
        assert result.cross_verification_passed is True
        assert result.typed_confirmation_valid is True
        assert result.rc_tag == TEST_RC_TAG
        assert result.image_digest == TEST_IMAGE_DIGEST

    def test_valid_evidence_without_typed_confirmation(self, vrc, tmp_path):
        """production-v* tag 触发(不需 typed_confirmation)应通过。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        inputs = _make_inputs(
            vrc,
            candidate_manifest_digest=f"sha256:{cm_sha256}",
            typed_confirmation="",  # tag 触发不需要
        )
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=False)
        assert result.overall_passed is True
        assert result.typed_confirmation_valid is True  # tag 触发仍标记为 valid

    def test_image_repo_digest_extracted(self, vrc, tmp_path):
        """image_repo_digest 应从 artifact 提取(image_name@digest)。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.overall_passed is True
        assert result.image_repo_digest == f"{TEST_IMAGE_NAME}@{TEST_IMAGE_DIGEST}"
        assert result.image_repo_digest.startswith("ghcr.io/")


# ════════════════════════════════════════════════════════════════
# K. CLI 参数解析与退出码
# ════════════════════════════════════════════════════════════════


class TestCLI:
    """K. CLI 参数解析与退出码测试。"""

    def test_cli_valid_args_returns_0(self, vrc, tmp_path, capsys):
        """合法 CLI 参数 + 有效 evidence 应返回退出码 0。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        output_path = tmp_path / "report.json"
        argv = [
            "--rc-tag", TEST_RC_TAG,
            "--rc-run-id", TEST_RC_RUN_ID,
            "--source-sha", TEST_SOURCE_SHA,
            "--candidate-manifest-digest", f"sha256:{cm_sha256}",
            "--image-digest", TEST_IMAGE_DIGEST,
            "--rc-evidence-dir", str(evidence_dir),
            "--typed-confirmation", TEST_TYPED_CONFIRMATION,
            "--output", str(output_path),
        ]
        # 抑制 loguru 输出
        vrc.logger.remove()
        exit_code = vrc.main(argv)
        assert exit_code == 0
        # 报告文件应存在
        assert output_path.exists()
        with open(output_path, "r", encoding="utf-8") as f:
            report = json.load(f)
        assert report["overall_passed"] is True

    def test_cli_verification_failure_returns_1(self, vrc, tmp_path):
        """核验失败应返回退出码 1。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        # 使用错误的 candidate-manifest-digest
        argv = [
            "--rc-tag", TEST_RC_TAG,
            "--rc-run-id", TEST_RC_RUN_ID,
            "--source-sha", TEST_SOURCE_SHA,
            "--candidate-manifest-digest", f"sha256:{'0' * 64}",  # 错误 digest
            "--image-digest", TEST_IMAGE_DIGEST,
            "--rc-evidence-dir", str(evidence_dir),
            "--typed-confirmation", TEST_TYPED_CONFIRMATION,
        ]
        vrc.logger.remove()
        exit_code = vrc.main(argv)
        assert exit_code == 1

    def test_cli_missing_required_arg_returns_2(self, vrc, tmp_path):
        """缺少必需参数应返回退出码 2。"""
        argv = [
            "--rc-tag", TEST_RC_TAG,
            # 缺少 --rc-run-id 等其他必需参数
        ]
        vrc.logger.remove()
        exit_code = vrc.main(argv)
        assert exit_code == 2

    def test_cli_no_require_typed_confirmation_flag(self, vrc, tmp_path):
        """--no-require-typed-confirmation 应跳过 typed_confirmation 校验。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        argv = [
            "--rc-tag", TEST_RC_TAG,
            "--rc-run-id", TEST_RC_RUN_ID,
            "--source-sha", TEST_SOURCE_SHA,
            "--candidate-manifest-digest", f"sha256:{cm_sha256}",
            "--image-digest", TEST_IMAGE_DIGEST,
            "--rc-evidence-dir", str(evidence_dir),
            "--typed-confirmation", "wrong-confirmation",
            "--no-require-typed-confirmation",
        ]
        vrc.logger.remove()
        exit_code = vrc.main(argv)
        assert exit_code == 0  # 跳过 typed_confirmation,核验通过


# ════════════════════════════════════════════════════════════════
# L. 输出 JSON 结构
# ════════════════════════════════════════════════════════════════


class TestOutputJsonStructure:
    """L. 输出 JSON 结构测试。"""

    def test_output_contains_all_required_fields(self, vrc, tmp_path):
        """输出 JSON 应包含所有必需字段。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        d = result.to_dict()
        required_fields = {
            "schema_version", "tool_version", "verified_at",
            "rc_tag", "rc_run_id", "source_sha",
            "candidate_manifest_digest", "image_digest", "image_repo_digest",
            "artifacts_verified", "cross_verification_passed",
            "typed_confirmation_valid", "overall_passed", "errors",
        }
        assert required_fields.issubset(set(d.keys()))

    def test_output_verified_at_is_iso8601(self, vrc, tmp_path):
        """verified_at 应为 ISO 8601 格式。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.verified_at
        # ISO 8601 应包含 'T' 分隔符
        assert "T" in result.verified_at

    def test_output_artifacts_verified_list_complete(self, vrc, tmp_path):
        """artifacts_verified 应包含全部 4 个 artifact。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert len(result.artifacts_verified) == 4
        assert set(result.artifacts_verified) == {
            "candidate-manifest", "oci-file-manifest",
            "release-attestation", "sbom",
        }


# ════════════════════════════════════════════════════════════════
# M. workflow_dispatch vs tag trigger 逻辑
# ════════════════════════════════════════════════════════════════


class TestTriggerLogic:
    """M. workflow_dispatch vs production-v* tag 触发逻辑测试。"""

    def test_workflow_dispatch_requires_typed_confirmation(self, vrc, tmp_path):
        """workflow_dispatch 触发需要 typed_confirmation。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        inputs = _make_inputs(
            vrc,
            candidate_manifest_digest=f"sha256:{cm_sha256}",
            typed_confirmation="wrong",
        )
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.overall_passed is False
        assert result.typed_confirmation_valid is False
        assert any("typed_confirmation" in e for e in result.errors)

    def test_tag_trigger_skips_typed_confirmation(self, vrc, tmp_path):
        """production-v* tag 触发跳过 typed_confirmation。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        inputs = _make_inputs(
            vrc,
            candidate_manifest_digest=f"sha256:{cm_sha256}",
            typed_confirmation="",  # 空,但 tag 触发不需要
        )
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=False)
        assert result.overall_passed is True
        assert result.typed_confirmation_valid is True


# ════════════════════════════════════════════════════════════════
# N. YAML 工作流门禁(P0-09/10)
# ════════════════════════════════════════════════════════════════


class TestYamlWorkflowGates:
    """N. YAML 工作流门禁测试(P0-09: production-v* 不触发 docker-build;
    P0-10: master push 进入 staging 命名空间)。"""

    def test_docker_build_if_excludes_production_v_tags(self):
        """P0-09: docker-build job 的 if: 应排除 production-v* tag。"""
        with open(RELEASE_GATES_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        # docker-build job 应有 if: 排除 production-v*
        assert "startsWith(github.ref, 'refs/tags/production-v')" in content
        # 应包含 P0-09 标记注释
        assert "R71 P0-09" in content
        # 应有 fail-closed guard step
        assert "refs/tags/production-v*" in content
        assert "FAIL: docker-build must not run on production-v* tags" in content

    def test_master_push_goes_to_staging_namespace(self):
        """P0-10: master push 应进入 ghcr.io/<repo>-staging 命名空间。"""
        with open(RELEASE_GATES_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        # 应包含 staging 命名空间
        assert "ghcr.io/${REPO}-staging" in content
        # 应包含 P0-10 标记注释
        assert "R71 P0-10" in content
        # master 分支应使用 staging 命名空间 + 30 天 retention
        assert "RETENTION_DAYS=\"30\"" in content
        # 应明确说明 master 不进入生产命名空间
        assert "NOT eligible for production promotion" in content

    def test_workflow_dispatch_has_rc_identity_inputs(self):
        """P0-11: workflow_dispatch 应有 6 个 RC 身份输入字段。"""
        with open(RELEASE_GATES_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        # 应包含所有 6 个输入字段
        for field in (
            "rc_tag",
            "rc_run_id",
            "source_sha",
            "candidate_manifest_digest",
            "image_digest",
            "typed_confirmation",
        ):
            assert field in content, f"workflow_dispatch 缺少输入字段: {field}"
        # 应包含 P0-11 标记注释
        assert "R71 P0-11" in content

    def test_verify_rc_identity_job_exists(self):
        """P0-11: verify-rc-identity job 应存在。"""
        with open(RELEASE_GATES_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        assert "verify-rc-identity:" in content
        # 应有 if: 仅在 workflow_dispatch 或 production-v* tag 触发
        assert (
            "github.event_name == 'workflow_dispatch' || startsWith(github.ref, "
            "'refs/tags/production-v')" in content
        )

    def test_production_promotion_gate_needs_verify_rc_identity(self):
        """P0-11: production-promotion-gate 应依赖 verify-rc-identity。"""
        with open(RELEASE_GATES_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        # needs 列表应包含 verify-rc-identity
        assert "verify-rc-identity" in content
        # production-promotion-gate 的 needs 行应包含 verify-rc-identity
        # (在 needs: [production-evidence, ... verify-rc-identity] 中)
        assert "needs: [production-evidence, crdb-ru-72h-attribution-gate, verify-only-3x, verify-rc-identity]" in content

    def test_release_summary_includes_verify_rc_identity(self):
        """P0-11: release-summary 应聚合 verify-rc-identity 结果。"""
        with open(RELEASE_GATES_PATH, "r", encoding="utf-8") as f:
            content = f.read()
        # release-summary 应有 VERIFY_RC_IDENTITY env 变量
        assert "VERIFY_RC_IDENTITY: ${{ needs.verify-rc-identity.result }}" in content
        # 应有 rc_verified 输出引用
        assert "needs.verify-rc-identity.outputs.rc_verified" in content
        # bash 循环应包含 verify-rc-identity
        assert "verify-rc-identity=${VERIFY_RC_IDENTITY}" in content


# ════════════════════════════════════════════════════════════════
# P. 辅助函数测试
# ════════════════════════════════════════════════════════════════


class TestHelperFunctions:
    """P. 辅助函数测试。"""

    def test_strip_sha_prefix(self, vrc):
        """_strip_sha_prefix 应剥离 sha256: 前缀。"""
        assert vrc._strip_sha_prefix("sha256:abc123") == "abc123"
        assert vrc._strip_sha_prefix("abc123") == "abc123"
        assert vrc._strip_sha_prefix("") == ""

    def test_normalize_digest(self, vrc):
        """_normalize_digest 应规范化为 sha256:<64-hex-lower>。"""
        hex64 = "A" * 64  # 大写
        assert vrc._normalize_digest(f"sha256:{hex64}") == f"sha256:{'a' * 64}"
        # 无前缀
        assert vrc._normalize_digest(hex64) == f"sha256:{'a' * 64}"
        # 格式不合法返回原值
        assert vrc._normalize_digest("invalid") == "invalid"
        # 空值
        assert vrc._normalize_digest("") == ""

    def test_get_field_case_insensitive(self, vrc):
        """_get_field 应支持大小写不敏感匹配。"""
        data = {"ImageDigest": "sha256:abc"}
        assert vrc._get_field(data, "image_digest", file_label="test") == "sha256:abc"
        assert vrc._get_field(data, "IMAGE_DIGEST", file_label="test") == "sha256:abc"

    def test_get_field_missing_returns_empty(self, vrc):
        """_get_field 缺失字段应返回空字符串。"""
        data = {"other": "value"}
        assert vrc._get_field(data, "image_digest", file_label="test") == ""

    def test_sha256_file(self, vrc, tmp_path):
        """_sha256_file 应正确计算文件 sha256。"""
        path = tmp_path / "test.txt"
        content = b"hello world"
        path.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert vrc._sha256_file(path) == expected

    def test_compute_image_repo_digest_extracts_from_image_name(self, vrc):
        """compute_image_repo_digest 应从 image_name 提取 repo digest。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        cm = {"image_name": "ghcr.io/owner/repo:tag"}
        digest = TEST_IMAGE_DIGEST
        repo_digest = vrc.compute_image_repo_digest(cm, None, None, digest, result)
        assert repo_digest == f"ghcr.io/owner/repo@{TEST_IMAGE_DIGEST}"
        assert result.image_repo_digest == repo_digest

    def test_compute_image_repo_digest_strips_at_digest_suffix(self, vrc):
        """compute_image_repo_digest 应剥离 @digest 后缀。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        cm = {"image_name": f"ghcr.io/owner/repo@sha256:{'x' * 64}"}
        digest = TEST_IMAGE_DIGEST
        repo_digest = vrc.compute_image_repo_digest(cm, None, None, digest, result)
        assert repo_digest == f"ghcr.io/owner/repo@{TEST_IMAGE_DIGEST}"

    def test_compute_image_repo_digest_fallback_to_digest_only(self, vrc):
        """无 image_name 时应回退到仅 digest。"""
        result = vrc.VerificationResult()
        result.overall_passed = True
        repo_digest = vrc.compute_image_repo_digest(None, None, None, TEST_IMAGE_DIGEST, result)
        assert repo_digest == TEST_IMAGE_DIGEST


# ════════════════════════════════════════════════════════════════
# Q. 端到端失败场景
# ════════════════════════════════════════════════════════════════


class TestEndToEndFailureScenarios:
    """Q. 端到端失败场景测试。"""

    def test_evidence_dir_not_exists_fails(self, vrc, tmp_path):
        """evidence 目录不存在应失败。"""
        inputs = _make_inputs(vrc)
        result = vrc.verify_rc_identity(
            inputs, tmp_path / "nonexistent", require_typed_confirmation=True
        )
        assert result.overall_passed is False
        assert any("不存在" in e for e in result.errors)

    def test_evidence_path_is_file_not_dir_fails(self, vrc, tmp_path):
        """evidence 路径是文件而非目录应失败。"""
        file_path = tmp_path / "not-a-dir"
        file_path.write_text("content")
        inputs = _make_inputs(vrc)
        result = vrc.verify_rc_identity(inputs, file_path, require_typed_confirmation=True)
        assert result.overall_passed is False
        assert any("不是目录" in e for e in result.errors)

    def test_invalid_inputs_short_circuits(self, vrc, tmp_path):
        """输入格式错误应短路,不继续校验 artifact。"""
        evidence_dir, _ = _make_full_evidence_dir(tmp_path)
        inputs = _make_inputs(vrc, rc_tag="invalid-tag")  # rc_tag 格式错误
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.overall_passed is False
        # artifacts_verified 应为空(短路)
        assert result.artifacts_verified == []

    def test_wrong_typed_confirmation_short_circuits(self, vrc, tmp_path):
        """typed_confirmation 错误应短路,不继续校验 artifact。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        inputs = _make_inputs(
            vrc,
            candidate_manifest_digest=f"sha256:{cm_sha256}",
            typed_confirmation="wrong",
        )
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.overall_passed is False
        assert result.artifacts_verified == []
        assert result.typed_confirmation_valid is False

    def test_release_attestation_subject_digest_supported(self, vrc, tmp_path):
        """release-attestation.json 支持 statement.subject[0].digest.sha256 格式。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        # 覆写 release-attestation.json 使用 statement.subject 格式
        att_path = evidence_dir / "release-attestation.json"
        att_data = {
            "statement": {
                "subject": [
                    {
                        "name": TEST_IMAGE_NAME,
                        "digest": {"sha256": TEST_IMAGE_DIGEST_HEX},
                    }
                ]
            }
        }
        with open(att_path, "w", encoding="utf-8") as f:
            json.dump(att_data, f, indent=2)
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.overall_passed is True
        assert "release-attestation" in result.artifacts_verified

    def test_json_decode_error_fails(self, vrc, tmp_path):
        """artifact JSON 解析失败应 fail-closed。"""
        evidence_dir, cm_sha256 = _make_full_evidence_dir(tmp_path)
        # 覆写 candidate-manifest.json 为非法 JSON
        cm_path = evidence_dir / "candidate-manifest.json"
        cm_path.write_text("{ invalid json", encoding="utf-8")
        inputs = _make_inputs(vrc, candidate_manifest_digest=f"sha256:{cm_sha256}")
        result = vrc.verify_rc_identity(inputs, evidence_dir, require_typed_confirmation=True)
        assert result.overall_passed is False
        assert any("JSON 解析失败" in e for e in result.errors)
