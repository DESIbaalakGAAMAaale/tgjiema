"""R47 P1-d: 模块化 i18n baseline 逐模块下降门禁测试。

覆盖:
    - 模块化 baseline 读取与结构
    - 超过 baseline → 失败(模块级 / total 级)
    - --ratchet 更新(下降/持平/上升拒绝/模块再平衡)
    - 清零目标计算(距清零 = 当前计数,目标 0)
"""
import json
import sys
from pathlib import Path

import pytest

# 让测试能导入 scripts/scan_hardcoded_strings.py
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import scan_hardcoded_strings as scan  # noqa: E402


# ---------------------------------------------------------------------------
# 辅助:构造合成 baseline(不依赖真实 baseline.json 的具体数值,避免债务清还后失效)
# ---------------------------------------------------------------------------
def _synthetic_baseline(**overrides) -> dict:
    """返回每模块 baseline=10 的合成 dict;可按需覆盖。

    R48: 包含 included_paths 字段,避免 _check_scope_change 误报 scope 变化。
    R56 §5.1: 使用新格式(modules 子字典 + user_visible=0/log_only=10)。
    """
    modules = {
        m: {"baseline": 10, "target": 0, "user_visible": 0, "log_only": 10}
        for m in scan.MODULE_KEYS
    }
    base = {
        "_original_r44_baseline": 954,
        "scanner_version": scan.SCANNER_VERSION,
        "included_paths": list(scan.INCLUDED_PATHS),  # R48: scope 不变
        "modules": modules,
        "total": {
            "baseline": 10 * len(scan.MODULE_KEYS),
            "target": 0,
            "user_visible": 0,
            "log_only": 10 * len(scan.MODULE_KEYS),
        },
    }
    base.update(overrides)
    return base


def _patch_classify_as_log_only(monkeypatch, counts):
    """R56 §5.1: 将 counts 全部视为 log_only(非 user_visible),避免绝对门禁误报。

    cmd_check 在未传 findings/root 时,兜底将 counts 当作 user_visible 处理,
    这会导致非零 counts 触发绝对门禁失败。此 helper 通过 monkeypatch
    classify_findings 让 counts 走 log_only 通道(允许走 ratchet)。
    """
    monkeypatch.setattr(
        scan, "classify_findings",
        lambda f, r: {m: {"user_visible": 0, "log_only": counts.get(m, 0)}
                      for m in scan.MODULE_KEYS},
    )


def _counts_from_baseline(baseline: dict) -> dict[str, int]:
    return {m: scan._baseline_module_count(baseline, m) for m in scan.MODULE_KEYS}


# ---------------------------------------------------------------------------
# 1. 模块化 baseline 读取与结构
# ---------------------------------------------------------------------------
def test_module_keys_cover_all_conceptual_modules():
    """模块键覆盖 Up/Idx/Dsp/Mon/Admin Bot/Admin Web/Services。"""
    for m in [
        "bots/up_bot.py",
        "bots/idx_bot.py",
        "bots/dsp_bot.py",
        "bots/mon_bot.py",
        "bots/admin_bot/",   # Admin Bot
        "admin/templates/",  # Admin Web HTML
        "services/",         # 服务层
    ]:
        assert m in scan.MODULE_KEYS


def test_module_for_file_mapping():
    """文件路径到模块键的映射准确(含 admin/ 子目录细分)。"""
    assert scan._module_for_file("bots/up_bot.py") == "bots/up_bot.py"
    assert scan._module_for_file("bots/idx_bot.py") == "bots/idx_bot.py"
    assert scan._module_for_file("bots/dsp_bot.py") == "bots/dsp_bot.py"
    assert scan._module_for_file("bots/mon_bot.py") == "bots/mon_bot.py"
    # Admin Bot 子目录
    assert scan._module_for_file("bots/admin_bot/handlers.py") == "bots/admin_bot/"
    assert scan._module_for_file("bots/admin_bot/conversation.py") == "bots/admin_bot/"
    # admin/ 整个目录细分为 admin/ + admin/templates/ + admin/static/
    assert scan._module_for_file("admin/__init__.py") == "admin/"
    assert scan._module_for_file("admin/sessions.py") == "admin/"
    assert scan._module_for_file("admin/templates/files.html") == "admin/templates/"
    assert scan._module_for_file("admin/templates/base.html") == "admin/templates/"
    # admin/static/ 当前不存在但保留占位清零门禁
    assert scan._module_for_file("admin/static/app.js") == "admin/static/"
    # services/
    assert scan._module_for_file("services/i18n.py") == "services/"
    assert scan._module_for_file("services/mon/scheduler.py") == "services/"
    # 未归属
    assert scan._module_for_file("README.md") is None
    assert scan._module_for_file("database/redis_queue.py") is None


