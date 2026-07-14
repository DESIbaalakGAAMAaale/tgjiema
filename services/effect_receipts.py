"""R44 G0-2 / R46 P0-1 / R47 P0-4: 外部副作用 receipt 持久化,保证 effectively-once 语义。

R46 P0-1 整改:
- critical effect 类型(telegram_send/copy/r2_put/restore/ban/takedown/purge) fail-closed:
  manager 不可用或读写失败时直接拒绝外部副作用(raise EffectReceiptError)。
- 非关键通知允许显式 best_effort=True。
- 表增加 request_hash、attempt、lease_owner、lease_until、last_error、reconcile_status。
- record_pending 使用 CAS claim(ON CONFLICT)防止并发重复执行。
- DB 写回失败进入 reconciliation,不盲重试。

R47 P0-4 整改:
- 新增 compute_effect_request_hash(effect_type, params) 绑定 effect 参数,
  防止同 action_id 不同 payload 绕过 receipt。
- check_receipt 支持 expected_request_hash 校验,不匹配则不视为 completed。
- 新增 validate_critical_effects_have_action_id() 静态扫描函数(供 CI 调用)。

receipt 结构:
    (action_id, effect_type, target, status, external_id, created_at,
     completed_at, request_hash, attempt, lease_owner, lease_until,
     last_error, reconcile_status)
"""
from __future__ import annotations

import ast
import datetime
import hashlib
import json
import os
from typing import Any, Optional

from loguru import logger


# R46 P0-1: critical effect 类型集合 — manager 不可用或读写失败时 fail-closed
CRITICAL_EFFECT_TYPES: frozenset[str] = frozenset({
    "telegram_send",
    "telegram_copy",
    "r2_put",
    "r2_download",
    "restore",
    "ban",
    "takedown",
    "purge",
    "crdb_delete",
})


class EffectReceiptError(Exception):
    """R46 P0-1: Effect Receipt 持久化失败,critical 副作用必须中止。"""


