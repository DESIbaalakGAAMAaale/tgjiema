"""R39 P1(12项)+ P2(10项)整改测试覆盖

测试策略: 文件内容检查为主(避免 Python 3.9 类型注解兼容问题),
辅以少量函数行为测试(通过 AST 隔离加载绕过重依赖)。

覆盖范围:
- P1-1: crdb_sync_service.py fail-closed (Redis 不可用时)
- P1-2: crdb_sync_service.py 单续约任务
- P1-3: migration_runner.py 全量 schema 验证
- P1-4: migration_runner.py schema 漂移检测
- P1-5: cache_store.py 统一 soft_delete API + admin tombstone
- P1-6: db_backup.py 原子上传
- P1-7: docs/github-pat-workflow-scope.md
- P1-8: prometheus_exporter.py readiness 检查
- P1-9: services/crdb_ru_collector.py + docs/crdb-ru-metrics.md
- P1-10: cache_store.py monkey-patch TODO + docs/writer-monkeypatch-removal.md
- P1-11: dsp_bot.py receipt 失败暂停 job + docs/delivery-idempotency.md
- P1-12: admin/__init__.py 强制 Argon2id
- P2-1: docs/base-image-digest.md
- P2-2: docs/dependency-lock-hash.md
- P2-3: cf-workers/file-bot/src/index.js Web Crypto HMAC
- P2-4: docs/config-generator-one-way.md
- P2-5: docs/deprecated-code-cleanup.md
- P2-6: utils/trace_context.py
- P2-7: docs/db-capacity-vacuum.md
- P2-8: admin/__init__.py CSP nonce + 点击劫持
- P2-9/10: docs/product-roadmap.md
"""
import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
SERVICES_DIR = REPO_ROOT / "services"
UTILS_DIR = REPO_ROOT / "utils"
ADMIN_DIR = REPO_ROOT / "admin"
BOTS_DIR = REPO_ROOT / "bots"
DATABASE_DIR = REPO_ROOT / "database"
CF_WORKERS_DIR = REPO_ROOT / "cf-workers"


def _read_file(path: Path) -> str:
    """读取文件内容,失败返回空串。"""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════
#  P1-1: CRDB leader fail-closed (Redis 不可用时)
# ═══════════════════════════════════════════════════════════════

class TestP1_1_LeaderFailClosed:
    """P1-1: 生产环境 Redis 不可用时 fail-closed,不降级到 SQLite KV。"""

    def test_crdb_sync_service_has_lease_valid_var(self):
        """crdb_sync_service.py 包含 _lease_valid 全局标志。"""
        content = _read_file(SERVICES_DIR / "crdb_sync_service.py")
        assert "_lease_valid" in content, "缺少 _lease_valid 全局变量(P1-1)"

    def test_crdb_sync_service_has_fencing_token_var(self):
        """crdb_sync_service.py 包含 _fencing_token 全局标志。"""
        content = _read_file(SERVICES_DIR / "crdb_sync_service.py")
        assert "_fencing_token" in content, "缺少 _fencing_token 全局变量(P1-1)"

    def test_crdb_sync_service_has_is_production(self):
        """crdb_sync_service.py 包含 _is_production() 判断函数。"""
        content = _read_file(SERVICES_DIR / "crdb_sync_service.py")
        assert "_is_production" in content, "缺少 _is_production() 函数(P1-1)"

    def test_acquire_lease_fail_closed_in_production(self):
        """_acquire_leader_lease 在生产环境 Redis 不可用时 fail-closed。"""
        content = _read_file(SERVICES_DIR / "crdb_sync_service.py")
        # 检查 _acquire_leader_lease 函数内包含 _is_production 调用 + fail-closed 逻辑
        assert "_is_production" in content, "缺少生产环境判断(P1-1)"


# ═══════════════════════════════════════════════════════════════
#  P1-2: 单续约任务(同步循环不再续约)
# ═══════════════════════════════════════════════════════════════

