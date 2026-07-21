-- R67 P1-06: Restore 外部副作用 recovery reconciler 持久化
--
-- 审计背景(R67 终审报告 P1-06: Restore 外部副作用仍需 recovery reconciler):
--   SQLite rename、CRDB routing switch 与数据库 UoW 不是同一原子事务。
--   execute_blue_green_switch 在 UoW 内调用 backend.commit_switch(对外部存储
--   产生不可逆副作用),然后才 INSERT rollback_target / UPSERT phase / INSERT
--   audit event。若进程在 backend.commit_switch 成功后、UoW commit 前崩溃:
--     - SQLite 文件已 rename(staging → active)
--     - CRDB routing 已切换到新 schema
--     - 但数据库内无 rollback_target / phase 仍为 await_approval / 无审计事件
--   重启后系统认为 "未切换",但实际生产数据已切到 staging — 状态不可恢复。
--
-- 整改方案(R67 P1-06):
--   1. 在任何 backend.commit_switch 前,先持久化 prepare intent(独立事务)
--      记录:operation_id / switch_version(fencing token) / previous_version /
--      approval_id / mfa_receipt_id / manifest_digest / status=preparing / expires_at
--   2. 每个 backend.commit_switch 成功后,立即持久化 backend receipt(独立事务)
--      记录:operation_id / switch_version / datasource / previous_target /
--      new_target / switched_at / received_at / backend_type
--      唯一约束:(operation_id, datasource) — 防止重复 receipt
--   3. UoW 提交后,更新 prepare intent status=committed(标记整体完成)
--   4. 进程重启时由 reconciler 扫描 status IN ('preparing','prepared','committing')
--      的 intent,根据 backend receipts 决策:
--      - 所有 datasource 都有 receipt → 完成操作(补写 rollback_target / phase /
--        audit event),intent status=committed
--      - 部分 datasource 有 receipt → 回滚已切换的 backend,intent status=failed
--      - 无 receipt → 无外部副作用,intent status=rolled_back(无需回滚)
--
-- 本 migration 创建两张表:
--   1. restore_switch_intents       — prepare intent + fencing token + 状态机
--   2. restore_backend_receipts     — 每个 backend 的 switch receipt(独立提交)
--
-- 表结构说明:
--   restore_switch_intents.status 状态机:
--     preparing   — intent 已持久化,尚未开始 backend.commit_switch
--     prepared    — 所有 backend.prepare_switch 成功(可选阶段,允许跳过)
--     committing  — 部分或全部 backend.commit_switch 已成功(检查 receipts)
--     committed   — UoW 已提交,操作完成(reconciler 不再处理)
--     failed      — 切换失败或 reconciler 决策回滚(终态)
--     rolled_back — 无外部副作用,reconciler 标记为已回滚(终态)
--
--   restore_backend_receipts 唯一约束 (operation_id, datasource):
--     防止同 operation 的同 datasource 重复写入 receipt。若 backend.commit_switch
--     被重试(如网络抖动),receipt 已存在则 INSERT OR IGNORE 跳过。
--
-- ALTER TABLE ADD COLUMN 幂等性:migrate.py 的 _should_skip_statement 预检
-- 会跳过已存在的列(等价 ADD COLUMN IF NOT EXISTS),故重复执行无副作用。
-- 表使用 CREATE TABLE IF NOT EXISTS,幂等可重复执行。
--
-- IMPORTANT: 本 migration 由 migrate.py 在单个 BEGIN IMMEDIATE 事务中执行;
-- 任一语句失败将整体 ROLLBACK。本 migration 通过 _migrations_applied 仅执行一次。

-- Step 1: restore_switch_intents 表 — prepare intent + fencing token + 状态机
CREATE TABLE IF NOT EXISTS restore_switch_intents (
    operation_id        TEXT PRIMARY KEY,
    switch_version      TEXT NOT NULL,                 -- fencing token (UUID,单调)
    previous_version    TEXT NOT NULL,                 -- 切换前 active 版本
    approval_id         TEXT NOT NULL,                 -- 绑定的 approval capability
    mfa_receipt_id      TEXT NOT NULL,                 -- 绑定的 MFA capability
    manifest_digest     TEXT NOT NULL,                 -- 绑定的 payload (防篡改)
    prepared_by         TEXT NOT NULL DEFAULT '',      -- hostname:pid
    prepared_at         TEXT NOT NULL,                 -- ISO8601 prepare 时间
    expires_at          TEXT NOT NULL,                 -- TTL(防止永久卡住)
    status              TEXT NOT NULL DEFAULT 'preparing'
                        CHECK (status IN (
                            'preparing', 'prepared', 'committing',
                            'committed', 'failed', 'rolled_back'
                        )),
    reconciled_at       TEXT,                          -- reconciler 处理时间(终态时)
    reconcile_decision  TEXT,                          -- completed/rolled_back/failed
    reconcile_reason    TEXT,                          -- 决策原因(诊断用)
    FOREIGN KEY (operation_id) REFERENCES restore_operations(operation_id)
);

-- Step 2: restore_backend_receipts 表 — 每个 backend 的 switch receipt
-- 在 backend.commit_switch 成功后立即写入(独立 commit,非 UoW)
-- reconciler 检查所有 datasource 的 receipt 是否齐全
CREATE TABLE IF NOT EXISTS restore_backend_receipts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id        TEXT NOT NULL,
    switch_version      TEXT NOT NULL,                 -- fencing token (绑定 intent)
    datasource          TEXT NOT NULL,                 -- 'crdb' / 'sqlite' / 'relay_sqlite'
    previous_target     TEXT NOT NULL,                 -- 切换前 active target
    new_target          TEXT NOT NULL,                 -- 切换后 active target(staging)
    switched_at         TEXT NOT NULL,                 -- backend 报告的切换时间
    received_at         TEXT NOT NULL,                 -- receipt 写入时间(独立 commit)
    backend_type        TEXT NOT NULL,                 -- 'SQLiteRestoreBackend' / 'CRDBRestoreBackend'
    -- 唯一约束: 同 operation + datasource 只能有一个 receipt(防重复)
    UNIQUE(operation_id, datasource),
    FOREIGN KEY (operation_id) REFERENCES restore_operations(operation_id)
);

-- Step 3: 索引(按 status / expires_at / switch_version 查询)
CREATE INDEX IF NOT EXISTS idx_restore_switch_intents_status
ON restore_switch_intents(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_restore_switch_intents_expires_at
ON restore_switch_intents(expires_at);
CREATE INDEX IF NOT EXISTS idx_restore_backend_receipts_operation_id
ON restore_backend_receipts(operation_id);
CREATE INDEX IF NOT EXISTS idx_restore_backend_receipts_switch_version
ON restore_backend_receipts(switch_version);
CREATE INDEX IF NOT EXISTS idx_restore_backend_receipts_datasource
ON restore_backend_receipts(operation_id, datasource);
