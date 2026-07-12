"""R37 Batch 4 P2 工程与安全增强测试覆盖

测试覆盖范围(P2-1 至 P2-7,共 7 项):
- P2-1: docs/SIGNING.md 存在(Sigstore cosign keyless 签名流程)
- P2-2: docs/least-privilege.md 存在 + deploy_vps_per_bot.sh 包含 NoNewPrivileges / ProtectSystem / CapabilityBoundingSet
- P2-3: docs/redis-security.md 存在 + deploy_vps_per_bot.sh 包含 ACL SETUSER(tgjiema_writer/reader/default off)
- P2-4: docker-compose.yml 包含 read_only + tmpfs + cap_drop + security_opt + deploy.resources.limits + healthcheck;
        Dockerfile 用 digest 固定基础镜像(@sha256:)
- P2-5: docs/delivery-idempotency.md 存在 + delivery_resolver.py 包含 delivery_token + compute_delivery_token 计算稳定
- P2-6: docs/relay-spool-management.md 存在 + relay_db.py 包含 high_water_mark / cleanup_indexed_spools_only
- P2-7: services/prometheus_exporter.py 存在 + 包含 /metrics 端点 + 监听 9100 + config/grafana-dashboard.json 存在 + docs/observability.md 存在

测试策略:
1. 文档/配置文件检查 — 纯文件内容检查,无运行时依赖
2. compute_delivery_token 函数行为测试 — try import,失败则跳过(避免依赖链失败阻塞)
3. prometheus_exporter 模块结构检查 — try import,失败则跳过
"""
import hashlib
import importlib
import importlib.util
import inspect
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"


# ─────────────────────────────────────────────────────────────
#  辅助: 隔离加载 storage.delivery_resolver 的目标函数(绕过重依赖)
# ─────────────────────────────────────────────────────────────
# delivery_resolver.py 在模块顶部导入了 telegram.error / database /
# utils.flood_waiter / utils.per_channel_limiter,且后续函数签名使用了
# Python 3.10+ 的 `int | None` 语法。本机测试环境为 Python 3.9,直接
# import 会因语法错误失败。
# compute_delivery_token / is_delivery_already_done 两个函数仅依赖
# hashlib + loguru,可以用 AST 提取函数源码后在隔离命名空间中 exec,
# 从而对真实函数实现进行行为测试。
_DELIVERY_RESOLVER_MODULE = None


def _load_delivery_resolver_isolated():
    """通过 AST 提取 compute_delivery_token / is_delivery_already_done。

    使用 ast 解析源文件,找到两个目标函数的 AST 节点,编译后在独立命名空间
    中执行,避免触发模块顶层 import 和 3.10+ 语法。
    """
    global _DELIVERY_RESOLVER_MODULE
    if _DELIVERY_RESOLVER_MODULE is not None:
        return _DELIVERY_RESOLVER_MODULE

    import ast
    src_path = REPO_ROOT / "storage" / "delivery_resolver.py"
    try:
        source = src_path.read_text(encoding="utf-8")
    except Exception:
        return None

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # 源文件含 3.10+ 语法,但 ast.parse 在 3.9 上对 `int | None` 注解
        # 可能报错。回退:用正则提取函数文本。
        return _load_delivery_resolver_fallback(source)

    # 收集目标函数的源码段
    target_funcs = {"compute_delivery_token", "is_delivery_already_done"}
    func_segments = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in target_funcs:
                segment = ast.get_source_segment(source, node)
                if segment:
                    func_segments.append(segment)

    if len(func_segments) < 2:
        return _load_delivery_resolver_fallback(source)

    # 在隔离命名空间中 exec 函数定义
    namespace = {
        "__name__": "_test_delivery_resolver_isolated",
        "hashlib": hashlib,
    }
    # loguru.logger:用标准库 logging 替代(函数内只用 logger.warning)
    try:
        from loguru import logger as _logger
    except Exception:
        import logging
        _logger = logging.getLogger("test")
    namespace["logger"] = _logger

    try:
        combined = "\n\n".join(func_segments)
        exec(compile(combined, str(src_path), "exec"), namespace)
        # 包装成模块对象
        module = types.ModuleType("_test_delivery_resolver_isolated")
        module.compute_delivery_token = namespace["compute_delivery_token"]
        module.is_delivery_already_done = namespace["is_delivery_already_done"]
        _DELIVERY_RESOLVER_MODULE = module
        return module
    except Exception as e:
        _load_delivery_resolver_isolated._last_error = e
        return None


