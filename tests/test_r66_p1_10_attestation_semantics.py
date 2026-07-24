"""R66 P1-10: 供应链 attestation statement 语义验证测试。

审计背景(R66 终审报告 P1-10):
    除 cosign verify 成功外,还必须断言 statement subject digest、source
    repository、source commit/tree、workflow ref、builder identity、issuer、
    Rekor inclusion、certificate validity、SBOM/provenance predicate type 与
    release manifest 完全一致。

测试覆盖矩阵(共 ~35 个用例):
    A. CLI / 退出码(6 个)
        1. 脚本存在
        2. --help 退出码 0
        3. 缺少必选参数 → 退出码 2
        4. statement 文件不存在 → 退出码 2
        5. statement 非法 JSON → 退出码 2
        6. 全部合法 → 退出码 0
    B. 断言 a-b:statement type / predicate type(4 个)
        7. 合法 _type v0.1 通过
        8. 合法 _type v1 通过
        9. 非法 _type → 失败
        10. 非法 predicateType → 失败
    C. 断言 c-d:subject digest / name(6 个)
        11. subject digest 与 image_digest 一致 → 通过
        12. subject digest 带 "sha256:" 前缀 → 仍通过(自动剥离)
        13. image_digest 带 "sha256:" 前缀 → 仍通过(自动剥离)
        14. subject digest 不匹配 → 失败
        15. subject name 不匹配 image_ref → 失败
        16. subject name 与 image_ref @digest 形式 → 通过
    D. 断言 e:SLSA provenance predicate(8 个)
        17. predicate 缺失(非 strict)→ warning,总通过
        18. predicate 缺失(strict)→ 失败
        19. builder.id 为空 → 失败
        20. buildType 为空 → 失败
        21. configSource.uri 不含 github.com/<owner>/<repo> → 失败
        22. configSource.digest.sha1 不匹配 → 失败
        23. configSource.digest.sha1 与 source_commit_sha 别名 → 通过
        24. materials 缺 source_tree_sha → 失败
        25. materials 含 source_tree_sha → 通过
        26. materials 缺 migration_manifest_digest → 失败
        27. migration_manifest_digest 未在 manifest 提供 → 跳过(通过)
    E. 断言 f:Rekor tlogEntries(3 个)
        28. tlogEntries 为空 → 失败
        29. tlogEntries 缺 logIndex → 失败
        30. tlogEntries 完整 → 通过
    F. 断言 g:OIDC issuer(3 个)
        31. issuer 非法 → 失败
        32. issuer 合法(在 bundle cert 中)→ 通过
        33. issuer 字段缺失 → 跳过(通过,warning)
    G. 断言 h:证书有效期(4 个)
        34. 签名时间早于 notBefore → 失败
        35. 签名时间晚于 notAfter → 失败
        36. 签名时间在有效期内 → 通过
        37. 证书有效期字段缺失 → 跳过(通过,warning)
    H. --strict 模式与 JSON 输出(4 个)
        38. 非 strict 模式下 warning 不影响 overall_passed
        39. strict 模式下 warning 升级为 error
        40. --json-output 写入机器可读 JSON 报告
        41. 完整 SLSA provenance + bundle + strict 端到端通过
"""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 测试环境兼容(mock telegram 库,避免 ImportError)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify_attestation_semantics.py"
sys.path.insert(0, str(REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# 模块加载(避免直接 main 触发 argparse)
# ════════════════════════════════════════════════════════════════

def _load_verify_module():
    """通过 importlib 加载 verify_attestation_semantics 模块。"""
    spec = importlib.util.spec_from_file_location(
        "verify_attestation_semantics", SCRIPT_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_verify_mod = _load_verify_module()


# ════════════════════════════════════════════════════════════════
# Fixture 工厂函数(返回深拷贝,允许用例自由修改)
# ════════════════════════════════════════════════════════════════

# 固定测试值(用例间一致,便于追踪)
IMAGE_SHA = "a" * 64                          # 64 字符 sha256
MIGRATION_MANIFEST_DIGEST = "d" * 64          # 64 字符 sha256
IMAGE_REF = f"ghcr.io/example/app@sha256:{IMAGE_SHA}"
SUBJECT_NAME = "ghcr.io/example/app"
SOURCE_REPO = "example/app"


def _get_real_head_commit_and_tree() -> tuple[str, str]:
    """R67 P1-12: 从当前 git 仓库获取真实 HEAD commit + tree SHA。

    新增的 predicate_materials_source_tree 检查通过 `git rev-parse <commit>^{tree}`
    验证 commit 派生的 tree SHA 与 manifest.source_tree_sha 一致。为使 CLI(subprocess)
    测试也能通过,使用真实 git commit/tree SHA 作为测试 fixture。

    Returns:
        (commit_sha, tree_sha) — 40 字符 SHA-1 字符串
    """
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        tree = subprocess.check_output(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=str(REPO_ROOT),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if commit and tree:
            return commit, tree
    except (OSError, subprocess.SubprocessError):
        pass
    # 回退到固定值(非 git 仓库场景)
    return "b" * 40, "c" * 40


# R67 P1-12: 测试用真实 HEAD commit + tree SHA(使 CLI subprocess 测试也能通过)
SOURCE_COMMIT_SHA, SOURCE_TREE_SHA = _get_real_head_commit_and_tree()


def _make_valid_statement() -> dict:
    """返回完整合法的 SLSA provenance v1 statement(深拷贝自模板)。"""
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": "https://slsa.dev/provenance/v1",
        "subject": [
            {
                "name": SUBJECT_NAME,
                "digest": {"sha256": IMAGE_SHA},
            }
        ],
        "predicate": {
            "builder": {
                "id": "https://github.com/actions/runner/github-hosted/ubuntu-22.04",
            },
            "buildType": "https://github.com/actions/buildtype/v1",
            "invocation": {
                "configSource": {
                    "uri": f"git+https://github.com/{SOURCE_REPO}",
                    "digest": {"sha1": SOURCE_COMMIT_SHA},
                },
            },
            "materials": [
                {
                    "uri": f"git+https://github.com/{SOURCE_REPO}",
                    "digest": {
                        "sha1": SOURCE_COMMIT_SHA,
                        "sha256": SOURCE_TREE_SHA,
                    },
                },
                {
                    "uri": "file://migration-manifest.json",
                    "digest": {"sha256": MIGRATION_MANIFEST_DIGEST},
                },
            ],
        },
    }


def _make_valid_manifest() -> dict:
    """返回完整合法的 release manifest。"""
    return {
        "image_digest": IMAGE_SHA,
        "image_ref": IMAGE_REF,
        "source_repository": SOURCE_REPO,
        "source_commit": SOURCE_COMMIT_SHA,
        "source_tree_sha": SOURCE_TREE_SHA,
        "migration_manifest_digest": MIGRATION_MANIFEST_DIGEST,
    }


def _make_valid_bundle() -> dict:
    """返回完整合法的 Rekor bundle(含 tlogEntries + 证书链 + issuer)。

    签名时间设置为 "现在",证书有效期覆盖 [现在-1天, 现在+1天]。
    """
    now = datetime.now(timezone.utc)
    not_before = now - timedelta(days=1)
    not_after = now + timedelta(days=1)
    return {
        "verificationMaterial": {
            "tlogEntries": [
                {
                    "logIndex": 12345,
                    "integratedTime": str(int(now.timestamp())),
                }
            ],
            "x509CertificateChain": {
                "certificates": [
                    {
                        "issuer": _verify_mod.EXPECTED_OIDC_ISSUER,
                        "notBefore": not_before.isoformat(),
                        "notAfter": not_after.isoformat(),
                    }
                ]
            },
        },
        "signingTime": now.isoformat(),
    }


def _write_json(path: Path, data: dict) -> Path:
    """将 dict 写入 JSON 文件。"""
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _run_cli(
    *args: str,
    statement: dict | None = None,
    manifest: dict | None = None,
    bundle: dict | None = None,
    tmp_path: Path | None = None,
) -> subprocess.CompletedProcess:
    """调用 verify_attestation_semantics.py CLI。

    若提供 statement/manifest/bundle dict,自动写入 tmp_path 并构造 CLI 参数。
    """
    cmd: list[str] = [sys.executable, str(SCRIPT_PATH)]
    if statement is not None:
        s_path = _write_json(tmp_path / "statement.json", statement)
        cmd += ["--statement", str(s_path)]
    elif "--statement" not in args and "-h" not in args and "--help" not in args:
        # 没有提供 statement 且不在 args 中,直接使用 args(用于测试缺参场景)
        pass
    if manifest is not None:
        m_path = _write_json(tmp_path / "manifest.json", manifest)
        cmd += ["--release-manifest", str(m_path)]
    if bundle is not None:
        b_path = _write_json(tmp_path / "bundle.json", bundle)
        cmd += ["--bundle", str(b_path)]
    cmd += list(args)
    return subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _check_named(result: dict, name: str) -> dict:
    """从 verify_attestation_semantics 返回结果中按 name 取出单条 check。"""
    for c in result.get("checks", []):
        if c["name"] == name:
            return c
    pytest.fail(f"未找到名为 {name!r} 的 check,实际 checks: "
                f"{[c['name'] for c in result.get('checks', [])]}")


# ════════════════════════════════════════════════════════════════
# A. CLI / 退出码
# ════════════════════════════════════════════════════════════════

class TestCliAndExitCodes:
    """验证 CLI 入口与退出码语义。"""

    def test_script_exists(self):
        """脚本文件应存在。"""
        assert SCRIPT_PATH.exists(), "scripts/verify_attestation_semantics.py 应存在"

    def test_cli_help_exits_zero(self):
        """--help 应退出 0 并打印 usage。"""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0
        assert "--statement" in result.stdout
        assert "--release-manifest" in result.stdout
        assert "--bundle" in result.stdout
        assert "--strict" in result.stdout
        assert "--json-output" in result.stdout

    def test_cli_missing_required_args_returns_2(self):
        """缺少 --statement / --release-manifest 应退出 2(argparse 错误)。"""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 2

    def test_cli_statement_file_not_found_returns_2(self, tmp_path):
        """statement 文件不存在应退出 2(IO 错误)。"""
        manifest_path = _write_json(tmp_path / "manifest.json", _make_valid_manifest())
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT_PATH),
                "--statement", str(tmp_path / "nonexistent.json"),
                "--release-manifest", str(manifest_path),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 2
        assert "statement" in result.stderr.lower() or "statement" in result.stdout.lower()

    def test_cli_invalid_json_statement_returns_2(self, tmp_path):
        """statement JSON 解析失败应退出 2。"""
        bad_statement = tmp_path / "bad_statement.json"
        bad_statement.write_text("{ this is not valid json }", encoding="utf-8")
        manifest_path = _write_json(tmp_path / "manifest.json", _make_valid_manifest())
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT_PATH),
                "--statement", str(bad_statement),
                "--release-manifest", str(manifest_path),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 2

    def test_cli_valid_inputs_exit_zero(self, tmp_path):
        """合法 statement + manifest(无 bundle,非 strict)应退出 0。"""
        result = _run_cli(
            statement=_make_valid_statement(),
            manifest=_make_valid_manifest(),
            tmp_path=tmp_path,
        )
        assert result.returncode == 0, (
            f"期望 exit 0,实际 {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_cli_invalid_assertion_returns_1(self, tmp_path):
        """断言失败应退出 1。"""
        bad_statement = _make_valid_statement()
        bad_statement["_type"] = "https://invalid.example/Unknown/v9"
        result = _run_cli(
            statement=bad_statement,
            manifest=_make_valid_manifest(),
            tmp_path=tmp_path,
        )
        assert result.returncode == 1


# ════════════════════════════════════════════════════════════════
# B. 断言 a-b:statement type / predicate type
# ════════════════════════════════════════════════════════════════

class TestStatementAndPredicateType:
    """验证 assertion a (statement._type) 与 assertion b (predicateType)。"""

    def test_valid_statement_type_v0_1_passes(self):
        """statement._type 为 v0.1 应通过。"""
        statement = _make_valid_statement()
        statement["_type"] = "https://in-toto.io/Statement/v0.1"
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "statement_type")
        assert check["passed"]

    def test_valid_statement_type_v1_passes(self):
        """statement._type 为 v1 应通过。"""
        statement = _make_valid_statement()
        statement["_type"] = "https://in-toto.io/Statement/v1"
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "statement_type")
        assert check["passed"]

    def test_invalid_statement_type_fails(self):
        """非法 _type 应失败。"""
        statement = _make_valid_statement()
        statement["_type"] = "https://invalid.example/Unknown/v9"
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "statement_type")
        assert not check["passed"]
        assert check["severity"] == "error"
        assert not result["overall_passed"]

    def test_invalid_predicate_type_fails(self):
        """非法 predicateType 应失败。"""
        statement = _make_valid_statement()
        statement["predicateType"] = "https://invalid.example/unknown-predicate/v1"
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_type")
        assert not check["passed"]
        assert check["severity"] == "error"
        assert not result["overall_passed"]

    def test_cosign_attestation_predicate_type_passes(self):
        """cosign sigstore attestation v1 predicateType 应通过。"""
        statement = _make_valid_statement()
        statement["predicateType"] = "https://cosign.sigstore.dev/attestation/v1"
        # cosign attestation 通常无 SLSA predicate 子结构
        statement["predicate"] = {"data": "..."}
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_type")
        assert check["passed"]


