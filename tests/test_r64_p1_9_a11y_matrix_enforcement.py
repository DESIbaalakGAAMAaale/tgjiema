"""R64 P1-09: a11y 强制矩阵门禁单测 — expected==executed,无 skip/xpass,矩阵完整覆盖。

审计 P1-09 原文核心:
    "a11y success 不能只看单点通过,必须覆盖完整矩阵:
     expected_test_count == executed_test_count;
     任何 skip/xpass 视为失败;
     矩阵覆盖 zh-CN/en-US × keyboard/screen_reader × 16 个状态 = 64 用例。"

本测试覆盖:
    A. expected_count == executed_count(check_expected_count_equals_executed)
    B. skip / xpass 视为失败(任一非 0 即 ok=False)
    C. 矩阵覆盖 zh-CN / en-US(2 locales)
    D. 矩阵覆盖 keyboard / screen_reader(2 input_modes)
    E. 矩阵覆盖 16 个状态(error/loading/empty/paginated/modal/dynamic_button/
       permission_denied/approval_*/mfa_*)
    F. 缺失任何维度 → fail-closed
    G. check_a11y_matrix_enforcement.py 脚本入口正确 exit code
    H. generated_a11y_matrix_cases.json 含 64 条用例 + expected_count=64
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 注入 scripts/ 目录到 sys.path,便于 import 检查脚本
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_a11y_precheck as precheck  # type: ignore  # noqa: E402
import check_a11y_matrix_enforcement as enforcement  # type: ignore  # noqa: E402
from check_a11y_precheck import (  # type: ignore  # noqa: E402
    CheckResult,
    EXPECTED_INPUT_MODES,
    EXPECTED_LOCALES,
    EXPECTED_MATRIX_CASE_COUNT,
    EXPECTED_STATES,
    EXPECTED_STATES_FULL_COUNT,
    MATRIX_CASES_JSON,
    check_expected_count_equals_executed,
    check_matrix_completeness,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ════════════════════════════════════════════════════════════════
# 测试常量
# ════════════════════════════════════════════════════════════════

# 16 个预期状态(与 generate_a11y_matrix_cases.py 中 STATES 一致)
EXPECTED_16_STATES = (
    "error", "loading", "empty", "paginated", "modal", "dynamic_button",
    "permission_denied",
    "approval_required", "approval_pending", "approval_approved",
    "approval_rejected", "approval_expired",
    "mfa_required", "mfa_pending", "mfa_verified", "mfa_expired",
)


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _make_matrix_payload(
    *,
    expected_count: int = 64,
    locales: tuple = EXPECTED_LOCALES,
    input_modes: tuple = EXPECTED_INPUT_MODES,
    states: tuple = EXPECTED_16_STATES,
    cases_override: list[dict] | None = None,
) -> dict:
    """构建矩阵 payload(用于写入临时文件测试)。

    默认生成完整的 2×2×16=64 用例;可通过参数注入不完整维度测试 fail-closed。
    """
    if cases_override is not None:
        cases = cases_override
    else:
        cases = []
        for state in states:
            for locale in locales:
                for im in input_modes:
                    cases.append({
                        "locale": locale,
                        "input_mode": im,
                        "state": state,
                        "path": "/readiness",
                        "a11y_testable": True,
                    })
    return {
        "expected_count": expected_count,
        "locales": list(locales),
        "input_modes": list(input_modes),
        "states": list(states),
        "cases": cases,
    }


def _write_matrix_file(tmp_path: Path, payload: dict) -> Path:
    """将 payload 写入临时 JSON 文件并返回路径。"""
    f = tmp_path / "matrix.json"
    f.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return f


# ════════════════════════════════════════════════════════════════
# A. expected_count == executed_count
# ════════════════════════════════════════════════════════════════


class TestExpectedEqualsExecuted:
    """A 节: expected_count 必须等于 executed_count。"""

    def test_executed_equals_expected_passes(self):
        """A1: executed==64 且 skip=0 xpass=0 → ok=True。"""
        cr = check_expected_count_equals_executed(
            executed_count=EXPECTED_MATRIX_CASE_COUNT,
            skip_count=0,
            xpass_count=0,
        )
        assert cr.ok is True
        assert cr.details["expected_count"] == EXPECTED_MATRIX_CASE_COUNT
        assert cr.details["executed_count"] == EXPECTED_MATRIX_CASE_COUNT

    def test_executed_less_than_expected_fails(self):
        """A2: executed=32 < expected=64 → ok=False(假绿:矩阵未跑完)。"""
        cr = check_expected_count_equals_executed(
            executed_count=32,
            skip_count=0,
            xpass_count=0,
        )
        assert cr.ok is False
        assert "expected_count" in cr.message.lower() or "executed_count" in cr.message.lower()

    def test_executed_more_than_expected_fails(self):
        """A3: executed=128 > expected=64 → ok=False(用例膨胀,可能重复计数)。"""
        cr = check_expected_count_equals_executed(
            executed_count=128,
            skip_count=0,
            xpass_count=0,
        )
        assert cr.ok is False

    def test_executed_zero_fails(self):
        """A4: executed=0 → ok=False(stub replacement 导致 0 用例 = 假绿)。"""
        cr = check_expected_count_equals_executed(
            executed_count=0,
            skip_count=0,
            xpass_count=0,
        )
        assert cr.ok is False

    def test_expected_count_constant_is_64(self):
        """A5: EXPECTED_MATRIX_CASE_COUNT == 64(2×2×16)。"""
        assert EXPECTED_MATRIX_CASE_COUNT == 64
        assert EXPECTED_STATES_FULL_COUNT == 16
        assert len(EXPECTED_LOCALES) == 2
        assert len(EXPECTED_INPUT_MODES) == 2


# ════════════════════════════════════════════════════════════════
# B. skip / xpass 视为失败
# ════════════════════════════════════════════════════════════════


class TestSkipXpassAsFailure:
    """B 节: 任何 skip / xpass 视为失败(不允许跳过矩阵用例)。"""

    def test_skip_one_fails(self):
        """B1: executed=64 但 skip=1 → ok=False(跳过即失败)。"""
        cr = check_expected_count_equals_executed(
            executed_count=64,
            skip_count=1,
            xpass_count=0,
        )
        assert cr.ok is False
        assert "skip" in cr.message.lower()

    def test_skip_many_fails(self):
        """B2: skip=10 → ok=False。"""
        cr = check_expected_count_equals_executed(
            executed_count=64,
            skip_count=10,
            xpass_count=0,
        )
        assert cr.ok is False

    def test_xpass_one_fails(self):
        """B3: xpass=1 → ok=False(xfail 标记残留视为失败)。"""
        cr = check_expected_count_equals_executed(
            executed_count=64,
            skip_count=0,
            xpass_count=1,
        )
        assert cr.ok is False
        assert "xpass" in cr.message.lower()

    def test_xpass_many_fails(self):
        """B4: xpass=5 → ok=False。"""
        cr = check_expected_count_equals_executed(
            executed_count=64,
            skip_count=0,
            xpass_count=5,
        )
        assert cr.ok is False

    def test_skip_and_xpass_both_fail(self):
        """B5: 同时 skip=2 + xpass=3 → ok=False(双重失败)。"""
        cr = check_expected_count_equals_executed(
            executed_count=64,
            skip_count=2,
            xpass_count=3,
        )
        assert cr.ok is False


# ════════════════════════════════════════════════════════════════
# C. 矩阵覆盖 zh-CN / en-US
# ════════════════════════════════════════════════════════════════


class TestMatrixLocaleCoverage:
    """C 节: 矩阵必须覆盖 zh-CN 与 en-US。"""

    def test_expected_locales_contains_zh_cn(self):
        """C1: EXPECTED_LOCALES 包含 zh-CN。"""
        assert "zh-CN" in EXPECTED_LOCALES

    def test_expected_locales_contains_en_us(self):
        """C2: EXPECTED_LOCALES 包含 en-US。"""
        assert "en-US" in EXPECTED_LOCALES

    def test_matrix_file_covers_both_locales(self):
        """C3: generated_a11y_matrix_cases.json 覆盖 zh-CN + en-US。"""
        assert MATRIX_CASES_JSON.exists(), f"矩阵文件不存在: {MATRIX_CASES_JSON}"
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        seen = {c.get("locale") for c in cases if isinstance(c, dict)}
        assert "zh-CN" in seen, "矩阵缺少 zh-CN 用例"
        assert "en-US" in seen, "矩阵缺少 en-US 用例"

    def test_missing_locale_fails_closed(self, tmp_path):
        """C4: 矩阵缺少 en-US → check_matrix_completeness fail-closed。"""
        # 构造 64 条全 zh-CN 用例(每个 state×input_mode 组合重复 2 次凑满 64),
        # 使 count 检查通过(64==64),从而抵达维度检查(missing_locales)。
        cases = []
        for state in EXPECTED_16_STATES:
            for im in EXPECTED_INPUT_MODES:
                for _ in range(2):  # 重复 2 次凑满 64 条
                    cases.append({
                        "locale": "zh-CN",
                        "input_mode": im,
                        "state": state,
                        "path": "/x",
                    })
        payload = {
            "expected_count": 64,
            "locales": ["zh-CN"],  # 缺少 en-US
            "input_modes": list(EXPECTED_INPUT_MODES),
            "states": list(EXPECTED_16_STATES),
            "cases": cases,
        }
        f = _write_matrix_file(tmp_path, payload)
        cr = check_matrix_completeness(cases_json=f)
        assert cr.ok is False
        assert "en-US" in cr.details.get("missing_locales", [])


# ════════════════════════════════════════════════════════════════
# D. 矩阵覆盖 keyboard / screen_reader
# ════════════════════════════════════════════════════════════════


class TestMatrixInputModeCoverage:
    """D 节: 矩阵必须覆盖 keyboard 与 screen_reader。"""

    def test_expected_input_modes_contains_keyboard(self):
        """D1: EXPECTED_INPUT_MODES 包含 keyboard。"""
        assert "keyboard" in EXPECTED_INPUT_MODES

    def test_expected_input_modes_contains_screen_reader(self):
        """D2: EXPECTED_INPUT_MODES 包含 screen_reader。"""
        assert "screen_reader" in EXPECTED_INPUT_MODES

    def test_matrix_file_covers_both_input_modes(self):
        """D3: generated_a11y_matrix_cases.json 覆盖 keyboard + screen_reader。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        seen = {c.get("input_mode") for c in cases if isinstance(c, dict)}
        assert "keyboard" in seen, "矩阵缺少 keyboard 用例"
        assert "screen_reader" in seen, "矩阵缺少 screen_reader 用例"

    def test_missing_input_mode_fails_closed(self, tmp_path):
        """D4: 矩阵缺少 screen_reader → fail-closed。"""
        # 构造 64 条全 keyboard 用例(每个 state×locale 组合重复 2 次凑满 64),
        # 使 count 检查通过(64==64),从而抵达维度检查(missing_input_modes)。
        cases = []
        for state in EXPECTED_16_STATES:
            for locale in EXPECTED_LOCALES:
                for _ in range(2):  # 重复 2 次凑满 64 条
                    cases.append({
                        "locale": locale,
                        "input_mode": "keyboard",  # 缺少 screen_reader
                        "state": state,
                        "path": "/x",
                    })
        payload = {
            "expected_count": 64,
            "locales": list(EXPECTED_LOCALES),
            "input_modes": ["keyboard"],
            "states": list(EXPECTED_16_STATES),
            "cases": cases,
        }
        f = _write_matrix_file(tmp_path, payload)
        cr = check_matrix_completeness(cases_json=f)
        assert cr.ok is False
        assert "screen_reader" in cr.details.get("missing_input_modes", [])


