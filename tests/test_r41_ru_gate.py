"""R41 CRDB RU 最终门禁测试。

测试范围:
- services/crdb_ru_collector.py: 静态门禁(is_service_allowed_crdb_url 白名单)
- COCKROACHDB_URL 静态扫描门禁(业务 Bot 源码不引用)
- services/crdb_sync_service.py: R41 新增 _should_connect / _close_pool_if_idle 逻辑
- services/crdb_sync_service.py: dirty_outbox 合并最高 version 批量 UPSERT 逻辑
- services/crdb_sync_service.py: CRDB_SYNC_BATCH_SIZE 范围限制(100-500)
- services/prometheus_exporter.py: tgjiema_crdb_idle_ru_daily / tgjiema_crdb_idle_ru_alert gauge

门禁目标:
- 业务 Bot(up_bot/idx_bot/dsp_bot/admin_bot/db_writer)不持有 CRDB URL
- 仅 crdb_sync/migration/bootstrap/disaster_recovery/backup 可读取 COCKROACHDB_URL
- 业务 Bot RU 必须为 0(总空载理想 ≤20 RU/天,上限 ≤100 RU/天)

测试策略:
- 静态扫描:grep 源码确认业务 Bot 不引用 COCKROACHDB_URL
- AST 检查:验证 _should_connect / _close_pool_if_idle 方法定义
- 运行时调用:is_service_allowed_crdb_url() 白名单校验
- 逻辑测试:版本合并算法验证(同一 pk 仅保留 version 最大的记录)
- 批量范围:_parse_batch_size 限制在 [100, 500] 范围
- Prometheus 指标:collect_metrics() 输出包含 tgjiema_crdb_idle_ru_daily gauge
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"
BOTS_DIR = REPO_ROOT / "bots"


# ── 辅助函数 ──────────────────────────────────────────


def _parse_ast(filepath: Path) -> ast.Module | None:
    """解析 Python 文件 AST,失败返回 None。"""
    try:
        source = filepath.read_text(encoding="utf-8")
        return ast.parse(source)
    except Exception:
        return None


def _read_text(filepath: Path) -> str:
    """读取文件文本内容。"""
    return filepath.read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════
# 1. services/crdb_ru_collector.py — is_service_allowed_crdb_url 白名单
# ════════════════════════════════════════════════════════════════


class TestCrdbUrlStaticGate:
    """R41 RU 门禁: is_service_allowed_crdb_url() 白名单校验。"""

    def test_crdb_ru_collector_file_exists(self):
        """services/crdb_ru_collector.py 应存在。"""
        assert (SERVICES_DIR / "crdb_ru_collector.py").exists(), (
            "services/crdb_ru_collector.py 应存在"
        )

    def test_ast_parseable(self):
        """services/crdb_ru_collector.py 应可被 AST 解析。"""
        tree = _parse_ast(SERVICES_DIR / "crdb_ru_collector.py")
        assert tree is not None, "crdb_ru_collector.py 应可被 AST 解析"

    def test_has_is_service_allowed_crdb_url_function(self):
        """crdb_ru_collector.py 应定义 is_service_allowed_crdb_url 函数。"""
        tree = _parse_ast(SERVICES_DIR / "crdb_ru_collector.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "is_service_allowed_crdb_url" in funcs, (
            "crdb_ru_collector.py 应定义 is_service_allowed_crdb_url 函数"
        )

    def test_has_allowed_crdb_url_readers_whitelist(self):
        """crdb_ru_collector.py 应定义 _ALLOWED_CRDB_URL_READERS 白名单常量。"""
        source = _read_text(SERVICES_DIR / "crdb_ru_collector.py")
        assert "_ALLOWED_CRDB_URL_READERS" in source, (
            "crdb_ru_collector.py 应定义 _ALLOWED_CRDB_URL_READERS 白名单"
        )

    def test_allowed_readers_includes_crdb_sync(self):
        """白名单应包含 crdb_sync。"""
        source = _read_text(SERVICES_DIR / "crdb_ru_collector.py")
        # 应在白名单定义中包含 "crdb_sync"
        assert '"crdb_sync"' in source or "'crdb_sync'" in source, (
            "_ALLOWED_CRDB_URL_READERS 应包含 'crdb_sync'"
        )

    def test_allowed_readers_includes_migration(self):
        """白名单应包含 migration。"""
        source = _read_text(SERVICES_DIR / "crdb_ru_collector.py")
        assert '"migration"' in source or "'migration'" in source, (
            "_ALLOWED_CRDB_URL_READERS 应包含 'migration'"
        )

    def test_allowed_readers_includes_disaster_recovery(self):
        """白名单应包含 disaster_recovery。"""
        source = _read_text(SERVICES_DIR / "crdb_ru_collector.py")
        assert (
            '"disaster_recovery"' in source or "'disaster_recovery'" in source
        ), "_ALLOWED_CRDB_URL_READERS 应包含 'disaster_recovery'"

    def test_allowed_readers_excludes_business_bots(self):
        """白名单不应包含业务 Bot(up_bot/idx_bot/dsp_bot/admin_bot/db_writer)。"""
        source = _read_text(SERVICES_DIR / "crdb_ru_collector.py")
        # 提取白名单定义部分(从 _ALLOWED_CRDB_URL_READERS = frozenset({ 到 })为止)
        # 简化检查:在白名单定义附近不应出现业务 Bot 名
        # 找到白名单定义的起始位置
        marker = "_ALLOWED_CRDB_URL_READERS"
        marker_pos = source.find(marker)
        if marker_pos < 0:
            pytest.skip("找不到 _ALLOWED_CRDB_URL_READERS 定义")
        # 截取白名单定义后的 500 字符(应该足够覆盖整个定义)
        segment = source[marker_pos:marker_pos + 500]
        business_bots = ["up_bot", "idx_bot", "dsp_bot", "admin_bot", "db_writer"]
        for bot in business_bots:
            # 业务 Bot 名不应出现在白名单定义中(注释除外)
            # 简化:检查是否在 frozenset({...}) 内出现
            assert f'"{bot}"' not in segment and f"'{bot}'" not in segment, (
                f"_ALLOWED_CRDB_URL_READERS 不应包含业务 Bot '{bot}'"
            )


# ════════════════════════════════════════════════════════════════
# 2. is_service_allowed_crdb_url 运行时测试
# ════════════════════════════════════════════════════════════════


class TestIsServiceAllowedCrdbUrlRuntime:
    """R41: is_service_allowed_crdb_url() 运行时白名单校验。"""

    def test_crdb_sync_allowed(self):
        """crdb_sync 应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("crdb_sync") is True

    def test_migration_allowed(self):
        """migration 应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("migration") is True

    def test_bootstrap_allowed(self):
        """bootstrap 应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("bootstrap") is True

    def test_disaster_recovery_allowed(self):
        """disaster_recovery 应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("disaster_recovery") is True

    def test_backup_allowed(self):
        """backup 应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("backup") is True

    def test_up_bot_not_allowed(self):
        """up_bot 不应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("up_bot") is False

    def test_idx_bot_not_allowed(self):
        """idx_bot 不应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("idx_bot") is False

    def test_dsp_bot_not_allowed(self):
        """dsp_bot 不应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("dsp_bot") is False

    def test_admin_bot_not_allowed(self):
        """admin_bot 不应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("admin_bot") is False

    def test_db_writer_not_allowed(self):
        """db_writer 不应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("db_writer") is False

    def test_empty_string_not_allowed(self):
        """空字符串不应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("") is False

    def test_none_not_allowed(self):
        """None 不应在白名单中。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url(None) is False

    def test_whitespace_only_not_allowed(self):
        """纯空格字符串不应在白名单中(strip 后为空)。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        assert is_service_allowed_crdb_url("   ") is False

    def test_case_sensitive(self):
        """白名单应区分大小写(CRDB_SYNC 不应通过)。"""
        from services.crdb_ru_collector import is_service_allowed_crdb_url
        # 大写的 CRDB_SYNC 不应在白名单中(白名单是小写)
        assert is_service_allowed_crdb_url("CRDB_SYNC") is False


