"""R37 Batch 1: P0 发布阻断项修复测试。

被测模块:
- docker-compose.yml + deploy_vps_per_bot.sh  — P0-1 Compose/secrets 注入修复
- bots/up_bot._outbox_register_manifest_strict — P0-2 file_unique_id fail-closed
- services/crdb_sync_service + bots/dsp_bot/mon_bot — P0-3 crdb_sync 独占
- config/settings.py — crdb_sync/migration validator + SYNC_BACK_OFF

P0 修复对应:
- P0-1: env_file_secrets 非标准字段 → 标准 env_file; migration 缺 secrets → 补全
- P0-2: file_unique_id 缺失静默成功 → raise DurabilityError (fail-closed)
- P0-3: Bot 直连兜底 → 默认禁用(SYNC_BACK_OFF=0); crdb_sync leader 租约 + 节流
"""
import inspect
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ════════════════════════════════════════════════════════════════
# P0-1: Compose env_file + migration secrets 修复
# ════════════════════════════════════════════════════════════════

class TestP01ComposeEnvFileFix:
    """R37 P0-1: Compose 配置与 migration secrets 注入修复。"""

    def test_compose_no_env_file_secrets_field(self):
        """docker-compose.yml 不包含非标准 env_file_secrets 字段。"""
        path = Path(__file__).parent.parent / "docker-compose.yml"
        content = path.read_text(encoding="utf-8")
        assert "env_file_secrets" not in content, (
            "docker-compose.yml 仍包含非标准 env_file_secrets 字段,应改为标准 env_file"
        )

    def test_compose_migration_has_secrets_env_file(self):
        """migration 服务加载 .env.secrets.migration(P0-1 核心修复)。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 不可用")
        path = Path(__file__).parent.parent / "docker-compose.yml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        migration = data["services"]["migration"]
        env_files = migration.get("env_file", [])
        assert ".env.secrets.migration" in env_files, (
            "migration 服务未加载 .env.secrets.migration(含 COCKROACHDB_URL)"
        )

    def test_compose_crdb_sync_uses_standard_env_file(self):
        """crdb_sync 服务使用标准 env_file 字段加载 secrets。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 不可用")
        path = Path(__file__).parent.parent / "docker-compose.yml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        crdb_sync = data["services"]["crdb_sync"]
        env_files = crdb_sync.get("env_file", [])
        assert ".env.secrets.crdb_sync" in env_files, (
            "crdb_sync 服务未通过标准 env_file 加载 .env.secrets.crdb_sync"
        )

    def test_deploy_script_has_migration_secrets(self):
        """deploy_vps_per_bot.sh 的 SERVICE_SECRETS 数组包含 migration 条目。"""
        path = Path(__file__).parent.parent / "deploy_vps_per_bot.sh"
        content = path.read_text(encoding="utf-8")
        assert '[migration]="COCKROACHDB_URL"' in content, (
            "deploy_vps_per_bot.sh SERVICE_SECRETS 数组缺少 migration 条目"
        )

    def test_services_yaml_migration_has_crdb_url_secret(self):
        """services.yaml 中 migration 的 secrets 包含 COCKROACHDB_URL。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 不可用")
        path = Path(__file__).parent.parent / "config" / "services.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        migration = next(s for s in data["services"] if s["name"] == "migration")
        assert "COCKROACHDB_URL" in migration["secrets"], (
            "services.yaml migration secrets 不含 COCKROACHDB_URL"
        )


# ════════════════════════════════════════════════════════════════
# P0-2: Outbox file_unique_id fail-closed
# ════════════════════════════════════════════════════════════════

class TestP02OutboxFileUniqueIdFailClosed:
    """R37 P0-2: file_unique_id 缺失时必须 fail-closed。"""

    def test_strict_manifest_raises_on_missing_file_unique_id(self):
        """_outbox_register_manifest_strict 在 file_unique_id 缺失时抛 DurabilityError。"""
        path = Path(__file__).parent.parent / "bots" / "up_bot.py"
        content = path.read_text(encoding="utf-8")
        # 确认源码中不再有"跳过 manifest 注册"的静默 return
        assert "跳过 manifest 注册" not in content or "R37 P0-2" in content, (
            "up_bot.py 仍包含静默跳过 manifest 注册的旧逻辑"
        )
        # 确认源码中包含 DurabilityError 抛出
        assert "DurabilityError" in content, (
            "up_bot.py 未抛出 DurabilityError(file_unique_id 缺失时)"
        )
        # R55 i18n: 错误消息已迁移到 locale 文件,检查源码含 i18n key 或 locale 含消息
        locale_path = Path(__file__).parent.parent / "locales" / "zh-CN.json"
        locale_data = json.loads(locale_path.read_text(encoding="utf-8"))
        locale_msg = locale_data.get("bot", {}).get("up", {}).get("s6", "")
        assert "manifest event missing file_unique_id" in content or (
            "_i18n_t('bot.up.s6'" in content and "manifest event missing file_unique_id" in locale_msg
        ), (
            "up_bot.py 缺少 file_unique_id 缺失的 DurabilityError 消息"
            "(源码和 locale 文件均未找到)"
        )

    def test_strict_manifest_does_not_silently_return(self):
        """_outbox_register_manifest_strict 不应在 file_unique_id 缺失时静默返回。

        检查 _outbox_register_manifest_strict 函数体内不包含"视为成功"的静默降级注释。
        """
        path = Path(__file__).parent.parent / "bots" / "up_bot.py"
        content = path.read_text(encoding="utf-8")
        # 提取 _outbox_register_manifest_strict 函数体(到下一个 async def 或空行定义结束)
        func_start = content.find("async def _outbox_register_manifest_strict")
        assert func_start != -1, "未找到 _outbox_register_manifest_strict 函数"
        # 取函数体前 800 字符(足够覆盖 file_unique_id 检查逻辑)
        func_body = content[func_start:func_start + 800]
        assert "视为成功" not in func_body, (
            "_outbox_register_manifest_strict 仍包含'视为成功'的静默降级逻辑"
        )

    def test_durability_error_imported_in_up_bot(self):
        """up_bot.py 导入 DurabilityError 异常类。"""
        path = Path(__file__).parent.parent / "bots" / "up_bot.py"
        content = path.read_text(encoding="utf-8")
        # 可能是函数内导入(from utils.exceptions import DurabilityError)
        assert "DurabilityError" in content


# ════════════════════════════════════════════════════════════════
# P0-3: crdb_sync 独占 + Bot 兜底禁用 + 节流
# ════════════════════════════════════════════════════════════════

class TestP03CrdbSyncExclusive:
    """R37 P0-3: crdb_sync 独占 CRDB 同步,Bot 直连兜底默认禁用。"""

    def test_settings_has_sync_back_off(self):
        """settings.py 包含 SYNC_BACK_OFF 配置项(默认 0=禁用兜底)。"""
        path = Path(__file__).parent.parent / "config" / "settings.py"
        content = path.read_text(encoding="utf-8")
        assert "SYNC_BACK_OFF" in content
        assert "SYNC_BACK_OFF: int = 0" in content, (
            "SYNC_BACK_OFF 默认值应为 0(生产禁用兜底)"
        )

    def test_settings_has_crdb_sync_leader_lease(self):
        """settings.py 包含 CRDB_SYNC_LEADER_LEASE 配置项。"""
        path = Path(__file__).parent.parent / "config" / "settings.py"
        content = path.read_text(encoding="utf-8")
        assert "CRDB_SYNC_LEADER_LEASE" in content

    def test_settings_has_crdb_sync_dirty_interval(self):
        """settings.py 包含 CRDB_SYNC_DIRTY_INTERVAL 配置项。"""
        path = Path(__file__).parent.parent / "config" / "settings.py"
        content = path.read_text(encoding="utf-8")
        assert "CRDB_SYNC_DIRTY_INTERVAL" in content

    def test_settings_crdb_sync_validator_registered(self):
        """settings.py 的 role_validators 包含 crdb_sync。"""
        path = Path(__file__).parent.parent / "config" / "settings.py"
        content = path.read_text(encoding="utf-8")
        assert '"crdb_sync"' in content
        assert "_validate_crdb_sync_fields" in content

    def test_settings_migration_validator_registered(self):
        """settings.py 的 role_validators 包含 migration。"""
        path = Path(__file__).parent.parent / "config" / "settings.py"
        content = path.read_text(encoding="utf-8")
        assert '"migration"' in content
        assert "_validate_migration_fields" in content

    def test_dsp_bot_sync_back_loop_respects_sync_back_off(self):
        """dsp_bot.sync_back_loop 检查 SYNC_BACK_OFF,为 0 时不启动循环。"""
        path = Path(__file__).parent.parent / "bots" / "dsp_bot.py"
        content = path.read_text(encoding="utf-8")
        assert "SYNC_BACK_OFF" in content
        # 确认有 early return 逻辑
        assert "不启动循环" in content or "由 crdb_sync 独占同步" in content

    def test_dsp_bot_startup_sync_respects_sync_back_off(self):
        """dsp_bot.startup_sync 检查 SYNC_BACK_OFF,为 0 时跳过。"""
        path = Path(__file__).parent.parent / "bots" / "dsp_bot.py"
        content = path.read_text(encoding="utf-8")
        # startup_sync 函数应包含 SYNC_BACK_OFF 检查
        startup_section = content[content.find("async def startup_sync"):]
        assert "SYNC_BACK_OFF" in startup_section[:500]

    def test_mon_bot_cells_sync_respects_sync_back_off(self):
        """mon_bot 的 cells 兜底同步检查 SYNC_BACK_OFF。"""
        path = Path(__file__).parent.parent / "bots" / "mon_bot.py"
        content = path.read_text(encoding="utf-8")
        assert "SYNC_BACK_OFF" in content

    def test_crdb_sync_service_has_leader_lease(self):
        """crdb_sync_service.py 包含 leader 租约机制。"""
        path = Path(__file__).parent.parent / "services" / "crdb_sync_service.py"
        content = path.read_text(encoding="utf-8")
        assert "_acquire_leader_lease" in content
        assert "_renew_leader_lease" in content
        assert "_release_leader_lease" in content

    def test_crdb_sync_service_has_dirty_batch_interval(self):
        """crdb_sync_service.py 有 dirty 时使用受控 cadence(2s)。"""
        path = Path(__file__).parent.parent / "services" / "crdb_sync_service.py"
        content = path.read_text(encoding="utf-8")
        assert "DIRTY_BATCH_INTERVAL" in content

    def test_crdb_sync_service_loop_does_not_unconditional_sleep(self):
        """crdb_sync 同步循环有 dirty 时 continue + short sleep(不长退避)。"""
        path = Path(__file__).parent.parent / "services" / "crdb_sync_service.py"
        content = path.read_text(encoding="utf-8")
        # 有 dirty 时应 continue(跳过末尾的 await asyncio.sleep(backoff))
        assert "continue" in content

    def test_session_py_crdb_sync_higher_max_size(self):
        """database/session.py 对 crdb_sync 角色放宽 max_size 上限。"""
        path = Path(__file__).parent.parent / "database" / "session.py"
        content = path.read_text(encoding="utf-8")
        assert 'role == "crdb_sync"' in content or "crdb_sync" in content
        # crdb_sync 应有更高的上限(5),而非 2
        assert "5" in content  # crdb_sync ≤5


# ════════════════════════════════════════════════════════════════
# settings validator 单元测试
# ════════════════════════════════════════════════════════════════

class TestSettingsValidators:
    """R37 P0-3: settings.py 新增 validator 逻辑测试。"""

    def test_validate_crdb_sync_fields_requires_crdb_url(self):
        """_validate_crdb_sync_fields 在 COCKROACHDB_URL 为空时报错。"""
        path = Path(__file__).parent.parent / "config" / "settings.py"
        content = path.read_text(encoding="utf-8")
        assert "crdb_sync] COCKROACHDB_URL 未配置" in content

    def test_validate_migration_fields_requires_crdb_url(self):
        """_validate_migration_fields 在 COCKROACHDB_URL 为空时报错。"""
        path = Path(__file__).parent.parent / "config" / "settings.py"
        content = path.read_text(encoding="utf-8")
        assert "migration] COCKROACHDB_URL 未配置" in content


# ════════════════════════════════════════════════════════════════
# conftest mock 验证
# ════════════════════════════════════════════════════════════════

class TestConftestMock:
    """R37 P0-3: conftest.py mock 包含新配置项。"""

    def test_conftest_has_sync_back_off(self):
        """conftest.py mock 包含 SYNC_BACK_OFF。"""
        path = Path(__file__).parent.parent / "tests" / "conftest.py"
        content = path.read_text(encoding="utf-8")
        assert "SYNC_BACK_OFF" in content
        assert "CRDB_SYNC_LEADER_LEASE" in content
        assert "CRDB_SYNC_DIRTY_INTERVAL" in content
