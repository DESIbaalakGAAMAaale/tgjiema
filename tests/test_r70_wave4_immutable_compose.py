"""R70 Wave 4: 不可变生产 Compose 测试。

整改背景:
    R70 P0-05: 生产 Compose 使用 `build: .` 而非不可变 digest,
    可绕过已签名镜像。

整改要求:
    1. 生产 compose 禁止 build: 字段
    2. 生产 compose 必须使用 image: ${TGJIEMA_IMAGE:?...}
    3. 生产 compose 禁止目录级代码 bind mount(./config:/app/config 等)
    4. 生产 compose 必须硬编码 APP_ENV=production
    5. 生产 compose 必须显式 unset 所有测试逃生舱变量
    6. 所有应用服务使用同一 image digest(通过 ${TGJIEMA_IMAGE} 变量)
    7. 基础设施服务(redis)豁免应用 digest 要求

测试覆盖:
    A. 生产 compose 文件存在性
    B. 不可变性校验脚本(check_compose_static_rules.py --immutable)
    C. 生产 compose 无 build: 字段
    D. 生产 compose 应用服务使用 ${TGJIEMA_IMAGE} 变量
    E. 生产 compose 无目录级代码 bind mount
    F. 生产 compose APP_ENV=production
    G. 生产 compose 逃生舱变量显式 unset
    H. 开发 compose 允许 build:(回归保护)
    I. 校验函数单元测试(模拟违规场景)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# 导入被测模块
from scripts.check_compose_static_rules import (  # noqa: E402
    DEFAULT_COMPOSE,
    FORBIDDEN_CODE_BIND_MOUNTS,
    FORBIDDEN_MUTABLE_TAGS,
    INFRASTRUCTURE_SERVICES,
    PRODUCTION_IMAGE_VARIABLE,
    Violation,
    _check_app_env_production,
    _check_escape_hatches_unset,
    _check_has_image,
    _check_no_build,
    _check_no_code_bind_mount,
    _check_no_mutable_tag,
    _check_unified_image_digest,
    _is_infrastructure_service,
    check,
)

PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"


# ════════════════════════════════════════════════════════════════
# A. 生产 compose 文件存在性
# ════════════════════════════════════════════════════════════════


class TestProdComposeFile:
    """R70 Wave 4 A: 生产 compose 文件存在性。"""

    def test_prod_compose_exists(self):
        """docker-compose.prod.yml 文件必须存在。"""
        assert PROD_COMPOSE.is_file(), (
            f"生产 compose 文件不存在: {PROD_COMPOSE} — "
            f"R70 Wave 4 要求拆分 dev/production compose"
        )

    def test_dev_compose_exists(self):
        """docker-compose.yml(开发版本)必须存在。"""
        assert DEFAULT_COMPOSE.is_file(), (
            f"开发 compose 文件不存在: {DEFAULT_COMPOSE}"
        )


# ════════════════════════════════════════════════════════════════
# B. 不可变性校验脚本(端到端)
# ════════════════════════════════════════════════════════════════


class TestImmutableCheckScript:
    """R70 Wave 4 B: check_compose_static_rules.py --immutable 端到端。"""

    def test_prod_compose_passes_immutable_check(self):
        """生产 compose 必须通过不可变性校验。"""
        exit_code, violations = check(PROD_COMPOSE, immutable=True)
        assert exit_code == 0, (
            f"生产 compose 未通过不可变性校验, 违规:\n"
            + "\n".join(str(v) for v in violations)
        )

    def test_dev_compose_fails_immutable_check(self):
        """开发 compose 必须未通过不可变性校验(有 build:)。"""
        exit_code, violations = check(DEFAULT_COMPOSE, immutable=True)
        assert exit_code == 1, (
            "开发 compose 不应通过不可变性校验(允许 build:)"
        )
        # 至少有 build: 违规
        build_violations = [v for v in violations if v.rule == "immutable_no_build"]
        assert len(build_violations) > 0, (
            "开发 compose 应有 build: 违规(不可变性模式下)"
        )

    def test_dev_compose_passes_static_check(self):
        """开发 compose 必须通过静态规则校验(不带 --immutable)。"""
        exit_code, violations = check(DEFAULT_COMPOSE, immutable=False)
        assert exit_code == 0, (
            f"开发 compose 未通过静态规则校验, 违规:\n"
            + "\n".join(str(v) for v in violations)
        )


# ════════════════════════════════════════════════════════════════
# C. 生产 compose 无 build: 字段
# ════════════════════════════════════════════════════════════════


class TestNoBuild:
    """R70 Wave 4 C: 生产 compose 无 build: 字段。"""

    def test_prod_compose_no_build(self):
        """生产 compose 所有应用服务不能有 build: 字段。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 未安装")

        with PROD_COMPOSE.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        services = data.get("services", {})
        for name, svc in services.items():
            assert "build" not in svc, (
                f"服务 {name} 有 build: 字段 — 生产 compose 禁止 build"
            )

    def test_check_no_build_detects_violation(self):
        """_check_no_build 检测到 build: 字段。"""
        svc = {"build": "."}
        violations = _check_no_build("test_svc", svc)
        assert len(violations) == 1
        assert violations[0].rule == "immutable_no_build"

    def test_check_no_build_passes_without_build(self):
        """_check_no_build 无 build: 时通过。"""
        svc = {"image": "test:latest"}
        violations = _check_no_build("test_svc", svc)
        assert len(violations) == 0


