"""R71 Wave 4: OCI rootfs 与 manifest 绑定验证 — 测试套件。

R71 报告 P0-12 指出:
    R70 Wave 8 的 generate_oci_file_manifest.py 只生成预期 manifest,
    没有在 CI 中验证最终镜像 rootfs 是否与 manifest 一致。
    攻击者/构建异常可能注入未声明文件,违反不可变部署原则。

R71 Wave 4 整改(P0-12, Commit 4):
    1. scripts/validate_oci_rootfs.py:
       - 在 CI runner 上(不在容器内)拉取镜像 by digest
       - docker save 到 tar, 解包提取 rootfs(组合所有 layer.tar)
       - 对比 manifest 中每个预期文件: path/type/mode/uid/gid/size/sha256
       - 区分 base image 文件与 app 文件(通过对比 base image rootfs)
       - fail-closed: 缺失/未声明/异常权限/越界 symlink 全部 FAIL
       - 输出 oci-file-manifest.json(绑定 source_sha + image_digest + base_image_digest)
    2. .github/workflows/release-gates.yml:
       - 新增 validate-oci-rootfs job(needs: docker-build, oci-allowlist-verify)
       - 添加到 release-summary required jobs
    3. 测试覆盖 40+ 用例(--from-tar 模式, 无需 Docker)

被测对象:
    - scripts/validate_oci_rootfs.py(主验证脚本)
    - .github/workflows/release-gates.yml(工作流门禁)

测试覆盖矩阵(42 个测试):
    A. 模块结构与数据类 — 5 个
    B. CLI 参数解析 — 4 个
    C. 有效 rootfs 验证 — 3 个
    D. 缺失文件验证 — 2 个
    E. 未声明文件验证 — 3 个
    F. 权限异常验证 — 4 个
    G. Symlink 逃逸验证 — 4 个
    H. Base vs App 文件区分 — 3 个
    I. sha256 验证 — 2 个
    J. 退出码 — 3 个
    K. Manifest 生成 — 3 个
    L. 不使用无限目录 allowlist — 2 个
    M. 从 tar 加载 rootfs — 2 个
    N. Docker 模式 monkeypatch — 2 个

测试策略:
    - 用 --from-tar / --base-tar 模式加载合成 tar(无需 Docker)
    - 用 monkeypatch 替换 _generate_expected_manifest 返回合成 manifest
    - 验证 exit code、manifest JSON 结构、fail-closed 行为
    - 严格遵守 R71 整改规范(无 TODO / pass / 占位符)
    - 测试在 Windows 无 Docker 环境下确定性运行
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "validate_oci_rootfs.py"
RELEASE_GATES_PATH = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"

# 测试用 image ref(包含有效 sha256: 64 hex)
TEST_IMAGE = "ghcr.io/test/tgjiema@sha256:" + "a" * 64
TEST_BASE_IMAGE = "python:3.12-slim@sha256:" + "b" * 64
TEST_SOURCE_SHA = "abc123def4567890abcdef1234567890abcdef12"


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
def vor():
    """加载 validate_oci_rootfs 模块(模块级缓存)。"""
    return _load_module_from_path("scripts.validate_oci_rootfs_r71w4", SCRIPT_PATH)


# ════════════════════════════════════════════════════════════════
# 辅助:合成 tar 创建
# ════════════════════════════════════════════════════════════════


def _create_flat_tar(
    tar_path: Path,
    files: dict[str, bytes] | None = None,
    modes: dict[str, int] | None = None,
    symlinks: dict[str, str] | None = None,
    uids: dict[str, int] | None = None,
    gids: dict[str, int] | None = None,
    dirs: dict[str, int] | None = None,
) -> None:
    """创建扁平 tar 文件(用于 --from-tar 测试)。

    Args:
        tar_path: 输出 tar 路径
        files: {path: content} 文件路径到内容的映射
        modes: {path: mode_int} 可选,文件权限(八进制整数,如 0o644)
        symlinks: {path: target} 可选,符号链接
        uids: {path: uid} 可选
        gids: {path: gid} 可选
        dirs: {dir_path: mode_int} 可选,显式目录权限
    """
    files = files or {}
    modes = modes or {}
    symlinks = symlinks or {}
    uids = uids or {}
    gids = gids or {}
    dirs = dirs or {}

    with tarfile.open(tar_path, "w") as tf:
        # 收集所有需要创建的目录(从文件路径推导 + 显式目录)
        all_paths = list(files.keys()) + list(symlinks.keys()) + list(dirs.keys())
        dir_set: set[str] = set()
        for path in all_paths:
            parts = path.split("/")
            for i in range(1, len(parts)):
                dir_set.add("/".join(parts[:i]))
        # 添加显式目录
        for d in dirs:
            dir_set.add(d)
        # 写入目录
        for d in sorted(dir_set):
            ti = tarfile.TarInfo(name=d)
            ti.type = tarfile.DIRTYPE
            ti.mode = dirs.get(d, modes.get(d, 0o755))
            ti.uid = uids.get(d, 0)
            ti.gid = gids.get(d, 0)
            ti.mtime = 0
            tf.addfile(ti)
        # 写入文件
        for path, content in files.items():
            ti = tarfile.TarInfo(name=path)
            ti.size = len(content)
            ti.mode = modes.get(path, 0o644)
            ti.uid = uids.get(path, 0)
            ti.gid = gids.get(path, 0)
            ti.mtime = 0
            tf.addfile(ti, io.BytesIO(content))
        # 写入符号链接
        for path, target in symlinks.items():
            ti = tarfile.TarInfo(name=path)
            ti.type = tarfile.SYMTYPE
            ti.linkname = target
            ti.mode = modes.get(path, 0o777)
            ti.uid = uids.get(path, 0)
            ti.gid = gids.get(path, 0)
            ti.mtime = 0
            tf.addfile(ti)


def _make_expected_manifest(files: list[dict]) -> dict:
    """构造 expected manifest 字典(模拟 generate_oci_file_manifest 输出)。

    files 中每个 dict 至少含 "path" 与 "size",可选 "sha256"。
    """
    return {
        "schema_version": "1.0",
        "generated_at": "2026-07-21T00:00:00",
        "tool_version": "R70-WAVE8-P0-09",
        "project": "tgjiema",
        "dockerfile": "Dockerfile",
        "dockerignore": ".dockerignore",
        "file_count": len(files),
        "files": files,
        "external_copies": [],
        "run_rm_paths": [],
        "dockerignore_rules": [],
        "allowed_roots": [
            "run_all.py", "requirements.txt", "services/", "bots/",
            "admin/", "config/", "database/", "locales/", "utils/",
            "storage/", "docker/", "venv/", "data/", "logs/",
        ],
    }


def _patch_expected_manifest(monkeypatch, module, files: list[dict]) -> dict:
    """Patch _generate_expected_manifest 返回合成 manifest。

    Returns:
        合成的 expected manifest 字典
    """
    expected = _make_expected_manifest(files)

    def mock_generate(*args, **kwargs):
        return expected

    monkeypatch.setattr(module, "_generate_expected_manifest", mock_generate)
    return expected


def _run_main(module, argv: list[str]) -> int:
    """调用 module.main(argv) 并返回退出码。"""
    return module.main(argv)


def _read_manifest(output_path: Path) -> dict:
    """读取 manifest JSON。"""
    return json.loads(output_path.read_text(encoding="utf-8"))


def _sha256_hex(content: bytes) -> str:
    """计算 bytes 内容的 sha256 hex。"""
    return hashlib.sha256(content).hexdigest()


# ════════════════════════════════════════════════════════════════
# A. 模块结构与数据类
# ════════════════════════════════════════════════════════════════


class TestModuleStructure:
    """R71 Wave 4 A: 模块结构与数据类定义。"""

    def test_script_file_exists(self):
        """scripts/validate_oci_rootfs.py 文件存在。"""
        assert SCRIPT_PATH.is_file(), (
            f"R71 Wave 4: 验证脚本不存在: {SCRIPT_PATH}"
        )

    def test_module_exposes_required_dataclasses(self, vor):
        """模块必须暴露 FileMetadata 与 ValidationReport 数据类。"""
        for cls_name in ("FileMetadata", "ValidationReport"):
            assert hasattr(vor, cls_name), (
                f"validate_oci_rootfs.py 必须暴露 {cls_name} 数据类"
            )

    def test_module_exposes_required_functions(self, vor):
        """模块必须暴露核心函数。"""
        required_funcs = [
            "main",
            "_validate_rootfs",
            "_extract_rootfs_from_flat_tar",
            "_extract_rootfs_from_docker_tar",
            "_classify_files",
            "_check_permission_anomaly",
            "_symlink_escapes_app",
            "_parse_image_ref",
            "_is_under_allowed_root",
        ]
        for func_name in required_funcs:
            assert hasattr(vor, func_name), (
                f"validate_oci_rootfs.py 必须暴露 {func_name}() 函数"
            )
            assert callable(getattr(vor, func_name)), (
                f"{func_name} 必须是可调用对象"
            )

    def test_module_exposes_required_constants(self, vor):
        """模块必须暴露退出码常量与 TOOL_VERSION。"""
        assert vor.TOOL_VERSION == "R71-WAVE4-P0-12"
        assert vor.EXIT_SUCCESS == 0
        assert vor.EXIT_VALIDATION_FAILURE == 1
        assert vor.EXIT_CLI_ERROR == 2
        assert vor.MANIFEST_SCHEMA_VERSION == "1.0"

    def test_file_metadata_has_required_fields(self, vor):
        """FileMetadata 必须包含 R71 Wave 4 必需字段。"""
        fields = {f.name for f in vor.FileMetadata.__dataclass_fields__.values()}
        required = {"path", "type", "mode", "uid", "gid", "size", "sha256", "link_target"}
        missing = required - fields
        assert not missing, (
            f"FileMetadata 缺少字段: {sorted(missing)}, "
            f"实际字段: {sorted(fields)}"
        )


# ════════════════════════════════════════════════════════════════
# B. CLI 参数解析
# ════════════════════════════════════════════════════════════════


class TestCLIArgumentParsing:
    """R71 Wave 4 B: CLI 参数解析。"""

    def test_missing_required_args_exits_2(self, vor, capsys):
        """缺少必需参数时 argparse 退出码为 2。"""
        with pytest.raises(SystemExit) as exc_info:
            vor.main([])
        assert exc_info.value.code == 2, (
            "缺少必需参数时 argparse 必须以 exit code 2 退出"
        )

    def test_invalid_image_ref_no_at_exits_2(
        self, vor, tmp_path, monkeypatch,
    ):
        """image ref 不含 @ → exit 2 (CLI 错误)。"""
        output = tmp_path / "manifest.json"
        rc = vor.main([
            "--image", "ghcr.io/test/repo:latest",  # 无 @sha256:
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(tmp_path / "nonexistent.tar"),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_CLI_ERROR, (
            "image ref 无 @sha256: digest 时必须返回 EXIT_CLI_ERROR(2)"
        )

    def test_invalid_image_ref_no_sha256_prefix_exits_2(
        self, vor, tmp_path,
    ):
        """digest 不以 sha256: 开头 → exit 2。"""
        output = tmp_path / "manifest.json"
        rc = vor.main([
            "--image", "ghcr.io/test/repo@md5:abc",
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(tmp_path / "nonexistent.tar"),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_CLI_ERROR

    def test_invalid_image_ref_short_digest_exits_2(
        self, vor, tmp_path,
    ):
        """digest 不是 64 位 hex → exit 2。"""
        output = tmp_path / "manifest.json"
        rc = vor.main([
            "--image", "ghcr.io/test/repo@sha256:abc",  # 太短
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(tmp_path / "nonexistent.tar"),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_CLI_ERROR


# ════════════════════════════════════════════════════════════════
# C. 有效 rootfs 验证
# ════════════════════════════════════════════════════════════════


class TestValidRootfsValidation:
    """R71 Wave 4 C: 有效 rootfs 通过验证。"""

    def test_valid_rootfs_passes(self, vor, tmp_path, monkeypatch):
        """所有预期文件存在且权限正常 → exit 0。"""
        # 准备合成 rootfs tar
        app_tar = tmp_path / "app.tar"
        files = {
            "app/run_all.py": b"# main entry\n",
            "app/services/__init__.py": b"",
            "app/services/restore_writer.py": b"# writer\n",
            "app/bots/__init__.py": b"",
            "app/bots/up_bot.py": b"# up bot\n",
        }
        _create_flat_tar(app_tar, files=files)
        # base tar 为空(无 /app 文件)
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        # 合成 expected manifest
        expected_files = [
            {"path": "run_all.py", "size": 14, "sha256": _sha256_hex(b"# main entry\n")},
            {"path": "services/restore_writer.py", "size": 9, "sha256": _sha256_hex(b"# writer\n")},
            {"path": "bots/up_bot.py", "size": 8, "sha256": _sha256_hex(b"# up bot\n")},
        ]
        _patch_expected_manifest(monkeypatch, vor, expected_files)
        # 运行
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_SUCCESS, (
            "有效 rootfs 必须通过验证(EXIT_SUCCESS=0)"
        )

    def test_valid_rootfs_writes_manifest(self, vor, tmp_path, monkeypatch):
        """验证通过后 manifest JSON 被正确写入。"""
        app_tar = tmp_path / "app.tar"
        files = {"app/run_all.py": b"main\n"}
        _create_flat_tar(app_tar, files=files)
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5, "sha256": _sha256_hex(b"main\n")},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert output.is_file(), "manifest JSON 文件必须被创建"
        manifest = _read_manifest(output)
        assert manifest["validation_passed"] is True

    def test_valid_rootfs_binds_source_sha_image_digest(
        self, vor, tmp_path, monkeypatch,
    ):
        """manifest 必须绑定 source_sha + image_digest + base_image_digest。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={"app/run_all.py": b"x"})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 1, "sha256": _sha256_hex(b"x")},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        assert manifest["source_sha"] == TEST_SOURCE_SHA
        assert manifest["image_digest"] == "sha256:" + "a" * 64
        assert manifest["image_repo_digest"] == TEST_IMAGE
        assert manifest["base_image_digest"] == "sha256:" + "b" * 64
        assert manifest["base_image_repo_digest"] == TEST_BASE_IMAGE