# ════════════════════════════════════════════════════════════════
# C. 断言 c-d:subject digest / name
# ════════════════════════════════════════════════════════════════

class TestSubjectDigestAndName:
    """验证 assertion c (subject digest) 与 assertion d (subject name)。"""

    def test_subject_digest_matches_passes(self):
        """subject[0].digest.sha256 与 image_digest 一致应通过。"""
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(),
        )
        check = _check_named(result, "subject_digest")
        assert check["passed"]

    def test_subject_digest_with_sha256_prefix_passes(self):
        """subject digest 带 "sha256:" 前缀应自动剥离后通过。"""
        statement = _make_valid_statement()
        statement["subject"][0]["digest"]["sha256"] = f"sha256:{IMAGE_SHA}"
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "subject_digest")
        assert check["passed"]

    def test_image_digest_with_sha256_prefix_passes(self):
        """manifest image_digest 带 "sha256:" 前缀应自动剥离后通过。"""
        manifest = _make_valid_manifest()
        manifest["image_digest"] = f"sha256:{IMAGE_SHA}"
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), manifest,
        )
        check = _check_named(result, "subject_digest")
        assert check["passed"]

    def test_subject_digest_mismatch_fails(self):
        """subject digest 不匹配应失败。"""
        statement = _make_valid_statement()
        statement["subject"][0]["digest"]["sha256"] = "0" * 64
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "subject_digest")
        assert not check["passed"]
        assert check["severity"] == "error"
        assert not result["overall_passed"]

    def test_subject_name_mismatch_fails(self):
        """subject name 不匹配 image_ref 应失败。"""
        statement = _make_valid_statement()
        statement["subject"][0]["name"] = "ghcr.io/other/repo"
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "subject_name")
        assert not check["passed"]
        assert not result["overall_passed"]

    def test_subject_name_matches_image_ref_with_digest(self):
        """subject name 为 base name,image_ref 为 base@digest 形式应通过。"""
        statement = _make_valid_statement()
        # subject name 为 base name (无 digest 后缀)
        statement["subject"][0]["name"] = SUBJECT_NAME
        manifest = _make_valid_manifest()
        # image_ref 含 @sha256:... 后缀
        manifest["image_ref"] = f"{SUBJECT_NAME}@sha256:{IMAGE_SHA}"
        result = _verify_mod.verify_attestation_semantics(
            statement, manifest,
        )
        check = _check_named(result, "subject_name")
        assert check["passed"]

    def test_subject_name_with_image_field_alias(self):
        """manifest 用 image 字段(而非 image_ref)应同样支持。"""
        statement = _make_valid_statement()
        manifest = _make_valid_manifest()
        # 删除 image_ref,改用 image
        manifest.pop("image_ref")
        manifest["image"] = IMAGE_REF
        result = _verify_mod.verify_attestation_semantics(
            statement, manifest,
        )
        check = _check_named(result, "subject_name")
        assert check["passed"]