# ════════════════════════════════════════════════════════════════
# D. 生产 compose 应用服务使用 ${TGJIEMA_IMAGE} 变量
# ════════════════════════════════════════════════════════════════


class TestUnifiedImageDigest:
    """R70 Wave 4 D: 所有应用服务使用同一 ${TGJIEMA_IMAGE} 变量。"""

    def test_prod_compose_app_services_use_variable(self):
        """生产 compose 所有应用服务的 image 必须引用 ${TGJIEMA_IMAGE}。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 未安装")

        with PROD_COMPOSE.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        services = data.get("services", {})
        for name, svc in services.items():
            if _is_infrastructure_service(name, svc):
                continue  # 基础设施服务豁免
            image = str(svc.get("image", ""))
            assert PRODUCTION_IMAGE_VARIABLE in image, (
                f"服务 {name} 的 image '{image}' 未引用 ${{TGJIEMA_IMAGE}} 变量"
            )

    def test_check_unified_digest_passes_with_variable(self):
        """_check_unified_image_digest 通过 ${TGJIEMA_IMAGE} 变量引用。"""
        svc = {"image": "${TGJIEMA_IMAGE:?must be set}"}
        violations = _check_unified_image_digest("test_svc", svc, is_infra=False)
        assert len(violations) == 0

    def test_check_unified_digest_detects_literal_tag(self):
        """_check_unified_image_digest 检测到字面 tag(非变量引用)。"""
        svc = {"image": "ghcr.io/maxiuquan/tgjiema:v1.0.0"}
        violations = _check_unified_image_digest("test_svc", svc, is_infra=False)
        assert len(violations) == 1
        assert violations[0].rule == "immutable_unified_digest"

    def test_check_unified_digest_infra_exempt(self):
        """_check_unified_image_digest 基础设施服务豁免。"""
        svc = {"image": "redis:7-alpine"}
        violations = _check_unified_image_digest("redis", svc, is_infra=True)
        assert len(violations) == 0


# ════════════════════════════════════════════════════════════════
# E. 生产 compose 无目录级代码 bind mount
# ════════════════════════════════════════════════════════════════


class TestNoCodeBindMount:
    """R70 Wave 4 E: 生产 compose 无目录级代码 bind mount。"""

    def test_prod_compose_no_code_mount(self):
        """生产 compose 不能有目录级代码 bind mount。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 未安装")

        with PROD_COMPOSE.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        services = data.get("services", {})
        for name, svc in services.items():
            volumes = svc.get("volumes", []) or []
            for vol in volumes:
                if not isinstance(vol, str):
                    continue
                parts = vol.split(":")
                if len(parts) < 2:
                    continue
                source = parts[0]
                target = parts[1]
                # 文件级挂载的 target/source 包含扩展名
                target_has_ext = "." in target.split("/")[-1]
                source_has_ext = "." in source.split("/")[-1]
                for forbidden in FORBIDDEN_CODE_BIND_MOUNTS:
                    forbidden_source = forbidden.split(":")[0]
                    if source == forbidden_source and not (
                        target_has_ext or source_has_ext
                    ):
                        pytest.fail(
                            f"服务 {name} 挂载目录级代码源: {vol}"
                        )

    def test_check_no_code_bind_mount_detects_directory_mount(self):
        """_check_no_code_bind_mount 检测到目录级挂载。"""
        svc = {"volumes": ["./config:/app/config"]}
        violations = _check_no_code_bind_mount("test_svc", svc)
        assert len(violations) == 1
        assert violations[0].rule == "immutable_no_code_mount"

    def test_check_no_code_bind_mount_allows_file_mount(self):
        """_check_no_code_bind_mount 允许文件级挂载。"""
        svc = {"volumes": [
            "./config/groups.yaml:/app/config/groups.yaml:ro",
            "./data:/app/data",
            "./logs:/app/logs",
        ]}
        violations = _check_no_code_bind_mount("test_svc", svc)
        assert len(violations) == 0

    def test_check_no_code_bind_mount_allows_data_dir(self):
        """_check_no_code_bind_mount 允许数据目录挂载。"""
        svc = {"volumes": [
            "./data:/app/data",
            "./logs:/app/logs",
        ]}
        violations = _check_no_code_bind_mount("test_svc", svc)
        assert len(violations) == 0

    def test_check_no_code_bind_mount_detects_services_dir(self):
        """_check_no_code_bind_mount 检测到 ./services:/app/services。"""
        svc = {"volumes": ["./services:/app/services"]}
        violations = _check_no_code_bind_mount("test_svc", svc)
        assert len(violations) == 1


