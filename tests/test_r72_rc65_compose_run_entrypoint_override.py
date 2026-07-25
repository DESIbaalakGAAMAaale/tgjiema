"""R72 RC65: compose run --entrypoint python 覆盖修复 — 测试套件。

R72 RC65 整改背景:
    compose-runtime-e2e backup_restore 阶段持续 600s 超时,根因为:
      Dockerfile ENTRYPOINT 是 `python /app/docker/entrypoint.py`,
      当 `docker compose run --rm -T db_backup python -m services.db_backup
      backup --once ...` 执行时,容器实际运行:
        python /app/docker/entrypoint.py python -m services.db_backup backup --once ...

      entrypoint.py 读取 SERVICE_ROLE=db_backup,构造 cmd = ["python",
      "run_all.py", "--standalone", "db_backup"],然后把 sys.argv[1:]
      (即 "python -m services.db_backup backup --once ...")作为 extra_args
      追加到 cmd,最终 execvp 执行:
        python run_all.py --standalone db_backup python -m services.db_backup ...

      这导致 db_backup DAEMON 被启动(永不退出),而非一次性 backup --once CLI。
      600s 超时强杀,stdout 为空,无 evidence 输出。

RC65 修复:
      在 `docker compose run` 命令中添加 `--entrypoint python` 选项,
      覆盖 Dockerfile ENTRYPOINT,使容器直接执行:
        python -m services.db_backup backup --once --timeout 240 --output-json ...

      绕过 entrypoint.py 的角色映射 + readiness gate,直接运行 CLI。
      合理性:backup_restore 阶段在 health_check 之后执行,
      此时 db_backup 服务已通过 readiness gate,无需重复检查。

测试策略:
    - 字符串匹配验证 --entrypoint python 选项存在
    - 顺序验证 --entrypoint 在服务名之前
    - 验证不再使用 "python" 作为 COMMAND(docker compose run 的第一参数)
    - 严格遵守 R72 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_RUNTIME_E2E_PATH = REPO_ROOT / "scripts" / "compose_runtime_e2e.py"


def _read_source() -> str:
    """读取 compose_runtime_e2e.py 源码。"""
    return COMPOSE_RUNTIME_E2E_PATH.read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════
# A. compose run 命令必须包含 --entrypoint python
# ════════════════════════════════════════════════════════════════


class TestComposeRunHasEntrypointOverride:
    """R72 RC65 A: docker compose run 命令必须包含 --entrypoint python。"""

    def test_backup_cmd_has_entrypoint_python(self):
        """backup_cmd 必须包含 --entrypoint python 选项。

        缺少 --entrypoint python 会导致 entrypoint.py 接管,
        启动 db_backup DAEMON(永不退出),而非一次性 backup --once CLI。
        """
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 backup_cmd = _compose_cmd(...) 定义"
        snippet = source[idx:idx + 800]
        assert '"--entrypoint"' in snippet, (
            "R72 RC65: backup_cmd 必须包含 --entrypoint 选项,"
            f"实际片段: {snippet[:300]}"
        )
        assert '"python"' in snippet, (
            "R72 RC65: backup_cmd 的 --entrypoint 参数值必须为 python,"
            f"实际片段: {snippet[:300]}"
        )

    def test_restore_cmd_has_entrypoint_python(self):
        """restore_cmd 必须包含 --entrypoint python 选项。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 restore_cmd = _compose_cmd(...) 定义"
        snippet = source[idx:idx + 800]
        assert '"--entrypoint"' in snippet, (
            "R72 RC65: restore_cmd 必须包含 --entrypoint 选项,"
            f"实际片段: {snippet[:300]}"
        )
        assert '"python"' in snippet, (
            "R72 RC65: restore_cmd 的 --entrypoint 参数值必须为 python,"
            f"实际片段: {snippet[:300]}"
        )


# ════════════════════════════════════════════════════════════════
# B. --entrypoint python 必须在服务名之前
# ════════════════════════════════════════════════════════════════