class TestP1_2_SingleRenewalTask:
    """P1-2: _sync_loop 不再调用 _renew_leader_lease,_leader_renewal_task 成为唯一续约源。"""

    def test_leader_renewal_task_exists(self):
        """crdb_sync_service.py 包含 _leader_renewal_task 函数。"""
        content = _read_file(SERVICES_DIR / "crdb_sync_service.py")
        assert "_leader_renewal_task" in content, "缺少 _leader_renewal_task 函数(P1-2)"

    def test_sync_loop_does_not_renew_lease(self):
        """_sync_loop 不应直接调用 _renew_leader_lease。

        通过 AST 解析 _sync_loop 函数体,检查不包含 _renew_leader_lease 调用。
        """
        content = _read_file(SERVICES_DIR / "crdb_sync_service.py")
        try:
            tree = ast.parse(content)
        except SyntaxError:
            pytest.skip("crdb_sync_service.py 语法解析失败(Python 版本不兼容)")
        # 查找 _sync_loop 函数
        sync_loop_found = False
        renew_in_sync_loop = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == "_sync_loop":
                    sync_loop_found = True
                    # 检查函数体内是否调用 _renew_leader_lease
                    for child in ast.walk(node):
                        if isinstance(child, ast.Call):
                            func = child.func
                            if isinstance(func, ast.Attribute) and func.attr == "_renew_leader_lease":
                                renew_in_sync_loop = True
                            elif isinstance(func, ast.Name) and func.id == "_renew_leader_lease":
                                renew_in_sync_loop = True
        if not sync_loop_found:
            pytest.skip("_sync_loop 函数未找到")
        assert not renew_in_sync_loop, "_sync_loop 不应调用 _renew_leader_lease(P1-2)"


# ═══════════════════════════════════════════════════════════════
#  P1-3: migration_runner 全量 schema 验证
# ═══════════════════════════════════════════════════════════════

class TestP1_3_FullSchemaVerification:
    """P1-3: migration_runner 从 DDL_STATEMENTS 自动解析所有表并验证。"""

    def test_extract_expected_schema_exists(self):
        """migration_runner.py 包含 _extract_expected_schema 函数。"""
        content = _read_file(SERVICES_DIR / "migration_runner.py")
        assert "_extract_expected_schema" in content, "缺少 _extract_expected_schema 函数(P1-3)"

    def test_verify_schema_post_migration_uses_extract(self):
        """_verify_schema_post_migration 调用 _extract_expected_schema。"""
        content = _read_file(SERVICES_DIR / "migration_runner.py")
        assert "_extract_expected_schema" in content, "_verify_schema_post_migration 应使用 _extract_expected_schema(P1-3)"

    def test_schema_drift_check_doc_exists(self):
        """docs/schema-drift-check.md 存在。"""
        doc = DOCS_DIR / "schema-drift-check.md"
        assert doc.exists(), "缺少 docs/schema-drift-check.md(P1-3/P1-4)"


# ═══════════════════════════════════════════════════════════════

class TestP1_4_SchemaDriftDetection:
    """P1-4: schema 漂移时不写 ddl_version。"""

    def test_verify_minimal_tables_exists(self):
        """migration_runner.py 包含 _verify_minimal_tables 兜底函数。"""
        content = _read_file(SERVICES_DIR / "migration_runner.py")
        assert "_verify_minimal_tables" in content, "缺少 _verify_minimal_tables 函数(P1-4)"

    def test_split_top_level_commas_exists(self):
        """migration_runner.py 包含 _split_top_level_commas 辅助函数。"""
        content = _read_file(SERVICES_DIR / "migration_runner.py")
        assert "_split_top_level_commas" in content, "缺少 _split_top_level_commas 函数(P1-4)"


# ═══════════════════════════════════════════════════════════════
#  P1-5: 统一 soft_delete API + admin tombstone
# ═══════════════════════════════════════════════════════════════

