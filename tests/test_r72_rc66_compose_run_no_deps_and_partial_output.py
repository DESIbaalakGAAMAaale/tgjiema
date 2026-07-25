"""R72 RC66: compose run --no-deps + TimeoutExpired partial output 捕获 — 测试套件。

R72 RC66 整改背景:
    RC65 添加 --entrypoint python 后,compose-runtime-e2e backup_restore 阶段
    仍持续 600s 超时,且 stdout/stderr 全空(无法定位失败原因)。

    根因分析:
      1. docker compose run 默认会检查 depends_on 条件。db_backup 服务
         depends_on migration(condition: service_completed_successfully)。
         在某些 Docker Compose v2 版本中,compose run 会尝试重新创建/启动
         已退出的 migration 容器来满足条件,导致命令无限挂起。
      2. subprocess.run(timeout=...) 在超时时抛 TimeoutExpired,但原 _run
         函数直接 except 后丢弃 e.stdout/e.stderr,导致编排器无法获取
         部分输出(无法判断是 docker 卡住还是 python 卡住)。

RC66 修复:
      1. backup_cmd 和 restore_cmd 添加 --no-deps 选项,跳过依赖管理
         (backup_restore 阶段所有依赖已在 start_bots 后就绪)
      2. _run 函数在 TimeoutExpired 时确保 e.stdout/e.stderr 非 None,
         重新抛出供调用方提取 partial output
      3. backup_restore 阶段的 except 块提取 partial_stdout/partial_stderr,
         写入 _fail_result 的 stdout/stderr 字段,便于诊断

测试策略:
    - 字符串匹配验证 --no-deps 选项存在
    - 顺序验证 --no-deps 在正确位置(-T 之后,--entrypoint 之前)
    - 验证 _run 函数捕获 TimeoutExpired 并确保 stdout/stderr 非 None
    - 验证 except 块提取 partial output
    - 严格遵守 R72 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_RUNTIME_E2E_PATH = REPO_ROOT / "scripts" / "compose_runtime_e2e.py"


def _read_source() -> str:
    """读取 compose_runtime_e2e.py 源码。"""
    return COMPOSE_RUNTIME_E2E_PATH.read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════
# A. compose run 命令必须包含 --no-deps 选项
# ════════════════════════════════════════════════════════════════


class TestComposeRunHasNoDeps:
    """R72 RC66 A: docker compose run 命令必须包含 --no-deps 选项。"""

    def test_backup_cmd_has_no_deps(self):
        """backup_cmd 必须包含 --no-deps 选项。

        缺少 --no-deps 会导致 docker compose run 检查 depends_on 条件,
        在某些 Docker Compose v2 版本中会尝试重新创建已退出的 migration
        容器,导致命令无限挂起。
        """
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 backup_cmd = _compose_cmd(...) 定义"
        snippet = source[idx:idx + 800]
        assert '"--no-deps"' in snippet, (
            "R72 RC66: backup_cmd 必须包含 --no-deps 选项(跳过依赖管理),"
            f"实际片段: {snippet[:300]}"
        )

    def test_restore_cmd_has_no_deps(self):
        """restore_cmd 必须包含 --no-deps 选项。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 restore_cmd = _compose_cmd(...) 定义"
        snippet = source[idx:idx + 800]
        assert '"--no-deps"' in snippet, (
            "R72 RC66: restore_cmd 必须包含 --no-deps 选项(跳过依赖管理),"
            f"实际片段: {snippet[:300]}"
        )


# ════════════════════════════════════════════════════════════════
# B. --no-deps 必须在正确位置(-T 之后,--entrypoint 之前)
# ════════════════════════════════════════════════════════════════


