"""R72 RC63: compose run -T 选项与容器内 evidence 路径修复 — 测试套件。

R72 RC63 整改背景:
    compose-runtime-e2e backup_restore 阶段持续 600s 超时,根因为:
      1. docker compose run --rm db_backup 缺少 -T 选项,在 GitHub Actions
         非 TTY 环境下会因等待 TTY 输入而无限挂起,导致编排器 600s 超时强杀,
         且 stdout/stderr 为空(无法定位失败原因)。
      2. --output-json 参数传递的是宿主机路径(REPO_ROOT/.tmp_backup_evidence_xxx.json),
         但 db_backup 容器 read_only: true,只挂载 ./data:/app/data,容器内看不到
         宿主机路径,导致 evidence 文件写入失败(OSError)。

RC63 修复:
      1. compose run 命令添加 -T 选项(与 compose exec -T 一致)
      2. evidence 文件路径改为容器内可写路径 /app/data/xxx.json
         (对应宿主机 REPO_ROOT/data/xxx.json,通过 ./data:/app/data 挂载映射)

测试策略:
    - AST 解析验证代码结构(不导入运行时模块)
    - 字符串匹配验证关键代码片段
    - 严格遵守 R72 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_RUNTIME_E2E_PATH = REPO_ROOT / "scripts" / "compose_runtime_e2e.py"


def _read_source() -> str:
    """读取 compose_runtime_e2e.py 源码。"""
    return COMPOSE_RUNTIME_E2E_PATH.read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════
# A. compose run 命令必须包含 -T 选项
# ════════════════════════════════════════════════════════════════


class TestComposeRunHasTtyDisable:
    """R72 RC63 A: docker compose run 命令必须包含 -T 选项。"""

    def test_backup_cmd_has_t_flag(self):
        """backup_cmd 必须包含 -T 选项(禁用 TTY 分配)。

        缺少 -T 在 GitHub Actions 非 TTY 环境下会导致 docker compose run
        无限挂起等待 TTY 输入,编排器 600s 超时强杀且无输出。
        """
        source = _read_source()
        # 查找 backup_cmd 定义附近 800 字符
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 backup_cmd = _compose_cmd(...) 定义"
        snippet = source[idx:idx + 800]
        assert '"-T"' in snippet or "'-T'" in snippet, (
            "R72 RC63: backup_cmd 必须包含 -T 选项(禁用 TTY 分配),"
            f"实际片段: {snippet[:300]}"
        )

    def test_restore_cmd_has_t_flag(self):
        """restore_cmd 必须包含 -T 选项(禁用 TTY 分配)。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 restore_cmd = _compose_cmd(...) 定义"
        snippet = source[idx:idx + 800]
        assert '"-T"' in snippet or "'-T'" in snippet, (
            "R72 RC63: restore_cmd 必须包含 -T 选项(禁用 TTY 分配),"
            f"实际片段: {snippet[:300]}"
        )

    def test_backup_cmd_t_flag_before_service_name(self):
        """-T 选项必须在服务名之前(否则会被当作服务参数)。

        docker compose run --rm -T db_backup ... 是正确顺序。
        docker compose run --rm db_backup -T ... 是错误顺序(-T 会被传给容器内命令)。
        """
        source = _read_source()
        idx = source.find('backup_cmd = _compose_cmd(')
        assert idx >= 0
        snippet = source[idx:idx + 800]
        # 查找 "run", "--rm", "-T", "db_backup" 的顺序
        run_idx = snippet.find('"run"')
        rm_idx = snippet.find('"--rm"', run_idx)
        t_idx = snippet.find('"-T"', rm_idx)
        svc_idx = snippet.find('"db_backup"', t_idx)
        assert run_idx < rm_idx < t_idx < svc_idx, (
            "R72 RC63: -T 必须在 --rm 之后、db_backup 之前,"
            f"实际顺序: run={run_idx} rm={rm_idx} T={t_idx} svc={svc_idx}"
        )

    def test_restore_cmd_t_flag_before_service_name(self):
        """restore_cmd 的 -T 选项也必须在服务名之前。"""
        source = _read_source()
        idx = source.find('restore_cmd = _compose_cmd(')
        assert idx >= 0
        snippet = source[idx:idx + 800]
        run_idx = snippet.find('"run"')
        rm_idx = snippet.find('"--rm"', run_idx)
        t_idx = snippet.find('"-T"', rm_idx)
        svc_idx = snippet.find('"db_writer"', t_idx)
        assert run_idx < rm_idx < t_idx < svc_idx, (
            "R72 RC63: restore_cmd 的 -T 必须在 --rm 之后、db_writer 之前,"
            f"实际顺序: run={run_idx} rm={rm_idx} T={t_idx} svc={svc_idx}"
        )