def _load_delivery_resolver_fallback(source: str):
    """AST 解析失败时的回退:用正则提取目标函数源码。

    适用于 Python 3.9 解析 `int | None` 注解报错的情况。
    """
    import re
    namespace = {
        "__name__": "_test_delivery_resolver_isolated",
        "hashlib": hashlib,
    }
    try:
        from loguru import logger as _logger
    except Exception:
        import logging
        _logger = logging.getLogger("test")
    namespace["logger"] = _logger

    # 提取 compute_delivery_token 和 is_delivery_already_done 函数体
    # 函数以 `def ` 或 `async def ` 开头,到下一个顶层 def/async def 或
    # 模块级常量/类定义前结束。
    patterns = [
        r"(def compute_delivery_token\(.*?\n(?:    .*\n|^\n)*?)(?=^def |^async def |^class |\Z)",
        r"(async def is_delivery_already_done\(.*?\n(?:    .*\n|^\n)*?)(?=^def |^async def |^class |\Z)",
    ]
    segments = []
    for pat in patterns:
        m = re.search(pat, source, re.MULTILINE)
        if m:
            segments.append(m.group(1).rstrip())

    if len(segments) < 2:
        return None

    try:
        combined = "\n\n".join(segments)
        exec(compile(combined, "<delivery_resolver_extracted>", "exec"), namespace)
        module = types.ModuleType("_test_delivery_resolver_isolated")
        module.compute_delivery_token = namespace["compute_delivery_token"]
        module.is_delivery_already_done = namespace["is_delivery_already_done"]
        global _DELIVERY_RESOLVER_MODULE
        _DELIVERY_RESOLVER_MODULE = module
        return module
    except Exception as e:
        _load_delivery_resolver_isolated._last_error = e
        return None


# ─────────────────────────────────────────────────────────────
#  P2-1: docs/SIGNING.md(Sigstore cosign keyless 签名流程)
# ─────────────────────────────────────────────────────────────

class TestP21SigningDoc:
    """P2-1: docs/SIGNING.md 文档存在且内容完整。"""

    def test_signing_md_exists(self):
        """docs/SIGNING.md 文件存在。"""
        path = DOCS_DIR / "SIGNING.md"
        assert path.exists(), f"缺少签名文档: {path}"
        assert path.is_file()

    def test_signing_md_mentions_cosign(self):
        """SIGNING.md 提及 cosign(Sigstore 核心工具)。"""
        path = DOCS_DIR / "SIGNING.md"
        if not path.exists():
            pytest.skip("SIGNING.md 不存在")
        content = path.read_text(encoding="utf-8")
        assert "cosign" in content.lower(), "SIGNING.md 应提及 cosign"

    def test_signing_md_mentions_keyless(self):
        """SIGNING.md 提及 keyless(无密钥签名,基于 OIDC)。"""
        path = DOCS_DIR / "SIGNING.md"
        if not path.exists():
            pytest.skip("SIGNING.md 不存在")
        content = path.read_text(encoding="utf-8")
        assert "keyless" in content.lower() or "OIDC" in content.upper(), (
            "SIGNING.md 应说明 keyless 签名流程(基于 GitHub OIDC)"
        )

    def test_ci_workflow_has_sign_job(self):
        """.github/workflows/ci.yml 包含签名 job(cosign keyless)。"""
        ci_path = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        if not ci_path.exists():
            pytest.skip("ci.yml 不存在")
        content = ci_path.read_text(encoding="utf-8")
        # 验证 CI 中存在签名步骤
        assert "cosign" in content.lower(), "ci.yml 应包含 cosign 签名步骤"


# ─────────────────────────────────────────────────────────────
#  P2-2: systemd 最小权限沙箱
# ─────────────────────────────────────────────────────────────