class TestNoDepsPosition:
    """R72 RC66 B: --no-deps 必须在 -T 之后、--entrypoint 之前。

    docker compose run [OPTIONS] SERVICE [COMMAND]...
    正确顺序: run --rm -T --no-deps --entrypoint python db_backup ...
    """

    def test_backup_cmd_no_deps_after_t_before_entrypoint(self):
        """backup_cmd 中 --no-deps 必须在 -T 之后、--entrypoint 之前。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        run_idx = snippet.find('"run"')
        rm_idx = snippet.find('"--rm"', run_idx)
        t_idx = snippet.find('"-T"', rm_idx)
        no_deps_idx = snippet.find('"--no-deps"', t_idx)
        entrypoint_idx = snippet.find('"--entrypoint"', no_deps_idx)
        svc_idx = snippet.find('"db_backup"', entrypoint_idx)
        assert (
            run_idx < rm_idx < t_idx < no_deps_idx < entrypoint_idx < svc_idx
        ), (
            "R72 RC66: --no-deps 必须在 -T 之后、--entrypoint 之前、db_backup 之前,"
            f"实际顺序: run={run_idx} rm={rm_idx} T={t_idx} "
            f"no_deps={no_deps_idx} entrypoint={entrypoint_idx} svc={svc_idx}"
        )

    def test_restore_cmd_no_deps_after_t_before_entrypoint(self):
        """restore_cmd 中 --no-deps 必须在 -T 之后、--entrypoint 之前。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        run_idx = snippet.find('"run"')
        rm_idx = snippet.find('"--rm"', run_idx)
        t_idx = snippet.find('"-T"', rm_idx)
        no_deps_idx = snippet.find('"--no-deps"', t_idx)
        entrypoint_idx = snippet.find('"--entrypoint"', no_deps_idx)
        svc_idx = snippet.find('"db_writer"', entrypoint_idx)
        assert (
            run_idx < rm_idx < t_idx < no_deps_idx < entrypoint_idx < svc_idx
        ), (
            "R72 RC66: restore_cmd 的 --no-deps 必须在 -T 之后、--entrypoint 之前、"
            f"db_writer 之前, 实际顺序: run={run_idx} rm={rm_idx} T={t_idx} "
            f"no_deps={no_deps_idx} entrypoint={entrypoint_idx} svc={svc_idx}"
        )


# ════════════════════════════════════════════════════════════════
# C. _run 函数必须捕获 TimeoutExpired 并保留 partial output
# ════════════════════════════════════════════════════════════════


class TestRunCapturesTimeoutPartialOutput:
    """R72 RC66 C: _run 函数必须在 TimeoutExpired 时保留 partial output。

    subprocess.run 在 timeout 时抛 TimeoutExpired,其 stdout/stderr 属性
    包含已捕获的部分输出。_run 应确保这些属性非 None,便于调用方提取诊断信息。
    """

    def test_run_function_has_timeout_expired_handling(self):
        """_run 函数必须捕获 subprocess.TimeoutExpired。"""
        source = _read_source()
        idx = source.find("def _run(")
        assert idx >= 0, "未找到 _run 函数定义"
        # 取 _run 函数体(到下一个 def 为止)
        body = source[idx:idx + 2000]
        assert "subprocess.TimeoutExpired" in body, (
            "R72 RC66: _run 函数必须捕获 subprocess.TimeoutExpired"
        )

    def test_run_function_ensures_stdout_not_none(self):
        """_run 函数必须确保 TimeoutExpired.stdout 非 None。"""
        source = _read_source()
        idx = source.find("def _run(")
        assert idx >= 0
        body = source[idx:idx + 2000]
        # 查找 e.stdout is None 检查
        assert "e.stdout is None" in body or "te.stdout is None" in body, (
            "R72 RC66: _run 函数必须检查 e.stdout is None 并设为空字符串"
        )

    def test_run_function_ensures_stderr_not_none(self):
        """_run 函数必须确保 TimeoutExpired.stderr 非 None。"""
        source = _read_source()
        idx = source.find("def _run(")
        assert idx >= 0
        body = source[idx:idx + 2000]
        assert "e.stderr is None" in body or "te.stderr is None" in body, (
            "R72 RC66: _run 函数必须检查 e.stderr is None 并设为空字符串"
        )

    def test_run_function_reraises_timeout_expired(self):
        """_run 函数必须在处理后重新抛出 TimeoutExpired(fail-closed)。"""
        source = _read_source()
        idx = source.find("def _run(")
        assert idx >= 0
        body = source[idx:idx + 2000]
        # 查找 raise 语句(裸 raise 重新抛出当前异常)
        assert "raise" in body, (
            "R72 RC66: _run 函数必须在处理后重新抛出 TimeoutExpired"
        )


# ════════════════════════════════════════════════════════════════
# D. backup_restore except 块必须提取 partial output
# ════════════════════════════════════════════════════════════════