# ════════════════════════════════════════════════════════════════
# D. 缺失文件验证
# ════════════════════════════════════════════════════════════════


class TestMissingFileValidation:
    """R71 Wave 4 D: 缺失预期文件 → FAIL。"""

    def test_missing_expected_file_fails(self, vor, tmp_path, monkeypatch):
        """缺失预期文件 → exit 1 (验证失败)。"""
        app_tar = tmp_path / "app.tar"
        # 缺少 services/restore_writer.py
        _create_flat_tar(app_tar, files={"app/run_all.py": b"main\n"})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
            {"path": "services/restore_writer.py", "size": 100},  # 不存在于 tar
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_VALIDATION_FAILURE, (
            "缺失预期文件必须返回 EXIT_VALIDATION_FAILURE(1)"
        )

    def test_missing_file_recorded_in_manifest(self, vor, tmp_path, monkeypatch):
        """缺失文件必须记录在 manifest 的 missing_files 字段。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={"app/run_all.py": b"main\n"})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
            {"path": "services/missing.py", "size": 100},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        assert "services/missing.py" in manifest["missing_files"], (
            "missing_files 必须包含缺失的文件路径"
        )
        assert manifest["validation_passed"] is False


# ════════════════════════════════════════════════════════════════
# E. 未声明文件验证
# ════════════════════════════════════════════════════════════════


class TestUnexpectedAppFileValidation:
    """R71 Wave 4 E: 未声明 app 文件 → FAIL。"""

    def test_unexpected_app_file_fails(self, vor, tmp_path, monkeypatch):
        """不在 ALLOWED_ROOTS 下的 app 文件 → exit 1。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={
            "app/run_all.py": b"main\n",
            "app/unknown_top_level.txt": b"malicious\n",  # 不在白名单
        })
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_VALIDATION_FAILURE, (
            "未声明 app 文件必须返回 EXIT_VALIDATION_FAILURE"
        )

    def test_unexpected_app_file_recorded(self, vor, tmp_path, monkeypatch):
        """未声明文件必须记录在 unexpected_app_files 字段。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={
            "app/run_all.py": b"main\n",
            "app/strange_file.bin": b"data",  # 不在白名单
        })
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        assert "app/strange_file.bin" in manifest["unexpected_app_files"]

    def test_allowed_root_files_pass(self, vor, tmp_path, monkeypatch):
        """在 ALLOWED_ROOTS 下的文件(如 venv/)通过验证。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={
            "app/run_all.py": b"main\n",
            "app/venv/bin/python": b"binary",  # venv/ 在 ALLOWED_ROOTS
            "app/data/cache.db": b"db",         # data/ 在 ALLOWED_ROOTS
        })
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_SUCCESS, (
            "venv/ data/ logs/ 等允许根目录下的文件不应触发 unexpected"
        )