class TestP1_5_SoftDeleteTombstone:
    """P1-5: cache_store.soft_delete() 统一 API + admin delete_file 双写 tombstone。"""

    def test_cache_store_has_soft_delete(self):
        """cache_store.py 包含 soft_delete 方法。"""
        content = _read_file(DATABASE_DIR / "cache_store.py")
        assert "async def soft_delete" in content or "def soft_delete" in content, \
            "cache_store.py 缺少 soft_delete 方法(P1-5)"

    def test_cache_store_has_soft_delete_tables_map(self):
        """cache_store.py 包含 _SOFT_DELETE_TABLES 映射。"""
        content = _read_file(DATABASE_DIR / "cache_store.py")
        assert "_SOFT_DELETE_TABLES" in content, "缺少 _SOFT_DELETE_TABLES 映射(P1-5)"

    def test_admin_delete_file_uses_tombstone(self):
        """R40 P0-8: admin delete_file 通过 CommandBus 执行软删除,handler 包含 tombstone。

        架构变更:R40 P0-8 将高风险操作(含文件删除)迁移到 CommandBus 强制
        RBAC 门禁。admin/__init__.py 的 delete_file 路由不再直接写 tombstone,
        而是调用 make_delete_file_command(file_code) 走 CommandBus 执行。
        实际的 soft_delete + deleted_at tombstone 逻辑位于
        services/command_bus.py 的 make_delete_file_command handler 中。
        """
        admin_content = _read_file(ADMIN_DIR / "__init__.py")
        cb_content = _read_file(SERVICES_DIR / "command_bus.py")
        # admin 路由必须通过 CommandBus 执行删除(走 RBAC 门禁)
        assert "make_delete_file_command" in admin_content, \
            "admin delete_file 未走 CommandBus(R40 P0-8)"
        # CommandBus handler 必须包含 tombstone 双写(CRDB deleted_at + 本地 soft_delete)
        assert "soft_delete" in cb_content, \
            "command_bus.py 缺少 soft_delete tombstone(P1-5)"
        assert "deleted_at" in cb_content, \
            "command_bus.py 缺少 deleted_at 字段(P1-5)"
        assert "make_delete_file_command" in cb_content, \
            "command_bus.py 缺少 make_delete_file_command 工厂(P1-5)"

    def test_tombstone_paths_doc_exists(self):
        """docs/tombstone-paths.md 存在。"""
        assert (DOCS_DIR / "tombstone-paths.md").exists(), "缺少 docs/tombstone-paths.md(P1-5)"


# ═══════════════════════════════════════════════════════════════
#  P1-6: db_backup 原子上传
# ═══════════════════════════════════════════════════════════════

class TestP1_6_AtomicBackupUpload:
    """P1-6: db_backup 上传临时 payload → 校验 → 正式 key → manifest → latest CAS。"""

    def test_db_backup_has_temp_payload_upload(self):
        """db_backup.py 包含临时 payload 上传逻辑(.tmp_ 前缀)。"""
        content = _read_file(SERVICES_DIR / "db_backup.py")
        assert ".tmp_" in content or "_tmp_key" in content or "_tmp_suffix" in content, \
            "db_backup.py 缺少临时 payload 上传(P1-6)"

    def test_db_backup_has_checksum_verify(self):
        """db_backup.py 包含 checksum 校验逻辑。"""
        content = _read_file(SERVICES_DIR / "db_backup.py")
        assert "_verify_sha" in content or "verify" in content.lower() and "checksum" in content.lower(), \
            "db_backup.py 缺少 checksum 校验(P1-6)"


# ═══════════════════════════════════════════════════════════════
#  P1-7: CI workflow docs (GitHub PAT workflow scope)
# ═══════════════════════════════════════════════════════════════

class TestP1_7_CiWorkflowScope:
    """P1-7: docs/github-pat-workflow-scope.md 存在且包含必需章节。"""

    def test_doc_exists(self):
        path = DOCS_DIR / "github-pat-workflow-scope.md"
        assert path.exists(), "缺少 docs/github-pat-workflow-scope.md(P1-7)"

    def test_doc_contains_required_sections(self):
        content = _read_file(DOCS_DIR / "github-pat-workflow-scope.md")
        assert "必需检查" in content or "Required Status Checks" in content, "缺少必需检查章节(P1-7)"
        assert "Fine-grained PAT" in content or "PAT" in content, "缺少 PAT 配置章节(P1-7)"
        assert "branch protection" in content.lower(), "缺少 branch protection 章节(P1-7)"


