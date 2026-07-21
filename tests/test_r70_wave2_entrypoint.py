"""R70 Wave 2: 正式容器入口 — 测试 docker/entrypoint.py。

R70 P0-04 根因修复的回归测试:
    旧 Dockerfile 默认 CMD ["python", "run_all.py"] 在 APP_ENV=production 下
    会调用 run_all.py main(),而 main() 在生产环境下直接 exit 1(拒绝多进程模式),
    导致容器进入 restart loop。
    旧 Compose 通过 `command: python run_all.py --standalone up` 覆盖 CMD,
    但 systemd 部署又复制了启动命令,造成多份命令定义漂移风险。

R70 Wave 2 整改:
    建立唯一容器入口 docker/entrypoint.py,通过 SERVICE_ROLE 环境变量映射到
    唯一生产命令。Dockerfile / Compose / systemd 全部只注入 SERVICE_ROLE。

测试矩阵(对应 R70 Wave 2 §3-§10 要求):
    1. SERVICE_ROLE 显式枚举完整性(12 个角色)
    2. 每个 SERVICE_ROLE 映射到正确启动命令
    3. 未指定 SERVICE_ROLE + APP_ENV=production → exit 1
    4. 未指定 SERVICE_ROLE + APP_ENV=staging → exit 1
    5. 未指定 SERVICE_ROLE + APP_ENV=development → 回退 run_all.py
    6. 未知 SERVICE_ROLE → exit 1
    7. APP_ENV 缺失 → exit 2(config.environment fail-closed)
    8. APP_ENV 冲突 → exit 2
    9. _build_command 对所有角色返回有效 argv
    10. migration/prometheus_exporter 走独立模块入口(非 run_all.py)
"""
from __future__ import annotations

import os
import sys
import importlib.util
from pathlib import Path

import pytest


