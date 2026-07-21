"""R70 Wave 8 P0-09: 精确 OCI 文件清单 manifest 生成器测试。

R70 P0-09 整改背景:
    生产镜像构建时没有精确文件清单,未知文件可进入镜像,违反不可变部署原则。
    scripts/generate_oci_file_manifest.py 通过解析 Dockerfile + .dockerignore,
    静态推导出预期的 /app 文件清单作为 SBOM。

测试覆盖矩阵:
    A. Dockerfile 解析正确性(COPY/ADD 指令)— 6 个
       1. 解析出所有本地 COPY 指令(run_all.py / services/ / bots/ 等)
       2. --from=builder 的 COPY 被识别为外部 stage
       3. COPY 指令的 dest_in_app() 标准化正确
       4. RUN rm 指令解析出 removed_paths
       5. find 命令被跳过(不误识别为 rm 目标)
       6. 多行续行正确合并
    B. .dockerignore 排除生效 — 6 个
       7. services/db_restore.py 被 .dockerignore 排除
       8. tests/ 目录下文件被排除
       9. scripts/ 目录下文件被排除
       10. *.pyc / __pycache__ 被排除
       11. *.md 被排除
       12. !.env.example 取反规则生效
    C. 关键文件必须在 manifest 中 — 4 个
       13. services/restore_writer.py 在 manifest 中
       14. config/environment.py 在 manifest 中
       15. docker/entrypoint.py 在 manifest 中
       16. run_all.py 在 manifest 中
    D. 关键文件/目录必不在 manifest 中 — 4 个
       17. services/db_restore.py 不在 manifest 中
       18. tests/ 下任何文件不在 manifest 中
       19. scripts/ 下任何文件不在 manifest 中
       20. docs/ 下任何文件不在 manifest 中
    E. --strict 模式 — 4 个
       21. 正常 manifest 通过 strict 验证
       22. 注入未知文件后 strict 模式 raise
       23. strict 模式 CLI 退出码为 1
       24. StrictModeViolation 错误信息含违规路径
    F. --validate 模式(stub)— 3 个
       25. 不存在的 tar 路径 raise FileNotFoundError
       26. 合法 tar 与 manifest 一致时返回 0
       27. tar 多余文件时返回 1
    G. manifest 结构完整性 — 4 个
       28. manifest 含 schema_version / tool_version / generated_at
       29. manifest file_count == len(files)
       30. 每个 file 条目含 path / size / source_instruction / copy_line_number
       31. manifest 含 external_copies / run_rm_paths / dockerignore_rules
"""
from __future__ import annotations

import json
import os
import sys
import tarfile
import io
from pathlib import Path

import pytest

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 导入被测模块
from scripts.generate_oci_file_manifest import (  # noqa: E402
    ALLOWED_ROOTS,
    DOCKERFILE_PATH,
    DOCKERIGNORE_PATH,
    CopyInstruction,
    FileEntry,
    RunRmInstruction,
    StrictModeViolation,
    TOOL_VERSION,
    _merge_continuation_lines,
    _parse_copy_instruction,
    _parse_run_rm,
    _tokenize_instruction,
    apply_dockerignore,
    apply_run_rm,
    expand_copy_to_files,
    generate_manifest,
    is_ignored_by_dockerignore,
    main,
    parse_dockerfile,
    parse_dockerignore,
    validate_against_image_tar,
    validate_strict,
)


# ════════════════════════════════════════════════════════════════
# 共享 fixture
# ════════════════════════════════════════════════════════════════


@pytest.fixture(scope="module")
def manifest() -> dict:
    """生成完整 manifest(模块级共享,避免重复计算)。"""
    return generate_manifest(
        repo_root=REPO_ROOT,
        dockerfile=DOCKERFILE_PATH,
        dockerignore=DOCKERIGNORE_PATH,
    )


@pytest.fixture(scope="module")
def manifest_paths(manifest: dict) -> set[str]:
    """manifest 中所有文件路径集合。"""
    return {e["path"] for e in manifest["files"]}


