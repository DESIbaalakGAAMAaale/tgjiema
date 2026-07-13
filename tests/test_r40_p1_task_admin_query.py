"""R40 P1-1: 任务中心 Admin 查询"所有用户"任务整改测试。

问题:
    admin/__init__.py 的 /tasks 路由调用 ``list_user_tasks(0, limit=100)``,
    原意是"查询所有用户任务",但实际执行 ``WHERE user_id=0``,
    仅返回 user_id=0 的任务(通常为空),无法看到真实任务。

整改:
    1. 在 services/task_center.py 新增 ``list_all_tasks(limit, offset, status_filter)``
       不带 user_id 过滤,支持分页与状态过滤。
    2. admin/__init__.py 的 /tasks 路由改用 ``list_all_tasks``。
    3. bots/admin_bot/handlers.py 新增 ``cmd_tasks`` 命令,/tasks 命令
       使用 ``list_all_tasks`` 查询。
    4. bots/admin_bot/run.py 注册 CommandHandler("tasks", cmd_tasks)。

测试策略:
    - 使用真实 SQLite 临时文件数据库(隔离于生产 cache_store.db)
    - 通过 monkeypatch 替换 DB_PATH 指向临时路径
    - 插入多用户任务,验证 list_all_tasks 返回全部且不按 user_id 过滤
    - 验证 admin 路由 /tasks 使用 list_all_tasks(而非 list_user_tasks(0))
    - 验证 admin_bot /tasks 命令处理函数已注册并使用 list_all_tasks
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# ── 模块级 skip 检查:cache_store 必须是真实类(非 conftest 降级 mock) ──
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def task_store():
    """创建带 tasks 表的临时 CacheStore 实例。"""
    tmpdir = tempfile.mkdtemp(prefix="r40_p1_tasks_")
    db_path = Path(tmpdir) / "test_tasks.db"
    original_path = _cs_module.DB_PATH
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _insert_task(store, task_type: str, user_id: int, status: str = "pending",
                       trace_id: str = "") -> int:
    """直接向 tasks 表插入一条记录,返回 id。"""
    import datetime as _dt
    now = _dt.datetime.now().isoformat()
    cursor = await store._db.execute(
        """INSERT INTO tasks (task_type, user_id, status, progress, eta_seconds,
                               payload, trace_id, created_at, updated_at)
           VALUES (?, ?, ?, 0, 0, '{}', ?, ?, ?)""",
        (task_type, user_id, status, trace_id, now, now),
    )
    await store._db.commit()
    return int(cursor.lastrowid) if cursor and cursor.lastrowid else 0


# ════════════════════════════════════════════════════════════════
# 1. list_all_tasks 函数存在性与签名
# ════════════════════════════════════════════════════════════════

class TestListAllTasksFunction:
    """R40 P1-1: list_all_tasks 函数定义与签名测试。"""

    def test_list_all_tasks_exists(self):
        """list_all_tasks 应在 services.task_center 模块中定义。"""
        from services import task_center
        assert hasattr(task_center, "list_all_tasks"), \
            "services.task_center 必须定义 list_all_tasks 函数"

    def test_list_all_tasks_is_callable(self):
        """list_all_tasks 应是可调用对象。"""
        from services import task_center
        assert callable(task_center.list_all_tasks), \
            "list_all_tasks 必须是可调用对象"

    def test_list_all_tasks_signature(self):
        """list_all_tasks 签名应支持 limit/offset/status_filter 参数。"""
        from services import task_center
        sig = inspect.signature(task_center.list_all_tasks)
        params = set(sig.parameters.keys())
        # 必须支持 limit/offset/status_filter,不带必填的 user_id
        assert "limit" in params, "list_all_tasks 必须支持 limit 参数"
        assert "offset" in params, "list_all_tasks 必须支持 offset 参数(分页)"
        assert "user_id" not in params, \
            "list_all_tasks 不应接受 user_id 参数(它查询所有用户)"


# ════════════════════════════════════════════════════════════════
# 2. list_all_tasks 行为测试(真实 SQLite)
# ════════════════════════════════════════════════════════════════

class TestListAllTasksBehavior:
    """R40 P1-1: list_all_tasks 行为测试(真实 SQLite)。"""

    @pytest.mark.asyncio
    async def test_returns_all_users_tasks(self, task_store, monkeypatch):
        """list_all_tasks 应返回所有用户的任务,不按 user_id 过滤。"""
        monkeypatch.setattr("services.task_center.get_cache_store", lambda: task_store)

        # 插入 3 个用户的任务
        await _insert_task(task_store, "upload", user_id=1001, status="pending")
        await _insert_task(task_store, "index", user_id=1002, status="running")
        await _insert_task(task_store, "delivery", user_id=1003, status="completed")

        from services.task_center import list_all_tasks
        tasks = await list_all_tasks(limit=100)

        assert len(tasks) == 3, f"应返回 3 条任务(所有用户),实际: {len(tasks)}"
        user_ids = {t["user_id"] for t in tasks}
        assert user_ids == {1001, 1002, 1003}, \
            f"应包含所有 3 个用户 id,实际: {user_ids}"

    @pytest.mark.asyncio
    async def test_does_not_filter_user_id_zero(self, task_store, monkeypatch):
        """list_all_tasks 不应只返回 user_id=0 的任务。"""
        monkeypatch.setattr("services.task_center.get_cache_store", lambda: task_store)

        # 仅插入非 user_id=0 的任务
        await _insert_task(task_store, "upload", user_id=5001)
        await _insert_task(task_store, "upload", user_id=5002)

        from services.task_center import list_all_tasks
        tasks = await list_all_tasks(limit=100)

        assert len(tasks) == 2, "应返回所有用户任务,而非只 user_id=0 的(空)"
        # 确认无 user_id=0 任务被错误返回
        assert all(t["user_id"] != 0 for t in tasks), \
            "list_all_tasks 不应错误返回 user_id=0 的任务"

    @pytest.mark.asyncio
    async def test_status_filter(self, task_store, monkeypatch):
        """list_all_tasks 应支持按 status 过滤。"""
        monkeypatch.setattr("services.task_center.get_cache_store", lambda: task_store)

        await _insert_task(task_store, "upload", user_id=1, status="pending")
        await _insert_task(task_store, "upload", user_id=2, status="running")
        await _insert_task(task_store, "upload", user_id=3, status="pending")
        await _insert_task(task_store, "upload", user_id=4, status="completed")

        from services.task_center import list_all_tasks
        # 仅查 pending
        pending = await list_all_tasks(limit=100, status_filter="pending")
        assert len(pending) == 2, f"pending 应有 2 条,实际: {len(pending)}"
        assert all(t["status"] == "pending" for t in pending)

        # 仅查 running
        running = await list_all_tasks(limit=100, status_filter="running")
        assert len(running) == 1, f"running 应有 1 条,实际: {len(running)}"

    @pytest.mark.asyncio
    async def test_pagination_limit_offset(self, task_store, monkeypatch):
        """list_all_tasks 应支持分页(limit + offset)。"""
        monkeypatch.setattr("services.task_center.get_cache_store", lambda: task_store)

        # 插入 5 条任务
        for i in range(5):
            await _insert_task(task_store, "upload", user_id=1000 + i)

        from services.task_center import list_all_tasks
        # 取前 2 条
        page1 = await list_all_tasks(limit=2, offset=0)
        assert len(page1) == 2, f"第一页应 2 条,实际: {len(page1)}"

        # 取第 3-4 条(offset=2)
        page2 = await list_all_tasks(limit=2, offset=2)
        assert len(page2) == 2, f"第二页应 2 条,实际: {len(page2)}"

        # 第一页和第二页的 id 不应重叠
        ids_p1 = {t["id"] for t in page1}
        ids_p2 = {t["id"] for t in page2}
        assert ids_p1.isdisjoint(ids_p2), \
            f"分页结果不应重叠: p1={ids_p1}, p2={ids_p2}"

        # 越界 offset 返回空
        page_empty = await list_all_tasks(limit=10, offset=100)
        assert page_empty == [], "越界 offset 应返回空列表"

    @pytest.mark.asyncio
    async def test_ordered_by_created_at_desc(self, task_store, monkeypatch):
        """list_all_tasks 应按 created_at 倒序排列(最新在前)。"""
        monkeypatch.setattr("services.task_center.get_cache_store", lambda: task_store)

        import datetime as _dt
        # 插入 3 条任务,使用递增时间戳(分钟数依次为 0/1/2)
        ids = []
        for i, minute in enumerate([0, 1, 2]):
            now = (_dt.datetime(2026, 1, 1, 10, minute) + _dt.timedelta(minutes=minute)).isoformat()
            cursor = await task_store._db.execute(
                """INSERT INTO tasks (task_type, user_id, status, progress, eta_seconds,
                                       payload, trace_id, created_at, updated_at)
                   VALUES (?, ?, 'pending', 0, 0, '{}', '', ?, ?)""",
                ("upload", 1000 + i, now, now),
            )
            ids.append(int(cursor.lastrowid))
        await task_store._db.commit()

        from services.task_center import list_all_tasks
        tasks = await list_all_tasks(limit=100)

        # 最新(后插入的)应在前
        assert tasks[0]["id"] == ids[-1], \
            f"最新任务应在前,实际首条 id={tasks[0]['id']}"
        assert tasks[-1]["id"] == ids[0], \
            f"最旧任务应在后,实际末条 id={tasks[-1]['id']}"

    @pytest.mark.asyncio
    async def test_default_limit_clamped(self, task_store, monkeypatch):
        """list_all_tasks 的 limit 应被 clamp 到合理范围(1-200)。"""
        monkeypatch.setattr("services.task_center.get_cache_store", lambda: task_store)

        for i in range(3):
            await _insert_task(task_store, "upload", user_id=i)

        from services.task_center import list_all_tasks
        # limit=0 应被 clamp 到最小值(1 或更多),不应报错
        result = await list_all_tasks(limit=0)
        assert isinstance(result, list), "limit=0 应被 clamp,不应抛异常"
        assert len(result) >= 1, "clamp 后应至少返回 1 条"

        # limit=-1(异常值)应被 clamp,不报错
        result_neg = await list_all_tasks(limit=-1)
        assert isinstance(result_neg, list), "limit=-1 应被 clamp,不应抛异常"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_data(self, task_store, monkeypatch):
        """无任务时 list_all_tasks 应返回空列表。"""
        monkeypatch.setattr("services.task_center.get_cache_store", lambda: task_store)

        from services.task_center import list_all_tasks
        result = await list_all_tasks(limit=100)
        assert result == [], "无任务时应返回空列表"

    @pytest.mark.asyncio
    async def test_task_dict_fields_complete(self, task_store, monkeypatch):
        """list_all_tasks 返回的字典应包含所有必要字段。"""
        monkeypatch.setattr("services.task_center.get_cache_store", lambda: task_store)
        await _insert_task(task_store, "upload", user_id=12345)

        from services.task_center import list_all_tasks
        tasks = await list_all_tasks(limit=1)
        assert len(tasks) == 1
        t = tasks[0]
        required_fields = {
            "id", "task_type", "user_id", "status", "progress",
            "eta_seconds", "payload", "result", "error", "trace_id",
            "created_at", "updated_at",
        }
        missing = required_fields - set(t.keys())
        assert not missing, f"任务字典缺少字段: {missing}"


# ════════════════════════════════════════════════════════════════
# 3. Admin Web 路由 /tasks 修复
# ════════════════════════════════════════════════════════════════

class TestAdminTasksRoute:
    """R40 P1-1: Admin Web /tasks 路由整改测试。"""

    def test_tasks_route_uses_list_all_tasks(self):
        """Admin /tasks 路由源码应使用 list_all_tasks,而非 list_user_tasks(0)。"""
        # 直接读 admin/__init__.py 源码做静态检查(避免起服务)
        admin_path = Path(__file__).parent.parent / "admin" / "__init__.py"
        src = admin_path.read_text(encoding="utf-8")

        # 找到 tasks_page 函数(从 @app.get("/tasks" 开始的一段)
        # 验证 list_all_tasks 已导入并使用
        assert "list_all_tasks" in src, \
            "admin/__init__.py 必须导入并使用 list_all_tasks"

        # 验证不再使用 list_user_tasks(0, ...) 这种错误调用
        # 找 tasks_page 函数体范围
        import re
        # 匹配 @app.get("/tasks" 到下一个 @app.get 之间的内容
        match = re.search(
            r'@app\.get\("/tasks"[^\n]*\nasync def tasks_page.*?(?=\n@app\.\w+\(|\nclass |\Z)',
            src, re.DOTALL,
        )
        assert match, "无法定位 tasks_page 函数"
        func_body = match.group(0)
        # 检查实际调用(以 await list_user_tasks(0 形式),
        # 不匹配文档字符串中"原实现调用 list_user_tasks(0,..."这种说明性文字
        assert "await list_user_tasks(0" not in func_body, \
            "tasks_page 不应再实际调用 list_user_tasks(0) (BUG:会查 WHERE user_id=0)"
        # 必须有实际调用 list_all_tasks(以 await 形式)
        assert "await list_all_tasks(" in func_body, \
            "tasks_page 必须实际调用 list_all_tasks"

    def test_tasks_route_no_user_id_zero_filter(self):
        """tasks_page 路由源码不应包含实际的 user_id=0 查询调用。"""
        admin_path = Path(__file__).parent.parent / "admin" / "__init__.py"
        src = admin_path.read_text(encoding="utf-8")

        import re
        match = re.search(
            r'@app\.get\("/tasks"[^\n]*\nasync def tasks_page.*?(?=\n@app\.\w+\(|\nclass |\Z)',
            src, re.DOTALL,
        )
        assert match
        func_body = match.group(0)
        # 不应有实际的 await list_user_tasks(0,...) 调用(允许文档字符串提及)
        assert "await list_user_tasks(0" not in func_body, \
            "tasks_page 不应再实际调用 list_user_tasks(0,...) (会查 WHERE user_id=0)"


# ════════════════════════════════════════════════════════════════
# 4. Admin Bot /tasks 命令注册
# ════════════════════════════════════════════════════════════════

class TestAdminBotTasksCommand:
    """R40 P1-1: Admin Bot /tasks 命令注册测试。"""

    def test_cmd_tasks_handler_exists(self):
        """bots/admin_bot/handlers.py 应定义 cmd_tasks 函数。"""
        handlers_path = Path(__file__).parent.parent / "bots" / "admin_bot" / "handlers.py"
        src = handlers_path.read_text(encoding="utf-8")
        assert "async def cmd_tasks" in src, \
            "bots/admin_bot/handlers.py 必须定义 async def cmd_tasks 函数"

    def test_cmd_tasks_uses_list_all_tasks(self):
        """cmd_tasks 函数体应调用 list_all_tasks,而非 list_user_tasks。"""
        handlers_path = Path(__file__).parent.parent / "bots" / "admin_bot" / "handlers.py"
        src = handlers_path.read_text(encoding="utf-8")

        import re
        # 匹配 async def cmd_tasks 到下一个 async def 之间的内容
        match = re.search(
            r'async def cmd_tasks.*?(?=\nasync def |\nclass |\Z)',
            src, re.DOTALL,
        )
        assert match, "无法定位 cmd_tasks 函数体"
        body = match.group(0)
        assert "list_all_tasks" in body, \
            "cmd_tasks 必须调用 list_all_tasks"
        assert "list_user_tasks(0" not in body, \
            "cmd_tasks 不应调用 list_user_tasks(0)"

    def test_tasks_command_registered_in_run(self):
        """bots/admin_bot/run.py 应注册 CommandHandler("tasks", cmd_tasks)。"""
        run_path = Path(__file__).parent.parent / "bots" / "admin_bot" / "run.py"
        src = run_path.read_text(encoding="utf-8")

        # 验证 import 和 handler 注册都存在
        assert "cmd_tasks" in src, \
            "bots/admin_bot/run.py 必须从 handlers 导入 cmd_tasks"
        assert 'CommandHandler("tasks"' in src, \
            'bots/admin_bot/run.py 必须注册 CommandHandler("tasks", cmd_tasks)'

    def test_cmd_tasks_decorator(self):
        """cmd_tasks 应使用 @_auth_required 装饰器(权限校验)。"""
        handlers_path = Path(__file__).parent.parent / "bots" / "admin_bot" / "handlers.py"
        src = handlers_path.read_text(encoding="utf-8")

        import re
        # cmd_tasks 前应有 @_auth_required 装饰器
        match = re.search(
            r'(@_auth_required\s*\n\s*async def cmd_tasks|async def cmd_tasks)',
            src,
        )
        assert match, "无法定位 cmd_tasks 定义"
        # 验证装饰器存在(允许 @_auth_required 在前一行)
        cmd_idx = src.find("async def cmd_tasks")
        assert cmd_idx >= 0
        # 在 cmd_tasks 定义前 200 字符内查找 @_auth_required
        preceding = src[max(0, cmd_idx - 200):cmd_idx]
        assert "@_auth_required" in preceding, \
            "cmd_tasks 必须使用 @_auth_required 装饰器"