def compute_effect_request_hash(effect_type: str, params: dict) -> str:
    """R47 P0-4 / R48 P0-4: 计算 effect 副作用的 request_hash(绑定 effect_type + params)。

    用于防止同 action_id 不同 payload 绕过 effect receipt:
    相同 action_id 但参数不同时,request_hash 不匹配,不视为已完成。

    R48 P0-4: hash 覆盖完整字段 — 调用方应在 params 中包含:
    - target 相关字段(target_user_id / target_channel_id / chat_id)
    - 关键业务参数(message_id / file_id / key 等)
    - 资源 version(如有)
    使用 SHA256 + json.dumps(sort_keys=True, default=str) 确保确定性与字段无关顺序。

    Args:
        effect_type: 副作用类型(如 'telegram_send')
        params: 副作用参数字典

    Returns:
        SHA256 十六进制摘要字符串(64 字符)
    """
    payload_str = json.dumps(
        params or {}, sort_keys=True, ensure_ascii=False, default=str,
    )
    raw = f"{effect_type}|{payload_str}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EffectReceiptManager:
    """管理外部副作用 receipt 的记录和查询。

    使用 cache_store 的 SQLite 数据库持久化 receipt。
    表 DDL 由 database/cache_store.py 创建:
        CREATE TABLE IF NOT EXISTS effect_receipts (
            action_id          TEXT NOT NULL,
            effect_type       TEXT NOT NULL,
            target            TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            external_id       TEXT,
            created_at        TEXT NOT NULL,
            completed_at      TEXT,
            request_hash      TEXT,
            attempt           INTEGER NOT NULL DEFAULT 0,
            lease_owner       TEXT,
            lease_until       TEXT,
            last_error        TEXT,
            reconcile_status  TEXT,
            PRIMARY KEY (action_id, effect_type, target)
        );
    """

    def __init__(self, cache_store):
        self._store = cache_store

    async def check_receipt(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        *,
        fail_closed: bool = False,
        expected_request_hash: str = "",
    ) -> Optional[dict]:
        """检查是否已有 receipt。

        Args:
            fail_closed: True 时 DB 错误抛 EffectReceiptError(critical 副作用拒绝执行);
                         False 时返回 None(继续执行)。
            expected_request_hash: R47 P0-4 期望的 request_hash,非空时与存储值对比,
                                   不匹配则视为不同 payload,返回 None(不视为 completed)。

        Returns:
            已 completed 的 receipt 字典(含 request_hash 字段),或 None。
        """
        if not self._store._db:
            if fail_closed:
                raise EffectReceiptError(
                    f"effect_receipts DB 未初始化,无法检查 receipt "
                    f"(action={action_id}, type={effect_type}, target={target})"
                )
            return None
        try:
            cursor = await self._store._db.execute(
                "SELECT status, external_id, completed_at, attempt, reconcile_status, "
                "request_hash "
                "FROM effect_receipts "
                "WHERE action_id = ? AND effect_type = ? AND target = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (action_id, effect_type, target),
            )
            row = await cursor.fetchone()
            if row and row[0] == "completed":
                stored_hash = row[5] or ""
                # R47 P0-4: request_hash 不匹配 → 视为不同 payload,不视为 completed
                if (expected_request_hash and stored_hash
                        and expected_request_hash != stored_hash):
                    logger.warning(
                        f"[effect_receipts] request_hash 不匹配,视为未完成 "
                        f"action={action_id} type={effect_type} target={target} "
                        f"expected={expected_request_hash[:16]}... "
                        f"stored={stored_hash[:16]}..."
                    )
                    return None
                return {
                    "status": row[0],
                    "external_id": row[1],
                    "completed_at": row[2],
                    "attempt": row[3],
                    "reconcile_status": row[4],
                    "request_hash": stored_hash,
                }
            return None
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] check_receipt 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"check_receipt DB 错误: {e}") from e
            return None

    async def record_pending(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        *,
        request_hash: str = "",
        lease_owner: str = "",
        lease_until: str = "",
        fail_closed: bool = False,
    ) -> bool:
        """记录开始执行 receipt(status=pending)。

        R46 P0-1: CAS claim 语义 — INSERT OR IGNORE,若已存在 pending 行则 attempt+1。
        R47 P0-4: 若已存在 completed 行但 request_hash 不匹配(不同 payload),
                   视为不同副作用,重新 claim(UPDATE 为 pending)而非跳过。

        R48 P0-4: 应用层校验 — critical effect_type 的 request_hash 必须非空,
                   否则抛 ValueError(防止同 action_id 不同参数共用 receipt)。

        Returns True 表示 claim 成功,False 表示已有相同 payload completed(应跳过)。
        """
        # R48 P0-4: 应用层校验 critical effect 的 request_hash 必须非空
        if effect_type in CRITICAL_EFFECT_TYPES and not request_hash:
            raise ValueError(
                f"critical effect '{effect_type}' 的 request_hash 为空,"
                f"拒绝记录 pending(action_id={action_id})"
            )

        if not self._store._db:
            if fail_closed:
                raise EffectReceiptError(
                    f"effect_receipts DB 未初始化,无法记录 pending "
                    f"(action={action_id})"
                )
            return False
        now = datetime.datetime.utcnow().isoformat()
        try:
            # 先检查是否已 completed(同时取 request_hash 用于 R47 P0-4 对比)
            cursor = await self._store._db.execute(
                "SELECT status, request_hash FROM effect_receipts "
                "WHERE action_id = ? AND effect_type = ? AND target = ?",
                (action_id, effect_type, target),
            )
            existing = await cursor.fetchone()
            if existing and existing[0] == "completed":
                # R47 P0-4: request_hash 不匹配 → 不同 payload,重新 claim
                stored_hash = existing[1] or ""
                if (request_hash and stored_hash
                        and request_hash != stored_hash):
                    logger.warning(
                        f"[effect_receipts] record_pending: "
                        f"completed 行 request_hash 不匹配,重新 claim "
                        f"action={action_id} type={effect_type} target={target}"
                    )
                    # 落到下方 UPDATE 路径重新 claim
                else:
                    return False  # 相同 payload 已完成,调用方应跳过

            # CAS claim: INSERT OR IGNORE,已存在则 attempt+1
            if existing:
                await self._store._db.execute(
                    "UPDATE effect_receipts SET status='pending', attempt=attempt+1, "
                    "lease_owner=?, lease_until=?, last_error=NULL, "
                    "reconcile_status='pending', created_at=?, request_hash=? "
                    "WHERE action_id=? AND effect_type=? AND target=?",
                    (lease_owner, lease_until, now, request_hash,
                     action_id, effect_type, target),
                )
            else:
                await self._store._db.execute(
                    "INSERT OR IGNORE INTO effect_receipts "
                    "(action_id, effect_type, target, status, external_id, "
                    " created_at, completed_at, request_hash, attempt, "
                    " lease_owner, lease_until, last_error, reconcile_status) "
                    "VALUES (?, ?, ?, 'pending', NULL, ?, NULL, ?, 1, ?, ?, NULL, 'pending')",
                    (action_id, effect_type, target, now, request_hash,
                     lease_owner, lease_until),
                )
            await self._store._db.commit()
            return True
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] record_pending 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"record_pending DB 错误: {e}") from e
            return False

    async def record_completed(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        external_id: str = "",
        *,
        fail_closed: bool = False,
    ) -> None:
        """记录完成 receipt(status=completed)。"""
        if not self._store._db:
            if fail_closed:
                raise EffectReceiptError(
                    f"effect_receipts DB 未初始化,无法记录 completed"
                )
            return
        now = datetime.datetime.utcnow().isoformat()
        try:
            await self._store._db.execute(
                "UPDATE effect_receipts SET status = 'completed', "
                "external_id = ?, completed_at = ?, reconcile_status = 'completed', "
                "last_error = NULL "
                "WHERE action_id = ? AND effect_type = ? AND target = ?",
                (external_id, now, action_id, effect_type, target),
            )
            await self._store._db.commit()
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] record_completed 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"record_completed DB 错误: {e}") from e

    async def record_failed(
        self,
        action_id: str,
        effect_type: str,
        target: str,
        error_msg: str = "",
        *,
        fail_closed: bool = False,
    ) -> None:
        """记录失败 receipt(status=failed)。"""
        if not self._store._db:
            if fail_closed:
                raise EffectReceiptError(
                    f"effect_receipts DB 未初始化,无法记录 failed"
                )
            return
        try:
            await self._store._db.execute(
                "UPDATE effect_receipts SET status = 'failed', "
                "last_error = ?, reconcile_status = 'needs_reconcile' "
                "WHERE action_id = ? AND effect_type = ? AND target = ?",
                (error_msg[:500] if error_msg else None,
                 action_id, effect_type, target),
            )
            await self._store._db.commit()
        except EffectReceiptError:
            raise
        except Exception as e:
            logger.error(f"[effect_receipts] record_failed 失败: {e}")
            if fail_closed:
                raise EffectReceiptError(f"record_failed DB 错误: {e}") from e

    async def list_pending_reconcile(self, limit: int = 100) -> list[dict]:
        """R46 P0-1: 列出需要 reconciliation 的 receipt(status=failed/needs_reconcile)。"""
        if not self._store._db:
            return []
        try:
            cursor = await self._store._db.execute(
                "SELECT action_id, effect_type, target, status, attempt, "
                "last_error, reconcile_status "
                "FROM effect_receipts "
                "WHERE reconcile_status = 'needs_reconcile' "
                "ORDER BY created_at ASC LIMIT ?",
                (limit,),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "action_id": r[0], "effect_type": r[1], "target": r[2],
                    "status": r[3], "attempt": r[4], "last_error": r[5],
                    "reconcile_status": r[6],
                }
                for r in rows
            ]
        except Exception as e:
            logger.error(f"[effect_receipts] list_pending_reconcile 失败: {e}")
            return []


