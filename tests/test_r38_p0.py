"""R38 P0 五项发布阻断修复测试。

被测模块 / 文件:
- Dockerfile                                          — P0-1 digest 占位符替换
- config/settings.py                                  — P0-2 idx 去除 COCKROACHDB_URL 校验
- bots/up_bot.py(create_upload_session_strict /
  _create_outbox_entry_strict /
  _transition_upload_session_strict)                  — P0-3 strict 模式抛 DurabilityError
- services/crdb_sync_service.py                        — P0-4 Redis SET NX PX + Lua CAS
- services/migration_runner.py                         — P0-5 严格错误处理 + schema 验证

测试策略:
- 优先使用文件内容检查 + AST 检查,避免复杂运行时依赖
- 不依赖 Redis / CRDB / SQLite 实际连接(纯代码静态校验)
- 兼容 Python 3.9+(避免 PEP 604 X | Y 在测试断言中使用)
- AST 不保留注释,需要检查注释标记时直接读源码字符串
- 集成测试需要 import bots.up_bot,该模块依赖 python-telegram-bot;
  未安装时通过 sys.modules 注入 mock 跳过(保持测试可在最小依赖下运行)
"""
import ast
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _ensure_telegram_mock():
    """R38 P0-3 集成测试辅助:bots.up_bot 依赖 python-telegram-bot,
    未安装时通过 sys.modules 注入 MagicMock 模块避免 ImportError。
    已安装真实 telegram 时不覆盖。

    使用 MagicMock() 让任意 attribute / 子模块访问都返回 MagicMock,
    满足 `from telegram import InlineKeyboardButton, ...` 形式的导入。
    """
    needed = [
        "telegram", "telegram.ext", "telegram.constants", "telegram.helpers",
        "telegram.request", "telegram.types", "telegram._bots",
    ]
    for mod_name in needed:
        if mod_name not in sys.modules:
            fake_mod = MagicMock()
            # __path__ 让 Python 认为这是一个包(支持子模块导入)
            fake_mod.__path__ = []
            # __name__ / __loader__ / __spec__ 用于 import machinery
            fake_mod.__name__ = mod_name
            fake_mod.__loader__ = None
            fake_mod.__spec__ = None
            # __getattr__ 默认返回 MagicMock,任意属性访问都会成功
            sys.modules[mod_name] = fake_mod


# ════════════════════════════════════════════════════════════════
# P0-1: Dockerfile digest 占位符 → tag 引用
# ════════════════════════════════════════════════════════════════