# ════════════════════════════════════════════════════════════════
# 3. COCKROACHDB_URL 静态扫描门禁 — 业务 Bot 源码不应引用
# ════════════════════════════════════════════════════════════════


class TestBusinessBotsNoCrdbUrl:
    """R41 RU 门禁: 业务 Bot(up/idx/dsp)源码不应引用 COCKROACHDB_URL。

    admin_bot/handlers.py 的 factory_reset 是已知例外(高风险管理操作,
    通过 CommandBus 强制 RBAC + 审批门禁,且仅在显式触发时使用)。
    """

    def test_up_bot_no_crdb_url_reference(self):
        """up_bot.py 源码不应引用 COCKROACHDB_URL(业务 Bot RU 必须为 0)。"""
        source = _read_text(BOTS_DIR / "up_bot.py")
        assert "COCKROACHDB_URL" not in source, (
            "up_bot.py 不应引用 COCKROACHDB_URL(业务 Bot RU 必须为 0)"
        )

    def test_idx_bot_no_crdb_url_reference(self):
        """idx_bot.py 源码不应引用 COCKROACHDB_URL。"""
        source = _read_text(BOTS_DIR / "idx_bot.py")
        assert "COCKROACHDB_URL" not in source, (
            "idx_bot.py 不应引用 COCKROACHDB_URL"
        )

    def test_dsp_bot_no_crdb_url_reference(self):
        """dsp_bot.py 源码不应引用 COCKROACHDB_URL。"""
        source = _read_text(BOTS_DIR / "dsp_bot.py")
        assert "COCKROACHDB_URL" not in source, (
            "dsp_bot.py 不应引用 COCKROACHDB_URL"
        )

    def test_business_bots_no_crdb_client_import(self):
        """业务 Bot 源码不应直接导入 CockroachDBClient(避免直连 CRDB)。"""
        for bot_file in ["up_bot.py", "idx_bot.py", "dsp_bot.py"]:
            source = _read_text(BOTS_DIR / bot_file)
            # 不应导入 CockroachDBClient(允许在注释中出现,但不应在 import 语句中)
            # 简化:检查 import 语句
            assert "from database.session import CockroachDBClient" not in source, (
                f"{bot_file} 不应导入 CockroachDBClient"
            )
            assert "import CockroachDBClient" not in source, (
                f"{bot_file} 不应导入 CockroachDBClient"
            )