def test_real_baseline_file_loads_and_consistent():
    """真实 locales/baseline.json 可读取,且 total == 各模块之和。

    R48: baseline 格式迁移到嵌套结构(modules/total 为 dict),通过 _baseline_module_count
    和 _baseline_total 访问器兼容新旧格式。
    """
    baseline = scan._load_module_baseline()
    assert baseline, "locales/baseline.json 未生成或为空(请先运行 --generate-baseline)"
    assert baseline.get("_original_r44_baseline") == 954
    # R48: modules 在 baseline["modules"] 下(新格式);兼容旧格式顶层键
    modules_dict = baseline.get("modules", baseline)
    for m in scan.MODULE_KEYS:
        assert m in modules_dict, f"baseline 缺少模块键: {m}"
    total = sum(scan._baseline_module_count(baseline, m) for m in scan.MODULE_KEYS)
    assert total == scan._baseline_total(baseline), (
        f"baseline.total({scan._baseline_total(baseline)}) != 各模块之和({total})"
    )


def test_baseline_is_json_serializable():
    """baseline.json 必须是合法 JSON 且可序列化(跨平台)。"""
    baseline = scan._load_module_baseline()
    # 反复序列化/反序列化应稳定
    s = json.dumps(baseline, ensure_ascii=False)
    again = json.loads(s)
    assert again == baseline


# ---------------------------------------------------------------------------
# 2. 超过 baseline → 失败
# ---------------------------------------------------------------------------
def test_check_passes_when_current_equals_baseline(monkeypatch):
    baseline = _synthetic_baseline()
    counts = _counts_from_baseline(baseline)
    _patch_classify_as_log_only(monkeypatch, counts)
    assert scan.cmd_check(counts, baseline, findings=[], root=Path(".")) == 0


def test_check_fails_when_single_module_exceeds():
    baseline = _synthetic_baseline()
    counts = _counts_from_baseline(baseline)
    counts["services/"] += 1  # 单模块超标
    assert scan.cmd_check(counts, baseline) == 1


def test_check_fails_when_total_exceeds_only():
    """仅 total 门禁触发:篡改 baseline.total 为更低值(模块值不变)。"""
    baseline = _synthetic_baseline()
    counts = _counts_from_baseline(baseline)
    bl = dict(baseline)
    bl["total"] = sum(counts.values()) - 1  # baseline.total 比当前少 1
    assert scan.cmd_check(counts, bl) == 1


def test_check_hints_ratchet_when_module_decreased(monkeypatch, capsys):
    """模块下降时 --check 不失败,但提示运行 --ratchet。

    R48: 输出含 '距target' 列(target=0);R47 的 '距清零' 改为 '距target'。
    R56 §5.1: 通过 monkeypatch classify_findings 将 counts 视为 log_only。
    """
    baseline = _synthetic_baseline()
    counts = _counts_from_baseline(baseline)
    counts["bots/up_bot.py"] -= 1
    _patch_classify_as_log_only(monkeypatch, counts)
    assert scan.cmd_check(counts, baseline, findings=[], root=Path(".")) == 0
    out = capsys.readouterr().out
    assert "--ratchet" in out
    assert "距target" in out  # R48: 输出距 target(0)的差距


def test_check_fails_when_baseline_missing(capsys):
    """baseline 缺失时 --check 失败并提示生成。"""
    counts = _counts_from_baseline(_synthetic_baseline())
    rc = scan.cmd_check(counts, {})  # 空 baseline
    assert rc == 1
    assert "--generate-baseline" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 3. --ratchet 更新
# ---------------------------------------------------------------------------
def test_ratchet_refuses_when_total_increases(monkeypatch, tmp_path):
    """total 增加 → 拒绝更新且不写文件。"""
    monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
    baseline = _synthetic_baseline()
    counts = _counts_from_baseline(baseline)
    counts["services/"] += 5  # total 增加 5
    assert scan.cmd_ratchet(counts, baseline) == 1
    assert not (tmp_path / "baseline.json").exists()  # 未写文件


def test_ratchet_updates_when_total_decreases(monkeypatch, tmp_path):
    """total 下降 → 写入新 baseline(各模块同步下降)。"""
    tmp_baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_baseline)
    baseline = _synthetic_baseline()  # 每模块 10
    counts = {m: 8 for m in scan.MODULE_KEYS}  # 每模块降 2
    assert scan.cmd_ratchet(counts, baseline) == 0
    saved = json.loads(tmp_baseline.read_text(encoding="utf-8"))
    # R48: 新格式 modules 在 "modules" 下,total 为 dict
    for m in scan.MODULE_KEYS:
        assert saved["modules"][m]["baseline"] == 8
    assert saved["total"]["baseline"] == 8 * len(scan.MODULE_KEYS)
    assert saved["_original_r44_baseline"] == 954  # 元数据保留


def test_ratchet_allows_module_rise_when_total_non_increasing(monkeypatch, tmp_path):
    """模块可升降,只要 total 非增加(允许模块间再平衡)。"""
    tmp_baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_baseline)
    baseline = _synthetic_baseline()  # 每模块 10, total=90
    counts = {m: 10 for m in scan.MODULE_KEYS}
    counts["bots/up_bot.py"] = 12   # 升 2
    counts["services/"] = 5          # 降 5 → total 净降 3
    assert scan.cmd_ratchet(counts, baseline) == 0
    saved = json.loads(tmp_baseline.read_text(encoding="utf-8"))
    # R48: 新格式 modules 在 "modules" 下
    assert saved["modules"]["bots/up_bot.py"]["baseline"] == 12   # 上升被接受
    assert saved["modules"]["services/"]["baseline"] == 5
    assert saved["total"]["baseline"] == sum(counts.values())


