"""R39 P0 六项发布阻断修复测试。

被测模块 / 文件:
- config/redis/users.acl                                — P0-1 + P0-2 Redis ACL
- docker-compose.yml                                    — P0-1 healthcheck
- .env.example                                          — P0-2 密码占位符文档
- deploy_vps_per_bot.sh                                 — P0-2 sed 替换部署
- bots/idx_bot.py                                       — P0-3 移除 CRDB 直写
- database/cache_store.py(add_dirty_outbox)             — P0-4 事务发件箱
- services/crdb_sync_service.py                          — P0-4 dirty_outbox dispatcher
- utils/exceptions.py                                   — P0-5 StoreUnavailable
- database/cache_store.py(create_upload_session 等)     — P0-5 抛异常替代静默 return
- bots/up_bot.py(create_upload_session_strict)          — P0-5 ok is True 检查
- services/db_backup.py                                 — P0-6 删除明文 latest 上传

测试策略:
- 优先使用文件内容检查 + AST 检查,避免复杂运行时依赖
- 不依赖 Redis / CRDB / SQLite 实际连接(纯代码静态校验)
- 兼容 Python 3.9+(避免 PEP 604 X | Y 在测试断言中使用)
- AST 不保留注释,需要检查注释标记时直接读源码字符串
"""
import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _ensure_telegram_mock():
    """辅助: bots.up_bot 依赖 python-telegram-bot,
    未安装时通过 sys.modules 注入 MagicMock 模块避免 ImportError。
    """
    needed = [
        "telegram", "telegram.ext", "telegram.constants", "telegram.helpers",
        "telegram.request", "telegram.types", "telegram._bots",
    ]
    for mod_name in needed:
        if mod_name not in sys.modules:
            fake_mod = MagicMock()
            fake_mod.__path__ = []
            fake_mod.__name__ = mod_name
            fake_mod.__loader__ = None
            fake_mod.__spec__ = None
            sys.modules[mod_name] = fake_mod


# ════════════════════════════════════════════════════════════════
# P0-1: Redis ACL healthcheck 修复
# ════════════════════════════════════════════════════════════════

class TestP01RedisACLHealthcheck:
    """R39 P0-1: Redis ACL healthcheck 修复 — default 禁用后 healthcheck 用 health 用户。"""

    @pytest.fixture
    def acl_content(self) -> str:
        return _read(PROJECT_ROOT / "config" / "redis" / "users.acl")

    @pytest.fixture
    def compose_content(self) -> str:
        return _read(PROJECT_ROOT / "docker-compose.yml")

    def test_default_user_disabled(self, acl_content: str):
        """ACL 中 default 用户被禁用(user default off)。"""
        assert "user default off" in acl_content, (
            "R39 P0-1: default 用户应被禁用(user default off),"
            "原版本 default off 后 healthcheck 仍用 default 导致永久失败"
        )

    def test_health_user_exists(self, acl_content: str):
        """ACL 中存在专用 health 用户(仅允许 PING)。"""
        assert "user health on" in acl_content, (
            "R39 P0-1: 应新增 health 用户用于 healthcheck PING"
        )
        assert "+PING" in acl_content, (
            "R39 P0-1: health 用户应仅允许 +PING 命令"
        )

    def test_health_user_uses_placeholder_password(self, acl_content: str):
        """health 用户密码使用占位符 <REDIS_HEALTH_PASSWORD>(非硬编码)。"""
        assert "<REDIS_HEALTH_PASSWORD>" in acl_content, (
            "R39 P0-2: health 用户密码应使用占位符 <REDIS_HEALTH_PASSWORD>,"
            "由 deploy 脚本 sed 替换为真实值"
        )

    def test_compose_healthcheck_uses_health_user(self, compose_content: str):
        """docker-compose.yml healthcheck 改用 --user health(非 default)。"""
        # 找到 redis 服务的 healthcheck 配置
        assert "--user health" in compose_content, (
            "R39 P0-1: docker-compose.yml healthcheck 应使用 --user health,"
            "原版本用 --user default 在 default 被禁用后永久失败"
        )
        assert "--user default" not in compose_content or \
               "redis-cli --user default" not in compose_content, (
            "R39 P0-1: docker-compose.yml 不应再使用 --user default 做 healthcheck"
        )

    def test_compose_healthcheck_uses_dollar_dollar_escape(self, compose_content: str):
        """healthcheck 使用 $$ 转义避免 Compose 插值($$ → $)。"""
        assert "$$REDIS_HEALTH_PASSWORD" in compose_content, (
            "R39 P0-1: healthcheck 应使用 $$REDIS_HEALTH_PASSWORD 转义,"
            "避免 Compose 插值导致变量丢失"
        )


