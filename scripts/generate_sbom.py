"""R41 P1-11: SBOM(Software Bill of Materials)生成器。

生成 JSON 格式的软件物料清单,包含:
- package name         (来自 pip freeze / requirements.txt)
- version              (来自 pip freeze / requirements.txt)
- license              (从 pip metadata 读取,失败回退 "UNKNOWN")
- sha256               (可选,从 PyPI API 获取;失败回退 "")
- source               ("requirements.txt" / "pip_freeze")
- metadata             (附加元数据,如 homepage / summary)

输出格式(JSON):
{
  "schema_version": "1.0",
  "generated_at": "2026-07-13T12:00:00",
  "generator": "tgjiema/scripts/generate_sbom.py",
  "project": "tgjiema",
  "packages": [
    {
      "name": "httpx",
      "version": "0.27.2",
      "license": "BSD-3-Clause",
      "sha256": "",
      "source": "requirements.txt",
      "homepage": "https://github.com/encode/httpx",
      "summary": ""
    },
    ...
  ]
}

使用方法:
    python scripts/generate_sbom.py
    python scripts/generate_sbom.py --requirements requirements.txt --output sbom.json
    python scripts/generate_sbom.py --with-sha256   # 启用 PyPI sha256 查询(网络访问)
    python scripts/generate_sbom.py --format cyclonedx  # 输出 CycloneDX 兼容格式

设计要点:
- 离线优先:默认不访问网络(license 从 pip metadata 读取)
- 容错:每个包的元数据查询独立 try/except,失败回退默认值
- 确定性:输出按包名字典序排列,便于 diff
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

# 项目根目录(本脚本位于 scripts/ 下)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
DEFAULT_OUTPUT = PROJECT_ROOT / "sbom.json"

# 正则:匹配 "package==version" 或 "package>=version" 形式
_REQ_PATTERN = re.compile(
    r"^\s*([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*([=<>!~]=)+\s*([A-Za-z0-9_.\-+*]+)"
)

# 正则:从 pip freeze 输出解析 "package==version"
_FREEZE_PATTERN = re.compile(
    r"^\s*([A-Za-z0-9_][A-Za-z0-9_.\-]*)==([A-Za-z0-9_.\-+*]+)"
)


def _parse_requirements(path: Path) -> list[dict]:
    """解析 requirements.txt,返回 [{name, version, source}] 列表。

    跳过注释行、空行、环境标记行(如 uvloop; sys_platform != 'win32')。
    """
    if not path.exists():
        return []
    items: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            # 跳过注释、空行、选项(-r / --hash / -e 等)
            if not line or line.startswith("#"):
                continue
            if line.startswith("-"):
                continue
            # 移除环境标记(; 后面的部分)
            line_no_marker = line.split(";")[0].strip()
            m = _REQ_PATTERN.match(line_no_marker)
            if not m:
                continue
            name, _, version = m.group(1), m.group(2), m.group(3)
            items.append({
                "name": name,
                "version": version,
                "source": "requirements.txt",
            })
    except Exception as e:
        print(f"[generate_sbom] 解析 {path} 失败: {e}", file=sys.stderr)
    return items


def _run_pip_freeze() -> list[dict]:
    """执行 `pip freeze` 并解析输出,返回 [{name, version, source}] 列表。

    失败时返回空列表(离线场景降级)。
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            print(
                f"[generate_sbom] pip freeze 返回非零退出码: {result.returncode}",
                file=sys.stderr,
            )
            return []
        items: list[dict] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _FREEZE_PATTERN.match(line)
            if not m:
                continue
            items.append({
                "name": m.group(1),
                "version": m.group(2),
                "source": "pip_freeze",
            })
        return items
    except FileNotFoundError:
        print("[generate_sbom] pip 不可用,跳过 pip freeze", file=sys.stderr)
        return []
    except subprocess.TimeoutExpired:
        print("[generate_sbom] pip freeze 超时", file=sys.stderr)
        return []
    except Exception as e:
        print(f"[generate_sbom] pip freeze 异常: {e}", file=sys.stderr)
        return []


