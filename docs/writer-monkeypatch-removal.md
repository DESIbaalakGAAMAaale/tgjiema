# R39 P1-10 — Writer Monkey-patch 移除迁移计划

## 背景

R39 终审发现,`database/cache_store.py` 的 Writer 事务模式仍使用 monkey-patch 替换 `aiosqlite.Connection.commit` 为 no-op,以阻止业务方法内部提前 commit。这种实现:
- **脆弱**: monkey-patch 替换实例方法,异常路径下可能不恢复(已有 R35 P1-2 修复,但仍需手动管理)
- **影响并发**: 同一连接上的其他协程在 monkey-patch 生效期间也会受影响
- **代码复杂**: 80+ 处 `if self._in_writer_tx:` 分支判断,增加维护负担
- **隐式契约**: 业务方法依赖 `_in_writer_tx` 标志决定是否 commit,属于隐式契约

## 当前实现

```python
# cache_store.py
async def begin_writer_tx(self):
    self._in_writer_tx = True
    self._original_commit = self._db.commit
    async def _noop_commit():
        pass
    self._db.commit = _noop_commit  # ← monkey-patch
    await self._db.execute("BEGIN IMMEDIATE")

async def commit_writer_tx(self):
    await self._db.execute("COMMIT")
    self._db.commit = self._original_commit  # ← 恢复
    self._in_writer_tx = False
```

业务方法中:
```python
async def upsert_user_local(self, record):
    await self._db.execute("INSERT OR REPLACE INTO users_local ...")
    if not self._in_writer_tx:  # ← 80+ 处此判断
        await self._db.commit()
```

## 迁移目标

迁移到**专用 connection + 明确 transaction-aware Command**:

1. DBWriter 使用专用 `aiosqlite.Connection`(不与 CacheStore 共享)
2. 业务方法不再调用 `self._db.commit()`(事务边界由 Writer 控制)
3. 移除 `_in_writer_tx` 标志和所有 `if self._in_writer_tx:` 分支
4. 使用 `async with store.writer_transaction():` 上下文管理器(已存在,推荐用法)

## 迁移步骤

### 阶段 1: 引入 TransactionContext(不破坏现有代码)

1. 新增 `TransactionContext` 类,封装 BEGIN IMMEDIATE / COMMIT / ROLLBACK
2. `writer_transaction()` 上下文管理器改为使用 TransactionContext(不 monkey-patch)
3. 新增的业务方法直接使用 TransactionContext,不检查 `_in_writer_tx`

### 阶段 2: 迁移业务方法(分批)

1. 将 80+ 处 `if self._in_writer_tx:` 分支改为无条件不 commit(由 Writer 控制)
2. 每批迁移后运行测试验证
3. 迁移顺序: 心跳 → 配额 → file_records → codes → cells → jobs → outbox

### 阶段 3: 移除 monkey-patch

1. `begin_writer_tx` 不再替换 `self._db.commit`
2. `commit_writer_tx` / `rollback_writer_tx` 只执行 COMMIT/ROLLBACK
3. 删除 `_in_writer_tx` 标志
4. 删除所有 `if self._in_writer_tx:` 分支
5. 全量测试验证

### 阶段 4: 验证

- `tests/test_redis_writer.py` — Writer 基本功能
- `tests/test_redis_writer_consistency.py` — 一致性
- `tests/test_redis_writer_integration.py` — 集成
- `tests/test_r35_batch2_writer_dlq.py` — DLQ
- 崩溃恢复测试(COMMIT 前/后/XACK 前崩溃)

## 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 迁移期间双模式兼容 | `_in_writer_tx` 标志保留,新方法忽略它,旧方法继续用 |
| 业务方法忘记迁移 | 白名单 `_ALLOWED_METHODS` 逐个迁移,迁移一个移除一个 |
| 事务嵌套问题 | SQLite 不支持嵌套事务,Writer 模式下禁止业务方法内部 BEGIN |
| 测试覆盖不足 | 分批迁移,每批一个 PR + 测试 |

## 当前状态

- **已完成**: 在 `begin_writer_tx` 添加 `TODO: R39 P1-10 待迁移` 注释
- **待执行**: 阶段 1-4 的代码迁移(预计 2-3 个迭代周期)
- **不阻塞商用**: monkey-patch 已有 R35 P1-2 异常安全性修复,功能正确,仅架构脆弱

## 相关文件

- `database/cache_store.py` — `begin_writer_tx()` / `commit_writer_tx()` / `rollback_writer_tx()` / `writer_transaction()`
- `database/db_writer.py` — `DBWriter._execute_atomic()` 调用 begin/commit/rollback
- `tests/test_redis_writer*.py` — Writer 测试套件
