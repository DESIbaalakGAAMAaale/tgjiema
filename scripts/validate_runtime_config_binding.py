#!/usr/bin/env python3
"""R71 P1-04 / P1-05 / P0-13: 镜像变量与运行配置身份绑定校验。

R71 P1-04 整改背景(镜像变量格式校验过宽):
    原 scripts/compose_runtime_e2e.py 的 preflight 阶段只检查
    ``if "@sha256:" not in tgjiema_image``,可被以下绕过:
      - "any-repo@sha256:0000"  (其他仓库 + 全零 digest)
      - "tgjiema@sha256:abc"     (短 hash)
      - "tgjiema:latest@sha256:..." (tag + digest 混合)
    整改:使用完整正则,仅允许预期 registry/repository + @sha256: + 64位小写 hex。
    pull 后读取真实 RepoDigest,与 candidate manifest 精确比较(不只是字符串包含)。

R71 P1-05 整改背景(host 配置文件需要身份绑定):
    groups.yaml / topology.yaml 虽不是 Python 代码,但会改变运行行为(角色分组、
    Redis 部署拓扑、Bot 部署位置等)。原 deployment manifest / E2E evidence /
    rollback record 不包含 host config digest,导致:
      - 部署时宿主机替换 groups.yaml,topology.yaml 不会被发现
      - 回滚到旧 release 但用新 host config,行为不一致
      - 跨环境复现不可追溯
    整改:对 host config 计算 sha256(规范排序后),写入:
      - deployment manifest(generate_release_manifest.py 输出)
      - E2E evidence(compose_runtime_e2e.py 输出)
      - promotion evidence(verify_rc_identity.py 输出)
      - rollback record(部署脚本输出)
    部署前后都验证 config digest;漂移时阻断,不允许静默使用宿主机新文件。

R71 P0-13 整改背景(当前 SHA 与证据绑定):
    所有 required checks 必须绑定当前候选 SHA,禁止用旧 run / 父提交 / PR head
    替代。本模块为调用方提供:
      - get_source_sha(): 当前 HEAD SHA
      - get_workflow_run_id(): GITHUB_RUN_ID
      - get_workflow_run_attempt(): GITHUB_RUN_ATTEMPT
    调用方应将上述字段写入 evidence,并 cross-verify 与 candidate manifest 一致。

退出码:
    0: 校验通过
    1: 校验失败(格式错误 / digest 不匹配 / 配置漂移)
    2: CLI 参数错误或 IO 错误

使用方法:
    # 仅校验 TGJIEMA_IMAGE 格式
    TGJIEMA_IMAGE="ghcr.io/maxiuquan/tgjiema@sha256:abc123..." \
        python scripts/validate_runtime_config_binding.py --mode image-only

    # 校验 image 与 candidate manifest 一致
    python scripts/validate_runtime_config_binding.py --mode image-verify \\
        --candidate-manifest ./candidate-manifest.json \\
        --pull-and-compare

    # 计算 host config digest 并与预期比对
    python scripts/validate_runtime_config_binding.py --mode host-config \\
        --expected-digest sha256:...

    # 输出完整 runtime config binding evidence
    python scripts/validate_runtime_config_binding.py --mode full-evidence \\
        --candidate-manifest ./candidate-manifest.json \\
        --output runtime-config-binding.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:
    from loguru import logger
except ImportError:  # pragma: no cover — 容错,CI 已装
    import logging
    logger = logging.getLogger("validate_runtime_config_binding")  # type: ignore[assignment]

# ════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════

SCHEMA_VERSION: str = "r71-wave7-p1-04/p1-05"
TOOL_VERSION: str = "R71-WAVE7-P1-04/05/P0-13"

# R71 P1-04: TGJIEMA_IMAGE 严格正则
# 仅允许: <registry>/<repository>@sha256:<64位小写hex>
#   - registry: 域名或 IP(:port 可选),如 ghcr.io / registry.example.com:5000
#   - repository: 路径可含 / ,如 maxiuquan/tgjiema
#   - @sha256: 字面量
#   - 64 位小写 hex (sha256 digest)
# 拒绝: tag / 短 hash / 其他仓库 / 多余字符 / 大写 hex
#
# 简化设计:用两个分离的正则避免复杂嵌套 — 一个匹配整体结构,
# 另一个验证 registry 是合法域名或 localhost(:port 可选)。
TGJIEMA_IMAGE_PATTERN: re.Pattern[str] = re.compile(
    r"^(?P<registry>[a-zA-Z0-9._-]+(?::\d{1,5})?|localhost(?::\d{1,5})?)"
    r"/"
    r"(?P<repository>[a-z0-9]+(?:[/.][a-z0-9-]+)*)"
    r"@sha256:"
    r"(?P<digest>[0-9a-f]{64})$"
)

# registry 合法性:至少含一个点(域名)或为 localhost
REGISTRY_DOMAIN_PATTERN: re.Pattern[str] = re.compile(
    r"^(localhost(:\d{1,5})?|"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)+"
    r"(?::\d{1,5})?)$"
)

# 默认预期 registry/repository(从环境变量或参数覆盖)
DEFAULT_EXPECTED_REGISTRY: str = "ghcr.io"
DEFAULT_EXPECTED_REPOSITORY: str = "maxiuquan/tgjiema"

# R71 P1-05: host config 文件列表(会改变运行行为)
# 这些文件虽不是 Python 代码,但影响 SERVICE_ROLE 分组、Redis 拓扑、
# Bot 部署位置等运行时行为,必须绑定身份。
HOST_CONFIG_FILES: tuple[str, ...] = (
    "config/groups.yaml",
    "config/topology.yaml",
)

# R71 P0-13: 当前 SHA / run ID / attempt 来源
GITHUB_SHA_ENV: str = "GITHUB_SHA"
GITHUB_RUN_ID_ENV: str = "GITHUB_RUN_ID"
GITHUB_RUN_ATTEMPT_ENV: str = "GITHUB_RUN_ATTEMPT"

# 退出码
EXIT_SUCCESS: int = 0
EXIT_VALIDATION_FAILURE: int = 1
EXIT_CLI_ERROR: int = 2


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════


@dataclass
class ImageReference:
    """解析后的镜像引用。

    Attributes:
        raw: 原始字符串(如 ghcr.io/maxiuquan/tgjiema@sha256:abc... )
        registry: registry 域名(如 ghcr.io)
        repository: repository 路径(如 maxiuquan/tgjiema)
        digest: sha256 digest (sha256: + 64 hex)
        digest_hex: 64位 hex (无 sha256: 前缀)
    """

    raw: str
    registry: str
    repository: str
    digest: str  # sha256:<64hex>
    digest_hex: str  # <64hex> 无前缀


@dataclass
class HostConfigDigest:
    """单个 host config 文件的 digest。

    Attributes:
        path: 文件相对路径(如 config/groups.yaml)
        exists: 文件是否存在
        sha256: 文件内容的 sha256 hex (64位,无 sha256: 前缀)
        size_bytes: 文件大小(字节)
        file_mode: 文件 mode(octal 字符串,如 "0o100644";R76 P1-04)
    """

    path: str
    exists: bool
    sha256: str
    size_bytes: int
    file_mode: str = ""


@dataclass
class RuntimeConfigBinding:
    """R71 P1-05: 运行配置绑定 evidence。

    包含:
      - 所有 host config 文件的 digest
      - 组合 digest(所有 host config 文件内容排序后拼接的 sha256)
      - 当前 source SHA / run ID / attempt (R71 P0-13)
    """

    schema_version: str = SCHEMA_VERSION
    tool_version: str = TOOL_VERSION
    generated_at: str = ""
    source_sha: str = ""
    workflow_run_id: str = ""
    workflow_run_attempt: str = ""
    host_config_digests: list[HostConfigDigest] = field(default_factory=list)
    combined_host_config_digest: str = ""  # sha256:<64hex>
    canonical_input_manifest: str = ""  # R76 P1-04: 可审计 canonical manifest JSON
    image_reference: str = ""
    image_registry: str = ""
    image_repository: str = ""
    image_digest: str = ""  # sha256:<64hex>
    image_repo_digest: str = ""  # ghcr.io/.../...@sha256:...
    overall_passed: bool = False
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 可序列化的 dict。"""
        d = asdict(self)
        return d

    def add_error(self, msg: str) -> None:
        """追加错误并标记 overall_passed=False。"""
        self.errors.append(msg)
        self.overall_passed = False


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    """计算 bytes 的 sha256 hex (64位,无 sha256: 前缀)。"""
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    """计算文件内容的 sha256 hex (64位,无 sha256: 前缀)。

    Raises:
        FileNotFoundError: 文件不存在
        OSError: 读取失败
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    """加载 JSON 文件。

    Raises:
        FileNotFoundError: 文件不存在
        json.JSONDecodeError: JSON 解析失败
    """
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_source_sha(repo_root: Path) -> str:
    """获取当前 git HEAD SHA(失败回退到 GITHUB_SHA 环境变量)。

    R71 P0-13: 所有 evidence 必须绑定当前候选 SHA。
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if sha:
                return sha
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return os.environ.get(GITHUB_SHA_ENV, "")


