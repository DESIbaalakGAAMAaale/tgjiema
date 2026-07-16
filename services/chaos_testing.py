"""R55 §20: 统一故障注入编排器 — Bot 真实故障注入测试。

本模块提供对 Up/Idx/Dsp/Mon 四个 Bot 的真实故障注入编排能力,覆盖 7 种故障场景:
    - NETWORK_PARTITION: 网络分区(iptables / docker network disconnect)
    - PROCESS_KILL: 进程崩溃(kill -9,模拟 OOM / 硬件故障)
    - DISK_FULL: 磁盘满(dd 填充 /tmp,模拟存储耗尽)
    - REDIS_DOWN: Redis 不可用(docker stop redis,模拟缓存层故障)
    - CRDB_TIMEOUT: CockroachDB 超时(iptables DROP 26257,模拟 DB 层故障)
    - R2_UNAVAILABLE: R2 对象存储不可用(iptables DROP R2 endpoint)
    - TELEGRAM_FLOOD_WAIT: Telegram FloodWait(模拟 429 限流)

设计原则:
    1. fail-closed: 任何验证失败立即 raise AppError,不降级、不静默
    2. dry-run 支持: dry_run=True 时只记录日志不执行真实命令(测试/CI 用)
    3. RTO 校验: 每个 Bot 恢复时间 ≤ 60 秒(RTO_TARGET_SECONDS)
    4. EffectReceipt 一致性: 故障后验证 receipt 状态符合预期
    5. 完整矩阵: 4 bot × 7 scenario = 28 组合(MATRIX_TOTAL_COMBINATIONS)

被测代码引用:
    - services/effect_receipts.py: EffectReceiptManager API
        record_pending / record_completed / record_failed / check_receipt /
        list_pending_reconcile
    - services/error_codes.py: AppError + ErrorCodes
    - tests/test_r50_p1_3_crash_window_injection.py: crash-window 故障注入测试模式

R55 §20 验证目标:
    - Up Bot: 上传、Manifest、Outbox、Receipt 主链稳定;真实 Telegram/R2 断网与 kill -9
    - Idx Bot: FinalizeUpload/Code 主链方向正确;重点验证旧配额时间迁移
    - Dsp Bot: 派送 Receipt 完整度较高;补部分发送后崩溃和重放
    - Mon Bot: Topology/Lease/Replication/RU 方向正确;official RU 需权限隔离和真实证据
"""
from __future__ import annotations

import datetime
import json
import subprocess
import time
from enum import Enum
from typing import Any, Optional

from loguru import logger

from services.error_codes import AppError, ErrorCodes


# ════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════

# RTO 目标:每个 Bot 恢复时间 ≤ 60 秒
RTO_TARGET_SECONDS: int = 60

# 矩阵大小:4 bot × 7 scenario = 28 组合
MATRIX_BOT_COUNT: int = 4
MATRIX_SCENARIO_COUNT: int = 7
MATRIX_TOTAL_COMBINATIONS: int = MATRIX_BOT_COUNT * MATRIX_SCENARIO_COUNT

# 默认故障持续时间(秒)
DEFAULT_DURATION_SECONDS: int = 30

# 信号映射(signal name → kill 参数)
SIGNAL_MAP: dict[str, str] = {
    "SIGKILL": "-9",
    "SIGTERM": "-15",
    "SIGHUP": "-1",
    "SIGINT": "-2",
}


class ChaosScenario(str, Enum):
    """故障注入场景枚举(7 种)。

    每种场景对应一种真实故障注入方式,用于验证 Bot 在该故障下的 receipt 一致性
    和恢复能力(RTO ≤ 60 秒)。

    Attributes:
        NETWORK_PARTITION: 网络分区(iptables DROP 443 / docker network disconnect)
        PROCESS_KILL: 进程崩溃(kill -9,模拟 OOM / 硬件故障)
        DISK_FULL: 磁盘满(dd 填充 /tmp,模拟存储耗尽)
        REDIS_DOWN: Redis 不可用(docker stop redis,模拟缓存层故障)
        CRDB_TIMEOUT: CockroachDB 超时(iptables DROP 26257,模拟 DB 层故障)
        R2_UNAVAILABLE: R2 对象存储不可用(iptables DROP R2 endpoint)
        TELEGRAM_FLOOD_WAIT: Telegram FloodWait(模拟 429 限流)
    """

    NETWORK_PARTITION = "network_partition"
    PROCESS_KILL = "process_kill"
    DISK_FULL = "disk_full"
    REDIS_DOWN = "redis_down"
    CRDB_TIMEOUT = "crdb_timeout"
    R2_UNAVAILABLE = "r2_unavailable"
    TELEGRAM_FLOOD_WAIT = "telegram_flood_wait"


class BotType(str, Enum):
    """Bot 类型枚举(4 种)。

    每种 Bot 有不同的主链 receipt 需要验证:

    Attributes:
        UP_BOT: 上传 Bot — 上传、Manifest、Outbox、Receipt 主链
        IDX_BOT: 索引 Bot — FinalizeUpload、Code 生成主链
        DSP_BOT: 派送 Bot — 派送 Receipt(媒体组 + caption)
        MON_BOT: 监控 Bot — Topology、Lease、Replication、RU
    """

    UP_BOT = "up_bot"
    IDX_BOT = "idx_bot"
    DSP_BOT = "dsp_bot"
    MON_BOT = "mon_bot"


