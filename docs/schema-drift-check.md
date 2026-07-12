# R39 P1-3 / P1-4 — 自动 Schema Diff 流程

## 背景

R39 终审发现,migration_runner 的 `_verify_schema_post_migration()` 只验证 `rotation_config` / `decode_logs` / `jobs` 三张表,未覆盖新增 tombstone 列、结构化 job 列、关键索引和全部核心表。此外,DDL_STATEMENTS 中定义的列与 file_records / backup_schema 中的字段存在漂移风险(如 note / updated_at / file_ttl_days / collection 等字段),仅靠手工同步三份定义无法保证一致。

## 整改方案

### 1. 全量 schema 验证(P1-3)

`services/migration_runner.py` 新增 `_extract_expected_schema(ddl_statements)`:
- 用正则解析 `DDL_STATEMENTS` 中所有 `CREATE TABLE IF NOT EXISTS` 语句
- 提取表名、列名、数据类型、nullable、PRIMARY KEY
- 返回 `dict[table_name, dict[column_name, dict]]`

`_verify_schema_post_migration(client)` 改造:
- 不再硬编码 3 张表,改为从 `DDL_STATEMENTS` 自动解析所有表名
- 对每张表查询 `information_schema.columns` 验证列存在
- 验证列类型(nullable)一致性
- 验证 PK 列(标记为 NOT NULL)
- 任一表/列缺失 → `drift_found = True` → 返回 False → 禁止写 ddl_version

### 2. 字段漂移检查(P1-4)

在 `_verify_schema_post_migration()` 中:
- 对比 DDL_STATEMENTS 定义的列 vs `information_schema.columns` 实际列
- DDL 中存在但 information_schema 缺失的列 → 记录 ERROR + `drift_found = True`
- 列类型差异(DDL vs 实际)→ 记录 WARNING(不阻断,因 CRDB/PG 类型名有别名差异)
- 漂移时返回 False,不写 ddl_version

### 3. 验证流程

```
migration_runner.run_migration()
  ├─ 执行 DDL_STATEMENTS
  ├─ 执行 MIGRATION_STATEMENTS
  ├─ 设置 TTL
  ├─ _verify_schema_post_migration()  ← R39 P1-3/P1-4 全量验证
  │   ├─ _extract_expected_schema(DDL_STATEMENTS)  ← 解析期望 schema
  │   ├─ 验证所有表存在(information_schema.tables)
  │   ├─ 验证每张表的列(information_schema.columns)
  │   ├─ 验证列类型(nullable)
  │   └─ 漂移 → return False
  └─ schema_ok = True → 写 ddl_version
     schema_ok = False → 不写版本(下次启动重新迁移)
```

## 兜底策略

若无法导入 `DDL_STATEMENTS`(如模块导入异常),退回到 `_verify_minimal_tables()` 验证 3 张核心表(rotation_config / decode_logs / jobs),保证向后兼容。

## 扩展建议

未来可增加:
- 唯一索引验证(`pg_indexes` / `pg_constraint`)
- 默认值验证
- schema hash(对所有表/列/类型计算 hash,版本写入时一并存储,启动时快速比对)
- 自动生成 schema 漂移报告(JSON,供 admin 展示)

## 相关文件

- `services/migration_runner.py` — `_extract_expected_schema()` / `_verify_schema_post_migration()` / `_verify_minimal_tables()` / `_split_top_level_commas()`
- `database/session.py` — `DDL_STATEMENTS` / `DDL_VERSION` / `MIGRATION_STATEMENTS`