def _get_workflow_run_id() -> str:
    """获取 GITHUB_RUN_ID 环境变量(R71 P0-13)。"""
    return os.environ.get(GITHUB_RUN_ID_ENV, "")


def _get_workflow_run_attempt() -> str:
    """获取 GITHUB_RUN_ATTEMPT 环境变量(R71 P0-13)。"""
    return os.environ.get(GITHUB_RUN_ATTEMPT_ENV, "")


# ════════════════════════════════════════════════════════════════
# R71 P1-04: TGJIEMA_IMAGE 严格校验
# ════════════════════════════════════════════════════════════════


def parse_image_reference(image_ref: str) -> ImageReference | None:
    """解析 TGJIEMA_IMAGE 引用为 ImageReference。

    Args:
        image_ref: 镜像引用字符串(如 ghcr.io/maxiuquan/tgjiema@sha256:abc...)

    Returns:
        ImageReference 实例(若格式合法)或 None(格式非法)
    """
    if not image_ref:
        return None
    m = TGJIEMA_IMAGE_PATTERN.match(image_ref)
    if not m:
        return None
    digest_hex = m.group("digest")
    return ImageReference(
        raw=image_ref,
        registry=m.group("registry"),
        repository=m.group("repository"),
        digest=f"sha256:{digest_hex}",
        digest_hex=digest_hex,
    )


