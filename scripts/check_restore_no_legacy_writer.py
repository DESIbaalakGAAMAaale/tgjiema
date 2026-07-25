#!/usr/bin/env python3
"""R65 P0-07 / P1-07 / R66 P0-07 / R67 P1-07: capability-seal 静态门禁 — 禁止生产代码直接调用旧 restore writer。

使用 Python ast 模块解析全仓 .py 文件,检测违规调用以下"旧 restore writer"函数:

默认模式(legacy writer 私有/CLI 入口):
    - _restore_from_backup_data     (services/db_restore.py + services/restore_writer.py 私有写入器)
    - _restore_crdb_tables          (services/db_restore.py + services/restore_writer.py 私有 CRDB 子写入器)
    - _restore_sqlite_tables_to_db  (services/db_restore.py + services/restore_writer.py 私有 SQLite 子写入器)
    - run_restore                   (services/db_restore.py CLI 入口,已被 capability-seal)

R69 Wave 2 整改说明:
    - services/restore_writer.py 是从 services/db_restore.py 提取出的生产 runtime 写入器模块
      (.dockerignore 不排除,生产镜像可用)。
    - services/db_restore.py 保留作为 CLI/tests 入口,生产镜像通过 .dockerignore 排除。
    - 生产路径:backup_dr_validate.validate_and_restore_backup_strict → restore_writer._restore_from_backup_data
    - 旧 db_restore.py 中的 _restore_from_backup_data 仍可被 tests 直接调用(向后兼容)。

--strict 模式(额外检测 strict service / backup wrapper 公共入口):
    - validate_and_restore_backup_strict  (services/backup_dr_validate.py 公共入口)
    - restore_from_backup                (services/db_backup.py 公共 wrapper)

R66 P0-07 整改:
    1. 白名单从"整个文件"改为精确函数+行范围+AST 调用关系
    2. 解析失败必须 fail(不再 skip)
    3. 禁止 wrapper 再导出 legacy writer

R67 P1-07 整改(本次变更):
    白名单从"函数+行范围(line_start/line_end)"改为
    "函数 qualified name + AST signature + source digest",
    禁止行范围授权。原因:
      - 普通注释或格式化会移动行号,导致误报
      - 大范围行区间可能意外包含新调用,导致漏检
    新方案:
      - 每个白名单条目存储函数的 AST signature(归一化 AST dump 的 SHA-256)
        与 source digest(函数源码的 SHA-256)
      - 运行时动态计算被调用函数的 signature/digest,与白名单对比
      - 函数源码任何改动都导致 signature/digest 变化,强制团队更新白名单
        (避免"静默漂移让违规通过")
      - 提供辅助脚本 scripts/regenerate_scanner_whitelist_digests.py
        自动计算并打印新的 signature/digest

违规示例:
    - bots/admin_bot/handlers.py 直接调用 db_restore.run_restore(...)
      → 默认模式报错;生产应改走 RestoreOrchestrator 蓝绿切换路径
    - services/db_backup.py:restore_from_backup() 调用 validate_and_restore_backup_strict()
      → --strict 模式报错;生产应改走 RestoreOrchestrator

CI 调用方式:
    # ci.yml(static-gates job)— 默认模式
    python scripts/check_restore_no_legacy_writer.py

    # release-gates.yml(--strict 模式)
    python scripts/check_restore_no_legacy_writer.py --strict

退出码:
    - 0: 无违规
    - 1: 检测到违规(或解析失败 — R66 P0-07)
    - 2: 严重错误(参数解析失败等)
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from pathlib import Path
from typing import Iterable

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 默认模式:扫描的 legacy writer 函数名(私有写入器 + CLI 入口)
LEGACY_WRITER_FUNDS_DEFAULT: set[str] = {
    "_restore_from_backup_data",
    "_restore_crdb_tables",
    "_restore_sqlite_tables_to_db",
    "run_restore",
}

# --strict 模式:额外扫描的 strict service / backup wrapper 公共入口
LEGACY_WRITER_FUNDS_STRICT_EXTRA: set[str] = {
    "validate_and_restore_backup_strict",
    "restore_from_backup",
}


# ═══════════════════════════════════════════════════════════════
# R67 P1-07: 精确白名单 — 函数 qualified name + AST signature + source digest
# ═══════════════════════════════════════════════════════════════
# 每个条目:
#   - file: 相对仓库根的 POSIX 路径
#   - function: 函数名(enclosing function name)
#   - ast_signature: 归一化 AST dump 的 SHA-256(忽略 lineno/col_offset/
#       end_lineno/end_col_offset/type_comment/type_ignores 等位置属性)
#   - source_digest: 函数源码的 SHA-256(ast.get_source_segment 提取)
#   - allowed_callees: 允许调用的 legacy writer 函数名 frozenset
#   - reason: 人类可读的允许原因
#
# 设计原则(R67 P1-07):
#   - 不再使用 line_start/line_end 行范围授权(行号会因注释/格式化漂移,
#     且大范围行区间可能意外包含新调用)
#   - 改用 ast_signature + source_digest 双重绑定:函数源码任何改动都
#     导致 signature/digest 变化,scanner 拒绝授权,强制团队更新白名单
#   - ast_signature 捕捉语义结构变化(语句/表达式/控制流)
#   - source_digest 捕捉字面源码变化(注释/字符串字面量/格式)
#   - **R67 P1-07 hotfix**: 授权判定以 source_digest 为主(跨 Python 版本稳定),
#     ast_signature 仅作为诊断信号(不阻塞)。原因:`ast.dump()` /
#     `ast.iter_fields()` 在不同 Python 版本(3.10/3.11/3.12/3.13/3.14)
#     之间产生的字段顺序/字段集不同(如 Python 3.14 新增 type_params /
#     TypeAlias 节点变化),导致同一函数源码在不同解释器下计算得到不同的
#     ast_signature。source_digest 基于 `ast.get_source_segment` 提取的
#     字面源码 SHA-256,不依赖 AST 内部表示,跨版本稳定。
#   - 因此:source_digest 匹配即授权通过(函数源码未被修改);
#     ast_signature 不匹配时仅打印诊断警告(不阻塞),供团队参考。
#   - 当白名单条目过期时(source_digest 不匹配 = 函数已修改),scanner 输出
#     详细的过期条目信息,指引运行 scripts/regenerate_scanner_whitelist_digests.py
#     重新生成。
PRECISE_WHITELIST: tuple[dict, ...] = (
    # R70 Wave 7: db_restore.py::_restore_from_backup_data 已移除 — 本模块改为
    # 薄 CLI adapter,_restore_from_backup_data 通过 re-export 从
    # services/restore_writer.py 导入(单一事实源)。本模块不再定义任何 writer
    # 实现,scanner 不再需要为 db_restore.py 中的 _restore_from_backup_data
    # 授权。授权移至 services/restore_writer.py::_restore_from_backup_data(下方)。
    # db_restore.py: run_restore CLI 入口委托给 validate_and_restore_backup_strict
    # (run_restore 本身被 ALLOW_LEGACY_RESTORE seal,但仍允许调用 strict service)
    # R67 P0-06: 在 capability-seal 之前增加 _production_guard 硬守卫(生产环境
    # APP_ENV=production|staging 无条件拒绝,不允许 ALLOW_LEGACY_RESTORE 解封)。
    # 守卫调用不调用 legacy writer,因此不影响白名单 allowed_callees。
    # R70 Wave 7: db_restore.py 重写为薄 adapter,run_restore 源码随之变化,
    # source_digest 重新生成。
    # R72 RC69: 添加 BACKUP_SIGNING_KEY 字符串到字节转换逻辑(供 HMAC 签名),
    # source_digest 重新生成。函数语义未变(仍是 CLI 入口 → strict service 委托)。
    {
        "file": "services/db_restore.py",
        "function": "run_restore",
        "ast_signature": "579eadb612b09618369620cf58b041a753ccf4dbb0ffc61d7686e401582eabe9",
        "source_digest": "eae9c989c23fe3212e4787ed26d63be24410a757e915be100abb0127dbd1b002",
        "allowed_callees": frozenset({"validate_and_restore_backup_strict"}),
        "reason": "CLI 入口委托:run_restore → validate_and_restore_backup_strict(strict service)",
    },
    # db_restore.py: main() CLI argparse 入口委托给 run_restore
    # R72: --target / --backup-id / --output-json 参数新增,source_digest 重新生成
    {
        "file": "services/db_restore.py",
        "function": "main",
        "ast_signature": "c19a5fc788f825213e1a57d4f86c4c30c7f0da23605c8e5d4e4f188a63ff33cd",
        "source_digest": "fd543ce76c47248bc40ec11d44e9a791d944ac5b8d370921265b0b9e338845ce",
        "allowed_callees": frozenset({"run_restore"}),
        "reason": "CLI argparse 入口委托:main → run_restore(运行时由 run_restore 的 seal 防护)",
    },
    # backup_dr_validate.py: validate_and_restore_backup_strict 构造 capability 后调用私有写入器
    # R69 Wave 2: 延迟 import 改为 from services.restore_writer(不再 import services.db_restore),
    # source_digest 随之更新。allowed_callees 仍为 _restore_from_backup_data(restore_writer.py 中)。
    {
        "file": "services/backup_dr_validate.py",
        "function": "validate_and_restore_backup_strict",
        "ast_signature": "b5542cdc9e9173653bd91693238d7e897bfab06afd1eb1b22b7c1aebe4fdca5e",
        "source_digest": "4f51443ba979d72d30a2ffe7337ae903b2bd4c43b8a11d26affa9e4c6cd77273",
        "allowed_callees": frozenset({"_restore_from_backup_data"}),
        "reason": "strict service 构造 capability 后调用私有写入器(R69 Wave 2: import 改为 services.restore_writer)",
    },
    # backup_dr_validate.py: _restore_preverified_payload 内部委托给 _restore_from_backup_data
    # R69 Wave 2: 延迟 import 改为 from services.restore_writer,source_digest 随之更新。
    {
        "file": "services/backup_dr_validate.py",
        "function": "_restore_preverified_payload",
        "ast_signature": "7076e586fde43d12790d4f568dc40ad6b47b510e43527203abbb207a3c0a41f3",
        "source_digest": "a60051021ac2397dcf4bfebabe94831f2d71352e9daa60e0e72c2cc01ed9b933",
        "allowed_callees": frozenset({"_restore_from_backup_data"}),
        "reason": "preverified payload 路径委托给私有写入器(R69 Wave 2: import 改为 services.restore_writer)",
    },
    # R69 Wave 2: services/restore_writer.py 是从 services/db_restore.py 提取出的
    # 生产 runtime 写入器模块(.dockerignore 不排除本文件,生产镜像可用)。
    # _restore_from_backup_data 内部委托给同模块子写入器(_restore_crdb_tables /
    # _restore_sqlite_tables_to_db),与 db_restore.py 中的旧实现保持语义一致。
    # 生产路径:backup_dr_validate.validate_and_restore_backup_strict → restore_writer._restore_from_backup_data
    # 旧 db_restore.py 保留作为 CLI/tests 入口,生产镜像通过 .dockerignore 排除。
    {
        "file": "services/restore_writer.py",
        "function": "_restore_from_backup_data",
        "ast_signature": "65ff17d599a0afebc23b6d8946976f6588cb34190045e1b1632a5e97935a3410",
        "source_digest": "cba917d40bf23f9ad855aff7ce340da46604f2b5eae030a6fe7eec1526ba60e7",
        "allowed_callees": frozenset({"_restore_crdb_tables", "_restore_sqlite_tables_to_db"}),
        "reason": "R69 Wave 2: restore_writer 同模块私有委托(从 db_restore.py 提取,生产镜像可用)",
    },
    # R66 P0-07: 已 sealed 的生产入口(带 ALLOW_LEGACY_RESTORE capability-seal 检查)
    # R67 P0-06: 在 capability-seal 之前增加 _production_guard 硬守卫。
    # 长期目标:迁移到 RestoreOrchestrator 蓝绿切换路径后移除这些白名单条目。
    #
    # db_backup.py: restore_from_backup 公共入口委托给 validate_and_restore_backup_strict
    # R72 RC69: 添加 BACKUP_SIGNING_KEY 字符串到字节转换逻辑(供 HMAC 签名),
    # source_digest 重新生成。函数语义未变(仍是 sealed 公共入口 → strict service 委托)。
    {
        "file": "services/db_backup.py",
        "function": "restore_from_backup",
        "ast_signature": "fd9d01c2f1569c7872fbc9a72dc237cfc2028048995184904392efedf797b88f",
        "source_digest": "23219be711b675bbfdda5f71cef846e98516d5105940a878b308a5084cc8b849",
        "allowed_callees": frozenset({"validate_and_restore_backup_strict"}),
        "reason": (
            "sealed 公共入口:"
            "生产环境被 RESTORE_LEGACY_WRITER_SEALED 阻断,"
            "仅 ALLOW_LEGACY_RESTORE=1 时委托 strict service 路径"
        ),
    },
    # command_bus.py: make_restore_backup_command 内的 _handler 委托给 db_backup.restore_from_backup
    # 注意:command_bus.py 中有 14 个 _handler 闭包(每个 make_*_command 一个),
    # 此处 signature/digest 对应 make_restore_backup_command 内的 _handler
    # (lines 2381-2435,包含 restore_from_backup 调用)。
    # 当 _handler 函数源码变化时,signature/digest 失配,scanner 拒绝授权,
    # 强制团队运行 regenerate_scanner_whitelist_digests.py 重新计算。
    {
        "file": "services/command_bus.py",
        "function": "_handler",
        "ast_signature": "46a96b6991bc5d996e53b1a6ad3ac7efae7a0ff1c0535f8c7bda2feab68fe7ee",
        "source_digest": "bdacb7201cb892702845cf13991bd5d369fce16282f336fb027dd841581d64ea",
        "allowed_callees": frozenset({"restore_from_backup"}),
        "reason": (
            "sealed command handler (make_restore_backup_command._handler):"
            "生产环境被 RESTORE_LEGACY_WRITER_SEALED 阻断,"
            "仅 ALLOW_LEGACY_RESTORE=1 时委托 sealed 公共入口"
        ),
    },
)

# 完整跳过的白名单文件(不扫描)— 仅引用错误码字符串,无 legacy writer 调用
WHITELIST_FILES_FULL_SKIP: frozenset[str] = frozenset({
    "services/error_codes.py",  # 仅引用错误码字符串(RESTORE_LEGACY_WRITER_SEALED),非调用
})

# 白名单目录(完整跳过)— 测试逃生舱 + gate 脚本自身
# R67 P1-08: tests/ 完整跳过合理(测试逃生舱),但 scripts/ 不再整体跳过 —
# scripts/ 含真实运维入口(full_machine_recovery.sh / blank_vps_recovery_test.sh
# / chaos_bot_fault_injection.sh / ru_72h_verification.sh / soak_test_7day.sh)
# 与治理脚本(configure_branch_protection.sh 等),必须接受 capability/approval
# /MFA 审查。Python 脚本中的 legacy writer 调用应通过 PRECISE_WHITELIST 显式
# 授权,而不是默认跳过整个目录。
#
# 整改(R67 P1-08):移除 "scripts/" 前缀,改用 `is_skippable_script()` 细粒度
# 判断 — 仅 GATE_SCANNERS 可跳过(避免自引用噪声);OFFLINE_RECOVERY_TOOLS
# 与 GOVERNANCE_SCRIPTS 必须被扫描。
WHITELIST_DIR_PREFIXES: tuple[str, ...] = (
    "tests/",
)

# R67 P1-08: scripts/ 下可跳过的文件清单(从 _script_categories 导入)
# 仅 gate 自身与辅助生成器可跳过;离线恢复工具与治理脚本必须被扫描。
try:
    from scripts._script_categories import is_skippable_script as _is_skippable_script_p1_08
except ImportError:
    # _script_categories 不可用时 fail-closed:不跳过任何 scripts/ 文件
    def _is_skippable_script_p1_08(rel_path: str) -> bool:
        return False

# 跳过的目录(不扫描)
SKIP_DIR_PARTS: list[str] = [
    "__pycache__",
    ".git",
    "node_modules",
    "venv",
    ".venv",
    ".pytest_cache",
    "data",
    "logs",
    "backups",
    "production-evidence",
    "migrations",
]

# 跳过的文件名(不扫描,通常是文档/报告)
SKIP_FILE_SUFFIXES: tuple[str, ...] = (
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".txt",
    ".sql",
    ".html",
    ".css",
    ".js",
)


# ═══════════════════════════════════════════════════════════════
# R67 P1-07: AST signature + source digest 计算
# ═══════════════════════════════════════════════════════════════

# AST 节点中应忽略的位置/元数据属性(不影响语义)
_AST_LOCATION_FIELDS: frozenset[str] = frozenset({
    "lineno",
    "col_offset",
    "end_lineno",
    "end_col_offset",
    "type_comment",
    "type_ignores",
})


def _normalize_ast(node: ast.AST) -> str:
    """递归归一化 AST 节点,忽略位置属性。

    返回值结构示例:
        FunctionDef(name='foo', args=arguments(...), body=[...], ...)
        Call(func=Name(id='bar'), args=[], keywords=[])
        Constant(value='hello')

    所有 lineno/col_offset/end_lineno/end_col_offset/type_comment/type_ignores
    属性均被忽略,因此注释/空行/格式调整不影响归一化结果。

    Args:
        node: AST 节点(可以是 FunctionDef / 表达式 / 列表等)

    Returns:
        归一化的字符串表示
    """
    if isinstance(node, ast.AST):
        fields = []
        for fname, fvalue in ast.iter_fields(node):
            if fname in _AST_LOCATION_FIELDS:
                continue
            fields.append(f"{fname}={_normalize_ast(fvalue)}")
        return f"{type(node).__name__}({', '.join(fields)})"
    elif isinstance(node, list):
        return f"[{', '.join(_normalize_ast(item) for item in node)}]"
    else:
        return repr(node)


def compute_ast_signature(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> str:
    """R67 P1-07: 计算函数的 AST signature(归一化 AST dump 的 SHA-256)。

    Args:
        func_node: FunctionDef / AsyncFunctionDef 节点

    Returns:
        64 字符 SHA-256 hex 字符串
    """
    normalized = _normalize_ast(func_node)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_source_digest(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef,
    source: str,
) -> str:
    """R67 P1-07: 计算函数源码的 SHA-256 digest。

    使用 ast.get_source_segment 提取函数源码(包含装饰器、docstring、
    注释、字面量等),计算 SHA-256。源码任何字面变化都导致 digest 变化。

    Args:
        func_node: FunctionDef / AsyncFunctionDef 节点
        source: 包含该函数的完整文件源码

    Returns:
        64 字符 SHA-256 hex 字符串
    """
    segment = ast.get_source_segment(source, func_node) or ""
    return hashlib.sha256(segment.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════
# 文件路径与白名单辅助
# ═══════════════════════════════════════════════════════════════


def _rel_posix(path: Path) -> str:
    """返回相对 REPO_ROOT 的 POSIX 路径字符串(用 / 分隔)。"""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _is_skipped_path(path: Path) -> bool:
    """检查路径是否应跳过(在 SKIP_DIR_PARTS 中或后缀在 SKIP_FILE_SUFFIXES 中)。"""
    rel = _rel_posix(path)
    for part in SKIP_DIR_PARTS:
        if part in rel:
            return True
    if path.suffix and path.suffix not in (".py",):
        return True
    return False


def _is_whitelisted(path: Path) -> bool:
    """R66 P0-07: 检查文件是否完全跳过(不扫描)。

    注意:此函数仅返回 True 表示"完全跳过扫描"。
    db_restore.py / backup_dr_validate.py 不再完全跳过,
    而是通过 PRECISE_WHITELIST 进行函数级精确白名单检查。

    R67 P1-08: scripts/ 不再整体跳过 — 通过 `is_skippable_script()`
    细粒度判断。仅 GATE_SCANNERS 可跳过;OFFLINE_RECOVERY_TOOLS
    与 GOVERNANCE_SCRIPTS 必须被扫描。
    """
    rel = _rel_posix(path)
    # 完整跳过的白名单文件(精确匹配)
    if rel in WHITELIST_FILES_FULL_SKIP:
        return True
    # 白名单目录(前缀匹配)
    for prefix in WHITELIST_DIR_PREFIXES:
        if rel.startswith(prefix):
            return True
    # R67 P1-08: scripts/ 细粒度判断
    if rel.startswith("scripts/") and _is_skippable_script_p1_08(rel):
        return True
    return False


def _is_call_allowed(
    file_rel: str,
    enclosing: str | None,
    callee: str,
    line: int,
    *,
    enclosing_func_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None,
    source: str | None = None,
) -> bool:
    """R67 P1-07: 检查 (file, enclosing_function, callee) 是否在精确白名单中。

    新方案(禁止行范围授权):
        1. 匹配 (file, enclosing_function, callee ∈ allowed_callees)
        2. 计算当前 enclosing function 的 source_digest(主授权信号)
        3. 与白名单条目中的 source_digest 对比
        4. **source_digest 匹配即授权通过**(R67 P1-07 hotfix)
        5. ast_signature 作为诊断信号 — 不匹配时打印警告(不阻塞)

    R67 P1-07 hotfix 说明(2026-07):
        原方案要求 ast_signature AND source_digest 均匹配。但 `ast.dump()` /
        `ast.iter_fields()` 在 Python 3.10/3.11/3.12/3.13/3.14 之间字段集不同
        (例如 Python 3.14 在 FunctionDef 上新增 type_params 字段、TypeAlias
        节点结构变化),导致同一函数源码在不同解释器下产生不同的 ast_signature。
        CI 矩阵运行 Python 3.10/3.11/3.12,本地开发常用 3.13/3.14,白名单
        ast_signature 由其中一个版本生成,在其他版本上必然失配 — 误报违规。
        source_digest 基于 `ast.get_source_segment` 提取的字面源码 SHA-256,
        不依赖 AST 内部表示,跨版本稳定,因此改为 source_digest 单独充分授权。

    若 enclosing_func_node 或 source 未提供(向后兼容旧测试),
    仅做 (file, function, callee) 匹配,不校验 signature/digest。
    **生产 scanner 主流程必须提供这两个参数**;不提供时仅用于历史测试。

    Args:
        file_rel: POSIX 相对路径
        enclosing: 调用所在的函数名(None 表示模块级)
        callee: 被调用的函数名
        line: 调用所在行号(保留参数,新方案不使用,但保持向后兼容)
        enclosing_func_node: 调用所在的 FunctionDef 节点(用于计算 signature)
        source: 包含该函数的文件源码(用于计算 source_digest)

    Returns:
        True 如果在精确白名单中且 source_digest 匹配(允许调用)
    """
    if enclosing is None:
        # 模块级调用 legacy writer 永远不允许
        return False
    for entry in PRECISE_WHITELIST:
        if (
            entry["file"] == file_rel
            and entry["function"] == enclosing
            and callee in entry["allowed_callees"]
        ):
            # (file, function, callee) 匹配
            # R67 P1-07: 进一步校验 source_digest(主授权信号)
            if enclosing_func_node is None or source is None:
                # 向后兼容:未提供 function node / source 时仅做基本匹配
                # (生产 scanner 主流程必须提供,这里仅用于历史测试)
                return True
            actual_sig = compute_ast_signature(enclosing_func_node)
            actual_src = compute_source_digest(enclosing_func_node, source)
            # R67 P1-07 hotfix: source_digest 匹配即授权(跨版本稳定)
            if actual_src == entry["source_digest"]:
                # ast_signature 不匹配时打印诊断警告(不阻塞)
                # 跨 Python 版本时 ast_signature 必然失配,这是预期的
                if actual_sig != entry["ast_signature"]:
                    print(
                        f"[scanner] WARN: ast_signature 跨版本失配 "
                        f"(非阻塞,source_digest 已匹配): "
                        f"{file_rel}::{enclosing}() — "
                        f"expected {entry['ast_signature'][:12]}..., "
                        f"actual {actual_sig[:12]}...",
                        file=sys.stderr,
                    )
                return True
            # source_digest 不匹配 — 函数源码已修改,白名单过期
            # 返回 False,scanner 会将其作为违规报告
            print(
                f"[scanner] STALE: source_digest 不匹配 — "
                f"函数 {file_rel}::{enclosing}() 源码已修改,白名单过期。"
                f"运行 scripts/regenerate_scanner_whitelist_digests.py 重新生成。",
                file=sys.stderr,
            )
            return False
    return False


def _iter_python_files() -> Iterable[Path]:
    """遍历 REPO_ROOT 下所有 .py 文件(跳过缓存/数据/白名单后缀目录)。"""
    for py_file in REPO_ROOT.rglob("*.py"):
        if _is_skipped_path(py_file):
            continue
        yield py_file


def _get_call_func_name(node: ast.Call) -> str | None:
    """提取 Call 节点的函数名(只看最后一段 attr 或 Name)。

    例如:
        db_restore.run_restore(...)        → "run_restore"
        _restore_from_backup_data(...)     → "_restore_from_backup_data"
        obj.method.run_restore(...)        → "run_restore"
        print(...)                          → "print"

    Returns:
        函数名字符串,无法识别时返回 None
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _build_parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """构建 parent map: {node_id: parent_node},用于查找 enclosing function。"""
    parent_map: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[id(child)] = parent
    return parent_map