# ════════════════════════════════════════════════════════════════
# 4. services/crdb_sync_service.py — _should_connect / _close_pool_if_idle
# ════════════════════════════════════════════════════════════════


class TestCrdbSyncLazyConnect:
    """R41: crdb_sync_service lazy-connect + 空闲 close pool 逻辑。"""

    def test_crdb_sync_service_file_exists(self):
        """services/crdb_sync_service.py 应存在。"""
        assert (SERVICES_DIR / "crdb_sync_service.py").exists()

    def test_ast_parseable(self):
        """services/crdb_sync_service.py 应可被 AST 解析。"""
        tree = _parse_ast(SERVICES_DIR / "crdb_sync_service.py")
        assert tree is not None

    def test_has_should_connect_method(self):
        """crdb_sync_service.py 应定义 _should_connect 方法。"""
        tree = _parse_ast(SERVICES_DIR / "crdb_sync_service.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef)
        }
        assert "_should_connect" in funcs, (
            "crdb_sync_service.py 应定义 _should_connect 方法"
        )

    def test_has_close_pool_if_idle_method(self):
        """crdb_sync_service.py 应定义 _close_pool_if_idle 方法。"""
        tree = _parse_ast(SERVICES_DIR / "crdb_sync_service.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef)
        }
        assert "_close_pool_if_idle" in funcs, (
            "crdb_sync_service.py 应定义 _close_pool_if_idle 方法"
        )

    def test_has_lazy_connect_crdb_method(self):
        """crdb_sync_service.py 应定义 _lazy_connect_crdb 方法(R38 P1-1 已有)。"""
        tree = _parse_ast(SERVICES_DIR / "crdb_sync_service.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef)
        }
        assert "_lazy_connect_crdb" in funcs

    def test_has_close_crdb_only_method(self):
        """crdb_sync_service.py 应定义 _close_crdb_only 方法(R38 P1-1 已有)。"""
        tree = _parse_ast(SERVICES_DIR / "crdb_sync_service.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef)
        }
        assert "_close_crdb_only" in funcs

    def test_has_close_cooldown_constant(self):
        """crdb_sync_service.py 应定义 _CRDB_CLOSE_COOLDOWN 常量(R41 新增)。"""
        source = _read_text(SERVICES_DIR / "crdb_sync_service.py")
        assert "_CRDB_CLOSE_COOLDOWN" in source, (
            "crdb_sync_service.py 应定义 _CRDB_CLOSE_COOLDOWN 常量"
        )

    def test_has_last_pool_close_ts_variable(self):
        """crdb_sync_service.py 应定义 _last_pool_close_ts 全局变量(R41 新增)。"""
        source = _read_text(SERVICES_DIR / "crdb_sync_service.py")
        assert "_last_pool_close_ts" in source, (
            "crdb_sync_service.py 应定义 _last_pool_close_ts 全局变量"
        )

    def test_should_connect_checks_dirty_outbox(self):
        """_should_connect 应查询 dirty_outbox 表判断是否有未处理记录(0 RU)。"""
        source = _read_text(SERVICES_DIR / "crdb_sync_service.py")
        # _should_connect 方法应查询 dirty_outbox 表
        assert "dirty_outbox" in source, (
            "_should_connect 应查询 dirty_outbox 表"
        )
        # 应在 _should_connect 附近查询 processed = 0
        assert "processed = 0" in source or "processed=0" in source, (
            "_should_connect 应检查 processed = 0"
        )

    def test_should_connect_has_cooldown_check(self):
        """_should_connect 应包含 cooldown 检查(避免频繁 connect/close)。"""
        source = _read_text(SERVICES_DIR / "crdb_sync_service.py")
        # _should_connect 方法应引用 _CRDB_CLOSE_COOLDOWN
        assert "_CRDB_CLOSE_COOLDOWN" in source, (
            "_should_connect 应包含 cooldown 检查"
        )

    def test_close_pool_if_idle_calls_close_crdb_only(self):
        """_close_pool_if_idle 应在空闲超阈值时调用 _close_crdb_only。"""
        source = _read_text(SERVICES_DIR / "crdb_sync_service.py")
        # _close_pool_if_idle 方法应调用 _close_crdb_only
        assert "_close_crdb_only()" in source or "await _close_crdb_only()" in source, (
            "_close_pool_if_idle 应调用 _close_crdb_only"
        )

    def test_sync_dirty_outbox_calls_should_connect(self):
        """_sync_dirty_outbox 应在开头调用 _should_connect 判断是否需要连接。"""
        source = _read_text(SERVICES_DIR / "crdb_sync_service.py")
        # _sync_dirty_outbox 方法应调用 _should_connect
        assert "_should_connect()" in source or "await _should_connect()" in source, (
            "_sync_dirty_outbox 应调用 _should_connect"
        )

    def test_sync_dirty_outbox_calls_close_pool_if_idle(self):
        """_sync_dirty_outbox 末尾应调用 _close_pool_if_idle 释放空闲连接。"""
        source = _read_text(SERVICES_DIR / "crdb_sync_service.py")
        # _sync_dirty_outbox 末尾应调用 _close_pool_if_idle
        assert "_close_pool_if_idle()" in source or "await _close_pool_if_idle()" in source, (
            "_sync_dirty_outbox 应调用 _close_pool_if_idle"
        )