def validate_image_reference(
    image_ref: str,
    expected_registry: str = DEFAULT_EXPECTED_REGISTRY,
    expected_repository: str = DEFAULT_EXPECTED_REPOSITORY,
) -> tuple[ImageReference | None, list[str]]:
    """R71 P1-04: 严格校验 TGJIEMA_IMAGE 格式。

    Args:
        image_ref: 镜像引用字符串
        expected_registry: 预期 registry(默认 ghcr.io)
        expected_repository: 预期 repository(默认 maxiuquan/tgjiema)

    Returns:
        (ImageReference, []) 若校验通过;否则 (None, [errors])
    """
    errors: list[str] = []
    if not image_ref:
        errors.append("TGJIEMA_IMAGE 环境变量为空")
        return None, errors

    parsed = parse_image_reference(image_ref)
    if parsed is None:
        errors.append(
            f"TGJIEMA_IMAGE 格式不合法 — 期望 "
            f"'{expected_registry}/{expected_repository}@sha256:<64位小写hex>',"
            f"实际值: {image_ref!r}。"
            f"常见错误: 短 hash / 大写 hex / 其他 registry / 含 tag / 多余字符"
        )
        return None, errors

    if parsed.registry != expected_registry:
        errors.append(
            f"TGJIEMA_IMAGE registry 不匹配: 期望 {expected_registry!r},"
            f"实际 {parsed.registry!r}"
        )
    # R71 P1-04: 额外校验 registry 是合法域名或 localhost
    if not REGISTRY_DOMAIN_PATTERN.match(parsed.registry):
        errors.append(
            f"TGJIEMA_IMAGE registry 不是合法域名或 localhost: "
            f"{parsed.registry!r}"
        )
    if parsed.repository != expected_repository:
        errors.append(
            f"TGJIEMA_IMAGE repository 不匹配: 期望 {expected_repository!r},"
            f"实际 {parsed.repository!r}"
        )
    if errors:
        return None, errors
    return parsed, []


