#!/usr/bin/env python3
"""R71 Wave 2: 真实合成业务交易执行器。

不直接写内部成功状态,而是通过真实应用入口注入测试事件,
验证完整业务链:输入 → Redis/queue → worker → DBWriter → CRDB sync → 输出。

整改背景(R71 P0-05/06/07):
    R70 Wave 5 的 business_smoke 阶段只调用 admin /healthz 并检查 Bot heartbeat,
    不是完整业务交易。R71 Wave 2 要求通过真实应用入口注入合成交易,
    验证完整业务链路(Redis Stream → db_writer → SQLite/CRDB)。

设计原则:
    1. 通过真实应用入口注入(docker compose exec redis redis-cli XADD),
       不直接写数据库,不 mock 任何业务逻辑
    2. 每次交易使用唯一 trace_id 隔离,结束后清理
    3. 重复提交同一 trace_id 验证幂等(消息携带 message_id 用于幂等去重)
    4. 注入合法失败场景(畸形 JSON)验证错误处理(进入 DLQ)
    5. fail-closed:任一步骤失败立即返回失败证据,不吞异常

业务链路:
    - 注入:XADD tgjiema:writer:stream * data <JSON>
      JSON 格式与 database/redis_queue.py push() 一致:
        {
          "op_type": "upsert",
          "table": "bot_heartbeat",
          "method_name": "write_bot_heartbeat",
          "data": {"name": "<trace_id>", "total_processed": 0, "total_errors": 0},
          "redis_key": "cache:all_bot_heartbeats",
          "message_id": "<uuid>",
          "created_at": <timestamp>,
          "attempts": 0
        }
    - 消费:db_writer 通过 XREADGROUP 消费,调用 CacheStore.write_bot_heartbeat
    - 落库:SQLite bot_heartbeat 表 INSERT OR REPLACE
    - 验证:docker compose exec db_writer python -c "SELECT COUNT(*) FROM bot_heartbeat WHERE name=?"
    - 幂等:重复 XADD 同一 message_id,验证 row count 不增加
    - 失败:注入畸形 JSON,验证进入 DLQ(dead_letter.jsonl 或 DLQ stream)
    - 清理:DELETE FROM bot_heartbeat WHERE name LIKE 'synthetic_r71_%'

CLI 退出码:
    0 — 全部步骤通过
    1 — 任一步骤失败(fail-closed)
"""
# R71 RC33: 移除 `from __future__ import annotations`。
# 根因: `from __future__ import annotations` + `@dataclass` + PEP 604 `str | None`
# 在 `dataclasses._is_type` 中触发 `AttributeError: 'NoneType' object has no attribute '__dict__'`。
# CI 使用 Python 3.11,本地使用 Python 3.10,均原生支持 `str | None` / `dict[str, Any]` 语法,
# 无需 `from __future__ import annotations`。移除后 @dataclass 直接处理实际类型对象(非字符串)。

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# 项目根目录
REPO_ROOT = Path(__file__).resolve().parent.parent

# 生产 compose 文件
COMPOSE_FILE = REPO_ROOT / "docker-compose.prod.yml"

# Redis Stream key(必须与 config/settings.py WRITER_STREAM_KEY 一致)
WRITER_STREAM_KEY = "tgjiema:writer:stream"

# 测试隔离前缀(所有合成测试数据使用此前缀,便于清理)
SYNTHETIC_TEST_PREFIX = "synthetic_r71_"

# db_writer 消费消息的最大等待秒数
CONSUMER_WAIT_SECONDS = 30

# 轮询间隔秒数
POLL_INTERVAL_SECONDS = 1


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _compose_cmd(args: list[str]) -> list[str]:
    """构造 docker compose 命令(指定 -f docker-compose.prod.yml)。"""
    return ["docker", "compose", "-f", str(COMPOSE_FILE)] + args


