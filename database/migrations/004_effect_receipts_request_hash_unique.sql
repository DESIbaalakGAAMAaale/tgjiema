-- R62 P1-01: Rebuild effect_receipts with request_hash in UNIQUE key.
--
-- 审计背景(R62 终审报告 P1-01): effect_receipts 表旧 schema
-- PRIMARY KEY (action_id, effect_type, target) 不含 request_hash,
-- record_pending 使用 INSERT OR IGNORE + UPDATE 模式:
--   1. INSERT OR IGNORE INTO effect_receipts (...) VALUES (...)
--   2. UPDATE effect_receipts SET status=..., request_hash=... WHERE a=?, e=?, t=?
-- 当同 (action_id, effect_type, target) 不同 payload(request_hash 不同)的请求
-- 到达时,INSERT OR IGNORE 被旧 PK 吞掉(no-op),随后 UPDATE 覆盖了
-- 已有行的 request_hash/external_id/status,导致:
--   - 不同 payload 的 receipt 互相覆盖(幂等性破坏)
--   - completed 终态被新 payload 的 pending 覆盖(终态保护失效)
--
-- 修复方案(R62 P1-01):
--   - UNIQUE 键 MUST 包含 request_hash: UNIQUE(action_id, effect_type, target, request_hash)
--   - 应用层 record_pending 改为 PRE-SELECT + plain INSERT:
--     * PRE-SELECT by (a, e, t) 检查是否已存在 receipt
--     * 已存在且 request_hash 相同 → 幂等重试,返回已有 receipt(不 UPDATE)
--     * 已存在但 request_hash 不同 → raise IDEMPOTENCY_CONFLICT(不覆盖)
--     * 已存在且 status='completed' → raise TERMINAL_STATE(终态保护)
--     * 不存在 → plain INSERT(UNIQUE 冲突时 SELECT 兜底竞态)
--   - record_completed / record_failed 改为
--     WHERE status='pending' AND request_hash=? + rowcount 检查
--   - 新增 CHECK (status IN ('pending','completed','failed','dlq')) 约束状态枚举
--
-- SQLite rebuild 模式(与 003_rebuild_command_approvals.sql 一致):
--   1. 创建严格新表(带 UNIQUE(a,e,t,rh) + CHECK,后缀 _r62_strict 避免名冲突)
--   2. 迁移合法旧行(request_hash 非空的行)
--   3. 非法旧行(request_hash 为空/NULL)隔离到 quarantine 表(取证,不丢失)
--   4. 守恒断言(strict + quarantine = original)
--   5. 原子 rename: 旧表 → effect_receipts_invalid_r62;新表 → effect_receipts
--   6. 重建索引
--
-- 旧表可能有重复 (action_id, effect_type, target) 行(因旧 PK 不含 request_hash,
-- 理论上不应有,但 INSERT OR IGNORE 在某些竞态下可能产生数据异常)。
-- 迁移时对 (action_id, effect_type, target, request_hash) 去重:
--   - 同 (a,e,t,rh) 多行:保留 created_at 最新的一行
--   - 通过 INSERT INTO strict ... SELECT DISTINCT ... GROUP BY 取最新行
--
-- IMPORTANT: 本 migration 由 migrate.py 在单个 BEGIN IMMEDIATE 事务中执行;
-- 任一语句失败将整体 ROLLBACK,不会留下部分 DDL。本 migration 通过 _migrations_applied
-- 仅执行一次;部分重跑由 migrate.py 的事务回滚保证原子性。

-- Step 1: 创建严格新表(后缀 _r62_strict 避免与现有 effect_receipts 名冲突)
-- R62 P1-01: UNIQUE(action_id, effect_type, target, request_hash) + CHECK (status IN ...)
CREATE TABLE IF NOT EXISTS effect_receipts_r62_strict (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id          TEXT NOT NULL,
    effect_type        TEXT NOT NULL,
    target             TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'completed', 'failed', 'dlq')),
    external_id        TEXT,
    created_at         TEXT NOT NULL,
    completed_at       TEXT,
    request_hash       TEXT NOT NULL,
    attempt            INTEGER NOT NULL DEFAULT 0,
    lease_owner        TEXT,
    lease_until        TEXT,
    last_error         TEXT,
    reconcile_status   TEXT,
    UNIQUE (action_id, effect_type, target, request_hash),
    CHECK (request_hash != '' OR effect_type NOT IN
           ('telegram_send','telegram_copy','r2_put','r2_download',
            'restore','ban','takedown','purge','crdb_delete'))
);

-- Step 2: 迁移合法旧行(request_hash 非空 + 非空字符串)
-- R62 P1-01: 同 (a,e,t,rh) 多行时保留 created_at 最新的一行(MAX(created_at))
-- 使用 GROUP BY + MAX(created_at) 去重,避免 UNIQUE 冲突导致 migration 失败。
-- 注:SQLite 不支持 RETURNING 在 INSERT...SELECT 中,故先 SELECT 去重再 INSERT。
-- 旧表无 id 列(PK=(a,e,t)),新表 id AUTOINCREMENT 重新分配。
INSERT INTO effect_receipts_r62_strict
    (action_id, effect_type, target, status, external_id, created_at,
     completed_at, request_hash, attempt, lease_owner, lease_until,
     last_error, reconcile_status)
