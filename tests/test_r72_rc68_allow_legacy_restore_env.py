"""R72 RC68: compose run -e ALLOW_LEGACY_RESTORE=1 — 测试套件。

R72 RC68 整改背景:
    RC67 添加 -v 挂载 db_restore.py + -e APP_ENV=development 后,
    compose-runtime-e2e backup_restore 阶段 restore 步骤仍失败,错误:
      AppError: 旧直接 restore 写入器已被 capability-seal,生产入口必须通过
      RestoreOrchestrator 蓝绿切换路径(caller=run_restore, reason=legacy_writer_sealed)

    根因分析:
      1. docker-compose.prod.yml 为所有服务(db_writer 等)设置
         `ALLOW_LEGACY_RESTORE=`(空字符串)作为生产安全策略,
         防止逃生舱被意外启用(13 处,见 docker-compose.prod.yml 第 144/188/236/... 行)。
      2. db_restore.main() 中 `--target staging` 分支调用
         `os.environ.setdefault("ALLOW_LEGACY_RESTORE", "1")` 试图启用逃生舱。
      3. 但 `dict.setdefault(key, default)` 仅在 key 不存在时设置 default;
         compose 文件已将 ALLOW_LEGACY_RESTORE 设为空字符串(key 存在,value=""),
         setdefault 不会覆盖,导致 os.environ["ALLOW_LEGACY_RESTORE"] 仍为 ""。
      4. run_restore() capability-seal 检查(line 348):
           `os.environ.get("ALLOW_LEGACY_RESTORE", "").lower() not in ("1", "true", "yes")`
         空字符串 "" 不在白名单中,检查失败,抛 AppError(legacy_writer_sealed)。

RC68 修复:
      1. restore_cmd 显式添加 `-e ALLOW_LEGACY_RESTORE=1` 覆盖 compose 文件的
         空字符串值(docker compose run 的 -e 选项优先级高于 compose 文件
         的 environment 字段)。
      2. os.environ["ALLOW_LEGACY_RESTORE"] 在容器启动时即为 "1",
         main() 的 setdefault 不再需要触发(但保留作为 defense-in-depth)。
      3. capability-seal 检查通过,run_restore() 继续执行三段式恢复流程。

测试策略:
    - 字符串匹配验证 -e ALLOW_LEGACY_RESTORE=1 选项存在
    - 顺序验证 -e ALLOW_LEGACY_RESTORE=1 在 -e APP_ENV=development 之后、-v 之前
    - 验证 ALLOW_LEGACY_RESTORE 的值为 "1"(非空字符串)
    - 验证注释提及 RC68 修复及根因说明
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
# A. restore_cmd 必须包含 -e ALLOW_LEGACY_RESTORE=1
# ════════════════════════════════════════════════════════════════


class TestRestoreCmdHasAllowLegacyRestore:
    """R72 RC68 A: restore_cmd 必须包含 -e ALLOW_LEGACY_RESTORE=1 选项。"""

    def test_restore_cmd_has_allow_legacy_restore_flag(self):
        """restore_cmd 必须包含 -e ALLOW_LEGACY_RESTORE=1 选项。

        缺少此选项会导致 docker-compose.prod.yml 中设置的空字符串
        `ALLOW_LEGACY_RESTORE=` 被继承到容器环境,使 db_restore.main()
        的 setdefault 无法生效,run_restore() capability-seal 检查失败。
        """
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 restore_cmd = _compose_cmd(...) 定义"
        snippet = source[idx:idx + 800]
        assert "ALLOW_LEGACY_RESTORE=1" in snippet, (
            "R72 RC68: restore_cmd 必须包含 -e ALLOW_LEGACY_RESTORE=1 选项"
            "(覆盖 docker-compose.prod.yml 的空字符串),"
            f"实际片段: {snippet[:300]}"
        )

    def test_restore_cmd_has_e_flag_before_allow_legacy_restore(self):
        """restore_cmd 中 ALLOW_LEGACY_RESTORE=1 必须以 -e 标志引入。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        # 必须存在 "-e", "ALLOW_LEGACY_RESTORE=1" 的连续序列
        assert '"-e", "ALLOW_LEGACY_RESTORE=1"' in snippet, (
            "R72 RC68: restore_cmd 必须以 '-e', 'ALLOW_LEGACY_RESTORE=1' "
            "的形式设置环境变量(确保 docker compose run -e 选项正确解析),"
            f"实际片段: {snippet[:300]}"
        )

    def test_allow_legacy_restore_value_is_one(self):
        """ALLOW_LEGACY_RESTORE 的值必须是 "1",不能是空字符串或其他值。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        # 确保不是空字符串 ALLOW_LEGACY_RESTORE=
        assert '"ALLOW_LEGACY_RESTORE="' not in snippet, (
            "R72 RC68: restore_cmd 中 ALLOW_LEGACY_RESTORE 不能是空字符串,"
            "必须为 '1' 以通过 capability-seal 检查"
        )
        assert '"ALLOW_LEGACY_RESTORE=1"' in snippet, (
            "R72 RC68: restore_cmd 中 ALLOW_LEGACY_RESTORE 的值必须为 '1'"
        )


# ════════════════════════════════════════════════════════════════
# B. -e ALLOW_LEGACY_RESTORE=1 必须在正确位置
#    (在 -e APP_ENV=development 之后,在 -v 之前)
# ════════════════════════════════════════════════════════════════


class TestAllowLegacyRestorePosition:
    """R72 RC68 B: -e ALLOW_LEGACY_RESTORE=1 必须在 -e APP_ENV=development 之后、-v 之前。

    docker compose run [OPTIONS] SERVICE [COMMAND]...
    正确顺序: run --rm -T --no-deps --entrypoint python
              -e APP_ENV=development -e ALLOW_LEGACY_RESTORE=1
              -v <host>:<container>:ro db_writer ...
    """

    def test_allow_legacy_restore_after_app_env(self):
        """-e ALLOW_LEGACY_RESTORE=1 必须在 -e APP_ENV=development 之后。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        app_env_idx = snippet.find('"APP_ENV=development"')
        allow_legacy_idx = snippet.find('"ALLOW_LEGACY_RESTORE=1"')
        assert app_env_idx >= 0, "未找到 APP_ENV=development"
        assert allow_legacy_idx >= 0, "未找到 ALLOW_LEGACY_RESTORE=1"
        assert app_env_idx < allow_legacy_idx, (
            "R72 RC68: -e ALLOW_LEGACY_RESTORE=1 必须在 -e APP_ENV=development 之后,"
            f"实际 app_env_idx={app_env_idx} allow_legacy_idx={allow_legacy_idx}"
        )

    def test_allow_legacy_restore_before_volume(self):
        """-e ALLOW_LEGACY_RESTORE=1 必须在 -v 选项之前。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        allow_legacy_idx = snippet.find('"ALLOW_LEGACY_RESTORE=1"')
        # -v 选项以 '"-v"' 形式出现
        v_idx = snippet.find('"-v"', allow_legacy_idx)
        assert allow_legacy_idx >= 0
        assert v_idx >= 0, "未找到 -v 选项"
        assert allow_legacy_idx < v_idx, (
            "R72 RC68: -e ALLOW_LEGACY_RESTORE=1 必须在 -v 选项之前,"
            f"实际 allow_legacy_idx={allow_legacy_idx} v_idx={v_idx}"
        )

    def test_allow_legacy_restore_before_service_name(self):
        """-e ALLOW_LEGACY_RESTORE=1 必须在 service name (db_writer) 之前。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        allow_legacy_idx = snippet.find('"ALLOW_LEGACY_RESTORE=1"')
        svc_idx = snippet.find('"db_writer"', allow_legacy_idx)
        assert allow_legacy_idx >= 0
        assert svc_idx >= 0, "未找到 db_writer service name"
        assert allow_legacy_idx < svc_idx, (
            "R72 RC68: -e ALLOW_LEGACY_RESTORE=1 必须在 db_writer 之前,"
            f"实际 allow_legacy_idx={allow_legacy_idx} svc_idx={svc_idx}"
        )

    def test_full_option_order(self):
        """验证 restore_cmd 中所有 -e/-v 选项的完整顺序。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        # 定位所有关键选项
        run_idx = snippet.find('"run"')
        rm_idx = snippet.find('"--rm"', run_idx)
        t_idx = snippet.find('"-T"', rm_idx)
        no_deps_idx = snippet.find('"--no-deps"', t_idx)
        entrypoint_idx = snippet.find('"--entrypoint"', no_deps_idx)
        app_env_idx = snippet.find('"APP_ENV=development"', entrypoint_idx)
        allow_legacy_idx = snippet.find('"ALLOW_LEGACY_RESTORE=1"', app_env_idx)
        v_idx = snippet.find('"-v"', allow_legacy_idx)
        svc_idx = snippet.find('"db_writer"', v_idx)
        assert (
            run_idx < rm_idx < t_idx < no_deps_idx < entrypoint_idx
            < app_env_idx < allow_legacy_idx < v_idx < svc_idx
        ), (
            "R72 RC68: restore_cmd 选项顺序错误,应为 "
            "run < --rm < -T < --no-deps < --entrypoint < APP_ENV < "
            "ALLOW_LEGACY_RESTORE < -v < db_writer, "
            f"实际: run={run_idx} rm={rm_idx} T={t_idx} no_deps={no_deps_idx} "
            f"entrypoint={entrypoint_idx} app_env={app_env_idx} "
            f"allow_legacy={allow_legacy_idx} v={v_idx} svc={svc_idx}"
        )


# ════════════════════════════════════════════════════════════════
# C. backup_cmd 不应包含 ALLOW_LEGACY_RESTORE
#    (backup 不需要 restore 逃生舱)
# ════════════════════════════════════════════════════════════════


class TestBackupCmdDoesNotNeedAllowLegacyRestore:
    """R72 RC68 C: backup_cmd 不应包含 ALLOW_LEGACY_RESTORE。

    backup --once 走 db_backup.backup_once() 路径,不调用 run_restore(),
    无需 capability-seal 逃生舱。设置 ALLOW_LEGACY_RESTORE 仅对 restore 有意义。
    """

    def test_backup_cmd_does_not_have_allow_legacy_restore(self):
        """backup_cmd 不应包含 ALLOW_LEGACY_RESTORE 选项。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        assert "ALLOW_LEGACY_RESTORE" not in snippet, (
            "R72 RC68: backup_cmd 不应包含 ALLOW_LEGACY_RESTORE "
            "(backup --once 不调用 run_restore,无需逃生舱)"
        )