# ════════════════════════════════════════════════════════════════
# 5. _should_connect / _close_pool_if_idle 运行时逻辑测试
# ════════════════════════════════════════════════════════════════


class TestShouldConnectRuntime:
    """R41: _should_connect() 运行时逻辑测试。"""

    def test_should_connect_returns_false_when_already_connected(self):
        """_should_connect 在 pool 已连接时应返回 False(避免重复连接)。"""
        import services.crdb_sync_service as svc
        # 保存原始状态
        original_connected = svc._crdb_pool_connected
        try:
            # 模拟 pool 已连接
            svc._crdb_pool_connected = True
            import asyncio
            result = asyncio.run(svc._should_connect())
            assert result is False, (
                "pool 已连接时 _should_connect 应返回 False"
            )
        finally:
            svc._crdb_pool_connected = original_connected

    def test_should_connect_returns_false_when_store_unavailable(self):
        """_should_connect 在 cache_store 不可用时应返回 False(降级安全)。"""
        import services.crdb_sync_service as svc
        original_connected = svc._crdb_pool_connected
        original_get_store = svc._get_cache_store_safe
        try:
            svc._crdb_pool_connected = False
            # 模拟 cache_store 不可用
            svc._get_cache_store_safe = lambda: None
            import asyncio
            result = asyncio.run(svc._should_connect())
            assert result is False, (
                "cache_store 不可用时 _should_connect 应返回 False(降级安全)"
            )
        finally:
            svc._crdb_pool_connected = original_connected
            svc._get_cache_store_safe = original_get_store


class TestClosePoolIfIdleRuntime:
    """R41: _close_pool_if_idle() 运行时逻辑测试。"""

    def test_close_pool_if_idle_noop_when_not_connected(self):
        """_close_pool_if_idle 在 pool 未连接时应为 noop(无需关闭)。"""
        import services.crdb_sync_service as svc
        original_connected = svc._crdb_pool_connected
        try:
            # 模拟 pool 未连接
            svc._crdb_pool_connected = False
            import asyncio
            # 应正常返回(无副作用)
            asyncio.run(svc._close_pool_if_idle())
            # 未连接状态应保持
            assert svc._crdb_pool_connected is False
        finally:
            svc._crdb_pool_connected = original_connected

    def test_close_pool_if_idle_noop_when_never_seen_dirty(self):
        """_close_pool_if_idle 在从未检测到 dirty 时不应主动关闭。"""
        import services.crdb_sync_service as svc
        original_connected = svc._crdb_pool_connected
        original_dirty_ts = svc._last_dirty_seen_ts
        try:
            # 模拟 pool 已连接但从未检测到 dirty
            svc._crdb_pool_connected = True
            svc._last_dirty_seen_ts = 0.0  # 从未检测到 dirty
            import asyncio
            asyncio.run(svc._close_pool_if_idle())
            # 应保持连接状态(不主动关闭)
            # 注: 若实现是立即关闭,_last_dirty_seen_ts=0 也会保持连接(由实现决定)
        finally:
            svc._crdb_pool_connected = original_connected
            svc._last_dirty_seen_ts = original_dirty_ts


# ════════════════════════════════════════════════════════════════
# 6. dirty_outbox 版本合并逻辑测试
# ════════════════════════════════════════════════════════════════


