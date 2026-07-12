# R39 P2-5: 废弃代码清理计划

## 背景

R39 终审指出: "删除 send_queue、旧 resolver、旧 safe helper、旧 CRDB sync 等已废弃双轨代码。"

项目演进中保留了多版本兼容代码,形成"双轨"现象:
- 新代码已上线,旧代码仍存在但无人调用
- 增加维护成本、引入歧义、扩大攻击面

本文档列出所有待清理的废弃代码,按优先级分批删除。

---

## 1. 废弃代码清单

### 1.1 P0 — 高优先级(已完全替代,可立即删除)

| 文件/模块 | 废弃内容 | 替代实现 | 删除条件 |
| --------- | -------- | -------- | -------- |
| `database/session.py` | `send_queue` 表 DDL + D1Collection | `jobs` 表 + `database/redis_queue.py` | 确认无服务读写 send_queue |
| `database/__init__.py` | `get_send_queue_col` 导出 | `get_pending_jobs_count_local` 等 | 移除导出 + 调用方 |
| `database/session.py` | `_send_queue_col = D1Collection("send_queue")` | 无(jobs 表走 SQLite) | 确认无导入 |
| `database/session.py` | `MIGRATION_STATEMENTS` 中 send_queue 的 ALTER 语句 | 无 | 保留 DDL 用于历史迁移,ALTER 可删 |
| 旧 resolver(`services/delivery_resolver.py` 中已弃用的方法) | `resolve_delivery_channel_legacy()` 等 | `ReplicaAwareResolver` | 确认无调用 |

### 1.2 P1 — 中优先级(过渡期保留,有明确退役时间)

| 文件/模块 | 废弃内容 | 替代实现 | 删除条件 |
| --------- | -------- | -------- | -------- |
| `database/session.py` | `_legacy_run_ddl_and_bootstrap()` | `migration_runner.py` | 所有部署完成 migration_runner 升级后 |
| `bots/dsp_bot.py` | `_extract_replica_info()` 的旧 job fallback(batch_file_meta JSON 解析) | 结构化字段 `file_unique_id`/`group_id` | 所有 job 已升级为新格式 |
| `services/crdb_sync_service.py` | 旧 split-brain fallback(Redis 不可用时降级 SQLite KV) | P1-1 fail-closed | P1-1 已实施,可删除旧 fallback 代码 |

### 1.3 P2 — 低优先级(参考价值,可保留)

| 文件/模块 | 内容 | 处理建议 |
| --------- | ---- | -------- |
| `tests/test_r36_batch1_resolver.py` | `fuid-legacy-001` 测试用例 | 保留(测试历史 job 兼容性) |
| `tests/test_r37_batch3_p1.py` | `_legacy_run_ddl_and_bootstrap` 测试 | 保留到 legacy 函数删除后同步删除 |

---

## 2. 清理流程

### 2.1 验证无调用(Grep 确认)

删除前必须确认无任何代码引用:

```bash
# 检查 send_queue 调用
grep -rn "send_queue" --include="*.py" --exclude-dir=tests --exclude-dir=docs .
# 应只剩 DDL 定义和注释,无实际读写代码

# 检查 _legacy_run_ddl_and_bootstrap 调用
grep -rn "_legacy_run_ddl_and_bootstrap" --include="*.py" .
# 应只剩函数定义和 init_db 的 DB_AUTO_MIGRATE 分支

# 检查 get_send_queue_col 调用
grep -rn "get_send_queue_col" --include="*.py" .
# 应只剩 __init__.py 的导出
```

### 2.2 删除顺序

按依赖关系删除(被依赖的最后删):

1. **删除调用方**: 移除 `database/__init__.py` 中 `get_send_queue_col` 导出
2. **删除实例**: 移除 `database/session.py` 中 `_send_queue_col = D1Collection(...)`
3. **删除 DDL**: 保留 `CREATE TABLE send_queue` 用于历史迁移兼容,但移除 `ALTER TABLE send_queue` 的 MIGRATION_STATEMENTS(新部署不需要)
4. **删除旧 resolver 方法**: 移除 `services/delivery_resolver.py` 中已弃用的 `resolve_delivery_channel_legacy()` 等
5. **删除旧 safe helper**: 移除 `utils/` 中已弃用的 helper 函数

### 2.3 删除后验证

```bash
# 1. 运行全部测试
python -m pytest tests/ -v --tb=short

# 2. AST 语法检查
python -c "
import ast, sys
files = [
    'database/cache_store.py', 'database/db_writer.py',
    'database/session.py', 'services/crdb_sync_service.py',
    'services/delivery_resolver.py',
]
for f in files:
    ast.parse(open(f, encoding='utf-8').read())
    print(f'OK: {f}')
"

# 3. 启动测试(本地)
python -c "from database import init_db; import asyncio; asyncio.run(init_db())"

# 4. CI 通过
```

