# R38 P1-8: Redis ACL 配置与启用说明

## 背景

R38 P1-8 要求 docker-compose.yml 中的 Redis 服务**实际启用 ACL**(Access Control List),
而非仅文档描述。启用 ACL 后每个服务使用独立用户名/密码连接 Redis,遵循最小权限原则。

---

## 1. ACL 文件

文件位置: `config/redis/users.acl`

### 用户清单

| 用户 | 用途 | 密码环境变量 | 权限 |
| ---- | ---- | ------------ | ---- |
| `default` | 默认用户 | — | **off**(禁用,不允许连接) |
| `db_writer` | Redis Stream 消费 + kv_store | `REDIS_PASSWORD_DB_WRITER` | `+@all -@dangerous` |
| `up_bot` | 上传 Bot | `REDIS_PASSWORD_UP` | `+@all -@dangerous` |
| `idx_bot` | 解码 Bot | `REDIS_PASSWORD_IDX` | `+@all -@dangerous` |
| `dsp_bot` | 派送 Bot | `REDIS_PASSWORD_DSP` | `+@all -@dangerous` |
| `mon_bot` | 监控 Bot | `REDIS_PASSWORD_MON` | `+@all -@dangerous` |
| `admin_bot` | 管理 Bot | `REDIS_PASSWORD_ADMIN_BOT` | `+@all -@dangerous` |
| `admin` | Web 后台 | `REDIS_PASSWORD_ADMIN` | `+@all -@dangerous` |

### 权限说明

- `+@all`: 允许所有命令分类
- `-@dangerous`: 禁止危险命令(flushall/flushdb/keys/config/debug/shutdown/monitor/client)
- `~*`: 允许访问所有 key
- `&*`: 允许订阅所有 channel

---

## 2. docker-compose.yml 配置

Redis 服务挂载 ACL 文件并添加 `--aclfile` 参数:

```yaml
redis:
  image: redis:7-alpine
  command: redis-server --appendonly yes --appendfsync everysec --maxmemory-policy noeviction --aclfile /etc/redis/users.acl
  volumes:
    - redis_data:/data
    - ./config/redis/users.acl:/etc/redis/users.acl:ro  # R38 P1-8: 挂载 ACL 文件
```

---

## 3. .env.shared 配置

各服务的 `REDIS_URL` 需包含用户名和密码:

```env
# .env.shared(公共配置,不含 secrets)
# Redis 连接 URL 格式: redis://<user>:<password>@redis:6379/0

# db_writer
REDIS_URL_DB_WRITER=redis://db_writer:db_writer_changeme_password@redis:6379/0

# up_bot
REDIS_URL_UP=redis://up_bot:up_bot_changeme_password@redis:6379/0

# idx_bot
REDIS_URL_IDX=redis://idx_bot:idx_bot_changeme_password@redis:6379/0

# dsp_bot
REDIS_URL_DSP=redis://dsp_bot:dsp_bot_changeme_password@redis:6379/0

# mon_bot
REDIS_URL_MON=redis://mon_bot:mon_bot_changeme_password@redis:6379/0

# admin_bot
REDIS_URL_ADMIN_BOT=redis://admin_bot:admin_bot_changeme_password@redis:6379/0

# admin
REDIS_URL_ADMIN=redis://admin:admin_changeme_password@redis:6379/0
```

**注意**: 密码应使用强随机字符串(如 `openssl rand -base64 32`),上述 `changeme_password` 仅为示例。

---

## 4. 密码轮转流程

1. 生成新密码: `openssl rand -base64 32`
2. 更新 `config/redis/users.acl` 中的密码
3. 更新 `.env.shared` 中对应的 `REDIS_URL_*`
4. 重启 Redis: `docker compose restart redis`
5. 重启所有依赖服务: `docker compose up -d`

---

## 5. 验证 ACL 生效

```bash
# 1. 验证 default 用户被禁用
docker exec tgjiema-redis redis-cli -a "" ping
# 应返回: AUTH failed 或 NOPERM

# 2. 验证 db_writer 用户可连接
docker exec tgjiema-redis redis-cli --user db_writer -a "db_writer_changeme_password" ping
# 应返回: PONG

# 3. 验证 db_writer 不能执行危险命令
docker exec tgjiema-redis redis-cli --user db_writer -a "db_writer_changeme_password" flushall
# 应返回: NOPERM (db_writer 无权限)
```

---

## 6. 本地开发(无 ACL)

本地开发不使用 Docker 时,Redis 可不启用 ACL(使用默认配置):

```bash
# 本地 Redis 无 ACL
redis-server --appendonly yes
# REDIS_URL=redis://localhost:6379/0 (无用户名密码)
```

生产环境**必须**启用 ACL。
