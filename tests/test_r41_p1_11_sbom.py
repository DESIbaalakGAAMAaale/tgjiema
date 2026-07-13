"""R41 P1-11: SBOM 生成器 + 依赖校验测试。

被测目标:
- scripts/generate_sbom.py: _parse_requirements / _merge_packages /
  generate_sbom / _format_cyclonedx / main
- scripts/verify_deps.sh: 存在性与基本结构检查

测试场景:
1. _parse_requirements 正确解析 requirements.txt(跳过注释/空行/选项行/环境标记)
2. _merge_packages 合并 req + freeze,requirements.txt 优先(版本权威)
3. generate_sbom 生成正确 JSON 结构(schema_version / packages / package_count)
4. generate_sbom --format cyclonedx 输出 CycloneDX 兼容格式
5. generate_sbom --no-pip-freeze 离线模式(不调用 pip freeze)
6. main CLI 入口返回 0 并写入 SBOM 文件
7. verify_deps.sh 文件存在且非空
"""
import importlib
import inspect
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ── 加载 scripts/generate_sbom.py(非标准包,通过 importlib 加载) ──
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

generate_sbom = importlib.import_module("generate_sbom")


# ════════════════════════════════════════════════════════════════
# 1. _parse_requirements 解析测试
# ════════════════════════════════════════════════════════════════

class TestParseRequirements:
    """_parse_requirements 正确解析 requirements.txt。"""

    def test_parse_basic_packages(self, tmp_path):
        """解析标准 "package==version" 条目。"""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "httpx==0.27.2\n"
            "fastapi==0.115.6\n"
            "loguru==0.7.3\n",
            encoding="utf-8",
        )
        items = generate_sbom._parse_requirements(req_file)
        names = [it["name"] for it in items]
        assert "httpx" in names
        assert "fastapi" in names
        assert "loguru" in names
        # 验证版本与 source
        httpx_item = next(it for it in items if it["name"] == "httpx")
        assert httpx_item["version"] == "0.27.2"
        assert httpx_item["source"] == "requirements.txt"

    def test_skip_comments_and_blanks(self, tmp_path):
        """跳过注释行、空行、-r 选项行。"""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "# 这是注释\n"
            "\n"
            "httpx==0.27.2\n"
            "-r other_requirements.txt\n"
            "--hash=sha256:abc123\n"
            "fastapi==0.115.6\n",
            encoding="utf-8",
        )
        items = generate_sbom._parse_requirements(req_file)
        names = [it["name"] for it in items]
        # 只应解析出 2 个包(跳过注释、空行、-r、--hash)
        assert len(items) == 2
        assert "httpx" in names
        assert "fastapi" in names

    def test_strip_environment_markers(self, tmp_path):
        """移除环境标记(; 后面的部分),如 uvloop; sys_platform != 'win32'。"""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "uvloop==0.19.0; sys_platform != 'win32'\n"
            "pyotp>=2.9.0\n",
            encoding="utf-8",
        )
        items = generate_sbom._parse_requirements(req_file)
        names = [it["name"] for it in items]
        assert "uvloop" in names
        assert "pyotp" in names
        # uvloop 的版本应为 0.19.0(移除了环境标记)
        uvloop_item = next(it for it in items if it["name"] == "uvloop")
        assert uvloop_item["version"] == "0.19.0"

    def test_nonexistent_file_returns_empty(self, tmp_path):
        """文件不存在时返回空列表。"""
        items = generate_sbom._parse_requirements(tmp_path / "nonexistent.txt")
        assert items == []


# ════════════════════════════════════════════════════════════════
# 2. _merge_packages 合并测试
# ════════════════════════════════════════════════════════════════

class TestMergePackages:
    """_merge_packages 合并 req + freeze,requirements.txt 优先。"""

    def test_req_overrides_freeze(self):
        """requirements.txt 的版本覆盖 pip freeze(版本权威)。"""
        req_items = [
            {"name": "httpx", "version": "0.27.2", "source": "requirements.txt"},
        ]
        freeze_items = [
            {"name": "httpx", "version": "0.28.0", "source": "pip_freeze"},
            {"name": "pytest", "version": "8.0.0", "source": "pip_freeze"},
        ]
        merged = generate_sbom._merge_packages(req_items, freeze_items)
        # 合并后应有 2 个包(httpx + pytest)
        assert len(merged) == 2
        # httpx 版本应为 requirements.txt 的版本(0.27.2)
        httpx = next(it for it in merged if it["name"] == "httpx")
        assert httpx["version"] == "0.27.2"
        assert httpx["source"] == "requirements.txt"
        # pytest 来自 pip freeze
        pytest_item = next(it for it in merged if it["name"] == "pytest")
        assert pytest_item["source"] == "pip_freeze"

    def test_sorted_by_name(self):
        """合并后按包名字典序排列。"""
        req_items = [
            {"name": "zlib", "version": "1.0", "source": "requirements.txt"},
            {"name": "httpx", "version": "0.27.2", "source": "requirements.txt"},
        ]
        freeze_items = [
            {"name": "asyncpg", "version": "0.30.0", "source": "pip_freeze"},
        ]
        merged = generate_sbom._merge_packages(req_items, freeze_items)
        names = [it["name"] for it in merged]
        assert names == ["asyncpg", "httpx", "zlib"]


