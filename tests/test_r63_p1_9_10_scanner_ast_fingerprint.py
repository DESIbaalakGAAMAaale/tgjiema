"""R63 P1-09 + P1-10: scanner 降级声明 + AST 结构指纹 + 按模块分类 allowlist 测试。

审计背景:
  P1-09: 硬编码字符串 scanner 的 cross-function 分析(R62 P1-05)宣称"完整
         source-to-sink 分析",但实际是 best-effort 启发式(最大回溯深度 3,
         仅函数内部 var_map)。需降级为"补充门禁(supplementary gate)"。
  P1-10: error_protocol scanner 的违规指纹使用 ``file:line:violation_type:context``,
         行号变化即导致指纹全变(不稳定)。281 条 allowlist 条目全部使用相同的
         owner/ticket/expiry,无法定位责任。需改为 AST 结构指纹 + 按模块分类。

测试覆盖矩阵:
  P1-09(scanner 降级):
    1. test_scanner_output_includes_supplementary_gate_disclaimer
       — scan_hardcoded_strings 输出包含 "supplementary gate" disclaimer
    2. test_completeness_disclaimer_constant_defined
       — COMPLETENESS_DISCLAIMER 常量已定义且包含关键短语
    3. test_completeness_disclaimer_flag_accepted
       — --completeness-disclaimer flag 被 argparse 接受(不报错)

  P1-10(AST 结构指纹):
    4. test_fingerprint_stable_across_line_shift
       — 添加空行后行号变化 → 指纹不变(核心稳定性)
    5. test_fingerprint_changes_when_violation_pattern_changes
       — "except Exception: pass" → "except Exception: return 0" 指纹变化
    6. test_fingerprint_changes_when_function_rename
       — 函数重命名 → 指纹变化(函数名是结构上下文的一部分)
    7. test_fingerprint_deterministic_ast
       — 相同 AST 结构 → 相同指纹(确定性)
    8. test_enclosing_function_name_resolution
       — AST 正确解析包裹函数名(含类方法 ClassName.method)

  P1-10(按模块分类 allowlist):
    9. test_allowlist_entry_has_categorization_fields
       — _build_allowlist_entry 返回 owner/root_cause/ticket/plan 字段
   10. test_allowlist_per_module_categorization
       — 不同模块(admin/services/bots/database)有不同的 ticket
   11. test_expired_allowlist_entry_rejected
       — 过期 expiry 的 allowlist 条目被拒绝(real_violations 增加)
   12. test_real_baseline_uses_ast_fingerprints
       — 真实 baseline 所有条目指纹为 64 字符 sha256,且 expiry=2026-08-18
   13. test_real_baseline_has_per_module_categorization
       — 真实 baseline allowlist 条目按模块分类(ticket 字段)
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

# ── 让测试能导入 scripts/ 下的模块 ──
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_error_protocol as scanner  # noqa: E402
import scan_hardcoded_strings as i18n_scan  # noqa: E402

# 真实 baseline 文件路径(用于集成测试)
REAL_BASELINE = SCRIPTS_DIR / "error_protocol_baseline.json"


# ════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════
def _clear_ast_cache() -> None:
    """清除 scanner 的 AST 函数映射缓存(测试间隔离)。"""
    scanner._AST_FUNC_CACHE.clear()


def _write_temp_module(tmp_path: Path, name: str, content: str) -> Path:
    """在 tmp_path 下写入一个临时 Python 模块,返回绝对路径。"""
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _compute_fp_for_file(
    abs_path: Path, line_no: int, violation_type: str,
) -> str:
    """对指定文件的指定行计算 R63 P1-10 AST 结构指纹。

    使用绝对路径(避免与 REPO_ROOT 相对路径混淆),清除缓存确保读取最新内容。
    """
    _clear_ast_cache()
    file_path = str(abs_path)
    context = scanner._compute_structural_context(file_path, line_no)
    return scanner._compute_violation_fingerprint(
        file_path, line_no, violation_type, context,
    )


# ════════════════════════════════════════════════════════════
# P1-09: scanner 降级声明(supplementary gate disclaimer)
# ════════════════════════════════════════════════════════════
class TestP1_09_ScannerDowngrade:
    """R63 P1-09: scan_hardcoded_strings 降级为补充门禁。"""

    def test_completeness_disclaimer_constant_defined(self):
        """COMPLETENESS_DISCLAIMER 常量已定义且包含关键降级声明短语。"""
        assert hasattr(i18n_scan, "COMPLETENESS_DISCLAIMER"), (
            "scan_hardcoded_strings 应定义 COMPLETENESS_DISCLAIMER 常量"
        )
        text = i18n_scan.COMPLETENESS_DISCLAIMER
        # 必须包含的关键短语(降级声明)
        assert "supplementary gate" in text, (
            f"disclaimer 应包含 'supplementary gate': {text!r}"
        )
        assert "NOT a complete" in text, (
            f"disclaimer 应声明 'NOT a complete' source-to-sink: {text!r}"
        )
        # 推荐完整 taint 分析工具
        assert "CodeQL" in text or "Semgrep" in text or "pyright" in text, (
            f"disclaimer 应推荐 CodeQL/Semgrep/pyright: {text!r}"
        )

    def test_scanner_output_includes_supplementary_gate_disclaimer(self):
        """scanner 运行时(--completeness-disclaimer)输出包含 disclaimer。"""
        result = subprocess.run(
            [sys.executable, "scripts/scan_hardcoded_strings.py",
             "--completeness-disclaimer"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=60,
        )
        output = result.stdout + result.stderr
        assert "supplementary gate" in output, (
            f"scanner 输出应包含 'supplementary gate' disclaimer。"
            f"\nstdout: {result.stdout[:500]}\nstderr: {result.stderr[:500]}"
        )
        assert "NOT a complete" in output, (
            f"scanner 输出应声明 'NOT a complete' source-to-sink。"
            f"\nstdout: {result.stdout[:500]}"
        )

    def test_completeness_disclaimer_flag_accepted(self):
        """--completeness-disclaimer flag 被 argparse 接受(不报 unrecognized)。"""
        result = subprocess.run(
            [sys.executable, "scripts/scan_hardcoded_strings.py",
             "--completeness-disclaimer", "--help"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=30,
        )
        # --help 会列出所有 flags,exit code 0
        assert result.returncode == 0, f"--help 应 exit 0: {result.stderr[:300]}"
        assert "--completeness-disclaimer" in result.stdout, (
            "--completeness-disclaimer 应出现在 --help 输出中"
        )

    def test_disclaimer_always_printed_without_flag(self):
        """即使不传 --completeness-disclaimer,disclaimer 也始终打印(R63 P1-09)。"""
        result = subprocess.run(
            [sys.executable, "scripts/scan_hardcoded_strings.py", "--check"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
            timeout=60,
        )
        output = result.stdout + result.stderr
        assert "supplementary gate" in output, (
            "disclaimer 应在无 flag 时也打印(始终输出)"
        )


# ════════════════════════════════════════════════════════════
# P1-10: AST 结构指纹(稳定性 + 变化检测)
# ════════════════════════════════════════════════════════════
class TestP1_10_ASTFingerprint:
    """R63 P1-10: AST 结构指纹 — 行号变化指纹不变,结构变化指纹变。"""

    def test_fingerprint_stable_across_line_shift(self, tmp_path):
        """核心用例:添加空行后行号变化 → 指纹不变。

        场景: 函数 foo 中有一个 except Exception: pass 违规(行 N)。
              在文件顶部添加一个空行后,违规移到行 N+1。
        期望: 两个指纹相同(行号不参与指纹,函数名+源行内容不变)。

        注意: 必须写入同一文件路径(指纹包含 file_path),仅行号不同。
        """
        # 原始文件(except Exception: 在 line 4)
        original = (
            "def foo():\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"   # line 4 — ExceptHandler lineno
            "        pass\n"
        )
        # 添加空行后(except Exception: 移到 line 5)
        shifted = "\n" + original

        # 写入同一文件路径(确保 file_path 相同,仅行号不同)
        p = tmp_path / "mod_same.py"
        p.write_text(original, encoding="utf-8")
        _clear_ast_cache()
        violation_type = "P1-5 规则1/2"
        fp1 = _compute_fp_for_file(p, 4, violation_type)

        # 覆盖为 shifted 版本(同一文件路径),清除缓存确保重新读取
        p.write_text(shifted, encoding="utf-8")
        _clear_ast_cache()
        fp2 = _compute_fp_for_file(p, 5, violation_type)

        assert fp1 == fp2, (
            f"行号变化(4→5)不应改变指纹(AST 结构相同):\n"
            f"  fp1={fp1} (line 4)\n"
            f"  fp2={fp2} (line 5)"
        )
        assert len(fp1) == 64, f"sha256 hex 应为 64 字符,实际: {len(fp1)}"

    def test_fingerprint_changes_when_violation_pattern_changes(
        self, tmp_path,
    ):
        """违规模式变化 → 指纹变化。

        场景: 同一函数 foo,两个文件:
              - mod_pass.py: except Exception: pass (Rule 1/2)
              - mod_return.py: except Exception: return 0 (Rule 3)
        期望: 指纹不同(violation_type 不同 + 源行内容不同)。
        """
        src_pass = (
            "def foo():\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"
            "        pass\n"            # Rule 1/2: except pass
        )
        src_return = (
            "def foo():\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"
            "        return 0\n"         # Rule 3: return 0
        )
        p_pass = _write_temp_module(tmp_path, "mod_pass.py", src_pass)
        p_return = _write_temp_module(tmp_path, "mod_return.py", src_return)

        # Rule 1/2 vs Rule 3 — violation_type 不同
        fp_pass = _compute_fp_for_file(p_pass, 4, "P1-5 规则1/2")
        fp_return = _compute_fp_for_file(p_return, 5, "P1-5 规则3")

        assert fp_pass != fp_return, (
            f"违规模式变化(pass vs return 0)应导致指纹变化:\n"
            f"  fp_pass={fp_pass}\n"
            f"  fp_return={fp_return}"
        )

    def test_fingerprint_changes_when_violation_pattern_changes_same_rule(
        self, tmp_path,
    ):
        """同一规则但源行内容变化 → 指纹变化。

        场景: 同一函数 foo,同一规则(Rule 5: raise ValueError 携带字符串),
              但字符串内容不同("foo" vs "bar")。
        期望: 指纹不同(源行内容不同 → 结构上下文不同)。
        """
        src_a = (
            "def foo():\n"
            "    raise ValueError(\"foo\")\n"
        )
        src_b = (
            "def foo():\n"
            "    raise ValueError(\"bar\")\n"
        )
        p_a = _write_temp_module(tmp_path, "mod_a.py", src_a)
        p_b = _write_temp_module(tmp_path, "mod_b.py", src_b)

        fp_a = _compute_fp_for_file(p_a, 2, "P1-5 规则5")
        fp_b = _compute_fp_for_file(p_b, 2, "P1-5 规则5")

        assert fp_a != fp_b, (
            f"源行内容变化(ValueError('foo') vs ValueError('bar'))"
            f"应导致指纹变化:\n  fp_a={fp_a}\n  fp_b={fp_b}"
        )

    def test_fingerprint_changes_when_function_rename(self, tmp_path):
        """函数重命名 → 指纹变化(函数名是结构上下文的一部分)。

        场景: 两个文件,同样违规(except pass),但函数名不同(foo vs bar)。
        期望: 指纹不同(结构上下文中的函数名不同)。
        """
        src_foo = (
            "def foo():\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"
            "        pass\n"
        )
        src_bar = (
            "def bar():\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"
            "        pass\n"
        )
        p_foo = _write_temp_module(tmp_path, "mod_foo.py", src_foo)
        p_bar = _write_temp_module(tmp_path, "mod_bar.py", src_bar)

        fp_foo = _compute_fp_for_file(p_foo, 4, "P1-5 规则1/2")
        fp_bar = _compute_fp_for_file(p_bar, 4, "P1-5 规则1/2")

        assert fp_foo != fp_bar, (
            f"函数重命名(foo vs bar)应导致指纹变化:\n"
            f"  fp_foo={fp_foo}\n  fp_bar={fp_bar}"
        )

    def test_fingerprint_deterministic_ast(self, tmp_path):
        """相同 AST 结构 → 相同指纹(确定性)。"""
        src = (
            "def handler():\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"
            "        pass\n"
        )
        p = _write_temp_module(tmp_path, "mod_det.py", src)

        fp1 = _compute_fp_for_file(p, 4, "P1-5 规则1/2")
        fp2 = _compute_fp_for_file(p, 4, "P1-5 规则1/2")

        assert fp1 == fp2, "相同 AST 结构必须产生相同指纹(确定性)"
        assert len(fp1) == 64

    def test_fingerprint_changes_when_file_path_changes(self, tmp_path):
        """文件路径变化 → 指纹变化(file_path 是指纹的一部分)。"""
        src = (
            "def foo():\n"
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"
            "        pass\n"
        )
        p1 = _write_temp_module(tmp_path, "mod_path1.py", src)
        p2 = _write_temp_module(tmp_path, "mod_path2.py", src)

        fp1 = _compute_fp_for_file(p1, 4, "P1-5 规则1/2")
        fp2 = _compute_fp_for_file(p2, 4, "P1-5 规则1/2")

        assert fp1 != fp2, "文件路径变化应导致指纹变化"

    def test_enclosing_function_name_resolution(self, tmp_path):
        """AST 正确解析包裹函数名(含类方法 ClassName.method)。"""
        src = (
            "class MyClass:\n"
            "    def my_method(self):\n"           # line 2, end line 5
            "        try:\n"
            "            pass\n"
            "        except Exception:\n"          # line 5
            "            pass\n"
            "\n"
            "def top_level_func():\n"              # line 8
            "    try:\n"
            "        pass\n"
            "    except Exception:\n"              # line 11
            "        pass\n"
        )
        p = _write_temp_module(tmp_path, "mod_func.py", src)

        _clear_ast_cache()
        file_path = str(p)

        # line 5 在 MyClass.my_method 内
        func_name_5 = scanner._get_enclosing_function_name(file_path, 5)
        assert func_name_5 == "MyClass.my_method", (
            f"line 5 应在 MyClass.my_method 内,实际: {func_name_5!r}"
        )

        # line 11 在 top_level_func 内
        func_name_11 = scanner._get_enclosing_function_name(file_path, 11)
        assert func_name_11 == "top_level_func", (
            f"line 11 应在 top_level_func 内,实际: {func_name_11!r}"
        )

        # line 1 在类定义外(模块级),不在任何函数内
        func_name_1 = scanner._get_enclosing_function_name(file_path, 1)
        assert func_name_1 == "<module>", (
            f"line 1 应为 <module>,实际: {func_name_1!r}"
        )

    def test_line_number_not_in_fingerprint_raw(self):
        """验证 _compute_violation_fingerprint 不使用 line_no。

        直接调用指纹函数,传入不同 line_no 但相同其他参数,
        指纹应相同(证明 line_no 被忽略)。
        """
        fp_line_100 = scanner._compute_violation_fingerprint(
            "services/foo.py", 100, "P1-5 规则3", "handler|return False",
        )
        fp_line_101 = scanner._compute_violation_fingerprint(
            "services/foo.py", 101, "P1-5 规则3", "handler|return False",
        )
        fp_line_999 = scanner._compute_violation_fingerprint(
            "services/foo.py", 999, "P1-5 规则3", "handler|return False",
        )

        assert fp_line_100 == fp_line_101 == fp_line_999, (
            "line_no 不应影响指纹(R63 P1-10: 行号不参与指纹计算)"
        )


# ════════════════════════════════════════════════════════════
# P1-10: 按模块分类 allowlist
# ════════════════════════════════════════════════════════════
class TestP1_10_ModuleCategorization:
    """R63 P1-10: allowlist 条目按模块分类(owner/root_cause/ticket/plan)。"""

    def test_allowlist_entry_has_categorization_fields(self):
        """_build_allowlist_entry 返回包含 owner/root_cause/ticket/plan 字段。"""
        entry = scanner._build_allowlist_entry(
            "services/foo.py", 42,
            "P1-5 规则3: except 块中 return 0/False",
        )
        required = {"file", "line", "fingerprint", "owner", "reason",
                    "expiry", "ticket", "root_cause", "plan"}
        missing = required - set(entry.keys())
        assert not missing, f"allowlist 条目缺少字段: {missing}\nentry={entry}"

    def test_allowlist_per_module_categorization(self):
        """不同模块(admin/services/bots/database)有不同的 ticket。"""
        modules = [
            ("admin/__init__.py", "R63-P1-10-admin"),
            ("services/i18n.py", "R63-P1-10-services"),
            ("bots/idx_bot.py", "R63-P1-10-bots"),
            ("database/cache.py", "R63-P1-10-database"),
        ]
        for file_path, expected_ticket in modules:
            entry = scanner._build_allowlist_entry(
                file_path, 10, "P1-5 规则1/2: except Exception 后直接 pass",
            )
            assert entry["ticket"] == expected_ticket, (
                f"{file_path} 应分类为 {expected_ticket},"
                f"实际: {entry['ticket']}"
            )
            # root_cause 和 plan 也应非空
            assert entry["root_cause"], f"{file_path} root_cause 不应为空"
            assert entry["plan"], f"{file_path} plan 不应为空"
            assert entry["owner"], f"{file_path} owner 不应为空"

    def test_allowlist_default_expiry_is_2026_08_18(self):
        """默认 expiry 为 2026-08-18(R63 P1-10: 30 天窗口)。"""
        entry = scanner._build_allowlist_entry(
            "services/foo.py", 1, "P1-5 规则1/2: test",
        )
        assert entry["expiry"] == "2026-08-18", (
            f"默认 expiry 应为 '2026-08-18',实际: {entry['expiry']!r}"
        )

    def test_expired_allowlist_entry_rejected(self, tmp_path):
        """过期 expiry 的 allowlist 条目被拒绝(real_violations 增加)。

        场景: 一个违规匹配 allowlist 条目,但 expiry 已过期。
        期望: 检查失败,expired_entries=1,real_violations=1。
        """
        # 构造合成 finding(使用不存在的文件,确保 context 为空 → 可控指纹)
        finding_file = "services/_r63_p1_10_synthetic_expired.py"
        finding = (finding_file, 42, "P1-5 规则3: except 块中 return 0/False")

        # 构造过期 allowlist 条目(使用 _build_allowlist_entry 但手动设过期)
        past_expiry = (date.today() - timedelta(days=1)).isoformat()
        entry = scanner._build_allowlist_entry(
            finding_file, finding[1], finding[2], expiry=past_expiry,
        )

        # 写入 baseline 文件
        baseline = {
            "description": "R63 P1-10 test baseline (expired)",
            "version": "R63-P1-10",
            "domains": {
                "observability": {
                    "description": "test",
                    "max_violations": 0,
                    "allowlist_required": True,
                    "allowlist": [entry],
                },
                "security": {"max_violations": 0, "baseline_violations": 0, "paths": []},
                "destructive": {"max_violations": 0, "baseline_violations": 0, "paths": []},
                "data-integrity": {"max_violations": 0, "baseline_violations": 0, "paths": []},
                "financial": {"max_violations": 0, "baseline_violations": 0, "paths": []},
            },
            "violation_count": 1,
        }
        baseline_path = tmp_path / "test_expired_baseline.json"
        baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2))

        _clear_ast_cache()
        passed, msg, summary = scanner._check_domain_baseline(
            [finding], baseline_path, strict=False,
        )

        assert not passed, "过期 allowlist 条目应导致失败"
        assert "已过期" in msg, f"消息应包含 '已过期': {msg}"
        assert summary["expired_entries"] == 1, "应有 1 条过期"
        assert summary["real_violations"] == 1, "过期后计为真实违规"

    def test_valid_allowlist_entry_accepted(self, tmp_path):
        """有效(未过期)allowlist 条目被接受(real_violations=0)。"""
        finding_file = "services/_r63_p1_10_synthetic_valid.py"
        finding = (finding_file, 42, "P1-5 规则3: except 块中 return 0/False")

        future_expiry = (date.today() + timedelta(days=30)).isoformat()
        entry = scanner._build_allowlist_entry(
            finding_file, finding[1], finding[2], expiry=future_expiry,
        )

        baseline = {
            "description": "R63 P1-10 test baseline (valid)",
            "version": "R63-P1-10",
            "domains": {
                "observability": {
                    "description": "test",
                    "max_violations": 0,
                    "allowlist_required": True,
                    "allowlist": [entry],
                },
                "security": {"max_violations": 0, "baseline_violations": 0, "paths": []},
                "destructive": {"max_violations": 0, "baseline_violations": 0, "paths": []},
                "data-integrity": {"max_violations": 0, "baseline_violations": 0, "paths": []},
                "financial": {"max_violations": 0, "baseline_violations": 0, "paths": []},
            },
            "violation_count": 1,
        }
        baseline_path = tmp_path / "test_valid_baseline.json"
        baseline_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2))

        _clear_ast_cache()
        passed, msg, summary = scanner._check_domain_baseline(
            [finding], baseline_path, strict=False,
        )

        assert passed, f"有效 allowlist 条目应通过: {msg}"
        assert summary["allowlisted"] == 1
        assert summary["real_violations"] == 0
        assert summary["expired_entries"] == 0


# ════════════════════════════════════════════════════════════
# P1-10: 真实 baseline 集成验证
# ════════════════════════════════════════════════════════════
class TestP1_10_RealBaselineIntegration:
    """R63 P1-10: 真实 baseline 集成验证(AST 指纹 + 按模块分类)。"""

    def test_real_baseline_uses_ast_fingerprints(self):
        """真实 baseline 所有条目指纹为 64 字符 sha256,且 expiry=2026-08-18。

        R65 P1-04: observability allowlist 已清空(175 → 0),所有违规已修复。
        allowlist 为空时,本测试验证空状态合规(无条目需校验)。
        """
        if not REAL_BASELINE.exists():
            pytest.skip(f"真实 baseline 不存在: {REAL_BASELINE}")

        data = json.loads(REAL_BASELINE.read_text(encoding="utf-8"))
        allowlist = data.get("domains", {}).get("observability", {}).get("allowlist", [])

        # R65 P1-04: allowlist 已清空(175 → 0) — 空状态为合规终态
        if len(allowlist) == 0:
            # 验证 violation_count 也为 0(allowlist 与 violation_count 应一致)
            assert data.get("violation_count", -1) == 0, (
                "R65 P1-04: allowlist 为空时 violation_count 应为 0,"
                f"实际: {data.get('violation_count')}"
            )
            return

        for i, entry in enumerate(allowlist):
            fp = entry.get("fingerprint", "")
            assert len(fp) == 64, (
                f"条目 {i} ({entry.get('file')}:{entry.get('line')}) "
                f"指纹应为 64 字符 sha256,实际: {len(fp)}"
            )
            # 验证是合法十六进制
            int(fp, 16)
            # expiry 应为 2026-08-18(R63 P1-10 默认)
            assert entry.get("expiry") == "2026-08-18", (
                f"条目 {i} expiry 应为 '2026-08-18',"
                f"实际: {entry.get('expiry')!r}"
            )

    def test_real_baseline_has_per_module_categorization(self):
        """真实 baseline allowlist 条目按模块分类(ticket 字段非统一值)。

        R65 P1-04: observability allowlist 已清空(175 → 0),所有违规已修复。
        allowlist 为空时,本测试验证空状态合规(无条目需分类)。
        """
        if not REAL_BASELINE.exists():
            pytest.skip(f"真实 baseline 不存在: {REAL_BASELINE}")

        data = json.loads(REAL_BASELINE.read_text(encoding="utf-8"))
        allowlist = data.get("domains", {}).get("observability", {}).get("allowlist", [])

        # R65 P1-04: allowlist 已清空(175 → 0) — 空状态为合规终态
        if len(allowlist) == 0:
            assert data.get("violation_count", -1) == 0, (
                "R65 P1-04: allowlist 为空时 violation_count 应为 0,"
                f"实际: {data.get('violation_count')}"
            )
            return

        tickets = set()
        for entry in allowlist:
            ticket = entry.get("ticket", "")
            assert ticket, f"条目 {entry.get('file')} ticket 不应为空"
            tickets.add(ticket)
            # 验证有 root_cause 和 plan 字段
            assert entry.get("root_cause"), (
                f"条目 {entry.get('file')} root_cause 不应为空"
            )
            assert entry.get("plan"), (
                f"条目 {entry.get('file')} plan 不应为空"
            )

        # 应有多个不同的 ticket(按模块分类,不是全部相同)
        assert len(tickets) >= 2, (
            f"allowlist 应按模块分类(至少 2 个不同 ticket),"
            f"实际: {tickets}"
        )
        # 应包含预期的模块 ticket
        expected_prefixes = {"R63-P1-10-admin", "R63-P1-10-services",
                             "R63-P1-10-bots", "R63-P1-10-database"}
        assert tickets & expected_prefixes, (
            f"ticket 应包含 R63-P1-10-<module> 前缀,实际: {tickets}"
        )

    def test_real_baseline_passes_strict_check(self):
        """真实 baseline 通过 strict 检查(所有违规已 allowlist 且未过期)。

        R65 P1-04: observability allowlist 已清空(175 → 0),所有违规已修复。
        findings 为空时(全部修复),strict 检查平凡通过(real_violations=0)。
        """
        if not REAL_BASELINE.exists():
            pytest.skip(f"真实 baseline 不存在: {REAL_BASELINE}")

        _clear_ast_cache()
        findings = scanner.collect_findings()

        # R65 P1-04: 所有违规已修复,findings 为空 — strict 检查平凡通过
        if not findings:
            passed, msg, summary = scanner._check_domain_baseline(
                findings, REAL_BASELINE, strict=True,
            )
            assert passed, (
                f"R65 P1-04: findings 为空时 strict 检查应平凡通过: {msg}"
            )
            assert summary["real_violations"] == 0
            assert summary["expired_entries"] == 0
            return

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
        assert summary["real_violations"] == 0
        assert summary["expired_entries"] == 0

    def test_real_baseline_version_is_r63_p1_10(self):
        """真实 baseline version 字段为 R63-P1-10。"""
        if not REAL_BASELINE.exists():
            pytest.skip(f"真实 baseline 不存在: {REAL_BASELINE}")

        data = json.loads(REAL_BASELINE.read_text(encoding="utf-8"))
        assert data.get("version") == "R63-P1-10", (
            f"baseline version 应为 'R63-P1-10',实际: {data.get('version')!r}"
        )