# ════════════════════════════════════════════════════════════════
# D. 断言 e:SLSA provenance predicate
# ════════════════════════════════════════════════════════════════

class TestSlsaProvenancePredicate:
    """验证 assertion e:SLSA provenance 子检查。"""

    def test_predicate_missing_is_warning_non_strict(self):
        """predicate 缺失(非 strict 模式)应为 warning,overall 通过。"""
        statement = _make_valid_statement()
        statement.pop("predicate")
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
            strict=False,
        )
        check = _check_named(result, "predicate_present")
        assert not check["passed"]
        assert check["severity"] == "warning"
        # 非 strict 模式下 warning 不影响 overall_passed
        assert result["overall_passed"]

    def test_predicate_missing_fails_in_strict_mode(self):
        """predicate 缺失(strict 模式)应失败。"""
        statement = _make_valid_statement()
        statement.pop("predicate")
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
            strict=True,
        )
        assert not result["overall_passed"]
        # strict 模式下 warning 升级为 error
        assert any("[strict]" in e for e in result["errors"])

    def test_predicate_builder_id_empty_fails(self):
        """predicate.builder.id 为空应失败。"""
        statement = _make_valid_statement()
        statement["predicate"]["builder"]["id"] = ""
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_builder_id")
        assert not check["passed"]
        assert not result["overall_passed"]

    def test_predicate_build_type_empty_fails(self):
        """predicate.buildType 为空应失败。"""
        statement = _make_valid_statement()
        statement["predicate"]["buildType"] = ""
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_build_type")
        assert not check["passed"]
        assert not result["overall_passed"]

    def test_predicate_config_source_uri_mismatch_fails(self):
        """configSource.uri 不含 github.com/<owner>/<repo> 应失败。"""
        statement = _make_valid_statement()
        statement["predicate"]["invocation"]["configSource"]["uri"] = (
            "git+https://gitlab.com/other/repo"
        )
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_config_source_uri")
        assert not check["passed"]
        assert not result["overall_passed"]

    def test_predicate_config_source_uri_with_github_prefix_in_manifest(self):
        """manifest.source_repository 含 'github.com/' 前缀时应正确匹配。"""
        statement = _make_valid_statement()
        manifest = _make_valid_manifest()
        manifest["source_repository"] = f"github.com/{SOURCE_REPO}"
        result = _verify_mod.verify_attestation_semantics(
            statement, manifest,
        )
        check = _check_named(result, "predicate_config_source_uri")
        assert check["passed"]

    def test_predicate_config_source_digest_mismatch_fails(self):
        """configSource.digest.sha1 与 source_commit 不一致应失败。"""
        statement = _make_valid_statement()
        statement["predicate"]["invocation"]["configSource"]["digest"]["sha1"] = "z" * 40
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_config_source_digest")
        assert not check["passed"]
        assert not result["overall_passed"]

    def test_predicate_config_source_digest_with_source_commit_sha_alias(self):
        """manifest 用 source_commit_sha(而非 source_commit)别名应同样支持。"""
        statement = _make_valid_statement()
        manifest = _make_valid_manifest()
        manifest.pop("source_commit")
        manifest["source_commit_sha"] = SOURCE_COMMIT_SHA
        result = _verify_mod.verify_attestation_semantics(
            statement, manifest,
        )
        check = _check_named(result, "predicate_config_source_digest")
        assert check["passed"]

    def test_predicate_materials_missing_source_tree_fails(self):
        """materials 缺 source_tree_sha 条目应失败。

        R67 P1-12: 检查名由 predicate_materials_source_tree_sha 重命名为
        predicate_materials_source_commit(实际验证 source_commit)。
        """
        statement = _make_valid_statement()
        # 移除含 source_tree_sha 的 material
        statement["predicate"]["materials"] = [
            {
                "uri": "file://other",
                "digest": {"sha256": "x" * 64},
            }
        ]
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_materials_source_commit")
        assert not check["passed"]
        assert not result["overall_passed"]

    def test_predicate_materials_source_tree_with_sha_prefix(self):
        """materials 中 source_tree_sha 条目带 "sha256:" 前缀应自动剥离后通过。

        R67 P1-12: 检查名由 predicate_materials_source_tree_sha 重命名为
        predicate_materials_source_commit(实际验证 source_commit)。
        """
        statement = _make_valid_statement()
        statement["predicate"]["materials"][0]["digest"]["sha256"] = f"sha256:{SOURCE_TREE_SHA}"
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_materials_source_commit")
        assert check["passed"]

    def test_predicate_materials_missing_migration_manifest_fails(self):
        """manifest 提供 migration_manifest_digest 但 materials 缺该条目应为 not_applicable(R71 RC50)。

        R66 P1-10 语义校正:
          标准 SLSA provenance(actions/attest-build-provenance)不会将 repo 内部文件
          (如 migration-manifest.json)作为独立 material 列出 — git source 条目已
          通过 commit SHA 绑定整个 repo 内容。

        R67 P0-07 语义校正:
          旧实现返回 passed=True, severity="warning"(soft-pass),被聚合器静默丢弃。

        R71 RC50 语义校正(关键修复):
          标准 SLSA attestation 不单独列出 repo 内部文件是设计行为,不是"未验证"。
          migration_manifest 通过 git source commit SHA 间接绑定,
          predicate_materials_source_commit 检查已验证 commit 一致。
          因此从 warning 升级为 not_applicable(strict 模式不再升级为 error)。
        """
        statement = _make_valid_statement()
        # 移除 migration material
        statement["predicate"]["materials"] = [
            statement["predicate"]["materials"][0]  # 仅保留 source_tree_sha material
        ]
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "predicate_materials_migration_manifest")
        # R71 RC50: not_applicable(标准 SLSA 设计行为,机器可验证理由)
        assert check["status"] == "not_applicable"
        assert check["passed"] is False  # 派生值:status != "passed"
        # not_applicable 不应进入 warnings 或 errors
        warning_msgs = result.get("warnings", [])
        assert not any("migration_manifest" in w for w in warning_msgs), (
            f"R71 RC50: not_applicable 不应进入 warnings,实际: {warning_msgs}"
        )
        # 非 strict 模式:overall_passed
        assert result["overall_passed"]

    def test_predicate_materials_missing_migration_strict_no_escalate(self):
        """R71 RC50: strict 模式下 migration material 缺失不升级为 error。

        R67 P0-07 旧行为:status="warning" → strict 模式升级为 error → overall_passed=False
        R71 RC50 新行为:status="not_applicable" → strict 模式不升级 → overall_passed=True

        原因:标准 SLSA attestation 不单独列出 repo 内部文件是设计行为,
        migration_manifest 通过 git source commit SHA 间接绑定,
        predicate_materials_source_commit 检查已验证 commit 一致。
        """
        statement = _make_valid_statement()
        statement["predicate"]["materials"] = [
            statement["predicate"]["materials"][0]  # 仅保留 source_tree_sha material
        ]
        # 提供合法 bundle,避免 bundle_present warning 在 strict 模式下升级为 error
        # (本测试聚焦于 migration_manifest 的 not_applicable 行为,bundle 不是验证目标)
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
            bundle=_make_valid_bundle(), strict=True,
        )
        check = _check_named(result, "predicate_materials_migration_manifest")
        # R71 RC50: not_applicable(strict 模式不升级)
        assert check["status"] == "not_applicable"
        # strict 模式:not_applicable 不升级为 error
        assert result["overall_passed"], (
            f"strict 模式下 not_applicable 不应阻断 overall_passed,实际 errors: "
            f"{result.get('errors', [])}"
        )
        error_msgs = result.get("errors", [])
        assert not any("migration_manifest" in e for e in error_msgs), (
            f"R71 RC50: not_applicable 不应升级为 error,实际 errors: {error_msgs}"
        )

    def test_predicate_materials_migration_skipped_when_manifest_missing_field(self):
        """manifest 未提供 migration_manifest_digest 时应跳过(not_applicable,R67 P0-07)。

        R67 P0-07 语义校正:
          旧实现返回 passed=True, severity="warning"(soft-pass),warning 被静默丢弃。
          新实现返回 status="not_applicable"(机器可验证理由:
          manifest.migration_manifest_digest 字段缺失),不阻断也不升级。
        """
        statement = _make_valid_statement()
        manifest = _make_valid_manifest()
        manifest.pop("migration_manifest_digest")
        result = _verify_mod.verify_attestation_semantics(
            statement, manifest,
        )
        check = _check_named(result, "predicate_materials_migration_manifest")
        # R67 P0-07: not_applicable 状态(不再用 passed=True 表达"跳过")
        assert check["status"] == "not_applicable"
        assert check["passed"] is False  # 派生值:status != "passed"
        # not_applicable 不应进入 warnings 或 errors
        warning_msgs = result.get("warnings", [])
        error_msgs = result.get("errors", [])
        assert not any("migration_manifest" in w for w in warning_msgs)
        assert not any("migration_manifest" in e for e in error_msgs)
        # overall_passed 应为 True(不阻断)
        assert result["overall_passed"]

    def test_predicate_materials_migration_not_applicable_strict_no_escalate(self):
        """R67 P0-07: not_applicable 在 strict 模式下不应升级为 error。

        not_applicable 与 warning 区别:
          - warning: 未直接验证,strict 模式升级为 error
          - not_applicable: 检查不适用(机器可验证理由),strict 模式不升级

        本测试提供合法 bundle,确保 strict 模式下 bundle_present 不产生 warning,
        只验证 migration_manifest 的 not_applicable 不升级。
        """
        statement = _make_valid_statement()
        manifest = _make_valid_manifest()
        manifest.pop("migration_manifest_digest")
        result = _verify_mod.verify_attestation_semantics(
            statement, manifest, bundle=_make_valid_bundle(), strict=True,
        )
        check = _check_named(result, "predicate_materials_migration_manifest")
        assert check["status"] == "not_applicable"
        # strict 模式:not_applicable 不升级为 error
        assert result["overall_passed"]
        error_msgs = result.get("errors", [])
        assert not any("migration_manifest" in e for e in error_msgs)