---

## 3. 数据迁移考虑

### 3.1 send_queue 历史数据

`send_queue` 表中可能残留历史未投递记录,删除前需确认:

```sql
-- 检查 send_queue 是否还有未处理记录
SELECT COUNT(*) FROM send_queue WHERE status = 'pending';
-- 若 > 0,需先迁移到 jobs 表或手动处理

-- 检查 jobs 表是否已完全接管
SELECT COUNT(*) FROM jobs WHERE status = 'pending';
```

若有残留:
1. 手动迁移到 `jobs` 表(通过脚本)
2. 确认 send_queue 表为空
3. 删除表 DDL(保留在 migration history 中供回滚)

### 3.2 batch_file_meta 兼容

`_extract_replica_info()` 的旧 job fallback 在所有 job 升级为新格式前不可删除:
- 检查 jobs 表中 `file_unique_id`/`group_id` 结构化字段是否已填充
- 若有 NULL 值,需通过脚本从 `batch_file_meta` JSON 回填

```sql
-- 检查未升级的 job
SELECT COUNT(*) FROM jobs WHERE file_unique_id IS NULL OR file_unique_id = '';
-- 若 > 0,需先回填
```

---

## 4. 删除后的回归测试

清理后需运行以下测试确保无回归:

```bash
# 1. 单元测试
python -m pytest tests/ -v --tb=short

# 2. 故障注入测试
python -m pytest tests/ -v -k "crash or fault or inject or recovery or restore or dlq or inbox"

# 3. 集成测试(若有)
python -m pytest tests/integration/ -v

# 4. 端到端验证(本地)
# 启动所有服务,完成上传→生成码→投递全流程
python run_all.py
```

---

## 5. 分批执行计划

| 批次 | 内容 | 预期时间 | 风险 |
| ---- | ---- | -------- | ---- |
| 1 | 删除 send_queue 相关代码(P0) | 1 天 | 低(已完全替代) |
| 2 | 删除旧 resolver 方法(P0) | 0.5 天 | 低 |
| 3 | 删除 crdb_sync 旧 fallback(P1,需 P1-1 完成后) | 0.5 天 | 中(需验证 fail-closed 生效) |
| 4 | 删除 _legacy_run_ddl_and_bootstrap(P1,需所有部署升级后) | 1 天 | 中(需 migration_runner 全覆盖) |
| 5 | 删除 batch_file_meta fallback(P1,需 job 全部升级后) | 0.5 天 | 中(需回填脚本) |

每批删除后:
- 运行全部测试
- 提交独立 PR(便于回滚)
- CI 通过后合并

---

## 6. 防止再次引入

### 6.1 CI 静态检查

在 CI 中添加规则,禁止新增对已删除模块的引用:

```yaml
    # R39 P2-5: 禁止引入已删除的废弃代码引用
    - name: Check for deprecated code references
      run: |
        python -c "
        import subprocess, sys
        # 已删除的模块/函数,不应再被引用
        FORBIDDEN = [
            'get_send_queue_col',
            'resolve_delivery_channel_legacy',
            # 添加新删除的项
        ]
        for item in FORBIDDEN:
            result = subprocess.run(
                ['grep', '-rn', item, '--include=*.py', '.'],
                capture_output=True, text=True,
            )
            # 排除 tests/docs/注释中的引用
            real_refs = [
                line for line in result.stdout.splitlines()
                if not line.startswith(('./tests/', './docs/'))
                and not line.strip().startswith('#')
            ]
            if real_refs:
                print(f'ERROR: 发现对已删除项 {item} 的引用:')
                for line in real_refs:
                    print(f'  {line}')
                sys.exit(1)
        print('R39 P2-5: 无废弃代码引用')
        "
```

### 6.2 代码审查清单

PR review 时检查:
- [ ] 新代码是否引用已废弃模块?
- [ ] 新功能是否复用了旧 helper 而非新 API?
- [ ] 是否引入了新的"双轨"代码?

---

## 7. 相关文件

- `database/session.py` — send_queue DDL + _legacy_run_ddl_and_bootstrap
- `database/__init__.py` — get_send_queue_col 导出
- `services/delivery_resolver.py` — 旧 resolver 方法
- `services/crdb_sync_service.py` — 旧 split-brain fallback(P1-1 已替代)
- `bots/dsp_bot.py` — _extract_replica_info 旧 job fallback
- `docs/writer-monkeypatch-removal.md` — Writer monkey-patch 移除计划(P1-10)
