"""R38 P1 + P2 整改测试 — 文件内容检查为主。

覆盖 9 项 P1 + 7 项 P2 整改,通过读取源码/配置/文档文件验证关键标记存在。
不依赖运行时基础设施(CRDB/Redis/Telegram),仅检查文件内容。

运行:
  py -3 -m pytest tests/test_r38_p1_p2.py -v --tb=short
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_file(rel_path: str) -> str:
    """读取项目内文件,返回全文。"""
    fpath = _PROJECT_ROOT / rel_path
    if not fpath.exists():
        pytest.fail(f"文件不存在: {rel_path}")
    return fpath.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════
#  P1-1: crdb_sync 懒加载 CRDB pool
# ═══════════════════════════════════════════════════════════


class TestP1_1CrdbSyncLazyLoad:
    """P1-1: crdb_sync 发现 dirty 才创建 CRDB pool,空闲时关闭。"""

    def test_crdb_idle_close_threshold_exists(self):
        content = _read_file("services/crdb_sync_service.py")
        assert "CRDB_IDLE_CLOSE_THRESHOLD" in content, (
            "P1-1: 缺少 CRDB_IDLE_CLOSE_THRESHOLD 常量"
        )

    def test_lazy_connect_function_exists(self):
        content = _read_file("services/crdb_sync_service.py")
        assert "_lazy_connect_crdb" in content, (
            "P1-1: 缺少 _lazy_connect_crdb() 懒加载函数"
        )

    def test_close_crdb_only_function_exists(self):
        content = _read_file("services/crdb_sync_service.py")
        assert "_close_crdb_only" in content, (
            "P1-1: 缺少 _close_crdb_only() 关闭函数"
        )

    def test_init_sqlite_only_function_exists(self):
        content = _read_file("services/crdb_sync_service.py")
        assert "_init_sqlite_only" in content, (
            "P1-1: 缺少 _init_sqlite_only() 初始化函数"
        )

    def test_r38_p1_1_annotation_exists(self):
        content = _read_file("services/crdb_sync_service.py")
        assert "R38 P1-1" in content, (
            "P1-1: 缺少 R38 P1-1 中文注释标注"
        )


# ═══════════════════════════════════════════════════════════
#  P1-2: dirty_outbox 统一表
# ═══════════════════════════════════════════════════════════


class TestP1_2DirtyOutbox:
    """P1-2: dirty_outbox 表迁移到统一表。"""

    def test_dirty_outbox_table_exists(self):
        content = _read_file("database/cache_store.py")
        assert "dirty_outbox" in content, (
            "P1-2: cache_store.py 缺少 dirty_outbox 表定义"
        )

    def test_add_dirty_outbox_method_exists(self):
        content = _read_file("database/cache_store.py")
        assert "add_dirty_outbox" in content, (
            "P1-2: 缺少 add_dirty_outbox() 方法"
        )

    def test_get_dirty_outbox_batch_method_exists(self):
        content = _read_file("database/cache_store.py")
        assert "get_dirty_outbox_batch" in content, (
            "P1-2: 缺少 get_dirty_outbox_batch() 方法"
        )

    def test_mark_dirty_processed_method_exists(self):
        content = _read_file("database/cache_store.py")
        assert "mark_dirty_processed" in content, (
            "P1-2: 缺少 mark_dirty_processed() 方法"
        )


# ═══════════════════════════════════════════════════════════
#  P1-3: Tombstone 全路径 soft-delete
# ═══════════════════════════════════════════════════════════


class TestP1_3TombstoneSoftDelete:
    """P1-3: DELETE → UPDATE SET deleted_at 软删除。"""

    def test_file_records_deleted_at_column(self):
        content = _read_file("database/cache_store.py")
        assert "deleted_at" in content, (
            "P1-3: cache_store.py 缺少 deleted_at 列"
        )

    def test_delete_file_record_uses_soft_delete(self):
        content = _read_file("database/cache_store.py")
        # 验证 delete_file_record_local 使用 UPDATE 而非 DELETE
        assert "SET deleted_at" in content, (
            "P1-3: 缺少 UPDATE SET deleted_at 软删除语句"
        )

    def test_relay_accounts_deleted_at_column(self):
        content = _read_file("database/relay_db.py")
        assert "deleted_at" in content, (
            "P1-3: relay_db.py 缺少 deleted_at 列"
        )

    def test_remove_account_uses_soft_delete(self):
        content = _read_file("database/relay_db.py")
        # remove_account 应使用 UPDATE 而非 DELETE FROM
        assert "SET deleted_at" in content, (
            "P1-3: relay_db.py 缺少软删除语句"
        )

    def test_tombstone_policy_doc_exists(self):
        content = _read_file("docs/tombstone-policy.md")
        assert "软删除" in content or "soft-delete" in content.lower(), (
            "P1-3: docs/tombstone-policy.md 缺少软删除策略说明"
        )


# ═══════════════════════════════════════════════════════════
#  P1-4: 生产强制加密
# ═══════════════════════════════════════════════════════════


class TestP1_4ProductionEncryption:
    """P1-4: ENVIRONMENT=production 时强制 BACKUP_ENCRYPTION_REQUIRED=True。"""

    def test_environment_field_exists(self):
        content = _read_file("config/settings.py")
        assert "ENVIRONMENT" in content, (
            "P1-4: settings.py 缺少 ENVIRONMENT 字段"
        )

    def test_backup_kek_file_field_exists(self):
        content = _read_file("config/settings.py")
        assert "BACKUP_KEK_FILE" in content, (
            "P1-4: settings.py 缺少 BACKUP_KEK_FILE 字段"
        )

    def test_production_enforces_encryption(self):
        content = _read_file("config/settings.py")
        assert "production" in content.lower(), (
            "P1-4: settings.py 缺少 production 环境检查"
        )
        assert "BACKUP_ENCRYPTION_REQUIRED" in content, (
            "P1-4: settings.py 缺少 BACKUP_ENCRYPTION_REQUIRED 强制逻辑"
        )

    def test_r38_p1_4_annotation_exists(self):
        content = _read_file("config/settings.py")
        assert "R38 P1-4" in content, (
            "P1-4: 缺少 R38 P1-4 中文注释标注"
        )


# ═══════════════════════════════════════════════════════════
#  P1-5: 备份 checksum/manifest 顺序修正
# ═══════════════════════════════════════════════════════════


class TestP1_5BackupManifestOrder:
    """P1-5: checksum 基于脱敏后的 plaintext,manifest 在脱敏后构建。"""

    def test_r38_metadata_key_exists(self):
        content = _read_file("services/db_backup.py")
        assert "_r38_p1_5_metadata" in content, (
            "P1-5: db_backup.py 缺少 _r38_p1_5_metadata 字段"
        )

    def test_manifest_built_after_redact(self):
        content = _read_file("services/db_backup.py")
        # 验证 _run_backup_loop 中 redact 在 manifest 构建之前
        # 注意: _build_bundle_manifest 有函数定义(在前)和调用(在 _run_backup_loop 中),
        # 需要找到 _run_backup_loop 内的调用位置,而非函数定义
        loop_start = content.find("async def _run_backup_loop")
        assert loop_start > 0, "P1-5: 缺少 _run_backup_loop 函数"
        loop_section = content[loop_start:]

        redact_pos = loop_section.find("_redact_secrets(data)")
        # 找调用(= 赋值),不是函数定义(def)
        manifest_pos = loop_section.find("manifest = _build_bundle_manifest(")
        assert redact_pos > 0 and manifest_pos > 0, (
            "P1-5: _run_backup_loop 中缺少 _redact_secrets 或 _build_bundle_manifest 调用"
        )
        assert manifest_pos > redact_pos, (
            "P1-5: manifest 构建应在脱敏之后(checksum 基于脱敏数据)"
        )

    def test_manifest_uploaded_separately(self):
        content = _read_file("services/db_backup.py")
        assert "manifest_{timestamp}" in content or "manifest_" in content, (
            "P1-5: manifest 应单独上传"
        )

    def test_backup_manifest_order_doc_exists(self):
        content = _read_file("docs/backup-manifest-order.md")
        assert "脱敏" in content, (
            "P1-5: docs/backup-manifest-order.md 缺少脱敏顺序说明"
        )
        assert "checksum" in content.lower(), (
            "P1-5: docs/backup-manifest-order.md 缺少 checksum 说明"
        )


# ═══════════════════════════════════════════════════════════
#  P1-6: CI workflow HEAD 文档
# ═══════════════════════════════════════════════════════════


class TestP1_6CiWorkflowHead:
    """P1-6: CI workflow 使用 HEAD commit SHA 签名,文档化 PAT 配置。"""

    def test_ci_uses_github_sha(self):
        content = _read_file(".github/workflows/ci.yml")
        assert "github.sha" in content, (
            "P1-6: ci.yml 缺少 github.sha 引用(HEAD commit)"
        )

    def test_ci_sign_artifacts_job_exists(self):
        content = _read_file(".github/workflows/ci.yml")
        assert "sign-artifacts" in content, (
            "P1-6: ci.yml 缺少 sign-artifacts job"
        )

    def test_github_pat_doc_exists(self):
        content = _read_file("docs/github-pat-instructions.md")
        assert "HEAD" in content or "github.sha" in content, (
            "P1-6: docs/github-pat-instructions.md 缺少 HEAD SHA 说明"
        )
        assert "PAT" in content or "Personal Access Token" in content, (
            "P1-6: docs/github-pat-instructions.md 缺少 PAT 配置说明"
        )


# ═══════════════════════════════════════════════════════════
#  P1-7: Docker healthcheck 角色专属
# ═══════════════════════════════════════════════════════════


class TestP1_7DockerHealthcheck:
    """P1-7: 各服务 healthcheck 不再全部相同,按角色定制。"""

    def test_healthchecks_are_role_specific(self):
        content = _read_file("docker-compose.yml")
        # 不应所有服务都用相同的 cache_store.db 检查
        # 应有角色专属检查(如 /proc/1/cmdline 或 HTTP 端点)
        assert "proc/1/cmdline" in content or "standalone" in content, (
            "P1-7: healthcheck 缺少角色专属检查(/proc/1/cmdline)"
        )

    def test_db_writer_healthcheck_checks_mtime(self):
        content = _read_file("docker-compose.yml")
        assert "getmtime" in content, (
            "P1-7: db_writer healthcheck 应检查 cache_store.db 修改时间(活跃写入)"
        )

    def test_db_backup_has_healthcheck(self):
        content = _read_file("docker-compose.yml")
        # db_backup 之前没有 healthcheck,现在应该有了
        db_backup_section = content[content.find("db_backup:"):]
        assert "healthcheck" in db_backup_section[:2000], (
            "P1-7: db_backup 缺少 healthcheck"
        )

    def test_r38_p1_7_annotation_exists(self):
        content = _read_file("docker-compose.yml")
        assert "R38 P1-7" in content, (
            "P1-7: 缺少 R38 P1-7 中文注释标注"
        )


# ═══════════════════════════════════════════════════════════
#  P1-8: Redis ACL Compose 实际启用
# ═══════════════════════════════════════════════════════════


class TestP1_8RedisAcl:
    """P1-8: Redis 服务挂载 ACL 文件 + --aclfile 命令参数。"""

    def test_acl_file_exists(self):
        content = _read_file("config/redis/users.acl")
        assert "user default off" in content, (
            "P1-8: users.acl 缺少 default 用户禁用"
        )
        assert "db_writer" in content, (
            "P1-8: users.acl 缺少 db_writer 用户"
        )

    def test_compose_mounts_acl_file(self):
        content = _read_file("docker-compose.yml")
        assert "users.acl" in content, (
            "P1-8: docker-compose.yml 未挂载 ACL 文件"
        )

    def test_compose_uses_aclfile_command(self):
        content = _read_file("docker-compose.yml")
        assert "--aclfile" in content, (
            "P1-8: docker-compose.yml redis command 缺少 --aclfile 参数"
        )

    def test_redis_acl_setup_doc_exists(self):
        content = _read_file("docs/redis-acl-setup.md")
        assert "ACL" in content, (
            "P1-8: docs/redis-acl-setup.md 缺少 ACL 说明"
        )

    def test_r38_p1_8_annotation_exists(self):
        content = _read_file("docker-compose.yml")
        assert "R38 P1-8" in content, (
            "P1-8: 缺少 R38 P1-8 中文注释标注"
        )


# ═══════════════════════════════════════════════════════════
#  P1-9: Prometheus exporter 加入 services.yaml
# ═══════════════════════════════════════════════════════════


class TestP1_9PrometheusExporter:
    """P1-9: prometheus_exporter 加入 services.yaml + docker-compose.yml。"""

    def test_services_yaml_has_prometheus_exporter(self):
        content = _read_file("config/services.yaml")
        assert "prometheus_exporter" in content, (
            "P1-9: services.yaml 缺少 prometheus_exporter 服务定义"
        )

    def test_compose_has_prometheus_exporter_service(self):
        content = _read_file("docker-compose.yml")
        assert "prometheus_exporter:" in content, (
            "P1-9: docker-compose.yml 缺少 prometheus_exporter 服务"
        )

    def test_compose_prometheus_exporter_has_healthcheck(self):
        content = _read_file("docker-compose.yml")
        prom_section = content[content.find("prometheus_exporter:"):]
        assert "healthcheck" in prom_section[:2000], (
            "P1-9: prometheus_exporter 缺少 healthcheck"
        )
        assert "9100" in prom_section[:2000], (
            "P1-9: prometheus_exporter 缺少端口 9100"
        )

    def test_ci_includes_prometheus_exporter(self):
        content = _read_file(".github/workflows/ci.yml")
        assert "prometheus_exporter" in content, (
            "P1-9: ci.yml 缺少 prometheus_exporter 服务"
        )

    def test_settings_has_prometheus_role(self):
        content = _read_file("config/settings.py")
        assert "prometheus_exporter" in content, (
            "P1-9: settings.py 缺少 prometheus_exporter 角色注册"
        )


# ═══════════════════════════════════════════════════════════
#  P2-1: run_all.py 标记开发专用
# ═══════════════════════════════════════════════════════════


class TestP2_1RunAllDevOnly:
    """P2-1: run_all.py 多进程模式仅用于开发,生产用 systemd + --standalone。"""

    def test_docstring_marks_dev_only(self):
        content = _read_file("run_all.py")
        assert "开发" in content or "development" in content.lower(), (
            "P2-1: run_all.py docstring 缺少开发专用标注"
        )

    def test_environment_production_check_exists(self):
        content = _read_file("run_all.py")
        assert "ENVIRONMENT" in content, (
            "P2-1: run_all.py 缺少 ENVIRONMENT 环境变量检查"
        )
        assert "production" in content.lower(), (
            "P2-1: run_all.py 缺少 production 环境检查"
        )

    def test_r38_p2_1_annotation_exists(self):
        content = _read_file("run_all.py")
        assert "R38 P2-1" in content, (
            "P2-1: 缺少 R38 P2-1 中文注释标注"
        )


# ═══════════════════════════════════════════════════════════
#  P2-2: 未知 SERVICE_ROLE fail-fast
# ═══════════════════════════════════════════════════════════


class TestP2_2UnknownRoleFailFast:
    """P2-2: 未知 SERVICE_ROLE 在生产环境 raise ValueError。"""

    def test_fail_fast_for_unknown_role(self):
        content = _read_file("config/settings.py")
        assert "ValueError" in content or "raise" in content, (
            "P2-2: settings.py 缺少未知 SERVICE_ROLE fail-fast 逻辑"
        )

    def test_r38_p2_2_annotation_exists(self):
        content = _read_file("config/settings.py")
        assert "R38 P2-2" in content, (
            "P2-2: 缺少 R38 P2-2 中文注释标注"
        )


# ═══════════════════════════════════════════════════════════
#  P2-3: Docker resources 实测文档
# ═══════════════════════════════════════════════════════════


class TestP2_3DockerResourcesDoc:
    """P2-3: Docker 资源限制实测文档。"""

    def test_docker_resources_doc_exists(self):
        content = _read_file("docs/docker-resources-test.md")
        assert "实测" in content, (
            "P2-3: docs/docker-resources-test.md 缺少实测说明"
        )
        assert "CPU" in content, (
            "P2-3: docs/docker-resources-test.md 缺少 CPU 实测数据"
        )
        assert "内存" in content or "memory" in content.lower(), (
            "P2-3: docs/docker-resources-test.md 缺少内存实测数据"
        )


# ═══════════════════════════════════════════════════════════
#  P2-4: 只读容器可写挂载校验
# ═══════════════════════════════════════════════════════════


class TestP2_4ReadonlyVolumes:
    """P2-4: 只读容器挂载必要的可写卷。"""

    def test_db_writer_has_logs_mount(self):
        content = _read_file("docker-compose.yml")
        # db_writer 应有 ./logs:/app/logs 挂载
        db_writer_section = content[content.find("db_writer:"):]
        db_writer_section = db_writer_section[:db_writer_section.find("crdb_sync:")]
        assert "./logs:/app/logs" in db_writer_section, (
            "P2-4: db_writer 缺少 ./logs:/app/logs 可写挂载"
        )

    def test_crdb_sync_has_logs_mount(self):
        content = _read_file("docker-compose.yml")
        # crdb_sync 应有 ./logs:/app/logs 挂载
        crdb_sync_section = content[content.find("crdb_sync:"):]
        crdb_sync_section = crdb_sync_section[:crdb_sync_section.find("up:")]
        assert "./logs:/app/logs" in crdb_sync_section, (
            "P2-4: crdb_sync 缺少 ./logs:/app/logs 可写挂载"
        )

    def test_readonly_volumes_doc_exists(self):
        content = _read_file("docs/readonly-volumes.md")
        assert "read_only" in content or "只读" in content, (
            "P2-4: docs/readonly-volumes.md 缺少只读容器说明"
        )
        assert "挂载" in content, (
            "P2-4: docs/readonly-volumes.md 缺少可写挂载说明"
        )

    def test_r38_p2_4_annotation_exists(self):
        content = _read_file("docker-compose.yml")
        assert "R38 P2-4" in content, (
            "P2-4: 缺少 R38 P2-4 中文注释标注"
        )


# ═══════════════════════════════════════════════════════════
#  P2-5: Delivery token effectively-once 文档
# ═══════════════════════════════════════════════════════════


class TestP2_5EffectivelyOnceSLO:
    """P2-5: delivery-idempotency.md 标注 effectively-once SLO。"""

    def test_slo_section_exists(self):
        content = _read_file("docs/delivery-idempotency.md")
        assert "SLO" in content, (
            "P2-5: delivery-idempotency.md 缺少 SLO 标注"
        )

    def test_effectively_once_annotation_exists(self):
        content = _read_file("docs/delivery-idempotency.md")
        assert "effectively-once" in content.lower() or "Effectively-Once" in content, (
            "P2-5: delivery-idempotency.md 缺少 effectively-once 语义标注"
        )

    def test_r38_p2_5_annotation_exists(self):
        content = _read_file("docs/delivery-idempotency.md")
        assert "R38 P2-5" in content, (
            "P2-5: 缺少 R38 P2-5 中文注释标注"
        )


# ═══════════════════════════════════════════════════════════
#  P2-6: release commit/tag/image 真实签名
# ═══════════════════════════════════════════════════════════


class TestP2_6RealSigningChecklist:
    """P2-6: SIGNING.md 新增真实签名执行清单。"""

    def test_real_signing_checklist_exists(self):
        content = _read_file("docs/SIGNING.md")
        assert "真实签名" in content, (
            "P2-6: SIGNING.md 缺少真实签名执行清单"
        )

    def test_commit_signing_section_exists(self):
        content = _read_file("docs/SIGNING.md")
        assert "Commit 签名" in content, (
            "P2-6: SIGNING.md 缺少 Commit 签名清单"
        )

    def test_tag_signing_section_exists(self):
        content = _read_file("docs/SIGNING.md")
        assert "Tag 签名" in content, (
            "P2-6: SIGNING.md 缺少 Tag 签名清单"
        )

    def test_image_signing_section_exists(self):
        content = _read_file("docs/SIGNING.md")
        assert "Image 签名" in content, (
            "P2-6: SIGNING.md 缺少 Image 签名清单"
        )

    def test_r38_p2_6_annotation_exists(self):
        content = _read_file("docs/SIGNING.md")
        assert "R38 P2-6" in content, (
            "P2-6: 缺少 R38 P2-6 中文注释标注"
        )


# ═══════════════════════════════════════════════════════════
#  P2-7: Prometheus label 禁止高基数
# ═══════════════════════════════════════════════════════════


class TestP2_7NoHighCardinalityLabels:
    """P2-7: prometheus_exporter.py 禁止高基数 label。"""

    def test_high_cardinality_blacklist_exists(self):
        content = _read_file("services/prometheus_exporter.py")
        assert "_HIGH_CARDINALITY_LABELS" in content, (
            "P2-7: prometheus_exporter.py 缺少高基数 label 黑名单"
        )

    def test_check_function_exists(self):
        content = _read_file("services/prometheus_exporter.py")
        assert "_check_no_high_cardinality_labels" in content, (
            "P2-7: prometheus_exporter.py 缺少高基数 label 检查函数"
        )

    def test_blacklist_includes_user_id(self):
        content = _read_file("services/prometheus_exporter.py")
        assert "user_id" in content, (
            "P2-7: 高基数 label 黑名单应包含 user_id"
        )

    def test_blacklist_includes_file_code(self):
        content = _read_file("services/prometheus_exporter.py")
        assert "file_code" in content, (
            "P2-7: 高基数 label 黑名单应包含 file_code"
        )

    def test_r38_p2_7_annotation_exists(self):
        content = _read_file("services/prometheus_exporter.py")
        assert "R38 P2-7" in content, (
            "P2-7: 缺少 R38 P2-7 中文注释标注"
        )


# ═══════════════════════════════════════════════════════════
#  汇总测试
# ═══════════════════════════════════════════════════════════


class TestR38AllDocsExist:
    """验证 R38 P1/P2 所有新增文档存在。"""

    @pytest.mark.parametrize("doc_path", [
        "docs/backup-manifest-order.md",       # P1-5
        "docs/github-pat-instructions.md",      # P1-6
        "docs/redis-acl-setup.md",              # P1-8
        "docs/docker-resources-test.md",        # P2-3
        "docs/readonly-volumes.md",             # P2-4
        "docs/tombstone-policy.md",             # P1-3
    ])
    def test_doc_exists(self, doc_path):
        fpath = _PROJECT_ROOT / doc_path
        assert fpath.exists(), f"文档不存在: {doc_path}"