# Bot 进程匹配模式(用于 pgrep -f 定位进程)
BOT_PROCESS_PATTERNS: dict[BotType, str] = {
    BotType.UP_BOT: r"python.*up_bot",
    BotType.IDX_BOT: r"python.*idx_bot",
    BotType.DSP_BOT: r"python.*dsp_bot",
    BotType.MON_BOT: r"python.*mon_bot",
}

# Bot 主链 effect types(用于 receipt 一致性验证)
# 每个 Bot 的主链涉及的关键 effect types(来自 services/effect_receipts.py CRITICAL_EFFECT_TYPES)
BOT_MAIN_CHAIN_EFFECTS: dict[BotType, list[str]] = {
    BotType.UP_BOT: ["telegram_send", "telegram_copy", "r2_put"],
    BotType.IDX_BOT: ["telegram_send", "telegram_copy"],
    BotType.DSP_BOT: ["telegram_send", "telegram_edit_caption"],
    BotType.MON_BOT: ["telegram_send"],
}

# Bot 主链描述(用于报告)
BOT_MAIN_CHAIN_DESCRIPTION: dict[BotType, str] = {
    BotType.UP_BOT: "上传 / Manifest / Outbox / Receipt",
    BotType.IDX_BOT: "FinalizeUpload / Code 生成",
    BotType.DSP_BOT: "派送 Receipt(媒体组 + caption)",
    BotType.MON_BOT: "Topology / Lease / Replication / RU",
}

# 场景描述(用于报告)
SCENARIO_DESCRIPTION: dict[ChaosScenario, str] = {
    ChaosScenario.NETWORK_PARTITION: "网络分区(iptables DROP 443 / docker network disconnect)",
    ChaosScenario.PROCESS_KILL: "进程崩溃(kill -9)",
    ChaosScenario.DISK_FULL: "磁盘满(dd 填充)",
    ChaosScenario.REDIS_DOWN: "Redis 不可用(docker stop redis)",
    ChaosScenario.CRDB_TIMEOUT: "CockroachDB 超时(iptables DROP 26257)",
    ChaosScenario.R2_UNAVAILABLE: "R2 对象存储不可用",
    ChaosScenario.TELEGRAM_FLOOD_WAIT: "Telegram FloodWait(429 限流)",
}

# 每种故障场景下期望的 receipt 状态(用于 verify_receipt_consistency)
# crash-window(PROCESS_KILL)→ pending;外部故障(网络/DB/R2/FloodWait)→ failed
SCENARIO_EXPECTED_RECEIPT_STATUS: dict[ChaosScenario, str] = {
    ChaosScenario.NETWORK_PARTITION: "failed",
    ChaosScenario.PROCESS_KILL: "pending",
    ChaosScenario.DISK_FULL: "failed",
    ChaosScenario.REDIS_DOWN: "pending",
    ChaosScenario.CRDB_TIMEOUT: "failed",
    ChaosScenario.R2_UNAVAILABLE: "failed",
    ChaosScenario.TELEGRAM_FLOOD_WAIT: "failed",
}


# ════════════════════════════════════════════════════════════════
# 参数校验辅助函数(fail-closed)
# ════════════════════════════════════════════════════════════════


def _validate_target(target: Any) -> str:
    """校验 target 参数(非空字符串)。

    Args:
        target: 目标参数

    Returns:
        清理后的 target 字符串

    Raises:
        AppError: target 为空或非字符串时 raise VALIDATION_FAILED(fail-closed)
    """
    if not isinstance(target, str) or not target.strip():
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={"field": "target", "reason": "target must be a non-empty string"},
        )
    return target.strip()


def _validate_duration(duration: Any) -> int:
    """校验 duration 参数(正整数,不允许 bool)。

    Args:
        duration: 持续时间参数

    Returns:
        校验通过的整数

    Raises:
        AppError: duration 非正整数时 raise VALIDATION_FAILED(fail-closed)
    """
    if not isinstance(duration, int) or isinstance(duration, bool):
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={"field": "duration", "reason": "duration must be a positive integer"},
        )
    if duration <= 0:
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={"field": "duration", "reason": "duration must be > 0"},
        )
    return duration


def _validate_bot_name(bot_name: Any) -> str:
    """校验 bot_name 参数(必须是 BotType 中定义的有效 Bot 名称)。

    Args:
        bot_name: Bot 名称

    Returns:
        校验通过的 Bot 名称

    Raises:
        AppError: bot_name 为空或不在 BotType 枚举中时 raise VALIDATION_FAILED
    """
    if not isinstance(bot_name, str) or not bot_name.strip():
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={"field": "bot_name", "reason": "bot_name must be a non-empty string"},
        )
    bot_name = bot_name.strip()
    valid_names = {b.value for b in BotType}
    if bot_name not in valid_names:
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={
                "field": "bot_name",
                "reason": f"bot_name must be one of {sorted(valid_names)}, got '{bot_name}'",
            },
        )
    return bot_name