def _find_enclosing_function_node(
    node: ast.AST,
    parent_map: dict[int, ast.AST],
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """R67 P1-07: 找到节点最近的 enclosing FunctionDef 节点对象。

    与 _find_enclosing_function 不同,本函数返回 AST 节点(而非仅函数名),
    以便后续计算 ast_signature + source_digest。

    Args:
        node: 起始节点
        parent_map: 父节点映射

    Returns:
        最近的 FunctionDef / AsyncFunctionDef 节点,或 None(模块级)
    """
    current = parent_map.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parent_map.get(id(current))
    return None


def _find_enclosing_function(
    node: ast.AST,
    parent_map: dict[int, ast.AST],
) -> str | None:
    """找到节点最近的 enclosing function 名(或 None 表示模块级)。

    通过 parent map 向上遍历,找到最近的 FunctionDef / AsyncFunctionDef。
    """
    current = parent_map.get(id(node))
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parent_map.get(id(current))
    return None


def _find_legacy_calls(
    tree: ast.AST,
    legacy_funds: set[str],
    parent_map: dict[int, ast.AST],
) -> list[dict]:
    """查找 AST 中所有 legacy writer 调用(不做白名单过滤)。

    Returns:
        [{line, col, func, enclosing, enclosing_node}, ...]
        enclosing: 调用所在的函数名(None 表示模块级)
        enclosing_node: 调用所在的 FunctionDef 节点(None 表示模块级)
    """
    calls: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func_name = _get_call_func_name(node)
            if func_name and func_name in legacy_funds:
                enclosing = _find_enclosing_function(node, parent_map)
                enclosing_node = _find_enclosing_function_node(node, parent_map)
                calls.append({
                    "line": node.lineno,
                    "col": node.col_offset,
                    "func": func_name,
                    "enclosing": enclosing,
                    "enclosing_node": enclosing_node,
                })
    return calls


def _find_reexport_violations(
    tree: ast.AST,
    legacy_funds: set[str],
) -> list[dict]:
    """R66 P0-07: 检测 wrapper re-export 违规:

    1. __all__ 包含 legacy writer 名 → 违规(显式再导出)
    2. from X import legacy_writer as alias (alias != legacy_writer) → 违规(别名再导出)

    Returns:
        [{line, col, func, enclosing}, ...]
    """
    violations: list[dict] = []
    for node in tree.body:
        # __all__ = [...] 检查(普通赋值)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        for elt in node.value.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                if elt.value in legacy_funds:
                                    violations.append({
                                        "line": node.lineno,
                                        "col": node.col_offset,
                                        "func": elt.value,
                                        "enclosing": "__all__",
                                    })
        # __all__: list[str] = [...] 检查(带类型注解的赋值)
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == "__all__"
                and node.value is not None
                and isinstance(node.value, (ast.List, ast.Tuple))
            ):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        if elt.value in legacy_funds:
                            violations.append({
                                                "line": node.lineno,
                                                "col": node.col_offset,
                                                "func": elt.value,
                                                "enclosing": "__all__",
                                            })
        # from X import Y as Z 检查(别名再导出)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (
                    alias.name in legacy_funds
                    and alias.asname is not None
                    and alias.asname != alias.name
                ):
                    violations.append({
                        "line": node.lineno,
                        "col": node.col_offset,
                        "func": alias.name,
                        "enclosing": f"import_as_{alias.asname}",
                    })
    return violations


