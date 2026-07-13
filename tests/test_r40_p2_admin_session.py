"""R40 P2-5: Admin Web session + MFA 测试。

测试范围:
- admin/sessions.py: SessionManager 类(create_session/validate_session/
  destroy_session/cleanup_expired_sessions)
- admin/mfa.py: MFAManager 类(generate_totp_secret/verify_totp_code/
  is_mfa_enabled/enable_mfa/disable_mfa)
- admin/__init__.py: /login GET/POST, /logout POST 路由存在性
  verify_session() / _async_validate_session() 函数存在性

测试策略:
- AST 语法检查(兼容 Python 3.9,不依赖运行时 import)
- 文件存在性检查
- 关键 async 方法存在性检查
- 占位实现行为验证(_verify_totp 始终返回 False)
- 路由定义存在性检查
- 中文注释检查(遵循用户规则)
"""
from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ADMIN_DIR = REPO_ROOT / "admin"


def _parse_ast(filepath: Path) -> ast.Module | None:
    """解析 Python 文件 AST,失败返回 None。"""
    try:
        source = filepath.read_text(encoding="utf-8")
        return ast.parse(source)
    except Exception:
        return None


def _get_async_funcs(tree: ast.Module) -> set[str]:
    """提取 AST 中所有 async def 函数名。"""
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }


def _get_sync_funcs(tree: ast.Module) -> set[str]:
    """提取 AST 中所有 sync def 函数名。"""
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }


def _get_classes(tree: ast.Module) -> set[str]:
    """提取 AST 中所有 ClassDef 名。"""
    return {
        node.name for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }


# ════════════════════════════════════════════════════════════════
# 1. admin/sessions.py 测试
# ════════════════════════════════════════════════════════════════


