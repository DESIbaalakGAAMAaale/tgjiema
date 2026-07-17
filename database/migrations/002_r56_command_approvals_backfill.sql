-- R59 P1: SQLite 版本化迁移 — R56 旧 schema 补列
--
-- 背景: R56 首次创建 command_approvals 表时仅包含基础列
--   (id, action_id, approver_id, approval_type, mfa_receipt, approved_at, metadata_json)
-- R58 P0-2 新增强绑定列: decision, request_hash, permission, expires_at, consumed_at, revoked_at
--
-- 本 migration 对已存在的 R56 旧表执行 ALTER TABLE ADD COLUMN 补列。
-- 对于全新安装(001 已创建完整 schema),这些 ALTER 会因列已存在而失败,
-- migrate.py 会捕获并忽略 "duplicate column" 错误(幂等设计)。
--
-- 注意: SQLite ALTER TABLE ADD COLUMN 不支持 IF NOT EXISTS 语法,
-- 重复添加列会抛出 "duplicate column name" 错误,由 migrate.py 白名单过滤。
--
-- DEFAULT 值说明:
--   decision     DEFAULT 'approved' — 旧记录视为已批准(向后兼容)
--   request_hash DEFAULT ''          — 旧记录无 hash(R59 P1 fail-closed 检查会拦截)
--   permission   DEFAULT ''          — 旧记录无权限快照
--   expires_at   DEFAULT ''          — 旧记录无过期时间(R59 P1 fail-closed 检查会拦截)
--   consumed_at  NULL                — 旧记录未消费
--   revoked_at   NULL                — 旧记录未撤销

ALTER TABLE command_approvals ADD COLUMN decision TEXT NOT NULL DEFAULT 'approved';
ALTER TABLE command_approvals ADD COLUMN request_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE command_approvals ADD COLUMN permission TEXT NOT NULL DEFAULT '';
ALTER TABLE command_approvals ADD COLUMN expires_at TEXT NOT NULL DEFAULT '';
ALTER TABLE command_approvals ADD COLUMN consumed_at TEXT;
ALTER TABLE command_approvals ADD COLUMN revoked_at TEXT;
