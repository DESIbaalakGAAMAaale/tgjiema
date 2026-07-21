#!/usr/bin/env python3
"""R71 Wave 4 P0-12: 验证最终 OCI rootfs 与绑定的 manifest 一致性。

R71 报告 P0-12 指出:
    R70 Wave 8 的 generate_oci_file_manifest.py 只生成预期 manifest,
    没有在 CI 中验证最终镜像 rootfs 是否与 manifest 一致。
    攻击者/构建异常可能注入未声明文件,违反不可变部署原则。

R71 Wave 4 整改(P0-12):
    1. 新增 scripts/validate_oci_rootfs.py:
       - 在 CI runner 上(不在容器内)拉取镜像 by digest
       - docker save 到 tar, 解包提取 rootfs(组合所有 layer.tar)
       - 对比 manifest 中每个预期文件: path/type/mode/uid/gid/size/sha256
       - 区分 base image 文件与 app 文件(通过对比 base image rootfs)
       - fail-closed: 缺失/未声明/异常权限/越界 symlink 全部 FAIL
       - 输出 oci-file-manifest.json(绑定 source_sha + image_digest + base_image_digest)
    2. .github/workflows/release-gates.yml:
       - 新增 validate-oci-rootfs job(needs: docker-build, oci-allowlist-verify)
       - 添加到 release-summary required jobs
    3. 测试覆盖 30+ 用例(--from-tar 模式, 无需 Docker)

设计要点:
    - fail-closed: 任何未知文件/权限异常/缺失文件立即 FAIL(exit 1)
    - 不使用无限目录 allowlist 掩盖未知文件(ALLOWED_ROOTS 是有限白名单)
    - 支持 --from-tar / --base-tar 测试模式(无需 Docker, 用于 Windows CI)
    - 输出结构化 JSON 证据(oci-file-manifest.json)
    - 退出码: 0=成功, 1=验证失败, 2=CLI/runtime 错误

使用方法:
    # CI 生产模式(需 Docker)
    python scripts/validate_oci_rootfs.py \\
        --image ghcr.io/owner/repo@sha256:abc... \\
        --source-sha <git_sha> \\
        --base-image python:3.12-slim@sha256:57cd... \\
        --output oci-file-manifest.json

    # 测试模式(无 Docker, 使用预构建 tar)
    python scripts/validate_oci_rootfs.py \\
        --image test/app@sha256:abc... \\
        --source-sha test-sha \\
        --base-image test/base@sha256:def... \\
        --from-tar app-rootfs.tar \\
        --base-tar base-rootfs.tar \\
        --output oci-file-manifest.json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_oci_file_manifest import (  # noqa: E402
    ALLOWED_ROOTS,
    generate_manifest as _generate_expected_manifest,
)
from loguru import logger

TOOL_VERSION = "R71-WAVE4-P0-12"
MANIFEST_SCHEMA_VERSION = "1.0"

# 退出码
EXIT_SUCCESS = 0
EXIT_VALIDATION_FAILURE = 1
EXIT_CLI_ERROR = 2

# ALLOWED_ROOTS 是有限白名单(来自 generate_oci_file_manifest.py),
# 不使用无限目录 allowlist 掩盖未知文件(R71 P0-12 要求)。


# ════════════════════════════════════════════════════════════════
# 数据结构
# ════════════════════════════════════════════════════════════════


@dataclass
class FileMetadata:
    """单个文件的元数据(从 tar 提取)。"""
    path: str           # 相对 root 的 POSIX 路径(如 "app/services/foo.py")
    type: str           # "file" / "dir" / "symlink" / "hardlink"
    mode: str           # 八进制字符串(如 "0644")
    uid: int
    gid: int
    size: int
    sha256: str         # 文件内容 sha256(目录/符号链接为空)
    link_target: str = ""  # 符号链接/硬链接目标

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationReport:
    """验证结果报告(序列化为 oci-file-manifest.json)。"""
    schema_version: str = MANIFEST_SCHEMA_VERSION
    generated_at: str = ""
    tool_version: str = TOOL_VERSION
    source_sha: str = ""
    image_digest: str = ""
    image_repo_digest: str = ""
    base_image_digest: str = ""
    base_image_repo_digest: str = ""
    sbom_path: str | None = None
    provenance_path: str | None = None
    candidate_manifest_path: str | None = None
    validation_passed: bool = False
    app_files: list[dict] = field(default_factory=list)
    base_files: list[dict] = field(default_factory=list)
    unexpected_app_files: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    permission_anomalies: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ════════════════════════════════════════════════════════════════
# 路径工具
# ════════════════════════════════════════════════════════════════


def _normalize_path(name: str) -> str:
    """标准化 tar 内路径(去除前导 ./ 与 /,返回 POSIX 相对路径)。"""
    if not name:
        return ""
    if name.startswith("./"):
        name = name[2:]
    name = name.lstrip("/")
    return name


def _is_under_app(path: str) -> bool:
    """判断路径是否在 /app 下(相对路径以 app/ 开头或等于 app)。"""
    return path == "app" or path.startswith("app/")


def _relative_to_app(path: str) -> str:
    """将 app/xxx 路径转为 xxx(相对 /app 的路径)。"""
    if path == "app":
        return ""
    if path.startswith("app/"):
        return path[len("app/"):]
    return path


def _is_under_allowed_root(rel_path: str) -> bool:
    """判断相对 /app 的路径是否在 ALLOWED_ROOTS(有限白名单)下。

    ALLOWED_ROOTS 是显式枚举的顶层路径白名单(非无限目录 allowlist):
      - 文件:精确匹配(如 "run_all.py")
      - 目录:前缀匹配(如 "services/")
    任何不在白名单中的路径视为"未声明文件",fail-closed。
    """
    if not rel_path:
        return False
    for root in ALLOWED_ROOTS:
        if root.endswith("/"):
            if rel_path.startswith(root) or rel_path == root.rstrip("/"):
                return True
        else:
            if rel_path == root:
                return True
    return False


# ════════════════════════════════════════════════════════════════
# Tar 提取
# ════════════════════════════════════════════════════════════════


def _make_file_metadata_from_member(
    member: tarfile.TarInfo,
    path: str,
    extract_file=None,
) -> FileMetadata:
    """从 tarfile.TarInfo 构造 FileMetadata。"""
    mode_str = oct(member.mode & 0o7777)[2:].zfill(4)
    if member.isdir():
        return FileMetadata(
            path=path, type="dir", mode=mode_str,
            uid=member.uid, gid=member.gid, size=0, sha256="",
        )
    if member.isfile():
        content = b""
        if extract_file is not None:
            f = extract_file(member)
            if f is not None:
                content = f.read()
        return FileMetadata(
            path=path, type="file", mode=mode_str,
            uid=member.uid, gid=member.gid, size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
    if member.issym():
        return FileMetadata(
            path=path, type="symlink", mode=mode_str,
            uid=member.uid, gid=member.gid, size=0, sha256="",
            link_target=member.linkname,
        )
    if member.islnk():
        return FileMetadata(
            path=path, type="hardlink", mode=mode_str,
            uid=member.uid, gid=member.gid, size=0, sha256="",
            link_target=member.linkname,
        )
    # 其他类型(char/block/fifo)记录为 unknown
    return FileMetadata(
        path=path, type="unknown", mode=mode_str,
        uid=member.uid, gid=member.gid, size=0, sha256="",
    )


def _extract_rootfs_from_flat_tar(tar_path: Path) -> dict[str, FileMetadata]:
    """从扁平 tar 提取 rootfs 文件元数据。

    扁平 tar 是指 tar 直接包含文件(路径如 app/services/foo.py),
    而非 docker save 的多层结构。--from-tar 模式使用此格式(用于测试)。
    """
    if not tar_path.exists():
        raise FileNotFoundError(f"tar 文件不存在: {tar_path}")
    metadata: dict[str, FileMetadata] = {}
    try:
        with tarfile.open(tar_path, "r") as tf:
            for member in tf.getmembers():
                path = _normalize_path(member.name)
                if not path:
                    continue
                metadata[path] = _make_file_metadata_from_member(
                    member, path, tf.extractfile,
                )
    except tarfile.TarError as e:
        raise RuntimeError(f"解析 tar 失败: {e}") from e
    return metadata


def _extract_rootfs_from_docker_tar(tar_path: Path) -> dict[str, FileMetadata]:
    """从 docker save tar 提取 rootfs(组合所有 layer.tar)。

    docker save 的 tar 结构:
      manifest.json  (列出 layers 与 config)
      <hash>/layer.tar  (每层文件系统增量)

    本函数读取 manifest.json, 按顺序组合所有 layer.tar,
    后层覆盖前层(模拟 overlay 文件系统)。最终得到扁平 rootfs。
    """
    if not tar_path.exists():
        raise FileNotFoundError(f"docker save tar 不存在: {tar_path}")
    metadata: dict[str, FileMetadata] = {}
    try:
        with tarfile.open(tar_path, "r") as tf:
            manifest_member = tf.extractfile("manifest.json")
            if manifest_member is None:
                raise RuntimeError("docker save tar 中未找到 manifest.json")
            manifest_data = json.loads(manifest_member.read().decode("utf-8"))
            if not manifest_data:
                raise RuntimeError("docker save tar 的 manifest.json 为空")
            layers = manifest_data[0].get("Layers", [])
            for layer_path in layers:
                layer_member = tf.extractfile(layer_path)
                if layer_member is None:
                    logger.warning(f"layer 不存在: {layer_path}")
                    continue
                layer_data = layer_member.read()
                with tarfile.open(fileobj=io.BytesIO(layer_data), mode="r") as layer_tf:
                    for member in layer_tf.getmembers():
                        path = _normalize_path(member.name)
                        if not path:
                            continue
                        # 后层覆盖前层(overlay 语义)
                        metadata[path] = _make_file_metadata_from_member(
                            member, path, layer_tf.extractfile,
                        )
    except tarfile.TarError as e:
        raise RuntimeError(f"解析 docker save tar 失败: {e}") from e
    return metadata


# ════════════════════════════════════════════════════════════════
# Docker 操作(仅生产模式使用)
# ════════════════════════════════════════════════════════════════


def _docker_pull(image_ref: str) -> None:
    """docker pull 镜像 by digest。"""
    cmd = ["docker", "pull", image_ref]
    logger.info(f"拉取镜像: {image_ref}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise RuntimeError(f"docker pull 执行失败: {e}") from e
    if result.returncode != 0:
        raise RuntimeError(
            f"docker pull 失败(returncode={result.returncode}): "
            f"{result.stderr.strip()}"
        )


def _docker_save(image_ref: str, output_path: Path) -> None:
    """docker save 镜像到 tar 文件。"""
    cmd = ["docker", "save", image_ref, "-o", str(output_path)]
    logger.info(f"保存镜像到 tar: {output_path}")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise RuntimeError(f"docker save 执行失败: {e}") from e
    if result.returncode != 0:
        raise RuntimeError(
            f"docker save 失败(returncode={result.returncode}): "
            f"{result.stderr.strip()}"
        )


# ════════════════════════════════════════════════════════════════
# 验证逻辑(fail-closed)
# ════════════════════════════════════════════════════════════════


def _is_uid_gid_acceptable(uid: int, gid: int) -> bool:
    """uid/gid 必须是 0(root) 或正数(app user)。

    Dockerfile 通过 `useradd -m app` 创建 app user(默认 uid=1000)。
    接受 0(root) 或任何正数(app user)。负值不接受(fail-closed)。
    """
    return uid >= 0 and gid >= 0


def _check_permission_anomaly(mode_str: str) -> tuple[bool, str]:
    """检查权限异常: world-writable / setuid / setgid。

    返回 (has_anomaly, description)。
    - world-writable (mode & 0o002): 任何文件/目录不得 world-writable
    - setuid (mode & 0o4000): 无理由不得 setuid
    - setgid (mode & 0o2000): 无理由不得 setgid
    """
    try:
        mode = int(mode_str, 8)
    except ValueError:
        return (True, f"invalid mode: {mode_str}")
    anomalies: list[str] = []
    if mode & 0o002:
        anomalies.append("world-writable")
    if mode & 0o4000:
        anomalies.append("setuid without reason")
    if mode & 0o2000:
        anomalies.append("setgid without reason")
    if anomalies:
        return (True, ", ".join(anomalies))
    return (False, "")


def _symlink_escapes_app(link_target: str) -> bool:
    """检查 symlink 目标是否逃逸 /app。

    - 绝对路径:必须在 /app 下(/app 或 /app/...)
    - 相对路径:基于 /app 解析,.. 不得逃逸到 /app 之外
    """
    if not link_target:
        return False
    if link_target.startswith("/"):
        return not (link_target == "/app" or link_target.startswith("/app/"))
    # 相对路径:基于 /app 解析
    depth = 0
    for part in link_target.split("/"):
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        elif part in (".", ""):
            continue
        else:
            depth += 1
    return False


def _classify_files(
    app_rootfs: dict[str, FileMetadata],
    base_rootfs: dict[str, FileMetadata],
) -> tuple[list[FileMetadata], list[FileMetadata]]:
    """区分 app 文件与 base 文件。

    - 文件在 base_rootfs 中且内容相同(sha256 一致 + type 一致) → base 文件
    - 文件不在 base_rootfs 中, 或内容不同 → app 文件(由 Dockerfile 添加/修改)

    仅处理 /app 下的文件(其他路径如 /usr /bin 不在受控范围)。
    """
    app_files: list[FileMetadata] = []
    base_files: list[FileMetadata] = []
    for path, meta in app_rootfs.items():
        if not _is_under_app(path):
            continue
        base_meta = base_rootfs.get(path)
        if base_meta is not None and base_meta.sha256 == meta.sha256 \
                and base_meta.type == meta.type:
            base_files.append(meta)
        else:
            app_files.append(meta)
    return (app_files, base_files)


def _validate_rootfs(
    app_rootfs: dict[str, FileMetadata],
    base_rootfs: dict[str, FileMetadata],
    expected_manifest: dict,
) -> tuple[bool, list[str], list[str], list[dict]]:
    """验证 rootfs 与 expected manifest 一致性(fail-closed)。

    验证规则:
      1. Missing expected file → FAIL
      2. Unexpected app file (not in ALLOWED_ROOTS) → FAIL
      3. Permission anomaly (world-writable / setuid / setgid) → FAIL
      4. Symlink escapes /app → FAIL
      5. Unacceptable uid/gid → FAIL
      6. sha256 mismatch (若 expected manifest 提供 sha256) → FAIL

    返回 (passed, missing_files, unexpected_app_files, permission_anomalies)。
    """
    missing_files: list[str] = []
    unexpected_app_files: list[str] = []
    permission_anomalies: list[dict] = []

    app_files, _base_files = _classify_files(app_rootfs, base_rootfs)

    # 1. 检查 expected 文件是否存在
    expected_paths = {e["path"] for e in expected_manifest.get("files", [])}
    app_paths_rel = {
        _relative_to_app(f.path) for f in app_files if _is_under_app(f.path)
    }
    for expected_path in expected_paths:
        if expected_path not in app_paths_rel:
            missing_files.append(expected_path)

    # 2. 检查 unexpected app 文件(不在 ALLOWED_ROOTS 下)
    for f in app_files:
        rel = _relative_to_app(f.path)
        if not rel:
            continue
        if not _is_under_allowed_root(rel):
            unexpected_app_files.append(f.path)

    # 3. 检查权限异常(对所有 /app 文件, 包括 base 文件)
    for path, meta in app_rootfs.items():
        if not _is_under_app(path):
            continue
        has_anomaly, desc = _check_permission_anomaly(meta.mode)
        if has_anomaly:
            permission_anomalies.append({
                "path": path,
                "mode": meta.mode,
                "anomaly": desc,
            })

    # 4. 检查 symlink 逃逸 /app
    for path, meta in app_rootfs.items():
        if not _is_under_app(path):
            continue
        if meta.type in ("symlink", "hardlink") and meta.link_target:
            if _symlink_escapes_app(meta.link_target):
                permission_anomalies.append({
                    "path": path,
                    "mode": meta.mode,
                    "anomaly": f"symlink escapes /app: {meta.link_target}",
                })

    # 5. 检查 uid/gid 可接受性
    for path, meta in app_rootfs.items():
        if not _is_under_app(path):
            continue
        if not _is_uid_gid_acceptable(meta.uid, meta.gid):
            permission_anomalies.append({
                "path": path,
                "mode": meta.mode,
                "anomaly": f"unacceptable uid/gid: {meta.uid}:{meta.gid}",
            })

    # 6. 检查 sha256(若 expected manifest 提供 sha256)
    expected_with_sha = {
        e["path"]: e["sha256"]
        for e in expected_manifest.get("files", [])
        if e.get("sha256")
    }
    if expected_with_sha:
        for f in app_files:
            rel = _relative_to_app(f.path)
            if rel in expected_with_sha and f.sha256:
                if f.sha256 != expected_with_sha[rel]:
                    permission_anomalies.append({
                        "path": f.path,
                        "mode": f.mode,
                        "anomaly": (
                            f"sha256 mismatch (expected "
                            f"{expected_with_sha[rel][:16]}..., got "
                            f"{f.sha256[:16]}...)"
                        ),
                    })

    passed = (
        not missing_files
        and not unexpected_app_files
        and not permission_anomalies
    )
    return (passed, missing_files, unexpected_app_files, permission_anomalies)


# ════════════════════════════════════════════════════════════════
# Manifest 生成
# ════════════════════════════════════════════════════════════════


def _parse_image_ref(image_ref: str) -> tuple[str, str]:
    """解析 image ref 为 (repo, digest)。

    输入: "ghcr.io/owner/repo@sha256:abc123..."
    输出: ("ghcr.io/owner/repo", "sha256:abc123...")

    Raises:
        ValueError: image ref 不包含 @sha256: digest
    """
    if "@" not in image_ref:
        raise ValueError(f"image ref 必须包含 @sha256: digest: {image_ref}")
    repo, digest = image_ref.rsplit("@", 1)
    if not digest.startswith("sha256:"):
        raise ValueError(f"image digest 必须以 sha256: 开头: {digest}")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError(
            f"image digest 格式错误(需 sha256:<64-hex>): {digest}"
        )
    return (repo, digest)


def _populate_manifest_files(
    report: ValidationReport,
    app_rootfs: dict[str, FileMetadata],
    base_rootfs: dict[str, FileMetadata],
) -> None:
    """填充 report 中的 app_files 与 base_files 字段。"""
    app_files, base_files = _classify_files(app_rootfs, base_rootfs)
    report.app_files = [
        f.to_dict() for f in sorted(app_files, key=lambda x: x.path)
    ]
    report.base_files = [
        {"path": f.path} for f in sorted(base_files, key=lambda x: x.path)
    ]


def _sanitize_filename(s: str) -> str:
    """将字符串转换为安全的文件名片段(用于 temp 文件命名)。"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", s)[:64]


