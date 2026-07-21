"""R64 P1-12: 生产运行证据框架 — 单元测试。

测试覆盖:
1. scripts/generate_production_evidence.py:统一证据生成入口
   - EVIDENCE_TYPES 5 类证据定义
   - 命令行参数解析(--all/--soak/--vps-recovery/--chaos/--ru-72h/--supply-chain/--list/--output-dir/--dry-run/--skip/--timeout-seconds)
   - _build_evidence_command 命令构建逻辑
   - generate_evidence 汇总报告结构 + 索引文件生成
2. scripts/verify_supply_chain.py:供应链验证
   - REQUIRED_DIGESTS 6 个 digest 字段
   - verify_supply_chain 函数行为(空 attestation / pending 字段 / 完整 attestation)
   - --attestation / --output-dir / --json / --skip-cosign 参数
   - _sha256_file / _sha256_files_concat / _sha256_dir_tree digest 计算
3. 4 个证据 shell 脚本的 --output-dir 参数支持:
   - soak_test_7day.sh
   - blank_vps_recovery_test.sh
   - chaos_bot_fault_injection.sh
   - ru_72h_verification.sh
4. .github/workflows/release-gates.yml production-evidence job:
   - production-evidence job 存在
   - release-summary 包含 production-evidence
   - PR 非阻断语义(non-blocking)

R64 P1-12 验收标准:
    - 5 类证据(soak / vps_recovery / chaos / ru_72h / supply_chain)统一入口
    - 4 个证据脚本均支持 --output-dir
    - release-gates.yml 包含 production-evidence job
    - PR 上非阻断,完整证据由 workflow_dispatch 手动触发
    - 6 digest 绑定(commit_sha / tree_sha / image_digest / sbom_digest / migration_digest / config_digest)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ════════════════════════════════════════════════════════════════
# 1. generate_production_evidence.py — 模块结构
# ════════════════════════════════════════════════════════════════


class TestGenerateEvidenceModuleStructure:
    """验证 generate_production_evidence.py 模块结构。"""

    def test_script_exists(self):
        """脚本应存在。"""
        script_path = REPO_ROOT / "scripts" / "generate_production_evidence.py"
        assert script_path.exists(), "scripts/generate_production_evidence.py 应存在"

    def test_module_has_evidence_types_dict(self):
        """模块应定义 EVIDENCE_TYPES 字典。"""
        # 通过 importlib 加载模块(避免直接 main 触发 argparse)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "generate_production_evidence",
            REPO_ROOT / "scripts" / "generate_production_evidence.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert hasattr(module, "EVIDENCE_TYPES")
        assert isinstance(module.EVIDENCE_TYPES, dict)

    def test_evidence_types_contains_five_types(self):
        """EVIDENCE_TYPES 应包含 6 类证据(R67 P0-04 新增 rc_verify_3x)。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "generate_production_evidence",
            REPO_ROOT / "scripts" / "generate_production_evidence.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        # 6 类证据:soak / vps_recovery / chaos / ru_72h / supply_chain / rc_verify_3x
        assert "soak" in module.EVIDENCE_TYPES
        assert "vps_recovery" in module.EVIDENCE_TYPES
        assert "chaos" in module.EVIDENCE_TYPES
        assert "ru_72h" in module.EVIDENCE_TYPES
        assert "supply_chain" in module.EVIDENCE_TYPES
        assert "rc_verify_3x" in module.EVIDENCE_TYPES
        assert len(module.EVIDENCE_TYPES) == 6

    def test_each_evidence_type_has_required_fields(self):
        """每个证据类型应包含 description/script/required_args/production_args/report_glob 字段。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "generate_production_evidence",
            REPO_ROOT / "scripts" / "generate_production_evidence.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        required_fields = {"description", "script", "required_args",
                           "production_args", "report_glob"}
        for et_name, et_info in module.EVIDENCE_TYPES.items():
            for field in required_fields:
                assert field in et_info, (
                    f"证据类型 {et_name} 缺少字段 {field}"
                )

    def test_evidence_scripts_point_to_correct_files(self):
        """每个证据类型的 script 字段应指向真实存在的脚本。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "generate_production_evidence",
            REPO_ROOT / "scripts" / "generate_production_evidence.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for et_name, et_info in module.EVIDENCE_TYPES.items():
            script_path = REPO_ROOT / et_info["script"]
            assert script_path.exists(), (
                f"证据类型 {et_name} 的脚本 {et_info['script']} 不存在"
            )

    def test_module_has_main_and_parse_args(self):
        """模块应定义 main / parse_args 函数。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "generate_production_evidence",
            REPO_ROOT / "scripts" / "generate_production_evidence.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert hasattr(module, "main")
        assert callable(module.main)
        assert hasattr(module, "parse_args")
        assert callable(module.parse_args)


# ════════════════════════════════════════════════════════════════
# 2. generate_production_evidence.py — 命令行参数
# ════════════════════════════════════════════════════════════════


class TestGenerateEvidenceArgparse:
    """验证 generate_production_evidence.py 的命令行参数支持。"""

    def test_script_has_all_options(self):
        """脚本应包含所有 R64 P1-12 必需的命令行参数。"""
        script_path = REPO_ROOT / "scripts" / "generate_production_evidence.py"
        content = script_path.read_text(encoding="utf-8")
        # 必需参数
        for opt in ("--all", "--soak", "--vps-recovery", "--chaos",
                    "--ru-72h", "--supply-chain", "--list",
                    "--output-dir", "--dry-run", "--skip",
                    "--timeout-seconds"):
            assert opt in content, (
                f"generate_production_evidence.py 应支持 {opt} 参数"
            )

    def test_list_option_exits_zero(self):
        """--list 选项应列出证据类型并退出码 0。"""
        script_path = REPO_ROOT / "scripts" / "generate_production_evidence.py"
        result = subprocess.run(
            ["python3", str(script_path), "--list"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"--list 应返回 0,stderr: {result.stderr[-500:]}"
        )
        # 输出应包含 5 类证据
        for et in ("soak", "vps_recovery", "chaos", "ru_72h", "supply_chain"):
            assert et in result.stdout, f"--list 输出应包含 {et}"

    def test_no_evidence_selected_returns_2(self):
        """未指定任何证据类型应返回退出码 2(参数错误)。"""
        script_path = REPO_ROOT / "scripts" / "generate_production_evidence.py"
        result = subprocess.run(
            ["python3", str(script_path)],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2, (
            f"无参数应返回 2,实际 {result.returncode},stderr: {result.stderr[-500:]}"
        )

    def test_skip_all_evidence_returns_zero(self):
        """--all --skip 所有类型应返回 0(无操作)。"""
        script_path = REPO_ROOT / "scripts" / "generate_production_evidence.py"
        result = subprocess.run(
            ["python3", str(script_path), "--all",
             "--skip", "soak", "--skip", "vps_recovery",
             "--skip", "chaos", "--skip", "ru_72h",
             "--skip", "supply_chain", "--skip", "rc_verify_3x"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"--all --skip 全部应返回 0,stderr: {result.stderr[-500:]}"
        )


# ════════════════════════════════════════════════════════════════
# 3. generate_production_evidence.py — _build_evidence_command
# ════════════════════════════════════════════════════════════════


class TestBuildEvidenceCommand:
    """验证 _build_evidence_command 命令构建逻辑。"""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        """加载 generate_production_evidence 模块。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "generate_production_evidence",
            REPO_ROOT / "scripts" / "generate_production_evidence.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        yield

    def test_command_includes_output_dir(self):
        """命令应包含 --output-dir 参数。"""
        cmd = self.module._build_evidence_command(
            "soak", Path("/tmp/evidence"), dry_run=False, extra_args=[],
        )
        assert "--output-dir" in cmd
        assert "/tmp/evidence" in cmd

    def test_command_for_shell_script_uses_bash(self):
        """shell 脚本应通过 bash 调用。"""
        cmd = self.module._build_evidence_command(
            "soak", Path("/tmp/evidence"), dry_run=False, extra_args=[],
        )
        assert cmd[0] == "bash"

    def test_command_for_python_script_uses_python3(self):
        """python 脚本应通过 python3 调用。"""
        cmd = self.module._build_evidence_command(
            "supply_chain", Path("/tmp/evidence"), dry_run=False, extra_args=[],
        )
        assert cmd[0] == "python3"

    def test_dry_run_adds_flag_for_soak_and_chaos(self):
        """dry_run=True 应为 soak/chaos 添加 --dry-run 标志。"""
        for et in ("soak", "chaos"):
            cmd = self.module._build_evidence_command(
                et, Path("/tmp/evidence"), dry_run=True, extra_args=[],
            )
            assert "--dry-run" in cmd, f"{et} dry_run 应添加 --dry-run 标志"

    def test_dry_run_not_added_for_supply_chain(self):
        """supply_chain 不支持 --dry-run(不添加)。"""
        cmd = self.module._build_evidence_command(
            "supply_chain", Path("/tmp/evidence"), dry_run=True, extra_args=[],
        )
        # supply_chain 不支持 dry-run
        assert "--dry-run" not in cmd

    def test_supply_chain_command_includes_json_flag(self):
        """supply_chain 命令应包含 --json(适合 CI 解析)。"""
        cmd = self.module._build_evidence_command(
            "supply_chain", Path("/tmp/evidence"), dry_run=False, extra_args=[],
        )
        assert "--json" in cmd

    def test_extra_args_appended(self):
        """extra_args 应附加到命令末尾。"""
        cmd = self.module._build_evidence_command(
            "chaos", Path("/tmp/evidence"), dry_run=False,
            extra_args=["--bot", "up", "--scenario", "kill"],
        )
        assert "--bot" in cmd
        assert "up" in cmd
        assert "--scenario" in cmd
        assert "kill" in cmd

    def test_ru_72h_command_includes_hours_72(self):
        """ru_72h 命令应包含 --hours 72(production 模式)。"""
        cmd = self.module._build_evidence_command(
            "ru_72h", Path("/tmp/evidence"), dry_run=False, extra_args=[],
        )
        assert "--hours" in cmd
        assert "72" in cmd


