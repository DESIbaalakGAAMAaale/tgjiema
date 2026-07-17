"""R40 P0-5: Unit of Work — 业务表 + audit_log + dirty_outbox 同事务原子提交。

背景:
  原实现中业务表 INSERT/UPDATE 先 commit,再单独调用 add_dirty_outbox(),
  且 add_dirty_outbox 失败仅 warning。这会导致:
    - 业务表已提交但 outbox 未写 → CRDB 同步丢失变更
    - audit_log 与主业务表分属不同事务,部分失败时数据不一致
  R40 P0-5 整改: 所有 R40 服务必须通过 UnitOfWork 把
    (业务表写入, audit_log 写入, dirty_outbox 写入) 包在同一 SQLite 事务内。

设计:
  - UnitOfWork 内部持有 CacheStore 的 aiosqlite.Connection(单例连接)
  - 进入上下文: BEGIN
  - 退出上下文(无异常): COMMIT
  - 退出上下文(有异常): ROLLBACK
  - 提供 execute() 透传到 self._db.execute()
  - 提供 connection 属性,可注入到 cache_store.add_dirty_outbox(connection=...)

用法:
    async with UnitOfWork() as uow:
        await uow.execute("INSERT INTO tasks ...", [...])
        await uow.execute("INSERT INTO audit_log ...", [...])
        await cache_store.add_dirty_outbox(
            "tasks", str(task_id), connection=uow.connection,
        )
    # 退出时自动 COMMIT(或异常时 ROLLBACK)

注意:
  - 在事务上下文中,业务代码不得再调用 store._db.commit() — 由 UnitOfWork 统一控制。
  - add_dirty_outbox(connection=...) 传入 uow.connection 时不会自动 commit,
    而是写入同一事务,失败抛异常让外层回滚(不再只 warning)。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from loguru import logger


class UnitOfWork:
    """R40 P0-5: 业务表 + audit_log + dirty_outbox 同事务原子提交的上下文管理器。

    内部维护 CacheStore 的 aiosqlite.Connection(单例),通过 BEGIN/COMMIT/ROLLBACK
    控制事务边界。可注入到 cache_store.add_dirty_outbox(connection=uow.connection)。
    """

    def __init__(self, store: Any = None):
        """初始化 UnitOfWork。

        Args:
            store: 可选的 CacheStore 实例。None 则在进入上下文时通过
                   get_cache_store() 获取单例。
        """
        self._store = store
        self._tx: Any = None  # aiosqlite.Connection
        self._active: bool = False
        # R61 P0-02: 事务所有权标志。
        # True=本 UnitOfWork 通过 BEGIN 开启了事务(退出时 COMMIT/ROLLBACK);
        # False=外层已有事务,本 UnitOfWork 用 SAVEPOINT 隔离(退出时 RELEASE/ROLLBACK TO,
        #   不擅自提交/回滚调用方事务)。
        # 旧实现用 catch-all `except Exception` 把所有 BEGIN 失败(锁竞争/连接损坏/I/O 错误)
        # 误判为"已处于事务中,复用",并随后无条件 COMMIT,会擅自提交调用方事务。
        self._owns_transaction: bool = False
        # R61 P0-02: 嵌套事务使用的 savepoint 名称(仅在 _owns_transaction=False 时生效)。
        self._savepoint_name: str = "uow_sp"

    async def __aenter__(self) -> "UnitOfWork":
        # 延迟导入避免循环依赖
        if self._store is None:
            from database.cache_store import get_cache_store
            self._store = get_cache_store()
        if not self._store or not self._store._db:
            raise RuntimeError(
                "[UnitOfWork] CacheStore 未初始化,无法开启事务"
            )
        self._tx = self._store._db
        # R61 P0-02: 通过 in_transaction 属性精确判断是否已处于外层事务中,
        # 而非把所有 BEGIN 异常当作"已处于事务中"复用(会误提交调用方事务)。
        # aiosqlite.Connection.in_transaction 委托到底层 sqlite3.Connection.in_transaction,
        # 返回 True 表示当前有一个未提交的事务(BEGIN 已发出但未 COMMIT/ROLLBACK)。
        already_in_tx = bool(getattr(self._tx, "in_transaction", False))
        if already_in_tx:
            # 外层已有事务: 不再 BEGIN,改用 SAVEPOINT 隔离本 UnitOfWork 的工作单元。
            # 退出时仅 RELEASE/ROLLBACK TO savepoint,绝不 COMMIT 调用方事务。
            self._owns_transaction = False
            await self._tx.execute(f"SAVEPOINT {self._savepoint_name}")
        else:
            # 不在事务中: 显式 BEGIN 开启新事务(本 UnitOfWork 拥有并负责提交/回滚)。
            # BEGIN 失败(锁竞争/连接损坏/I/O 错误)必须显式抛 AppError,
            # 不再像旧实现那样 catch-all 后静默"复用现有事务"。
            self._owns_transaction = True
            try:
                await self._tx.execute("BEGIN")
            except Exception as e:
                # 延迟导入避免循环依赖
                from services.error_codes import AppError, ErrorCodes
                raise AppError(
                    ErrorCodes.DB_CACHE_UNAVAILABLE,
                    params={
                        "component": "unit_of_work",
                        "reason": f"begin_failed: {type(e).__name__}: {e}",
                    },
                ) from e
        self._active = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self._active:
            return
        self._active = False
        if exc_type is not None:
            # 异常: 回滚
            # R61 P0-02: 仅当本 UnitOfWork 拥有事务时才 ROLLBACK 整个事务;
            # 否则只 ROLLBACK TO SAVEPOINT(不结束调用方事务)。
            try:
                if self._owns_transaction:
                    await self._tx.rollback()
                else:
                    await self._tx.execute(
                        f"ROLLBACK TO SAVEPOINT {self._savepoint_name}"
                    )
                logger.debug(
                    f"[UnitOfWork] 事务已回滚(owns={self._owns_transaction}, "
                    f"异常={exc_type.__name__}: {exc})"
                )
            except Exception as rollback_err:
                logger.warning(f"[UnitOfWork] rollback 失败: {rollback_err}")
            return
        # 正常退出: 提交
        # R61 P0-02: 仅当本 UnitOfWork 拥有事务时才 COMMIT;
        # 否则只 RELEASE SAVEPOINT(保留调用方事务由其自行提交)。
        try:
            if self._owns_transaction:
                await self._tx.commit()
            else:
                await self._tx.execute(
                    f"RELEASE SAVEPOINT {self._savepoint_name}"
                )
        except Exception as commit_err:
            logger.error(f"[UnitOfWork] commit 失败,尝试 rollback: {commit_err}")
            try:
                if self._owns_transaction:
                    await self._tx.rollback()
                else:
                    await self._tx.execute(
                        f"ROLLBACK TO SAVEPOINT {self._savepoint_name}"
                    )
            except Exception:
                pass
            raise

    async def execute(self, sql: str, params: Any = None):
        """在当前事务内执行 SQL,返回 cursor。

        Args:
            sql: SQL 语句
            params: 参数(tuple/list 或 None)

        Returns:
            aiosqlite.Cursor
        """
        if not self._active or self._tx is None:
            raise RuntimeError("[UnitOfWork] 事务未开启,无法执行 SQL")
        if params is None:
            return await self._tx.execute(sql)
        return await self._tx.execute(sql, params)

    async def executemany(self, sql: str, params_seq):
        """在当前事务内批量执行 SQL。"""
        if not self._active or self._tx is None:
            raise RuntimeError("[UnitOfWork] 事务未开启,无法执行 SQL")
        return await self._tx.executemany(sql, params_seq)

    async def fetchone(self, sql: str, params: Any = None):
        """在当前事务内查询单行。"""
        if params is None:
            cursor = await self._tx.execute(sql)
        else:
            cursor = await self._tx.execute(sql, params)
        return await cursor.fetchone()

    async def fetchall(self, sql: str, params: Any = None):
        """在当前事务内查询多行。"""
        if params is None:
            cursor = await self._tx.execute(sql)
        else:
            cursor = await self._tx.execute(sql, params)
        return await cursor.fetchall()

    @property
    def connection(self):
        """返回底层 aiosqlite.Connection,可传给 add_dirty_outbox(connection=...)。"""
        return self._tx

    @property
    def store(self):
        """返回关联的 CacheStore 实例。"""
        return self._store


@asynccontextmanager
async def transaction(store: Any = None):
    """便捷上下文管理器: 等价于 `async with UnitOfWork(store) as uow:`。

    用法:
        async with transaction() as tx:
            await tx.execute("INSERT ...")
            await cache_store.add_dirty_outbox("t", "pk", connection=tx)
    """
    async with UnitOfWork(store=store) as uow:
        yield uow