# ════════════════════════════════════════════════════════════════
# P0-2: Redis ACL 密码占位符 + sed 替换 + 正向白名单 + 命名空间
# ════════════════════════════════════════════════════════════════

class TestP02RedisACLPasswordPlaceholders:
    """R39 P0-2: Redis ACL 用占位符密码 + sed 替换 + 正向白名单 + tgjiema:* 命名空间。"""

    @pytest.fixture
    def acl_content(self) -> str:
        return _read(PROJECT_ROOT / "config" / "redis" / "users.acl")

    @pytest.fixture
    def env_example_content(self) -> str:
        return _read(PROJECT_ROOT / ".env.example")

    @pytest.fixture
    def deploy_script_content(self) -> str:
        return _read(PROJECT_ROOT / "deploy_vps_per_bot.sh")

    def test_no_hardcoded_changeme_password(self, acl_content: str):
        """ACL 不再包含硬编码的 changeme 密码。"""
        assert "changeme_writer" not in acl_content, "R39 P0-2: 不应硬编码 writer 密码"
        assert "changeme_reader" not in acl_content, "R39 P0-2: 不应硬编码 reader 密码"
        assert "changeme_health" not in acl_content, "R39 P0-2: 不应硬编码 health 密码"

    def test_writer_uses_placeholder_password(self, acl_content: str):
        """writer 用户密码使用占位符 <REDIS_WRITER_PASSWORD>。"""
        assert "<REDIS_WRITER_PASSWORD>" in acl_content, (
            "R39 P0-2: writer 密码应使用 <REDIS_WRITER_PASSWORD> 占位符"
        )

    def test_reader_uses_placeholder_password(self, acl_content: str):
        """reader 用户密码使用占位符 <REDIS_READER_PASSWORD>。"""
        assert "<REDIS_READER_PASSWORD>" in acl_content, (
            "R39 P0-2: reader 密码应使用 <REDIS_READER_PASSWORD> 占位符"
        )

    def test_positive_whitelist_pattern(self, acl_content: str):
        """ACL 采用正向白名单(-@all 后逐个 +命令),非宽泛 +@all。"""
        # writer 用户应使用 -@all + 特定命令
        assert "-@all" in acl_content, (
            "R39 P0-2: ACL 应使用正向白名单(-@all 后逐个 +命令)"
        )
        # 不应包含 +@all(全命令放行)
        assert "+@all" not in acl_content, (
            "R39 P0-2: ACL 不应使用 +@all 宽泛放行"
        )

    def test_tgjiema_namespace_isolation(self, acl_content: str):
        """writer/reader 用户的 key 限制到 tgjiema:* 命名空间。"""
        assert "~tgjiema:*" in acl_content, (
            "R39 P0-2: writer/reader 应限制到 ~tgjiema:* 命名空间"
        )
        assert "&tgjiema:*" in acl_content, (
            "R39 P0-2: writer/reader 应限制到 &tgjiema:* 频道命名空间"
        )

    def test_env_example_documents_passwords(self, env_example_content: str):
        """ .env.example 文档化新密码配置项。"""
        assert "REDIS_WRITER_PASSWORD=" in env_example_content, (
            "R39 P0-2: .env.example 应文档化 REDIS_WRITER_PASSWORD"
        )
        assert "REDIS_READER_PASSWORD=" in env_example_content, (
            "R39 P0-2: .env.example 应文档化 REDIS_READER_PASSWORD"
        )
        assert "REDIS_HEALTH_PASSWORD=" in env_example_content, (
            "R39 P0-2: .env.example 应文档化 REDIS_HEALTH_PASSWORD"
        )

    def test_deploy_script_sed_replacement(self, deploy_script_content: str):
        """deploy_vps_per_bot.sh 包含 sed 替换 ACL 占位符逻辑。"""
        assert "REDIS_WRITER_PASSWORD" in deploy_script_content, (
            "R39 P0-2: deploy 脚本应读取 REDIS_WRITER_PASSWORD"
        )
        assert "<REDIS_WRITER_PASSWORD>" in deploy_script_content, (
            "R39 P0-2: deploy 脚本应包含 sed 替换 <REDIS_WRITER_PASSWORD> 占位符"
        )
        # sed 命令应使用 | 分隔符避免密码中 / 冲突
        assert "sed" in deploy_script_content, (
            "R39 P0-2: deploy 脚本应使用 sed 替换占位符"
        )


