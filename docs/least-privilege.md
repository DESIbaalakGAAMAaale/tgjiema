# 最小权限分离(R37 P2-2)

本文档定义 TG文件解码器 各服务的运行账户、CRDB 数据库账号、R2 访问 key
的最小权限分离策略。目标:**单个服务被攻陷时,影响半径不超过其权限边界**。

---

## 1. systemd 服务运行账户

### 1.1 当前模型

| 服务名 | systemd unit | User= | 共用账户 |
| ------ | ------------ | ----- | ------- |
| up | `tgjiema-up.service` | `tgjiema` | ✅(共用) |
| idx | `tgjiema-idx.service` | `tgjiema` | ✅ |
| dsp | `tgjiema-dsp.service` | `tgjiema` | ✅ |
| mon | `tgjiema-mon.service` | `tgjiema` | ✅ |
| admin_bot | `tgjiema-admin_bot.service` | `tgjiema` | ✅ |
| admin | `tgjiema-admin.service` | `tgjiema` | ✅ |
| db_backup | `tgjiema-db_backup.service` | `tgjiema` | ✅ |
| db_writer | `tgjiema-db_writer.service` | `tgjiema` | ✅ |
| crdb_sync | `tgjiema-crdb_sync.service` | `tgjiema` | ✅ |
| migration | `tgjiema-migration.service`(oneshot) | `tgjiema` | ✅ |

**共用账户 `tgjiema`** 是系统账户(`useradd -r`,无登录 shell `/usr/sbin/nologin`,
家目录 `/opt/tgjiema`),不持有 root 权限。

### 1.2 推荐升级路径(可选,单租户高敏感场景)

按服务拆分独立账户 `tgjiema-<svc>`:

```bash
for svc in up idx dsp mon admin_bot admin db_backup db_writer crdb_sync migration; do
    useradd -r -s /usr/sbin/nologin -d /opt/tgjiema -M "tgjiema-${svc}"
    usermod -aG tgjiema "tgjiema-${svc}"
done
# 数据目录 setgid,允许组成员读写
chmod 2770 /opt/tgjiema/data
```

并在 systemd 单元中替换 `User=tgjiema` → `User=tgjiema-<svc>`。

**取舍**: 拆分后单服务进程只读自己 secrets,但
SQLite relay_pool.db / cache_store.db 跨服务读写需要 setgid 组,
配置复杂度上升。**对单租户 VPS 部署仍以共用 `tgjiema` + secrets 文件级隔离为主**。

### 1.3 systemd 沙箱(必须)

每个业务 Bot 单元均配置以下 `SystemCallFilter` / `Protect*` 选项:

```ini
[Service]
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/tgjiema/data /opt/tgjiema/logs
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
CapabilityBoundingSet=
AmbientCapabilities=
LockPersonality=true
RestrictRealtime=true
RestrictSUIDSGID=true
SystemCallFilter=@system-service
SystemCallFilter=~@privileged @resources @mount
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectProc=invisible
PrivateDevices=true
```

理由: 阻断 SETUID / mount / 内核模块加载等高权 syscall。

`deploy_vps_per_bot.sh` 的 `generate_service()` 已在 systemd 模板中
追加完整沙箱配置,无需运维手工编辑。

---

## 2. CRDB 数据库账号分离

CockroachDB Cloud 单集群内按职能划分 3 个独立用户:

| 用户 | 权限范围 | 用途 |
| ---- | ------- | --- |
| `tgjiema_migration` | `CREATE / ALTER / DROP / GRANT`(DDL) | migration oneshot 一次性执行 DDL/TTL |
| `tgjiema_sync` | `SELECT / INSERT / UPDATE / DELETE`(DML) | crdb_sync 服务,读写所有业务表 |
| `tgjiema_runtime` | `SELECT`(只读) | 业务 Bot(up/idx/dsp/mon/admin/admin_bot),只读本地 cache + 偶尔 SELECT CRDB |

### 2.1 创建 SQL

```sql
-- 在 admin 库内执行(用 root / admin 账号)
CREATE USER tgjiema_migration WITH PASSWORD '<MISSING_FROM_ENV>';
CREATE USER tgjiema_sync      WITH PASSWORD '<MISSING_FROM_ENV>';
CREATE USER tgjiema_runtime   WITH PASSWORD '<MISSING_FROM_ENV>';

-- migration: DDL 权限(单独 grant 一次)
GRANT CREATE,ALTER,DROP ON DATABASE tgjiema TO tgjiema_migration;
GRANT CREATE ON SCHEMA public TO tgjiema_migration;

-- crdb_sync: 全表 DML
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN DATABASE tgjiema TO tgjiema_sync;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO tgjiema_sync;

-- runtime: 只读
GRANT SELECT ON ALL TABLES IN DATABASE tgjiema TO tgjiema_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO tgjiema_runtime;

-- 业务 Bot 不再持有写权限(防止误操作 drop table)
REVOKE INSERT,UPDATE,DELETE ON ALL TABLES IN DATABASE tgjiema FROM tgjiema_runtime;
```

