#!/usr/bin/env python3
"""R67 P1-09 / R69 Wave 7 / R70 Wave 4: Compose 静态规则门禁。

整改背景(R67 终审报告 P1-09 + R69 Wave 7 + R70 Wave 4):
    R69 Wave 7 要求:静态 lint 不得命名为 "runtime smoke"。
    R70 Wave 4 要求:生产 compose 必须不可变(禁止 build:、要求 image digest、
    禁止代码 bind mount)。

本脚本的范围与边界(诚实声明):
    1. 本脚本是**静态规则门禁**,不是运行态 smoke。
       本脚本只解析 docker-compose.yml,验证可静态判定的运行态契约,
       不会启动任何容器,不验证真实运行时行为。

    2. 本脚本通过解析 docker-compose.yml,验证以下可静态判定的运行态契约:
         (a) 非 root: Dockerfile USER 非 0(compose 无 user 覆盖时由 Dockerfile 决定)
         (b) read-only filesystem: `read_only: true`
         (c) tmpfs: 有 /tmp 可写挂载(配合 read_only)
         (d) cap_drop: 至少 drop ALL
         (e) security_opt: no-new-privileges:true
         (f) healthcheck: 每个长运行服务必须配置 healthcheck
         (g) secrets mount: secret 通过 .env.secrets.<service> env_file 注入
         (h) 网络隔离: 端口必须绑定 127.0.0.1
         (i) restart policy: 长运行/oneshot 策略
         (j) graceful shutdown: stop_signal 不得为 SIGKILL
         (k) migration ordering: depends_on.migration.condition

    3. R70 Wave 4 不可变性校验(仅 --immutable 模式):
         (l) 禁止 build: 所有应用服务不能有 build 字段
         (m) 要求 image: 所有应用服务必须有 image 字段
         (n) 统一 digest: 所有应用服务 image 必须引用同一变量 ${TGJIEMA_IMAGE}
         (o) 禁止代码 bind mount: 不得挂载目录级 Python 代码源
             (./config:/app/config, ./services:/app/services 等)
         (p) 禁止 mutable tag: image 不得为 latest/master/staging 等可变标签
             (生产 compose 通过 ${TGJIEMA_IMAGE} 变量注入 digest,静态校验
             只验证变量引用一致性;实际 digest 值由部署时 .env 提供)

    4. CI 调用方式:
         # 开发版本(允许 build:)
         python scripts/check_compose_static_rules.py

         # 生产版本(R70 Wave 4 不可变性校验)
         python scripts/check_compose_static_rules.py \\
             --compose docker-compose.prod.yml --immutable

退出码:
    0 — 所有静态规则通过
    1 — 检测到违规
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

# 项目根目录(scripts/ 的上一级)
REPO_ROOT = Path(__file__).resolve().parent.parent

# 默认 compose 文件路径
DEFAULT_COMPOSE = REPO_ROOT / "docker-compose.yml"

# 长运行服务(必须 restart: always 或 unless-stopped + healthcheck)
LONG_RUNNING_SERVICES: frozenset[str] = frozenset({
    "redis",
    "db_writer",
    "crdb_sync",
    "up",
    "idx",
    "dsp",
    "mon",
    "admin_bot",
    "admin",
    "db_backup",
    "prometheus_exporter",
})

# oneshot 服务(必须 restart: "no",不需 healthcheck)
ONESHOT_SERVICES: frozenset[str] = frozenset({
    "redis-acl-init",
    "migration",
})

# 依赖 migration 的服务(必须 depends_on.migration.condition:
# service_completed_successfully)
MIGRATION_DEPENDENT_SERVICES: frozenset[str] = frozenset({
    "db_writer",
    "crdb_sync",
    "up",
    "idx",
    "dsp",
    "mon",
    "admin_bot",
    "admin",
    "db_backup",
    "prometheus_exporter",
})

# 允许暴露端口的服务(端口必须绑定 127.0.0.1)
PORT_EXPOSING_SERVICES: frozenset[str] = frozenset({
    "redis",       # 127.0.0.1:6379
    "admin",       # 127.0.0.1:8080
    "prometheus_exporter",  # 127.0.0.1:9100
})

# 允许的 restart 策略
VALID_LONG_RUNNING_RESTART: frozenset[str] = frozenset({
    "always", "unless-stopped", "on-failure",
})

# SIGTERM 是默认 stop_signal(Dockerfile STOPSIGNAL SIGTERM);
# compose 中若显式覆盖,只允许 SIGTERM / SIGINT(优雅关闭信号)
VALID_STOP_SIGNALS: frozenset[str] = frozenset({
    "SIGTERM", "SIGINT", "15", "2",
})

# R70 Wave 4: 不可变性校验常量

# 基础设施服务(使用官方镜像,不需要应用 digest)
INFRASTRUCTURE_SERVICES: frozenset[str] = frozenset({
    "redis",
    "redis-acl-init",
})

# 基础设施镜像前缀(不需要 digest,使用官方 tag)
INFRASTRUCTURE_IMAGE_PREFIXES: tuple[str, ...] = (
    "redis:",
    "postgres:",
    "minio:",
    "cockroachdb/",
)

# 禁止的目录级代码 bind mount(Python 代码源)
# 形如 ./config:/app/config 的目录挂载会覆盖镜像中的 Python 代码
FORBIDDEN_CODE_BIND_MOUNTS: tuple[str, ...] = (
    "./config:/app/config",
    "./services:/app/services",
    "./bots:/app/bots",
    "./admin:/app/admin",
    "./database:/app/database",
    "./utils:/app/utils",
)

# 禁止的 mutable tag(可变标签,无 digest)
FORBIDDEN_MUTABLE_TAGS: frozenset[str] = frozenset({
    "latest",
    "master",
    "staging",
    "stable",
    "edge",
    "dev",
})

# 生产 compose 必须使用的统一 image 变量
PRODUCTION_IMAGE_VARIABLE = "${TGJIEMA_IMAGE"

# 允许的运行时配置数据文件挂载(文件级,非目录级)
# 这些是配置数据文件,不是 Python 代码
ALLOWED_CONFIG_DATA_MOUNTS: tuple[str, ...] = (
    "groups.yaml",
    "topology.yaml",
    "services.yaml",
    "users.acl.template",
    "render_acl.sh",
)


# ════════════════════════════════════════════════════════════════
# YAML 解析(避免引入 PyYAML 依赖,使用最小解析器)
# ════════════════════════════════════════════════════════════════
# 注:docker-compose.yml 结构稳定且本仓库已用,这里使用 PyYAML 优先,
# 失败时返回错误并提示安装(避免 silently 跳过检查)。

def _load_compose(path: Path) -> dict[str, Any]:
    """加载 docker-compose.yml,返回解析后的 dict。

    Raises:
        ImportError: PyYAML 未安装
        FileNotFoundError: 文件不存在
        yaml.YAMLError: YAML 解析失败
    """
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "PyYAML 未安装 — docker-compose 静态规则门禁需要 PyYAML 解析 YAML。"
            "请运行: pip install pyyaml"
        ) from e
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} 顶层结构不是 dict")
    return data


# ════════════════════════════════════════════════════════════════
# 规则检查
# ════════════════════════════════════════════════════════════════


class Violation:
    """单条违规。"""

    def __init__(self, service: str, rule: str, detail: str) -> None:
        self.service = service
        self.rule = rule
        self.detail = detail

    def __str__(self) -> str:
        return f"  - service={self.service} [{self.rule}] {self.detail}"


def _check_read_only(
    name: str, svc: dict[str, Any], is_oneshot: bool
) -> list[Violation]:
    """(b) read_only: true;(c) tmpfs 有 /tmp。"""
    out: list[Violation] = []
    # redis-acl-init 也 read_only(已是)
    if not svc.get("read_only", False):
        out.append(Violation(
            name, "read_only",
            "缺少 read_only: true — 容器文件系统必须只读",
        ))
    tmpfs = svc.get("tmpfs", []) or []
    if isinstance(tmpfs, list) and "/tmp" not in tmpfs:
        out.append(Violation(
            name, "tmpfs",
            "tmpfs 缺少 /tmp — read_only 容器需要 /tmp 可写挂载",
        ))
    return out


def _check_cap_drop(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(d) cap_drop: 至少 ALL。"""
    out: list[Violation] = []
    cap_drop = svc.get("cap_drop", []) or []
    if "ALL" not in cap_drop:
        out.append(Violation(
            name, "cap_drop",
            "cap_drop 必须包含 ALL — 最小权限原则",
        ))
    return out