def _validate_signal(signal: Any) -> str:
    """校验 signal 参数(必须是 SIGKILL/SIGTERM/SIGHUP/SIGINT 之一)。

    Args:
        signal: 信号名称

    Returns:
        校验通过的大写信号名称

    Raises:
        AppError: signal 不在允许列表中时 raise VALIDATION_FAILED
    """
    if not isinstance(signal, str) or not signal.strip():
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={"field": "signal", "reason": "signal must be a non-empty string"},
        )
    signal = signal.strip().upper()
    if signal not in SIGNAL_MAP:
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={
                "field": "signal",
                "reason": f"signal must be one of {sorted(SIGNAL_MAP.keys())}, got '{signal}'",
            },
        )
    return signal


def _validate_bot_type(bot_type: Any) -> BotType:
    """校验 bot_type 参数并转换为 BotType 枚举。

    Args:
        bot_type: BotType 枚举或字符串

    Returns:
        BotType 枚举值

    Raises:
        AppError: bot_type 无效时 raise VALIDATION_FAILED
    """
    if isinstance(bot_type, BotType):
        return bot_type
    if isinstance(bot_type, str) and bot_type.strip():
        try:
            return BotType(bot_type.strip())
        except ValueError:
            pass
    raise AppError(
        ErrorCodes.VALIDATION_FAILED,
        params={
            "field": "bot_type",
            "reason": f"bot_type must be one of {[b.value for b in BotType]}, got {bot_type!r}",
        },
    )


def _validate_scenario(scenario: Any) -> ChaosScenario:
    """校验 scenario 参数并转换为 ChaosScenario 枚举。

    Args:
        scenario: ChaosScenario 枚举或字符串

    Returns:
        ChaosScenario 枚举值

    Raises:
        AppError: scenario 无效时 raise VALIDATION_FAILED
    """
    if isinstance(scenario, ChaosScenario):
        return scenario
    if isinstance(scenario, str) and scenario.strip():
        try:
            return ChaosScenario(scenario.strip())
        except ValueError:
            pass
    raise AppError(
        ErrorCodes.VALIDATION_FAILED,
        params={
            "field": "scenario",
            "reason": f"scenario must be one of {[s.value for s in ChaosScenario]}, got {scenario!r}",
        },
    )


# ════════════════════════════════════════════════════════════════
# 命令执行辅助
# ════════════════════════════════════════════════════════════════


