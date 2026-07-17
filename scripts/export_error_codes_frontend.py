#!/usr/bin/env python3
"""R56 §5.2: 导出错误码前端映射 JSON — 供 Admin Web / Bot 加载。

生成 ``locales/error_codes_frontend.json``,包含所有错误码的:
    - code: 三段式错误码
    - message_key: i18n key(供前端渲染本地化消息)
    - http_status: HTTP 状态码
    - retryable: 是否可重试(决定是否显示"重试"按钮)
    - severity: 严重级别(info/warning/error/critical)
    - safe_params: 可安全记录的参数名白名单
    - telegram_presentation: Bot 端展示方式(short_hint/inline/silent/modal/toast)
    - show_retry_button: 是否显示重试按钮(R61 P1-05: 独立显式设置,
      通常等于 retryable 但可独立覆盖)

用法:
    python scripts/export_error_codes_frontend.py
    python scripts/export_error_codes_frontend.py --output path/to/file.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "locales" / "error_codes_frontend.json"


def main() -> int:
    output_path = DEFAULT_OUTPUT
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_path = Path(sys.argv[idx + 1])

    sys.path.insert(0, str(REPO_ROOT))
    from services.error_codes import ErrorEnum, ErrorRegistry  # type: ignore

    # 触发初始化
    ErrorRegistry.all_codes()

    mapping = ErrorRegistry.to_frontend_mapping()
    output = {
        "_meta": {
            "version": "R56-§5.2",
            "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "total_codes": len(mapping),
            "description": (
                "前端错误码映射 — 由 scripts/export_error_codes_frontend.py 生成。"
                "请勿手动修改,通过 ErrorRegistry.to_frontend_mapping() 自动生成。"
            ),
            "schema": {
                "code": "三段式错误码 DOMAIN.OPERATION.REASON",
                "message_key": "i18n key(在 locales/zh-CN.json 和 en-US.json 中)",
                "http_status": "HTTP 状态码",
                "retryable": "是否可重试(True=临时性故障,可显示重试按钮)",
                "severity": "严重级别(info/warning/error/critical)",
                "safe_params": "可安全记录的参数名白名单",
                "telegram_presentation": (
                    "Bot 端展示方式(R61 P1-05 显式字段): "
                    "short_hint(短提示+查看详情)/inline(直接展开,用于 critical)/"
                    "silent(不展示,用于 info)/modal/toast(预留扩展)"
                ),
                "show_retry_button": (
                    "是否显示重试按钮(R61 P1-05: 独立显式设置,"
                    "通常等于 retryable 但可独立覆盖)"
                ),
            },
            "enum_count": len(list(ErrorEnum)),
        },
        "codes": mapping,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(f"✓ 已导出 {len(mapping)} 个错误码映射到: {output_path}")
    print(f"  版本: {output['_meta']['version']}")
    print(f"  生成时间: {output['_meta']['generated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