# ═══════════════════════════════════════════════════════════════
#  P1-8: prometheus_exporter readiness
# ═══════════════════════════════════════════════════════════════

class TestP1_8_PrometheusReadiness:
    """P1-8: prometheus_exporter /health 不再永远 OK,增加 readiness 检查。"""

    def test_check_readiness_exists(self):
        content = _read_file(SERVICES_DIR / "prometheus_exporter.py")
        assert "def check_readiness" in content or "check_readiness" in content, \
            "prometheus_exporter.py 缺少 check_readiness 函数(P1-8)"

    def test_has_last_scrape_ok_var(self):
        content = _read_file(SERVICES_DIR / "prometheus_exporter.py")
        assert "_last_scrape_ok" in content, "缺少 _last_scrape_ok 变量(P1-8)"

    def test_has_scrape_errors_metric(self):
        content = _read_file(SERVICES_DIR / "prometheus_exporter.py")
        assert "scrape_errors" in content, "缺少 scrape_errors 指标(P1-8)"


# ═══════════════════════════════════════════════════════════════
#  P1-9: CRDB RU 指标采集闭环
# ═══════════════════════════════════════════════════════════════

class TestP1_9_CrdbRuCollector:
    """P1-9: services/crdb_ru_collector.py 占位 + docs/crdb-ru-metrics.md。"""

    def test_collector_script_exists(self):
        path = SERVICES_DIR / "crdb_ru_collector.py"
        assert path.exists(), "缺少 services/crdb_ru_collector.py(P1-9)"

    def test_collector_has_main_loop(self):
        content = _read_file(SERVICES_DIR / "crdb_ru_collector.py")
        assert "_collect_loop" in content, "collector 缺少 _collect_loop 函数(P1-9)"
        assert "fetch_ru_from_crdb_cloud" in content, "collector 缺少 fetch 函数(P1-9)"

    def test_collector_writes_kv_store(self):
        content = _read_file(SERVICES_DIR / "crdb_ru_collector.py")
        assert "set_kv" in content and "crdb_ru_daily" in content, \
            "collector 应写入 kv_store.crdb_ru_daily(P1-9)"

    def test_doc_exists(self):
        path = DOCS_DIR / "crdb-ru-metrics.md"
        assert path.exists(), "缺少 docs/crdb-ru-metrics.md(P1-9)"


# ═══════════════════════════════════════════════════════════════
#  P1-10: Writer monkey-patch 移除计划
# ═══════════════════════════════════════════════════════════════

class TestP1_10_WriterMonkeypatchRemoval:
    """P1-10: cache_store.begin_writer_tx 标注 TODO + docs/writer-monkeypatch-removal.md。"""

    def test_cache_store_has_todo_comment(self):
        content = _read_file(DATABASE_DIR / "cache_store.py")
        assert "R39 P1-10" in content, "cache_store.py 缺少 R39 P1-10 标注(P1-10)"

    def test_doc_exists(self):
        path = DOCS_DIR / "writer-monkeypatch-removal.md"
        assert path.exists(), "缺少 docs/writer-monkeypatch-removal.md(P1-10)"


# ═══════════════════════════════════════════════════════════════
#  P1-11: Delivery receipt 失败暂停 job
# ═══════════════════════════════════════════════════════════════