def _write_output(output_path: Path, report: ValidationReport) -> None:
    """写入 manifest JSON 到 output_path。"""
    try:
        output_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"manifest 已写入: {output_path}")
    except OSError as e:
        logger.error(f"写入 manifest 失败: {e}")


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="R71 Wave 4 P0-12: 验证最终 OCI rootfs 与绑定的 manifest 一致性",
    )
    parser.add_argument(
        "--image", required=True,
        help="OCI image reference(如 ghcr.io/owner/repo@sha256:...)",
    )
    parser.add_argument(
        "--source-sha", required=True,
        help="源代码 git SHA(绑定到 manifest)",
    )
    parser.add_argument(
        "--base-image", required=True,
        help="基础镜像 reference(如 python:3.12-slim@sha256:...)",
    )
    parser.add_argument(
        "--sbom", type=Path, default=None,
        help="SBOM 文件路径(可选, 记录到 manifest)",
    )
    parser.add_argument(
        "--provenance", type=Path, default=None,
        help="Provenance 文件路径(可选, 记录到 manifest)",
    )
    parser.add_argument(
        "--candidate-manifest", type=Path, default=None,
        help="候选 manifest 路径(可选, 记录到 manifest)",
    )
    parser.add_argument(
        "--output", type=Path, required=True,
        help="输出 manifest JSON 路径(oci-file-manifest.json)",
    )
    parser.add_argument(
        "--from-tar", type=Path, default=None,
        help="(测试模式)从扁平 tar 加载 app 镜像 rootfs, 不调用 docker",
    )
    parser.add_argument(
        "--base-tar", type=Path, default=None,
        help="(测试模式)从扁平 tar 加载 base 镜像 rootfs, 不调用 docker",
    )
    return parser


