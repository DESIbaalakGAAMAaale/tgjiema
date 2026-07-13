"""R42 P1-1: Docker digest 在 CI 中真实拉取验证 — 测试套件。

被测对象:
- Dockerfile                          — PYTHON_IMAGE ARG 与 digest 格式
- scripts/verify_docker_digest.sh     — digest 拉取校验脚本
- .github/workflows/release-gates.yml — docker-digest-verify job 存在性

测试策略:
- 静态文件内容检查,不依赖 docker CLI 或网络访问
- 兼容 Python 3.9+(避免 PEP 604 X | Y 类型语法)
- 通过正则解析 Dockerfile,不使用 docker build
- 校验占位 digest 已替换为真实值

对应 R42 P1-1 整改要求:
    "Docker digest 在 CI 中通过 docker manifest inspect 真实拉取验证"
"""
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
WORKFLOW_FILE = PROJECT_ROOT / ".github" / "workflows" / "release-gates.yml"
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_docker_digest.sh"


def _read_dockerfile() -> str:
    """读取 Dockerfile 内容(UTF-8)。"""
    return DOCKERFILE.read_text(encoding="utf-8")


def _read_workflow() -> str:
    """读取 release-gates.yml 内容(UTF-8)。"""
    return WORKFLOW_FILE.read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════
# P1-1.1: digest 格式正确(64 位 sha256)
# ════════════════════════════════════════════════════════════════
class TestDockerDigestFormat:
    """R42 P1-1: Dockerfile 中的 digest 必须为 64 位 sha256 hex 格式。"""

    @pytest.fixture
    def dockerfile_content(self) -> str:
        return _read_dockerfile()

    def test_digest_is_64_char_sha256_hex(self, dockerfile_content: str):
        """digest 必须为 64 位小写十六进制 sha256 格式。

        期望格式: ARG PYTHON_IMAGE=python:3.12-slim@sha256:<64-hex>
        """
        # 提取 PYTHON_IMAGE 行
        m = re.search(r'^ARG\s+PYTHON_IMAGE=(\S+)', dockerfile_content, re.MULTILINE)
        assert m, "Dockerfile 未定义 ARG PYTHON_IMAGE"
        image_ref = m.group(1)
        # 提取 digest
        digest_match = re.search(r'@sha256:([a-f0-9]+)', image_ref)
        assert digest_match, (
            f"PYTHON_IMAGE 未包含 @sha256:<digest> 段: {image_ref}\n"
            f"R40 P2-2 要求基础镜像必须固定 digest"
        )
        digest = digest_match.group(1)
        # 校验长度为 64
        assert len(digest) == 64, (
            f"digest 长度不为 64,实际长度: {len(digest)}\n"
            f"digest: {digest}"
        )
        # 校验为小写 hex
        assert re.match(r'^[a-f0-9]{64}$', digest), (
            f"digest 不是合法的 64 位小写 hex: {digest}\n"
            f"应仅包含 [0-9a-f] 字符"
        )

    def test_digest_is_lowercase(self, dockerfile_content: str):
        """digest 必须为小写 hex(Docker Hub manifest digest 规范)。"""
        m = re.search(r'^ARG\s+PYTHON_IMAGE=(\S+)', dockerfile_content, re.MULTILINE)
        assert m
        image_ref = m.group(1)
        digest_match = re.search(r'@sha256:([a-fA-F0-9]+)', image_ref)
        assert digest_match
        digest = digest_match.group(1)
        # 不应包含大写字母
        assert digest == digest.lower(), (
            f"digest 应为小写,实际: {digest}\n"
            f"Docker Hub manifest digest 规范要求小写"
        )