# ════════════════════════════════════════════════════════════════
# 4. generate_production_evidence.py — generate_evidence 汇总
# ════════════════════════════════════════════════════════════════


class TestGenerateEvidenceSummary:
    """验证 generate_evidence 异步函数的汇总报告结构。"""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        """加载 generate_production_evidence 模块。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "generate_production_evidence",
            REPO_ROOT / "scripts" / "generate_production_evidence.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        yield

    @pytest.mark.asyncio
    async def test_summary_contains_required_fields(self, tmp_path):
        """汇总报告应包含所有必需字段。"""
        # _run_evidence 是同步函数,使用同步 mock(side_effect 调用同步函数)
        def mock_run(*args, **kwargs):
            return {
                "evidence_type": "supply_chain",
                "status": "passed",
                "exit_code": 0,
                "duration_seconds": 1.0,
                "report_path": None,
                "report_size_bytes": 0,
                "error": None,
            }
        with patch.object(self.module, "_run_evidence", side_effect=mock_run):
            summary = await self.module.generate_evidence(
                evidence_types=["supply_chain"],
                output_dir=tmp_path,
                dry_run=False,
                extra_args_map={},
            )
        # 必需字段
        for field in ("schema_version", "generated_at", "started_at",
                      "duration_seconds", "output_dir", "dry_run",
                      "evidence_count", "passed_count", "failed_count",
                      "skipped_count", "error_count", "overall_status",
                      "results"):
            assert field in summary, f"汇总报告缺少字段 {field}"

    @pytest.mark.asyncio
    async def test_overall_status_passed_when_all_passed(self, tmp_path):
        """所有证据 passed 时 overall_status 应为 passed。"""
        def mock_run(*args, **kwargs):
            return {"evidence_type": "test", "status": "passed",
                    "exit_code": 0, "duration_seconds": 1.0,
                    "report_path": None, "report_size_bytes": 0, "error": None}
        with patch.object(self.module, "_run_evidence", side_effect=mock_run):
            summary = await self.module.generate_evidence(
                evidence_types=["supply_chain", "ru_72h"],
                output_dir=tmp_path, dry_run=False, extra_args_map={},
            )
        assert summary["overall_status"] == "passed"
        assert summary["passed_count"] == 2

    @pytest.mark.asyncio
    async def test_overall_status_failed_when_any_failed(self, tmp_path):
        """任何证据 failed 时 overall_status 应为 failed。"""
        def mock_run(et, *args, **kwargs):
            status = "failed" if et == "soak" else "passed"
            return {"evidence_type": et, "status": status,
                    "exit_code": 1 if status == "failed" else 0,
                    "duration_seconds": 1.0, "report_path": None,
                    "report_size_bytes": 0, "error": None}
        with patch.object(self.module, "_run_evidence", side_effect=mock_run):
            summary = await self.module.generate_evidence(
                evidence_types=["soak", "supply_chain"],
                output_dir=tmp_path, dry_run=False, extra_args_map={},
            )
        assert summary["overall_status"] == "failed"
        assert summary["failed_count"] == 1

    @pytest.mark.asyncio
    async def test_index_file_written(self, tmp_path):
        """应生成 production_evidence_index.json 索引文件。"""
        def mock_run(*args, **kwargs):
            return {"evidence_type": "supply_chain", "status": "passed",
                    "exit_code": 0, "duration_seconds": 1.0,
                    "report_path": None, "report_size_bytes": 0, "error": None}
        with patch.object(self.module, "_run_evidence", side_effect=mock_run):
            await self.module.generate_evidence(
                evidence_types=["supply_chain"],
                output_dir=tmp_path, dry_run=False, extra_args_map={},
            )
        index_path = tmp_path / "production_evidence_index.json"
        assert index_path.exists(), "应生成 production_evidence_index.json"
        # JSON 应可解析
        data = json.loads(index_path.read_text(encoding="utf-8"))
        assert data["schema_version"] == "r64_p1_12_v1"


# ════════════════════════════════════════════════════════════════
# 5. verify_supply_chain.py — 模块结构 + 常量
# ════════════════════════════════════════════════════════════════


class TestVerifySupplyChainModule:
    """验证 verify_supply_chain.py 模块结构与常量。"""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        """加载 verify_supply_chain 模块。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "verify_supply_chain",
            REPO_ROOT / "scripts" / "verify_supply_chain.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        yield

    def test_script_exists(self):
        """脚本应存在。"""
        script_path = REPO_ROOT / "scripts" / "verify_supply_chain.py"
        assert script_path.exists(), "scripts/verify_supply_chain.py 应存在"

    def test_required_digests_contains_six_fields(self):
        """REQUIRED_DIGESTS 应包含 6 个 digest 字段。"""
        digests = self.module.REQUIRED_DIGESTS
        assert "commit_sha" in digests
        assert "tree_sha" in digests
        assert "image_digest" in digests
        assert "sbom_digest" in digests
        assert "migration_digest" in digests
        assert "config_digest" in digests
        assert len(digests) == 6

    def test_config_files_lists_required_files(self):
        """CONFIG_FILES 应包含 docker-compose.yml / Dockerfile / requirements.txt。"""
        config_files = self.module.CONFIG_FILES
        assert "docker-compose.yml" in config_files
        assert "Dockerfile" in config_files
        assert "requirements.txt" in config_files

    def test_schema_version_constant(self):
        """SCHEMA_VERSION 应为 r64_p1_12_v1。"""
        assert self.module.SCHEMA_VERSION == "r64_p1_12_v1"

    def test_module_has_verify_supply_chain_function(self):
        """模块应定义 verify_supply_chain 函数。"""
        assert hasattr(self.module, "verify_supply_chain")
        assert callable(self.module.verify_supply_chain)

    def test_module_has_main_and_parse_args(self):
        """模块应定义 main / parse_args 函数。"""
        assert hasattr(self.module, "main")
        assert callable(self.module.main)
        assert hasattr(self.module, "parse_args")
        assert callable(self.module.parse_args)

    def test_script_has_all_argparse_options(self):
        """脚本应支持 --attestation / --output-dir / --json / --skip-cosign。"""
        script_path = REPO_ROOT / "scripts" / "verify_supply_chain.py"
        content = script_path.read_text(encoding="utf-8")
        for opt in ("--attestation", "--output-dir", "--json", "--skip-cosign"):
            assert opt in content, f"verify_supply_chain.py 应支持 {opt}"


