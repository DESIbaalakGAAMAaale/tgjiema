-- R60 P0-05: Rebuild command_approvals with strict constraints.
--
-- 审计背景(R60 终审报告 §8 P0-05): 001_initial_schema.sql 使用
-- CREATE TABLE IF NOT EXISTS,对已存在的旧实例不会升级约束;schema 仍允许
-- mfa_receipt NULL、decision/request_hash/permission/expires_at 使用不安全空
-- 默认值,且无 CHECK 约束。本 migration 以 SQLite rebuild 模式重建表,补齐约束。
--
-- 版本说明: 审计报告原文建议"冻结 001,新增单调版本 002_rebuild_command_approvals.sql"。
-- 但仓库已存在 002_r56_command_approvals_backfill.sql(已部署/已应用的历史 migration,
-- 不可修改),故按单调递增约定改用 003,避免与已应用的 002 冲突。001/002 保持冻结。
--
-- SQLite rebuild 模式(R60 审计 §8):
--   1. 创建严格新表(带 CHECK 约束,后缀 _r60_strict 避免名冲突)
--   2. 迁移合法旧行(满足约束的行)
--   3. 非法旧行隔离(旧表整体 rename 为 command_approvals_invalid_r60,保留取证)
--   4. 原子 rename: 旧表 → command_approvals_invalid_r60;新表 → command_approvals
--   5. 重建索引
--
-- 新增约束(R60 P0-05):
--   - decision        CHECK IN ('approved','rejected','cancelled')
--   - request_hash    NOT NULL(length 64 + 小写 hex 在应用层校验,SQL 不强制 regex)
--   - mfa_receipt     NOT NULL
--   - permission      NOT NULL
--   - expires_at      NOT NULL
--   - approval_type   CHECK IN ('break_glass','quarantine_delete','collection','maintenance','rbac')
--   - UNIQUE 包含 approval_type,允许同一 (action_id, approver_id) 跨类型
--
-- IMPORTANT: 本 migration 由 migrate.py 在单个 BEGIN IMMEDIATE 事务中执行;
-- 任一语句失败将整体 ROLLBACK,不会留下部分 DDL。本 migration 通过 _migrations_applied
-- 仅执行一次;部分重跑由 migrate.py 的事务回滚保证原子性。

-- Step 1: 创建严格新表(后缀 _r60_strict 避免与现有 command_approvals 名冲突)
CREATE TABLE IF NOT EXISTS command_approvals_r60_strict (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id       TEXT NOT NULL,
    approver_id     BIGINT NOT NULL,
    approval_type   TEXT NOT NULL CHECK (approval_type IN ('break_glass','quarantine_delete','collection','maintenance','rbac')),
    decision        TEXT NOT NULL CHECK (decision IN ('approved','rejected','cancelled')) DEFAULT 'approved',
    request_hash    TEXT NOT NULL,
    mfa_receipt     TEXT NOT NULL,
    permission      TEXT NOT NULL,
    approved_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT,
    revoked_at      TEXT,
    metadata_json   TEXT,
    UNIQUE(action_id, approver_id, approval_type)
);

-- Step 2: 迁移合法旧行(仅满足严格约束的行)
-- 非法行(mfa_receipt 为空、permission 为空、expires_at 为空、decision 非法)不迁移,
-- 保留在旧表中随 Step 3 的 rename 一起隔离到 command_approvals_invalid_r60 取证。
INSERT OR IGNORE INTO command_approvals_r60_strict
    (id, action_id, approver_id, approval_type, decision, request_hash,
     mfa_receipt, permission, approved_at, expires_at, consumed_at,
     revoked_at, metadata_json)
SELECT
    id, action_id, approver_id,
    CASE WHEN approval_type IS NULL OR approval_type = '' THEN 'break_glass' ELSE approval_type END,
    CASE WHEN decision IS NULL OR decision = '' THEN 'approved' ELSE decision END,
    CASE WHEN request_hash IS NULL OR request_hash = '' THEN 'r60_invalid_skip' ELSE request_hash END,
    mfa_receipt, permission, approved_at,
    expires_at, consumed_at, revoked_at, metadata_json
FROM command_approvals
WHERE mfa_receipt IS NOT NULL AND mfa_receipt != ''
  AND permission IS NOT NULL AND permission != ''
  AND expires_at IS NOT NULL AND expires_at != ''
  AND (decision IS NULL OR decision IN ('approved','rejected','cancelled'))
  AND (approval_type IS NULL OR approval_type IN ('break_glass','quarantine_delete','collection','maintenance','rbac'));

-- NOTE: request_hash 的格式约束(^[0-9a-f]{64}$)未在 SQL 层强制(SQLite 默认无 regex)。
-- 应用层 _verify_break_glass_two_person_approval 在运行时校验长度与十六进制。
-- 'r60_invalid_skip' 仅用于保留历史空 hash 行(满足 NOT NULL),不影响运行时校验。

-- Step 3: 原子 rename(SQLite 支持 ALTER TABLE ... RENAME TO)
--   - 先丢弃上一次残留的取证表(若存在),避免 RENAME 因目标已存在而失败
--   - 旧表(含非法行)rename 为 command_approvals_invalid_r60,保留取证
--   - 新严格表 rename 为 command_approvals
--   三条语句在同一事务(migrate.py 包裹 BEGIN IMMEDIATE)中原子执行。
DROP TABLE IF EXISTS command_approvals_invalid_r60;
ALTER TABLE command_approvals RENAME TO command_approvals_invalid_r60;
ALTER TABLE command_approvals_r60_strict RENAME TO command_approvals;

-- Step 4: 在重命名后的表上重建 action_id 索引
-- 先丢弃旧表遗留的同名索引:随 Step 3 的 rename,旧 idx_command_approvals_action_id
-- 已挂到 command_approvals_invalid_r60(索引名 DB 范围唯一)。若不先 DROP,
-- CREATE INDEX IF NOT EXISTS 会因索引名已存在而 no-op,导致新表缺 action_id 索引。
DROP INDEX IF EXISTS idx_command_approvals_action_id;
CREATE INDEX IF NOT EXISTS idx_command_approvals_action_id
ON command_approvals(action_id);

-- Step 5: 新增 consumed_at 复合索引(CAS 查询优化,_verify_break_glass_two_person_approval)
CREATE INDEX IF NOT EXISTS idx_command_approvals_consume_cas
ON command_approvals(action_id, approval_type, consumed_at, revoked_at, expires_at);
