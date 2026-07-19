-- R64 P1-02: capability nonce ledger 迁移到独立 CRDB security schema
--
-- 审计背景(R64 终审报告 P1-02):
--   R63 P1-01 将 _RestoreCapability nonce 防重放从进程内 _CONSUMED_NONCES set
--   迁移到 SQLite restore_capability_nonces 表(INSERT OR IGNORE CAS)。
--   但 SQLite 是本地存储,多实例/多区域部署时无法跨实例共享 nonce ledger,
--   且 nonce 状态机单一(consumed/non-existent),无 reserved/failed 中间态:
--     - assert_valid 直接 consume,若后续 restore 失败,nonce 已被消费,
--       同一 operation 无法重试(必须重新签发 capability,增加运维负担)
--     - restore 失败后 nonce 状态不可追溯(无 failed 审计记录)
--
-- 整改方案(R64 P1-02):
--   1. nonce ledger 迁移到 CRDB security.restore_capability_nonces 表
--      (security schema 隔离,跨实例共享,多区域一致)
--   2. nonce 状态机扩展: reserved → consumed | failed
--      - reserve_capability_nonce: INSERT status='reserved'(CAS,PRIMARY KEY=nonce)
--      - consume_capability_nonce: UPDATE reserved→consumed(CAS,WHERE status='reserved')
--      - fail_capability_nonce:    UPDATE reserved→failed(CAS,WHERE status='reserved')
--   3. assert_valid 调用 reserve_capability_nonce(不再直接 consume),
--      writer 在 restore 成功后 consume / 失败后 fail
--   4. failed 状态允许同 operation 重试(新 capability 新 nonce,旧 failed nonce 留审计)
--   5. CRDB 不可用时回退 SQLite(记录 warning,不阻断 — 单实例部署仍可用)
--
-- CRDB schema(lazily created by CacheStore._ensure_crdb_restore_capability_nonces):
--   CREATE SCHEMA IF NOT EXISTS security;
--   CREATE TABLE IF NOT EXISTS security.restore_capability_nonces (
--       nonce            TEXT PRIMARY KEY,
--       operation_id     TEXT NOT NULL,
--       backup_id        TEXT NOT NULL,
--       manifest_sha256  TEXT NOT NULL,
--       payload_digest   TEXT NOT NULL,
--       status           TEXT NOT NULL DEFAULT 'reserved'
--                        CHECK (status IN ('reserved', 'consumed', 'failed')),
--       reserved_at      TEXT NOT NULL,
--       reserved_by      TEXT,
--       consumed_at      TEXT,
--       failed_at        TEXT,
--       consumed_by      TEXT,
--       failure_reason   TEXT
--   );
--   CREATE INDEX IF NOT EXISTS idx_restore_nonces_op
--       ON security.restore_capability_nonces(operation_id);
--   CREATE INDEX IF NOT EXISTS idx_restore_nonces_backup_id
--       ON security.restore_capability_nonces(backup_id);
--
-- SQLite fallback schema(本 migration 文件):
--   旧表(R63 P1-01 inline 创建于 cache_store.init):
--     restore_capability_nonces(nonce PK, backup_id, manifest_sha256,
--                               payload_digest, consumed_at, consumed_by)
--   新表(R64 P1-02):增加 operation_id / status / reserved_at / reserved_by /
--   failed_at / failure_reason 列,支持 reserved→consumed|failed 状态机。
--   旧记录(consumed_at 非空)回填 status='consumed',保持向后兼容。
--
-- ALTER TABLE ADD COLUMN 幂等性:migrate.py 的 _should_skip_statement 预检
-- 会跳过已存在的列(等价 ADD COLUMN IF NOT EXISTS),故重复执行无副作用。
-- 旧库(已有 restore_capability_nonces 表)仅 ADD COLUMN;新库由 cache_store.init
-- 先 CREATE 旧 schema,本 migration 再 ADD COLUMN(顺序保证)。
--
-- IMPORTANT: 本 migration 由 migrate.py 在单个 BEGIN IMMEDIATE 事务中执行;
-- 任一语句失败将整体 ROLLBACK。本 migration 通过 _migrations_applied 仅执行一次。

-- Step 1: 为旧表添加状态机所需列(幂等 — _should_skip_statement 预检跳过已存在列)
-- operation_id: 关联恢复操作 ID(审计字段,允许多个 nonce 关联同一 operation)
ALTER TABLE restore_capability_nonces ADD COLUMN operation_id TEXT;
-- status: nonce 状态机(reserved=已预留待消费,consumed=已消费,failed=已失败)
-- 旧记录默认 'consumed'(因旧 schema 仅有 consumed 语义)
ALTER TABLE restore_capability_nonces ADD COLUMN status TEXT NOT NULL DEFAULT 'consumed';
-- reserved_at: nonce 预留时间(assert_valid 调用 reserve_capability_nonce 时写入)
ALTER TABLE restore_capability_nonces ADD COLUMN reserved_at TEXT;
-- reserved_by: 预留者标识(hostname:pid,审计字段 — 谁调用了 assert_valid 预留 nonce)
ALTER TABLE restore_capability_nonces ADD COLUMN reserved_by TEXT;
-- failed_at: nonce 失败时间(fail_capability_nonce 时写入)
ALTER TABLE restore_capability_nonces ADD COLUMN failed_at TEXT;
-- failure_reason: 失败原因(fail_capability_nonce 时写入,审计追溯)
ALTER TABLE restore_capability_nonces ADD COLUMN failure_reason TEXT;

-- Step 2: 回填旧记录的 status='consumed' + reserved_at=consumed_at(向后兼容)
-- 旧 schema 中 consumed_at 非空即表示已消费;新 schema 显式标记 status='consumed'。
-- UPDATE 幂等(WHERE status IS NULL OR status='' 限制,重复执行不覆盖新数据)。
UPDATE restore_capability_nonces
SET status = 'consumed',
    reserved_at = CASE WHEN reserved_at IS NULL OR reserved_at = ''
                       THEN consumed_at ELSE reserved_at END
WHERE (status IS NULL OR status = '' OR status = 'consumed')
  AND consumed_at IS NOT NULL AND consumed_at != '';

-- Step 3: 添加 operation_id 索引(支持按 operation 查询 nonce 审计轨迹)
-- 索引名唯一,IF NOT EXISTS 幂等。
DROP INDEX IF EXISTS idx_restore_nonces_operation_id;
CREATE INDEX IF NOT EXISTS idx_restore_nonces_operation_id
ON restore_capability_nonces(operation_id);

-- Step 4: 添加 status 索引(支持按状态查询 — 如查找所有 failed nonce)
DROP INDEX IF EXISTS idx_restore_nonces_status;
CREATE INDEX IF NOT EXISTS idx_restore_nonces_status
ON restore_capability_nonces(status);
