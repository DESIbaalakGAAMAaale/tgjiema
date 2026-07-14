"""R48 P1-c: i18n 模块化 baseline scope 审批 / delta / classify 测试。

覆盖 R48 P1-c 五项整改:
1. baseline.json 记录 scanner_version 与 included_paths
2. scanner scope 变化检测(_check_scope_change)
3. --allow-scope-change 参数(--generate-baseline / --ratchet)
4. --delta CI 模式(git base/head 比对,delta > 0 → exit 1)
5. --classify 模式(user_visible / log_only 分类)

额外覆盖:
- --generate-baseline 强制 --reason(缺失 → exit 1)
- 非 master 分支强制 --force(缺失 → exit 1)
- --generate-baseline 输出 "please commit locales/baseline.json separately" 警告
- --check 向后兼容(无参数 / --check 均走 cmd_check)
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

# 让测试能导入 scripts/scan_hardcoded_strings.py
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import scan_hardcoded_strings as scan  # noqa: E402


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _synthetic_baseline_with_scope(**overrides) -> dict:
    """合成 R48 新格式 baseline(含 scanner_version/included_paths/modules/total)。

    默认 included_paths 与 scan.INCLUDED_PATHS 一致(scope 无变化)。
    """
    modules = {
        m: {"baseline": 10, "target": 0, "user_visible": 8, "log_only": 2}
        for m in scan.MODULE_KEYS
    }
    base = {
        "_description": "test baseline",
        "_original_r44_baseline": 954,
        "scanner_version": scan.SCANNER_VERSION,
        "included_paths": list(scan.INCLUDED_PATHS),
        "modules": modules,
        "total": {
            "baseline": 10 * len(scan.MODULE_KEYS),
            "target": 0,
            "user_visible": 8 * len(scan.MODULE_KEYS),
            "log_only": 2 * len(scan.MODULE_KEYS),
        },
        "last_updated": "2026-07-14",
        "last_updated_by": "test",
    }
    base.update(overrides)
    return base


def _counts_from_baseline(baseline: dict) -> dict[str, int]:
    return {m: scan._baseline_module_count(baseline, m) for m in scan.MODULE_KEYS}


def _module_file(m: str) -> str:
    """返回属于模块 m 的合成文件路径(用于构造 findings)。"""
    if m.endswith("/"):
        if m == "admin/templates/":
            return m + "test.html"
        return m + "test.py"
    return m  # bots/up_bot.py 等具体文件模块


def _findings_per_module(count: int, prefix: str = "f") -> list[tuple[str, int, str, str]]:
    """每模块 count 条 findings(不同 content,确保去重后计数 = count)。"""
    findings = []
    for m in scan.MODULE_KEYS:
        file = _module_file(m)
        for i in range(count):
            findings.append((file, i + 1, "p", f"{prefix}_{m}_{i}"))
    return findings


def _patch_collect_and_classify(monkeypatch, counts=None):
    """Patch collect_findings + classify_findings(避免真实扫描,加速测试)。"""
    if counts is None:
        counts = {m: 0 for m in scan.MODULE_KEYS}
    monkeypatch.setattr(scan, "collect_findings", lambda root: [])
    monkeypatch.setattr(
        scan, "classify_findings",
        lambda f, r: {m: {"user_visible": counts[m], "log_only": 0} for m in scan.MODULE_KEYS},
    )


# ===========================================================================
# 1. baseline.json 记录 scanner_version 与 included_paths
# ===========================================================================
class TestScannerVersionAndIncludedPaths:
    """R48 P1-c 要求 1:baseline.json 记录 scanner_version 与 included_paths。"""

    def test_scanner_version_constant_exists(self):
        """SCANNER_VERSION 常量存在且为非空字符串。"""
        assert isinstance(scan.SCANNER_VERSION, str)
        assert scan.SCANNER_VERSION  # 非空

    def test_included_paths_constant_matches_module_keys(self):
        """INCLUDED_PATHS 与 MODULE_KEYS 一致(scope 定义可追溯)。"""
        assert scan.INCLUDED_PATHS == list(scan.MODULE_KEYS)

    def test_real_baseline_has_scanner_version(self):
        """真实 locales/baseline.json 包含 scanner_version 且与当前 scanner 一致。"""
        baseline = scan._load_module_baseline()
        assert baseline, "baseline.json 未生成"
        assert "scanner_version" in baseline
        assert baseline["scanner_version"] == scan.SCANNER_VERSION

    def test_real_baseline_has_included_paths(self):
        """真实 baseline.json 包含 included_paths 且与当前 INCLUDED_PATHS 一致。"""
        baseline = scan._load_module_baseline()
        assert "included_paths" in baseline
        assert isinstance(baseline["included_paths"], list)
        assert baseline["included_paths"] == scan.INCLUDED_PATHS

    def test_save_module_baseline_writes_scanner_version(self, monkeypatch, tmp_path):
        """_save_module_baseline 写入 scanner_version / included_paths / last_updated_by。"""
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        scan._save_module_baseline(
            {m: 0 for m in scan.MODULE_KEYS}, reason="unit_test",
        )
        saved = json.loads((tmp_path / "baseline.json").read_text(encoding="utf-8"))
        assert saved["scanner_version"] == scan.SCANNER_VERSION
        assert saved["included_paths"] == scan.INCLUDED_PATHS
        assert saved["last_updated_by"] == "unit_test"
        assert "last_updated" in saved

    def test_save_module_baseline_with_classify_results(self, monkeypatch, tmp_path):
        """_save_module_baseline 携带 classify_results 时写入 user_visible / log_only。"""
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        counts = {m: 5 for m in scan.MODULE_KEYS}
        classify = {m: {"user_visible": 3, "log_only": 2} for m in scan.MODULE_KEYS}
        scan._save_module_baseline(counts, reason="classify_test", classify_results=classify)
        saved = json.loads((tmp_path / "baseline.json").read_text(encoding="utf-8"))
        for m in scan.MODULE_KEYS:
            assert saved["modules"][m]["baseline"] == 5
            assert saved["modules"][m]["user_visible"] == 3
            assert saved["modules"][m]["log_only"] == 2
            assert saved["modules"][m]["target"] == 0
        assert saved["total"]["user_visible"] == 3 * len(scan.MODULE_KEYS)
        assert saved["total"]["log_only"] == 2 * len(scan.MODULE_KEYS)

    def test_baseline_is_json_serializable(self):
        """真实 baseline.json 可反复序列化/反序列化(跨平台一致性)。"""
        baseline = scan._load_module_baseline()
        s = json.dumps(baseline, ensure_ascii=False)
        again = json.loads(s)
        assert again == baseline


# ===========================================================================
# 2. included_paths 变化检测(_check_scope_change)
# ===========================================================================
class TestScopeChangeDetection:
    """R48 P1-c 要求 2:scanner scope 变化检测。"""

    def test_no_change_when_paths_match(self):
        """baseline included_paths == INCLUDED_PATHS → changed=False。"""
        baseline = {"included_paths": list(scan.INCLUDED_PATHS)}
        changed, added, removed = scan._check_scope_change(baseline)
        assert changed is False
        assert added == []
        assert removed == []

    def test_detects_added_paths(self):
        """baseline 缺少某个 path → changed=True, added 含缺失的 path。"""
        # baseline 的 included_paths 少一个 → 新 scanner 多了(added)
        old_paths = scan.INCLUDED_PATHS[:-1]
        baseline = {"included_paths": old_paths}
        changed, added, removed = scan._check_scope_change(baseline)
        assert changed is True
        assert added == [scan.INCLUDED_PATHS[-1]]
        assert removed == []

    def test_detects_removed_paths(self):
        """baseline 多出 path → changed=True, removed 含多余的 path。"""
        extra = "old/path/that/removed/"
        baseline = {"included_paths": list(scan.INCLUDED_PATHS) + [extra]}
        changed, added, removed = scan._check_scope_change(baseline)
        assert changed is True
        assert added == []
        assert removed == [extra]

    def test_detects_both_added_and_removed(self):
        """同时有增删 → changed=True, added/removed 均非空。"""
        old_paths = scan.INCLUDED_PATHS[:-1] + ["old/removed/"]
        baseline = {"included_paths": old_paths}
        changed, added, removed = scan._check_scope_change(baseline)
        assert changed is True
        assert scan.INCLUDED_PATHS[-1] in added
        assert "old/removed/" in removed

    def test_empty_baseline_no_change(self):
        """空 baseline → changed=False(首次生成不视为 scope 变化)。"""
        changed, added, removed = scan._check_scope_change({})
        assert changed is False
        assert added == []
        assert removed == []

    def test_baseline_without_included_paths_key(self):
        """baseline 无 included_paths 键(旧格式)→ 全部 INCLUDED_PATHS 视为 added。"""
        baseline = {"_description": "old format without included_paths"}
        changed, added, removed = scan._check_scope_change(baseline)
        assert changed is True
        assert added == scan.INCLUDED_PATHS
        assert removed == []

    def test_included_paths_not_list_treated_as_empty(self):
        """included_paths 非 list → 视为空(全部为 added)。"""
        baseline = {"included_paths": "not a list"}
        changed, added, removed = scan._check_scope_change(baseline)
        assert changed is True
        assert added == scan.INCLUDED_PATHS


# ===========================================================================
# 3. --allow-scope-change 参数
# ===========================================================================
class TestAllowScopeChange:
    """R48 P1-c 要求 3:scope 变化需 --allow-scope-change(--ratchet 拒绝 scope 变化)。"""

    def test_generate_baseline_refuses_scope_change_without_flag(
        self, monkeypatch, tmp_path, capsys,
    ):
        """--generate-baseline 无 --allow-scope-change 时 scope 变化 → exit 1。"""
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        monkeypatch.setattr(scan, "_git_current_branch", lambda: "master")
        # baseline 的 included_paths 少一个 → scope 已变化
        baseline = _synthetic_baseline_with_scope(included_paths=scan.INCLUDED_PATHS[:-1])
        counts = _counts_from_baseline(baseline)
        rc = scan.cmd_generate_baseline(
            counts, baseline, tmp_path,
            reason="test", allow_scope_change=False, force=False,
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "scope" in out.lower()
        # baseline 文件未写入(拒绝)
        assert not (tmp_path / "baseline.json").exists()

    def test_generate_baseline_allows_scope_change_with_flag(
        self, monkeypatch, tmp_path, capsys,
    ):
        """--generate-baseline 有 --allow-scope-change 时 scope 变化 → exit 0。"""
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        monkeypatch.setattr(scan, "_git_current_branch", lambda: "master")
        _patch_collect_and_classify(monkeypatch)
        baseline = _synthetic_baseline_with_scope(included_paths=scan.INCLUDED_PATHS[:-1])
        counts = _counts_from_baseline(baseline)
        rc = scan.cmd_generate_baseline(
            counts, baseline, tmp_path,
            reason="scope change", allow_scope_change=True, force=False,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "scope" in out.lower()
        # baseline 已写入
        assert (tmp_path / "baseline.json").exists()

    def test_generate_baseline_no_scope_change_passes_without_flag(
        self, monkeypatch, tmp_path,
    ):
        """scope 不变时 --generate-baseline 无需 --allow-scope-change。"""
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        monkeypatch.setattr(scan, "_git_current_branch", lambda: "master")
        _patch_collect_and_classify(monkeypatch)
        baseline = _synthetic_baseline_with_scope()  # scope 一致
        counts = _counts_from_baseline(baseline)
        rc = scan.cmd_generate_baseline(
            counts, baseline, tmp_path,
            reason="no scope change", allow_scope_change=False, force=False,
        )
        assert rc == 0

    def test_ratchet_refuses_scope_change(self, monkeypatch, tmp_path, capsys):
        """--ratchet 遇 scope 变化 → exit 1(无 --allow-scope-change 选项)。"""
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        baseline = _synthetic_baseline_with_scope(included_paths=scan.INCLUDED_PATHS[:-1])
        counts = _counts_from_baseline(baseline)
        rc = scan.cmd_ratchet(counts, baseline)
        assert rc == 1
        out = capsys.readouterr().out
        assert "scope" in out.lower()
        # baseline 未写入
        assert not (tmp_path / "baseline.json").exists()

    def test_ratchet_passes_when_scope_unchanged(self, monkeypatch, tmp_path):
        """--ratchet scope 不变 + total 下降 → exit 0。"""
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        baseline = _synthetic_baseline_with_scope()  # scope 一致
        counts = {m: 8 for m in scan.MODULE_KEYS}  # total 下降
        rc = scan.cmd_ratchet(counts, baseline)
        assert rc == 0

    def test_argparse_accepts_allow_scope_change_flag(self, monkeypatch):
        """main(argv) 接受 --allow-scope-change 参数(argparse 识别)。"""
        monkeypatch.setattr(scan, "_git_current_branch", lambda: "master")
        _patch_collect_and_classify(monkeypatch)
        # 使用 tempfile.mkdtemp 创建跨平台可写的临时目录(避免 /tmp 在 Windows 上权限拒绝)
        tmp_dir = tempfile.mkdtemp(prefix="r48_p1_c_argparse_")
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", Path(tmp_dir) / "test_baseline_dummy.json")
        # 若 argparse 不识别 --allow-scope-change,会 SystemExit(2)
        rc = scan.main(argv=[
            "--generate-baseline", "--reason", "argparse_test",
            "--allow-scope-change",
        ])
        assert rc == 0


# ===========================================================================
# 4. --delta CI 模式
# ===========================================================================
class TestDeltaMode:
    """R48 P1-c 要求 4:--delta CI 模式(git base/head 比对)。"""

    def test_delta_skips_when_no_git_base(self, monkeypatch, capsys):
        """无 git base commit → 优雅跳过,exit 0(非 git 环境降级)。"""
        monkeypatch.setattr(scan, "_git_base_commit", lambda: None)
        rc = scan.cmd_delta(Path("."))
        assert rc == 0
        out = capsys.readouterr().out
        assert "跳过" in out or "skip" in out.lower()

    def test_delta_fails_when_count_increases(self, monkeypatch, tmp_path, capsys):
        """head 比 base 多 → delta > 0 → exit 1。"""
        monkeypatch.setattr(scan, "_git_base_commit", lambda: "fake_base_commit")
        base_findings = _findings_per_module(1, "base")  # 每模块 1 条
        head_findings = _findings_per_module(2, "head")  # 每模块 2 条(delta +1)
        monkeypatch.setattr(scan, "collect_findings_at_commit", lambda root, commit: base_findings)
        monkeypatch.setattr(scan, "collect_findings", lambda root: head_findings)
        rc = scan.cmd_delta(tmp_path)
        assert rc == 1
        out = capsys.readouterr().out
        assert "增加" in out or "delta > 0" in out

    def test_delta_passes_when_count_decreases(self, monkeypatch, tmp_path):
        """head 比 base 少 → delta < 0 → exit 0。"""
        monkeypatch.setattr(scan, "_git_base_commit", lambda: "fake_base_commit")
        base_findings = _findings_per_module(2, "base")  # 每模块 2 条
        head_findings = _findings_per_module(1, "head")  # 每模块 1 条(delta -1)
        monkeypatch.setattr(scan, "collect_findings_at_commit", lambda root, commit: base_findings)
        monkeypatch.setattr(scan, "collect_findings", lambda root: head_findings)
        rc = scan.cmd_delta(tmp_path)
        assert rc == 0

    def test_delta_passes_when_count_equal(self, monkeypatch, tmp_path):
        """head == base → delta == 0 → exit 0。"""
        monkeypatch.setattr(scan, "_git_base_commit", lambda: "fake_base_commit")
        findings = _findings_per_module(1, "same")
        monkeypatch.setattr(scan, "collect_findings_at_commit", lambda root, commit: findings)
        monkeypatch.setattr(scan, "collect_findings", lambda root: findings)
        rc = scan.cmd_delta(tmp_path)
        assert rc == 0

    def test_delta_fails_when_single_module_increases(self, monkeypatch, tmp_path):
        """单模块 delta > 0(即使 total 也增加)→ exit 1。"""
        monkeypatch.setattr(scan, "_git_base_commit", lambda: "fake_base_commit")
        base_findings = _findings_per_module(1, "base")
        # head: services/ 模块多 1 条,其余持平
        head_findings = _findings_per_module(1, "head")
        extra_file = _module_file("services/")
        head_findings.append((extra_file, 99, "p", "extra_services_increase"))
        monkeypatch.setattr(scan, "collect_findings_at_commit", lambda root, commit: base_findings)
        monkeypatch.setattr(scan, "collect_findings", lambda root: head_findings)
        rc = scan.cmd_delta(tmp_path)
        assert rc == 1

    def test_delta_via_main_argparse(self, monkeypatch):
        """main(argv=['--delta']) 正确分发到 cmd_delta。"""
        monkeypatch.setattr(scan, "_git_base_commit", lambda: None)
        monkeypatch.setattr(scan, "collect_findings", lambda root: [])
        rc = scan.main(argv=["--delta"])
        assert rc == 0

    def test_delta_output_shows_base_head_columns(self, monkeypatch, tmp_path, capsys):
        """--delta 输出含 base/head/delta 列。"""
        monkeypatch.setattr(scan, "_git_base_commit", lambda: "fake_base_commit")
        base_findings = _findings_per_module(1, "base")
        head_findings = _findings_per_module(1, "head")
        monkeypatch.setattr(scan, "collect_findings_at_commit", lambda root, commit: base_findings)
        monkeypatch.setattr(scan, "collect_findings", lambda root: head_findings)
        scan.cmd_delta(tmp_path)
        out = capsys.readouterr().out
        assert "base" in out
        assert "head" in out
        assert "delta" in out


# ===========================================================================
# 5. --classify 模式
# ===========================================================================
class TestClassifyMode:
    """R48 P1-c 要求 5:--classify 模式(user_visible / log_only 分类)。"""

    def test_classify_finding_html_is_user_visible(self):
        """HTML 文件 → user_visible(模板内容对用户可见)。"""
        assert scan.classify_finding("admin/templates/test.html", "<div>测试</div>", 1) == "user_visible"

    def test_classify_finding_python_with_logger_is_log_only(self):
        """Python 文件行附近有 logger. → log_only。"""
        content = "import logging\nlogger = logging.getLogger()\nlogger.info('xxx')\n"
        assert scan.classify_finding("services/test.py", content, 3) == "log_only"

    def test_classify_finding_python_with_print_is_log_only(self):
        """Python 文件行附近有 print( → log_only。"""
        content = "x = 1\nprint('hello')\ny = 2\n"
        assert scan.classify_finding("services/test.py", content, 2) == "log_only"

    def test_classify_finding_python_without_logger_is_user_visible(self):
        """Python 文件行附近无 logger/print → user_visible。"""
        content = "x = 1\nreply_text('你好')\ny = 2\n"
        assert scan.classify_finding("services/test.py", content, 2) == "user_visible"

    def test_classify_finding_checks_nearby_lines(self):
        """finding 行 ±2 范围内有 logger → log_only;超出范围 → user_visible。"""
        content = "a = 1\nlogger.info('log')\nb = 2\nc = 3\nd = 4\n"
        # line 4 (0-indexed 3): logger on line 2 (offset -2) → log_only
        assert scan.classify_finding("services/test.py", content, 4) == "log_only"
        # line 5 (0-indexed 4): logger on line 2 (offset -3, 超出 ±2 范围) → user_visible
        assert scan.classify_finding("services/test.py", content, 5) == "user_visible"

    def test_classify_findings_no_double_counting(self, tmp_path):
        """classify_findings 的 user_visible + log_only 之和 == count_by_module(无双重计数)。"""
        # 创建临时 Python 文件(含 logger 附近和非 logger 附近的 finding)
        (tmp_path / "services").mkdir(parents=True)
        (tmp_path / "services" / "test.py").write_text(
            "x = 1\n"                        # line 1
            "reply_text('用户可见1')\n"        # line 2: finding,附近无 logger → user_visible
            "y = 2\n"                         # line 3
            "z = 3\n"                         # line 4
            "logger.info('xxx')\n"            # line 5: logger 调用(scanner 跳过)
            "answer('日志附近')\n"             # line 6: finding,logger 在 line 5 (offset -1) → log_only
            , encoding="utf-8",
        )
        findings = [
            ("services/test.py", 2, "p", "用户可见1"),
            ("services/test.py", 6, "p", "日志附近"),
        ]
        classified = scan.classify_findings(findings, tmp_path)
        counts = scan.count_by_module(findings)
        for m in scan.MODULE_KEYS:
            uv = classified.get(m, {}).get("user_visible", 0)
            ll = classified.get(m, {}).get("log_only", 0)
            assert uv + ll == counts.get(m, 0), (
                f"{m}: uv({uv}) + ll({ll}) != count({counts.get(m, 0)})"
            )
        # services/ 模块:1 user_visible + 1 log_only = 2
        assert classified["services/"]["user_visible"] == 1
        assert classified["services/"]["log_only"] == 1

    def test_classify_findings_groups_by_file_content(self, tmp_path):
        """同一 (file, content) 多行出现只计一次(任一行附近有 logger → log_only)。"""
        (tmp_path / "services").mkdir(parents=True)
        (tmp_path / "services" / "test.py").write_text(
            "reply_text('重复文本')\n"     # line 1: 无 logger 附近
            "x = 2\n"                      # line 2
            "logger.info('log')\n"         # line 3: logger
            "reply_text('重复文本')\n"     # line 4: logger 在 line 3 (offset -1) → log_only
            , encoding="utf-8",
        )
        findings = [
            ("services/test.py", 1, "p", "重复文本"),
            ("services/test.py", 4, "p", "重复文本"),  # 同 file 同 content
        ]
        classified = scan.classify_findings(findings, tmp_path)
        # 同 (file, content) 只计一次;因 line 4 附近有 logger → log_only
        assert classified["services/"]["user_visible"] == 0
        assert classified["services/"]["log_only"] == 1

    def test_cmd_classify_returns_zero(self, monkeypatch, capsys):
        """cmd_classify 返回 0 并输出分类表。"""
        rc = scan.cmd_classify([], Path("."))
        assert rc == 0
        out = capsys.readouterr().out
        assert "user_visible" in out
        assert "log_only" in out

    def test_cmd_classify_outputs_correct_counts(self, monkeypatch, tmp_path, capsys):
        """cmd_classify 输出的 user_visible + log_only == total(无双重计数)。"""
        (tmp_path / "services").mkdir(parents=True)
        (tmp_path / "services" / "test.py").write_text(
            "reply_text('可见')\n"          # line 1: user_visible
            "logger.info('log')\n"          # line 2: logger (scanner skips)
            "answer('日志附近')\n"           # line 3: log_only (logger nearby)
            , encoding="utf-8",
        )
        findings = [
            ("services/test.py", 1, "p", "可见"),
            ("services/test.py", 3, "p", "日志附近"),
        ]
        rc = scan.cmd_classify(findings, tmp_path)
        assert rc == 0
        out = capsys.readouterr().out
        assert "user_visible" in out
        assert "log_only" in out

    def test_classify_via_main_argparse(self, monkeypatch, capsys):
        """main(argv=['--classify']) 正确分发到 cmd_classify。"""
        monkeypatch.setattr(scan, "collect_findings", lambda root: [])
        rc = scan.main(argv=["--classify"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "user_visible" in out


# ===========================================================================
# 额外:--generate-baseline 强制 --reason / 非 master 强制 --force / 警告输出
# ===========================================================================
class TestGenerateBaselineReasonForceWarning:
    """--generate-baseline 的 --reason、--force、warning 输出。"""

    def test_generate_baseline_requires_reason(self, monkeypatch, capsys):
        """--generate-baseline 无 --reason → exit 1。"""
        monkeypatch.setattr(scan, "_git_current_branch", lambda: "master")
        baseline = _synthetic_baseline_with_scope()
        counts = _counts_from_baseline(baseline)
        rc = scan.cmd_generate_baseline(
            counts, baseline, Path("."),
            reason=None, allow_scope_change=False, force=False,
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "--reason" in out

    def test_generate_baseline_empty_reason_rejected(self, monkeypatch):
        """--generate-baseline reason='' → exit 1(空字符串视为未提供)。"""
        monkeypatch.setattr(scan, "_git_current_branch", lambda: "master")
        baseline = _synthetic_baseline_with_scope()
        counts = _counts_from_baseline(baseline)
        rc = scan.cmd_generate_baseline(
            counts, baseline, Path("."),
            reason="", allow_scope_change=False, force=False,
        )
        assert rc == 1

    def test_generate_baseline_refuses_non_master_without_force(self, monkeypatch, capsys):
        """非 master 分支无 --force → exit 1。"""
        monkeypatch.setattr(scan, "_git_current_branch", lambda: "feature/r48")
        baseline = _synthetic_baseline_with_scope()
        counts = _counts_from_baseline(baseline)
        rc = scan.cmd_generate_baseline(
            counts, baseline, Path("."),
            reason="test", allow_scope_change=False, force=False,
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "--force" in out or "force" in out

    def test_generate_baseline_allows_non_master_with_force(
        self, monkeypatch, tmp_path,
    ):
        """非 master 分支有 --force + scope 不变 → exit 0。"""
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        monkeypatch.setattr(scan, "_git_current_branch", lambda: "feature/r48")
        _patch_collect_and_classify(monkeypatch)
        baseline = _synthetic_baseline_with_scope()
        counts = _counts_from_baseline(baseline)
        rc = scan.cmd_generate_baseline(
            counts, baseline, tmp_path,
            reason="force test", allow_scope_change=False, force=True,
        )
        assert rc == 0

    def test_generate_baseline_master_no_force_needed(self, monkeypatch, tmp_path):
        """master 分支不需要 --force(scope 不变时直接通过)。"""
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        monkeypatch.setattr(scan, "_git_current_branch", lambda: "master")
        _patch_collect_and_classify(monkeypatch)
        baseline = _synthetic_baseline_with_scope()
        counts = _counts_from_baseline(baseline)
        rc = scan.cmd_generate_baseline(
            counts, baseline, tmp_path,
            reason="master test", allow_scope_change=False, force=False,
        )
        assert rc == 0

    def test_generate_baseline_warns_commit_separately(
        self, monkeypatch, tmp_path, capsys,
    ):
        """--generate-baseline 成功时输出 'please commit locales/baseline.json separately'。"""
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        monkeypatch.setattr(scan, "_git_current_branch", lambda: "master")
        _patch_collect_and_classify(monkeypatch)
        baseline = _synthetic_baseline_with_scope()
        counts = _counts_from_baseline(baseline)
        rc = scan.cmd_generate_baseline(
            counts, baseline, tmp_path,
            reason="warning test", allow_scope_change=False, force=False,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "please commit locales/baseline.json separately" in out

    def test_generate_baseline_reason_via_main_argparse(self, monkeypatch):
        """main(argv) 无 --reason 时 --generate-baseline → exit 1。"""
        monkeypatch.setattr(scan, "_git_current_branch", lambda: "master")
        monkeypatch.setattr(scan, "collect_findings", lambda root: [])
        rc = scan.main(argv=["--generate-baseline"])  # 无 --reason
        assert rc == 1


# ===========================================================================
# 额外:--check 向后兼容
# ===========================================================================
class TestCheckBackwardCompat:
    """--check 向后兼容(CI 默认行为不变)。"""

    def test_check_is_default_behavior(self, capsys):
        """无参数调用 main() 等价于 --check(真实 baseline 通过)。"""
        rc = scan.main(argv=[])
        assert rc == 0  # 真实 baseline 通过 --check

    def test_check_explicit_flag(self, capsys):
        """--check 显式调用,exit 0(真实 baseline 通过)。"""
        rc = scan.main(argv=["--check"])
        assert rc == 0

    def test_check_output_contains_scanner_version(self, capsys):
        """--check 输出包含 scanner_version 和 included_paths 信息(R48 新增)。"""
        scan.main(argv=["--check"])
        out = capsys.readouterr().out
        assert "scanner_version" in out
        assert "included_paths" in out

    def test_check_output_contains_target_column(self, capsys):
        """--check 输出包含 target 列(R48 新增,target=0)。"""
        scan.main(argv=["--check"])
        out = capsys.readouterr().out
        assert "target" in out
        assert "距target" in out
