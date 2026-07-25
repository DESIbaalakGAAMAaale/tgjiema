"""R72 RC60: backup_once 整体超时与 asyncpg 连接超时修复 — 测试套件。

R72 RC60 整改背景:
    compose-runtime-e2e backup_restore 阶段 600s 超时,根因为 db_backup --once
    调用的 asyncpg 连接池无 command_timeout/timeout 配置,在 CI 环境中因网络
    问题导致连接挂起。RC60 修复:
      1. database/session.py: pool_kwargs 新增 command_timeout=30 + timeout=15
      2. services/db_backup.py: backup_once 接受 timeout 参数(默认 240s),
         用 asyncio.wait_for 包裹内部 backup 逻辑;超时返回 status="timeout"
         evidence 文件并由 main() 退出码 1 标记失败。
      3. scripts/compose_runtime_e2e.py: backup_restore 阶段向 db_backup 命令
         传递 --timeout 240,让 db_backup 先于编排器 600s 超时返回结构化错误。

测试策略:
    - AST 解析验证代码结构(不导入运行时模块,避免 loguru/asyncpg 依赖)
    - 字符串匹配验证关键代码片段
    - 严格遵守 R72 整改规范(无 TODO / pass / 占位符)
"""
from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_BACKUP_PATH = REPO_ROOT / "services" / "db_backup.py"
SESSION_PATH = REPO_ROOT / "database" / "session.py"
COMPOSE_RUNTIME_E2E_PATH = REPO_ROOT / "scripts" / "compose_runtime_e2e.py"


def _parse_function_args(file_path: Path, func_name: str) -> tuple[list[str], list[ast.expr]]:
    """AST 解析函数,返回 (位置参数名列表, 默认值列表)。

    用于在不导入模块的情况下验证函数签名。
    """
    source = file_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            args = node.args
            arg_names = [a.arg for a in args.args]
            return arg_names, list(args.defaults)
    return [], []


def _find_string_in_context(
    file_path: Path, needle: str, context_marker: str, context_size: int = 600
) -> str:
    """在 file_path 中查找 context_marker,返回附近 context_size 字符片段。

    若找不到 context_marker,返回空字符串。
    """
    source = file_path.read_text(encoding="utf-8")
    idx = source.find(context_marker)
    if idx < 0:
        return ""
    return source[idx:idx + context_size]


# ════════════════════════════════════════════════════════════════
# A. database/session.py: pool_kwargs 含 command_timeout + timeout
# ════════════════════════════════════════════════════════════════