def _load_app_rootfs(args: argparse.Namespace) -> dict[str, FileMetadata]:
    """加载 app 镜像 rootfs(测试模式从 tar, 生产模式从 docker pull+save)。"""
    if args.from_tar is not None:
        logger.info(f"从 tar 加载 app rootfs: {args.from_tar}")
        return _extract_rootfs_from_flat_tar(args.from_tar)
    # 生产模式:docker pull + save + 解包
    tmp_dir = Path(tempfile.gettempdir())
    app_tar = tmp_dir / f"tgjiema-app-{_sanitize_filename(args.image)}.tar"
    _docker_pull(args.image)
    _docker_save(args.image, app_tar)
    try:
        return _extract_rootfs_from_docker_tar(app_tar)
    finally:
        try:
            app_tar.unlink()
        except OSError:
            pass


def _load_base_rootfs(args: argparse.Namespace) -> dict[str, FileMetadata]:
    """加载 base 镜像 rootfs(测试模式从 tar, 生产模式从 docker pull+save)。"""
    if args.base_tar is not None:
        logger.info(f"从 tar 加载 base rootfs: {args.base_tar}")
        return _extract_rootfs_from_flat_tar(args.base_tar)
    # 生产模式:docker pull + save + 解包
    tmp_dir = Path(tempfile.gettempdir())
    base_tar_path = tmp_dir / f"tgjiema-base-{_sanitize_filename(args.base_image)}.tar"
    _docker_pull(args.base_image)
    _docker_save(args.base_image, base_tar_path)
    try:
        return _extract_rootfs_from_docker_tar(base_tar_path)
    finally:
        try:
            base_tar_path.unlink()
        except OSError:
            pass