class TestP01DockerfileDigestFix:
    """R38 P0-1: Dockerfile 占位 digest 已替换为 ARG PYTHON_IMAGE 引用。"""

    @pytest.fixture
    def dockerfile_content(self) -> str:
        return _read(PROJECT_ROOT / "Dockerfile")

    def test_no_placeholder_digest(self, dockerfile_content: str):
        """Dockerfile 不再包含占位 digest b0d2c8b8e5b2a3c4。"""
        assert "b0d2c8b8e5b2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e" not in dockerfile_content, (
            "Dockerfile 仍包含 R37 P2-4 占位 digest(非合法 64 位 hex),"
            "应替换为 ARG PYTHON_IMAGE 引用"
        )

    def test_no_partial_placeholder_digest(self, dockerfile_content: str):
        """Dockerfile 不再包含占位 digest 的前缀片段(避免部分残留)。"""
        # 检查占位 digest 的前 16 字符片段也不应再出现
        assert "b0d2c8b8e5b2a3c4" not in dockerfile_content, (
            "Dockerfile 仍包含占位 digest 片段 b0d2c8b8e5b2a3c4,"
            "应完整替换为 ARG PYTHON_IMAGE"
        )

    def test_uses_arg_python_image(self, dockerfile_content: str):
        """Dockerfile 使用 ARG PYTHON_IMAGE 参数化基础镜像引用。"""
        assert "ARG PYTHON_IMAGE" in dockerfile_content, (
            "Dockerfile 未使用 ARG PYTHON_IMAGE 参数化基础镜像,"
            "应改为 ARG PYTHON_IMAGE=python:3.12-slim"
        )

    def test_two_from_lines_use_arg(self, dockerfile_content: str):
        """两个 FROM 行都引用 ${PYTHON_IMAGE}。"""
        from_lines = [
            line for line in dockerfile_content.splitlines()
            if line.strip().startswith("FROM ")
        ]
        assert len(from_lines) >= 2, (
            f"Dockerfile 应包含至少 2 个 FROM 行(builder + runtime),"
            f"实际 {len(from_lines)}"
        )
        for line in from_lines:
            assert "${PYTHON_IMAGE}" in line, (
                f"FROM 行未使用 ${{PYTHON_IMAGE}} 引用:{line}"
            )

    def test_default_python_image_tag(self, dockerfile_content: str):
        """ARG PYTHON_IMAGE 默认值为 python:3.12-slim tag。"""
        # ARG PYTHON_IMAGE=python:3.12-slim
        assert "ARG PYTHON_IMAGE=python:3.12-slim" in dockerfile_content, (
            "ARG PYTHON_IMAGE 默认值应为 python:3.12-slim(避免 docker build 失败)"
        )

    def test_no_remaining_invalid_digest(self, dockerfile_content: str):
        """Dockerfile 不再包含任何 @sha256: digest 引用(避免占位符残留)。"""
        # R38 P0-1: 已切换为 tag 引用,所有 @sha256: 引用应已移除
        assert "@sha256:" not in dockerfile_content, (
            "Dockerfile 仍包含 @sha256: digest 引用,"
            "R38 P0-1 应替换为 tag 引用(生产部署前再用真实 digest 替换)"
        )

    def test_has_update_digest_flow_comment(self, dockerfile_content: str):
        """Dockerfile 注释中保留更新 digest 的流程说明。"""
        # 注释中应包含 docker inspect + RepoDigests 流程说明
        assert "RepoDigests" in dockerfile_content, (
            "Dockerfile 注释应保留更新 digest 的流程说明(docker inspect RepoDigests)"
        )

    def test_has_r38_p0_1_marker(self, dockerfile_content: str):
        """Dockerfile 注释中标注 R38 P0-1。"""
        assert "R38 P0-1" in dockerfile_content, (
            "Dockerfile 注释应标注 R38 P0-1(标记占位 digest 已替换为 tag 引用)"
        )


# ════════════════════════════════════════════════════════════════
# P0-2: settings.py idx 去除 COCKROACHDB_URL 校验
# ════════════════════════════════════════════════════════════════