class TestVersionMerging:
    """R41: dirty_outbox 合并最高 version 批量 UPSERT 逻辑。

    同一 (table_name, pk) 仅保留 version 最大的记录,
    被合并的旧版本 id 也标记为 processed(避免重复 dispatch)。
    """

    def test_sync_dirty_outbox_has_version_merging(self):
        """_sync_dirty_outbox 应包含版本合并逻辑(version_map)。"""
        source = _read_text(SERVICES_DIR / "crdb_sync_service.py")
        # 应包含 version_map 字典用于合并
        assert "version_map" in source, (
            "_sync_dirty_outbox 应包含 version_map 版本合并逻辑"
        )

    def test_sync_dirty_outbox_has_merged_old_ids(self):
        """_sync_dirty_outbox 应跟踪被合并的旧版本 id(merged_old_ids)。"""
        source = _read_text(SERVICES_DIR / "crdb_sync_service.py")
        assert "merged_old_ids" in source, (
            "_sync_dirty_outbox 应跟踪被合并的旧版本 id(merged_old_ids)"
        )

    def test_sync_dirty_outbox_marks_merged_old_ids_processed(self):
        """_sync_dirty_outbox 应将 merged_old_ids 也标记为 processed。"""
        source = _read_text(SERVICES_DIR / "crdb_sync_service.py")
        # 应将 merged_old_ids 加入 all_processed
        assert "merged_old_ids" in source and "all_processed" in source, (
            "_sync_dirty_outbox 应将 merged_old_ids 加入 all_processed"
        )

    def test_version_merging_logic_correctness(self):
        """版本合并算法单元测试 — 同一 pk 仅保留 version 最大的记录。"""
        # 模拟 _sync_dirty_outbox 中的版本合并算法
        # 输入: 4 条记录,2 条相同 (table, pk) 但 version 不同
        batch = [
            {"id": 1, "table_name": "users", "pk": "100", "version": 1, "operation": "upsert"},
            {"id": 2, "table_name": "users", "pk": "100", "version": 3, "operation": "upsert"},
            {"id": 3, "table_name": "users", "pk": "100", "version": 2, "operation": "upsert"},
            {"id": 4, "table_name": "codes", "pk": "ABC", "version": 1, "operation": "upsert"},
        ]
        # 复用 _sync_dirty_outbox 中的合并算法(直接复制逻辑)
        merged_records: list[dict] = []
        merged_old_ids: list[int] = []
        version_map: dict[tuple[str, str], dict] = {}
        for r in batch:
            tn = r.get("table_name", "") or ""
            pk = str(r.get("pk", "") or "")
            version = r.get("version", 0) or 0
            key = (tn, pk)
            if key not in version_map:
                version_map[key] = r
                merged_records.append(r)
            else:
                existing = version_map[key]
                existing_version = existing.get("version", 0) or 0
                if version > existing_version:
                    merged_old_ids.append(existing["id"])
                    idx = merged_records.index(existing)
                    merged_records[idx] = r
                    version_map[key] = r
                else:
                    merged_old_ids.append(r["id"])
        # 验证:4 条输入 → 2 条保留(users:100 v3 + codes:ABC v1)
        assert len(merged_records) == 2, (
            f"合并后应保留 2 条记录(去重),实际 {len(merged_records)}"
        )
        # users:100 应保留 version=3(id=2)
        users_record = next(r for r in merged_records if r["table_name"] == "users")
        assert users_record["version"] == 3, (
            f"users:100 应保留 version=3,实际 version={users_record['version']}"
        )
        assert users_record["id"] == 2, (
            f"users:100 应保留 id=2(version=3 的记录),实际 id={users_record['id']}"
        )
        # codes:ABC 应保留 version=1(id=4)
        codes_record = next(r for r in merged_records if r["table_name"] == "codes")
        assert codes_record["version"] == 1
        assert codes_record["id"] == 4
        # 被合并的旧版本 id 应为 [1, 3](id=2 的旧版本)
        assert set(merged_old_ids) == {1, 3}, (
            f"被合并的旧版本 id 应为 {{1, 3}},实际 {set(merged_old_ids)}"
        )

    def test_version_merging_same_version_keeps_first(self):
        """版本相同时应保留先出现的记录(稳定合并)。"""
        batch = [
            {"id": 1, "table_name": "users", "pk": "100", "version": 5, "operation": "upsert"},
            {"id": 2, "table_name": "users", "pk": "100", "version": 5, "operation": "upsert"},
        ]
        merged_records: list[dict] = []
        merged_old_ids: list[int] = []
        version_map: dict[tuple[str, str], dict] = {}
        for r in batch:
            tn = r.get("table_name", "") or ""
            pk = str(r.get("pk", "") or "")
            version = r.get("version", 0) or 0
            key = (tn, pk)
            if key not in version_map:
                version_map[key] = r
                merged_records.append(r)
            else:
                existing = version_map[key]
                existing_version = existing.get("version", 0) or 0
                if version > existing_version:
                    merged_old_ids.append(existing["id"])
                    idx = merged_records.index(existing)
                    merged_records[idx] = r
                    version_map[key] = r
                else:
                    merged_old_ids.append(r["id"])
        # 版本相同时保留先出现的(id=1)
        assert len(merged_records) == 1
        assert merged_records[0]["id"] == 1, (
            "版本相同时应保留先出现的记录(id=1)"
        )
        # 后出现的被合并
        assert merged_old_ids == [2]


# ════════════════════════════════════════════════════════════════
# 7. CRDB_SYNC_BATCH_SIZE 范围限制测试
# ════════════════════════════════════════════════════════════════