# ════════════════════════════════════════════════════════════════
# F. 权限异常验证
# ════════════════════════════════════════════════════════════════


class TestPermissionAnomalyValidation:
    """R71 Wave 4 F: 权限异常 → FAIL。"""

    def test_world_writable_file_fails(self, vor, tmp_path, monkeypatch):
        """world-writable 文件 → exit 1。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(
            app_tar,
            files={"app/run_all.py": b"main\n"},
            modes={"app/run_all.py": 0o666},  # world-writable
        )
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_VALIDATION_FAILURE
        manifest = _read_manifest(output)
        anomalies = manifest["permission_anomalies"]
        assert any("world-writable" in a["anomaly"] for a in anomalies), (
            "world-writable 必须记录在 permission_anomalies"
        )

    def test_setuid_file_fails(self, vor, tmp_path, monkeypatch):
        """setuid 文件 → exit 1。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(
            app_tar,
            files={"app/run_all.py": b"main\n"},
            modes={"app/run_all.py": 0o4755},  # setuid
        )
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_VALIDATION_FAILURE
        manifest = _read_manifest(output)
        anomalies = manifest["permission_anomalies"]
        assert any("setuid" in a["anomaly"] for a in anomalies)

    def test_setgid_file_fails(self, vor, tmp_path, monkeypatch):
        """setgid 文件 → exit 1。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(
            app_tar,
            files={"app/run_all.py": b"main\n"},
            modes={"app/run_all.py": 0o2755},  # setgid
        )
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_VALIDATION_FAILURE
        manifest = _read_manifest(output)
        anomalies = manifest["permission_anomalies"]
        assert any("setgid" in a["anomaly"] for a in anomalies)

    def test_normal_permissions_pass(self, vor, tmp_path, monkeypatch):
        """正常权限 0644/0755 → PASS。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(
            app_tar,
            files={
                "app/run_all.py": b"main\n",
                "app/services/init.py": b"",
            },
            modes={
                "app/run_all.py": 0o644,
                "app/services/init.py": 0o755,
            },
        )
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_SUCCESS