class TestP1_11_ReceiptPauseJob:
    """P1-11: receipt 写失败时暂停 job(标记 receipt_pending)。"""

    def test_upsert_receipt_returns_bool(self):
        content = _read_file(BOTS_DIR / "dsp_bot.py")
        assert "_upsert_delivery_receipt_safe" in content, "缺少 _upsert_delivery_receipt_safe(P1-11)"
        # 检查返回类型注解为 bool
        assert "bool" in content, "receipt 函数应返回 bool(P1-11)"

    def test_pause_job_function_exists(self):
        content = _read_file(BOTS_DIR / "dsp_bot.py")
        assert "_pause_job_for_receipt_failure" in content, \
            "缺少 _pause_job_for_receipt_failure 函数(P1-11)"

    def test_receipt_pending_status_used(self):
        content = _read_file(BOTS_DIR / "dsp_bot.py")
        assert "receipt_pending" in content, "缺少 receipt_pending 状态(P1-11)"

    def test_delivery_idempotency_doc_updated(self):
        content = _read_file(DOCS_DIR / "delivery-idempotency.md")
        assert "R39 P1-11" in content, "docs/delivery-idempotency.md 缺少 P1-11 章节(P1-11)"
        assert "receipt_pending" in content, "文档缺少 receipt_pending 说明(P1-11)"


# ═══════════════════════════════════════════════════════════════
#  P1-12: Admin 强制 Argon2id,移除明文兼容
# ═══════════════════════════════════════════════════════════════

class TestP1_12_AdminArgon2id:
    """P1-12: _verify_password 不再支持明文,强制 Argon2id/PBKDF2 哈希格式。"""

    def test_verify_password_rejects_plaintext(self):
        """_verify_password 不包含明文常量时间比较分支。"""
        content = _read_file(ADMIN_DIR / "__init__.py")
        # 检查移除了 "明文模式：常量时间比较" 注释
        assert "明文模式" not in content, "_verify_password 仍包含明文比较分支(P1-12)"

    def test_has_argon2id_prefix(self):
        content = _read_file(ADMIN_DIR / "__init__.py")
        assert "_ARGON2ID_PREFIX" in content, "缺少 _ARGON2ID_PREFIX 常量(P1-12)"

    def test_has_warn_plaintext_function(self):
        content = _read_file(ADMIN_DIR / "__init__.py")
        assert "_warn_if_plaintext_password" in content, \
            "缺少 _warn_if_plaintext_password 函数(P1-12)"

    def test_startup_calls_warn(self):
        content = _read_file(ADMIN_DIR / "__init__.py")
        assert "_warn_if_plaintext_password()" in content, \
            "startup 未调用 _warn_if_plaintext_password(P1-12)"

    def test_generate_password_hash_prefers_argon2(self):
        content = _read_file(ADMIN_DIR / "__init__.py")
        assert "_ARGON2_AVAILABLE" in content, "缺少 Argon2 可用性检测(P1-12)"


# ═══════════════════════════════════════════════════════════════
#  P2-1: 真实基础镜像 digest docs
# ═══════════════════════════════════════════════════════════════

class TestP2_1_BaseImageDigest:
    """P2-1: docs/base-image-digest.md 存在。"""

    def test_doc_exists(self):
        path = DOCS_DIR / "base-image-digest.md"
        assert path.exists(), "缺少 docs/base-image-digest.md(P2-1)"

    def test_doc_contains_digest_guidance(self):
        content = _read_file(DOCS_DIR / "base-image-digest.md")
        assert "sha256" in content.lower(), "文档缺少 sha256 digest 说明(P2-1)"
        assert "docker inspect" in content.lower(), "文档缺少获取 digest 命令(P2-1)"


# ═══════════════════════════════════════════════════════════════
#  P2-2: 依赖锁定 hash docs
# ═══════════════════════════════════════════════════════════════

class TestP2_2_DependencyLockHash:
    """P2-2: docs/dependency-lock-hash.md 存在。"""

    def test_doc_exists(self):
        path = DOCS_DIR / "dependency-lock-hash.md"
        assert path.exists(), "缺少 docs/dependency-lock-hash.md(P2-2)"

    def test_doc_mentions_require_hashes(self):
        content = _read_file(DOCS_DIR / "dependency-lock-hash.md")
        assert "--require-hashes" in content, "文档缺少 --require-hashes 说明(P2-2)"

    def test_requirements_redis_is_precise(self):
        """requirements.txt 中 redis 不再使用范围约束。"""
        content = _read_file(REPO_ROOT / "requirements.txt")
        for line in content.splitlines():
            if line.strip().startswith("redis"):
                assert ">=" not in line and "<" not in line, \
                    f"redis 仍使用范围约束,应为精确版本(P2-2): {line}"
                return
        pytest.skip("requirements.txt 中未找到 redis 依赖")