# ════════════════════════════════════════════════════════════════
# P1-1.2: digest 非占位值
# ════════════════════════════════════════════════════════════════
class TestDockerDigestNotPlaceholder:
    """R42 P1-1: digest 必须为真实可拉取的值,而非占位符。"""

    # 已知占位 digest 列表(来自历史 docs/docker-image-pinning.md)
    KNOWN_PLACEHOLDERS = [
        "5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef",
        "b0d2c8b8e5b2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e",
        "0000000000000000000000000000000000000000000000000000000000000000",
        "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "placeholderplaceholderplaceholderplaceholderplaceholderplaceholder",
        "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
    ]

    @pytest.fixture
    def dockerfile_content(self) -> str:
        return _read_dockerfile()

    def test_digest_is_not_known_placeholder(self, dockerfile_content: str):
        """digest 不应是已知占位值列表中的任何一个。"""
        m = re.search(r'^ARG\s+PYTHON_IMAGE=(\S+)', dockerfile_content, re.MULTILINE)
        assert m
        image_ref = m.group(1)
        digest_match = re.search(r'@sha256:([a-f0-9]{64})', image_ref)
        assert digest_match, f"digest 格式不合法: {image_ref}"
        digest = digest_match.group(1)
        assert digest not in self.KNOWN_PLACEHOLDERS, (
            f"digest 仍为占位值: {digest}\n"
            f"请替换为真实 digest,获取方法:\n"
            f"  docker inspect --format='{{{{index .RepoDigests 0}}}}' python:3.12-slim\n"
            f"  或访问 https://hub.docker.com/v2/repositories/library/python/tags/3.12-slim"
        )

    def test_digest_is_not_pattern_like_placeholder(self, dockerfile_content: str):
        """digest 不应匹配常见占位模式(连续重复字符 / 顺序字符)。"""
        m = re.search(r'^ARG\s+PYTHON_IMAGE=(\S+)', dockerfile_content, re.MULTILINE)
        assert m
        image_ref = m.group(1)
        digest_match = re.search(r'@sha256:([a-f0-9]{64})', image_ref)
        assert digest_match
        digest = digest_match.group(1)
        # 全 0
        assert digest != "0" * 64, "digest 不应为全 0"
        # 全 f
        assert digest != "f" * 64, "digest 不应为全 f"
        # 重复模式(同一 4 字符片段重复 16 次 = 64 字符)
        # 使用捕获组 + 反向引用,\1 表示重复第一个捕获组
        repeat_match = re.match(r'^([a-f0-9]{4})\1{15}$', digest)
        assert not repeat_match, (
            f"digest 看起来是重复模式占位值: {digest}\n"
            f"重复片段: {repeat_match.group(1) if repeat_match else 'N/A'}"
        )
        # 顺序字符(0123...abcdef 重复)
        if digest == "0123456789abcdef" * 4:
            pytest.fail(f"digest 是顺序字符占位值: {digest}")


# ════════════════════════════════════════════════════════════════
# P1-1.3: PYTHON_IMAGE ARG 存在
# ════════════════════════════════════════════════════════════════
class TestPythonImageArgExists:
    """R42 P1-1: Dockerfile 必须定义 ARG PYTHON_IMAGE。"""

    def test_python_image_arg_defined(self):
        """Dockerfile 必须包含 ARG PYTHON_IMAGE 定义。"""
        content = _read_dockerfile()
        # 匹配 ARG PYTHON_IMAGE=<value>(可能有空格)
        m = re.search(r'^ARG\s+PYTHON_IMAGE=(.+)$', content, re.MULTILINE)
        assert m, (
            "Dockerfile 未定义 ARG PYTHON_IMAGE\n"
            "R40 P2-2 要求使用 ARG PYTHON_IMAGE 固定基础镜像 digest"
        )
        image_ref = m.group(1).strip()
        # 值不应为空
        assert image_ref, "ARG PYTHON_IMAGE 值为空"
        # 必须包含镜像名
        assert "python:" in image_ref, (
            f"PYTHON_IMAGE 必须为 python 镜像: {image_ref}"
        )

    def test_python_image_used_in_from(self):
        """PYTHON_IMAGE 必须在 FROM 指令中被使用(至少一次)。"""
        content = _read_dockerfile()
        # 至少一个 FROM ${PYTHON_IMAGE}
        from_lines = re.findall(r'^FROM\s+\$\{PYTHON_IMAGE\}', content, re.MULTILINE)
        assert len(from_lines) >= 2, (
            "FROM ${PYTHON_IMAGE} 应至少出现 2 次(builder + runtime),"
            f"实际: {len(from_lines)} 次"
        )