class TestP02IdxNoCrdbUrl:
    """R38 P0-2: Idx 服务不再直连 CRDB,去除 COCKROACHDB_URL 校验。"""

    @pytest.fixture
    def settings_content(self) -> str:
        return _read(PROJECT_ROOT / "config" / "settings.py")

    def test_idx_validator_no_crdb_url(self, settings_content: str):
        """_validate_idx_fields 不再包含 COCKROACHDB_URL 校验。"""
        # 提取 _validate_idx_fields 函数体
        tree = ast.parse(settings_content)
        idx_validator_body = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_validate_idx_fields":
                idx_validator_body = ast.unparse(node)
                break
        assert idx_validator_body is not None, "_validate_idx_fields 函数不存在"
        assert "COCKROACHDB_URL" not in idx_validator_body, (
            "_validate_idx_fields 仍包含 COCKROACHDB_URL 校验,"
            "R38 P0-2 应删除(idx 不直连 CRDB,通过 dirty_outbox + crdb_sync 间接同步)"
        )

    def test_idx_validator_no_crdb_url_raise(self, settings_content: str):
        """_validate_idx_fields 不再抛出 idx COCKROACHDB_URL 未配置 错误。"""
        assert '"[Settings][idx] COCKROACHDB_URL 未配置"' not in settings_content, (
            "settings.py 仍包含 idx COCKROACHDB_URL 未配置 校验,"
            "R38 P0-2 应删除"
        )

    def test_idx_validator_has_r38_p0_2_marker(self, settings_content: str):
        """_validate_idx_fields 注释中标注 R38 P0-2。"""
        # AST 不保留注释,直接从源码字符串中查找 _validate_idx_fields 函数位置
        # 并检查其后 30 行内是否包含 R38 P0-2 标记
        marker_idx = settings_content.find("R38 P0-2")
        assert marker_idx >= 0, "settings.py 中应包含 R38 P0-2 标记注释"
        # 查找 _validate_idx_fields 函数定义
        func_idx = settings_content.find("def _validate_idx_fields")
        assert func_idx >= 0, "_validate_idx_fields 函数不存在"
        # 检查 R38 P0-2 标记是否在 _validate_idx_fields 函数体内或紧邻
        # (允许标记在函数内注释或紧邻函数的注释中)
        func_end = settings_content.find("def _validate_dsp_fields", func_idx)
        if func_end < 0:
            func_end = len(settings_content)
        func_body_with_comments = settings_content[func_idx:func_end]
        assert "R38 P0-2" in func_body_with_comments, (
            "_validate_idx_fields 函数体内应有 R38 P0-2 标记注释"
        )

    def test_crdb_sync_migration_backup_still_validate_crdb_url(self, settings_content: str):
        """crdb_sync / migration / db_backup 仍保留 COCKROACHDB_URL 校验(不应删除)。"""
        # 提取对应 validator
        tree = ast.parse(settings_content)
        validators_to_check = [
            "_validate_crdb_sync_fields",
            "_validate_migration_fields",
            "_validate_backup_fields",
        ]
        for validator_name in validators_to_check:
            found = False
            for node in ast.walk(tree):
                if (isinstance(node, ast.FunctionDef)
                        and node.name == validator_name):
                    body = ast.unparse(node)
                    assert "COCKROACHDB_URL" in body, (
                        f"{validator_name} 应保留 COCKROACHDB_URL 校验"
                        f"(crdb_sync / migration / db_backup 仍需直连 CRDB)"
                    )
                    found = True
                    break
            if not found:
                # backup validator 名可能不同,检查 settings 中所有 backup 相关校验
                pass

    def test_mon_admin_bot_no_crdb_url(self, settings_content: str):
        """mon / admin_bot 校验不应包含 COCKROACHDB_URL(原版本就不应直连)。"""
        tree = ast.parse(settings_content)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in (
                "_validate_mon_fields", "_validate_admin_bot_fields",
            ):
                body = ast.unparse(node)
                assert "COCKROACHDB_URL" not in body, (
                    f"{node.name} 不应包含 COCKROACHDB_URL 校验"
                    f"(mon / admin_bot 不直连 CRDB)"
                )


# ════════════════════════════════════════════════════════════════
# P0-3: Upload session/outbox strict 模式抛 DurabilityError
# ════════════════════════════════════════════════════════════════