# ════════════════════════════════════════════════════════════════
# E. 矩阵覆盖 16 个状态
# ════════════════════════════════════════════════════════════════


class TestMatrixStateCoverage:
    """E 节: 矩阵必须覆盖 16 个状态(含 error/loading/empty/approval/mfa 全状态)。"""

    def test_expected_states_count_is_16(self):
        """E1: EXPECTED_STATES 含 16 个状态。"""
        assert len(EXPECTED_STATES) >= 16, (
            f"EXPECTED_STATES 应至少含 16 个状态,实际 {len(EXPECTED_STATES)}"
        )

    @pytest.mark.parametrize("state", EXPECTED_16_STATES)
    def test_matrix_file_covers_state(self, state):
        """E2: 矩阵文件覆盖每个预期状态(参数化 16 个)。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        seen_states = {c.get("state") for c in cases if isinstance(c, dict)}
        assert state in seen_states, f"矩阵缺少 state={state}"

    def test_matrix_covers_error_state(self):
        """E3: 矩阵覆盖 error 状态(错误恢复流程)。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        assert any(c.get("state") == "error" for c in cases)

    def test_matrix_covers_all_approval_states(self):
        """E4: 矩阵覆盖 5 个 approval_* 状态(required/pending/approved/rejected/expired)。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        seen = {c.get("state") for c in cases if isinstance(c, dict)}
        for s in (
            "approval_required", "approval_pending", "approval_approved",
            "approval_rejected", "approval_expired",
        ):
            assert s in seen, f"矩阵缺少 approval 状态: {s}"

    def test_matrix_covers_all_mfa_states(self):
        """E5: 矩阵覆盖 4 个 mfa_* 状态(required/pending/verified/expired)。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        seen = {c.get("state") for c in cases if isinstance(c, dict)}
        for s in ("mfa_required", "mfa_pending", "mfa_verified", "mfa_expired"):
            assert s in seen, f"矩阵缺少 mfa 状态: {s}"

    def test_missing_state_fails_closed(self, tmp_path):
        """E6: 矩阵缺少 mfa_expired → fail-closed。"""
        # 构造 15 个状态(去掉 mfa_expired)
        reduced_states = tuple(s for s in EXPECTED_16_STATES if s != "mfa_expired")
        cases = []
        for state in reduced_states:
            for locale in EXPECTED_LOCALES:
                for im in EXPECTED_INPUT_MODES:
                    cases.append({
                        "locale": locale, "input_mode": im,
                        "state": state, "path": "/x",
                    })
        payload = {
            "expected_count": 64,  # 声称 64 但实际只有 60 条
            "locales": list(EXPECTED_LOCALES),
            "input_modes": list(EXPECTED_INPUT_MODES),
            "states": list(reduced_states),
            "cases": cases,
        }
        f = _write_matrix_file(tmp_path, payload)
        cr = check_matrix_completeness(cases_json=f)
        assert cr.ok is False