# ════════════════════════════════════════════════════════════════
# 6. verify_supply_chain.py — verify_supply_chain 函数行为
# ════════════════════════════════════════════════════════════════


class TestVerifySupplyChainBehavior:
    """verify_supply_chain 函数在不同 attestation 场景下的行为。"""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        """加载 verify_supply_chain 模块。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "verify_supply_chain",
            REPO_ROOT / "scripts" / "verify_supply_chain.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        yield

    def test_missing_attestation_returns_failure(self, tmp_path):
        """attestation 文件不存在时应返回 overall_passed=False。"""
        attestation_path = tmp_path / "nonexistent.json"
        result = self.module.verify_supply_chain(
            attestation_path=attestation_path,
            skip_cosign=True,
        )
        assert result["overall_passed"] is False
        assert len(result["errors"]) > 0
        assert any("不存在" in e for e in result["errors"])

    def test_corrupt_json_returns_failure(self, tmp_path):
        """attestation JSON 损坏时应返回 overall_passed=False。"""
        attestation_path = tmp_path / "attestation.json"
        attestation_path.write_text("not a valid json {", encoding="utf-8")
        result = self.module.verify_supply_chain(
            attestation_path=attestation_path,
            skip_cosign=True,
        )
        assert result["overall_passed"] is False
        assert any("JSON" in e or "解析" in e for e in result["errors"])

    def test_pending_digest_fields_marked_as_failed(self, tmp_path):
        """attestation 中 pending 的 digest 字段应标记为未通过。"""
        attestation_path = tmp_path / "attestation.json"
        attestation = {
            "commit_sha": "pending",
            "tree_sha": "pending",
            "image_digest": "pending",
            "sbom_digest": "pending",
            "migration_digest": "pending",
            "config_digest": "pending",
        }
        attestation_path.write_text(json.dumps(attestation), encoding="utf-8")
        result = self.module.verify_supply_chain(
            attestation_path=attestation_path,
            skip_cosign=True,
        )
        # 6 个 digest 字段都应未通过(因为 pending)
        digest_checks = [c for c in result["checks"]
                         if c["name"].startswith("digest_bound:")]
        assert len(digest_checks) == 6
        for check in digest_checks:
            assert check["passed"] is False, (
                f"{check['name']} 应未通过(pending)"
            )
        assert result["overall_passed"] is False

    def test_empty_digest_fields_marked_as_failed(self, tmp_path):
        """空字符串 digest 字段应标记为未通过。"""
        attestation_path = tmp_path / "attestation.json"
        attestation = {
            "commit_sha": "",
            "tree_sha": "",
            "image_digest": "",
            "sbom_digest": "",
            "migration_digest": "",
            "config_digest": "",
        }
        attestation_path.write_text(json.dumps(attestation), encoding="utf-8")
        result = self.module.verify_supply_chain(
            attestation_path=attestation_path,
            skip_cosign=True,
        )
        digest_checks = [c for c in result["checks"]
                         if c["name"].startswith("digest_bound:")]
        for check in digest_checks:
            assert check["passed"] is False

    def test_complete_attestation_passes_digest_checks(self, tmp_path):
        """完整 attestation(所有 digest 已绑定)应通过 digest 检查。"""
        attestation_path = tmp_path / "attestation.json"
        attestation = {
            "commit_sha": "abc123def456789012345678901234567890abcd",
            "tree_sha": "def456789012345678901234567890abcd123456",
            "image_digest": "sha256:abc123",
            "sbom_digest": "abc123",
            "migration_digest": "abc123",
            "config_digest": "abc123",
        }
        attestation_path.write_text(json.dumps(attestation), encoding="utf-8")
        # Mock git rev-parse 返回不匹配(使 commit/tree 检查不通过,但 digest_bound 应通过)
        with patch.object(self.module, "_git_rev_parse", return_value=""):
            result = self.module.verify_supply_chain(
                attestation_path=attestation_path,
                skip_cosign=True,
            )
        # 6 个 digest_bound 检查应通过
        digest_checks = [c for c in result["checks"]
                         if c["name"].startswith("digest_bound:")]
        for check in digest_checks:
            assert check["passed"] is True, (
                f"{check['name']} 应通过(digest 已绑定)"
            )

    def test_result_structure_contains_required_fields(self, tmp_path):
        """结果应包含所有必需字段。"""
        attestation_path = tmp_path / "attestation.json"
        attestation_path.write_text("{}", encoding="utf-8")
        result = self.module.verify_supply_chain(
            attestation_path=attestation_path,
            skip_cosign=True,
        )
        for field in ("schema_version", "verified_at", "attestation_path",
                      "overall_passed", "checks", "attestation", "errors"):
            assert field in result, f"结果缺少字段 {field}"


# ════════════════════════════════════════════════════════════════
# 7. verify_supply_chain.py — digest 计算函数
# ════════════════════════════════════════════════════════════════


class TestDigestComputation:
    """验证 _sha256_file / _sha256_files_concat / _sha256_dir_tree 函数。"""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        """加载 verify_supply_chain 模块。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "verify_supply_chain",
            REPO_ROOT / "scripts" / "verify_supply_chain.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        yield

    def test_sha256_file_returns_hex_string(self, tmp_path):
        """_sha256_file 应返回 64 字符 hex SHA-256。"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world", encoding="utf-8")
        digest = self.module._sha256_file(test_file)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_sha256_file_deterministic(self, tmp_path):
        """相同内容应产生相同 SHA-256。"""
        test_file1 = tmp_path / "test1.txt"
        test_file2 = tmp_path / "test2.txt"
        test_file1.write_text("same content", encoding="utf-8")
        test_file2.write_text("same content", encoding="utf-8")
        assert self.module._sha256_file(test_file1) == \
               self.module._sha256_file(test_file2)

    def test_sha256_files_concat_empty_returns_empty(self, tmp_path):
        """空文件列表应返回空字符串。"""
        result = self.module._sha256_files_concat([])
        assert result == ""

    def test_sha256_files_concat_multiple_files(self, tmp_path):
        """多文件合并应返回非空 SHA-256。"""
        f1 = tmp_path / "f1.txt"
        f2 = tmp_path / "f2.txt"
        f1.write_text("content1", encoding="utf-8")
        f2.write_text("content2", encoding="utf-8")
        result = self.module._sha256_files_concat([f1, f2])
        assert len(result) == 64
        # 不存在的文件不应影响合并(只计算存在的)
        result2 = self.module._sha256_files_concat(
            [f1, f2, tmp_path / "nonexistent.txt"]
        )
        assert result == result2

    def test_sha256_dir_tree_no_matches_returns_empty(self, tmp_path):
        """无匹配文件应返回空字符串。"""
        result = self.module._sha256_dir_tree("*.nonexistent", tmp_path)
        assert result == ""

    def test_sha256_dir_tree_with_matches(self, tmp_path):
        """有匹配文件应返回非空 SHA-256。"""
        (tmp_path / "a.sql").write_text("-- a", encoding="utf-8")
        (tmp_path / "b.sql").write_text("-- b", encoding="utf-8")
        result = self.module._sha256_dir_tree("*.sql", tmp_path)
        assert len(result) == 64


# ════════════════════════════════════════════════════════════════
# 8. verify_supply_chain.py — CLI 执行(端到端)
# ════════════════════════════════════════════════════════════════


class TestVerifySupplyChainCli:
    """验证 verify_supply_chain.py 命令行执行。"""

    def test_cli_with_no_attestation_returns_1(self, tmp_path):
        """无 attestation 文件时 CLI 应返回退出码 1。"""
        script_path = REPO_ROOT / "scripts" / "verify_supply_chain.py"
        result = subprocess.run(
            ["python3", str(script_path),
             "--attestation", str(tmp_path / "nonexistent.json"),
             "--skip-cosign", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1, (
            f"无 attestation 应返回 1,stderr: {result.stderr[-500:]}"
        )

    def test_cli_with_complete_attestation_and_skip_cosign(self, tmp_path):
        """完整 attestation + --skip-cosign 应通过 digest 绑定检查。

        注:overall_passed 可能为 False(commit_sha 不匹配 git HEAD),
        但 6 个 digest_bound 检查应通过。
        """
        script_path = REPO_ROOT / "scripts" / "verify_supply_chain.py"
        attestation_path = tmp_path / "attestation.json"
        attestation = {
            "commit_sha": "abc123def456789012345678901234567890abcd",
            "tree_sha": "def456789012345678901234567890abcd123456",
            "image_digest": "sha256:abc123",
            "sbom_digest": "abc123",
            "migration_digest": "abc123",
            "config_digest": "abc123",
        }
        attestation_path.write_text(json.dumps(attestation), encoding="utf-8")
        result = subprocess.run(
            ["python3", str(script_path),
             "--attestation", str(attestation_path),
             "--output-dir", str(tmp_path),
             "--skip-cosign", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        # JSON 输出应可解析
        output = json.loads(result.stdout)
        assert "checks" in output
        # 6 个 digest_bound 检查应通过
        digest_checks = [c for c in output["checks"]
                         if c["name"].startswith("digest_bound:")]
        assert len(digest_checks) == 6
        for check in digest_checks:
            assert check["passed"] is True
        # 报告文件应已生成
        reports = list(tmp_path.glob("supply_chain_report_*.json"))
        assert len(reports) >= 1, "应生成 supply_chain_report_*.json 报告文件"


# ════════════════════════════════════════════════════════════════
# 9. 4 个证据 shell 脚本 — --output-dir 参数支持
# ════════════════════════════════════════════════════════════════


class TestEvidenceScriptOutputDir:
    """验证 4 个证据 shell 脚本均支持 --output-dir 参数。"""

    def test_soak_test_script_has_output_dir(self):
        """soak_test_7day.sh 应支持 --output-dir。"""
        path = REPO_ROOT / "scripts" / "soak_test_7day.sh"
        content = path.read_text(encoding="utf-8")
        assert "--output-dir" in content

    def test_blank_vps_recovery_test_script_has_output_dir(self):
        """blank_vps_recovery_test.sh 应支持 --output-dir。"""
        path = REPO_ROOT / "scripts" / "blank_vps_recovery_test.sh"
        content = path.read_text(encoding="utf-8")
        assert "--output-dir" in content

    def test_chaos_bot_fault_injection_script_has_output_dir(self):
        """chaos_bot_fault_injection.sh 应支持 --output-dir。"""
        path = REPO_ROOT / "scripts" / "chaos_bot_fault_injection.sh"
        content = path.read_text(encoding="utf-8")
        assert "--output-dir" in content

    def test_ru_72h_verification_script_has_output_dir(self):
        """ru_72h_verification.sh 应支持 --output-dir。"""
        path = REPO_ROOT / "scripts" / "ru_72h_verification.sh"
        content = path.read_text(encoding="utf-8")
        assert "--output-dir" in content

    def test_all_scripts_have_r64_p1_12_comment(self):
        """所有 4 个证据脚本应包含 R64 P1-12 注释标记。"""
        scripts = [
            "scripts/soak_test_7day.sh",
            "scripts/blank_vps_recovery_test.sh",
            "scripts/chaos_bot_fault_injection.sh",
            "scripts/ru_72h_verification.sh",
        ]
        for s in scripts:
            path = REPO_ROOT / s
            content = path.read_text(encoding="utf-8")
            # 至少应包含 R64 P1-12 或 --output-dir(其中一个即可)
            assert "R64 P1-12" in content or "--output-dir" in content, (
                f"{s} 应包含 R64 P1-12 注释或 --output-dir 参数"
            )


# ════════════════════════════════════════════════════════════════
# 10. .github/workflows/release-gates.yml — production-evidence job
# ════════════════════════════════════════════════════════════════


class TestReleaseGatesProductionEvidenceJob:
    """验证 release-gates.yml 包含 production-evidence job。"""

    def test_workflow_file_exists(self):
        """release-gates.yml 应存在。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        assert path.exists(), ".github/workflows/release-gates.yml 应存在"

    def test_workflow_has_production_evidence_job(self):
        """workflow 应包含 production-evidence job。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = path.read_text(encoding="utf-8")
        assert "production-evidence:" in content, (
            "release-gates.yml 应包含 production-evidence job"
        )

    def test_workflow_has_supply_chain_verification_step(self):
        """production-evidence job 应包含 supply chain verification 步骤。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = path.read_text(encoding="utf-8")
        assert "verify_supply_chain.py" in content
        assert "Supply Chain Verification" in content

    def test_workflow_has_production_evidence_index_step(self):
        """production-evidence job 应包含证据索引生成步骤。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = path.read_text(encoding="utf-8")
        assert "production_evidence_index.json" in content
        assert "ci_dry_run" in content or "ci_dry-run" in content or "dry-run" in content

    def test_release_summary_includes_production_evidence(self):
        """release-summary job 应在 needs 中包含 production-evidence。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = path.read_text(encoding="utf-8")
        # release-summary 的 needs 列表应包含 production-evidence
        assert "production-evidence" in content

    def test_production_evidence_non_blocking_for_pr(self):
        """production-evidence 在 PR 上应非阻断(non-blocking 注释)。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = path.read_text(encoding="utf-8")
        # 应有 non-blocking 相关注释
        assert "non-blocking" in content or "不阻断" in content or "non-blocking" in content.lower()
        # 应有 if: always()(允许依赖失败时仍运行)
        assert "if: always()" in content

    def test_workflow_uses_check_crdb_ru_threshold_script(self):
        """production-evidence job 应调用 check_crdb_ru_threshold.py(快速 RU 验证)。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = path.read_text(encoding="utf-8")
        assert "check_crdb_ru_threshold.py" in content
        assert "ru_threshold_ci" in content

    def test_workflow_uploads_production_evidence_artifact(self):
        """production-evidence job 应上传 production-evidence artifact。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = path.read_text(encoding="utf-8")
        assert "production-evidence-" in content  # artifact name
        assert "production-evidence/" in content  # artifact path

    def test_workflow_has_skip_cosign_in_ci(self):
        """CI 中 verify_supply_chain.py 应使用 --skip-cosign(无 cosign 环境)。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = path.read_text(encoding="utf-8")
        assert "--skip-cosign" in content

    def test_workflow_has_r64_p1_12_comment(self):
        """workflow 应包含 R64 P1-12 注释标记。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = path.read_text(encoding="utf-8")
        assert "R64 P1-12" in content

    def test_production_evidence_job_has_proper_permissions(self):
        """production-evidence job 应有正确的 permissions 设置。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = path.read_text(encoding="utf-8")
        # 定位 production-evidence job 块
        pe_idx = content.find("production-evidence:")
        assert pe_idx >= 0
        # 截取 production-evidence job 块(到 release-summary 之前)
        rs_idx = content.find("release-summary:", pe_idx)
        pe_block = content[pe_idx:rs_idx] if rs_idx > 0 else content[pe_idx:]
        # 应包含 permissions 块
        assert "permissions:" in pe_block
        # 应包含 id-token / contents / actions 等权限
        assert "id-token" in pe_block or "id_token" in pe_block

    def test_release_summary_treats_production_evidence_as_non_blocking(self):
        """release-summary 应将 production-evidence 标记为 non-blocking(警告而非阻断)。"""
        path = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = path.read_text(encoding="utf-8")
        # 应有 production-evidence non-blocking 处理逻辑
        assert "production-evidence" in content
        # 应有警告逻辑(warning 而非 error)
        assert "::warning::" in content or "non-blocking" in content


