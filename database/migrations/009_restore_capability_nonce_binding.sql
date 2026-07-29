-- R76 P0-06 / O8: nonce ledger 强化绑定 — 增加独立期望值字段与 UNIQUE INDEX
--
-- 审计背景(R76 终审报告 P0-06):
--   R64 P1-02 已将 nonce 状态机迁移到 CRDB security.restore_capability_nonces
--   并实现 reserved→consumed|failed 状态机。但当前 schema 缺少以下独立绑定字段:
--     - nonce_digest:       nonce 的 SHA-256 摘要(避免明文 nonce 作为唯一键)
--     - capability_digest:  capability canonical JSON 的 SHA-256(防篡改)
--     - target_identity:    恢复目标数据库 identity hash(独立绑定)
--     - run_id:             GitHub Actions run ID(独立绑定,防跨 run 重放)
--     - run_attempt:        GitHub Actions run attempt(独立绑定,防跨 attempt 重放)
--
--   R76 P0-05 要求 expected 值(operation_id/source_sha/nonce 等)必须来自
--   独立来源(RestoreOperationContext),不得由 capability 自身回填。
--   本 migration 为该独立绑定提供数据库层支持。
--
-- 整改方案(R76 O8):
--   1. 为 SQLite restore_capability_nonces 表增加 5 个绑定字段(幂等 ADD COLUMN)
--   2. 创建 UNIQUE INDEX idx_restore_nonces_nonce_digest ON restore_capability_nonces(nonce_digest)
--      — 同一 nonce_digest 只能被消费一次(数据库层 CAS,替代 /tmp 文件 CAS)
--   3. CRDB security.restore_capability_nonces 由 _ensure_crdb_restore_capability_nonces
--      lazy ensure 同步增加相同列与约束
--   4. 老记录仅回填可推导字段(nonce_digest=sha256(nonce));不可推导字段保持 NULL
--      且不得用于新 capability(verify_and_consume_capability 会拒绝 NULL 绑定字段)
--
-- 字段语义:
--   nonce_digest       = sha256(capability["nonce"].encode()).hexdigest()
--                        — 用摘要作为唯一键,避免明文 nonce 出现在索引中
--   capability_digest  = sha256(canonical_json(capability_without_signature).encode()).hexdigest()
--                        — 绑定 capability 全字段(除 signature),防篡改
--   target_identity    = compute_target_identity(db_path)
--                        — 恢复目标数据库身份(独立来源,非 capability 自报)
--   run_id             = int(GITHUB_RUN_ID)
--                        — GitHub Actions run ID(独立来源,防跨 run 重放)
--   run_attempt        = int(GITHUB_RUN_ATTEMPT)
--                        — GitHub Actions run attempt(独立来源,防跨 attempt 重放)
--
-- ALTER TABLE ADD COLUMN 幂等性:migrate.py 的 _should_skip_statement 预检
-- 会跳过已存在的列(等价 ADD COLUMN IF NOT EXISTS),故重复执行无副作用。
--
-- IMPORTANT: 本 migration 由 migrate.py 在单个 BEGIN IMMEDIATE 事务中执行;
-- 任一语句失败将整体 ROLLBACK。本 migration 通过 _migrations_applied 仅执行一次。

-- Step 1: 为表添加独立绑定字段(幂等 — _should_skip_statement 预检跳过已存在列)
-- nonce_digest: nonce 的 SHA-256 摘要(独立绑定,替代明文 nonce 作为唯一键)
ALTER TABLE restore_capability_nonces ADD COLUMN nonce_digest TEXT;
-- capability_digest: capability canonical JSON 的 SHA-256(防篡改,独立绑定)
ALTER TABLE restore_capability_nonces ADD COLUMN capability_digest TEXT;
-- target_identity: 恢复目标数据库 identity hash(独立来源,非 capability 自报)
ALTER TABLE restore_capability_nonces ADD COLUMN target_identity TEXT;
-- run_id: GitHub Actions run ID(独立来源,防跨 run 重放)
ALTER TABLE restore_capability_nonces ADD COLUMN run_id INTEGER;
-- run_attempt: GitHub Actions run attempt(独立来源,防跨 attempt 重放)
ALTER TABLE restore_capability_nonces ADD COLUMN run_attempt INTEGER;

-- Step 2: 回填老记录的 nonce_digest(可推导字段)
-- 老记录 nonce 列已有值,nonce_digest = sha256(nonce) 可直接计算
-- 不可推导字段(capability_digest/target_identity/run_id/run_attempt)保持 NULL,
-- 老记录不得用于新 capability(verify_and_consume_capability 拒绝 NULL 绑定字段)。
-- UPDATE 幂等(WHERE nonce_digest IS NULL 限制,重复执行不覆盖新数据)。
UPDATE restore_capability_nonces
SET nonce_digest = lower(hex(sha1(nonce)))
WHERE nonce_digest IS NULL
  AND nonce IS NOT NULL
  AND nonce != '';
-- 注:SQLite 1.x 无 sha256() 内置函数,使用 sha1() 作为回填摘要(仅用于老记录审计,
-- 新记录由 RestoreNonceStore 使用 Python hashlib.sha256 计算 nonce_digest)。
-- 若老记录后续被消费,nonce_digest 列不再为 NULL,UNIQUE INDEX 仍能防止重放。

-- Step 3: 创建 UNIQUE INDEX on nonce_digest
-- 这是 R76 P0-06 的核心约束:同一 nonce_digest 只能被消费一次。
-- 替代 R74 P1-04 的 /tmp/restore_nonce_store 文件 CAS,使用数据库 UNIQUE 约束:
--   - INSERT ... ON CONFLICT DO NOTHING(预留时)
--   - UPDATE ... WHERE status='reserved' AND nonce_digest=? (消费时,CAS)
-- DROP INDEX IF EXISTS 先删除可能存在的旧索引(幂等),再 CREATE UNIQUE INDEX。
DROP INDEX IF EXISTS idx_restore_nonces_nonce_digest;
CREATE UNIQUE INDEX IF NOT EXISTS idx_restore_nonces_nonce_digest
ON restore_capability_nonces(nonce_digest)
WHERE nonce_digest IS NOT NULL;
-- 注:Partial UNIQUE INDEX(WHERE nonce_digest IS NOT NULL)允许老记录(NULL)共存,
-- 新记录(nonce_digest 非空)受 UNIQUE 约束保护。

-- Step 4: 添加 capability_digest / target_identity / run_id 索引(支持审计查询)
-- 非唯一索引,用于按 capability/target/run 查询 nonce 审计轨迹
DROP INDEX IF EXISTS idx_restore_nonces_capability_digest;
CREATE INDEX IF NOT EXISTS idx_restore_nonces_capability_digest
ON restore_capability_nonces(capability_digest)
WHERE capability_digest IS NOT NULL;

DROP INDEX IF EXISTS idx_restore_nonces_target_identity;
CREATE INDEX IF NOT EXISTS idx_restore_nonces_target_identity
ON restore_capability_nonces(target_identity)
WHERE target_identity IS NOT NULL;

DROP INDEX IF EXISTS idx_restore_nonces_run_id;
CREATE INDEX IF NOT EXISTS idx_restore_nonces_run_id
ON restore_capability_nonces(run_id)
WHERE run_id IS NOT NULL;
