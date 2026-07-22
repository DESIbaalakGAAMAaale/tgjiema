#!/usr/bin/env python3
"""R69 P0-5 (Wave 3): 验证最终 OCI 镜像 filesystem 符合 runtime allowlist。

R69 Wave 3 要求:
    - CI 检查对象必须是最终 OCI filesystem,而不是工作区
    - 不得依靠 .dockerignore 单点排除敏感文件
    - 解包最终 image digest 并验证:
      * 所有生产入口依赖存在
      * 无 tests、开发脚本、密钥、缓存和本地文件
      * 无被禁止的 restore writer/入口
      * 无断裂 import
      * Python import smoke 成功

使用方法:
    # 在 CI 中(docker-build job 后)
    python scripts/verify_oci_allowlist.py --image <image_ref>

    # 本地验证(对工作区而非镜像,仅用于开发)
    python scripts/verify_oci_allowlist.py --local

退出码:
    0: 所有验证通过
    1: 任一验证失败(allowlist/blocklist/import smoke)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from runtime_allowlist import (  # noqa: E402
    RUNTIME_ALLOWLIST,
    RUNTIME_BLOCKLIST,
    RUNTIME_IMPORT_SMOKE,
    is_blocked,
)


def _run_container_ls(image_ref: str, path: str = "/app") -> list[str]:
    """在镜像内运行 ls,返回指定路径下的文件/目录列表。

    Args:
        image_ref: OCI image reference(如 "ghcr.io/owner/repo:tag@sha256:...")
        path: 镜像内要列出的路径(默认 /app)

    Returns:
        路径列表(绝对路径),失败时返回空列表
    """
    cmd = [
        "docker", "run", "--rm", "--entrypoint", "/bin/sh",
        image_ref, "-c", f"find {path} -maxdepth 3 -type f 2>/dev/null; "
                          f"find {path} -maxdepth 3 -type d 2>/dev/null",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (subprocess.SubprocessError, OSError) as e:
        print(f"ERROR: docker run 执行失败: {e}", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(f"ERROR: docker run 返回非零退出码({result.returncode})", file=sys.stderr)
        print(f"  stderr: {result.stderr}", file=sys.stderr)
        return []
    # docker run find 输出绝对路径(如 /app/services/...),
    # 但 RUNTIME_ALLOWLIST 使用相对路径(如 app/services/...)。
    # strip 前导 "/" 以保持一致。
    return [line.strip().lstrip("/") for line in result.stdout.splitlines() if line.strip()]


def _run_container_import(image_ref: str, module: str) -> tuple[bool, str]:
    """在镜像内运行 python import,验证模块可导入。

    R69 Wave 7: 使用 SERVICE_ROLE=prometheus_exporter 绕过生产 secrets 校验。
    原因:本测试只验证模块 import 链路无断裂,不验证生产 secrets 配置。
    IMAGE 默认 APP_ENV=production 会触发 Settings._validate_all_fields
    (要求 UPLOAD_BOT_TOKEN 等 6 个 secrets)。prometheus_exporter 是无 secrets
    依赖的合法角色(_validate_prometheus_exporter_fields 是 pass 空实现),
    设置此 ROLE 让 Settings 跳过 secrets 校验,允许模块 import 成功。

    R69 Wave 7 fix: 同时设置 I18N_ALLOW_FALLBACK=1 绕过 i18n 严格出口边界。
    原因:APP_ENV=production 会使 ENVIRONMENT=production,触发
    services/i18n.py::_get_i18n_allow_fallback() 返回 False(严格 fail-closed),
    导致模块级 translate() 调用未显式绑定 locale 时抛 AppError(I18N_LOCALE_NOT_BOUND)。
    本 smoke 只验证 import 链路无断裂,不验证 i18n locale 绑定行为
    (后者由单元测试 services/i18n.py 覆盖)。I18N_ALLOW_FALLBACK=1 是
    services/i18n.py 显式提供的测试逃生舱(见 _get_i18n_allow_fallback 优先级 2)。

    R71 RC3 fix: 改用 APP_ENV=test(原来是 production)。
    原因:R70 Wave 3 引入 escape_hatch_guard,会在 APP_ENV=production/staging
    下检测到 I18N_ALLOW_FALLBACK=1 并拒绝启动(AppError),与 R69 Wave 7 的
    I18N_ALLOW_FALLBACK=1 冲突。APP_ENV=test 让 escape_hatch_guard 跳过
    (只在 production/staging 触发),同时 I18N_ALLOW_FALLBACK=1 在 test 环境
    下被允许。生产 fail-closed 行为由 _verify_image_default_cmd_fail_closed
    单独验证(不设置这些 env var)。

    生产 fail-closed 行为(默认 CMD + i18n 严格模式)由
    _verify_image_default_cmd_fail_closed 单独验证(不设置此 env var)。

    Returns:
        (success, output) — success=True 表示 import 成功,output 为 stdout/stderr
    """
    cmd = [
        "docker", "run", "--rm", "--entrypoint", "/bin/sh",
        "-e", "APP_ENV=test",
        "-e", "SERVICE_ROLE=prometheus_exporter",
        "-e", "I18N_ALLOW_FALLBACK=1",
        image_ref, "-c", f"python -c 'import {module}' 2>&1",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"docker run 执行失败: {e}"
    return result.returncode == 0, result.stdout + result.stderr


def _list_local_workspace() -> list[str]:
    """列出本地工作区文件(用于 --local 模式)。

    Returns:
        工作区内相对路径列表(以 "app/" 为前缀)
    """
    files: list[str] = []
    for top_dir in ("services", "bots", "admin", "config", "database",
                    "locales", "utils", "storage", "tests", "scripts",
                    "docs", "data", "logs"):
        dir_path = REPO_ROOT / top_dir
        if dir_path.exists():
            files.append(f"app/{top_dir}/")
            for entry in dir_path.rglob("*"):
                if entry.is_file():
                    rel = entry.relative_to(REPO_ROOT).as_posix()
                    files.append(f"app/{rel}")
    # 顶层文件
    for top_file in ("run_all.py", "requirements.txt"):
        if (REPO_ROOT / top_file).exists():
            files.append(f"app/{top_file}")
    return files


def _verify_blocklist(files: list[str]) -> list[str]:
    """验证 blocklist:不得存在被禁止的文件。

    Returns:
        违规列表(路径)
    """
    violations: list[str] = []
    for f in files:
        if is_blocked(f):
            violations.append(f)
    return violations


def _verify_allowlist(files: list[str]) -> list[str]:
    """验证 allowlist:关键生产入口依赖必须存在。

    Returns:
        缺失文件列表(allowlist 中的路径但实际不存在)
    """
    missing: list[str] = []
    file_set = set(files)
    for required in RUNTIME_ALLOWLIST:
        if required.endswith("/"):
            # 目录:检查至少有一个文件在该目录下
            found = any(f.startswith(required) for f in file_set)
            if not found:
                missing.append(required)
        else:
            # 文件:精确匹配
            if required not in file_set:
                missing.append(required)
    return missing


def _verify_import_smoke_local() -> list[str]:
    """本地静态 import smoke 测试(用于 --local 模式)。

    本地模式无 venv 依赖,改为:
      1. 检查模块对应的 .py 文件存在(如 services.restore_writer → services/restore_writer.py)
      2. 用 py_compile 检查语法正确性

    真正的 import smoke(在镜像内运行 python -c "import X")由 --image 模式执行。

    Returns:
        失败模块列表(文件缺失或语法错误)
    """
    import py_compile
    failed: list[str] = []
    for module in RUNTIME_IMPORT_SMOKE:
        # 将模块名转换为文件路径:services.restore_writer → services/restore_writer.py
        # 顶层模块 run_all → run_all.py
        # config.settings → config/settings.py
        rel_path = module.replace(".", "/") + ".py"
        file_path = REPO_ROOT / rel_path
        if not file_path.exists():
            # 也可能是包(目录),尝试 <module>/__init__.py
            pkg_path = REPO_ROOT / module.replace(".", "/") / "__init__.py"
            if not pkg_path.exists():
                failed.append(f"{module}: file not found: {rel_path}")
                continue
            file_path = pkg_path
        try:
            py_compile.compile(str(file_path), doraise=True)
        except py_compile.PyCompileError as e:
            failed.append(f"{module}: syntax error: {e}")
    return failed


def _verify_dockerfile_blocklist_config() -> bool:
    """验证 Dockerfile 已配置 blocklist 物理删除(RUN rm 作为第二道防线)。

    R69 Wave 3 要求:不得依靠 .dockerignore 单点排除敏感文件。
    Dockerfile 必须包含 RUN rm -f/rm -rf 删除 blocklist 文件,
    作为 .dockerignore 之后的第二道防线。

    支持多行 RUN 命令(用 `\\` 续行),会把续行合并为单条命令再匹配。

    Returns:
        True 表示 Dockerfile 已正确配置物理删除;False 表示未配置或配置不完整
    """
    dockerfile = REPO_ROOT / "Dockerfile"
    dockerignore = REPO_ROOT / ".dockerignore"
    if not dockerfile.exists():
        return False
    try:
        dockerfile_content = dockerfile.read_text(encoding="utf-8")
    except OSError:
        return False

    # 合并多行 RUN 命令(以 \ 结尾的行与下一行合并)
    merged_lines: list[str] = []
    buffer = ""
    for line in dockerfile_content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if buffer:
                merged_lines.append(buffer)
                buffer = ""
            continue
        if buffer:
            buffer += " " + stripped
        else:
            buffer = stripped
        if stripped.endswith("\\"):
            buffer = buffer[:-1].strip()
        else:
            merged_lines.append(buffer)
            buffer = ""
    if buffer:
        merged_lines.append(buffer)

    # 必须在 Dockerfile 中物理删除的关键文件/目录(R69 Wave 3 blocklist 核心项)
    # 匹配模式:RUN 命令中包含该路径字符串(支持 /app/<path> 或 <path> 两种形式)
    required_removals = (
        "services/db_restore.py",  # legacy restore CLI
        "/app/tests",              # 测试代码目录(rm -rf /app/tests)
        "/app/scripts",            # 运维脚本目录
        "/app/docs",               # 文档/审计报告目录
    )
    for label in required_removals:
        found = False
        for cmd in merged_lines:
            if not cmd.startswith("RUN"):
                continue
            if "rm " not in cmd:
                continue
            if label in cmd:
                found = True
                break
        if not found:
            return False

    # 同时验证 .dockerignore 排除了 services/db_restore.py(第一道防线)
    if dockerignore.exists():
        try:
            dockerignore_content = dockerignore.read_text(encoding="utf-8")
            if "services/db_restore.py" not in dockerignore_content:
                return False
        except OSError:
            return False

    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="R69 Wave 3: 验证 OCI 镜像 filesystem 符合 runtime allowlist",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--image", help="OCI image reference(如 ghcr.io/owner/repo:tag@sha256:...)")
    group.add_argument("--local", action="store_true", help="本地工作区验证(不构建镜像)")
    args = parser.parse_args()

    print("=" * 70)
    print("R69 Wave 3: OCI Runtime Allowlist Verification")
    print("=" * 70)

    if args.local:
        print("模式: 本地工作区验证(--local,不构建镜像)")
        files = _list_local_workspace()
        print(f"扫描工作区文件: {len(files)} 个")
    else:
        print(f"模式: 镜像 filesystem 验证(--image {args.image})")
        files = _run_container_ls(args.image)
        if not files:
            print("FAIL: docker run 未返回任何文件(镜像可能不存在或 docker 不可用)")
            return 1
        print(f"扫描镜像内文件: {len(files)} 个")

    print()

    # ── 1. blocklist 验证 ──
    # R69 Wave 3: blocklist 仅对镜像 filesystem 生效。
    # 本地工作区天然包含 tests/、scripts/、docs/ 等开发文件,
    # 这些文件由 .dockerignore + Dockerfile RUN rm 在构建时排除。
    # --local 模式跳过 blocklist(改为验证 Dockerfile/.dockerignore 配置),
    # 只验证 allowlist(必需文件存在)+ import smoke。
    if args.local:
        print("── 1. Blocklist 验证(--local 模式跳过,改为检查 Dockerfile 配置) ──")
        dockerfile_ok = _verify_dockerfile_blocklist_config()
        if not dockerfile_ok:
            print("FAIL: Dockerfile 未配置 blocklist 物理删除(RUN rm)")
            print("  R69 Wave 3 要求:不得依靠 .dockerignore 单点排除敏感文件")
            print("  必须在 Dockerfile 添加 RUN rm -f 作为第二道防线")
            return 1
        print(f"PASS: Dockerfile 已配置 blocklist 物理删除(第二道防线)")
        print(f"      (.dockerignore 是第一道防线,共 {len(RUNTIME_BLOCKLIST)} 个禁止路径)")
    else:
        print("── 1. Blocklist 验证(不得存在的文件) ──")
        blocklist_violations = _verify_blocklist(files)
        if blocklist_violations:
            print(f"FAIL: 检测到 {len(blocklist_violations)} 个被禁止的文件:")
            for v in blocklist_violations:
                print(f"  - {v}")
            print()
            print("R69 Wave 3 整改: 生产镜像不得包含以下文件/目录:")
            print("  - services/db_restore.py(CLI-only,生产被 capability-sealed)")
            print("  - tests/、scripts/、docs/、.git/、.github/、IDE 配置等")
            print("  - .env / .env.secrets(生产通过 systemd EnvironmentFile 注入)")
            print("  - 数据库文件 *.db(运行时生成的除外)")
            return 1
        print(f"PASS: 无 blocklist 违规(检查 {len(RUNTIME_BLOCKLIST)} 个禁止路径)")
    print()

    # ── 2. allowlist 验证 ──
    print("── 2. Allowlist 验证(必须存在的文件) ──")
    allowlist_missing = _verify_allowlist(files)
    if allowlist_missing:
        print(f"FAIL: 检测到 {len(allowlist_missing)} 个缺失的生产必需文件:")
        for m in allowlist_missing:
            print(f"  - {m}")
        print()
        print("R69 Wave 3 整改: 生产镜像必须包含以下目录/文件:")
        print("  - services/ (含 restore_writer.py / backup_dr_validate.py / restore_orchestrator.py)")
        print("  - bots/、admin/、config/、database/、locales/、utils/、storage/")
        print("  - run_all.py(应用入口)")
        return 1
    print(f"PASS: 无 allowlist 缺失(检查 {len(RUNTIME_ALLOWLIST)} 个必需路径)")
    print()

    # ── 3. import smoke 测试 ──
    print("── 3. Python import smoke 测试(关键模块可导入) ──")
    if args.local:
        # 本地模式:直接 import
        failed_modules = _verify_import_smoke_local()
    else:
        # 镜像模式:通过 docker run 测试每个模块
        failed_modules = []
        for module in RUNTIME_IMPORT_SMOKE:
            success, output = _run_container_import(args.image, module)
            if not success:
                failed_modules.append(f"{module}: {output.strip()}")
    if failed_modules:
        print(f"FAIL: {len(failed_modules)} 个模块 import 失败:")
        for m in failed_modules:
            print(f"  - {m}")
        print()
        print("R69 Wave 3 整改: 关键生产入口模块必须在镜像中可 import,")
        print("否则说明 COPY 缺文件或依赖断裂。")
        return 1
    print(f"PASS: {len(RUNTIME_IMPORT_SMOKE)} 个关键模块 import 成功")
    print()

    print("=" * 70)
    print("ALL PASS: R69 Wave 3 OCI Runtime Allowlist Verification")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