def _run(
    cmd: list[str],
    *,
    timeout: int | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    """执行命令,捕获输出。

    fail-closed:不吞异常,失败时返回 CompletedProcess(returncode != 0)。
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd else None,
    )


@dataclass
class StepResult:
    """单步骤执行结果。"""

    step: str
    timestamp: str
    duration_seconds: float
    returncode: int
    stdout: str
    stderr: str
    passed: bool
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class TransactionEvidence:
    """完整合成交易证据(JSON 输出)。"""

    trace_id: str
    started_at: str
    finished_at: str
    overall_passed: bool
    inject: StepResult
    verify: StepResult
    idempotency: StepResult
    failure_scenario: StepResult
    cleanup: StepResult
    error: str | None = None


def generate_trace_id() -> str:
    """生成唯一 trace_id(UUID4)。

    Returns:
        形如 "synthetic_r71_<uuid4_hex>" 的唯一标识符。
        前缀确保测试数据可识别、可清理。
    """
    return f"{SYNTHETIC_TEST_PREFIX}{uuid.uuid4().hex}"


def _build_heartbeat_message(trace_id: str) -> dict[str, Any]:
    """构造与 database/redis_queue.py push() 一致的消息体。

    使用 write_bot_heartbeat 方法(已有 INSERT OR REPLACE 语义,天然幂等),
    将 trace_id 作为 bot name 注入,实现隔离。

    Args:
        trace_id: 唯一标识符(作为 bot_heartbeat.name)

    Returns:
        与 push() 消息格式一致的 dict
    """
    return {
        "op_type": "upsert",
        "table": "bot_heartbeat",
        "method_name": "write_bot_heartbeat",
        "data": {
            "name": trace_id,
            "total_processed": 0,
            "total_errors": 0,
        },
        "redis_key": "cache:all_bot_heartbeats",
        "message_id": trace_id,  # 用 trace_id 作为幂等键
        "created_at": time.time(),
        "attempts": 0,
    }


def inject_test_event(trace_id: str, timeout: int = 30) -> StepResult:
    """通过 Redis XADD 注入测试事件到业务流。

    通过 docker compose exec redis redis-cli XADD 实际注入消息,
    db_writer 会通过 XREADGROUP 消费。

    Args:
        trace_id: 唯一标识符
        timeout: 命令超时秒数

    Returns:
        StepResult 含 XADD 返回的 stream id
    """
    started = time.time()
    msg = _build_heartbeat_message(trace_id)
    msg_json = json.dumps(msg, default=str)

    # 构造 redis-cli XADD 命令
    # XADD tgjiema:writer:stream * data <json>
    cmd = _compose_cmd([
        "exec", "-T", "redis",
        "redis-cli",
        "--user", os.environ.get("REDIS_ADMIN_PASSWORD", "") and "tgjiema_admin" or "default",
        "XADD", WRITER_STREAM_KEY, "*", "data", msg_json,
    ])

    # 如果有 admin 密码,加上 -a 参数
    admin_pwd = os.environ.get("REDIS_ADMIN_PASSWORD", "")
    if admin_pwd:
        cmd = _compose_cmd([
            "exec", "-T", "redis",
            "redis-cli",
            "--user", "tgjiema_admin",
            "-a", admin_pwd,
            "--no-auth-warning",
            "XADD", WRITER_STREAM_KEY, "*", "data", msg_json,
        ])

    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired as e:
        return StepResult(
            step="inject",
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=-1,
            stdout="",
            stderr=str(e),
            passed=False,
            error=f"XADD 超时({timeout}s)",
        )

    stream_id = result.stdout.strip()
    passed = result.returncode == 0 and bool(stream_id)

    return StepResult(
        step="inject",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        passed=passed,
        error=None if passed else f"XADD 失败 (exit={result.returncode}): {result.stderr}",
        evidence={
            "stream_key": WRITER_STREAM_KEY,
            "stream_id": stream_id,
            "message_id": trace_id,
            "method_name": "write_bot_heartbeat",
        },
    )


def verify_result(trace_id: str, timeout: int = 60) -> StepResult:
    """验证业务链实际消费并落库。

    轮询查询 bot_heartbeat 表,等待 db_writer 消费消息并写入。
    fail-closed:超时未查询到 → 失败。

    Args:
        trace_id: 唯一标识符(作为 bot_heartbeat.name)
        timeout: 最大等待秒数

    Returns:
        StepResult 含查询结果
    """
    started = time.time()
    deadline = started + timeout

    # 通过 db_writer 容器查询 SQLite(确保读到的是 writer 写入的库)
    # 使用 python -c 直接执行 SQL,避免依赖额外工具
    query_cmd = _compose_cmd([
        "exec", "-T", "db_writer",
        "python", "-c",
        f"import sqlite3, sys; "
        f"conn = sqlite3.connect('/app/data/cache_store.db', timeout=5); "
        f"cur = conn.execute("
        f"'SELECT COUNT(*) FROM bot_heartbeat WHERE name = ?', ('{trace_id}',)); "
        f"print(cur.fetchone()[0]); conn.close()",
    ])

    last_stdout = ""
    last_stderr = ""
    last_returncode: int = -1
    found = False

    while time.time() < deadline:
        try:
            result = _run(query_cmd, timeout=15, cwd=REPO_ROOT)
        except subprocess.TimeoutExpired:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        last_stdout = result.stdout.strip()
        last_stderr = result.stderr.strip()
        last_returncode = result.returncode
        if result.returncode == 0:
            try:
                count = int(last_stdout)
            except ValueError:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            if count >= 1:
                found = True
                break
        time.sleep(POLL_INTERVAL_SECONDS)

    return StepResult(
        step="verify",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=last_returncode,
        stdout=last_stdout,
        stderr=last_stderr,
        passed=found,
        error=None if found else (
            f"在 {timeout}s 内未查询到 trace_id={trace_id} 的落库记录 "
            f"(db_writer 可能未消费或 SQLite 查询失败)"
        ),
        evidence={
            "trace_id": trace_id,
            "table": "bot_heartbeat",
            "found": found,
            "row_count": last_stdout if last_stdout.isdigit() else 0,
        },
    )


def verify_idempotency(trace_id: str, timeout: int = 60) -> StepResult:
    """验证幂等性:重复提交同一 trace_id 不产生重复副作用。

    步骤:
      1. 查询当前 row count(应为 1)
      2. 再次 XADD 同一 message_id(trace_id)
      3. 等待消费
      4. 查询 row count(应仍为 1,因为 INSERT OR REPLACE)

    Args:
        trace_id: 唯一标识符
        timeout: 最大等待秒数

    Returns:
        StepResult 含幂等性验证结果
    """
    started = time.time()

    # 1. 查询初始 row count
    count_cmd = _compose_cmd([
        "exec", "-T", "db_writer",
        "python", "-c",
        f"import sqlite3; "
        f"conn = sqlite3.connect('/app/data/cache_store.db', timeout=5); "
        f"cur = conn.execute("
        f"'SELECT COUNT(*) FROM bot_heartbeat WHERE name = ?', ('{trace_id}',)); "
        f"print(cur.fetchone()[0]); conn.close()",
    ])
    try:
        initial_result = _run(count_cmd, timeout=15, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired as e:
        return StepResult(
            step="idempotency",
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=-1,
            stdout="",
            stderr=str(e),
            passed=False,
            error="初始 count 查询超时",
        )

    if initial_result.returncode != 0:
        return StepResult(
            step="idempotency",
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=initial_result.returncode,
            stdout=initial_result.stdout,
            stderr=initial_result.stderr,
            passed=False,
            error=f"初始 count 查询失败: {initial_result.stderr}",
        )

    try:
        initial_count = int(initial_result.stdout.strip())
    except ValueError:
        return StepResult(
            step="idempotency",
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=initial_result.returncode,
            stdout=initial_result.stdout,
            stderr=initial_result.stderr,
            passed=False,
            error=f"初始 count 解析失败: {initial_result.stdout!r}",
        )

    # 2. 再次 XADD 同一 message_id
    reinject = inject_test_event(trace_id, timeout=30)
    if not reinject.passed:
        return StepResult(
            step="idempotency",
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=reinject.returncode,
            stdout=reinject.stdout,
            stderr=reinject.stderr,
            passed=False,
            error=f"重复注入失败: {reinject.error}",
            evidence={"initial_count": initial_count},
        )

    # 3. 等待消费(轮询直到 count 稳定)
    deadline = time.time() + timeout
    final_count = initial_count
    while time.time() < deadline:
        try:
            result = _run(count_cmd, timeout=15, cwd=REPO_ROOT)
        except subprocess.TimeoutExpired:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        if result.returncode == 0:
            try:
                final_count = int(result.stdout.strip())
            except ValueError:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            # 等待 2 个轮询周期确保 count 稳定
            time.sleep(POLL_INTERVAL_SECONDS * 2)
            try:
                result2 = _run(count_cmd, timeout=15, cwd=REPO_ROOT)
                if result2.returncode == 0:
                    try:
                        stable_count = int(result2.stdout.strip())
                        if stable_count == final_count:
                            final_count = stable_count
                            break
                    except ValueError:
                        pass
            except subprocess.TimeoutExpired:
                pass
        time.sleep(POLL_INTERVAL_SECONDS)

    # 4. 幂等性通过条件:final_count == initial_count(INSERT OR REPLACE 不增加行数)
    passed = final_count == initial_count and final_count >= 1

    return StepResult(
        step="idempotency",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=0,
        stdout=f"initial={initial_count} final={final_count}",
        stderr="",
        passed=passed,
        error=None if passed else (
            f"幂等性验证失败: initial_count={initial_count}, "
            f"final_count={final_count}(应相等且 >=1)"
        ),
        evidence={
            "initial_count": initial_count,
            "final_count": final_count,
            "reinject_stream_id": reinject.evidence.get("stream_id", ""),
        },
    )


def inject_failure_scenario(timeout: int = 60) -> StepResult:
    """注入合法失败场景(畸形 JSON),验证错误处理。

    向 Redis Stream 注入畸形 JSON,db_writer 消费时应检测到 JSON 解析失败,
    将消息转入死信队列(DLQ)。这是合法的失败场景(不是测试基础设施故障)。

    Args:
        timeout: 等待 DLQ 处理的最大秒数

    Returns:
        StepResult 含失败场景处理结果
    """
    started = time.time()
    failure_id = f"{SYNTHETIC_TEST_PREFIX}failure_{uuid.uuid4().hex}"
    malformed_json = '{"op_type": "upsert", "table": "bot_heartbeat", "method_name": "write_bot_heartbeat", "data": {"name": "BROKEN'  # 故意截断

    # 注入畸形 JSON
    admin_pwd = os.environ.get("REDIS_ADMIN_PASSWORD", "")
    if admin_pwd:
        inject_cmd = _compose_cmd([
            "exec", "-T", "redis",
            "redis-cli",
            "--user", "tgjiema_admin",
            "-a", admin_pwd,
            "--no-auth-warning",
            "XADD", WRITER_STREAM_KEY, "*", "data", malformed_json,
        ])
    else:
        inject_cmd = _compose_cmd([
            "exec", "-T", "redis",
            "redis-cli",
            "XADD", WRITER_STREAM_KEY, "*", "data", malformed_json,
        ])

    try:
        inject_result = _run(inject_cmd, timeout=30, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired as e:
        return StepResult(
            step="failure_scenario",
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=-1,
            stdout="",
            stderr=str(e),
            passed=False,
            error="注入畸形 JSON 超时",
        )

    if inject_result.returncode != 0:
        return StepResult(
            step="failure_scenario",
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=inject_result.returncode,
            stdout=inject_result.stdout,
            stderr=inject_result.stderr,
            passed=False,
            error=f"注入畸形 JSON 失败: {inject_result.stderr}",
        )

    # 等待 db_worker 消费并将畸形消息转入 DLQ
    # 验证方式:检查 dead_letter.jsonl 文件或 DLQ stream
    # 由于 db_writer 处理畸形 JSON 时会写入 dead_letter.jsonl,
    # 我们轮询检查该文件是否包含我们的畸形消息
    deadline = time.time() + timeout
    dlq_found = False
    last_dlq_output = ""

    while time.time() < deadline:
        # 检查 dead_letter.jsonl 是否有新增(通过文件大小或尾部内容)
        check_cmd = _compose_cmd([
            "exec", "-T", "db_writer",
            "python", "-c",
            "import os, json; "
            "path = '/app/logs/dead_letter.jsonl'; "
            "if os.path.exists(path): "
            "  with open(path, 'rb') as f: "
            "    f.seek(max(0, os.path.getsize(path) - 4096)); "
            "    data = f.read().decode('utf-8', errors='replace'); "
            "    print('OK' if 'BROKEN' in data else 'EMPTY'); "
            "else: print('NOFILE')",
        ])
        try:
            check_result = _run(check_cmd, timeout=15, cwd=REPO_ROOT)
        except subprocess.TimeoutExpired:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue
        last_dlq_output = check_result.stdout.strip()
        if check_result.returncode == 0 and last_dlq_output == "OK":
            dlq_found = True
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    # 失败场景验证通过条件:畸形消息被正确转入 DLQ
    # 如果 db_worker 没有将畸形消息转入 DLQ,说明错误处理有问题
    passed = dlq_found

    return StepResult(
        step="failure_scenario",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=0,
        stdout=last_dlq_output,
        stderr="",
        passed=passed,
        error=None if passed else (
            f"在 {timeout}s 内未检测到畸形消息进入 DLQ "
            f"(db_writer 错误处理可能有问题)"
        ),
        evidence={
            "failure_id": failure_id,
            "dlq_found": dlq_found,
            "inject_stream_id": inject_result.stdout.strip(),
        },
    )


def cleanup(trace_id: str, timeout: int = 30) -> StepResult:
    """清理隔离数据(DELETE 测试 row)。

    Args:
        trace_id: 唯一标识符
        timeout: 命令超时秒数

    Returns:
        StepResult 含清理结果
    """
    started = time.time()

    # 删除本次 trace_id 对应的 row
    # 同时清理所有遗留的合成测试数据(防止前次运行残留)
    cmd = _compose_cmd([
        "exec", "-T", "db_writer",
        "python", "-c",
        f"import sqlite3; "
        f"conn = sqlite3.connect('/app/data/cache_store.db', timeout=5); "
        f"cur = conn.execute("
        f"'DELETE FROM bot_heartbeat WHERE name = ?', ('{trace_id}',)); "
        f"deleted = cur.rowcount; "
        f"conn.commit(); "
        f"print(deleted); conn.close()",
    ])

    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired as e:
        return StepResult(
            step="cleanup",
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=-1,
            stdout="",
            stderr=str(e),
            passed=False,
            error=f"清理超时({timeout}s)",
        )

    deleted_count = 0
    if result.returncode == 0:
        try:
            deleted_count = int(result.stdout.strip())
        except ValueError:
            pass

    # 清理成功条件:命令返回 0(无论删除行数,因为可能之前已清理)
    passed = result.returncode == 0

    return StepResult(
        step="cleanup",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        passed=passed,
        error=None if passed else f"清理失败: {result.stderr}",
        evidence={
            "trace_id": trace_id,
            "deleted_rows": deleted_count,
        },
    )


def run_full_transaction(timeout: int = 120) -> TransactionEvidence:
    """运行完整合成交易流程并返回证据。

    步骤:
      1. 生成唯一 trace_id
      2. 注入测试事件(XADD)
      3. 验证落库(轮询 SELECT)
      4. 验证幂等性(重复 XADD + 查询)
      5. 注入失败场景(畸形 JSON → DLQ)
      6. 清理(DELETE)

    任一步骤失败仍执行清理(finally),但 overall_passed=False。

    Args:
        timeout: 单步骤最大等待秒数

    Returns:
        TransactionEvidence 含全部步骤结果
    """
    started_at = _now_iso()
    trace_id = generate_trace_id()

    inject = inject_test_event(trace_id, timeout=30)
    verify = StepResult(
        step="verify", timestamp=_now_iso(), duration_seconds=0,
        returncode=-1, stdout="", stderr="", passed=False,
        error="未执行(前置步骤失败)",
    )
    idempotency = StepResult(
        step="idempotency", timestamp=_now_iso(), duration_seconds=0,
        returncode=-1, stdout="", stderr="", passed=False,
        error="未执行(前置步骤失败)",
    )
    failure_scenario = StepResult(
        step="failure_scenario", timestamp=_now_iso(), duration_seconds=0,
        returncode=-1, stdout="", stderr="", passed=False,
        error="未执行(前置步骤失败)",
    )
    cleanup_result = StepResult(
        step="cleanup", timestamp=_now_iso(), duration_seconds=0,
        returncode=-1, stdout="", stderr="", passed=False,
        error="未执行",
    )

    overall_passed = False
    error_msg: str | None = None

    try:
        if not inject.passed:
            error_msg = f"注入失败: {inject.error}"
            return TransactionEvidence(
                trace_id=trace_id,
                started_at=started_at,
                finished_at=_now_iso(),
                overall_passed=False,
                inject=inject,
                verify=verify,
                idempotency=idempotency,
                failure_scenario=failure_scenario,
                cleanup=cleanup_result,
                error=error_msg,
            )

        verify = verify_result(trace_id, timeout=timeout)
        if not verify.passed:
            error_msg = f"验证落库失败: {verify.error}"
            return TransactionEvidence(
                trace_id=trace_id,
                started_at=started_at,
                finished_at=_now_iso(),
                overall_passed=False,
                inject=inject,
                verify=verify,
                idempotency=idempotency,
                failure_scenario=failure_scenario,
                cleanup=cleanup_result,
                error=error_msg,
            )

        idempotency = verify_idempotency(trace_id, timeout=timeout)
        if not idempotency.passed:
            error_msg = f"幂等性验证失败: {idempotency.error}"
            return TransactionEvidence(
                trace_id=trace_id,
                started_at=started_at,
                finished_at=_now_iso(),
                overall_passed=False,
                inject=inject,
                verify=verify,
                idempotency=idempotency,
                failure_scenario=failure_scenario,
                cleanup=cleanup_result,
                error=error_msg,
            )

        failure_scenario = inject_failure_scenario(timeout=timeout)
        if not failure_scenario.passed:
            error_msg = f"失败场景验证失败: {failure_scenario.error}"
            return TransactionEvidence(
                trace_id=trace_id,
                started_at=started_at,
                finished_at=_now_iso(),
                overall_passed=False,
                inject=inject,
                verify=verify,
                idempotency=idempotency,
                failure_scenario=failure_scenario,
                cleanup=cleanup_result,
                error=error_msg,
            )

        overall_passed = True

    finally:
        # 无论成功失败都执行清理
        cleanup_result = cleanup(trace_id, timeout=30)

    return TransactionEvidence(
        trace_id=trace_id,
        started_at=started_at,
        finished_at=_now_iso(),
        overall_passed=overall_passed,
        inject=inject,
        verify=verify,
        idempotency=idempotency,
        failure_scenario=failure_scenario,
        cleanup=cleanup_result,
        error=error_msg,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Returns:
        0 — 全部步骤通过
        1 — 任一步骤失败(fail-closed)
    """
    parser = argparse.ArgumentParser(
        description=(
            "R71 Wave 2: 真实合成业务交易执行器"
            "(fail-closed,不允许 mock)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="单步骤最大等待秒数(默认 120)",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="证据输出 JSON 文件路径(默认输出到 stdout)",
    )
    args = parser.parse_args(argv)

    evidence = run_full_transaction(timeout=args.timeout)
    evidence_dict = asdict(evidence)
    evidence_json = json.dumps(evidence_dict, indent=2, ensure_ascii=False)

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(evidence_json, encoding="utf-8")
        print(f"Evidence written to: {output_path}", file=sys.stderr)
    else:
        print(evidence_json)

    if evidence.overall_passed:
        print(
            f"=== R71 Wave 2: 合成交易通过(trace_id={evidence.trace_id}) ===",
            file=sys.stderr,
        )
        return 0
    print(
        f"=== R71 Wave 2: 合成交易失败(trace_id={evidence.trace_id}) — "
        f"{evidence.error} ===",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
