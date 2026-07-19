-- R64 P0-04: outbox_events lease fencing token + DLQ 审计闭环
--
-- 审计背景(R64 终审报告 P0-04: outbox 尚无生产闭环证据):
--   R63 P0-05 的 outbox lease 仅使用 lease_owner + request_hash 做 CAS,
--   缺少 fencing token / 版本号。长 provider 调用期间 lease 被回收后,
--   旧持有者仍可 complete(双重执行);complete/fail/renew 无版本号 CAS,
--   无法防御 ABA 问题(worker A 持有 lease v1 → lease 过期被 worker B 回收
--   → worker B claim 升级到 v2 → worker A 残留调用 complete 仍匹配
--   lease_owner+request_hash,误完成 worker B 的事件)。
--   DLQ 仅更新状态,无告警、无可审批 replay 记录,人工无法追溯与重放。
--
-- 整改方案(R64 P0-04):
--   1. outbox_events 新增 lease_version INTEGER DEFAULT 0 列(fencing token):
--      - claim_outbox_events CAS WHERE status='pending' AND lease_version=0,
--        成功后 lease_version=1(每次 claim/续租递增)
--      - complete/fail/renew 必须 CAS event_id+owner+lease_version+request_hash
--      - renew_outbox_lease 成功后 lease_version += 1
--   2. outbox_events 新增 dlq_reason / dlq_at 列:记录 DLQ 原因与时间(审计)
--   3. 新增 outbox_dlq_audit 表:DLQ 可审批 replay 审计记录(独立于
--      outbox_events,永久保留,人工审批后可 replay)
--
-- ALTER TABLE ADD COLUMN 幂等性:migrate.py 的 _should_skip_statement 预检
-- 会跳过已存在的列(等价 ADD COLUMN IF NOT EXISTS),故重复执行无副作用。
-- 旧库(已有 outbox_events 表)仅 ADD COLUMN;新库由 cache_store.init
-- 先 CREATE 旧 schema,本 migration 再 ADD COLUMN(顺序保证)。
--
-- IMPORTANT: 本 migration 由 migrate.py 在单个 BEGIN IMMEDIATE 事务中执行;
-- 任一语句失败将整体 ROLLBACK。本 migration 通过 _migrations_applied 仅执行一次。

-- Step 1: outbox_events 新增 lease_version 列(fencing token)
-- 旧行回填 0(等价于"未认领"),新行 DEFAULT 0;claim CAS WHERE lease_version=0
ALTER TABLE outbox_events ADD COLUMN lease_version INTEGER NOT NULL DEFAULT 0;

-- Step 2: outbox_events 新增 dlq_reason / dlq_at 列(DLQ 审计字段)
-- move_outbox_to_dlq 写入原因与时间,便于人工追溯与可审批 replay
ALTER TABLE outbox_events ADD COLUMN dlq_reason TEXT;
ALTER TABLE outbox_events ADD COLUMN dlq_at TEXT;

-- Step 3: 回填旧行的 lease_version=0(显式化,DEFAULT 已覆盖但确保 NULL→0)
-- UPDATE 幂等(WHERE lease_version IS NULL,重复执行不覆盖新数据)
UPDATE outbox_events
SET lease_version = 0
WHERE lease_version IS NULL;

-- Step 4: 新增 outbox_dlq_audit 表 — DLQ 可审批 replay 审计记录
-- 独立于 outbox_events(后者进 DLQ 后状态冻结,审计记录支持人工审批 replay)
-- 每条 DLQ 事件写入一条审计记录,包含完整 replay 上下文(payload/action/target)
CREATE TABLE IF NOT EXISTS outbox_dlq_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL,
    action_id       TEXT NOT NULL,
    effect_type     TEXT NOT NULL,
    target          TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    dlq_reason      TEXT NOT NULL,
    dlq_at          TEXT NOT NULL,
    lease_owner     TEXT,
    lease_version   INTEGER,
    attempt_count   INTEGER,
    replay_status   TEXT NOT NULL DEFAULT 'pending'
                    CHECK (replay_status IN ('pending', 'approved', 'rejected', 'replayed')),
    replayed_at     TEXT,
    replayed_by     TEXT,
    replay_note     TEXT,
    created_at      TEXT NOT NULL
);

-- Step 5: outbox_dlq_audit 索引(按 replay_status / effect_type 查询)
CREATE INDEX IF NOT EXISTS idx_outbox_dlq_audit_replay_status
ON outbox_dlq_audit(replay_status);
CREATE INDEX IF NOT EXISTS idx_outbox_dlq_audit_event_id
ON outbox_dlq_audit(event_id);
CREATE INDEX IF NOT EXISTS idx_outbox_dlq_audit_action_id
ON outbox_dlq_audit(action_id);