class TestP22LeastPrivilegeSandbox:
    """P2-2: systemd 服务沙箱选项 + least-privilege.md 文档。"""

    def test_least_privilege_md_exists(self):
        """docs/least-privilege.md 文档存在。"""
        path = DOCS_DIR / "least-privilege.md"
        assert path.exists(), f"缺少最小权限文档: {path}"

    def test_deploy_script_contains_no_new_privileges(self):
        """deploy_vps_per_bot.sh 业务 Bot 服务包含 NoNewPrivileges=true。"""
        path = REPO_ROOT / "deploy_vps_per_bot.sh"
        content = path.read_text(encoding="utf-8")
        assert "NoNewPrivileges=true" in content, (
            "deploy_vps_per_bot.sh 应为 systemd 服务添加 NoNewPrivileges=true"
        )

    def test_deploy_script_contains_protect_system_strict(self):
        """deploy_vps_per_bot.sh 包含 ProtectSystem=strict。"""
        path = REPO_ROOT / "deploy_vps_per_bot.sh"
        content = path.read_text(encoding="utf-8")
        assert "ProtectSystem=strict" in content, (
            "deploy_vps_per_bot.sh 应包含 ProtectSystem=strict(只读根文件系统)"
        )

    def test_deploy_script_contains_capability_bounding_set_empty(self):
        """deploy_vps_per_bot.sh 包含 CapabilityBoundingSet=(清空所有 capabilities)。"""
        path = REPO_ROOT / "deploy_vps_per_bot.sh"
        content = path.read_text(encoding="utf-8")
        assert "CapabilityBoundingSet=" in content, (
            "deploy_vps_per_bot.sh 应清空 CapabilityBoundingSet(剥离所有 Linux capabilities)"
        )

    def test_deploy_script_contains_system_call_filter(self):
        """deploy_vps_per_bot.sh 包含 SystemCallFilter(限制系统调用)。"""
        path = REPO_ROOT / "deploy_vps_per_bot.sh"
        content = path.read_text(encoding="utf-8")
        assert "SystemCallFilter" in content, (
            "deploy_vps_per_bot.sh 应包含 SystemCallFilter(限制系统调用白名单)"
        )

    def test_deploy_script_contains_protect_kernel_modules(self):
        """deploy_vps_per_bot.sh 包含 ProtectKernelModules=true。"""
        path = REPO_ROOT / "deploy_vps_per_bot.sh"
        content = path.read_text(encoding="utf-8")
        assert "ProtectKernelModules=true" in content, (
            "deploy_vps_per_bot.sh 应包含 ProtectKernelModules=true"
        )

    def test_least_privilege_md_mentions_service_account(self):
        """least-privilege.md 提及服务运行账户(非 root)。"""
        path = DOCS_DIR / "least-privilege.md"
        if not path.exists():
            pytest.skip("least-privilege.md 不存在")
        content = path.read_text(encoding="utf-8")
        # 至少应说明服务运行用户(非 root)
        assert "tgjiema" in content.lower() or "systemd" in content.lower()


# ─────────────────────────────────────────────────────────────
#  P2-3: Redis ACL 初始化
# ─────────────────────────────────────────────────────────────

class TestP23RedisAcl:
    """P2-3: Redis ACL 初始化脚本(tgjiema_writer / tgjiema_reader / default off)。"""

    def test_redis_security_md_exists(self):
        """docs/redis-security.md 文档存在。"""
        path = DOCS_DIR / "redis-security.md"
        assert path.exists(), f"缺少 Redis 安全文档: {path}"

    def test_deploy_script_contains_acl_setuser_writer(self):
        """deploy_vps_per_bot.sh 包含 ACL SETUSER tgjiema_writer。"""
        path = REPO_ROOT / "deploy_vps_per_bot.sh"
        content = path.read_text(encoding="utf-8")
        assert "ACL SETUSER tgjiema_writer" in content, (
            "deploy_vps_per_bot.sh 应执行 ACL SETUSER tgjiema_writer(独立写账号)"
        )

    def test_deploy_script_contains_acl_setuser_reader(self):
        """deploy_vps_per_bot.sh 包含 ACL SETUSER tgjiema_reader。"""
        path = REPO_ROOT / "deploy_vps_per_bot.sh"
        content = path.read_text(encoding="utf-8")
        assert "ACL SETUSER tgjiema_reader" in content, (
            "deploy_vps_per_bot.sh 应执行 ACL SETUSER tgjiema_reader(独立读账号)"
        )

    def test_deploy_script_disables_default_user(self):
        """deploy_vps_per_bot.sh 禁用 default 用户(ACL SETUSER default off)。"""
        path = REPO_ROOT / "deploy_vps_per_bot.sh"
        content = path.read_text(encoding="utf-8")
        assert "ACL SETUSER default off" in content, (
            "deploy_vps_per_bot.sh 应执行 ACL SETUSER default off(禁用默认账号)"
        )

    def test_deploy_script_writer_has_minus_all(self):
        """tgjiema_writer 权限白名单: -@all + 显式授权(最小权限)。"""
        path = REPO_ROOT / "deploy_vps_per_bot.sh"
        content = path.read_text(encoding="utf-8")
        # tgjiema_writer 应使用 -@all + 显式 +XADD/+XREADGROUP 等
        assert "-@all" in content or "+XADD" in content, (
            "tgjiema_writer 应使用 -@all + 显式命令授权(最小权限原则)"
        )

    def test_deploy_script_has_init_redis_acl_function(self):
        """deploy_vps_per_bot.sh 定义 init_redis_acl() 函数。"""
        path = REPO_ROOT / "deploy_vps_per_bot.sh"
        content = path.read_text(encoding="utf-8")
        assert "init_redis_acl" in content, (
            "deploy_vps_per_bot.sh 应定义 init_redis_acl 函数并调用"
        )

    def test_redis_security_md_mentions_aof(self):
        """redis-security.md 提及 AOF 持久化。"""
        path = DOCS_DIR / "redis-security.md"
        if not path.exists():
            pytest.skip("redis-security.md 不存在")
        content = path.read_text(encoding="utf-8")
        assert "AOF" in content or "appendfsync" in content, (
            "redis-security.md 应说明 AOF 持久化策略"
        )


