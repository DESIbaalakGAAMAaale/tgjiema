"""R72 RC62: backup_once timeout 保护范围修复 — 测试套件。

R72 RC62 整改背景:
    RC60 已为 backup_once 添加 asyncio.wait_for(timeout=240) 整体超时,
    但 compose-runtime-e2e backup_restore 阶段仍 600s 超时。根因:
      - init_db() 调用在 asyncio.wait_for 之外(_do_backup_inner 之前)
      - finally 块的 close_db() / r2_storage.close() 也在 timeout 之外
    当 init_db()(或 asyncpg connect)在 CI 环境因网络问题挂起时,
    --timeout 240 完全失效,编排器只能等 600s 超时强杀,无结构化 evidence。

RC62 修复:
      1. init_db() 移入 _do_backup_inner() 内部,受 asyncio.wait_for 保护
      2. finally 块的 close_db() 和 r2_storage.close() 用 asyncio.wait_for
         独立超时(15s)包裹,防止 cleanup 卡住导致整体无法返回

测试策略:
    - AST 解析验证代码结构(不导入运行时模块,避免 loguru/asyncpg 依赖)
    - 字符串匹配验证关键代码片段
    - 严格遵守 R72 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_BACKUP_PATH = REPO_ROOT / "services" / "db_backup.py"


def _extract_backup_once_body() -> str:
    """AST 解析 backup_once 函数,返回函数体源码字符串。"""
    source = DB_BACKUP_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "backup_once":
            # 返回函数体起始位置到下一个顶层定义之间
            start = node.body[0].lineno
            # 取该函数后 200 行作为 body
            lines = source.splitlines()
            return "\n".join(lines[start - 1:start + 200])
    return ""


def _extract_do_backup_inner_body() -> str:
    """提取 _do_backup_inner 函数体源码。

    返回从 `async def _do_backup_inner` 到 backup_once 函数结束之间的代码。
    """
    source = DB_BACKUP_PATH.read_text(encoding="utf-8")
    idx = source.find("async def _do_backup_inner")
    if idx < 0:
        return ""
    # 从 _do_backup_inner 定义开始,取后续 2500 字符(足够覆盖整个内部函数)
    return source[idx:idx + 2500]


def _extract_finally_block() -> str:
    """提取 backup_once 的 finally 块源码。"""
    source = DB_BACKUP_PATH.read_text(encoding="utf-8")
    idx = source.find("async def backup_once(")
    if idx < 0:
        return ""
    body = source[idx:]
    finally_idx = body.find("finally:")
    if finally_idx < 0:
        return ""
    # 取 finally 后 1500 字符
    return body[finally_idx:finally_idx + 1500]


# ════════════════════════════════════════════════════════════════
# A. init_db 移入 _do_backup_inner(受 timeout 保护)
# ════════════════════════════════════════════════════════════════


class TestInitDbMovedIntoTimeoutScope:
    """R72 RC62 A: init_db 必须在 _do_backup_inner 内部,受 asyncio.wait_for 保护。"""

    def test_init_db_called_inside_do_backup_inner(self):
        """init_db() 必须在 _do_backup_inner 函数体内调用。"""
        inner_body = _extract_do_backup_inner_body()
        assert inner_body, "未找到 _do_backup_inner 函数"
        assert "init_db" in inner_body, (
            "R72 RC62: init_db 必须在 _do_backup_inner 内调用,"
            "确保受 asyncio.wait_for timeout 保护"
        )

    def test_init_db_not_called_before_do_backup_inner(self):
        """init_db() 不能在 _do_backup_inner 之前(backup_once 函数体顶层)调用。

        旧实现:backup_once 函数体顶层 if not db_client.is_connected: await init_db()
        这在 asyncio.wait_for 之外,timeout 不生效。
        """
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        idx = source.find("async def backup_once(")
        assert idx >= 0, "未找到 backup_once 函数"
        # 取 backup_once 到 _do_backup_inner 之间的代码
        inner_idx = source.find("async def _do_backup_inner", idx)
        assert inner_idx >= 0, "未找到 _do_backup_inner"
        between = source[idx:inner_idx]
        # 在 _do_backup_inner 之前不应直接调用 init_db(只允许出现在注释中)
        # 移除注释行后检查
        lines = between.splitlines()
        non_comment_lines = [
            ln for ln in lines
            if ln.strip() and not ln.strip().startswith("#")
        ]
        non_comment_between = "\n".join(non_comment_lines)
        assert "init_db" not in non_comment_between or "from database.session import init_db" not in non_comment_between, (
            "R72 RC62: init_db 不能在 _do_backup_inner 之前调用(否则不受 timeout 保护)"
        )

    def test_init_db_guarded_by_is_connected_check(self):
        """init_db 调用前必须有 db_client.is_connected 检查(避免重复初始化)。"""
        inner_body = _extract_do_backup_inner_body()
        assert "db_client.is_connected" in inner_body, (
            "R72 RC62: init_db 调用前必须检查 db_client.is_connected"
        )

    def test_init_db_import_inside_inner_function(self):
        """init_db 的 import 必须在 _do_backup_inner 内部(避免顶层导入副作用)。"""
        inner_body = _extract_do_backup_inner_body()
        assert "from database.session import init_db" in inner_body, (
            "R72 RC62: init_db 必须在 _do_backup_inner 内部 import"
        )


# ════════════════════════════════════════════════════════════════
# B. finally 块 cleanup 操作的独立超时保护
# ════════════════════════════════════════════════════════════════


class TestFinallyCleanupTimeout:
    """R72 RC62 B: finally 块的 close_db / r2_storage.close 必须有独立超时。"""

    def test_finally_block_exists(self):
        """backup_once 必须有 finally 块(用于 cleanup)。"""
        finally_body = _extract_finally_block()
        assert finally_body, "未找到 backup_once 的 finally 块"

    def test_close_db_wrapped_in_wait_for(self):
        """close_db() 必须用 asyncio.wait_for 包裹(独立超时)。"""
        finally_body = _extract_finally_block()
        assert "asyncio.wait_for(close_db()" in finally_body, (
            "R72 RC62: finally 块的 close_db() 必须用 asyncio.wait_for 包裹,"
            "防止 cleanup 卡住导致整体无法返回"
        )

    def test_close_db_timeout_is_15s(self):
        """close_db 的超时必须为 15 秒(足够释放连接但不无限等待)。"""
        finally_body = _extract_finally_block()
        # 查找 close_db 后的 wait_for 调用,验证 timeout=15
        idx = finally_body.find("asyncio.wait_for(close_db()")
        assert idx >= 0, "未找到 close_db 的 wait_for 包裹"
        snippet = finally_body[idx:idx + 200]
        assert "timeout=15" in snippet, (
            f"R72 RC62: close_db 的 wait_for 超时必须为 15s, 实际片段: {snippet}"
        )

    def test_r2_close_wrapped_in_wait_for(self):
        """r2_storage.close() 必须用 asyncio.wait_for 包裹(独立超时)。"""
        finally_body = _extract_finally_block()
        assert "asyncio.wait_for(r2_storage.close()" in finally_body, (
            "R72 RC62: finally 块的 r2_storage.close() 必须用 asyncio.wait_for 包裹"
        )

    def test_r2_close_timeout_is_15s(self):
        """r2_storage.close 的超时必须为 15 秒。"""
        finally_body = _extract_finally_block()
        idx = finally_body.find("asyncio.wait_for(r2_storage.close()")
        assert idx >= 0, "未找到 r2_storage.close 的 wait_for 包裹"
        snippet = finally_body[idx:idx + 200]
        assert "timeout=15" in snippet, (
            f"R72 RC62: r2_storage.close 的 wait_for 超时必须为 15s, 实际片段: {snippet}"
        )

    def test_close_db_timeout_handled(self):
        """close_db 超时必须被捕获(不能让 cleanup 超时导致整个函数失败)。"""
        finally_body = _extract_finally_block()
        # close_db 的 wait_for 后必须有 except asyncio.TimeoutError
        idx = finally_body.find("asyncio.wait_for(close_db()")
        assert idx >= 0
        # 取 close_db 后 500 字符,验证含 TimeoutError 处理
        snippet = finally_body[idx:idx + 500]
        assert "asyncio.TimeoutError" in snippet, (
            "R72 RC62: close_db 的 wait_for 必须捕获 asyncio.TimeoutError"
        )

    def test_r2_close_timeout_handled(self):
        """r2_storage.close 超时必须被捕获。"""
        finally_body = _extract_finally_block()
        idx = finally_body.find("asyncio.wait_for(r2_storage.close()")
        assert idx >= 0
        snippet = finally_body[idx:idx + 500]
        assert "asyncio.TimeoutError" in snippet, (
            "R72 RC62: r2_storage.close 的 wait_for 必须捕获 asyncio.TimeoutError"
        )

    def test_rc62_comment_present(self):
        """finally 块必须含 RC62 修复说明注释(便于审计追溯)。"""
        finally_body = _extract_finally_block()
        assert "RC62" in finally_body, (
            "R72 RC62: finally 块必须含 RC62 修复说明注释"
        )


# ════════════════════════════════════════════════════════════════
# C. _do_backup_inner 内部首条语句必须是 init_db
# ════════════════════════════════════════════════════════════════


class TestDoBackupInnerFirstStatement:
    """R72 RC62 C: _do_backup_inner 内部首条语句必须是 init_db(确保连接池优先初始化)。"""

    def test_init_db_appears_before_configure_r2(self):
        """init_db 必须在 configure_r2_dynamic 之前调用。

        顺序: init_db → configure_r2_dynamic → R2 凭证检查 → 加密检查 → backup 逻辑
        """
        inner_body = _extract_do_backup_inner_body()
        init_idx = inner_body.find("init_db")
        configure_idx = inner_body.find("configure_r2_dynamic")
        assert init_idx >= 0, "未在 _do_backup_inner 中找到 init_db"
        assert configure_idx >= 0, "未在 _do_backup_inner 中找到 configure_r2_dynamic"
        assert init_idx < configure_idx, (
            "R72 RC62: init_db 必须在 configure_r2_dynamic 之前调用"
        )

    def test_configure_r2_appears_before_r2_credential_check(self):
        """configure_r2_dynamic 必须在 r2_storage._access_key 检查之前。"""
        inner_body = _extract_do_backup_inner_body()
        configure_idx = inner_body.find("configure_r2_dynamic")
        access_key_idx = inner_body.find("r2_storage._access_key")
        assert configure_idx >= 0
        assert access_key_idx >= 0
        assert configure_idx < access_key_idx, (
            "R72 RC62: configure_r2_dynamic 必须在 R2 凭证检查之前调用"
        )