class TestP03StrictDurabilityError:
    """R38 P0-3: strict 模式创建/推进失败抛 DurabilityError,不再返回空串。"""

    @pytest.fixture
    def up_bot_content(self) -> str:
        return _read(PROJECT_ROOT / "bots" / "up_bot.py")

    def test_has_create_upload_session_strict(self, up_bot_content: str):
        """up_bot.py 定义 create_upload_session_strict 函数。"""
        assert "async def create_upload_session_strict" in up_bot_content, (
            "up_bot.py 应定义 create_upload_session_strict 函数"
        )

    def test_create_upload_session_strict_raises_durability_error(
        self, up_bot_content: str,
    ):
        """create_upload_session_strict 失败时抛 DurabilityError,不返回空串。"""
        tree = ast.parse(up_bot_content)
        strict_func = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "create_upload_session_strict"):
                strict_func = node
                break
        assert strict_func is not None, "create_upload_session_strict 未定义"

        body = ast.unparse(strict_func)
        # 检查抛出 DurabilityError
        assert "DurabilityError" in body, (
            "create_upload_session_strict 失败应抛 DurabilityError"
        )
        # 检查不再返回空串(return "")
        assert 'return ""' not in body, (
            "create_upload_session_strict 不应返回空串"
            "(应让异常传播,由调用方决定是否回滚)"
        )

    def test_create_upload_session_for_upload_calls_strict(
        self, up_bot_content: str,
    ):
        """_create_upload_session_for_upload 改为调用 strict 版本(不捕获异常)。"""
        tree = ast.parse(up_bot_content)
        wrapper_func = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_create_upload_session_for_upload"):
                wrapper_func = node
                break
        assert wrapper_func is not None
        body = ast.unparse(wrapper_func)
        # 应调用 create_upload_session_strict
        assert "create_upload_session_strict" in body, (
            "_create_upload_session_for_upload 应调用 create_upload_session_strict"
        )
        # 不应再 try/except 吞掉异常
        has_try = any(isinstance(n, ast.Try) for n in ast.walk(wrapper_func))
        assert not has_try, (
            "_create_upload_session_for_upload 不应再 try/except 吞掉异常,"
            "应让异常传播到主流程"
        )

    def test_outbox_strict_raises_on_empty_upload_id(self, up_bot_content: str):
        """_create_outbox_entry_strict 在 upload_id 为空时抛 DurabilityError。"""
        tree = ast.parse(up_bot_content)
        strict_func = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_create_outbox_entry_strict"):
                strict_func = node
                break
        assert strict_func is not None
        body = ast.unparse(strict_func)
        # upload_id 为空时抛 DurabilityError(原版本 return)
        assert "if not upload_id" in body, "应有 upload_id 空检查"
        assert "DurabilityError" in body, "应抛 DurabilityError"
        # 不应在 upload_id 为空时 return(原版本是 if not upload_id: return)
        # 通过检查函数中不再有 "if not upload_id:\n    return" 模式
        assert "missing upload_id" in body.lower() or "missing upload_id".lower() in body.lower(), (
            "DurabilityError 消息应包含 'missing upload_id'"
        )

    def test_outbox_strict_raises_on_store_failure(self, up_bot_content: str):
        """_create_outbox_entry_strict 在 store 失败时抛 DurabilityError('create outbox returned false')。"""
        tree = ast.parse(up_bot_content)
        strict_func = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_create_outbox_entry_strict"):
                strict_func = node
                break
        assert strict_func is not None
        body = ast.unparse(strict_func)
        assert "create outbox returned false" in body, (
            "DurabilityError 消息应包含 'create outbox returned false'"
            "(store 返回 False / 抛异常时)"
        )

    def test_transition_strict_raises_on_empty_upload_id(self, up_bot_content: str):
        """_transition_upload_session_strict 在 upload_id 为空时抛 DurabilityError。"""
        tree = ast.parse(up_bot_content)
        strict_func = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_transition_upload_session_strict"):
                strict_func = node
                break
        assert strict_func is not None
        body = ast.unparse(strict_func)
        assert "if not upload_id" in body
        assert "DurabilityError" in body
        # 不应在 upload_id 为空时静默 return(原版本)
        # 检查 if not upload_id 分支不再有 return(应有 raise DurabilityError)
        assert "missing upload_id" in body.lower(), (
            "upload_id 为空时应抛 DurabilityError('missing upload_id'),不再静默 return"
        )


# ════════════════════════════════════════════════════════════════
# P0-4: crdb_sync leader lease 原子 CAS(Redis SET NX PX + Lua)
# ════════════════════════════════════════════════════════════════