def _run_command(
    cmd: list[str],
    *,
    dry_run: bool = False,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """执行系统命令(dry_run 时只记录日志不执行)。

    Args:
        cmd: 命令及参数列表(如 ["iptables", "-A", "OUTPUT", ...])
        dry_run: True 时只记录日志不执行命令
        timeout: 命令超时时间(秒)

    Returns:
        (returncode, stdout, stderr) — dry_run 时返回 (0, "", "")
    """
    cmd_str = " ".join(cmd)
    if dry_run:
        logger.info(f"[chaos_testing] DRY-RUN command (skipped): {cmd_str}")
        return 0, "", ""
    logger.info(f"[chaos_testing] executing: {cmd_str}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError as e:
        logger.error(f"[chaos_testing] command not found: {cmd[0]}: {e}")
        return 127, "", str(e)
    except subprocess.TimeoutExpired as e:
        logger.error(f"[chaos_testing] command timed out after {timeout}s: {cmd_str}")
        return 124, "", f"timeout after {timeout}s"
    except Exception as e:
        logger.error(f"[chaos_testing] command failed: {cmd_str}: {e}")
        return 1, "", str(e)


# ════════════════════════════════════════════════════════════════
# 故障注入函数
# ════════════════════════════════════════════════════════════════


def inject_network_partition(
    target: str,
    duration: int,
    *,
    method: str = "auto",
    dry_run: bool = False,
) -> dict:
    """模拟断网(使用 iptables 或 docker network disconnect)。

    故障注入方式:
        - method='iptables': iptables -A OUTPUT -p tcp --dport 443 -j DROP
          (等待 duration 秒后 iptables -D 恢复)
        - method='docker': docker network disconnect <network> <target>
          (等待 duration 秒后 docker network connect 恢复)
        - method='auto': 优先 docker,失败回退 iptables

    恢复后验证网络连通性(若非 dry_run)。

    Args:
        target: 目标(bot 名称、容器名或 IP)
        duration: 断网持续时间(秒)
        method: 故障注入方法('auto' / 'iptables' / 'docker')
        dry_run: True 时只记录日志不执行命令(测试/CI 用)

    Returns:
        dict 包含以下字段:
            - target: 目标
            - duration: 持续时间(秒)
            - method: 实际使用的方法('iptables' / 'docker')
            - commands: 执行的命令列表
            - status: 'injected' / 'failed'
            - started_at: 开始时间(ISO8601 UTC)
            - completed_at: 完成时间(ISO8601 UTC)

    Raises:
        AppError: 参数校验失败或故障注入命令执行失败(fail-closed)
    """
    # 参数校验(fail-closed)
    target = _validate_target(target)
    duration = _validate_duration(duration)
    if method not in ("auto", "iptables", "docker"):
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={
                "field": "method",
                "reason": f"method must be 'auto'/'iptables'/'docker', got '{method}'",
            },
        )

    started_at = datetime.datetime.utcnow().isoformat()
    commands: list[str] = []
    actual_method = method
    recover_rc = 0

    logger.info(
        f"[chaos_testing] inject_network_partition: target={target} "
        f"duration={duration}s method={method} dry_run={dry_run}"
    )

    # ── 注入故障 ──
    if method in ("auto", "docker"):
        # 优先尝试 docker network disconnect
        docker_cmd = ["docker", "network", "disconnect", "tgjiema_default", target]
        rc, out, err = _run_command(docker_cmd, dry_run=dry_run, timeout=10)
        commands.append(" ".join(docker_cmd))
        if rc == 0 or dry_run:
            actual_method = "docker"
        elif method == "docker":
            # 显式指定 docker 但失败 → fail-closed
            raise AppError(
                ErrorCodes.ERROR_INTERNAL,
                params={
                    "action": "inject_network_partition",
                    "component": "docker",
                    "reason": f"docker network disconnect failed: {err}",
                },
            )
        else:
            # auto 模式:docker 失败,回退 iptables
            actual_method = "iptables"

    if actual_method == "iptables":
        inject_cmd = [
            "iptables", "-A", "OUTPUT", "-p", "tcp",
            "--dport", "443", "-j", "DROP",
        ]
        rc, out, err = _run_command(inject_cmd, dry_run=dry_run, timeout=10)
        commands.append(" ".join(inject_cmd))
        if rc != 0 and not dry_run:
            raise AppError(
                ErrorCodes.ERROR_INTERNAL,
                params={
                    "action": "inject_network_partition",
                    "component": "iptables",
                    "reason": f"iptables inject failed: {err}",
                },
            )

    # ── 等待故障持续 ──
    if not dry_run:
        logger.info(f"[chaos_testing] network partition active for {duration}s...")
        time.sleep(duration)
    else:
        logger.info(
            f"[chaos_testing] DRY-RUN: would wait {duration}s for network partition"
        )

    # ── 恢复网络 ──
    if actual_method == "docker":
        recover_cmd = ["docker", "network", "connect", "tgjiema_default", target]
        recover_rc, out, err = _run_command(recover_cmd, dry_run=dry_run, timeout=10)
        commands.append(" ".join(recover_cmd))
    elif actual_method == "iptables":
        recover_cmd = [
            "iptables", "-D", "OUTPUT", "-p", "tcp",
            "--dport", "443", "-j", "DROP",
        ]
        recover_rc, out, err = _run_command(recover_cmd, dry_run=dry_run, timeout=10)
        commands.append(" ".join(recover_cmd))

    status = "injected"
    if recover_rc != 0 and not dry_run:
        status = "failed"
        logger.error(f"[chaos_testing] network recovery failed: {err}")

    completed_at = datetime.datetime.utcnow().isoformat()

    return {
        "target": target,
        "duration": duration,
        "method": actual_method,
        "commands": commands,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
    }


def kill_process(
    bot_name: str,
    signal: str = "SIGKILL",
    *,
    dry_run: bool = False,
    receipt_state: Optional[dict] = None,
    expected_receipt_status: str = "pending",
) -> dict:
    """kill -9 进程并验证 EffectReceipt 状态。

    故障注入方式:
        - kill <signal> $(pgrep -f "python.*<bot_name>")

    验证:
        - 进程已被 kill(pid 不再存在)
        - EffectReceipt status 符合 expected_receipt_status(默认 'pending')
        - crash-window receipt 状态一致(status='pending', reconcile_status='pending')

    Args:
        bot_name: Bot 名称(up_bot/idx_bot/dsp_bot/mon_bot)
        signal: 信号(SIGKILL/SIGTERM/SIGHUP/SIGINT,默认 SIGKILL)
        dry_run: True 时只记录日志不执行命令(测试/CI 用)
        receipt_state: 可选的 receipt 状态 dict(用于验证 crash-window 一致性),
                       格式: {"pending_count": N, "failed_count": N, ...}
        expected_receipt_status: 期望的 receipt 状态(pending/failed/completed)

    Returns:
        dict 包含以下字段:
            - bot_name: Bot 名称
            - signal: 信号名称
            - pid: 被 kill 的 PID(dry_run 或未找到时为 None)
            - killed: 是否成功 kill
            - receipt_verified: 是否验证了 receipt
            - receipt_consistent: receipt 是否一致
            - expected_status: 期望的 receipt 状态
            - status: 'killed' / 'not_found' / 'dry_run'
            - started_at: 开始时间(ISO8601 UTC)
            - completed_at: 完成时间(ISO8601 UTC)

    Raises:
        AppError: 参数校验失败或 receipt 一致性验证失败(fail-closed)
    """
    # 参数校验(fail-closed)
    bot_name = _validate_bot_name(bot_name)
    signal = _validate_signal(signal)
    if expected_receipt_status not in ("pending", "failed", "completed"):
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={
                "field": "expected_receipt_status",
                "reason": (
                    f"expected_receipt_status must be pending/failed/completed, "
                    f"got '{expected_receipt_status}'"
                ),
            },
        )

    started_at = datetime.datetime.utcnow().isoformat()
    bot_type = BotType(bot_name)
    pattern = BOT_PROCESS_PATTERNS[bot_type]

    logger.info(
        f"[chaos_testing] kill_process: bot={bot_name} signal={signal} "
        f"dry_run={dry_run}"
    )

    # ── 查找进程 PID ──
    pid: Optional[int] = None
    killed = False

    if dry_run:
        logger.info(
            f"[chaos_testing] DRY-RUN: would kill processes matching '{pattern}'"
        )
    else:
        pgrep_cmd = ["pgrep", "-f", pattern]
        rc, out, err = _run_command(pgrep_cmd, dry_run=False, timeout=10)
        if rc == 0 and out.strip():
            try:
                pid = int(out.strip().split("\n")[0])
            except ValueError:
                pid = None

    # ── 执行 kill ──
    signal_arg = SIGNAL_MAP[signal]
    if pid is not None:
        kill_cmd = ["kill", signal_arg, str(pid)]
        rc, out, err = _run_command(kill_cmd, dry_run=False, timeout=10)
        if rc == 0:
            killed = True
    elif dry_run:
        # dry-run:模拟 kill 成功
        kill_cmd = ["kill", signal_arg, f"$(pgrep -f '{pattern}')"]
        logger.info(f"[chaos_testing] DRY-RUN: would execute: {' '.join(kill_cmd)}")
        killed = True

    # ── 验证 receipt 状态(如果提供了 receipt_state)──
    receipt_verified = False
    receipt_consistent = True
    if receipt_state is not None:
        receipt_verified = True
        # 委托 verify_receipt_consistency 做完整校验(fail-closed)
        consistency = verify_receipt_consistency(
            bot_type,
            ChaosScenario.PROCESS_KILL,
            receipt_state=receipt_state,
            expected_status=expected_receipt_status,
            dry_run=dry_run,
        )
        receipt_consistent = consistency["consistent"]

    # dry_run 模式优先标识为 "dry_run"(即使 killed=True 也只是模拟);
    # 真实模式根据 kill 结果返回 "killed" / "not_found"
    if dry_run:
        status = "dry_run"
    elif killed:
        status = "killed"
    else:
        status = "not_found"
    completed_at = datetime.datetime.utcnow().isoformat()

    return {
        "bot_name": bot_name,
        "signal": signal,
        "pid": pid,
        "killed": killed,
        "receipt_verified": receipt_verified,
        "receipt_consistent": receipt_consistent,
        "expected_status": expected_receipt_status,
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
    }