### 2.2 .env 配置

```
# migration service
COCKROACHDB_URL=postgresql://tgjiema_migration:<pwd>@<host>:26257/tgjiema?sslmode=verify-full

# crdb_sync service
COCKROACHDB_URL=postgresql://tgjiema_sync:<pwd>@<host>:26257/tgjiema?sslmode=verify-full

# 业务 Bot 不再使用 COCKROACHDB_URL(读 cache_store 即可)
# 若个别 Bot 必须直读 CRDB,单独配 read-only URL:
# COCKROACHDB_URL_RO=postgresql://tgjiema_runtime:<pwd>@...
```

### 2.3 验证

```bash
# migration 能 DDL,不能 SELECT 业务数据
psql "$COCKROACHDB_URL" -c "CREATE TABLE _perm_test(id int);"
psql "$COCKROACHDB_URL" -c "SELECT * FROM users;"  # 应失败

# runtime 能 SELECT,不能 INSERT
psql "$COCKROACHDB_URL_RO" -c "SELECT count(*) FROM users;"
psql "$COCKROACHDB_URL_RO" -c "INSERT INTO users VALUES (...);"  # 应失败
```

---

## 3. R2 访问 key 最小权限

Cloudflare R2 通过 **API Token + bucket 级 scoped access key** 实现最小权限:

| Token | 作用 bucket | 权限 | 用途 |
| ----- | ---------- | ---- | --- |
| `tgjiema_db_backup` | `tgjiema-backups` | Object Read + Write | db_backup 服务上传 / 恢复 |
| `tgjiema_admin_export` | `tgjiema-backups` | Object Read | admin 后台下载 / 导出 |
| (其它 Bot) | (无) | (无) | 业务 Bot 不需要 R2 访问 |

### 3.1 创建步骤

1. Cloudflare Dashboard → R2 → Manage R2 API Tokens → Create API token
2. Token name: `tgjiema_db_backup`
3. Permissions: **Object Read & Write**
4. Specify bucket: `tgjiema-backups`(只勾这一个 bucket)
5. TTL: 365 天(到期前 7 天告警 + 轮换)
6. 创建后只保留 `Access Key ID` + `Secret Access Key`,保存到
   `.env.secrets.db_backup`(权限 600)

### 3.2 .env 配置

```
# .env.secrets.db_backup(只 db_backup 服务可见)
R2_ACCOUNT_ID=<account_id>
R2_ACCESS_KEY_ID=<db_backup_access_key>
R2_SECRET_ACCESS_KEY=<db_backup_secret>
R2_BUCKET_NAME=tgjiema-backups
R2_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
```

业务 Bot 的 secrets 文件**不包含** R2_* 变量,
即使 Bot 被攻陷也无法访问备份存储。

### 3.3 验证

```bash
# db_backup token 应只能访问指定 bucket
aws s3 --endpoint-url $R2_ENDPOINT ls s3://tgjiema-backups/   # OK
aws s3 --endpoint-url $R2_ENDPOINT ls s3://other-bucket/      # AccessDenied
```

---

## 4. 实施清单

- [x] systemd 服务用 `tgjiema` 系统账户(无登录 shell)
- [x] systemd 沙箱配置(NoNewPrivileges / ProtectSystem=strict / CapabilityBoundingSet=)
- [x] secrets 文件级隔离(`.env.secrets.<service>`)
- [x] .env 文件权限 600
- [ ] CRDB 数据库账号 3 分(待运维执行 SQL)
- [ ] R2 API token scoped 到 single bucket(待运维在 Cloudflare 后台创建)

前 4 项已通过 `deploy_vps_per_bot.sh` 自动化实施;
后 2 项需运维在外部系统(CRDB Cloud / Cloudflare 控制台)完成。

---

## 5. 引用

- `deploy_vps_per_bot.sh` — systemd 服务生成 + 沙箱配置
- `docs/SIGNING.md` — 制品签名验证
- `docs/redis-security.md` — Redis ACL 最小权限
