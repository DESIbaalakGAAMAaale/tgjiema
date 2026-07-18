-- R60 P0-05 / R61 P1-01: Rebuild command_approvals with strict constraints.
--
-- 审计背景(R60 终审报告 §8 P0-05 / R61 P1-01): 001_initial_schema.sql 使用
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
--   2. 迁移合法旧行(满足约束的行,严格 WHERE 过滤)
--   3. 非法旧行隔离到 quarantine 表(取证,不丢失)
--   4. 守恒断言(strict + quarantine = original)
--   5. 原子 rename: 旧表 → command_approvals_invalid_r60;新表 → command_approvals
--   6. 重建索引
--
-- 新增约束(R60 P0-05 / R61 P1-01):
--   - decision        NOT NULL + CHECK IN ('approved','rejected','cancelled')
--   - request_hash    NOT NULL + CHECK (length=64 AND NOT GLOB '*[^0-9a-f]*' 完整小写十六进制校验)
--   - mfa_receipt     NOT NULL + CHECK (length(trim(...)) > 0 防空白绕过)
--   - permission      NOT NULL + CHECK (length(trim(...)) > 0)
--   - expires_at      NOT NULL + CHECK (length(trim(...)) > 0)
--   - approval_type   NOT NULL + CHECK IN ('break_glass','quarantine_delete','collection','maintenance','rbac')
--   - UNIQUE 包含 approval_type,允许同一 (action_id, approver_id) 跨类型
--
-- R61 P1-01 修复:
--   - 移除 'r60_invalid_skip' 伪 hash(原代码在 SELECT 中用字面量填充空 hash,
--     导致 strict 表混入非法行,违背"严格表只含合法行"语义)
--   - INSERT OR IGNORE → plain INSERT(UNIQUE 冲突应失败而非静默丢行)
--   - 新增 quarantine 表 command_approvals_r60_quarantine 隔离非法旧行(取证)
--   - 新增守恒断言:count(strict) + count(quarantine) = count(original)
--
-- IMPORTANT: 本 migration 由 migrate.py 在单个 BEGIN IMMEDIATE 事务中执行;
-- 任一语句失败将整体 ROLLBACK,不会留下部分 DDL。本 migration 通过 _migrations_applied
-- 仅执行一次;部分重跑由 migrate.py 的事务回滚保证原子性。

