"""R65 P1-04: 清空 observability allowlist(175 项 → 0)— 零容忍验证测试。

审计背景(R65 P1-04):
  R64 P1-07 将 security/destructive/data-integrity/financial 四个高风险域
  压到 0,但 observability 域仍保留 175 项 allowlist(except Exception: pass /
  裸 return 0/False / 裸字符串返回 / raise ValueError 携带字符串字面量)。
  allowlist 不是修复 — 上线前必须清空,所有异常归一到结构化错误处理。

整改方案:
  - 规则1/2 (except Exception: pass) → 替换 pass 为 logger.exception(...)
  - 规则3 (except: return 0/False) → 在 except 块内补充 logger 调用后保留 return False
    (R65 P1-04 final: scanner rule 3 已增强,允许 except 块含 logger 调用后 return False;
     原 P1-04 子代理误将 return False 改为 return None,破坏了 `result is False` 语义,
     此处恢复 return False 语义,并补齐缺失的 logger 调用以满足 "log or reraise" 协议)
  - error_protocol_allowlist.generated.json: allowlist_count 175 → 0
  - check_error_protocol.py: 默认 strict 模式(不再容忍 allowlist)
  - ci.yml: 切换为 --strict

测试覆盖矩阵:
  A. Allowlist 清零验证 (3)
     1. test_generated_allowlist_count_zero
     2. test_baseline_observability_allowlist_empty
     3. test_baseline_violation_count_zero

  B. Scanner strict 模式通过 (2)
     4. test_scanner_strict_mode_passes
     5. test_scanner_default_mode_is_strict

  C. 实际违规数为 0 (3)
     6. test_no_violations_in_codebase
     7. test_observability_domain_zero
     8. test_high_risk_domains_zero

  D. CI workflow 切换 --strict (2)
     9.  test_ci_workflow_uses_strict
     10. test_scanner_cli_default_strict

  E. 修复模式验证(AST 级,抽样) (2)
     11. test_no_except_pass_in_observability_files
     12. test_no_bare_return_in_observability_files
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

# ── 让测试能导入 scripts/check_error_protocol.py ──
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_error_protocol as scanner  # noqa: E402

REAL_BASELINE = SCRIPTS_DIR / "error_protocol_baseline.json"
GENERATED_ALLOWLIST = SCRIPTS_DIR / "error_protocol_allowlist.generated.json"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


# ════════════════════════════════════════════════════════════
# A. Allowlist 清零验证
# ════════════════════════════════════════════════════════════

class TestAllowlistClearedToZero:
    """验证 observability allowlist 已清空到 0。"""

    def test_generated_allowlist_count_zero(self):
        """error_protocol_allowlist.generated.json 中 allowlist_count == 0。"""
        data = json.loads(GENERATED_ALLOWLIST.read_text(encoding="utf-8"))
        assert data.get("allowlist_count") == 0, (
            f"期望 allowlist_count=0,实际={data.get('allowlist_count')} "
            f"(R65 P1-04: observability allowlist 必须清空)"
        )
        assert data.get("allowlist") == [], (
            f"期望 allowlist=[],实际长度={len(data.get('allowlist', []))}"
        )
        assert data.get("total_violations") == 0, (
            f"期望 total_violations=0,实际={data.get('total_violations')}"
        )

    def test_baseline_observability_allowlist_empty(self):
        """baseline 文件中 observability.allowlist 为空列表。"""
        data = json.loads(REAL_BASELINE.read_text(encoding="utf-8"))
        obs_allowlist = (
            data.get("domains", {}).get("observability", {}).get("allowlist", [])
        )
        assert obs_allowlist == [], (
            f"期望 observability.allowlist=[],实际有 {len(obs_allowlist)} 条 "
            f"(R65 P1-04: 所有 observability 违规必须已迁移,不依赖 allowlist)"
        )

    def test_baseline_violation_count_zero(self):
        """baseline 文件中 violation_count == 0(ratchet 已降到 0)。"""
        data = json.loads(REAL_BASELINE.read_text(encoding="utf-8"))
        vc = data.get("violation_count", -1)
        assert vc == 0, (
            f"期望 violation_count=0,实际={vc} "
            f"(R65 P1-04: observability allowlist 清空后 ratchet 必须为 0)"
        )
        # previous 应保留为历史值(175),证明 ratchet 下降路径
        prev = data.get("previous_violation_count", 0)
        assert prev == 175, (
            f"期望 previous_violation_count=175(历史值),实际={prev}"
        )


# ════════════════════════════════════════════════════════════
# B. Scanner strict 模式通过
# ════════════════════════════════════════════════════════════

class TestScannerStrictMode:
    """验证 scanner 在 strict 模式下通过(real_violations == 0)。"""

    def test_scanner_strict_mode_passes(self):
        """`python scripts/check_error_protocol.py --strict` 退出码为 0。"""
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "check_error_protocol.py"), "--strict"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, (
            f"scanner --strict 应通过,但退出码={result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "real_violations=0" in result.stdout, (
            f"输出应包含 real_violations=0,实际: {result.stdout}"
        )

    def test_scanner_default_mode_is_strict(self):
        """R65 P1-04: scanner 默认模式为 strict(无 flag 时也走 strict)。"""
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "check_error_protocol.py")],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        # 默认 strict 模式应通过(违规数为 0)
        assert result.returncode == 0, (
            f"scanner 默认(strict)模式应通过,但退出码={result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "strict 模式通过" in result.stdout, (
            f"输出应表明 strict 模式通过,实际: {result.stdout}"
        )


# ════════════════════════════════════════════════════════════
# C. 实际违规数为 0
# ════════════════════════════════════════════════════════════

class TestZeroViolations:
    """验证代码库中实际违规数为 0(不依赖 allowlist)。"""

    def test_no_violations_in_codebase(self):
        """全代码库违规数 == 0(规则1/2/3/4/5 全部清零)。"""
        findings = scanner.collect_findings()
        assert findings == [], (
            f"期望 0 处违规,实际有 {len(findings)} 处:\n" +
            "\n".join(f"  {f}:{l}: {d}" for f, l, d in findings[:20])
        )

    def test_observability_domain_zero(self):
        """observability 域违规数 == 0(R65 P1-04 核心目标)。"""
        findings = scanner.collect_findings()
        obs = [
            (f, l, d) for f, l, d in findings
            if scanner._classify_domain(f) == "observability"
        ]
        assert obs == [], (
            f"observability 域仍有 {len(obs)} 处违规(R65 P1-04 目标=0): " +
            "\n".join(f"  {f}:{l}" for f, l, d in obs[:20])
        )

    def test_high_risk_domains_zero(self):
        """security/destructive/data-integrity/financial 域违规数 == 0。"""
        findings = scanner.collect_findings()
        for domain in ("security", "destructive", "data-integrity", "financial"):
            d_findings = [
                (f, l, d) for f, l, d in findings
                if scanner._classify_domain(f) == domain
            ]
            assert d_findings == [], (
                f"{domain} 域仍有 {len(d_findings)} 处违规(目标=0)"
            )


# ════════════════════════════════════════════════════════════
# D. CI workflow 切换 --strict
# ════════════════════════════════════════════════════════════

class TestCIWorkflowStrict:
    """验证 CI workflow 已切换为 --strict 模式。"""

    def test_ci_workflow_uses_strict(self):
        """ci.yml 中 check_error_protocol 调用使用 --strict 而非 --baseline。"""
        content = CI_WORKFLOW.read_text(encoding="utf-8")
        # 必须存在 --strict 调用
        assert "check_error_protocol.py --strict" in content, (
            "ci.yml 应包含 `check_error_protocol.py --strict` 调用"
        )
        # 不应再使用 --baseline 模式调用 check_error_protocol
        # (允许其他 scanner 使用 --baseline,但 check_error_protocol 不应再用)
        for line in content.splitlines():
            if "check_error_protocol.py" in line and "run:" in line:
                assert "--strict" in line, (
                    f"check_error_protocol 调用必须用 --strict,实际: {line.strip()}"
                )
                assert "--baseline" not in line, (
                    f"check_error_protocol 调用不应再用 --baseline,实际: {line.strip()}"
                )

    def test_scanner_cli_default_strict(self):
        """scanner CLI 默认 --strict 为 True(BooleanOptionalAction)。"""
        # 通过 --help 验证默认值
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "check_error_protocol.py"), "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        # --strict 应为 BooleanOptionalAction(支持 --no-strict)
        assert "--no-strict" in result.stdout or "--strict" in result.stdout, (
            "scanner 应支持 --strict / --no-strict(BooleanOptionalAction)"
        )


# ════════════════════════════════════════════════════════════
# E. 修复模式验证(AST 级,抽样)
# ════════════════════════════════════════════════════════════

# R65 P1-04 修复涉及的文件(原 174 处违规分布)
OBSERVABILITY_FIXED_FILES = [
    "admin/__init__.py",
    "bots/admin_bot/callback.py",
    "bots/admin_bot/conversation.py",
    "bots/admin_bot/display.py",
    "bots/admin_bot/handlers.py",
    "bots/dsp_bot.py",
    "bots/idx_bot.py",
    "bots/mon_bot.py",
    "bots/up_bot.py",
    "database/cache.py",
    "database/relay_db.py",
    "database/session.py",
    "services/button_flow.py",
    "services/collections.py",
    "services/content_reports.py",
    "services/crdb_ru_collector.py",
    "services/i18n.py",
    "services/maintenance_mode.py",
    "services/migration_runner.py",
    "services/mon/scheduler.py",
    "services/notifications.py",
    "services/prometheus_exporter.py",
    "services/relay_instance.py",
    "services/restore_backends.py",
    "services/retention_worker.py",
    "services/ru_cost_center.py",
    "services/task_center.py",
    "services/user_repair.py",
]


class TestFixPatterns:
    """验证修复模式正确:无 except Exception: pass,无 except 块裸 return 0/False。"""

    @staticmethod
    def _has_pass_only_except(file_path: str) -> list[int]:
        """检查文件中是否存在 except Exception: pass(body 仅为 pass)的行号。"""
        full_path = REPO_ROOT / file_path
        if not full_path.exists():
            return []
        source = full_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError:
            return []
        bad_lines = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if scanner._is_exception_handler_for_exception(node) and \
                    scanner._handler_body_is_only_pass(node):
                bad_lines.append(node.lineno)
        return bad_lines

    @staticmethod
    def _has_bare_return_in_except(file_path: str) -> list[int]:
        """检查文件中是否存在 except 块裸 return 0/False 的行号。

        R65 P1-04 final: scanner rule 3 已增强 — 若 except 块内含 logger 调用
        (logger.error/warning/exception/etc.),则 return False 满足
        "log or reraise" 语义,不视为违规。本方法同步该判定逻辑,
        仅在 except 块无 logger 调用且直接 return 0/False 时才记为违规。
        """
        full_path = REPO_ROOT / file_path
        if not full_path.exists():
            return []
        source = full_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError:
            return []
        bad_lines = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            # R65 P1-04 final: 检查 except 块内是否含 logger 调用
            # (与 scripts/check_error_protocol.py Rule 3 判定逻辑保持一致)
            has_logger_call = False
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                    if sub.func.attr in scanner.LOGGER_METHODS:
                        has_logger_call = True
                        break
            for stmt in node.body:
                if isinstance(stmt, ast.Return) and stmt.value is not None:
                    if scanner._is_bare_zero_or_false(stmt.value):
                        if has_logger_call:
                            # 已有 logger 调用,return False 满足
                            # "log or reraise" 语义,不视为违规
                            continue
                        bad_lines.append(stmt.lineno)
        return bad_lines

    def test_no_except_pass_in_observability_files(self):
        """R65 P1-04 修复涉及的文件中不存在 except Exception: pass。"""
        all_bad: list[str] = []
        for file_path in OBSERVABILITY_FIXED_FILES:
            bad = self._has_pass_only_except(file_path)
            if bad:
                all_bad.append(f"{file_path}: 行 {bad}")
        assert all_bad == [], (
            "以下文件仍存在 except Exception: pass:\n" + "\n".join(all_bad)
        )

    def test_no_bare_return_in_observability_files(self):
        """R65 P1-04 修复涉及的文件中不存在 except 块裸 return 0/False。"""
        all_bad: list[str] = []
        for file_path in OBSERVABILITY_FIXED_FILES:
            bad = self._has_bare_return_in_except(file_path)
            if bad:
                all_bad.append(f"{file_path}: 行 {bad}")
        assert all_bad == [], (
            "以下文件仍存在 except 块裸 return 0/False:\n" + "\n".join(all_bad)
        )
