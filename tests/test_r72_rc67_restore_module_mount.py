"""R72 RC67: restore_cmd 挂载 db_restore.py + APP_ENV=development — 测试套件。

R72 RC67 整改背景:
    RC66 修复后 compose-runtime-e2e backup_restore 阶段不再 600s 超时,
    但 restore 步骤快速失败(6.5s):
      stderr: "/app/venv/bin/python: No module named services.db_restore"

    根因:Dockerfile 第 81 行 `RUN rm -f /app/services/db_restore.py` 物理删除
    db_restore.py(R69 P0-5 blocklist 第二道防线),
    且 .dockerignore 第 41 行也排除(R68 P0-07 第一道防线)。
    verify_oci_allowlist.py 强制要求这两处排除,不得修改。

    即使挂载 db_restore.py,_production_guard.assert_no_legacy_restore_in_production()
    也会因 APP_ENV=production(Dockerfile ENV 设置)无条件拒绝 legacy restore CLI
    (生产环境无逃生舱,即使 ALLOW_LEGACY_RESTORE=1 也不解封)。

RC67 修复:
    restore_cmd 添加两个选项:
      1. -v <host>/services/db_restore.py:/app/services/db_restore.py:ro
         只读挂载宿主机源码到容器内,绕过 Dockerfile 物理删除
         (不修改 Dockerfile/.dockerignore,保持 verify_oci_allowlist.py 通过)
      2. -e APP_ENV=development
         覆盖 Dockerfile ENV APP_ENV=production,使 _production_guard 通过
         (development 环境允许 ALLOW_LEGACY_RESTORE 逃生舱,
          --target staging 已在 db_restore.main() 内部 setdefault
          ALLOW_LEGACY_RESTORE=1)

    合理性:compose-runtime-e2e 是 CI 测试场景,不涉及生产数据
    (--target staging 恢复到隔离 staging 数据库)。

测试策略:
    - 字符串匹配验证 -e APP_ENV=development 选项存在
    - 字符串匹配验证 -v 挂载选项存在
    - 顺序验证 -e 和 -v 在 SERVICE 之前
    - 验证挂载目标路径为 /app/services/db_restore.py:ro
    - 验证挂载源路径使用 as_posix()(正斜杠,跨平台兼容)
    - 验证注释提及 RC67 修复说明
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
# A. restore_cmd 必须包含 -e APP_ENV=development
# ════════════════════════════════════════════════════════════════


class TestRestoreCmdHasAppEnvDev:
    """R72 RC67 A: restore_cmd 必须包含 -e APP_ENV=development。

    缺少此选项会导致 _production_guard 因 APP_ENV=production(Dockerfile ENV)
    无条件拒绝 legacy restore CLI(即使 ALLOW_LEGACY_RESTORE=1 也不解封)。
    """

    def test_restore_cmd_has_e_flag(self):
        """restore_cmd 必须包含 -e 选项。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 restore_cmd = _compose_cmd(...) 定义"
        snippet = source[idx:idx + 1200]
        assert '"-e"' in snippet, (
            "R72 RC67: restore_cmd 必须包含 -e 选项(覆盖 APP_ENV),"
            f"实际片段: {snippet[:400]}"
        )

    def test_restore_cmd_has_app_env_development(self):
        """restore_cmd 的 -e 参数值必须为 APP_ENV=development。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        assert "APP_ENV=development" in snippet, (
            "R72 RC67: restore_cmd 必须设置 APP_ENV=development 覆盖 Dockerfile "
            "ENV APP_ENV=production,使 _production_guard 通过"
            f"实际片段: {snippet[:400]}"
        )

    def test_restore_cmd_not_app_env_production(self):
        """restore_cmd 不得设置 APP_ENV=production(会被 _production_guard 拒绝)。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        # 不应在 restore_cmd 中设置 APP_ENV=production 或 APP_ENV=staging
        # (staging 也会被 _production_guard 拒绝)
        assert "APP_ENV=production" not in snippet, (
            "R72 RC67: restore_cmd 不得设置 APP_ENV=production(会被 _production_guard 拒绝)"
        )
        assert "APP_ENV=staging" not in snippet, (
            "R72 RC67: restore_cmd 不得设置 APP_ENV=staging(staging 也会被 _production_guard 拒绝)"
        )


# ════════════════════════════════════════════════════════════════
# B. restore_cmd 必须包含 -v 挂载 db_restore.py
# ════════════════════════════════════════════════════════════════


