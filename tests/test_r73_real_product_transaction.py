"""R73 P0-04: 真实产品交易链整改测试。

验证 scripts/synthetic_transaction.py 的整改:
1. _build_file_index_message 返回的消息中 mark_dirty=True
2. _build_dsp_dispatch_message 返回的消息包含 trace_id
3. verify_crdb_sync_result 在 CRDB 不可用时返回 fail-closed
4. 故障注入函数能正确停止/启动角色(mock docker compose 命令)

R73 §5.2 整改背景:
    原实现存在以下违反 R73 P0-04 的问题:
    - _build_file_index_message 使用 mark_dirty=False 跳过 CRDB sync
    - 未覆盖 dsp bot 派送链路
    - 无统一 trace_id 贯穿
    - 无 CRDB sync 验证(fail-closed 缺失)
    - 无故障注入测试

整改要求:
    - mark_dirty=True 触发 dirty_outbox → crdb_sync → CRDB 完整链路
    - 新增 dsp 派送覆盖(create_outbox_entry)
    - 统一 trace_id 贯穿 Update→up→idx→dsp→writer→CRDB→输出
    - CRDB 不可用时 fail-closed
    - 故障注入测试(角色停止时交易 fail-closed)
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# conftest.py 在收集阶段已注入 config/telegram mock;此处再注入一次以防
# 本文件被单独运行(conftest 未加载场景)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNTHETIC_TRANSACTION_PATH = REPO_ROOT / "scripts" / "synthetic_transaction.py"


@pytest.fixture(scope="module")
def synthetic_tx():
    """加载 scripts/synthetic_transaction.py 模块(避免 sys.path 污染)。"""
    spec = importlib.util.spec_from_file_location(
        "_test_synthetic_tx_r73", SYNTHETIC_TRANSACTION_PATH,
    )
    assert spec is not None and spec.loader is not None, (
        f"无法加载 synthetic_transaction.py: {SYNTHETIC_TRANSACTION_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestR73MarkDirtyTrue:
    """R73 P0-04: 验证 _build_file_index_message 的 mark_dirty=True。"""

    def test_file_index_message_mark_dirty_is_true(self, synthetic_tx):
        """_build_file_index_message 返回的消息中 mark_dirty 必须为 True。

        R73 P0-04 整改:原 mark_dirty=False 跳过 CRDB sync,违反真实产品交易链要求。
        """
        trace_id = "synthetic_r73_mark_dirty_test"
        msg = synthetic_tx._build_file_index_message(trace_id)
        assert msg["data"]["mark_dirty"] is True, (
            f"R73 P0-04: mark_dirty 必须为 True,实际: "
            f"{msg['data']['mark_dirty']!r}"
        )

    def test_file_index_message_no_mark_dirty_false_comment(self):
        """源码中不应再出现 mark_dirty=False 的注释或赋值。"""
        source = SYNTHETIC_TRANSACTION_PATH.read_text(encoding="utf-8")
        # 查找 mark_dirty=False 的赋值(排除字符串字面量内的引用)
        import re
        # 匹配 "mark_dirty": False 或 mark_dirty=False(在 _build_file_index_message 内)
        offending = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            # 跳过注释行
            if stripped.startswith("#"):
                continue
            # 检测 mark_dirty=False / "mark_dirty": False
            if re.search(r'"mark_dirty"\s*:\s*False', stripped) or \
               re.search(r'\bmark_dirty\s*=\s*False\b', stripped):
                offending.append((i, stripped))
        assert not offending, (
            f"R73 P0-04: 源码中不应再出现 mark_dirty=False,发现 {len(offending)} 处: "
            f"{offending}"
        )

    def test_file_index_message_has_trace_id_in_record(self, synthetic_tx):
        """R73 P0-04: file_index 消息的 record 中应包含 trace_id 字段。"""
        trace_id = "synthetic_r73_trace_id_in_record"
        msg = synthetic_tx._build_file_index_message(trace_id)
        record = msg["data"]["record"]
        assert "trace_id" in record, (
            "R73 P0-04: file_index 消息的 record 中应包含 trace_id 字段"
        )
        assert record["trace_id"] == trace_id, (
            f"R73 P0-04: record.trace_id 应为 {trace_id!r},"
            f"实际: {record['trace_id']!r}"
        )

    def test_file_index_message_has_top_level_trace_id(self, synthetic_tx):
        """R73 P0-04: file_index 消息顶层应包含 trace_id 字段。"""
        trace_id = "synthetic_r73_top_level_trace_id"
        msg = synthetic_tx._build_file_index_message(trace_id)
        assert msg.get("trace_id") == trace_id, (
            f"R73 P0-04: file_index 消息顶层 trace_id 应为 {trace_id!r},"
            f"实际: {msg.get('trace_id')!r}"
        )


class TestR73DspDispatchMessage:
    """R73 P0-04: 验证 _build_dsp_dispatch_message 包含 trace_id。"""

    def test_dsp_dispatch_message_has_trace_id_in_data(self, synthetic_tx):
        """R85 fix: _build_dsp_dispatch_message 的 data 中不得包含 trace_id。

        create_outbox_entry 方法签名不接受 trace_id 参数,若放入 data 会导致
        db_writer._execute_sqlite 的 await method(**data) 抛 TypeError →
        永久死信 → ACK,verify 永远查不到落库记录。
        trace_id 只应放在顶层 msg["trace_id"]。
        """
        trace_id = "synthetic_r73_dsp_trace_id"
        msg = synthetic_tx._build_dsp_dispatch_message(trace_id)
        assert "trace_id" not in msg["data"], (
            "R85 fix: dsp_dispatch 消息的 data 中不得包含 trace_id 字段"
            "(create_outbox_entry 不接受此参数,会导致 TypeError)"
        )

    def test_dsp_dispatch_message_has_top_level_trace_id(self, synthetic_tx):
        """_build_dsp_dispatch_message 返回的消息顶层应包含 trace_id 字段。"""
        trace_id = "synthetic_r73_dsp_top_level"
        msg = synthetic_tx._build_dsp_dispatch_message(trace_id)
        assert msg.get("trace_id") == trace_id, (
            f"R73 P0-04: dsp_dispatch 消息顶层 trace_id 应为 {trace_id!r},"
            f"实际: {msg.get('trace_id')!r}"
        )

    def test_dsp_dispatch_message_method_name(self, synthetic_tx):
        """_build_dsp_dispatch_message 应使用 create_outbox_entry 方法。"""
        trace_id = "synthetic_r73_dsp_method"
        msg = synthetic_tx._build_dsp_dispatch_message(trace_id)
        assert msg["method_name"] == "create_outbox_entry", (
            f"R73 P0-04: dsp_dispatch 应使用 create_outbox_entry 方法,"
            f"实际: {msg['method_name']!r}"
        )

    def test_dsp_dispatch_message_message_id_unique(self, synthetic_tx):
        """dsp_dispatch 的 message_id 应使用 :dsp_dispatch 后缀,跨步骤唯一。"""
        trace_id = "synthetic_r73_dsp_msg_id"
        msg = synthetic_tx._build_dsp_dispatch_message(trace_id)
        assert msg["message_id"] == f"{trace_id}:dsp_dispatch", (
            f"R73 P0-04: dsp_dispatch message_id 应为 '{trace_id}:dsp_dispatch',"
            f"实际: {msg['message_id']!r}"
        )

    def test_dsp_dispatch_message_has_outbox_id_pk(self, synthetic_tx):
        """dsp_dispatch 消息应使用 trace_id 作为 outbox_id 主键。"""
        trace_id = "synthetic_r73_dsp_outbox_id"
        msg = synthetic_tx._build_dsp_dispatch_message(trace_id)
        assert msg["data"]["outbox_id"] == trace_id, (
            f"R73 P0-04: dsp_dispatch.data.outbox_id 应为 trace_id,"
            f"实际: {msg['data']['outbox_id']!r}"
        )


class TestR73UnifiedTraceId:
    """R73 P0-04: 验证所有消息使用同一 trace_id 贯穿。"""

    def test_all_messages_have_top_level_trace_id(self, synthetic_tx):
        """所有 _build_*_message 返回的消息顶层应包含 trace_id 字段。"""
        trace_id = "synthetic_r73_unified_trace_id"
        messages = [
            synthetic_tx._build_heartbeat_message(trace_id),
            synthetic_tx._build_upload_session_message(trace_id),
            synthetic_tx._build_file_index_message(trace_id),
            synthetic_tx._build_dsp_dispatch_message(trace_id),
        ]
        for i, msg in enumerate(messages, 1):
            assert msg.get("trace_id") == trace_id, (
                f"R73 P0-04: 第 {i} 条消息顶层 trace_id 应为 {trace_id!r},"
                f"实际: {msg.get('trace_id')!r}"
            )

    def test_all_message_ids_unique_with_same_trace_id(self, synthetic_tx):
        """同一 trace_id 下四个 message_id 互不相同(跨步骤唯一)。"""
        trace_id = "synthetic_r73_msg_ids_unique"
        ids = {
            synthetic_tx._build_heartbeat_message(trace_id)["message_id"],
            synthetic_tx._build_upload_session_message(trace_id)["message_id"],
            synthetic_tx._build_file_index_message(trace_id)["message_id"],
            synthetic_tx._build_dsp_dispatch_message(trace_id)["message_id"],
        }
        assert len(ids) == 4, (
            f"R73 P0-04: 同一 trace_id 下四个 message_id 必须互不相同,"
            f"实际集合大小: {len(ids)}"
        )


class TestR73CrdbSyncFailClosed:
    """R73 P0-04: 验证 verify_crdb_sync_result 在 CRDB 不可用时 fail-closed。"""

    def test_crdb_sync_returns_fail_closed_when_docker_unavailable(
        self, synthetic_tx,
    ):
        """CRDB 查询失败(docker 不可用)时,verify_crdb_sync_result 必须返回失败。

        fail-closed 原则:CRDB 不可用时禁止只验证 SQLite 成功。
        """
        trace_id = "synthetic_r73_crdb_fail_closed"

        # mock _query_crdb_count 返回 (-1, "", "docker unavailable", -1)
        # 模拟 docker / CRDB 不可用场景
        def fake_query(*args, **kwargs):
            return (-1, "", "docker daemon not available", -1)

        # mock time.sleep 加速测试(避免 120s 轮询)
        with patch.object(synthetic_tx, "_query_crdb_count", side_effect=fake_query), \
             patch.object(synthetic_tx, "time") as mock_time:
            # 让 time.time() 快速到达 deadline(模拟 120s 已过)
            call_count = [0]

            def fake_time():
                call_count[0] += 1
                # 前 5 次返回 0(在 deadline 内),第 6 次返回 200(超出 deadline)
                return 0 if call_count[0] <= 5 else 200

            mock_time.time.side_effect = fake_time
            mock_time.sleep = MagicMock()  # no-op sleep

            result = synthetic_tx.verify_crdb_sync_result(
                trace_id, timeout=120,
            )

        # fail-closed:CRDB 不可用时必须返回失败
        assert result.passed is False, (
            f"R73 P0-04: CRDB 不可用时 verify_crdb_sync_result 必须返回失败(fail-closed),"
            f"实际 passed={result.passed}"
        )
        assert result.error is not None, (
            "R73 P0-04: CRDB 不可用时 error 字段不应为 None"
        )
        assert "fail-closed" in result.error or "未查询到" in result.error, (
            f"R73 P0-04: error 应含 fail-closed 或未查询到描述,实际: {result.error!r}"
        )
        # evidence 应标记 crdb_unavailable
        assert result.evidence.get("crdb_unavailable") is True, (
            "R73 P0-04: evidence.crdb_unavailable 应为 True"
        )

    def test_crdb_sync_returns_pass_when_record_found(
        self, synthetic_tx,
    ):
        """CRDB 查询到记录时,verify_crdb_sync_result 应返回成功。"""
        trace_id = "synthetic_r73_crdb_pass"

        # mock _query_crdb_count 返回 count=1(找到记录)
        def fake_query(*args, **kwargs):
            return (1, "1", "", 0)

        with patch.object(synthetic_tx, "_query_crdb_count", side_effect=fake_query):
            result = synthetic_tx.verify_crdb_sync_result(
                trace_id, timeout=30,
            )

        assert result.passed is True, (
            f"R73 P0-04: CRDB 查到记录时 verify_crdb_sync_result 应返回成功,"
            f"实际 passed={result.passed}, error={result.error}"
        )
        assert result.evidence.get("found") is True
        assert result.evidence.get("row_count") == 1


class TestR73FaultInjection:
    """R73 P0-04: 验证故障注入函数能正确停止/启动角色(mock docker compose)。"""

    def test_validate_fault_role_rejects_invalid_role(self, synthetic_tx):
        """_validate_fault_role 应拒绝白名单外的角色(防止误停基础服务)。"""
        with pytest.raises(ValueError, match="非法故障注入角色"):
            synthetic_tx._validate_fault_role("redis")
        with pytest.raises(ValueError, match="非法故障注入角色"):
            synthetic_tx._validate_fault_role("db_writer")
        with pytest.raises(ValueError, match="非法故障注入角色"):
            synthetic_tx._validate_fault_role("admin")

    def test_validate_fault_role_accepts_valid_roles(self, synthetic_tx):
        """_validate_fault_role 应接受白名单内的角色。"""
        for role in ("up", "idx", "dsp", "crdb_sync"):
            assert synthetic_tx._validate_fault_role(role) == role

    def test_inject_fault_stop_role_passes_on_success(self, synthetic_tx):
        """inject_fault_stop_role 在 docker compose stop 成功时返回 passed=True。"""
        # mock _run 返回成功
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = ""
        fake_result.stderr = ""

        with patch.object(synthetic_tx, "_run", return_value=fake_result) as mock_run:
            result = synthetic_tx.inject_fault_stop_role("crdb_sync", timeout=10)

        assert result.passed is True, (
            f"docker compose stop 成功时 inject_fault_stop_role 应 passed=True,"
            f"实际: {result.passed}, error={result.error}"
        )
        assert result.evidence["role_name"] == "crdb_sync"
        assert result.evidence["action"] == "stop"
        # 验证调用了 docker compose stop crdb_sync
        called_cmd = mock_run.call_args[0][0]
        assert "stop" in called_cmd
        assert "crdb_sync" in called_cmd

    def test_inject_fault_stop_role_fails_on_error(self, synthetic_tx):
        """inject_fault_stop_role 在 docker compose stop 失败时返回 passed=False。"""
        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = "container not found"

        with patch.object(synthetic_tx, "_run", return_value=fake_result):
            result = synthetic_tx.inject_fault_stop_role("up", timeout=10)

        assert result.passed is False, (
            "docker compose stop 失败时 inject_fault_stop_role 应 passed=False"
        )
        assert "up" in result.error

    def test_inject_fault_start_role_passes_on_success(self, synthetic_tx):
        """inject_fault_start_role 在 docker compose start 成功时返回 passed=True。"""
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = ""
        fake_result.stderr = ""

        with patch.object(synthetic_tx, "_run", return_value=fake_result) as mock_run:
            result = synthetic_tx.inject_fault_start_role("dsp", timeout=30)

        assert result.passed is True
        assert result.evidence["role_name"] == "dsp"
        assert result.evidence["action"] == "start"
        called_cmd = mock_run.call_args[0][0]
        assert "start" in called_cmd
        assert "dsp" in called_cmd

    def test_verify_transaction_fails_when_role_stopped_passes_on_fail_closed(
        self, synthetic_tx,
    ):
        """verify_transaction_fails_when_role_stopped 在交易按预期失败时返回 passed=True。

        场景:角色已停止,inject 失败(消息无法到达下游),
        verify_transaction_fails_when_role_stopped 应判定 fail-closed 生效(passed=True)。
        """
        trace_id = "synthetic_r73_fault_fail_closed"

        # mock inject_test_event 返回失败(模拟消息无法到达下游)
        failed_inject = synthetic_tx.StepResult(
            step="inject",
            timestamp="2026-07-26T00:00:00Z",
            duration_seconds=0.1,
            returncode=1,
            stdout="",
            stderr="connection refused",
            passed=False,
            error="inject failed",
        )

        with patch.object(synthetic_tx, "inject_test_event", return_value=failed_inject):
            result = synthetic_tx.verify_transaction_fails_when_role_stopped(
                "crdb_sync", trace_id, timeout=5,
            )

        # inject 失败 → fail-closed 生效 → passed=True
        assert result.passed is True, (
            "R73 P0-04: 角色停止导致 inject 失败时,应判定 fail-closed 生效(passed=True)"
        )
        assert result.evidence["fail_closed_reason"] == "inject_failed_as_expected"

    def test_verify_transaction_fails_when_role_stopped_fails_on_unexpected_pass(
        self, synthetic_tx,
    ):
        """verify_transaction_fails_when_role_stopped 在交易意外通过时返回 passed=False。

        场景:角色已停止,但 verify 仍然成功(假阳性,违反 fail-closed),
        verify_transaction_fails_when_role_stopped 应判定 passed=False。
        """
        trace_id = "synthetic_r73_fault_unexpected_pass"

        # mock inject_test_event 返回成功
        passed_inject = synthetic_tx.StepResult(
            step="inject",
            timestamp="2026-07-26T00:00:00Z",
            duration_seconds=0.1,
            returncode=0,
            stdout="1234567890",
            stderr="",
            passed=True,
        )
        # mock verify_result 返回成功(假阳性 — 角色已停止但交易仍通过)
        passed_verify = synthetic_tx.StepResult(
            step="verify",
            timestamp="2026-07-26T00:00:00Z",
            duration_seconds=0.1,
            returncode=0,
            stdout="1",
            stderr="",
            passed=True,
        )

        with patch.object(synthetic_tx, "inject_test_event", return_value=passed_inject), \
             patch.object(synthetic_tx, "verify_result", return_value=passed_verify):
            result = synthetic_tx.verify_transaction_fails_when_role_stopped(
                "crdb_sync", trace_id, timeout=5,
            )

        # verify 意外通过 → 违反 fail-closed → passed=False
        assert result.passed is False, (
            "R73 P0-04: 角色停止时交易意外通过应判定违反 fail-closed(passed=False)"
        )
        assert "fail-closed" in result.error


class TestR73TransactionEvidenceFields:
    """R73 P0-04: 验证 TransactionEvidence dataclass 包含新增字段。"""

    def test_transaction_evidence_has_dsp_dispatch_fields(self, synthetic_tx):
        """TransactionEvidence 应包含 dsp_dispatch_inject/verify/idempotency 字段。"""
        from dataclasses import fields
        field_names = {f.name for f in fields(synthetic_tx.TransactionEvidence)}
        assert "dsp_dispatch_inject" in field_names
        assert "dsp_dispatch_verify" in field_names
        assert "dsp_dispatch_idempotency" in field_names

    def test_transaction_evidence_has_crdb_sync_verify_field(self, synthetic_tx):
        """TransactionEvidence 应包含 crdb_sync_verify 字段。"""
        from dataclasses import fields
        field_names = {f.name for f in fields(synthetic_tx.TransactionEvidence)}
        assert "crdb_sync_verify" in field_names

    def test_transaction_evidence_has_fault_injection_field(self, synthetic_tx):
        """TransactionEvidence 应包含 fault_injection 字段。"""
        from dataclasses import fields
        field_names = {f.name for f in fields(synthetic_tx.TransactionEvidence)}
        assert "fault_injection" in field_names

    def test_transaction_evidence_default_skipped_steps(self, synthetic_tx):
        """新增字段的默认值应为 skipped step(passed=False)。"""
        # 构造一个最小合法的 TransactionEvidence
        skipped = synthetic_tx._skipped_step("test")
        evidence = synthetic_tx.TransactionEvidence(
            trace_id="test",
            started_at="2026-07-26T00:00:00Z",
            finished_at="2026-07-26T00:00:00Z",
            overall_passed=False,
            inject=skipped,
            verify=skipped,
            idempotency=skipped,
            failure_scenario=skipped,
            cleanup=skipped,
        )
        # dsp_dispatch_* 默认应为 skipped
        assert evidence.dsp_dispatch_inject.passed is False
        assert evidence.dsp_dispatch_verify.passed is False
        assert evidence.dsp_dispatch_idempotency.passed is False
        # crdb_sync_verify 默认应为 skipped
        assert evidence.crdb_sync_verify.passed is False
        # fault_injection 默认应为空 dict
        assert evidence.fault_injection == {}


class TestR73CleanupExtended:
    """R73 P0-04: 验证 cleanup 函数清理范围扩展。"""

    def test_cleanup_cleans_extended_tables(self, synthetic_tx):
        """cleanup 应清理 bot_heartbeat + upload_sessions + file_records_local
        + upload_outbox + dirty_outbox + writer_inbox 六个表。"""
        trace_id = "synthetic_r73_cleanup_test"

        # mock _delete_rows 返回成功(删除 0 行,但命令成功)
        def fake_delete(target_db, table, column, where_value, timeout=30):
            return (0, "0", "", 0)

        # mock _run 用于 writer_inbox LIKE 删除 + CRDB 清理
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "0"
        fake_result.stderr = ""

        with patch.object(synthetic_tx, "_delete_rows", side_effect=fake_delete) as mock_delete, \
             patch.object(synthetic_tx, "_run", return_value=fake_result):
            result = synthetic_tx.cleanup(trace_id, timeout=10)

        # cleanup 应通过
        assert result.passed is True, (
            f"cleanup 在所有命令成功时应 passed=True,实际: {result.passed}, "
            f"error: {result.error}"
        )
        # 验证清理的表列表包含新增的 upload_outbox / dirty_outbox / writer_inbox
        tables_cleaned = result.evidence.get("tables_cleaned", [])
        assert "upload_outbox" in tables_cleaned, (
            f"R73 P0-04: cleanup 应清理 upload_outbox,实际: {tables_cleaned}"
        )
        assert "dirty_outbox" in tables_cleaned, (
            f"R73 P0-04: cleanup 应清理 dirty_outbox,实际: {tables_cleaned}"
        )
        assert "writer_inbox" in tables_cleaned, (
            f"R73 P0-04: cleanup 应清理 writer_inbox,实际: {tables_cleaned}"
        )
        # 验证 crdb_cleaned 标记为 True
        assert result.evidence.get("crdb_cleaned") is True, (
            "R73 P0-04: cleanup 应清理 CRDB,crdb_cleaned 应为 True"
        )

    def test_cleanup_fails_when_crdb_clean_fails(self, synthetic_tx):
        """cleanup 在 CRDB 清理失败时应返回 passed=False(fail-closed)。"""
        trace_id = "synthetic_r73_cleanup_crdb_fail"

        # mock _delete_rows 返回成功
        def fake_delete(*args, **kwargs):
            return (0, "0", "", 0)

        # mock _run:writer_inbox LIKE 成功,CRDB 清理失败
        # 需要分别 mock 两次调用
        crdb_fail_result = MagicMock()
        crdb_fail_result.returncode = 1
        crdb_fail_result.stdout = ""
        crdb_fail_result.stderr = "CRDB unavailable"

        like_ok_result = MagicMock()
        like_ok_result.returncode = 0
        like_ok_result.stdout = "0"
        like_ok_result.stderr = ""

        with patch.object(synthetic_tx, "_delete_rows", side_effect=fake_delete), \
             patch.object(synthetic_tx, "_run", side_effect=[like_ok_result, crdb_fail_result]):
            result = synthetic_tx.cleanup(trace_id, timeout=10)

        # CRDB 清理失败 → cleanup 失败(fail-closed)
        assert result.passed is False, (
            "R73 P0-04: CRDB 清理失败时 cleanup 应 passed=False(fail-closed)"
        )
        assert result.evidence.get("crdb_cleaned") is False


# R76 §10.B: TestR73E2EUpdateAdapter 已删除。
# 整改背景: R76 §10.B 要求 e2e_update_adapter.py 彻底重写为外部黑盒驱动器,
# 删除所有内部注入实现(build_test_update / verify_test_signature / _validate_role /
# dispatch_to_*_handler 等),改为通过公开 HTTP/状态接口驱动应用。
# 旧测试 TestR73E2EUpdateAdapter 测试的 build_test_update / verify_test_signature /
# _validate_role 均为已删除的内部 API,因此本测试类一并移除。
# 新的测试覆盖见 tests/integration/test_secretless_provider_transaction.py
# 和 tests/integration/test_provider_fault_contract.py。


class TestR85DataSignatureCompatibility:
    """R85 fix: 验证 _build_*_message 返回的 data 字段与业务方法签名兼容。

    整改背景: rc-v1.0.89 在 compose-runtime-e2e phase 4
    (real_product_transaction_before_backup) 失败,根因为
    _build_heartbeat_message / _build_dsp_dispatch_message 在 data 中
    放入了 trace_id 字段,但 write_bot_heartbeat / create_outbox_entry
    方法签名不接受此参数,导致 db_writer._execute_sqlite 的
    await method(**data) 抛 TypeError → 永久死信 → ACK,
    verify 永远查不到落库记录。

    本测试类用 inspect.signature 校验每个 _build_*_message 的 data
    字段与对应业务方法签名兼容,防止未来再次出现同类问题。
    """

    def _get_cache_store_method_signature(self, method_name: str):
        """加载 CacheStore 类并返回指定方法的参数集合。"""
        import inspect
        # 延迟导入,避免模块加载阶段触发 config/settings 初始化
        sys.modules.setdefault("telegram", MagicMock())
        sys.modules.setdefault("telegram.ext", MagicMock())
        from database.cache_store import CacheStore
        method = getattr(CacheStore, method_name, None)
        assert method is not None, f"CacheStore.{method_name} 不存在"
        sig = inspect.signature(method)
        return set(sig.parameters.keys())

    def test_heartbeat_data_compatible_with_write_bot_heartbeat(self, synthetic_tx):
        """_build_heartbeat_message 的 data 字段必须与 write_bot_heartbeat 签名兼容。"""
        trace_id = "synthetic_r85_heartbeat_sig"
        msg = synthetic_tx._build_heartbeat_message(trace_id)
        method_params = self._get_cache_store_method_signature("write_bot_heartbeat")
        # write_bot_heartbeat(self, name, total_processed, total_errors)
        # 移除 self
        method_params.discard("self")
        data_keys = set(msg["data"].keys())
        extra_keys = data_keys - method_params
        assert not extra_keys, (
            f"R85 fix: heartbeat data 含方法签名不接受的字段: {extra_keys}"
            f"(write_bot_heartbeat 接受: {method_params})"
        )
        # 验证 trace_id 不在 data 中(顶层才有)
        assert "trace_id" not in msg["data"], (
            "R85 fix: heartbeat data 不得包含 trace_id 字段"
        )
        assert msg.get("trace_id") == trace_id, (
            "R85 fix: heartbeat 顶层 trace_id 必须存在"
        )

    def test_dsp_dispatch_data_compatible_with_create_outbox_entry(self, synthetic_tx):
        """_build_dsp_dispatch_message 的 data 字段必须与 create_outbox_entry 签名兼容。"""
        trace_id = "synthetic_r85_dsp_sig"
        msg = synthetic_tx._build_dsp_dispatch_message(trace_id)
        method_params = self._get_cache_store_method_signature("create_outbox_entry")
        method_params.discard("self")
        data_keys = set(msg["data"].keys())
        extra_keys = data_keys - method_params
        assert not extra_keys, (
            f"R85 fix: dsp_dispatch data 含方法签名不接受的字段: {extra_keys}"
            f"(create_outbox_entry 接受: {method_params})"
        )
        assert "trace_id" not in msg["data"], (
            "R85 fix: dsp_dispatch data 不得包含 trace_id 字段"
        )
        assert msg.get("trace_id") == trace_id, (
            "R85 fix: dsp_dispatch 顶层 trace_id 必须存在"
        )

    def test_upload_session_data_compatible_with_create_upload_session(self, synthetic_tx):
        """_build_upload_session_message 的 data 字段必须与 create_upload_session 签名兼容。

        create_upload_session 已包含 trace_id 形参,所以 data 中可含 trace_id。
        """
        trace_id = "synthetic_r85_upload_sig"
        msg = synthetic_tx._build_upload_session_message(trace_id)
        method_params = self._get_cache_store_method_signature("create_upload_session")
        method_params.discard("self")
        data_keys = set(msg["data"].keys())
        extra_keys = data_keys - method_params
        assert not extra_keys, (
            f"R85 fix: upload_session data 含方法签名不接受的字段: {extra_keys}"
            f"(create_upload_session 接受: {method_params})"
        )

    def test_file_index_data_compatible_with_upsert_file_record_local(self, synthetic_tx):
        """_build_file_index_message 的 data 字段必须与 upsert_file_record_local 签名兼容。"""
        trace_id = "synthetic_r85_file_idx_sig"
        msg = synthetic_tx._build_file_index_message(trace_id)
        method_params = self._get_cache_store_method_signature("upsert_file_record_local")
        method_params.discard("self")
        data_keys = set(msg["data"].keys())
        extra_keys = data_keys - method_params
        assert not extra_keys, (
            f"R85 fix: file_index data 含方法签名不接受的字段: {extra_keys}"
            f"(upsert_file_record_local 接受: {method_params})"
        )

    def test_all_builders_data_compatible_with_method_signatures(self, synthetic_tx):
        """R85 防回归:所有 _build_*_message 的 data 字段必须与业务方法签名兼容。

        这是防止未来再次出现 TypeError 死信问题的统一防回归。
        """
        import inspect
        sys.modules.setdefault("telegram", MagicMock())
        sys.modules.setdefault("telegram.ext", MagicMock())
        from database.cache_store import CacheStore

        builders = [
            ("write_bot_heartbeat", synthetic_tx._build_heartbeat_message),
            ("create_upload_session", synthetic_tx._build_upload_session_message),
            ("upsert_file_record_local", synthetic_tx._build_file_index_message),
            ("create_outbox_entry", synthetic_tx._build_dsp_dispatch_message),
        ]
        trace_id = "synthetic_r85_all_compat"
        for method_name, builder in builders:
            msg = builder(trace_id)
            method = getattr(CacheStore, method_name, None)
            assert method is not None, f"CacheStore.{method_name} 不存在"
            sig = inspect.signature(method)
            method_params = set(sig.parameters.keys()) - {"self"}
            data_keys = set(msg["data"].keys())
            extra_keys = data_keys - method_params
            assert not extra_keys, (
                f"R85 fix: {method_name} 的 data 含不接受字段: {extra_keys}"
                f"(接受: {method_params})"
            )