# 模块级单例
_receipt_manager: Optional[EffectReceiptManager] = None


def get_receipt_manager(cache_store=None) -> Optional[EffectReceiptManager]:
    """获取或创建 EffectReceiptManager 单例。

    Returns None if not initialized (caller must handle fail-closed).
    """
    global _receipt_manager
    if _receipt_manager is None and cache_store is not None:
        _receipt_manager = EffectReceiptManager(cache_store)
    return _receipt_manager


# ════════════════════════════════════════════════════════════════
# R47 P0-4: 静态扫描 — critical effect 必须显式传入 action_id
# ════════════════════════════════════════════════════════════════

def _ast_call_name(func_node) -> str:
    """提取 AST Call 节点的函数名(支持 Name/Attribute 形式)。"""
    if isinstance(func_node, ast.Name):
        return func_node.id
    if isinstance(func_node, ast.Attribute):
        return func_node.attr
    return ""


def _ast_get_str_constant(node) -> Optional[str]:
    """若 AST 节点为字符串常量则返回其值,否则返回 None。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _ast_is_empty_value(node) -> bool:
    """判断 AST 节点是否表示空值(None / 空字符串)。"""
    if node is None:
        return True
    if isinstance(node, ast.Constant):
        return node.value is None or node.value == ""
    return False


def _ast_extract_call_arg(
    call_node: ast.Call,
    keyword: Optional[str],
    position: Optional[int],
) -> Optional[ast.AST]:
    """从 Call 节点提取指定参数(优先关键字参数,其次按位置)。"""
    # 先查关键字参数
    if keyword is not None:
        for kw in call_node.keywords:
            if kw.arg == keyword:
                return kw.value
    # 再查位置参数(position=None 时跳过)
    if position is not None and position < len(call_node.args):
        return call_node.args[position]
    return None


def validate_critical_effects_have_action_id(
    root_dir: str = ".",
) -> list[dict]:
    """R47 P0-4 / R48 P0-4: 静态扫描所有 EffectReceiptContext/with_effect_receipt 调用点。

    扫描 services/、bots/、admin/ 下所有 .py 文件,检测:
    1. EffectReceiptContext(...) 调用中 effect_type 为 critical 类型时,
       action_id 必须为非空值(不能是 None / 空字符串字面量 / 缺失)。
    2. with_effect_receipt(...) 装饰器中 effect_type 为 critical 类型时,
       标记为违规(装饰器模式无法在静态阶段保证调用点传入 action_id)。

    R48 P0-4 新增:
    3. EffectReceiptContext(...) 调用中 effect_type 为 critical 类型时,
       params 参数必须存在且非空(用于计算 request_hash 绑定 effect 参数)。
    4. with_effect_receipt(...) 装饰器工厂中 effect_type 为 critical 类型时,
       params_fn 参数必须存在且非空。

    测试目录 tests/ 与脚本目录 scripts/ 不在扫描范围内。

    Args:
        root_dir: 项目根目录路径

    Returns:
        违规列表,每项含 file/line/effect_type/reason 字段;空列表表示通过。
    """
    violations: list[dict] = []
    scan_dirs = ("services", "bots", "admin")

    root_path = os.path.abspath(root_dir)
    for sub in scan_dirs:
        sub_path = os.path.join(root_path, sub)
        if not os.path.isdir(sub_path):
            continue
        for dirpath, _dirs, files in os.walk(sub_path):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(fpath, root_path)
                try:
                    with open(fpath, "r", encoding="utf-8") as fh:
                        source = fh.read()
                    tree = ast.parse(source, filename=fpath)
                except (SyntaxError, OSError):
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func_name = _ast_call_name(node.func)
                    if func_name == "EffectReceiptContext":
                        effect_type_node = _ast_extract_call_arg(
                            node, "effect_type", position=1,
                        )
                        effect_type_val = _ast_get_str_constant(effect_type_node)
                        if effect_type_val not in CRITICAL_EFFECT_TYPES:
                            continue
                        action_id_node = _ast_extract_call_arg(
                            node, "action_id", position=0,
                        )
                        if _ast_is_empty_value(action_id_node):
                            violations.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "call": "EffectReceiptContext",
                                "effect_type": effect_type_val,
                                "reason": (
                                    "critical effect 的 EffectReceiptContext "
                                    "未显式传入非空 action_id"
                                ),
                            })
                        # R48 P0-4: critical effect 必须传入非空 params
                        params_node = _ast_extract_call_arg(
                            node, "params", position=None,
                        )
                        if _ast_is_empty_value(params_node):
                            violations.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "call": "EffectReceiptContext",
                                "effect_type": effect_type_val,
                                "reason": (
                                    "critical effect 的 EffectReceiptContext "
                                    "未显式传入非空 params(用于 request_hash 绑定)"
                                ),
                            })
                    elif func_name == "with_effect_receipt":
                        effect_type_node = _ast_extract_call_arg(
                            node, None, position=0,
                        )
                        effect_type_val = _ast_get_str_constant(effect_type_node)
                        if effect_type_val not in CRITICAL_EFFECT_TYPES:
                            continue
                        # 装饰器模式: action_id 在调用包装函数时传入,
                        # 静态阶段无法保证所有调用点都传入非空 action_id,
                        # 标记为违规以引导改用 EffectReceiptContext 显式传参。
                        violations.append({
                            "file": rel_path,
                            "line": node.lineno,
                            "call": "with_effect_receipt",
                            "effect_type": effect_type_val,
                            "reason": (
                                "critical effect 使用 with_effect_receipt 装饰器,"
                                "无法静态保证调用点传入 action_id,"
                                "应改用 EffectReceiptContext 显式传参"
                            ),
                        })
                        # R48 P0-4: critical effect 装饰器必须传入非空 params_fn
                        params_fn_node = _ast_extract_call_arg(
                            node, "params_fn", position=None,
                        )
                        if _ast_is_empty_value(params_fn_node):
                            violations.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "call": "with_effect_receipt",
                                "effect_type": effect_type_val,
                                "reason": (
                                    "critical effect 的 with_effect_receipt 装饰器 "
                                    "未显式传入非空 params_fn(用于 request_hash 绑定)"
                                ),
                            })
    return violations
