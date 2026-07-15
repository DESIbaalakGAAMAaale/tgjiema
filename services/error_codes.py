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
"""
from __future__ import annotations

import datetime as _dt
import json
import uuid
from dataclasses import dataclass, field
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
    AUTH_SESSION_EXPIRED = "AUTH.SESSION.EXPIRED"

    # BACKUP
    BACKUP_RESTORE_APPROVAL_INVALID = "BACKUP.RESTORE.APPROVAL_INVALID"
    BACKUP_RESTORE_CHECKSUM_MISMATCH = "BACKUP.RESTORE.CHECKSUM_MISMATCH"

    # EFFECT_RECEIPT
    EFFECT_RECEIPT_MANAGER_UNAVAILABLE = "EFFECT.RECEIPT.MANAGER_UNAVAILABLE"
    EFFECT_RECEIPT_DB_ERROR = "EFFECT.RECEIPT.DB_ERROR"

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
    """
    code: str
    message_key: str
    http_status: int
    retryable: bool
    severity: str
    safe_params: list[str] = field(default_factory=list)


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
        """获取 ErrorDefinition。未注册时 fallback 到 ERROR_INTERNAL。"""
        cls._ensure_initialized()
        definition = cls._definitions.get(code)
        if definition is None:
            logger.warning(
                f"[ErrorRegistry] 未注册的错误码 fallback 到 ERROR_INTERNAL: {code}"
            )
            return cls._definitions[ErrorCodes.ERROR_INTERNAL]
        return definition

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

    @classmethod
    def _ensure_initialized(cls) -> None:
        """确保默认 ErrorDefinition 已注册(幂等)。"""
        if not cls._initialized:
            _register_defaults()
            cls._initialized = True


# ════════════════════════════════════════════════════════════════
# 5. AppError 异常类
# ════════════════════════════════════════════════════════════════
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
        """返回 dict 表示(供 JSON 序列化)。"""
        return self.envelope.to_dict()

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
            return False


