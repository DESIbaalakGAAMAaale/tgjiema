-- ============================================================
-- 关闭 CRDB 行级 TTL job（每小时消耗数百万 RU 的元凶）
-- 改用 Python 端清理（见 database/cache.py _flush_decode_log_buffer_loop）
-- ============================================================

-- 1. 立即停掉现有 TTL job
ALTER TABLE decode_logs RESET (ttl_expiration_expression, ttl_job_cron);
ALTER TABLE jobs       RESET (ttl_expiration_expression, ttl_job_cron);

-- 2. 验证 TTL 已关闭（应返回 0 行）
SHOW JOBS;
-- 找 description 包含 "ttl for tgbot.public.decode_logs" 或 "ttl for tgbot.public.jobs"
-- 还在跑的 TTL job 用 CANCEL JOB <job_id> 手动取消

-- 3. 一次性清理 7 天前数据（替代原 TTL job 第一次跑的动作，可选）
-- DELETE FROM decode_logs WHERE request_time < (CURRENT_TIMESTAMP() - INTERVAL '7 days');
-- DELETE FROM jobs       WHERE created_at  < (CURRENT_TIMESTAMP() - INTERVAL '7 days') AND status IN ('done', 'failed');

-- 4. 验证 RU 消耗
-- Metrics → SQL Queries → 1 小时 RU 消耗应从 ~300 万降到 < 50 万