# ════════════════════════════════════════════════════════════════
# 3. generate_sbom JSON 格式测试
# ════════════════════════════════════════════════════════════════

class TestGenerateSbomJson:
    """generate_sbom 生成 JSON 格式 SBOM。"""

    def test_json_structure(self, tmp_path, monkeypatch):
        """生成正确 JSON 结构(schema_version / packages / package_count)。"""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("httpx==0.27.2\n", encoding="utf-8")
        # Mock 元数据查询,避免调用 pip show
        monkeypatch.setattr(
            generate_sbom, "_get_license_from_metadata",
            lambda name: ("BSD-3-Clause", "https://github.com/encode/httpx", "HTTP client"),
        )
        monkeypatch.setattr(generate_sbom, "_run_pip_freeze", lambda: [])

        sbom = generate_sbom.generate_sbom(
            requirements_path=req_file,
            use_pip_freeze=False,
        )
        assert sbom["schema_version"] == "1.0"
        assert sbom["project"] == "tgjiema"
        assert sbom["package_count"] == 1
        assert len(sbom["packages"]) == 1
        pkg = sbom["packages"][0]
        assert pkg["name"] == "httpx"
        assert pkg["version"] == "0.27.2"
        assert pkg["license"] == "BSD-3-Clause"
        assert pkg["source"] == "requirements.txt"

    def test_with_sha256(self, tmp_path, monkeypatch):
        """--with-sha256 启用 PyPI sha256 查询。"""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("httpx==0.27.2\n", encoding="utf-8")
        monkeypatch.setattr(
            generate_sbom, "_get_license_from_metadata",
            lambda name: ("MIT", "", ""),
        )
        # Mock PyPI sha256 查询
        monkeypatch.setattr(
            generate_sbom, "_get_sha256_from_pypi",
            lambda name, version: "abc123sha256",
        )
        monkeypatch.setattr(generate_sbom, "_run_pip_freeze", lambda: [])

        sbom = generate_sbom.generate_sbom(
            requirements_path=req_file,
            with_sha256=True,
            use_pip_freeze=False,
        )
        pkg = sbom["packages"][0]
        assert pkg["sha256"] == "abc123sha256"

    def test_offline_mode(self, tmp_path, monkeypatch):
        """--no-pip-freeze 离线模式(不调用 pip freeze)。"""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text(
            "httpx==0.27.2\nfastapi==0.115.6\n", encoding="utf-8",
        )
        monkeypatch.setattr(
            generate_sbom, "_get_license_from_metadata",
            lambda name: ("UNKNOWN", "", ""),
        )
        # 若 use_pip_freeze=False,_run_pip_freeze 不应被调用
        call_log = {"freeze_called": False}

        def _fake_freeze():
            call_log["freeze_called"] = True
            return []

        monkeypatch.setattr(generate_sbom, "_run_pip_freeze", _fake_freeze)

        sbom = generate_sbom.generate_sbom(
            requirements_path=req_file,
            use_pip_freeze=False,
        )
        assert call_log["freeze_called"] is False
        assert sbom["package_count"] == 2


# ════════════════════════════════════════════════════════════════
# 4. CycloneDX 格式测试
# ════════════════════════════════════════════════════════════════

class TestCycloneDxFormat:
    """generate_sbom --format cyclonedx 输出 CycloneDX 兼容格式。"""

    def test_cyclonedx_structure(self, tmp_path, monkeypatch):
        """CycloneDX 格式包含 bomFormat / specVersion / components。"""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("httpx==0.27.2\n", encoding="utf-8")
        monkeypatch.setattr(
            generate_sbom, "_get_license_from_metadata",
            lambda name: ("BSD-3-Clause", "", ""),
        )
        monkeypatch.setattr(generate_sbom, "_run_pip_freeze", lambda: [])

        sbom = generate_sbom.generate_sbom(
            requirements_path=req_file,
            use_pip_freeze=False,
            output_format="cyclonedx",
        )
        assert sbom["bomFormat"] == "CycloneDX"
        assert sbom["specVersion"] == "1.5"
        assert len(sbom["components"]) == 1
        comp = sbom["components"][0]
        assert comp["type"] == "library"
        assert comp["name"] == "httpx"
        assert comp["version"] == "0.27.2"
        assert comp["purl"] == "pkg:pypi/httpx@0.27.2"

    def test_cyclonedx_with_hashes(self, tmp_path, monkeypatch):
        """CycloneDX 格式包含 sha256 hashes(当 --with-sha256 启用时)。"""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("httpx==0.27.2\n", encoding="utf-8")
        monkeypatch.setattr(
            generate_sbom, "_get_license_from_metadata",
            lambda name: ("MIT", "", ""),
        )
        monkeypatch.setattr(
            generate_sbom, "_get_sha256_from_pypi",
            lambda name, version: "deadbeefsha256",
        )
        monkeypatch.setattr(generate_sbom, "_run_pip_freeze", lambda: [])

        sbom = generate_sbom.generate_sbom(
            requirements_path=req_file,
            with_sha256=True,
            use_pip_freeze=False,
            output_format="cyclonedx",
        )
        comp = sbom["components"][0]
        assert len(comp["hashes"]) == 1
        assert comp["hashes"][0]["alg"] == "SHA-256"
        assert comp["hashes"][0]["content"] == "deadbeefsha256"