# ════════════════════════════════════════════════════════════════
# F. 缺失维度 fail-closed
# ════════════════════════════════════════════════════════════════


class TestMissingDimensionFailClosed:
    """F 节: 缺失任何维度(文件/expected_count/用例数)→ fail-closed。"""

    def test_missing_matrix_file_fails(self, tmp_path):
        """F1: 矩阵文件不存在 → fail-closed。"""
        nonexistent = tmp_path / "nonexistent.json"
        cr = check_matrix_completeness(cases_json=nonexistent)
        assert cr.ok is False

    def test_missing_expected_count_fails(self, tmp_path):
        """F2: 矩阵文件无 expected_count 字段 → fail-closed。"""
        payload = {
            "locales": list(EXPECTED_LOCALES),
            "input_modes": list(EXPECTED_INPUT_MODES),
            "states": list(EXPECTED_16_STATES),
            "cases": _make_matrix_payload()["cases"],
            # 无 expected_count
        }
        f = _write_matrix_file(tmp_path, payload)
        cr = check_matrix_completeness(cases_json=f)
        assert cr.ok is False

    def test_expected_count_zero_fails(self, tmp_path):
        """F3: expected_count=0 → fail-closed。"""
        payload = _make_matrix_payload(expected_count=0)
        f = _write_matrix_file(tmp_path, payload)
        cr = check_matrix_completeness(cases_json=f)
        assert cr.ok is False

    def test_expected_count_mismatch_fails(self, tmp_path):
        """F4: expected_count=32 与实际用例数 64 不一致 → fail-closed。"""
        payload = _make_matrix_payload(expected_count=32)
        f = _write_matrix_file(tmp_path, payload)
        cr = check_matrix_completeness(cases_json=f)
        assert cr.ok is False

    def test_actual_count_less_than_expected_fails(self, tmp_path):
        """F5: 实际用例数 60 < expected_count 64 → fail-closed。"""
        # 完整 cases 但 expected_count 设为 64 而实际只有 60
        cases = []
        for state in EXPECTED_16_STATES[:15]:  # 只取 15 个状态 = 60 条
            for locale in EXPECTED_LOCALES:
                for im in EXPECTED_INPUT_MODES:
                    cases.append({
                        "locale": locale, "input_mode": im,
                        "state": state, "path": "/x",
                    })
        payload = {
            "expected_count": 64,
            "locales": list(EXPECTED_LOCALES),
            "input_modes": list(EXPECTED_INPUT_MODES),
            "states": list(EXPECTED_16_STATES),
            "cases": cases,
        }
        f = _write_matrix_file(tmp_path, payload)
        cr = check_matrix_completeness(cases_json=f)
        assert cr.ok is False

    def test_malformed_json_fails(self, tmp_path):
        """F6: JSON 格式错误 → fail-closed。"""
        f = tmp_path / "bad.json"
        f.write_text("{not valid json", encoding="utf-8")
        cr = check_matrix_completeness(cases_json=f)
        assert cr.ok is False