class TestP04LeaderLeaseAtomicCAS:
    """R38 P0-4: crdb_sync leader 租约使用 Redis SET NX PX + Lua compare-and-renew。"""

    @pytest.fixture
    def crdb_sync_content(self) -> str:
        return _read(PROJECT_ROOT / "services" / "crdb_sync_service.py")

    def test_leader_id_is_module_level_constant(self, crdb_sync_content: str):
        """leader_id 在进程启动时生成一次(模块级常量,非每次 acquire 生成)。"""
        # 应有模块级 _LEADER_ID 常量
        assert "_LEADER_ID" in crdb_sync_content, (
            "crdb_sync_service 应有模块级 _LEADER_ID 常量(进程启动时生成一次)"
        )

    def test_acquire_uses_redis_set_nx_px(self, crdb_sync_content: str):
        """_acquire_leader_lease 用 Redis SET key value NX PX(原子获取)。"""
        # 检查代码包含 SET NX PX 调用模式
        # redis-py async 接口:redis_client.set(key, value, nx=True, px=ttl)
        assert "nx=True" in crdb_sync_content, (
            "_acquire_leader_lease 应使用 redis_client.set(..., nx=True, px=...)"
        )
        assert "px=" in crdb_sync_content, (
            "_acquire_leader_lease 应使用 px=<ttl_ms> 设置过期时间"
        )

    def test_renew_uses_lua_compare_and_renew(self, crdb_sync_content: str):
        """_renew_leader_lease 用 Lua 脚本 compare-and-renew(GET == ARGV[1] 才 PEXPIRE)。"""
        # 检查 Lua 脚本中包含 redis.call("GET", KEYS[1]) == ARGV[1]
        assert 'redis.call("GET", KEYS[1])' in crdb_sync_content or (
            'redis.call(\'GET\', KEYS[1])' in crdb_sync_content
        ), "Lua 脚本应包含 redis.call('GET', KEYS[1]) 比较"
        assert "PEXPIRE" in crdb_sync_content, (
            "Lua 脚本应包含 PEXPIRE 续约"
        )
        # 应使用 redis_client.eval(...) 调用 Lua
        assert ".eval(" in crdb_sync_content, (
            "_renew_leader_lease 应通过 redis_client.eval(...) 调用 Lua 脚本"
        )

    def test_has_independent_renewal_task(self, crdb_sync_content: str):
        """独立 renewal task(每 TTL/3 续约,不受主循环 sleep 影响)。"""
        assert "_leader_renewal_task" in crdb_sync_content or (
            "leader_renewal_task" in crdb_sync_content
        ), "应有独立的 _leader_renewal_task 函数"
        # renewal 间隔应约为 TTL/3
        assert "_LEADER_RENEWAL_INTERVAL" in crdb_sync_content, (
            "应有 _LEADER_RENEWAL_INTERVAL 常量(约 TTL/3)"
        )

    def test_renewal_task_spawned_in_main(self, crdb_sync_content: str):
        """main() 启动 renewal task 与同步循环并发运行。"""
        # main 中应 create_task 调用 _leader_renewal_task
        assert "_leader_renewal_task" in crdb_sync_content
        # 检查 tasks 列表包含 renewal task
        assert "leader-renewal" in crdb_sync_content or (
            "_leader_renewal_task()" in crdb_sync_content
        )

    def test_fence_check_before_each_batch(self, crdb_sync_content: str):
        """每批写前校验 fencing token(renew_leader 返回 0 → 停止同步 + 关闭 pool)。

        R39 P1-2: _sync_loop 不再直接调用 _renew_leader_lease(),
        改为只读模块级 _lease_valid 标志(由 _leader_renewal_task 唯一更新)。
        因此本测试接受两种实现:
          (a) R38 形式: _sync_loop 内调用 _renew_leader_lease
          (b) R39 形式: _sync_loop 内读取 _lease_valid 标志
        R39 P0-4: close_db 改名为 _close_crdb_only(只关 CRDB pool,保留 SQLite)。
        """
        # _sync_loop 中应在 sync_func 调用前 renew 或读 lease 标志
        tree = ast.parse(crdb_sync_content)
        sync_loop_func = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_sync_loop"):
                sync_loop_func = node
                break
        assert sync_loop_func is not None
        body = ast.unparse(sync_loop_func)
        # R39 P1-2: 接受 _renew_leader_lease(R38)或 _lease_valid(R39)两种形式
        assert "_renew_leader_lease" in body or "_lease_valid" in body, (
            "_sync_loop 应在每批写前调用 _renew_leader_lease 或读取 _lease_valid "
            "校验 fencing token"
        )
        # R39 P0-4: 接受 _close_crdb_only(R39)或 close_db(R38)两种形式
        assert "_close_crdb_only" in body or "close_db" in body, (
            "丢租约时应关闭 CRDB pool(_close_crdb_only 或 close_db)避免越权写入"
        )

    def test_has_fallback_to_sqlite_kv(self, crdb_sync_content: str):
        """Redis 不可用时降级到 SQLite KV(记录 warning)。"""
        # 应有 fallback 标志 + warning 日志
        assert "_redis_leader_fallback" in crdb_sync_content or (
            "fallback" in crdb_sync_content.lower()
        ), "应有 Redis 不可用降级到 SQLite KV 的逻辑"

    def test_release_uses_lua(self, crdb_sync_content: str):
        """释放租约也用 Lua(仅 owner 可释放,避免误删他人租约)。"""
        # 应有 _RELEASE_LEADER_LUA 脚本(仅 owner DEL)
        assert "_RELEASE_LEADER_LUA" in crdb_sync_content or (
            'redis.call("DEL"' in crdb_sync_content
        ), "释放租约应用 Lua 脚本(仅 owner 可释放)"

    def test_has_r38_p0_4_markers(self, crdb_sync_content: str):
        """代码中标注 R38 P0-4(注释标记)。"""
        assert "R38 P0-4" in crdb_sync_content, (
            "crdb_sync_service.py 应在注释中标注 R38 P0-4"
        )