# ════════════════════════════════════════════════════════════════
# E. 断言 f:Rekor tlogEntries
# ════════════════════════════════════════════════════════════════

class TestRekorTlogEntries:
    """验证 assertion f:Rekor bundle tlogEntries。"""

    def test_tlog_entries_empty_fails(self):
        """tlogEntries 为空应失败。"""
        bundle = _make_valid_bundle()
        bundle["verificationMaterial"]["tlogEntries"] = []
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "rekor_tlog_entries")
        assert not check["passed"]
        assert not result["overall_passed"]

    def test_tlog_entries_missing_log_index_fails(self):
        """tlogEntries entry 缺 logIndex 应失败。"""
        bundle = _make_valid_bundle()
        del bundle["verificationMaterial"]["tlogEntries"][0]["logIndex"]
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "rekor_tlog_entries")
        assert not check["passed"]
        assert "logIndex" in check["message"]

    def test_tlog_entries_missing_integrated_time_fails(self):
        """tlogEntries entry 缺 integratedTime 应失败。"""
        bundle = _make_valid_bundle()
        del bundle["verificationMaterial"]["tlogEntries"][0]["integratedTime"]
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "rekor_tlog_entries")
        assert not check["passed"]
        assert "integratedTime" in check["message"]

    def test_tlog_entries_complete_passes(self):
        """tlogEntries 完整应通过。"""
        bundle = _make_valid_bundle()
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "rekor_tlog_entries")
        assert check["passed"]

    def test_tlog_entries_missing_entire_verification_material_fails(self):
        """整个 verificationMaterial 缺失应失败。"""
        bundle = _make_valid_bundle()
        del bundle["verificationMaterial"]
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "rekor_tlog_entries")
        assert not check["passed"]