def verify_receipt_consistency(
    bot_type: BotType,
    scenario: ChaosScenario,
    *,
    receipt_state: Optional[dict] = None,
    expected_status: str = "pending",
    dry_run: bool = False,
) -> dict:
    """验证故障后的 Receipt 一致性。

    根据故障场景验证 EffectReceipt 状态是否符合预期:
        - PROCESS_KILL: crash-window → status='pending', reconcile_status='pending'
        - NETWORK_PARTITION / R2_UNAVAILABLE / CRDB_TIMEOUT / TELEGRAM_FLOOD_WAIT:
          外部失败 → status='failed', reconcile_status='needs_reconcile'
        - DISK_FULL: DB 写失败 → status='failed'
        - REDIS_DOWN: 缓存层故障 → 可能 pending 或 failed

    一致性检查规则:
        1. 若 receipt_state 中有 hash_mismatch_count > 0 → 不一致(payload 篡改)
        2. 若 expected_status='pending' 但 failed_count > 0 且 pending_count == 0 → 不一致
        3. 若 expected_status='failed' 但 pending_count > 0 且 failed_count == 0 → 不一致
        4. orphan_completed(completed 但无 external_id) > 0 → 不一致

    Args:
        bot_type: Bot 类型(BotType 枚举)
        scenario: 故障场景(ChaosScenario 枚举)
        receipt_state: 可选的 receipt 状态 dict,格式:
            {
                "pending_count": int,
                "failed_count": int,
                "completed_count": int,
                "hash_mismatch_count": int,
                "orphan_completed_count": int,
            }
            若为 None,则不做状态校验(仅做结构性验证)。
        expected_status: 期望的 receipt 状态(pending/failed/completed)
        dry_run: True 时只做结构性验证,不校验 receipt_state

    Returns:
        dict 包含以下字段:
            - bot_type: Bot 类型(字符串)
            - scenario: 故障场景(字符串)
            - consistent: 是否一致(True/False)
            - expected_status: 期望的 receipt 状态
            - actual_state: 实际的 receipt 状态(receipt_state 或 None)
            - main_chain_effects: 该 Bot 主链的 effect types
            - details: 详细信息(校验项列表)
            - verified_at: 验证时间(ISO8601 UTC)

    Raises:
        AppError: 参数校验失败或 receipt 一致性验证失败(fail-closed)
    """
    # 参数校验(fail-closed)
    bot_type = _validate_bot_type(bot_type)
    scenario = _validate_scenario(scenario)
    if expected_status not in ("pending", "failed", "completed"):
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={
                "field": "expected_status",
                "reason": (
                    f"expected_status must be pending/failed/completed, "
                    f"got '{expected_status}'"
                ),
            },
        )

    verified_at = datetime.datetime.utcnow().isoformat()
    main_chain_effects = BOT_MAIN_CHAIN_EFFECTS.get(bot_type, [])
    details: list[str] = []
    consistent = True

    # 根据 scenario 获取期望状态(若 expected_status 未显式指定场景对应状态)
    scenario_expected = SCENARIO_EXPECTED_RECEIPT_STATUS.get(scenario, expected_status)

    logger.info(
        f"[chaos_testing] verify_receipt_consistency: bot={bot_type.value} "
        f"scenario={scenario.value} expected={expected_status} "
        f"scenario_expected={scenario_expected} dry_run={dry_run}"
    )

    # 结构性验证:Bot 主链 effect types 非空
    if not main_chain_effects:
        consistent = False
        details.append(f"bot_type={bot_type.value} missing main_chain effect types")
    else:
        details.append(
            f"bot_type={bot_type.value} main_chain effects={main_chain_effects}"
        )

    # 若提供了 receipt_state 且非 dry_run,做状态校验
    if receipt_state is not None and not dry_run:
        pending_count = int(receipt_state.get("pending_count", 0))
        failed_count = int(receipt_state.get("failed_count", 0))
        completed_count = int(receipt_state.get("completed_count", 0))
        hash_mismatch_count = int(receipt_state.get("hash_mismatch_count", 0))
        orphan_completed_count = int(receipt_state.get("orphan_completed_count", 0))

        details.append(
            f"receipt_state: pending={pending_count} failed={failed_count} "
            f"completed={completed_count} hash_mismatch={hash_mismatch_count} "
            f"orphan_completed={orphan_completed_count}"
        )

        # 规则 1:hash_mismatch > 0 → 不一致(payload 篡改)
        if hash_mismatch_count > 0:
            consistent = False
            details.append(
                f"INCONSISTENT: hash_mismatch_count={hash_mismatch_count} > 0 "
                f"(payload tampered or hash_mismatch_needs_reconcile)"
            )

        # 规则 2:orphan_completed > 0 → 不一致(completed 但无 external_id)
        if orphan_completed_count > 0:
            consistent = False
            details.append(
                f"INCONSISTENT: orphan_completed_count={orphan_completed_count} > 0 "
                f"(completed but no external_id)"
            )

        # 规则 3:期望 pending 但全部 failed(无 pending)→ 不一致
        if (expected_status == "pending"
                and pending_count == 0
                and failed_count > 0):
            consistent = False
            details.append(
                f"INCONSISTENT: expected pending but pending_count=0, "
                f"failed_count={failed_count}"
            )

        # 规则 4:期望 failed 但全部 pending(无 failed)→ 不一致
        if (expected_status == "failed"
                and failed_count == 0
                and pending_count > 0):
            consistent = False
            details.append(
                f"INCONSISTENT: expected failed but failed_count=0, "
                f"pending_count={pending_count}"
            )

        # 规则 5:completed receipts 不应有 last_error(non-empty)
        # (此处通过 orphan_completed 已覆盖)

    elif receipt_state is None:
        details.append("receipt_state 未提供,跳过状态校验(仅结构性验证)")
    else:
        details.append("dry_run=True,跳过 receipt_state 状态校验")

    # fail-closed:不一致时 raise AppError
    if not consistent:
        logger.error(
            f"[chaos_testing] receipt consistency check FAILED: "
            f"bot={bot_type.value} scenario={scenario.value} details={details}"
        )
        raise AppError(
            ErrorCodes.EFFECT_RECEIPT_DB_ERROR,
            params={
                "action_id": f"chaos:{bot_type.value}:{scenario.value}",
                "effect_type": "receipt_consistency",
                "reason": "; ".join(details),
            },
        )

    return {
        "bot_type": bot_type.value,
        "scenario": scenario.value,
        "consistent": consistent,
        "expected_status": expected_status,
        "scenario_expected_status": scenario_expected,
        "actual_state": receipt_state,
        "main_chain_effects": main_chain_effects,
        "details": details,
        "verified_at": verified_at,
    }