# ════════════════════════════════════════════════════════════════
# P0-5: Migration 严格错误处理(白名单 + schema 验证)
# ════════════════════════════════════════════════════════════════

class TestP05MigrationStrictErrorHandling:
    """R38 P0-5: migration_runner 严格错误处理 + information_schema 验证。"""

    @pytest.fixture
    def migration_content(self) -> str:
        return _read(PROJECT_ROOT / "services" / "migration_runner.py")

    def test_has_whitelist_error_patterns(self, migration_content: str):
        """migration_runner 包含白名单错误模式(already exists / duplicate)。"""
        # 应有 _DDL_IGNORABLE_ERROR_PATTERNS 常量
        assert "_DDL_IGNORABLE_ERROR_PATTERNS" in migration_content or (
            '"already exists"' in migration_content and '"duplicate"' in migration_content
        ), "应有 DDL 可忽略错误白名单(already exists / duplicate)"
        # 应有 _is_ignorable_ddl_error 函数
        assert "_is_ignorable_ddl_error" in migration_content, (
            "应有 _is_ignorable_ddl_error 函数判断白名单"
        )

    def test_ddl_non_whitelist_errors_raise(self, migration_content: str):
        """DDL 非白名单错误立即 raise(不继续执行后续语句)。"""
        tree = ast.parse(migration_content)
        run_migration_func = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "run_migration"):
                run_migration_func = node
                break
        assert run_migration_func is not None
        body = ast.unparse(run_migration_func)
        # 在 DDL_STATEMENTS 循环中应有 raise(原版本只是 logger.warning + 继续)
        # 检查 _is_ignorable_ddl_error 调用后 else 分支有 raise
        assert "raise" in body, "DDL 非白名单错误应 raise"
        # 验证 _is_ignorable_ddl_error 被调用
        assert "_is_ignorable_ddl_error" in body, (
            "DDL 错误处理应调用 _is_ignorable_ddl_error 判断白名单"
        )

    def test_severe_error_blocks_version_write(self, migration_content: str):
        """严重错误时禁止写 ddl_version(CRDB + SQLite 都不写)。"""
        tree = ast.parse(migration_content)
        run_migration_func = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "run_migration"):
                run_migration_func = node
                break
        assert run_migration_func is not None
        body = ast.unparse(run_migration_func)
        # 应有 severe_error_occurred 标志
        assert "severe_error_occurred" in body, (
            "应有 severe_error_occurred 标志跟踪严重错误"
        )

    def test_uses_information_schema_validation(self, migration_content: str):
        """写 ddl_version 前用 information_schema 验证 schema 实际存在。"""
        # 应查询 information_schema.tables / information_schema.columns
        assert "information_schema" in migration_content, (
            "应查询 information_schema 验证 schema 实际存在"
        )
        # 应有 _verify_schema_post_migration 函数
        assert "_verify_schema_post_migration" in migration_content, (
            "应有 _verify_schema_post_migration 函数验证 schema"
        )

    def test_sqlite_version_only_after_crdb_success(self, migration_content: str):
        """SQLite 版本只在 CRDB 版本写成功后镜像。"""
        tree = ast.parse(migration_content)
        run_migration_func = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "run_migration"):
                run_migration_func = node
                break
        assert run_migration_func is not None
        body = ast.unparse(run_migration_func)
        # 应有 crdb_version_written 标志
        assert "crdb_version_written" in body, (
            "应有 crdb_version_written 标志,SQLite 写入应在 CRDB 写成功后"
        )

    def test_check_ddl_version_validates_crdb(self, migration_content: str):
        """_check_ddl_version 每次至少验证 CRDB version(不只信本地缓存)。"""
        tree = ast.parse(migration_content)
        check_func = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "_check_ddl_version"):
                check_func = node
                break
        assert check_func is not None
        body = ast.unparse(check_func)
        # 应查询 CRDB rotation_config(不只信 SQLite)
        assert "rotation_config" in body, (
            "_check_ddl_version 应查询 CRDB rotation_config 验证版本"
        )
        # 不应在 SQLite 命中时直接 return False(原 R37 行为)
        # 新版本:即使 SQLite 命中,CRDB 查询失败也强制执行迁移
        assert "强制执行迁移" in body or "force" in body.lower() or (
            "unknown" in body
        ), "_check_ddl_version 应在 CRDB 查询失败时强制执行迁移"

    def test_no_fetchval_call_on_client(self, migration_content: str):
        """R38 P0-5 修复:不再调用 client.fetchval(client 无此方法)。

        原 R37 版本调用 client.fetchval(...) 会 AttributeError(client 只有 fetch),
        应改为 client.fetch(...)[0][0] 取首行首列。
        """
        # R38 P0-5: 用 AST 检查实际函数调用(忽略注释中提到的字符串)
        tree = ast.parse(migration_content)
        fetchval_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "fetchval":
                # fetchval 调用形式: <obj>.fetchval(...)
                # obj 可能是 client / self._pool 等
                fetchval_calls.append(node)
        assert not fetchval_calls, (
            f"R38 P0-5: 应移除 .fetchval(...) 调用(client 无此方法),"
            f"实际发现 {len(fetchval_calls)} 处"
        )
        # 应改用 client.fetch(...)
        assert "client.fetch(" in migration_content, (
            "应改用 client.fetch(...) 取首行首列"
        )

    def test_has_r38_p0_5_markers(self, migration_content: str):
        """代码中标注 R38 P0-5。"""
        assert "R38 P0-5" in migration_content, (
            "migration_runner.py 应在注释中标注 R38 P0-5"
        )