@pytest.fixture(scope="module")
def dockerfile_copies() -> list[CopyInstruction]:
    """Dockerfile 中所有 COPY/ADD 指令。"""
    copies, _ = parse_dockerfile(DOCKERFILE_PATH)
    return copies


@pytest.fixture(scope="module")
def dockerignore_rules() -> list[str]:
    """.dockerignore 规则列表。"""
    return parse_dockerignore(DOCKERIGNORE_PATH)


# ════════════════════════════════════════════════════════════════
# A. Dockerfile 解析正确性
# ════════════════════════════════════════════════════════════════


class TestDockerfileParsing:
    """R70 Wave 8: Dockerfile COPY/ADD 与 RUN rm 指令解析。"""

    def test_local_copy_instructions_parsed(self, dockerfile_copies):
        """Dockerfile 解析出所有本地 COPY 指令(不含 --from=builder)。

        根据 Dockerfile,本地 COPY 应包含:
          run_all.py / services/ / bots/ / admin/ / config/ / database/
          locales/ / utils/ / storage/ / docker/ / requirements.txt
        """
        local_copies = [c for c in dockerfile_copies if not c.is_from_external_stage]
        # 至少 11 条本地 COPY(Dockerfile 第 54-64 行)
        assert len(local_copies) >= 11, (
            f"R70 Wave 8: 应至少解析出 11 条本地 COPY 指令,实际 {len(local_copies)}"
        )
        # 验证关键 COPY 指令存在
        copy_sources = {src for c in local_copies for src in c.sources}
        assert "run_all.py" in copy_sources, "缺少 COPY run_all.py 指令"
        assert "services/" in copy_sources, "缺少 COPY services/ 指令"
        assert "docker/" in copy_sources, "缺少 COPY docker/ 指令"
        assert "config/" in copy_sources, "缺少 COPY config/ 指令"
        assert "requirements.txt" in copy_sources, "缺少 COPY requirements.txt 指令"

    def test_external_stage_copy_detected(self, dockerfile_copies):
        """--from=builder 的 COPY 被识别为外部 stage(不展开文件)。"""
        external = [c for c in dockerfile_copies if c.is_from_external_stage]
        assert len(external) >= 1, (
            "R70 Wave 8: 应识别出 --from=builder 的 COPY 指令"
        )
        # 验证 source_stage() 返回 "builder"
        stages = {c.source_stage() for c in external}
        assert "builder" in stages, (
            f"R70 Wave 8: 外部 stage 应为 'builder',实际 {stages}"
        )

    def test_dest_in_app_normalization(self):
        """CopyInstruction.dest_in_app() 正确标准化目标路径。"""
        # ./xxx → xxx
        c1 = CopyInstruction(
            raw="COPY run_all.py ./",
            instruction="COPY",
            flags=(),
            sources=("run_all.py",),
            dest="./",
            line_number=1,
        )
        assert c1.dest_in_app() == "", (
            f"./ 应标准化为空字符串,实际 {c1.dest_in_app()!r}"
        )

        # services/ ./services/ → services/
        c2 = CopyInstruction(
            raw="COPY services/ ./services/",
            instruction="COPY",
            flags=(),
            sources=("services/",),
            dest="./services/",
            line_number=2,
        )
        assert c2.dest_in_app() == "services/", (
            f"./services/ 应标准化为 services/,实际 {c2.dest_in_app()!r}"
        )

        # /app/venv → venv(绝对路径)
        c3 = CopyInstruction(
            raw="COPY --from=builder /app/venv /app/venv",
            instruction="COPY",
            flags=("--from=builder",),
            sources=("/app/venv",),
            dest="/app/venv",
            line_number=3,
            is_from_external_stage=True,
        )
        assert c3.dest_in_app() == "venv", (
            f"/app/venv 应标准化为 venv,实际 {c3.dest_in_app()!r}"
        )

    def test_run_rm_instruction_parsed(self):
        """RUN rm 指令解析出 removed_paths(含 services/db_restore.py)。"""
        _, rm_instructions = parse_dockerfile(DOCKERFILE_PATH)
        assert len(rm_instructions) >= 1, (
            "R70 Wave 8: Dockerfile 应至少含 1 条 RUN rm 指令"
        )
        # 收集所有 rm 的路径(标准化后)
        all_removed: list[str] = []
        for rm in rm_instructions:
            all_removed.extend(rm.removed_paths)
        # services/db_restore.py 必须在 rm 列表中(可能以 /app/ 前缀形式)
        has_db_restore = any("services/db_restore.py" in p for p in all_removed)
        assert has_db_restore, (
            "R70 Wave 8: RUN rm 应包含 services/db_restore.py 路径,"
            f"实际 removed_paths: {all_removed}"
        )
        # tests / scripts / docs 目录也应在 rm 列表中
        has_tests = any("/app/tests" in p for p in all_removed)
        has_scripts = any("/app/scripts" in p for p in all_removed)
        has_docs = any("/app/docs" in p for p in all_removed)
        assert has_tests, f"RUN rm 应包含 /app/tests,实际: {all_removed}"
        assert has_scripts, f"RUN rm 应包含 /app/scripts,实际: {all_removed}"
        assert has_docs, f"RUN rm 应包含 /app/docs,实际: {all_removed}"

    def test_find_command_skipped_in_rm_parse(self):
        """find ... -exec rm 命令被跳过(不误识别为 rm 目标)。"""
        # 构造含 find 的 RUN 命令
        raw = (
            "RUN rm -f /app/services/db_restore.py && "
            "find /app -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true"
        )
        rm = _parse_run_rm(raw, line_no=99)
        assert rm is not None, "应解析出至少一条 rm"
        # removed_paths 应只含 /app/services/db_restore.py,不含 find 的参数
        assert "/app/services/db_restore.py" in rm.removed_paths
        for p in rm.removed_paths:
            assert "find" not in p, f"find 参数不应出现在 removed_paths 中: {p}"
            assert "__pycache__" not in p, (
                f"find 的 __pycache__ 参数不应出现在 removed_paths 中: {p}"
            )

    def test_multiline_continuation_merged(self):
        """多行续行(以 \\ 结尾)正确合并为单条逻辑行。"""
        content = (
            "RUN rm -f /app/services/db_restore.py && \\\n"
            "    rm -rf /app/tests /app/scripts && \\\n"
            "    find /app -name __pycache__ -exec rm -rf {} +\n"
        )
        merged = _merge_continuation_lines(content)
        # 应合并为 1 条逻辑行(3 行续行)
        assert len(merged) == 1, (
            f"3 行续行应合并为 1 条逻辑行,实际 {len(merged)}"
        )
        line_no, logical = merged[0]
        assert line_no == 1
        # 合并后应含全部 3 个子命令
        assert "rm -f /app/services/db_restore.py" in logical
        assert "rm -rf /app/tests /app/scripts" in logical
        assert "find /app -name __pycache__" in logical
        # 不应含续行符 \
        assert "\\" not in logical, (
            f"合并后不应含续行符 \\,实际: {logical!r}"
        )


