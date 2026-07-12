# Redis 安全策略(R37 P2-3)

本文档说明 TG文件解码器 项目 Redis 的 ACL 策略、AOF 持久化、noeviction 内存策略。
**目标**: Stream 消息既不丢、也不被未授权客户端篡改。

---

## 1. ACL 用户策略

业务用 Redis 7+,启用 ACL 多用户隔离:

| 用户 | 权限 | 适用服务 | 命令白名单 |
| ---- | ---- | ------- | --------- |
| `tgjiema_writer` | 读 + 写 | db_writer / crdb_sync / up / idx / dsp / mon / admin_bot / admin | `+XADD +XREADGROUP +XACK +XLEN +XPENDING +XCLAIM +XTRIM +XINFO +XDEL -@all` |
| `tgjiema_reader` | 只读 | 业务 Bot 消费组(只读) | `+XREADGROUP +XINFO +XLEN +XPENDING -@all` |
| `default` | **禁用** | — | `off` |

**Key 命名空间**: 全部 ACL 用户限定 `~tgjiema:*` 前缀,
禁止读写其他前缀 key。

### 1.1 初始化脚本

`deploy_vps_per_bot.sh` 中的 `init_redis_acl()` 函数会:

1. 从 `.env` 读取 `REDIS_WRITER_PWD` / `REDIS_READER_PWD`,缺失则生成随机密码回写
2. `redis-cli ACL SETUSER tgjiema_writer on >PASSWORD ~tgjiema:* +... -@all`
3. `redis-cli ACL SETUSER tgjiema_reader on >PASSWORD ~tgjiema:* +... -@all`
4. `redis-cli ACL SETUSER default off` 禁用无密码默认用户
5. `redis-cli ACL SAVE` 持久化到 `/etc/redis/users.acl`

### 1.2 业务侧连接配置

`.env.shared` 应改写 `REDIS_URL` 为带 ACL 用户的 URL:

```
# db_writer / crdb_sync 用 writer
REDIS_URL=redis://tgjiema_writer:<pwd>@127.0.0.1:6379/0

# 业务 Bot 用 reader(可在 .env.secrets.<svc> 中覆盖)
REDIS_URL=redis://tgjiema_reader:<pwd>@127.0.0.1:6379/0
```

### 1.3 禁用危险命令

ACL 中 `-@all` 已禁用所有命令,**仅显式白名单放行**,
`FLUSHALL / FLUSHDB / CONFIG / DEBUG / SHUTDOWN / KEYS` 等危险命令均不可用。

---

## 2. AOF 持久化策略

### 2.1 配置

`/etc/redis/redis.conf` 关键项:

```conf
appendonly yes
appendfsync everysec
no-appendfsync-on-rewrite no
auto-aof-rewrite-percentage 100
auto-aof-rewrite-min-size 64mb
```

| 参数 | 值 | 理由 |
| ---- | -- | --- |
| `appendonly` | `yes` | 启用 AOF(比 RDB 更耐丢) |
| `appendfsync` | `everysec` | 每秒 fsync,最多丢 1 秒数据(性能与安全的平衡) |

### 2.2 R33 P1-3 要求

- **Stream 消息不丢**: AOF everysec 保证服务崩溃时丢失 ≤ 1 秒
- **崩溃恢复**: Redis 重启时自动 replay AOF,Stream 数据完整恢复

---

## 3. noeviction 内存策略

```conf
maxmemory-policy noeviction
# 可选: 设置最大内存(默认 0=不限制,VPS 上建议 256MB)
# maxmemory 256mb
```

### 3.1 选择 noeviction 的理由

| 策略 | 行为 | 是否适用 Stream |
| ---- | ---- | -------------- |
| `noeviction` | 内存满时写直接报错(不删数据) | ✅ 推荐 |
| `allkeys-lru` | 满时驱逐最久未用的 key | ❌ 会丢 Stream 消息 |
| `volatile-lru` | 仅驱逐带 TTL 的 key | ❌ Stream 不带 TTL 也会被驱逐 |

业务 Stream 消息一旦被驱逐,会导致:
- consumer group 读不到旧消息
- pending entries 丢失,无法 retry
- 队列"无声"丢消息,难以追溯

`noeviction` 在内存满时显式报错,触发告警,运维介入扩容或清理,
**永远不让 Redis 自行决定丢什么数据**。

### 3.2 内存监控

Prometheus exporter 暴露 `redis_pel_depth` 指标(详见 `docs/observability.md`),
配 `redis_memory_used_bytes / redis_memory_max_bytes > 0.85` 告警,
提前介入。

---

## 4. 网络隔离

### 4.1 仅监听本地

`/etc/redis/redis.conf`:

```conf
bind 127.0.0.1 ::1
protected-mode yes
port 6379
```

Redis 只绑定 loopback,外部网络无法访问。

### 4.2 Unix Socket(可选,更高安全)

```conf
unixsocket /run/redis/redis-server.sock
unixsocketperm 770
port 0
```

业务侧:
```
REDIS_URL=redis+socket:///run/redis/redis-server.sock
```

并加入 `tgjiema` 用户到 `redis` 组:

```bash
usermod -aG redis tgjiema
```

### 4.3 私网部署(多机集群)

如果 Redis 必须跨机部署:
- 放在单独私有子网(`10.x.x.x/24`)
- 安全组仅放行 tgjiema VPS 的源 IP
- 启用 TLS(`tls-port 6379` + `tls-cert-file / tls-key-file`)

---

## 5. 备份与审计

### 5.1 RDB + AOF 双备份

```bash
# 定期备份
cp /var/lib/redis/dump.rdb /backup/redis/dump-$(date +%F).rdb
cp /var/lib/redis/appendonly.aof /backup/redis/appendonly-$(date +%F).aof
```

备份文件加入 `db_backup` 服务的备份范围。

### 5.2 ACL 审计

定期执行:
```bash
redis-cli ACL WHOAMI          # 当前身份
redis-cli ACL LIST            # 所有用户(注意密码会脱敏)
redis-cli ACL GETUSER tgjiema_writer  # 查看权限
redis-cli ACL LOG             # 安全事件日志(权限拒绝等)
```

---

## 6. 引用

- `deploy_vps_per_bot.sh` → `init_redis_acl()` 自动初始化 ACL
- `docs/least-privilege.md` — 整体最小权限策略
- `docs/observability.md` — Redis 监控指标