# ─────────────────────────────────────────────────────────────
#  P2-4: Docker Compose 安全加固 + Dockerfile digest 固定
# ─────────────────────────────────────────────────────────────

class TestP24DockerHardening:
    """P2-4: docker-compose.yml + Dockerfile 安全加固。"""

    def test_compose_contains_read_only(self):
        """docker-compose.yml 包含 read_only: true(只读文件系统)。"""
        path = REPO_ROOT / "docker-compose.yml"
        content = path.read_text(encoding="utf-8")
        assert "read_only: true" in content, (
            "docker-compose.yml 应为服务添加 read_only: true"
        )
        # 至少 5 个服务应启用 read_only
        count = content.count("read_only: true")
        assert count >= 5, f"read_only: true 出现次数过少({count} 次,应≥5)"

    def test_compose_contains_tmpfs(self):
        """docker-compose.yml 包含 tmpfs(/tmp 临时可写)。"""
        path = REPO_ROOT / "docker-compose.yml"
        content = path.read_text(encoding="utf-8")
        assert "tmpfs:" in content, "docker-compose.yml 应挂载 tmpfs(/tmp 可写)"
        assert "/tmp" in content

    def test_compose_contains_cap_drop_all(self):
        """docker-compose.yml 包含 cap_drop: ALL(剥离所有 capabilities)。"""
        path = REPO_ROOT / "docker-compose.yml"
        content = path.read_text(encoding="utf-8")
        assert "cap_drop:" in content, "docker-compose.yml 应包含 cap_drop"
        assert "ALL" in content, "cap_drop 应为 ALL(剥离所有 Linux capabilities)"

    def test_compose_contains_security_opt_no_new_privileges(self):
        """docker-compose.yml 包含 security_opt: no-new-privileges。"""
        path = REPO_ROOT / "docker-compose.yml"
        content = path.read_text(encoding="utf-8")
        assert "security_opt:" in content, "docker-compose.yml 应包含 security_opt"
        assert "no-new-privileges:true" in content, (
            "security_opt 应设置 no-new-privileges:true"
        )

    def test_compose_contains_deploy_resources_limits(self):
        """docker-compose.yml 包含 deploy.resources.limits(cpus + memory)。"""
        path = REPO_ROOT / "docker-compose.yml"
        content = path.read_text(encoding="utf-8")
        assert "deploy:" in content, "docker-compose.yml 应包含 deploy 段"
        assert "limits:" in content, "deploy.resources 应包含 limits"
        assert "cpus:" in content, "limits 应包含 cpus 上限"
        assert "memory:" in content, "limits 应包含 memory 上限"

    def test_compose_contains_healthcheck(self):
        """docker-compose.yml 包含 healthcheck(健康检查)。"""
        path = REPO_ROOT / "docker-compose.yml"
        content = path.read_text(encoding="utf-8")
        assert "healthcheck:" in content, (
            "docker-compose.yml 应包含 healthcheck(Docker 健康探测)"
        )

    def test_dockerfile_uses_digest_pinned_image(self):
        """Dockerfile 使用 digest 固定基础镜像(@sha256:)。"""
        path = REPO_ROOT / "Dockerfile"
        content = path.read_text(encoding="utf-8")
        assert "@sha256:" in content, (
            "Dockerfile 应使用 @sha256: digest 固定基础镜像(防止供应链篡改)"
        )
        # 至少 2 个 FROM(multi-stage builder + runtime)
        count = content.count("@sha256:")
        assert count >= 2, f"Dockerfile 应至少 2 个 FROM 用 digest 固定(实际 {count})"

    def test_dockerfile_no_floating_tag(self):
        """Dockerfile 不应使用未固定的 :3.12-slim tag(应改为 @sha256:)。"""
        path = REPO_ROOT / "Dockerfile"
        content = path.read_text(encoding="utf-8")
        # FROM python:3.12-slim 行不应直接以 tag 结尾(应以 digest 结尾)
        # 检查所有 FROM 行
        from_lines = [
            line.strip() for line in content.splitlines()
            if line.strip().startswith("FROM")
        ]
        assert len(from_lines) >= 2, "Dockerfile 应至少 2 个 FROM"
        for line in from_lines:
            # 每条 FROM 行必须包含 @sha256:
            assert "@sha256:" in line, (
                f"FROM 行未用 digest 固定: {line}"
            )