# ════════════════════════════════════════════════════════════════
# B. .dockerignore 排除生效
# ════════════════════════════════════════════════════════════════


class TestDockerignoreExclusion:
    """R70 Wave 8: .dockerignore 规则正确排除文件。"""

    def test_db_restore_excluded_by_dockerignore(self, dockerignore_rules):
        """services/db_restore.py 被 .dockerignore 排除。"""
        assert is_ignored_by_dockerignore(
            "services/db_restore.py", dockerignore_rules
        ), "services/db_restore.py 应被 .dockerignore 排除"

    def test_tests_dir_excluded_by_dockerignore(self, dockerignore_rules):
        """tests/ 目录下文件被 .dockerignore 排除。"""
        assert is_ignored_by_dockerignore(
            "tests/test_foo.py", dockerignore_rules
        ), "tests/test_foo.py 应被 .dockerignore 排除"
        assert is_ignored_by_dockerignore(
            "tests/subdir/bar.py", dockerignore_rules
        ), "tests/subdir/bar.py 应被 .dockerignore 排除"

    def test_scripts_dir_excluded_by_dockerignore(self, dockerignore_rules):
        """scripts/ 目录下文件被 .dockerignore 排除。"""
        assert is_ignored_by_dockerignore(
            "scripts/foo.py", dockerignore_rules
        ), "scripts/foo.py 应被 .dockerignore 排除"

    def test_pyc_pycache_excluded_by_dockerignore(self, dockerignore_rules):
        """*.pyc 与 __pycache__ 被排除。"""
        assert is_ignored_by_dockerignore(
            "services/foo.pyc", dockerignore_rules
        ), "services/foo.pyc 应被 *.pyc 规则排除"
        assert is_ignored_by_dockerignore(
            "services/__pycache__/foo.cpython-312.pyc", dockerignore_rules
        ), "services/__pycache__/... 应被 __pycache__ 规则排除"

    def test_md_files_excluded_by_dockerignore(self, dockerignore_rules):
        """*.md 文件被排除。"""
        assert is_ignored_by_dockerignore(
            "README.md", dockerignore_rules
        ), "README.md 应被 *.md 规则排除"
        assert is_ignored_by_dockerignore(
            "services/notes.md", dockerignore_rules
        ), "services/notes.md 应被 *.md 规则排除"

    def test_env_example_negation(self, dockerignore_rules):
        """!.env.example 取反规则:.env 本身被排除,但 .env.example 保留。"""
        assert is_ignored_by_dockerignore(
            ".env", dockerignore_rules
        ), ".env 应被 .env 规则排除"
        # .env.* 排除 .env.production,但 !.env.example 取反保留 .env.example
        assert not is_ignored_by_dockerignore(
            ".env.example", dockerignore_rules
        ), ".env.example 应被 !.env.example 取反规则保留"