class TestEntrypointBeforeServiceName:
    """R72 RC65 B: --entrypoint python 必须在服务名之前。

    docker compose run --rm -T --entrypoint python db_backup ... 是正确顺序。
    docker compose run --rm -T db_backup --entrypoint python ... 是错误顺序
    (--entrypoint 会被当作容器内命令参数)。
    """

    def test_backup_cmd_entrypoint_before_service(self):
        """backup_cmd 的 --entrypoint python 必须在 db_backup 之前。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        run_idx = snippet.find('"run"')
        rm_idx = snippet.find('"--rm"', run_idx)
        t_idx = snippet.find('"-T"', rm_idx)
        entrypoint_idx = snippet.find('"--entrypoint"', t_idx)
        python_idx = snippet.find('"python"', entrypoint_idx)
        svc_idx = snippet.find('"db_backup"', python_idx)
        assert run_idx < rm_idx < t_idx < entrypoint_idx < python_idx < svc_idx, (
            "R72 RC65: --entrypoint python 必须在 -T 之后、db_backup 之前,"
            f"实际顺序: run={run_idx} rm={rm_idx} T={t_idx} "
            f"entrypoint={entrypoint_idx} python={python_idx} svc={svc_idx}"
        )

    def test_restore_cmd_entrypoint_before_service(self):
        """restore_cmd 的 --entrypoint python 必须在 db_writer 之前。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        run_idx = snippet.find('"run"')
        rm_idx = snippet.find('"--rm"', run_idx)
        t_idx = snippet.find('"-T"', rm_idx)
        entrypoint_idx = snippet.find('"--entrypoint"', t_idx)
        python_idx = snippet.find('"python"', entrypoint_idx)
        svc_idx = snippet.find('"db_writer"', python_idx)
        assert run_idx < rm_idx < t_idx < entrypoint_idx < python_idx < svc_idx, (
            "R72 RC65: restore_cmd 的 --entrypoint python 必须在 -T 之后、"
            f"db_writer 之前,实际顺序: run={run_idx} rm={rm_idx} T={t_idx} "
            f"entrypoint={entrypoint_idx} python={python_idx} svc={svc_idx}"
        )


# ════════════════════════════════════════════════════════════════
# C. compose run 命令不应再使用 "python" 作为 COMMAND 参数
# ════════════════════════════════════════════════════════════════