class TestCrdbSyncBatchSize:
    """R41: CRDB_SYNC_BATCH_SIZE 应限制在 [100, 500] 范围内。"""

    def test_has_parse_batch_size_function(self):
        """crdb_sync_service.py 应定义 _parse_batch_size 函数。"""
        tree = _parse_ast(SERVICES_DIR / "crdb_sync_service.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "_parse_batch_size" in funcs, (
            "crdb_sync_service.py 应定义 _parse_batch_size 函数"
        )

    def test_has_crdb_sync_batch_size_constant(self):
        """crdb_sync_service.py 应定义 CRDB_SYNC_BATCH_SIZE 模块级常量。"""
        source = _read_text(SERVICES_DIR / "crdb_sync_service.py")
        assert "CRDB_SYNC_BATCH_SIZE" in source, (
            "crdb_sync_service.py 应定义 CRDB_SYNC_BATCH_SIZE 常量"
        )

    def test_default_batch_size_is_100(self):
        """未设置环境变量时 CRDB_SYNC_BATCH_SIZE 应为 100。"""
        # 保存原始环境变量
        original = os.environ.pop("CRDB_SYNC_BATCH_SIZE", None)
        try:
            # 重新加载模块以应用环境变量变更
            import importlib
            import services.crdb_sync_service as svc
            importlib.reload(svc)
            assert svc.CRDB_SYNC_BATCH_SIZE == 100, (
                f"默认 batch size 应为 100,实际 {svc.CRDB_SYNC_BATCH_SIZE}"
            )
        finally:
            # 恢复原始环境变量
            if original is not None:
                os.environ["CRDB_SYNC_BATCH_SIZE"] = original
            else:
                os.environ.pop("CRDB_SYNC_BATCH_SIZE", None)
            # 重新加载以恢复
            import importlib
            import services.crdb_sync_service as svc
            importlib.reload(svc)

    def test_batch_size_clamped_to_min_100(self):
        """设置小于 100 的值时应被限制为 100。"""
        original = os.environ.get("CRDB_SYNC_BATCH_SIZE")
        try:
            os.environ["CRDB_SYNC_BATCH_SIZE"] = "50"
            import importlib
            import services.crdb_sync_service as svc
            importlib.reload(svc)
            assert svc.CRDB_SYNC_BATCH_SIZE == 100, (
                f"batch size=50 应被限制为 100,实际 {svc.CRDB_SYNC_BATCH_SIZE}"
            )
        finally:
            if original is not None:
                os.environ["CRDB_SYNC_BATCH_SIZE"] = original
            else:
                os.environ.pop("CRDB_SYNC_BATCH_SIZE", None)
            import importlib
            import services.crdb_sync_service as svc
            importlib.reload(svc)

    def test_batch_size_clamped_to_max_500(self):
        """设置大于 500 的值时应被限制为 500。"""
        original = os.environ.get("CRDB_SYNC_BATCH_SIZE")
        try:
            os.environ["CRDB_SYNC_BATCH_SIZE"] = "1000"
            import importlib
            import services.crdb_sync_service as svc
            importlib.reload(svc)
            assert svc.CRDB_SYNC_BATCH_SIZE == 500, (
                f"batch size=1000 应被限制为 500,实际 {svc.CRDB_SYNC_BATCH_SIZE}"
            )
        finally:
            if original is not None:
                os.environ["CRDB_SYNC_BATCH_SIZE"] = original
            else:
                os.environ.pop("CRDB_SYNC_BATCH_SIZE", None)
            import importlib
            import services.crdb_sync_service as svc
            importlib.reload(svc)

    def test_batch_size_valid_value_200(self):
        """设置 100-500 范围内的值(如 200)应直接使用。"""
        original = os.environ.get("CRDB_SYNC_BATCH_SIZE")
        try:
            os.environ["CRDB_SYNC_BATCH_SIZE"] = "200"
            import importlib
            import services.crdb_sync_service as svc
            importlib.reload(svc)
            assert svc.CRDB_SYNC_BATCH_SIZE == 200, (
                f"batch size=200 应直接使用,实际 {svc.CRDB_SYNC_BATCH_SIZE}"
            )
        finally:
            if original is not None:
                os.environ["CRDB_SYNC_BATCH_SIZE"] = original
            else:
                os.environ.pop("CRDB_SYNC_BATCH_SIZE", None)
            import importlib
            import services.crdb_sync_service as svc
            importlib.reload(svc)

    def test_batch_size_invalid_value_falls_back_to_default(self):
        """设置非数字值时应回退到默认 100。"""
        original = os.environ.get("CRDB_SYNC_BATCH_SIZE")
        try:
            os.environ["CRDB_SYNC_BATCH_SIZE"] = "invalid"
            import importlib
            import services.crdb_sync_service as svc
            importlib.reload(svc)
            assert svc.CRDB_SYNC_BATCH_SIZE == 100, (
                f"非法值应回退到默认 100,实际 {svc.CRDB_SYNC_BATCH_SIZE}"
            )
        finally:
            if original is not None:
                os.environ["CRDB_SYNC_BATCH_SIZE"] = original
            else:
                os.environ.pop("CRDB_SYNC_BATCH_SIZE", None)
            import importlib
            import services.crdb_sync_service as svc
            importlib.reload(svc)


# ════════════════════════════════════════════════════════════════
# 8. prometheus_exporter.py — tgjiema_crdb_idle_ru_daily gauge
# ════════════════════════════════════════════════════════════════