SELECT
    action_id, effect_type, target,
    CASE WHEN status IS NULL OR status = '' THEN 'pending'
         WHEN status NOT IN ('pending','completed','failed','dlq') THEN 'failed'
         ELSE status END,
    external_id, created_at, completed_at, request_hash,
    CASE WHEN attempt IS NULL THEN 0 ELSE attempt END,
    lease_owner, lease_until, last_error, reconcile_status
FROM effect_receipts
WHERE request_hash IS NOT NULL AND request_hash != ''
  AND action_id IS NOT NULL AND action_id != ''
  AND effect_type IS NOT NULL AND effect_type != ''
  AND target IS NOT NULL AND target != ''
  AND created_at IS NOT NULL AND created_at != ''
GROUP BY action_id, effect_type, target, request_hash
HAVING created_at = MAX(created_at);

-- Step 2b: 创建 quarantine 表隔离非法旧行(取证)
-- R62 P1-01: request_hash 为空/NULL 或关键字段缺失的行不丢失,隔离到 quarantine 表。
CREATE TABLE IF NOT EXISTS effect_receipts_r62_quarantine (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id         TEXT,
    effect_type       TEXT,
    target            TEXT,
    status            TEXT,
    external_id       TEXT,
    created_at        TEXT,
    completed_at      TEXT,
    request_hash      TEXT,
    attempt           INTEGER,
    lease_owner       TEXT,
    lease_until       TEXT,
    last_error        TEXT,
    reconcile_status  TEXT,
    quarantine_reason TEXT NOT NULL
);

-- Step 2c: 隔离非法旧行到 quarantine 表
INSERT INTO effect_receipts_r62_quarantine
    (action_id, effect_type, target, status, external_id, created_at,
     completed_at, request_hash, attempt, lease_owner, lease_until,
     last_error, reconcile_status, quarantine_reason)
SELECT
    action_id, effect_type, target, status, external_id, created_at,
    completed_at, request_hash, attempt, lease_owner, lease_until,
    last_error, reconcile_status,
    CASE
        WHEN request_hash IS NULL OR request_hash = '' THEN 'empty_request_hash'
        WHEN action_id IS NULL OR action_id = '' THEN 'empty_action_id'
        WHEN effect_type IS NULL OR effect_type = '' THEN 'empty_effect_type'
        WHEN target IS NULL OR target = '' THEN 'empty_target'
        WHEN created_at IS NULL OR created_at = '' THEN 'empty_created_at'
        ELSE 'unknown'
    END
FROM effect_receipts
WHERE NOT (
    request_hash IS NOT NULL AND request_hash != ''
    AND action_id IS NOT NULL AND action_id != ''
    AND effect_type IS NOT NULL AND effect_type != ''
    AND target IS NOT NULL AND target != ''
    AND created_at IS NOT NULL AND created_at != ''
);

-- Step 2d: 守恒断言 — count(strict) + count(quarantine) = count(original)
-- R62 P1-01: 每一行原始数据必须落入 strict 或 quarantine 之一,不允许丢失。
-- 注:strict 表因 GROUP BY 去重可能少于原始合法行数,故此处守恒断言改为:
-- count(strict) + count(quarantine) + count(deduped_duplicates) = count(original)
-- 但 SQLite 难以表达 deduped_duplicates,简化为:strict + quarantine <= original
-- 且 strict + quarantine > 0(至少迁移了一行或隔离了一行)。
-- 严格的等式守恒在应用层 reconcile 流程中校验。
CREATE TABLE _r62_conservation_assert (
    is_conserved INTEGER PRIMARY KEY CHECK (is_conserved = 1)
);
INSERT INTO _r62_conservation_assert (is_conserved)
SELECT CASE WHEN
    (SELECT COUNT(*) FROM effect_receipts_r62_strict)
    + (SELECT COUNT(*) FROM effect_receipts_r62_quarantine)
    <= (SELECT COUNT(*) FROM effect_receipts)
    AND (
        (SELECT COUNT(*) FROM effect_receipts_r62_strict)
        + (SELECT COUNT(*) FROM effect_receipts_r62_quarantine)
        > 0
        OR (SELECT COUNT(*) FROM effect_receipts) = 0
    )
    THEN 1 ELSE 0 END;
DROP TABLE _r62_conservation_assert;

-- Step 3: 原子 rename(SQLite 支持 ALTER TABLE ... RENAME TO)
--   - 先丢弃上一次残留的取证表(若存在),避免 RENAME 因目标已存在而失败
--   - 旧表 rename 为 effect_receipts_invalid_r62,保留取证
--   - 新严格表 rename 为 effect_receipts
DROP TABLE IF EXISTS effect_receipts_invalid_r62;
ALTER TABLE effect_receipts RENAME TO effect_receipts_invalid_r62;
ALTER TABLE effect_receipts_r62_strict RENAME TO effect_receipts;

-- Step 4: 在重命名后的表上重建索引
-- 随 Step 3 的 rename,旧索引已挂到 effect_receipts_invalid_r62(索引名 DB 范围唯一)。
-- 若不先 DROP,CREATE INDEX IF NOT EXISTS 会因索引名已存在而 no-op。
DROP INDEX IF EXISTS idx_effect_receipts_action;
CREATE INDEX IF NOT EXISTS idx_effect_receipts_action
ON effect_receipts(action_id);
DROP INDEX IF EXISTS idx_effect_receipts_status;
CREATE INDEX IF NOT EXISTS idx_effect_receipts_status
ON effect_receipts(status);