# ════════════════════════════════════════════════════════════════
# B. evidence 文件路径必须使用容器内可写路径
# ════════════════════════════════════════════════════════════════


class TestEvidencePathContainerWritable:
    """R72 RC63 B: evidence 文件路径必须使用容器内 /app/data/ 路径。

    db_backup 容器 read_only: true,只挂载 ./data:/app/data。
    传入宿主机路径会导致写入失败(OSError),evidence 文件无法被编排器读取。
    """

    def test_backup_evidence_container_path_defined(self):
        """必须定义 backup_evidence_container_path 变量。"""
        source = _read_source()
        assert "backup_evidence_container_path" in source, (
            "R72 RC63: 必须定义 backup_evidence_container_path 变量(容器内路径)"
        )

    def test_backup_evidence_container_path_uses_app_data(self):
        """backup_evidence_container_path 必须以 /app/data/ 开头。"""
        source = _read_source()
        # 查找 backup_evidence_container_path 赋值
        pattern = r'backup_evidence_container_path\s*=\s*f?["\'](/app/data/[^"\']+)["\']'
        match = re.search(pattern, source)
        assert match, (
            "R72 RC63: backup_evidence_container_path 必须以 /app/data/ 开头,"
            "确保容器内可写(通过 ./data:/app/data 挂载映射到宿主机)"
        )
        path = match.group(1)
        assert path.startswith("/app/data/"), (
            f"R72 RC63: backup_evidence_container_path 必须以 /app/data/ 开头,"
            f"实际: {path}"
        )

    def test_backup_evidence_host_path_uses_data_dir(self):
        """backup_evidence_path(宿主机路径)必须指向 REPO_ROOT/data/ 目录。

        容器内 /app/data/ 通过 ./data:/app/data 挂载映射到宿主机 REPO_ROOT/data/。
        """
        source = _read_source()
        # 查找 backup_evidence_path 赋值
        idx = source.find("backup_evidence_path = ")
        assert idx >= 0, "未找到 backup_evidence_path 赋值"
        snippet = source[idx:idx + 300]
        assert '"data"' in snippet or "'data'" in snippet, (
            "R72 RC63: backup_evidence_path 必须指向 REPO_ROOT/data/ 目录,"
            f"实际片段: {snippet[:200]}"
        )

    def test_backup_cmd_uses_container_path_not_host(self):
        """--output-json 参数必须使用容器内路径,而非宿主机路径。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        # 必须引用 backup_evidence_container_path,而非 backup_evidence_path
        assert "backup_evidence_container_path" in snippet, (
            "R72 RC63: --output-json 参数必须使用 backup_evidence_container_path(容器内路径)"
        )
        # 不能直接使用 backup_evidence_path(宿主机路径)作为 --output-json 参数
        # 排除变量定义和读取逻辑,只检查 --output-json 这一行
        output_json_idx = snippet.find('"--output-json"')
        assert output_json_idx >= 0
        # 取 --output-json 后 200 字符
        param_snippet = snippet[output_json_idx:output_json_idx + 200]
        assert "backup_evidence_container_path" in param_snippet, (
            "R72 RC63: --output-json 必须引用 backup_evidence_container_path,"
            f"实际片段: {param_snippet[:150]}"
        )

    def test_restore_evidence_container_path_defined(self):
        """必须定义 restore_evidence_container_path 变量。"""
        source = _read_source()
        assert "restore_evidence_container_path" in source, (
            "R72 RC63: 必须定义 restore_evidence_container_path 变量(容器内路径)"
        )

    def test_restore_evidence_container_path_uses_app_data(self):
        """restore_evidence_container_path 必须以 /app/data/ 开头。"""
        source = _read_source()
        pattern = r'restore_evidence_container_path\s*=\s*f?["\'](/app/data/[^"\']+)["\']'
        match = re.search(pattern, source)
        assert match, (
            "R72 RC63: restore_evidence_container_path 必须以 /app/data/ 开头"
        )
        path = match.group(1)
        assert path.startswith("/app/data/"), (
            f"R72 RC63: restore_evidence_container_path 必须以 /app/data/ 开头,"
            f"实际: {path}"
        )

    def test_restore_cmd_uses_container_path_not_host(self):
        """restore --output-json 参数必须使用容器内路径。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        output_json_idx = snippet.find('"--output-json"')
        assert output_json_idx >= 0
        param_snippet = snippet[output_json_idx:output_json_idx + 200]
        assert "restore_evidence_container_path" in param_snippet, (
            "R72 RC63: restore --output-json 必须引用 restore_evidence_container_path,"
            f"实际片段: {param_snippet[:150]}"
        )


