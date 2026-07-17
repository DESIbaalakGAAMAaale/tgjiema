#!/usr/bin/env python3
"""R60 §13 无障碍专项: 导出 Admin FastAPI 路由清单(machine-readable inventory)。

供 ``tests/e2e/accessibility_behavior.spec.ts`` 的 parity 测试消费:
  - 导入 ``admin.app``,枚举所有已注册路由(``@app.get`` / ``@app.post`` 等)
  - 输出 JSON 到 stdout: ``[{"path": "/users", "methods": ["GET", "HEAD"]}, ...]``
  - CI parity 测试对比 ``TEMPLATE_TO_ROUTE`` 与本 inventory,
    新增 Admin GET/POST 路由必须自动进入 inventory,否则测试失败。

用法:
    python scripts/export_admin_routes.py
    python scripts/export_admin_routes.py --output path/to/routes.json

依赖: ``fastapi``(admin/__init__.py 顶层 import)。E2E 环境已执行
``pip install -r requirements.txt``,故本脚本可在 CI 中正常运行。

设计说明:
  - 仅构建路由表(导入 admin 触发 ``@app.get``/``@app.post`` 装饰器注册),
    不启动 uvicorn 服务,不打开数据库连接(路由 handler 内才惰性取 DB)。
  - 状态消息输出到 stderr,stdout 仅输出 JSON,便于测试侧 ``JSON.parse``。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _iter_admin_routes():
    """导入 admin.app 并枚举路由条目(path + methods + module)。

    过滤掉 FastAPI/Starlette 框架内置路由(/docs、/redoc、/openapi.json、
    /docs/oauth2-redirect 等):这些路由的 endpoint 定义在 fastapi/starlette 模块内,
    非 admin 业务路由,不应纳入 parity 范围。同时跳过无 endpoint 的非函数路由(Mount)。
    """
    sys.path.insert(0, str(REPO_ROOT))
    # 导入 admin 触发路由注册(仅构建路由表,不启动服务)
    import admin  # type: ignore

    app = admin.app
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path:
            # 跳过无 path 的非 HTTP 路由项
            continue
        endpoint = getattr(route, "endpoint", None)
        module = getattr(endpoint, "__module__", "") or "" if endpoint else ""
        # 过滤框架内置路由(fastapi/starlette 模块定义的 endpoint,如 OpenAPI 文档端点)
        if module.startswith(("fastapi", "starlette")):
            continue
        methods = getattr(route, "methods", None)
        # methods 为 set(如 {'GET', 'HEAD'})或 None(Mount 等无固定方法)
        method_list = sorted(methods) if methods else []
        yield {"path": path, "methods": method_list, "module": module}


def main() -> int:
    output_path: Path | None = None
    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_path = Path(sys.argv[idx + 1])

    inventory = list(_iter_admin_routes())
    # 稳定排序便于 diff 与 CI 日志对比
    inventory.sort(key=lambda r: (r["path"], ",".join(r["methods"])))

    payload = json.dumps(inventory, ensure_ascii=False, indent=2)
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
        print(f"✓ 已导出 {len(inventory)} 条路由到: {output_path}", file=sys.stderr)
    else:
        # stdout 仅输出 JSON,状态消息走 stderr(供测试侧 JSON.parse)
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
