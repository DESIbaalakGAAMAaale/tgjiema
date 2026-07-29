#!/usr/bin/env python3
"""R79 §10.3 / P0-03 / P1-01 — Secretless resolved service graph 硬门禁。

整改背景:
    R78 用 docker-compose.yml + docker-compose.secretless.yml 叠加构造测试
    环境,Compose override 会合并服务和 depends_on,不会自动删除旧服务 —
    导致 secretless 流程同时等待 production CRDB 和 secretless CRDB(双 CRDB
    拓扑),并以削弱 production 安全配置为代价修测试。R79 P1-01 要求在
    resolved config 上机器断言服务集合和依赖图,不得靠肉眼判断 overlay。

本脚本消费 ``docker compose config --format json``(或 YAML)的 resolved
配置,断言(secretless 模式):
    1. 服务图只有一个 CockroachDB 服务
       (违反 → SECRETLESS_MULTIPLE_CRDB_SERVICES,exit 1)
    2. migration/db_writer/crdb_sync/db_backup/up/idx/dsp 全部依赖同一个 CRDB
    3. 所有 CRDB DSN(SECRETLESS_CRDB_URL / COCKROACHDB_URL)的 host 与
       SQL port 一致,且指向唯一 CRDB 服务
    4. Secretless 图不包含 production secrets 形态(真实 telegram/R2 端点)、
       production Environment 或公网端口绑定
    5. 唯一 CRDB 必须满足最小写集合加固(read_only + tmpfs 三挂载)
       — 证明 override 继承未被破坏

production 模式(输入 = 仅 docker-compose.yml 的 resolved config):
    - 不得包含 provider-sim / MinIO 临时凭据 / SECRETLESS_MODE=true
      (违反 → PRODUCTION_GRAPH_FORBIDDEN_ITEM,exit 1)

用法:
    docker compose -f docker-compose.yml -f docker-compose.secretless.yml \
        config --format json > resolved.json
    python scripts/validate_secretless_service_graph.py resolved.json \
        --export-graph artifacts/secretless-e2e/service-graph.json

退出码:
    0 — 全部断言通过
    1 — 断言失败(打印稳定错误码)
    2 — 输入不可解析
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# ════════════════════════════════════════════════════════════════
# 稳定错误码(R79 §10.3)
# ════════════════════════════════════════════════════════════════

ERR_MULTIPLE_CRDB = "SECRETLESS_MULTIPLE_CRDB_SERVICES"
ERR_NO_CRDB = "SECRETLESS_NO_CRDB_SERVICE"
ERR_DEP_MISMATCH = "SECRETLESS_CRDB_DEPENDENCY_MISMATCH"
ERR_DSN_MISMATCH = "SECRETLESS_CRDB_DSN_MISMATCH"
ERR_FORBIDDEN = "SECRETLESS_GRAPH_FORBIDDEN_ITEM"
ERR_HARDENING = "SECRETLESS_CRDB_HARDENING_MISSING"
ERR_PROD_FORBIDDEN = "PRODUCTION_GRAPH_FORBIDDEN_ITEM"

#: CockroachDB 镜像识别子串
CRDB_IMAGE_MARKER = "cockroachdb/cockroach"

#: 必须依赖唯一 CRDB 的应用角色
CRDB_DEPENDENT_ROLES: tuple[str, ...] = (
    "migration",
    "db_writer",
    "crdb_sync",
    "db_backup",
    "up",
    "idx",
    "dsp",
)

#: CRDB DSN 环境变量(值必须一致且指向唯一 CRDB)
CRDB_DSN_VARS: tuple[str, ...] = ("SECRETLESS_CRDB_URL", "COCKROACHDB_URL")

#: 允许的 SQL 端口(R78/R79 端口契约:listen=localhost:26257,SQL=0.0.0.0:26258)
EXPECTED_SQL_PORT = "26258"

#: 最小写集合 tmpfs 挂载(R79 §10.1)
REQUIRED_TMPFS_TARGETS: tuple[str, ...] = ("/tmp", "/cockroach/run", "/cockroach/certs")

#: production 形态端点(secretless 图中禁止出现)
PRODUCTION_ENDPOINT_PATTERNS: tuple[str, ...] = (
    "api.telegram.org",
    ".r2.cloudflarestorage.com",
    "backblazeb2.com",
)

#: production 图中禁止出现的 secretless 标记
PRODUCTION_FORBIDDEN_ENV: tuple[str, ...] = (
    "SECRETLESS_MODE=true",
    "PROVIDER_BACKEND=contract",
    "OBJECT_STORAGE_BACKEND=minio",
)
PRODUCTION_FORBIDDEN_SERVICES: tuple[str, ...] = ("provider-sim", "minio", "minio-init")
PRODUCTION_FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = ("CI_MINIO_", "CI_PROVIDER_")


# ════════════════════════════════════════════════════════════════
# resolved config 解析(兼容 JSON / YAML)
# ════════════════════════════════════════════════════════════════

def load_resolved(path: Path) -> dict[str, Any]:
    """加载 resolved compose 配置(JSON 优先,YAML 兜底)。"""
    text = path.read_text(encoding="utf-8")
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise ValueError("输入既非 JSON,且 PyYAML 不可用,无法解析 YAML") from exc
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"resolved config 不可解析(JSON/YAML 均失败): {exc}") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("services"), dict):
        raise ValueError("resolved config 缺少 services 段")
    return doc


def _environment_pairs(service: dict[str, Any]) -> list[str]:
    """统一 environment 为 ["K=V", ...] 形式(兼容 dict / list)。"""
    env = service.get("environment", {})
    if isinstance(env, dict):
        return [f"{k}={v}" for k, v in env.items()]
    if isinstance(env, list):
        pairs: list[str] = []
        for item in env:
            if isinstance(item, str) and "=" in item:
                pairs.append(item)
            elif isinstance(item, str):
                pairs.append(f"{item}=")
        return pairs
    return []


def _depends_on_names(service: dict[str, Any]) -> set[str]:
    deps = service.get("depends_on", {})
    if isinstance(deps, dict):
        return set(deps.keys())
    if isinstance(deps, list):
        return {str(d) for d in deps}
    return set()


def _port_entries(service: dict[str, Any]) -> list[dict[str, Any]]:
    """统一 ports 为 [{"host_ip":..., "published":..., "target":...}]。"""
    ports = service.get("ports", []) or []
    out: list[dict[str, Any]] = []
    for p in ports:
        if isinstance(p, dict):
            out.append(p)
        elif isinstance(p, str):
            # "127.0.0.1:8080:8080" / "8080:8080" / "8080"
            parts = p.split(":")
            if len(parts) == 3:
                out.append({"host_ip": parts[0], "published": parts[1], "target": parts[2]})
            elif len(parts) == 2:
                out.append({"host_ip": "0.0.0.0", "published": parts[0], "target": parts[1]})
            else:
                out.append({"host_ip": "0.0.0.0", "published": parts[0], "target": parts[0]})
    return out


def _tmpfs_targets(service: dict[str, Any]) -> set[str]:
    tmpfs = service.get("tmpfs", []) or []
    if isinstance(tmpfs, str):
        tmpfs = [tmpfs]
    return {str(t).split(":", 1)[0] for t in tmpfs}


# ════════════════════════════════════════════════════════════════
# 图导出(P2: 结构化 artifact)
# ════════════════════════════════════════════════════════════════

def export_service_graph(doc: dict[str, Any], output: Path) -> None:
    """导出服务图(节点/依赖边/健康检查目标)为结构化 artifact。"""
    services = doc.get("services", {})
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for name, svc in sorted(services.items()):
        if not isinstance(svc, dict):
            continue
        health = svc.get("healthcheck", {}) or {}
        health_test = health.get("test", [])
        nodes.append({
            "name": name,
            "image": svc.get("image") or svc.get("build", {}).get("dockerfile", "build:."),
            "is_crdb": CRDB_IMAGE_MARKER in str(svc.get("image", "")),
            "read_only": bool(svc.get("read_only", False)),
            "tmpfs": sorted(_tmpfs_targets(svc)),
            "ports": _port_entries(svc),
            "healthcheck": " ".join(str(t) for t in health_test) if health_test else None,
        })
        for dep in sorted(_depends_on_names(svc)):
            dep_cond = svc.get("depends_on", {})
            cond = ""
            if isinstance(dep_cond, dict) and isinstance(dep_cond.get(dep), dict):
                cond = str(dep_cond[dep].get("condition", ""))
            edges.append({"from": name, "to": dep, "condition": cond})
    graph = {
        "schema_version": "service-graph/v1",
        "services": nodes,
        "dependency_edges": edges,
        "crdb_service_count": sum(1 for n in nodes if n["is_crdb"]),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")


# ════════════════════════════════════════════════════════════════
# 断言实现
# ════════════════════════════════════════════════════════════════

def find_crdb_services(doc: dict[str, Any]) -> list[str]:
    """按镜像识别 CockroachDB 服务。"""
    out: list[str] = []
    for name, svc in doc.get("services", {}).items():
        if isinstance(svc, dict) and CRDB_IMAGE_MARKER in str(svc.get("image", "")):
            out.append(name)
    return sorted(out)


def validate_secretless(doc: dict[str, Any]) -> list[tuple[str, str]]:
    """secretless 图断言,返回 (error_code, detail) 列表。"""
    violations: list[tuple[str, str]] = []
    services: dict[str, Any] = doc.get("services", {})

    # 1. 单 CRDB
    crdb_services = find_crdb_services(doc)
    if len(crdb_services) > 1:
        violations.append((
            ERR_MULTIPLE_CRDB,
            f"服务图包含 {len(crdb_services)} 个 CockroachDB 服务: "
            f"{crdb_services} — 单 CRDB 拓扑要求恰好 1 个",
        ))
        return violations  # 双 CRDB 是致命结构错误,立即返回
    if not crdb_services:
        violations.append((ERR_NO_CRDB, "服务图不包含 CockroachDB 服务"))
        return violations
    crdb_name = crdb_services[0]

    # 2. 应用角色依赖同一 CRDB
    for role in CRDB_DEPENDENT_ROLES:
        svc = services.get(role)
        if not isinstance(svc, dict):
            violations.append((ERR_DEP_MISMATCH, f"应用角色 {role} 不在服务图中"))
            continue
        deps = _depends_on_names(svc)
        if crdb_name not in deps:
            violations.append((
                ERR_DEP_MISMATCH,
                f"{role}.depends_on 缺少 {crdb_name}(实际: {sorted(deps)})",
            ))

    # 3. DSN host/port 一致
    dsn_values: dict[str, str] = {}
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for pair in _environment_pairs(svc):
            key, _, value = pair.partition("=")
            if key in CRDB_DSN_VARS and value:
                dsn_values[f"{name}.{key}"] = value
    canonical: set[str] = set(dsn_values.values())
    if len(canonical) > 1:
        violations.append((
            ERR_DSN_MISMATCH,
            f"CRDB DSN 不一致: {json.dumps(dsn_values, ensure_ascii=False)}",
        ))
    dsn_pattern = re.compile(r"^postgresql://\w+@(?P<host>[\w.-]+):(?P<port>\d+)/")
    for owner, dsn in dsn_values.items():
        m = dsn_pattern.match(dsn)
        if not m:
            violations.append((ERR_DSN_MISMATCH, f"{owner} DSN 不可解析: {dsn}"))
            continue
        if m.group("host") != crdb_name:
            violations.append((
                ERR_DSN_MISMATCH,
                f"{owner} DSN host={m.group('host')} 必须等于唯一 CRDB 服务 {crdb_name}",
            ))
        if m.group("port") != EXPECTED_SQL_PORT:
            violations.append((
                ERR_DSN_MISMATCH,
                f"{owner} DSN port={m.group('port')} 必须为 SQL 端口 {EXPECTED_SQL_PORT}",
            ))

    # 4. 禁止 production 形态(真实端点 / 公网端口)
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for pair in _environment_pairs(svc):
            for pattern in PRODUCTION_ENDPOINT_PATTERNS:
                if pattern in pair:
                    violations.append((
                        ERR_FORBIDDEN,
                        f"{name} 环境包含 production 端点 {pattern}: {pair}",
                    ))
        for port in _port_entries(svc):
            host_ip = str(port.get("host_ip", "0.0.0.0"))
            if host_ip not in ("127.0.0.1", "localhost", "::1"):
                violations.append((
                    ERR_FORBIDDEN,
                    f"{name} 端口 {port} 绑定 {host_ip} — "
                    f"secretless 图禁止非公网隔离(loopback 以外)端口绑定",
                ))

    # 5. 唯一 CRDB 最小写集合加固(override 继承证明)
    crdb_svc = services.get(crdb_name, {})
    if not crdb_svc.get("read_only", False):
        violations.append((
            ERR_HARDENING,
            f"{crdb_name} read_only != true — R79 §10.1 最小写集合要求只读 rootfs",
        ))
    missing_tmpfs = set(REQUIRED_TMPFS_TARGETS) - _tmpfs_targets(crdb_svc)
    if missing_tmpfs:
        violations.append((
            ERR_HARDENING,
            f"{crdb_name} tmpfs 缺少 {sorted(missing_tmpfs)}"
            f"(实际: {sorted(_tmpfs_targets(crdb_svc))})",
        ))

    return violations


def validate_production(doc: dict[str, Any]) -> list[tuple[str, str]]:
    """production 图断言(输入 = 仅 docker-compose.yml resolved config)。"""
    violations: list[tuple[str, str]] = []
    services: dict[str, Any] = doc.get("services", {})

    for forbidden in PRODUCTION_FORBIDDEN_SERVICES:
        if forbidden in services:
            violations.append((
                ERR_PROD_FORBIDDEN,
                f"production 图禁止包含 {forbidden} 服务(R76 10.C 隔离要求)",
            ))

    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        for pair in _environment_pairs(svc):
            for forbidden in PRODUCTION_FORBIDDEN_ENV:
                if pair == forbidden:
                    violations.append((
                        ERR_PROD_FORBIDDEN,
                        f"{name} 环境包含 secretless 标记 {forbidden}",
                    ))
            for prefix in PRODUCTION_FORBIDDEN_ENV_PREFIXES:
                if pair.startswith(prefix):
                    violations.append((
                        ERR_PROD_FORBIDDEN,
                        f"{name} 环境包含临时凭据变量 {pair.split('=', 1)[0]}",
                    ))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("resolved", type=Path, help="resolved compose config(JSON 或 YAML)")
    parser.add_argument(
        "--mode",
        choices=("secretless", "production"),
        default="secretless",
        help="断言模式(默认 secretless)",
    )
    parser.add_argument(
        "--export-graph",
        type=Path,
        default=None,
        help="导出结构化服务图 artifact(P2)",
    )
    args = parser.parse_args(argv)

    try:
        doc = load_resolved(args.resolved)
    except (OSError, ValueError) as exc:
        print(f"ERROR: 无法解析 resolved config: {exc}", file=sys.stderr)
        return 2

    if args.export_graph is not None:
        export_service_graph(doc, args.export_graph)
        print(f"service graph exported: {args.export_graph}")

    if args.mode == "secretless":
        violations = validate_secretless(doc)
    else:
        violations = validate_production(doc)

    if violations:
        for code, detail in violations:
            print(f"::error::{code}: {detail}")
        return 1

    if args.mode == "secretless":
        crdb = find_crdb_services(doc)
        print(
            f"PASS: secretless service graph — 单 CRDB ({crdb[0]}),"
            f"{len(doc.get('services', {}))} 服务,依赖/DSN/隔离/加固断言全部通过"
        )
    else:
        print("PASS: production service graph — 无 secretless 组件/标记/临时凭据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