# ──────────────────────────────────────────────────────────────────
# 测试隔离:直接加载 docker.entrypoint 模块,绕过 config/__init__.py
# ──────────────────────────────────────────────────────────────────
def _load_entrypoint_module():
    """直接加载 docker.entrypoint 模块,不触发 config/__init__.py。"""
    if "docker.entrypoint" in sys.modules and hasattr(
        sys.modules["docker.entrypoint"], "main"
    ):
        return sys.modules["docker.entrypoint"]

    # 先确保 config.environment 可加载(不触发 config/__init__.py)
    if "config.environment" not in sys.modules or not hasattr(
        sys.modules.get("config.environment", None), "parse_app_env"
    ):
        env_path = Path(__file__).resolve().parent.parent / "config" / "environment.py"
        spec = importlib.util.spec_from_file_location("config.environment", env_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules["config.environment"] = module
        spec.loader.exec_module(module)

    # 构造独立的 docker 包占位对象(不执行 __init__.py)
    if "docker" not in sys.modules or not hasattr(sys.modules["docker"], "__path__"):
        docker_pkg = type(sys)("docker")
        docker_pkg.__path__ = [str(Path(__file__).resolve().parent.parent / "docker")]
        sys.modules["docker"] = docker_pkg

    entry_path = Path(__file__).resolve().parent.parent / "docker" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("docker.entrypoint", entry_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["docker.entrypoint"] = module
    spec.loader.exec_module(module)
    return sys.modules["docker.entrypoint"]


@pytest.fixture
def entry_module():
    """提供 docker.entrypoint 模块实例。"""
    return _load_entrypoint_module()


@pytest.fixture
def clean_env(monkeypatch):
    """清理环境变量,确保测试隔离。

    R70 Wave 3: 同时清理所有逃生舱变量,因为 conftest.py 的
    allow_legacy_restore_writer autouse fixture 会设置 ALLOW_LEGACY_RESTORE=1,
    这会导致 entrypoint 的 escape_hatch_guard 在 production 测试场景下提前触发
    (exit 3 而非 exit 1),干扰 Wave 2 测试的 fail-closed 行为验证。
    """
    for var in ("APP_ENV", "ENVIRONMENT", "DEPLOY_ENV", "SERVICE_ROLE"):
        monkeypatch.delenv(var, raising=False)
    # R70 Wave 3: 清理所有逃生舱变量(与 escape_hatch_guard.ESCAPE_HATCH_REGISTRY 一致)
    for var in (
        "I18N_ALLOW_FALLBACK",
        "ALLOW_LEGACY_RESTORE",
        "TEST_ONLY",
        "DEV_ONLY",
        "BYPASS",
        "SKIP_VERIFY",
        "SKIP_VALIDATION",
        "ALLOW_INSECURE",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ══════════════════════════════════════════════════════════════════
# 测试 1:SERVICE_ROLE 显式枚举完整性
# ══════════════════════════════════════════════════════════════════
def test_service_role_enum_completeness(entry_module):
    """SERVICE_ROLE 枚举必须包含 R70 Wave 2 §3 要求的全部 12 个角色。"""
    required_roles = {
        "up", "idx", "dsp", "mon", "admin", "admin_bot",
        "db_writer", "crdb_sync", "db_backup", "migration",
        "prometheus_exporter",
    }
    actual_roles = entry_module.ALLOWED_SERVICE_ROLES
    missing = required_roles - actual_roles
    assert not missing, f"缺少必需的 SERVICE_ROLE: {missing}"
    # r40_scheduler 是额外允许的(已在 run_all.py BOT_RUNNERS 中)
    assert "r40_scheduler" in actual_roles


def test_service_role_no_unknown(entry_module):
    """ALLOWED_SERVICE_ROLES 不应包含未声明的角色。"""
    # 显式列出所有允许的角色
    expected = {
        "up", "idx", "dsp", "mon", "admin", "admin_bot",
        "db_writer", "crdb_sync", "db_backup", "migration",
        "prometheus_exporter", "r40_scheduler",
    }
    assert entry_module.ALLOWED_SERVICE_ROLES == expected


# ══════════════════════════════════════════════════════════════════
# 测试 2:每个 SERVICE_ROLE 映射到正确启动命令
# ══════════════════════════════════════════════════════════════════
def test_build_command_up(entry_module):
    """SERVICE_ROLE=up → python run_all.py --standalone up"""
    cmd = entry_module._build_command("up")
    assert "--standalone" in cmd
    assert "up" in cmd
    assert "run_all.py" in cmd[-3] or "run_all.py" in cmd[-4]


def test_build_command_idx(entry_module):
    """SERVICE_ROLE=idx → python run_all.py --standalone idx"""
    cmd = entry_module._build_command("idx")
    assert "--standalone" in cmd
    assert "idx" in cmd


def test_build_command_dsp(entry_module):
    """SERVICE_ROLE=dsp → python run_all.py --standalone dsp"""
    cmd = entry_module._build_command("dsp")
    assert "--standalone" in cmd
    assert "dsp" in cmd


def test_build_command_mon(entry_module):
    """SERVICE_ROLE=mon → python run_all.py --standalone mon"""
    cmd = entry_module._build_command("mon")
    assert "--standalone" in cmd
    assert "mon" in cmd


def test_build_command_admin(entry_module):
    """SERVICE_ROLE=admin → python run_all.py --standalone admin"""
    cmd = entry_module._build_command("admin")
    assert "--standalone" in cmd
    assert "admin" in cmd


def test_build_command_admin_bot(entry_module):
    """SERVICE_ROLE=admin_bot → python run_all.py --standalone admin_bot"""
    cmd = entry_module._build_command("admin_bot")
    assert "--standalone" in cmd
    assert "admin_bot" in cmd


def test_build_command_db_writer(entry_module):
    """SERVICE_ROLE=db_writer → python run_all.py --standalone db_writer"""
    cmd = entry_module._build_command("db_writer")
    assert "--standalone" in cmd
    assert "db_writer" in cmd


def test_build_command_crdb_sync(entry_module):
    """SERVICE_ROLE=crdb_sync → python run_all.py --standalone crdb_sync"""
    cmd = entry_module._build_command("crdb_sync")
    assert "--standalone" in cmd
    assert "crdb_sync" in cmd


def test_build_command_db_backup(entry_module):
    """SERVICE_ROLE=db_backup → python run_all.py --standalone db_backup"""
    cmd = entry_module._build_command("db_backup")
    assert "--standalone" in cmd
    assert "db_backup" in cmd


def test_build_command_r40_scheduler(entry_module):
    """SERVICE_ROLE=r40_scheduler → python run_all.py --standalone r40_scheduler"""
    cmd = entry_module._build_command("r40_scheduler")
    assert "--standalone" in cmd
    assert "r40_scheduler" in cmd


# ══════════════════════════════════════════════════════════════════
# 测试 10:migration/prometheus_exporter 走独立模块入口(非 run_all.py)
# ══════════════════════════════════════════════════════════════════
def test_build_command_migration(entry_module):
    """SERVICE_ROLE=migration → python -m services.migration_runner(不走 run_all.py)"""
    cmd = entry_module._build_command("migration")
    assert "-m" in cmd
    assert "services.migration_runner" in cmd
    # 不应包含 --standalone(那是 run_all.py 的参数)
    assert "--standalone" not in cmd


def test_build_command_prometheus_exporter(entry_module):
    """SERVICE_ROLE=prometheus_exporter → python -m services.prometheus_exporter(不走 run_all.py)"""
    cmd = entry_module._build_command("prometheus_exporter")
    assert "-m" in cmd
    assert "services.prometheus_exporter" in cmd
    # 不应包含 --standalone
    assert "--standalone" not in cmd


def test_service_role_module_mapping(entry_module):
    """SERVICE_ROLE_MODULE 字典正确映射 migration / prometheus_exporter。"""
    assert entry_module.SERVICE_ROLE_MODULE["migration"] == "services.migration_runner"
    assert entry_module.SERVICE_ROLE_MODULE["prometheus_exporter"] == "services.prometheus_exporter"


def test_service_role_run_all_set(entry_module):
    """SERVICE_ROLE_RUN_ALL 集合包含所有走 run_all.py 的角色。"""
    expected = {
        "up", "idx", "dsp", "mon", "admin", "admin_bot",
        "db_writer", "crdb_sync", "db_backup", "r40_scheduler",
    }
    assert entry_module.SERVICE_ROLE_RUN_ALL == expected


# ══════════════════════════════════════════════════════════════════
# 测试 3 & 4:未指定 SERVICE_ROLE + production/staging → exit 1
# ══════════════════════════════════════════════════════════════════
def test_no_service_role_production_fail_closed(entry_module, clean_env, monkeypatch):
    """APP_ENV=production 且未指定 SERVICE_ROLE → exit 1(fail-closed)。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(sys, "argv", ["docker/entrypoint.py"])
    with pytest.raises(SystemExit) as exc_info:
        entry_module.main()
    assert exc_info.value.code == 1


def test_no_service_role_staging_fail_closed(entry_module, clean_env, monkeypatch):
    """APP_ENV=staging 且未指定 SERVICE_ROLE → exit 1(fail-closed)。"""
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setattr(sys, "argv", ["docker/entrypoint.py"])
    with pytest.raises(SystemExit) as exc_info:
        entry_module.main()
    assert exc_info.value.code == 1


# ══════════════════════════════════════════════════════════════════
# 测试 6:未知 SERVICE_ROLE → exit 1
# ══════════════════════════════════════════════════════════════════
def test_unknown_service_role_rejected(entry_module, clean_env, monkeypatch):
    """未知 SERVICE_ROLE=unknown_role → exit 1。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SERVICE_ROLE", "unknown_role")
    monkeypatch.setattr(sys, "argv", ["docker/entrypoint.py"])
    with pytest.raises(SystemExit) as exc_info:
        entry_module.main()
    assert exc_info.value.code == 1


def test_empty_service_role_production_rejected(entry_module, clean_env, monkeypatch):
    """SERVICE_ROLE='' (空字符串)在 production 下 → exit 1。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SERVICE_ROLE", "")
    monkeypatch.setattr(sys, "argv", ["docker/entrypoint.py"])
    with pytest.raises(SystemExit) as exc_info:
        entry_module.main()
    assert exc_info.value.code == 1


# ══════════════════════════════════════════════════════════════════
# 测试 7:APP_ENV 缺失 → exit 2(config.environment fail-closed)
# ══════════════════════════════════════════════════════════════════
def test_no_app_env_fail_closed(entry_module, clean_env, monkeypatch):
    """APP_ENV / ENVIRONMENT / DEPLOY_ENV 全缺失 → exit 2(fail-closed)。"""
    monkeypatch.setattr(sys, "argv", ["docker/entrypoint.py"])
    with pytest.raises(SystemExit) as exc_info:
        entry_module.main()
    assert exc_info.value.code == 2


# ══════════════════════════════════════════════════════════════════
# 测试 8:APP_ENV 冲突 → exit 2
# ══════════════════════════════════════════════════════════════════
def test_app_env_conflict_fail_closed(entry_module, clean_env, monkeypatch):
    """APP_ENV=production + ENVIRONMENT=staging 冲突 → exit 2。"""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setattr(sys, "argv", ["docker/entrypoint.py"])
    with pytest.raises(SystemExit) as exc_info:
        entry_module.main()
    assert exc_info.value.code == 2


def test_unknown_app_env_fail_closed(entry_module, clean_env, monkeypatch):
    """APP_ENV=prodution(拼写错误)→ exit 2。"""
    monkeypatch.setenv("APP_ENV", "prodution")  # typo
    monkeypatch.setattr(sys, "argv", ["docker/entrypoint.py"])
    with pytest.raises(SystemExit) as exc_info:
        entry_module.main()
    assert exc_info.value.code == 2


# ══════════════════════════════════════════════════════════════════
# 测试 9:每个角色的 _build_command 返回有效 argv
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("role", [
    "up", "idx", "dsp", "mon", "admin", "admin_bot",
    "db_writer", "crdb_sync", "db_backup", "r40_scheduler",
    "migration", "prometheus_exporter",
])
def test_all_roles_build_valid_command(entry_module, role):
    """所有 12 个角色都能构造出有效的 argv list。"""
    cmd = entry_module._build_command(role)
    assert isinstance(cmd, list)
    assert len(cmd) >= 2  # 至少 [python, module_or_script]
    assert cmd[0] == sys.executable  # 第一个元素是 python 解释器


def test_build_command_unknown_role_exits(entry_module):
    """未知角色 → exit 2(不应到达此分支,但 _build_command 有兜底)。"""
    with pytest.raises(SystemExit) as exc_info:
        entry_module._build_command("totally_unknown_role")
    assert exc_info.value.code == 2


# ══════════════════════════════════════════════════════════════════
# 测试 _resolve_app_env 函数
# ══════════════════════════════════════════════════════════════════
def test_resolve_app_env_production(entry_module, clean_env, monkeypatch):
    """_resolve_app_env() 在 APP_ENV=production 下返回 'production'。"""
    monkeypatch.setenv("APP_ENV", "production")
    result = entry_module._resolve_app_env()
    assert result == "production"


def test_resolve_app_env_staging(entry_module, clean_env, monkeypatch):
    """_resolve_app_env() 在 APP_ENV=staging 下返回 'staging'。"""
    monkeypatch.setenv("APP_ENV", "staging")
    result = entry_module._resolve_app_env()
    assert result == "staging"


def test_resolve_app_env_development(entry_module, clean_env, monkeypatch):
    """_resolve_app_env() 在 APP_ENV=development 下返回 'development'。"""
    monkeypatch.setenv("APP_ENV", "development")
    result = entry_module._resolve_app_env()
    assert result == "development"


def test_resolve_app_env_missing_exits(entry_module, clean_env, monkeypatch):
    """_resolve_app_env() 在三变量全缺失时 exit 2(fail-closed)。"""
    with pytest.raises(SystemExit) as exc_info:
        entry_module._resolve_app_env()
    assert exc_info.value.code == 2