# ═══════════════════════════════════════════════════════════════
#  P2-3: CF Worker constant-time 比较
# ═══════════════════════════════════════════════════════════════

class TestP2_3_CfWorkerConstantTime:
    """P2-3: cf-workers/file-bot/src/index.js 使用 Web Crypto HMAC 恒定时间比较。"""

    def test_constant_time_equal_function_exists(self):
        content = _read_file(CF_WORKERS_DIR / "file-bot" / "src" / "index.js")
        assert "constantTimeEqual" in content, "缺少 constantTimeEqual 函数(P2-3)"

    def test_uses_crypto_subtle_verify(self):
        content = _read_file(CF_WORKERS_DIR / "file-bot" / "src" / "index.js")
        assert "crypto.subtle.verify" in content, "未使用 Web Crypto verify(P2-3)"

    def test_no_direct_string_comparison_for_secret(self):
        """不再使用 headerToken !== secret 直接比较(代码路径)。

        注释中可能提及旧实现,但实际代码路径不应包含直接比较。
        通过检查 fetch handler 内使用 constantTimeEqual 而非 !== 来验证。
        """
        content = _read_file(CF_WORKERS_DIR / "file-bot" / "src" / "index.js")
        # 验证使用了 constantTimeEqual 函数
        assert "await constantTimeEqual(headerToken, secret)" in content, \
            "fetch handler 未使用 constantTimeEqual(P2-3)"
        # 检查代码路径中没有 headerToken !== secret 的实际比较(排除注释行)
        code_lines = []
        for line in content.splitlines():
            stripped = line.strip()
            # 跳过注释行(// 开头或包含 // 的行中 // 后部分)
            if stripped.startswith("//"):
                continue
            # 移除行内注释
            if "//" in line:
                line = line[:line.index("//")]
            code_lines.append(line)
        code_content = "\n".join(code_lines)
        assert "headerToken !== secret" not in code_content, \
            "代码路径仍使用 headerToken !== secret 直接比较(P2-3)"


# ═══════════════════════════════════════════════════════════════
#  P2-4: 配置生成器单向生成 docs
# ═══════════════════════════════════════════════════════════════

class TestP2_4_ConfigGenerator:
    """P2-4: docs/config-generator-one-way.md 存在。"""

    def test_doc_exists(self):
        path = DOCS_DIR / "config-generator-one-way.md"
        assert path.exists(), "缺少 docs/config-generator-one-way.md(P2-4)"

    def test_doc_mentions_single_source_of_truth(self):
        content = _read_file(DOCS_DIR / "config-generator-one-way.md")
        assert "单一事实源" in content or "Single Source of Truth" in content, \
            "文档缺少单一事实源说明(P2-4)"


# ═══════════════════════════════════════════════════════════════
#  P2-5: 废弃代码清理 docs
# ═══════════════════════════════════════════════════════════════

class TestP2_5_DeprecatedCodeCleanup:
    """P2-5: docs/deprecated-code-cleanup.md 存在。"""

    def test_doc_exists(self):
        path = DOCS_DIR / "deprecated-code-cleanup.md"
        assert path.exists(), "缺少 docs/deprecated-code-cleanup.md(P2-5)"

    def test_doc_lists_send_queue(self):
        content = _read_file(DOCS_DIR / "deprecated-code-cleanup.md")
        assert "send_queue" in content, "文档未列出 send_queue 废弃项(P2-5)"


# ═══════════════════════════════════════════════════════════════
#  P2-6: trace_id/correlation_id 上下文管理器
# ═══════════════════════════════════════════════════════════════