# ════════════════════════════════════════════════════════════════
# G. Symlink 逃逸验证
# ════════════════════════════════════════════════════════════════


class TestSymlinkEscapeValidation:
    """R71 Wave 4 G: symlink 逃逸 /app → FAIL。"""

    def test_symlink_to_outside_app_fails(self, vor, tmp_path, monkeypatch):
        """symlink → /etc/passwd → exit 1。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(
            app_tar,
            files={"app/run_all.py": b"main\n"},
            symlinks={"app/evil_link": "/etc/passwd"},
        )
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_VALIDATION_FAILURE
        manifest = _read_manifest(output)
        anomalies = manifest["permission_anomalies"]
        assert any("escapes /app" in a["anomaly"] for a in anomalies), (
            "symlink 逃逸 /app 必须记录在 permission_anomalies"
        )

    def test_symlink_to_app_passes(self, vor, tmp_path, monkeypatch):
        """symlink → /app/foo → PASS(不逃逸)。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(
            app_tar,
            files={"app/run_all.py": b"main\n", "app/target.py": b"target\n"},
            symlinks={"app/link": "/app/target.py"},
        )
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        # symlink 自身可能因不在 ALLOWED_ROOTS 而失败,但不会因 escapes 失败
        manifest = _read_manifest(output)
        anomalies = manifest["permission_anomalies"]
        assert not any("escapes /app" in a["anomaly"] for a in anomalies), (
            "指向 /app 的 symlink 不应触发 escape 异常"
        )

    def test_relative_symlink_escaping_fails(self, vor, tmp_path, monkeypatch):
        """相对 symlink → ../../etc → exit 1(逃逸 /app)。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(
            app_tar,
            files={"app/run_all.py": b"main\n"},
            symlinks={"app/services/evil": "../../etc/passwd"},
        )
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_VALIDATION_FAILURE
        manifest = _read_manifest(output)
        anomalies = manifest["permission_anomalies"]
        assert any("escapes /app" in a["anomaly"] for a in anomalies)

    def test_relative_symlink_within_app_passes(self, vor, tmp_path, monkeypatch):
        """相对 symlink → foo/bar → PASS(不逃逸)。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(
            app_tar,
            files={
                "app/run_all.py": b"main\n",
                "app/services/real.py": b"real\n",
            },
            symlinks={"app/services/link": "real.py"},  # 同目录相对链接
        )
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        anomalies = manifest["permission_anomalies"]
        assert not any("escapes /app" in a["anomaly"] for a in anomalies), (
            "指向 /app 内的相对 symlink 不应触发 escape"
        )