# ════════════════════════════════════════════════════════════════
# C. 关键文件必须在 manifest 中
# ════════════════════════════════════════════════════════════════


class TestManifestRequiredFiles:
    """R70 Wave 8: 关键生产文件必须出现在 manifest 中。"""

    def test_restore_writer_in_manifest(self, manifest_paths):
        """services/restore_writer.py 必须在 manifest 中(生产 runtime 写入器)。"""
        assert "services/restore_writer.py" in manifest_paths, (
            "R70 Wave 8: services/restore_writer.py 必须在 manifest 中"
            "(R70 Wave 7 唯一 writer 实现,生产 runtime 必需)"
        )

    def test_environment_in_manifest(self, manifest_paths):
        """config/environment.py 必须在 manifest 中(APP_ENV 单一事实源)。"""
        assert "config/environment.py" in manifest_paths, (
            "R70 Wave 8: config/environment.py 必须在 manifest 中"
            "(R70 Wave 1 APP_ENV 单一事实源)"
        )

    def test_entrypoint_in_manifest(self, manifest_paths):
        """docker/entrypoint.py 必须在 manifest 中(R70 Wave 2 容器入口)。"""
        assert "docker/entrypoint.py" in manifest_paths, (
            "R70 Wave 8: docker/entrypoint.py 必须在 manifest 中"
            "(R70 Wave 2 正式 ENTRYPOINT)"
        )

    def test_run_all_in_manifest(self, manifest_paths):
        """run_all.py 必须在 manifest 中(应用入口)。"""
        assert "run_all.py" in manifest_paths, (
            "R70 Wave 8: run_all.py 必须在 manifest 中(应用入口)"
        )


# ════════════════════════════════════════════════════════════════
# D. 关键文件/目录必不在 manifest 中
# ════════════════════════════════════════════════════════════════