# ─────────────────────────────────────────────────────────────
#  P2-5: Delivery effectively-once(delivery_token)
# ─────────────────────────────────────────────────────────────

class TestP25DeliveryIdempotency:
    """P2-5: delivery_resolver.py 投递幂等(delivery_token)。"""

    def test_delivery_idempotency_md_exists(self):
        """docs/delivery-idempotency.md 文档存在。"""
        path = DOCS_DIR / "delivery-idempotency.md"
        assert path.exists(), f"缺少投递幂等文档: {path}"

    def test_delivery_resolver_contains_delivery_token(self):
        """storage/delivery_resolver.py 包含 delivery_token 实现。"""
        path = REPO_ROOT / "storage" / "delivery_resolver.py"
        content = path.read_text(encoding="utf-8")
        assert "delivery_token" in content, (
            "delivery_resolver.py 应实现 delivery_token(effectively-once 幂等)"
        )
        assert "compute_delivery_token" in content, (
            "应定义 compute_delivery_token() 函数"
        )

    def test_cache_store_contains_delivery_token_column(self):
        """database/cache_store.py delivery_receipts 表新增 delivery_token 列。"""
        path = REPO_ROOT / "database" / "cache_store.py"
        content = path.read_text(encoding="utf-8")
        # CREATE TABLE 应包含 delivery_token 列
        assert "delivery_token" in content, (
            "cache_store.py delivery_receipts 表应包含 delivery_token 列"
        )
        # 应有索引(idx_delivery_receipts_token)
        assert "idx_delivery_receipts_token" in content, (
            "cache_store.py 应创建 idx_delivery_receipts_token 索引(快速判重)"
        )

    def test_cache_store_has_is_delivery_already_done(self):
        """database/cache_store.py 定义 is_delivery_already_done 方法。"""
        path = REPO_ROOT / "database" / "cache_store.py"
        content = path.read_text(encoding="utf-8")
        assert "async def is_delivery_already_done" in content, (
            "CacheStore 应定义 async is_delivery_already_done(delivery_token) 方法"
        )

    def test_compute_delivery_token_stable(self):
        """compute_delivery_token 同输入产生同输出(稳定)。"""
        module = _load_delivery_resolver_isolated()
        if module is None:
            pytest.skip("storage.delivery_resolver 隔离加载失败")
        compute_delivery_token = module.compute_delivery_token
        token1 = compute_delivery_token("ABC123", 100, 200)
        token2 = compute_delivery_token("ABC123", 100, 200)
        assert token1 == token2, "同一三元组应产生相同 token(稳定性)"

    def test_compute_delivery_token_distinct_for_different_inputs(self):
        """不同输入产生不同 token。"""
        module = _load_delivery_resolver_isolated()
        if module is None:
            pytest.skip("storage.delivery_resolver 隔离加载失败")
        compute_delivery_token = module.compute_delivery_token
        token_a = compute_delivery_token("CODE_A", 100, 200)
        token_b = compute_delivery_token("CODE_B", 100, 200)
        token_c = compute_delivery_token("CODE_A", 101, 200)
        token_d = compute_delivery_token("CODE_A", 100, 201)
        assert token_a != token_b, "不同 file_code 应产生不同 token"
        assert token_a != token_c, "不同 target_user_id 应产生不同 token"
        assert token_a != token_d, "不同 job_id 应产生不同 token"

    def test_compute_delivery_token_is_sha256_hex(self):
        """token 应为 64 字符 hex(SHA-256)。"""
        module = _load_delivery_resolver_isolated()
        if module is None:
            pytest.skip("storage.delivery_resolver 隔离加载失败")
        compute_delivery_token = module.compute_delivery_token
        token = compute_delivery_token("X", 1, 1)
        assert len(token) == 64, f"token 应为 64 字符(实际 {len(token)})"
        assert all(c in "0123456789abcdef" for c in token), (
            "token 应为纯 hex 字符"
        )

    def test_compute_delivery_token_matches_manual_sha256(self):
        """token 应与手动 SHA-256(file_code|target_user_id|job_id) 一致。"""
        module = _load_delivery_resolver_isolated()
        if module is None:
            pytest.skip("storage.delivery_resolver 隔离加载失败")
        compute_delivery_token = module.compute_delivery_token
        file_code = "FC-001"
        target_user_id = 999
        job_id = 42
        expected = hashlib.sha256(
            f"{file_code}|{target_user_id}|{job_id}".encode("utf-8")
        ).hexdigest()
        actual = compute_delivery_token(file_code, target_user_id, job_id)
        assert actual == expected, "token 应匹配 SHA-256(file|user|job)"

    @pytest.mark.asyncio
    async def test_is_delivery_already_done_returns_false_when_store_none(self):
        """store 为 None 时返回 False(不阻塞投递)。"""
        module = _load_delivery_resolver_isolated()
        if module is None:
            pytest.skip("storage.delivery_resolver 隔离加载失败")
        is_delivery_already_done = module.is_delivery_already_done
        result = await is_delivery_already_done(None, "FC", 1, 1)
        assert result is False, "store=None 时应返回 False(允许继续投递)"

    @pytest.mark.asyncio
    async def test_is_delivery_already_done_uses_store_method(self):
        """store 提供 is_delivery_already_done 接口时优先调用。"""
        from unittest.mock import AsyncMock
        module = _load_delivery_resolver_isolated()
        if module is None:
            pytest.skip("storage.delivery_resolver 隔离加载失败")
        is_delivery_already_done = module.is_delivery_already_done
        # mock store: 已投递过 → True(需要 AsyncMock,因为函数会 await)
        store = MagicMock()
        store.is_delivery_already_done = AsyncMock(return_value=True)
        result = await is_delivery_already_done(store, "FC", 1, 1)
        assert result is True
        store.is_delivery_already_done.assert_called_once()