def pull_and_read_repo_digest(image_ref: str) -> str:
    """R71 P1-04: docker pull + docker inspect 读取真实 RepoDigest。

    pull 后从 docker inspect 读取 .RepoDigests 字段,返回与 image_ref
    匹配的 RepoDigest。失败时返回空字符串。

    Args:
        image_ref: 镜像引用(必须是 <registry>/<repo>@sha256:<64hex> 格式)

    Returns:
        RepoDigest 字符串(如 ghcr.io/maxiuquan/tgjiema@sha256:abc...)
        或空字符串(失败)
    """
    if not image_ref:
        return ""
    parsed = parse_image_reference(image_ref)
    if parsed is None:
        return ""
    try:
        # docker pull(已存在则 no-op)
        pull_result = subprocess.run(
            ["docker", "pull", image_ref],
            capture_output=True, text=True, timeout=300,
        )
        if pull_result.returncode != 0:
            logger.warning(
                f"docker pull 失败(returncode={pull_result.returncode}): "
                f"{pull_result.stderr.strip()[:200]}"
            )
            return ""
        # docker inspect .RepoDigests
        inspect_result = subprocess.run(
            ["docker", "inspect", "--format",
             "{{json .RepoDigests}}", image_ref],
            capture_output=True, text=True, timeout=15,
        )
        if inspect_result.returncode != 0:
            logger.warning(
                f"docker inspect 失败: {inspect_result.stderr.strip()[:200]}"
            )
            return ""
        # RepoDigests 是 JSON 数组,如 ["ghcr.io/maxiuquan/tgjiema@sha256:abc..."]
        repo_digests = json.loads(inspect_result.stdout.strip())
        if not repo_digests:
            return ""
        # 找到与 image_ref 匹配的 RepoDigest(忽略 digest 大小写)
        target_digest_lower = parsed.digest.lower()
        for rd in repo_digests:
            if "@" in rd:
                rd_digest = rd.split("@", 1)[1].lower()
                if rd_digest == target_digest_lower:
                    return rd
        # 未找到完全匹配,返回第一个(供诊断)
        return repo_digests[0]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError,
            json.JSONDecodeError) as exc:
        logger.warning(f"pull_and_read_repo_digest 异常: {exc}")
        return ""