# ════════════════════════════════════════════════════════════════
# P1-1.4: verify_docker_digest.sh 脚本存在且可执行
# ════════════════════════════════════════════════════════════════
class TestVerifyDockerDigestScript:
    """R42 P1-1: scripts/verify_docker_digest.sh 脚本必须存在且可执行。"""

    def test_script_file_exists(self):
        """scripts/verify_docker_digest.sh 文件存在。"""
        assert VERIFY_SCRIPT.exists(), (
            f"scripts/verify_docker_digest.sh 不存在: {VERIFY_SCRIPT}\n"
            f"R42 P1-1 要求提供 digest 拉取校验脚本"
        )

    def test_script_has_shebang(self):
        """脚本必须包含 bash shebang。"""
        if not VERIFY_SCRIPT.exists():
            pytest.skip("verify_docker_digest.sh 不存在")
        content = VERIFY_SCRIPT.read_text(encoding="utf-8")
        first_line = content.split("\n", 1)[0]
        assert first_line.startswith("#!/usr/bin/env bash") or \
               first_line.startswith("#!/bin/bash"), (
            f"verify_docker_digest.sh 缺少 bash shebang,首行: {first_line!r}"
        )

    def test_script_calls_docker_manifest_inspect(self):
        """脚本必须调用 docker manifest inspect 验证 digest。"""
        if not VERIFY_SCRIPT.exists():
            pytest.skip("verify_docker_digest.sh 不存在")
        content = VERIFY_SCRIPT.read_text(encoding="utf-8")
        # 必须包含 docker manifest inspect 调用
        assert "docker manifest inspect" in content, (
            "verify_docker_digest.sh 未调用 docker manifest inspect\n"
            "R42 P1-1 要求通过 manifest inspect 真实验证 digest 可拉取"
        )

    def test_script_is_executable(self):
        """脚本应具有可执行权限(Unix / Git Bash 环境)。

        在 Windows 文件系统上,该检查可能不适用,允许跳过。
        """
        if not VERIFY_SCRIPT.exists():
            pytest.skip("verify_docker_digest.sh 不存在")
        if sys.platform.startswith("win"):
            pytest.skip("Windows 文件系统不支持 Unix 可执行位检查")
        mode = VERIFY_SCRIPT.stat().st_mode
        assert mode & stat.S_IXUSR, (
            f"verify_docker_digest.sh 缺少用户可执行位,mode={oct(mode)}\n"
            f"修复: chmod +x scripts/verify_docker_digest.sh"
        )

    def test_script_exits_nonzero_on_failure(self):
        """脚本必须包含 set -uo pipefail 或失败时 exit 1。"""
        if not VERIFY_SCRIPT.exists():
            pytest.skip("verify_docker_digest.sh 不存在")
        content = VERIFY_SCRIPT.read_text(encoding="utf-8")
        # 至少包含 set -e / set -uo pipefail / exit 1 中的一种
        assert "set -uo pipefail" in content or "set -e" in content, (
            "verify_docker_digest.sh 未启用严格模式(set -uo pipefail / set -e)"
        )
        # 必须有 exit 1 失败退出
        assert re.search(r'exit\s+1', content), (
            "verify_docker_digest.sh 未在失败时 exit 1"
        )