class TestSessionPoolKwargsTimeout:
    """R72 RC60 A: CockroachDBClient.connect_runtime_only 的 pool_kwargs 必须含超时。"""

    def _extract_pool_kwargs_keys(self) -> set[str]:
        """通过 AST 解析 connect_runtime_only,提取 pool_kwargs 字典的键。

        支持两种语法:
          - ast.Assign: pool_kwargs = {...}
          - ast.AnnAssign: pool_kwargs: dict = {...}(实际代码用这种)
        """
        source = SESSION_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "connect_runtime_only":
                for sub in ast.walk(node):
                    # 处理 ast.AnnAssign(pool_kwargs: dict = {...})
                    if (
                        isinstance(sub, ast.AnnAssign)
                        and isinstance(sub.target, ast.Name)
                        and sub.target.id == "pool_kwargs"
                        and isinstance(sub.value, ast.Dict)
                    ):
                        return {
                            k.value
                            for k in sub.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)
                        }
                    # 兼容 ast.Assign(pool_kwargs = {...})
                    if (
                        isinstance(sub, ast.Assign)
                        and any(
                            isinstance(t, ast.Name) and t.id == "pool_kwargs"
                            for t in sub.targets
                        )
                        and isinstance(sub.value, ast.Dict)
                    ):
                        return {
                            k.value
                            for k in sub.value.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)
                        }
        return set()

    def test_pool_kwargs_contains_command_timeout(self):
        """pool_kwargs 必须含 command_timeout(单条 SQL 超时)。"""
        keys = self._extract_pool_kwargs_keys()
        assert "command_timeout" in keys, (
            f"R72 RC60: pool_kwargs 必须含 'command_timeout',"
            f"实际 keys: {sorted(keys)}"
        )

    def test_pool_kwargs_contains_timeout(self):
        """pool_kwargs 必须含 timeout(建连超时)。"""
        keys = self._extract_pool_kwargs_keys()
        assert "timeout" in keys, (
            f"R72 RC60: pool_kwargs 必须含 'timeout',"
            f"实际 keys: {sorted(keys)}"
        )

    def test_pool_kwargs_command_timeout_value_is_30(self):
        """command_timeout 必须为 30(秒),覆盖 SQL 查询与写入。"""
        source = SESSION_PATH.read_text(encoding="utf-8")
        assert '"command_timeout": 30' in source or "'command_timeout': 30" in source, (
            "R72 RC60: command_timeout 必须设置为 30 秒"
        )

    def test_pool_kwargs_timeout_value_is_15(self):
        """timeout 必须为 15(秒),覆盖首次建连。"""
        source = SESSION_PATH.read_text(encoding="utf-8")
        assert '"timeout": 15' in source or "'timeout': 15" in source, (
            "R72 RC60: timeout 必须设置为 15 秒"
        )

    def test_pool_kwargs_documents_rc60_rationale(self):
        """pool_kwargs 必须含 RC60 修复说明注释(便于审计追溯)。"""
        source = SESSION_PATH.read_text(encoding="utf-8")
        # 在 pool_kwargs 附近查找 RC60 标记
        idx = source.find("pool_kwargs: dict = {")
        assert idx >= 0, "未找到 pool_kwargs 定义"
        # 查找前后 200 字符内的 RC60 标记
        context = source[max(0, idx - 200):idx + 400]
        assert "RC60" in context, (
            "R72 RC60: pool_kwargs 附近必须含 RC60 修复说明注释"
        )


# ════════════════════════════════════════════════════════════════
# B. services/db_backup.py: backup_once 接受 timeout 参数
# ════════════════════════════════════════════════════════════════