class TestManifestForbiddenFiles:
    """R70 Wave 8: 禁止文件不得出现在 manifest 中。"""

    def test_db_restore_not_in_manifest(self, manifest_paths):
        """services/db_restore.py 必不在 manifest 中(legacy restore CLI)。"""
        assert "services/db_restore.py" not in manifest_paths, (
            "R70 Wave 8: services/db_restore.py 不得在 manifest 中"
            "(.dockerignore + RUN rm 双重排除)"
        )

    def test_tests_dir_not_in_manifest(self, manifest_paths):
        """tests/ 目录下任何文件不得在 manifest 中。"""
        tests_files = [p for p in manifest_paths if p.startswith("tests/")]
        assert not tests_files, (
            f"R70 Wave 8: tests/ 下文件不得在 manifest 中,发现: {tests_files}"
        )

    def test_scripts_dir_not_in_manifest(self, manifest_paths):
        """scripts/ 目录下任何文件不得在 manifest 中。"""
        scripts_files = [p for p in manifest_paths if p.startswith("scripts/")]
        assert not scripts_files, (
            f"R70 Wave 8: scripts/ 下文件不得在 manifest 中,发现: {scripts_files}"
        )

    def test_docs_dir_not_in_manifest(self, manifest_paths):
        """docs/ 目录下任何文件不得在 manifest 中。"""
        docs_files = [p for p in manifest_paths if p.startswith("docs/")]
        assert not docs_files, (
            f"R70 Wave 8: docs/ 下文件不得在 manifest 中,发现: {docs_files}"
        )


# ════════════════════════════════════════════════════════════════
# E. --strict 模式
# ════════════════════════════════════════════════════════════════


class TestStrictMode:
    """R70 Wave 8: --strict 模式 fail-closed 行为。"""

    def test_normal_manifest_passes_strict(self, manifest):
        """正常 manifest(无未知文件)通过 strict 验证。"""
        # manifest 中所有文件应都在 ALLOWED_ROOTS 下
        validate_strict(manifest, ALLOWED_ROOTS)

    def test_strict_raises_on_unknown_file(self, manifest):
        """注入未知文件后 strict 模式 raise StrictModeViolation。"""
        polluted = json.loads(json.dumps(manifest))
        polluted["files"].append({
            "path": "unknown_root/evil.py",
            "size": 666,
            "source_instruction": "INJECTED",
            "copy_line_number": -1,
        })
        with pytest.raises(StrictModeViolation) as exc_info:
            validate_strict(polluted, ALLOWED_ROOTS)
        assert "unknown_root/evil.py" in str(exc_info.value), (
            "StrictModeViolation 错误信息应含违规路径 unknown_root/evil.py"
        )

    def test_strict_cli_exit_code_on_violation(self, manifest, tmp_path, monkeypatch):
        """--strict 模式 CLI 在违规时退出码为 1。

        构造一个临时 Dockerfile,其 COPY 指令复制一个不在 ALLOWED_ROOTS 的目录,
        验证 --strict 模式退出码为 1。
        """
        # 创建临时仓库结构
        tmp_repo = tmp_path / "repo"
        tmp_repo.mkdir()
        # 创建一个不在 ALLOWED_ROOTS 的目录与文件
        unknown_dir = tmp_repo / "unknown_root"
        unknown_dir.mkdir()
        (unknown_dir / "evil.py").write_text("# evil", encoding="utf-8")
        # 创建临时 Dockerfile
        tmp_dockerfile = tmp_repo / "Dockerfile"
        tmp_dockerfile.write_text(
            "FROM python:3.12-slim\n"
            "WORKDIR /app\n"
            "COPY unknown_root/ ./unknown_root/\n",
            encoding="utf-8",
        )
        # 临时 .dockerignore(空)
        tmp_dockerignore = tmp_repo / ".dockerignore"
        tmp_dockerignore.write_text("", encoding="utf-8")

        # 修改 REPO_ROOT 指向临时目录(通过 monkeypatch 模块的 REPO_ROOT)
        import scripts.generate_oci_file_manifest as mod
        monkeypatch.setattr(mod, "REPO_ROOT", tmp_repo)
        monkeypatch.setattr(mod, "DOCKERFILE_PATH", tmp_dockerfile)
        monkeypatch.setattr(mod, "DOCKERIGNORE_PATH", tmp_dockerignore)

        rc = main([
            "--dockerfile", str(tmp_dockerfile),
            "--dockerignore", str(tmp_dockerignore),
            "--strict",
            "--output", str(tmp_path / "out.json"),
        ])
        assert rc == 1, (
            "R70 Wave 8: --strict 模式遇到未知文件应返回退出码 1"
        )

    def test_strict_violation_message_contains_all_violations(self, manifest):
        """StrictModeViolation 错误信息包含所有违规路径(不截断)。"""
        polluted = json.loads(json.dumps(manifest))
        polluted["files"].extend([
            {"path": "unknown_a/foo.py", "size": 1, "source_instruction": "X",
             "copy_line_number": 1},
            {"path": "unknown_b/bar.py", "size": 2, "source_instruction": "Y",
             "copy_line_number": 2},
        ])
        with pytest.raises(StrictModeViolation) as exc_info:
            validate_strict(polluted, ALLOWED_ROOTS)
        msg = str(exc_info.value)
        assert "unknown_a/foo.py" in msg
        assert "unknown_b/bar.py" in msg