# ════════════════════════════════════════════════════════════════
# H. Base vs App 文件区分
# ════════════════════════════════════════════════════════════════


class TestBaseAppFileClassification:
    """R71 Wave 4 H: base image 文件与 app 文件区分。"""

    def test_base_files_distinguished_from_app_files(
        self, vor, tmp_path, monkeypatch,
    ):
        """base image 中已存在的文件(内容相同)被归类为 base_files。"""
        app_tar = tmp_path / "app.tar"
        base_tar = tmp_path / "base.tar"
        # base 与 app 都有 app/legacy.txt,内容相同
        shared_content = b"legacy data\n"
        _create_flat_tar(app_tar, files={
            "app/run_all.py": b"main\n",
            "app/venv/legacy.txt": shared_content,
        })
        _create_flat_tar(base_tar, files={
            "app/venv/legacy.txt": shared_content,
        })
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_SUCCESS
        manifest = _read_manifest(output)
        base_paths = [b["path"] for b in manifest["base_files"]]
        assert "app/venv/legacy.txt" in base_paths, (
            "base image 中已存在且内容相同的文件必须归类为 base_files"
        )
        # app_files 不应包含 legacy.txt
        app_paths = [a["path"] for a in manifest["app_files"]]
        assert "app/venv/legacy.txt" not in app_paths

    def test_modified_base_file_becomes_app_file(
        self, vor, tmp_path, monkeypatch,
    ):
        """base image 中存在但内容被修改的文件被归类为 app_files。"""
        app_tar = tmp_path / "app.tar"
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(app_tar, files={
            "app/run_all.py": b"main\n",
            "app/venv/modified.txt": b"modified content\n",
        })
        _create_flat_tar(base_tar, files={
            "app/venv/modified.txt": b"original content\n",
        })
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        app_paths = [a["path"] for a in manifest["app_files"]]
        assert "app/venv/modified.txt" in app_paths, (
            "内容被修改的 base 文件必须归类为 app_files"
        )

    def test_base_files_recorded_in_manifest(self, vor, tmp_path, monkeypatch):
        """base_files 字段记录所有 base 文件路径。"""
        app_tar = tmp_path / "app.tar"
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(app_tar, files={
            "app/run_all.py": b"main\n",
            "app/venv/shared.py": b"shared\n",
        })
        _create_flat_tar(base_tar, files={
            "app/venv/shared.py": b"shared\n",
        })
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        assert isinstance(manifest["base_files"], list)
        assert all(isinstance(b, dict) and "path" in b for b in manifest["base_files"])


# ════════════════════════════════════════════════════════════════
# I. sha256 验证
# ════════════════════════════════════════════════════════════════


