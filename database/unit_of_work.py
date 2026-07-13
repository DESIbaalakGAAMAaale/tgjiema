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
        # 显式 BEGIN: 开启事务(SQLite 默认 autocommit=off,显式 BEGIN 更清晰)
        try:
            await self._tx.execute("BEGIN")
        except Exception as e:
            # SQLite 在已处于事务中时再 BEGIN 会报 "cannot start a transaction within a transaction"
            # 这种情况说明上层已有事务,直接复用即可(不重新 BEGIN,也不主动 COMMIT)
            logger.debug(f"[UnitOfWork] BEGIN 失败(可能已处于事务中,复用现有事务): {e}")
        self._active = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if not self._active:
            return
        self._active = False
        if exc_type is not None:
            # 异常: 回滚
            try:
                await self._tx.rollback()
                logger.debug(
                    f"[UnitOfWork] 事务已回滚(异常={exc_type.__name__}: {exc})"
                )
            except Exception as rollback_err:
                logger.warning(f"[UnitOfWork] rollback 失败: {rollback_err}")
            return
        # 正常退出: 提交
        try:
            await self._tx.commit()
        except Exception as commit_err:
            logger.error(f"[UnitOfWork] commit 失败,尝试 rollback: {commit_err}")
            try:
                await self._tx.rollback()
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