def compare_image_with_candidate_manifest(
    parsed_image: ImageReference,
    candidate_manifest: dict[str, Any],
) -> tuple[bool, list[str]]:
    """R71 P1-04: 与 candidate manifest 精确比较 image digest。

    比对逻辑:
      1. candidate_manifest 必须包含 image_digest 字段(sha256:<64hex>)
      2. parsed_image.digest 必须与 candidate_manifest.image_digest 完全一致
         (大小写不敏感,但都应为小写)
      3. 若 candidate_manifest 包含 image_repo_digest 字段,也必须与
         parsed_image.raw 一致(忽略大小写)

    Args:
        parsed_image: 已解析的 ImageReference
        candidate_manifest: candidate-manifest.json 解析结果

    Returns:
        (True, []) 若一致;(False, [errors]) 若不一致
    """
    errors: list[str] = []
    if not candidate_manifest:
        errors.append("candidate_manifest 为空")
        return False, errors

    cm_image_digest = (
        candidate_manifest.get("image_digest")
        or candidate_manifest.get("imageDigest")
        or ""
    )
    if not cm_image_digest:
        errors.append(
            "candidate_manifest 缺少 image_digest 字段(或 imageDigest)"
        )
        return False, errors

    # 规范化比较(去前缀 + 小写)
    cm_digest_hex = cm_image_digest
    if cm_digest_hex.startswith("sha256:"):
        cm_digest_hex = cm_digest_hex[len("sha256:"):]
    cm_digest_hex = cm_digest_hex.lower()

    if cm_digest_hex != parsed_image.digest_hex.lower():
        errors.append(
            f"image_digest 不匹配: TGJIEMA_IMAGE digest="
            f"sha256:{parsed_image.digest_hex},"
            f" candidate_manifest image_digest={cm_image_digest}"
        )

    # 若 candidate_manifest 包含 image_repo_digest,也必须一致
    cm_repo_digest = (
        candidate_manifest.get("image_repo_digest")
        or candidate_manifest.get("imageRepoDigest")
        or ""
    )
    if cm_repo_digest:
        # 规范化比较(忽略大小写)
        if cm_repo_digest.lower() != parsed_image.raw.lower():
            errors.append(
                f"image_repo_digest 不匹配: TGJIEMA_IMAGE={parsed_image.raw!r},"
                f" candidate_manifest image_repo_digest={cm_repo_digest!r}"
            )

    if errors:
        return False, errors
    return True, []


# ════════════════════════════════════════════════════════════════
# R71 P1-05: host config digest 计算
# ════════════════════════════════════════════════════════════════


def compute_host_config_digest(
    repo_root: Path,
    config_files: tuple[str, ...] = HOST_CONFIG_FILES,
) -> tuple[list[HostConfigDigest], str]:
    """R71 P1-05 / R76 P1-04: 计算 host config 文件的 digest。

    R76 P1-04 整改:
        - 不再使用 ``path:sha\\n`` 拼接(隐式纳入路径文本和顺序)
        - 改为生成可审计 canonical input manifest(JSON),包含每个文件的
          path / file_mode / size_bytes / sha256
        - combined digest = SHA-256(canonical JSON of manifest)
        - 部署端可同算法回读:从 ``host_config_digests`` 字段重建 canonical
          manifest 并重新计算 digest 比对

    Args:
        repo_root: 仓库根目录
        config_files: host config 文件相对路径列表

    Returns:
        (per_file_digests, combined_digest)
        - per_file_digests: 每个文件的 HostConfigDigest 列表(含 file_mode)
        - combined_digest: canonical manifest 的 sha256(sha256:<64hex>),
          用于部署前后比对
    """
    per_file: list[HostConfigDigest] = []
    for rel_path in sorted(config_files):
        abs_path = repo_root / rel_path
        if not abs_path.exists():
            per_file.append(HostConfigDigest(
                path=rel_path,
                exists=False,
                sha256="",
                size_bytes=0,
                file_mode="",
            ))
            continue
        try:
            sha = _sha256_file(abs_path)
            stat_result = abs_path.stat()
            size = stat_result.st_size
            # R76 P1-04: 记录文件 mode(octal 字符串)
            file_mode = oct(stat_result.st_mode)
            per_file.append(HostConfigDigest(
                path=rel_path,
                exists=True,
                sha256=sha,
                size_bytes=size,
                file_mode=file_mode,
            ))
        except OSError as exc:
            per_file.append(HostConfigDigest(
                path=rel_path,
                exists=True,
                sha256="",
                size_bytes=0,
                file_mode="",
            ))
            logger.warning(f"读取 {abs_path} 失败: {exc}")

    # R76 P1-04: 构建 canonical input manifest(可审计 JSON)
    canonical_manifest_json = build_canonical_input_manifest(per_file)
    combined_digest = f"sha256:{hashlib.sha256(canonical_manifest_json.encode('utf-8')).hexdigest()}"
    return per_file, combined_digest