# ════════════════════════════════════════════════════════════════
# F. 生产 compose APP_ENV=production
# ════════════════════════════════════════════════════════════════


class TestAppEnvProduction:
    """R70 Wave 4 F: 生产 compose 硬编码 APP_ENV=production。"""

    def test_prod_compose_app_env(self):
        """生产 compose 所有应用服务必须 APP_ENV=production。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 未安装")

        with PROD_COMPOSE.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        services = data.get("services", {})
        for name, svc in services.items():
            if _is_infrastructure_service(name, svc):
                continue
            env = svc.get("environment", [])
            if isinstance(env, list):
                env_vars = {
                    item.split("=", 1)[0]: item.split("=", 1)[1] if "=" in item else ""
                    for item in env if isinstance(item, str)
                }
            elif isinstance(env, dict):
                env_vars = dict(env)
            else:
                env_vars = {}
            assert env_vars.get("APP_ENV") == "production", (
                f"服务 {name} APP_ENV={env_vars.get('APP_ENV', '(missing)')} "
                f"必须为 production"
            )

    def test_check_app_env_passes_with_production(self):
        """_check_app_env_production 通过 APP_ENV=production。"""
        svc = {"environment": ["APP_ENV=production"]}
        violations = _check_app_env_production("test_svc", svc)
        assert len(violations) == 0

    def test_check_app_env_detects_development(self):
        """_check_app_env_production 检测到 APP_ENV=development。"""
        svc = {"environment": ["APP_ENV=development"]}
        violations = _check_app_env_production("test_svc", svc)
        assert len(violations) == 1
        assert violations[0].rule == "immutable_app_env"

    def test_check_app_env_detects_missing(self):
        """_check_app_env_production 检测到 APP_ENV 缺失。"""
        svc = {"environment": ["SERVICE_ROLE=up"]}
        violations = _check_app_env_production("test_svc", svc)
        assert len(violations) == 1


# ════════════════════════════════════════════════════════════════
# G. 生产 compose 逃生舱变量显式 unset
# ════════════════════════════════════════════════════════════════


class TestEscapeHatchesUnset:
    """R70 Wave 4 G: 生产 compose 显式 unset 所有测试逃生舱变量。"""

    REQUIRED_UNSET = [
        "I18N_ALLOW_FALLBACK",
        "ALLOW_LEGACY_RESTORE",
        "TEST_ONLY",
        "DEV_ONLY",
        "BYPASS",
        "SKIP_VERIFY",
        "SKIP_VALIDATION",
        "ALLOW_INSECURE",
    ]

    def test_prod_compose_escape_hatches_unset(self):
        """生产 compose 所有应用服务必须显式 unset 逃生舱变量。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 未安装")

        with PROD_COMPOSE.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        services = data.get("services", {})
        for name, svc in services.items():
            if _is_infrastructure_service(name, svc):
                continue
            env = svc.get("environment", [])
            if isinstance(env, list):
                env_vars = {
                    item.split("=", 1)[0]: item.split("=", 1)[1] if "=" in item else ""
                    for item in env if isinstance(item, str)
                }
            elif isinstance(env, dict):
                env_vars = dict(env)
            else:
                env_vars = {}
            for var in self.REQUIRED_UNSET:
                assert var in env_vars, (
                    f"服务 {name} 缺少 {var}= — 必须显式 unset 测试逃生舱变量"
                )
                assert env_vars[var] == "", (
                    f"服务 {name} 的 {var}={env_vars[var]} — "
                    f"生产 compose 必须设为空字符串"
                )

    def test_check_escape_hatches_passes_when_unset(self):
        """_check_escape_hatches_unset 通过全部 unset。"""
        env = [f"{var}=" for var in self.REQUIRED_UNSET]
        svc = {"environment": env}
        violations = _check_escape_hatches_unset("test_svc", svc)
        assert len(violations) == 0

    def test_check_escape_hatches_detects_missing(self):
        """_check_escape_hatches_unset 检测到缺失的变量。"""
        svc = {"environment": []}  # 完全缺失
        violations = _check_escape_hatches_unset("test_svc", svc)
        assert len(violations) == len(self.REQUIRED_UNSET)


