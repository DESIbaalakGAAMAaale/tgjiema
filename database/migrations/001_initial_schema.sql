-- R59 P1: SQLite 版本化迁移 — 初始 schema
--
-- 本文件创建 command_approvals 表(完整 R58 P0-2 schema),替换以下位置的惰性 DDL:
--   - services/data_lifecycle.py:_ensure_command_approvals_table()
--   - database/redis_queue.py:quarantine_repair() 内联 CREATE TABLE
--
-- 表结构说明(R58 P0-2 强绑定字段):
--   id              自增主键
--   action_id       关联 command_executions.action_id
--   approver_id     审批人 principal_id
--   approval_type   审批类型(break_glass / quarantine_delete)
--   decision        审批决定(approved/rejected),记录存在 ≠ 批准,必须显式 approved
--   request_hash    请求哈希(64 hex),两人必须批准同一请求,防参数错位
--   mfa_receipt     MFA 凭证(强制非空,绑定 MFA receipt)
--   permission      RBAC 权限快照(执行时再授权)
--   approved_at     审批时间
--   expires_at      过期时间(旧审批不可无限复用)
--   consumed_at     消费时间(CAS 消费,防重用)
--   revoked_at      撤销时间(显式撤销)
--   metadata_json   额外元数据
--
-- 一个 action_id 可有多条记录(多人审批),UNIQUE(action_id, approver_id) 防重复审批。
--
-- 幂等性: CREATE TABLE IF NOT EXISTS 保证重复执行无副作用。

CREATE TABLE IF NOT EXISTS command_approvals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id       TEXT NOT NULL,
    approver_id     BIGINT NOT NULL,
    approval_type   TEXT NOT NULL,
    decision        TEXT NOT NULL DEFAULT 'approved',
    request_hash    TEXT NOT NULL DEFAULT '',
    mfa_receipt     TEXT,
    permission      TEXT NOT NULL DEFAULT '',
    approved_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL DEFAULT '',
    consumed_at     TEXT,
    revoked_at      TEXT,
    metadata_json   TEXT,
    UNIQUE(action_id, approver_id)
);

-- action_id 查询索引(按 action_id 查询审批记录)
CREATE INDEX IF NOT EXISTS idx_command_approvals_action_id
ON command_approvals(action_id);