class TestSha256Validation:
    """R71 Wave 4 I: sha256 不匹配 → FAIL。"""

    def test_tampered_content_sha256_mismatch_fails(
        self, vor, tmp_path, monkeypatch,
    ):
        """文件内容被篡改(sha256 不匹配)→ exit 1。"""
        app_tar = tmp_path / "app.tar"
        actual_content = b"actual content\n"
        _create_flat_tar(app_tar, files={"app/run_all.py": actual_content})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        # expected manifest 中的 sha256 是 "tampered content" 的(不匹配)
        wrong_sha = _sha256_hex(b"tampered content\n")
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": len(actual_content), "sha256": wrong_sha},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_VALIDATION_FAILURE, (
            "sha256 不匹配必须返回 EXIT_VALIDATION_FAILURE"
        )
        manifest = _read_manifest(output)
        anomalies = manifest["permission_anomalies"]
        assert any("sha256 mismatch" in a["anomaly"] for a in anomalies), (
            "sha256 不匹配必须记录在 permission_anomalies"
        )

    def test_correct_sha256_passes(self, vor, tmp_path, monkeypatch):
        """文件内容与 expected sha256 匹配 → PASS。"""
        app_tar = tmp_path / "app.tar"
        content = b"correct content\n"
        _create_flat_tar(app_tar, files={"app/run_all.py": content})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        correct_sha = _sha256_hex(content)
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": len(content), "sha256": correct_sha},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_SUCCESS


# ════════════════════════════════════════════════════════════════
# J. 退出码
# ════════════════════════════════════════════════════════════════


class TestExitCodes:
    """R71 Wave 4 J: 退出码语义。"""

    def test_exit_code_success_is_0(self, vor, tmp_path, monkeypatch):
        """成功验证 → exit 0。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={"app/run_all.py": b"main\n"})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == 0

    def test_exit_code_validation_failure_is_1(self, vor, tmp_path, monkeypatch):
        """验证失败 → exit 1。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={"app/run_all.py": b"main\n"})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
            {"path": "services/missing.py", "size": 100},  # 缺失
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == 1

    def test_exit_code_cli_error_is_2(self, vor, tmp_path):
        """CLI 错误(无效 image ref)→ exit 2。"""
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", "invalid-no-digest",
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(tmp_path / "x.tar"),
            "--output", str(output),
        ])
        assert rc == 2


# ════════════════════════════════════════════════════════════════
# K. Manifest 生成
# ════════════════════════════════════════════════════════════════


class TestManifestGeneration:
    """R71 Wave 4 K: manifest JSON 结构。"""

    def test_manifest_has_required_fields(self, vor, tmp_path, monkeypatch):
        """manifest 必须包含所有必需字段。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={"app/run_all.py": b"main\n"})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        required_fields = {
            "schema_version", "generated_at", "tool_version", "source_sha",
            "image_digest", "image_repo_digest", "base_image_digest",
            "base_image_repo_digest", "sbom_path", "provenance_path",
            "candidate_manifest_path", "validation_passed", "app_files",
            "base_files", "unexpected_app_files", "missing_files",
            "permission_anomalies", "error",
        }
        missing = required_fields - set(manifest.keys())
        assert not missing, (
            f"manifest 缺少字段: {sorted(missing)}"
        )

    def test_manifest_app_files_have_sha256(self, vor, tmp_path, monkeypatch):
        """app_files 中每个文件(非目录)必须有非空 sha256 字段。"""
        app_tar = tmp_path / "app.tar"
        content = b"main\n"
        _create_flat_tar(app_tar, files={"app/run_all.py": content})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        # 仅检查 file 类型条目(目录与 symlink 的 sha256 为空是正常的)
        file_entries = [f for f in manifest["app_files"] if f["type"] == "file"]
        assert len(file_entries) > 0, "app_files 必须包含至少一个文件条目"
        for f in file_entries:
            assert "sha256" in f, "每个 app_file(文件)必须有 sha256 字段"
            assert f["sha256"] != "", "文件 sha256 不得为空字符串"
            assert f["sha256"] == _sha256_hex(content), (
                "app_file 的 sha256 必须与文件内容匹配"
            )

    def test_manifest_validation_passed_flag(self, vor, tmp_path, monkeypatch):
        """validation_passed 标志正确反映验证结果。"""
        # 验证通过
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={"app/run_all.py": b"main\n"})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        assert manifest["validation_passed"] is True

        # 验证失败
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
            {"path": "missing.py", "size": 100},
        ])
        output2 = tmp_path / "manifest2.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output2),
        ])
        manifest2 = _read_manifest(output2)
        assert manifest2["validation_passed"] is False


# ════════════════════════════════════════════════════════════════
# L. 不使用无限目录 allowlist
# ════════════════════════════════════════════════════════════════


class TestNoInfiniteAllowlist:
    """R71 Wave 4 L: 不使用无限目录 allowlist 掩盖未知文件。"""

    def test_allowed_roots_is_finite_tuple(self, vor):
        """ALLOWED_ROOTS 必须是有限集合(非无限 allowlist)。"""
        from generate_oci_file_manifest import ALLOWED_ROOTS
        assert isinstance(ALLOWED_ROOTS, tuple), (
            "ALLOWED_ROOTS 必须是 tuple(有限白名单)"
        )
        assert len(ALLOWED_ROOTS) > 0, "ALLOWED_ROOTS 不能为空"
        assert len(ALLOWED_ROOTS) < 100, (
            "ALLOWED_ROOTS 必须是有限集合(少于 100 项), 不得使用无限 allowlist"
        )
        # 不允许包含通配符(通配符 = 无限 allowlist)
        for root in ALLOWED_ROOTS:
            assert "*" not in root, (
                f"ALLOWED_ROOTS 不允许通配符(无限 allowlist): {root}"
            )

    def test_unknown_top_level_file_fails(self, vor, tmp_path, monkeypatch):
        """不在 ALLOWED_ROOTS 的顶层文件 → FAIL(不被无限 allowlist 掩盖)。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={
            "app/run_all.py": b"main\n",
            "app/mystery_file": b"mystery\n",  # 顶层未知文件
        })
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_VALIDATION_FAILURE
        manifest = _read_manifest(output)
        assert "app/mystery_file" in manifest["unexpected_app_files"]


