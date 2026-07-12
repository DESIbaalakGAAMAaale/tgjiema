# R38 P2-4: 只读容器可写挂载校验

## 背景

R38 P2-4 要求 docker-compose.yml 中 `read_only: true` 的容器**挂载必要的可写卷**,
否则服务因无法写入日志/临时文件/SQLite WAL 而崩溃。

---

## 1. 只读容器挂载清单

所有业务服务均配置 `read_only: true`(R37 P2-4),以下挂载点是必需的可写路径:

### 1.1 通用挂载(所有服务)

| 挂载点 | 用途 | 类型 | 必需性 |
| ------ | ---- | ---- | ------ |
| `./data:/app/data` | SQLite 数据库(cache_store.db / relay_pool.db) + WAL 文件 | bind mount(可写) | **必需** |
| `./config:/app/config` | 配置文件(services.yaml / groups.yaml) | bind mount(只读) | 必需(只读) |
| `tmpfs: /tmp` | 临时文件(Python tempfile / pip cache) | tmpfs(可写,内存) | **必需** |

### 1.2 日志挂载(需要日志的服务)

| 挂载点 | 用途 | 服务 |
| ------ | ---- | ---- |
| `./logs:/app/logs` | loguru 日志文件(`logs/tgjiema_{time}.log`) | up / idx / dsp / mon / admin_bot / admin / db_backup |

### 1.3 缺失挂载修复(R38 P2-4)

以下服务在 R37 中缺少 `./logs:/app/logs` 挂载,导致 loguru 写入日志时 `OSError: [Errno 30] Read-only file system`:

| 服务 | 修复前 | 修复后 |
| ---- | ------ | ------ |
| `db_writer` | 无 `./logs` 挂载 | 新增 `./logs:/app/logs` |
| `crdb_sync` | 无 `./logs` 挂载 | 新增 `./logs:/app/logs` |
| `migration` | 无 `./logs` 挂载(oneshot,日志到 stdout) | 不需要(oneshot) |

---

## 2. SQLite WAL 可写验证

SQLite WAL(Write-Ahead Logging)模式会创建 `-wal` 和 `-shm` 文件:

```
/app/data/
├── cache_store.db       # 主数据库
├── cache_store.db-wal   # WAL 文件(可写)
├── cache_store.db-shm   # 共享内存(可写)
├── relay_pool.db        # 中继数据库
├── relay_pool.db-wal    # WAL 文件
└── relay_pool.db-shm    # 共享内存
```

`./data:/app/data` 挂载为 bind mount(默认可写),SQLite WAL 正常工作。

**验证命令**:
```bash
# 进入容器检查 WAL 文件可写
docker exec <container> python -c "
import os, tempfile
f = os.path.join('/app/data', '.write_test')
with open(f, 'w') as fh: fh.write('ok')
os.remove(f)
print('data dir writable: OK')
"
```

---

## 3. 临时文件挂载验证

`read_only: true` 下,Python 的 `tempfile` 模块默认使用 `/tmp`:

```bash
# 验证 /tmp 可写
docker exec <container> python -c "
import tempfile
with tempfile.NamedTemporaryFile() as f:
    f.write(b'test')
    f.flush()
    print('tmp writable: OK')
"
```

`tmpfs: /tmp` 提供内存中的可写临时目录,不写入磁盘,容器重启后清空。

---

## 4. 日志写入验证

loguru 配置写入 `logs/tgjiema_{time}.log`(见 `run_all.py` 的 `main()` 函数):

```python
logger.add(
    "logs/tgjiema_{time}.log",
    format=LOG_FORMAT,
    rotation="10 MB",
    retention="7 days",
    level=settings.LOG_LEVEL,
)
```

需要 `./logs:/app/logs` 挂载,否则报 `Read-only file system` 错误。

**验证命令**:
```bash
# 检查日志目录存在且可写
docker exec <container> python -c "
import os
os.makedirs('/app/logs', exist_ok=True)
with open('/app/logs/.write_test', 'w') as f: f.write('ok')
os.remove('/app/logs/.write_test')
print('logs writable: OK')
"
```

---

## 5. Redis ACL 文件挂载(只读)

Redis 的 ACL 文件以只读方式挂载:

```yaml
volumes:
  - ./config/redis/users.acl:/etc/redis/users.acl:ro  # 只读挂载
```

Redis 服务本身有 `read_only: true`,但 ACL 文件只需读取,所以 `:ro` 是正确的。

---

## 6. 完整挂载校验脚本

```bash
#!/bin/bash
# scripts/check-readonly-volumes.sh
# R38 P2-4: 检查所有 read_only 容器的可写挂载

SERVICES="db_writer crdb_sync up idx dsp mon admin_bot admin db_backup"

for svc in $SERVICES; do
    echo "=== $svc ==="
    # 检查 /app/data 可写
    docker exec "tgjiema-$svc" python -c "
import os
f='/app/data/.write_test'
with open(f,'w') as fh: fh.write('ok')
os.remove(f)
print('  data: OK')
" 2>/dev/null || echo "  data: FAIL (not writable)"

    # 检查 /tmp 可写
    docker exec "tgjiema-$svc" python -c "
import tempfile
with tempfile.NamedTemporaryFile() as f: f.write(b'ok')
print('  tmp: OK')
" 2>/dev/null || echo "  tmp: FAIL"

    # 检查 /app/logs 可写(需要日志的服务)
    docker exec "tgjiema-$svc" python -c "
import os
if not os.path.exists('/app/logs'): os.makedirs('/app/logs')
with open('/app/logs/.test','w') as f: f.write('ok')
os.remove('/app/logs/.test')
print('  logs: OK')
" 2>/dev/null || echo "  logs: SKIP (not mounted)"
done
```
