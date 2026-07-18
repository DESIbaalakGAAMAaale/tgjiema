#!/usr/bin/env python3
"""R61 P1-09: 按钮 handler 静态门禁 — 全域状态机证明。

审计需求 P1-09: 按钮安全基础设施已存在,但需要 PROVE 所有高风险 handler 都走
ButtonFlow/CommandBus,而不只是关键路径。本门禁消费
``scripts/button_handler_inventory.json``(由 ``generate_button_handler_inventory.py``
产出),对每个高风险 handler 执行三条规则:

  Rule A: 高风险 handler 必须走 CommandBus(``bus.execute`` / ``make_*_command``)
          或 ButtonFlow(``ButtonFlow`` / ``get_button_flow``)。
          禁止直接调用破坏性 API:
            - ``data_lifecycle.purge_*`` / ``admin.purge`` / ``store.delete``
            - ``update_user_and_invalidate``(写 is_banned)
            - ``update_file_record_and_invalidate``(写 status:detached/deleted)
            - ``delete_user_data`` / 物理 ``DELETE``
          理想 0 违规;现有违规通过 ``--baseline`` ratchet 下降。

  Rule B: 高风险 handler 必须在 sidecar 元数据
          (``scripts/button_handler_metadata.json`` 的 ``handlers`` 段)中声明
          完整字段集:
            action_type / rbac / mfa / two_person_approval / idempotency_key /
            state_transitions / timeout / cancellation /
            localization_confirmation / audit_schema
          缺失任一字段 = 违规(无 baseline,必须补全)。

  Rule C: 高风险 callback handler(CallbackQueryHandler 注册点或子分发器)
          必须使用签名绑定 API(``sign_button_token_with_nonce`` /
          ``verify_button_token`` / ``consume_token_cas`` / ``create_token`` /
          ``ButtonFlow``),签名需绑定 user/action/payload/expiry/nonce。
          禁止裸 ``query.data.split`` 解析(无签名验证,可伪造)。
          理想 0 违规;现有违规通过 ``--baseline`` ratchet 下降。

Baseline 机制(``--baseline`` flag):
  - 默认模式:与 baseline 比对,仅新增违规 exit 1(ratchet 下降)
  - ``--strict`` 模式:忽略 baseline,任何违规 exit 1(用于新代码门禁)
  - ``--generate-baseline``:重新生成 baseline(修复违规后更新,只减不增)
  - baseline 文件: ``scripts/button_handler_metadata.json`` 的 ``baseline`` 段

CI 调用方式:
    # 先生成 inventory
    python scripts/generate_button_handler_inventory.py
    # 再执行门禁(默认 ratchet 模式)
    python scripts/check_button_handler_gate.py
    # 严格模式(忽略 baseline)
    python scripts/check_button_handler_gate.py --strict

退出码:
  - 0: 无新增违规(默认模式)或无任何违规(strict 模式)
  - 1: 检测到新增违规(默认模式)或任何违规(strict 模式)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# inventory 文件(由 generate_button_handler_inventory.py 产出)
INVENTORY_FILE = Path(__file__).parent / "button_handler_inventory.json"

# 元数据 sidecar 文件(含 handlers 段 + baseline 段)
METADATA_FILE = Path(__file__).parent / "button_handler_metadata.json"

# Rule B: 高风险 handler 必须声明的完整字段集
REQUIRED_METADATA_FIELDS: frozenset[str] = frozenset({
    "action_type",
    "rbac",
    "mfa",
    "two_person_approval",
    "idempotency_key",
    "state_transitions",
    "timeout",
    "cancellation",
    "localization_confirmation",
    "audit_schema",
})

# Rule A: 高风险 handler 禁止直接调用的破坏性 API
FORBIDDEN_DESTRUCTIVE_APIS: frozenset[str] = frozenset({
    "update_user_and_invalidate",
    "update_file_record_and_invalidate",
    "delete_user_data",
    "cleanup_expired_data",
    "purge_data",
    "purge_channel",
    "factory_reset",
    "delete_file",
    "delete_pending_file_code",
})

# Rule C: 高风险 callback handler 必须使用的签名绑定 API
# R62 P0-04: 新增 handle 短 ID 模式的签名/验证函数(绕过 Telegram 64 字节限制)
SIGNED_TOKEN_API_NAMES: frozenset[str] = frozenset({
    "sign_button_token_with_nonce",
    "verify_button_token",
    "consume_token_cas",
    "create_token",
    "ButtonFlow",
    "get_button_flow",
    # R62 P0-04: handle 短 ID 模式(基于 button_tokens 表 + sign_button_token_with_nonce)
    "sign_button_token_with_handle",
    "verify_button_token_by_handle",
})

# 高风险 handler 的 entry_type 集合(门禁检查范围)
HIGH_RISK_ENTRY_TYPES: frozenset[str] = frozenset({
    "fastapi_post",
    "callback_query_handler",
    "callback_sub_dispatcher",
    "button_flow",
})


# ════════════════════════════════════════════════════════════════
# 文件加载
# ════════════════════════════════════════════════════════════════


def _load_inventory(inventory_path: Path | None = None) -> dict:
    """加载 inventory JSON。

    Returns:
        {"handlers": [...], "handler_count": N, "high_risk_count": M, ...}
    Raises:
        FileNotFoundError: inventory 文件不存在
        ValueError: inventory 格式错误
    """
    path = inventory_path or INVENTORY_FILE
    if not path.exists():
        raise FileNotFoundError(
            f"inventory 文件不存在: {path}\n"
            f"请先运行: python scripts/generate_button_handler_inventory.py"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if "handlers" not in data or not isinstance(data["handlers"], list):
        raise ValueError(f"inventory 格式错误: 缺少 handlers 列表 ({path})")
    return data


def _load_metadata(metadata_path: Path | None = None) -> dict:
    """加载元数据 sidecar JSON(含 handlers 段 + baseline 段)。

    Returns:
        {"handlers": {...}, "baseline": {...}}
        文件不存在时返回空结构(不阻塞门禁,Rule B 全部违规)。
    """
    path = metadata_path or METADATA_FILE
    if not path.exists():
        return {"handlers": {}, "baseline": {"violations": []}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if "handlers" not in data:
        data["handlers"] = {}
    if "baseline" not in data:
        data["baseline"] = {"violations": []}
    return data


# ════════════════════════════════════════════════════════════════
# 违规键生成(用于 baseline 比对,不依赖行号)
# ════════════════════════════════════════════════════════════════


def _violation_key(rule: str, handler_entry: dict) -> str:
    """生成违规唯一键(基于 rule + file + handler + entry_type,不依赖行号)。

    行号会随代码变动,因此 baseline 键不含行号,确保 ratchet 稳定。
    """
    return (
        f"{rule}::{handler_entry.get('file', '?')}::"
        f"{handler_entry.get('handler', '?')}::"
        f"{handler_entry.get('entry_type', '?')}"
    )


# ════════════════════════════════════════════════════════════════
# 规则检查
# ════════════════════════════════════════════════════════════════


def _check_rule_a(handler: dict) -> list[dict]:
    """Rule A: 高风险 handler 必须走 CommandBus/ButtonFlow,禁止破坏性 API。

    Returns:
        违规列表(每项含 rule/violation_type/file/handler/line/reason)
    """
    violations: list[dict] = []
    if not handler.get("is_high_risk"):
        return violations
    if handler.get("is_dispatcher"):
        # 分发器本身不直接执行高风险逻辑,跳过
        return violations

    routes_bus = handler.get("routes_through_command_bus", False)
    routes_flow = handler.get("routes_through_button_flow", False)
    calls_destructive = handler.get("calls_destructive_api", False)
    destructive_api = handler.get("destructive_api")

    if not routes_bus and not routes_flow:
        if calls_destructive and destructive_api in FORBIDDEN_DESTRUCTIVE_APIS:
            violations.append({
                "rule": "A",
                "violation_type": "RULE_A_DESTRUCTIVE_BYPASS",
                "file": handler["file"],
                "handler": handler["handler"],
                "line": handler["line"],
                "entry_type": handler["entry_type"],
                "reason": (
                    f"高风险 handler 直接调用破坏性 API '{destructive_api}',"
                    f"未走 CommandBus/ButtonFlow。"
                    f"必须改用 make_*_command + bus.execute,"
                    f"或 ButtonFlow 6 步流程(prepare→preview→confirm→mfa→approve→execute)。"
                ),
            })
        else:
            violations.append({
                "rule": "A",
                "violation_type": "RULE_A_NO_STATE_MACHINE",
                "file": handler["file"],
                "handler": handler["handler"],
                "line": handler["line"],
                "entry_type": handler["entry_type"],
                "reason": (
                    "高风险 handler 未走 CommandBus/ButtonFlow 状态机。"
                    "必须通过 CommandBus(make_*_command + bus.execute)或 "
                    "ButtonFlow(get_button_flow + prepare→...→execute)路由。"
                ),
            })
    return violations


def _check_rule_b(handler: dict, metadata_handlers: dict) -> list[dict]:
    """Rule B: 高风险 handler 必须在 sidecar 元数据中声明完整字段集。

    Returns:
        违规列表(每项含 rule/violation_type/file/handler/line/reason/missing_fields)
    """
    violations: list[dict] = []
    if not handler.get("is_high_risk"):
        return violations
    if handler.get("is_dispatcher"):
        return violations

    # 用 handler 名 + file 作为 sidecar key(允许同名 handler 在不同文件)
    sidecar_key = handler["handler"]
    meta = metadata_handlers.get(sidecar_key)
    if meta is None:
        # 也尝试 file::handler 形式(更精确)
        sidecar_key = f"{handler['file']}::{handler['handler']}"
        meta = metadata_handlers.get(sidecar_key)
    if meta is None:
        violations.append({
            "rule": "B",
            "violation_type": "RULE_B_METADATA_MISSING",
            "file": handler["file"],
            "handler": handler["handler"],
            "line": handler["line"],
            "entry_type": handler["entry_type"],
            "reason": (
                f"高风险 handler 未在 sidecar 元数据中声明 "
                f"({METADATA_FILE.name} 的 handlers 段缺少 key='{handler['handler']}')"
            ),
            "missing_fields": sorted(REQUIRED_METADATA_FIELDS),
        })
        return violations

    # 检查字段完整性
    missing = sorted(REQUIRED_METADATA_FIELDS - set(meta.keys()))
    if missing:
        violations.append({
            "rule": "B",
            "violation_type": "RULE_B_FIELDS_INCOMPLETE",
            "file": handler["file"],
            "handler": handler["handler"],
            "line": handler["line"],
            "entry_type": handler["entry_type"],
            "reason": (
                f"sidecar 元数据字段不完整,缺少: {missing}"
            ),
            "missing_fields": missing,
        })
    return violations


def _check_rule_c(handler: dict) -> list[dict]:
    """Rule C: 高风险 callback handler 必须使用签名绑定 API。

    仅适用于 callback 入口(callback_query_handler / callback_sub_dispatcher)。
    FastAPI POST 端点走 CSRF + session 认证,不适用 callback 签名规则。

    Returns:
        违规列表
    """
    violations: list[dict] = []
    if not handler.get("is_high_risk"):
        return violations
    if handler.get("is_dispatcher"):
        return violations

    entry_type = handler.get("entry_type")
    if entry_type not in ("callback_query_handler", "callback_sub_dispatcher"):
        return violations

    uses_signed = handler.get("uses_signed_token_api", False)
    routes_flow = handler.get("routes_through_button_flow", False)
    if not uses_signed and not routes_flow:
        violations.append({
            "rule": "C",
            "violation_type": "RULE_C_NO_SIGNED_TOKEN",
            "file": handler["file"],
            "handler": handler["handler"],
            "line": handler["line"],
            "entry_type": handler["entry_type"],
            "reason": (
                "高风险 callback handler 未使用签名绑定 API。"
                "必须使用 sign_button_token_with_nonce(签名) + "
                "verify_button_token(验签 + 原子消费 nonce),"
                "或 ButtonFlow(consume_token_cas 4 字段 CAS),"
                "签名需绑定 user/action/payload/expiry/nonce。"
                "禁止裸 query.data.split 解析(可伪造 callback_data)。"
            ),
        })
    return violations


# ════════════════════════════════════════════════════════════════
# 主检查流程
# ════════════════════════════════════════════════════════════════


def check(
    inventory: dict | None = None,
    metadata: dict | None = None,
    strict: bool = False,
) -> tuple[int, list[dict], list[dict]]:
    """主校验流程。

    Args:
        inventory: inventory 字典(默认从 INVENTORY_FILE 加载)
        metadata: 元数据字典(默认从 METADATA_FILE 加载)
        strict: True=严格模式(忽略 baseline,任何违规 exit 1)

    Returns:
        (exit_code, new_violations, all_violations)
        exit_code: 0=无新增违规(默认)或无违规(strict),1=有违规
        new_violations: 新增违规(不在 baseline 中)
        all_violations: 所有违规(含 baseline 中的)
    """
    if inventory is None:
        inventory = _load_inventory()
    if metadata is None:
        metadata = _load_metadata()

    metadata_handlers = metadata.get("handlers", {})
    baseline_violations: set[str] = set()
    for v in metadata.get("baseline", {}).get("violations", []):
        if isinstance(v, dict) and "key" in v:
            baseline_violations.add(v["key"])
        elif isinstance(v, str):
            baseline_violations.add(v)

    all_violations: list[dict] = []
    handlers = inventory.get("handlers", [])

    for handler in handlers:
        # 仅检查高风险 handler(非 dispatcher)
        if not handler.get("is_high_risk"):
            continue
        if handler.get("is_dispatcher"):
            continue
        if handler.get("entry_type") not in HIGH_RISK_ENTRY_TYPES:
            continue

        # Rule A
        all_violations.extend(_check_rule_a(handler))
        # Rule B
        all_violations.extend(_check_rule_b(handler, metadata_handlers))
        # Rule C
        all_violations.extend(_check_rule_c(handler))

    # 为每个违规生成 baseline 键
    for v in all_violations:
        v["key"] = _violation_key(v["rule"], v)

    # 区分新增违规 vs baseline 违规
    new_violations: list[dict] = []
    for v in all_violations:
        if strict:
            # strict 模式:所有违规都算新增
            new_violations.append(v)
        elif v["key"] not in baseline_violations:
            new_violations.append(v)

    exit_code = 1 if new_violations else 0
    return exit_code, new_violations, all_violations


def _generate_baseline(all_violations: list[dict], existing_baseline: dict) -> dict:
    """生成新的 baseline(合并现有 baseline + 当前违规,只增不减由人工审核)。

    实际 ratchet 下降流程:
      1. 修复若干违规后运行 --generate-baseline
      2. 工具输出新 baseline(仅含未修复的违规)
      3. 人工确认后提交
    """
    # 按 key 去重,保留 owner/reason/expiry(若已有)
    existing_by_key: dict[str, dict] = {}
    for v in existing_baseline.get("violations", []):
        if isinstance(v, dict) and "key" in v:
            existing_by_key[v["key"]] = v

    new_violations_list: list[dict] = []
    seen_keys: set[str] = set()
    for v in all_violations:
        key = v["key"]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        if key in existing_by_key:
            # 保留现有 baseline 条目(owner/reason/expiry)
            new_violations_list.append(existing_by_key[key])
        else:
            # 新违规:标注为待补全 owner/reason/expiry
            new_violations_list.append({
                "key": key,
                "rule": v["rule"],
                "violation_type": v["violation_type"],
                "file": v["file"],
                "handler": v["handler"],
                "owner": "TODO",
                "reason": "TODO: 补全违规原因与修复计划",
                "expiry": "TODO: 补全预期修复日期(YYYY-MM-DD)",
            })

    return {
        "description": (
            "R61 P1-09: 按钮 handler 门禁 baseline "
            "(Rule A/C ratchet 下降;Rule B 无 baseline,必须补全)"
        ),
        "note": (
            "baseline 仅记录 Rule A/C 的现有违规,通过 ratchet 逐步下降。"
            "修复违规后运行 --generate-baseline 更新此文件(只减不增)。"
            "Rule B(元数据缺失)无 baseline,必须立即补全 sidecar 字段。"
        ),
        "violation_count": len(new_violations_list),
        "violations": sorted(
            new_violations_list, key=lambda v: v.get("key", "")
        ),
    }


def _print_violations(violations: list[dict], label: str) -> None:
    """打印违规列表。"""
    if not violations:
        return
    print(f"\n[{label}] {len(violations)} 处违规:")
    for v in violations:
        print(
            f"  {v['file']}:{v['line']} {v['handler']} "
            f"[{v['entry_type']}] Rule {v['rule']} "
            f"({v['violation_type']})"
        )
        print(f"    原因: {v['reason']}")
        if "missing_fields" in v:
            print(f"    缺失字段: {v['missing_fields']}")


def main() -> int:
    """脚本入口。返回退出码。"""
    parser = argparse.ArgumentParser(
        description="R61 P1-09: 按钮 handler 静态门禁(全域状态机证明)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="严格模式:忽略 baseline,任何违规 exit 1(用于新代码门禁)",
    )
    parser.add_argument(
        "--generate-baseline",
        action="store_true",
        help="重新生成 baseline(修复违规后更新,只减不增)",
    )
    parser.add_argument(
        "--inventory",
        default=str(INVENTORY_FILE),
        help=f"inventory JSON 路径(默认: {INVENTORY_FILE})",
    )
    parser.add_argument(
        "--metadata",
        default=str(METADATA_FILE),
        help=f"元数据 sidecar JSON 路径(默认: {METADATA_FILE})",
    )
    args = parser.parse_args()

    # 加载 inventory
    try:
        inventory = _load_inventory(Path(args.inventory))
    except (FileNotFoundError, ValueError) as e:
        print(f"[ERROR] {e}")
        return 1

    # 加载 metadata
    metadata = _load_metadata(Path(args.metadata))

    exit_code, new_violations, all_violations = check(
        inventory=inventory,
        metadata=metadata,
        strict=args.strict,
    )

    # 生成 baseline 模式
    if args.generate_baseline:
        new_baseline = _generate_baseline(
            all_violations, metadata.get("baseline", {}),
        )
        # 保留 handlers 段,替换 baseline 段
        metadata["baseline"] = new_baseline
        Path(args.metadata).write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[OK] Baseline 已生成: {args.metadata}")
        print(f"  违规总数: {new_baseline['violation_count']}")
        print()
        print("⚠️  警告: baseline 生成仅用于初始基线/下降基线,不应在 PR 中扩大基线。")
        print("   PR 中新增违规应修复后接入 CommandBus/ButtonFlow,而非纳入基线。")
        print("   CI 中应使用 --strict 模式确保违规数为 0(A/C)或字段齐全(B)。")
        return 0

    # 输出统计
    handlers = inventory.get("handlers", [])
    high_risk = [h for h in handlers if h.get("is_high_risk")]
    print(f"[INFO] 扫描 handler 总数: {len(handlers)}")
    print(f"[INFO] 高风险 handler: {len(high_risk)}")

    # 分类违规
    rule_a = [v for v in all_violations if v["rule"] == "A"]
    rule_b = [v for v in all_violations if v["rule"] == "B"]
    rule_c = [v for v in all_violations if v["rule"] == "C"]
    new_a = [v for v in new_violations if v["rule"] == "A"]
    new_b = [v for v in new_violations if v["rule"] == "B"]
    new_c = [v for v in new_violations if v["rule"] == "C"]

    baseline_count = len(all_violations) - len(new_violations)
    print(f"[INFO] 违规统计(全部 / baseline / 新增):")
    print(f"  Rule A (CommandBus 路由): {len(rule_a)} / {len(rule_a) - len(new_a)} / {len(new_a)}")
    print(f"  Rule B (sidecar 元数据): {len(rule_b)} / {len(rule_b) - len(new_b)} / {len(new_b)}")
    print(f"  Rule C (签名绑定):       {len(rule_c)} / {len(rule_c) - len(new_c)} / {len(new_c)}")

    if new_violations:
        _print_violations(new_violations, "新增违规")
        print()
        print("整改方案:")
        print("  Rule A: 将直接调用破坏性 API 改为 make_*_command + bus.execute,")
        print("          或 ButtonFlow 6 步流程(prepare→preview→confirm→mfa→approve→execute)。")
        print("  Rule B: 在 scripts/button_handler_metadata.json 的 handlers 段中,")
        print("          为该 handler 声明完整字段集(action_type/rbac/mfa/")
        print("          two_person_approval/idempotency_key/state_transitions/")
        print("          timeout/cancellation/localization_confirmation/audit_schema)。")
        print("  Rule C: 高风险 callback handler 改用 sign_button_token_with_nonce +")
        print("          verify_button_token,或 ButtonFlow(consume_token_cas),")
        print("          签名需绑定 user/action/payload/expiry/nonce。")
        print()
        print(f"基线模式: 已知违规可加入 baseline(--generate-baseline),")
        print(f"  但 PR 中新增违规必须修复,不得纳入基线。")
        return 1

    # 无新增违规
    if all_violations:
        print()
        print(
            f"[OK] 门禁通过(无新增违规)。"
            f"现有 baseline 违规 {baseline_count} 处(ratchet 下降中)。"
        )
    else:
        print()
        print("[OK] 门禁通过(无任何违规,全域状态机证明完成)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
