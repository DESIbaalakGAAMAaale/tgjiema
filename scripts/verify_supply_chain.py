#!/usr/bin/env python3
"""R64 P1-12: 相同 digest 供应链验证脚本。

验证发布供应链中所有 digest 相互绑定且一致:
    1. commit_sha — git commit SHA(源代码版本)
    2. tree_sha — git tree SHA(源代码内容地址)
    3. image_digest — OCI 镜像 digest(构建产物内容地址)
    4. sbom_digest — SBOM 文件 SHA-256(依赖清单)
    5. migration_digest — migration SQL 文件 SHA-256(数据库 schema 版本)
    6. config_digest — 关键配置文件 SHA-256(docker-compose.yml / Dockerfile / requirements.txt)

验证项:
    - release_attestation.json 中 6 digest 均已绑定(非空、非 pending)
    - 镜像 digest 已被 cosign 签名(可选,需要 cosign 在 PATH)
    - SBOM digest 与实际 sbom.json 一致(若文件可用)
    - migration digest 与实际 migration 文件一致
    - config digest 与实际配置文件一致
    - commit_sha 与 git HEAD 一致

使用方法:
    # 验证当前 workspace 中的 release_attestation.json
    python scripts/verify_supply_chain.py

    # 指定 attestation 文件路径
    python scripts/verify_supply_chain.py --attestation /path/to/release_attestation.json

    # 指定输出目录(生成结构化 JSON 报告)
    python scripts/verify_supply_chain.py --output-dir production-evidence/

    # JSON 输出模式(适合 CI 解析)
    python scripts/verify_supply_chain.py --json

    # 跳过 cosign 验证(CI 无 cosign 时)
    python scripts/verify_supply_chain.py --skip-cosign

退出码:
    0: 所有验证通过
    1: 至少一项验证失败
    2: 参数错误或环境不可用
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ─── R64 P1-12: 6 digest 字段定义 ──────────────────────────
REQUIRED_DIGESTS = [
    "commit_sha",
    "tree_sha",
    "image_digest",
    "sbom_digest",
    "migration_digest",
    "config_digest",
]

# 关键配置文件(用于计算 config_digest)
CONFIG_FILES = [
    "docker-compose.yml",
    "Dockerfile",
    "requirements.txt",
    ".env.shared.example",
]

SCHEMA_VERSION = "r64_p1_12_v1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    """计算单个文件的 SHA-256。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_files_concat(paths: list[Path]) -> str:
    """计算多个文件 SHA-256 的合并 digest(与 release-gates.yml 算法一致)。

    算法:逐个文件 sha256sum → 拼接字符串 → 再 sha256sum
    """
    concat = ""
    for p in paths:
        if p.exists():
            concat += _sha256_file(p)
    if not concat:
        return ""
    return hashlib.sha256(concat.encode()).hexdigest()


def _sha256_dir_tree(glob_pattern: str, base_dir: Path) -> str:
    """计算匹配 glob 的所有文件的合并 SHA-256(仅用于非 migration 场景)。

    注意:此算法与 release-gates.yml publish-attestation 步骤中 migration_digest 的
    算法不一致(workflow 用 find . -name '*.sql' -path '*/migrations/*' 匹配所有
    migrations 子目录,本函数只匹配 base_dir 下直接子文件)。
    migration_digest 验证请使用 _migration_digest_workflow_algorithm()。
    """
    matches = sorted(base_dir.glob(glob_pattern))
    if not matches:
        return ""
    lines = []
    for m in matches:
        digest = _sha256_file(m)
        lines.append(f"{digest}  {m.relative_to(base_dir)}")
    content = "\n".join(lines)
    return hashlib.sha256(content.encode()).hexdigest()