def _get_license_from_metadata(name: str) -> tuple[str, str, str]:
    """从 pip metadata 读取 license / homepage / summary。

    使用 `pip show <name>` 命令,失败返回 ("UNKNOWN", "", "")。
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            return ("UNKNOWN", "", "")
        license_str = "UNKNOWN"
        homepage = ""
        summary = ""
        for line in result.stdout.splitlines():
            if line.startswith("License:") or line.startswith("License: "):
                val = line.split(":", 1)[1].strip()
                if val:
                    license_str = val
            elif line.startswith("Home-page:") or line.startswith("Home-page: "):
                val = line.split(":", 1)[1].strip()
                if val:
                    homepage = val
            elif line.startswith("Summary:") or line.startswith("Summary: "):
                val = line.split(":", 1)[1].strip()
                if val:
                    summary = val
        return (license_str, homepage, summary)
    except Exception:
        return ("UNKNOWN", "", "")


def _get_sha256_from_pypi(name: str, version: str) -> str:
    """从 PyPI API 获取指定包版本的 sha256(可选,需要网络访问)。

    失败时返回空字符串(降级,不阻塞 SBOM 生成)。
    """
    try:
        import urllib.request
        import urllib.error
        url = f"https://pypi.org/pypi/{name}/{version}/json"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # 优先 sdist 的 sha256,其次 wheel
        urls = data.get("urls", []) or []
        for entry in urls:
            digests = entry.get("digests", {}) or {}
            sha = digests.get("sha256", "")
            if sha:
                return sha
        return ""
    except Exception as e:
        print(
            f"[generate_sbom] PyPI 查询失败 name={name} version={version}: {e}",
            file=sys.stderr,
        )
        return ""


def _merge_packages(
    req_items: list[dict], freeze_items: list[dict]
) -> list[dict]:
    """合并 requirements.txt 与 pip freeze 结果。

    优先使用 requirements.txt 中的版本(权威锁定),
    pip freeze 仅用于补充已安装但未在 requirements.txt 中声明的包
    (如开发依赖)。
    """
    merged: dict[str, dict] = {}
    # 先放 pip freeze(已安装的包),会被 requirements.txt 覆盖
    for item in freeze_items:
        merged[item["name"]] = item
    # requirements.txt 覆盖(版本权威)
    for item in req_items:
        merged[item["name"]] = item
    # 按包名字典序排列
    return sorted(merged.values(), key=lambda x: x["name"].lower())


def _enrich_with_metadata(
    packages: list[dict], with_sha256: bool = False
) -> list[dict]:
    """为每个包补充 license / homepage / summary / sha256 元数据。"""
    enriched: list[dict] = []
    for pkg in packages:
        name = pkg["name"]
        version = pkg["version"]
        license_str, homepage, summary = _get_license_from_metadata(name)
        sha256 = ""
        if with_sha256:
            sha256 = _get_sha256_from_pypi(name, version)
        enriched.append({
            "name": name,
            "version": version,
            "license": license_str,
            "sha256": sha256,
            "source": pkg.get("source", "requirements.txt"),
            "homepage": homepage,
            "summary": summary,
        })
    return enriched


def _format_cyclonedx(packages: list[dict]) -> dict:
    """输出 CycloneDX 1.5 兼容格式(简化版)。"""
    components = []
    for pkg in packages:
        hashes = []
        if pkg.get("sha256"):
            hashes.append({"alg": "SHA-256", "content": pkg["sha256"]})
        components.append({
            "type": "library",
            "name": pkg["name"],
            "version": pkg["version"],
            "licenses": [
                {"license": {"id": pkg.get("license", "UNKNOWN") or "UNKNOWN"}}
            ],
            "purl": f"pkg:pypi/{pkg['name']}@{pkg['version']}",
            "bom-ref": f"pkg:pypi/{pkg['name']}@{pkg['version']}",
            "hashes": hashes,
        })
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "timestamp": _dt.datetime.now().isoformat(),
            "tools": [
                {
                    "vendor": "tgjiema",
                    "name": "scripts/generate_sbom.py",
                    "version": "1.0",
                }
            ],
        },
        "components": components,
    }


def generate_sbom(
    requirements_path: Path = DEFAULT_REQUIREMENTS,
    with_sha256: bool = False,
    use_pip_freeze: bool = True,
    output_format: str = "json",
) -> dict:
    """生成 SBOM 字典(可序列化为 JSON)。

    Args:
        requirements_path: requirements.txt 路径
        with_sha256: 是否查询 PyPI sha256(需要网络)
        use_pip_freeze: 是否合并 pip freeze 输出
        output_format: "json"(自定义)或 "cyclonedx"(CycloneDX 1.5 兼容)

    Returns:
        SBOM 字典
    """
    req_items = _parse_requirements(requirements_path)
    freeze_items = _run_pip_freeze() if use_pip_freeze else []
    packages = _merge_packages(req_items, freeze_items)
    packages = _enrich_with_metadata(packages, with_sha256=with_sha256)
    if output_format == "cyclonedx":
        return _format_cyclonedx(packages)
    return {
        "schema_version": "1.0",
        "generated_at": _dt.datetime.now().isoformat(),
        "generator": "tgjiema/scripts/generate_sbom.py",
        "project": "tgjiema",
        "requirements_file": str(requirements_path),
        "package_count": len(packages),
        "packages": packages,
    }


def main(argv: Iterable[str] | None = None) -> int:
    """命令行入口。

    Returns:
        0=成功;1=失败
    """
    parser = argparse.ArgumentParser(
        description="生成 tgjiema 项目的 SBOM(JSON 格式)"
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=DEFAULT_REQUIREMENTS,
        help=f"requirements.txt 路径(默认: {DEFAULT_REQUIREMENTS})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"输出 SBOM 文件路径(默认: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--with-sha256",
        action="store_true",
        help="启用 PyPI sha256 查询(需要网络访问)",
    )
    parser.add_argument(
        "--no-pip-freeze",
        action="store_true",
        help="不合并 pip freeze 输出(仅使用 requirements.txt)",
    )
    parser.add_argument(
        "--format",
        choices=["json", "cyclonedx"],
        default="json",
        help="输出格式:json(自定义)或 cyclonedx(CycloneDX 1.5)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.requirements.exists():
        print(f"[generate_sbom] 文件不存在: {args.requirements}", file=sys.stderr)
        return 1

    print(
        f"[generate_sbom] 生成 SBOM: requirements={args.requirements} "
        f"format={args.format} sha256={args.with_sha256}"
    )
    sbom = generate_sbom(
        requirements_path=args.requirements,
        with_sha256=args.with_sha256,
        use_pip_freeze=not args.no_pip_freeze,
        output_format=args.format,
    )
    try:
        args.output.write_text(
            json.dumps(sbom, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[generate_sbom] 写入 {args.output} 失败: {e}", file=sys.stderr)
        return 1
    pkg_count = sbom.get("package_count") or len(sbom.get("components", []))
    print(f"[generate_sbom] SBOM 已写入: {args.output} ({pkg_count} 个包)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