class TestSessionsFile:
    """R40 P2-5: admin/sessions.py 文件级检查。"""

    def test_file_exists(self):
        assert (ADMIN_DIR / "sessions.py").exists(), "admin/sessions.py 应存在"

    def test_ast_parseable(self):
        tree = _parse_ast(ADMIN_DIR / "sessions.py")
        assert tree is not None, "admin/sessions.py 应可被 AST 解析"

    def test_has_session_manager_class(self):
        tree = _parse_ast(ADMIN_DIR / "sessions.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        classes = _get_classes(tree)
        assert "SessionManager" in classes, "应定义 SessionManager 类"

    def test_has_get_session_manager_function(self):
        tree = _parse_ast(ADMIN_DIR / "sessions.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_sync_funcs(tree)
        assert "get_session_manager" in funcs, "应提供 get_session_manager 单例获取函数"

    def test_has_required_async_methods(self):
        """SessionManager 类应包含 4 个核心 async 方法。"""
        tree = _parse_ast(ADMIN_DIR / "sessions.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        async_funcs = _get_async_funcs(tree)
        required = {
            "create_session",
            "validate_session",
            "destroy_session",
            "cleanup_expired_sessions",
        }
        missing = required - async_funcs
        assert not missing, f"SessionManager 缺少 async 方法: {missing}"

    def test_uses_kv_store_prefix(self):
        """session key 应使用 admin:session: 前缀,便于 kv_store 分组清理。"""
        source = (ADMIN_DIR / "sessions.py").read_text(encoding="utf-8")
        assert "admin:session:" in source, "session key 应使用 admin:session: 前缀"

    def test_uses_secrets_token_urlsafe(self):
        """session_id 应使用 secrets.token_urlsafe 生成(256 位熵)。"""
        source = (ADMIN_DIR / "sessions.py").read_text(encoding="utf-8")
        assert "secrets.token_urlsafe" in source, "session_id 应使用 secrets.token_urlsafe"

    def test_has_ttl_default(self):
        """应有默认 TTL 常量(8 小时 = 8 * 3600)。"""
        source = (ADMIN_DIR / "sessions.py").read_text(encoding="utf-8")
        assert "_SESSION_TTL_SECONDS" in source, "应定义 _SESSION_TTL_SECONDS 默认 TTL"

    def test_has_chinese_comments(self):
        """R40 规则:代码注释用中文。"""
        source = (ADMIN_DIR / "sessions.py").read_text(encoding="utf-8")
        # 检查至少 3 处中文注释(单行或多行注释中含中文)
        chinese_count = sum(
            1 for line in source.split("\n")
            if "#" in line and any(
                "\u4e00" <= ch <= "\u9fff"
                for ch in line.split("#", 1)[1]
            )
        )
        assert chinese_count >= 3, f"中文注释数量应 >= 3,实际 {chinese_count}"


# ════════════════════════════════════════════════════════════════
# 2. admin/mfa.py 测试
# ════════════════════════════════════════════════════════════════


class TestMfaFile:
    """R40 P2-5: admin/mfa.py 文件级检查。"""

    def test_file_exists(self):
        assert (ADMIN_DIR / "mfa.py").exists(), "admin/mfa.py 应存在"

    def test_ast_parseable(self):
        tree = _parse_ast(ADMIN_DIR / "mfa.py")
        assert tree is not None, "admin/mfa.py 应可被 AST 解析"

    def test_has_mfa_manager_class(self):
        tree = _parse_ast(ADMIN_DIR / "mfa.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        classes = _get_classes(tree)
        assert "MFAManager" in classes, "应定义 MFAManager 类"

    def test_has_required_async_methods(self):
        """MFAManager 类应包含 3 个核心 async 方法(任务规范)。"""
        tree = _parse_ast(ADMIN_DIR / "mfa.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        async_funcs = _get_async_funcs(tree)
        required = {
            "generate_totp_secret",
            "verify_totp_code",
            "is_mfa_enabled",
        }
        missing = required - async_funcs
        assert not missing, f"MFAManager 缺少 async 方法: {missing}"

    def test_has_enable_disable_methods(self):
        """MFAManager 应包含 enable_mfa / disable_mfa 方法。"""
        tree = _parse_ast(ADMIN_DIR / "mfa.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        async_funcs = _get_async_funcs(tree)
        assert "enable_mfa" in async_funcs, "应包含 enable_mfa 方法"
        assert "disable_mfa" in async_funcs, "应包含 disable_mfa 方法"

    def test_has_get_mfa_manager_function(self):
        tree = _parse_ast(ADMIN_DIR / "mfa.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        funcs = _get_sync_funcs(tree)
        assert "get_mfa_manager" in funcs, "应提供 get_mfa_manager 单例获取函数"

    def test_uses_kv_store_prefix(self):
        """MFA 密钥应使用 admin:mfa: 前缀。"""
        source = (ADMIN_DIR / "mfa.py").read_text(encoding="utf-8")
        assert "admin:mfa:secret:" in source, "MFA 密钥应使用 admin:mfa:secret: 前缀"
        assert "admin:mfa:enabled:" in source, "MFA 启用状态应使用 admin:mfa:enabled: 前缀"

    def test_placeholder_verify_returns_false(self):
        """占位实现:_verify_totp 应始终返回 False(fail-closed)。"""
        source = (ADMIN_DIR / "mfa.py").read_text(encoding="utf-8")
        # 验证 _verify_totp 函数体中包含 return False
        # 简化检查:函数体中明确 return False(防止误实现返回 True)
        assert "return False" in source, "_verify_totp 占位实现应返回 False"

    def test_has_chinese_comments(self):
        """R40 规则:代码注释用中文。"""
        source = (ADMIN_DIR / "mfa.py").read_text(encoding="utf-8")
        chinese_count = sum(
            1 for line in source.split("\n")
            if "#" in line and any(
                "\u4e00" <= ch <= "\u9fff"
                for ch in line.split("#", 1)[1]
            )
        )
        assert chinese_count >= 3, f"中文注释数量应 >= 3,实际 {chinese_count}"

    def test_has_placeholder_documentation(self):
        """占位实现应文档化后续接入步骤。"""
        source = (ADMIN_DIR / "mfa.py").read_text(encoding="utf-8")
        # 检查模块 docstring 或注释中包含 pyotp 关键字(后续接入提示)
        assert "pyotp" in source.lower(), "占位实现应文档化后续接入 pyotp 的步骤"


# ════════════════════════════════════════════════════════════════
# 3. admin/__init__.py 路由检查
# ════════════════════════════════════════════════════════════════


class TestAdminRoutes:
    """R40 P2-5: admin/__init__.py 路由存在性检查。"""

    def test_admin_init_exists(self):
        assert (ADMIN_DIR / "__init__.py").exists(), "admin/__init__.py 应存在"

    def test_ast_parseable(self):
        tree = _parse_ast(ADMIN_DIR / "__init__.py")
        assert tree is not None, "admin/__init__.py 应可被 AST 解析"

    def test_has_login_get_route(self):
        """应有 /login GET 路由(login_page)。"""
        tree = _parse_ast(ADMIN_DIR / "__init__.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        async_funcs = _get_async_funcs(tree)
        assert "login_page" in async_funcs, "应定义 login_page 异步路由(GET /login)"

    def test_has_login_post_route(self):
        """应有 /login POST 路由(login_submit)。"""
        tree = _parse_ast(ADMIN_DIR / "__init__.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        async_funcs = _get_async_funcs(tree)
        assert "login_submit" in async_funcs, "应定义 login_submit 异步路由(POST /login)"

    def test_has_logout_post_route(self):
        """应有 /logout POST 路由(logout_submit)。"""
        tree = _parse_ast(ADMIN_DIR / "__init__.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        async_funcs = _get_async_funcs(tree)
        assert "logout_submit" in async_funcs, "应定义 logout_submit 异步路由(POST /logout)"

    def test_has_verify_session_function(self):
        """应定义 verify_session 同步函数(依赖注入入口)。"""
        tree = _parse_ast(ADMIN_DIR / "__init__.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        sync_funcs = _get_sync_funcs(tree)
        assert "verify_session" in sync_funcs, "应定义 verify_session 同步函数"

    def test_has_async_validate_session_function(self):
        """应定义 _async_validate_session 异步辅助函数。"""
        tree = _parse_ast(ADMIN_DIR / "__init__.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        async_funcs = _get_async_funcs(tree)
        assert "_async_validate_session" in async_funcs, "应定义 _async_validate_session 异步辅助函数"

    def test_has_extract_session_id_function(self):
        """应定义 _extract_session_id 辅助函数。"""
        tree = _parse_ast(ADMIN_DIR / "__init__.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        sync_funcs = _get_sync_funcs(tree)
        assert "_extract_session_id" in sync_funcs, "应定义 _extract_session_id 辅助函数"

    def test_login_route_uses_csrf(self):
        """login_submit 应执行 CSRF 验证。"""
        source = (ADMIN_DIR / "__init__.py").read_text(encoding="utf-8")
        # 查找 login_submit 函数体,验证包含 CSRF 验证逻辑
        assert "csrf_token" in source, "login 路由应使用 csrf_token 表单字段"
        assert "compare_digest" in source, "login 路由应使用 secrets.compare_digest 验证 CSRF"

    def test_login_route_sets_session_cookie(self):
        """登录成功后应设置 session_id cookie。"""
        source = (ADMIN_DIR / "__init__.py").read_text(encoding="utf-8")
        assert 'key="session_id"' in source, "登录成功应设置 session_id cookie"
        assert "httponly=True" in source, "session_id cookie 应为 httponly"

    def test_logout_route_destroys_session(self):
        """logout 路由应调用 SessionManager.destroy_session。"""
        source = (ADMIN_DIR / "__init__.py").read_text(encoding="utf-8")
        assert "destroy_session" in source, "logout 路由应调用 destroy_session"

    def test_logout_route_deletes_cookie(self):
        """logout 路由应清除 session_id cookie。"""
        source = (ADMIN_DIR / "__init__.py").read_text(encoding="utf-8")
        assert "delete_cookie" in source, "logout 路由应调用 delete_cookie"

    def test_retains_verify_admin_for_backward_compat(self):
        """R40 P2-5: 保留旧 verify_admin() 以保证向后兼容。"""
        tree = _parse_ast(ADMIN_DIR / "__init__.py")
        if tree is None:
            pytest.skip("AST 解析失败")
        sync_funcs = _get_sync_funcs(tree)
        assert "verify_admin" in sync_funcs, "应保留 verify_admin 函数(向后兼容)"


# ════════════════════════════════════════════════════════════════
# 4. 运行时 SessionManager 单元测试(需要 cache_store 初始化)
# ════════════════════════════════════════════════════════════════


class TestSessionManagerRuntime:
    """R40 P2-5: SessionManager 运行时单元测试。

    若 cache_store 无法初始化(测试环境缺依赖),自动 skip。
    """

    def _try_init_manager(self):
        """尝试初始化 SessionManager + cache_store。"""
        try:
            from admin.sessions import SessionManager
            # 触发 cache_store 初始化
            from database.cache_store import get_cache_store, CacheStore
            store = get_cache_store()
            if not store._db:
                # 尝试初始化内存 SQLite(避免依赖文件)
                import aiosqlite
                store._db = MagicMock()
            return SessionManager()
        except Exception as e:
            pytest.skip(f"SessionManager 不可初始化: {e}")

    @pytest.mark.asyncio
    async def test_create_session_returns_non_empty_string(self):
        """create_session 应返回非空 session_id。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        # 使用 mock 避免 kv_store 写入
        with patch("admin.sessions._save_session_data", new_callable=MagicMock) as mock_save:
            # _save_session_data 是 async 函数,需用 AsyncMock
            from unittest.mock import AsyncMock
            mock_save.return_value = True
            # 重新 patch(因为 patch 已创建 MagicMock)
            with patch("admin.sessions._save_session_data", AsyncMock(return_value=True)):
                principal = MagicMock()
                principal.id = 123
                principal.username = "admin"
                principal.roles = ["super_admin"]
                session_id = await manager.create_session(principal)
                assert session_id, "session_id 不应为空"
                assert len(session_id) >= 32, f"session_id 应至少 32 字符,实际 {len(session_id)}"

    @pytest.mark.asyncio
    async def test_validate_session_returns_none_for_empty(self):
        """validate_session("") 应返回 None。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        result = await manager.validate_session("")
        assert result is None

    @pytest.mark.asyncio
    async def test_destroy_session_with_empty_id_no_op(self):
        """destroy_session("") 应为 no-op(不抛异常)。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        # 应不抛异常
        await manager.destroy_session("")

    @pytest.mark.asyncio
    async def test_cleanup_expired_sessions_returns_int(self):
        """cleanup_expired_sessions 应返回 int(清理数量)。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        result = await manager.cleanup_expired_sessions()
        assert isinstance(result, int), "应返回 int"


# ════════════════════════════════════════════════════════════════
# 5. MFAManager 运行时单元测试
# ════════════════════════════════════════════════════════════════


class TestMFAManagerRuntime:
    """R40 P2-5: MFAManager 运行时单元测试。"""

    def _try_init_manager(self):
        try:
            from admin.mfa import MFAManager
            return MFAManager()
        except Exception as e:
            pytest.skip(f"MFAManager 不可初始化: {e}")

    @pytest.mark.asyncio
    async def test_generate_totp_secret_with_empty_user(self):
        """user_id=0 应返回空字符串。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        result = await manager.generate_totp_secret(0)
        assert result == "", "user_id=0 应返回空字符串"

    @pytest.mark.asyncio
    async def test_verify_totp_code_with_empty_user(self):
        """user_id=0 应返回 False。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        result = await manager.verify_totp_code(0, "123456")
        assert result is False, "user_id=0 应返回 False"

    @pytest.mark.asyncio
    async def test_verify_totp_code_with_empty_code(self):
        """code 为空应返回 False。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        result = await manager.verify_totp_code(123, "")
        assert result is False, "空 code 应返回 False"

    @pytest.mark.asyncio
    async def test_is_mfa_enabled_with_empty_user(self):
        """user_id=0 应返回 False。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        result = await manager.is_mfa_enabled(0)
        assert result is False, "user_id=0 应返回 False"

    @pytest.mark.asyncio
    async def test_enable_mfa_with_empty_user(self):
        """user_id=0 应返回 False。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        result = await manager.enable_mfa(0)
        assert result is False, "user_id=0 应返回 False"

    @pytest.mark.asyncio
    async def test_disable_mfa_with_empty_user(self):
        """user_id=0 应返回 False。"""
        manager = self._try_init_manager()
        if manager is None:
            return
        result = await manager.disable_mfa(0)
        assert result is False, "user_id=0 应返回 False"

    def test_placeholder_verify_always_returns_false(self):
        """占位实现:_verify_totp 应始终返回 False(fail-closed 安全设计)。"""
        try:
            from admin.mfa import _verify_totp
        except ImportError:
            pytest.skip("admin.mfa 不可导入")
        # 任意输入均应返回 False
        assert _verify_totp("any_secret", "123456") is False
        assert _verify_totp("ARANDOMBASE32SECRET", "000000") is False
        assert _verify_totp("", "") is False
