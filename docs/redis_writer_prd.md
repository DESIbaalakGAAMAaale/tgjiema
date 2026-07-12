# PRD: Redis + Writer 进程架构升级

项目名称: `tgjiema_redis_writer`
语言: Python 3.12
技术栈: asyncio + redis.asyncio + aiosqlite + systemd

## 一、产品目标

1. **彻底消除 SQLite 'database is locked' 锁冲突**: 7 个 bot 进程并发写入不再产生锁等待,写延迟从 5-50ms 降至 <0.1ms
2. **保持数据强一致性**: Writer 进程串行落盘 SQLite,写入零丢失;Redis 仅作缓冲,不作为权威存储
3. **零调用方改动**: 所有 bot 进程的 `cache_store` 调用代码保持不变,只改 `cache_store.py` 内部实现

## 二、用户故事

1. 作为系统管理员,我希望 `journalctl -u tgjiema-mon` 不再刷屏 `database is locked` ERROR,这样我可以快速定位真实问题
2. 作为开发者,我希望重构后所有 bot 进程的代码不需要修改,这样我可以低风险上线
3. 作为运维人员,我希望 Writer 进程崩溃后自动重启且 Redis 队列缓冲不丢数据,这样系统可自愈
4. 作为用户,我希望解码和投递延迟不因数据库锁等待而增加,这样体验更流畅
5. 作为开发者,我希望新增的 db_writer 服务遵循现有 systemd 独立服务模式,这样部署运维一致

## 三、需求池

### P0(必须实现)

| ID | 需求 | 验收标准 |
|---|---|---|
| P0-1 | Redis 写入缓冲层 | 所有写操作先入 Redis Queue,`LPUSH` <0.1ms 返回 |
| P0-2 | Writer 进程串行落盘 | 独立 systemd 服务,`BRPOP` 消费 Redis 队列,串行写 SQLite |
| P0-3 | 双写一致性保证 | Writer 写完 SQLite 后 `DEL` Redis 对应 key,以 SQLite 为权威 |
| P0-4 | 读操作透明降级 | 热数据读 Redis 缓存,未命中回退 SQLite;关键数据(CAS/配额)直读 SQLite |
| P0-5 | 零调用方改动 | 19 个引用 cache_store 的文件无需修改任何代码 |
| P0-6 | Writer 单点容错 | systemd `Restart=always` + Redis 队列积压监控告警 |
| P0-7 | 信号优雅关闭 | Writer 处理 SIGTERM:消费完当前消息后退出,不丢失 |
| P0-8 | Redis 不可用降级 | REDIS_URL 为空时自动降级到现有 SQLite 直写模式 |

### P1(应该实现)

| ID | 需求 | 验收标准 |
|---|---|---|
| P1-1 | CAS 语义正确迁移 | `mark_local_job_dispatched` / `try_consume_quota` 用 Redis 原子命令实现 |
| P1-2 | 事务性操作保证 | `batch_update_cells_local` / `delete_cell_local` 用 Redis MULTI/EXEC 或 Lua 脚本 |
| P1-3 | 跨进程通知迁移 | 5 张 notify 表改为 Redis Pub/Sub 或 List,降低 SQLite 写入 |
| P1-4 | 队列积压监控 | mon_bot 监控 Redis 队列长度,超过 1000 告警 |
| P1-5 | 灰度开关 | `.env` 新增 `WRITER_MODE=redis|sqlite`,可随时切回旧模式 |

### P2(可以实现)

| ID | 需求 | 验收标准 |
|---|---|---|
| P2-1 | 计数器迁移 | `counter_snapshot` 用 Redis INCR,天然原子 |
| P2-2 | TTL 缓存迁移 | `ttl_cache` 用 Redis 原生 TTL,代码简化 |
| P2-3 | 部署脚本更新 | `deploy_vps_per_bot.sh` 自动注册 db_writer 服务 |

## 四、UI 设计草案

无 UI 变化。管理员通过 `journalctl` 和 admin 面板 `/health-page` 观察:
- 新增 db_writer 服务状态行
- 新增 Redis 队列积压指标

## 五、待确认问题

1. Redis 部署方式:复用现有 `redis-server`(apt 安装)还是 Docker?(倾向复用现有)
2. db_writer 是否需要注册到 `run_all.py` 的多进程模式?(倾向是,保持一致)
3. 是否需要 Redis 持久化(AOF)?(倾向开启 `appendfsync everysec`,最多丢 1 秒)
4. 队列积压告警阈值:1000 还是 5000?(倾向 1000,保守起步)