def _find_violations(
    tree: ast.AST,
    legacy_funds: set[str],
) -> list[tuple[int, int, str]]:
    """向后兼容: 查找 AST 中所有 legacy writer 调用(不做白名单过滤)。

    注意:此函数仅返回原始调用列表(不含 enclosing 信息与白名单过滤)。
    新代码应使用 _find_legacy_calls + _is_call_allowed 进行精确白名单检查。

    Returns:
        [(lineno, col_offset, func_name), ...]
    """
    parent_map = _build_parent_map(tree)
    calls = _find_legacy_calls(tree, legacy_funds, parent_map)
    return [(c["line"], c["col"], c["func"]) for c in calls]


def check(strict: bool = False) -> tuple[int, list[dict]]:
    """主校验流程。

    Args:
        strict: 是否启用 --strict 模式(扫描更广的 legacy writer 集合)

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规(或解析失败 — R66 P0-07)
        violations: 违规列表 [{file, line, col, func, enclosing}, ...]
    """
    # 构建当前模式的 legacy writer 函数集合
    legacy_funds = set(LEGACY_WRITER_FUNDS_DEFAULT)
    if strict:
        legacy_funds |= LEGACY_WRITER_FUNDS_STRICT_EXTRA

    violations: list[dict] = []
    parse_errors: list[dict] = []
    scanned_count = 0
    whitelisted_skipped = 0

    for py_file in _iter_python_files():
        rel = _rel_posix(py_file)
        # 完整跳过的白名单文件(error_codes.py / tests/ / scripts/)
        if _is_whitelisted(py_file):
            whitelisted_skipped += 1
            continue
        scanned_count += 1

        # R66 P0-07: 解析失败必须 fail(不再 skip),防止语法/编码异常让扫描器漏检
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError, OSError) as e:
            parse_errors.append({
                "file": rel,
                "error": f"{type(e).__name__}: {e}",
            })
            continue

        parent_map = _build_parent_map(tree)

        # 查找所有 legacy writer 调用,按精确白名单过滤
        calls = _find_legacy_calls(tree, legacy_funds, parent_map)
        for c in calls:
            if not _is_call_allowed(
                rel, c["enclosing"], c["func"], c["line"],
                enclosing_func_node=c.get("enclosing_node"),
                source=source,
            ):
                violations.append({
                    "file": rel,
                    "line": c["line"],
                    "col": c["col"],
                    "func": c["func"],
                    "enclosing": c["enclosing"],
                })

        # R66 P0-07: 检测 wrapper re-export 违规(__all__ / from ... import ... as ...)
        reexports = _find_reexport_violations(tree, legacy_funds)
        for r in reexports:
            violations.append({
                "file": rel,
                "line": r["line"],
                "col": r["col"],
                "func": r["func"],
                "enclosing": r["enclosing"],
            })

    # R66 P0-07: 解析失败必须 fail(不再 skip)
    if parse_errors:
        print(
            f"[FAIL] R66 P0-07: 检测到 {len(parse_errors)} 个文件解析失败 "
            f"(必须 fail,防止语法/编码异常让扫描器漏检):"
        )
        for pe in parse_errors:
            print(f"  - {pe['file']}: {pe['error']}")
        print()
        print("R66 P0-07 整改: 解析失败必须 fail(不再 skip)。")
        print("请修复语法/编码错误后重新运行 scanner。")
        return 1, violations

    if violations:
        mode_label = "--strict" if strict else "default"
        print(
            f"[FAIL] 检测到 {len(violations)} 处违规调用旧 restore writer "
            f"(模式: {mode_label}, 扫描 {scanned_count} 个生产 .py 文件,"
            f"白名单跳过 {whitelisted_skipped} 个文件):"
        )
        for v in violations:
            enclosing = v["enclosing"] if v["enclosing"] else "<module>"
            print(
                f"  - {v['file']}:{v['line']}:{v['col']} -> "
                f"调用 {v['func']!r} (in {enclosing})"
            )
        print()
        print("R65 P0-07 / P1-07 / R66 P0-07 / R67 P1-07: 旧直接 restore writer 已被 capability-seal,")
        print("生产恢复必须通过 RestoreOrchestrator 蓝绿切换路径执行:")
        print("  1. start_operation(backup_id, manifest_digest, payload_digest, nonce)")
        print("  2. provision_staging(operation_id)")
        print("  3. restore_to_staging(operation_id, datasource)")
        print("  4. validate_staging(operation_id)")
        print("  5. request_approval(operation_id, approval_id, mfa_receipt_id)")
        print("  6. execute_blue_green_switch(operation_id, approval_id, mfa_receipt_id)")
        print()
        print("逃生舱(仅限 tests/ 与 scripts/):")
        print("  设置环境变量 ALLOW_LEGACY_RESTORE=1")
        print("  生产部署绝不应配置此环境变量(应在系统层强制 unset)。")
        print()
        print("R67 P1-07 精确白名单(函数 qualified name + AST signature + source digest):")
        for entry in PRECISE_WHITELIST:
            callees = ", ".join(sorted(entry["allowed_callees"]))
            print(
                f"  - {entry['file']}::{entry['function']}() "
                f"→ 可调用: {callees}"
            )
            print(f"    ast_signature: {entry['ast_signature']}")
            print(f"    source_digest: {entry['source_digest']}")
        print()
        print("若白名单条目因函数修改而过期,请运行:")
        print("  python3 scripts/regenerate_scanner_whitelist_digests.py")
        print("重新计算并更新 signature/digest。")
        print()
        print("完全跳过的白名单(仅引用错误码字符串,非调用):")
        for f in sorted(WHITELIST_FILES_FULL_SKIP):
            print(f"  - {f}")
        for d in sorted(WHITELIST_DIR_PREFIXES):
            print(f"  - {d}*")
        return 1, violations

    mode_label = "--strict" if strict else "default"
    print(
        f"[OK] R65 P0-07 / P1-07 / R66 P0-07 / R67 P1-07 capability-seal 门禁检查通过 "
        f"(模式: {mode_label}, 扫描 {scanned_count} 个生产 .py 文件,"
        f"白名单跳过 {whitelisted_skipped} 个文件,无违规调用)"
    )
    return 0, violations


def main() -> None:
    """脚本入口。"""
    parser = argparse.ArgumentParser(
        description=(
            "R65 P0-07 / P1-07 / R66 P0-07 / R67 P1-07: capability-seal 静态门禁 — "
            "禁止生产代码直接调用旧 restore writer。"
        )
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "严格模式:额外检测 validate_and_restore_backup_strict 与 "
            "restore_from_backup 调用(覆盖 strict service 公共入口)。"
            "默认模式仅检测私有 writer(_restore_from_backup_data 等)与 CLI 入口。"
        ),
    )
    args = parser.parse_args()
    exit_code, _ = check(strict=args.strict)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