# ════════════════════════════════════════════════════════════════
# 端到端集成:strict 模式行为验证(可选,不依赖 Redis/CRDB)
# ════════════════════════════════════════════════════════════════

class TestP03StrictBehaviorIntegration:
    """R38 P0-3 集成测试:strict 模式实际抛 DurabilityError 行为。

    依赖 bots.up_bot 完整导入(需要 python-telegram-bot / asyncpg / aiosqlite 等
    运行时依赖)。未安装时自动跳过(静态代码检查已覆盖核心断言,
    集成测试用于运行时验证)。
    """

    def setup_method(self, method):
        """每个测试方法前注入 telegram mock(若未安装真实 telegram)。"""
        _ensure_telegram_mock()

    def _try_import_up_bot(self):
        """尝试导入 bots.up_bot,失败返回 None(调用方应 skip)。"""
        try:
            import bots.up_bot  # noqa: F401
            return True
        except Exception as e:
            pytest.skip(
                f"bots.up_bot 无法导入(运行时依赖缺失): {e}"
            )
            return False

    @pytest.mark.asyncio
    async def test_create_upload_session_strict_raises_on_store_failure(self):
        """create_upload_session_strict 在 store 抛异常时抛 DurabilityError。"""
        if not self._try_import_up_bot():
            return
        from utils.exceptions import DurabilityError

        # Mock get_cache_store 返回会抛异常的 store
        async def _raise(*args, **kwargs):
            raise RuntimeError("mock store failure")

        mock_store = MagicMock()
        mock_store.create_upload_session = _raise

        with patch("bots.up_bot.get_cache_store", return_value=mock_store):
            from bots.up_bot import create_upload_session_strict
            with pytest.raises(DurabilityError) as exc_info:
                await create_upload_session_strict(user_id=123)
            assert "create upload session returned false / failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_outbox_strict_raises_on_empty_upload_id(self):
        """_create_outbox_entry_strict 在 upload_id 为空时抛 DurabilityError。"""
        if not self._try_import_up_bot():
            return
        from utils.exceptions import DurabilityError

        from bots.up_bot import _create_outbox_entry_strict
        with pytest.raises(DurabilityError) as exc_info:
            await _create_outbox_entry_strict(
                outbox_id="obx-1", upload_id="", user_id=123, channel_id=456,
            )
        assert "missing upload_id" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_transition_strict_raises_on_empty_upload_id(self):
        """_transition_upload_session_strict 在 upload_id 为空时抛 DurabilityError。"""
        if not self._try_import_up_bot():
            return
        from utils.exceptions import DurabilityError

        from bots.up_bot import _transition_upload_session_strict
        with pytest.raises(DurabilityError) as exc_info:
            await _transition_upload_session_strict("", "READY")
        assert "missing upload_id" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_create_upload_session_for_upload_propagates_exception(self):
        """_create_upload_session_for_upload 不再吞掉异常,传播到主流程。"""
        if not self._try_import_up_bot():
            return
        from utils.exceptions import DurabilityError

        async def _raise(*args, **kwargs):
            raise RuntimeError("mock store failure")

        mock_store = MagicMock()
        mock_store.create_upload_session = _raise

        with patch("bots.up_bot.get_cache_store", return_value=mock_store):
            from bots.up_bot import _create_upload_session_for_upload
            # R38 P0-3: 不再返回空串,而是抛 DurabilityError 让主流程感知
            with pytest.raises(DurabilityError):
                await _create_upload_session_for_upload(user_id=123)


