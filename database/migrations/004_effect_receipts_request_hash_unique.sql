-- R62 P1-01 / R63 P1-03: Rebuild effect_receipts with request_hash in UNIQUE key.
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
--   2. 迁移合法旧行(request_hash 非空的行) — winner 行入 strict
--   3. 非法旧行(request_hash 为空/NULL)隔离到 quarantine 表(取证,不丢失)
--   4. 为每条原始 row 写入 duplicates 取证表(strict|duplicate|quarantine)
--   5. 守恒断言(严格等式)+ 证据完整性断言
--   6. 原子 rename: 旧表 → effect_receipts_invalid_r62;新表 → effect_receipts
--   7. 重建索引
--
-- 旧表可能有重复 (action_id, effect_type, target, request_hash) 行(因旧 PK 不含
-- request_hash,INSERT OR IGNORE 在某些竞态下可能产生数据异常)。
-- 迁移时对 (a,e,t,rh) 去重:
--   - 同 (a,e,t,rh) 多行:保留 created_at 最新的一行(winner)
--   - 其余行(loser)不丢弃,而是写入 effect_receipts_r62_duplicates 取证表
--     (classification='duplicate'),记录 source_rowid 与 winner_rowid,可追溯每条
--     原始记录的去向。
--
-- R63 P1-03 整改(R63 终审报告 P1-03):
--   旧 004 migration 将严格守恒等式降级为 `strict + quarantine <= original`,
--   且 GROUP BY 去重时丢弃的重复行未写入独立取证表,无法证明每条原始记录的去向
--   (审计无法回溯 loser 行是否被静默丢失)。整改措施:
--   - 新增 effect_receipts_r62_duplicates 取证表,为每条原始 row 记录
--     source_rowid / classification / winner_rowid(分类:strict|duplicate|quarantine)
--   - 去重 loser 行不再静默丢弃,而是在取证表中留痕(classification='duplicate')
--   - 守恒断言升级为严格等式:
--       count(strict) + count(quarantine)
--         + count(duplicates WHERE classification='duplicate') == count(original)
--   - 额外证据完整性断言:count(duplicates) == count(original)(每条原始 row 都有取证)
--   - 不再依赖 GROUP BY 静默丢行:winner 通过确定性子查询(MAX(created_at) + MAX(rowid)
--     打破并列)选取,strict INSERT 与取证分类共享同一 winner 集合,保证一致。
--
-- IMPORTANT: 本 migration 由 migrate.py 在单个 BEGIN IMMEDIATE 事务中执行;
-- 任一语句失败将整体 ROLLBACK,不会留下部分 DDL。本 migration 通过 _migrations_applied
-- 仅执行一次;部分重跑由 migrate.py 的事务回滚保证原子性。migrate.py 的
-- _assert_migration_fingerprint 在 COMMIT 前再做一次 Python 层守恒断言(防御纵深)。

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

-- Step 2: 创建 duplicates 取证表(R63 P1-03)
-- 为每条原始 row 记录去向:source_rowid(旧表 rowid)、classification
-- (strict|duplicate|quarantine)、winner_rowid(winner 的旧表 rowid;quarantine 行为 NULL)。
-- 不用 GROUP BY 静默丢证据 — 每条原始 row 都在此表有且仅有一行(由证据完整性断言保证)。
CREATE TABLE IF NOT EXISTS effect_receipts_r62_duplicates (
    source_rowid    INTEGER,
    classification  TEXT NOT NULL CHECK (classification IN ('duplicate','quarantine','strict')),
    winner_rowid    INTEGER,
    action_id       TEXT,
    effect_type     TEXT,
    target          TEXT,
    request_hash    TEXT,
    migrated_at     TEXT NOT NULL
);

-- Step 2a: 确定性选取每个合法 (a,e,t,rh) 分组的 winner rowid
-- R63 P1-03: winner = MAX(created_at);并列时用 MAX(rowid) 打破(确定性,可复现)。
-- 该临时映射同时供 strict INSERT 与 duplicates 取证分类使用,保证二者对 "谁是 winner"
-- 的判定完全一致 — 不再用 GROUP BY 静默丢行,loser 全部留痕于取证表。
-- 临时表在断言后、rename 前 DROP(不残留 schema)。
DROP TABLE IF EXISTS _r62_winner_rowids;
CREATE TEMP TABLE _r62_winner_rowids AS
SELECT er.rowid AS winner_rowid,
       er.action_id, er.effect_type, er.target, er.request_hash
FROM effect_receipts er
WHERE er.request_hash IS NOT NULL AND er.request_hash != ''
  AND er.action_id IS NOT NULL AND er.action_id != ''
  AND er.effect_type IS NOT NULL AND er.effect_type != ''
  AND er.target IS NOT NULL AND er.target != ''
  AND er.created_at IS NOT NULL AND er.created_at != ''
  AND er.rowid = (
      SELECT s.rowid FROM effect_receipts s
      WHERE s.action_id = er.action_id
        AND s.effect_type = er.effect_type
        AND s.target = er.target
        AND s.request_hash = er.request_hash
        AND s.request_hash IS NOT NULL AND s.request_hash != ''
        AND s.action_id IS NOT NULL AND s.action_id != ''
        AND s.effect_type IS NOT NULL AND s.effect_type != ''
        AND s.target IS NOT NULL AND s.target != ''
        AND s.created_at IS NOT NULL AND s.created_at != ''
      ORDER BY s.created_at DESC, s.rowid DESC
      LIMIT 1
  );