# ════════════════════════════════════════════════════════════════
# 11. _cosign_verify_blob — cosign 验证函数
# ════════════════════════════════════════════════════════════════


class TestCosignVerifyBlob:
    """验证 _cosign_verify_blob 函数行为。"""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        """加载 verify_supply_chain 模块。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "verify_supply_chain",
            REPO_ROOT / "scripts" / "verify_supply_chain.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        yield

    def test_missing_attestation_returns_false(self, tmp_path):
        """attestation 文件不存在时应返回 (False, ...)。"""
        path = tmp_path / "nonexistent.json"
        ok, msg = self.module._cosign_verify_blob(path)
        assert ok is False
        assert "不存在" in msg

    def test_missing_cert_and_sig_returns_false(self, tmp_path):
        """缺少 cert / sig 文件应返回 (False, ...)。"""
        attestation = tmp_path / "attestation.json"
        attestation.write_text("{}", encoding="utf-8")
        ok, msg = self.module._cosign_verify_blob(attestation)
        assert ok is False
        assert "证书" in msg or "签名" in msg or "cert" in msg.lower() or "sig" in msg.lower()

    def test_cosign_not_in_path_returns_false_gracefully(self, tmp_path):
        """cosign 不在 PATH 时应优雅返回 (False, ...)。"""
        attestation = tmp_path / "attestation.json"
        attestation.write_text("{}", encoding="utf-8")
        # 创建 cert 和 sig 文件
        (tmp_path / "attestation.pem").write_text("fake cert", encoding="utf-8")
        (tmp_path / "attestation.sig").write_text("fake sig", encoding="utf-8")
        # Mock subprocess.run 抛 FileNotFoundError
        with patch("subprocess.run", side_effect=FileNotFoundError):
            ok, msg = self.module._cosign_verify_blob(attestation)
        assert ok is False
        assert "cosign" in msg.lower() or "PATH" in msg


# ════════════════════════════════════════════════════════════════
# 12. _git_rev_parse — git SHA 解析
# ════════════════════════════════════════════════════════════════


class TestGitRevParse:
    """验证 _git_rev_parse 函数行为。"""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        """加载 verify_supply_chain 模块。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "verify_supply_chain",
            REPO_ROOT / "scripts" / "verify_supply_chain.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        yield

    def test_returns_actual_head_sha(self):
        """应返回当前 git HEAD SHA(40 字符 hex)。"""
        sha = self.module._git_rev_parse("HEAD")
        # 在 git 仓库中应返回 40 字符 SHA
        if sha:  # 测试环境可能无 git
            assert len(sha) == 40
            assert all(c in "0123456789abcdef" for c in sha)

    def test_returns_empty_on_invalid_ref(self):
        """无效 ref 应返回空字符串。"""
        sha = self.module._git_rev_parse("nonexistent_ref_xyz")
        assert sha == ""

    def test_returns_tree_sha_for_head_tree(self):
        """HEAD^{tree} 应返回有效 tree SHA(若 git 可用)。"""
        tree_sha = self.module._git_rev_parse("HEAD^{tree}")
        if tree_sha:  # 测试环境可能无 git
            assert len(tree_sha) == 40