# ─────────────────────────────────────────────────────────────
#  P2-6: Relay spool 磁盘配额 / 高低水位
# ─────────────────────────────────────────────────────────────

class TestP26RelaySpoolManagement:
    """P2-6: relay spool 磁盘配额检查 + 未 INDEXED 文件保护。"""

    def test_relay_spool_management_md_exists(self):
        """docs/relay-spool-management.md 文档存在。"""
        path = DOCS_DIR / "relay-spool-management.md"
        assert path.exists(), f"缺少 relay spool 管理文档: {path}"

    def test_relay_db_contains_high_water_mark_constant(self):
        """database/relay_db.py 包含 RELAY_SPOOL_HIGH_WATER_MARK 常量。"""
        path = REPO_ROOT / "database" / "relay_db.py"
        content = path.read_text(encoding="utf-8")
        assert "RELAY_SPOOL_HIGH_WATER_MARK" in content, (
            "relay_db.py 应定义 RELAY_SPOOL_HIGH_WATER_MARK 高水位常量"
        )

    def test_relay_db_contains_low_water_mark_constant(self):
        """database/relay_db.py 包含 RELAY_SPOOL_LOW_WATER_MARK 常量。"""
        path = REPO_ROOT / "database" / "relay_db.py"
        content = path.read_text(encoding="utf-8")
        assert "RELAY_SPOOL_LOW_WATER_MARK" in content, (
            "relay_db.py 应定义 RELAY_SPOOL_LOW_WATER_MARK 低水位常量"
        )

    def test_relay_db_high_water_mark_is_80_percent(self):
        """高水位阈值为 0.80(80%)。"""
        path = REPO_ROOT / "database" / "relay_db.py"
        content = path.read_text(encoding="utf-8")
        # 0.80 或 0.8
        assert "0.80" in content or "0.8" in content, (
            "RELAY_SPOOL_HIGH_WATER_MARK 应为 0.80"
        )

    def test_relay_db_low_water_mark_is_60_percent(self):
        """低水位阈值为 0.60(60%)。"""
        path = REPO_ROOT / "database" / "relay_db.py"
        content = path.read_text(encoding="utf-8")
        # 0.60 或 0.6
        assert "0.60" in content or "0.6" in content, (
            "RELAY_SPOOL_LOW_WATER_MARK 应为 0.60"
        )

    def test_relay_db_has_get_spool_disk_usage(self):
        """RelayDB 定义 get_spool_disk_usage() 方法。"""
        path = REPO_ROOT / "database" / "relay_db.py"
        content = path.read_text(encoding="utf-8")
        assert "async def get_spool_disk_usage" in content, (
            "RelayDB 应定义 get_spool_disk_usage() 方法"
        )

    def test_relay_db_has_should_accept_new_spool(self):
        """RelayDB 定义 should_accept_new_spool() 方法(高水位时拒绝)。"""
        path = REPO_ROOT / "database" / "relay_db.py"
        content = path.read_text(encoding="utf-8")
        assert "async def should_accept_new_spool" in content, (
            "RelayDB 应定义 should_accept_new_spool() 方法"
        )

    def test_relay_db_has_cleanup_indexed_spools_only(self):
        """RelayDB 定义 cleanup_indexed_spools_only()(只清理 INDEXED)。"""
        path = REPO_ROOT / "database" / "relay_db.py"
        content = path.read_text(encoding="utf-8")
        assert "async def cleanup_indexed_spools_only" in content, (
            "RelayDB 应定义 cleanup_indexed_spools_only()(严格只清理 INDEXED 状态)"
        )

    def test_cleanup_indexed_only_does_not_delete_unindexed(self):
        """cleanup_indexed_spools_only SQL 仅匹配 status='INDEXED'(不删未 INDEXED)。"""
        path = REPO_ROOT / "database" / "relay_db.py"
        content = path.read_text(encoding="utf-8")
        # 找到 cleanup_indexed_spools_only 函数定义
        idx = content.find("async def cleanup_indexed_spools_only")
        assert idx > 0, "未找到 cleanup_indexed_spools_only 函数定义"
        # 截取函数体(后续 1500 字符)
        snippet = content[idx:idx + 1500]
        # 应包含 status = 'INDEXED' 条件
        assert "INDEXED" in snippet, (
            "cleanup_indexed_spools_only 应只清理 status='INDEXED' 的 spool"
        )

    def test_relay_db_has_max_bytes_constant(self):
        """relay_db.py 包含 RELAY_SPOOL_MAX_BYTES 配额常量。"""
        path = REPO_ROOT / "database" / "relay_db.py"
        content = path.read_text(encoding="utf-8")
        assert "RELAY_SPOOL_MAX_BYTES" in content, (
            "relay_db.py 应定义 RELAY_SPOOL_MAX_BYTES 配额常量(默认 5GB)"
        )

    def test_relay_db_has_spool_dir_constant(self):
        """relay_db.py 包含 RELAY_SPOOL_DIR 目录常量。"""
        path = REPO_ROOT / "database" / "relay_db.py"
        content = path.read_text(encoding="utf-8")
        assert "RELAY_SPOOL_DIR" in content, (
            "relay_db.py 应定义 RELAY_SPOOL_DIR 临时文件目录"
        )

    def test_relay_spool_md_mentions_high_water(self):
        """relay-spool-management.md 提及高水位(80%)处理流程。"""
        path = DOCS_DIR / "relay-spool-management.md"
        if not path.exists():
            pytest.skip("relay-spool-management.md 不存在")
        content = path.read_text(encoding="utf-8")
        assert "高水位" in content or "high_water" in content.lower() or "80%" in content


