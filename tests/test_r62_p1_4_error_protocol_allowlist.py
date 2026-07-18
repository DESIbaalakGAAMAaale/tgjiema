"""R62 P1-04: 错误协议结构化 allowlist 测试。

审计背景(R62 终审报告 P1-04):
  observability 域使用 `max_violations=305`、`violation_count=278`、空 allowlist,
  不能定位 owner/reason/expiry,也允许大量存量长期存在。

  修复要求:每个违规必须成为结构化 allowlist 条目(file/line/fingerprint/owner/
  reason/expiry/ticket);过期条目导致失败;每个 commit 只能减少违规数;
  生产目标 real_violations=0。

测试覆盖矩阵(7 个核心用例 + 1 个集成用例):
  A. allowlist 放行 (1)
     1. test_valid_expiry_allowlisted: 有效 expiry 的条目 → 违规被放行(passes)

  B. 过期与未匹配 (2)
     2. test_expired_expiry_fails: 过期 expiry 的条目 → 失败(expired entry)
     3. test_violation_not_in_allowlist_fails: 违规未在 allowlist → 失败(real violation)

  C. 指纹计算 (2)
     4. test_fingerprint_deterministic: 相同输入 → 相同指纹(确定性)
     5. test_fingerprint_changes_with_line: 行号变化 → 指纹变化

  D. 模式与 ratchet (2)
     6. test_strict_mode_fails_on_non_allowlisted: strict 模式下未 allowlist 即失败
     7. test_ratchet_fails_on_increase: violation_count 增加即失败(只减不增)

  E. 集成验证 (1)
     8. test_real_baseline_passes_strict: 真实 baseline (278 条目) 通过 strict 检查
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

# ── 让测试能导入 scripts/check_error_protocol.py ──
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_error_protocol as scanner  # noqa: E402

# 真实 baseline 文件路径(用于集成测试)
REAL_BASELINE = SCRIPTS_DIR / "error_protocol_baseline.json"


# ════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════
def _make_synthetic_finding(
    file_path: str = "services/_r62_p1_4_synthetic_test_module.py",
    line_no: int = 42,
    detail: str = "P1-5 规则3: except 块中 return 0/False (synthetic test)",
) -> tuple[str, int, str]:
    """构造一个合成的违规 finding(用于测试,不依赖真实代码库状态)。

    使用不存在的文件路径,确保 _get_source_line_context 返回空字符串,
    使指纹计算仅依赖 file/line/violation_type(可控、确定)。
    """
    return (file_path, line_no, detail)


def _write_baseline(tmp_path: Path, *, allowlist: list, violation_count: int) -> Path:
    """在 tmp_path 下生成一个最小可用的 baseline JSON 文件。

    Args:
        tmp_path: pytest tmp_path fixture 提供的临时目录
        allowlist: observability.allowlist 字段内容
        violation_count: 顶层 violation_count 字段(用于 ratchet 检查)

    Returns:
        baseline 文件路径
    """
    baseline = {
        "description": "R62 P1-04 test baseline",
        "version": "R62-P1-04",
        "domains": {
            "observability": {
                "description": "test observability domain",
                "max_violations": 0,
                "allowlist_required": True,
                "allowlist": allowlist,
            },
            # 零容忍域配置(测试中保持 0)
            "security": {"max_violations": 0, "baseline_violations": 0, "paths": []},
            "destructive": {"max_violations": 0, "baseline_violations": 0, "paths": []},
            "data-integrity": {"max_violations": 0, "baseline_violations": 0, "paths": []},
            "financial": {"max_violations": 0, "baseline_violations": 0, "paths": []},
        },
        "violation_count": violation_count,
    }
    baseline_path = tmp_path / "test_baseline.json"
    baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2))
    return baseline_path


# ════════════════════════════════════════════════════════════
# A. allowlist 放行
# ════════════════════════════════════════════════════════════
def test_valid_expiry_allowlisted(tmp_path):
    """测试 1: 结构化 allowlist 条目(有效 expiry)→ 违规被放行(passes)。

    场景: 一个违规的指纹匹配 allowlist 条目,且 expiry > today。
    期望: 检查通过,real_violations=0,allowlisted=1,expired_entries=0。
    """
    finding = _make_synthetic_finding()
    file_path, line_no, _ = finding

    # 构建 allowlist 条目(expiry 在未来)
    future_expiry = (date.today() + timedelta(days=30)).isoformat()
    entry = scanner._build_allowlist_entry(file_path, line_no, finding[2], expiry=future_expiry)

    baseline_path = _write_baseline(
        tmp_path, allowlist=[entry], violation_count=1,
    )

    passed, msg, summary = scanner._check_domain_baseline(
        [finding], baseline_path, strict=False,
    )

    assert passed, f"应通过(违规已 allowlist 且未过期): {msg}"
    assert summary["allowlisted"] == 1, "应有 1 条被 allowlist"
    assert summary["real_violations"] == 0, "real_violations 必须为 0"
    assert summary["expired_entries"] == 0, "不应有过期条目"
    assert summary["total_violations"] == 1


# ════════════════════════════════════════════════════════════
# B. 过期与未匹配
# ════════════════════════════════════════════════════════════
def test_expired_expiry_fails(tmp_path):
    """测试 2: 结构化 allowlist 条目(过期 expiry)→ 失败(expired entry)。

    场景: 违规的指纹匹配 allowlist 条目,但 expiry < today。
    期望: 检查失败,expired_entries=1,real_violations=1(过期后视为真实违规)。
    """
    finding = _make_synthetic_finding()
    file_path, line_no, _ = finding

    # 构建 allowlist 条目(expiry 已过期)
    past_expiry = (date.today() - timedelta(days=1)).isoformat()
    entry = scanner._build_allowlist_entry(file_path, line_no, finding[2], expiry=past_expiry)

    baseline_path = _write_baseline(
        tmp_path, allowlist=[entry], violation_count=1,
    )

    passed, msg, summary = scanner._check_domain_baseline(
        [finding], baseline_path, strict=False,
    )

    assert not passed, "应失败(allowlist 条目已过期)"
    assert "已过期" in msg, f"消息应包含 '已过期': {msg}"
    assert summary["expired_entries"] == 1, "应有 1 条过期"
    assert summary["real_violations"] == 1, "过期后计为真实违规"
    assert summary["allowlisted"] == 0, "不应有被放行的"


def test_violation_not_in_allowlist_fails(tmp_path):
    """测试 3: 违规未在 allowlist 中 → 失败(real violation)。

    场景: 一个违规的指纹不在 allowlist 中(空 allowlist)。
    期望: 检查失败,real_violations=1,allowlisted=0,消息含 '未在 allowlist'。
    """
    finding = _make_synthetic_finding()

    # 空 allowlist
    baseline_path = _write_baseline(
        tmp_path, allowlist=[], violation_count=1,
    )

    passed, msg, summary = scanner._check_domain_baseline(
        [finding], baseline_path, strict=False,
    )

    assert not passed, "应失败(违规未在 allowlist 中)"
    assert "未在 allowlist" in msg, f"消息应包含 '未在 allowlist': {msg}"
    assert summary["real_violations"] == 1, "应有 1 条真实违规"
    assert summary["allowlisted"] == 0, "不应有被放行的"
    assert summary["expired_entries"] == 0, "不应有过期的"


# ════════════════════════════════════════════════════════════
# C. 指纹计算
# ════════════════════════════════════════════════════════════
def test_fingerprint_deterministic():
    """测试 4: 指纹计算是确定性的(相同输入 → 相同指纹)。

    场景: 用相同参数调用 _compute_violation_fingerprint 两次。
    期望: 两次返回相同的 64 字符 sha256 hex 字符串。
    """
    fp1 = scanner._compute_violation_fingerprint(
        "services/foo.py", 100, "P1-5 规则3", "return False",
    )
    fp2 = scanner._compute_violation_fingerprint(
        "services/foo.py", 100, "P1-5 规则3", "return False",
    )

    assert fp1 == fp2, "相同输入必须产生相同指纹(确定性)"
    assert len(fp1) == 64, f"sha256 hex 应为 64 字符,实际: {len(fp1)}"
    # 验证是合法的十六进制字符串
    int(fp1, 16)  # 若含非 hex 字符会抛 ValueError


def test_fingerprint_changes_with_line():
    """测试 5: 行号变化 → 指纹变化(确保 allowlist 条目随代码移动需更新)。

    场景: 同一文件、同一违规类型,但行号不同(100 vs 101)。
    期望: 两个指纹不同(行号是指纹的一部分)。
    """
    fp_line_100 = scanner._compute_violation_fingerprint(
        "services/foo.py", 100, "P1-5 规则3", "return False",
    )
    fp_line_101 = scanner._compute_violation_fingerprint(
        "services/foo.py", 101, "P1-5 规则3", "return False",
    )

    assert fp_line_100 != fp_line_101, "行号变化必须导致指纹变化"
    # 验证两个都是合法的 64 字符 sha256
    assert len(fp_line_100) == 64
    assert len(fp_line_101) == 64


def test_fingerprint_changes_with_other_fields():
    """补充测试: 文件路径、违规类型、上下文变化 → 指纹变化。

    确保指纹包含所有 4 个维度(file/line/violation_type/context),
    单一维度变化即可导致指纹变化(防止单维度碰撞)。
    """
    base = scanner._compute_violation_fingerprint(
        "services/foo.py", 100, "P1-5 规则3", "return False",
    )
    # 文件路径变化
    assert base != scanner._compute_violation_fingerprint(
        "services/bar.py", 100, "P1-5 规则3", "return False",
    )
    # 违规类型变化
    assert base != scanner._compute_violation_fingerprint(
        "services/foo.py", 100, "P1-5 规则5", "return False",
    )
    # 上下文变化
    assert base != scanner._compute_violation_fingerprint(
        "services/foo.py", 100, "P1-5 规则3", "return True",
    )


# ════════════════════════════════════════════════════════════
# D. 模式与 ratchet
# ════════════════════════════════════════════════════════════
def test_strict_mode_fails_on_non_allowlisted(tmp_path):
    """测试 6: --strict 模式下,任何未在 allowlist 中的违规即失败。

    场景: strict=True,违规不在 allowlist 中。
    期望: 检查失败,消息含 'strict'。
    """
    finding = _make_synthetic_finding()

    # 空 allowlist,strict 模式
    baseline_path = _write_baseline(
        tmp_path, allowlist=[], violation_count=1,
    )

    passed, msg, summary = scanner._check_domain_baseline(
        [finding], baseline_path, strict=True,
    )

    assert not passed, "strict 模式下未 allowlist 的违规应失败"
    assert "strict" in msg, f"消息应包含 'strict': {msg}"
    assert summary["real_violations"] == 1


def test_strict_mode_passes_when_all_allowlisted(tmp_path):
    """补充测试: --strict 模式下,所有违规已 allowlist(未过期)→ 通过。

    strict 模式跳过 ratchet 检查,只检查 real_violations == 0。
    """
    finding = _make_synthetic_finding()
    file_path, line_no, _ = finding

    future_expiry = (date.today() + timedelta(days=30)).isoformat()
    entry = scanner._build_allowlist_entry(file_path, line_no, finding[2], expiry=future_expiry)

    # baseline violation_count=0,但 strict 模式跳过 ratchet → 仍应通过
    baseline_path = _write_baseline(
        tmp_path, allowlist=[entry], violation_count=0,
    )

    passed, msg, summary = scanner._check_domain_baseline(
        [finding], baseline_path, strict=True,
    )

    assert passed, f"strict 模式下所有违规已 allowlist 应通过: {msg}"
    assert summary["real_violations"] == 0
    assert summary["allowlisted"] == 1


def test_ratchet_fails_on_increase(tmp_path):
    """测试 7: 每个 commit 只能减少不能增加(violation_count 增加 → 失败)。

    场景: baseline violation_count=0,但当前有 1 个违规(已 allowlist)。
          非 strict 模式 → ratchet 检查 1 > 0 → 失败。
    """
    finding = _make_synthetic_finding()
    file_path, line_no, _ = finding

    # 构建 allowlist 条目(有效 expiry,使 real_violations=0)
    future_expiry = (date.today() + timedelta(days=30)).isoformat()
    entry = scanner._build_allowlist_entry(file_path, line_no, finding[2], expiry=future_expiry)

    # baseline violation_count=0,但当前有 1 个违规 → ratchet 失败
    baseline_path = _write_baseline(
        tmp_path, allowlist=[entry], violation_count=0,
    )

    passed, msg, summary = scanner._check_domain_baseline(
        [finding], baseline_path, strict=False,
    )

    assert not passed, "应失败(ratchet: 违规数增加)"
    assert "ratchet" in msg, f"消息应包含 'ratchet': {msg}"
    assert summary["total_violations"] == 1


def test_ratchet_passes_when_equal(tmp_path):
    """补充测试: violation_count 相等 → ratchet 通过(只要 real_violations=0)。

    场景: baseline violation_count=1,当前 1 个违规(已 allowlist)→ 通过。
    """
    finding = _make_synthetic_finding()
    file_path, line_no, _ = finding

    future_expiry = (date.today() + timedelta(days=30)).isoformat()
    entry = scanner._build_allowlist_entry(file_path, line_no, finding[2], expiry=future_expiry)

    baseline_path = _write_baseline(
        tmp_path, allowlist=[entry], violation_count=1,
    )

    passed, msg, summary = scanner._check_domain_baseline(
        [finding], baseline_path, strict=False,
    )

    assert passed, f"应通过(ratchet 相等且 real_violations=0): {msg}"
    assert summary["real_violations"] == 0


def test_ratchet_passes_when_decreased(tmp_path):
    """补充测试: violation_count 减少 → ratchet 通过(允许下降)。

    场景: baseline violation_count=2,当前只有 1 个违规(已 allowlist)→ 通过。
    """
    finding = _make_synthetic_finding()
    file_path, line_no, _ = finding

    future_expiry = (date.today() + timedelta(days=30)).isoformat()
    entry = scanner._build_allowlist_entry(file_path, line_no, finding[2], expiry=future_expiry)

    # baseline violation_count=2(更高),当前只有 1 个违规 → ratchet 通过
    baseline_path = _write_baseline(
        tmp_path, allowlist=[entry], violation_count=2,
    )

    passed, msg, summary = scanner._check_domain_baseline(
        [finding], baseline_path, strict=False,
    )

    assert passed, f"应通过(ratchet 下降): {msg}"
    assert summary["total_violations"] == 1
    assert summary["real_violations"] == 0


# ════════════════════════════════════════════════════════════
# E. 集成验证(真实 baseline)
# ════════════════════════════════════════════════════════════
def test_real_baseline_passes_strict():
    """测试 8: 真实 baseline (278 条目) 通过 strict 检查。

    集成测试: 加载 scripts/error_protocol_baseline.json,运行 scanner 收集
    真实违规,验证所有违规都在 allowlist 中且未过期(real_violations=0)。
    """
    if not REAL_BASELINE.exists():
        pytest.skip(f"真实 baseline 不存在: {REAL_BASELINE}")

    # 收集真实违规
    findings = scanner.collect_findings()
    assert findings, "scanner 应该能找到至少一个违规(否则测试无意义)"

    # 用真实 baseline 运行 strict 检查
    passed, msg, summary = scanner._check_domain_baseline(
        findings, REAL_BASELINE, strict=True,
    )

    assert passed, (
        f"真实 baseline 应通过 strict 检查(所有违规已 allowlist): {msg}\n"
        f"summary: total={summary['total_violations']}, "
        f"allowlisted={summary['allowlisted']}, "
        f"real={summary['real_violations']}, "
        f"expired={summary['expired_entries']}"
    )
    assert summary["real_violations"] == 0, "real_violations 必须为 0"
    assert summary["expired_entries"] == 0, "不应有过期条目"
    assert summary["allowlisted"] == summary["total_violations"], (
        "所有违规都应被 allowlist"
    )


def test_real_baseline_passes_non_strict_ratchet():
    """补充集成测试: 真实 baseline 通过非 strict 模式(含 ratchet 检查)。

    非 strict 模式额外检查: 总违规数 <= baseline violation_count。
    """
    if not REAL_BASELINE.exists():
        pytest.skip(f"真实 baseline 不存在: {REAL_BASELINE}")

    findings = scanner.collect_findings()

    passed, msg, summary = scanner._check_domain_baseline(
        findings, REAL_BASELINE, strict=False,
    )

    assert passed, (
        f"真实 baseline 应通过非 strict 检查(含 ratchet): {msg}"
    )


def test_real_baseline_allowlist_entries_have_all_fields():
    """补充集成测试: 真实 baseline 的所有 allowlist 条目都有完整字段。

    验证每个条目包含: file/line/fingerprint/owner/reason/expiry/ticket。
    """
    if not REAL_BASELINE.exists():
        pytest.skip(f"真实 baseline 不存在: {REAL_BASELINE}")

    data = json.loads(REAL_BASELINE.read_text(encoding="utf-8"))
    allowlist = data.get("domains", {}).get("observability", {}).get("allowlist", [])

    required_fields = {"file", "line", "fingerprint", "owner", "reason", "expiry", "ticket"}
    assert len(allowlist) > 0, "allowlist 不应为空(应有 278 条存量违规)"

    for i, entry in enumerate(allowlist):
        missing = required_fields - set(entry.keys())
        assert not missing, (
            f"allowlist 条目 {i} ({entry.get('file', '?')}:{entry.get('line', '?')}) "
            f"缺少字段: {missing}"
        )
        # 验证字段类型
        assert isinstance(entry["file"], str) and entry["file"], f"条目 {i} file 无效"
        assert isinstance(entry["line"], int) and entry["line"] > 0, f"条目 {i} line 无效"
        assert isinstance(entry["fingerprint"], str) and len(entry["fingerprint"]) == 64, (
            f"条目 {i} fingerprint 应为 64 字符 sha256 hex"
        )
        assert isinstance(entry["owner"], str) and entry["owner"], f"条目 {i} owner 无效"
        assert isinstance(entry["reason"], str) and entry["reason"], f"条目 {i} reason 无效"
        assert isinstance(entry["expiry"], str), f"条目 {i} expiry 无效"
        # 验证 expiry 格式(ISO 日期)
        date.fromisoformat(entry["expiry"])
        assert isinstance(entry["ticket"], str) and entry["ticket"], f"条目 {i} ticket 无效"


def test_real_baseline_allowlist_fingerprints_unique():
    """补充集成测试: 真实 baseline 的 allowlist 指纹唯一(无重复)。

    若指纹重复,说明 _build_allowlist_entry 生成的条目无法区分不同违规。
    """
    if not REAL_BASELINE.exists():
        pytest.skip(f"真实 baseline 不存在: {REAL_BASELINE}")

    data = json.loads(REAL_BASELINE.read_text(encoding="utf-8"))
    allowlist = data.get("domains", {}).get("observability", {}).get("allowlist", [])

    fingerprints = [entry["fingerprint"] for entry in allowlist]
    assert len(fingerprints) == len(set(fingerprints)), (
        f"allowlist 指纹有重复: 总 {len(fingerprints)}, 唯一 {len(set(fingerprints))}"
    )


def test_build_allowlist_entry_has_all_fields():
    """补充测试: _build_allowlist_entry 生成的条目包含所有必需字段。"""
    finding = _make_synthetic_finding()
    entry = scanner._build_allowlist_entry(*finding)

    required_fields = {"file", "line", "fingerprint", "owner", "reason", "expiry", "ticket"}
    assert set(entry.keys()) == required_fields, (
        f"条目字段不完整: {set(entry.keys())} vs {required_fields}"
    )

    # 验证字段值
    assert entry["file"] == finding[0]
    assert entry["line"] == finding[1]
    assert len(entry["fingerprint"]) == 64
    assert entry["owner"] == scanner.DEFAULT_ALLOWLIST_OWNER
    assert entry["reason"] == scanner.DEFAULT_ALLOWLIST_REASON
    assert entry["expiry"] == scanner.DEFAULT_ALLOWLIST_EXPIRY
    assert entry["ticket"] == scanner.DEFAULT_ALLOWLIST_TICKET


def test_extract_violation_type():
    """补充测试: _extract_violation_type 正确提取违规类型标识。"""
    assert scanner._extract_violation_type("P1-5 规则3: some detail") == "P1-5 规则3"
    assert scanner._extract_violation_type("P1-5 规则1/2: detail") == "P1-5 规则1/2"
    # 无冒号 → 返回原字符串
    assert scanner._extract_violation_type("no colon here") == "no colon here"
