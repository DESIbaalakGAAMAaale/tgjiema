"""R55 §20: Bot 真实故障注入测试 — 统一故障注入编排器验证。

测试覆盖范围:
    A. ChaosScenario 枚举完整性(7 种故障场景)
    B. BotType 枚举完整性(4 个 Bot)
    C. inject_network_partition 参数校验(fail-closed)
    D. kill_process 参数校验(fail-closed)
    E. verify_receipt_consistency 逻辑(一致性规则)
    F. run_bot_fault_injection_matrix 矩阵完整性(4 bot × 7 scenario = 28)
    G. generate_chaos_report 输出格式(JSON 结构)
    H. fail-closed 行为(任何验证失败立即 raise AppError)

被测代码引用:
    - services/chaos_testing.py:
        ChaosScenario / BotType 枚举
        inject_network_partition / kill_process / verify_receipt_consistency
        run_bot_fault_injection_matrix / generate_chaos_report
    - services/error_codes.py: AppError + ErrorCodes

测试策略:
    - 所有测试使用 dry_run=True 避免执行真实系统命令(iptables/docker/kill)
    - 参数校验测试验证 AppError(VALIDATION_FAILED) 被正确 raise
    - 矩阵测试验证 28 个组合全部 pass(dry_run 模式)
    - fail-closed 测试验证不一致的 receipt_state 触发 AppError
"""
from __future__ import annotations

import json

import pytest

from services.chaos_testing import (
    BOT_MAIN_CHAIN_DESCRIPTION,
    BOT_MAIN_CHAIN_EFFECTS,
    BOT_PROCESS_PATTERNS,
    BotType,
    ChaosScenario,
    MATRIX_BOT_COUNT,
    MATRIX_SCENARIO_COUNT,
    MATRIX_TOTAL_COMBINATIONS,
    RTO_TARGET_SECONDS,
    SCENARIO_DESCRIPTION,
    SCENARIO_EXPECTED_RECEIPT_STATUS,
    SIGNAL_MAP,
    generate_chaos_report,
    inject_network_partition,
    kill_process,
    run_bot_fault_injection_matrix,
    verify_receipt_consistency,
)
from services.error_codes import AppError, ErrorCodes


# ════════════════════════════════════════════════════════════════
# A. ChaosScenario 枚举完整性测试
# ════════════════════════════════════════════════════════════════


class TestChaosScenarioEnum:
    """A. ChaosScenario 枚举完整性测试(7 种故障场景)。"""

    def test_scenario_count_is_seven(self):
        """ChaosScenario 必须有 7 个成员。"""
        assert len(list(ChaosScenario)) == 7, (
            f"ChaosScenario 应有 7 个成员,实际 {len(list(ChaosScenario))}"
        )

    def test_all_required_scenarios_present(self):
        """必须包含 R55 §20 要求的 7 种故障场景。"""
        required = {
            "network_partition",
            "process_kill",
            "disk_full",
            "redis_down",
            "crdb_timeout",
            "r2_unavailable",
            "telegram_flood_wait",
        }
        actual = {s.value for s in ChaosScenario}
        missing = required - actual
        assert not missing, f"ChaosScenario 缺少成员: {missing}"

    def test_scenario_values_are_strings(self):
        """所有 scenario 值必须是字符串(str Enum)。"""
        for s in ChaosScenario:
            assert isinstance(s.value, str), (
                f"ChaosScenario.{s.name} 的值必须是字符串,实际 {type(s.value).__name__}"
            )

    def test_scenario_values_unique(self):
        """所有 scenario 值必须唯一。"""
        values = [s.value for s in ChaosScenario]
        assert len(values) == len(set(values)), "ChaosScenario 值有重复"

    def test_scenario_description_complete(self):
        """每个 scenario 必须有对应的描述。"""
        for s in ChaosScenario:
            assert s in SCENARIO_DESCRIPTION, (
                f"SCENARIO_DESCRIPTION 缺少 {s.value} 的描述"
            )
            assert isinstance(SCENARIO_DESCRIPTION[s], str)
            assert len(SCENARIO_DESCRIPTION[s]) > 0

    def test_scenario_expected_receipt_status_complete(self):
        """每个 scenario 必须有期望的 receipt 状态。"""
        for s in ChaosScenario:
            assert s in SCENARIO_EXPECTED_RECEIPT_STATUS, (
                f"SCENARIO_EXPECTED_RECEIPT_STATUS 缺少 {s.value}"
            )
            status = SCENARIO_EXPECTED_RECEIPT_STATUS[s]
            assert status in ("pending", "failed"), (
                f"scenario {s.value} 期望状态必须是 pending/failed,实际 '{status}'"
            )

    def test_process_kill_expects_pending(self):
        """PROCESS_KILL(crash-window)期望 receipt status='pending'。"""
        assert SCENARIO_EXPECTED_RECEIPT_STATUS[ChaosScenario.PROCESS_KILL] == "pending"

    def test_external_failures_expect_failed(self):
        """外部故障(网络/R2/CRDB/FloodWait)期望 receipt status='failed'。"""
        external_scenarios = [
            ChaosScenario.NETWORK_PARTITION,
            ChaosScenario.DISK_FULL,
            ChaosScenario.CRDB_TIMEOUT,
            ChaosScenario.R2_UNAVAILABLE,
            ChaosScenario.TELEGRAM_FLOOD_WAIT,
        ]
        for s in external_scenarios:
            assert SCENARIO_EXPECTED_RECEIPT_STATUS[s] == "failed", (
                f"外部故障 {s.value} 应期望 'failed'"
            )