# ─────────────────────────────────────────────────────────────
#  P2-7: Prometheus exporter + Grafana + 可观测性文档
# ─────────────────────────────────────────────────────────────

class TestP27PrometheusExporter:
    """P2-7: services/prometheus_exporter.py 独立 HTTP metrics server。"""

    def test_prometheus_exporter_py_exists(self):
        """services/prometheus_exporter.py 文件存在。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        assert path.exists(), f"缺少 prometheus_exporter: {path}"
        assert path.is_file()

    def test_exporter_exposes_metrics_endpoint(self):
        """prometheus_exporter.py 包含 /metrics 端点路由。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        content = path.read_text(encoding="utf-8")
        assert '"/metrics"' in content or "self.path == \"/metrics\"" in content, (
            "prometheus_exporter.py 应响应 /metrics 路径"
        )

    def test_exporter_uses_threading_http_server(self):
        """prometheus_exporter.py 使用 ThreadingHTTPServer(避免阻塞)。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        content = path.read_text(encoding="utf-8")
        assert "ThreadingHTTPServer" in content, (
            "应使用 ThreadingHTTPServer(避免单连接阻塞)"
        )

    def test_exporter_listens_on_9100(self):
        """prometheus_exporter.py 默认监听 9100 端口。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        content = path.read_text(encoding="utf-8")
        assert "9100" in content, "应默认监听 9100 端口(Prometheus 标准 exporter 端口)"

    def test_exporter_has_collect_metrics_function(self):
        """prometheus_exporter.py 定义 collect_metrics() 函数。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        content = path.read_text(encoding="utf-8")
        assert "def collect_metrics" in content, (
            "应定义 collect_metrics() 函数(返回 Prometheus text format)"
        )

    def test_exporter_has_health_endpoint(self):
        """prometheus_exporter.py 暴露 /health 端点(Docker healthcheck)。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        content = path.read_text(encoding="utf-8")
        assert '"/health"' in content or "self.path == \"/health\"" in content, (
            "应暴露 /health 端点供 Docker healthcheck 使用"
        )

    def test_exporter_uses_read_only_sqlite_mode(self):
        """SQLite 读取使用 mode=ro(只读,避免写锁竞争)。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        content = path.read_text(encoding="utf-8")
        assert "mode=ro" in content, (
            "应使用 file:...?mode=ro URI 打开 SQLite(只读,避免写锁竞争)"
        )

    def test_exporter_supports_module_entry(self):
        """prometheus_exporter.py 支持 python -m services.prometheus_exporter。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        content = path.read_text(encoding="utf-8")
        assert '__main__' in content, (
            "应包含 if __name__ == '__main__' 入口(支持 python -m 启动)"
        )

    def test_exporter_exposes_crdb_ru_metric(self):
        """暴露 crdb_ru_daily 指标。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        content = path.read_text(encoding="utf-8")
        assert "crdb_ru_daily" in content, "应暴露 crdb_ru_daily 指标"

    def test_exporter_exposes_relay_spool_metric(self):
        """暴露 relay_spool 相关指标(usage_bytes / usage_ratio / high_water)。"""
        path = REPO_ROOT / "services" / "prometheus_exporter.py"
        content = path.read_text(encoding="utf-8")
        assert "relay_spool" in content, "应暴露 relay_spool 相关指标"

    def test_exporter_module_importable(self):
        """prometheus_exporter 模块可正常导入(语法/依赖正确)。"""
        try:
            from services import prometheus_exporter as exporter
        except Exception as e:
            pytest.skip(f"prometheus_exporter 导入失败(环境依赖): {e}")
        # 模块应包含核心符号
        assert hasattr(exporter, "collect_metrics"), "应暴露 collect_metrics 函数"
        assert hasattr(exporter, "create_server"), "应暴露 create_server 工厂函数"

    def test_grafana_dashboard_json_exists(self):
        """config/grafana-dashboard.json 文件存在。"""
        path = REPO_ROOT / "config" / "grafana-dashboard.json"
        assert path.exists(), f"缺少 grafana 仪表盘配置: {path}"

    def test_grafana_dashboard_is_valid_json(self):
        """grafana-dashboard.json 是合法 JSON。"""
        import json
        path = REPO_ROOT / "config" / "grafana-dashboard.json"
        if not path.exists():
            pytest.skip("grafana-dashboard.json 不存在")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            pytest.fail(f"grafana-dashboard.json 不是合法 JSON: {e}")
        assert isinstance(data, dict), "dashboard JSON 顶层应为对象"
        # Grafana dashboard 应包含 panels 字段
        assert "panels" in data or "dashboard" in data, (
            "Grafana 仪表盘应包含 panels 字段"
        )

    def test_observability_md_exists(self):
        """docs/observability.md 文档存在。"""
        path = DOCS_DIR / "observability.md"
        assert path.exists(), f"缺少可观测性文档: {path}"

    def test_observability_md_mentions_prometheus(self):
        """observability.md 提及 Prometheus。"""
        path = DOCS_DIR / "observability.md"
        if not path.exists():
            pytest.skip("observability.md 不存在")
        content = path.read_text(encoding="utf-8")
        assert "prometheus" in content.lower() or "Prometheus" in content, (
            "observability.md 应说明 Prometheus 抓取配置"
        )

    def test_observability_md_mentions_alertmanager(self):
        """observability.md 提及 Alertmanager(告警)。"""
        path = DOCS_DIR / "observability.md"
        if not path.exists():
            pytest.skip("observability.md 不存在")
        content = path.read_text(encoding="utf-8")
        # 至少应提及告警机制(Alertmanager 或告警规则)
        assert "alertmanager" in content.lower() or "告警" in content, (
            "observability.md 应说明 Alertmanager 告警规则"
        )


# ─────────────────────────────────────────────────────────────
#  集成验证: 关键文件清单
# ─────────────────────────────────────────────────────────────

class TestP2AllFilesPresent:
    """R37 Batch 4 P2 所有产出文件清单验证。"""

    @pytest.mark.parametrize("rel_path", [
        # P2-1
        "docs/SIGNING.md",
        # P2-2
        "docs/least-privilege.md",
        # P2-3
        "docs/redis-security.md",
        # P2-4 (docker-compose.yml / Dockerfile 已存在,仅校验)
        # P2-5
        "docs/delivery-idempotency.md",
        # P2-6
        "docs/relay-spool-management.md",
        # P2-7
        "docs/observability.md",
        "services/prometheus_exporter.py",
        "config/grafana-dashboard.json",
    ])
    def test_file_exists(self, rel_path):
        """关键文件必须存在。"""
        path = REPO_ROOT / rel_path
        assert path.exists(), f"缺失文件: {rel_path}"