def test_ratchet_allows_rebalance_when_total_equal(monkeypatch, tmp_path):
    """total 持平(非增加)的再平衡允许更新(≤ 语义)。"""
    tmp_baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_baseline)
    baseline = _synthetic_baseline()
    counts = _counts_from_baseline(baseline)
    counts["bots/up_bot.py"] += 1
    counts["services/"] -= 1  # total 持平
    assert scan.cmd_ratchet(counts, baseline) == 0


def test_ratchet_fails_when_baseline_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
    counts = _counts_from_baseline(_synthetic_baseline())
    assert scan.cmd_ratchet(counts, {}) == 1


# ---------------------------------------------------------------------------
# 4. 清零目标计算 & 计数去重
# ---------------------------------------------------------------------------
def test_clear_to_zero_gap_equals_current():
    """距清零目标(0)的差距 = 模块当前计数。"""
    baseline = _synthetic_baseline()
    counts = _counts_from_baseline(baseline)
    for m in scan.MODULE_KEYS:
        # 清零目标 0;差距 = 当前计数
        assert counts[m] >= 0
        gap_to_zero = counts[m]
        assert gap_to_zero == scan._baseline_module_count(baseline, m)  # 持平时


def test_count_by_module_dedups_same_content_in_file():
    """同一文件内相同内容多次出现只计一次(file::content 去重)。"""
    findings = [
        ("bots/up_bot.py", 1, "p", "你好"),
        ("bots/up_bot.py", 2, "p", "你好"),   # 同 file 同 content → 去重
        ("bots/up_bot.py", 3, "p", "再见"),
        ("services/errors.py", 4, "p", "错误"),
        ("services/errors.py", 5, "p", "错误"),  # 去重
    ]
    counts = scan.count_by_module(findings)
    assert counts["bots/up_bot.py"] == 2   # 你好 / 再见
    assert counts["services/"] == 1        # 错误


def test_count_by_module_does_not_dedup_across_files():
    """不同文件相同内容分别计数(键含文件路径)。"""
    findings = [
        ("bots/up_bot.py", 1, "p", "你好"),
        ("bots/idx_bot.py", 1, "p", "你好"),   # 不同文件 → 分别计
    ]
    counts = scan.count_by_module(findings)
    assert counts["bots/up_bot.py"] == 1
    assert counts["bots/idx_bot.py"] == 1


def test_count_by_module_sum_equals_global_unique():
    """各模块计数之和 = 全局去重总数(键含文件路径,无跨模块重叠)。"""
    findings = [
        ("bots/up_bot.py", 1, "p", "a"),
        ("bots/idx_bot.py", 1, "p", "a"),
        ("bots/admin_bot/handlers.py", 1, "p", "b"),
        ("services/x.py", 1, "p", "c"),
        ("admin/templates/f.html", 1, "html_text", "d"),
    ]
    counts = scan.count_by_module(findings)
    assert sum(counts.values()) == 5


def test_file_counts_sorted_desc():
    """file_counts 按违规数降序返回。"""
    findings = [
        ("a.py", 1, "p", "x"),
        ("a.py", 2, "p", "y"),   # a.py: 2
        ("b.py", 1, "p", "z"),   # b.py: 1
    ]
    fc = scan.file_counts(findings)
    assert fc[0] == ("a.py", 2)
    assert fc[1] == ("b.py", 1)


# ---------------------------------------------------------------------------
# 5. --generate-baseline 写入
# ---------------------------------------------------------------------------
def test_save_module_baseline_writes_json(monkeypatch, tmp_path):
    """_save_module_baseline 写入可序列化 JSON,total = 各模块之和。

    R48: 新格式 modules 在 "modules" 下,total 为 dict(含 baseline/target/user_visible/log_only)。
    """
    tmp_baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_baseline)
    counts = {m: 0 for m in scan.MODULE_KEYS}
    counts["bots/up_bot.py"] = 3
    counts["services/"] = 7
    scan._save_module_baseline(counts)
    saved = json.loads(tmp_baseline.read_text(encoding="utf-8"))
    # R48: 新格式 modules 嵌套在 "modules" 下
    assert saved["modules"]["bots/up_bot.py"]["baseline"] == 3
    assert saved["modules"]["services/"]["baseline"] == 7
    assert saved["total"]["baseline"] == 10
    assert saved["_original_r44_baseline"] == 954
    assert saved["_description"]  # 元数据存在
    # R48: 新增字段
    assert saved["scanner_version"] == scan.SCANNER_VERSION
    assert saved["included_paths"] == scan.INCLUDED_PATHS
    assert "last_updated" in saved
    assert "last_updated_by" in saved