class TestNoPythonAsCommandArg:
    """R72 RC65 C: compose run 命令不应再使用 "python" 作为 COMMAND 参数。

    旧写法:
        docker compose run --rm -T db_backup python -m services.db_backup ...
    此处 "python" 是 COMMAND,会被 entrypoint.py 当作 extra_args 追加。

    新写法:
        docker compose run --rm -T --entrypoint python db_backup -m services.db_backup ...
    此处 "python" 是 --entrypoint 的参数值,-m services.db_backup 是 COMMAND。
    """

    def test_backup_cmd_uses_m_flag_not_python_command(self):
        """backup_cmd 的 COMMAND 应为 -m services.db_backup,而非 python -m ...。

        --entrypoint python 已经指定了解释器,COMMAND 只需 -m services.db_backup。
        不应再在 COMMAND 位置写 "python"(会被 entrypoint.py 当作 extra_args)。
        """
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        # 找到 db_backup 服务名后的部分(即 COMMAND)
        svc_idx = snippet.find('"db_backup"')
        assert svc_idx >= 0, "未找到 db_backup 服务名"
        cmd_part = snippet[svc_idx + len('"db_backup"'):]
        # COMMAND 的第一个元素应该是 "-m",而不是 "python"
        # (因为 --entrypoint python 已经指定了解释器)
        m_idx = cmd_part.find('"-m"')
        python_idx = cmd_part.find('"python"')
        if m_idx >= 0 and python_idx >= 0:
            assert m_idx < python_idx, (
                "R72 RC65: backup_cmd COMMAND 应以 -m 开头(因 --entrypoint python "
                f"已指定解释器),不应再有 python 在 -m 之前。"
                f"m_idx={m_idx} python_idx={python_idx}"
            )
        # -m 必须存在(--entrypoint python + -m services.db_backup = python -m ...)
        assert m_idx >= 0, (
            "R72 RC65: backup_cmd 必须包含 -m services.db_backup 作为 COMMAND,"
            f"实际 COMMAND 部分: {cmd_part[:200]}"
        )

    def test_restore_cmd_uses_m_flag_not_python_command(self):
        """restore_cmd 的 COMMAND 应为 -m services.db_restore,而非 python -m ...。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        svc_idx = snippet.find('"db_writer"')
        assert svc_idx >= 0, "未找到 db_writer 服务名"
        cmd_part = snippet[svc_idx + len('"db_writer"'):]
        m_idx = cmd_part.find('"-m"')
        python_idx = cmd_part.find('"python"')
        if m_idx >= 0 and python_idx >= 0:
            assert m_idx < python_idx, (
                "R72 RC65: restore_cmd COMMAND 应以 -m 开头(因 --entrypoint python "
                f"已指定解释器),不应再有 python 在 -m 之前。"
                f"m_idx={m_idx} python_idx={python_idx}"
            )
        assert m_idx >= 0, (
            "R72 RC65: restore_cmd 必须包含 -m services.db_restore 作为 COMMAND,"
            f"实际 COMMAND 部分: {cmd_part[:200]}"
        )


# ════════════════════════════════════════════════════════════════
# D. --entrypoint 和 -T 选项的相对顺序
# ════════════════════════════════════════════════════════════════


class TestEntrypointAndTOrder:
    """R72 RC65 D: --entrypoint 和 -T 的顺序。

    docker compose run 的语法:
        docker compose run [OPTIONS] SERVICE [COMMAND] [ARGS...]

    --entrypoint 和 -T 都是 OPTIONS,必须在 SERVICE 之前。
    两者相对顺序不影响功能,但保持一致风格(-T 在 --entrypoint 之前)。
    """

    def test_backup_cmd_t_before_entrypoint(self):
        """backup_cmd 中 -T 应在 --entrypoint 之前(与 --rm 紧邻)。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        t_idx = snippet.find('"-T"')
        entrypoint_idx = snippet.find('"--entrypoint"')
        assert t_idx < entrypoint_idx, (
            "R72 RC65: -T 应在 --entrypoint 之前(保持 --rm -T --entrypoint 顺序),"
            f"实际: T={t_idx} entrypoint={entrypoint_idx}"
        )

    def test_restore_cmd_t_before_entrypoint(self):
        """restore_cmd 中 -T 应在 --entrypoint 之前。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        t_idx = snippet.find('"-T"')
        entrypoint_idx = snippet.find('"--entrypoint"')
        assert t_idx < entrypoint_idx, (
            "R72 RC65: restore_cmd 的 -T 应在 --entrypoint 之前,"
            f"实际: T={t_idx} entrypoint={entrypoint_idx}"
        )


# ════════════════════════════════════════════════════════════════
# E. 注释中必须提及 RC65 修复
# ════════════════════════════════════════════════════════════════


class TestRc65CommentPresent:
    """R72 RC65 E: backup_cmd 上方注释必须提及 RC65 修复。

    确保 RC65 修复有完整注释说明根因和修复方案,
    便于后续审计和防止回退。
    """

    def test_backup_cmd_comment_mentions_rc65(self):
        """backup_cmd 上方注释必须提及 RC65。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 backup_cmd 定义"
        # 取 backup_cmd 之前 2000 字符(注释区,容纳 RC60-RC66 多重注释叠加)
        before = source[max(0, idx - 2000):idx]
        assert "RC65" in before, (
            "R72 RC65: backup_cmd 上方注释必须提及 RC65 修复, "
            f"实际前 2000 字符: {before[-300:]}"
        )

    def test_restore_cmd_comment_mentions_rc65(self):
        """restore_cmd 上方注释必须提及 RC65。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 restore_cmd 定义"
        # 取 restore_cmd 之前 1200 字符(容纳 RC63/RC65/RC66 多重注释叠加)
        before = source[max(0, idx - 1200):idx]
        assert "RC65" in before, (
            "R72 RC65: restore_cmd 上方注释必须提及 RC65 修复, "
            f"实际前 1200 字符: {before[-300:]}"
        )

    def test_backup_cmd_comment_explains_entrypoint_issue(self):
        """backup_cmd 注释必须解释 entrypoint.py 问题。

        注释应说明:
        1. Dockerfile ENTRYPOINT 是 entrypoint.py
        2. entrypoint.py 会启动 daemon 而非 CLI
        3. --entrypoint python 覆盖修复
        """
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        before = source[max(0, idx - 2000):idx]
        # 注释应提及 entrypoint.py
        assert "entrypoint.py" in before or "ENTRYPOINT" in before, (
            "R72 RC65: backup_cmd 注释必须解释 entrypoint.py 问题,"
            f"实际注释: {before[-400:]}"
        )
        # 注释应提及 daemon 或 run_all.py
        assert "daemon" in before.lower() or "run_all" in before, (
            "R72 RC65: backup_cmd 注释必须说明 entrypoint.py 启动 daemon 的问题,"
            f"实际注释: {before[-400:]}"
        )