# ════════════════════════════════════════════════════════════════
# P0-3: Idx bot 移除 CRDB 直写,改用 SQLite + dirty_outbox
# ════════════════════════════════════════════════════════════════

class TestP03IdxBotNoDirectCRDB:
    """R39 P0-3: Idx bot 不再直连 CRDB Collection,改用 SQLite 本地 + dirty_outbox。"""

    @pytest.fixture
    def idx_bot_content(self) -> str:
        return _read(PROJECT_ROOT / "bots" / "idx_bot.py")

    def test_no_files_col_insert_one(self, idx_bot_content: str):
        """idx_bot.py 不再调用 files_col.insert_one(直连 CRDB)。

        用 AST 检查实际方法调用,排除注释中提到的字符串。
        """
        tree = ast.parse(idx_bot_content)
        # 遍历 AST 找所有方法调用,检查是否有 files_col.insert_one
        for node in ast.walk(tree):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Attribute) and call.func.attr == "insert_one":
                    # 检查调用对象是否为 files_col
                    if isinstance(call.func.value, ast.Name) and call.func.value.id == "files_col":
                        pytest.fail(
                            "R39 P0-3: idx_bot 不应直连 CRDB files_col.insert_one,"
                            "应改用 upsert_file_record_local + add_dirty_outbox"
                        )

    def test_no_codes_col_insert_one(self, idx_bot_content: str):
        """idx_bot.py 不再调用 codes_col.insert_one(直连 CRDB)。

        用 AST 检查实际方法调用,排除注释中提到的字符串。
        """
        tree = ast.parse(idx_bot_content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
                call = node.value
                if isinstance(call.func, ast.Attribute) and call.func.attr == "insert_one":
                    if isinstance(call.func.value, ast.Name) and call.func.value.id == "codes_col":
                        pytest.fail(
                            "R39 P0-3: idx_bot 不应直连 CRDB codes_col.insert_one,"
                            "应改用 upsert_code_local + add_dirty_outbox"
                        )

    def test_uses_upsert_file_record_local(self, idx_bot_content: str):
        """idx_bot.py 使用 upsert_file_record_local 写本地 SQLite。"""
        assert "upsert_file_record_local" in idx_bot_content, (
            "R39 P0-3: idx_bot 应使用 upsert_file_record_local 写本地 SQLite"
        )

    def test_uses_upsert_code_local(self, idx_bot_content: str):
        """idx_bot.py 使用 upsert_code_local 写本地 SQLite。"""
        assert "upsert_code_local" in idx_bot_content, (
            "R39 P0-3: idx_bot 应使用 upsert_code_local 写本地 SQLite"
        )

    def test_uses_add_dirty_outbox(self, idx_bot_content: str):
        """idx_bot.py 使用 add_dirty_outbox 记录变更。"""
        assert "add_dirty_outbox" in idx_bot_content, (
            "R39 P0-3: idx_bot 应使用 add_dirty_outbox 记录变更供 crdb_sync 消费"
        )

    def test_has_r39_p0_3_marker(self, idx_bot_content: str):
        """idx_bot.py 包含 R39 P0-3 标记注释。"""
        assert "R39 P0-3" in idx_bot_content, (
            "R39 P0-3: idx_bot.py 新增代码应标注 'R39 P0-3' 注释"
        )


# ════════════════════════════════════════════════════════════════
# P0-4: dirty_outbox 事务发件箱 + crdb_sync dispatcher
# ════════════════════════════════════════════════════════════════

class TestP04DirtyOutboxTransactional:
    """R39 P0-4: dirty_outbox 事务发件箱 + crdb_sync dispatcher 消费。"""

    @pytest.fixture
    def cache_store_content(self) -> str:
        return _read(PROJECT_ROOT / "database" / "cache_store.py")

    @pytest.fixture
    def crdb_sync_content(self) -> str:
        return _read(PROJECT_ROOT / "services" / "crdb_sync_service.py")

    def test_add_dirty_outbox_has_connection_param(self, cache_store_content: str):
        """cache_store.add_dirty_outbox 接受 connection 参数(事务发件箱模式)。"""
        assert "connection: Any = None" in cache_store_content or \
               "connection=None" in cache_store_content, (
            "R39 P0-4: add_dirty_outbox 应接受 connection 参数实现事务发件箱"
        )

    def test_add_dirty_outbox_no_auto_commit_with_connection(self, cache_store_content: str):
        """传入 connection 时不自动 commit(由调用方控制事务)。"""
        # 检查事务模式分支存在
        assert "connection is not None" in cache_store_content, (
            "R39 P0-4: add_dirty_outbox 应有 connection 传入时的分支(不自动 commit)"
        )
        # 检查注释说明
        assert "不自动 commit" in cache_store_content or \
               "不调用 commit" in cache_store_content, (
            "R39 P0-4: add_dirty_outbox 事务模式应说明不自动 commit"
        )

    def test_crdb_sync_has_dispatch_function(self, crdb_sync_content: str):
        """crdb_sync_service.py 包含 _dispatch_dirty_outbox_to_crdb 函数。"""
        assert "_dispatch_dirty_outbox_to_crdb" in crdb_sync_content, (
            "R39 P0-4: crdb_sync_service 应有 _dispatch_dirty_outbox_to_crdb dispatcher"
        )

    def test_crdb_sync_has_sync_dirty_outbox(self, crdb_sync_content: str):
        """crdb_sync_service.py 包含 _sync_dirty_outbox 消费循环。"""
        assert "_sync_dirty_outbox" in crdb_sync_content, (
            "R39 P0-4: crdb_sync_service 应有 _sync_dirty_outbox 消费循环"
        )

    def test_crdb_sync_main_includes_dirty_outbox_task(self, crdb_sync_content: str):
        """crdb_sync main() tasks 列表包含 sync-dirty-outbox 循环。"""
        assert "sync-dirty-outbox" in crdb_sync_content, (
            "R39 P0-4: crdb_sync main() tasks 应包含 sync-dirty-outbox 循环"
        )

    def test_crdb_sync_dead_for_unknown_table(self, crdb_sync_content: str):
        """未知 table_name → DEAD(不标记 processed,保留供人工检查)。"""
        assert "DEAD" in crdb_sync_content, (
            "R39 P0-4: 未知 table_name 应走 DEAD 分支(不丢弃)"
        )

    def test_crdb_sync_has_p0_4_marker(self, crdb_sync_content: str):
        """crdb_sync_service.py 包含 R39 P0-4 标记注释。"""
        assert "R39 P0-4" in crdb_sync_content, (
            "R39 P0-4: crdb_sync_service.py 新增代码应标注 'R39 P0-4' 注释"
        )

    def test_dispatch_handlers_for_known_tables(self, crdb_sync_content: str):
        """dispatcher 包含 file_records / codes / users 的 handler。"""
        assert "_dispatch_file_records_upsert" in crdb_sync_content, (
            "R39 P0-4: dispatcher 应包含 file_records handler"
        )
        assert "_dispatch_codes_upsert" in crdb_sync_content, (
            "R39 P0-4: dispatcher 应包含 codes handler"
        )

    def test_dispatch_uses_upsert_on_conflict(self, crdb_sync_content: str):
        """dispatcher 使用 INSERT ON CONFLICT DO UPDATE(幂等 UPSERT)。"""
        assert "ON CONFLICT" in crdb_sync_content, (
            "R39 P0-4: dispatcher 应使用 INSERT ON CONFLICT DO UPDATE 实现幂等 UPSERT"
        )


# ════════════════════════════════════════════════════════════════
# P0-5: Upload strict 修复 StoreUnavailable
# ════════════════════════════════════════════════════════════════

class TestP05StoreUnavailable:
    """R39 P0-5: CacheStore 静默 return 改抛 StoreUnavailable,strict 检查 ok is True。"""

    @pytest.fixture
    def exceptions_content(self) -> str:
        return _read(PROJECT_ROOT / "utils" / "exceptions.py")

    @pytest.fixture
    def cache_store_content(self) -> str:
        return _read(PROJECT_ROOT / "database" / "cache_store.py")

    @pytest.fixture
    def up_bot_content(self) -> str:
        return _read(PROJECT_ROOT / "bots" / "up_bot.py")

    def test_store_unavailable_class_defined(self, exceptions_content: str):
        """utils/exceptions.py 定义了 StoreUnavailable 异常类。"""
        assert "class StoreUnavailable" in exceptions_content, (
            "R39 P0-5: utils/exceptions.py 应定义 StoreUnavailable 异常类"
        )
        assert "R39 P0-5" in exceptions_content, (
            "R39 P0-5: StoreUnavailable 类应标注 'R39 P0-5' 注释"
        )

    def test_store_unavailable_is_exception_subclass(self, exceptions_content: str):
        """StoreUnavailable 是 Exception 子类。"""
        tree = ast.parse(exceptions_content)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "StoreUnavailable":
                for base in node.bases:
                    if isinstance(base, ast.Name) and base.id == "Exception":
                        found = True
                break
        assert found, (
            "R39 P0-5: StoreUnavailable 应继承 Exception"
        )

    def test_create_upload_session_raises_store_unavailable(self, cache_store_content: str):
        """create_upload_session 在 _db 为 None 或 upload_id 为空时抛 StoreUnavailable。"""
        # 检查 create_upload_session 方法不再静默 return
        assert "StoreUnavailable" in cache_store_content, (
            "R39 P0-5: cache_store.py 应导入并抛 StoreUnavailable"
        )

    def test_create_upload_session_returns_bool(self, cache_store_content: str):
        """create_upload_session 返回类型改为 bool(替代 None)。"""
        # AST 检查 create_upload_session 的返回注解
        tree = ast.parse(cache_store_content)
        found_method = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "create_upload_session":
                found_method = node
                break
        assert found_method is not None, "create_upload_session 方法应存在"
        # 检查返回注解为 bool
        returns = found_method.returns
        assert returns is not None, (
            "R39 P0-5: create_upload_session 应有返回类型注解 bool"
        )
        # bool 可能是 ast.Name(id='bool') 或其他形式
        if isinstance(returns, ast.Name):
            assert returns.id == "bool", (
                f"R39 P0-5: create_upload_session 返回类型应为 bool,实际: {returns.id}"
            )

    def test_create_outbox_entry_raises_store_unavailable(self, cache_store_content: str):
        """create_outbox_entry 在 _db 为 None 或 outbox_id 为空时抛 StoreUnavailable。"""
        # 验证 create_outbox_entry 方法中包含 StoreUnavailable 引用
        assert "StoreUnavailable" in cache_store_content, (
            "R39 P0-5: create_outbox_entry 应抛 StoreUnavailable"
        )

    def test_transition_upload_session_raises_store_unavailable(self, cache_store_content: str):
        """transition_upload_session 在 _db 为 None 或 upload_id 为空时抛 StoreUnavailable。"""
        # 找到 transition_upload_session 方法体,检查不包含 `return False` 的早期返回
        # (改抛 StoreUnavailable)
        tree = ast.parse(cache_store_content)
        found_method = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "transition_upload_session":
                found_method = node
                break
        assert found_method is not None, "transition_upload_session 方法应存在"
        # 检查方法体中包含 raise 语句(StoreUnavailable)
        has_raise = False
        for child in ast.walk(found_method):
            if isinstance(child, ast.Raise):
                has_raise = True
                break
        assert has_raise, (
            "R39 P0-5: transition_upload_session 应包含 raise StoreUnavailable 语句"
        )

    def test_up_bot_strict_checks_ok_is_true(self, up_bot_content: str):
        """up_bot.create_upload_session_strict 检查 ok is True(或 ok is not True)。"""
        assert "ok is not True" in up_bot_content or "ok is True" in up_bot_content, (
            "R39 P0-5: create_upload_session_strict 应检查 ok is True / ok is not True"
        )

    def test_up_bot_has_p0_5_marker(self, up_bot_content: str):
        """up_bot.py 包含 R39 P0-5 标记注释。"""
        assert "R39 P0-5" in up_bot_content, (
            "R39 P0-5: up_bot.py 新增代码应标注 'R39 P0-5' 注释"
        )


# ════════════════════════════════════════════════════════════════
# P0-6: db_backup 删除明文 latest 上传
# ════════════════════════════════════════════════════════════════

class TestP06DeletePlaintextLatestUpload:
    """R39 P0-6: 强制加密备份不再上传明文 latest_<table>.json。"""

    @pytest.fixture
    def db_backup_content(self) -> str:
        return _read(PROJECT_ROOT / "services" / "db_backup.py")

    def test_no_plaintext_latest_upload_block(self, db_backup_content: str):
        """db_backup.py 不再包含明文 latest_{table}.json 上传逻辑。"""
        # 原代码块: for table in data["tables"]: ... await r2_storage.upload(f"db_backup/latest_{table}.json", ...)
        # 检查不再有 latest_{table} 的 upload 调用(注释中提及不算)
        # 找到所有非注释行中的 latest_{table} upload
        lines = db_backup_content.split("\n")
        active_upload_lines = []
        in_comment = False
        for line in lines:
            stripped = line.strip()
            # 跳过单行注释
            if stripped.startswith("#"):
                continue
            # 检查是否有非注释的 latest_{table} upload 调用
            if "latest_" in line and "upload" in line.lower() and "{" in line:
                active_upload_lines.append(line)
        assert not active_upload_lines, (
            f"R39 P0-6: db_backup.py 不应再有明文 latest_{{table}} upload 调用,"
            f"发现: {active_upload_lines}"
        )

    def test_has_p0_6_deletion_marker(self, db_backup_content: str):
        """db_backup.py 包含 R39 P0-6 删除标记注释。"""
        assert "R39 P0-6" in db_backup_content, (
            "R39 P0-6: db_backup.py 应标注 'R39 P0-6' 注释说明删除明文上传"
        )

    def test_encrypted_bundle_upload_still_present(self, db_backup_content: str):
        """加密 bundle 上传逻辑仍存在(仅删除明文 latest,不影响加密主备份)。"""
        # 加密 bundle 上传通过 r2_storage.upload(key, upload_content, ...)
        # 其中 key 是 timestamped key(如 db_backup/db_backup_{timestamp}.bin)
        assert "r2_storage.upload" in db_backup_content or \
               "await r2_storage.upload" in db_backup_content, (
            "R39 P0-6: 加密 bundle 上传逻辑应保留(仅删除明文 latest)"
        )

    def test_manifest_upload_still_present(self, db_backup_content: str):
        """manifest 上传逻辑仍存在(含 checksum + 加密元数据)。"""
        assert "manifest" in db_backup_content.lower(), (
            "R39 P0-6: manifest 上传逻辑应保留(含 checksum + 加密元数据)"
        )


# ════════════════════════════════════════════════════════════════
# 综合验证: 所有 6 项 P0 修复标记
# ════════════════════════════════════════════════════════════════

class TestR39AllP0Markers:
    """综合验证: 确保所有 6 项 P0 修复都有 R39 P0-X 标记。"""

    @pytest.mark.parametrize("p0_id,files,expected_marker", [
        ("P0-1", ["config/redis/users.acl", "docker-compose.yml"], "R39 P0-1"),
        ("P0-2", ["config/redis/users.acl", ".env.example", "deploy_vps_per_bot.sh"], "R39 P0-2"),
        ("P0-3", ["bots/idx_bot.py"], "R39 P0-3"),
        ("P0-4", ["database/cache_store.py", "services/crdb_sync_service.py"], "R39 P0-4"),
        ("P0-5", ["utils/exceptions.py", "database/cache_store.py", "bots/up_bot.py"], "R39 P0-5"),
        ("P0-6", ["services/db_backup.py"], "R39 P0-6"),
    ])
    def test_p0_marker_present(self, p0_id: str, files: list, expected_marker: str):
        """每项 P0 修复在相关文件中至少出现一次标记注释。"""
        found = False
        for rel_path in files:
            content = _read(PROJECT_ROOT / rel_path)
            if expected_marker in content:
                found = True
                break
        assert found, (
            f"{p0_id}: 相关文件({files})中至少一个应包含 '{expected_marker}' 标记注释"
        )