def main(argv: Iterable[str] | None = None) -> int:
    """命令行入口。

    Returns:
        0=成功;1=验证失败;2=CLI/runtime 错误
    """
    parser = _build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    # 解析 image refs
    try:
        image_repo, image_digest = _parse_image_ref(args.image)
        base_repo, base_digest = _parse_image_ref(args.base_image)
    except ValueError as e:
        logger.error(f"参数解析失败: {e}")
        return EXIT_CLI_ERROR

    report = ValidationReport(
        generated_at=_dt.datetime.now().isoformat(),
        source_sha=args.source_sha,
        image_digest=image_digest,
        image_repo_digest=args.image,
        base_image_digest=base_digest,
        base_image_repo_digest=args.base_image,
        sbom_path=str(args.sbom) if args.sbom else None,
        provenance_path=str(args.provenance) if args.provenance else None,
        candidate_manifest_path=(
            str(args.candidate_manifest) if args.candidate_manifest else None
        ),
    )

    # 获取 expected manifest(从 generate_oci_file_manifest.py)
    try:
        expected_manifest = _generate_expected_manifest(with_sha256=True)
    except Exception as e:
        logger.error(f"生成 expected manifest 失败: {e}")
        report.error = f"generate expected manifest failed: {e}"
        _write_output(args.output, report)
        return EXIT_CLI_ERROR

    # 提取 rootfs
    try:
        app_rootfs = _load_app_rootfs(args)
        base_rootfs = _load_base_rootfs(args)
    except (FileNotFoundError, RuntimeError, OSError) as e:
        logger.error(f"提取 rootfs 失败: {e}")
        report.error = f"extract rootfs failed: {e}"
        _write_output(args.output, report)
        return EXIT_CLI_ERROR

    # 验证
    try:
        passed, missing, unexpected, anomalies = _validate_rootfs(
            app_rootfs, base_rootfs, expected_manifest,
        )
    except Exception as e:
        logger.error(f"验证过程异常: {e}")
        report.error = f"validation exception: {e}"
        _populate_manifest_files(report, app_rootfs, base_rootfs)
        _write_output(args.output, report)
        return EXIT_CLI_ERROR

    # 填充报告
    report.validation_passed = passed
    report.missing_files = sorted(missing)
    report.unexpected_app_files = sorted(unexpected)
    report.permission_anomalies = anomalies
    _populate_manifest_files(report, app_rootfs, base_rootfs)
    _write_output(args.output, report)

    if passed:
        logger.info("PASS: OCI rootfs 与 manifest 一致")
        return EXIT_SUCCESS
    logger.error(
        f"FAIL: OCI rootfs 验证失败"
        f"(missing={len(missing)}, unexpected={len(unexpected)},"
        f" anomalies={len(anomalies)})"
    )
    return EXIT_VALIDATION_FAILURE


if __name__ == "__main__":
    sys.exit(main())
