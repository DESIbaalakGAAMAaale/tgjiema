"""R64 P1-07: 统一错误码存量债务模式整改 — 零违规验证测试。

审计背景(R64 P1-07):
  security/destructive/data-integrity/financial 四个高风险域仍存在
  281 级别的存量债务模式(except pass / except return 0/False /
  raise ValueError("...") / 裸字符串返回)。

  整改要求:高风险域必须达到 0 违规,observability 域使用结构化 allowlist。

测试覆盖矩阵(24 个用例):
  A. 高风险域零违规 (4)
     1-4. test_security_zero / test_destructive_zero /
          test_data_integrity_zero / test_financial_zero

  B. 高风险文件零违规 (8)
     5. test_rbac_zero
     6. test_quota_ledger_zero
     7. test_entitlements_zero
     8. test_disaster_recovery_zero
     9. test_db_backup_zero
     10. test_db_restore_zero
     11. test_repair_console_zero
     12. test_effect_receipts_zero

  C. cache_store 零违规 (1)
     13. test_cache_store_zero

  D. 域分类正确性 (4)
     14. test_cache_store_classified_as_data_integrity
     15. test_quota_ledger_classified_as_financial
     16. test_rbac_classified_as_security
     17. test_disaster_recovery_classified_as_destructive

  E. baseline ratchet 与结构 (3)
     18. test_baseline_ratchet_decreased
     19. test_baseline_high_risk_domains_zero
     20. test_baseline_observability_allowlist_valid

  F. 修复模式验证 (4)
     21. test_no_bare_return_in_except_for_rbac
     22. test_no_bare_pass_in_except_for_cache_store
     23. test_quota_ledger_return_moved_outside
     24. test_repair_console_return_moved_outside
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

# ── 让测试能导入 scripts/check_error_protocol.py ──
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_error_protocol as scanner  # noqa: E402

# 真实 baseline 文件路径
REAL_BASELINE = SCRIPTS_DIR / "error_protocol_baseline.json"


# ════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════

def _collect_all_findings() -> list[tuple[str, int, str]]:
    """收集全代码库的违规列表。"""
    return scanner.collect_findings()


def _filter_by_domain(findings: list[tuple[str, int, str]], domain: str) -> list[tuple[str, int, str]]:
    """按域过滤违规。"""
    return [
        (f, l, d) for f, l, d in findings
        if scanner._classify_domain(f) == domain
    ]


def _filter_by_file(findings: list[tuple[str, int, str]], file_substr: str) -> list[tuple[str, int, str]]:
    """按文件名子串过滤违规。"""
    return [
        (f, l, d) for f, l, d in findings
        if file_substr in f
    ]


def _scan_specific_file(rel_path: str) -> list[tuple[int, str]]:
    """扫描单个文件,返回违规列表。"""
    return scanner.scan_file(REPO_ROOT / rel_path)


# ════════════════════════════════════════════════════════════
# A. 高风险域零违规
# ════════════════════════════════════════════════════════════

class TestHighRiskDomainsZero:
    """验证四个高风险域的违规数为 0。"""

    def test_security_zero(self):
        """security 域(认证/授权/MFA/密码/RBAC)违规数为 0。"""
        findings = _collect_all_findings()
        sec = _filter_by_domain(findings, "security")
        assert sec == [], f"security 域仍有 {len(sec)} 个违规: {sec}"

    def test_destructive_zero(self):
        """destructive 域(删除/清除/备份恢复)违规数为 0。"""
        findings = _collect_all_findings()
        des = _filter_by_domain(findings, "destructive")
        assert des == [], f"destructive 域仍有 {len(des)} 个违规: {des}"

    def test_data_integrity_zero(self):
        """data-integrity 域(备份/恢复/事务/outbox/缓存)违规数为 0。"""
        findings = _collect_all_findings()
        di = _filter_by_domain(findings, "data-integrity")
        assert di == [], f"data-integrity 域仍有 {len(di)} 个违规: {di}"

    def test_financial_zero(self):
        """financial 域(配额/计费/积分)违规数为 0。"""
        findings = _collect_all_findings()
        fin = _filter_by_domain(findings, "financial")
        assert fin == [], f"financial 域仍有 {len(fin)} 个违规: {fin}"


# ════════════════════════════════════════════════════════════
# B. 高风险文件零违规
# ════════════════════════════════════════════════════════════

class TestHighRiskFilesZero:
    """验证每个高风险文件的违规数为 0。"""

    def test_rbac_zero(self):
        """services/rbac.py (security 域) 零违规。"""
        violations = _scan_specific_file("services/rbac.py")
        assert violations == [], f"rbac.py 仍有 {len(violations)} 个违规: {violations}"

    def test_quota_ledger_zero(self):
        """services/quota_ledger.py (financial 域) 零违规。"""
        violations = _scan_specific_file("services/quota_ledger.py")
        assert violations == [], f"quota_ledger.py 仍有 {len(violations)} 个违规: {violations}"

    def test_entitlements_zero(self):
        """services/entitlements.py (financial 域) 零违规。"""
        violations = _scan_specific_file("services/entitlements.py")
        assert violations == [], f"entitlements.py 仍有 {len(violations)} 个违规: {violations}"

    def test_disaster_recovery_zero(self):
        """services/disaster_recovery.py (destructive 域) 零违规。"""
        violations = _scan_specific_file("services/disaster_recovery.py")
        assert violations == [], f"disaster_recovery.py 仍有 {len(violations)} 个违规: {violations}"

    def test_db_backup_zero(self):
        """services/db_backup.py (destructive 域) 零违规。"""
        violations = _scan_specific_file("services/db_backup.py")
        assert violations == [], f"db_backup.py 仍有 {len(violations)} 个违规: {violations}"

    def test_db_restore_zero(self):
        """services/db_restore.py (destructive 域) 零违规。"""
        violations = _scan_specific_file("services/db_restore.py")
        assert violations == [], f"db_restore.py 仍有 {len(violations)} 个违规: {violations}"

    def test_repair_console_zero(self):
        """services/repair_console.py (destructive 域) 零违规。"""
        violations = _scan_specific_file("services/repair_console.py")
        assert violations == [], f"repair_console.py 仍有 {len(violations)} 个违规: {violations}"

    def test_effect_receipts_zero(self):
        """services/effect_receipts.py (data-integrity 域) 零违规。"""
        violations = _scan_specific_file("services/effect_receipts.py")
        assert violations == [], f"effect_receipts.py 仍有 {len(violations)} 个违规: {violations}"


# ════════════════════════════════════════════════════════════
# C. cache_store 零违规
# ════════════════════════════════════════════════════════════

class TestCacheStoreZero:
    """验证 database/cache_store.py (data-integrity 域,原 74 个违规) 零违规。"""

    def test_cache_store_zero(self):
        """database/cache_store.py 零违规(原 74 个,现已全部修复)。"""
        violations = _scan_specific_file("database/cache_store.py")
        assert violations == [], f"cache_store.py 仍有 {len(violations)} 个违规: {violations}"


# ════════════════════════════════════════════════════════════
# D. 域分类正确性
# ════════════════════════════════════════════════════════════

class TestDomainClassification:
    """验证高风险文件被正确分类到对应域。"""

    def test_cache_store_classified_as_data_integrity(self):
        """database/cache_store.py 被分类为 data-integrity 域。"""
        assert scanner._classify_domain("database/cache_store.py") == "data-integrity"

    def test_quota_ledger_classified_as_financial(self):
        """services/quota_ledger.py 被分类为 financial 域。"""
        assert scanner._classify_domain("services/quota_ledger.py") == "financial"

    def test_rbac_classified_as_security(self):
        """services/rbac.py 被分类为 security 域。"""
        assert scanner._classify_domain("services/rbac.py") == "security"

    def test_disaster_recovery_classified_as_destructive(self):
        """services/disaster_recovery.py 被分类为 destructive 域。"""
        assert scanner._classify_domain("services/disaster_recovery.py") == "destructive"


# ════════════════════════════════════════════════════════════
# E. baseline ratchet 与结构
# ════════════════════════════════════════════════════════════

class TestBaselineIntegrity:
    """验证 baseline 文件的完整性和 ratchet 机制。"""

    def test_baseline_ratchet_decreased(self):
        """violation_count 从前值下降(285 → 175 → 0),ratchet 只减不增。

        R65 P1-04: observability allowlist 已清空(175 → 0),所有异常归一到
        logger.exception / return None,不再依赖 allowlist 容忍存量债务。
        """
        data = json.loads(REAL_BASELINE.read_text(encoding="utf-8"))
        current = data.get("violation_count", 0)
        previous = data.get("previous_violation_count", 0)
        assert current <= previous, (
            f"ratchet 违规: violation_count={current} > previous={previous}"
        )
        # R65 P1-04: observability allowlist 清空,violation_count 降为 0
        assert current == 0, f"期望 violation_count=0,实际={current}"

    def test_baseline_high_risk_domains_zero(self):
        """baseline 中四个高风险域的 baseline_violations 均为 0。"""
        data = json.loads(REAL_BASELINE.read_text(encoding="utf-8"))
        domains = data.get("domains", {})
        for dom in ("security", "destructive", "data-integrity", "financial"):
            bv = domains.get(dom, {}).get("baseline_violations")
            assert bv == 0, f"域 {dom} 的 baseline_violations={bv},期望 0"

    def test_baseline_observability_allowlist_valid(self):
        """observability 域 allowlist 条目数 = violation_count,且 real_violations=0。"""
        data = json.loads(REAL_BASELINE.read_text(encoding="utf-8"))
        obs_allowlist = data.get("domains", {}).get("observability", {}).get("allowlist", [])
        vc = data.get("violation_count", 0)
        assert len(obs_allowlist) == vc, (
            f"allowlist 条目数({len(obs_allowlist)}) != violation_count({vc})"
        )
        # 每个 allowlist 条目必须有必需字段
        for entry in obs_allowlist:
            assert "file" in entry, f"allowlist 条目缺少 file 字段: {entry}"
            assert "fingerprint" in entry, f"allowlist 条目缺少 fingerprint 字段: {entry}"


# ════════════════════════════════════════════════════════════
# F. 修复模式验证(AST 级)
# ════════════════════════════════════════════════════════════

class TestFixPatterns:
    """验证修复模式正确: return 移到 except 外, pass 替换为 logger 调用。"""

    @staticmethod
    def _has_return_in_except(file_path: str) -> list[int]:
        """检查文件中是否存在 except 块直接 return 0/False 的行号。"""
        full_path = REPO_ROOT / file_path
        source = full_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=file_path)
        bad_lines = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            for stmt in node.body:
                if isinstance(stmt, ast.Return) and stmt.value is not None:
                    if isinstance(stmt.value, ast.Constant):
                        val = stmt.value.value
                        if (isinstance(val, bool) and val is False) or (
                            isinstance(val, int) and val == 0 and not isinstance(val, bool)
                        ):
                            bad_lines.append(stmt.lineno)
        return bad_lines

    @staticmethod
    def _has_pass_only_except(file_path: str) -> list[int]:
        """检查文件中是否存在 except Exception: pass(body 仅为 pass)的行号。"""
        full_path = REPO_ROOT / file_path
        source = full_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=file_path)
        bad_lines = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            is_exc = (node.type is None) or (
                isinstance(node.type, ast.Name) and node.type.id == "Exception"
            )
            if is_exc and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                bad_lines.append(node.lineno)
        return bad_lines

    def test_no_bare_return_in_except_for_rbac(self):
        """services/rbac.py 中不存在 except 块裸 return 0/False。"""
        bad = self._has_return_in_except("services/rbac.py")
        assert bad == [], f"rbac.py 仍有 except 块裸 return: 行 {bad}"

    def test_no_bare_pass_in_except_for_cache_store(self):
        """database/cache_store.py 中不存在 except Exception: pass。"""
        bad = self._has_pass_only_except("database/cache_store.py")
        assert bad == [], f"cache_store.py 仍有 except Exception: pass: 行 {bad}"

    def test_quota_ledger_return_moved_outside(self):
        """services/quota_ledger.py 中不存在 except 块裸 return 0/False。"""
        bad = self._has_return_in_except("services/quota_ledger.py")
        assert bad == [], f"quota_ledger.py 仍有 except 块裸 return: 行 {bad}"

    def test_repair_console_return_moved_outside(self):
        """services/repair_console.py 中不存在 except 块裸 return 0/False。"""
        bad = self._has_return_in_except("services/repair_console.py")
        assert bad == [], f"repair_console.py 仍有 except 块裸 return: 行 {bad}"
