#!/usr/bin/env python3
"""R47 P0-4 / R48 P0-4: Effect Receipt 覆盖率静态门禁 — critical effect 必须显式传入 action_id + params。

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

critical effect_type 集合(CRITICAL_EFFECT_TYPES):
    telegram_send / telegram_copy / r2_put / r2_download /
    restore / ban / takedown / purge / crdb_delete

CI 调用方式(在 .github/workflows/release-gates.yml 中添加):
    - name: Effect Receipt 覆盖率门禁
      run: python scripts/check_effect_receipt_coverage.py

退出码:
    0 — 通过(无违规)
    1 — 失败(存在违规,需修复)
"""
from __future__ import annotations

import os
import sys

# 将项目根目录加入 sys.path,以便导入 services.effect_receipts
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from services.effect_receipts import (  # noqa: E402
    CRITICAL_EFFECT_TYPES,
    validate_critical_effects_have_action_id,
)


def main() -> int:
    """CI 入口:扫描项目根目录下的 services/、bots/、admin/,输出违规并返回退出码。"""
    violations = validate_critical_effects_have_action_id(_PROJECT_ROOT)

    if not violations:
        print(
            f"[check_effect_receipt_coverage] PASS — "
            f"critical effect 调用点均显式传入 action_id + params/params_fn "
            f"(critical types: {sorted(CRITICAL_EFFECT_TYPES)})"
        )
        return 0

    print(
        f"[check_effect_receipt_coverage] FAIL — "
        f"发现 {len(violations)} 处违规:"
    )
    for v in violations:
        print(
            f"  - {v['file']}:{v['line']} "
            f"[{v['call']}] effect_type={v['effect_type']} "
            f"— {v['reason']}"
        )
    print()
    print(
        "修复建议:critical effect 必须通过 EffectReceiptContext 显式传入非空 "
        "action_id 和 params(用于 request_hash 绑定),不应使用 with_effect_receipt "
        "装饰器(装饰器无法静态保证调用点传入 action_id)。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