# ════════════════════════════════════════════════════════════════
# C. 宿主机路径与容器内路径必须对应(通过 ./data:/app/data 挂载)
# ════════════════════════════════════════════════════════════════


class TestHostAndContainerPathCorrespond:
    """R72 RC63 C: 宿主机路径与容器内路径必须通过 ./data:/app/data 挂载对应。"""

    def test_backup_evidence_paths_correspond(self):
        """backup_evidence_path(宿主机)与 backup_evidence_container_path(容器)必须对应。

        宿主机 REPO_ROOT/data/backup_evidence_xxx.json
        容器   /app/data/backup_evidence_xxx.json
        通过 ./data:/app/data 挂载映射。
        """
        source = _read_source()
        # 提取宿主机路径模式
        host_pattern = r'backup_evidence_path\s*=\s*REPO_ROOT\s*/\s*"data"\s*/\s*f"backup_evidence_\{trace_id\}\.json"'
        assert re.search(host_pattern, source), (
            "R72 RC63: backup_evidence_path 必须为 REPO_ROOT / \"data\" / f\"backup_evidence_{trace_id}.json\""
        )
        # 提取容器路径模式
        container_pattern = r'backup_evidence_container_path\s*=\s*f"/app/data/backup_evidence_\{trace_id\}\.json"'
        assert re.search(container_pattern, source), (
            "R72 RC63: backup_evidence_container_path 必须为 /app/data/backup_evidence_{trace_id}.json"
        )

    def test_restore_evidence_paths_correspond(self):
        """restore_evidence_path(宿主机)与 restore_evidence_container_path(容器)必须对应。"""
        source = _read_source()
        host_pattern = r'restore_evidence_path\s*=\s*REPO_ROOT\s*/\s*"data"\s*/\s*f"restore_evidence_\{trace_id\}\.json"'
        assert re.search(host_pattern, source), (
            "R72 RC63: restore_evidence_path 必须为 REPO_ROOT / \"data\" / f\"restore_evidence_{trace_id}.json\""
        )
        container_pattern = r'restore_evidence_container_path\s*=\s*f"/app/data/restore_evidence_\{trace_id\}\.json"'
        assert re.search(container_pattern, source), (
            "R72 RC63: restore_evidence_container_path 必须为 /app/data/restore_evidence_{trace_id}.json"
        )


# ════════════════════════════════════════════════════════════════
# D. 编排器读取 evidence 必须从宿主机路径(而非容器路径)
# ════════════════════════════════════════════════════════════════