def _migration_digest_workflow_algorithm() -> str:
    """计算 migration_digest,与 release-gates.yml publish-attestation 步骤算法 bit-for-bit 一致。

    R65 fix: workflow 使用 shell 命令计算 migration_digest:
        find . -name "*.sql" -path "*/migrations/*" -exec sha256sum {} + 2>/dev/null \\
          | sort | sha256sum | awk '{print $1}'

    该命令:
      1. 匹配所有路径含 /migrations/ 的 .sql 文件(如 database/migrations/*.sql
         和 admin/migrations/*.sql),而非仅 database/migrations/ 直接子文件
      2. sha256sum 输出格式为 "<hash>  ./path/file.sql"(带 ./ 前缀和完整相对路径)
      3. sort 对 "<hash>  path" 行做字典序排序(先按 hash 排序)
      4. sha256sum 对排序后的文本(含每行末尾换行符)做最终哈希

    本函数通过 subprocess 调用相同 shell 命令,确保与 workflow 生成端完全一致,
    避免 Python 重写算法时因路径格式/排序/newline 差异导致永久 mismatch。
    """
    try:
        result = subprocess.run(
            ["bash", "-c",
             "find . -name '*.sql' -path '*/migrations/*' -exec sha256sum {} + 2>/dev/null "
             "| sort | sha256sum | awk '{print $1}'"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as _e:
        print(f"[WARN] _migration_digest_workflow_algorithm: subprocess failed: {_e}", file=sys.stderr)
    return ""


def _git_rev_parse(ref: str) -> str:
    """执行 git rev-parse,返回 SHA 字符串。失败返回空。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", ref],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as _e:
        print(f"[WARN] _git_rev_parse: git rev-parse {ref} failed: {_e}", file=sys.stderr)
    return ""


def _cosign_verify_blob(
    attestation_path: Path,
    cert_path: Path | None = None,
    sig_path: Path | None = None,
) -> tuple[bool, str]:
    """执行 cosign verify-blob 验证签名。

    Returns:
        (verified, message)
    """
    if not attestation_path.exists():
        return False, f"attestation 文件不存在: {attestation_path}"
    # 找证书/签名文件(默认与 attestation 同目录)
    if cert_path is None:
        cert_path = attestation_path.with_suffix(".pem")
    if sig_path is None:
        sig_path = attestation_path.with_suffix(".sig")
    if not cert_path.exists() or not sig_path.exists():
        return False, (
            f"缺少证书/签名文件: cert={cert_path}, sig={sig_path}"
        )
    try:
        cmd = [
            "cosign", "verify-blob",
            "--certificate", str(cert_path),
            "--signature", str(sig_path),
            "--certificate-identity-regexp", ".*",
            "--certificate-oidc-issuer-regexp", ".*",
            str(attestation_path),
        ]
        env = os.environ.copy()
        env["COSIGN_EXPERIMENTAL"] = "1"
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=15, env=env,
        )
        if result.returncode == 0:
            return True, "cosign verify-blob 通过"
        return False, f"cosign verify-blob 失败: {result.stderr[:500]}"
    except FileNotFoundError:
        return False, "cosign 不在 PATH 中(跳过)"
    except subprocess.TimeoutExpired:
        return False, "cosign verify-blob 超时"
    except Exception as e:
        return False, f"cosign verify-blob 异常: {e}"


def verify_supply_chain(
    attestation_path: Path,
    skip_cosign: bool = False,
) -> dict:
    """验证供应链 digest 一致性。

    Returns:
        {
            "schema_version": str,
            "verified_at": str,
            "attestation_path": str,
            "overall_passed": bool,
            "checks": [
                {
                    "name": str,
                    "passed": bool,
                    "expected": str,
                    "actual": str,
                    "message": str,
                },
            ],
            "attestation": dict,  # 原始 attestation 内容
            "errors": [str],
        }
    """
    checks: list[dict] = []
    errors: list[str] = []

    # 1. 加载 attestation.json
    attestation: dict = {}
    if not attestation_path.exists():
        errors.append(f"attestation 文件不存在: {attestation_path}")
        return {
            "schema_version": SCHEMA_VERSION,
            "verified_at": _now_iso(),
            "attestation_path": str(attestation_path),
            "overall_passed": False,
            "checks": [],
            "attestation": {},
            "errors": errors,
        }
    try:
        with open(attestation_path, "r", encoding="utf-8") as f:
            attestation = json.load(f)
    except Exception as e:
        errors.append(f"attestation JSON 解析失败: {e}")
        return {
            "schema_version": SCHEMA_VERSION,
            "verified_at": _now_iso(),
            "attestation_path": str(attestation_path),
            "overall_passed": False,
            "checks": [],
            "attestation": {},
            "errors": errors,
        }

    # 2. 检查 6 digest 字段均已绑定(非空、非 "pending")
    for field in REQUIRED_DIGESTS:
        val = attestation.get(field, "")
        passed = bool(val) and val != "pending"
        checks.append({
            "name": f"digest_bound:{field}",
            "passed": passed,
            "expected": "非空且非 'pending'",
            "actual": val[:48] if val else "(empty)",
            "message": (
                f"{field} 已绑定: {val[:24]}..."
                if passed and len(val) > 24
                else f"{field} 已绑定: {val}" if passed
                else f"{field} 未绑定或为 pending"
            ),
        })

    # 3. 验证 commit_sha 与当前 git HEAD 一致(若 attestation 中的 commit_sha 有效)
    attestation_commit = attestation.get("commit_sha", "")
    if attestation_commit and attestation_commit != "pending":
        head_sha = _git_rev_parse("HEAD")
        passed = bool(head_sha) and head_sha == attestation_commit
        checks.append({
            "name": "commit_sha_matches_git_head",
            "passed": passed,
            "expected": attestation_commit,
            "actual": head_sha or "(git 不可用)",
            "message": (
                "commit_sha 与 git HEAD 一致"
                if passed
                else f"commit_sha 不匹配: attestation={attestation_commit}, "
                     f"git HEAD={head_sha}"
            ),
        })

    # 4. 验证 tree_sha 与 git rev-parse HEAD^{tree} 一致
    attestation_tree = attestation.get("tree_sha", "")
    if attestation_tree and attestation_tree != "pending":
        actual_tree = _git_rev_parse("HEAD^{tree}")
        passed = bool(actual_tree) and actual_tree == attestation_tree
        checks.append({
            "name": "tree_sha_matches_git_tree",
            "passed": passed,
            "expected": attestation_tree,
            "actual": actual_tree or "(git 不可用)",
            "message": (
                "tree_sha 与 git tree 一致"
                if passed
                else f"tree_sha 不匹配: attestation={attestation_tree}, "
                     f"git tree={actual_tree}"
            ),
        })

    # 5. 验证 migration_digest 与实际 migration 文件一致
    # R65 fix: 使用与 release-gates.yml publish-attestation 步骤相同的 shell 算法,
    # 确保 bit-for-bit 一致(避免 Python glob 与 find 算法差异导致永久 mismatch)。
    attestation_migration = attestation.get("migration_digest", "")
    if attestation_migration and attestation_migration != "pending":
        actual_migration = _migration_digest_workflow_algorithm()
        passed = bool(actual_migration) and actual_migration == attestation_migration
        checks.append({
            "name": "migration_digest_matches",
            "passed": passed,
            "expected": attestation_migration,
            "actual": actual_migration or "(find 命令失败或无 migration 文件)",
            "message": (
                "migration_digest 与实际文件一致"
                if passed
                else f"migration_digest 不匹配(可能 migration 文件已更新)"
            ),
        })

    # 6. 验证 config_digest 与实际配置文件一致
    attestation_config = attestation.get("config_digest", "")
    if attestation_config and attestation_config != "pending":
        config_paths = [_REPO_ROOT / cf for cf in CONFIG_FILES]
        actual_config = _sha256_files_concat(config_paths)
        passed = bool(actual_config) and actual_config == attestation_config
        checks.append({
            "name": "config_digest_matches",
            "passed": passed,
            "expected": attestation_config,
            "actual": actual_config or "(配置文件不存在)",
            "message": (
                "config_digest 与实际文件一致"
                if passed
                else f"config_digest 不匹配(配置文件已更新)"
            ),
        })

    # 7. 验证 sbom_digest(若 workspace 中有 sbom.json)
    attestation_sbom = attestation.get("sbom_digest", "")
    sbom_path = _REPO_ROOT / "sbom.json"
    if attestation_sbom and attestation_sbom != "pending" and sbom_path.exists():
        actual_sbom = _sha256_file(sbom_path)
        passed = actual_sbom == attestation_sbom
        checks.append({
            "name": "sbom_digest_matches",
            "passed": passed,
            "expected": attestation_sbom,
            "actual": actual_sbom,
            "message": (
                "sbom_digest 与 sbom.json 一致"
                if passed
                else f"sbom_digest 不匹配(sbom.json 已更新)"
            ),
        })

    # 8. cosign 验证(可选)
    if not skip_cosign:
        cosign_ok, cosign_msg = _cosign_verify_blob(attestation_path)
        checks.append({
            "name": "cosign_signature_valid",
            "passed": cosign_ok,
            "expected": "cosign verify-blob 通过",
            "actual": cosign_msg,
            "message": cosign_msg,
        })

    overall_passed = all(c["passed"] for c in checks) and not errors

    return {
        "schema_version": SCHEMA_VERSION,
        "verified_at": _now_iso(),
        "attestation_path": str(attestation_path),
        "overall_passed": overall_passed,
        "checks": checks,
        "attestation": attestation,
        "errors": errors,
    }


def _print_human_report(result: dict) -> None:
    """以人类可读格式打印验证报告。"""
    print("═" * 70)
    print("R64 P1-12: 相同 digest 供应链验证报告")
    print("═" * 70)
    print(f"验证时间: {result.get('verified_at', '')}")
    print(f"attestation: {result.get('attestation_path', '')}")
    print()
    print(f"{'检查项':<35} {'结果':<10} 详情")
    print("─" * 70)
    for c in result.get("checks", []):
        status = "✓ PASS" if c["passed"] else "✗ FAIL"
        print(f"  {c['name']:<33} {status:<10} {c.get('message', '')}")
    print("─" * 70)
    errors = result.get("errors", [])
    if errors:
        print(f"\n错误:")
        for e in errors:
            print(f"  - {e}")
    print()
    print(f"总结果: {'✓ 通过' if result.get('overall_passed') else '✗ 失败'}")
    print("═" * 70)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="R64 P1-12: 相同 digest 供应链验证",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--attestation", default=None,
        help="release_attestation.json 路径(默认:仓库根目录)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="报告输出目录(默认不写文件,仅打印)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="JSON 输出模式(适合 CI 解析)",
    )
    parser.add_argument(
        "--skip-cosign", action="store_true",
        help="跳过 cosign 签名验证",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # 决定 attestation 路径
    if args.attestation:
        attestation_path = Path(args.attestation)
    else:
        # 默认在仓库根目录查找
        attestation_path = _REPO_ROOT / "release_attestation.json"

    result = verify_supply_chain(
        attestation_path=attestation_path,
        skip_cosign=args.skip_cosign,
    )

    # 输出报告
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human_report(result)

    # 写入输出目录(若指定)
    if args.output_dir:
        output_dir = Path(args.output_dir)
        if not output_dir.is_absolute():
            output_dir = _REPO_ROOT / output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        report_path = output_dir / f"supply_chain_report_{ts}.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        if not args.json:
            print(f"\n报告已写入: {report_path}")

    return 0 if result.get("overall_passed") else 1


if __name__ == "__main__":
    sys.exit(main())