# ════════════════════════════════════════════════════════════════
# G. check_a11y_matrix_enforcement.py 脚本入口
# ════════════════════════════════════════════════════════════════


class TestEnforcementScript:
    """G 节: check_a11y_matrix_enforcement.py 脚本入口正确 exit code。"""

    def test_check_returns_exit_zero_on_valid_matrix(self):
        """G1: 完整矩阵 + 无测试报告 → check() 返回 exit_code=0。"""
        exit_code, results = enforcement.check()
        assert exit_code == 0
        assert len(results) >= 2
        # 检查 1(矩阵完整性)必须通过
        assert results[0].ok is True

    def test_check_returns_exit_one_on_missing_matrix_file(self, tmp_path):
        """G2: 矩阵文件不存在 → check() 返回 exit_code=1。"""
        nonexistent = tmp_path / "nonexistent.json"
        exit_code, results = enforcement.check(matrix_path=nonexistent)
        assert exit_code == 1
        assert results[0].ok is False

    def test_enforce_matrix_completeness_passes(self):
        """G3: enforce_matrix_completeness() 对真实矩阵文件返回 ok=True。"""
        result = enforcement.enforce_matrix_completeness()
        assert result.ok is True

    def test_enforce_execution_parity_no_report_passes(self):
        """G4: 无测试报告时 enforce_execution_parity() 返回 ok=True(跳过检查)。"""
        result = enforcement.enforce_execution_parity(test_report=None)
        assert result.ok is True

    def test_enforce_execution_parity_missing_report_fails(self, tmp_path):
        """G5: 测试报告不存在 → enforce_execution_parity() 返回 ok=False。"""
        nonexistent = tmp_path / "no_report.json"
        result = enforcement.enforce_execution_parity(test_report=nonexistent)
        assert result.ok is False

    def test_enforce_execution_parity_with_valid_report(self, tmp_path):
        """G6: 有效报告(executed=64, skip=0)→ ok=True。"""
        report = tmp_path / "report.json"
        report_data = {
            "stats": {"expected": 64, "skipped": 0},
            "suites": [
                {
                    "specs": [
                        {
                            "tests": [
                                {"results": [{"status": "passed"}]}
                                for _ in range(64)
                            ]
                        }
                    ]
                }
            ],
        }
        report.write_text(json.dumps(report_data), encoding="utf-8")
        result = enforcement.enforce_execution_parity(test_report=report)
        assert result.ok is True
        # details 同时含 executed_count(初始 0)与 executed(stats 更新值)
        assert result.details["executed"] == 64

    def test_enforce_execution_parity_with_skip_fails(self, tmp_path):
        """G7: 报告含 skip=2 → ok=False。"""
        report = tmp_path / "report.json"
        # 62 passed + 2 skipped
        results_list = [{"status": "passed"}] * 62 + [{"status": "skipped"}] * 2
        report_data = {
            "stats": {"expected": 64, "skipped": 2},
            "suites": [{"specs": [{"tests": [{"results": [r]} for r in results_list]}]}],
        }
        report.write_text(json.dumps(report_data), encoding="utf-8")
        result = enforcement.enforce_execution_parity(test_report=report)
        assert result.ok is False
        # details 中 skip 字段(stats 更新)应 >= 2
        assert result.details["skip"] >= 2

    def test_enforce_execution_parity_with_few_executed_fails(self, tmp_path):
        """G8: 报告 executed=32 < expected=64 → ok=False。"""
        report = tmp_path / "report.json"
        report_data = {
            "stats": {"expected": 64, "skipped": 0},
            "suites": [
                {"specs": [{"tests": [{"results": [{"status": "passed"}]}]}]}
                for _ in range(32)
            ],
        }
        report.write_text(json.dumps(report_data), encoding="utf-8")
        result = enforcement.enforce_execution_parity(test_report=report)
        assert result.ok is False