class TestBackupOnceTimeoutParameter:
    """R72 RC60 B: backup_once 必须接受 timeout 参数(默认 240s)。"""

    def test_backup_once_has_timeout_parameter(self):
        """backup_once 函数签名必须含 timeout 参数。"""
        arg_names, _ = _parse_function_args(DB_BACKUP_PATH, "backup_once")
        assert "timeout" in arg_names, (
            f"R72 RC60: backup_once 必须含 timeout 参数,"
            f"实际参数: {arg_names}"
        )

    def test_backup_once_timeout_default_is_240(self):
        """backup_once 的 timeout 参数默认值必须为 240。"""
        arg_names, defaults = _parse_function_args(DB_BACKUP_PATH, "backup_once")
        # backup_once(output_json_path=None, timeout=240)
        # defaults 列表对应最后 N 个参数
        assert len(defaults) >= 2, (
            f"backup_once 应有 2 个带默认值的参数, 实际 defaults 数: {len(defaults)}"
        )
        last_default = defaults[-1]
        assert isinstance(last_default, ast.Constant) and last_default.value == 240, (
            f"R72 RC60: timeout 默认值必须为 240, 实际: {ast.dump(last_default)}"
        )

    def test_backup_once_uses_asyncio_wait_for(self):
        """backup_once 必须使用 asyncio.wait_for 包裹内部 backup 逻辑。"""
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        assert "asyncio.wait_for(" in source, (
            "R72 RC60: backup_once 必须用 asyncio.wait_for 包裹内部逻辑"
        )
        # 进一步验证 wait_for 在 backup_once 函数体内
        idx = source.find("async def backup_once(")
        assert idx >= 0, "未找到 backup_once 函数"
        body = source[idx:]
        assert "asyncio.wait_for(" in body, (
            "R72 RC60: asyncio.wait_for 必须出现在 backup_once 函数体内"
        )

    def test_backup_once_handles_timeout_error(self):
        """backup_once 必须捕获 asyncio.TimeoutError 并返回 status=timeout evidence。"""
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        assert "asyncio.TimeoutError" in source, (
            "R72 RC60: backup_once 必须捕获 asyncio.TimeoutError"
        )
        assert '"status": "timeout"' in source or "'status': 'timeout'" in source, (
            "R72 RC60: backup_once 超时时必须返回 status='timeout' evidence"
        )

    def test_backup_once_timeout_evidence_contains_error_message(self):
        """超时 evidence 必须含 error 字段说明超时原因。

        R72 RC60 整改 i18n 化后,源码不直接含中文字符串,
        改为通过 i18n key 'services.db_backup.s11' 引用超时错误文案。
        本测试同时校验源码引用 key + zh-CN.json 中 key 的值含期望文案。
        """
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        # 1. 源码必须通过 _i18n_t 引用 s11 key(超时错误消息)
        assert "services.db_backup.s11" in source, (
            "R72 RC60: 超时 evidence 必须通过 _i18n_t('services.db_backup.s11') 引用错误文案"
        )
        # 2. zh-CN.json 中 s11 的值必须含 'backup_once 整体超时' 文案
        zh_cn_path = DB_BACKUP_PATH.parent.parent / "locales" / "zh-CN.json"
        zh_cn = json.loads(zh_cn_path.read_text(encoding="utf-8"))
        s11_value = zh_cn.get("services", {}).get("db_backup", {}).get("s11", "")
        assert "backup_once 整体超时" in s11_value, (
            f"R72 RC60: locales/zh-CN.json 中 services.db_backup.s11 必须含 'backup_once 整体超时', 实际: {s11_value}"
        )

    def test_backup_once_writes_timeout_evidence_to_file(self):
        """超时时 evidence 必须写入 output_json_path 文件(供编排器解析)。"""
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        # 验证 TimeoutError except 块中含 output_json_path 写入逻辑
        idx = source.find("except asyncio.TimeoutError")
        assert idx >= 0, "未找到 except asyncio.TimeoutError 块"
        body = source[idx:idx + 1500]
        assert "output_json_path" in body, (
            "R72 RC60: TimeoutError except 块必须写 output_json_path 文件"
        )

    def test_backup_once_inner_function_exists(self):
        """backup_once 必须定义内部 async 函数 _do_backup_inner(被 wait_for 包裹)。"""
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        idx = source.find("async def backup_once(")
        assert idx >= 0, "未找到 backup_once 函数"
        body = source[idx:]
        assert "_do_backup_inner" in body, (
            "R72 RC60: backup_once 内必须定义 _do_backup_inner 内部函数"
        )
        # 验证 asyncio.wait_for 调用 _do_backup_inner
        wait_idx = body.find("asyncio.wait_for(")
        assert wait_idx >= 0, "未找到 asyncio.wait_for 调用"
        # 取 wait_for 后 200 字符,验证调用了 _do_backup_inner
        call_context = body[wait_idx:wait_idx + 200]
        assert "_do_backup_inner" in call_context, (
            f"R72 RC60: asyncio.wait_for 必须调用 _do_backup_inner, 实际片段: {call_context}"
        )


# ════════════════════════════════════════════════════════════════
# C. services/db_backup.py: main() CLI --timeout 参数
# ════════════════════════════════════════════════════════════════