# ════════════════════════════════════════════════════════════════
# D. 注释中必须提及 RC68 修复及根因
# ════════════════════════════════════════════════════════════════


class TestRc68CommentPresent:
    """R72 RC68 D: restore_cmd 上方注释必须提及 RC68 修复及根因。"""

    def test_restore_cmd_comment_mentions_rc68(self):
        """restore_cmd 上方注释必须提及 RC68。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 restore_cmd 定义"
        # 取 restore_cmd 之前 4000 字符(容纳 RC63-RC68 多重注释叠加)
        before = source[max(0, idx - 4000):idx]
        assert "RC68" in before, (
            "R72 RC68: restore_cmd 上方注释必须提及 RC68 修复, "
            f"实际前 4000 字符: {before[-300:]}"
        )

    def test_restore_cmd_comment_mentions_docker_compose_empty_string(self):
        """注释必须解释 docker-compose.prod.yml 设置空字符串的根因。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        before = source[max(0, idx - 4000):idx]
        # 注释应提及 docker-compose.prod.yml 和空字符串
        assert "docker-compose.prod.yml" in before, (
            "R72 RC68: 注释必须提及 docker-compose.prod.yml 作为根因来源"
        )
        assert "空字符串" in before or "ALLOW_LEGACY_RESTORE=" in before, (
            "R72 RC68: 注释必须解释 compose 文件设置空字符串导致 setdefault 失效"
        )

    def test_restore_cmd_comment_mentions_setdefault(self):
        """注释必须提及 setdefault 语义无法覆盖现有 key 的根因。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        before = source[max(0, idx - 4000):idx]
        assert "setdefault" in before, (
            "R72 RC68: 注释必须提及 os.environ.setdefault 语义"
            "(仅在 key 不存在时设置,无法覆盖空字符串)"
        )

    def test_restore_cmd_comment_mentions_capability_seal(self):
        """注释必须提及 capability-seal 检查。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        before = source[max(0, idx - 4000):idx]
        assert "capability-seal" in before or "capability_seal" in before, (
            "R72 RC68: 注释必须提及 capability-seal 检查失败"
        )