# ════════════════════════════════════════════════════════════════
# H. 开发 compose 允许 build:(回归保护)
# ════════════════════════════════════════════════════════════════


class TestDevComposeRegression:
    """R70 Wave 4 H: 开发 compose 回归保护(允许 build:)。"""

    def test_dev_compose_has_build(self):
        """开发 compose 必须保留 build:(用于本地开发)。"""
        try:
            import yaml
        except ImportError:
            pytest.skip("PyYAML 未安装")

        with DEFAULT_COMPOSE.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        services = data.get("services", {})
        # 至少有一个服务有 build:(开发版本允许)
        build_services = [
            name for name, svc in services.items() if "build" in svc
        ]
        assert len(build_services) > 0, (
            "开发 compose 应至少有一个服务使用 build:(用于本地开发)"
        )

    def test_dev_compose_passes_static_check(self):
        """开发 compose 必须通过静态规则校验(不带 --immutable)。"""
        exit_code, _ = check(DEFAULT_COMPOSE, immutable=False)
        assert exit_code == 0


# ════════════════════════════════════════════════════════════════
# I. 校验函数单元测试(模拟违规场景)
# ════════════════════════════════════════════════════════════════


class TestImmutableCheckFunctions:
    """R70 Wave 4 I: 不可变性校验函数单元测试。"""

    # --- _is_infrastructure_service ---

    def test_is_infrastructure_redis(self):
        """_is_infrastructure_service 识别 redis 为基础设施。"""
        assert _is_infrastructure_service("redis", {"image": "redis:7-alpine"})

    def test_is_infrastructure_redis_acl_init(self):
        """_is_infrastructure_service 识别 redis-acl-init 为基础设施。"""
        assert _is_infrastructure_service("redis-acl-init", {"image": "redis:7-alpine"})

    def test_is_infrastructure_app_service(self):
        """_is_infrastructure_service 识别应用服务为非基础设施。"""
        assert not _is_infrastructure_service("up", {
            "image": "${TGJIEMA_IMAGE:?must be set}"
        })

    def test_is_infrastructure_by_image_prefix(self):
        """_is_infrastructure_service 通过镜像前缀识别。"""
        assert _is_infrastructure_service("custom-redis", {
            "image": "redis:7-alpine"
        })
        assert not _is_infrastructure_service("custom-app", {
            "image": "ghcr.io/maxiuquan/tgjiema@sha256:abc"
        })

    # --- _check_has_image ---

    def test_check_has_image_passes(self):
        """_check_has_image 通过有 image 的服务。"""
        svc = {"image": "${TGJIEMA_IMAGE:?must be set}"}
        violations = _check_has_image("test_svc", svc)
        assert len(violations) == 0

    def test_check_has_image_detects_missing(self):
        """_check_has_image 检测到缺失 image。"""
        svc = {"build": "."}
        violations = _check_has_image("test_svc", svc)
        assert len(violations) == 1
        assert violations[0].rule == "immutable_requires_image"

    # --- _check_no_mutable_tag ---

    def test_check_no_mutable_tag_passes_with_digest(self):
        """_check_no_mutable_tag 通过带 digest 的 image。"""
        svc = {"image": "ghcr.io/maxiuquan/tgjiema@sha256:abc123"}
        violations = _check_no_mutable_tag("test_svc", svc)
        assert len(violations) == 0

    def test_check_no_mutable_tag_passes_with_variable(self):
        """_check_no_mutable_tag 通过 ${TGJIEMA_IMAGE} 变量。"""
        svc = {"image": "${TGJIEMA_IMAGE:?must be set}"}
        violations = _check_no_mutable_tag("test_svc", svc)
        assert len(violations) == 0

    def test_check_no_mutable_tag_passes_with_infra(self):
        """_check_no_mutable_tag 通过基础设施镜像。"""
        svc = {"image": "redis:7-alpine"}
        violations = _check_no_mutable_tag("test_svc", svc)
        assert len(violations) == 0

    def test_check_no_mutable_tag_detects_latest(self):
        """_check_no_mutable_tag 检测到 latest。"""
        svc = {"image": "ghcr.io/maxiuquan/tgjiema:latest"}
        violations = _check_no_mutable_tag("test_svc", svc)
        assert len(violations) == 1
        assert violations[0].rule == "immutable_no_mutable_tag"

    def test_check_no_mutable_tag_detects_master(self):
        """_check_no_mutable_tag 检测到 master。"""
        svc = {"image": "ghcr.io/maxiuquan/tgjiema:master"}
        violations = _check_no_mutable_tag("test_svc", svc)
        assert len(violations) == 1

    def test_check_no_mutable_tag_detects_staging(self):
        """_check_no_mutable_tag 检测到 staging。"""
        svc = {"image": "ghcr.io/maxiuquan/tgjiema:staging"}
        violations = _check_no_mutable_tag("test_svc", svc)
        assert len(violations) == 1

    def test_check_no_mutable_tag_detects_no_tag(self):
        """_check_no_mutable_tag 检测到无 tag(隐式 latest)。"""
        svc = {"image": "ghcr.io/maxiuquan/tgjiema"}
        violations = _check_no_mutable_tag("test_svc", svc)
        assert len(violations) == 1