class TestBackupCliTimeoutArg:
    """R72 RC60 C: db_backup CLI main() 必须支持 --timeout 参数。"""

    def test_main_has_timeout_argument(self):
        """main() 必须注册 --timeout argparse 参数。"""
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        assert '"--timeout"' in source or "'--timeout'" in source, (
            "R72 RC60: main() 必须注册 --timeout 参数"
        )

    def test_main_timeout_default_is_240(self):
        """--timeout 参数默认值必须为 240。"""
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        idx = source.find('"--timeout"')
        if idx < 0:
            idx = source.find("'--timeout'")
        assert idx >= 0, "未找到 --timeout 参数定义"
        # 取后续 300 字符查找 default=240
        snippet = source[idx:idx + 300]
        assert "default=240" in snippet, (
            f"R72 RC60: --timeout 默认值必须为 240, 附近代码: {snippet[:200]}"
        )

    def test_main_passes_timeout_to_backup_once(self):
        """main() 必须将 args.timeout 传给 backup_once。"""
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        assert "timeout=args.timeout" in source, (
            "R72 RC60: main() 必须将 args.timeout 传给 backup_once(timeout=...)"
        )

    def test_main_returns_nonzero_on_timeout_status(self):
        """status != 'success' 时 main() 必须返回 1(fail-closed)。"""
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        assert (
            'result.get("status") != "success"' in source
            or "result.get('status') != 'success'" in source
        ), (
            "R72 RC60: main() 必须检查 result['status'], 非 'success' 时返回 1"
        )

    def test_main_rejects_timeout_below_30(self):
        """--timeout < 30 必须拒绝(main() 返回 1)。"""
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        assert "args.timeout < 30" in source, (
            "R72 RC60: main() 必须校验 --timeout >= 30,否则拒绝执行"
        )

    def test_main_help_text_mentions_timeout(self):
        """main() docstring 必须提及 --timeout 用法。"""
        source = DB_BACKUP_PATH.read_text(encoding="utf-8")
        idx = source.find("def main()")
        assert idx >= 0, "未找到 main() 函数"
        # 取 main() 后 1500 字符的 docstring
        body = source[idx:idx + 1500]
        assert "--timeout" in body, (
            "R72 RC60: main() docstring 必须含 --timeout 用法说明"
        )


# ════════════════════════════════════════════════════════════════
# D. compose_runtime_e2e.py: backup_restore 阶段传递 --timeout 240
# ════════════════════════════════════════════════════════════════


class TestOrchestratorPassesTimeout:
    """R72 RC60 D: compose_runtime_e2e backup_restore 阶段必须传递 --timeout 240。"""

    def test_backup_cmd_contains_timeout_240(self):
        """backup_cmd 必须包含 ['--timeout', '240'] 参数。"""
        snippet = _find_string_in_context(
            COMPOSE_RUNTIME_E2E_PATH,
            needle="--timeout",
            context_marker="backup_cmd = _compose_cmd(",
            context_size=800,
        )
        assert snippet, "未找到 backup_cmd = _compose_cmd(...) 定义"
        assert '"--timeout"' in snippet or "'--timeout'" in snippet, (
            f"R72 RC60: backup_cmd 必须含 '--timeout' 参数, 实际片段: {snippet[:300]}"
        )
        assert '"240"' in snippet or "'240'" in snippet, (
            f"R72 RC60: backup_cmd --timeout 值必须为 '240', 实际片段: {snippet[:300]}"
        )

    def test_backup_cmd_comment_mentions_rc60(self):
        """backup_cmd 上方注释必须提及 RC60 修复(便于审计追溯)。"""
        snippet = _find_string_in_context(
            COMPOSE_RUNTIME_E2E_PATH,
            needle="RC60",
            context_marker="backup_cmd = _compose_cmd(",
            context_size=800,
        )
        # 注释在 backup_cmd 之前,需检查 backup_cmd 之前 800 字符
        source = COMPOSE_RUNTIME_E2E_PATH.read_text(encoding="utf-8")
        idx = source.find("backup_cmd = _compose_cmd(")
        assert idx >= 0, "未找到 backup_cmd 定义"
        # 取 backup_cmd 之前 500 字符(注释区)
        before = source[max(0, idx - 500):idx]
        assert "RC60" in before, (
            "R72 RC60: backup_cmd 上方注释必须提及 RC60 修复, "
            f"实际前 500 字符: {before[-200:]}"
        )