# ════════════════════════════════════════════════════════════════
# M. 从 tar 加载 rootfs
# ════════════════════════════════════════════════════════════════


class TestFromTarMode:
    """R71 Wave 4 M: --from-tar 模式加载扁平 tar。"""

    def test_from_tar_mode_loads_flat_tar(self, vor, tmp_path, monkeypatch):
        """--from-tar 正确加载扁平 tar 文件。"""
        app_tar = tmp_path / "app.tar"
        files = {
            "app/run_all.py": b"main\n",
            "app/services/foo.py": b"foo\n",
        }
        _create_flat_tar(app_tar, files=files)
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_SUCCESS
        manifest = _read_manifest(output)
        app_paths = [a["path"] for a in manifest["app_files"]]
        assert "app/run_all.py" in app_paths
        assert "app/services/foo.py" in app_paths

    def test_from_tar_missing_file_exits_2(self, vor, tmp_path, monkeypatch):
        """--from-tar 指向不存在的 tar → exit 2 (runtime 错误)。"""
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(tmp_path / "nonexistent.tar"),
            "--base-tar", str(tmp_path / "base-nonexistent.tar"),
            "--output", str(output),
        ])
        assert rc == vor.EXIT_CLI_ERROR, (
            "--from-tar 文件不存在时必须返回 EXIT_CLI_ERROR(2)"
        )


# ════════════════════════════════════════════════════════════════
# N. Docker 模式 monkeypatch
# ════════════════════════════════════════════════════════════════


class TestDockerModeMonkeypatch:
    """R71 Wave 4 N: Docker 模式(无 --from-tar)失败处理。"""

    def test_docker_pull_failure_exits_2(self, vor, tmp_path, monkeypatch):
        """docker pull 失败 → exit 2 (runtime 错误)。"""
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])

        def mock_pull(image_ref: str) -> None:
            raise RuntimeError(f"docker pull failed for {image_ref}")

        monkeypatch.setattr(vor, "_docker_pull", mock_pull)
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--output", str(output),
        ])
        assert rc == vor.EXIT_CLI_ERROR, (
            "docker pull 失败必须返回 EXIT_CLI_ERROR(2)"
        )
        manifest = _read_manifest(output)
        assert manifest["error"] is not None
        assert "extract rootfs failed" in manifest["error"]

    def test_docker_save_failure_exits_2(self, vor, tmp_path, monkeypatch):
        """docker save 失败 → exit 2 (runtime 错误)。"""
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])

        def mock_pull(image_ref: str) -> None:
            return  # pull 成功

        def mock_save(image_ref: str, output_path: Path) -> None:
            raise RuntimeError(f"docker save failed for {image_ref}")

        monkeypatch.setattr(vor, "_docker_pull", mock_pull)
        monkeypatch.setattr(vor, "_docker_save", mock_save)
        output = tmp_path / "manifest.json"
        rc = _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--output", str(output),
        ])
        assert rc == vor.EXIT_CLI_ERROR


# ════════════════════════════════════════════════════════════════
# O. release-gates.yml 门禁配置
# ════════════════════════════════════════════════════════════════


