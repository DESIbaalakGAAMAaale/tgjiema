#!/usr/bin/env python3
"""R64 P1-09: 生成 a11y 强制矩阵测试用例(2 locales × 2 input_modes × 16 states = 64)。

审计 P1-09 要求:
    - a11y success 不能只看单点通过,必须覆盖完整矩阵
    - expected_test_count == executed_test_count
    - 任何 skip / xpass 视为失败
    - 矩阵维度: locales (zh-CN, en-US) × input_modes (keyboard, screen_reader)
      × states (error/loading/empty/paginated/modal/dynamic_button/
                permission_denied/approval_*/mfa_*) = 64 个用例

与 ``generate_a11y_test_cases.py`` 区别:
    - generate_a11y_test_cases.py: R61 路由级用例(每条 GET 路由一个 case,list schema)
    - generate_a11y_matrix_cases.py: R64 矩阵用例(状态 × locale × input_mode,dict schema)

输出 schema:
    {
        "expected_count": 64,
        "locales": ["zh-CN", "en-US"],
        "input_modes": ["keyboard", "screen_reader"],
        "states": [...16 个...],
        "cases": [
            {
                "locale": "zh-CN",
                "input_mode": "keyboard",
                "state": "error",
                "path": "/readiness",       # 代表性路由
                "template": "readiness.html",
                "permission": "require_session",
                "module": "admin",
                "a11y_testable": true
            },
            ...
        ]
    }

用法:
    python scripts/generate_a11y_matrix_cases.py
    python scripts/generate_a11y_matrix_cases.py --output path/to/matrix.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "tests" / "e2e" / "generated_a11y_matrix_cases.json"

# 矩阵维度(与 check_a11y_precheck.py 中 EXPECTED_* 常量保持一致)
LOCALES = ("zh-CN", "en-US")
INPUT_MODES = ("keyboard", "screen_reader")
STATES = (
    "error",
    "loading",
    "empty",
    "paginated",
    "modal",
    "dynamic_button",
    "permission_denied",
    "approval_required",
    "approval_pending",
    "approval_approved",
    "approval_rejected",
    "approval_expired",
    "mfa_required",
    "mfa_pending",
    "mfa_verified",
    "mfa_expired",
)

# state → 代表性路由映射(每个状态选一个能触发该 UI 形态的路由)
# 用于 e2e 测试在该路由上注入对应状态(如 mock API 返回 error / empty / paginated)
_STATE_ROUTE_MAP: dict[str, dict] = {
    "error": {
        "path": "/readiness",
        "template": "readiness.html",
        "permission": "require_session",
        "module": "admin",
    },
    "loading": {
        "path": "/tasks",
        "template": "tasks.html",
        "permission": "require_session",
        "module": "admin",
    },
    "empty": {
        "path": "/notifications",
        "template": "notifications.html",
        "permission": "require_session",
        "module": "admin",
    },
    "paginated": {
        "path": "/users",
        "template": "users.html",
        "permission": "require_session",
        "module": "admin",
    },
    "modal": {
        "path": "/reports",
        "template": "reports.html",
        "permission": "require_session",
        "module": "admin",
    },
    "dynamic_button": {
        "path": "/files",
        "template": "files.html",
        "permission": "require_session",
        "module": "admin",
    },
    "permission_denied": {
        "path": "/rbac",
        "template": "rbac.html",
        "permission": "require_session",
        "module": "admin",
    },
    "approval_required": {
        "path": "/approvals",
        "template": "approvals.html",
        "permission": "require_session",
        "module": "admin",
    },
    "approval_pending": {
        "path": "/approvals",
        "template": "approvals.html",
        "permission": "require_session",
        "module": "admin",
    },
    "approval_approved": {
        "path": "/approvals",
        "template": "approvals.html",
        "permission": "require_session",
        "module": "admin",
    },
    "approval_rejected": {
        "path": "/approvals",
        "template": "approvals.html",
        "permission": "require_session",
        "module": "admin",
    },
    "approval_expired": {
        "path": "/approvals",
        "template": "approvals.html",
        "permission": "require_session",
        "module": "admin",
    },
    "mfa_required": {
        "path": "/mfa/setup",
        "template": None,
        "permission": "require_session",
        "module": "admin",
    },
    "mfa_pending": {
        "path": "/mfa/setup",
        "template": None,
        "permission": "require_session",
        "module": "admin",
    },
    "mfa_verified": {
        "path": "/mfa/setup",
        "template": None,
        "permission": "require_session",
        "module": "admin",
    },
    "mfa_expired": {
        "path": "/mfa/setup",
        "template": None,
        "permission": "require_session",
        "module": "admin",
    },
}


def build_matrix_cases() -> list[dict]:
    """构建 2 × 2 × 16 = 64 个矩阵用例。

    用例顺序: state(外) → locale(中) → input_mode(内),便于 diff 与排查。
    """
    cases: list[dict] = []
    for state in STATES:
        route = _STATE_ROUTE_MAP.get(state, {})
        for locale in LOCALES:
            for input_mode in INPUT_MODES:
                cases.append({
                    "locale": locale,
                    "input_mode": input_mode,
                    "state": state,
                    "path": route.get("path", "/"),
                    "template": route.get("template"),
                    "permission": route.get("permission", "require_session"),
                    "module": route.get("module", "admin"),
                    "a11y_testable": True,
                })
    return cases


def build_payload() -> dict:
    """构建完整 payload(含 expected_count + cases)。"""
    cases = build_matrix_cases()
    return {
        "expected_count": len(cases),
        "locales": list(LOCALES),
        "input_modes": list(INPUT_MODES),
        "states": list(STATES),
        "cases": cases,
    }


def main() -> int:
    """CLI 主入口。返回 0。"""
    output_path: Path | None = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_path = Path(sys.argv[idx + 1])

    payload = build_payload()
    text = json.dumps(payload, ensure_ascii=False, indent=2)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
        print(
            f"✓ R64 P1-09: 已生成 {len(payload['cases'])} 条 a11y 矩阵用例 → {output_path}",
            file=sys.stderr,
        )
        print(
            f"  locales={payload['locales']} input_modes={payload['input_modes']}"
            f" states={len(payload['states'])}",
            file=sys.stderr,
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