# ════════════════════════════════════════════════════════════════
# H. generated_a11y_matrix_cases.json 完整性
# ════════════════════════════════════════════════════════════════


class TestMatrixFileIntegrity:
    """H 节: generated_a11y_matrix_cases.json 含 64 条用例 + expected_count=64。"""

    def test_matrix_file_exists(self):
        """H1: 矩阵文件存在。"""
        assert MATRIX_CASES_JSON.exists(), f"矩阵文件不存在: {MATRIX_CASES_JSON}"

    def test_matrix_file_has_expected_count_64(self):
        """H2: 矩阵文件 expected_count == 64。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        assert data.get("expected_count") == 64, (
            f"expected_count 应为 64,实际 {data.get('expected_count')}"
        )

    def test_matrix_file_has_64_cases(self):
        """H3: 矩阵文件 cases 数组含 64 条用例。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        assert len(cases) == 64, f"cases 应为 64 条,实际 {len(cases)}"

    def test_matrix_file_declares_locales(self):
        """H4: 矩阵文件声明 locales=[zh-CN, en-US]。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        assert set(data.get("locales", [])) == {"zh-CN", "en-US"}

    def test_matrix_file_declares_input_modes(self):
        """H5: 矩阵文件声明 input_modes=[keyboard, screen_reader]。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        assert set(data.get("input_modes", [])) == {"keyboard", "screen_reader"}

    def test_matrix_file_declares_16_states(self):
        """H6: 矩阵文件声明 16 个 states。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        states = data.get("states", [])
        assert len(states) == 16, f"states 应为 16 个,实际 {len(states)}"

    def test_matrix_file_cases_all_testable(self):
        """H7: 矩阵文件中每条用例 a11y_testable=True。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        for i, c in enumerate(cases):
            assert c.get("a11y_testable") is True, (
                f"case[{i}] a11y_testable 应为 True"
            )

    def test_matrix_file_passes_completeness_check(self):
        """H8: 真实矩阵文件通过 check_matrix_completeness() 检查。"""
        cr = check_matrix_completeness()
        assert cr.ok is True, f"矩阵完整性检查失败: {cr.message}"

    def test_matrix_dimensions_multiply_to_64(self):
        """H9: 2 locales × 2 input_modes × 16 states == 64(矩阵规模正确)。"""
        assert len(EXPECTED_LOCALES) * len(EXPECTED_INPUT_MODES) * 16 == 64

    def test_each_case_has_required_fields(self):
        """H10: 每条用例含 locale/input_mode/state/path 字段。"""
        data = json.loads(MATRIX_CASES_JSON.read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        required = ("locale", "input_mode", "state", "path")
        for i, c in enumerate(cases):
            for field in required:
                assert c.get(field), f"case[{i}] 缺少或为空字段: {field}"


# ════════════════════════════════════════════════════════════════
# I. enforce_matrix opt-in 向后兼容
# ════════════════════════════════════════════════════════════════


class TestEnforceMatrixOptIn:
    """I 节: enforce_matrix 参数默认 False(向后兼容 R62 regression)。"""

    def test_run_all_checks_without_enforce_matrix_skips_matrix_check(self):
        """I1: 默认 enforce_matrix=False → run_all_checks 不含矩阵检查结果。"""
        passed, failed = precheck.run_all_checks(
            skip_node_require=True,
            skip_playwright=True,
        )
        # 不强制矩阵时不应有矩阵完整性失败(矩阵检查未运行)
        matrix_fails = [
            r for r in failed
            if "矩阵" in r.message or "matrix" in r.message.lower()
        ]
        # 矩阵检查未运行,所以不应有矩阵相关失败
        assert len(matrix_fails) == 0

    def test_run_all_checks_with_enforce_matrix_runs_matrix_check(self):
        """I2: enforce_matrix=True → run_all_checks 含矩阵检查结果(通过)。"""
        passed, failed = precheck.run_all_checks(
            skip_node_require=True,
            skip_playwright=True,
            enforce_matrix=True,
        )
        # 矩阵检查应通过(真实矩阵文件完整)
        matrix_results = [
            r for r in (passed + failed)
            if "矩阵" in r.message or "P1-09" in r.message
        ]
        assert len(matrix_results) >= 1, "enforce_matrix=True 时应运行矩阵检查"
        # 矩阵检查应通过
        matrix_passes = [r for r in passed if "P1-09" in r.message]
        assert len(matrix_passes) >= 1, "矩阵完整性检查应通过"