# ════════════════════════════════════════════════════════════════
# 6. 辅助函数
# ════════════════════════════════════════════════════════════════
def _filter_safe_params(params: dict, safe_params: list[str]) -> dict:
    """按 safe_params 白名单过滤 params(避免敏感信息泄露)。

    Args:
        params: 原始参数字典
        safe_params: 可安全记录的参数名列表

    Returns:
        过滤后的 dict(仅包含 safe_params 列入的字段)
    """
    if not params or not safe_params:
        return {}
    return {k: v for k, v in params.items() if k in safe_params}


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
    ))

    # ── UPLOAD ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_COPY_TELEGRAM_TIMEOUT,
        message_key="errors.upload.copy.telegram_timeout",
        http_status=504,
        retryable=True,
        severity="warning",
        safe_params=["file_code", "channel_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_COPY_TELEGRAM_FORBIDDEN,
        message_key="errors.upload.copy.telegram_forbidden",
        http_status=403,
        retryable=False,
        severity="error",
        safe_params=["file_code", "channel_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_MANIFEST_OUTBOX_FAILED,
        message_key="errors.upload.manifest.outbox_failed",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["file_code", "batch_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_PENDING_OUTBOX_TX_FAILED,
        message_key="errors.upload.pending.outbox_tx_failed",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["file_code", "batch_id"],
    ))

    # ── INDEX ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.INDEX_FINALIZE_OUTBOX_FAILED,
        message_key="errors.index.finalize.outbox_failed",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["file_code", "code"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.INDEX_CODE_CONFLICT,
        message_key="errors.index.code.conflict",
        http_status=409,
        retryable=False,
        severity="warning",
        safe_params=["code"],
    ))

    # ── DELIVERY ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DELIVERY_SEND_FLOOD_WAIT,
        message_key="errors.delivery.send.flood_wait",
        http_status=429,
        retryable=True,
        severity="warning",
        safe_params=["wait_seconds", "user_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DELIVERY_SEND_FORBIDDEN,
        message_key="errors.delivery.send.forbidden",
        http_status=403,
        retryable=False,
        severity="error",
        safe_params=["user_id", "channel_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DELIVERY_RECEIPT_FAILED,
        message_key="errors.delivery.receipt.failed",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["user_id", "file_code"],
    ))

    # ── AUTH ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.AUTH_MFA_REPLAYED,
        message_key="errors.auth.mfa.replayed",
        http_status=401,
        retryable=False,
        severity="critical",
        safe_params=["user_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.AUTH_MFA_LOCKED,
        message_key="errors.auth.mfa.locked",
        http_status=423,
        retryable=False,
        severity="error",
        safe_params=["user_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.AUTH_SESSION_EXPIRED,
        message_key="errors.auth.session.expired",
        http_status=401,
        retryable=False,
        severity="info",
        safe_params=["user_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.AUTH_RBAC_PERMISSION_DENIED,
        message_key="errors.auth.rbac.permission_denied",
        http_status=403,
        retryable=False,
        severity="warning",
        safe_params=["user_id", "permission"],
    ))

    # ── BACKUP ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_RESTORE_APPROVAL_INVALID,
        message_key="errors.backup.restore.approval_invalid",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["approval_id", "backup_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_RESTORE_CHECKSUM_MISMATCH,
        message_key="errors.backup.restore.checksum_mismatch",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["backup_id"],
    ))

    # ── EFFECT_RECEIPT ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.EFFECT_RECEIPT_MANAGER_UNAVAILABLE,
        message_key="errors.effect.receipt.manager_unavailable",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["action_id", "effect_type"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.EFFECT_RECEIPT_DB_ERROR,
        message_key="errors.effect.receipt.db_error",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["action_id", "effect_type"],
    ))

    # ── APPROVAL ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.APPROVAL_REQUIRED,
        message_key="errors.approval.gate.required",
        http_status=202,
        retryable=False,
        severity="info",
        safe_params=["approval_id", "action"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.APPROVAL_STATE_INVALID,
        message_key="errors.approval.state.invalid",
        http_status=409,
        retryable=False,
        severity="warning",
        safe_params=["approval_id", "current_status"],
    ))

    # ── COMMAND ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COMMAND_CONCURRENT_CLAIM,
        message_key="errors.command.concurrent.claim",
        http_status=409,
        retryable=True,
        severity="warning",
        safe_params=["action_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COMMAND_HASH_MISMATCH,
        message_key="errors.command.hash.mismatch",
        http_status=400,
        retryable=False,
        severity="critical",
        safe_params=["action_id"],
    ))

    # ── DB ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DB_CACHE_UNAVAILABLE,
        message_key="errors.db.cache.unavailable",
        http_status=503,
        retryable=True,
        severity="critical",
        safe_params=["component"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DB_REDIS_UNAVAILABLE,
        message_key="errors.db.redis.unavailable",
        http_status=503,
        retryable=True,
        severity="error",
        safe_params=["component"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DB_CRDB_UNAVAILABLE,
        message_key="errors.db.crdb.unavailable",
        http_status=503,
        retryable=True,
        severity="error",
        safe_params=["component"],
    ))

    # ── 业务 ──
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.QUOTA_EXCEEDED,
        message_key="errors.quota.decode.exceeded",
        http_status=429,
        retryable=False,
        severity="info",
        safe_params=["user_id", "quota"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.FILE_NOT_FOUND,
        message_key="errors.file.lookup.not_found",
        http_status=404,
        retryable=False,
        severity="info",
        safe_params=["file_code"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.FILE_EXPIRED,
        message_key="errors.file.lookup.expired",
        http_status=410,
        retryable=False,
        severity="info",
        safe_params=["file_code"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.FILE_CODE_INVALID,
        message_key="errors.file.code.invalid",
        http_status=400,
        retryable=False,
        severity="info",
        safe_params=["code"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.USER_BANNED,
        message_key="errors.user.state.banned",
        http_status=403,
        retryable=False,
        severity="info",
        safe_params=["user_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.SYSTEM_MAINTENANCE,
        message_key="errors.system.state.maintenance",
        http_status=503,
        retryable=True,
        severity="warning",
        safe_params=["reason"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.SYSTEM_RATE_LIMITED,
        message_key="errors.system.rate.limited",
        http_status=429,
        retryable=True,
        severity="warning",
        safe_params=["user_id"],
    ))
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.VALIDATION_FAILED,
        message_key="errors.validation.input.failed",
        http_status=400,
        retryable=False,
        severity="info",
        safe_params=["field"],
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
    ))
    # topology.yaml 中没有槽位配置(admin/seed_topology.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.TOPOLOGY_LOAD_NO_SLOTS,
        message_key="errors.topology.load.no_slots",
        http_status=500,
        retryable=False,
        severity="error",
        safe_params=[],
    ))
    # 索引生成码时数据库未初始化(bots/idx_bot.py:_generate_unique_code_with_retry)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.INDEX_GENERATE_DB_UNINITIALIZED,
        message_key="errors.index.generate.db_uninitialized",
        http_status=503,
        retryable=True,
        severity="critical",
        safe_params=["action"],
    ))
    # 索引 finalize_upload 时数据库未初始化(bots/idx_bot.py:finalize_upload)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.INDEX_FINALIZE_DB_UNINITIALIZED,
        message_key="errors.index.finalize.db_uninitialized",
        http_status=503,
        retryable=True,
        severity="critical",
        safe_params=["action"],
    ))
    # 无可用存储频道(bots/idx_bot.py:_get_storage_channel_id)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.INDEX_STORAGE_NO_CHANNEL,
        message_key="errors.index.storage.no_channel",
        http_status=503,
        retryable=True,
        severity="error",
        safe_params=[],
    ))
    # MON_BOT_TOKEN 未配置(bots/mon_bot.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BOT_MON_TOKEN_MISSING,
        message_key="errors.bot.mon.token_missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=[],
    ))
    # _bot 全局引用未初始化(bots/up_bot.py:_outbox_archive_to_r100_strict)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_OUTBOX_BOT_UNINITIALIZED,
        message_key="errors.upload.outbox.bot_uninitialized",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=[],
    ))
    # 无可用活跃槽位(bots/up_bot.py:_get_upload_target_channel)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.UPLOAD_SLOT_NONE_ACTIVE,
        message_key="errors.upload.slot.none_active",
        http_status=503,
        retryable=True,
        severity="error",
        safe_params=[],
    ))
    # cryptography 未安装(services/backup_crypto.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_DECRYPT_DEP_MISSING,
        message_key="errors.backup.decrypt.dep_missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["dep_name"],
    ))
    # BACKUP_KEK 未配置(services/backup_crypto.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_DECRYPT_KEK_MISSING,
        message_key="errors.backup.decrypt.kek_missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=[],
    ))
    # R2 凭证未配置(services/db_backup.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_RESTORE_R2_CREDENTIAL_MISSING,
        message_key="errors.backup.restore.r2_credential_missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=[],
    ))
    # 中继账号验证码获取失败(services/relay_instance.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RELAY_AUTH_CODE_FAILED,
        message_key="errors.relay.auth.code_failed",
        http_status=401,
        retryable=True,
        severity="warning",
        safe_params=["phone"],
    ))
    # 中继账号二步验证密码获取超时(services/relay_instance.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RELAY_AUTH_PASSWORD_TIMEOUT,
        message_key="errors.relay.auth.password_timeout",
        http_status=401,
        retryable=True,
        severity="warning",
        safe_params=["phone"],
    ))
    # 中继账号 api_hash 校验失败(services/relay_pool.py)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RELAY_CONFIG_API_HASH_INVALID,
        message_key="errors.relay.config.api_hash_invalid",
        http_status=400,
        retryable=False,
        severity="info",
        safe_params=[],
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
    ))
    # admin bootstrap 未完成,Web 进程应退出
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ADMIN_BOOTSTRAP_NOT_VERIFIED,
        message_key="errors.admin.bootstrap.not_verified",
        http_status=503,
        retryable=False,
        severity="critical",
        safe_params=[],
    ))
    # production 环境必须配置 BOT_TOKEN
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.PRODUCTION_BOT_TOKEN_MISSING,
        message_key="errors.production.bot_token.missing",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["environment"],
    ))
    # 灾备恢复必须传 approval_action_id
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.BACKUP_RESTORE_APPROVAL_ACTION_ID_REQUIRED,
        message_key="errors.backup.restore.approval_action_id_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["backup_id"],
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
    ))
    # notification_outbox 重复插入(dedup_key + window 唯一约束冲突)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.NOTIFICATION_OUTBOX_DUPLICATE,
        message_key="errors.notification.outbox.duplicate",
        http_status=409,
        retryable=False,
        severity="info",
        safe_params=["user_id", "dedup_key"],
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
    ))
    # production 恢复 expected_request_hash 与存储 hash 不匹配
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.PRODUCTION_RESTORE_HASH_MISMATCH,
        message_key="errors.production.restore.hash_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["backup_id", "approval_action_id"],
    ))
    # command_executions 已 executed,禁止重复执行 restore
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.RESTORE_ALREADY_EXECUTED,
        message_key="errors.restore.execute.already_executed",
        http_status=409,
        retryable=False,
        severity="error",
        safe_params=["approval_action_id"],
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
    ))
    # 内容申诉状态无效(如重复审批 / 已在 restore_pending 等待 executor)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.CONTENT_APPEAL_INVALID_STATE,
        message_key="errors.content.appeal.invalid_state",
        http_status=409,
        retryable=False,
        severity="warning",
        safe_params=["appeal_id", "current_status"],
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
    ))
    # disable/recover 操作必须绑定 request_hash + principal + approval_action_id
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MAINTENANCE_RECOVER_BINDING_REQUIRED,
        message_key="errors.maintenance.recover.binding_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["approval_action_id", "principal_id"],
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
    ))
    # 指标采集器失败(输出 collector_success=0,不输出 0 伪装健康)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.METRICS_COLLECTOR_FAILED,
        message_key="errors.metrics.collector.failed",
        http_status=503,
        retryable=True,
        severity="warning",
        safe_params=["collector"],
    ))
    # RU 估算值标记(非官方 CockroachDB Cloud Metrics)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.METRICS_RU_ESTIMATED,
        message_key="errors.metrics.ru.estimated",
        http_status=200,
        retryable=False,
        severity="info",
        safe_params=["service"],
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
    ))
    # 删除请求失败(局部失败导致整个请求未 completed)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DATA_LIFECYCLE_DELETE_REQUEST_FAILED,
        message_key="errors.data.lifecycle.delete_request_failed",
        http_status=500,
        retryable=False,
        severity="error",
        safe_params=["user_id", "request_id", "failed_steps"],
    ))
    # 物理删除前验证 backup marker 失败(无备份标记)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DATA_LIFECYCLE_BACKUP_MARKER_MISSING,
        message_key="errors.data.lifecycle.backup_marker_missing",
        http_status=409,
        retryable=False,
        severity="critical",
        safe_params=["user_id", "table_name", "pk"],
    ))
    # R53 P1-3: skip_backup_check=True 无 break-glass 审批(只允许审批后绕过)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.DATA_LIFECYCLE_BREAK_GLASS_APPROVAL_REQUIRED,
        message_key="errors.data.lifecycle.break_glass_approval_required",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["reason", "approval_action_id"],
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
    ))
    # set_user_plan 事务失败(套餐/配额/audit/dirty_outbox 任一失败)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ENTITLEMENT_SET_PLAN_TX_FAILED,
        message_key="errors.entitlement.set_plan.tx_failed",
        http_status=500,
        retryable=False,
        severity="error",
        safe_params=["user_id", "plan_name"],
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
    ))
    # 集合 CAS 版本冲突
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_CAS_CONFLICT,
        message_key="errors.collection.cas.conflict",
        http_status=409,
        retryable=True,
        severity="info",
        safe_params=["collection_id", "expected_version", "current_version"],
    ))
    # bypass_cas 必须显式声明并审计
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_CAS_BYPASS_NOT_ALLOWED,
        message_key="errors.collection.cas.bypass_not_allowed",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["collection_id", "caller"],
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
    ))
    # 未知 task status 拒绝(不再静默回退)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.TASK_CENTER_UNKNOWN_STATUS,
        message_key="errors.task.center.unknown_status",
        http_status=400,
        retryable=False,
        severity="warning",
        safe_params=["status"],
    ))
    # 列表查询 DB 异常(返回错误 envelope,不返回空列表伪装"无任务")
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.TASK_CENTER_LIST_DB_ERROR,
        message_key="errors.task.center.list_db_error",
        http_status=500,
        retryable=True,
        severity="error",
        safe_params=["scope"],
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
    ))
    # 审批 hash/owner 校验失败(不仅校验 status,必须校验 request_hash + principal_id)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.REPAIR_CONSOLE_APPROVAL_HASH_MISMATCH,
        message_key="errors.repair.console.approval_hash_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["approval_action_id", "expected_hash", "actual_hash"],
    ))
    # 审批 principal 不匹配(approval_action_id 关联的 principal 与当前 principal 不一致)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.REPAIR_CONSOLE_APPROVAL_PRINCIPAL_MISMATCH,
        message_key="errors.repair.console.approval_principal_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["approval_action_id", "expected_principal", "actual_principal"],
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
    ))
    # command_executions 未处于 approved 状态(执行前必须审批通过)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COMMAND_NOT_APPROVED,
        message_key="errors.command.status.not_approved",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["action_id", "reason", "current_status", "expected_status"],
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
    ))
    # 套餐变更必须通过 CommandBus(禁止直接调用 set_user_plan 进行生产变更)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ENTITLEMENT_PLAN_REQUIRES_COMMAND_BUS,
        message_key="errors.entitlement.plan.requires_command_bus",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["user_id", "plan_name", "caller"],
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
    ))
    # recover_status 持久化失败(触发 critical alert,不允许 fail-open)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.MAINTENANCE_RECOVER_PERSIST_CRITICAL,
        message_key="errors.maintenance.recover.persist_critical",
        http_status=500,
        retryable=False,
        severity="critical",
        safe_params=["reason"],
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
    ))
    # 审批 request_hash 不匹配(防篡改,前 16 字符也不匹配)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_APPROVAL_HASH_MISMATCH,
        message_key="errors.collection.approval.hash_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["collection_id", "approval_action_id"],
    ))
    # 审批 principal_id 不匹配(他人审批,防越权)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_APPROVAL_PRINCIPAL_MISMATCH,
        message_key="errors.collection.approval.principal_mismatch",
        http_status=403,
        retryable=False,
        severity="critical",
        safe_params=["collection_id", "approval_action_id", "expected_principal_id", "actual_principal_id"],
    ))
    # 审批已被执行(状态='executed',禁止重复执行)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.COLLECTION_APPROVAL_ALREADY_EXECUTED,
        message_key="errors.collection.approval.already_executed",
        http_status=409,
        retryable=True,
        severity="warning",
        safe_params=["collection_id", "approval_action_id"],
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
    ))
    # 修改套餐必须提供 expected_version(production 强制 CAS,400 critical)
    ErrorRegistry.register(ErrorDefinition(
        code=ErrorCodes.ENTITLEMENTS_EXPECTED_VERSION_REQUIRED,
        message_key="errors.entitlement.plan.expected_version_required",
        http_status=400,
        retryable=False,
        severity="critical",
        safe_params=["user_id", "plan_name", "environment"],
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
    ))


# 模块加载时即注册默认定义(确保任何 import 都触发注册)
_register_defaults()
ErrorRegistry._initialized = True


__all__ = [
    "ErrorCodes",
    "ErrorDefinition",
    "ErrorEnvelope",
    "ErrorRegistry",
    "AppError",
]