# ════════════════════════════════════════════════════════════════
# F. --validate 模式(stub)
# ════════════════════════════════════════════════════════════════


class TestValidateMode:
    """R70 Wave 8: --validate 模式与 tar 内容比对。"""

    def test_nonexistent_tar_raises(self, tmp_path):
        """不存在的 tar 路径 raise FileNotFoundError(fail-closed)。"""
        fake_tar = tmp_path / "nonexistent.tar"
        with pytest.raises(FileNotFoundError):
            validate_against_image_tar(fake_tar, {"files": []})

    def test_valid_tar_matches_manifest(self, tmp_path, manifest):
        """合法 tar 与 manifest 完全一致时返回 0。

        构造一个 tar,包含 manifest 中所有文件(以 app/ 前缀),
        验证 validate_against_image_tar 返回 0。
        """
        tar_path = tmp_path / "image.tar"
        # 构造 tar:每个 manifest 文件对应一个 app/<path> 条目
        with tarfile.open(tar_path, "w") as tf:
            for entry in manifest["files"]:
                content = b"# placeholder content"
                info = tarfile.TarInfo(name=f"app/{entry['path']}")
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
        rc = validate_against_image_tar(tar_path, manifest)
        assert rc == 0, (
            "R70 Wave 8: tar 与 manifest 完全一致时应返回 0"
        )

    def test_tar_with_extra_files_returns_1(self, tmp_path, manifest):
        """tar 多余文件时返回 1(fail-closed)。"""
        tar_path = tmp_path / "image.tar"
        with tarfile.open(tar_path, "w") as tf:
            # 添加 manifest 中的所有文件
            for entry in manifest["files"]:
                content = b"# placeholder"
                info = tarfile.TarInfo(name=f"app/{entry['path']}")
                info.size = len(content)
                tf.addfile(info, io.BytesIO(content))
            # 添加一个未声明的多余文件
            extra_content = b"# evil"
            extra_info = tarfile.TarInfo(name="app/unknown/evil.py")
            extra_info.size = len(extra_content)
            tf.addfile(extra_info, io.BytesIO(extra_content))
        rc = validate_against_image_tar(tar_path, manifest)
        assert rc == 1, (
            "R70 Wave 8: tar 含未声明文件时应返回 1(fail-closed)"
        )


# ════════════════════════════════════════════════════════════════
# G. manifest 结构完整性
# ════════════════════════════════════════════════════════════════


