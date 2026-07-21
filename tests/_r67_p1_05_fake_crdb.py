"""R67 P1-05: tests-only fake CRDB client — 支持 verify_authority_state 验证。

审计背景(R67 终审报告 P1-05):
    R66 P0-06 的 ``check_startup_readiness`` 只检查 SQLite ``sqlite_master``
    的 rollback target 表存在。R67 P1-05 要求对每个注册 backend 调用
    ``verify_authority_state()``,验证连接/schema/权限/CAS/版本一致性。

    现有测试 ``test_r66_p0_06_restore_no_degradation.py`` 使用 ``MagicMock()``
    作为 CRDB client,不支持 ``async with client.acquire() as conn:`` 模式,
    无法通过 ``verify_authority_state`` 的连接检查。

    本模块提供:
      - ``FakeCRDBClient``: 支持 acquire() async context manager
      - ``FakeCRDBConn``: 支持 execute/fetchone/fetchval/transaction
      - ``make_fake_crdb_client``: 工厂函数,返回配置好的 fake client

    fake client 的行为:
      - ``SELECT 1`` → 成功(connected=True)
      - ``information_schema.tables`` 查询 → 返回 0 张表(schema_present=True)
      - ``CREATE SCHEMA`` + ``INSERT`` + ``DROP SCHEMA`` → 成功(permissions_ok=True)
      - ``UPDATE ... WHERE`` → rowcount 正确(cas_capable=True)
      - ``restore_active_pointer`` 表查询 → 表不存在(current_version=None)

    通过参数可控制 fake 的行为,模拟各种故障场景:
      - ``connected=False``: 模拟连接失败
      - ``schema_present=False``: 模拟 schema 不可读
      - ``permissions_ok=False``: 模拟权限不足
      - ``cas_capable=False``: 模拟 CAS 不支持
      - ``current_version``: 模拟 active_pointer 表的版本

⚠️ 本模块仅存在于 tests/,生产代码(services/、bots/、admin/)不得引用。
"""
from __future__ import annotations

from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock


class _FakeAsyncContextManager:
    """简单 async context manager — 返回固定 value。"""

    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *args: Any) -> bool:
        return False


class _FakeCRDBCursor:
    """fake asyncpg cursor — 支持 fetchone/fetchall/rowcount。"""

    def __init__(
        self,
        fetchone_result: Optional[tuple] = None,
        fetchall_result: Optional[list] = None,
        rowcount: int = 0,
    ) -> None:
        self._fetchone_result = fetchone_result
        self._fetchall_result = fetchall_result or []
        self.rowcount = rowcount

    async def fetchone(self) -> Optional[tuple]:
        return self._fetchone_result

    async def fetchall(self) -> list:
        return self._fetchall_result


class _FakeCRDBConn:
    """fake asyncpg connection — 支持 execute/fetchone/fetchval/transaction。

    行为由 ``_FakeCRDBClient`` 的配置控制(connected/schema_present/
    permissions_ok/cas_capable/current_version)。
    """

    def __init__(self, client: "_FakeCRDBClient") -> None:
        self._client = client

    async def execute(self, query: str, *params: Any) -> Any:
        """模拟 asyncpg conn.execute。

        根据 query 内容返回不同的 cursor 或 raise(模拟故障)。
        """
        # 故障注入:连接失败模式(虽然已 acquire,但 execute 失败)
        if not self._client._connected:
            raise RuntimeError("fake_crdb: connection lost")

        # SELECT 1 — 连接探测
        if query.strip() == "SELECT 1":
            return _FakeCRDBCursor(fetchone_result=(1,))

        # information_schema.tables 查询 — schema_present 检查
        if "information_schema.tables" in query and "COUNT" in query:
            if not self._client._schema_present:
                raise RuntimeError("fake_crdb: information_schema not accessible")
            # 返回 active_schema 的表数量(0 表示空 schema,但仍可访问)
            return _FakeCRDBCursor(fetchone_result=(0,))

        # information_schema.tables EXISTS 查询 — restore_active_pointer 表存在性
        if "information_schema.tables" in query and "EXISTS" in query:
            if not self._client._schema_present:
                raise RuntimeError("fake_crdb: information_schema not accessible")
            # has_pointer_table 由 client 控制
            has_table = self._client._has_active_pointer_table
            return _FakeCRDBCursor(fetchone_result=(has_table,))

        # restore_active_pointer 表查询 — 读取 current_version
        if "restore_active_pointer" in query and "SELECT" in query.upper():
            if self._client._current_version is None:
                return _FakeCRDBCursor(fetchone_result=None)
            return _FakeCRDBCursor(
                fetchone_result=(self._client._current_version,)
            )

        # CREATE SCHEMA — permissions_ok 检查
        if "CREATE SCHEMA" in query:
            if not self._client._permissions_ok:
                raise RuntimeError("fake_crdb: CREATE SCHEMA permission denied")
            return _FakeCRDBCursor()

        # CREATE TABLE — permissions_ok 检查
        if "CREATE TABLE" in query:
            if not self._client._permissions_ok:
                raise RuntimeError("fake_crdb: CREATE TABLE permission denied")
            return _FakeCRDBCursor()

        # INSERT — permissions_ok 检查
        if "INSERT" in query:
            if not self._client._permissions_ok:
                raise RuntimeError("fake_crdb: INSERT permission denied")
            return _FakeCRDBCursor()

        # UPDATE — cas_capable 检查
        if "UPDATE" in query:
            if not self._client._cas_capable:
                # CAS 不支持:返回 rowcount=0(始终不匹配)
                return _FakeCRDBCursor(rowcount=0)
            # CAS 支持:根据是否是第二次调用返回不同 rowcount
            # 第一次 UPDATE(WHERE version='v1')→ rowcount=1
            # 第二次 UPDATE(WHERE version='v1' 但实际已是 v2)→ rowcount=0
            # 通过 params 判断:第三次参数是 'v1'(WHERE 子句的 version)
            if len(params) >= 3 and params[2] == "v1":
                # 检查是否是第二次调用(通过 client 内部计数器)
                self._client._cas_call_count += 1
                if self._client._cas_call_count == 1:
                    return _FakeCRDBCursor(rowcount=1)
                else:
                    return _FakeCRDBCursor(rowcount=0)
            return _FakeCRDBCursor(rowcount=1)

        # DROP SCHEMA — permissions_ok 检查
        if "DROP SCHEMA" in query:
            if not self._client._permissions_ok:
                raise RuntimeError("fake_crdb: DROP SCHEMA permission denied")
            return _FakeCRDBCursor()

        # 默认:返回空 cursor
        return _FakeCRDBCursor()

    async def fetchval(self, query: str, *params: Any) -> Any:
        """模拟 asyncpg conn.fetchval。"""
        if not self._client._connected:
            raise RuntimeError("fake_crdb: connection lost")
        if query.strip() == "SELECT 1":
            return 1
        return None

    async def fetchone(self, query: str, *params: Any) -> Optional[tuple]:
        """模拟 asyncpg conn.fetchone。"""
        cursor = await self.execute(query, *params)
        return await cursor.fetchone()

    async def fetchall(self, query: str, *params: Any) -> list:
        """模拟 asyncpg conn.fetchall。"""
        cursor = await self.execute(query, *params)
        return await cursor.fetchall()

    def transaction(self) -> _FakeAsyncContextManager:
        """模拟 asyncpg conn.transaction() — 返回 async context manager。

        事务退出时不实际 commit(模拟 ROLLBACK 行为,适合 verify_authority_state
        的写权限测试)。
        """
        return _FakeAsyncContextManager(self)