def build_canonical_input_manifest(
    per_file: list[HostConfigDigest],
) -> str:
    """R76 P1-04: 构建 canonical input manifest JSON 字符串。

    canonical manifest = JSON object with:
        - schema_version: "r76-p1-04-canonical-input-manifest"
        - algorithm: "sha256-canonical-json"
        - files: sorted list of {path, exists, file_mode, size_bytes, sha256}

    排序规则:
        - files 按 path 升序排序
        - JSON keys 按 sort_keys=True 排序
        - separators=(",", ":") 紧凑格式
        - ensure_ascii=False 保留 UTF-8

    部署端回读算法:
        1. 从 evidence 读取 ``host_config_digests`` 列表
        2. 调用本函数重建 canonical manifest JSON
        3. 计算 SHA-256,与 ``combined_host_config_digest`` 比对

    Args:
        per_file: HostConfigDigest 列表

    Returns:
        canonical manifest JSON 字符串
    """
    manifest_entries = []
    for f in per_file:
        if not f.exists:
            continue
        manifest_entries.append({
            "path": f.path,
            "exists": f.exists,
            "file_mode": f.file_mode,
            "size_bytes": f.size_bytes,
            "sha256": f.sha256,
        })
    manifest_entries.sort(key=lambda e: e["path"])
    canonical_manifest = {
        "schema_version": "r76-p1-04-canonical-input-manifest",
        "algorithm": "sha256-canonical-json",
        "files": manifest_entries,
    }
    return json.dumps(
        canonical_manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def verify_host_config_digest_readback(
    per_file: list[HostConfigDigest],
    expected_digest: str,
) -> tuple[bool, str]:
    """R76 P1-04: 部署端回读验证 — 从 per_file 重建 manifest 并比对 digest。

    Args:
        per_file: 部署端实际读取的 HostConfigDigest 列表
        expected_digest: 部署前记录的 combined digest (sha256:<64hex>)

    Returns:
        (True, "") 若一致;(False, error_msg) 若漂移
    """
    if not expected_digest:
        return False, "expected_digest 为空(部署前未记录)"
    canonical_json = build_canonical_input_manifest(per_file)
    actual_digest = f"sha256:{hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()}"
    if expected_digest.lower() != actual_digest.lower():
        return False, (
            f"host config digest 漂移: expected={expected_digest},"
            f" actual={actual_digest} — "
            f"宿主机 config 文件在部署过程中被修改,违反 R76 P1-04 配置身份绑定"
        )
    return True, ""


def compare_host_config_digest(
    expected_digest: str,
    actual_digest: str,
) -> tuple[bool, str]:
    """R71 P1-05: 比对 host config digest(部署前后)。

    Args:
        expected_digest: 部署前记录的 combined digest
        actual_digest: 部署后计算的 combined digest

    Returns:
        (True, "") 若一致;(False, error_msg) 若漂移
    """
    if not expected_digest:
        return False, "expected_digest 为空(部署前未记录)"
    if not actual_digest:
        return False, "actual_digest 为空(部署后计算失败)"
    if expected_digest.lower() != actual_digest.lower():
        return False, (
            f"host config digest 漂移: expected={expected_digest},"
            f" actual={actual_digest} — "
            f"宿主机 config/groups.yaml 或 config/topology.yaml 在部署过程中"
            f"被修改,违反 P1-05 配置身份绑定"
        )
    return True, ""


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════


def build_runtime_config_binding(
    repo_root: Path,
    image_ref: str = "",
    candidate_manifest_path: Path | None = None,
    expected_registry: str = DEFAULT_EXPECTED_REGISTRY,
    expected_repository: str = DEFAULT_EXPECTED_REPOSITORY,
    pull_and_compare: bool = False,
) -> RuntimeConfigBinding:
    """构建完整的 runtime config binding evidence。

    集成 R71 P1-04 (镜像变量校验) + P1-05 (host config 绑定) + P0-13 (SHA 绑定)。

    Args:
        repo_root: 仓库根目录
        image_ref: TGJIEMA_IMAGE 字符串(若空则从环境变量读取)
        candidate_manifest_path: candidate-manifest.json 路径(可选)
        expected_registry: 预期 registry
        expected_repository: 预期 repository
        pull_and_compare: 若为 True,docker pull + inspect 真实 RepoDigest

    Returns:
        RuntimeConfigBinding 实例(overall_passed 表示校验是否通过)
    """
    if not image_ref:
        image_ref = os.environ.get("TGJIEMA_IMAGE", "")

    binding = RuntimeConfigBinding(
        schema_version=SCHEMA_VERSION,
        tool_version=TOOL_VERSION,
        generated_at=_now_iso(),
        source_sha=_get_source_sha(repo_root),
        workflow_run_id=_get_workflow_run_id(),
        workflow_run_attempt=_get_workflow_run_attempt(),
    )
    binding.overall_passed = True  # 初始为 True,任一检查失败置 False

    # 1. host config digest (P1-05 / R76 P1-04)
    per_file, combined = compute_host_config_digest(repo_root)
    binding.host_config_digests = per_file
    binding.combined_host_config_digest = combined
    # R76 P1-04: 记录 canonical input manifest 供审计与部署端回读
    binding.canonical_input_manifest = build_canonical_input_manifest(per_file)
    # host config 文件缺失不算失败(项目可选),但记录 warning
    for f in per_file:
        if not f.exists:
            logger.info(f"host config 文件不存在(可选): {f.path}")

    # 2. image reference (P1-04)
    if image_ref:
        parsed, errors = validate_image_reference(
            image_ref, expected_registry, expected_repository
        )
        if parsed is None:
            for e in errors:
                binding.add_error(e)
        else:
            binding.image_reference = parsed.raw
            binding.image_registry = parsed.registry
            binding.image_repository = parsed.repository
            binding.image_digest = parsed.digest

            # 3. 与 candidate manifest 比对(P1-04 + P0-13)
            if candidate_manifest_path is not None:
                try:
                    cm = _load_json(candidate_manifest_path)
                    ok, cm_errors = compare_image_with_candidate_manifest(
                        parsed, cm
                    )
                    if not ok:
                        for e in cm_errors:
                            binding.add_error(e)
                except (FileNotFoundError, json.JSONDecodeError) as exc:
                    binding.add_error(
                        f"加载 candidate_manifest 失败: {exc}"
                    )

            # 4. pull + 真实 RepoDigest 比对(P1-04)
            if pull_and_compare:
                repo_digest = pull_and_read_repo_digest(parsed.raw)
                if not repo_digest:
                    binding.add_error(
                        f"无法读取 docker inspect RepoDigest"
                        f"(image={parsed.raw})"
                    )
                else:
                    binding.image_repo_digest = repo_digest
                    # RepoDigest 大小写不敏感比较
                    if repo_digest.lower() != parsed.raw.lower():
                        binding.add_error(
                            f"RepoDigest 不匹配: TGJIEMA_IMAGE="
                            f"{parsed.raw!r}, docker inspect RepoDigest="
                            f"{repo_digest!r}"
                        )
    else:
        binding.add_error("TGJIEMA_IMAGE 环境变量未设置")

    return binding


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Returns:
        0: 校验通过
        1: 校验失败
        2: CLI 参数错误或 IO 错误
    """
    parser = argparse.ArgumentParser(
        description=(
            "R71 P1-04 / P1-05 / P0-13: 镜像变量与运行配置身份绑定校验"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["image-only", "image-verify", "host-config", "full-evidence"],
        default="full-evidence",
        help="校验模式(默认 full-evidence)",
    )
    parser.add_argument(
        "--image",
        default=None,
        help="镜像引用(默认从 TGJIEMA_IMAGE 环境变量读取)",
    )
    parser.add_argument(
        "--expected-registry",
        default=DEFAULT_EXPECTED_REGISTRY,
        help=f"预期 registry(默认 {DEFAULT_EXPECTED_REGISTRY})",
    )
    parser.add_argument(
        "--expected-repository",
        default=DEFAULT_EXPECTED_REPOSITORY,
        help=f"预期 repository(默认 {DEFAULT_EXPECTED_REPOSITORY})",
    )
    parser.add_argument(
        "--candidate-manifest",
        type=Path,
        default=None,
        help="candidate-manifest.json 路径(用于 image-verify 模式)",
    )
    parser.add_argument(
        "--pull-and-compare",
        action="store_true",
        help="docker pull + inspect 真实 RepoDigest 比对",
    )
    parser.add_argument(
        "--expected-host-config-digest",
        default=None,
        help="预期 host config combined digest(用于 host-config 模式)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="仓库根目录(默认脚本所在仓库)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="输出 JSON evidence 文件路径(可选)",
    )
    args = parser.parse_args(argv)

    image_ref = args.image or os.environ.get("TGJIEMA_IMAGE", "")

    # === image-only 模式 ===
    if args.mode == "image-only":
        parsed, errors = validate_image_reference(
            image_ref, args.expected_registry, args.expected_repository
        )
        if parsed is None:
            for e in errors:
                print(f"FAIL: {e}", file=sys.stderr)
            return EXIT_VALIDATION_FAILURE
        print(f"PASS: TGJIEMA_IMAGE 格式合法")
        print(f"  registry:   {parsed.registry}")
        print(f"  repository: {parsed.repository}")
        print(f"  digest:     {parsed.digest}")
        return EXIT_SUCCESS

    # === host-config 模式 ===
    if args.mode == "host-config":
        per_file, combined = compute_host_config_digest(args.repo_root)
        print(f"host config files:")
        for f in per_file:
            status = "OK" if f.exists else "MISSING"
            print(f"  [{status}] {f.path}  sha256={f.sha256}  size={f.size_bytes}")
        print(f"combined digest: {combined}")
        if args.expected_host_config_digest:
            ok, err = compare_host_config_digest(
                args.expected_host_config_digest, combined
            )
            if not ok:
                print(f"FAIL: {err}", file=sys.stderr)
                return EXIT_VALIDATION_FAILURE
            print(f"PASS: host config digest 与预期一致")
        if args.output:
            evidence = {
                "schema_version": SCHEMA_VERSION,
                "tool_version": TOOL_VERSION,
                "generated_at": _now_iso(),
                "host_config_digests": [asdict(f) for f in per_file],
                "combined_host_config_digest": combined,
            }
            args.output.write_text(
                json.dumps(evidence, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"evidence 已写入: {args.output}")
        return EXIT_SUCCESS

    # === image-verify / full-evidence 模式 ===
    binding = build_runtime_config_binding(
        repo_root=args.repo_root,
        image_ref=image_ref,
        candidate_manifest_path=args.candidate_manifest,
        expected_registry=args.expected_registry,
        expected_repository=args.expected_repository,
        pull_and_compare=args.pull_and_compare,
    )

    # 输出结果
    print("=== R71 P1-04 / P1-05 / P0-13: Runtime Config Binding ===")
    print(f"source_sha:               {binding.source_sha}")
    print(f"workflow_run_id:          {binding.workflow_run_id}")
    print(f"workflow_run_attempt:     {binding.workflow_run_attempt}")
    print(f"image_reference:          {binding.image_reference}")
    print(f"image_registry:           {binding.image_registry}")
    print(f"image_repository:          {binding.image_repository}")
    print(f"image_digest:             {binding.image_digest}")
    print(f"image_repo_digest:        {binding.image_repo_digest}")
    print(f"combined_host_config_digest: {binding.combined_host_config_digest}")
    print(f"host_config_digests:")
    for f in binding.host_config_digests:
        status = "OK" if f.exists else "MISSING"
        print(f"  [{status}] {f.path}  sha256={f.sha256}  size={f.size_bytes}")
    print(f"overall_passed:           {binding.overall_passed}")
    if binding.errors:
        print(f"errors ({len(binding.errors)}):")
        for e in binding.errors:
            print(f"  - {e}", file=sys.stderr)

    if args.output:
        args.output.write_text(
            json.dumps(binding.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nevidence 已写入: {args.output}")

    return EXIT_SUCCESS if binding.overall_passed else EXIT_VALIDATION_FAILURE


if __name__ == "__main__":
    sys.exit(main())