class TestReleaseGatesConfig:
    """R71 Wave 4 O: release-gates.yml 门禁配置。"""

    def test_release_gates_file_exists(self):
        """release-gates.yml 文件存在。"""
        assert RELEASE_GATES_PATH.is_file(), (
            f"release-gates.yml 不存在: {RELEASE_GATES_PATH}"
        )

    def test_validate_oci_rootfs_job_exists(self):
        """release-gates.yml 必须包含 validate-oci-rootfs job。"""
        content = RELEASE_GATES_PATH.read_text(encoding="utf-8")
        assert "  validate-oci-rootfs:" in content, (
            "release-gates.yml 必须定义 validate-oci-rootfs job"
        )

    def test_validate_oci_rootfs_needs_docker_build_and_allowlist(self):
        """validate-oci-rootfs 必须依赖 docker-build 与 oci-allowlist-verify。"""
        content = RELEASE_GATES_PATH.read_text(encoding="utf-8")
        # 查找 validate-oci-rootfs job 的 needs 行
        idx = content.find("  validate-oci-rootfs:")
        assert idx >= 0
        job_section = content[idx:idx + 2000]
        assert "needs: [docker-build, oci-allowlist-verify]" in job_section, (
            "validate-oci-rootfs 必须依赖 docker-build 与 oci-allowlist-verify"
        )

    def test_validate_oci_rootfs_in_release_summary_needs(self):
        """release-summary 的 needs 列表必须包含 validate-oci-rootfs。"""
        content = RELEASE_GATES_PATH.read_text(encoding="utf-8")
        idx = content.find("  release-summary:")
        assert idx >= 0
        summary_section = content[idx:idx + 3000]
        assert "validate-oci-rootfs" in summary_section, (
            "release-summary needs 列表必须包含 validate-oci-rootfs"
        )

    def test_validate_oci_rootfs_env_var_in_release_summary(self):
        """release-summary 必须设置 VALIDATE_OCI_ROOTFS 环境变量。"""
        content = RELEASE_GATES_PATH.read_text(encoding="utf-8")
        assert "VALIDATE_OCI_ROOTFS: ${{ needs.validate-oci-rootfs.result }}" in content, (
            "release-summary 必须设置 VALIDATE_OCI_ROOTFS 环境变量"
        )

    def test_validate_oci_rootfs_in_bash_check(self):
        """release-summary 的 bash 检查必须包含 validate-oci-rootfs。"""
        content = RELEASE_GATES_PATH.read_text(encoding="utf-8")
        assert '"validate-oci-rootfs=${VALIDATE_OCI_ROOTFS}"' in content, (
            "release-summary bash 检查必须包含 validate-oci-rootfs"
        )

    def test_validate_oci_rootfs_uploads_artifact(self):
        """validate-oci-rootfs 必须上传 oci-file-manifest artifact。"""
        content = RELEASE_GATES_PATH.read_text(encoding="utf-8")
        idx = content.find("  validate-oci-rootfs:")
        assert idx >= 0
        job_section = content[idx:idx + 5000]
        assert "oci-file-manifest" in job_section, (
            "validate-oci-rootfs 必须上传 oci-file-manifest artifact"
        )
        assert "if-no-files-found: error" in job_section, (
            "validate-oci-rootfs artifact 上传必须 fail-closed (if-no-files-found: error)"
        )

    def test_validate_oci_rootfs_no_continue_on_error(self):
        """validate-oci-rootfs 不得使用 continue-on-error(不得降级门禁)。"""
        content = RELEASE_GATES_PATH.read_text(encoding="utf-8")
        idx = content.find("  validate-oci-rootfs:")
        assert idx >= 0
        # 查找下一个 job 的起点
        next_job_idx = content.find("\n  # ───", idx + 1)
        if next_job_idx < 0:
            next_job_idx = len(content)
        job_section = content[idx:next_job_idx]
        assert "continue-on-error" not in job_section, (
            "validate-oci-rootfs 不得使用 continue-on-error(违反 R71 P0-12 不得降级门禁)"
        )


# ════════════════════════════════════════════════════════════════
# P. 可选参数 sbom/provenance/candidate-manifest
# ════════════════════════════════════════════════════════════════


class TestOptionalArgs:
    """R71 Wave 4 P: 可选参数 sbom/provenance/candidate-manifest。"""

    def test_sbom_path_recorded_in_manifest(self, vor, tmp_path, monkeypatch):
        """--sbom 路径记录在 manifest 的 sbom_path 字段。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={"app/run_all.py": b"main\n"})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        sbom_path = tmp_path / "sbom.json"
        sbom_path.write_text("{}", encoding="utf-8")
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--sbom", str(sbom_path),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        assert manifest["sbom_path"] == str(sbom_path)

    def test_provenance_path_recorded_in_manifest(
        self, vor, tmp_path, monkeypatch,
    ):
        """--provenance 路径记录在 manifest 的 provenance_path 字段。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={"app/run_all.py": b"main\n"})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        provenance_path = tmp_path / "provenance.json"
        provenance_path.write_text("{}", encoding="utf-8")
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--provenance", str(provenance_path),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        assert manifest["provenance_path"] == str(provenance_path)

    def test_optional_paths_default_null(self, vor, tmp_path, monkeypatch):
        """未提供可选参数时,sbom_path/provenance_path/candidate_manifest_path 为 null。"""
        app_tar = tmp_path / "app.tar"
        _create_flat_tar(app_tar, files={"app/run_all.py": b"main\n"})
        base_tar = tmp_path / "base.tar"
        _create_flat_tar(base_tar, files={})
        _patch_expected_manifest(monkeypatch, vor, [
            {"path": "run_all.py", "size": 5},
        ])
        output = tmp_path / "manifest.json"
        _run_main(vor, [
            "--image", TEST_IMAGE,
            "--source-sha", TEST_SOURCE_SHA,
            "--base-image", TEST_BASE_IMAGE,
            "--from-tar", str(app_tar),
            "--base-tar", str(base_tar),
            "--output", str(output),
        ])
        manifest = _read_manifest(output)
        assert manifest["sbom_path"] is None
        assert manifest["provenance_path"] is None
        assert manifest["candidate_manifest_path"] is None
        assert manifest["error"] is None