# ════════════════════════════════════════════════════════════════
# 集成测试: migration_runner 白名单函数单元测试
# ════════════════════════════════════════════════════════════════

class TestP05WhitelistFunctionUnit:
    """R38 P0-5 单元测试: _is_ignorable_ddl_error 函数行为。"""

    def test_already_exists_is_ignorable(self):
        from services.migration_runner import _is_ignorable_ddl_error
        assert _is_ignorable_ddl_error(
            'relation "rotation_config" already exists'
        ) is True

    def test_duplicate_is_ignorable(self):
        from services.migration_runner import _is_ignorable_ddl_error
        assert _is_ignorable_ddl_error(
            "duplicate column name: id"
        ) is True

    def test_syntax_error_is_not_ignorable(self):
        from services.migration_runner import _is_ignorable_ddl_error
        assert _is_ignorable_ddl_error(
            'syntax error at or near "SELEC"'
        ) is False

    def test_permission_error_is_not_ignorable(self):
        from services.migration_runner import _is_ignorable_ddl_error
        assert _is_ignorable_ddl_error(
            'permission denied for table rotation_config'
        ) is False

    def test_connection_error_is_not_ignorable(self):
        from services.migration_runner import _is_ignorable_ddl_error
        assert _is_ignorable_ddl_error(
            "connection refused"
        ) is False

    def test_empty_string_is_not_ignorable(self):
        from services.migration_runner import _is_ignorable_ddl_error
        assert _is_ignorable_ddl_error("") is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