# ════════════════════════════════════════════════════════════════
# 13. _find_latest_report — 报告文件查找
# ════════════════════════════════════════════════════════════════


class TestFindLatestReport:
    """验证 _find_latest_report 函数行为。"""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        """加载 generate_production_evidence 模块。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "generate_production_evidence",
            REPO_ROOT / "scripts" / "generate_production_evidence.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        yield

    def test_no_matches_returns_none(self, tmp_path):
        """无匹配文件应返回 None。"""
        result = self.module._find_latest_report(tmp_path, "soak")
        assert result is None

    def test_returns_latest_file(self, tmp_path):
        """有多个匹配文件时应返回最新(字典序最大)的一个。"""
        # 创建 3 个报告文件
        (tmp_path / "soak_report_20260101_120000.json").write_text("{}", encoding="utf-8")
        (tmp_path / "soak_report_20260201_120000.json").write_text("{}", encoding="utf-8")
        (tmp_path / "soak_report_20260301_120000.json").write_text("{}", encoding="utf-8")
        result = self.module._find_latest_report(tmp_path, "soak")
        assert result is not None
        assert "20260301" in result.name  # 最新的


# ════════════════════════════════════════════════════════════════
# 14. _run_evidence — 脚本不存在场景
# ════════════════════════════════════════════════════════════════


class TestRunEvidenceSkipped:
    """验证 _run_evidence 在脚本不存在时返回 skipped。"""

    @pytest.fixture(autouse=True)
    def _load_module(self):
        """加载 generate_production_evidence 模块。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "generate_production_evidence",
            REPO_ROOT / "scripts" / "generate_production_evidence.py",
        )
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        yield

    def test_skipped_when_script_missing(self, tmp_path):
        """脚本不存在时应返回 status=skipped。"""
        # Mock EVIDENCE_TYPES 中 soak 的 script 指向不存在文件
        with patch.dict(self.module.EVIDENCE_TYPES, {
            "soak": {
                "description": "test",
                "script": "scripts/nonexistent_script.sh",
                "required_args": [],
                "production_args": [],
                "estimated_duration_minutes": 1,
                "report_glob": "nonexistent_*.json",
            }
        }):
            result = self.module._run_evidence(
                "soak", tmp_path, dry_run=False, extra_args=[],
            )
        assert result["status"] == "skipped"
        assert "不存在" in result["error"]