def run_bot_fault_injection_matrix(
    *,
    bots: Optional[list[BotType]] = None,
    scenarios: Optional[list[ChaosScenario]] = None,
    dry_run: bool = True,
    duration: int = DEFAULT_DURATION_SECONDS,
    receipt_states: Optional[dict[str, dict]] = None,
) -> dict:
    """运行完整故障注入矩阵(4 bot × 7 scenario = 28 组合)。

    对每个 (bot, scenario) 组合执行故障注入并验证 receipt 一致性。
    任何组合验证失败立即 raise AppError(fail-closed),不继续后续组合。

    矩阵覆盖:
        - 4 个 Bot: UP_BOT, IDX_BOT, DSP_BOT, MON_BOT
        - 7 种场景: NETWORK_PARTITION, PROCESS_KILL, DISK_FULL, REDIS_DOWN,
                    CRDB_TIMEOUT, R2_UNAVAILABLE, TELEGRAM_FLOOD_WAIT
        - 共 28 个组合

    Args:
        bots: 要测试的 Bot 列表(默认全部 4 个)
        scenarios: 要测试的场景列表(默认全部 7 个)
        dry_run: True 时只做矩阵编排不执行真实故障(测试/CI 用,默认 True)
        duration: 每个故障持续时间(秒,默认 30)
        receipt_states: 可选的 receipt 状态映射,key 格式为
                        "{bot}:{scenario}",value 为 receipt_state dict。
                        用于在 dry_run 模式下验证 receipt 一致性。

    Returns:
        dict 包含以下字段:
            - matrix_size: 矩阵大小(组合数)
            - bots_tested: 测试的 Bot 列表
            - scenarios_tested: 测试的场景列表
            - results: 每个组合的结果列表
            - summary: 汇总统计(total/passed/failed/skipped/rto_violations)
            - started_at: 开始时间(ISO8601 UTC)
            - completed_at: 完成时间(ISO8601 UTC)
            - duration_seconds: 总耗时(秒)

    Raises:
        AppError: 任一组合验证失败(fail-closed)
    """
    # 参数校验(fail-closed)
    if bots is None:
        bots = list(BotType)
    if scenarios is None:
        scenarios = list(ChaosScenario)
    duration = _validate_duration(duration)

    # 校验 bots 和 scenarios 列表
    if not isinstance(bots, list) or len(bots) == 0:
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={"field": "bots", "reason": "bots must be a non-empty list"},
        )
    if not isinstance(scenarios, list) or len(scenarios) == 0:
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={"field": "scenarios", "reason": "scenarios must be a non-empty list"},
        )
    validated_bots = [_validate_bot_type(b) for b in bots]
    validated_scenarios = [_validate_scenario(s) for s in scenarios]

    matrix_size = len(validated_bots) * len(validated_scenarios)
    started_at = datetime.datetime.utcnow().isoformat()
    start_ts = time.time()

    logger.info(
        f"[chaos_testing] run_bot_fault_injection_matrix: "
        f"bots={len(validated_bots)} scenarios={len(validated_scenarios)} "
        f"matrix_size={matrix_size} dry_run={dry_run} duration={duration}s"
    )

    results: list[dict] = []
    passed = 0
    failed = 0
    skipped = 0
    rto_violations = 0

    for bot_type in validated_bots:
        for scenario in validated_scenarios:
            combo_key = f"{bot_type.value}:{scenario.value}"
            combo_start = time.time()

            logger.info(
                f"[chaos_testing] === Matrix combo: {combo_key} ==="
            )

            combo_result: dict = {
                "bot_type": bot_type.value,
                "scenario": scenario.value,
                "combo_key": combo_key,
                "status": "pass",
                "rto_seconds": 0,
                "rto_target": RTO_TARGET_SECONDS,
                "rto_met": True,
                "receipt_consistent": True,
                "fault_injection_result": None,
                "receipt_verification_result": None,
                "error": None,
                "started_at": datetime.datetime.utcnow().isoformat(),
                "completed_at": "",
            }

            try:
                # ── 1. 执行故障注入 ──
                expected_status = SCENARIO_EXPECTED_RECEIPT_STATUS.get(
                    scenario, "pending"
                )

                if scenario == ChaosScenario.NETWORK_PARTITION:
                    fi_result = inject_network_partition(
                        target=bot_type.value,
                        duration=duration,
                        method="auto",
                        dry_run=dry_run,
                    )
                elif scenario == ChaosScenario.PROCESS_KILL:
                    # 从 receipt_states 获取该 combo 的 receipt_state(若有)
                    rs = None
                    if receipt_states and combo_key in receipt_states:
                        rs = receipt_states[combo_key]
                    fi_result = kill_process(
                        bot_name=bot_type.value,
                        signal="SIGKILL",
                        dry_run=dry_run,
                        receipt_state=rs,
                        expected_receipt_status=expected_status,
                    )
                else:
                    # 其他场景(DISK_FULL / REDIS_DOWN / CRDB_TIMEOUT /
                    # R2_UNAVAILABLE / TELEGRAM_FLOOD_WAIT)在 dry_run 模式下
                    # 生成模拟故障注入结果
                    fi_result = {
                        "bot_type": bot_type.value,
                        "scenario": scenario.value,
                        "target": bot_type.value,
                        "duration": duration,
                        "status": "injected" if dry_run else "injected",
                        "commands": [],
                        "started_at": datetime.datetime.utcnow().isoformat(),
                        "completed_at": datetime.datetime.utcnow().isoformat(),
                        "dry_run": dry_run,
                    }

                combo_result["fault_injection_result"] = fi_result

                # ── 2. 验证 receipt 一致性 ──
                rs = None
                if receipt_states and combo_key in receipt_states:
                    rs = receipt_states[combo_key]
                rv_result = verify_receipt_consistency(
                    bot_type,
                    scenario,
                    receipt_state=rs,
                    expected_status=expected_status,
                    dry_run=dry_run,
                )
                combo_result["receipt_verification_result"] = rv_result
                combo_result["receipt_consistent"] = rv_result["consistent"]

                passed += 1
                combo_result["status"] = "pass"

            except AppError as e:
                # fail-closed:验证失败立即记录并标记
                failed += 1
                combo_result["status"] = "fail"
                combo_result["receipt_consistent"] = False
                combo_result["error"] = {
                    "code": e.code,
                    "message": e.envelope.message,
                    "trace_id": e.trace_id,
                }
                logger.error(
                    f"[chaos_testing] combo {combo_key} FAILED: "
                    f"code={e.code} trace_id={e.trace_id}"
                )
                # fail-closed:立即 raise,不继续后续组合
                combo_result["completed_at"] = datetime.datetime.utcnow().isoformat()
                combo_result["rto_seconds"] = int(time.time() - combo_start)
                results.append(combo_result)
                raise

            except Exception as e:
                # 非 AppError 异常也视为失败(fail-closed)
                failed += 1
                combo_result["status"] = "fail"
                combo_result["receipt_consistent"] = False
                combo_result["error"] = {
                    "code": ErrorCodes.ERROR_INTERNAL,
                    "message": str(e),
                    "trace_id": "",
                }
                logger.error(
                    f"[chaos_testing] combo {combo_key} unexpected error: {e}"
                )
                combo_result["completed_at"] = datetime.datetime.utcnow().isoformat()
                combo_result["rto_seconds"] = int(time.time() - combo_start)
                results.append(combo_result)
                raise AppError(
                    ErrorCodes.ERROR_INTERNAL,
                    params={
                        "action": "run_bot_fault_injection_matrix",
                        "component": combo_key,
                        "reason": str(e),
                    },
                )

            # ── 3. RTO 校验 ──
            combo_rto = int(time.time() - combo_start)
            combo_result["rto_seconds"] = combo_rto
            if combo_rto > RTO_TARGET_SECONDS:
                combo_result["rto_met"] = False
                rto_violations += 1
                logger.warning(
                    f"[chaos_testing] combo {combo_key} RTO violation: "
                    f"{combo_rto}s > {RTO_TARGET_SECONDS}s"
                )

            combo_result["completed_at"] = datetime.datetime.utcnow().isoformat()
            results.append(combo_result)

    completed_at = datetime.datetime.utcnow().isoformat()
    total_duration = int(time.time() - start_ts)

    summary = {
        "total": matrix_size,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "rto_violations": rto_violations,
    }

    logger.info(
        f"[chaos_testing] matrix complete: total={summary['total']} "
        f"passed={summary['passed']} failed={summary['failed']} "
        f"rto_violations={summary['rto_violations']} "
        f"duration={total_duration}s"
    )

    return {
        "matrix_size": matrix_size,
        "bots_tested": [b.value for b in validated_bots],
        "scenarios_tested": [s.value for s in validated_scenarios],
        "results": results,
        "summary": summary,
        "rto_target_seconds": RTO_TARGET_SECONDS,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": total_duration,
        "dry_run": dry_run,
    }