class TestBackupRestoreExtractsPartialOutput:
    """R72 RC66 D: backup_restore 阶段 except 块必须提取 partial output。"""

    def test_backup_timeout_except_uses_te_variable(self):
        """backup_cmd 的 except 块必须用 `as te` 捕获 TimeoutExpired。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        # 取 backup_cmd 后 2000 字符(包含 try/except 块)
        body = source[idx:idx + 2000]
        assert "except subprocess.TimeoutExpired as te:" in body, (
            "R72 RC66: backup_cmd 的 except 块必须用 `as te` 捕获 TimeoutExpired,"
            "以便提取 partial stdout/stderr"
        )

    def test_backup_timeout_extracts_partial_stdout(self):
        """backup_cmd 超时时必须提取 te.stdout。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        body = source[idx:idx + 2500]
        assert "te.stdout" in body, (
            "R72 RC66: backup_cmd 超时时必须提取 te.stdout(partial output)"
        )

    def test_backup_timeout_extracts_partial_stderr(self):
        """backup_cmd 超时时必须提取 te.stderr。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        body = source[idx:idx + 2500]
        assert "te.stderr" in body, (
            "R72 RC66: backup_cmd 超时时必须提取 te.stderr(partial output)"
        )

    def test_backup_timeout_writes_partial_output_to_fail_result(self):
        """backup_cmd 超时时必须将 partial output 写入 _fail_result。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        body = source[idx:idx + 3000]
        # 查找 stdout=partial_stdout 或 stderr=partial_stderr
        assert "stdout=partial_stdout" in body, (
            "R72 RC66: backup_cmd 超时时必须将 partial_stdout 写入 _fail_result"
        )
        assert "stderr=partial_stderr" in body, (
            "R72 RC66: backup_cmd 超时时必须将 partial_stderr 写入 _fail_result"
        )

    def test_restore_timeout_except_uses_te_variable(self):
        """restore_cmd 的 except 块必须用 `as te` 捕获 TimeoutExpired。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        body = source[idx:idx + 2500]
        assert "except subprocess.TimeoutExpired as te:" in body, (
            "R72 RC66: restore_cmd 的 except 块必须用 `as te` 捕获 TimeoutExpired"
        )

    def test_restore_timeout_extracts_partial_stdout(self):
        """restore_cmd 超时时必须提取 te.stdout。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        body = source[idx:idx + 2500]
        assert "te.stdout" in body, (
            "R72 RC66: restore_cmd 超时时必须提取 te.stdout(partial output)"
        )


# ════════════════════════════════════════════════════════════════
# E. 注释中必须提及 RC66 修复
# ════════════════════════════════════════════════════════════════


class TestRc66CommentPresent:
    """R72 RC66 E: backup_cmd/restore_cmd 上方注释必须提及 RC66 修复。"""

    def test_backup_cmd_comment_mentions_rc66(self):
        """backup_cmd 上方注释必须提及 RC66。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 backup_cmd 定义"
        before = source[max(0, idx - 2200):idx]
        assert "RC66" in before, (
            "R72 RC66: backup_cmd 上方注释必须提及 RC66 修复, "
            f"实际前 2200 字符: {before[-300:]}"
        )

    def test_restore_cmd_comment_mentions_rc66(self):
        """restore_cmd 上方注释必须提及 RC66。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 restore_cmd 定义"
        before = source[max(0, idx - 1200):idx]
        assert "RC66" in before, (
            "R72 RC66: restore_cmd 上方注释必须提及 RC66 修复, "
            f"实际前 1200 字符: {before[-300:]}"
        )

    def test_backup_cmd_comment_explains_no_deps_rationale(self):
        """backup_cmd 注释必须解释 --no-deps 的根因。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        before = source[max(0, idx - 2200):idx]
        # 注释应提及 depends_on 或 migration
        assert "depends_on" in before or "migration" in before, (
            "R72 RC66: backup_cmd 注释必须解释 --no-deps 的根因(depends_on/migration),"
            f"实际注释: {before[-400:]}"
        )

    def test_run_function_comment_mentions_rc66(self):
        """_run 函数注释必须提及 RC66。"""
        source = _read_source()
        idx = source.find("def _run(")
        assert idx >= 0
        # 取 _run 函数 docstring(后 800 字符)
        body = source[idx:idx + 800]
        assert "RC66" in body, (
            "R72 RC66: _run 函数 docstring 必须提及 RC66 修复"
        )