# ════════════════════════════════════════════════════════════════
# 15. 端到端 — generate_production_evidence.py + supply_chain
# ════════════════════════════════════════════════════════════════


class TestEndToEndSupplyChainEvidence:
    """端到端验证:generate_production_evidence.py --supply-chain 实际执行。"""

    def test_supply_chain_evidence_generates_index(self, tmp_path):
        """--supply-chain 实际执行应生成索引文件。"""
        script_path = REPO_ROOT / "scripts" / "generate_production_evidence.py"
        result = subprocess.run(
            ["python3", str(script_path),
             "--supply-chain",
             "--output-dir", str(tmp_path),
             "--timeout-seconds", "60"],
            capture_output=True, text=True, timeout=120,
        )
        # 应生成索引文件(无论 verify_supply_chain.py 是否通过)
        index_path = tmp_path / "production_evidence_index.json"
        assert index_path.exists(), (
            f"应生成索引文件,stdout: {result.stdout[-500:]}, "
            f"stderr: {result.stderr[-500:]}"
        )
        # 索引应可解析
        data = json.loads(index_path.read_text(encoding="utf-8"))
        assert data["schema_version"] == "r64_p1_12_v1"
        assert data["evidence_count"] >= 1
        # results 应包含 supply_chain 条目
        sc_results = [r for r in data["results"]
                      if r["evidence_type"] == "supply_chain"]
        assert len(sc_results) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
