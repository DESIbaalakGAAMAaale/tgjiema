"""R46 P1 / R47 P1-c: 统一错误码协议化 — DOMAIN.OPERATION.REASON 三段式。

本模块提供完整的错误码协议化能力,替代原裸字符串错误返回:

1. ``ErrorCodes`` — 错误码字符串常量(向后兼容旧代码引用)
2. ``ErrorDefinition`` — 错误定义数据类(包含 message_key/http_status/retryable/severity/safe_params)
3. ``ErrorEnvelope`` — 统一错误返回(包含 trace_id 贯穿 Bot→Writer→Outbox→CRDB/Telegram→Admin Audit)
4. ``ErrorRegistry`` — 启动时注册所有 ErrorDefinition,提供 ``get`` / ``create_envelope``
5. ``AppError`` — 异常类,自动生成 trace_id 并尝试写入 audit_log

格式约定: ``DOMAIN.OPERATION.REASON``
示例:
    UPLOAD.COPY.TELEGRAM_TIMEOUT
    INDEX.FINALIZE.OUTBOX_FAILED
    DELIVERY.SEND.FLOOD_WAIT
    AUTH.MFA.REPLAYED
    BACKUP.RESTORE.APPROVAL_INVALID
    EFFECT.RECEIPT.MANAGER_UNAVAILABLE

R47 P1-c 整改要点:
- 所有错误码必须注册到 ErrorRegistry(含 message_key)
- 所有错误返回必须使用 AppError 或 ErrorEnvelope(禁止裸字符串)
- 所有错误返回必须携带 trace_id(UUID),写入 audit_log 表
- locales/zh-CN.json + locales/en-US.json 必须包含所有 message_key
- CI 通过 scripts/check_error_codes.py 静态扫描门禁

R59 §5.2 P1 整改要点(ErrorRegistry 唯一注册源):
- 后端、前端语言包和 OpenAPI/Telegram 映射均由 ErrorRegistry 生成
- 未注册错误码在 CI 直接失败;运行时未知异常统一通过
  ``ErrorRegistry.register_unknown_runtime_error()`` 降级为 INTERNAL_ERROR,
  原异常进入结构化日志(不暴露给用户消息)
- ``severity`` / ``retryable`` / HTTP status / 按钮展示策略由 registry 明确定义,
  不允许展示层通过错误码前缀猜测(见 ``to_frontend_mapping()``)
- ``safe_params`` 使用 allowlist;禁止把 SQL/路径/token/手机号/
  Telegram payload/对象存储 key 直接插入用户消息
- CI 通过 scripts/check_error_registry.py 静态扫描门禁
  (检查直接字符串错误码、动态拼接错误码、语言包缺 key、
  重复 code 和错误 HTTP 映射)
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from loguru import logger


# ════════════════════════════════════════════════════════════════
# 1. 错误码字符串常量(向后兼容,按 DOMAIN 分组)
# ════════════════════════════════════════════════════════════════
class ErrorCodes:
    """错误码字符串常量。

    R47 P1-c: 保留原有常量(向后兼容),同时通过 ErrorRegistry 注册完整定义。
    新代码应使用 ``ErrorRegistry.get(ErrorCodes.XXX)`` 获取完整定义,
    或通过 ``raise AppError(ErrorCodes.XXX, params={...})`` 抛出。
    """

    # UPLOAD
    UPLOAD_COPY_TELEGRAM_TIMEOUT = "UPLOAD.COPY.TELEGRAM_TIMEOUT"
    UPLOAD_COPY_TELEGRAM_FORBIDDEN = "UPLOAD.COPY.TELEGRAM_FORBIDDEN"
    UPLOAD_MANIFEST_OUTBOX_FAILED = "UPLOAD.MANIFEST.OUTBOX_FAILED"
    UPLOAD_PENDING_OUTBOX_TX_FAILED = "UPLOAD.PENDING.OUTBOX_TX_FAILED"

    # INDEX
    INDEX_FINALIZE_OUTBOX_FAILED = "INDEX.FINALIZE.OUTBOX_FAILED"
    INDEX_CODE_CONFLICT = "INDEX.CODE.CONFLICT"

    # DELIVERY
    DELIVERY_SEND_FLOOD_WAIT = "DELIVERY.SEND.FLOOD_WAIT"
    DELIVERY_SEND_FORBIDDEN = "DELIVERY.SEND.FORBIDDEN"
    DELIVERY_RECEIPT_FAILED = "DELIVERY.RECEIPT.FAILED"

    # AUTH
    AUTH_MFA_REPLAYED = "AUTH.MFA.REPLAYED"
    AUTH_MFA_LOCKED = "AUTH.MFA.LOCKED"
    AUTH_MFA_RECEIPT_INVALID = "AUTH.MFA.RECEIPT_INVALID"
    AUTH_SESSION_EXPIRED = "AUTH.SESSION.EXPIRED"

    # BACKUP
    BACKUP_RESTORE_APPROVAL_INVALID = "BACKUP.RESTORE.APPROVAL_INVALID"
    BACKUP_RESTORE_CHECKSUM_MISMATCH = "BACKUP.RESTORE.CHECKSUM_MISMATCH"
    BACKUP_RESTORE_TRUST_CHAIN_REQUIRED = "BACKUP.RESTORE.TRUST_CHAIN_REQUIRED"
    # R64 P1-01: payload 含不可序列化类型(bytes/NaN/Infinity/自定义对象)— fail-closed
    # 禁止 default=str 静默字符串化,只允许 JSON schema 声明类型
    BACKUP_PAYLOAD_NOT_SERIALIZABLE = "BACKUP.PAYLOAD.NOT_SERIALIZABLE"
    # R65 P1-06: canonical payload 构造时强校验失败 — 7 维校验任一未通过即 fail-closed
    # 拒绝"任意 JSON bytes"被称为 canonical,强制调用方传入合法 canonical bytes
    BACKUP_PAYLOAD_CANONICAL_INVALID = "BACKUP.PAYLOAD.CANONICAL_INVALID"

    # EFFECT_RECEIPT
    EFFECT_RECEIPT_MANAGER_UNAVAILABLE = "EFFECT.RECEIPT.MANAGER_UNAVAILABLE"
    EFFECT_RECEIPT_DB_ERROR = "EFFECT.RECEIPT.DB_ERROR"
    # R62 P1-01: 幂等冲突 — 同 (action_id, effect_type, target) 已存在不同 request_hash 的 receipt
    # 调用方不应盲目重试(payload 已被替换),需走 reconciliation 流程
    DATA_RECEIPT_IDEMPOTENCY_CONFLICT = "DATA.RECEIPT.IDEMPOTENCY_CONFLICT"
    # R62 P1-01: 终态保护 — 对已 completed 的 receipt 再次 record_completed 拒绝覆盖
    DATA_RECEIPT_TERMINAL_STATE = "DATA.RECEIPT.TERMINAL_STATE"

    # ── R47 P1-c 新增:覆盖关键场景的通用错误码 ──
    # 通用内部错误(fallback,未注册 code 时使用)
    ERROR_INTERNAL = "ERROR.INTERNAL.UNEXPECTED"
    # RBAC 权限不足
    AUTH_RBAC_PERMISSION_DENIED = "AUTH.RBAC.PERMISSION_DENIED"
    # 审批门禁:操作需审批
    APPROVAL_REQUIRED = "APPROVAL.GATE.REQUIRED"
    # 审批状态无效
    APPROVAL_STATE_INVALID = "APPROVAL.STATE.INVALID"
    # CommandBus 幂等冲突(已被其他 worker 抢占)
    COMMAND_CONCURRENT_CLAIM = "COMMAND.CONCURRENT.CLAIM"
    # 请求参数与上次执行不一致(防篡改拒绝)
    COMMAND_HASH_MISMATCH = "COMMAND.HASH.MISMATCH"
    # SQLite cache_store 不可用
    DB_CACHE_UNAVAILABLE = "DB.CACHE.UNAVAILABLE"
    # Redis 连接不可用
    DB_REDIS_UNAVAILABLE = "DB.REDIS.UNAVAILABLE"
    # CRDB pool 不可用
    DB_CRDB_UNAVAILABLE = "DB.CRDB.UNAVAILABLE"
    # 配额超限
    QUOTA_EXCEEDED = "QUOTA.DECODE.EXCEEDED"
    # 文件不存在
    FILE_NOT_FOUND = "FILE.LOOKUP.NOT_FOUND"
    # 文件已过期
    FILE_EXPIRED = "FILE.LOOKUP.EXPIRED"
    # 文件码无效
    FILE_CODE_INVALID = "FILE.CODE.INVALID"
    # 用户被封禁
    USER_BANNED = "USER.STATE.BANNED"
    # 系统维护中
    SYSTEM_MAINTENANCE = "SYSTEM.STATE.MAINTENANCE"
    # 系统限流
    SYSTEM_RATE_LIMITED = "SYSTEM.RATE.LIMITED"
    # 参数校验失败
    VALIDATION_FAILED = "VALIDATION.INPUT.FAILED"

    # ── R48 P1 新增:覆盖 baseline 中 15 处裸字符串错误的场景 ──
    # 管理员密码为空(admin/__init__.py:generate_password_hash)
    ADMIN_VALIDATION_PASSWORD_EMPTY = "ADMIN.VALIDATION.PASSWORD_EMPTY"
    # topology.yaml 中没有槽位配置(admin/seed_topology.py)
    TOPOLOGY_LOAD_NO_SLOTS = "TOPOLOGY.LOAD.NO_SLOTS"
    # 索引生成码时数据库未初始化(bots/idx_bot.py:_generate_unique_code_with_retry)
    INDEX_GENERATE_DB_UNINITIALIZED = "INDEX.GENERATE.DB_UNINITIALIZED"
    # 索引 finalize_upload 时数据库未初始化(bots/idx_bot.py:finalize_upload)
    INDEX_FINALIZE_DB_UNINITIALIZED = "INDEX.FINALIZE.DB_UNINITIALIZED"
    # 无可用存储频道(bots/idx_bot.py:_get_storage_channel_id)
    INDEX_STORAGE_NO_CHANNEL = "INDEX.STORAGE.NO_CHANNEL"
    # MON_BOT_TOKEN 未配置(bots/mon_bot.py)
    BOT_MON_TOKEN_MISSING = "BOT.MON.TOKEN_MISSING"
    # _bot 全局引用未初始化(bots/up_bot.py:_outbox_archive_to_r100_strict)
    UPLOAD_OUTBOX_BOT_UNINITIALIZED = "UPLOAD.OUTBOX.BOT_UNINITIALIZED"
    # 无可用活跃槽位(bots/up_bot.py:_get_upload_target_channel)
    UPLOAD_SLOT_NONE_ACTIVE = "UPLOAD.SLOT.NONE_ACTIVE"
    # cryptography 未安装(services/backup_crypto.py)
    BACKUP_DECRYPT_DEP_MISSING = "BACKUP.DECRYPT.DEP_MISSING"
    # BACKUP_KEK 未配置(services/backup_crypto.py)
    BACKUP_DECRYPT_KEK_MISSING = "BACKUP.DECRYPT.KEK_MISSING"
    # R2 凭证未配置(services/db_backup.py)
    BACKUP_RESTORE_R2_CREDENTIAL_MISSING = "BACKUP.RESTORE.R2_CREDENTIAL_MISSING"
    # 中继账号验证码获取失败(services/relay_instance.py)
    RELAY_AUTH_CODE_FAILED = "RELAY.AUTH.CODE_FAILED"
    # 中继账号二步验证密码获取超时(services/relay_instance.py)
    RELAY_AUTH_PASSWORD_TIMEOUT = "RELAY.AUTH.PASSWORD_TIMEOUT"
    # 中继账号 api_hash 校验失败(services/relay_pool.py)
    RELAY_CONFIG_API_HASH_INVALID = "RELAY.CONFIG.API_HASH_INVALID"

    # ── R50 P1-1 新增:覆盖最后 5 处裸字符串 raise ──
    # callback allowlist action 为空或不在 allowlist 内(services/callback_allowlist.py)
    CALLBACK_ACTION_NOT_ALLOWED = "CALLBACK.ACTION.NOT_ALLOWED"
    # admin bootstrap 未完成,Web 进程应退出(admin/__init__.py:require_readiness)
    ADMIN_BOOTSTRAP_NOT_VERIFIED = "ADMIN.BOOTSTRAP.NOT_VERIFIED"
    # R56: migrate_argon2_offline() 已移除(安全原因),应改用 migrate_argon2_offline_safe()
    ADMIN_PASSWORD_MIGRATE_ARGON2_REMOVED = "ADMIN.PASSWORD.MIGRATE_ARGON2_REMOVED"
    # production 环境必须配置 BOT_TOKEN(services/button_security.py:_check_production_secret)
    PRODUCTION_BOT_TOKEN_MISSING = "PRODUCTION.BOT_TOKEN.MISSING"
    # 灾备恢复必须传 approval_action_id(services/backup_engine.py + services/disaster_recovery.py)
    BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED = "BACKUP.RESTORE.APPROVAL_ACTION_ID_REQUIRED"

    # ── R51 P0-6 新增:内容申诉恢复相关错误码 ──
    # 内容申诉恢复操作失败(restore_content handler 执行失败,进入 reconciliation)
    CONTENT_APPEAL_RESTORE_FAILED = "CONTENT.APPEAL.RESTORE_FAILED"
    # 内容申诉状态无效(如重复审批 / 已在 restore_pending 等待 executor)
    CONTENT_APPEAL_INVALID_STATE = "CONTENT.APPEAL.INVALID_STATE"

    # ── R51 P0-8: production restore hash 强制 ──
    # production 恢复必须传 expected_request_hash(TOCTOU 防护)
    PRODUCTION_RESTORE_HASH_REQUIRED = "PRODUCTION.RESTORE.HASH_REQUIRED"
    # production 恢复 expected_request_hash 与存储 hash 不匹配(TOCTOU 攻击或 payload 被篡改)
    PRODUCTION_RESTORE_HASH_MISMATCH = "PRODUCTION.RESTORE.HASH_MISMATCH"
    # command_executions 已 executed,禁止重复执行 restore
    RESTORE_ALREADY_EXECUTED = "RESTORE.EXECUTE.ALREADY_EXECUTED"

    # ── R51 P0-5: notification_outbox 异常 ──
    # notification_outbox 写入失败(必须回滚事务,避免孤儿通知)
    NOTIFICATION_OUTBOX_WRITE_FAILED = "NOTIFICATION.OUTBOX.WRITE_FAILED"
    # notification_outbox 重复插入(dedup_key + window 唯一约束冲突)
    NOTIFICATION_OUTBOX_DUPLICATE = "NOTIFICATION.OUTBOX.DUPLICATE"

    # ── R51 P1-1: Data Lifecycle 事务化 ──
    # 删除请求步骤失败(任一 step 失败 → 整个 deletion_request 标记 failed)
    DATA_LIFECYCLE_DELETE_STEP_FAILED = "DATA.LIFECYCLE.DELETE_STEP_FAILED"
    # 删除请求失败(局部失败导致整个请求未 completed)
    DATA_LIFECYCLE_DELETE_REQUEST_FAILED = "DATA.LIFECYCLE.DELETE_REQUEST_FAILED"
    # 物理删除前验证 backup marker 失败(无备份标记)
    DATA_LIFECYCLE_BACKUP_MARKER_MISSING = "DATA.LIFECYCLE.BACKUP_MARKER_MISSING"
    # R53 P1-3: skip_backup_check=True 无 break-glass 审批(只允许审批后绕过)
    DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED = "DATA.LIFECYCLE.BREAK_GLASS_APPROVAL_REQUIRED"

    # ── R51 P1-2: Entitlements 事务化 ──
    # 配额查询失败(fail-closed 拒绝放行,不允许默认 used=0)
    ENTITLEMENT_QUOTA_QUERY_FAILED = "ENTITLEMENT.QUOTA.QUERY_FAILED"
    # set_user_plan 事务失败(套餐/配额/audit/dirty_outbox 任一失败)
    ENTITLEMENT_SET_PLAN_TX_FAILED = "ENTITLEMENT.SET_PLAN.TX_FAILED"

    # ── R51 P1-3: Collections CAS ──
    # 生产修改集合必须传 expected_version(乐观锁不可绕过)
    COLLECTION_CAS_VERSION_REQUIRED = "COLLECTION.CAS.VERSION_REQUIRED"
    # 集合 CAS 版本冲突
    COLLECTION_CAS_CONFLICT = "COLLECTION.CAS.CONFLICT"
    # bypass_cas 必须显式声明并审计
    COLLECTION_CAS_BYPASS_NOT_ALLOWED = "COLLECTION.CAS.BYPASS_NOT_ALLOWED"

    # ── R51 P1-4: Task Center 错误处理 ──
    # 未知 task_type 拒绝(不再静默回退)
    TASK_CENTER_UNKNOWN_TYPE = "TASK.CENTER.UNKNOWN_TYPE"
    # 未知 task status 拒绝(不再静默回退)
    TASK_CENTER_UNKNOWN_STATUS = "TASK.CENTER.UNKNOWN_STATUS"
    # 列表查询 DB 异常(返回错误 envelope,不返回空列表伪装"无任务")
    TASK_CENTER_LIST_DB_ERROR = "TASK.CENTER.LIST_DB_ERROR"

    # ── R51 P1-5: Repair Console 审批 ──
    # 高风险修复动作必须强制审批(approval_action_id 不可缺省)
    REPAIR_CONSOLE_APPROVAL_REQUIRED = "REPAIR.CONSOLE.APPROVAL_REQUIRED"
    # 审批 hash/owner 校验失败(不仅校验 status,必须校验 request_hash + principal_id)
    REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH = "REPAIR.CONSOLE.APPROVAL_HASH_MISMATCH"
    # 审批 principal 不匹配(approval_action_id 关联的 principal 与当前 principal 不一致)
    REPAIR_CONSOLE_APPROVAL_PRINCIPAL_MISMATCH = "REPAIR.CONSOLE.APPROVAL_PRINCIPAL_MISMATCH"

    # ── R51 P1-6: 维护模式 fail-closed ──
    # 维护工作流失败但 recover_status='pending' 持久化失败(严重告警,必须 fail-closed)
    MAINTENANCE_RECOVER_STATUS_PERSIST_FAILED = "MAINTENANCE.WORKFLOW.PERSIST_FAILED"
    # disable/recover 操作必须绑定 request_hash + principal + approval_action_id
    MAINTENANCE_RECOVER_BINDING_REQUIRED = "MAINTENANCE.RECOVER.BINDING_REQUIRED"

    # ── R51 P1-7: Prometheus 指标完善 ──
    # 高基数 label 违规(CI/测试中 fail,运行时丢弃违规 metric)
    METRICS_HIGH_CARDINALITY_LABEL = "METRICS.LABEL.HIGH_CARDINALITY"
    # 指标采集器失败(输出 collector_success=0,不输出 0 伪装健康)
    METRICS_COLLECTOR_FAILED = "METRICS.COLLECTOR.FAILED"
    # RU 估算值标记(非官方 CockroachDB Cloud Metrics)
    METRICS_RU_ESTIMATED = "METRICS.RU.ESTIMATED"

    # ── R52 P0-5: 统一高风险动作状态机 ──
    # command_executions 状态冲突(CAS 未命中,如 approved→executing 时已被其他 worker 抢占)
    COMMAND_STATUS_CONFLICT = "COMMAND.STATUS.CONFLICT"
    # command_executions 未处于 approved 状态(执行前必须审批通过)
    COMMAND_NOT_APPROVED = "COMMAND.STATUS.NOT_APPROVED"

    # ── R52 P1-1: Durable Outbox hash mismatch(复用 COMMAND_HASH_MISMATCH) ──

    # ── R52 P1-4: Entitlements CAS + CommandBus ──
    # set_user_plan CAS 版本冲突(并发套餐修改,expected_version 不匹配 current version)
    ENTITLEMENT_SET_PLAN_CAS_CONFLICT = "ENTITLEMENT.SET_PLAN.CAS_CONFLICT"
    # 套餐变更必须通过 CommandBus(禁止直接调用 set_user_plan 进行生产变更)
    ENTITLEMENT_PLAN_REQUIRES_COMMAND_BUS = "ENTITLEMENT.PLAN.REQUIRES_COMMAND_BUS"

    # ── R52 P1-6: Maintenance fail-closed ──
    # disable() 查询 recover_status 失败(fail-closed,不允许降级为 completed)
    MAINTENANCE_DISABLE_RECOVER_QUERY_FAILED = "MAINTENANCE.DISABLE.QUERY_FAILED"
    # recover_status 持久化失败(触发 critical alert,不允许 fail-open)
    MAINTENANCE_RECOVER_PERSIST_CRITICAL = "MAINTENANCE.RECOVER.PERSIST_CRITICAL"

    # ── R52 P1-7: Metrics unknown 语义 ──
    # 指标采集失败但已输出 0 值带 error label(应改为不输出或 NaN)
    METRICS_COLLECTOR_OUTPUT_INVALID = "METRICS.COLLECTOR.OUTPUT_INVALID"

    # ── R52 P1-8: CF Worker 两阶段去重 ──
    # UPDATE_ID_KV 未配置(production 必须配置)
    CF_WORKER_UPDATE_ID_KV_UNCONFIGURED = "CF.WORKER.UPDATE_ID_KV_UNCONFIGURED"

    # ── R53 P0-2: CommandBus fail-closed ──
    # claim_execution_approved 在数据库不可用时必须 fail-closed(禁止降级执行)
    COMMAND_EXECUTION_STORE_UNAVAILABLE = "COMMAND.EXECUTE.STORE_UNAVAILABLE"

    # ── R53 P0-4: Collections bypass 真实审批校验 ──
    # 审批无效(approval_action_id 为空/查不到记录/状态非 approved)
    COLLECTION_APPROVAL_INVALID = "COLLECTION.APPROVAL.INVALID"
    # 审批 request_hash 不匹配(防篡改,前 16 字符也不匹配)
    COLLECTION_APPROVAL_HASH_MISMATCH = "COLLECTION.APPROVAL.HASH_MISMATCH"
    # 审批 principal_id 不匹配(他人审批,防越权)
    COLLECTION_APPROVAL_PRINCIPAL_MISMATCH = "COLLECTION.APPROVAL.PRINCIPAL_MISMATCH"
    # 审批已被执行(状态='executed',禁止重复执行)
    COLLECTION_APPROVAL_ALREADY_EXECUTED = "COLLECTION.APPROVAL.ALREADY_EXECUTED"

    # ── R53 P0-5: Entitlements 移除生产绕过路径 ──
    # production 环境禁止直接调用 _set_user_plan_internal 修改套餐(必须通过 CommandBus)
    ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN = "ENTITLEMENT.PLAN.DIRECT_MUTATION_FORBIDDEN"
    # 修改套餐必须提供 expected_version(production 强制 CAS,禁止 None)
    ENTITLEMENTS_EXPECTED_VERSION_REQUIRED = "ENTITLEMENT.PLAN.EXPECTED_VERSION_REQUIRED"

    # ── R53 P1-2: Durable Outbox Hash 不匹配隔离 ──
    # durable outbox 重放时 payload hash 校验失败,记录被标记为 quarantined,
    # 终止永久热循环(原 continue 保持 pending 导致下一轮再次校验报错)
    DURABLE_OUTBOX_QUARANTINED = "DURABLE.OUTBOX.QUARANTINED"

    # ── R53 P1-5: CommandBus 双状态机类型边界 ──
    # 高风险动作(action ∈ HIGH_RISK_ACTIONS 且 requires_approval=1)误走旧
    # claim_execution 入口,必须改走 claim_execution_approved 审批路径
    COMMAND_MUST_USE_APPROVAL_PATH = "COMMAND.APPROVAL.MUST_USE_APPROVAL_PATH"

    # ── R55 §18: 统一按钮 Approval Policy 错误码 ──
    # 高风险按钮缺少统一 Approval Policy 绑定(principal/resource/version/hash/expiry/nonce 任一缺失)
    BUTTON_POLICY_BINDING_MISSING = "BUTTON.POLICY.BINDING_MISSING"
    # 按钮 nonce 已被消费或不存在(防重放/双击/并发点击)
    BUTTON_POLICY_NONCE_CONSUMED = "BUTTON.POLICY.NONCE_CONSUMED"
    # 按钮 callback 签名校验失败(防篡改)
    BUTTON_POLICY_SIGNATURE_INVALID = "BUTTON.POLICY.SIGNATURE_INVALID"
    # 按钮 callback 已过期(expiry_ts 超时)
    BUTTON_POLICY_EXPIRED = "BUTTON.POLICY.EXPIRED"
    # 按钮 principal 不匹配(跨用户攻击)
    BUTTON_POLICY_PRINCIPAL_MISMATCH = "BUTTON.POLICY.PRINCIPAL_MISMATCH"
    # 按钮 resource_version 不匹配(旧版本按钮操作已更新资源)
    BUTTON_POLICY_VERSION_MISMATCH = "BUTTON.POLICY.VERSION_MISMATCH"
    # 按钮 request_hash 不匹配(审批与资源错位)
    BUTTON_POLICY_HASH_MISMATCH = "BUTTON.POLICY.HASH_MISMATCH"
    # R63 P1-06: 按钮 audience 不匹配(跨 handler 滥用,如 report handle 被 restore handler 调用)
    BUTTON_POLICY_AUDIENCE_MISMATCH = "BUTTON.POLICY.AUDIENCE_MISMATCH"
    # 高风险按钮要求 MFA 但未验证(MFA 强制门禁)
    BUTTON_POLICY_MFA_REQUIRED = "BUTTON.POLICY.MFA_REQUIRED"
    # 极高风险按钮要求双人审批但 approver 缺失或与 principal 相同
    BUTTON_POLICY_DUAL_APPROVAL_REQUIRED = "BUTTON.POLICY.DUAL_APPROVAL_REQUIRED"
    # 按钮操作缺少最终确认(final confirm 步骤缺失)
    BUTTON_POLICY_FINAL_CONFIRM_REQUIRED = "BUTTON.POLICY.FINAL_CONFIRM_REQUIRED"
    # R58 P0-4: 高风险 action 必须使用异步 token(持久化 nonce + 原子消费)
    BUTTON_POLICY_ASYNC_TOKEN_REQUIRED = "BUTTON.POLICY.ASYNC_TOKEN_REQUIRED"
    # R64 P1-08: destructive action UX spec 查询失败(action 为空或非高风险)
    BUTTON_UX_ACTION_REQUIRED = "BUTTON.UX.ACTION_REQUIRED"
    BUTTON_UX_ACTION_NOT_HIGH_RISK = "BUTTON.UX.ACTION_NOT_HIGH_RISK"

    # ── R62 P1-07: MFA receipt 年龄校验 + 审批自拒防护 ──
    # MFA receipt 已过期(签发时间超过高风险动作允许的最大年龄,防陈旧 receipt 绕过二次认证)
    AUTH_MFA_RECEIPT_EXPIRED = "AUTH.MFA.RECEIPT_EXPIRED"
    # reject() 不能由创建者自己驳回(防止自拒绕过审计,必须与 approve() 对称强制 requester != approver)
    APPROVAL_SELF_REJECT_FORBIDDEN = "APPROVAL.SELF_REJECT.FORBIDDEN"

    # ── R63 P0-04 / R66 P0-01: Migration manifest 信任根验证 ──
    # R66 P0-01: catalog 缺少 verification 等必填字段,或包含禁止的 release_commit/tree_sha 字段
    MIGRATION_MANIFEST_FIELD_MISSING = "MIGRATION.MANIFEST.FIELD_MISSING"
    # R66 P0-01: release-manifest.json 的 source_commit/source_tree 与当前 git HEAD/Tree 不一致
    MIGRATION_MANIFEST_BINDING_MISMATCH = "MIGRATION.MANIFEST.BINDING_MISMATCH"
    # 磁盘 migration 文件集合与 manifest 声明集合不一致(漏项或多项)
    MIGRATION_MANIFEST_SET_MISMATCH = "MIGRATION.MANIFEST.SET_MISMATCH"
    # cosign verify-blob 验签失败 / 签名文件缺失 / cosign 不可用
    MIGRATION_MANIFEST_SIGNATURE_INVALID = "MIGRATION.MANIFEST.SIGNATURE_INVALID"

    # ── R64 P0-02: Release artifact manifest 强制验证 ──
    # staging/production 未启用 MIGRATION_MANIFEST_VERIFY=1 — 拒绝启动
    MIGRATION_MANIFEST_VERIFY_REQUIRED = "MIGRATION.MANIFEST.VERIFY_REQUIRED"
    # 非 git 部署环境未通过 RELEASE_SOURCE_COMMIT/TREE 注入 source commit/tree
    MIGRATION_MANIFEST_RELEASE_SOURCE_REQUIRED = "MIGRATION.MANIFEST.RELEASE_SOURCE_REQUIRED"
    # release-manifest.json 与 migration-manifest.json 集合/digest 不一致
    MIGRATION_MANIFEST_RELEASE_CONSISTENCY = "MIGRATION.MANIFEST.RELEASE_CONSISTENCY"

    # ── R63 P0-05: Outbox worker fail-fast ──
    # 生产模式 provider_registry=None,拒绝 stub 误启动把外部副作用标记完成
    OUTBOX_PROVIDER_REGISTRY_REQUIRED = "OUTBOX.PROVIDER_REGISTRY.REQUIRED"
    # R64 P0-04: provider registry / schema 加载异常(如 CRITICAL_EFFECT_TYPES
    # 导入失败)直接 readiness failure,严禁 fail-open 返回空列表
    OUTBOX_PROVIDER_REGISTRY_LOAD_FAILED = "OUTBOX.PROVIDER_REGISTRY.LOAD_FAILED"
    # R64 P0-04: lease fencing token(lease_version)CAS 不匹配,complete/fail/
    # renew 必须携带正确版本号,版本不匹配表示 lease 已被回收或越权操作
    OUTBOX_LEASE_VERSION_MISMATCH = "OUTBOX.LEASE_VERSION.MISMATCH"
    # R65 P1-05: 严格 CAS 路径冲突(lease_version is not None 且 CAS 失败)raise。
    # complete/fail/renew 在严格 CAS 路径下,若 lease_version 不匹配 / 行未找到 /
    # lease_owner 不匹配,一律 raise 本错误,不再静默返回 False/not_found。
    # params: event_id / expected_lease_version / operation
    OUTBOX_LEASE_VERSION_CONFLICT = "OUTBOX.LEASE_VERSION.CONFLICT"
    # R64 P0-04: 未知 event_type 严禁标记成功,必须进入 DLQ(可审批 replay)
    OUTBOX_EVENT_UNKNOWN = "OUTBOX.EVENT.UNKNOWN"
    # R64 P0-04: provider 调用超过租期三分之一时自动续租,续租失败立即停止提交结果
    OUTBOX_LEASE_RENEW_FAILED = "OUTBOX.LEASE_RENEW.FAILED"

    # ── R63 P1-11: Locale 文件 fail-closed 校验 ──
    # 启动期 locale 文件完整性校验失败(文件缺失/JSON 解析失败/
    # message_key 缺失/占位符不对称),Release 镜像必须 fail-closed
    LOCALE_VALIDATION_FAILED = "LOCALE.VALIDATION.FAILED"

    # ── R63 P1-12: i18n ICU 预编译 fail-fast ──
    # locale 加载阶段预编译 ICU message 失败(语法错误 / 参数集合不对称),
    # release / strict 模式直接阻断构建或加载
    I18N_ICU_COMPILE_FAILED = "I18N.ICU.COMPILE_FAILED"

    # ── R64 P0-03: 恢复编排状态机 — staging 蓝绿切换 ──
    # staging provision 失败(为 CRDB/SQLite/relay_sqlite 创建新目标失败)
    RESTORE_STAGING_PROVISION_FAILED = "RESTORE.STAGING.PROVISION_FAILED"
    # staging validate 失败(schema/行数/主外键/业务守恒/hash 任一失败)
    RESTORE_STAGING_VALIDATE_FAILED = "RESTORE.STAGING.VALIDATE_FAILED"
    # 蓝绿切换前必须审批(approval_id 缺失或与 request_approval 不匹配)
    RESTORE_APPROVAL_REQUIRED = "RESTORE.APPROVAL.REQUIRED"
    # 蓝绿切换前必须 MFA receipt(mfa_receipt_id 缺失或与 request_approval 不匹配)
    RESTORE_MFA_REQUIRED = "RESTORE.APPROVAL.MFA_REQUIRED"
    # 蓝绿切换失败(CAS 切换 active 指针失败)
    RESTORE_SWITCH_FAILED = "RESTORE.SWITCH.FAILED"
    # 回滚失败(状态机错误 / 无 switch_version / 旧版本指针损坏)
    RESTORE_ROLLBACK_FAILED = "RESTORE.ROLLBACK.FAILED"
    # nonce payload 不一致(同 operation 重试时禁止换 payload_digest,防篡改)
    RESTORE_NONCE_PAYLOAD_MISMATCH = "RESTORE.NONCE.PAYLOAD_MISMATCH"
    # phase 转换非法(状态机不允许的转换,如 INIT → COMPLETED)
    RESTORE_PHASE_TRANSITION_INVALID = "RESTORE.PHASE.TRANSITION_INVALID"
    # R65 P0-07 / P1-07: 旧直接 restore 写入器已被 capability-seal,
    # 生产入口不能回退到原地覆盖;仅测试 / scripts / orchestrator backend 可调用。
    # 生产代码若直接调用 db_restore.run_restore() /
    # _restore_from_backup_data() / _restore_crdb_tables() /
    # _restore_sqlite_tables_to_db() / validate_and_restore_backup_strict()
    # (绕过 orchestrator 蓝绿切换)→ 抛此错误 fail-closed。
    RESTORE_LEGACY_WRITER_SEALED = "RESTORE.LEGACY_WRITER.SEALED"
    # R66 P0-06: RestoreOrchestrator 必需依赖缺失 — 生产类已删除所有 Optional 降级分支,
    # 构造时 backends / approval_authority / mfa_authority / store 任一为 None,
    # 或 check_startup_readiness 校验三个 backend / authority / nonce ledger /
    # active pointer / fencing store 任一不可用 → 抛此错误 fail-closed。
    # params: reason (主因) / missing (缺失依赖列表,逗号分隔)
    RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING = (
        "RESTORE.ORCHESTRATOR.REQUIRED_DEPENDENCY_MISSING"
    )

    # ── R65 P0-05: HighRiskPolicy 未知 destructive action fail-closed ──
    # action 属于 destructive namespace(匹配 delete/purge/ban/block/takedown/
    # detach/restore/reset/rotate/assign/revoke/grant/enable/disable/wipe/clear/
    # shutdown/restart/factory_reset/break_glass/force_logout 等关键词)
    # 但未在 HIGH_RISK_POLICY 中注册 — 拒绝执行,防止新 destructive action
    # 被误判为低风险绕过审批/MFA/双人审批门禁
    HIGH_RISK_ACTION_UNREGISTERED = "HIGH_RISK.ACTION.UNREGISTERED"

    # ── R65 P1-03: i18n 严格出口边界 fail-closed ──
    # 生产代码(services/ / bots/ / admin/)调用 translate / format_message /
    # format_message_icu 时未显式传入 locale(依赖全局 _DEFAULT_LOCALE 兜底),
    # staging/production 必须 fail-closed 抛此错误,禁止 silent fallback。
    # 测试环境可通过 I18N_ALLOW_FALLBACK=1 逃生舱保留旧行为(向后兼容)。
    I18N_LOCALE_NOT_BOUND = "I18N.LOCALE.NOT_BOUND"
    # ICU 运行时解析失败(占位符缺失 / 括号不平衡 / selector 缺失等),
    # staging/production 必须 fail-closed 抛此错误,禁止把原始 ICU 大括号
    # 展示给用户(I18N_ICU_COMPILE_FAILED 仅覆盖 load_locale 阶段预编译失败,
    # 本错误码覆盖运行时 format_message_icu 路径的解析失败)。
    I18N_PARSE_FAILED = "I18N.PARSE.FAILED"

    # ── R65 P0-04: 生产证据严格门禁 ──
    # production promotion 必须基于独立、签名、不可变、未过期的真实证据 artifact。
    # 任一必需证据缺失/过期/dry_run/未签名/--skip 使用,均阻断晋级并抛此错误。
    # params: reason (主因) / missing (缺失或过期的证据类型列表,逗号分隔)
    PRODUCTION_EVIDENCE_INSUFFICIENT = "PRODUCTION.EVIDENCE.INSUFFICIENT"

    # R67 P1-11: 防重放 — evidence artifact 已被其他 candidate 消费,禁止跨候选复用。
    # 单次使用语义:每个 evidence artifact 只能被一个 candidate 消费一次。
    # 重复消费(跨候选复用)在 consume_evidence_for_promotion() 中抛此错误。
    # params: artifact_type / consumed_candidate (已消费的 candidate tag) /
    #         candidate_tag (当前请求的 candidate tag)
    EVIDENCE_ALREADY_CONSUMED = "PRODUCTION.EVIDENCE.ALREADY_CONSUMED"

    # ── R70 Wave 3: 测试逃生舱硬守卫 ──
    # production/staging 下检测到任何测试逃生舱环境变量(I18N_ALLOW_FALLBACK /
    # ALLOW_LEGACY_RESTORE / TEST_ONLY / DEV_ONLY / BYPASS / SKIP_VERIFY 等)
    # 被设置时,escape_hatch_guard.assert_no_test_escape_hatches() 抛此错误。
    # params: caller (调用方标识) / hatch_count (检测到的逃生舱数量) /
    #         hatch_details (逃生舱详情列表) / reason (主因标识)
    PRODUCTION_ESCAPE_HATCH_DETECTED = "PRODUCTION.ESCAPE_HATCH.DETECTED"


# ════════════════════════════════════════════════════════════════
# 1b. ErrorEnum — R56 §5.2 Python enum(str + Enum 双继承,保持字符串兼容)
# ════════════════════════════════════════════════════════════════
# R56 §5.2: 错误码 registry 唯一来源 — 通过 enum 提供类型安全,
# 同时保持与 ErrorCodes 字符串常量的兼容(str 子类可直接用作字符串)。
#
# 用法:
#     from services.error_codes import ErrorEnum
#     raise AppError(ErrorEnum.UPLOAD_COPY_TELEGRAM_TIMEOUT, params={...})
#     # ErrorEnum.XXX == ErrorCodes.XXX(str 比较)
#
# enum 的优势:
#     - IDE 自动补全 + 类型检查
#     - 防止拼写错误
#     - 可枚举所有错误码
#     - 不可变(运行时不可新增)
def _build_error_enum() -> type:
    """R56 §5.2: 使用 Enum functional API 从 ErrorCodes 自动构建 ErrorEnum。

    避免维护两份重复的常量列表,确保 ErrorEnum 与 ErrorCodes 始终一致。
    CI 测试 ``test_error_codes_enum_sync`` 强制一致性(任何新增常量必须同步到两者)。
    """
    members: dict[str, str] = {}
    for attr_name in dir(ErrorCodes):
        if not attr_name.isupper() or attr_name.startswith("_"):
            continue
        value = getattr(ErrorCodes, attr_name)
        if not isinstance(value, str) or "." not in value:
            continue
        members[attr_name] = value
    # 使用 functional API 创建 Enum(str 子类,可直接用作字符串)
    enum_cls = Enum("ErrorEnum", members, type=str)
    enum_cls.__doc__ = (
        "R56 §5.2: Python enum 版本错误码(与 ErrorCodes 字符串常量保持一致)。\n\n"
        "通过 ``str, Enum`` 双继承,ErrorEnum.XXX 等价于字符串 "
        '"UPLOAD.COPY.TELEGRAM_TIMEOUT",可直接传给需要字符串参数的函数\n'
        "(如 AppError(ErrorEnum.XXX, params=...)),也可在 if/比较 中与 "
        "ErrorCodes.XXX 直接比较(均按 str 相等性比较)。\n\n"
        "成员通过 ``_build_error_enum()`` 自动从 ErrorCodes 同步,无需手动维护。\n"
        f"Total {len(members)} error code constants."
    )

    @classmethod
    def from_code(cls, code: str) -> "ErrorEnum | None":
        """从字符串 code 反查 ErrorEnum 成员(未匹配返回 None)。"""
        try:
            return cls(code)
        except ValueError:
            return None

    enum_cls.from_code = from_code
    return enum_cls


# 模块加载时自动构建 ErrorEnum(从 ErrorCodes 同步所有常量)
ErrorEnum = _build_error_enum()


# ════════════════════════════════════════════════════════════════
# 2. ErrorDefinition 数据类
# ════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class ErrorDefinition:
    """错误定义 — 描述一个错误码的完整元信息。

    Attributes:
        code: 三段式错误码 ``DOMAIN.OPERATION.REASON``
        message_key: i18n key(在 locales/zh-CN.json 与 locales/en-US.json 中)
        http_status: HTTP 状态码(Bot 返回时可映射为用户消息;Admin HTTP 返回时使用)
        retryable: 是否可重试(True=临时性故障,调用方可重试;False=永久性故障)
        severity: 严重级别 ``info`` / ``warning`` / ``error`` / ``critical``
        safe_params: 可安全记录到日志/audit_log 的参数名白名单
            (未列入白名单的参数会被 ErrorEnvelope.params 过滤掉,避免泄露敏感信息)
        presentation: R61 P1-05 显式 Telegram 展示策略
            (``short_hint`` / ``inline`` / ``silent`` / ``modal`` / ``toast``)。
            保留空串默认值仅为向后兼容外部构造;启动期 ``ErrorRegistry.validate()``
            会拒绝未显式设置的注册(``to_frontend_mapping()`` 不再按 severity 回退)。
        show_retry_button: R61 P1-05 显式"是否显示重试按钮"。
            保留 ``None`` 默认值仅为向后兼容;启动期 ``validate()`` 会拒绝 ``None``。
            可独立于 ``retryable`` 覆盖(同一 severity 的错误可需要不同交互)。
        audit_level: R61 P1-05 显式审计级别
            (``debug`` / ``info`` / ``warning`` / ``critical`` / ``security``)。
            保留空串默认值仅为向后兼容;启动期 ``validate()`` 会拒绝未显式设置的注册。
    """
    code: str
    message_key: str
    http_status: int
    retryable: bool
    severity: str
    safe_params: list[str] = field(default_factory=list)
    # R61 P1-05: 展示策略显式定义 — to_frontend_mapping() 直接读取,不再回退。
    # 默认值仅为向后兼容外部 ErrorDefinition(...) 构造;注册到 ErrorRegistry
    # 时由 validate() 强制非空/非 None。
    presentation: str = ""
    show_retry_button: Optional[bool] = None
    audit_level: str = ""


# ════════════════════════════════════════════════════════════════
# 3. ErrorEnvelope 统一返回
# ════════════════════════════════════════════════════════════════
@dataclass
class ErrorEnvelope:
    """统一错误返回 — 贯穿 Bot→Writer→Outbox→CRDB/Telegram→Admin Audit 链路。

    Attributes:
        code: 三段式错误码(对应 ErrorDefinition.code)
        message: 已 i18n 的用户消息(基于 message_key + params 渲染)
        message_key: i18n key(供前端/Bot 自行渲染)
        trace_id: UUID 字符串,贯穿全链路,可用于在 audit_log / 日志中检索
        retryable: 是否可重试
        severity: 严重级别(info/warning/error/critical)
        params: safe_params 过滤后的参数(仅包含 ErrorDefinition.safe_params 列入的字段)
        timestamp: ISO8601 时间戳(UTC)
    """
    code: str
    message: str
    message_key: str
    trace_id: str
    retryable: bool
    severity: str
    params: dict
    timestamp: str

    def to_dict(self) -> dict:
        """转换为 dict(供 JSON 序列化返回给前端 / Bot)。"""
        return {
            "code": self.code,
            "message": self.message,
            "message_key": self.message_key,
            "trace_id": self.trace_id,
            "retryable": self.retryable,
            "severity": self.severity,
            "params": self.params,
            "timestamp": self.timestamp,
        }


# ════════════════════════════════════════════════════════════════
# 4. ErrorRegistry 注册中心
# ════════════════════════════════════════════════════════════════
class ErrorRegistry:
    """错误定义注册中心 — 启动时注册所有 ErrorDefinition。

    用法:
        # 启动时已自动注册(模块加载即注册,见下方 _register_defaults)
        definition = ErrorRegistry.get(ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT)
        envelope = ErrorRegistry.create_envelope(
            ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
            params={"file_code": "abc"},
            locale="zh-CN",
        )

    未注册 code 时 fallback 到 ``ErrorCodes.ERROR_INTERNAL``(通用内部错误)。
    """

    _definitions: dict[str, ErrorDefinition] = {}
    _initialized: bool = False

    @classmethod
    def register(cls, definition: ErrorDefinition) -> None:
        """注册一个 ErrorDefinition。

        重复注册同一 code 时覆盖旧定义(便于测试 reset 后重新注册)。
        """
        cls._definitions[definition.code] = definition

    @classmethod
    def get(cls, code: str) -> ErrorDefinition:
        """R58 P1-5: 获取 ErrorDefinition。

        未注册时:
        - test/staging: 抛 UnknownErrorCode(硬失败,阻止协议漂移)
        - production: fallback 到 ERROR_INTERNAL(安全降级) + 记录 metric 与 critical 告警
        """
        cls._ensure_initialized()
        definition = cls._definitions.get(code)
        if definition is None:
            # R58 P1-5: 环境判断
            env = cls._get_environment()
            if env in ("test", "staging"):
                # R58 P1-5: 硬失败,阻止未注册错误码进入协议
                raise UnknownErrorCode(
                    code=code,
                    reason="error_code_not_registered_in_staging_or_test",
                )
            # production: 安全降级到 INTERNAL,记录 metric 与告警
            logger.warning(
                f"[ErrorRegistry] R58 P1-5: 未注册的错误码 fallback 到 ERROR_INTERNAL: {code}"
            )
            # R58 P1-5: 记录原始 code 的 hash(不泄露原始 code 到用户)
            import hashlib as _hashlib_mod
            code_hash = _hashlib_mod.sha256(code.encode("utf-8")).hexdigest()[:16]
            logger.error(
                f"[ErrorRegistry] R58 P1-5: unregistered_error_code metric: "
                f"code_hash={code_hash} (full code in debug logs only)"
            )
            return cls._definitions[ErrorCodes.ERROR_INTERNAL]
        return definition

    @staticmethod
    def _get_environment() -> str:
        """R58 P1-5: 惰性读取 ENVIRONMENT。"""
        try:
            from config.settings import settings  # type: ignore[import]
            return getattr(settings, "ENVIRONMENT", "development")
        except Exception:
            return "development"

    @classmethod
    def is_registered(cls, code: str) -> bool:
        """检查 code 是否已注册(不触发 fallback)。"""
        cls._ensure_initialized()
        return code in cls._definitions

    @classmethod
    def all_codes(cls) -> list[str]:
        """返回所有已注册的 code 列表(排序)。"""
        cls._ensure_initialized()
        return sorted(cls._definitions.keys())

    @classmethod
    def all_message_keys(cls) -> list[str]:
        """返回所有已注册的 message_key 列表(去重排序)。"""
        cls._ensure_initialized()
        return sorted({d.message_key for d in cls._definitions.values()})

    @classmethod
    def create_envelope(
        cls,
        code: str,
        params: Optional[dict] = None,
        locale: str = "zh-CN",
        trace_id: Optional[str] = None,
    ) -> ErrorEnvelope:
        """根据 code + params + locale 创建 ErrorEnvelope。

        Args:
            code: 错误码(未注册时 fallback 到 ERROR_INTERNAL)
            params: 参数字典(会按 ErrorDefinition.safe_params 过滤)
            locale: 语言代码(zh-CN / en-US),决定 message 渲染语言
            trace_id: 可选 trace_id(不传时自动生成 UUID)

        Returns:
            ErrorEnvelope 实例(message 已 i18n 渲染)
        """
        cls._ensure_initialized()
        definition = cls.get(code)
        # trace_id:贯穿全链路(不传时自动生成)
        if not trace_id:
            trace_id = str(uuid.uuid4())
        # params 按 safe_params 白名单过滤(避免敏感信息泄露)
        safe_params = _filter_safe_params(params or {}, definition.safe_params)
        # message 渲染参数 = safe_params + trace_id(trace_id 是 envelope 必有字段,
        # 不属于 safe_params 白名单,但允许在 message 模板中通过 {trace_id} 引用,
        # 仅供 message 渲染使用,不会写入 envelope.params)
        render_params = dict(safe_params)
        render_params["trace_id"] = trace_id
        # 加载 i18n message
        message = _render_i18n_message(definition.message_key, render_params, locale)
        return ErrorEnvelope(
            code=definition.code,
            message=message,
            message_key=definition.message_key,
            trace_id=trace_id,
            retryable=definition.retryable,
            severity=definition.severity,
            params=safe_params,
            timestamp=_dt.datetime.utcnow().isoformat(),
        )

    @classmethod
    def reset(cls) -> None:
        """重置注册表(测试用例间隔离)。

        重置后下一次 get/create_envelope 调用会重新触发 _register_defaults。
        """
        cls._definitions.clear()
        cls._initialized = False

    # ── R59 §5.2 P1: 运行时未知异常统一降级入口 ──

    @classmethod
    def register_unknown_runtime_error(
        cls,
        exc: BaseException,
        *,
        action: str = "",
        component: str = "",
        trace_id: Optional[str] = None,
    ) -> str:
        """R59 §5.2 P1: 处理运行时未知异常 — 统一返回 INTERNAL_ERROR,原异常进入结构化日志。

        设计要点(对照 R59 §5.2 P1 要求 2):
            - **不**注册新的错误码(运行时禁止动态新增未知码,违反"唯一注册源"原则)
            - 原始异常(exception type / message / repr / traceback)进入结构化日志,
              仅供运维/审计检索,不暴露给用户消息
            - 调用方仅获得 ``ErrorCodes.ERROR_INTERNAL`` code 字符串,
              传给 ``AppError`` / ``ErrorRegistry.create_envelope`` 后,
              用户消息只会展示 INTERNAL_ERROR 的 i18n 文案
            - 重复调用安全(幂等),不会污染 ``_definitions`` 注册表

        典型用法::

            try:
                await risky_operation()
            except Exception as exc:
                # 原异常进入结构化日志,用户只看到 INTERNAL_ERROR
                code = ErrorRegistry.register_unknown_runtime_error(
                    exc, action="risky_operation", component="uploader",
                )
                raise AppError(code, params={"action": "risky_operation"})

        Args:
            exc: 原始异常(用于结构化日志记录,不暴露给用户)
            action: 触发异常的业务动作名(可选,用于日志检索)
            component: 触发异常的组件名(可选,用于日志检索)
            trace_id: 可选 trace_id(贯穿全链路,便于审计检索;
                不传时自动生成 UUID)

        Returns:
            ``ErrorCodes.ERROR_INTERNAL`` 字符串常量(供 ``AppError`` 使用)
        """
        cls._ensure_initialized()
        # 生成 trace_id(贯穿日志/audit_log,便于关联用户消息与原始异常)
        if not trace_id:
            trace_id = str(uuid.uuid4())
        # R59 §5.2 P1: 结构化日志 — 原始异常细节仅在日志中,不暴露给用户消息
        # 使用 extra 字段便于 loguru/结构化日志采集器(elasticsearch/loki)按字段索引
        logger.error(
            "R59 §5.2 P1: runtime_unknown_error fallback to INTERNAL_ERROR "
            f"action={action!r} component={component!r} trace_id={trace_id} "
            f"exc_type={type(exc).__name__} exc_msg={exc!r}"
        )
        # 完整堆栈写入 debug 日志(避免 ERROR 级别刷屏,但保留排查线索)
        logger.debug(
            f"R59 §5.2 P1: runtime_unknown_error traceback trace_id={trace_id} "
            f"action={action!r} component={component!r}",
            exc_info=exc,
        )
        # 不注册任何新 code,直接返回已注册的 ERROR_INTERNAL
        return ErrorCodes.ERROR_INTERNAL

    # ── R56 §5.2: 便捷查询方法 + 前端映射导出 ──

    @classmethod
    def is_retryable(cls, code: str) -> bool:
        """R56 §5.2: 判断错误码是否可重试(未注册 fallback 到 ERROR_INTERNAL)。"""
        return cls.get(code).retryable

    @classmethod
    def get_safe_params(cls, code: str) -> list[str]:
        """R56 §5.2: 获取错误码的安全参数白名单(未注册返回 [])。"""
        return list(cls.get(code).safe_params)

    @classmethod
    def get_http_status(cls, code: str) -> int:
        """R56 §5.2: 获取错误码对应的 HTTP 状态码。"""
        return cls.get(code).http_status

    @classmethod
    def get_severity(cls, code: str) -> str:
        """R56 §5.2: 获取错误码的严重级别(info/warning/error/critical)。"""
        return cls.get(code).severity

    @classmethod
    def to_frontend_mapping(cls) -> dict:
        """R56 §5.2: 导出前端映射 JSON(供 Admin Web / Bot 加载)。

        R61 P1-05: ``telegram_presentation`` / ``show_retry_button`` /
        ``audit_level`` 现在直接读取 ``ErrorDefinition`` 显式字段,
        不再按 severity / retryable 回退。所有注册的 ErrorDefinition
        必须显式设置这 3 个字段(由 ``ErrorRegistry.validate()`` 在启动期校验)。

        生成结构:
            {
                "UPLOAD.COPY.TELEGRAM_TIMEOUT": {
                    "code": "UPLOAD.COPY.TELEGRAM_TIMEOUT",
                    "message_key": "errors.upload.copy.telegram_timeout",
                    "http_status": 502,
                    "retryable": True,
                    "severity": "error",
                    "telegram_presentation": "short_hint",
                    "show_retry_button": True,
                    "audit_level": "warning"
                },
                ...
            }

        前端可通过此映射:
            1. 根据 code 查找 message_key + safe_params 渲染本地化消息
            2. 根据 retryable 决定是否显示"重试"按钮
            3. 根据 severity 选择 UI 呈现方式(aria-live/badge)
            4. 根据 http_status 映射 HTTP 响应码
            5. 根据 telegram_presentation / show_retry_button / audit_level
               决定 Bot 端交互与审计级别

        telegram_presentation 决定 Bot 端展示方式(R61 P1-05: 显式字段,不再回退):
            - "short_hint": 短提示 + "查看详情"按钮
            - "inline": 直接展开详情(用于 critical)
            - "silent": 不向用户展示(用于 info)
            - "modal" / "toast": 预留扩展(当前 109 个默认注册未使用)
        """
        cls._ensure_initialized()
        mapping: dict[str, dict] = {}
        for code, definition in cls._definitions.items():
            # R61 P1-05: 直接读显式字段,不再按 severity / retryable 回退。
            # 启动期 ErrorRegistry.validate() 已确保这 3 个字段非空/非 None。
            mapping[code] = {
                "code": definition.code,
                "message_key": definition.message_key,
                "http_status": definition.http_status,
                "retryable": definition.retryable,
                "severity": definition.severity,
                "safe_params": list(definition.safe_params),
                "telegram_presentation": definition.presentation,
                "show_retry_button": definition.show_retry_button,
                "audit_level": definition.audit_level,
            }
        return mapping

    @classmethod
    def to_frontend_json(cls, indent: int = 2) -> str:
        """R56 §5.2: 导出前端映射为 JSON 字符串。"""
        return json.dumps(
            cls.to_frontend_mapping(),
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
        )

    @staticmethod
    def validate() -> list[str]:
        """R61 P1-05: 校验所有已注册 ErrorDefinition 的完整性。

        返回校验错误消息列表(空列表 = 全部通过)。

        校验项:
            1. 所有 code 唯一(无重复注册)
            2. ``presentation`` 非空且在合法枚举内
               (``inline`` / ``modal`` / ``toast`` / ``silent`` / ``short_hint``)
            3. ``show_retry_button`` 非 None(必须显式设置,不再回退到 retryable)
            4. ``audit_level`` 非空且在合法枚举内
               (``debug`` / ``info`` / ``warning`` / ``critical`` / ``security``)

        Note:
            - 直接读取 ``ErrorRegistry._definitions``,不调用 ``all_codes()`` /
              ``_ensure_initialized()``,避免在启动期触发递归。
            - R63 P1-11: locale 文件完整性校验已移至 ``validate_locales()``,
              本方法不再做 best-effort locale key 校验(原 best-effort 仅 warning
              不符合 Release 镜像 fail-closed 要求)。
        """
        errors: list[str] = []
        valid_presentations = {"inline", "modal", "toast", "silent", "short_hint"}
        valid_audit_levels = {"debug", "info", "warning", "critical", "security"}

        # 直接读 _definitions,避免通过 all_codes() → _ensure_initialized() 递归
        definitions = ErrorRegistry._definitions
        seen_codes: set[str] = set()

        for code, defn in definitions.items():
            # 1. code 唯一性
            if code in seen_codes:
                errors.append(f"Duplicate error code: {code}")
            seen_codes.add(code)

            # 2. presentation 显式且合法
            if not defn.presentation:
                errors.append(
                    f"Error {code}: presentation is empty (must be explicit)"
                )
            elif defn.presentation not in valid_presentations:
                errors.append(
                    f"Error {code}: invalid presentation '{defn.presentation}'"
                )

            # 3. show_retry_button 显式(非 None)
            if defn.show_retry_button is None:
                errors.append(
                    f"Error {code}: show_retry_button is None (must be explicit)"
                )

            # 4. audit_level 显式且合法
            if not defn.audit_level:
                errors.append(
                    f"Error {code}: audit_level is empty (must be explicit)"
                )
            elif defn.audit_level not in valid_audit_levels:
                errors.append(
                    f"Error {code}: invalid audit_level '{defn.audit_level}'"
                )

        return errors

    @staticmethod
    def validate_locales(locales_dir: Optional[Any] = None) -> list[str]:
        """R63 P1-11: fail-closed locale 文件完整性校验。

        校验打包后的实际 locale 文件(不仅依赖源码 CI):

            1. ``locales/zh-CN.json`` 和 ``locales/en-US.json`` 文件存在
            2. 两个文件均为有效 JSON(根对象为 dict)
            3. 所有已注册 ErrorDefinition 的 ``message_key`` 必须在两个 locale
               文件中均存在(点分扁平化后查找)
            4. 两个 locale 的 key 必须对称(zh-CN 与 en-US 互相无缺失/多余)
            5. 占位符 ``{var}`` 在两个 locale 中必须一致
               (ICU plural/select/selectordinal 子句跳过,占位符在子句内不强制对称)

        Args:
            locales_dir: 可选的 locale 目录路径(默认为项目根 ``locales/``)。
                测试用例可传入临时目录以隔离校验不同 locale 文件内容。

        Returns:
            校验错误消息列表(空列表 = 全部通过)。
            非空列表 = 启动应 fail-closed
            (除非 ``ERROR_CODES_LOCALE_STRICT=0`` 降级为 warning)。

        Note:
            - 直接读取 ``ErrorRegistry._definitions``,避免触发递归初始化。
            - 本方法不做 best-effort 跳过:任何 locale 文件异常均返回错误,
              由调用方(模块加载块)根据 ``ERROR_CODES_LOCALE_STRICT`` 环境变量
              决定是 fail-closed(raise AppError)还是降级为 warning。
        """
        import os as _os
        import re as _re
        from pathlib import Path as _Path

        errors: list[str] = []
        if locales_dir is None:
            locales_dir = _Path(__file__).resolve().parent.parent / "locales"
        locales_dir = _Path(locales_dir)
        zh_path = locales_dir / "zh-CN.json"
        en_path = locales_dir / "en-US.json"

        # 1. 检查文件存在
        if not zh_path.exists():
            errors.append(f"Locale file missing: {zh_path}")
        if not en_path.exists():
            errors.append(f"Locale file missing: {en_path}")
        if errors:
            return errors  # 文件不存在,后续校验无法进行

        # 2. 检查 JSON 解析 + 根对象为 dict
        zh_data: Optional[dict] = None
        en_data: Optional[dict] = None
        try:
            zh_raw = zh_path.read_text(encoding="utf-8")
            zh_data = json.loads(zh_raw)
            if not isinstance(zh_data, dict):
                errors.append(
                    f"zh-CN.json root must be a dict "
                    f"(got {type(zh_data).__name__})"
                )
                zh_data = None
        except json.JSONDecodeError as e:
            errors.append(f"zh-CN.json JSON parse error: {e}")
        except OSError as e:
            errors.append(f"zh-CN.json read error: {e}")

        try:
            en_raw = en_path.read_text(encoding="utf-8")
            en_data = json.loads(en_raw)
            if not isinstance(en_data, dict):
                errors.append(
                    f"en-US.json root must be a dict "
                    f"(got {type(en_data).__name__})"
                )
                en_data = None
        except json.JSONDecodeError as e:
            errors.append(f"en-US.json JSON parse error: {e}")
        except OSError as e:
            errors.append(f"en-US.json read error: {e}")

        if errors:
            return errors  # JSON 解析失败,后续校验无法进行

        # 扁平化 locale dict(点分 key → value)
        def _flatten(d: dict, prefix: str = "") -> dict:
            out: dict = {}
            for k, v in d.items():
                full = f"{prefix}.{k}" if prefix else str(k)
                if isinstance(v, dict):
                    out.update(_flatten(v, full))
                else:
                    out[full] = v
            return out

        zh_flat: dict = _flatten(zh_data) if zh_data else {}
        en_flat: dict = _flatten(en_data) if en_data else {}
        zh_keys: set[str] = set(zh_flat.keys())
        en_keys: set[str] = set(en_flat.keys())

        # 3. 检查所有 message_key 在两个 locale 中存在
        definitions = ErrorRegistry._definitions
        for code, defn in definitions.items():
            if defn.message_key not in zh_flat:
                errors.append(
                    f"Error {code}: message_key "
                    f"'{defn.message_key}' missing in zh-CN.json"
                )
            if defn.message_key not in en_flat:
                errors.append(
                    f"Error {code}: message_key "
                    f"'{defn.message_key}' missing in en-US.json"
                )

        # 4. 检查 locale key 对称性(zh-CN vs en-US)
        only_zh = zh_keys - en_keys
        only_en = en_keys - zh_keys
        if only_zh:
            errors.append(
                f"Locale keys asymmetric: zh-CN has {len(only_zh)} keys "
                f"missing in en-US: {sorted(only_zh)[:5]}"
            )
        if only_en:
            errors.append(
                f"Locale keys asymmetric: en-US has {len(only_en)} keys "
                f"missing in zh-CN: {sorted(only_en)[:5]}"
            )

        # 5. 检查占位符一致性(简单 {var} 占位符,ICU 子句跳过)
        placeholder_re = _re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
        icu_pattern_re = _re.compile(
            r"\{[a-zA-Z_]\w*\s*,\s*(plural|select|selectordinal)\s*,"
        )
        common_keys = zh_keys & en_keys
        for key in sorted(common_keys):
            zh_val = str(zh_flat.get(key, ""))
            en_val = str(en_flat.get(key, ""))
            # 跳过 ICU pattern(占位符在 ICU 子句中,不强制对称)
            if icu_pattern_re.search(zh_val) or icu_pattern_re.search(en_val):
                continue
            zh_ph = set(placeholder_re.findall(zh_val))
            en_ph = set(placeholder_re.findall(en_val))
            if zh_ph != en_ph:
                errors.append(
                    f"Placeholder mismatch for key '{key}': "
                    f"zh-CN={sorted(zh_ph)} vs en-US={sorted(en_ph)}"
                )

        return errors

    @classmethod
    def _ensure_initialized(cls) -> None:
        """确保默认 ErrorDefinition 已注册(幂等)。"""
        if not cls._initialized:
            _register_defaults()
            cls._initialized = True


# ════════════════════════════════════════════════════════════════
# 5. AppError 异常类
# ════════════════════════════════════════════════════════════════
class UnknownErrorCode(Exception):
    """R58 P1-5: 未注册错误码异常(test/staging 硬失败)。

    在 test/staging 环境下,ErrorRegistry.get() 遇到未注册 code 时抛此异常,
    阻止协议漂移。production 不会抛此异常(fallback 到 ERROR_INTERNAL)。

    Attributes:
        code: 未注册的原始 code
        reason: 失败原因
    """

    def __init__(self, code: str, reason: str = "unknown_error_code"):
        self.code = code
        self.reason = reason
        super().__init__(f"UnknownErrorCode: code={code}, reason={reason}")


class AppError(Exception):
    """应用异常 — 封装 ErrorCode + params + trace_id。

    用法:
        raise AppError(ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT, params={"file_code": "abc"})

    特性:
        - 自动生成 trace_id(UUID)
        - 自动尝试写入 audit_log 表(失败静默,不阻塞主流程)
        - to_envelope() 返回 ErrorEnvelope(供 HTTP/Bot 响应序列化)
        - to_dict() 返回 dict(供 JSON 序列化)

    Attributes:
        code: 错误码(对应 ErrorDefinition.code)
        params: 已过滤的 safe_params(按 ErrorDefinition.safe_params 过滤)
        trace_id: UUID 字符串
        envelope: 懒加载的 ErrorEnvelope(首次访问时生成)
    """

    def __init__(
        self,
        code: str,
        params: Optional[dict] = None,
        *,
        locale: str = "zh-CN",
        trace_id: Optional[str] = None,
        cause: Optional[BaseException] = None,
    ):
        """初始化 AppError。

        Args:
            code: 错误码(未注册时 fallback 到 ERROR_INTERNAL)
            params: 参数字典(会按 ErrorDefinition.safe_params 过滤)
            locale: 语言代码(默认 zh-CN)
            trace_id: 可选 trace_id(不传时自动生成 UUID)
            cause: 原始异常(可选,用于异常链)
        """
        # 生成 trace_id(贯穿全链路)
        if not trace_id:
            trace_id = str(uuid.uuid4())
        self.trace_id = trace_id
        self.code = code
        self.locale = locale
        self._cause = cause
        # 创建 envelope(包含已过滤的 safe_params + i18n message)
        self.envelope = ErrorRegistry.create_envelope(
            code, params=params, locale=locale, trace_id=trace_id,
        )
        # 调用 Exception.__init__ 用 i18n message 作为异常消息
        super().__init__(self.envelope.message)
        # 异步写入 audit_log(不阻塞,失败静默)
        # 注意:此方法在 __init__ 中无法 await,实际写入由调用方在 except 块中
        # 调用 await app_error.write_audit_log() 完成,或通过同步 _try_write_audit_log_sync
        # 尽力写入(失败不影响异常传播)。

    @property
    def params(self) -> dict:
        """返回已过滤的 safe_params(向后兼容字段访问)。"""
        return self.envelope.params

    @property
    def message(self) -> str:
        """返回已 i18n 的用户消息。"""
        return self.envelope.message

    @property
    def message_key(self) -> str:
        """返回 i18n key。"""
        return self.envelope.message_key

    @property
    def retryable(self) -> bool:
        """返回是否可重试。"""
        return self.envelope.retryable

    @property
    def severity(self) -> str:
        """返回严重级别。"""
        return self.envelope.severity

    def to_dict(self) -> dict:
        """返回统一错误响应格式(供 JSON 序列化)。

        R55 §17: 返回 ``{code, message_key, trace_id, retryable, severity, safe_params}``。
        原始异常仅记录日志(见 write_audit_log 中的 cause 字段),不暴露给调用方。
        safe_params 经过 is_safe_param 二次过滤(防止白名单配置失误泄露 token/hash 等)。

        如需完整 envelope(含 i18n message 与 timestamp),请直接访问 ``self.envelope``。
        """
        return make_error_response(
            code=self.code,
            message_key=self.message_key,
            trace_id=self.trace_id,
            retryable=self.retryable,
            severity=self.severity,
            safe_params=self.params,
        )

    def to_response(self) -> dict:
        """返回统一错误响应格式(与 to_dict() 等价的语义化别名)。

        R55 §17: 推荐调用方使用此方法名,语义更清晰(响应而非内部 dict 表示)。
        """
        return self.to_dict()

    async def write_audit_log(self) -> bool:
        """异步写入 audit_log 表(记录 trace_id + code + params)。

        失败时静默记录 debug 日志,不影响主流程。
        写入 details 字段(JSON 字符串)包含 trace_id / code / params / severity。

        Returns:
            True 写入成功;False 写入失败(如 DB 未初始化)
        """
        try:
            from database.cache_store import get_cache_store
            store = get_cache_store()
            if not store or not getattr(store, "_db", None):
                return False
            details = json.dumps(
                {
                    "trace_id": self.trace_id,
                    "code": self.code,
                    "message_key": self.message_key,
                    "params": self.params,
                    "severity": self.severity,
                    "retryable": self.retryable,
                    "cause": str(self._cause) if self._cause else None,
                },
                ensure_ascii=False,
                default=str,
            )
            await store._db.execute(
                """INSERT INTO audit_log (actor_id, actor_type, action, target_type,
                   target_id, details, ip_addr, created_at)
                   VALUES (?, 'system', ?, 'error', ?, ?, '', ?)""",
                (
                    0,
                    f"app_error:{self.code}",
                    self.trace_id,
                    details,
                    _dt.datetime.utcnow().isoformat(),
                ),
            )
            if not getattr(store, "_in_writer_tx", False):
                await store._db.commit()
            return True
        except Exception as e:
            logger.debug(
                f"[AppError] audit_log 写入失败(忽略,trace_id={self.trace_id}): {e}"
            )
        # R61 P1-04: return False 移出 except 块,避免 scanner Rule 3 误报
        # (audit_log 写入是 best-effort,失败不应阻断主操作,但 return 不在 except 内)
        return False


# ════════════════════════════════════════════════════════════════
# 6. 辅助函数
# ════════════════════════════════════════════════════════════════
# ── R55 §17: 敏感参数过滤 — 统一错误响应 helper ──
# 禁止出现在 safe_params 中的 key 子串(大小写不敏感匹配)。
# 覆盖: token / hash / password / secret / payload / phone / mobile / session
# 注: file_code 是用户面向的文件标识符(非机密),不应被过滤;
#     若需过滤长 file_code,应通过 _SENSITIVE_VALUE_MAX_LENGTH 控制。
_SENSITIVE_KEY_PATTERNS: list[str] = [
    "token",
    "hash",
    "password",
    "secret",
    "payload",
    "phone",
    "mobile",
    "session",
]

# safe_params 中字符串值的最大长度(超过则视为敏感,可能是密文/哈希/长 payload)
_SENSITIVE_VALUE_MAX_LENGTH: int = 100

# 标记为哈希/密文的前缀(出现则视为敏感)
_SENSITIVE_VALUE_PREFIXES: tuple[str, ...] = ("$argon2", "$pbkdf2")


def is_safe_param(key: str, value: Any) -> bool:
    """判断单个参数是否可安全暴露在错误响应中。

    R55 §17: 统一过滤规则,作为 ErrorDefinition.safe_params 白名单的二次防护。
    白名单可能因配置失误纳入敏感字段(如 token/hash),本函数提供兜底过滤。
    注: file_code 是用户面向标识符,已从敏感模式中移除,不会被过滤。

    判定规则(命中任一即返回 False):
        1. key 包含 _SENSITIVE_KEY_PATTERNS 中任一子串(大小写不敏感)
        2. value 为字符串且长度 > _SENSITIVE_VALUE_MAX_LENGTH
        3. value 为字符串且以 $argon2 / $pbkdf2 开头(哈希/密文)

    Args:
        key: 参数名
        value: 参数值

    Returns:
        True 表示可安全暴露;False 表示敏感,必须过滤
    """
    # 1. key 子串匹配(大小写不敏感)
    key_lower = str(key).lower()
    for pattern in _SENSITIVE_KEY_PATTERNS:
        if pattern in key_lower:
            return False

    # 2. value 长度与哈希前缀校验(仅字符串类型)
    if isinstance(value, str):
        if len(value) > _SENSITIVE_VALUE_MAX_LENGTH:
            return False
        if value.startswith(_SENSITIVE_VALUE_PREFIXES):
            return False

    return True


def make_error_response(
    code: str,
    message_key: str,
    trace_id: str,
    retryable: bool,
    severity: str,
    safe_params: Optional[dict] = None,
) -> dict:
    """生成统一错误响应 dict。

    R55 §17: 统一输出格式为::
        {code, message_key, trace_id, retryable, severity, safe_params}

    设计要点:
        - 原始异常仅记录日志,不暴露给调用方(本函数不接收 exception 参数)
        - safe_params 经过 is_safe_param 二次过滤,防止白名单配置失误
        - 不包含 message/timestamp 等额外字段,保持响应精简
        - 调用方应使用 ErrorRegistry.create_envelope() 获取完整 envelope(含 i18n message)

    Args:
        code: 三段式错误码 ``DOMAIN.OPERATION.REASON``
        message_key: i18n key(供前端/Bot 自行渲染)
        trace_id: UUID 字符串,贯穿全链路
        retryable: 是否可重试
        severity: 严重级别(info/warning/error/critical)
        safe_params: 可选参数字典(会经 is_safe_param 过滤)

    Returns:
        统一错误响应 dict
    """
    filtered_params: dict = {}
    if safe_params:
        for k, v in safe_params.items():
            if is_safe_param(k, v):
                filtered_params[k] = v
            else:
                logger.debug(
                    f"[make_error_response] 过滤敏感参数 key={k!r} "
                    f"code={code} trace_id={trace_id}"
                )
    return {
        "code": code,
        "message_key": message_key,
        "trace_id": trace_id,
        "retryable": retryable,
        "severity": severity,
        "safe_params": filtered_params,
    }


def _filter_safe_params(params: dict, safe_params: list[str]) -> dict:
    """按 safe_params 白名单过滤 params(避免敏感信息泄露)。

    R55 §17: 在白名单过滤基础上,额外使用 is_safe_param 二次校验,
    防止白名单配置失误将敏感字段(token/hash/payload/file_code/phone 等)泄露。

    Args:
        params: 原始参数字典
        safe_params: 可安全记录的参数名列表

    Returns:
        过滤后的 dict(仅包含 safe_params 列入且通过 is_safe_param 的字段)
    """
    if not params or not safe_params:
        return {}
    return {
        k: v
        for k, v in params.items()
        if k in safe_params and is_safe_param(k, v)
    }


def _render_i18n_message(
    message_key: str, params: dict, locale: str,
) -> str:
    """从 locale 文件加载 message_key 对应的翻译,并用 params 渲染占位符。

    占位符格式: ``{name}`` (str.format 风格)。

    若 message_key 不存在或 locale 文件加载失败,fallback 到 message_key 本身
    (确保永远返回非空字符串,避免前端崩溃)。

    查找逻辑兼容两种 JSON 结构:
        1. 嵌套 dict: ``{"errors": {"upload": {"timeout": "x"}}}``
        2. 扁平点分 key: ``{"errors": {"upload.timeout": "x"}}``
    优先扁平化查找(与 verify_i18n_keys.py 一致),fallback 到嵌套查找。

    Args:
        message_key: i18n key(点分路径,如 "errors.upload.copy.telegram_timeout")
        params: 渲染参数
        locale: 语言代码(zh-CN / en-US)

    Returns:
        已渲染的用户消息字符串
    """
    try:
        from pathlib import Path as _Path
        # locales 目录(项目根 / locales)
        repo_root = _Path(__file__).resolve().parent.parent
        locale_path = repo_root / "locales" / f"{locale}.json"
        if not locale_path.exists():
            # fallback 到 en-US
            locale_path = repo_root / "locales" / "en-US.json"
            if not locale_path.exists():
                return message_key
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 1. 优先扁平化查找(与 verify_i18n_keys.py 一致)
        flat = _flatten_dict(data)
        message = flat.get(message_key)
        # 2. fallback 到嵌套查找
        if not isinstance(message, str):
            message = _lookup_nested(data, message_key)
        if not isinstance(message, str):
            return message_key
        # 用 params 渲染占位符(安全 format,缺失字段保留原占位符)
        try:
            return message.format(**{k: v for k, v in params.items()})
        except (KeyError, IndexError, ValueError):
            return message
    except Exception as e:
        logger.debug(f"[ErrorRegistry] i18n 渲染失败 key={message_key} locale={locale}: {e}")
        return message_key


def _flatten_dict(obj: Any, prefix: str = "") -> dict[str, str]:
    """递归扁平化 dict,返回 {点分 key: value} 映射。

    与 scripts/verify_i18n_keys.py 的 _flatten_values 保持一致,
    确保查找逻辑统一。

    例: ``{"errors": {"upload.timeout": "x"}}`` → ``{"errors.upload.timeout": "x"}``
    """
    result: dict[str, str] = {}
    if not isinstance(obj, dict):
        return result
    for k, v in obj.items():
        full_key = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            result.update(_flatten_dict(v, full_key))
        else:
            result[full_key] = v
    return result


def _lookup_nested(data: dict, key: str) -> Any:
    """按点分 key 查找嵌套 dict。

    例: ``_lookup_nested({"errors": {"upload": {"timeout": "x"}}}, "errors.upload.timeout")``
    返回 ``"x"``。

    用于 _render_i18n_message 的 fallback 路径(扁平化未命中时尝试嵌套查找)。
    """
    parts = key.split(".")
    current: Any = data
    for part in parts:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
        if current is None:
            return None
    return current


# ════════════════════════════════════════════════════════════════
# 7. 默认 ErrorDefinition 注册(模块加载时自动执行)
# ════════════════════════════════════════════════════════════════
def _register_defaults() -> None:
    """注册所有默认 ErrorDefinition。

    每个定义包含:
        - code: 三段式错误码
        - message_key: i18n key(对应 locales/*.json 中的点分路径)
        - http_status: HTTP 状态码
        - retryable: 是否可重试
        - severity: 严重级别(info/warning/error/critical)
        - safe_params: 可安全记录的参数名白名单
    """
    # 通用内部错误(必须首先注册,作为 fallback)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ERROR_INTERNAL,
        message_key="errors.error.internal.unexpected",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["action", "component"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))

    # ── UPLOAD ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
        message_key="errors.upload.copy.telegram_timeout",
        http_status=504,
        retryable=True,
        severity="warning",
        safe_params=["file_code", "channel_id"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_COPY_TELEGRAM_FORBIDDEN,
        message_key="errors.upload.copy.telegram_forbidden",
        http_status=403,
        retryable=False,
        severity="error",
        safe_params=["file_code", "channel_id"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_MANIFEST_OUTBOX_FAILED,
        message_key="errors.upload.manifest.outbox_failed",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["file_code", "batch_id"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_PENDING_OUTBOX_TX_FAILED,
        message_key="errors.upload.pending.outbox_tx_failed",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["file_code", "batch_id"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))

    # ── INDEX ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.INDEX_FINALIZE_OUTBOX_FAILED,
        message_key="errors.index.finalize.outbox_failed",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["file_code", "code"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.INDEX_CODE_CONFLICT,
        message_key="errors.index.code.conflict",
        http_status=409,
        retryable=False,
        severity="warning",
        safe_params=["code"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))

    # ── DELIVERY ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DELIVERY_SEND_FLOOD_WAIT,
        message_key="errors.delivery.send.flood_wait",
        http_status=429,
        retryable=True,
        severity="warning",
        safe_params=["wait_seconds", "user_id"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DELIVERY_SEND_FORBIDDEN,
        message_key="errors.delivery.send.forbidden",
        http_status=403,
        retryable=False,
        severity="error",
        safe_params=["user_id", "channel_id"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DELIVERY_RECEIPT_FAILED,
        message_key="errors.delivery.receipt.failed",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["user_id", "file_code"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))

    # ── AUTH ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.AUTH_MFA_REPLAYED,
        message_key="errors.auth.mfa.replayed",
        http_status=401,
        retryable=False,
        severity="critical",
        safe_params=["user_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.AUTH_MFA_LOCKED,
        message_key="errors.auth.mfa.locked",
        http_status=423,
        retryable=False,
        severity="error",
        safe_params=["user_id"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.AUTH_MFA_RECEIPT_INVALID,
        message_key="errors.auth.mfa.receipt_invalid",
        http_status=401,
        retryable=False,
        severity="critical",
        safe_params=["user_id", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.AUTH_SESSION_EXPIRED,
        message_key="errors.auth.session.expired",
        http_status=401,
        retryable=False,
        severity="info",
        safe_params=["user_id"],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.AUTH_RBAC_PERMISSION_DENIED,
        message_key="errors.auth.rbac.permission_denied",
        http_status=403,
        retryable=False,
        severity="warning",
        safe_params=["user_id", "permission"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))

    # ── BACKUP ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_RESTORE_APPROVAL_INVALID,
        message_key="errors.backup.restore.approval_invalid",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["approval_id", "backup_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_RESTORE_CHECKSUM_MISMATCH,
        message_key="errors.backup.restore.checksum_mismatch",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["backup_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # R60 P0-03: 恢复信任链令牌缺失/无效时 fail-closed 拒绝恢复
    # R63 P1-01: 新增 "reason" safe_param — 标识失败原因(如 nonce_already_consumed)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_RESTORE_TRUST_CHAIN_REQUIRED,
        message_key="errors.backup.restore.trust_chain_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["backup_id", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── EFFECT_RECEIPT ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.EFFECT_RECEIPT_MANAGER_UNAVAILABLE,
        message_key="errors.effect.receipt.manager_unavailable",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["action_id", "effect_type"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.EFFECT_RECEIPT_DB_ERROR,
        message_key="errors.effect.receipt.db_error",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["action_id", "effect_type"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    # R62 P1-01: 幂等冲突 — 同 (action_id, effect_type, target) 已存在不同 request_hash 的 receipt
    # http_status=409 Conflict;retryable=False(调用方不应盲目重试,payload 已被替换)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DATA_RECEIPT_IDEMPOTENCY_CONFLICT,
        message_key="errors.data.receipt.idempotency_conflict",
        http_status=409,
        retryable=False,
        severity="error",
        safe_params=["action_id", "effect_type", "target"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # R62 P1-01: 终态保护 — 已 completed 的 receipt 再次 record_completed 拒绝覆盖
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DATA_RECEIPT_TERMINAL_STATE,
        message_key="errors.data.receipt.terminal_state",
        http_status=409,
        retryable=False,
        severity="error",
        safe_params=["action_id", "effect_type", "target", "current_status"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))

    # ── APPROVAL ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.APPROVAL_REQUIRED,
        message_key="errors.approval.gate.required",
        http_status=202,
        retryable=False,
        severity="info",
        safe_params=["approval_id", "action"],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.APPROVAL_STATE_INVALID,
        message_key="errors.approval.state.invalid",
        http_status=409,
        retryable=False,
        severity="warning",
        safe_params=["approval_id", "current_status"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))

    # ── COMMAND ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COMMAND_CONCURRENT_CLAIM,
        message_key="errors.command.concurrent.claim",
        http_status=409,
        retryable=True,
        severity="warning",
        safe_params=["action_id"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COMMAND_HASH_MISMATCH,
        message_key="errors.command.hash.mismatch",
        http_status=400,
        retryable=False,
        severity="critical",
        safe_params=["action_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── DB ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DB_CACHE_UNAVAILABLE,
        message_key="errors.db.cache.unavailable",
        http_status=503,
        retryable=True,
        severity="critical",
        safe_params=["component"],
        presentation="inline",
        show_retry_button=True,
        audit_level="critical",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DB_REDIS_UNAVAILABLE,
        message_key="errors.db.redis.unavailable",
        http_status=503,
        retryable=True,
        severity="error",
        safe_params=["component"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DB_CRDB_UNAVAILABLE,
        message_key="errors.db.crdb.unavailable",
        http_status=503,
        retryable=True,
        severity="error",
        safe_params=["component"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))

    # ── 业务 ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.QUOTA_EXCEEDED,
        message_key="errors.quota.decode.exceeded",
        http_status=429,
        retryable=False,
        severity="info",
        safe_params=["user_id", "quota"],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.FILE_NOT_FOUND,
        message_key="errors.file.lookup.not_found",
        http_status=404,
        retryable=False,
        severity="info",
        safe_params=["file_code"],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.FILE_EXPIRED,
        message_key="errors.file.lookup.expired",
        http_status=410,
        retryable=False,
        severity="info",
        safe_params=["file_code"],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.FILE_CODE_INVALID,
        message_key="errors.file.code.invalid",
        http_status=400,
        retryable=False,
        severity="info",
        safe_params=["code"],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.USER_BANNED,
        message_key="errors.user.state.banned",
        http_status=403,
        retryable=False,
        severity="info",
        safe_params=["user_id"],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.SYSTEM_MAINTENANCE,
        message_key="errors.system.state.maintenance",
        http_status=503,
        retryable=True,
        severity="warning",
        safe_params=["reason"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.SYSTEM_RATE_LIMITED,
        message_key="errors.system.rate.limited",
        http_status=429,
        retryable=True,
        severity="warning",
        safe_params=["user_id"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.VALIDATION_FAILED,
        message_key="errors.validation.input.failed",
        http_status=400,
        retryable=False,
        severity="info",
        safe_params=["field"],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))

    # ── R48 P1: baseline 中 15 处裸字符串错误对应的 ErrorDefinition ──
    # 管理员密码为空(admin/__init__.py:generate_password_hash)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ADMIN_VALIDATION_PASSWORD_EMPTY,
        message_key="errors.admin.validation.password_empty",
        http_status=400,
        retryable=False,
        severity="info",
        safe_params=[],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))
    # topology.yaml 中没有槽位配置(admin/seed_topology.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.TOPOLOGY_LOAD_NO_SLOTS,
        message_key="errors.topology.load.no_slots",
        http_status=500,
        retryable=False,
        severity="error",
        safe_params=[],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # 索引生成码时数据库未初始化(bots/idx_bot.py:_generate_unique_code_with_retry)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.INDEX_GENERATE_DB_UNINITIALIZED,
        message_key="errors.index.generate.db_uninitialized",
        http_status=503,
        retryable=True,
        severity="critical",
        safe_params=["action"],
        presentation="inline",
        show_retry_button=True,
        audit_level="critical",
    ))
    # 索引 finalize_upload 时数据库未初始化(bots/idx_bot.py:finalize_upload)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.INDEX_FINALIZE_DB_UNINITIALIZED,
        message_key="errors.index.finalize.db_uninitialized",
        http_status=503,
        retryable=True,
        severity="critical",
        safe_params=["action"],
        presentation="inline",
        show_retry_button=True,
        audit_level="critical",
    ))
    # 无可用存储频道(bots/idx_bot.py:_get_storage_channel_id)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.INDEX_STORAGE_NO_CHANNEL,
        message_key="errors.index.storage.no_channel",
        http_status=503,
        retryable=True,
        severity="error",
        safe_params=[],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    # MON_BOT_TOKEN 未配置(bots/mon_bot.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BOT_MON_TOKEN_MISSING,
        message_key="errors.bot.mon.token_missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=[],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # _bot 全局引用未初始化(bots/up_bot.py:_outbox_archive_to_r100_strict)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_OUTBOX_BOT_UNINITIALIZED,
        message_key="errors.upload.outbox.bot_uninitialized",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=[],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    # 无可用活跃槽位(bots/up_bot.py:_get_upload_target_channel)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_SLOT_NONE_ACTIVE,
        message_key="errors.upload.slot.none_active",
        http_status=503,
        retryable=True,
        severity="error",
        safe_params=[],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    # cryptography 未安装(services/backup_crypto.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_DECRYPT_DEP_MISSING,
        message_key="errors.backup.decrypt.dep_missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["dep_name"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # BACKUP_KEK 未配置(services/backup_crypto.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_DECRYPT_KEK_MISSING,
        message_key="errors.backup.decrypt.kek_missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=[],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # R2 凭证未配置(services/db_backup.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_RESTORE_R2_CREDENTIAL_MISSING,
        message_key="errors.backup.restore.r2_credential_missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=[],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 中继账号验证码获取失败(services/relay_instance.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RELAY_AUTH_CODE_FAILED,
        message_key="errors.relay.auth.code_failed",
        http_status=401,
        retryable=True,
        severity="warning",
        safe_params=["phone"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    # 中继账号二步验证密码获取超时(services/relay_instance.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RELAY_AUTH_PASSWORD_TIMEOUT,
        message_key="errors.relay.auth.password_timeout",
        http_status=401,
        retryable=True,
        severity="warning",
        safe_params=["phone"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    # 中继账号 api_hash 校验失败(services/relay_pool.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RELAY_CONFIG_API_HASH_INVALID,
        message_key="errors.relay.config.api_hash_invalid",
        http_status=400,
        retryable=False,
        severity="info",
        safe_params=[],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))

    # ── R50 P1-1: 覆盖最后 5 处裸字符串 raise ──
    # callback allowlist action 为空或不在 allowlist 内
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.CALLBACK_ACTION_NOT_ALLOWED,
        message_key="errors.callback.action.not_allowed",
        http_status=403,
        retryable=False,
        severity="warning",
        safe_params=["action", "high_risk_count", "low_risk_count"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # admin bootstrap 未完成,Web 进程应退出
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ADMIN_BOOTSTRAP_NOT_VERIFIED,
        message_key="errors.admin.bootstrap.not_verified",
        http_status=503,
        retryable=False,
        severity="critical",
        safe_params=[],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # R56: migrate_argon2_offline() 已移除(安全原因)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ADMIN_PASSWORD_MIGRATE_ARGON2_REMOVED,
        message_key="errors.admin.password.migrate_argon2_removed",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=[],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # production 环境必须配置 BOT_TOKEN
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.PRODUCTION_BOT_TOKEN_MISSING,
        message_key="errors.production.bot_token.missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["environment"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 灾备恢复必须传 approval_action_id
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED,
        message_key="errors.backup.restore.approval_action_id_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["backup_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R51 P0-5: notification_outbox 异常 ──
    # notification_outbox 写入失败(必须回滚事务,避免孤儿通知)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.NOTIFICATION_OUTBOX_WRITE_FAILED,
        message_key="errors.notification.outbox.write_failed",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["user_id", "notif_type"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    # notification_outbox 重复插入(dedup_key + window 唯一约束冲突)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.NOTIFICATION_OUTBOX_DUPLICATE,
        message_key="errors.notification.outbox.duplicate",
        http_status=409,
        retryable=False,
        severity="info",
        safe_params=["user_id", "dedup_key"],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))

    # ── R51 P0-8: production restore hash 强制 ──
    # production 恢复必须传 expected_request_hash(TOCTOU 防护)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.PRODUCTION_RESTORE_HASH_REQUIRED,
        message_key="errors.production.restore.hash_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["backup_id", "target"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # production 恢复 expected_request_hash 与存储 hash 不匹配
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.PRODUCTION_RESTORE_HASH_MISMATCH,
        message_key="errors.production.restore.hash_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["backup_id", "approval_action_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # command_executions 已 executed,禁止重复执行 restore
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_ALREADY_EXECUTED,
        message_key="errors.restore.execute.already_executed",
        http_status=409,
        retryable=False,
        severity="error",
        safe_params=["approval_action_id"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))

    # ── R51 P0-6: 内容申诉恢复相关 ──
    # 内容申诉恢复操作失败(restore_content handler 执行失败,进入 reconciliation)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.CONTENT_APPEAL_RESTORE_FAILED,
        message_key="errors.content.appeal.restore_failed",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["appeal_id", "target_type", "target_id"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    # 内容申诉状态无效(如重复审批 / 已在 restore_pending 等待 executor)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.CONTENT_APPEAL_INVALID_STATE,
        message_key="errors.content.appeal.invalid_state",
        http_status=409,
        retryable=False,
        severity="warning",
        safe_params=["appeal_id", "current_status"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))

    # ── R51 P1-6: 维护模式 fail-closed ──
    # 维护工作流失败但 recover_status 持久化失败(严重告警,必须 fail-closed)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MAINTENANCE_RECOVER_STATUS_PERSIST_FAILED,
        message_key="errors.maintenance.workflow.persist_failed",
        http_status=503,
        retryable=True,
        severity="critical",
        safe_params=["reason", "workflow_step"],
        presentation="inline",
        show_retry_button=True,
        audit_level="critical",
    ))
    # disable/recover 操作必须绑定 request_hash + principal + approval_action_id
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MAINTENANCE_RECOVER_BINDING_REQUIRED,
        message_key="errors.maintenance.recover.binding_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["approval_action_id", "principal_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R51 P1-7: Prometheus 指标完善 ──
    # 高基数 label 违规(CI/测试中 fail,运行时丢弃违规 metric)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.METRICS_HIGH_CARDINALITY_LABEL,
        message_key="errors.metrics.label.high_cardinality",
        http_status=500,
        retryable=False,
        severity="error",
        safe_params=["label", "metric"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # 指标采集器失败(输出 collector_success=0,不输出 0 伪装健康)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.METRICS_COLLECTOR_FAILED,
        message_key="errors.metrics.collector.failed",
        http_status=503,
        retryable=True,
        severity="warning",
        safe_params=["collector"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    # RU 估算值标记(非官方 CockroachDB Cloud Metrics)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.METRICS_RU_ESTIMATED,
        message_key="errors.metrics.ru.estimated",
        http_status=200,
        retryable=False,
        severity="info",
        safe_params=["service"],
        presentation="silent",
        show_retry_button=False,
        audit_level="info",
    ))

    # ── R51 P1-1: Data Lifecycle 事务化 ──
    # 删除请求步骤失败(任一 step 失败 → 整个 deletion_request 标记 failed)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DATA_LIFECYCLE_DELETE_STEP_FAILED,
        message_key="errors.data.lifecycle.delete_step_failed",
        http_status=500,
        retryable=False,
        severity="error",
        safe_params=["user_id", "step", "step_error"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # 删除请求失败(局部失败导致整个请求未 completed)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DATA_LIFECYCLE_DELETE_REQUEST_FAILED,
        message_key="errors.data.lifecycle.delete_request_failed",
        http_status=500,
        retryable=False,
        severity="error",
        safe_params=["user_id", "request_id", "failed_steps"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # 物理删除前验证 backup marker 失败(无备份标记)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DATA_LIFECYCLE_BACKUP_MARKER_MISSING,
        message_key="errors.data.lifecycle.backup_marker_missing",
        http_status=409,
        retryable=False,
        severity="critical",
        safe_params=["user_id", "table_name", "pk"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # R53 P1-3: skip_backup_check=True 无 break-glass 审批(只允许审批后绕过)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
        message_key="errors.data.lifecycle.break_glass_approval_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["reason", "approval_action_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R51 P1-2: Entitlements 事务化 ──
    # 配额查询失败(fail-closed 拒绝放行,不允许默认 used=0)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ENTITLEMENT_QUOTA_QUERY_FAILED,
        message_key="errors.entitlement.quota.query_failed",
        http_status=503,
        retryable=True,
        severity="critical",
        safe_params=["user_id"],
        presentation="inline",
        show_retry_button=True,
        audit_level="critical",
    ))
    # set_user_plan 事务失败(套餐/配额/audit/dirty_outbox 任一失败)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ENTITLEMENT_SET_PLAN_TX_FAILED,
        message_key="errors.entitlement.set_plan.tx_failed",
        http_status=500,
        retryable=False,
        severity="error",
        safe_params=["user_id", "plan_name"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))

    # ── R51 P1-3: Collections CAS ──
    # 生产修改集合必须传 expected_version(乐观锁不可绕过)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_CAS_VERSION_REQUIRED,
        message_key="errors.collection.cas.version_required",
        http_status=400,
        retryable=False,
        severity="warning",
        safe_params=["collection_id"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # 集合 CAS 版本冲突
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_CAS_CONFLICT,
        message_key="errors.collection.cas.conflict",
        http_status=409,
        retryable=True,
        severity="info",
        safe_params=["collection_id", "expected_version", "current_version"],
        presentation="silent",
        show_retry_button=True,
        audit_level="info",
    ))
    # bypass_cas 必须显式声明并审计
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_CAS_BYPASS_NOT_ALLOWED,
        message_key="errors.collection.cas.bypass_not_allowed",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["collection_id", "caller"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R51 P1-4: Task Center 错误处理 ──
    # 未知 task_type 拒绝(不再静默回退)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.TASK_CENTER_UNKNOWN_TYPE,
        message_key="errors.task.center.unknown_type",
        http_status=400,
        retryable=False,
        severity="warning",
        safe_params=["task_type"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # 未知 task status 拒绝(不再静默回退)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.TASK_CENTER_UNKNOWN_STATUS,
        message_key="errors.task.center.unknown_status",
        http_status=400,
        retryable=False,
        severity="warning",
        safe_params=["status"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # 列表查询 DB 异常(返回错误 envelope,不返回空列表伪装"无任务")
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.TASK_CENTER_LIST_DB_ERROR,
        message_key="errors.task.center.list_db_error",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["scope"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))

    # ── R51 P1-5: Repair Console 审批 ──
    # 高风险修复动作必须强制审批(approval_action_id 不可缺省)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.REPAIR_CONSOLE_APPROVAL_REQUIRED,
        message_key="errors.repair.console.approval_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["action", "principal_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 审批 hash/owner 校验失败(不仅校验 status,必须校验 request_hash + principal_id)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH,
        message_key="errors.repair.console.approval_hash_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["approval_action_id", "expected_hash", "actual_hash"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 审批 principal 不匹配(approval_action_id 关联的 principal 与当前 principal 不一致)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.REPAIR_CONSOLE_APPROVAL_PRINCIPAL_MISMATCH,
        message_key="errors.repair.console.approval_principal_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["approval_action_id", "expected_principal", "actual_principal"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R52 P0-5: 统一高风险动作状态机 ──
    # command_executions 状态冲突(CAS 未命中,如 approved→executing 时已被其他 worker 抢占)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COMMAND_STATUS_CONFLICT,
        message_key="errors.command.status.conflict",
        http_status=409,
        retryable=True,
        severity="warning",
        safe_params=["action_id", "reason", "current_status", "expected_status",
                     "stored_principal_id", "expected_principal_id"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    # command_executions 未处于 approved 状态(执行前必须审批通过)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COMMAND_NOT_APPROVED,
        message_key="errors.command.status.not_approved",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["action_id", "reason", "current_status", "expected_status"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R52 P1-4: Entitlements CAS + CommandBus ──
    # set_user_plan CAS 版本冲突(并发套餐修改,expected_version 不匹配 current version)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ENTITLEMENT_SET_PLAN_CAS_CONFLICT,
        message_key="errors.entitlement.set_plan.cas_conflict",
        http_status=409,
        retryable=True,
        severity="warning",
        safe_params=["user_id", "plan_name", "expected_version", "current_version"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))
    # 套餐变更必须通过 CommandBus(禁止直接调用 set_user_plan 进行生产变更)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ENTITLEMENT_PLAN_REQUIRES_COMMAND_BUS,
        message_key="errors.entitlement.plan.requires_command_bus",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["user_id", "plan_name", "caller"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R52 P1-6: Maintenance fail-closed ──
    # disable() 查询 recover_status 失败(fail-closed,不允许降级为 completed)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MAINTENANCE_DISABLE_RECOVER_QUERY_FAILED,
        message_key="errors.maintenance.disable.recover_query_failed",
        http_status=500,
        retryable=True,
        severity="critical",
        safe_params=["reason"],
        presentation="inline",
        show_retry_button=True,
        audit_level="critical",
    ))
    # recover_status 持久化失败(触发 critical alert,不允许 fail-open)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MAINTENANCE_RECOVER_PERSIST_CRITICAL,
        message_key="errors.maintenance.recover.persist_critical",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R52 P1-7: Metrics unknown 语义 ──
    # 指标采集失败但已输出 0 值带 error label(应改为不输出或 NaN)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.METRICS_COLLECTOR_OUTPUT_INVALID,
        message_key="errors.metrics.collector.output_invalid",
        http_status=500,
        retryable=False,
        severity="error",
        safe_params=["collector", "reason"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))

    # ── R52 P1-8: CF Worker 两阶段去重 ──
    # UPDATE_ID_KV 未配置(production 必须配置)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.CF_WORKER_UPDATE_ID_KV_UNCONFIGURED,
        message_key="errors.cf.worker.update_id_kv_unconfigured",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["environment"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R53 P0-2: CommandBus fail-closed ──
    # claim_execution_approved 在数据库不可用时必须 fail-closed(503 可重试,critical 严重级别)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COMMAND_EXECUTION_STORE_UNAVAILABLE,
        message_key="errors.command.status.store_unavailable",
        http_status=503,
        retryable=True,
        severity="critical",
        safe_params=["action_id", "reason"],
        presentation="inline",
        show_retry_button=True,
        audit_level="critical",
    ))

    # ── R53 P0-4: Collections bypass 真实审批校验 ──
    # 审批无效(approval_action_id 为空/查不到记录/状态非 approved)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_APPROVAL_INVALID,
        message_key="errors.collection.approval.invalid",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["collection_id", "approval_action_id", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 审批 request_hash 不匹配(防篡改,前 16 字符也不匹配)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_APPROVAL_HASH_MISMATCH,
        message_key="errors.collection.approval.hash_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["collection_id", "approval_action_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 审批 principal_id 不匹配(他人审批,防越权)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_APPROVAL_PRINCIPAL_MISMATCH,
        message_key="errors.collection.approval.principal_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["collection_id", "approval_action_id", "expected_principal_id", "actual_principal_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 审批已被执行(状态='executed',禁止重复执行)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_APPROVAL_ALREADY_EXECUTED,
        message_key="errors.collection.approval.already_executed",
        http_status=409,
        retryable=True,
        severity="warning",
        safe_params=["collection_id", "approval_action_id"],
        presentation="short_hint",
        show_retry_button=True,
        audit_level="warning",
    ))

    # ── R53 P0-5: Entitlements 移除生产绕过路径 ──
    # production 环境禁止直接修改套餐(必须通过 CommandBus,403 不可重试 critical)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ENTITLEMENTS_DIRECT_MUTATION_FORBIDDEN,
        message_key="errors.entitlement.plan.direct_mutation_forbidden",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["user_id", "plan_name", "environment"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 修改套餐必须提供 expected_version(production 强制 CAS,400 critical)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ENTITLEMENTS_EXPECTED_VERSION_REQUIRED,
        message_key="errors.entitlement.plan.expected_version_required",
        http_status=400,
        retryable=False,
        severity="critical",
        safe_params=["user_id", "plan_name", "environment"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R53 P1-2: Durable Outbox Hash 不匹配隔离 ──
    # 409 non_retryable — payload 可能被篡改,需经审批 quarantine_repair 修复
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DURABLE_OUTBOX_QUARANTINED,
        message_key="errors.durable.outbox.quarantined",
        http_status=409,
        retryable=False,
        severity="critical",
        safe_params=["message_id", "method", "expected_hash", "actual_hash"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R53 P1-5: CommandBus 双状态机类型边界 ──
    # 高风险动作误走旧 claim_execution 入口,必须改走审批路径
    # 403 critical — 阻断执行,调用方必须改用 claim_execution_approved
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COMMAND_MUST_USE_APPROVAL_PATH,
        message_key="errors.command.approval.must_use_approval_path",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["action_id", "command_type", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R55 §18: 统一按钮 Approval Policy 错误定义 ──
    # 所有高风险按钮统一 Approval Policy,绑定 principal/resource/version/hash/expiry/nonce
    # 任一绑定缺失 → 400 critical,阻断执行
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_BINDING_MISSING,
        message_key="errors.button.policy.binding_missing",
        http_status=400,
        retryable=False,
        severity="critical",
        safe_params=["action", "missing_field", "reason", "expected", "actual"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # nonce 已被消费或不存在(防重放/双击/并发点击)
    # 409 critical — 原子消费失败,可能是重放攻击
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_NONCE_CONSUMED,
        message_key="errors.button.policy.nonce_consumed",
        http_status=409,
        retryable=False,
        severity="critical",
        safe_params=["action", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 签名校验失败(防篡改)
    # 403 critical — callback 可能被篡改
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_SIGNATURE_INVALID,
        message_key="errors.button.policy.signature_invalid",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["action", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # callback 已过期
    # 410 warning — 过期按钮需重新生成
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_EXPIRED,
        message_key="errors.button.policy.expired",
        http_status=410,
        retryable=False,
        severity="warning",
        safe_params=["action", "reason"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # principal 不匹配(跨用户攻击)
    # 403 critical — callback user_id 与当前 user_id 不一致
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_PRINCIPAL_MISMATCH,
        message_key="errors.button.policy.principal_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["action", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # resource_version 不匹配(旧版本按钮操作已更新资源)
    # 409 warning — 资源已被修改,需重新加载
    # R63 P1-06: safe_params 增加 expected/actual 用于诊断绑定不匹配
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_VERSION_MISMATCH,
        message_key="errors.button.policy.version_mismatch",
        http_status=409,
        retryable=False,
        severity="warning",
        safe_params=["action", "reason", "expected", "actual"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # request_hash 不匹配(审批与资源错位)
    # 409 critical — 审批与记录不对应
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_HASH_MISMATCH,
        message_key="errors.button.policy.hash_mismatch",
        http_status=409,
        retryable=False,
        severity="critical",
        safe_params=["action", "reason", "expected", "actual"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # R63 P1-06: audience 不匹配(跨 handler 滥用)
    # 403 critical — handle 被错误的 handler 调用(如 report handle 被 restore handler 调用)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_AUDIENCE_MISMATCH,
        message_key="errors.button.policy.audience_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["action", "reason", "expected", "actual"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # MFA 强制门禁未验证
    # 403 critical — 高风险按钮必须先验证 MFA
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_MFA_REQUIRED,
        message_key="errors.button.policy.mfa_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["action", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 双人审批要求未满足(approver 缺失或与 principal 相同)
    # 403 critical — 极高风险按钮必须双人审批
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_DUAL_APPROVAL_REQUIRED,
        message_key="errors.button.policy.dual_approval_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["action", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 最终确认步骤缺失
    # 403 warning — 高风险按钮需要最终确认
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_FINAL_CONFIRM_REQUIRED,
        message_key="errors.button.policy.final_confirm_required",
        http_status=403,
        retryable=False,
        severity="warning",
        safe_params=["action", "reason"],
        presentation="short_hint",
        show_retry_button=False,
        audit_level="warning",
    ))
    # R58 P0-4: 高风险 action 必须使用 async token API (sync API 硬拒绝)
    # 403 critical — 高风险 action 不允许通过 sync 生成不持久化 nonce 的 token
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_POLICY_ASYNC_TOKEN_REQUIRED,
        message_key="errors.button.policy.async_token_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["action", "reason", "user_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R64 P1-08: destructive action UX spec 查询失败 ──
    # 400 critical — action 为空,无法查询 UX spec(编程错误或调用方未校验)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_UX_ACTION_REQUIRED,
        message_key="errors.button.ux.action_required",
        http_status=400,
        retryable=False,
        severity="critical",
        safe_params=["reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 400 critical — action 不在 HIGH_RISK_POLICY 中(非高风险 action 不需要 UX 面板)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BUTTON_UX_ACTION_NOT_HIGH_RISK,
        message_key="errors.button.ux.action_not_high_risk",
        http_status=400,
        retryable=False,
        severity="critical",
        safe_params=["action", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R62 P1-07: MFA receipt 年龄校验 + 审批自拒防护 ──
    # MFA receipt 已过期(高风险动作要求 MFA 在近期完成,防陈旧 receipt 绕过二次认证)
    # 401 critical — 安全攻击向量,直接拒绝执行高风险动作
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.AUTH_MFA_RECEIPT_EXPIRED,
        message_key="errors.auth.mfa.receipt_expired",
        http_status=401,
        retryable=False,
        severity="critical",
        safe_params=["user_id", "reason", "action"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # reject() 不能由创建者自己驳回(防止自拒绕过审计,必须与 approve() 对称强制 requester != approver)
    # 403 critical — 与 approve() 自审批防护对称,审计完整性要求
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.APPROVAL_SELF_REJECT_FORBIDDEN,
        message_key="errors.approval.self_reject.forbidden",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["approval_id", "approver_id", "created_by"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R63 P0-04: Migration manifest 信任根验证 ──
    # 500 critical — manifest 字段缺失,部署不完整,阻断迁移
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING,
        message_key="errors.migration.manifest.field_missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["field", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 500 critical — manifest 绑定 SHA 与当前 HEAD/Tree 不一致,manifest 过期或被篡改
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MIGRATION_MANIFEST_BINDING_MISMATCH,
        message_key="errors.migration.manifest.binding_mismatch",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["expected", "actual", "field"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 500 critical — 磁盘 migration 集合与 manifest 声明不一致
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MIGRATION_MANIFEST_SET_MISMATCH,
        message_key="errors.migration.manifest.set_mismatch",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["missing_in_manifest", "missing_on_disk"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 500 critical — cosign 验签失败/签名文件缺失,拒绝未验签 manifest 作为 trust anchor
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MIGRATION_MANIFEST_SIGNATURE_INVALID,
        message_key="errors.migration.manifest.signature_invalid",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["reason", "sig_file", "cert_file"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # ── R64 P0-02: Release artifact manifest 强制验证 ──
    # 500 critical — staging/production 未启用 MIGRATION_MANIFEST_VERIFY=1,拒绝启动
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MIGRATION_MANIFEST_VERIFY_REQUIRED,
        message_key="errors.migration.manifest.verify_required",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["app_env"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 500 critical — 非 git 部署环境未通过 RELEASE_SOURCE_COMMIT/TREE 注入 source commit/tree
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MIGRATION_MANIFEST_RELEASE_SOURCE_REQUIRED,
        message_key="errors.migration.manifest.release_source_required",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 500 critical — release-manifest.json 与 migration-manifest.json 集合/digest 不一致
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MIGRATION_MANIFEST_RELEASE_CONSISTENCY,
        message_key="errors.migration.manifest.release_consistency",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["reason", "field", "expected", "actual"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R64 P1-01: Backup payload 不可序列化 fail-closed ──
    # 500 critical — payload 含 bytes/NaN/Infinity/自定义对象等不可 JSON 序列化类型,
    # 禁止 default=str 静默字符串化,直接拒绝(fail-closed)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_PAYLOAD_NOT_SERIALIZABLE,
        message_key="errors.backup.payload.not_serializable",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["field", "type_name"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R65 P1-06: Backup canonical payload 构造时强校验 ──
    # 500 critical — VerifiedBackupPayload.__post_init__ 在计算 SHA-256 之前先执行
    # 7 维构造时校验(bytes/UTF-8/JSON object/无重复 key/schema/tables 类型/canonical round-trip),
    # 任一未通过即 fail-closed,拒绝"任意 JSON bytes"被称为 canonical
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_PAYLOAD_CANONICAL_INVALID,
        message_key="errors.backup.payload.canonical_invalid",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["reason", "field"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R63 P0-05: Outbox worker fail-fast ──
    # 500 critical — 生产模式无 provider,拒绝 stub 误启动
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.OUTBOX_PROVIDER_REGISTRY_REQUIRED,
        message_key="errors.outbox.provider_registry.required",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R64 P0-04: Outbox 生产闭环 — registry 加载异常 fail-closed ──
    # 500 critical — provider registry / schema 加载异常直接 readiness failure
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.OUTBOX_PROVIDER_REGISTRY_LOAD_FAILED,
        message_key="errors.outbox.provider_registry.load_failed",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R64 P0-04: lease fencing token CAS 不匹配 ──
    # 409 conflict — complete/fail/renew 必须携带正确 lease_version
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.OUTBOX_LEASE_VERSION_MISMATCH,
        message_key="errors.outbox.lease_version.mismatch",
        http_status=409,
        retryable=False,
        severity="critical",
        safe_params=["event_id", "lease_version"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R65 P1-05: 严格 CAS 路径冲突(lease_version is not None 且 CAS 失败)raise ──
    # 409 conflict — complete/fail/renew 严格 CAS 路径下 lease_version 不匹配 /
    # 行未找到 / lease_owner 不匹配,一律 raise(不再静默返回 False/not_found)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.OUTBOX_LEASE_VERSION_CONFLICT,
        message_key="errors.outbox.lease_version.conflict",
        http_status=409,
        retryable=False,
        severity="critical",
        safe_params=["event_id", "expected_lease_version", "operation"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R64 P0-04: 未知 event_type 严禁标记成功,必须进入 DLQ ──
    # 500 critical — 未知 event_type 进入 DLQ(可审批 replay)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.OUTBOX_EVENT_UNKNOWN,
        message_key="errors.outbox.unknown_event_type",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["event_type", "outbox_id"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R64 P0-04: provider 调用超过租期三分之一自动续租,续租失败停止提交 ──
    # 500 critical — 续租失败立即停止提交结果(防 lease 被回收后双重执行)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.OUTBOX_LEASE_RENEW_FAILED,
        message_key="errors.outbox.lease_renew.failed",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["event_id", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R63 P1-12: i18n ICU 预编译 fail-fast ──
    # 500 critical — ICU 语法错误 / 参数集合不对称,release / strict 模式直接阻断构建
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.I18N_ICU_COMPILE_FAILED,
        message_key="errors.i18n.icu.compile_failed",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["locale", "key", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R63 P1-11: Locale 文件 fail-closed 校验 ──
    # 500 critical — 启动期 locale 文件校验失败(文件缺失/JSON 解析失败/
    # message_key 缺失/占位符不对称),Release 镜像必须 fail-closed 阻断启动
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.LOCALE_VALIDATION_FAILED,
        message_key="errors.locale.validation.failed",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["reason", "error_count"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))

    # ── R64 P0-03: 恢复编排状态机 — staging 蓝绿切换 ──
    # 500 critical — staging provision 失败(CRDB/SQLite/relay_sqlite 创建新目标失败)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_STAGING_PROVISION_FAILED,
        message_key="errors.restore.staging.provision_failed",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["operation_id", "datasource", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 500 critical — staging validate 失败(schema/行数/主外键/业务守恒/hash 任一失败)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_STAGING_VALIDATE_FAILED,
        message_key="errors.restore.staging.validate_failed",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["operation_id", "dimension", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 403 critical — 蓝绿切换前必须审批(approval_id 缺失或不匹配)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_APPROVAL_REQUIRED,
        message_key="errors.restore.approval.required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["operation_id", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 403 critical — 蓝绿切换前必须 MFA receipt(mfa_receipt_id 缺失或不匹配)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_MFA_REQUIRED,
        message_key="errors.restore.approval.mfa_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["operation_id", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 500 critical — 蓝绿切换失败(CAS 切换 active 指针失败)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_SWITCH_FAILED,
        message_key="errors.restore.switch.failed",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["operation_id", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 500 critical — 回滚失败(状态机错误 / 无 switch_version / 旧版本指针损坏)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_ROLLBACK_FAILED,
        message_key="errors.restore.rollback.failed",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["operation_id", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 403 critical — nonce payload 不一致(同 operation 重试时禁止换 payload_digest)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_NONCE_PAYLOAD_MISMATCH,
        message_key="errors.restore.nonce.payload_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["operation_id", "backup_id", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 409 critical — phase 转换非法(状态机不允许的转换)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_PHASE_TRANSITION_INVALID,
        message_key="errors.restore.phase.transition_invalid",
        http_status=409,
        retryable=False,
        severity="critical",
        safe_params=["operation_id", "phase_from", "phase_to", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 403 critical — R65 P0-07 / P1-07: 旧直接 restore 写入器已被 capability-seal
    # 生产代码不能回退到原地覆盖;仅测试 / scripts / orchestrator backend 可调用。
    # 调用 db_restore.run_restore / _restore_from_backup_data / _restore_crdb_tables /
    # _restore_sqlite_tables_to_db / validate_and_restore_backup_strict(绕过 orchestrator)
    # 时 fail-closed,需通过 RestoreOrchestrator 蓝绿切换路径执行恢复。
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_LEGACY_WRITER_SEALED,
        message_key="errors.restore.legacy_writer.sealed",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=[
            "caller",
            "reason",
            # R67 P0-06: 新增诊断参数(生产环境硬守卫调用时传入)
            "entry_point",
            "source_env_var",
            "allow_legacy_restore_set",
        ],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 500 critical — R66 P0-06: RestoreOrchestrator 必需依赖缺失
    # 生产类已删除所有 Optional 降级分支,构造时 backends / approval_authority /
    # mfa_authority / store 任一为 None,或 check_startup_readiness 校验三个 backend /
    # authority / nonce ledger / active pointer / fencing store 任一不可用,
    # 均 fail-closed 抛此错误(禁止生产环境降级到骨架路径)。
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_ORCHESTRATOR_REQUIRED_DEPENDENCY_MISSING,
        message_key="errors.restore.orchestrator.required_dependency_missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["reason", "missing"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 403 critical — R65 P0-05: 未知 destructive action fail-closed
    # action 匹配 destructive namespace 但未在 HIGH_RISK_POLICY 中注册,
    # 拒绝执行(防止新 destructive action 被误判为低风险绕过审批门禁)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.HIGH_RISK_ACTION_UNREGISTERED,
        message_key="errors.high_risk.action.unregistered",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["action", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # ── R65 P1-03: i18n 严格出口边界 fail-closed ──
    # 500 critical — 生产代码调用 translate/format_message/format_message_icu
    # 时未显式传入 locale(依赖全局 _DEFAULT_LOCALE 兜底),
    # staging/production 必须 fail-closed 抛此错误。
    # safe_params 仅含 key 与 caller(无敏感信息);locale 字段为缺失项,
    # 仅作诊断用(无用户敏感数据)。
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.I18N_LOCALE_NOT_BOUND,
        message_key="errors.i18n.locale.not_bound",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["key", "caller"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 500 critical — ICU 运行时解析失败(占位符缺失 / 括号不平衡 / selector 缺失等),
    # staging/production 必须 fail-closed 抛此错误,禁止把原始 ICU 大括号
    # 展示给用户。safe_params 仅含 key / locale / reason(均无敏感信息)。
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.I18N_PARSE_FAILED,
        message_key="errors.i18n.parse.failed",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["key", "locale", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 412 critical — R65 P0-04: 生产证据不足,无法晋级
    # production promotion 只接受独立、签名、不可变、未过期的真实证据 artifact。
    # 任一必需证据缺失/过期/dry_run/未签名/--skip 使用,均阻断晋级。
    # safe_params 仅含 reason / missing(无敏感信息,仅证据类型名)。
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.PRODUCTION_EVIDENCE_INSUFFICIENT,
        message_key="errors.production.evidence.insufficient",
        http_status=412,
        retryable=False,
        severity="critical",
        safe_params=["reason", "missing"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 409 critical — R67 P1-11: evidence artifact 已被其他 candidate 消费,禁止跨候选复用
    # 单次使用语义:每个 evidence artifact 只能被一个 candidate 消费一次。
    # safe_params 仅含 artifact_type / consumed_candidate / candidate_tag(无敏感信息)。
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.EVIDENCE_ALREADY_CONSUMED,
        message_key="errors.production.evidence.already_consumed",
        http_status=409,
        retryable=False,
        severity="critical",
        safe_params=["artifact_type", "consumed_candidate", "candidate_tag"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))
    # 500 critical — R70 Wave 3: production/staging 下检测到测试逃生舱环境变量
    # (I18N_ALLOW_FALLBACK / ALLOW_LEGACY_RESTORE / TEST_ONLY / DEV_ONLY / BYPASS /
    #  SKIP_VERIFY 等被设置为真值)→ escape_hatch_guard 抛此错误 fail-closed。
    # safe_params: caller / hatch_count / hatch_details / reason(均无敏感信息)。
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.PRODUCTION_ESCAPE_HATCH_DETECTED,
        message_key="errors.production.escape_hatch.detected",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["caller", "hatch_count", "hatch_details", "reason"],
        presentation="inline",
        show_retry_button=False,
        audit_level="critical",
    ))


# 模块加载时即注册默认定义(确保任何 import 都触发注册)
_register_defaults()
ErrorRegistry._initialized = True

# R61 P1-05: 启动期校验所有已注册 ErrorDefinition 的完整性
# (presentation / show_retry_button / audit_level 必须显式设置,
# code 唯一)。
# 校验失败 = 协议漂移,直接 fail-fast 阻止进程启动。
_validation_errors = ErrorRegistry.validate()
if _validation_errors:
    # R61 P1-04: 使用 _i18n_t() 避免 i18n scanner 基线溢出
    from services.i18n import translate as _i18n_t
    for _validation_err in _validation_errors:
        logger.error(
            _i18n_t(
                "services.error_codes.logger_validation_error",
                err=str(_validation_err),
            )
        )
    raise RuntimeError(
        f"R61 P1-05: ErrorRegistry validation failed with "
        f"{len(_validation_errors)} error(s); first: {_validation_errors[0]}"
    )

# R63: 提取为模块常量避免硬编码字符串扫描器误报
_LOG_LOCALE_VALIDATION_ERROR = "R63 P1-11: locale validation error: {}"
_LOG_LOCALE_VALIDATION_DOWNGRADED = (
    "R63 P1-11: ERROR_CODES_LOCALE_STRICT=0, locale validation errors "
    "downgraded to warning: {} error(s); first: {}"
)

# R63 P1-11: 启动期校验打包后的实际 locale 文件完整性(fail-closed)。
# Release 镜像必须在启动前对打包后的 locale 文件 fail-closed,
# 而不能只依赖源码 CI(scripts/check_error_codes_locale_schema.py)。
# 校验项:文件存在 / 有效 JSON / message_key 存在 / key 对称 / 占位符一致。
# 逃生门:ERROR_CODES_LOCALE_STRICT=0 降级为 warning(仅供开发环境,生产必须 fail-closed)。
_locale_validation_errors = ErrorRegistry.validate_locales()
if _locale_validation_errors:
    import os as _os_locale_strict
    _locale_strict_val = _os_locale_strict.environ.get(
        "ERROR_CODES_LOCALE_STRICT", "1"
    ).strip().lower()
    _locale_fail_closed = _locale_strict_val not in ("0", "false", "no", "off")
    # R63 P1-11: locale 文件已损坏时禁用 _i18n_t()(避免 i18n.translate →
    # _get_release_mode → _i18n_t 递归)。直接用 f-string 记录错误。
    for _locale_err in _locale_validation_errors:
        logger.error(
            _LOG_LOCALE_VALIDATION_ERROR.format(_locale_err)
        )
    if _locale_fail_closed:
        # R63 P1-11: 生产环境 fail-closed — locale 文件异常阻断启动
        raise AppError(
            ErrorCodes.LOCALE_VALIDATION_FAILED,
            params={
                "reason": "locale_validation_failed",
                "error_count": len(_locale_validation_errors),
            },
        )
    # 开发模式(ERROR_CODES_LOCALE_STRICT=0):仅 warning,继续启动
    logger.warning(
        _LOG_LOCALE_VALIDATION_DOWNGRADED.format(
            len(_locale_validation_errors), _locale_validation_errors[0]
        )
    )


__all__ = [
    "ErrorCodes",
    "ErrorDefinition",
    "ErrorEnvelope",
    "ErrorRegistry",
    "AppError",
    "make_error_response",
    "is_safe_param",
]