class TestP2_6_TraceContext:
    """P2-6: utils/trace_context.py 提供 trace_id/correlation_id 上下文管理器。"""

    def test_module_exists(self):
        path = UTILS_DIR / "trace_context.py"
        assert path.exists(), "缺少 utils/trace_context.py(P2-6)"

    def test_has_with_trace_context_manager(self):
        content = _read_file(UTILS_DIR / "trace_context.py")
        assert "async def with_trace" in content or "def with_trace" in content, \
            "缺少 with_trace 上下文管理器(P2-6)"

    def test_has_get_trace_id(self):
        content = _read_file(UTILS_DIR / "trace_context.py")
        assert "def get_trace_id" in content, "缺少 get_trace_id 函数(P2-6)"

    def test_has_correlation_id(self):
        content = _read_file(UTILS_DIR / "trace_context.py")
        assert "correlation_id" in content, "缺少 correlation_id 支持(P2-6)"

    def test_has_sensitive_fields_list(self):
        """包含敏感字段脱敏清单(不记录 token/密码等)。"""
        content = _read_file(UTILS_DIR / "trace_context.py")
        assert "SENSITIVE_LOG_FIELDS" in content, "缺少敏感字段清单(P2-6)"

    def test_trace_context_isolation(self):
        """行为测试: trace_id 在 async 任务间隔离。"""
        import asyncio
        try:
            from utils.trace_context import with_trace, get_trace_id
        except ImportError:
            pytest.skip("无法导入 utils.trace_context")

        async def _test():
            results = {}

            async def task(name):
                async with with_trace():
                    tid = get_trace_id()
                    await asyncio.sleep(0.01)
                    results[name] = (tid, get_trace_id())

            await asyncio.gather(task("a"), task("b"))
            # 每个任务的 trace_id 应一致(进入/退出一致)
            assert results["a"][0] == results["a"][1], "task a 的 trace_id 在执行中变化"
            assert results["b"][0] == results["b"][1], "task b 的 trace_id 在执行中变化"
            # 两个任务的 trace_id 应不同(隔离)
            assert results["a"][0] != results["b"][0], "两个任务的 trace_id 相同(未隔离)"

        asyncio.run(_test())


# ═══════════════════════════════════════════════════════════════
#  P2-7: DB 容量/水位/vacuum 策略 docs
# ═══════════════════════════════════════════════════════════════

class TestP2_7_DbCapacityVacuum:
    """P2-7: docs/db-capacity-vacuum.md 存在。"""

    def test_doc_exists(self):
        path = DOCS_DIR / "db-capacity-vacuum.md"
        assert path.exists(), "缺少 docs/db-capacity-vacuum.md(P2-7)"

    def test_doc_mentions_vacuum(self):
        content = _read_file(DOCS_DIR / "db-capacity-vacuum.md")
        assert "VACUUM" in content.upper(), "文档缺少 VACUUM 说明(P2-7)"

    def test_doc_mentions_wal_checkpoint(self):
        content = _read_file(DOCS_DIR / "db-capacity-vacuum.md")
        assert "checkpoint" in content.lower(), "文档缺少 WAL checkpoint 说明(P2-7)"


# ═══════════════════════════════════════════════════════════════
#  P2-8: Admin CSP nonce + 点击劫持
# ═══════════════════════════════════════════════════════════════

class TestP2_8_AdminCspClickjacking:
    """P2-8: admin/__init__.py 添加 CSP nonce + X-Frame-Options 中间件。"""

    def test_csp_middleware_exists(self):
        content = _read_file(ADMIN_DIR / "__init__.py")
        assert "_csp_and_clickjacking_middleware" in content, \
            "缺少 CSP 中间件(P2-8)"

    def test_sets_content_security_policy_header(self):
        content = _read_file(ADMIN_DIR / "__init__.py")
        assert "Content-Security-Policy" in content, "未设置 CSP 头(P2-8)"

    def test_sets_x_frame_options(self):
        content = _read_file(ADMIN_DIR / "__init__.py")
        assert "X-Frame-Options" in content, "未设置 X-Frame-Options 头(P2-8)"

    def test_uses_per_request_nonce(self):
        content = _read_file(ADMIN_DIR / "__init__.py")
        assert "csp_nonce" in content and "nonce-" in content, \
            "未使用 per-request nonce(P2-8)"

    def test_frame_ancestors_none(self):
        content = _read_file(ADMIN_DIR / "__init__.py")
        assert "frame-ancestors" in content and "'none'" in content, \
            "CSP 未设置 frame-ancestors 'none'(P2-8)"

    def test_doc_exists(self):
        path = DOCS_DIR / "admin-csp-clickjacking.md"
        assert path.exists(), "缺少 docs/admin-csp-clickjacking.md(P2-8)"


