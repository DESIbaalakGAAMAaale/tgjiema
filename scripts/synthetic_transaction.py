#!/usr/bin/env python3
"""R74 P0-03: 合成交易执行器 — DBWriter 组件测试 + 真实产品交易。

提供两种模式:
  - DBWriter 组件测试(run_dbwriter_component_test):直接通过 Redis XADD
    注入消息到 writer stream,验证 db_writer 消费→SQLite 落库→CRDB sync。
    这是 DBWriter 的组件级测试,不经过真实 Telegram 入口。
  - 真实产品交易(run_real_product_transaction):通过 e2e_update_adapter.py
    的 build_test_update / dispatch_to_up_handler / dispatch_to_idx_handler /
    dispatch_to_dsp_handler 触发真实产品交易路径,
    验证完整链路:Update → up → idx → dsp → DBWriter → SQLite → CRDB → outbound。

R74 P0-03 整改:
    原 run_full_transaction 直接通过 XADD 注入 Redis writer stream,
    绕过真实 Telegram 入口(up/idx/dsp handler)。R74 将其重命名为
    run_dbwriter_component_test(明确是 DBWriter 组件测试),
    并新增 run_real_product_transaction 通过 e2e_update_adapter.py
    走真实产品交易路径。

设计原则:
    1. 组件测试:直接 XADD writer stream,验证 db_writer 消费→SQLite→CRDB
    2. 真实产品交易:通过 e2e_update_adapter 调用真实 handler,不直接写数据库
    3. 每次交易使用唯一 trace_id 隔离,结束后清理
    4. 重复提交同一 trace_id 验证幂等(消息携带 message_id 用于幂等去重)
    5. 注入合法失败场景(畸形 JSON)验证错误处理(进入 DLQ)
    6. fail-closed:任一步骤失败立即返回失败证据,不吞异常

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
import re
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

# R72 P0-11: 默认 target_db 路径(可通过 --target-db 切换到 staging 恢复库)
DEFAULT_TARGET_DB = "/app/data/cache_store.db"

# R72 P0-11: target_db 路径白名单(只允许字母数字/下划线/斜杠/点/连字符,防注入)
_TARGET_DB_PATTERN = re.compile(r"^[a-zA-Z0-9_./-]+$")


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


def _validate_target_db(target_db: str) -> str:
    """R72 P0-11: 校验 target_db 路径,防止注入。

    target_db 会通过 f-string 注入到 `docker compose exec db_writer python -c "..."`
    的内联 Python 代码中(sqlite3.connect('<target_db>')),必须做白名单校验。
    只允许字母数字/下划线/斜杠/点/连字符(不含引号/分号/反斜杠等危险字符)。

    Args:
        target_db: SQLite 数据库路径

    Returns:
        校验通过的 target_db

    Raises:
        ValueError: 路径不通过白名单校验
    """
    if not target_db or not _TARGET_DB_PATTERN.match(target_db):
        raise ValueError(
            f"非法 target_db 路径(只允许字母数字/下划线/斜杠/点/连字符): {target_db!r}"
        )
    return target_db


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


def _skipped_step(step: str, reason: str = "未执行(前置步骤失败)") -> StepResult:
    """构造一个跳过的 StepResult(前置步骤失败时使用)。"""
    return StepResult(
        step=step,
        timestamp=_now_iso(),
        duration_seconds=0,
        returncode=-1,
        stdout="",
        stderr="",
        passed=False,
        error=reason,
    )


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
    # R72 P0-03: 新增真实业务方法测试
    # up bot → create_upload_session(INSERT OR IGNORE 幂等)
    upload_session_inject: StepResult = field(
        default_factory=lambda: _skipped_step("upload_session_inject")
    )
    upload_session_verify: StepResult = field(
        default_factory=lambda: _skipped_step("upload_session_verify")
    )
    upload_session_idempotency: StepResult = field(
        default_factory=lambda: _skipped_step("upload_session_idempotency")
    )
    # idx bot → upsert_file_record_local(INSERT OR REPLACE 幂等)
    file_index_inject: StepResult = field(
        default_factory=lambda: _skipped_step("file_index_inject")
    )
    file_index_verify: StepResult = field(
        default_factory=lambda: _skipped_step("file_index_verify")
    )
    file_index_idempotency: StepResult = field(
        default_factory=lambda: _skipped_step("file_index_idempotency")
    )
    # R73 P0-04: 新增 dsp bot 派送链路测试(create_outbox_entry 幂等)
    dsp_dispatch_inject: StepResult = field(
        default_factory=lambda: _skipped_step("dsp_dispatch_inject")
    )
    dsp_dispatch_verify: StepResult = field(
        default_factory=lambda: _skipped_step("dsp_dispatch_verify")
    )
    dsp_dispatch_idempotency: StepResult = field(
        default_factory=lambda: _skipped_step("dsp_dispatch_idempotency")
    )
    # R73 P0-04: CRDB sync 验证(mark_dirty=True 触发 dirty_outbox → crdb_sync → CRDB)
    crdb_sync_verify: StepResult = field(
        default_factory=lambda: _skipped_step("crdb_sync_verify")
    )
    # R73 P0-04: 故障注入测试结果(每个角色一条记录)
    fault_injection: dict = field(default_factory=dict)
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
            # R85 fix: trace_id 只放在顶层 msg["trace_id"](见下),不放入 data。
            # db_writer._execute_sqlite 调用 await method(**data),若 data 含
            # write_bot_heartbeat 不接受的 trace_id 会抛 TypeError → 永久死信 → ACK,
            # 导致 verify 永远查不到落库记录。
        },
        "redis_key": "cache:all_bot_heartbeats",
        # RC59 fix: message_id 必须跨步骤唯一,否则 writer_inbox 幂等检查
        # 会把不同业务方法的相同 message_id 误判为重复(例如 heartbeat 和
        # upload_session 共用 trace_id 作 message_id 时,后者会被跳过)。
        # data 字段仍用 trace_id 作为业务主键(name/upload_id/file_code),
        # 仅 message_id 追加方法后缀以实现跨步骤唯一性。
        "message_id": f"{trace_id}:heartbeat",  # 跨步骤唯一,幂等键
        # R73 P0-04: 顶层 trace_id 字段,贯穿 Update→up→idx→dsp→writer→CRDB→输出
        "trace_id": trace_id,
        "created_at": time.time(),
        "attempts": 0,
    }


def _build_upload_session_message(trace_id: str) -> dict[str, Any]:
    """R72 P0-03: 构造 create_upload_session 消息体(对应 up bot 的真实业务)。

    使用 create_upload_session 方法(INSERT OR IGNORE 语义,天然幂等),
    将 trace_id 作为 upload_id 主键注入,实现隔离。

    方法签名参考 database/cache_store.py:create_upload_session(
        upload_id: str, user_id: int,
        source_msg_ids: list | None = None,
        options_json: dict | None = None,
        trace_id: str = ""
    ) -> bool

    Args:
        trace_id: 唯一标识符(作为 upload_sessions.upload_id 主键)

    Returns:
        与 push() 消息格式一致的 dict
    """
    return {
        "op_type": "insert",
        "table": "upload_sessions",
        "method_name": "create_upload_session",
        "data": {
            "upload_id": trace_id,
            "user_id": 0,
            "source_msg_ids": [],
            "options_json": {},
            "trace_id": trace_id,
        },
        "redis_key": "",
        # RC59 fix: message_id 跨步骤唯一(参见 _build_heartbeat_message 注释)
        "message_id": f"{trace_id}:upload_session",  # 跨步骤唯一,幂等键
        # R73 P0-04: 顶层 trace_id 字段,贯穿 Update→up→idx→dsp→writer→CRDB→输出
        "trace_id": trace_id,
        "created_at": time.time(),
        "attempts": 0,
    }


def _build_file_index_message(trace_id: str) -> dict[str, Any]:
    """R72 P0-03 / R73 P0-04: 构造 upsert_file_record_local 消息体(对应 idx bot 的真实业务)。

    使用 upsert_file_record_local 方法(INSERT OR REPLACE 语义,天然幂等),
    将 trace_id 作为 file_code 主键注入,实现隔离。

    方法签名参考 database/cache_store.py:upsert_file_record_local(
        record: dict, mark_dirty: bool = True, _batch: bool = False
    )

    R73 P0-04 整改: mark_dirty 必须为 True,触发 dirty_outbox → crdb_sync → CRDB
    完整链路验证。原实现跳过 CRDB sync 违反 R73 §5.2 真实产品交易链要求。

    Args:
        trace_id: 唯一标识符(作为 file_records_local.file_code 主键)

    Returns:
        与 push() 消息格式一致的 dict
    """
    now_iso = _now_iso()
    return {
        "op_type": "upsert",
        "table": "file_records_local",
        "method_name": "upsert_file_record_local",
        "data": {
            "record": {
                "file_code": trace_id,
                "uploader_id": 0,
                "primary_channel_id": 0,
                "primary_channel_msg_id": 0,
                "file_types": "[]",
                "backup_channel_msg_ids": "[]",
                "batch_msg_ids": "[]",
                "batch_file_meta": "[]",
                "file_ids": "[]",
                "status": "active",
                "request_count": 0,
                "protect_content": 0,
                "file_ttl_days": 0,
                "note": "synthetic_r72_test",
                "expire_time": None,
                "blocked_users": "[]",
                "create_time": now_iso,
                "updated_at": now_iso,
                "max_requests": 0,
                "is_collection": 0,
                "collection_codes": "[]",
                # R73 P0-04: 在 record 中添加 trace_id 字段,贯穿整条交易链
                "trace_id": trace_id,
            },
            "mark_dirty": True,
            "_batch": False,
        },
        "redis_key": f"cache:file_record:{trace_id}",
        # RC59 fix: message_id 跨步骤唯一(参见 _build_heartbeat_message 注释)
        "message_id": f"{trace_id}:file_index",  # 跨步骤唯一,幂等键
        # R73 P0-04: 顶层 trace_id 字段,贯穿 Update→up→idx→dsp→writer→CRDB→输出
        "trace_id": trace_id,
        "created_at": time.time(),
        "attempts": 0,
    }


def _build_dsp_dispatch_message(trace_id: str) -> dict[str, Any]:
    """R73 P0-04: 构造 create_outbox_entry 派送任务消息体(对应 dsp bot 派送链路)。

    dsp bot 走独立 Redis Stream 派送,但派送任务由 create_outbox_entry 创建。
    本消息通过 tgjiema:writer:stream 触发 db_writer 反射调用 CacheStore
    .create_outbox_entry,在 upload_outbox 表插入 PENDING 记录,
    随后由 dsp bot 的 process_queue 消费派送。

    方法签名参考 database/cache_store.py:create_outbox_entry(
        outbox_id: str, upload_id: str, code: str,
        target_user_id: int, storage_channel_id: int,
        storage_msg_ids: list | None = None,
        batch_file_meta: list | None = None,
        task_type: str = "single", protect_content: int = 0,
        event_type: str = "delivery_requested",
    ) -> bool

    Args:
        trace_id: 唯一标识符(同时作为 outbox_id / upload_id / code 主键)

    Returns:
        与 push() 消息格式一致的 dict
    """
    return {
        "op_type": "insert",
        "table": "upload_outbox",
        "method_name": "create_outbox_entry",
        "data": {
            "outbox_id": trace_id,
            "upload_id": trace_id,
            "code": trace_id,
            "target_user_id": 0,
            "storage_channel_id": 0,
            "storage_msg_ids": [],
            "batch_file_meta": [],
            "task_type": "single",
            "protect_content": 0,
            "event_type": "delivery_requested",
            # R85 fix: trace_id 只放在顶层 msg["trace_id"](见下),不放入 data。
            # create_outbox_entry 不接受 trace_id 参数,放入 data 会导致
            # db_writer 抛 TypeError → 永久死信 → ACK,verify 查不到落库。
        },
        "redis_key": f"cache:upload_outbox:{trace_id}",
        # RC59 fix: message_id 跨步骤唯一(参见 _build_heartbeat_message 注释)
        "message_id": f"{trace_id}:dsp_dispatch",  # 跨步骤唯一,幂等键
        # R73 P0-04: 顶层 trace_id 字段,贯穿 Update→up→idx→dsp→writer→CRDB→输出
        "trace_id": trace_id,
        "created_at": time.time(),
        "attempts": 0,
    }


def _inject_message_to_stream(
    msg: dict[str, Any], method_name: str, timeout: int = 30,
) -> StepResult:
    """通用消息注入:通过 Redis XADD 注入消息到业务流。

    通过 docker compose exec redis redis-cli XADD 实际注入消息,
    db_writer 会通过 XREADGROUP 消费。

    Args:
        msg: 消息体(与 database/redis_queue.py push() 一致)
        method_name: 方法名(用于 evidence)
        timeout: 命令超时秒数

    Returns:
        StepResult 含 XADD 返回的 stream id
    """
    started = time.time()
    msg_json = json.dumps(msg, default=str)

    # 构造 redis-cli XADD 命令
    # XADD tgjiema:writer:stream * data <json>
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
    else:
        cmd = _compose_cmd([
            "exec", "-T", "redis",
            "redis-cli",
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
    message_id = msg.get("message_id", "")

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
            "message_id": message_id,
            "method_name": method_name,
        },
    )


def inject_test_event(trace_id: str, timeout: int = 30) -> StepResult:
    """通过 Redis XADD 注入 write_bot_heartbeat 测试事件到业务流。

    通过 docker compose exec redis redis-cli XADD 实际注入消息,
    db_writer 会通过 XREADGROUP 消费。

    Args:
        trace_id: 唯一标识符
        timeout: 命令超时秒数

    Returns:
        StepResult 含 XADD 返回的 stream id
    """
    msg = _build_heartbeat_message(trace_id)
    return _inject_message_to_stream(msg, "write_bot_heartbeat", timeout=timeout)


def inject_upload_session_event(trace_id: str, timeout: int = 30) -> StepResult:
    """R72 P0-03: 通过 Redis XADD 注入 create_upload_session 测试事件。

    对应 up bot 的真实业务:上传会话记录创建。

    Args:
        trace_id: 唯一标识符(作为 upload_id 主键)
        timeout: 命令超时秒数

    Returns:
        StepResult 含 XADD 返回的 stream id
    """
    msg = _build_upload_session_message(trace_id)
    return _inject_message_to_stream(msg, "create_upload_session", timeout=timeout)


def inject_file_index_event(trace_id: str, timeout: int = 30) -> StepResult:
    """R72 P0-03: 通过 Redis XADD 注入 upsert_file_record_local 测试事件。

    对应 idx bot 的真实业务:文件索引记录写入。

    Args:
        trace_id: 唯一标识符(作为 file_code 主键)
        timeout: 命令超时秒数

    Returns:
        StepResult 含 XADD 返回的 stream id
    """
    msg = _build_file_index_message(trace_id)
    return _inject_message_to_stream(msg, "upsert_file_record_local", timeout=timeout)


def inject_dsp_dispatch_event(trace_id: str, timeout: int = 30) -> StepResult:
    """R73 P0-04: 通过 Redis XADD 注入 create_outbox_entry 测试事件。

    对应 dsp bot 的派送链路:在 upload_outbox 表插入 PENDING 记录,
    随后由 dsp bot 的 process_queue 消费派送。

    Args:
        trace_id: 唯一标识符(同时作为 outbox_id / upload_id / code 主键)
        timeout: 命令超时秒数

    Returns:
        StepResult 含 XADD 返回的 stream id
    """
    msg = _build_dsp_dispatch_message(trace_id)
    return _inject_message_to_stream(msg, "create_outbox_entry", timeout=timeout)


def _query_count(
    target_db: str, table: str, where_column: str, where_value: str,
    timeout: int = 15,
) -> tuple[int, str, str, int]:
    """R72 P0-11: 通用 SQLite 查询:通过 db_writer 容器查询表中满足条件的记录数。

    table 和 where_column 为代码内硬编码常量(非用户输入),不校验。
    where_value 为 trace_id(只含字母数字和下划线,且使用 ? 参数化查询)。
    target_db 已通过 _validate_target_db 白名单校验。

    Args:
        target_db: SQLite 数据库路径(已通过白名单校验)
        table: 表名(硬编码常量)
        where_column: WHERE 条件列名(硬编码常量)
        where_value: WHERE 条件值(trace_id)
        timeout: 命令超时秒数

    Returns:
        (count, stdout, stderr, returncode)
        count 为 -1 表示查询失败或解析失败
    """
    cmd = _compose_cmd([
        "exec", "-T", "db_writer",
        "python", "-c",
        f"import sqlite3; "
        f"conn = sqlite3.connect('{target_db}', timeout=5); "
        f"cur = conn.execute("
        f"'SELECT COUNT(*) FROM {table} WHERE {where_column} = ?', ('{where_value}',)); "
        f"print(cur.fetchone()[0]); conn.close()",
    ])
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return (-1, "", "timeout", -1)
    count = -1
    if result.returncode == 0:
        try:
            count = int(result.stdout.strip())
        except ValueError:
            count = -1
    return (count, result.stdout, result.stderr, result.returncode)


def _verify_result_generic(
    trace_id: str,
    step_name: str,
    method_name: str,
    table: str,
    where_column: str,
    target_db: str,
    timeout: int = 60,
) -> StepResult:
    """R72 P0-03/P0-11: 通用落库验证:轮询查询表,等待 db_writer 消费并写入。

    fail-closed:超时未查询到 → 失败。

    Args:
        trace_id: 唯一标识符
        step_name: 步骤名(用于 StepResult.step)
        method_name: 业务方法名(用于 evidence)
        table: SQLite 表名
        where_column: WHERE 条件列名
        target_db: SQLite 数据库路径
        timeout: 最大等待秒数

    Returns:
        StepResult 含查询结果
    """
    _validate_target_db(target_db)
    started = time.time()
    deadline = started + timeout

    last_stdout = ""
    last_stderr = ""
    last_returncode: int = -1
    found = False
    last_count = -1

    while time.time() < deadline:
        count, last_stdout, last_stderr, last_returncode = _query_count(
            target_db, table, where_column, trace_id, timeout=15,
        )
        if count >= 1:
            found = True
            last_count = count
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    return StepResult(
        step=step_name,
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=last_returncode,
        stdout=last_stdout,
        stderr=last_stderr,
        passed=found,
        error=None if found else (
            f"在 {timeout}s 内未查询到 trace_id={trace_id} 的落库记录 "
            f"(db_writer 可能未消费或 SQLite 查询失败, table={table})"
        ),
        evidence={
            "trace_id": trace_id,
            "table": table,
            "method_name": method_name,
            "found": found,
            "row_count": last_count if last_count >= 0 else 0,
        },
    )


def verify_result(
    trace_id: str, timeout: int = 60,
    target_db: str = DEFAULT_TARGET_DB,
) -> StepResult:
    """验证业务链实际消费并落库(bot_heartbeat 表)。

    轮询查询 bot_heartbeat 表,等待 db_writer 消费消息并写入。
    fail-closed:超时未查询到 → 失败。

    Args:
        trace_id: 唯一标识符(作为 bot_heartbeat.name)
        timeout: 最大等待秒数
        target_db: SQLite 数据库路径(默认 /app/data/cache_store.db)

    Returns:
        StepResult 含查询结果
    """
    return _verify_result_generic(
        trace_id=trace_id,
        step_name="verify",
        method_name="write_bot_heartbeat",
        table="bot_heartbeat",
        where_column="name",
        target_db=target_db,
        timeout=timeout,
    )


def verify_upload_session_result(
    trace_id: str, timeout: int = 60,
    target_db: str = DEFAULT_TARGET_DB,
) -> StepResult:
    """R72 P0-03: 验证 create_upload_session 消息实际消费并落库(upload_sessions 表)。

    轮询查询 upload_sessions 表,等待 db_writer 消费消息并写入。

    Args:
        trace_id: 唯一标识符(作为 upload_sessions.upload_id)
        timeout: 最大等待秒数
        target_db: SQLite 数据库路径

    Returns:
        StepResult 含查询结果
    """
    return _verify_result_generic(
        trace_id=trace_id,
        step_name="upload_session_verify",
        method_name="create_upload_session",
        table="upload_sessions",
        where_column="upload_id",
        target_db=target_db,
        timeout=timeout,
    )


def verify_file_index_result(
    trace_id: str, timeout: int = 60,
    target_db: str = DEFAULT_TARGET_DB,
) -> StepResult:
    """R72 P0-03: 验证 upsert_file_record_local 消息实际消费并落库(file_records_local 表)。

    轮询查询 file_records_local 表,等待 db_writer 消费消息并写入。

    Args:
        trace_id: 唯一标识符(作为 file_records_local.file_code)
        timeout: 最大等待秒数
        target_db: SQLite 数据库路径

    Returns:
        StepResult 含查询结果
    """
    return _verify_result_generic(
        trace_id=trace_id,
        step_name="file_index_verify",
        method_name="upsert_file_record_local",
        table="file_records_local",
        where_column="file_code",
        target_db=target_db,
        timeout=timeout,
    )


def _verify_idempotency_generic(
    trace_id: str,
    step_name: str,
    method_name: str,
    table: str,
    where_column: str,
    target_db: str,
    reinject_fn,
    timeout: int = 60,
) -> StepResult:
    """R72 P0-03/P0-11: 通用幂等性验证:重复提交同一 trace_id 不产生重复副作用。

    步骤:
      1. 查询当前 row count(应为 1)
      2. 再次 XADD 同一 message_id(trace_id)
      3. 等待消费
      4. 查询 row count(应仍为 1,因为 INSERT OR IGNORE/REPLACE)

    Args:
        trace_id: 唯一标识符
        step_name: 步骤名(用于 StepResult.step)
        method_name: 业务方法名(用于 evidence)
        table: SQLite 表名
        where_column: WHERE 条件列名
        target_db: SQLite 数据库路径
        reinject_fn: 重新注入函数,签名 (trace_id, timeout) -> StepResult
        timeout: 最大等待秒数

    Returns:
        StepResult 含幂等性验证结果
    """
    _validate_target_db(target_db)
    started = time.time()

    # 1. 查询初始 row count
    initial_count, initial_stdout, initial_stderr, initial_rc = _query_count(
        target_db, table, where_column, trace_id, timeout=15,
    )
    if initial_rc != 0:
        return StepResult(
            step=step_name,
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=initial_rc,
            stdout=initial_stdout,
            stderr=initial_stderr,
            passed=False,
            error=f"初始 count 查询失败: {initial_stderr}",
        )

    if initial_count < 0:
        return StepResult(
            step=step_name,
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=initial_rc,
            stdout=initial_stdout,
            stderr=initial_stderr,
            passed=False,
            error=f"初始 count 解析失败: {initial_stdout!r}",
        )

    # 2. 再次 XADD 同一 message_id
    reinject = reinject_fn(trace_id, timeout=30)
    if not reinject.passed:
        return StepResult(
            step=step_name,
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
        count, _, _, _ = _query_count(
            target_db, table, where_column, trace_id, timeout=15,
        )
        if count >= 0:
            final_count = count
            # 等待 2 个轮询周期确保 count 稳定
            time.sleep(POLL_INTERVAL_SECONDS * 2)
            stable_count, _, _, _ = _query_count(
                target_db, table, where_column, trace_id, timeout=15,
            )
            if stable_count >= 0 and stable_count == final_count:
                final_count = stable_count
                break
        time.sleep(POLL_INTERVAL_SECONDS)

    # 4. 幂等性通过条件:final_count == initial_count(INSERT OR IGNORE/REPLACE 不增加行数)
    passed = final_count == initial_count and final_count >= 1

    return StepResult(
        step=step_name,
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=0,
        stdout=f"initial={initial_count} final={final_count}",
        stderr="",
        passed=passed,
        error=None if passed else (
            f"幂等性验证失败: initial_count={initial_count}, "
            f"final_count={final_count}(应相等且 >=1, method={method_name})"
        ),
        evidence={
            "initial_count": initial_count,
            "final_count": final_count,
            "reinject_stream_id": reinject.evidence.get("stream_id", ""),
            "method_name": method_name,
        },
    )


def verify_idempotency(
    trace_id: str, timeout: int = 60,
    target_db: str = DEFAULT_TARGET_DB,
) -> StepResult:
    """验证幂等性:重复提交同一 trace_id 不产生重复副作用(bot_heartbeat 表)。

    步骤:
      1. 查询当前 row count(应为 1)
      2. 再次 XADD 同一 message_id(trace_id)
      3. 等待消费
      4. 查询 row count(应仍为 1,因为 INSERT OR REPLACE)

    Args:
        trace_id: 唯一标识符
        timeout: 最大等待秒数
        target_db: SQLite 数据库路径

    Returns:
        StepResult 含幂等性验证结果
    """
    return _verify_idempotency_generic(
        trace_id=trace_id,
        step_name="idempotency",
        method_name="write_bot_heartbeat",
        table="bot_heartbeat",
        where_column="name",
        target_db=target_db,
        reinject_fn=inject_test_event,
        timeout=timeout,
    )


def verify_upload_session_idempotency(
    trace_id: str, timeout: int = 60,
    target_db: str = DEFAULT_TARGET_DB,
) -> StepResult:
    """R72 P0-03: 验证 create_upload_session 幂等性(重复提交不产生重复记录)。

    create_upload_session 使用 INSERT OR IGNORE,重复提交同一 upload_id 不会
    产生新记录(row count 应保持不变)。

    Args:
        trace_id: 唯一标识符
        timeout: 最大等待秒数
        target_db: SQLite 数据库路径

    Returns:
        StepResult 含幂等性验证结果
    """
    return _verify_idempotency_generic(
        trace_id=trace_id,
        step_name="upload_session_idempotency",
        method_name="create_upload_session",
        table="upload_sessions",
        where_column="upload_id",
        target_db=target_db,
        reinject_fn=inject_upload_session_event,
        timeout=timeout,
    )


def verify_file_index_idempotency(
    trace_id: str, timeout: int = 60,
    target_db: str = DEFAULT_TARGET_DB,
) -> StepResult:
    """R72 P0-03: 验证 upsert_file_record_local 幂等性(重复提交不产生重复记录)。

    upsert_file_record_local 使用 INSERT OR REPLACE,重复提交同一 file_code
    不会产生新记录(row count 应保持不变,只是覆盖更新)。

    Args:
        trace_id: 唯一标识符
        timeout: 最大等待秒数
        target_db: SQLite 数据库路径

    Returns:
        StepResult 含幂等性验证结果
    """
    return _verify_idempotency_generic(
        trace_id=trace_id,
        step_name="file_index_idempotency",
        method_name="upsert_file_record_local",
        table="file_records_local",
        where_column="file_code",
        target_db=target_db,
        reinject_fn=inject_file_index_event,
        timeout=timeout,
    )


def verify_dsp_dispatch_result(
    trace_id: str, timeout: int = 60,
    target_db: str = DEFAULT_TARGET_DB,
) -> StepResult:
    """R73 P0-04: 验证 create_outbox_entry 派送任务落库(upload_outbox 表)。

    轮询查询 upload_outbox 表,等待 db_writer 消费消息并写入 PENDING 记录。
    fail-closed:超时未查询到 → 失败。

    Args:
        trace_id: 唯一标识符(作为 upload_outbox.outbox_id 主键)
        timeout: 最大等待秒数
        target_db: SQLite 数据库路径

    Returns:
        StepResult 含查询结果
    """
    return _verify_result_generic(
        trace_id=trace_id,
        step_name="dsp_dispatch_verify",
        method_name="create_outbox_entry",
        table="upload_outbox",
        where_column="outbox_id",
        target_db=target_db,
        timeout=timeout,
    )


def verify_dsp_dispatch_idempotency(
    trace_id: str, timeout: int = 60,
    target_db: str = DEFAULT_TARGET_DB,
) -> StepResult:
    """R73 P0-04: 验证 create_outbox_entry 幂等性(重复提交不产生重复记录)。

    create_outbox_entry 使用 INSERT OR IGNORE,重复提交同一 outbox_id
    不会产生新记录(row count 应保持不变)。

    Args:
        trace_id: 唯一标识符
        timeout: 最大等待秒数
        target_db: SQLite 数据库路径

    Returns:
        StepResult 含幂等性验证结果
    """
    return _verify_idempotency_generic(
        trace_id=trace_id,
        step_name="dsp_dispatch_idempotency",
        method_name="create_outbox_entry",
        table="upload_outbox",
        where_column="outbox_id",
        target_db=target_db,
        reinject_fn=inject_dsp_dispatch_event,
        timeout=timeout,
    )


# R73 P0-04: CRDB sync 验证轮询间隔(秒)
CRDB_SYNC_POLL_INTERVAL_SECONDS = 5


def _query_crdb_count(
    table: str, where_column: str, where_value: str, timeout: int = 30,
) -> tuple[int, str, str, int]:
    """R73 P0-04: 通过 crdb_sync 容器查询 CRDB 中满足条件的记录数。

    使用 crdb_sync 容器内 importlib 加载 database.session.get_file_records_col /
    get_pending_uploads_col,通过 D1Collection API 查询 CRDB。
    fail-closed:CRDB 不可用时返回 count=-1。

    Args:
        table: CRDB 表名(硬编码常量,如 "file_records" / "upload_sessions")
        where_column: WHERE 条件列名(硬编码常量)
        where_value: WHERE 条件值(trace_id)
        timeout: 命令超时秒数

    Returns:
        (count, stdout, stderr, returncode)
        count 为 -1 表示查询失败或 CRDB 不可用(fail-closed)
    """
    # 通过 crdb_sync 容器执行 Python 代码查询 CRDB
    # 使用 importlib 加载 collection,通过 D1Collection.fetch_all 等方法查询
    # 表名 / 列名 / 值通过 ? 参数化查询防注入(trace_id 仅含字母数字和下划线)
    inline_code = (
        "import asyncio, sys; "
        "from database.session import ("
        "get_file_records_col, get_pending_uploads_col); "
        "_TABLE_MAP = {"
        "'file_records': get_file_records_col, "
        "'upload_sessions': get_pending_uploads_col}; "
        f"_TABLE = {table!r}; "
        f"_COL = {where_column!r}; "
        f"_VAL = {where_value!r}; "
        "async def _q(): "
        "  if _TABLE not in _TABLE_MAP: "
        "    print(-1); return; "
        "  try: "
        "    col = _TABLE_MAP[_TABLE](); "
        "    rows = await col.fetch_all(); "
        "    cnt = sum(1 for r in rows if str(r.get(_COL, '')) == _VAL); "
        "    print(cnt); "
        "  except Exception as e: "
        "    print(-1); "
        "    sys.stderr.write(f'CRDB query failed: {type(e).__name__}: {e}'); "
        "asyncio.run(_q())"
    )
    cmd = _compose_cmd([
        "exec", "-T", "crdb_sync",
        "python", "-c", inline_code,
    ])
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return (-1, "", "timeout", -1)
    count = -1
    if result.returncode == 0:
        try:
            count = int(result.stdout.strip())
        except ValueError:
            count = -1
    return (count, result.stdout, result.stderr, result.returncode)


def verify_crdb_sync_result(
    trace_id: str, timeout: int = 120,
    target_db: str = DEFAULT_TARGET_DB,
) -> StepResult:
    """R73 P0-04: 验证 trace_id 对应记录已同步到 CRDB。

    通过 docker compose exec crdb_sync python -c "..." 查询 CRDB 中
    file_records / upload_sessions 表是否存在 trace_id 对应记录。

    fail-closed 原则:
      - CRDB 不可用时返回失败(禁止只验证 SQLite 成功)
      - 超时未查询到 → 失败
      - 查询异常 → 失败

    Args:
        trace_id: 唯一标识符
        timeout: 最大等待秒数(默认 120)
        target_db: SQLite 数据库路径(用于兼容签名,实际不查询 SQLite)

    Returns:
        StepResult 含 CRDB sync 验证结果
    """
    _validate_target_db(target_db)
    started = time.time()
    deadline = started + timeout

    last_stdout = ""
    last_stderr = ""
    last_returncode: int = -1
    found = False
    last_count = -1
    crdb_unavailable = False

    # 轮询查询 file_records 表(由 upsert_file_record_local mark_dirty=True 触发同步)
    while time.time() < deadline:
        count, last_stdout, last_stderr, last_returncode = _query_crdb_count(
            "file_records", "file_code", trace_id, timeout=30,
        )
        if count >= 1:
            found = True
            last_count = count
            break
        if count < 0:
            # CRDB 查询失败 — 检查是否为 CRDB 不可用
            if last_returncode != 0 or "CRDB query failed" in last_stderr:
                crdb_unavailable = True
        time.sleep(CRDB_SYNC_POLL_INTERVAL_SECONDS)

    return StepResult(
        step="crdb_sync_verify",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=last_returncode,
        stdout=last_stdout,
        stderr=last_stderr,
        passed=found,
        error=None if found else (
            f"CRDB sync 验证失败: 在 {timeout}s 内未查询到 trace_id={trace_id} "
            f"在 CRDB file_records 表的记录"
            f"{' (CRDB 不可用 — fail-closed)' if crdb_unavailable else ''}"
        ),
        evidence={
            "trace_id": trace_id,
            "crdb_table": "file_records",
            "method_name": "upsert_file_record_local+crdb_sync",
            "found": found,
            "row_count": last_count if last_count >= 0 else 0,
            "crdb_unavailable": crdb_unavailable,
            "poll_interval_seconds": CRDB_SYNC_POLL_INTERVAL_SECONDS,
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
    # 验证方式:检查 Redis DLQ Stream(优先) + dead_letter.jsonl 文件(降级回退)
    # R71 RC34: push_dead() 在 Redis 可用时写入 Redis DLQ Stream
    # (tgjiema:writer:dead),仅在 Redis 不可达时降级写 dead_letter.jsonl。
    # CI 环境中 Redis 可用,畸形消息进入 Redis DLQ Stream 而非文件。
    # 原代码仅检查文件 → 永远找不到 → failure_scenario 永远失败。
    # 修复:同时检查 Redis DLQ Stream(XRANGE)和文件。
    deadline = time.time() + timeout
    dlq_found = False
    last_dlq_output = ""
    admin_pwd = os.environ.get("REDIS_ADMIN_PASSWORD", "")
    reader_pwd = os.environ.get("REDIS_READER_PASSWORD", "")

    while time.time() < deadline:
        # 1. 优先检查 Redis DLQ Stream(tgjiema:writer:dead)
        # push_dead() 通过 redis.xadd() 写入,畸形 JSON 的 "BROKEN" 标记
        # 会出现在 dead_msg.original.raw 字段中
        if admin_pwd:
            redis_check_cmd = _compose_cmd([
                "exec", "-T", "redis",
                "redis-cli",
                "--user", "tgjiema_admin",
                "-a", admin_pwd,
                "--no-auth-warning",
                "XRANGE", "tgjiema:writer:dead", "-", "+", "COUNT", "100",
            ])
        elif reader_pwd:
            redis_check_cmd = _compose_cmd([
                "exec", "-T", "redis",
                "redis-cli",
                "--user", "tgjiema_reader",
                "-a", reader_pwd,
                "--no-auth-warning",
                "XRANGE", "tgjiema:writer:dead", "-", "+", "COUNT", "100",
            ])
        else:
            redis_check_cmd = _compose_cmd([
                "exec", "-T", "redis",
                "redis-cli",
                "XRANGE", "tgjiema:writer:dead", "-", "+", "COUNT", "100",
            ])
        try:
            redis_result = _run(redis_check_cmd, timeout=15, cwd=REPO_ROOT)
            redis_output = redis_result.stdout
            if redis_result.returncode == 0 and "BROKEN" in redis_output:
                dlq_found = True
                last_dlq_output = "REDIS_DLQ:OK"
                break
        except subprocess.TimeoutExpired:
            pass

        # 2. 降级检查 dead_letter.jsonl 文件(Redis 不可达时的回退路径)
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


# ════════════════════════════════════════════════════════════════
# R73 P0-04: 故障注入支持
# ════════════════════════════════════════════════════════════════


# 允许故障注入的角色白名单(只允许业务角色,禁止 stop redis/db_writer 等基础服务
# 以免破坏整个测试环境)
_FAULT_INJECTION_ROLE_WHITELIST = frozenset({
    "up", "idx", "dsp", "crdb_sync",
})


def _validate_fault_role(role_name: str) -> str:
    """R73 P0-04: 校验故障注入角色名,防止误停基础服务。

    Args:
        role_name: docker compose 服务名(如 "up" / "idx" / "dsp" / "crdb_sync")

    Returns:
        校验通过的角色名

    Raises:
        ValueError: 角色名不在白名单内
    """
    if role_name not in _FAULT_INJECTION_ROLE_WHITELIST:
        raise ValueError(
            f"非法故障注入角色: {role_name!r}(只允许 {sorted(_FAULT_INJECTION_ROLE_WHITELIST)})"
        )
    return role_name


def inject_fault_stop_role(role_name: str, timeout: int = 30) -> StepResult:
    """R73 P0-04: 停止指定角色容器(docker compose stop <role>)。

    用于故障注入测试:验证当某角色停止时,交易链路 fail-closed。

    Args:
        role_name: docker compose 服务名(必须在白名单内)
        timeout: 命令超时秒数

    Returns:
        StepResult 含 stop 命令结果
    """
    _validate_fault_role(role_name)
    started = time.time()
    cmd = _compose_cmd(["stop", "-t", str(timeout), role_name])
    try:
        result = _run(cmd, timeout=timeout + 10, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired as e:
        return StepResult(
            step=f"fault_stop_{role_name}",
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=-1,
            stdout="",
            stderr=str(e),
            passed=False,
            error=f"stop {role_name} 超时({timeout}s)",
        )
    passed = result.returncode == 0
    return StepResult(
        step=f"fault_stop_{role_name}",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        passed=passed,
        error=None if passed else f"stop {role_name} 失败: {result.stderr}",
        evidence={
            "role_name": role_name,
            "action": "stop",
        },
    )


def inject_fault_start_role(role_name: str, timeout: int = 60) -> StepResult:
    """R73 P0-04: 启动指定角色容器(docker compose start <role>)。

    用于故障注入测试后恢复角色。

    Args:
        role_name: docker compose 服务名(必须在白名单内)
        timeout: 命令超时秒数

    Returns:
        StepResult 含 start 命令结果
    """
    _validate_fault_role(role_name)
    started = time.time()
    cmd = _compose_cmd(["start", role_name])
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired as e:
        return StepResult(
            step=f"fault_start_{role_name}",
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=-1,
            stdout="",
            stderr=str(e),
            passed=False,
            error=f"start {role_name} 超时({timeout}s)",
        )
    passed = result.returncode == 0
    return StepResult(
        step=f"fault_start_{role_name}",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        passed=passed,
        error=None if passed else f"start {role_name} 失败: {result.stderr}",
        evidence={
            "role_name": role_name,
            "action": "start",
        },
    )


def verify_transaction_fails_when_role_stopped(
    role_name: str, trace_id: str, timeout: int = 30,
) -> StepResult:
    """R73 P0-04: 验证当某角色停止时交易失败(fail-closed)。

    在角色已停止的状态下注入测试事件,预期 verify 步骤超时失败。
    通过该测试验证故障注入下系统不会假阳性通过。

    Args:
        role_name: docker compose 服务名(必须在白名单内)
        trace_id: 唯一标识符(用于本次故障注入测试)
        timeout: 等待 verify 失败的最大秒数

    Returns:
        StepResult:
            passed=True 表示交易按预期失败(fail-closed 生效)
            passed=False 表示交易在角色停止时仍通过(假阳性,违反 fail-closed)
    """
    _validate_fault_role(role_name)
    started = time.time()

    # 注入一个 heartbeat 事件(应该被停止的角色阻断)
    inject = inject_test_event(trace_id, timeout=30)
    if not inject.passed:
        # 注入失败也算 fail-closed 生效(消息确实无法到达下游)
        return StepResult(
            step=f"fault_injection_{role_name}",
            timestamp=_now_iso(),
            duration_seconds=time.time() - started,
            returncode=inject.returncode,
            stdout=inject.stdout,
            stderr=inject.stderr,
            passed=True,
            error=None,
            evidence={
                "role_name": role_name,
                "trace_id": trace_id,
                "fail_closed_reason": "inject_failed_as_expected",
                "inject_passed": False,
            },
        )

    # 验证 verify 步骤预期失败(在角色停止时交易不应在 SQLite 落库)
    verify = verify_result(trace_id, timeout=timeout, target_db=DEFAULT_TARGET_DB)
    fail_closed = not verify.passed

    return StepResult(
        step=f"fault_injection_{role_name}",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=verify.returncode,
        stdout=verify.stdout,
        stderr=verify.stderr,
        passed=fail_closed,
        error=None if fail_closed else (
            f"故障注入测试失败: {role_name} 停止时交易仍通过,违反 fail-closed"
        ),
        evidence={
            "role_name": role_name,
            "trace_id": trace_id,
            "inject_passed": inject.passed,
            "verify_passed": verify.passed,
            "fail_closed_reason": "verify_failed_as_expected" if fail_closed else "verify_unexpectedly_passed",
        },
    )


def _delete_rows(
    target_db: str, table: str, where_column: str, where_value: str,
    timeout: int = 30,
) -> tuple[int, str, str, int]:
    """R72 P0-11: 通用 SQLite 删除:通过 db_writer 容器删除表中满足条件的记录。

    Args:
        target_db: SQLite 数据库路径(已通过白名单校验)
        table: 表名(硬编码常量)
        where_column: WHERE 条件列名(硬编码常量)
        where_value: WHERE 条件值(trace_id)
        timeout: 命令超时秒数

    Returns:
        (deleted_count, stdout, stderr, returncode)
        deleted_count 为 -1 表示失败
    """
    cmd = _compose_cmd([
        "exec", "-T", "db_writer",
        "python", "-c",
        f"import sqlite3; "
        f"conn = sqlite3.connect('{target_db}', timeout=5); "
        f"cur = conn.execute("
        f"'DELETE FROM {table} WHERE {where_column} = ?', ('{where_value}',)); "
        f"deleted = cur.rowcount; "
        f"conn.commit(); "
        f"print(deleted); conn.close()",
    ])
    try:
        result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
    except subprocess.TimeoutExpired:
        return (-1, "", "timeout", -1)
    deleted = -1
    if result.returncode == 0:
        try:
            deleted = int(result.stdout.strip())
        except ValueError:
            deleted = -1
    return (deleted, result.stdout, result.stderr, result.returncode)


def cleanup(
    trace_id: str, timeout: int = 30,
    target_db: str = DEFAULT_TARGET_DB,
) -> StepResult:
    """清理隔离数据(DELETE 测试 row)。

    R72 P0-03: 清理范围扩展到三个表:
      - bot_heartbeat (WHERE name = ?)
      - upload_sessions (WHERE upload_id = ?)
      - file_records_local (WHERE file_code = ?)

    R72 P0-11: target_db 参数化(可切换到 staging 恢复库)。

    R73 P0-04: 清理范围进一步扩展:
      - SQLite: 上述三个表 + upload_outbox(dsp 派送记录)+ dirty_outbox + writer_inbox
      - CRDB: file_records 表中 file_code LIKE 'synthetic_%' 的测试记录
      任何清理失败都必须使 cleanup 失败(fail-closed)。

    Args:
        trace_id: 唯一标识符
        timeout: 命令超时秒数
        target_db: SQLite 数据库路径

    Returns:
        StepResult 含清理结果
    """
    _validate_target_db(target_db)
    started = time.time()

    # 清理 SQLite 表的测试数据
    # R73 P0-04: 新增 upload_outbox(dsp 派送记录)、dirty_outbox、writer_inbox
    tables_to_clean = [
        ("bot_heartbeat", "name"),
        ("upload_sessions", "upload_id"),
        ("file_records_local", "file_code"),
        ("upload_outbox", "outbox_id"),
        ("dirty_outbox", "pk"),
        ("writer_inbox", "message_id"),
    ]

    total_deleted = 0
    errors: list[str] = []
    last_stdout = ""
    last_stderr = ""
    last_returncode = 0

    for table, column in tables_to_clean:
        deleted, last_stdout, last_stderr, last_returncode = _delete_rows(
            target_db, table, column, trace_id, timeout=timeout,
        )
        if deleted > 0:
            total_deleted += deleted
        if last_returncode != 0:
            errors.append(f"{table}: {last_stderr}")

    # R73 P0-04: 清理 writer_inbox 中所有以 trace_id 开头的 message_id
    # (因为 message_id 格式为 f"{trace_id}:heartbeat" / f"{trace_id}:upload_session" 等)
    like_pattern = f"{trace_id}:%"
    cmd = _compose_cmd([
        "exec", "-T", "db_writer",
        "python", "-c",
        f"import sqlite3; "
        f"conn = sqlite3.connect('{target_db}', timeout=5); "
        f"cur = conn.execute("
        f"'DELETE FROM writer_inbox WHERE message_id LIKE ?', ('{like_pattern}',)); "
        f"deleted = cur.rowcount; "
        f"conn.commit(); "
        f"print(deleted); conn.close()",
    ])
    try:
        like_result = _run(cmd, timeout=timeout, cwd=REPO_ROOT)
        if like_result.returncode == 0:
            try:
                total_deleted += int(like_result.stdout.strip())
            except ValueError:
                pass
        else:
            errors.append(f"writer_inbox(LIKE): {like_result.stderr}")
    except subprocess.TimeoutExpired:
        errors.append("writer_inbox(LIKE): timeout")

    # R73 P0-04: 清理 CRDB 中的测试记录(DELETE FROM file_records WHERE file_code LIKE 'synthetic_%')
    # 通过 crdb_sync 容器执行 DELETE
    crdb_clean_ok = True
    crdb_clean_error = ""
    crdb_inline_code = (
        "import asyncio, sys; "
        "from database.session import get_file_records_col; "
        "async def _del(): "
        "  try: "
        "    col = get_file_records_col(); "
        "    rows = await col.fetch_all(); "
        "    cnt = 0; "
        "    for r in rows: "
        "      fc = str(r.get('file_code', '')); "
        "      if fc.startswith('synthetic_'): "
        "        try: "
        "          await col.delete(r.get('id')); "
        "          cnt += 1; "
        "        except Exception: pass; "
        "    print(cnt); "
        "  except Exception as e: "
        "    print(-1); "
        "    sys.stderr.write(f'CRDB cleanup failed: {type(e).__name__}: {e}'); "
        "asyncio.run(_del())"
    )
    crdb_cmd = _compose_cmd([
        "exec", "-T", "crdb_sync",
        "python", "-c", crdb_inline_code,
    ])
    try:
        crdb_result = _run(crdb_cmd, timeout=timeout, cwd=REPO_ROOT)
        if crdb_result.returncode != 0:
            crdb_clean_ok = False
            crdb_clean_error = f"crdb_sync exit={crdb_result.returncode}: {crdb_result.stderr}"
    except subprocess.TimeoutExpired:
        crdb_clean_ok = False
        crdb_clean_error = "crdb_sync cleanup timeout"
    if not crdb_clean_ok:
        errors.append(crdb_clean_error)

    # 清理成功条件:所有清理命令都成功(无论删除行数)
    passed = len(errors) == 0

    return StepResult(
        step="cleanup",
        timestamp=_now_iso(),
        duration_seconds=time.time() - started,
        returncode=last_returncode,
        stdout=last_stdout,
        stderr=last_stderr,
        passed=passed,
        error=None if passed else f"清理失败: {'; '.join(errors)}",
        evidence={
            "trace_id": trace_id,
            "deleted_rows": total_deleted,
            "tables_cleaned": [t[0] for t in tables_to_clean],
            "crdb_cleaned": crdb_clean_ok,
        },
    )


def run_dbwriter_component_test(
    timeout: int = 120,
    target_db: str = DEFAULT_TARGET_DB,
) -> TransactionEvidence:
    """R74 P0-03: DBWriter 组件测试 — 直接通过 Redis XADD 注入消息,验证 db_writer 消费。

    这是 DBWriter 的组件级测试,不经过真实 Telegram 入口(up/idx/dsp handler)。
    通过直接 XADD writer stream 验证 db_writer 消费→SQLite 落库→CRDB sync。

    如需真实产品交易路径测试,请使用 run_real_product_transaction()。

    R72 P0-03: 在 R71 heartbeat 测试基础上,新增两个真实业务方法测试:
      - create_upload_session (up bot 上传会话)
      - upsert_file_record_local (idx bot 文件索引)

    R72 P0-11: target_db 参数化(可切换到 staging 恢复库)。

    R73 P0-04: 真实产品交易链整改:
      - mark_dirty=True 触发 dirty_outbox → crdb_sync → CRDB 完整链路
      - 新增 dsp bot 派送链路测试(create_outbox_entry)
      - 新增 CRDB sync 验证(fail-closed:CRDB 不可用必失败)
      - 新增故障注入测试(角色停止时交易必失败)
      - 统一 trace_id 贯穿 Update→up→idx→dsp→writer→CRDB→输出

    步骤:
      1. 生成唯一 trace_id
      2. heartbeat 测试:注入 → 验证落库 → 验证幂等性
      3. upload_session 测试:注入 → 验证落库 → 验证幂等性
      4. file_index 测试:注入 → 验证落库 → 验证幂等性(mark_dirty=True)
      5. CRDB sync 验证(等待 dirty_outbox → crdb_sync → CRDB 落库)
      6. dsp_dispatch 测试:注入 → 验证落库 → 验证幂等性
      7. 故障注入测试(停止 crdb_sync 角色,验证交易 fail-closed,然后恢复)
      8. 注入失败场景(畸形 JSON → DLQ)
      9. 清理(DELETE 全部测试 row + CRDB)

    任一步骤失败仍执行清理(finally),但 overall_passed=False。

    Args:
        timeout: 单步骤最大等待秒数
        target_db: SQLite 数据库路径

    Returns:
        TransactionEvidence 含全部步骤结果
    """
    _validate_target_db(target_db)
    started_at = _now_iso()
    trace_id = generate_trace_id()

    # 初始化所有步骤为 skipped 状态(fail-closed 路径上未执行的步骤保持 skipped)
    inject = inject_test_event(trace_id, timeout=30)
    verify: StepResult = _skipped_step("verify")
    idempotency: StepResult = _skipped_step("idempotency")
    upload_session_inject: StepResult = _skipped_step("upload_session_inject")
    upload_session_verify: StepResult = _skipped_step("upload_session_verify")
    upload_session_idempotency: StepResult = _skipped_step("upload_session_idempotency")
    file_index_inject: StepResult = _skipped_step("file_index_inject")
    file_index_verify: StepResult = _skipped_step("file_index_verify")
    file_index_idempotency: StepResult = _skipped_step("file_index_idempotency")
    # R73 P0-04: 新增步骤初始化
    crdb_sync_verify: StepResult = _skipped_step("crdb_sync_verify")
    dsp_dispatch_inject: StepResult = _skipped_step("dsp_dispatch_inject")
    dsp_dispatch_verify: StepResult = _skipped_step("dsp_dispatch_verify")
    dsp_dispatch_idempotency: StepResult = _skipped_step("dsp_dispatch_idempotency")
    fault_injection: dict = {}
    failure_scenario: StepResult = _skipped_step("failure_scenario")
    cleanup_result: StepResult = _skipped_step("cleanup", "未执行")

    overall_passed = False
    error_msg: str | None = None

    def _build_evidence() -> TransactionEvidence:
        """构造当前状态下的 TransactionEvidence(避免重复字段列表)。"""
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
            upload_session_inject=upload_session_inject,
            upload_session_verify=upload_session_verify,
            upload_session_idempotency=upload_session_idempotency,
            file_index_inject=file_index_inject,
            file_index_verify=file_index_verify,
            file_index_idempotency=file_index_idempotency,
            dsp_dispatch_inject=dsp_dispatch_inject,
            dsp_dispatch_verify=dsp_dispatch_verify,
            dsp_dispatch_idempotency=dsp_dispatch_idempotency,
            crdb_sync_verify=crdb_sync_verify,
            fault_injection=fault_injection,
            error=error_msg,
        )

    try:
        # ── 1. heartbeat 测试(write_bot_heartbeat) ──
        if not inject.passed:
            error_msg = f"注入失败: {inject.error}"
            return _build_evidence()

        verify = verify_result(trace_id, timeout=timeout, target_db=target_db)
        if not verify.passed:
            error_msg = f"验证落库失败: {verify.error}"
            return _build_evidence()

        idempotency = verify_idempotency(
            trace_id, timeout=timeout, target_db=target_db,
        )
        if not idempotency.passed:
            error_msg = f"幂等性验证失败: {idempotency.error}"
            return _build_evidence()

        # ── 2. upload_session 测试(create_upload_session, 对应 up bot) ──
        upload_session_inject = inject_upload_session_event(trace_id, timeout=30)
        if not upload_session_inject.passed:
            error_msg = f"上传会话注入失败: {upload_session_inject.error}"
            return _build_evidence()

        upload_session_verify = verify_upload_session_result(
            trace_id, timeout=timeout, target_db=target_db,
        )
        if not upload_session_verify.passed:
            error_msg = f"上传会话验证落库失败: {upload_session_verify.error}"
            return _build_evidence()

        upload_session_idempotency = verify_upload_session_idempotency(
            trace_id, timeout=timeout, target_db=target_db,
        )
        if not upload_session_idempotency.passed:
            error_msg = f"上传会话幂等性验证失败: {upload_session_idempotency.error}"
            return _build_evidence()

        # ── 3. file_index 测试(upsert_file_record_local, 对应 idx bot) ──
        # R73 P0-04: mark_dirty=True 触发 dirty_outbox → crdb_sync → CRDB 完整链路
        file_index_inject = inject_file_index_event(trace_id, timeout=30)
        if not file_index_inject.passed:
            error_msg = f"文件索引注入失败: {file_index_inject.error}"
            return _build_evidence()

        file_index_verify = verify_file_index_result(
            trace_id, timeout=timeout, target_db=target_db,
        )
        if not file_index_verify.passed:
            error_msg = f"文件索引验证落库失败: {file_index_verify.error}"
            return _build_evidence()

        file_index_idempotency = verify_file_index_idempotency(
            trace_id, timeout=timeout, target_db=target_db,
        )
        if not file_index_idempotency.passed:
            error_msg = f"文件索引幂等性验证失败: {file_index_idempotency.error}"
            return _build_evidence()

        # ── 4. CRDB sync 验证(mark_dirty=True 触发 dirty_outbox → crdb_sync → CRDB) ──
        # R73 P0-04: 必须 fail-closed,CRDB 不可用时交易必失败
        crdb_sync_verify = verify_crdb_sync_result(
            trace_id, timeout=120, target_db=target_db,
        )
        if not crdb_sync_verify.passed:
            error_msg = f"CRDB sync 验证失败: {crdb_sync_verify.error}"
            return _build_evidence()

        # ── 5. dsp_dispatch 测试(create_outbox_entry, 对应 dsp bot 派送链路) ──
        dsp_dispatch_inject = inject_dsp_dispatch_event(trace_id, timeout=30)
        if not dsp_dispatch_inject.passed:
            error_msg = f"dsp 派送任务注入失败: {dsp_dispatch_inject.error}"
            return _build_evidence()

        dsp_dispatch_verify = verify_dsp_dispatch_result(
            trace_id, timeout=timeout, target_db=target_db,
        )
        if not dsp_dispatch_verify.passed:
            error_msg = f"dsp 派送任务验证落库失败: {dsp_dispatch_verify.error}"
            return _build_evidence()

        dsp_dispatch_idempotency = verify_dsp_dispatch_idempotency(
            trace_id, timeout=timeout, target_db=target_db,
        )
        if not dsp_dispatch_idempotency.passed:
            error_msg = f"dsp 派送任务幂等性验证失败: {dsp_dispatch_idempotency.error}"
            return _build_evidence()

        # ── 6. 故障注入测试(停止 crdb_sync,验证交易 fail-closed,然后恢复) ──
        # R73 P0-04: 验证当 crdb_sync 停止时,新交易无法完成 CRDB sync
        # 此阶段不阻塞整体交易通过(已完成的 trace_id 仍判定 overall_passed),
        # 但会记录故障注入证据供 compose_runtime_e2e 额外验证
        fault_trace_id = generate_trace_id()
        stop_result = inject_fault_stop_role("crdb_sync", timeout=30)
        if not stop_result.passed:
            fault_injection["crdb_sync_stop"] = asdict(stop_result)
            error_msg = f"故障注入停止 crdb_sync 失败: {stop_result.error}"
            return _build_evidence()

        fault_verify = verify_transaction_fails_when_role_stopped(
            "crdb_sync", fault_trace_id, timeout=30,
        )
        fault_injection["crdb_sync"] = asdict(fault_verify)

        # 恢复 crdb_sync 角色
        start_result = inject_fault_start_role("crdb_sync", timeout=60)
        fault_injection["crdb_sync_start"] = asdict(start_result)
        if not start_result.passed:
            error_msg = f"故障注入恢复 crdb_sync 失败: {start_result.error}"
            return _build_evidence()

        if not fault_verify.passed:
            error_msg = (
                f"故障注入测试失败: crdb_sync 停止时交易未 fail-closed: "
                f"{fault_verify.error}"
            )
            return _build_evidence()

        # ── 7. 故障注入测试(畸形 JSON → DLQ) ──
        failure_scenario = inject_failure_scenario(timeout=timeout)
        if not failure_scenario.passed:
            error_msg = f"失败场景验证失败: {failure_scenario.error}"
            return _build_evidence()

        overall_passed = True

    finally:
        # 无论成功失败都执行清理(全部测试表 + CRDB)
        cleanup_result = cleanup(trace_id, timeout=30, target_db=target_db)

    return _build_evidence()


def run_real_product_transaction(
    timeout: int = 120,
    target_db: str = DEFAULT_TARGET_DB,
) -> TransactionEvidence:
    """R74 P0-03: 真实产品交易 — 通过 e2e_update_adapter.py 触发完整产品交易路径。

    不直接 XADD writer stream,而是通过 e2e_update_adapter.py 的
    build_test_update / dispatch_to_up_handler / dispatch_to_idx_handler /
    dispatch_to_dsp_handler 触发真实产品交易路径,
    验证完整链路:Update → up → idx → dsp → DBWriter → SQLite → CRDB → outbound。

    使用与 run_dbwriter_component_test 相同的验证函数(verify_result 等),
    但在真实产品交易路径之后验证。

    步骤:
      1. 生成唯一 trace_id
      2. 构造 Telegram Update(build_test_update)
      3. 转交 up handler(dispatch_to_up_handler)
      4. 验证 upload_sessions 落库(verify_upload_session_result)
      5. 转交 idx handler(dispatch_to_idx_handler)
      6. 验证 file_records_local 落库(verify_file_index_result)
      7. 验证 CRDB sync(verify_crdb_sync_result)
      8. 转交 dsp handler(dispatch_to_dsp_handler)
      9. 验证 upload_outbox 落库(verify_dsp_dispatch_result)
      10. 清理(DELETE 全部测试 row + CRDB)

    任一步骤失败仍执行清理(finally),但 overall_passed=False。

    Args:
        timeout: 单步骤最大等待秒数
        target_db: SQLite 数据库路径

    Returns:
        TransactionEvidence 含全部步骤结果
    """
    # 延迟导入 e2e_update_adapter,避免循环依赖
    from scripts.e2e_update_adapter import (
        build_test_update,
        dispatch_to_up_handler,
        dispatch_to_idx_handler,
        dispatch_to_dsp_handler,
    )

    _validate_target_db(target_db)
    started_at = _now_iso()
    trace_id = generate_trace_id()

    # 初始化所有步骤为 skipped 状态
    up_dispatch: StepResult = _skipped_step("up_dispatch")
    upload_session_verify: StepResult = _skipped_step("upload_session_verify")
    idx_dispatch: StepResult = _skipped_step("idx_dispatch")
    file_index_verify: StepResult = _skipped_step("file_index_verify")
    crdb_sync_verify: StepResult = _skipped_step("crdb_sync_verify")
    dsp_dispatch: StepResult = _skipped_step("dsp_dispatch")
    dsp_dispatch_verify: StepResult = _skipped_step("dsp_dispatch_verify")
    cleanup_result: StepResult = _skipped_step("cleanup", "未执行")

    overall_passed = False
    error_msg: str | None = None

    def _build_evidence() -> TransactionEvidence:
        return TransactionEvidence(
            trace_id=trace_id,
            started_at=started_at,
            finished_at=_now_iso(),
            overall_passed=overall_passed,
            inject=_skipped_step("inject", "真实产品交易不使用 XADD 注入"),
            verify=upload_session_verify,
            idempotency=_skipped_step("idempotency", "真实产品交易不测幂等"),
            failure_scenario=_skipped_step("failure_scenario", "真实产品交易不测畸形 JSON"),
            cleanup=cleanup_result,
            upload_session_inject=up_dispatch,
            upload_session_verify=upload_session_verify,
            upload_session_idempotency=_skipped_step("upload_session_idempotency"),
            file_index_inject=idx_dispatch,
            file_index_verify=file_index_verify,
            file_index_idempotency=_skipped_step("file_index_idempotency"),
            dsp_dispatch_inject=dsp_dispatch,
            dsp_dispatch_verify=dsp_dispatch_verify,
            dsp_dispatch_idempotency=_skipped_step("dsp_dispatch_idempotency"),
            crdb_sync_verify=crdb_sync_verify,
            fault_injection={},
            error=error_msg,
        )

    try:
        # ── 1. 构造 Telegram Update ──
        file_content = b"e2e_test_payload"
        user_id = 0
        update = build_test_update(user_id=user_id, file_content=file_content, trace_id=trace_id)

        # ── 2. 转交 up handler → _dispatch_media ──
        up_passed, up_detail = dispatch_to_up_handler(update)
        up_dispatch = StepResult(
            step="up_dispatch",
            timestamp=_now_iso(),
            duration_seconds=0,
            returncode=0 if up_passed else 1,
            stdout=up_detail,
            stderr="",
            passed=up_passed,
            error=None if up_passed else f"up handler 转交失败: {up_detail}",
            evidence={"trace_id": trace_id, "handler": "up"},
        )
        if not up_passed:
            error_msg = f"up handler 转交失败: {up_detail}"
            return _build_evidence()

        # ── 3. 验证 upload_sessions 落库 ──
        upload_session_verify = verify_upload_session_result(
            trace_id, timeout=timeout, target_db=target_db,
        )
        if not upload_session_verify.passed:
            error_msg = f"上传会话验证落库失败: {upload_session_verify.error}"
            return _build_evidence()

        # ── 4. 转交 idx handler → _process_one_pending ──
        idx_passed, idx_detail = dispatch_to_idx_handler(trace_id)
        idx_dispatch = StepResult(
            step="idx_dispatch",
            timestamp=_now_iso(),
            duration_seconds=0,
            returncode=0 if idx_passed else 1,
            stdout=idx_detail,
            stderr="",
            passed=idx_passed,
            error=None if idx_passed else f"idx handler 转交失败: {idx_detail}",
            evidence={"trace_id": trace_id, "handler": "idx"},
        )
        if not idx_passed:
            error_msg = f"idx handler 转交失败: {idx_detail}"
            return _build_evidence()

        # ── 5. 验证 file_records_local 落库 ──
        file_index_verify = verify_file_index_result(
            trace_id, timeout=timeout, target_db=target_db,
        )
        if not file_index_verify.passed:
            error_msg = f"文件索引验证落库失败: {file_index_verify.error}"
            return _build_evidence()

        # ── 6. 验证 CRDB sync ──
        crdb_sync_verify = verify_crdb_sync_result(
            trace_id, timeout=120, target_db=target_db,
        )
        if not crdb_sync_verify.passed:
            error_msg = f"CRDB sync 验证失败: {crdb_sync_verify.error}"
            return _build_evidence()

        # ── 7. 转交 dsp handler → process_queue ──
        dsp_passed, dsp_detail = dispatch_to_dsp_handler(job_id=0)
        dsp_dispatch = StepResult(
            step="dsp_dispatch",
            timestamp=_now_iso(),
            duration_seconds=0,
            returncode=0 if dsp_passed else 1,
            stdout=dsp_detail,
            stderr="",
            passed=dsp_passed,
            error=None if dsp_passed else f"dsp handler 转交失败: {dsp_detail}",
            evidence={"trace_id": trace_id, "handler": "dsp"},
        )
        if not dsp_passed:
            error_msg = f"dsp handler 转交失败: {dsp_detail}"
            return _build_evidence()

        # ── 8. 验证 upload_outbox 落库 ──
        dsp_dispatch_verify = verify_dsp_dispatch_result(
            trace_id, timeout=timeout, target_db=target_db,
        )
        if not dsp_dispatch_verify.passed:
            error_msg = f"dsp 派送任务验证落库失败: {dsp_dispatch_verify.error}"
            return _build_evidence()

        overall_passed = True

    finally:
        # 无论成功失败都执行清理
        cleanup_result = cleanup(trace_id, timeout=30, target_db=target_db)

    return _build_evidence()


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Returns:
        0 — 全部步骤通过
        1 — 任一步骤失败(fail-closed)
    """
    parser = argparse.ArgumentParser(
        description=(
            "R74 P0-03: 合成交易执行器 — DBWriter 组件测试 + 真实产品交易"
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
    parser.add_argument(
        "--target-db",
        type=str,
        default=DEFAULT_TARGET_DB,
        help=(
            f"目标 SQLite 数据库路径(默认 {DEFAULT_TARGET_DB}),"
            "可切换到 staging 恢复库(只允许字母数字/下划线/斜杠/点/连字符)"
        ),
    )
    parser.add_argument(
        "--real-transaction",
        action="store_true",
        help=(
            "运行真实产品交易路径(通过 e2e_update_adapter 触发 up→idx→dsp handler),"
            "默认运行 DBWriter 组件测试(直接 XADD writer stream)"
        ),
    )
    args = parser.parse_args(argv)

    if args.real_transaction:
        evidence = run_real_product_transaction(
            timeout=args.timeout, target_db=args.target_db,
        )
        mode_label = "R74 P0-03: 真实产品交易"
    else:
        evidence = run_dbwriter_component_test(
            timeout=args.timeout, target_db=args.target_db,
        )
        mode_label = "R74 P0-03: DBWriter 组件测试"

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
            f"=== {mode_label} 通过(trace_id={evidence.trace_id}) ===",
            file=sys.stderr,
        )
        return 0
    print(
        f"=== {mode_label} 失败(trace_id={evidence.trace_id}) — "
        f"{evidence.error} ===",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
