"""R72 RC59: synthetic_transaction 消息 message_id 跨步骤唯一性测试。

RC59 fix 根因:
    scripts/synthetic_transaction.py 中三个 _build_*_message 函数
    (_build_heartbeat_message / _build_upload_session_message /
     _build_file_index_message) 都使用 trace_id 作为 message_id,
    导致同一 trace_id 下的多步合成交易第二步起全部被 db_writer 的
    writer_inbox 幂等检查误判为重复消息并跳过。

    实际运行证据(rc-v1.0.59 compose-runtime-e2e):
      - 步骤 1 (bot_heartbeat): inject + verify PASS (row_count=1)
      - 步骤 2 (create_upload_session): inject PASS,verify FAIL
        (600s 内未在 upload_sessions 表查到记录)
      - 原因:db_writer 接收步骤 2 消息后,_execute_atomic 中的
        INSERT OR IGNORE INTO writer_inbox (message_id=trace_id, ...)
        rowcount=0(已被步骤 1 占用)→ raise _InboxConflict → 跳过业务写

RC59 fix:
    每个 _build_*_message 的 message_id 追加方法后缀:
      - heartbeat    → f"{trace_id}:heartbeat"
      - upload_session → f"{trace_id}:upload_session"
      - file_index   → f"{trace_id}:file_index"
    data 字段(name/upload_id/file_code)仍用 trace_id 作为业务主键,
    不影响 cleanup 与 verify 查询(WHERE 子句仍按业务主键匹配)。

测试覆盖矩阵:
    A. _build_heartbeat_message.message_id == f"{trace_id}:heartbeat"
    B. _build_upload_session_message.message_id == f"{trace_id}:upload_session"
    C. _build_file_index_message.message_id == f"{trace_id}:file_index"
    D. 同一 trace_id 下三个 message_id 互不相同(跨步骤唯一)
    E. data 字段仍用 trace_id 作为业务主键(name/upload_id/file_code)
    F. idempotency 场景:同步骤重新注入使用相同 message_id 仍被检测为重复
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

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
        "_test_synthetic_tx_rc59", SYNTHETIC_TRANSACTION_PATH
    )
    assert spec is not None and spec.loader is not None, (
        f"无法加载 synthetic_transaction.py: {SYNTHETIC_TRANSACTION_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestRC59MessageIdUniqueness:
    """R72 RC59: 验证 _build_*_message 的 message_id 跨步骤唯一。"""

    def test_A_build_heartbeat_message_id_has_heartbeat_suffix(
        self, synthetic_tx,
    ):
        """A: _build_heartbeat_message.message_id == f"{trace_id}:heartbeat"。"""
        trace_id = "synthetic_r71_rc59_test_001"
        msg = synthetic_tx._build_heartbeat_message(trace_id)
        assert msg["message_id"] == f"{trace_id}:heartbeat", (
            f"RC59: heartbeat message_id 应为 '{trace_id}:heartbeat', "
            f"实际: {msg['message_id']!r}"
        )

    def test_B_build_upload_session_message_id_has_upload_session_suffix(
        self, synthetic_tx,
    ):
        """B: _build_upload_session_message.message_id == f"{trace_id}:upload_session"。"""
        trace_id = "synthetic_r71_rc59_test_002"
        msg = synthetic_tx._build_upload_session_message(trace_id)
        assert msg["message_id"] == f"{trace_id}:upload_session", (
            f"RC59: upload_session message_id 应为 '{trace_id}:upload_session', "
            f"实际: {msg['message_id']!r}"
        )

    def test_C_build_file_index_message_id_has_file_index_suffix(
        self, synthetic_tx,
    ):
        """C: _build_file_index_message.message_id == f"{trace_id}:file_index"。"""
        trace_id = "synthetic_r71_rc59_test_003"
        msg = synthetic_tx._build_file_index_message(trace_id)
        assert msg["message_id"] == f"{trace_id}:file_index", (
            f"RC59: file_index message_id 应为 '{trace_id}:file_index', "
            f"实际: {msg['message_id']!r}"
        )

    def test_D_message_ids_unique_across_steps_with_same_trace_id(
        self, synthetic_tx,
    ):
        """D: 同一 trace_id 下三个 message_id 互不相同。

        这是 RC59 的核心断言 — RC59 修复前三个函数都返回 message_id=trace_id,
        导致 db_writer writer_inbox 在第二步起误判重复并跳过业务写。
        """
        trace_id = "synthetic_r71_rc59_unique_check"
        ids = {
            synthetic_tx._build_heartbeat_message(trace_id)["message_id"],
            synthetic_tx._build_upload_session_message(trace_id)["message_id"],
            synthetic_tx._build_file_index_message(trace_id)["message_id"],
        }
        assert len(ids) == 3, (
            f"RC59: 同一 trace_id 下三个 message_id 必须互不相同, "
            f"实际集合: {ids}(size={len(ids)})"
        )

    def test_E_data_field_still_uses_trace_id_as_business_pk(
        self, synthetic_tx,
    ):
        """E: data 字段仍用 trace_id 作为业务主键。

        RC59 fix 不影响业务主键,只改 message_id(幂等键)。
        verify 查询通过业务主键(WHERE name=? / upload_id=? / file_code=?)
        匹配,与 message_id 无关。
        """
        trace_id = "synthetic_r71_rc59_pk_check"

        # heartbeat: data.name = trace_id (bot_heartbeat.name 主键)
        hb = synthetic_tx._build_heartbeat_message(trace_id)
        assert hb["data"]["name"] == trace_id, (
            f"RC59: heartbeat.data.name 应仍为 trace_id, "
            f"实际: {hb['data']['name']!r}"
        )

        # upload_session: data.upload_id = trace_id (upload_sessions.upload_id 主键)
        us = synthetic_tx._build_upload_session_message(trace_id)
        assert us["data"]["upload_id"] == trace_id, (
            f"RC59: upload_session.data.upload_id 应仍为 trace_id, "
            f"实际: {us['data']['upload_id']!r}"
        )
        # upload_session: data.trace_id = trace_id (upload_sessions.trace_id 列)
        assert us["data"]["trace_id"] == trace_id, (
            f"RC59: upload_session.data.trace_id 应仍为 trace_id, "
            f"实际: {us['data']['trace_id']!r}"
        )

        # file_index: data.record.file_code = trace_id (file_records_local.file_code 主键)
        fi = synthetic_tx._build_file_index_message(trace_id)
        assert fi["data"]["record"]["file_code"] == trace_id, (
            f"RC59: file_index.data.record.file_code 应仍为 trace_id, "
            f"实际: {fi['data']['record']['file_code']!r}"
        )

    def test_F_idempotency_reinject_uses_same_message_id_within_step(
        self, synthetic_tx,
    ):
        """F: 同步骤重新注入(幂等性测试)使用相同 message_id。

        RC59 fix 后,_verify_idempotency_generic 调用 reinject_fn(trace_id)
        重新注入,reinject_fn 内部调用 _build_*_message(trace_id),
        生成的 message_id 与首次注入相同(同步骤同 suffix),writer_inbox
        仍能正确检测为重复并跳过,保持幂等性。
        """
        trace_id = "synthetic_r71_rc59_idempotency"

        # 模拟两次注入(首次 + 重新注入)
        first = synthetic_tx._build_heartbeat_message(trace_id)
        reinject = synthetic_tx._build_heartbeat_message(trace_id)

        assert first["message_id"] == reinject["message_id"], (
            "RC59: 同步骤重新注入必须使用相同 message_id 以触发 writer_inbox 幂等检查"
        )

        # 同样验证 upload_session 和 file_index
        us_first = synthetic_tx._build_upload_session_message(trace_id)
        us_reinject = synthetic_tx._build_upload_session_message(trace_id)
        assert us_first["message_id"] == us_reinject["message_id"], (
            "RC59: upload_session 重新注入必须使用相同 message_id"
        )

        fi_first = synthetic_tx._build_file_index_message(trace_id)
        fi_reinject = synthetic_tx._build_file_index_message(trace_id)
        assert fi_first["message_id"] == fi_reinject["message_id"], (
            "RC59: file_index 重新注入必须使用相同 message_id"
        )


class TestRC59SourceCodePattern:
    """R72 RC59: 验证源码中不再出现 message_id=trace_id 的反模式。"""

    def test_source_no_longer_uses_bare_trace_id_as_message_id(self):
        """源码中不应再出现 "message_id": trace_id 这样的裸赋值。

        RC59 修复前所有 _build_*_message 都使用 "message_id": trace_id,
        修复后改为 f"{trace_id}:<suffix>"。
        """
        source = SYNTHETIC_TRANSACTION_PATH.read_text(encoding="utf-8")
        # 查找所有 _build_*_message 函数体内的 message_id 赋值
        # 模式: "message_id": trace_id (裸赋值,无 f-string suffix)
        # 排除注释行
        offending_lines = []
        for i, line in enumerate(source.splitlines(), 1):
            stripped = line.strip()
            # 跳过注释
            if stripped.startswith("#"):
                continue
            # 检查 "message_id": trace_id 模式(无 f-string,无 suffix)
            # 正则: "message_id"\s*:\s*trace_id\s*,?\s*$
            import re
            if re.search(r'"message_id"\s*:\s*trace_id\s*,?\s*$', stripped):
                offending_lines.append((i, stripped))

        assert not offending_lines, (
            f"RC59: 源码中不应再出现 \"message_id\": trace_id 裸赋值, "
            f"发现 {len(offending_lines)} 处: {offending_lines}"
        )

    def test_source_uses_fstring_message_id_with_suffix(self):
        """源码中 _build_*_message 应使用 f-string 追加方法后缀。"""
        source = SYNTHETIC_TRANSACTION_PATH.read_text(encoding="utf-8")
        # 必须出现三次 f"{trace_id}:<suffix>" 模式
        # R73 P0-04: 新增 dsp_dispatch 后缀(原 R72 RC59 三个 → R73 四个)
        import re
        matches = re.findall(r'"message_id"\s*:\s*f"\{trace_id\}:(\w+)"', source)
        suffixes = set(matches)
        expected = {"heartbeat", "upload_session", "file_index", "dsp_dispatch"}
        assert suffixes == expected, (
            f"RC59/R73: message_id 后缀集合应为 {expected}, "
            f"实际: {suffixes}"
        )