class TestOrchestratorReadsHostPath:
    """R72 RC63 D: 编排器读取 evidence 必须从宿主机路径。"""

    def test_backup_evidence_read_from_host_path(self):
        """backup_evidence_path.read_text() 必须使用宿主机路径。

        宿主机路径 REPO_ROOT/data/backup_evidence_xxx.json 通过 ./data:/app/data
        挂载,与容器内 /app/data/backup_evidence_xxx.json 是同一文件。
        """
        source = _read_source()
        # 查找 backup_evidence_path.read_text
        idx = source.find("backup_evidence_path.read_text")
        assert idx >= 0, "未找到 backup_evidence_path.read_text 调用"
        # 确认是 backup_evidence_path 而非 backup_evidence_container_path
        snippet = source[max(0, idx - 50):idx + 100]
        assert "backup_evidence_path.read_text" in snippet, (
            "R72 RC63: 必须从 backup_evidence_path(宿主机路径)读取 evidence"
        )

    def test_cleanup_uses_host_path(self):
        """清理逻辑必须使用宿主机路径(backup_evidence_path, restore_evidence_path)。"""
        source = _read_source()
        # 查找清理逻辑
        idx = source.find("for _tmp in (backup_evidence_path, restore_evidence_path)")
        assert idx >= 0, "未找到清理逻辑 for _tmp in (backup_evidence_path, restore_evidence_path)"
        # 确认使用宿主机路径变量
        snippet = source[idx:idx + 200]
        assert "backup_evidence_path" in snippet
        assert "restore_evidence_path" in snippet


# ════════════════════════════════════════════════════════════════
# E. docker-compose.prod.yml 必须挂载 ./data:/app/data
# ════════════════════════════════════════════════════════════════


class TestComposeFileMountsDataDir:
    """R72 RC63 E: docker-compose.prod.yml 必须为 db_backup 挂载 ./data:/app/data。"""

    def test_db_backup_mounts_data_dir(self):
        """db_backup 服务必须挂载 ./data:/app/data(否则容器内 /app/data 不可写)。"""
        compose_path = REPO_ROOT / "docker-compose.prod.yml"
        source = compose_path.read_text(encoding="utf-8")
        # 查找 db_backup 服务的 volumes 部分
        idx = source.find("  db_backup:")
        assert idx >= 0, "未找到 db_backup 服务定义"
        # 取 db_backup 服务定义后 1000 字符
        snippet = source[idx:idx + 1000]
        assert "./data:/app/data" in snippet, (
            "R72 RC63: db_backup 服务必须挂载 ./data:/app/data,"
            "否则容器内 /app/data 路径不可写,evidence 文件无法写入"
        )

    def test_db_writer_mounts_data_dir(self):
        """db_writer 服务也必须挂载 ./data:/app/data(restore 通过 db_writer 执行)。"""
        compose_path = REPO_ROOT / "docker-compose.prod.yml"
        source = compose_path.read_text(encoding="utf-8")
        idx = source.find("  db_writer:")
        assert idx >= 0, "未找到 db_writer 服务定义"
        snippet = source[idx:idx + 1000]
        assert "./data:/app/data" in snippet, (
            "R72 RC63: db_writer 服务必须挂载 ./data:/app/data,"
            "否则 restore evidence 文件无法写入"
        )


# ════════════════════════════════════════════════════════════════
# F. 不应再使用 .tmp_backup_evidence_ 或 .tmp_restore_evidence_ 前缀
# ════════════════════════════════════════════════════════════════


class TestNoOldTmpPrefixPaths:
    """R72 RC63 F: 不应再使用 .tmp_backup_evidence_ 或 .tmp_restore_evidence_ 前缀。

    旧路径(REPO_ROOT/.tmp_backup_evidence_xxx.json)在容器内不可见,
    必须改用 REPO_ROOT/data/backup_evidence_xxx.json(对应容器 /app/data/)。
    """

    def test_no_old_backup_evidence_tmp_path(self):
        """不应再使用 .tmp_backup_evidence_ 前缀作为 evidence 路径。"""
        source = _read_source()
        # 查找 .tmp_backup_evidence_(旧路径模式)
        # 排除注释中的引用(以 # 开头的行)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert ".tmp_backup_evidence_" not in stripped, (
                f"R72 RC63: 不应再使用 .tmp_backup_evidence_ 前缀(旧路径在容器内不可见),"
                f"实际行: {stripped}"
            )

    def test_no_old_restore_evidence_tmp_path(self):
        """不应再使用 .tmp_restore_evidence_ 前缀作为 evidence 路径。"""
        source = _read_source()
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert ".tmp_restore_evidence_" not in stripped, (
                f"R72 RC63: 不应再使用 .tmp_restore_evidence_ 前缀(旧路径在容器内不可见),"
                f"实际行: {stripped}"
            )