# ════════════════════════════════════════════════════════════════
# F. 断言 g:OIDC issuer
# ════════════════════════════════════════════════════════════════

class TestOidcIssuer:
    """验证 assertion g:OIDC issuer 检查。"""

    def test_oidc_issuer_mismatch_fails(self):
        """OIDC issuer 非法应失败。"""
        bundle = _make_valid_bundle()
        bundle["verificationMaterial"]["x509CertificateChain"]["certificates"][0]["issuer"] = (
            "https://evil.example/oidc"
        )
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "oidc_issuer")
        assert not check["passed"]
        assert not result["overall_passed"]

    def test_oidc_issuer_valid_in_bundle_cert_passes(self):
        """bundle cert.issuer 为 GitHub Actions issuer 应通过。"""
        bundle = _make_valid_bundle()
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "oidc_issuer")
        assert check["passed"]

    def test_oidc_issuer_from_bundle_top_level_passes(self):
        """bundle 顶层 issuer 字段也应支持。"""
        bundle = _make_valid_bundle()
        # 删除 cert.issuer,改用顶层 issuer
        del bundle["verificationMaterial"]["x509CertificateChain"]["certificates"][0]["issuer"]
        bundle["issuer"] = _verify_mod.EXPECTED_OIDC_ISSUER
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "oidc_issuer")
        assert check["passed"]

    def test_oidc_issuer_from_statement_passes(self):
        """statement 顶层 issuer 字段也应支持(无 bundle 场景)。"""
        statement = _make_valid_statement()
        statement["issuer"] = _verify_mod.EXPECTED_OIDC_ISSUER
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        check = _check_named(result, "oidc_issuer")
        assert check["passed"]

    def test_oidc_issuer_missing_is_warning(self):
        """issuer 字段缺失时应跳过(not_applicable,R67 P0-07)。

        OIDC issuer 检查始终执行(因 issuer 可能在 statement 或 bundle 中);
        当两者均未提供 issuer 字段时,跳过检查 — R67 P0-07:
          旧实现返回 passed=True, severity="warning"(soft-pass),warning 被静默丢弃;
          新实现返回 status="not_applicable"(机器可验证理由:
          issuer 字段在 statement 与 bundle 中均缺失),不阻断也不升级。
        """
        # 不提供 bundle,statement 也不含 issuer
        statement = _make_valid_statement()
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        # oidc_issuer check 始终存在,但应跳过(status="not_applicable")
        check = _check_named(result, "oidc_issuer")
        assert check["status"] == "not_applicable"
        assert check["passed"] is False  # 派生值:status != "passed"
        # not_applicable 不应进入 warnings 或 errors
        warning_msgs = result.get("warnings", [])
        error_msgs = result.get("errors", [])
        assert not any("oidc_issuer" in w for w in warning_msgs)
        assert not any("oidc_issuer" in e for e in error_msgs)
        # bundle_present 也应存在(因未提供 bundle)
        check_names = [c["name"] for c in result["checks"]]
        assert "bundle_present" in check_names

    def test_oidc_issuer_not_applicable_strict_no_escalate(self):
        """R67 P0-07: not_applicable 在 strict 模式下不升级为 error。"""
        statement = _make_valid_statement()
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(), strict=True,
        )
        check = _check_named(result, "oidc_issuer")
        assert check["status"] == "not_applicable"
        # strict 模式:not_applicable 不升级为 error
        # (但 bundle_present 是 warning,strict 模式会升级 — 所以 overall 可能 fail)
        # 这里只验证 oidc_issuer 本身不进入 errors
        error_msgs = result.get("errors", [])
        assert not any("oidc_issuer" in e for e in error_msgs)