# ════════════════════════════════════════════════════════════════
# J. 常量完整性测试
# ════════════════════════════════════════════════════════════════


class TestImmutableConstants:
    """R70 Wave 4 J: 常量完整性测试。"""

    def test_forbidden_code_bind_mounts_not_empty(self):
        """FORBIDDEN_CODE_BIND_MOUNTS 不能为空。"""
        assert len(FORBIDDEN_CODE_BIND_MOUNTS) > 0
        assert "./config:/app/config" in FORBIDDEN_CODE_BIND_MOUNTS
        assert "./services:/app/services" in FORBIDDEN_CODE_BIND_MOUNTS
        assert "./bots:/app/bots" in FORBIDDEN_CODE_BIND_MOUNTS

    def test_forbidden_mutable_tags_not_empty(self):
        """FORBIDDEN_MUTABLE_TAGS 不能为空。"""
        assert len(FORBIDDEN_MUTABLE_TAGS) > 0
        assert "latest" in FORBIDDEN_MUTABLE_TAGS
        assert "master" in FORBIDDEN_MUTABLE_TAGS
        assert "staging" in FORBIDDEN_MUTABLE_TAGS

    def test_infrastructure_services_not_empty(self):
        """INFRASTRUCTURE_SERVICES 不能为空。"""
        assert len(INFRASTRUCTURE_SERVICES) > 0
        assert "redis" in INFRASTRUCTURE_SERVICES
        assert "redis-acl-init" in INFRASTRUCTURE_SERVICES

    def test_production_image_variable(self):
        """PRODUCTION_IMAGE_VARIABLE 正确。"""
        assert PRODUCTION_IMAGE_VARIABLE == "${TGJIEMA_IMAGE"
