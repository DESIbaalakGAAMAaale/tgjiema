-- R64 P0-03: 恢复编排状态机 + 蓝绿切换 + 限时回滚点持久化
--
-- 审计背景(R64 终审报告 P0-03: restore 仍可能形成跨数据源混合时间点):
--   原 writer 按 CRDB → cache SQLite → relay SQLite 顺序执行 restore。
--   前一数据源成功、后一数据源失败时,无法用普通事务回滚已经提交的另一个存储。
--   覆盖模式尤其可能先清空生产表再失败,造成 active 数据被破坏且不可恢复。
--
-- 整改方案(R64 P0-03):
--   1. 恢复只能写入全新的 staging CRDB database/schema 与新的 SQLite 文件,
--      禁止原地覆盖生产(蓝绿切换模型)
--   2. 每个数据源完成 schema/行数/主外键/业务守恒/抽样+全量 hash 和应用只读演练
--   3. 所有 staging 数据源均验证成功后,在维护窗口执行版本化蓝绿切换;
--      任何失败只销毁 staging,不影响 active 数据
--   4. 切换后保留旧版本作为限时回滚点;回滚也必须使用状态机和审计事件
--   5. nonce 不在真正写入前永久消费 — 采用 operation ledger:
--      验证后 reserved,成功切换后 consumed,失败后允许同 operation 安全重试
--      但禁止换 payload(防篡改)
--
-- 本 migration 创建三张表:
--   1. restore_operations      — 操作状态机持久化(每个阶段切换都写入)
--   2. restore_operation_events — 审计事件轨迹(phase_from→phase_to)
--   3. restore_rollback_targets — 蓝绿切换前的旧版本指针(限时回滚点)
--
-- 表结构说明:
--   restore_operations.datasource_states  JSON 字符串,记录每个数据源状态:
--     {"crdb": {"status": "pending|restored|validated|failed",
--               "rows": 0, "schema_ok": true, ...},
--      "sqlite": {...}, "relay_sqlite": {...}}
--   restore_operations.validation_summary  JSON 字符串,验证摘要:
--     {"schema": "ok", "row_count": "ok", "fk": "ok", "hash": "ok",
--      "business_invariant": "ok", "dry_run": "ok"}
--   restore_operations.phase  状态机当前阶段:
--     init | staging_provision | staging_restore | staging_validate |
--     await_approval | blue_green_switch | completed | failed | rolled_back
--   restore_operation_events.event_type  事件类型:
--     phase_transition | staging_provisioned | staging_restored |
--     staging_validated | approval_requested | switched | rolled_back |
--     failed | retried
--   restore_rollback_targets.active_pointer  JSON 字符串,旧版本指针:
--     {"crdb": {"database": "...", "schema": "..."},
--      "sqlite": {"path": "..."}, "relay_sqlite": {"path": "..."}}
--
-- ALTER TABLE ADD COLUMN 幂等性:migrate.py 的 _should_skip_statement 预检
-- 会跳过已存在的列(等价 ADD COLUMN IF NOT EXISTS),故重复执行无副作用。
-- 表使用 CREATE TABLE IF NOT EXISTS,幂等可重复执行。
--
-- IMPORTANT: 本 migration 由 migrate.py 在单个 BEGIN IMMEDIATE 事务中执行;
-- 任一语句失败将整体 ROLLBACK。本 migration 通过 _migrations_applied 仅执行一次。

-- Step 1: restore_operations 表 — 操作状态机持久化
CREATE TABLE IF NOT EXISTS restore_operations (
    operation_id        TEXT PRIMARY KEY,
    backup_id           TEXT NOT NULL,
    manifest_digest     TEXT NOT NULL,
    phase               TEXT NOT NULL DEFAULT 'init'
                        CHECK (phase IN (
                            'init', 'staging_provision', 'staging_restore',
                            'staging_validate', 'await_approval',
                            'blue_green_switch', 'completed', 'failed',
                            'rolled_back'
                        )),
    datasource_states   TEXT NOT NULL DEFAULT '{}',
    validation_summary  TEXT NOT NULL DEFAULT '{}',
    approval_id         TEXT,
    mfa_receipt_id      TEXT,
    switch_version      TEXT,
    previous_version    TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    created_by          TEXT NOT NULL DEFAULT ''
);

-- Step 2: restore_operation_events 表 — 审计事件轨迹
-- 每次 phase 转换、staging 操作、切换、回滚都写入一条事件记录
CREATE TABLE IF NOT EXISTS restore_operation_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id    TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    phase_from      TEXT,
    phase_to        TEXT NOT NULL,
    payload         TEXT NOT NULL DEFAULT '{}',
    trace_id        TEXT,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES restore_operations(operation_id)
);

-- Step 3: restore_rollback_targets 表 — 限时回滚点(蓝绿切换前的旧版本指针)
-- 切换成功后插入旧版本指针,过期后由 GC 清理
CREATE TABLE IF NOT EXISTS restore_rollback_targets (
    switch_version  TEXT PRIMARY KEY,
    operation_id    TEXT NOT NULL,
    active_pointer   TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES restore_operations(operation_id)
);

-- Step 4: 索引(按 operation_id / phase / switch_version 查询)
CREATE INDEX IF NOT EXISTS idx_restore_operations_backup_id
ON restore_operations(backup_id);
CREATE INDEX IF NOT EXISTS idx_restore_operations_phase
ON restore_operations(phase);
CREATE INDEX IF NOT EXISTS idx_restore_operation_events_operation_id
ON restore_operation_events(operation_id);
CREATE INDEX IF NOT EXISTS idx_restore_operation_events_event_type
ON restore_operation_events(event_type);
CREATE INDEX IF NOT EXISTS idx_restore_rollback_targets_operation_id
ON restore_rollback_targets(operation_id);
CREATE INDEX IF NOT EXISTS idx_restore_rollback_targets_expires_at
ON restore_rollback_targets(expires_at);