class TestRestoreCmdHasVolumeMount:
    """R72 RC67 B: restore_cmd 必须包含 -v 挂载 db_restore.py。

    Dockerfile 第 81 行物理删除了 /app/services/db_restore.py,
    必须通过 -v 挂载宿主机源码到容器内才能使用 services.db_restore 模块。
    """

    def test_restore_cmd_has_v_flag(self):
        """restore_cmd 必须包含 -v 选项。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        assert '"-v"' in snippet, (
            "R72 RC67: restore_cmd 必须包含 -v 选项(挂载 db_restore.py),"
            f"实际片段: {snippet[:400]}"
        )

    def test_restore_cmd_mounts_db_restore_py(self):
        """restore_cmd 的 -v 必须挂载 db_restore.py。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        assert "db_restore.py" in snippet, (
            "R72 RC67: restore_cmd 的 -v 必须挂载 db_restore.py 文件,"
            f"实际片段: {snippet[:400]}"
        )

    def test_restore_cmd_mount_target_is_app_services_db_restore_py(self):
        """-v 挂载目标必须是 /app/services/db_restore.py。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        assert "/app/services/db_restore.py" in snippet, (
            "R72 RC67: -v 挂载目标必须是 /app/services/db_restore.py"
            "(Dockerfile 删除的路径),"
            f"实际片段: {snippet[:400]}"
        )

    def test_restore_cmd_mount_is_read_only(self):
        """-v 挂载必须为只读(:ro)。

    只读挂载防止容器内意外修改宿主机源码。
    """
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        assert ":ro" in snippet, (
            "R72 RC67: -v 挂载必须为只读(:ro),"
            f"实际片段: {snippet[:400]}"
        )

    def test_restore_cmd_uses_as_posix_for_host_path(self):
        """宿主机路径必须使用 as_posix() 转换为正斜杠。

    Windows 上 Path 默认用反斜杠,Docker -v 选项需要正斜杠。
    as_posix() 在 Linux/Windows 上都返回正斜杠路径。
    """
        source = _read_source()
        # 查找 db_restore_host_path 定义
        idx = source.find("db_restore_host_path = ")
        assert idx >= 0, (
            "R72 RC67: 必须定义 db_restore_host_path 变量(使用 as_posix())"
        )
        snippet = source[idx:idx + 200]
        assert "as_posix()" in snippet, (
            "R72 RC67: db_restore_host_path 必须使用 as_posix() 转换路径,"
            "确保 Windows 上也使用正斜杠(Docker -v 选项要求)"
            f"实际片段: {snippet[:200]}"
        )


# ════════════════════════════════════════════════════════════════
# C. -e 和 -v 必须在 SERVICE 之前
# ════════════════════════════════════════════════════════════════


class TestEnvAndVolumeBeforeService:
    """R72 RC67 C: -e 和 -v 必须在 db_writer 服务名之前。

    docker compose run [OPTIONS] SERVICE [COMMAND]...
    -e 和 -v 都是 OPTIONS,必须在 SERVICE 之前。
    """

    def test_restore_cmd_e_before_service(self):
        """-e APP_ENV=development 必须在 db_writer 之前。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        e_idx = snippet.find('"-e"')
        svc_idx = snippet.find('"db_writer"')
        assert e_idx >= 0 and svc_idx >= 0 and e_idx < svc_idx, (
            "R72 RC67: -e 必须在 db_writer 之前,"
            f"实际: e={e_idx} svc={svc_idx}"
        )

    def test_restore_cmd_v_before_service(self):
        """-v 挂载必须在 db_writer 之前。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        v_idx = snippet.find('"-v"')
        svc_idx = snippet.find('"db_writer"')
        assert v_idx >= 0 and svc_idx >= 0 and v_idx < svc_idx, (
            "R72 RC67: -v 必须在 db_writer 之前,"
            f"实际: v={v_idx} svc={svc_idx}"
        )

    def test_restore_cmd_e_and_v_after_entrypoint(self):
        """-e 和 -v 必须在 --entrypoint python 之后(保持选项顺序)。

    顺序: run --rm -T --no-deps --entrypoint python -e ... -v ... db_writer ...
    """
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        entrypoint_idx = snippet.find('"--entrypoint"')
        python_idx = snippet.find('"python"', entrypoint_idx)
        e_idx = snippet.find('"-e"', python_idx)
        v_idx = snippet.find('"-v"', e_idx)
        svc_idx = snippet.find('"db_writer"', v_idx)
        assert (
            entrypoint_idx < python_idx < e_idx < v_idx < svc_idx
        ), (
            "R72 RC67: 选项顺序应为 --entrypoint python → -e → -v → db_writer,"
            f"实际: entrypoint={entrypoint_idx} python={python_idx} "
            f"e={e_idx} v={v_idx} svc={svc_idx}"
        )


# ════════════════════════════════════════════════════════════════
# D. backup_cmd 不需要 -v 挂载(db_backup.py 未被删除)
# ════════════════════════════════════════════════════════════════


class TestBackupCmdDoesNotNeedMount:
    """R72 RC67 D: backup_cmd 不需要 -v 挂载。

    db_backup.py 不在 R69 P0-5 blocklist 中(Dockerfile 未删除),
    容器内 /app/services/db_backup.py 存在,可直接 -m services.db_backup 调用。
    仅 db_restore.py 被 Dockerfile 物理删除,需要 -v 挂载。
    """

    def test_backup_cmd_does_not_mount_db_restore(self):
        """backup_cmd 不应挂载 db_restore.py(不需要)。"""
        source = _read_source()
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        # backup_cmd 不应包含 db_restore.py 挂载
        assert "db_restore.py" not in snippet or "-v" not in snippet, (
            "R72 RC67: backup_cmd 不应挂载 db_restore.py"
            "(db_backup.py 未被 Dockerfile 删除,不需要 -v 挂载)"
        )


# ════════════════════════════════════════════════════════════════
# E. 注释必须提及 RC67 修复
# ════════════════════════════════════════════════════════════════


class TestRc67CommentPresent:
    """R72 RC67 E: restore_cmd 注释必须提及 RC67 修复。

    确保 RC67 修复有完整注释说明根因和修复方案,
    便于后续审计和防止回退。
    """

    def test_restore_cmd_comment_mentions_rc67(self):
        """restore_cmd 上方注释必须提及 RC67。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 restore_cmd 定义"
        # 取 restore_cmd 之前 3000 字符(注释区,容纳 RC60-RC67 多重注释叠加)
        before = source[max(0, idx - 3000):idx]
        assert "RC67" in before, (
            "R72 RC67: restore_cmd 上方注释必须提及 RC67 修复, "
            f"实际前 3000 字符末尾: {before[-300:]}"
        )

    def test_restore_cmd_comment_explains_dockerfile_removal(self):
        """注释必须解释 Dockerfile 物理删除 db_restore.py 的问题。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        before = source[max(0, idx - 3000):idx]
        # 注释应提及 Dockerfile 或 RUN rm
        assert "Dockerfile" in before or "RUN rm" in before, (
            "R72 RC67: restore_cmd 注释必须解释 Dockerfile 物理删除问题,"
            f"实际注释末尾: {before[-400:]}"
        )
        # 注释应提及 verify_oci_allowlist
        assert "verify_oci_allowlist" in before, (
            "R72 RC67: restore_cmd 注释必须说明 verify_oci_allowlist 强制要求,"
            f"实际注释末尾: {before[-400:]}"
        )

    def test_restore_cmd_comment_explains_app_env_override(self):
        """注释必须解释 APP_ENV=development 覆盖的原因。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        before = source[max(0, idx - 3000):idx]
        # 注释应提及 _production_guard
        assert "_production_guard" in before, (
            "R72 RC67: restore_cmd 注释必须说明 _production_guard 守卫机制,"
            f"实际注释末尾: {before[-400:]}"
        )
        # 注释应提及 APP_ENV=development 覆盖
        assert "APP_ENV=development" in before, (
            "R72 RC67: restore_cmd 注释必须说明 APP_ENV=development 覆盖原因,"
            f"实际注释末尾: {before[-400:]}"
        )


# ════════════════════════════════════════════════════════════════
# F. restore_cmd 仍保留 RC65/RC66 的修复
# ════════════════════════════════════════════════════════════════


class TestRestoreCmdRetainsPreviousFixes:
    """R72 RC67 F: restore_cmd 必须保留 RC65/RC66 的修复。

    RC65: --entrypoint python 覆盖 Dockerfile ENTRYPOINT
    RC66: --no-deps 跳过依赖检查 + TimeoutExpired partial output 捕获
    RC67: -v 挂载 db_restore.py + -e APP_ENV=development
    """

    def test_restore_cmd_retains_entrypoint_python(self):
        """restore_cmd 仍保留 --entrypoint python(RC65)。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        assert '"--entrypoint"' in snippet
        assert '"python"' in snippet

    def test_restore_cmd_retains_no_deps(self):
        """restore_cmd 仍保留 --no-deps(RC66)。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        assert '"--no-deps"' in snippet

    def test_restore_cmd_retains_t_flag(self):
        """restore_cmd 仍保留 -T(RC63)。"""
        source = _read_source()
        idx = source.find("restore_cmd = _compose_cmd(")
        assert idx >= 0
        snippet = source[idx:idx + 1200]
        assert '"-T"' in snippet