# ════════════════════════════════════════════════════════════════
# B. BotType 枚举完整性测试
# ════════════════════════════════════════════════════════════════


class TestBotTypeEnum:
    """B. BotType 枚举完整性测试(4 个 Bot)。"""

    def test_bot_count_is_four(self):
        """BotType 必须有 4 个成员(Up/Idx/Dsp/Mon)。"""
        assert len(list(BotType)) == 4, (
            f"BotType 应有 4 个成员,实际 {len(list(BotType))}"
        )

    def test_all_required_bots_present(self):
        """必须包含 Up/Idx/Dsp/Mon 四个 Bot。"""
        required = {"up_bot", "idx_bot", "dsp_bot", "mon_bot"}
        actual = {b.value for b in BotType}
        missing = required - actual
        assert not missing, f"BotType 缺少成员: {missing}"

    def test_bot_values_are_strings(self):
        """所有 bot 值必须是字符串(str Enum)。"""
        for b in BotType:
            assert isinstance(b.value, str), (
                f"BotType.{b.name} 的值必须是字符串,实际 {type(b.value).__name__}"
            )

    def test_bot_values_unique(self):
        """所有 bot 值必须唯一。"""
        values = [b.value for b in BotType]
        assert len(values) == len(set(values)), "BotType 值有重复"

    def test_bot_process_patterns_complete(self):
        """每个 Bot 必须有进程匹配模式。"""
        for b in BotType:
            assert b in BOT_PROCESS_PATTERNS, (
                f"BOT_PROCESS_PATTERNS 缺少 {b.value}"
            )
            pattern = BOT_PROCESS_PATTERNS[b]
            assert isinstance(pattern, str)
            assert "python" in pattern, (
                f"BotType.{b.name} 进程模式应包含 'python',实际 '{pattern}'"
            )

    def test_bot_main_chain_effects_complete(self):
        """每个 Bot 必须有主链 effect types。"""
        for b in BotType:
            assert b in BOT_MAIN_CHAIN_EFFECTS, (
                f"BOT_MAIN_CHAIN_EFFECTS 缺少 {b.value}"
            )
            effects = BOT_MAIN_CHAIN_EFFECTS[b]
            assert isinstance(effects, list)
            assert len(effects) > 0, (
                f"BotType.{b.name} 主链 effects 不应为空"
            )

    def test_bot_main_chain_description_complete(self):
        """每个 Bot 必须有主链描述。"""
        for b in BotType:
            assert b in BOT_MAIN_CHAIN_DESCRIPTION, (
                f"BOT_MAIN_CHAIN_DESCRIPTION 缺少 {b.value}"
            )
            desc = BOT_MAIN_CHAIN_DESCRIPTION[b]
            assert isinstance(desc, str)
            assert len(desc) > 0

    def test_up_bot_effects_include_r2_put(self):
        """UP_BOT 主链应包含 r2_put(R2 上传)。"""
        effects = BOT_MAIN_CHAIN_EFFECTS[BotType.UP_BOT]
        assert "r2_put" in effects, (
            f"UP_BOT 主链应包含 'r2_put',实际 {effects}"
        )

    def test_dsp_bot_effects_include_edit_caption(self):
        """DSP_BOT 主链应包含 telegram_edit_caption(caption 编辑)。"""
        effects = BOT_MAIN_CHAIN_EFFECTS[BotType.DSP_BOT]
        assert "telegram_edit_caption" in effects, (
            f"DSP_BOT 主链应包含 'telegram_edit_caption',实际 {effects}"
        )


# ════════════════════════════════════════════════════════════════
# C. inject_network_partition 参数校验测试
# ════════════════════════════════════════════════════════════════