-- Step 2b: 迁移合法 winner 行到 strict 表
-- R62 P1-01: 同 (a,e,t,rh) 多行时仅 winner(MAX created_at,并列用 MAX rowid))入 strict。
-- R63 P1-03: 不再用 GROUP BY + HAVING 静默丢 loser,改为按 _r62_winner_rowids 精确选取,
-- loser 在 Step 2e 写入 duplicates 取证表。
-- 旧表无 id 列(PK=(a,e,t)),新表 id AUTOINCREMENT 重新分配。
-- status/attempt 做与旧实现一致的默认值归一(NULL/非法 → 'pending'/'failed';NULL attempt → 0)。
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
WHERE rowid IN (SELECT winner_rowid FROM _r62_winner_rowids);

-- Step 2c: 创建 quarantine 表隔离非法旧行(取证)
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

-- Step 2d: 隔离非法旧行到 quarantine 表
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

-- Step 2e: 为每条原始 row 写入 duplicates 取证表(R63 P1-03)
-- classification:
--   'quarantine' — 非法行(同时已入 quarantine 表),winner_rowid = NULL
--   'strict'     — 合法 winner 行(同时已入 strict 表),winner_rowid = 自身 rowid
--   'duplicate'  — 合法 loser 行(被去重,不入 strict/quarantine),winner_rowid = 同组 winner 的 rowid
-- 每条原始 row 在此表有且仅有一行(由 Step 2g 证据完整性断言保证)。
-- 不得用 GROUP BY 静默丢失证据 — 此处扫描全表,逐行留痕。
INSERT INTO effect_receipts_r62_duplicates
    (source_rowid, classification, winner_rowid, action_id, effect_type,
     target, request_hash, migrated_at)
SELECT
    er.rowid,
    CASE
        WHEN NOT (
            er.request_hash IS NOT NULL AND er.request_hash != ''
            AND er.action_id IS NOT NULL AND er.action_id != ''
            AND er.effect_type IS NOT NULL AND er.effect_type != ''
            AND er.target IS NOT NULL AND er.target != ''
            AND er.created_at IS NOT NULL AND er.created_at != ''
        ) THEN 'quarantine'
        WHEN er.rowid IN (SELECT winner_rowid FROM _r62_winner_rowids) THEN 'strict'
        ELSE 'duplicate'
    END,
    CASE
        WHEN NOT (
            er.request_hash IS NOT NULL AND er.request_hash != ''
            AND er.action_id IS NOT NULL AND er.action_id != ''
            AND er.effect_type IS NOT NULL AND er.effect_type != ''
            AND er.target IS NOT NULL AND er.target != ''
            AND er.created_at IS NOT NULL AND er.created_at != ''
        ) THEN NULL
        ELSE (
            SELECT w.winner_rowid FROM _r62_winner_rowids w
            WHERE w.action_id = er.action_id
              AND w.effect_type = er.effect_type
              AND w.target = er.target
              AND w.request_hash = er.request_hash
        )
    END,
    er.action_id, er.effect_type, er.target, er.request_hash,
    datetime('now')
FROM effect_receipts er;

-- Step 2f: 守恒断言 — 严格等式(R63 P1-03)
-- R63 P1-03: 旧实现降级为 strict + quarantine <= original(无法证明 loser 去向)。
-- 新实现升级为严格等式:
--   count(strict) + count(quarantine) + count(duplicates WHERE classification='duplicate')
--   == count(original)
-- 每条原始 row 必须落入 strict(winner) / quarantine(非法) / duplicate(loser) 三者之一,
-- 不允许丢失。等式不成立时 CASE 返回 0,违反 CHECK 约束 → 事务 ROLLBACK(fail-closed)。
-- 注:此处 COUNT(*) 比对发生在 rename 之前,effect_receipts 仍是旧表(original)。
CREATE TABLE _r62_conservation_assert (
    is_conserved INTEGER PRIMARY KEY CHECK (is_conserved = 1)
);
INSERT INTO _r62_conservation_assert (is_conserved)
SELECT CASE WHEN
    (SELECT COUNT(*) FROM effect_receipts_r62_strict)
    + (SELECT COUNT(*) FROM effect_receipts_r62_quarantine)
    + (SELECT COUNT(*) FROM effect_receipts_r62_duplicates WHERE classification = 'duplicate')
    = (SELECT COUNT(*) FROM effect_receipts)
    THEN 1 ELSE 0 END;
DROP TABLE _r62_conservation_assert;

-- Step 2g: 证据完整性断言(R63 P1-03)
-- duplicates 取证表必须为每条原始 row 留痕(不能漏记):
--   count(duplicates) == count(original)
-- 不成立时违反 CHECK → 事务 ROLLBACK。防止 winner 选取 / 取证写入遗漏某条原始 row。
CREATE TABLE _r62_evidence_assert (
    is_complete INTEGER PRIMARY KEY CHECK (is_complete = 1)
);
INSERT INTO _r62_evidence_assert (is_complete)
SELECT CASE WHEN
    (SELECT COUNT(*) FROM effect_receipts_r62_duplicates)
    = (SELECT COUNT(*) FROM effect_receipts)
    THEN 1 ELSE 0 END;
DROP TABLE _r62_evidence_assert;

-- 清理临时 winner 映射表(rename 前清理,不残留 schema)
DROP TABLE IF EXISTS _r62_winner_rowids;

-- Step 3: 原子 rename(SQLite 支持 ALTER TABLE ... RENAME TO)
--   - 先丢弃上一次残留的取证表(若存在),避免 RENAME 因目标已存在而失败
--   - 旧表 rename 为 effect_receipts_invalid_r62,保留取证
--   - 新严格表 rename 为 effect_receipts
--   - quarantine / duplicates 取证表保留原名(审计取证)
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