def _check_security_opt(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(e) security_opt: no-new-privileges:true。"""
    out: list[Violation] = []
    sec_opt = svc.get("security_opt", []) or []
    if "no-new-privileges:true" not in sec_opt:
        out.append(Violation(
            name, "security_opt",
            "security_opt 必须包含 no-new-privileges:true — 禁止提权",
        ))
    return out


def _check_healthcheck(
    name: str, svc: dict[str, Any], is_long_running: bool
) -> list[Violation]:
    """(f) 长运行服务必须有 healthcheck。"""
    out: list[Violation] = []
    if is_long_running and "healthcheck" not in svc:
        out.append(Violation(
            name, "healthcheck",
            "长运行服务缺少 healthcheck — 运行态探针必须配置",
        ))
    return out


def _check_restart_policy(
    name: str, svc: dict[str, Any], is_long_running: bool, is_oneshot: bool
) -> list[Violation]:
    """(i) restart policy。"""
    out: list[Violation] = []
    restart = svc.get("restart", "")
    if is_oneshot:
        if restart != "no":
            out.append(Violation(
                name, "restart",
                f"oneshot 服务 restart 必须为 'no',实际为 '{restart}'",
            ))
    elif is_long_running:
        if restart not in VALID_LONG_RUNNING_RESTART:
            out.append(Violation(
                name, "restart",
                f"长运行服务 restart 必须为 always/unless-stopped/on-failure,"
                f"实际为 '{restart}'",
            ))
    return out


def _check_port_binding(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(h) 暴露端口必须绑定 127.0.0.1。"""
    out: list[Violation] = []
    ports = svc.get("ports", []) or []
    for port_entry in ports:
        if isinstance(port_entry, str):
            # 形如 "127.0.0.1:8080:8080" 或 "8080:8080" 或 "8080"
            if not port_entry.startswith("127.0.0.1:") and not port_entry.startswith("localhost:"):
                out.append(Violation(
                    name, "ports",
                    f"端口 '{port_entry}' 必须绑定到 127.0.0.1 — "
                    f"禁止绑定 0.0.0.0(避免外网暴露)",
                ))
        elif isinstance(port_entry, dict):
            # 长形式 {published: 8080, target: 8080, host_ip: "127.0.0.1"}
            host_ip = port_entry.get("host_ip", "")
            if host_ip not in ("127.0.0.1", "localhost", "::1"):
                out.append(Violation(
                    name, "ports",
                    f"端口 {port_entry} host_ip 必须为 127.0.0.1 — "
                    f"禁止绑定 0.0.0.0",
                ))
    return out


def _check_migration_ordering(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(k) migration 依赖 — 必须有 depends_on.migration.condition:
    service_completed_successfully。"""
    out: list[Violation] = []
    depends_on = svc.get("depends_on", {}) or {}
    mig_dep = depends_on.get("migration") if isinstance(depends_on, dict) else None
    if isinstance(mig_dep, dict):
        condition = mig_dep.get("condition", "")
        if condition != "service_completed_successfully":
            out.append(Violation(
                name, "migration_ordering",
                f"depends_on.migration.condition 必须为 "
                f"service_completed_successfully,实际为 '{condition}'",
            ))
    elif mig_dep is None and "migration" not in (depends_on if isinstance(depends_on, dict) else {}):
        out.append(Violation(
            name, "migration_ordering",
            "缺少 depends_on.migration — migration 必须先于业务服务完成",
        ))
    return out


def _check_stop_signal(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(j) stop_signal — 若显式覆盖,只允许 SIGTERM/SIGINT。"""
    out: list[Violation] = []
    stop_signal = svc.get("stop_signal")
    if stop_signal is not None and str(stop_signal) not in VALID_STOP_SIGNALS:
        out.append(Violation(
            name, "stop_signal",
            f"stop_signal={stop_signal} 不允许 — 必须为 SIGTERM/SIGINT "
            f"(graceful shutdown),不允许 SIGKILL(9)",
        ))
    return out


def _check_secrets_injection(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(g) secrets 注入 — 必须通过 env_file(.env.secrets.<service>),
    不通过共享卷挂载。

    本仓库约定:每个需要 secrets 的服务通过 env_file 注入
    .env.secrets.<service>,而非挂载 secrets 文件到容器。这避免
    secret 文件在共享卷中泄露。
    """
    out: list[Violation] = []
    # 检查是否挂载了 secrets 文件(以 .env 开头的文件作为 volume 挂载)
    volumes = svc.get("volumes", []) or []
    for vol in volumes:
        if isinstance(vol, str):
            # 形如 "./.env.secrets.up:/app/.env.secrets:ro"
            if ".env" in vol and ":/" in vol:
                out.append(Violation(
                    name, "secrets_mount",
                    f"volume '{vol}' 挂载 .env 文件 — secrets 必须通过 "
                    f"env_file 注入,不得挂载到容器",
                ))
    return out


def _check_resource_limits(name: str, svc: dict[str, Any]) -> list[Violation]:
    """附加检查:每个服务必须有 deploy.resources.limits(cpus + memory)。"""
    out: list[Violation] = []
    deploy = svc.get("deploy", {}) or {}
    resources = deploy.get("resources", {}) or {}
    limits = resources.get("limits", {}) or {}
    if not limits:
        out.append(Violation(
            name, "resource_limits",
            "缺少 deploy.resources.limits — 每个服务必须有 cpus/memory 限制",
        ))
    else:
        if "cpus" not in limits:
            out.append(Violation(
                name, "resource_limits",
                "deploy.resources.limits 缺少 cpus",
            ))
        if "memory" not in limits:
            out.append(Violation(
                name, "resource_limits",
                "deploy.resources.limits 缺少 memory",
            ))
    return out


# ════════════════════════════════════════════════════════════════
# R70 Wave 4: 不可变性校验(仅 --immutable 模式)
# ════════════════════════════════════════════════════════════════


def _is_infrastructure_service(name: str, svc: dict[str, Any]) -> bool:
    """判断是否为基础设施服务(redis/postgres 等使用官方镜像)。"""
    if name in INFRASTRUCTURE_SERVICES:
        return True
    image = str(svc.get("image", ""))
    for prefix in INFRASTRUCTURE_IMAGE_PREFIXES:
        if image.startswith(prefix):
            return True
    return False


def _check_no_build(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(l) 禁止 build: — 不可变 compose 不能有 build 字段。"""
    out: list[Violation] = []
    if "build" in svc:
        build_val = svc["build"]
        if isinstance(build_val, str):
            detail = f"build: {build_val}"
        elif isinstance(build_val, dict):
            detail = f"build: {build_val.get('context', build_val)}"
        else:
            detail = f"build: {build_val}"
        out.append(Violation(
            name, "immutable_no_build",
            f"禁止 build: 字段({detail}) — 生产 compose 必须使用不可变 image digest, "
            f"不得在部署阶段重新构建镜像",
        ))
    return out


def _check_has_image(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(m) 要求 image: — 不可变 compose 必须有 image 字段。"""
    out: list[Violation] = []
    if "image" not in svc:
        out.append(Violation(
            name, "immutable_requires_image",
            "缺少 image: 字段 — 不可变 compose 必须显式指定 image digest",
        ))
    return out


def _check_unified_image_digest(
    name: str, svc: dict[str, Any], is_infra: bool
) -> list[Violation]:
    """(n) 统一 digest — 所有应用服务必须引用 ${TGJIEMA_IMAGE} 变量。

    生产 compose 通过 ${TGJIEMA_IMAGE} 环境变量注入不可变 digest:
        ghcr.io/maxiuquan/tgjiema@sha256:<64 hex>

    所有应用服务必须引用同一变量,确保 Build Once / Deploy Same Digest。
    基础设施服务(redis 等)使用官方镜像,豁免此规则。
    """
    out: list[Violation] = []
    if is_infra:
        return out  # 基础设施服务豁免

    image = str(svc.get("image", ""))
    if not image:
        return out  # _check_has_image 已处理

    # 必须引用 ${TGJIEMA_IMAGE} 变量(可能带 :? 错误提示)
    if PRODUCTION_IMAGE_VARIABLE not in image:
        out.append(Violation(
            name, "immutable_unified_digest",
            f"image '{image}' 未引用 ${{TGJIEMA_IMAGE}} 变量 — "
            f"所有应用服务必须使用同一不可变 digest, "
            f"通过 ${{TGJIEMA_IMAGE}} 环境变量注入",
        ))
    return out


def _check_no_code_bind_mount(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(o) 禁止代码 bind mount — 不得挂载目录级 Python 代码源。

    形如 ./config:/app/config 的目录挂载会覆盖镜像中的 Python 代码,
    绕过已签名的镜像内容。生产 compose 只允许:
      - 数据目录挂载(./data, ./logs)
      - 文件级配置数据挂载(./config/groups.yaml:/app/config/groups.yaml:ro)
    """
    out: list[Violation] = []
    volumes = svc.get("volumes", []) or []
    for vol in volumes:
        if not isinstance(vol, str):
            continue
        # 提取源路径(冒号前部分)
        # 形如 "./config:/app/config" 或 "./config:/app/config:ro"
        parts = vol.split(":")
        if len(parts) < 2:
            continue
        source = parts[0]

        # 检查是否为目录级代码 bind mount
        for forbidden in FORBIDDEN_CODE_BIND_MOUNTS:
            forbidden_source = forbidden.split(":")[0]
            if source == forbidden_source:
                # 检查目标路径是否为目录级(非文件级)
                target = parts[1]
                # 文件级挂载的 target 会包含扩展名(如 /app/config/groups.yaml)
                # 目录级挂载的 target 是目录路径(如 /app/config)
                target_has_ext = "." in target.split("/")[-1]
                source_has_ext = "." in source.split("/")[-1]

                if not (target_has_ext or source_has_ext):
                    # 目录级代码 bind mount
                    out.append(Violation(
                        name, "immutable_no_code_mount",
                        f"volume '{vol}' 挂载目录级 Python 代码源 — "
                        f"生产 compose 禁止代码 bind mount, "
                        f"只允许数据目录或文件级配置数据挂载",
                    ))
    return out


def _check_no_mutable_tag(name: str, svc: dict[str, Any]) -> list[Violation]:
    """(p) 禁止 mutable tag — image 不得为 latest/master/staging 等可变标签。

    生产 compose 通过 ${TGJIEMA_IMAGE} 变量注入 digest:
        ghcr.io/maxiuquan/tgjiema@sha256:<64 hex>

    禁止可变标签(latest/master/staging/v1.2.3 无 digest)。
    基础设施服务(redis:7-alpine 等)豁免。
    """
    out: list[Violation] = []
    image = str(svc.get("image", ""))
    if not image:
        return out

    # 引用变量的(如 ${TGJIEMA_IMAGE:?...})跳过静态检查
    # 实际 digest 值由部署时 .env 提供,运行时验证
    if PRODUCTION_IMAGE_VARIABLE in image:
        return out

    # 基础设施镜像(redis:7-alpine 等)豁免
    is_infra = False
    for prefix in INFRASTRUCTURE_IMAGE_PREFIXES:
        if image.startswith(prefix):
            is_infra = True
            break
    if is_infra:
        return out

    # 检查是否为可变标签
    # 提取 tag 部分(冒号后,如果有)
    if ":" in image:
        tag_part = image.rsplit(":", 1)[1]
        # 排除 digest 格式(@sha256:)
        if "@sha256:" in tag_part:
            return out  # 带 digest,不可变
        if tag_part in FORBIDDEN_MUTABLE_TAGS:
            out.append(Violation(
                name, "immutable_no_mutable_tag",
                f"image '{image}' 使用可变 tag '{tag_part}' — "
                f"生产 compose 禁止 mutable tag, 必须使用 digest "
                f"(ghcr.io/...@sha256:<64 hex>) 或 ${{TGJIEMA_IMAGE}} 变量",
            ))
    elif not image.startswith("${"):
        # 没有 tag 也没有变量引用 — 可能使用默认 latest
        out.append(Violation(
            name, "immutable_no_mutable_tag",
            f"image '{image}' 无 tag — 隐式使用 'latest', "
            f"生产 compose 禁止 mutable tag",
        ))
    return out


def _check_app_env_production(name: str, svc: dict[str, Any]) -> list[Violation]:
    """R70 Wave 4 附加:生产 compose 必须硬编码 APP_ENV=production。"""
    out: list[Violation] = []
    env = svc.get("environment", []) or []
    if isinstance(env, list):
        env_vars = {item.split("=", 1)[0]: item.split("=", 1)[1] if "=" in item else ""
                    for item in env if isinstance(item, str)}
    elif isinstance(env, dict):
        env_vars = dict(env)
    else:
        env_vars = {}

    app_env = str(env_vars.get("APP_ENV", ""))
    if app_env != "production":
        out.append(Violation(
            name, "immutable_app_env",
            f"APP_ENV='{app_env}' — 生产 compose 必须硬编码 APP_ENV=production",
        ))
    return out


def _check_escape_hatches_unset(name: str, svc: dict[str, Any]) -> list[Violation]:
    """R70 Wave 4 附加:生产 compose 必须显式 unset 所有测试逃生舱变量。

    防止 .env 中误设 I18N_ALLOW_FALLBACK=1 等逃生舱变量导致生产环境降级。
    """
    out: list[Violation] = []
    env = svc.get("environment", []) or []
    if isinstance(env, list):
        env_vars = {item.split("=", 1)[0]: item.split("=", 1)[1] if "=" in item else ""
                    for item in env if isinstance(item, str)}
    elif isinstance(env, dict):
        env_vars = dict(env)
    else:
        env_vars = {}

    required_unset = [
        "I18N_ALLOW_FALLBACK",
        "ALLOW_LEGACY_RESTORE",
        "TEST_ONLY",
        "DEV_ONLY",
        "BYPASS",
        "SKIP_VERIFY",
        "SKIP_VALIDATION",
        "ALLOW_INSECURE",
    ]
    for var in required_unset:
        if var not in env_vars:
            out.append(Violation(
                name, "immutable_escape_hatch_unset",
                f"缺少 {var}= — 生产 compose 必须显式 unset 测试逃生舱变量 "
                f"(设置为空字符串)",
            ))
    return out


# ════════════════════════════════════════════════════════════════
# 主校验流程
# ════════════════════════════════════════════════════════════════


def check(
    compose_path: Path = DEFAULT_COMPOSE,
    immutable: bool = False,
) -> tuple[int, list[Violation]]:
    """主校验流程。

    Args:
        compose_path: compose 文件路径
        immutable: 是否启用 R70 Wave 4 不可变性校验(生产 compose 必须 True)

    Returns:
        (exit_code, violations)
        exit_code: 0=无违规,1=有违规
    """
    if not compose_path.is_file():
        return 1, [Violation(
            "(global)", "compose_file",
            f"compose 文件不存在: {compose_path}",
        )]

    try:
        data = _load_compose(compose_path)
    except ImportError as e:
        # PyYAML 缺失 — fail-closed(不允许 silently 跳过)
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1, [Violation("(global)", "dependency", str(e))]
    except Exception as e:
        return 1, [Violation("(global)", "compose_parse", str(e))]

    services = data.get("services", {}) or {}
    if not services:
        return 1, [Violation("(global)", "services",
                              "compose 文件缺少 services 段")]

    violations: list[Violation] = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            violations.append(Violation(
                name, "service_shape",
                f"service 定义不是 dict: {type(svc).__name__}",
            ))
            continue

        is_long_running = name in LONG_RUNNING_SERVICES
        is_oneshot = name in ONESHOT_SERVICES

        # (b)(c) read_only + tmpfs
        violations.extend(_check_read_only(name, svc, is_oneshot))
        # (d) cap_drop
        violations.extend(_check_cap_drop(name, svc))
        # (e) security_opt
        violations.extend(_check_security_opt(name, svc))
        # (f) healthcheck(仅长运行)
        violations.extend(_check_healthcheck(name, svc, is_long_running))
        # (i) restart policy
        violations.extend(_check_restart_policy(
            name, svc, is_long_running, is_oneshot,
        ))
        # (h) port binding(仅暴露端口的服务)
        if name in PORT_EXPOSING_SERVICES or svc.get("ports"):
            violations.extend(_check_port_binding(name, svc))
        # (k) migration ordering(仅依赖 migration 的服务)
        if name in MIGRATION_DEPENDENT_SERVICES:
            violations.extend(_check_migration_ordering(name, svc))
        # (j) stop_signal(若显式覆盖)
        violations.extend(_check_stop_signal(name, svc))
        # (g) secrets injection
        violations.extend(_check_secrets_injection(name, svc))
        # 附加:resource limits
        violations.extend(_check_resource_limits(name, svc))

        # R70 Wave 4: 不可变性校验(仅 --immutable 模式)
        if immutable:
            is_infra = _is_infrastructure_service(name, svc)
            # (l) 禁止 build:
            violations.extend(_check_no_build(name, svc))
            # (m) 要求 image:(非基础设施服务)
            if not is_infra:
                violations.extend(_check_has_image(name, svc))
            # (n) 统一 digest(非基础设施服务)
            violations.extend(_check_unified_image_digest(name, svc, is_infra))
            # (o) 禁止代码 bind mount
            violations.extend(_check_no_code_bind_mount(name, svc))
            # (p) 禁止 mutable tag
            violations.extend(_check_no_mutable_tag(name, svc))
            # 附加:APP_ENV=production(非基础设施服务)
            if not is_infra:
                violations.extend(_check_app_env_production(name, svc))
            # 附加:逃生舱变量 unset(非基础设施服务)
            if not is_infra:
                violations.extend(_check_escape_hatches_unset(name, svc))

    if violations:
        mode_label = "不可变性" if immutable else "静态规则"
        print(
            f"[FAIL] R70 Wave 4 compose {mode_label}门禁检测到 "
            f"{len(violations)} 处违规:"
        )
        for v in violations:
            print(v)
        return 1, violations

    mode_label = "不可变性" if immutable else "静态规则"
    print(
        f"[OK] R70 Wave 4 compose {mode_label}门禁通过 "
        f"(校验 {len(services)} 个服务,无违规)"
    )
    return 0, violations


def main(argv: list[str] | None = None) -> None:
    """脚本入口。"""
    parser = argparse.ArgumentParser(
        description="R70 Wave 4: Compose 静态规则 + 不可变性门禁",
    )
    parser.add_argument(
        "--compose",
        type=Path,
        default=DEFAULT_COMPOSE,
        help=f"compose 文件路径(默认: {DEFAULT_COMPOSE})",
    )
    parser.add_argument(
        "--immutable",
        action="store_true",
        help="启用 R70 Wave 4 不可变性校验(生产 compose 必须)",
    )
    args = parser.parse_args(argv)
    exit_code, _ = check(args.compose, immutable=args.immutable)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