# ════════════════════════════════════════════════════════════════
# G. 断言 h:证书有效期
# ════════════════════════════════════════════════════════════════

class TestCertificateValidity:
    """验证 assertion h:证书有效期检查。"""

    def test_signing_time_before_not_before_fails(self):
        """签名时间早于 notBefore 应失败。"""
        bundle = _make_valid_bundle()
        # notBefore 设为未来 → signingTime(现在)早于 notBefore
        future = datetime.now(timezone.utc) + timedelta(days=10)
        bundle["verificationMaterial"]["x509CertificateChain"]["certificates"][0]["notBefore"] = (
            future.isoformat()
        )
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "certificate_validity")
        assert not check["passed"]
        assert "notBefore" in check["message"]
        assert not result["overall_passed"]

    def test_signing_time_after_not_after_fails(self):
        """签名时间晚于 notAfter 应失败。"""
        bundle = _make_valid_bundle()
        # notAfter 设为过去 → signingTime(现在)晚于 notAfter
        past = datetime.now(timezone.utc) - timedelta(days=10)
        bundle["verificationMaterial"]["x509CertificateChain"]["certificates"][0]["notAfter"] = (
            past.isoformat()
        )
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "certificate_validity")
        assert not check["passed"]
        assert "notAfter" in check["message"]

    def test_signing_time_within_validity_passes(self):
        """签名时间在 notBefore/notAfter 之间应通过。"""
        bundle = _make_valid_bundle()
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "certificate_validity")
        assert check["passed"]

    def test_certificate_validity_uses_integrated_time_as_signing_time(self):
        """无 signingTime 字段时,应使用 tlogEntries[0].integratedTime 作为签名时间。"""
        bundle = _make_valid_bundle()
        del bundle["signingTime"]
        # integratedTime 已设为 "现在",仍在证书有效期内
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "certificate_validity")
        assert check["passed"]

    def test_certificate_validity_top_level_not_before_after_fallback(self):
        """bundle 顶层 notBefore/notAfter 应作为证书链缺失时的回退。"""
        bundle = _make_valid_bundle()
        # 删除证书链中的 notBefore/notAfter,改用顶层字段
        cert = bundle["verificationMaterial"]["x509CertificateChain"]["certificates"][0]
        del cert["notBefore"]
        del cert["notAfter"]
        now = datetime.now(timezone.utc)
        bundle["notBefore"] = (now - timedelta(days=1)).isoformat()
        bundle["notAfter"] = (now + timedelta(days=1)).isoformat()
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(), bundle=bundle,
        )
        check = _check_named(result, "certificate_validity")
        assert check["passed"]