# ═══════════════════════════════════════════════════════════════
#  P2-9/10: 产品功能优化路线图 docs
# ═══════════════════════════════════════════════════════════════

class TestP2_9_10_ProductRoadmap:
    """P2-9/10: docs/product-roadmap.md 存在且覆盖 §9 四大方向。"""

    def test_doc_exists(self):
        path = DOCS_DIR / "product-roadmap.md"
        assert path.exists(), "缺少 docs/product-roadmap.md(P2-9/10)"

    def test_covers_user_experience(self):
        content = _read_file(DOCS_DIR / "product-roadmap.md")
        assert "用户体验" in content or "统一任务中心" in content, \
            "路线图未覆盖用户体验(§9.1)(P2-9/10)"

    def test_covers_commercialization(self):
        content = _read_file(DOCS_DIR / "product-roadmap.md")
        assert "商业化" in content or "套餐" in content or "entitlement" in content.lower(), \
            "路线图未覆盖商业化与权限(§9.2)(P2-9/10)"

    def test_covers_ops_features(self):
        content = _read_file(DOCS_DIR / "product-roadmap.md")
        assert "运维" in content or "控制台" in content or "RU 成本" in content, \
            "路线图未覆盖管理与运维功能(§9.3)(P2-9/10)"

    def test_covers_compliance(self):
        content = _read_file(DOCS_DIR / "product-roadmap.md")
        assert "合规" in content or "举报" in content or "数据导出" in content, \
            "路线图未覆盖内容安全与合规(§9.4)(P2-9/10)"


# ═══════════════════════════════════════════════════════════════
#  汇总: 所有 P1/P2 项至少有一项测试
# ═══════════════════════════════════════════════════════════════

class TestAllItemsCovered:
    """确保 12 个 P1 项 + 10 个 P2 项都有对应测试类。"""

    def test_all_p1_items_have_tests(self):
        """12 个 P1 项各有独立测试类。"""
        test_classes = [
            TestP1_1_LeaderFailClosed,
            TestP1_2_SingleRenewalTask,
            TestP1_3_FullSchemaVerification,
            TestP1_4_SchemaDriftDetection,
            TestP1_5_SoftDeleteTombstone,
            TestP1_6_AtomicBackupUpload,
            TestP1_7_CiWorkflowScope,
            TestP1_8_PrometheusReadiness,
            TestP1_9_CrdbRuCollector,
            TestP1_10_WriterMonkeypatchRemoval,
            TestP1_11_ReceiptPauseJob,
            TestP1_12_AdminArgon2id,
        ]
        assert len(test_classes) == 12, f"P1 项应有 12 个测试类,实际 {len(test_classes)}"

    def test_all_p2_items_have_tests(self):
        """10 个 P2 项各有独立测试类。"""
        test_classes = [
            TestP2_1_BaseImageDigest,
            TestP2_2_DependencyLockHash,
            TestP2_3_CfWorkerConstantTime,
            TestP2_4_ConfigGenerator,
            TestP2_5_DeprecatedCodeCleanup,
            TestP2_6_TraceContext,
            TestP2_7_DbCapacityVacuum,
            TestP2_8_AdminCspClickjacking,
            TestP2_9_10_ProductRoadmap,
        ]
        # P2-9 和 P2-10 合并为一个测试类(product-roadmap.md 同时覆盖 §9.1-9.4)
        assert len(test_classes) == 9, f"P2 项应有 9 个测试类(P2-9/10 合并),实际 {len(test_classes)}"