class TestManifestStructure:
    """R70 Wave 8: manifest JSON 结构完整性。"""

    def test_manifest_top_level_fields(self, manifest):
        """manifest 含必需顶层字段。"""
        required_fields = (
            "schema_version",
            "generated_at",
            "tool_version",
            "project",
            "dockerfile",
            "dockerignore",
            "file_count",
            "files",
            "external_copies",
            "run_rm_paths",
            "dockerignore_rules",
            "allowed_roots",
        )
        for field in required_fields:
            assert field in manifest, (
                f"R70 Wave 8: manifest 缺少必需字段 {field}"
            )
        assert manifest["schema_version"] == "1.0"
        assert manifest["tool_version"] == TOOL_VERSION
        assert manifest["project"] == "tgjiema"

    def test_file_count_matches_files_length(self, manifest):
        """manifest file_count == len(files)。"""
        assert manifest["file_count"] == len(manifest["files"]), (
            f"file_count({manifest['file_count']}) != len(files)"
            f"({len(manifest['files'])})"
        )

    def test_each_file_entry_has_required_fields(self, manifest):
        """每个 file 条目含 path / size / source_instruction / copy_line_number。"""
        required = ("path", "size", "source_instruction", "copy_line_number")
        for entry in manifest["files"]:
            for field in required:
                assert field in entry, (
                    f"R70 Wave 8: file 条目缺少字段 {field}: {entry}"
                )
            assert isinstance(entry["path"], str) and entry["path"]
            assert isinstance(entry["size"], int) and entry["size"] >= 0
            assert isinstance(entry["source_instruction"], str)
            assert entry["source_instruction"].startswith(("COPY", "ADD"))
            assert isinstance(entry["copy_line_number"], int)
            assert entry["copy_line_number"] > 0

    def test_manifest_has_external_copies_and_rm_paths(self, manifest):
        """manifest 含 external_copies(--from=builder)与 run_rm_paths。"""
        # external_copies 应至少含 1 条(--from=builder /app/venv)
        assert len(manifest["external_copies"]) >= 1, (
            "R70 Wave 8: manifest 应至少含 1 条 external_copy(--from=builder)"
        )
        ext = manifest["external_copies"][0]
        assert "instruction" in ext
        assert "line_number" in ext
        assert "dest" in ext
        assert "source_stage" in ext
        assert ext["source_stage"] == "builder"

        # run_rm_paths 应至少含 1 条(Dockerfile 的 RUN rm -f ...)
        assert len(manifest["run_rm_paths"]) >= 1, (
            "R70 Wave 8: manifest 应至少含 1 条 run_rm_paths"
        )
        # 收集所有 rm 的路径
        all_removed: list[str] = []
        for rm in manifest["run_rm_paths"]:
            all_removed.extend(rm["removed"])
        # services/db_restore.py 必须在 rm 列表中
        has_db_restore = any(
            "services/db_restore.py" in p for p in all_removed
        )
        assert has_db_restore, (
            "R70 Wave 8: run_rm_paths 应包含 services/db_restore.py"
        )


# ════════════════════════════════════════════════════════════════
# H. CLI 集成测试
# ════════════════════════════════════════════════════════════════


class TestCliIntegration:
    """R70 Wave 8: CLI 端到端集成测试。"""

    def test_cli_output_to_file(self, tmp_path):
        """CLI --output 写入 JSON 文件并可被 json.load 解析。"""
        out_path = tmp_path / "manifest.json"
        rc = main(["--output", str(out_path)])
        assert rc == 0, f"CLI 应返回 0,实际 {rc}"
        assert out_path.exists(), f"输出文件应存在: {out_path}"
        data = json.loads(out_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "files" in data
        assert data["file_count"] == len(data["files"])
        assert data["file_count"] > 0

    def test_cli_strict_passes_for_clean_dockerfile(self, tmp_path):
        """CLI --strict 对当前仓库 Dockerfile 应通过(退出码 0)。"""
        out_path = tmp_path / "manifest.json"
        rc = main(["--output", str(out_path), "--strict"])
        assert rc == 0, (
            "R70 Wave 8: 当前仓库 Dockerfile --strict 模式应通过(退出码 0)"
        )

    def test_cli_with_sha256(self, tmp_path):
        """CLI --with-sha256 为每个文件填充 sha256 字段。"""
        out_path = tmp_path / "manifest.json"
        rc = main(["--output", str(out_path), "--with-sha256"])
        assert rc == 0
        data = json.loads(out_path.read_text(encoding="utf-8"))
        files_with_sha = [f for f in data["files"] if f.get("sha256")]
        # 至少部分文件应有 sha256(排除二进制等可能失败的情况,但本实现不会失败)
        assert len(files_with_sha) > 0, (
            "R70 Wave 8: --with-sha256 应为至少部分文件填充 sha256"
        )
        # 验证 sha256 格式(64 位十六进制)
        for f in files_with_sha:
            assert len(f["sha256"]) == 64
            assert all(c in "0123456789abcdef" for c in f["sha256"])