# ════════════════════════════════════════════════════════════════
# P1-1.5: release-gates.yml workflow 文件存在且包含 docker-digest-verify job
# ════════════════════════════════════════════════════════════════
class TestReleaseGatesWorkflow:
    """R42 P0-1: release-gates.yml workflow 文件必须存在并包含 docker-digest-verify job。"""

    def test_workflow_file_exists(self):
        """release-gates.yml workflow 文件存在。"""
        assert WORKFLOW_FILE.exists(), (
            f".github/workflows/release-gates.yml 不存在: {WORKFLOW_FILE}\n"
            f"R42 P0-1 要求新增独立 release-gates workflow"
        )

    def test_workflow_contains_docker_digest_verify_job(self):
        """workflow 必须包含 docker-digest-verify job。"""
        if not WORKFLOW_FILE.exists():
            pytest.skip("release-gates.yml 不存在")
        content = _read_workflow()
        # 检查 docker-digest-verify job 定义
        assert re.search(r'^\s*docker-digest-verify:\s*$', content, re.MULTILINE), (
            "release-gates.yml 未定义 docker-digest-verify job\n"
            "R42 P1-1 要求通过独立 job 验证 digest 可拉取"
        )

    def test_workflow_calls_verify_docker_digest_sh(self):
        """workflow 必须在 docker-digest-verify job 中调用 verify_docker_digest.sh。"""
        if not WORKFLOW_FILE.exists():
            pytest.skip("release-gates.yml 不存在")
        content = _read_workflow()
        # 检查 verify_docker_digest.sh 被调用
        assert "scripts/verify_docker_digest.sh" in content, (
            "release-gates.yml 未调用 scripts/verify_docker_digest.sh\n"
            "R42 P1-1 要求通过该脚本验证 digest"
        )

    def test_workflow_has_required_jobs(self):
        """workflow 必须包含 R42 P0-1 要求的所有 10 个 jobs。"""
        if not WORKFLOW_FILE.exists():
            pytest.skip("release-gates.yml 不存在")
        content = _read_workflow()
        required_jobs = [
            "docker-build",
            "docker-digest-verify",
            "compose-config",
            "redis-acl-matrix",
            "schema-diff",
            "backup-restore-drill",
            "sbom",
            "pip-audit",
            "trivy",
            "sign-image",
        ]
        missing = []
        for job in required_jobs:
            # 匹配 job 定义(YAML 缩进 + job_id + 冒号)
            pattern = rf'^\s*{re.escape(job)}:\s*$'
            if not re.search(pattern, content, re.MULTILINE):
                missing.append(job)
        assert not missing, (
            f"release-gates.yml 缺少 jobs: {missing}\n"
            f"R42 P0-1 要求完整 10 个发布门禁 job"
        )

    def test_workflow_triggers_on_master_push_and_pr(self):
        """workflow 必须在 push 到 master/main 和 PR 到 master/main 时触发。"""
        if not WORKFLOW_FILE.exists():
            pytest.skip("release-gates.yml 不存在")
        content = _read_workflow()
        # 检查触发条件包含 master/main
        assert "master" in content and "main" in content, (
            "release-gates.yml 触发条件未包含 master/main 分支"
        )
        # 检查 push 和 pull_request 触发
        assert "push:" in content, "release-gates.yml 缺少 push 触发器"
        assert "pull_request:" in content, "release-gates.yml 缺少 pull_request 触发器"


# ════════════════════════════════════════════════════════════════
# 集成测试:End-to-end digest 校验链路完整性
# ════════════════════════════════════════════════════════════════
class TestDigestVerificationChain:
    """R42 P1-1: 验证 Dockerfile → verify_docker_digest.sh → workflow 的完整链路。"""

    def test_dockerfile_to_script_consistency(self):
        """Dockerfile 中的 digest 应能被 verify_docker_digest.sh 解析(静态规则一致)。"""
        # 读取 Dockerfile 中的 digest
        dockerfile_content = _read_dockerfile()
        m = re.search(r'^ARG\s+PYTHON_IMAGE=(\S+)', dockerfile_content, re.MULTILINE)
        assert m
        image_ref = m.group(1)
        # Dockerfile 中的 digest
        dockerfile_digest_match = re.search(r'@sha256:([a-f0-9]{64})', image_ref)
        assert dockerfile_digest_match, "Dockerfile digest 格式不合法"
        dockerfile_digest = dockerfile_digest_match.group(1)

        # 读取 verify_docker_digest.sh 中的占位 digest 列表
        if not VERIFY_SCRIPT.exists():
            pytest.skip("verify_docker_digest.sh 不存在")
        script_content = VERIFY_SCRIPT.read_text(encoding="utf-8")
        # 提取脚本中的占位 digest 列表
        script_placeholders = re.findall(r'"([a-f0-9]{64}|[a-z]+[a-z]+[a-z]+[a-z]+)"', script_content)
        # Dockerfile digest 不应出现在脚本的占位列表中
        assert dockerfile_digest not in script_placeholders, (
            f"Dockerfile digest {dockerfile_digest} 出现在 verify_docker_digest.sh 的占位列表中"
        )

    def test_workflow_to_script_consistency(self):
        """workflow 中调用的脚本路径与实际脚本文件一致。"""
        if not WORKFLOW_FILE.exists():
            pytest.skip("release-gates.yml 不存在")
        workflow_content = _read_workflow()
        # workflow 中应引用 scripts/verify_docker_digest.sh
        assert "scripts/verify_docker_digest.sh" in workflow_content
        # 该脚本文件应实际存在
        assert VERIFY_SCRIPT.exists(), (
            "workflow 引用 scripts/verify_docker_digest.sh 但文件不存在"
        )