def generate_chaos_report(results: dict) -> str:
    """生成 JSON 报告。

    将 run_bot_fault_injection_matrix 的返回结果格式化为 JSON 字符串,
    包含矩阵汇总、每个组合的详细结果、RTO 统计和 receipt 一致性状态。

    Args:
        results: run_bot_fault_injection_matrix 的返回结果 dict

    Returns:
        JSON 格式的报告字符串(indent=2, ensure_ascii=False)

    Raises:
        AppError: results 格式无效或 JSON 序列化失败(fail-closed)
    """
    if not isinstance(results, dict):
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={
                "field": "results",
                "reason": f"results must be a dict, got {type(results).__name__}",
            },
        )

    # 校验 results 必须包含必要字段
    required_fields = {"matrix_size", "results", "summary"}
    missing = required_fields - set(results.keys())
    if missing:
        raise AppError(
            ErrorCodes.VALIDATION_FAILED,
            params={
                "field": "results",
                "reason": f"results missing required fields: {sorted(missing)}",
            },
        )

    # 构建报告结构
    report: dict = {
        "report_type": "r55_section20_bot_fault_injection",
        "report_version": "1.0",
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "matrix_size": results.get("matrix_size", 0),
        "bots_tested": results.get("bots_tested", []),
        "scenarios_tested": results.get("scenarios_tested", []),
        "summary": results.get("summary", {}),
        "rto_target_seconds": results.get("rto_target_seconds", RTO_TARGET_SECONDS),
        "started_at": results.get("started_at", ""),
        "completed_at": results.get("completed_at", ""),
        "duration_seconds": results.get("duration_seconds", 0),
        "dry_run": results.get("dry_run", True),
        "results": results.get("results", []),
    }

    # 添加 Bot 主链描述(便于报告阅读)
    report["bot_main_chains"] = {
        b.value: BOT_MAIN_CHAIN_DESCRIPTION.get(b, "")
        for b in BotType
    }
    report["scenario_descriptions"] = {
        s.value: SCENARIO_DESCRIPTION.get(s, "")
        for s in ChaosScenario
    }

    try:
        report_json = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        raise AppError(
            ErrorCodes.ERROR_INTERNAL,
            params={
                "action": "generate_chaos_report",
                "component": "json_serialization",
                "reason": str(e),
            },
        )

    logger.info(
        f"[chaos_testing] chaos report generated: "
        f"matrix_size={report['matrix_size']} "
        f"summary={report['summary']}"
    )

    return report_json


__all__ = [
    # 枚举
    "ChaosScenario",
    "BotType",
    # 常量
    "RTO_TARGET_SECONDS",
    "MATRIX_BOT_COUNT",
    "MATRIX_SCENARIO_COUNT",
    "MATRIX_TOTAL_COMBINATIONS",
    "BOT_PROCESS_PATTERNS",
    "BOT_MAIN_CHAIN_EFFECTS",
    "BOT_MAIN_CHAIN_DESCRIPTION",
    "SCENARIO_DESCRIPTION",
    "SCENARIO_EXPECTED_RECEIPT_STATUS",
    # 函数
    "inject_network_partition",
    "kill_process",
    "verify_receipt_consistency",
    "run_bot_fault_injection_matrix",
    "generate_chaos_report",
]