# ════════════════════════════════════════════════════════════════
# E. restore_cmd 必须保留之前的所有修复(RC63/RC65/RC66/RC67)
# ════════════════════════════════════════════════════════════════


class TestRestoreCmdRetainsPreviousFixes:
    """R72 RC68 E: restore_cmd 必须保留 RC63-RC67 的所有修复。"""

    def test_retains_t_flag(self):
        """RC63: 必须保留 -T 选项(禁用 TTY 分配)。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        assert '"-T"' in snippet, "R72 RC68: restore_cmd 必须保留 -T 选项(RC63)"

    def test_retains_no_deps(self):
        """RC66: 必须保留 --no-deps 选项(跳过依赖检查)。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        assert '"--no-deps"' in snippet, "R72 RC68: restore_cmd 必须保留 --no-deps 选项(RC66)"

    def test_retains_entrypoint_python(self):
        """RC65: 必须保留 --entrypoint python 选项(绕过 entrypoint.py)。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        assert '"--entrypoint"' in snippet and '"python"' in snippet, (
            "R72 RC68: restore_cmd 必须保留 --entrypoint python 选项(RC65)"
        )

    def test_retains_app_env_development(self):
        """RC67: 必须保留 -e APP_ENV=development 选项(覆盖生产守卫)。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        assert '"APP_ENV=development"' in snippet, (
            "R72 RC68: restore_cmd 必须保留 -e APP_ENV=development 选项(RC67)"
        )

    def test_retains_volume_mount(self):
        """RC67: 必须保留 -v 挂载 db_restore.py 选项。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 800]
        assert '"-v"' in snippet, "R72 RC68: restore_cmd 必须保留 -v 选项(RC67)"
        assert "db_restore.py" in snippet, (
            "R72 RC68: restore_cmd 必须保留 -v 挂载 db_restore.py(RC67)"
        )


# ════════════════════════════════════════════════════════════════
# F. docker-compose.prod.yml 必须设置 ALLOW_LEGACY_RESTORE= (空字符串)
#    作为生产安全策略(验证根因仍然存在,确保 RC68 修复的必要性)
# ════════════════════════════════════════════════════════════════


class TestComposeProdYmlSetsEmptyAllowLegacyRestore:
    """R72 RC68 F: docker-compose.prod.yml 必须为 db_writer 设置 ALLOW_LEGACY_RESTORE=。

    这是 RC68 修复的根因来源 — compose 文件设置空字符串导致 setdefault 失效。
    验证此设置仍然存在,确保 RC68 修复的必要性和持续性。
    同时验证这是生产安全策略(不应被移除)。
    """

    def test_docker_compose_prod_yml_has_allow_legacy_restore_for_db_writer(self):
        """docker-compose.prod.yml 必须为 db_writer 设置 ALLOW_LEGACY_RESTORE=。"""
        compose_path = REPO_ROOT / "docker-compose.prod.yml"
        assert compose_path.exists(), "docker-compose.prod.yml 不存在"
        content = compose_path.read_text(encoding="utf-8")
        # 必须存在 ALLOW_LEGACY_RESTORE= (空字符串)
        assert "ALLOW_LEGACY_RESTORE=" in content, (
            "R72 RC68: docker-compose.prod.yml 必须设置 ALLOW_LEGACY_RESTORE= "
            "(空字符串,生产安全策略)"
        )

    def test_docker_compose_prod_yml_sets_empty_string_not_one(self):
        """docker-compose.prod.yml 的 ALLOW_LEGACY_RESTORE 必须为空字符串,不能为 1。"""
        compose_path = REPO_ROOT / "docker-compose.prod.yml"
        content = compose_path.read_text(encoding="utf-8")
        # 不能设置为 "1" 或 "true"(生产绝不应启用逃生舱)
        lines = content.splitlines()
        for line in lines:
            stripped = line.strip()
            if "ALLOW_LEGACY_RESTORE" in stripped:
                # 必须是 ALLOW_LEGACY_RESTORE= 或 ALLOW_LEGACY_RESTORE= 后跟注释
                # 不能是 ALLOW_LEGACY_RESTORE=1 或 ALLOW_LEGACY_RESTORE=true
                assert "ALLOW_LEGACY_RESTORE=1" not in stripped, (
                    f"R72 RC68: docker-compose.prod.yml 不应将 "
                    f"ALLOW_LEGACY_RESTORE 设为 1 (生产安全策略), 实际: {stripped}"
                )
                assert "ALLOW_LEGACY_RESTORE=true" not in stripped.lower(), (
                    f"R72 RC68: docker-compose.prod.yml 不应将 "
                    f"ALLOW_LEGACY_RESTORE 设为 true (生产安全策略), 实际: {stripped}"
                )