class TestInjectNetworkPartitionValidation:
    """C. inject_network_partition 参数校验测试(fail-closed)。"""

    def test_valid_dry_run(self):
        """有效参数 + dry_run=True 应正常返回。"""
        result = inject_network_partition(
            target="up_bot", duration=10, method="iptables", dry_run=True,
        )
        assert result["target"] == "up_bot"
        assert result["duration"] == 10
        assert result["method"] == "iptables"
        assert result["status"] == "injected"
        assert len(result["commands"]) > 0
        assert result["started_at"]
        assert result["completed_at"]

    def test_empty_target_raises_apperror(self):
        """target 为空字符串 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            inject_network_partition(target="", duration=10, dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_none_target_raises_apperror(self):
        """target 为 None → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            inject_network_partition(target=None, duration=10, dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_whitespace_target_raises_apperror(self):
        """target 为纯空白 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            inject_network_partition(target="   ", duration=10, dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_zero_duration_raises_apperror(self):
        """duration=0 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            inject_network_partition(target="up_bot", duration=0, dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_negative_duration_raises_apperror(self):
        """duration=-1 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            inject_network_partition(target="up_bot", duration=-1, dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_bool_duration_raises_apperror(self):
        """duration=True(bool)→ raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            inject_network_partition(target="up_bot", duration=True, dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_string_duration_raises_apperror(self):
        """duration="30"(字符串)→ raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            inject_network_partition(target="up_bot", duration="30", dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_invalid_method_raises_apperror(self):
        """method='invalid' → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            inject_network_partition(
                target="up_bot", duration=10, method="invalid", dry_run=True,
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_auto_method_dry_run(self):
        """method='auto' + dry_run → 应回退到 docker 或 iptables。"""
        result = inject_network_partition(
            target="up_bot", duration=5, method="auto", dry_run=True,
        )
        assert result["method"] in ("docker", "iptables")
        assert result["status"] == "injected"

    def test_docker_method_dry_run(self):
        """method='docker' + dry_run → 应使用 docker。"""
        result = inject_network_partition(
            target="up_bot", duration=5, method="docker", dry_run=True,
        )
        assert result["method"] == "docker"

    def test_iptables_method_dry_run(self):
        """method='iptables' + dry_run → 应使用 iptables。"""
        result = inject_network_partition(
            target="up_bot", duration=5, method="iptables", dry_run=True,
        )
        assert result["method"] == "iptables"

    def test_result_has_required_fields(self):
        """返回结果必须包含所有必要字段。"""
        result = inject_network_partition(
            target="up_bot", duration=10, dry_run=True,
        )
        required_fields = {
            "target", "duration", "method", "commands",
            "status", "started_at", "completed_at",
        }
        assert required_fields.issubset(set(result.keys())), (
            f"返回结果缺少字段: {required_fields - set(result.keys())}"
        )


# ════════════════════════════════════════════════════════════════
# D. kill_process 参数校验测试
# ════════════════════════════════════════════════════════════════


class TestKillProcessValidation:
    """D. kill_process 参数校验测试(fail-closed)。"""

    def test_valid_dry_run(self):
        """有效参数 + dry_run=True 应正常返回。"""
        result = kill_process(
            bot_name="up_bot", signal="SIGKILL", dry_run=True,
        )
        assert result["bot_name"] == "up_bot"
        assert result["signal"] == "SIGKILL"
        assert result["killed"] is True
        assert result["status"] == "dry_run"
        assert result["receipt_verified"] is False
        assert result["receipt_consistent"] is True

    def test_empty_bot_name_raises_apperror(self):
        """bot_name 为空 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            kill_process(bot_name="", signal="SIGKILL", dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_invalid_bot_name_raises_apperror(self):
        """bot_name 不在 BotType 中 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            kill_process(bot_name="invalid_bot", signal="SIGKILL", dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_none_bot_name_raises_apperror(self):
        """bot_name 为 None → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            kill_process(bot_name=None, signal="SIGKILL", dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_empty_signal_raises_apperror(self):
        """signal 为空 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            kill_process(bot_name="up_bot", signal="", dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_invalid_signal_raises_apperror(self):
        """signal 不在允许列表中 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            kill_process(bot_name="up_bot", signal="SIGBOGUS", dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_invalid_expected_receipt_status_raises_apperror(self):
        """expected_receipt_status 无效 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            kill_process(
                bot_name="up_bot", signal="SIGKILL",
                expected_receipt_status="invalid",
                dry_run=True,
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_all_valid_bots_dry_run(self):
        """所有 4 个 Bot 在 dry_run 模式下应正常。"""
        for bot_name in ("up_bot", "idx_bot", "dsp_bot", "mon_bot"):
            result = kill_process(
                bot_name=bot_name, signal="SIGKILL", dry_run=True,
            )
            assert result["bot_name"] == bot_name
            assert result["killed"] is True

    def test_all_valid_signals_dry_run(self):
        """所有支持的信号在 dry_run 模式下应正常。"""
        for sig in ("SIGKILL", "SIGTERM", "SIGHUP", "SIGINT"):
            result = kill_process(
                bot_name="up_bot", signal=sig, dry_run=True,
            )
            assert result["signal"] == sig

    def test_signal_case_insensitive(self):
        """signal 大小写不敏感('sigkill' 应正常)。"""
        result = kill_process(
            bot_name="up_bot", signal="sigkill", dry_run=True,
        )
        assert result["signal"] == "SIGKILL"

    def test_result_has_required_fields(self):
        """返回结果必须包含所有必要字段。"""
        result = kill_process(
            bot_name="up_bot", signal="SIGKILL", dry_run=True,
        )
        required_fields = {
            "bot_name", "signal", "pid", "killed",
            "receipt_verified", "receipt_consistent",
            "expected_status", "status",
            "started_at", "completed_at",
        }
        assert required_fields.issubset(set(result.keys())), (
            f"返回结果缺少字段: {required_fields - set(result.keys())}"
        )

    def test_with_receipt_state_dry_run(self):
        """提供 receipt_state + dry_run → 应验证 receipt(dry_run 跳过状态校验)。"""
        receipt_state = {
            "pending_count": 2,
            "failed_count": 0,
            "completed_count": 5,
            "hash_mismatch_count": 0,
            "orphan_completed_count": 0,
        }
        result = kill_process(
            bot_name="up_bot", signal="SIGKILL",
            dry_run=True,
            receipt_state=receipt_state,
            expected_receipt_status="pending",
        )
        assert result["receipt_verified"] is True
        assert result["receipt_consistent"] is True


# ════════════════════════════════════════════════════════════════
# E. verify_receipt_consistency 逻辑测试
# ════════════════════════════════════════════════════════════════


class TestVerifyReceiptConsistency:
    """E. verify_receipt_consistency 逻辑测试(一致性规则)。"""

    def test_valid_dry_run_no_state(self):
        """dry_run + 无 receipt_state → 仅结构性验证,consistent=True。"""
        result = verify_receipt_consistency(
            BotType.UP_BOT, ChaosScenario.PROCESS_KILL, dry_run=True,
        )
        assert result["consistent"] is True
        assert result["bot_type"] == "up_bot"
        assert result["scenario"] == "process_kill"
        assert len(result["main_chain_effects"]) > 0

    def test_valid_with_consistent_state(self):
        """一致的 receipt_state(无 hash_mismatch/orphan)→ consistent=True。"""
        receipt_state = {
            "pending_count": 3,
            "failed_count": 0,
            "completed_count": 5,
            "hash_mismatch_count": 0,
            "orphan_completed_count": 0,
        }
        result = verify_receipt_consistency(
            BotType.UP_BOT, ChaosScenario.PROCESS_KILL,
            receipt_state=receipt_state,
            expected_status="pending",
            dry_run=False,
        )
        assert result["consistent"] is True

    def test_hash_mismatch_raises_apperror(self):
        """hash_mismatch_count > 0 → raise AppError(fail-closed)。"""
        receipt_state = {
            "pending_count": 1,
            "failed_count": 0,
            "completed_count": 0,
            "hash_mismatch_count": 1,
            "orphan_completed_count": 0,
        }
        with pytest.raises(AppError) as exc_info:
            verify_receipt_consistency(
                BotType.UP_BOT, ChaosScenario.PROCESS_KILL,
                receipt_state=receipt_state,
                expected_status="pending",
                dry_run=False,
            )
        assert exc_info.value.code == ErrorCodes.EFFECT_RECEIPT_DB_ERROR

    def test_orphan_completed_raises_apperror(self):
        """orphan_completed_count > 0 → raise AppError(fail-closed)。"""
        receipt_state = {
            "pending_count": 1,
            "failed_count": 0,
            "completed_count": 2,
            "hash_mismatch_count": 0,
            "orphan_completed_count": 1,
        }
        with pytest.raises(AppError) as exc_info:
            verify_receipt_consistency(
                BotType.UP_BOT, ChaosScenario.PROCESS_KILL,
                receipt_state=receipt_state,
                expected_status="pending",
                dry_run=False,
            )
        assert exc_info.value.code == ErrorCodes.EFFECT_RECEIPT_DB_ERROR

    def test_expected_pending_but_all_failed_raises_apperror(self):
        """期望 pending 但全部 failed(无 pending)→ raise AppError。"""
        receipt_state = {
            "pending_count": 0,
            "failed_count": 3,
            "completed_count": 0,
            "hash_mismatch_count": 0,
            "orphan_completed_count": 0,
        }
        with pytest.raises(AppError) as exc_info:
            verify_receipt_consistency(
                BotType.UP_BOT, ChaosScenario.PROCESS_KILL,
                receipt_state=receipt_state,
                expected_status="pending",
                dry_run=False,
            )
        assert exc_info.value.code == ErrorCodes.EFFECT_RECEIPT_DB_ERROR

    def test_expected_failed_but_all_pending_raises_apperror(self):
        """期望 failed 但全部 pending(无 failed)→ raise AppError。"""
        receipt_state = {
            "pending_count": 3,
            "failed_count": 0,
            "completed_count": 0,
            "hash_mismatch_count": 0,
            "orphan_completed_count": 0,
        }
        with pytest.raises(AppError) as exc_info:
            verify_receipt_consistency(
                BotType.UP_BOT, ChaosScenario.NETWORK_PARTITION,
                receipt_state=receipt_state,
                expected_status="failed",
                dry_run=False,
            )
        assert exc_info.value.code == ErrorCodes.EFFECT_RECEIPT_DB_ERROR

    def test_expected_pending_with_some_failed_ok(self):
        """期望 pending 且有 pending(即使有 failed)→ consistent=True。"""
        receipt_state = {
            "pending_count": 2,
            "failed_count": 1,
            "completed_count": 0,
            "hash_mismatch_count": 0,
            "orphan_completed_count": 0,
        }
        result = verify_receipt_consistency(
            BotType.UP_BOT, ChaosScenario.PROCESS_KILL,
            receipt_state=receipt_state,
            expected_status="pending",
            dry_run=False,
        )
        assert result["consistent"] is True

    def test_expected_failed_with_some_pending_ok(self):
        """期望 failed 且有 failed(即使有 pending)→ consistent=True。"""
        receipt_state = {
            "pending_count": 1,
            "failed_count": 2,
            "completed_count": 0,
            "hash_mismatch_count": 0,
            "orphan_completed_count": 0,
        }
        result = verify_receipt_consistency(
            BotType.UP_BOT, ChaosScenario.NETWORK_PARTITION,
            receipt_state=receipt_state,
            expected_status="failed",
            dry_run=False,
        )
        assert result["consistent"] is True

    def test_invalid_bot_type_raises_apperror(self):
        """无效 bot_type → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            verify_receipt_consistency(
                "invalid_bot", ChaosScenario.PROCESS_KILL, dry_run=True,
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_invalid_scenario_raises_apperror(self):
        """无效 scenario → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            verify_receipt_consistency(
                BotType.UP_BOT, "invalid_scenario", dry_run=True,
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_invalid_expected_status_raises_apperror(self):
        """无效 expected_status → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            verify_receipt_consistency(
                BotType.UP_BOT, ChaosScenario.PROCESS_KILL,
                expected_status="invalid", dry_run=True,
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_result_has_required_fields(self):
        """返回结果必须包含所有必要字段。"""
        result = verify_receipt_consistency(
            BotType.UP_BOT, ChaosScenario.PROCESS_KILL, dry_run=True,
        )
        required_fields = {
            "bot_type", "scenario", "consistent", "expected_status",
            "main_chain_effects", "details", "verified_at",
        }
        assert required_fields.issubset(set(result.keys())), (
            f"返回结果缺少字段: {required_fields - set(result.keys())}"
        )

    def test_all_bot_scenario_combos_dry_run(self):
        """所有 28 个 bot×scenario 组合在 dry_run 模式下应 consistent=True。"""
        for bot in BotType:
            for scn in ChaosScenario:
                result = verify_receipt_consistency(
                    bot, scn, dry_run=True,
                )
                assert result["consistent"] is True, (
                    f"bot={bot.value} scenario={scn.value} dry_run 应 consistent=True"
                )


# ════════════════════════════════════════════════════════════════
# F. run_bot_fault_injection_matrix 矩阵完整性测试
# ════════════════════════════════════════════════════════════════


class TestRunBotFaultInjectionMatrix:
    """F. run_bot_fault_injection_matrix 矩阵完整性测试(4×7=28)。"""

    def test_full_matrix_dry_run(self):
        """完整矩阵(4 bot × 7 scenario = 28)dry_run 模式应全部 pass。"""
        result = run_bot_fault_injection_matrix(dry_run=True)
        assert result["matrix_size"] == MATRIX_TOTAL_COMBINATIONS
        assert result["matrix_size"] == 28
        assert result["summary"]["total"] == 28
        assert result["summary"]["passed"] == 28
        assert result["summary"]["failed"] == 0
        assert len(result["results"]) == 28

    def test_matrix_constants(self):
        """矩阵常量正确(4 bot × 7 scenario = 28)。"""
        assert MATRIX_BOT_COUNT == 4
        assert MATRIX_SCENARIO_COUNT == 7
        assert MATRIX_TOTAL_COMBINATIONS == 28

    def test_matrix_bots_tested(self):
        """矩阵测试的 Bot 列表应包含全部 4 个。"""
        result = run_bot_fault_injection_matrix(dry_run=True)
        assert set(result["bots_tested"]) == {
            "up_bot", "idx_bot", "dsp_bot", "mon_bot",
        }
        assert len(result["bots_tested"]) == 4

    def test_matrix_scenarios_tested(self):
        """矩阵测试的 scenario 列表应包含全部 7 个。"""
        result = run_bot_fault_injection_matrix(dry_run=True)
        expected_scenarios = {s.value for s in ChaosScenario}
        assert set(result["scenarios_tested"]) == expected_scenarios
        assert len(result["scenarios_tested"]) == 7

    def test_matrix_all_results_pass(self):
        """矩阵中每个 combo 的 status 应为 'pass'。"""
        result = run_bot_fault_injection_matrix(dry_run=True)
        for combo in result["results"]:
            assert combo["status"] == "pass", (
                f"combo {combo['combo_key']} status={combo['status']}, "
                f"expected 'pass'"
            )
            assert combo["receipt_consistent"] is True
            assert combo["error"] is None

    def test_matrix_rto_met(self):
        """矩阵中每个 combo 的 RTO 应达标(≤ 60s,dry_run 模式下必然达标)。"""
        result = run_bot_fault_injection_matrix(dry_run=True)
        for combo in result["results"]:
            assert combo["rto_met"] is True, (
                f"combo {combo['combo_key']} RTO 未达标: "
                f"{combo['rto_seconds']}s > {combo['rto_target']}s"
            )
            assert combo["rto_target"] == RTO_TARGET_SECONDS

    def test_matrix_result_has_required_fields(self):
        """矩阵返回结果必须包含所有必要字段。"""
        result = run_bot_fault_injection_matrix(dry_run=True)
        required_fields = {
            "matrix_size", "bots_tested", "scenarios_tested",
            "results", "summary", "rto_target_seconds",
            "started_at", "completed_at", "duration_seconds", "dry_run",
        }
        assert required_fields.issubset(set(result.keys())), (
            f"矩阵结果缺少字段: {required_fields - set(result.keys())}"
        )

    def test_matrix_combo_has_required_fields(self):
        """矩阵中每个 combo 必须包含所有必要字段。"""
        result = run_bot_fault_injection_matrix(dry_run=True)
        required_combo_fields = {
            "bot_type", "scenario", "combo_key", "status",
            "rto_seconds", "rto_target", "rto_met",
            "receipt_consistent", "fault_injection_result",
            "receipt_verification_result", "error",
            "started_at", "completed_at",
        }
        for combo in result["results"]:
            assert required_combo_fields.issubset(set(combo.keys())), (
                f"combo {combo.get('combo_key', '?')} 缺少字段: "
                f"{required_combo_fields - set(combo.keys())}"
            )

    def test_matrix_subset_bots(self):
        """指定部分 Bot 列表应只测试指定的 Bot。"""
        result = run_bot_fault_injection_matrix(
            bots=[BotType.UP_BOT, BotType.IDX_BOT],
            dry_run=True,
        )
        assert result["matrix_size"] == 2 * 7  # 2 bot × 7 scenario = 14
        assert set(result["bots_tested"]) == {"up_bot", "idx_bot"}

    def test_matrix_subset_scenarios(self):
        """指定部分 scenario 列表应只测试指定的 scenario。"""
        result = run_bot_fault_injection_matrix(
            scenarios=[ChaosScenario.PROCESS_KILL, ChaosScenario.NETWORK_PARTITION],
            dry_run=True,
        )
        assert result["matrix_size"] == 4 * 2  # 4 bot × 2 scenario = 8
        assert set(result["scenarios_tested"]) == {
            "process_kill", "network_partition",
        }

    def test_matrix_invalid_duration_raises_apperror(self):
        """无效 duration → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            run_bot_fault_injection_matrix(duration=0, dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_matrix_empty_bots_raises_apperror(self):
        """空 bots 列表 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            run_bot_fault_injection_matrix(bots=[], dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_matrix_empty_scenarios_raises_apperror(self):
        """空 scenarios 列表 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            run_bot_fault_injection_matrix(scenarios=[], dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_matrix_invalid_bot_raises_apperror(self):
        """无效 bot(字符串)→ raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            run_bot_fault_injection_matrix(
                bots=["invalid_bot"], dry_run=True,
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_matrix_invalid_scenario_raises_apperror(self):
        """无效 scenario(字符串)→ raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            run_bot_fault_injection_matrix(
                scenarios=["invalid_scenario"], dry_run=True,
            )
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_matrix_each_combo_has_fault_injection_result(self):
        """每个 combo 必须有 fault_injection_result。"""
        result = run_bot_fault_injection_matrix(dry_run=True)
        for combo in result["results"]:
            assert combo["fault_injection_result"] is not None, (
                f"combo {combo['combo_key']} 缺少 fault_injection_result"
            )

    def test_matrix_each_combo_has_receipt_verification(self):
        """每个 combo 必须有 receipt_verification_result。"""
        result = run_bot_fault_injection_matrix(dry_run=True)
        for combo in result["results"]:
            assert combo["receipt_verification_result"] is not None, (
                f"combo {combo['combo_key']} 缺少 receipt_verification_result"
            )
            rv = combo["receipt_verification_result"]
            assert rv["consistent"] is True

    def test_matrix_combo_keys_unique(self):
        """所有 combo_key 必须唯一。"""
        result = run_bot_fault_injection_matrix(dry_run=True)
        keys = [c["combo_key"] for c in result["results"]]
        assert len(keys) == len(set(keys)), "combo_key 有重复"

    def test_matrix_covers_all_combinations(self):
        """矩阵必须覆盖所有 28 个 bot×scenario 组合。"""
        result = run_bot_fault_injection_matrix(dry_run=True)
        expected_keys = set()
        for bot in BotType:
            for scn in ChaosScenario:
                expected_keys.add(f"{bot.value}:{scn.value}")
        actual_keys = {c["combo_key"] for c in result["results"]}
        assert actual_keys == expected_keys, (
            f"矩阵缺少组合: {expected_keys - actual_keys}"
        )


# ════════════════════════════════════════════════════════════════
# G. generate_chaos_report 输出格式测试
# ════════════════════════════════════════════════════════════════


class TestGenerateChaosReport:
    """G. generate_chaos_report 输出格式测试(JSON 结构)。"""

    def test_valid_report_generation(self):
        """有效 results → 生成 JSON 字符串。"""
        matrix_result = run_bot_fault_injection_matrix(dry_run=True)
        report_json = generate_chaos_report(matrix_result)
        assert isinstance(report_json, str)
        assert len(report_json) > 0

    def test_report_is_valid_json(self):
        """生成的报告必须是有效 JSON。"""
        matrix_result = run_bot_fault_injection_matrix(dry_run=True)
        report_json = generate_chaos_report(matrix_result)
        report = json.loads(report_json)
        assert isinstance(report, dict)

    def test_report_has_required_fields(self):
        """报告必须包含所有必要字段。"""
        matrix_result = run_bot_fault_injection_matrix(dry_run=True)
        report_json = generate_chaos_report(matrix_result)
        report = json.loads(report_json)
        required_fields = {
            "report_type", "report_version", "generated_at",
            "matrix_size", "bots_tested", "scenarios_tested",
            "summary", "rto_target_seconds",
            "started_at", "completed_at", "duration_seconds",
            "dry_run", "results",
            "bot_main_chains", "scenario_descriptions",
        }
        assert required_fields.issubset(set(report.keys())), (
            f"报告缺少字段: {required_fields - set(report.keys())}"
        )

    def test_report_type_correct(self):
        """report_type 应为 'r55_section20_bot_fault_injection'。"""
        matrix_result = run_bot_fault_injection_matrix(dry_run=True)
        report_json = generate_chaos_report(matrix_result)
        report = json.loads(report_json)
        assert report["report_type"] == "r55_section20_bot_fault_injection"

    def test_report_version_correct(self):
        """report_version 应为 '1.0'。"""
        matrix_result = run_bot_fault_injection_matrix(dry_run=True)
        report_json = generate_chaos_report(matrix_result)
        report = json.loads(report_json)
        assert report["report_version"] == "1.0"

    def test_report_matrix_size_matches(self):
        """报告中的 matrix_size 应与输入一致。"""
        matrix_result = run_bot_fault_injection_matrix(dry_run=True)
        report_json = generate_chaos_report(matrix_result)
        report = json.loads(report_json)
        assert report["matrix_size"] == 28
        assert report["matrix_size"] == matrix_result["matrix_size"]

    def test_report_summary_matches(self):
        """报告中的 summary 应与输入一致。"""
        matrix_result = run_bot_fault_injection_matrix(dry_run=True)
        report_json = generate_chaos_report(matrix_result)
        report = json.loads(report_json)
        assert report["summary"]["total"] == 28
        assert report["summary"]["passed"] == 28
        assert report["summary"]["failed"] == 0

    def test_report_bot_main_chains_complete(self):
        """报告中 bot_main_chains 应包含全部 4 个 Bot。"""
        matrix_result = run_bot_fault_injection_matrix(dry_run=True)
        report_json = generate_chaos_report(matrix_result)
        report = json.loads(report_json)
        assert set(report["bot_main_chains"].keys()) == {
            "up_bot", "idx_bot", "dsp_bot", "mon_bot",
        }

    def test_report_scenario_descriptions_complete(self):
        """报告中 scenario_descriptions 应包含全部 7 个场景。"""
        matrix_result = run_bot_fault_injection_matrix(dry_run=True)
        report_json = generate_chaos_report(matrix_result)
        report = json.loads(report_json)
        assert len(report["scenario_descriptions"]) == 7

    def test_report_results_count_matches(self):
        """报告中 results 数量应与 matrix_size 一致。"""
        matrix_result = run_bot_fault_injection_matrix(dry_run=True)
        report_json = generate_chaos_report(matrix_result)
        report = json.loads(report_json)
        assert len(report["results"]) == 28

    def test_invalid_results_type_raises_apperror(self):
        """results 非 dict → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            generate_chaos_report("not a dict")
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_results_missing_required_fields_raises_apperror(self):
        """results 缺少必要字段 → raise AppError(VALIDATION_FAILED)。"""
        with pytest.raises(AppError) as exc_info:
            generate_chaos_report({"incomplete": True})
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED

    def test_report_json_serializable_with_unicode(self):
        """报告 JSON 应正确处理 Unicode(中文描述)。"""
        matrix_result = run_bot_fault_injection_matrix(dry_run=True)
        report_json = generate_chaos_report(matrix_result)
        # 确保中文不乱码(ensure_ascii=False)
        assert "上传" in report_json or "Receipt" in report_json
        # 确保可反序列化
        report = json.loads(report_json)
        assert isinstance(report["bot_main_chains"]["up_bot"], str)


# ════════════════════════════════════════════════════════════════
# H. fail-closed 行为测试
# ════════════════════════════════════════════════════════════════


class TestFailClosedBehavior:
    """H. fail-closed 行为测试(任何验证失败立即 raise AppError)。"""

    def test_inject_network_partition_fail_closed(self):
        """inject_network_partition 参数校验 fail-closed。"""
        # 空目标
        with pytest.raises(AppError):
            inject_network_partition(target="", duration=10, dry_run=True)
        # 负 duration
        with pytest.raises(AppError):
            inject_network_partition(target="up_bot", duration=-1, dry_run=True)
        # 无效 method
        with pytest.raises(AppError):
            inject_network_partition(
                target="up_bot", duration=10, method="bogus", dry_run=True,
            )

    def test_kill_process_fail_closed(self):
        """kill_process 参数校验 fail-closed。"""
        with pytest.raises(AppError):
            kill_process(bot_name="", dry_run=True)
        with pytest.raises(AppError):
            kill_process(bot_name="up_bot", signal="BOGUS", dry_run=True)
        with pytest.raises(AppError):
            kill_process(
                bot_name="up_bot", signal="SIGKILL",
                expected_receipt_status="bogus", dry_run=True,
            )

    def test_verify_receipt_consistency_fail_closed(self):
        """verify_receipt_consistency 不一致时 fail-closed。"""
        # hash_mismatch > 0 → fail-closed
        bad_state = {
            "pending_count": 0,
            "failed_count": 0,
            "completed_count": 0,
            "hash_mismatch_count": 1,
            "orphan_completed_count": 0,
        }
        with pytest.raises(AppError) as exc_info:
            verify_receipt_consistency(
                BotType.UP_BOT, ChaosScenario.PROCESS_KILL,
                receipt_state=bad_state, dry_run=False,
            )
        assert exc_info.value.code == ErrorCodes.EFFECT_RECEIPT_DB_ERROR

    def test_matrix_fail_closed_on_inconsistent_receipt(self):
        """矩阵中 receipt 不一致时 fail-closed,立即 raise。

        限定到 up_bot:process_kill 单组合:kill_process 在 pgrep 不可用环境
        (如 Windows CI)下不会 raise(仅 status=not_found),从而让执行流
        走到 verify_receipt_consistency,由 receipt_state 不一致触发 fail-closed。
        """
        # 为 up_bot:process_kill 提供不一致的 receipt_state
        # (hash_mismatch > 0)
        bad_state = {
            "pending_count": 0,
            "failed_count": 0,
            "completed_count": 0,
            "hash_mismatch_count": 1,
            "orphan_completed_count": 0,
        }
        receipt_states = {
            "up_bot:process_kill": bad_state,
        }
        with pytest.raises(AppError) as exc_info:
            run_bot_fault_injection_matrix(
                bots=[BotType.UP_BOT],
                scenarios=[ChaosScenario.PROCESS_KILL],
                dry_run=False,  # 需要 dry_run=False 才会校验 receipt_state
                receipt_states=receipt_states,
            )
        # fail-closed 应 raise AppError
        assert exc_info.value.code == ErrorCodes.EFFECT_RECEIPT_DB_ERROR

    def test_app_error_has_trace_id(self):
        """AppError 必须携带 trace_id(贯穿全链路)。"""
        with pytest.raises(AppError) as exc_info:
            inject_network_partition(target="", duration=10, dry_run=True)
        assert exc_info.value.trace_id
        assert len(exc_info.value.trace_id) > 0

    def test_app_error_has_code(self):
        """AppError 必须携带 code(三段式错误码)。"""
        with pytest.raises(AppError) as exc_info:
            kill_process(bot_name="bogus", dry_run=True)
        assert exc_info.value.code == ErrorCodes.VALIDATION_FAILED
        # 三段式:DOMAIN.OPERATION.REASON
        assert exc_info.value.code.count(".") >= 2

    def test_matrix_dry_run_never_fails(self):
        """dry_run 模式下矩阵不应 fail-closed(无真实故障)。"""
        # 干跑 28 个组合,全部应 pass
        result = run_bot_fault_injection_matrix(dry_run=True)
        assert result["summary"]["failed"] == 0
        assert result["summary"]["passed"] == 28

    def test_signal_map_complete(self):
        """SIGNAL_MAP 必须包含所有支持的信号。"""
        required_signals = {"SIGKILL", "SIGTERM", "SIGHUP", "SIGINT"}
        assert required_signals.issubset(set(SIGNAL_MAP.keys()))
        for sig, arg in SIGNAL_MAP.items():
            assert isinstance(arg, str)
            assert arg.startswith("-")

    def test_rto_target_is_60_seconds(self):
        """RTO 目标必须是 60 秒。"""
        assert RTO_TARGET_SECONDS == 60