# ════════════════════════════════════════════════════════════════
# H. --strict 模式与 JSON 输出
# ════════════════════════════════════════════════════════════════

class TestStrictModeAndJsonOutput:
    """验证 --strict 模式行为与 --json-output 报告。"""

    def test_non_strict_mode_warning_does_not_fail(self):
        """非 strict 模式下,warning 不影响 overall_passed。"""
        # 不提供 bundle → bundle_present warning
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(),
            bundle=None, strict=False,
        )
        assert result["overall_passed"]
        assert len(result["warnings"]) > 0
        assert len(result["errors"]) == 0

    def test_strict_mode_warning_becomes_error(self):
        """strict 模式下,warning 应升级为 error,overall 失败。"""
        # 不提供 bundle → bundle_present warning
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(), _make_valid_manifest(),
            bundle=None, strict=True,
        )
        assert not result["overall_passed"]
        assert len(result["errors"]) > 0
        # strict 模式下错误信息应含 [strict] 标记
        assert any("[strict]" in e for e in result["errors"])
        assert result["strict_mode"] is True

    def test_strict_mode_with_bundle_warning_on_missing_predicate(self):
        """strict 模式下,predicate 缺失应触发 warning → error。"""
        statement = _make_valid_statement()
        statement.pop("predicate")
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
            bundle=_make_valid_bundle(),
            strict=True,
        )
        assert not result["overall_passed"]
        # 应同时存在 predicate_present warning 升级为 error
        assert any("predicate_present" in e for e in result["errors"])

    def test_json_output_file_written(self, tmp_path):
        """--json-output 应写入机器可读 JSON 报告。"""
        manifest_path = _write_json(tmp_path / "manifest.json", _make_valid_manifest())
        statement_path = _write_json(tmp_path / "statement.json", _make_valid_statement())
        json_output_path = tmp_path / "report.json"
        result = subprocess.run(
            [
                sys.executable, str(SCRIPT_PATH),
                "--statement", str(statement_path),
                "--release-manifest", str(manifest_path),
                "--json-output", str(json_output_path),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert json_output_path.exists()
        report = json.loads(json_output_path.read_text(encoding="utf-8"))
        # 报告应含必需字段
        for field in ("schema_version", "verified_at", "overall_passed",
                      "checks", "errors", "warnings", "strict_mode"):
            assert field in report, f"JSON 报告缺字段 {field}"
        assert report["schema_version"] == _verify_mod.SCHEMA_VERSION
        assert report["overall_passed"] is True
        assert isinstance(report["checks"], list)
        assert len(report["checks"]) > 0

    def test_full_strict_mode_with_bundle_passes(self, tmp_path):
        """完整 SLSA provenance + bundle + strict 模式应端到端通过(exit 0)。"""
        result = _run_cli(
            "--strict",
            statement=_make_valid_statement(),
            manifest=_make_valid_manifest(),
            bundle=_make_valid_bundle(),
            tmp_path=tmp_path,
        )
        assert result.returncode == 0, (
            f"期望 exit 0,实际 {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_full_strict_mode_missing_bundle_fails(self, tmp_path):
        """strict 模式下不提供 bundle 应失败(exit 1)。"""
        result = _run_cli(
            "--strict",
            statement=_make_valid_statement(),
            manifest=_make_valid_manifest(),
            tmp_path=tmp_path,
        )
        assert result.returncode == 1


# ════════════════════════════════════════════════════════════════
# I. 综合端到端测试
# ════════════════════════════════════════════════════════════════

class TestEndToEndScenarios:
    """综合场景测试。"""

    def test_complete_valid_scenario_all_checks_pass(self):
        """完整合法场景:所有 a-h 断言均通过。"""
        result = _verify_mod.verify_attestation_semantics(
            _make_valid_statement(),
            _make_valid_manifest(),
            bundle=_make_valid_bundle(),
            strict=True,
        )
        assert result["overall_passed"], (
            f"完整合法场景应通过,errors: {result['errors']}, warnings: {result['warnings']}"
        )
        # 所有 check 均通过
        for c in result["checks"]:
            assert c["passed"], f"check {c['name']} 未通过: {c['message']}"

    def test_allowed_predicate_types_all_pass(self):
        """允许的 predicateType 集合应包含 3 种类型。"""
        assert _verify_mod.ALLOWED_PREDICATE_TYPES == frozenset({
            "https://slsa.dev/provenance/v1",
            "https://slsa.dev/provenance/v0.2",
            "https://cosign.sigstore.dev/attestation/v1",
        })

    def test_allowed_statement_types_all_pass(self):
        """允许的 statement._type 集合应包含 v0.1 与 v1。"""
        assert _verify_mod.ALLOWED_STATEMENT_TYPES == frozenset({
            "https://in-toto.io/Statement/v0.1",
            "https://in-toto.io/Statement/v1",
        })

    def test_expected_oidc_issuer_value(self):
        """EXPECTED_OIDC_ISSUER 应为 GitHub Actions OIDC issuer。"""
        assert _verify_mod.EXPECTED_OIDC_ISSUER == (
            "https://token.actions.githubusercontent.com"
        )

    def test_multiple_errors_collected(self):
        """多个断言失败时,errors 列表应收集全部错误。"""
        statement = _make_valid_statement()
        # 制造 3 个错误:非法 _type + 非法 predicateType + digest 不匹配
        statement["_type"] = "https://invalid/v9"
        statement["predicateType"] = "https://invalid-predicate/v9"
        statement["subject"][0]["digest"]["sha256"] = "0" * 64
        result = _verify_mod.verify_attestation_semantics(
            statement, _make_valid_manifest(),
        )
        assert not result["overall_passed"]
        # 应至少有 3 个错误
        assert len(result["errors"]) >= 3
        check_names_failed = [
            c["name"] for c in result["checks"] if not c["passed"]
        ]
        assert "statement_type" in check_names_failed
        assert "predicate_type" in check_names_failed
        assert "subject_digest" in check_names_failed

    def test_deep_copy_safety_between_calls(self):
        """多次调用不应互相污染(确保不会修改输入 dict)。"""
        statement = _make_valid_statement()
        manifest = _make_valid_manifest()
        # 第一次调用
        _verify_mod.verify_attestation_semantics(statement, manifest)
        # 第二次调用应得到相同结果(说明输入未被破坏)
        result2 = _verify_mod.verify_attestation_semantics(statement, manifest)
        assert result2["overall_passed"]