# ════════════════════════════════════════════════════════════════
# 5. main CLI 入口测试
# ════════════════════════════════════════════════════════════════

class TestMainCli:
    """main CLI 入口返回 0 并写入 SBOM 文件。"""

    def test_main_writes_sbom_file(self, tmp_path, monkeypatch):
        """main() 返回 0 并写入 SBOM JSON 文件。"""
        req_file = tmp_path / "requirements.txt"
        req_file.write_text("httpx==0.27.2\n", encoding="utf-8")
        out_file = tmp_path / "sbom.json"
        monkeypatch.setattr(
            generate_sbom, "_get_license_from_metadata",
            lambda name: ("MIT", "", ""),
        )
        monkeypatch.setattr(generate_sbom, "_run_pip_freeze", lambda: [])

        rc = generate_sbom.main([
            "--requirements", str(req_file),
            "--output", str(out_file),
            "--no-pip-freeze",
        ])
        assert rc == 0
        assert out_file.exists()
        sbom = json.loads(out_file.read_text(encoding="utf-8"))
        assert sbom["schema_version"] == "1.0"
        assert sbom["package_count"] == 1

    def test_main_nonexistent_requirements(self, tmp_path):
        """requirements.txt 不存在时返回 1。"""
        out_file = tmp_path / "sbom.json"
        rc = generate_sbom.main([
            "--requirements", str(tmp_path / "nonexistent.txt"),
            "--output", str(out_file),
        ])
        assert rc == 1


# ════════════════════════════════════════════════════════════════
# 6. verify_deps.sh 存在性测试
# ════════════════════════════════════════════════════════════════

class TestVerifyDepsScript:
    """verify_deps.sh 文件存在且非空。"""

    def test_verify_deps_sh_exists(self):
        """verify_deps.sh 文件存在。"""
        script_path = SCRIPTS_DIR / "verify_deps.sh"
        assert script_path.exists(), f"verify_deps.sh 不存在: {script_path}"

    def test_verify_deps_sh_nonempty(self):
        """verify_deps.sh 非空且包含核心校验逻辑。"""
        script_path = SCRIPTS_DIR / "verify_deps.sh"
        content = script_path.read_text(encoding="utf-8")
        assert len(content) > 100, "verify_deps.sh 内容过短"
        # 验证包含 pip show 校验逻辑
        assert "pip show" in content or "pip freeze" in content, \
            "verify_deps.sh 应包含 pip show 或 pip freeze 校验逻辑"
        # 验证包含 requirements.txt 引用
        assert "requirements.txt" in content or "requirements.lock" in content, \
            "verify_deps.sh 应引用 requirements.txt 或 requirements.lock"

    def test_verify_deps_sh_has_strict_mode(self):
        """verify_deps.sh 支持 --strict 参数。"""
        script_path = SCRIPTS_DIR / "verify_deps.sh"
        content = script_path.read_text(encoding="utf-8")
        assert "--strict" in content, "verify_deps.sh 应支持 --strict 参数"


# ════════════════════════════════════════════════════════════════
# 7. ci.yml SBOM 步骤检查
# ════════════════════════════════════════════════════════════════

class TestCiYmlSbomSteps:
    """ci.yml 包含 SBOM 生成 + cosign 签名步骤。"""

    def test_ci_yml_has_sbom_step(self):
        """ci.yml 包含 SBOM 生成步骤。"""
        ci_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
        if not ci_path.exists():
            pytest.skip("ci.yml 不存在(本地保留版本)")
        content = ci_path.read_text(encoding="utf-8")
        assert "generate_sbom" in content or "sbom" in content.lower(), \
            "ci.yml 应包含 SBOM 生成步骤"

    def test_ci_yml_has_cosign_step(self):
        """ci.yml 包含 cosign 镜像签名步骤。"""
        ci_path = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
        if not ci_path.exists():
            pytest.skip("ci.yml 不存在(本地保留版本)")
        content = ci_path.read_text(encoding="utf-8")
        assert "cosign" in content.lower(), \
            "ci.yml 应包含 cosign 签名步骤"