class _FakeCRDBClient:
    """fake asyncpg client/pool — 支持 acquire() async context manager。

    通过构造参数控制 ``verify_authority_state`` 各维度的行为:
      - connected: 能否 acquire + execute SELECT 1
      - schema_present: information_schema.tables 能否查询
      - permissions_ok: CREATE SCHEMA + INSERT + DROP 能否执行
      - cas_capable: UPDATE ... WHERE 能否返回正确 rowcount
      - has_active_pointer_table: restore_active_pointer 表是否存在
      - current_version: restore_active_pointer 表中的版本值
    """

    def __init__(
        self,
        *,
        connected: bool = True,
        schema_present: bool = True,
        permissions_ok: bool = True,
        cas_capable: bool = True,
        has_active_pointer_table: bool = False,
        current_version: Optional[str] = None,
    ) -> None:
        self._connected = connected
        self._schema_present = schema_present
        self._permissions_ok = permissions_ok
        self._cas_capable = cas_capable
        self._has_active_pointer_table = has_active_pointer_table
        self._current_version = current_version
        self._cas_call_count = 0
        self._conn = _FakeCRDBConn(self)

    def acquire(self) -> _FakeAsyncContextManager:
        """模拟 client.acquire() — 返回 async context manager。

        若 connected=False,返回的 context manager 在 __aenter__ 时 raise。
        """
        if not self._connected:
            # 返回一个会 raise 的 context manager
            return _FailingAsyncContextManager()
        return _FakeAsyncContextManager(self._conn)


class _FailingAsyncContextManager:
    """模拟连接失败的 async context manager — __aenter__ raise。"""

    async def __aenter__(self) -> Any:
        raise RuntimeError("fake_crdb: acquire failed (connection unavailable)")

    async def __aexit__(self, *args: Any) -> bool:
        return False


def make_fake_crdb_client(
    *,
    connected: bool = True,
    schema_present: bool = True,
    permissions_ok: bool = True,
    cas_capable: bool = True,
    has_active_pointer_table: bool = False,
    current_version: Optional[str] = None,
) -> _FakeCRDBClient:
    """工厂函数 — 创建配置好的 fake CRDB client。

    所有参数默认 True(模拟健康的 CRDB 后端),测试可按需关闭某维度
    以模拟故障场景。

    Args:
        connected: 能否 acquire + execute SELECT 1
        schema_present: information_schema.tables 能否查询
        permissions_ok: CREATE SCHEMA + INSERT + DROP 能否执行
        cas_capable: UPDATE ... WHERE 能否返回正确 rowcount
        has_active_pointer_table: restore_active_pointer 表是否存在
        current_version: restore_active_pointer 表中的版本值

    Returns:
        _FakeCRDBClient 实例(可直接传给 CRDBRestoreBackend)
    """
    return _FakeCRDBClient(
        connected=connected,
        schema_present=schema_present,
        permissions_ok=permissions_ok,
        cas_capable=cas_capable,
        has_active_pointer_table=has_active_pointer_table,
        current_version=current_version,
    )
