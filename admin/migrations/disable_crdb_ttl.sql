-- ============================================================
-- 关闭 CRDB 行级 TTL job（每小时消耗数百万 RU 的元凶）
-- 改用 Python 端清理（见 database/cache.py _flush_decode_log_buffer_loop）
-- ============================================================

-- 1. 把过期时间延长到 100 年，cron 改 @yearly（CRDB 22.1+ 不支持纯 RESET ttl，必须 SET）
ALTER TABLE decode_logs SET (ttl_expiration_expression = 'CAST(request_time AS TIMESTAMPTZ) + INTERVAL ''100 years''', ttl_job_cron = '@yearly');
ALTER TABLE jobs       SET (ttl_expiration_expression = 'CAST(created_at  AS TIMESTAMPTZ) + INTERVAL ''100 years''', ttl_job_cron = '@yearly');

-- 2. 验证：再 SHOW CREATE TABLE 看 TTL 行的 cron 是否已变为 @yearly
-- SHOW CREATE TABLE decode_logs;
-- SHOW CREATE TABLE jobs;

-- 3. 验证 RU 消耗
-- Metrics → SQL Queries → 1 小时 RU 消耗应从 ~300 万降到 < 50 万