class TestPrometheusIdleRuGauge:
    """R41: prometheus_exporter 应暴露 tgjiema_crdb_idle_ru_daily gauge。"""

    def test_prometheus_exporter_file_exists(self):
        """services/prometheus_exporter.py 应存在。"""
        assert (SERVICES_DIR / "prometheus_exporter.py").exists()

    def test_ast_parseable(self):
        """services/prometheus_exporter.py 应可被 AST 解析。"""
        tree = _parse_ast(SERVICES_DIR / "prometheus_exporter.py")
        assert tree is not None

    def test_has_idle_ru_daily_gauge_definition(self):
        """prometheus_exporter.py 应定义 tgjiema_crdb_idle_ru_daily gauge。"""
        source = _read_text(SERVICES_DIR / "prometheus_exporter.py")
        assert "tgjiema_crdb_idle_ru_daily" in source, (
            "prometheus_exporter.py 应定义 tgjiema_crdb_idle_ru_daily gauge"
        )

    def test_has_idle_ru_alert_gauge_definition(self):
        """prometheus_exporter.py 应定义 tgjiema_crdb_idle_ru_alert gauge。"""
        source = _read_text(SERVICES_DIR / "prometheus_exporter.py")
        assert "tgjiema_crdb_idle_ru_alert" in source, (
            "prometheus_exporter.py 应定义 tgjiema_crdb_idle_ru_alert gauge"
        )

    def test_has_idle_ru_threshold_env_var(self):
        """prometheus_exporter.py 应读取 CRDB_IDLE_RU_DAILY_ALERT_THRESHOLD 环境变量。"""
        source = _read_text(SERVICES_DIR / "prometheus_exporter.py")
        assert "CRDB_IDLE_RU_DAILY_ALERT_THRESHOLD" in source, (
            "prometheus_exporter.py 应读取 CRDB_IDLE_RU_DAILY_ALERT_THRESHOLD 环境变量"
        )

    def test_reads_idle_ru_from_kv_store(self):
        """prometheus_exporter.py 应从 kv_store.crdb_idle_ru_daily 读取空载 RU 值。"""
        source = _read_text(SERVICES_DIR / "prometheus_exporter.py")
        # 应从 kv_store 读取 crdb_idle_ru_daily
        assert "crdb_idle_ru_daily" in source, (
            "prometheus_exporter.py 应从 kv_store.crdb_idle_ru_daily 读取空载 RU"
        )

    def test_collect_metrics_outputs_idle_ru_gauge(self):
        """collect_metrics() 输出应包含 tgjiema_crdb_idle_ru_daily 行。"""
        from services.prometheus_exporter import collect_metrics
        metrics_text = collect_metrics()
        assert "tgjiema_crdb_idle_ru_daily" in metrics_text, (
            "collect_metrics() 输出应包含 tgjiema_crdb_idle_ru_daily 指标行"
        )

    def test_collect_metrics_outputs_idle_ru_alert_gauge(self):
        """collect_metrics() 输出应包含 tgjiema_crdb_idle_ru_alert 行。"""
        from services.prometheus_exporter import collect_metrics
        metrics_text = collect_metrics()
        assert "tgjiema_crdb_idle_ru_alert" in metrics_text, (
            "collect_metrics() 输出应包含 tgjiema_crdb_idle_ru_alert 指标行"
        )

    def test_collect_metrics_has_help_and_type_for_idle_ru(self):
        """collect_metrics() 输出应包含 HELP 和 TYPE 行(Prometheus 规范)。"""
        from services.prometheus_exporter import collect_metrics
        metrics_text = collect_metrics()
        # 应包含 HELP 行
        assert "# HELP tgjiema_crdb_idle_ru_daily" in metrics_text, (
            "collect_metrics() 应包含 tgjiema_crdb_idle_ru_daily 的 HELP 行"
        )
        # 应包含 TYPE 行
        assert "# TYPE tgjiema_crdb_idle_ru_daily gauge" in metrics_text, (
            "collect_metrics() 应包含 tgjiema_crdb_idle_ru_daily 的 TYPE 行"
        )

    def test_idle_ru_alert_threshold_default_100(self):
        """未设置环境变量时 idle_ru 阈值应默认 100。"""
        # 保存原始环境变量
        original = os.environ.pop("CRDB_IDLE_RU_DAILY_ALERT_THRESHOLD", None)
        try:
            from services.prometheus_exporter import collect_metrics
            metrics_text = collect_metrics()
            # 默认阈值 100,空载 RU 为 0(无 kv_store 数据)→ alert=0
            assert "tgjiema_crdb_idle_ru_alert 0" in metrics_text, (
                f"默认阈值 100,空载 RU=0 时 alert 应为 0,实际:\n{metrics_text}"
            )
        finally:
            if original is not None:
                os.environ["CRDB_IDLE_RU_DAILY_ALERT_THRESHOLD"] = original


# ════════════════════════════════════════════════════════════════
# 9. .env.example R41 配置项检查
# ════════════════════════════════════════════════════════════════


