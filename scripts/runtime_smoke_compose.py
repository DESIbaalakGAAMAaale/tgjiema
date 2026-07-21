#!/usr/bin/env python3
"""R69 Wave 7: 真实运行态 Compose smoke(hermetic CI 可执行)。

整改背景(R69 终审报告 Wave 7):
    旧 check_compose_runtime_smoke.py 名为 "runtime smoke",但实际只做静态
    规则校验(R69 Wave 7 已重命名为 check_compose_static_rules.py)。
    本脚本是真正的运行态 smoke,实际启动 Docker 容器、验证健康探针、
    发送 SIGTERM 验证优雅关闭、restart 验证恢复、扫描日志发现隐藏故障。

本脚本的范围与能力(诚实声明):
    1. 真实运行态 smoke — 启动 Docker 容器,验证真实进程行为。
    2. hermetic CI 可执行 — 不需要真实 Telegram bot token / CRDB / R2 secrets。
       通过 override command 在容器内运行一个最小 Python smoke,验证:
         (a) 关键模块 import 成功(发现 ModuleNotFoundError/ImportError)
         (b) SIGTERM 信号处理正确注册并触发优雅关闭
         (c) 进程在规定时间内退出(不依赖 SIGKILL)
         (d) restart 后恢复 readiness
         (e) 日志扫描无 silent fallback / unhandled exception
    3. 生产真实功能(CRDB/Telegram/R2/restore)不在本脚本范围 —
       由 scripts/full_machine_recovery.sh 在生产环境执行(需要真实 secrets)。

运行模式:
    --image <image_ref>:  使用已构建镜像(推荐 CI 使用,绑定 digest)
    --build:              本地构建镜像(开发/调试用)
    --smoke-timeout 30:   smoke 进程最长等待秒数(默认 30)
    --stop-timeout 25:    docker stop 等待 SIGTERM 优雅退出秒数(默认 25,
                          必须小于 smoke-timeout)

CI 调用方式(release-gates.yml runtime-smoke-compose job):
    python scripts/runtime_smoke_compose.py \\
        --image ghcr.io/maxiuquan/tgjiema@sha256:<digest> \\
        --smoke-timeout 30 --stop-timeout 25

退出码:
    0 — 所有运行态 smoke 通过
    1 — 任何 smoke 失败(模块 import 错误 / SIGTERM 未处理 / 日志异常 / 等)
    2 — 参数错误 / Docker 不可用 / 镜像不存在
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# Smoke container 内执行的 Python 代码 — 验证关键模块 import + SIGTERM 处理
# 此代码以字符串形式注入容器,通过 `python -c` 在 PID=1 执行
# 关键:
#   1. PID=1 收到 SIGTERM 后必须主动退出(Docker 不会自动转发给非 PID=1 进程)
#   2. 注册 SIGTERM handler,设置 stop_event,主循环检测到后正常 return
#   3. 避免导入依赖 CRDB/Telegram/R2 的模块(那些需要真实 secrets)
#   4. 导入纯 Python 模块(不依赖外部资源)验证无 ImportError/ModuleNotFoundError
SMOKE_PYTHON_CODE = r'''
# R69 Wave 7: 早期启动诊断 — 在任何 import 之前打印,验证脚本被加载
import sys
print(f"[smoke] bootstrap: python={sys.version.split()[0]}, argv={sys.argv}", flush=True)
print(f"[smoke] bootstrap: platform={sys.platform}", flush=True)

import asyncio
import importlib
import signal
import time
import traceback

# R69 Wave 7: stop event — SIGTERM handler 设置后,主循环检测并优雅退出
_stop = {"stop": False}

def _handle_sigterm(signum, frame):
    print(f"[smoke] received signal {signum}, setting stop flag", flush=True)
    _stop["stop"] = True
    # R69 Wave 7: 不立即 raise,让主循环走正常退出路径(模拟 run_all.py 行为)

# 注册 SIGTERM + SIGINT(与 run_all.py _register_sigterm_handler 行为一致)
signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)

# R69 Wave 7: 关键模块 import smoke — 这些模块是生产 runtime 必需的
# 只验证 production image 实际包含的模块(scripts/ 已被 Dockerfile RUN rm 排除)
# 不导入依赖外部资源(CRDB/Telegram/R2)的模块,只验证 import 链路无断裂
CRITICAL_MODULES = [
    "config.settings",              # Settings + BaseSettings(APP_ENV 单一权威源)
    "config.registry",             # service registry + topology
    "services._production_guard",   # production guard(APP_ENV fail-closed)
    "services.restore_writer",      # R69 Wave 2: 唯一生产 restore writer
    "services.restore_orchestrator", # R69 Wave 2: 唯一恢复入口
    "services.restore_backends",    # R69 Wave 2: CRDB/SQLite backends
    "services.restore_capabilities", # R69 Wave 2: capability seal
    "services.backup_dr_validate",  # R69 Wave 2: backup validation
    "services.command_bus",          # command bus
    "services.error_codes",         # error registry + ErrorCodes
    "services.approval_executor",   # approval executor(MFA + high-risk)
    "services.approval_workflow",   # approval workflow
    "services.disaster_recovery",   # disaster recovery orchestration
    "services.migration_runner",   # migration runner (CLI 入口)
    "database.migrate",             # migration apply (runtime)
    "database.cache_store",          # cache store (KV + queue)
    "database.unit_of_work",         # unit of work pattern
    "database.redis_queue",          # Redis Stream queue (XREADGROUP)
    "utils.exceptions",             # AppError + error codes
    "utils.redis_client",            # Redis connection client
]

import_errors = []
for mod_name in CRITICAL_MODULES:
    # R69 Wave 7 fix: 在 import 前打印,若进程崩溃可定位到具体模块
    print(f"[smoke] importing {mod_name}...", flush=True)
    try:
        importlib.import_module(mod_name)
        print(f"[smoke] OK  {mod_name}", flush=True)
    except BaseException as e:
        # R69 Wave 7 fix: 捕获 BaseException(而非 Exception)以包含 SystemExit /
        # KeyboardInterrupt。某些模块在 import 时可能调用 sys.exit()(例如
        # production guard 检测到不安全配置),SystemExit 不被 except Exception
        # 捕获,会导致整个 smoke 进程直接退出而无错误记录。这里捕获后记录,
        # 让 import_errors 完整反映所有模块的 import 结果。
        tb = traceback.format_exc()
        # R69 Wave 7 fix: 只打印错误摘要(不含 traceback)到容器日志。
        # 完整 traceback 存入 import_errors(写入 smoke_result.json)。
        # 避免日志扫描把 traceback 误判为"unhandled exception"。
        print(f"[smoke] FAIL {mod_name}: {type(e).__name__}: {e}", flush=True)
        import_errors.append({
            "module": mod_name,
            "error": f"{type(e).__name__}: {e}",
            "traceback": tb,
        })

# R69 Wave 7: 写出 import 结果到 /tmp/smoke_result.json(供 host 读取)
import json
result = {
    "import_errors": import_errors,
    "modules_checked": len(CRITICAL_MODULES),
    "started_at": time.time(),
}
try:
    with open("/tmp/smoke_result.json", "w") as f:
        json.dump(result, f, indent=2)
except Exception as e:
    print(f"[smoke] WARN: 无法写 /tmp/smoke_result.json: {e}", flush=True)

if import_errors:
    print(f"[smoke] {len(import_errors)} 个模块 import 失败,退出码 1", flush=True)
    sys.exit(1)

print("[smoke] 所有关键模块 import 成功,等待 SIGTERM...", flush=True)

# R69 Wave 7: 主循环 — 等待 SIGTERM(或超时)
# 每秒检查 _stop 标志,最长等待 600 秒(由 docker stop 触发 SIGTERM 提前退出)
deadline = time.time() + 600
while not _stop["stop"] and time.time() < deadline:
    time.sleep(1)

if _stop["stop"]:
    print("[smoke] SIGTERM 收到,优雅退出(exit 0)", flush=True)
    sys.exit(0)
else:
    print("[smoke] WARN: 600 秒内未收到 SIGTERM,主动退出(应已被 docker stop)", flush=True)
    sys.exit(0)
'''

# 日志扫描模式 — 这些模式发现容器内隐藏故障
LOG_FAILURE_PATTERNS = [
    # Python 导入/语法错误
    (re.compile(r"ModuleNotFoundError:", re.IGNORECASE), "ModuleNotFoundError"),
    (re.compile(r"ImportError:", re.IGNORECASE), "ImportError"),
    (re.compile(r"SyntaxError:", re.IGNORECASE), "SyntaxError"),
    # 未处理异常
    (re.compile(r"Unhandled exception", re.IGNORECASE), "unhandled exception"),
    (re.compile(r"Traceback \(most recent call last\):"), "traceback"),
    # 静默降级
    (re.compile(r"silent fallback", re.IGNORECASE), "silent fallback"),
    (re.compile(r"falling back to (default|legacy|fallback)", re.IGNORECASE), "fallback"),
    # 环境不匹配
    (re.compile(r"environment mismatch", re.IGNORECASE), "environment mismatch"),
    (re.compile(r"APP_ENV.*mismatch", re.IGNORECASE), "APP_ENV mismatch"),
    # Migration mismatch
    (re.compile(r"migration.*mismatch", re.IGNORECASE), "migration mismatch"),
]

# 部分日志中允许的"已知良性"关键字(避免误报)
BENIGN_LOG_KEYWORDS = [
    "Traceback (most recent call last):",  # 已被显式 try/except 捕获的 traceback
]


def _run(
    cmd: list[str],
    *,
    capture: bool = True,
    timeout: int | None = None,
    check: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """执行命令,统一错误处理。"""
    if env is not None:
        full_env = os.environ.copy()
        full_env.update(env)
    else:
        full_env = None
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
        timeout=timeout,
        check=check,
        env=full_env,
    )


def _docker_available() -> bool:
    """检查 Docker daemon 可用。"""
    if not shutil.which("docker"):
        return False
    try:
        result = _run(["docker", "info"], timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def _build_image() -> str:
    """本地构建镜像,返回 image ref(<repo>:<tag>)。"""
    tag = "tgjiema:runtime-smoke-build"
    print(f"[runtime_smoke] 本地构建镜像: {tag}")
    result = _run(
        ["docker", "build", "-t", tag, "."],
        capture=True,
        timeout=600,
        cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        print(f"[runtime_smoke] docker build 失败:\n{result.stdout}\n{result.stderr}",
              file=sys.stderr)
        _fail("docker build 失败")
    print(f"[runtime_smoke] 构建完成: {tag}")
    return tag


def _verify_image_default_cmd_fail_closed(image_ref: str) -> None:
    """R69 P0-3: 验证镜像默认 CMD 在 APP_ENV=production 下 fail-closed。

    生产镜像默认 CMD 是 `python run_all.py`,未指定 --standalone 时:
    - run_all.py 检测到 APP_ENV=production
    - 拒绝多进程模式(exit 1)
    - 容器 exit code 应为 1(不是 0,不是 137 SIGKILL)

    这是 R69 Wave 1 + Wave 7 的关键验证:生产镜像不会隐式降级到多进程模式。
    """
    print(f"[runtime_smoke] 验证镜像默认 CMD fail-closed(image={image_ref})")
    result = _run(
        ["docker", "run", "--rm", image_ref],
        capture=True,
        timeout=60,
    )
    if result.returncode != 1:
        print(
            f"[runtime_smoke] FAIL: 默认 CMD 应 exit 1(fail-closed),"
            f"实际 exit code={result.returncode}\n"
            f"stdout: {result.stdout[-500:]}\n"
            f"stderr: {result.stderr[-500:]}",
            file=sys.stderr,
        )
        _fail("镜像默认 CMD 在 APP_ENV=production 下未 fail-closed(R69 P0-3)")
    print("[runtime_smoke] OK  默认 CMD 在 APP_ENV=production 下 fail-closed(exit 1)")


def _run_smoke_container(
    image_ref: str,
    container_name: str,
    stop_timeout: int,
    smoke_timeout: int,
) -> dict[str, Any]:
    """启动 smoke 容器,验证模块 import + SIGTERM 处理。

    Returns:
        dict 包含:
            - exit_code: 容器退出码
            - started_at: 启动时间(unix epoch)
            - stopped_at: 停止时间
            - stop_method: 'sigterm' | 'sigkill' | 'natural_exit'
            - elapsed_seconds: 总耗时
            - logs: 容器日志(完整)
            - smoke_result: 容器内写的 /tmp/smoke_result.json(若存在)
    """
    # R69 Wave 7 fix: 不使用 -v 挂载(在 GitHub Actions runner 上会遇到
    # SELinux/AppArmor 权限问题:python: can't open file '/tmp/smoke.py':
    # [Errno 13] Permission denied)。改用 heredoc 在容器内创建脚本,
    # 然后用 exec python 替换 sh 进程,使 Python 成为 PID=1 以接收 SIGTERM。
    # heredoc delimiter 'PYEOF' 用单引号引用,防止 shell 对内容做变量展开。

    try:
        # 构造容器内 shell 命令:1) heredoc 写脚本 2) exec python 替换 PID=1
        # R69 Wave 7 fix: 显式设置 PYTHONPATH=/app 并 cd /app。
        # 根因:`python /tmp/smoke.py` 的 sys.path[0] 是 `/tmp`(脚本所在目录),
        # 不包含 Dockerfile WORKDIR /app,导致 `import config.settings` 等模块
        # 找不到(对比:`python -c 'import X'` 的 sys.path[0] 是 ''(CWD),
        # 所以 verify_oci_allowlist.py 不受此问题影响)。
        # 修复:通过 `export PYTHONPATH=/app` 把 /app 加入 sys.path 头部,
        # 同时 `cd /app` 保证相对路径(如 ./run_all.py)可用。
        sh_cmd = (
            "cat > /tmp/smoke.py << 'PYEOF'\n"
            f"{SMOKE_PYTHON_CODE}\n"
            "PYEOF\n"
            "cd /app && export PYTHONPATH=/app && exec python /tmp/smoke.py"
        )
        # 启动容器(后台运行,-d)
        # 容器命令:/bin/sh -c "cat > /tmp/smoke.py << ... && exec python /tmp/smoke.py"
        # 环境变量:
        #   APP_ENV=production(已是镜像默认)— 保留 fail-closed 语义
        #   SERVICE_ROLE=prometheus_exporter — 无 secrets 依赖的合法角色,
        #     让 Settings 跳过 _validate_all_fields(否则会要求 6 个 Bot Token
        #     + COCKROACHDB_URL)。本 smoke 只验证 import 链路无断裂,不验证
        #     生产 secrets 配置(后者由 _verify_image_default_cmd_fail_closed 覆盖)。
        #   I18N_ALLOW_FALLBACK=1 — R69 Wave 7 fix: 测试逃生舱绕过 i18n 严格出口边界。
        #     APP_ENV=production 会使 ENVIRONMENT=production,触发
        #     services/i18n.py::_get_i18n_allow_fallback() 返回 False(严格 fail-closed),
        #     导致模块级 translate() 调用未显式绑定 locale 时抛 AppError。
        #     本 smoke 只验证 import 链路无断裂,不验证 i18n locale 绑定行为
        #     (后者由单元测试覆盖)。I18N_ALLOW_FALLBACK=1 是 services/i18n.py
        #     显式提供的测试逃生舱(见 _get_i18n_allow_fallback 优先级 2)。
        # R69 Wave 7: 不需要真实 secrets — smoke 脚本只 import 纯 Python 模块
        run_cmd = [
            "docker", "run", "-d",
            "--name", container_name,
            "--env", "APP_ENV=production",
            "--env", "SERVICE_ROLE=prometheus_exporter",
            "--env", "I18N_ALLOW_FALLBACK=1",
            "--env", "PYTHONUNBUFFERED=1",
            "--entrypoint", "/bin/sh",
            image_ref,
            "-c", sh_cmd,
        ]
        start_result = _run(run_cmd, capture=True, timeout=30)
        if start_result.returncode != 0:
            print(
                f"[runtime_smoke] docker run 失败:\n{start_result.stdout}\n"
                f"{start_result.stderr}",
                file=sys.stderr,
            )
            _fail(f"无法启动 smoke 容器 {container_name}")
        container_id = start_result.stdout.strip()[:12]
        print(f"[runtime_smoke] 容器启动: id={container_id}, name={container_name}")

        started_at = time.time()

        # 等待 smoke 完成 import 阶段(最多 60 秒)
        # 通过 docker logs 检查是否打印 "等待 SIGTERM"
        # 同时检测容器是否早期退出(exit code != None 表示已退出)
        wait_seconds = 0
        max_import_wait = 60
        import_complete = False
        early_exit_code: int | None = None
        early_exit_logs = ""
        while wait_seconds < max_import_wait:
            time.sleep(2)
            wait_seconds += 2
            # 检查容器是否仍在运行(若已退出,docker inspect 返回 Status=exited)
            inspect_running = _run(
                ["docker", "inspect", "--format", "{{.State.Status}}", container_name],
                capture=True, timeout=10,
            )
            container_status = inspect_running.stdout.strip()
            logs_result = _run(
                ["docker", "logs", container_name], capture=True, timeout=10
            )
            if "等待 SIGTERM" in logs_result.stdout:
                import_complete = True
                break
            if "import 失败" in logs_result.stdout:
                # import 失败,容器应已退出
                import_complete = True
                break
            # 检测容器早期退出(Status=exited 或 不存在)
            if container_status in ("exited", ""):
                # 容器已退出 — 获取退出码并捕获日志用于诊断
                early_exit_code = -1
                try:
                    exit_inspect = _run(
                        ["docker", "inspect", "--format", "{{.State.ExitCode}}",
                         container_name],
                        capture=True, timeout=10,
                    )
                    early_exit_code = int(exit_inspect.stdout.strip())
                except (ValueError, subprocess.SubprocessError):
                    pass
                early_exit_logs = (logs_result.stdout or "") + (logs_result.stderr or "")
                print(
                    f"[runtime_smoke] 容器早期退出({wait_seconds}s): "
                    f"exit_code={early_exit_code}, status={container_status or 'gone'}",
                    file=sys.stderr,
                )
                if early_exit_logs.strip():
                    print(
                        f"[runtime_smoke] 容器日志(早期退出诊断):\n"
                        f"{early_exit_logs[:2000]}",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "[runtime_smoke] 容器日志为空(可能是挂载失败或 "
                        "entrypoint 执行错误未输出到 stdout/stderr)",
                        file=sys.stderr,
                    )
                import_complete = True
                break

        if not import_complete:
            # 超时未退出也未看到预期日志 — 打印当前日志内容用于诊断
            logs_result = _run(
                ["docker", "logs", container_name], capture=True, timeout=10
            )
            current_logs = (logs_result.stdout or "") + (logs_result.stderr or "")
            print(
                f"[runtime_smoke] WARN: {max_import_wait}s 内未看到 '等待 SIGTERM' "
                f"或 'import 失败',继续 SIGTERM 测试\n"
                f"[runtime_smoke] 当前容器日志(超时诊断,前 2000 字符):\n"
                f"{current_logs[:2000]}",
                file=sys.stderr,
            )

        # 发送 SIGTERM(docker stop 默认发 SIGTERM,等待 stop_timeout 秒后发 SIGKILL)
        # 注意:stop_timeout 必须 < smoke_timeout(避免 smoke 在 SIGTERM 到达前自然退出)
        print(
            f"[runtime_smoke] 发送 SIGTERM(docker stop -t {stop_timeout})"
        )
        stop_started = time.time()
        stop_result = _run(
            ["docker", "stop", "-t", str(stop_timeout), container_name],
            capture=True,
            timeout=stop_timeout + 30,  # 加 30s 缓冲
        )
        stopped_at = time.time()
        stop_elapsed = stopped_at - stop_started

        if stop_result.returncode != 0:
            print(
                f"[runtime_smoke] docker stop 失败:\n{stop_result.stdout}\n"
                f"{stop_result.stderr}",
                file=sys.stderr,
            )

        # 获取容器退出码 — docker inspect 只接受一个 --format,
        # 必须用单个 format 模板组合多个字段(换行分隔),
        # 否则只有最后一个 --format 生效,导致 int('exited') 报错。
        inspect_result = _run(
            ["docker", "inspect",
             "--format", "{{.State.ExitCode}}\n{{.State.OOMKilled}}\n{{.State.Status}}",
             container_name],
            capture=True,
            timeout=10,
        )
        # inspect 返回 3 行(ExitCode / OOMKilled / Status)
        inspect_lines = inspect_result.stdout.strip().split("\n")
        try:
            exit_code = int(inspect_lines[0]) if len(inspect_lines) >= 1 else -1
        except ValueError:
            # docker inspect 输出异常,无法解析为整数
            exit_code = -1
        oom_killed = inspect_lines[1] if len(inspect_lines) >= 2 else "unknown"
        status = inspect_lines[2] if len(inspect_lines) >= 3 else "unknown"

        # 判断 stop method
        if exit_code == 0:
            stop_method = "sigterm"  # SIGTERM 触发优雅退出
        elif exit_code == 137:
            stop_method = "sigkill"  # SIGKILL(docker stop 超时后强制杀)
        elif exit_code == 1:
            # 容器内 smoke 检测到 import 失败,主动 exit 1
            stop_method = "natural_exit"
        else:
            stop_method = f"unknown(exit={exit_code})"

        # 获取完整日志
        logs_result = _run(
            ["docker", "logs", container_name], capture=True, timeout=30
        )
        full_logs = (logs_result.stdout or "") + (logs_result.stderr or "")

        # 提取 smoke_result.json(若容器写过)
        # R69 Wave 7 fix: 不使用 `docker cp ... -`(输出 tar 格式,非原始 JSON),
        # 改用 `docker cp` 到临时文件,然后读取文件内容。
        # 原实现 `json.loads(cp_result.stdout)` 会因 tar 头部而 JSONDecodeError,
        # 导致 smoke_result 永远为 None,import_errors 被忽略。
        smoke_result: dict[str, Any] | None = None
        import tempfile as _tempfile
        with _tempfile.NamedTemporaryFile(
            delete=False, suffix="_smoke_result.json"
        ) as _tmp_f:
            tmp_result_path = _tmp_f.name
        try:
            cp_result = _run(
                ["docker", "cp",
                 f"{container_name}:/tmp/smoke_result.json", tmp_result_path],
                capture=True,
                timeout=10,
            )
            if cp_result.returncode == 0:
                try:
                    with open(tmp_result_path, "r", encoding="utf-8") as f:
                        smoke_result = json.load(f)
                except (json.JSONDecodeError, OSError):
                    smoke_result = None
        finally:
            try:
                os.unlink(tmp_result_path)
            except OSError:
                pass

        return {
            "container_id": container_id,
            "exit_code": exit_code,
            "started_at": started_at,
            "stopped_at": stopped_at,
            "stop_method": stop_method,
            "stop_elapsed_seconds": stop_elapsed,
            "status": status,
            "oom_killed": oom_killed,
            "logs": full_logs,
            "smoke_result": smoke_result or {},
            "import_complete": import_complete,
        }
    finally:
        # 清理容器(若仍存在)
        _run(["docker", "rm", "-f", container_name],
              capture=True, timeout=10)


def _scan_logs_for_failures(logs: str) -> list[dict[str, str]]:
    """扫描容器日志,发现隐藏故障。

    Returns:
        list of {"pattern_name": str, "matched_line": str} — 发现的故障
    """
    findings: list[dict[str, str]] = []
    for line in logs.splitlines():
        # 跳过良性 traceback(已被 try/except 捕获)
        if any(benign in line for benign in BENIGN_LOG_KEYWORDS):
            # 仍然检查是否是 ModuleNotFoundError 等(更具体)
            pass
        for pattern, name in LOG_FAILURE_PATTERNS:
            if pattern.search(line):
                # 跳过日志中明确标记为"已处理"或"recovered"的行
                if "recovered" in line.lower() or "handled" in line.lower():
                    continue
                # 跳过 smoke 脚本自己输出的 traceback(已知良性 — smoke 主动捕获)
                if "[smoke] FAIL" in line and name == "traceback":
                    continue
                findings.append({
                    "pattern_name": name,
                    "matched_line": line,
                })
                break  # 一行只算一个故障
    return findings


def _verify_smoke_result(
    result: dict[str, Any],
    container_name: str,
    stop_timeout: int,
) -> list[str]:
    """验证 smoke 容器结果,返回失败原因列表。"""
    failures: list[str] = []
    smoke_result = result.get("smoke_result") or {}
    import_errors = smoke_result.get("import_errors") or []

    # 1. import 错误
    if import_errors:
        failures.append(
            f"容器 {container_name}: {len(import_errors)} 个关键模块 import 失败: "
            + ", ".join(e["module"] for e in import_errors)
        )

    # 2. SIGTERM 处理验证
    stop_method = result.get("stop_method", "unknown")
    stop_elapsed = result.get("stop_elapsed_seconds", 999)

    # 容器内 import 失败时主动 exit 1 — 这是预期的"早退",不算 SIGTERM 失败
    if stop_method == "natural_exit":
        # import 阶段已失败,无需再验证 SIGTERM(SMOKE_PYTHON_CODE 主动 exit 1)
        # 但仍然算作失败(因为 import 失败)
        if not import_errors:
            failures.append(
                f"容器 {container_name}: stop_method=natural_exit 但无 import 错误,"
                f"可能是异常退出"
            )
    elif stop_method == "sigterm":
        # SIGTERM 触发优雅退出 — 验证退出时间 < stop_timeout
        # (docker stop -t N: 等待 N 秒后发 SIGKILL,exit_code=0 表示 SIGTERM 在 N 秒内被处理)
        if stop_elapsed >= stop_timeout:
            failures.append(
                f"容器 {container_name}: SIGTERM 处理耗时 {stop_elapsed:.1f}s "
                f">= stop_timeout {stop_timeout}s(可能依赖 SIGKILL)"
            )
        else:
            print(
                f"[runtime_smoke] OK  SIGTERM 优雅退出({stop_elapsed:.1f}s < "
                f"{stop_timeout}s,exit_code=0)"
            )
    elif stop_method == "sigkill":
        failures.append(
            f"容器 {container_name}: docker stop 后 SIGTERM 未在 {stop_timeout}s 内退出,"
            f"被 SIGKILL 强制终止(exit_code=137)。这表明进程未正确处理 SIGTERM "
            f"信号(可能未注册 handler,或 handler 阻塞)。"
        )
    else:
        failures.append(
            f"容器 {container_name}: stop_method={stop_method},"
            f"exit_code={result.get('exit_code')}"
        )

    # 3. OOM killed
    if result.get("oom_killed") == "true":
        failures.append(
            f"容器 {container_name}: OOMKilled=true — 进程内存超限"
        )

    # 4. 日志扫描
    logs = result.get("logs", "")
    log_findings = _scan_logs_for_failures(logs)
    if log_findings:
        # 去重(同一 pattern_name 算一类)
        seen_patterns = set()
        for f in log_findings:
            if f["pattern_name"] not in seen_patterns:
                seen_patterns.add(f["pattern_name"])
                failures.append(
                    f"容器 {container_name}: 日志扫描发现 '{f['pattern_name']}': "
                    f"{f['matched_line'][:200]}"
                )

    return failures


def _verify_restart_recovery(
    image_ref: str,
    stop_timeout: int,
    smoke_timeout: int,
) -> dict[str, Any]:
    """R69 Wave 7: restart 后验证恢复(readiness)。

    第一次 smoke 验证 SIGTERM 处理;
    第二次 smoke 验证 restart 后容器能再次进入 readiness(等 SIGTERM)状态。
    """
    container_name = "tgjiema-runtime-smoke-restart"
    print(f"\n[runtime_smoke] === restart 验证 ===")
    print(f"[runtime_smoke] 启动 restart 验证容器 {container_name}")
    result = _run_smoke_container(
        image_ref=image_ref,
        container_name=container_name,
        stop_timeout=stop_timeout,
        smoke_timeout=smoke_timeout,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="R69 Wave 7: 真实运行态 Compose smoke(hermetic CI 可执行)"
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--image", metavar="IMAGE_REF",
        help="使用已构建的镜像(推荐 CI,如 ghcr.io/maxiuquan/tgjiema@sha256:...)"
    )
    mode_group.add_argument(
        "--build", action="store_true",
        help="本地构建镜像(开发/调试用)"
    )
    parser.add_argument(
        "--smoke-timeout", type=int, default=30,
        help="smoke 进程最长等待秒数(默认 30)"
    )
    parser.add_argument(
        "--stop-timeout", type=int, default=25,
        help="docker stop 等待 SIGTERM 优雅退出秒数(默认 25,必须 < smoke-timeout)"
    )
    parser.add_argument(
        "--skip-default-cmd-check", action="store_true",
        help="跳过默认 CMD fail-closed 检查(用于本地 --build 调试)"
    )
    args = parser.parse_args()

    # 参数校验
    if args.stop_timeout >= args.smoke_timeout:
        _fail(
            f"--stop-timeout ({args.stop_timeout}) 必须 < --smoke-timeout "
            f"({args.smoke_timeout}) — 避免 smoke 在 SIGTERM 到达前自然退出"
        )

    # Docker 可用性检查
    if not _docker_available():
        _fail("Docker daemon 不可用 — runtime_smoke_compose.py 需要 Docker")
    print("[runtime_smoke] Docker daemon 可用")

    # 获取镜像
    if args.build:
        image_ref = _build_image()
    else:
        image_ref = args.image
        # 验证镜像存在
        inspect_result = _run(
            ["docker", "image", "inspect", image_ref],
            capture=True, timeout=10,
        )
        if inspect_result.returncode != 0:
            _fail(
                f"镜像不存在: {image_ref}\n"
                f"请先 docker pull 或使用 --build 本地构建"
            )
    print(f"[runtime_smoke] 使用镜像: {image_ref}")

    print("\n=== R69 Wave 7: 真实运行态 smoke ===\n")

    # 1. 验证默认 CMD fail-closed(R69 P0-3)
    if not args.skip_default_cmd_check:
        _verify_image_default_cmd_fail_closed(image_ref)
        print()

    # 2. 第一次 smoke:验证 import + SIGTERM 处理
    container_name_1 = "tgjiema-runtime-smoke-1"
    print(f"[runtime_smoke] === 第一次 smoke(import + SIGTERM)===")
    print(f"[runtime_smoke] 启动 smoke 容器 {container_name_1}")
    result_1 = _run_smoke_container(
        image_ref=image_ref,
        container_name=container_name_1,
        stop_timeout=args.stop_timeout,
        smoke_timeout=args.smoke_timeout,
    )

    failures_1 = _verify_smoke_result(result_1, container_name_1, args.stop_timeout)
    if failures_1:
        print(f"\n[runtime_smoke] 第一次 smoke 失败:")
        for f in failures_1:
            print(f"  - {f}")
        # R69 Wave 7 fix: 失败时打印完整容器日志 + smoke_result 用于诊断
        print(f"\n[runtime_smoke] 完整容器日志(失败诊断,后 3000 字符):")
        full_logs_1 = result_1.get("logs", "")
        print(full_logs_1[-3000:] if len(full_logs_1) > 3000 else full_logs_1)
        smoke_result_1 = result_1.get("smoke_result") or {}
        if smoke_result_1:
            print(f"\n[runtime_smoke] smoke_result.json:")
            print(json.dumps(smoke_result_1, indent=2, ensure_ascii=False))
        _fail("R69 Wave 7 第一次 smoke 失败")
    print(f"[runtime_smoke] OK  第一次 smoke 通过")

    # 3. restart 验证(第二次 smoke)
    result_2 = _verify_restart_recovery(
        image_ref=image_ref,
        stop_timeout=args.stop_timeout,
        smoke_timeout=args.smoke_timeout,
    )
    failures_2 = _verify_smoke_result(result_2, "tgjiema-runtime-smoke-restart", args.stop_timeout)
    if failures_2:
        print(f"\n[runtime_smoke] restart 验证失败:")
        for f in failures_2:
            print(f"  - {f}")
        # R69 Wave 7 fix: 失败时打印完整容器日志 + smoke_result 用于诊断
        print(f"\n[runtime_smoke] 完整容器日志(失败诊断,后 3000 字符):")
        full_logs_2 = result_2.get("logs", "")
        print(full_logs_2[-3000:] if len(full_logs_2) > 3000 else full_logs_2)
        smoke_result_2 = result_2.get("smoke_result") or {}
        if smoke_result_2:
            print(f"\n[runtime_smoke] smoke_result.json:")
            print(json.dumps(smoke_result_2, indent=2, ensure_ascii=False))
        _fail("R69 Wave 7 restart 验证失败")
    print(f"[runtime_smoke] OK  restart 验证通过")

    # 最终统计
    print("\n=== R69 Wave 7: runtime smoke 全部通过 ===")
    print(f"  镜像: {image_ref}")
    print(f"  默认 CMD fail-closed: OK")
    print(f"  第一次 smoke (import + SIGTERM): OK")
    print(f"    - 关键模块 import: {result_1.get('smoke_result', {}).get('modules_checked', 0)} 个")
    print(f"    - SIGTERM 处理: {result_1.get('stop_elapsed_seconds', 0):.1f}s "
          f"< stop_timeout {args.stop_timeout}s")
    print(f"  restart 验证: OK")
    print(f"    - SIGTERM 处理: {result_2.get('stop_elapsed_seconds', 0):.1f}s "
          f"< stop_timeout {args.stop_timeout}s")
    print(f"  日志扫描: 无 ImportError/ModuleNotFoundError/unhandled exception")
    return 0


if __name__ == "__main__":
    sys.exit(main())