-- Step 1: 创建严格新表(后缀 _r60_strict 避免与现有 command_approvals 名冲突)
-- R61 P1-01: 添加 CHECK 约束(length/hex/trim-non-empty)
CREATE TABLE IF NOT EXISTS command_approvals_r60_strict (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id       TEXT NOT NULL,
    approver_id     BIGINT NOT NULL,
    approval_type   TEXT NOT NULL CHECK (approval_type IN ('break_glass','quarantine_delete','collection','maintenance','rbac')),
    decision        TEXT NOT NULL CHECK (decision IN ('approved','rejected','cancelled')) DEFAULT 'approved',
    request_hash    TEXT NOT NULL CHECK (length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'),
    mfa_receipt     TEXT NOT NULL CHECK (length(trim(mfa_receipt)) > 0),
    permission      TEXT NOT NULL CHECK (length(trim(permission)) > 0),
    approved_at     TEXT NOT NULL,
    expires_at      TEXT NOT NULL CHECK (length(trim(expires_at)) > 0),
    consumed_at     TEXT,
    revoked_at      TEXT,
    metadata_json   TEXT,
    UNIQUE(action_id, approver_id, approval_type)
);

-- Step 2: 迁移合法旧行(仅满足严格约束的行)
-- R61 P1-01: 严格 WHERE 过滤,只迁移真正合法的行:
--   - mfa_receipt 非空(trim 后非空)
--   - permission 非空(trim 后非空)
--   - expires_at 非空(trim 后非空)
--   - request_hash 非空 + length=64 + NOT GLOB '*[^0-9a-f]*' 完整小写十六进制校验
--   - decision 为 NULL 或在合法集合内(NULL → 'approved' 默认值,经 CASE 转换)
--   - approval_type 为 NULL 或在合法集合内(NULL → 'break_glass' 默认值,经 CASE 转换)
-- 非法行不迁移到 strict 表,而是隔离到 quarantine 表(Step 2b)。
-- R61 P1-01: plain INSERT(非 INSERT OR IGNORE),UNIQUE 冲突应让 migration 失败
-- 而非静默丢行(fail-closed,审计可追溯)。
INSERT INTO command_approvals_r60_strict
    (id, action_id, approver_id, approval_type, decision, request_hash,
     mfa_receipt, permission, approved_at, expires_at, consumed_at,
     revoked_at, metadata_json)
SELECT
    id, action_id, approver_id,
    CASE WHEN approval_type IS NULL OR approval_type = '' THEN 'break_glass' ELSE approval_type END,
    CASE WHEN decision IS NULL OR decision = '' THEN 'approved' ELSE decision END,
    request_hash,
    mfa_receipt, permission, approved_at,
    expires_at, consumed_at, revoked_at, metadata_json
FROM command_approvals
WHERE mfa_receipt IS NOT NULL AND length(trim(mfa_receipt)) > 0
  AND permission IS NOT NULL AND length(trim(permission)) > 0
  AND expires_at IS NOT NULL AND length(trim(expires_at)) > 0
  AND request_hash IS NOT NULL AND length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'
  AND (decision IS NULL OR decision IN ('approved','rejected','cancelled'))
  AND (approval_type IS NULL OR approval_type IN ('break_glass','quarantine_delete','collection','maintenance','rbac'));

-- NOTE: request_hash 的格式约束(^[0-9a-f]{64}$)在 SQL 层做完整十六进制校验
-- (length=64 AND NOT GLOB '*[^0-9a-f]*'),即字符串中不得包含任何非 [0-9a-f] 字符。
-- R62 P1-03: 旧实现 GLOB '[0-9a-f]*' 仅校验首字符为十六进制,后 63 字符可任意,
-- 无法防止 'a$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$$' 等非法
-- hash 通过 CHECK。新实现 NOT GLOB '*[^0-9a-f]*' 保证全部 64 字符均为小写十六进制。
-- 应用层 _verify_break_glass_two_person_approval 仍做运行时复核(参见 services/data_lifecycle.py)。
-- R61 P1-01: 不再使用 'r60_invalid_skip' 伪 hash 填充空 hash 行 — 非法行
-- 直接隔离到 quarantine 表,不混入 strict 表。

-- Step 2b: 创建 quarantine 表隔离非法旧行(取证)
-- R61 P1-01: 不满足 strict 约束的行不丢失,而是隔离到 quarantine 表保留取证。
-- quarantine 表保留原始字段值(不做默认值转换),并新增 quarantine_reason 字段
-- 标注隔离原因(便于人工审计 / DR 复核)。
CREATE TABLE IF NOT EXISTS command_approvals_r60_quarantine (
    id                INTEGER PRIMARY KEY,
    action_id         TEXT,
    approver_id       BIGINT,
    approval_type     TEXT,
    decision          TEXT,
    request_hash      TEXT,
    mfa_receipt       TEXT,
    permission        TEXT,
    approved_at       TEXT,
    expires_at        TEXT,
    consumed_at       TEXT,
    revoked_at        TEXT,
    metadata_json     TEXT,
    quarantine_reason TEXT NOT NULL
);

-- Step 2c: 隔离非法旧行到 quarantine 表
-- R61 P1-01: WHERE NOT (strict 条件) — 即所有不满足 strict 约束的行
-- quarantine_reason 标注隔离原因(便于人工审计定位失败字段)
INSERT INTO command_approvals_r60_quarantine
    (id, action_id, approver_id, approval_type, decision, request_hash,
     mfa_receipt, permission, approved_at, expires_at, consumed_at,
     revoked_at, metadata_json, quarantine_reason)
SELECT
    id, action_id, approver_id, approval_type, decision, request_hash,
    mfa_receipt, permission, approved_at, expires_at, consumed_at,
    revoked_at, metadata_json,
    CASE
        WHEN mfa_receipt IS NULL OR length(trim(mfa_receipt)) = 0 THEN 'empty_mfa_receipt'
        WHEN permission IS NULL OR length(trim(permission)) = 0 THEN 'empty_permission'
        WHEN expires_at IS NULL OR length(trim(expires_at)) = 0 THEN 'empty_expires_at'
        WHEN request_hash IS NULL OR length(request_hash) != 64 OR request_hash GLOB '*[^0-9a-f]*' THEN 'invalid_request_hash'
        WHEN decision IS NOT NULL AND decision NOT IN ('approved','rejected','cancelled') THEN 'invalid_decision'
        WHEN approval_type IS NOT NULL AND approval_type NOT IN ('break_glass','quarantine_delete','collection','maintenance','rbac') THEN 'invalid_approval_type'
        ELSE 'unknown'
    END
FROM command_approvals
WHERE NOT (
    mfa_receipt IS NOT NULL AND length(trim(mfa_receipt)) > 0
    AND permission IS NOT NULL AND length(trim(permission)) > 0
    AND expires_at IS NOT NULL AND length(trim(expires_at)) > 0
    AND request_hash IS NOT NULL AND length(request_hash) = 64 AND request_hash NOT GLOB '*[^0-9a-f]*'
    AND (decision IS NULL OR decision IN ('approved','rejected','cancelled'))
    AND (approval_type IS NULL OR approval_type IN ('break_glass','quarantine_delete','collection','maintenance','rbac'))
);

-- Step 2d: 守恒断言 — count(strict) + count(quarantine) = count(original)
-- R61 P1-01: 每一行原始数据必须落入 strict 或 quarantine 之一,不允许丢失。
-- 若守恒不成立,以下 INSERT 会违反 CHECK 约束(is_conserved=0),事务 ROLLBACK,
-- migration 失败(fail-closed,禁止数据静默丢失)。
-- 临时断言表在断言后立即删除(不残留 schema)。
CREATE TABLE _r60_conservation_assert (
    is_conserved INTEGER PRIMARY KEY CHECK (is_conserved = 1)
);
INSERT INTO _r60_conservation_assert (is_conserved)
SELECT CASE WHEN
    (SELECT COUNT(*) FROM command_approvals_r60_strict)
    + (SELECT COUNT(*) FROM command_approvals_r60_quarantine)
    = (SELECT COUNT(*) FROM command_approvals)
    THEN 1 ELSE 0 END;
DROP TABLE _r60_conservation_assert;

-- Step 3: 原子 rename(SQLite 支持 ALTER TABLE ... RENAME TO)
--   - 先丢弃上一次残留的取证表(若存在),避免 RENAME 因目标已存在而失败
--   - 旧表(含非法行已隔离到 quarantine,strict 行已迁移)rename 为
--     command_approvals_invalid_r60,保留取证(注:此时该表为"已迁移走合法行"
--     的旧表,与 quarantine 表分工不同 — quarantine 是非法行取证,
--     invalid_r60 是 rename 后的旧表壳,审计时关注 quarantine 即可)
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