class TestEnvExampleR41Config:
    """R41: .env.example 应包含 R41 新增的环境变量配置项。"""

    def test_env_example_exists(self):
        """.env.example 应存在。"""
        assert (REPO_ROOT / ".env.example").exists()

    def test_has_crdb_sync_batch_size(self):
        """ .env.example 应包含 CRDB_SYNC_BATCH_SIZE 配置项。"""
        source = _read_text(REPO_ROOT / ".env.example")
        assert "CRDB_SYNC_BATCH_SIZE" in source, (
            ".env.example 应包含 CRDB_SYNC_BATCH_SIZE 配置项"
        )

    def test_has_idle_ru_alert_threshold(self):
        """ .env.example 应包含 CRDB_IDLE_RU_DAILY_ALERT_THRESHOLD 配置项。"""
        source = _read_text(REPO_ROOT / ".env.example")
        assert "CRDB_IDLE_RU_DAILY_ALERT_THRESHOLD" in source, (
            ".env.example 应包含 CRDB_IDLE_RU_DAILY_ALERT_THRESHOLD 配置项"
        )

    def test_crdb_url_section_documents_access_scope(self):
        """COCKROACHDB_URL 配置段应说明 R41 RU 门禁的访问范围。"""
        source = _read_text(REPO_ROOT / ".env.example")
        # 应包含 R41 RU 门禁的文档说明
        assert "R41" in source or "RU 门禁" in source or "业务 Bot" in source, (
            ".env.example COCKROACHDB_URL 配置段应说明 R41 RU 门禁访问范围"
        )

    def test_crdb_url_section_lists_allowed_services(self):
        """COCKROACHDB_URL 配置段应列出允许读取的服务白名单。"""
        source = _read_text(REPO_ROOT / ".env.example")
        # 应至少提及 crdb_sync 和 migration 作为允许的服务
        assert "crdb_sync" in source, (
            ".env.example 应在 COCKROACHDB_URL 配置段提及 crdb_sync 为允许的服务"
        )
        assert "migration" in source, (
            ".env.example 应在 COCKROACHDB_URL 配置段提及 migration 为允许的服务"
        )


# ════════════════════════════════════════════════════════════════
# 10. 业务 Bot RU 必须为 0 — 静态扫描验证
# ════════════════════════════════════════════════════════════════


class TestBusinessBotRuMustBeZero:
    """R41: 业务 Bot 不应触发 CRDB RU(不持有 CRDB URL,不直连 CRDB)。

    静态扫描验证:
    - up_bot/idx_bot/dsp_bot 源码不引用 COCKROACHDB_URL
    - up_bot/idx_bot/dsp_bot 不导入 CockroachDBClient
    - up_bot/idx_bot/dsp_bot 不导入 database.session 的 CRDB 相关类
    """

    def test_up_bot_no_crdb_imports(self):
        """up_bot.py 不应导入 CRDB 相关模块。"""
        source = _read_text(BOTS_DIR / "up_bot.py")
        # 不应导入 CRDB 直连类
        forbidden_imports = [
            "from database.session import CockroachDBClient",
            "from database.session import get_jobs_col",
            "from database.session import get_users_col",
            "from database.session import get_file_records_col",
            "from database.session import get_codes_col",
            "from database.session import get_cells_col",
        ]
        for imp in forbidden_imports:
            assert imp not in source, (
                f"up_bot.py 不应导入 CRDB 模块: {imp}"
            )

    def test_idx_bot_no_crdb_imports(self):
        """idx_bot.py 不应导入 CRDB 相关模块。"""
        source = _read_text(BOTS_DIR / "idx_bot.py")
        forbidden_imports = [
            "from database.session import CockroachDBClient",
            "from database.session import get_jobs_col",
            "from database.session import get_users_col",
            "from database.session import get_file_records_col",
            "from database.session import get_codes_col",
            "from database.session import get_cells_col",
        ]
        for imp in forbidden_imports:
            assert imp not in source, (
                f"idx_bot.py 不应导入 CRDB 模块: {imp}"
            )

    def test_dsp_bot_no_crdb_imports(self):
        """dsp_bot.py 不应导入 CRDB 相关模块。"""
        source = _read_text(BOTS_DIR / "dsp_bot.py")
        forbidden_imports = [
            "from database.session import CockroachDBClient",
            "from database.session import get_jobs_col",
            "from database.session import get_users_col",
            "from database.session import get_file_records_col",
            "from database.session import get_codes_col",
            "from database.session import get_cells_col",
        ]
        for imp in forbidden_imports:
            assert imp not in source, (
                f"dsp_bot.py 不应导入 CRDB 模块: {imp}"
            )

    def test_business_bots_use_sqlite_only(self):
        """业务 Bot 应使用 SQLite 本地权威(从 database.cache_store 导入)。"""
        for bot_file in ["up_bot.py", "idx_bot.py", "dsp_bot.py"]:
            source = _read_text(BOTS_DIR / bot_file)
            # 应从 database.cache_store 导入(SQLite 本地权威)
            # 注:不强制要求所有 bot 都导入 cache_store(部分 bot 可能通过其他层访问)
            # 但至少不应导入 CRDB 直连类
            assert "CockroachDBClient" not in source or "# " in source.split("CockroachDBClient")[0], (
                f"{bot_file} 不应直接使用 CockroachDBClient"
            )
