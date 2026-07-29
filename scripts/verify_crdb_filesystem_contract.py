#!/usr/bin/env python3
"""R79 §10.1 / P0-02 / P1-06 — CockroachDB 容器文件系统契约验证。

整改背景:
    R78 曾以 "v24.1 需要可写 workdir/FIFO" 为由删除生产 CRDB 的
    ``read_only: true``,把整个 rootfs 放开为可写 — 属于生产安全回退。
    R79 §10.1 要求恢复 read_only,并建立最小写集合显式契约:
      - 只读: 整个 rootfs
      - tmpfs(易失): /tmp、/cockroach/run(工作目录,server_fifo 等)、
        /cockroach/certs(v24.1 启动时创建/探测)
      - named volume(持久): /cockroach/cockroach-data(数据目录)

本脚本在容器启动后执行,通过机器断言验证契约:
    1. ``docker inspect`` 断言 ReadonlyRootfs=true、tmpfs 三挂载在位、
       cap_drop 含 ALL、no-new-privileges 生效、working_dir=/cockroach/run。
    2. ``docker diff`` 断言 rootfs 零越权写入 — diff 中出现的任何路径
       必须落在允许写集合内(named volume 与 tmpfs 挂载正常情况下不会
       出现在 diff 中;出现即视为可疑并逐条核对前缀)。
    3. 负测: 容器内向 /etc 写入必须失败(read-only file system)。
    4. 全部证据写入 JSON artifact(不得只打印日志)。

退出码:
    0 — 契约满足
    1 — 契约违反(稳定错误码 CRDB_FILESYSTEM_CONTRACT_VIOLATION)
    2 — 前置条件不满足(docker 不可用 / 容器不存在)

用法:
    python scripts/verify_crdb_filesystem_contract.py \
        --container tgjiema-cockroachdb \
        --output artifacts/secretless-e2e/crdb-filesystem-contract.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ════════════════════════════════════════════════════════════════
# 契约常量(R79 §10.1 最小写集合)
# ════════════════════════════════════════════════════════════════

#: 允许写入的路径前缀(named volume + tmpfs)。其余路径必须保持只读。
ALLOWED_WRITE_PREFIXES: tuple[str, ...] = (
    "/cockroach/cockroach-data",  # named volume(持久数据目录)
    "/cockroach/run",             # tmpfs(工作目录,server_fifo 等运行时文件)
    "/cockroach/certs",           # tmpfs(v24.1 启动时创建/探测证书目录)
    "/tmp",                       # tmpfs(临时文件)
)

#: 允许的精确路径(非前缀)。Docker 挂载子路径时父目录元数据自然变更(C),
#: 这是预期行为,不表示 rootfs 被突破。
ALLOWED_EXACT_PATHS: tuple[str, ...] = (
    "/cockroach",  # 父目录 — 子路径挂载时 docker diff 报告 C /cockroach
)

#: 必须存在的 tmpfs 挂载目标
REQUIRED_TMPFS_TARGETS: tuple[str, ...] = (
    "/tmp",
    "/cockroach/run",
    "/cockroach/certs",
)

#: 必须存在的数据卷挂载目标
REQUIRED_VOLUME_TARGET = "/cockroach/cockroach-data"

#: 必需的工作目录
REQUIRED_WORKING_DIR = "/cockroach/run"

#: 稳定错误码
ERROR_CONTRACT_VIOLATION = "CRDB_FILESYSTEM_CONTRACT_VIOLATION"
ERROR_PRECONDITION = "CRDB_FILESYSTEM_CONTRACT_PRECONDITION"


@dataclass
class ContractEvidence:
    """契约验证证据(写入 artifact)。"""

    container: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    checks: list[dict[str, Any]] = field(default_factory=list)
    docker_diff: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    error_code: str | None = None
    verdict: str = "UNKNOWN"

    def add_check(self, name: str, ok: bool, detail: str) -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            self.violations.append(f"{name}: {detail}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "crdb-filesystem-contract/v1",
            "container": self.container,
            "timestamp": self.timestamp,
            "verdict": self.verdict,
            "error_code": self.error_code,
            "violations": self.violations,
            "checks": self.checks,
            "docker_diff": self.docker_diff,
            "allowed_write_prefixes": list(ALLOWED_WRITE_PREFIXES),
        }


# ════════════════════════════════════════════════════════════════
# docker diff 解析(纯函数,可单测)
# ════════════════════════════════════════════════════════════════

def parse_docker_diff(output: str) -> list[tuple[str, str]]:
    """解析 ``docker diff`` 输出为 (change_kind, path) 列表。

    docker diff 每行格式: ``A /path``(新增)、``C /path``(变更)、``D /path``(删除)。
    """
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in ("A", "C", "D"):
            entries.append((parts[0], parts[1].strip()))
    return entries


def find_diff_violations(
    entries: list[tuple[str, str]],
    allowed_prefixes: tuple[str, ...] = ALLOWED_WRITE_PREFIXES,
) -> list[str]:
    """找出落在允许写集合之外的 diff 条目。

    任何 A/C(新增/变更)条目若不在 allowed_prefixes 之下,即违反
    "rootfs 只读 + 最小写集合"契约。D(删除)条目同样视为越权 —
    只读 rootfs 上不应发生任何删除。

    R80: 同时检查 ALLOWED_EXACT_PATHS(精确匹配,非前缀),
    允许父目录元数据变更(如 C /cockroach)。
    """
    violations: list[str] = []
    for kind, path in entries:
        normalized = path.rstrip("/") or "/"
        # 精确路径允许(父目录元数据变更)
        if normalized in ALLOWED_EXACT_PATHS:
            continue
        if any(
            normalized == prefix or normalized.startswith(prefix + "/")
            for prefix in allowed_prefixes
        ):
            continue
        violations.append(f"{kind} {path} — 不在允许写集合 {list(allowed_prefixes)} 内")
    return violations


# ════════════════════════════════════════════════════════════════
# docker inspect 断言
# ════════════════════════════════════════════════════════════════

def verify_inspect_contract(inspect_doc: dict[str, Any], evidence: ContractEvidence) -> None:
    """基于 ``docker inspect`` JSON 断言静态契约。"""
    host_config = inspect_doc.get("HostConfig", {}) or {}
    config = inspect_doc.get("Config", {}) or {}

    evidence.add_check(
        "readonly_rootfs",
        bool(host_config.get("ReadonlyRootfs")),
        f"ReadonlyRootfs={host_config.get('ReadonlyRootfs')}",
    )

    tmpfs = host_config.get("Tmpfs", {}) or {}
    for target in REQUIRED_TMPFS_TARGETS:
        evidence.add_check(
            f"tmpfs_{target}",
            target in tmpfs,
            f"tmpfs 挂载={sorted(tmpfs.keys())}",
        )

    mounts = inspect_doc.get("Mounts", []) or []
    volume_targets = {m.get("Destination") for m in mounts if m.get("Type") == "volume"}
    evidence.add_check(
        f"volume_{REQUIRED_VOLUME_TARGET}",
        REQUIRED_VOLUME_TARGET in volume_targets,
        f"volume 挂载={sorted(t for t in volume_targets if t)}",
    )

    cap_drop = host_config.get("CapDrop", []) or []
    evidence.add_check(
        "cap_drop_all",
        "ALL" in cap_drop,
        f"CapDrop={cap_drop}",
    )

    sec_opt = host_config.get("SecurityOpt", []) or []
    evidence.add_check(
        "no_new_privileges",
        "no-new-privileges:true" in sec_opt,
        f"SecurityOpt={sec_opt}",
    )

    evidence.add_check(
        "working_dir",
        config.get("WorkingDir") == REQUIRED_WORKING_DIR,
        f"WorkingDir={config.get('WorkingDir')!r}(要求 {REQUIRED_WORKING_DIR})",
    )


# ════════════════════════════════════════════════════════════════
# docker 命令封装
# ════════════════════════════════════════════════════════════════

def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except FileNotFoundError:
        return 127, "docker not found in PATH"
    except subprocess.TimeoutExpired:
        return 124, f"command timed out: {' '.join(cmd)}"


def collect_and_verify(container: str) -> ContractEvidence:
    """对运行中的容器执行完整契约验证。"""
    evidence = ContractEvidence(container=container)

    # 1. docker inspect
    rc, out = _run(["docker", "inspect", container])
    if rc != 0:
        evidence.error_code = ERROR_PRECONDITION
        evidence.violations.append(f"docker inspect 失败(rc={rc}): {out[:300]}")
        evidence.verdict = "PRECONDITION_FAILED"
        return evidence
    try:
        inspect_list = json.loads(out)
        inspect_doc = inspect_list[0]
    except (json.JSONDecodeError, IndexError, KeyError) as exc:
        evidence.error_code = ERROR_PRECONDITION
        evidence.violations.append(f"docker inspect 输出解析失败: {exc}")
        evidence.verdict = "PRECONDITION_FAILED"
        return evidence

    verify_inspect_contract(inspect_doc, evidence)

    # 2. docker diff — rootfs 越权写入断言
    rc, out = _run(["docker", "diff", container])
    if rc != 0:
        evidence.add_check("docker_diff", False, f"docker diff 失败(rc={rc}): {out[:300]}")
    else:
        entries = parse_docker_diff(out)
        evidence.docker_diff = [f"{k} {p}" for k, p in entries]
        diff_violations = find_diff_violations(entries)
        for v in diff_violations:
            evidence.violations.append(f"docker_diff: {v}")
        evidence.add_check(
            "docker_diff_within_allowed_write_set",
            not diff_violations,
            f"diff 条目数={len(entries)},越权条目数={len(diff_violations)}",
        )

    # 3. 负测: /etc 写入必须失败(read-only rootfs)
    rc, out = _run(
        ["docker", "exec", container, "sh", "-c",
         "touch /etc/.crdb-contract-violation-probe"],
        timeout=15,
    )
    evidence.add_check(
        "negative_write_etc_must_fail",
        rc != 0,
        f"touch /etc rc={rc}(必须非 0){(': ' + out[:200]) if out.strip() else ''}",
    )

    # 4. 正测: 数据目录写入必须成功(named volume 可写)
    rc, out = _run(
        ["docker", "exec", container, "sh", "-c",
         "touch /cockroach/cockroach-data/.crdb-contract-probe && "
         "rm -f /cockroach/cockroach-data/.crdb-contract-probe"],
        timeout=15,
    )
    evidence.add_check(
        "positive_write_data_volume_must_succeed",
        rc == 0,
        f"touch+rm /cockroach/cockroach-data rc={rc}(必须为 0)"
        f"{(': ' + out[:200]) if out.strip() else ''}",
    )

    evidence.error_code = ERROR_CONTRACT_VIOLATION if evidence.violations else None
    evidence.verdict = "PASS" if not evidence.violations else "FAIL"
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="tgjiema-cockroachdb",
                        help="目标容器名(默认 tgjiema-cockroachdb)")
    parser.add_argument("--output", type=Path,
                        default=Path("artifacts/secretless-e2e/crdb-filesystem-contract.json"),
                        help="证据 artifact 输出路径")
    args = parser.parse_args(argv)

    evidence = collect_and_verify(args.container)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(evidence.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    for check in evidence.checks:
        mark = "✓" if check["ok"] else "✗"
        print(f"{mark} {check['name']}: {check['detail']}")

    if evidence.verdict == "PASS":
        print(f"CRDB filesystem contract PASS — evidence: {args.output}")
        return 0
    if evidence.verdict == "PRECONDITION_FAILED":
        print(f"{ERROR_PRECONDITION}: {evidence.violations}", file=sys.stderr)
        return 2
    print(f"{ERROR_CONTRACT_VIOLATION}: {evidence.violations}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
