"""R66 P1-09: skip inventory 生成器测试。

被测对象:
    ``scripts/collect_skip_inventory.py`` — pytest skip 标记收集器

测试覆盖矩阵:
    A. 类别推断(infer_category)
       - 每个类别至少一个用例(telethon_dependency / crdb_dependency /
         temporarily_disabled / historical_legacy / dependency_missing / uncategorized)
       - 优先级测试:Telethon + aiosqlite → telethon_dependency(不归为 dependency_missing)
       - 优先级测试:Telegram reason + "legacy" 文件 → telethon_dependency(不归为历史问题)
    B. 生产影响推断(infer_production_impact)
       - high:bots/ / services/restore* / scripts/check_* / .github/workflows*
       - medium:services/foo / database/foo
       - low:其它
    C. Owner 推断(infer_owner)
       - bots/ → bot-team
       - services/restore* → restore-team
       - database/migrate → db-team
       - 未匹配路径 → unassigned
    D. 日期提取(extract_due_date)
       - "2025-12-31" → "2025-12-31"
       - "by 2025" / "by 2025-12" / "by 2025-12-31"
       - "2025年12月31日" → "2025-12-31"
       - 无日期 → 空字符串
    E. AST 解析(collect_skips_from_source)
       - @pytest.mark.skip(reason=...) 装饰器
       - @pytest.mark.skipif(condition, reason=...) 装饰器
       - pytest.skip("...") 调用
       - 模块级 pytestmark = pytest.mark.skipif(...)
       - 无 skip 标记 → 空列表
       - 类级装饰器(全类 skip)
    F. 汇总统计(build_summary)
       - total 正确
       - by_category / by_production_impact / by_owner 正确
       - missing_owner / missing_due_date / critical_path_missing_due_date 正确
    G. JSON 输出(output_inventory)
       - 文件输出 + stdout 输出
       - JSON 结构包含 description / generated_at / summary / skips
       - skips 条目含全部字段

R66 P1-09 严格要求:
    "涉及 Telegram 主链、恢复、密钥、部署的 skip 不得归为无关历史问题"
    → 类别优先级 telethon_dependency > historical_legacy,确保 Telegram skip
       即便 reason 含 "legacy" 字样也归为 telethon_dependency。
"""
from __future__ import annotations

import ast
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


# ════════════════════════════════════════════════════════════════
# 测试辅助:导入被测模块
# ════════════════════════════════════════════════════════════════


def _import_collect_mod():
    """导入 collect_skip_inventory 模块(直接从 scripts/ 路径加载)。

    使用 importlib 从文件路径加载。需先将模块注册到 sys.modules,
    否则 @dataclass 装饰器在解析类型注解时会因 cls.__module__ 未注册失败。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "collect_skip_inventory",
        REPO_ROOT / "scripts" / "collect_skip_inventory.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["collect_skip_inventory"] = mod
    spec.loader.exec_module(mod)
    return mod


COLLECT = _import_collect_mod()


# ════════════════════════════════════════════════════════════════
# A. 类别推断(infer_category)
# ════════════════════════════════════════════════════════════════


class TestInferCategory:
    """R66 P1-09: infer_category 关键词推断测试。"""

    def test_telethon_dependency(self):
        """包含 Telethon → telethon_dependency。"""
        assert COLLECT.infer_category("Telethon 不可用") == "telethon_dependency"

    def test_telethon_dependency_lowercase(self):
        """包含 telethon(小写)→ telethon_dependency。"""
        assert COLLECT.infer_category("telethon 库未安装") == "telethon_dependency"

    def test_telegram_dependency(self):
        """包含 telegram → telethon_dependency(Telegram 主链)。"""
        assert COLLECT.infer_category("telegram bot 依赖") == "telethon_dependency"

    def test_crdb_dependency(self):
        """包含 CRDB → crdb_dependency。"""
        assert COLLECT.infer_category("CRDB 集群未就绪") == "crdb_dependency"

    def test_crdb_dependency_cockroachdb(self):
        """包含 CockroachDB → crdb_dependency。"""
        assert COLLECT.infer_category("CockroachDB connection failed") == "crdb_dependency"

    def test_temporarily_disabled_todo(self):
        """包含 TODO → temporarily_disabled。"""
        assert COLLECT.infer_category("TODO: 修复后启用") == "temporarily_disabled"

    def test_temporarily_disabled_fixme(self):
        """包含 FIXME → temporarily_disabled。"""
        assert COLLECT.infer_category("FIXME: 暂时禁用") == "temporarily_disabled"

    def test_historical_legacy(self):
        """包含 legacy → historical_legacy。"""
        assert COLLECT.infer_category("legacy 历史测试,待清理") == "historical_legacy"

    def test_dependency_missing_aiosqlite(self):
        """包含 aiosqlite → dependency_missing。"""
        assert COLLECT.infer_category("aiosqlite 未安装") == "dependency_missing"

    def test_dependency_missing_asyncpg(self):
        """包含 asyncpg → dependency_missing。"""
        assert COLLECT.infer_category("asyncpg 不可用") == "dependency_missing"

    def test_uncategorized_default(self):
        """无任何匹配关键词 → uncategorized。"""
        assert COLLECT.infer_category("随机原因,无关键词") == "uncategorized"

    def test_empty_reason_is_uncategorized(self):
        """空 reason → uncategorized。"""
        assert COLLECT.infer_category("") == "uncategorized"

    def test_priority_telethon_over_dependency_missing(self):
        """R66 P1-09 关键:Telethon + aiosqlite → telethon_dependency(不归为 dependency_missing)。

        验证 telethon_dependency 优先级高于 dependency_missing。
        """
        reason = "Telethon 与 aiosqlite 都不可用"
        assert COLLECT.infer_category(reason) == "telethon_dependency"

    def test_priority_telethon_over_historical_legacy(self):
        """R66 P1-09 关键:Telegram reason 即便含 'legacy' 也归为 telethon_dependency。

        验证 telethon_dependency 优先级高于 historical_legacy。
        这防止 Telegram 主链 skip 被误归为"无关历史问题"。
        """
        reason = "telegram legacy adapter,历史遗留"
        assert COLLECT.infer_category(reason) == "telethon_dependency"


# ════════════════════════════════════════════════════════════════
# B. 生产影响推断(infer_production_impact)
# ════════════════════════════════════════════════════════════════


class TestInferProductionImpact:
    """R66 P1-09: infer_production_impact 路径推断测试。"""

    def test_bots_high(self):
        """bots/ → high(主链 Bot)。"""
        assert COLLECT.infer_production_impact("bots/admin_bot/handlers.py") == "high"

    def test_services_restore_high(self):
        """services/restore* → high(恢复链)。"""
        assert COLLECT.infer_production_impact("services/restore_orchestrator.py") == "high"

    def test_services_db_backup_high(self):
        """services/db_backup → high(备份链)。"""
        assert COLLECT.infer_production_impact("services/db_backup.py") == "high"

    def test_services_command_bus_high(self):
        """services/command_bus → high(命令总线)。"""
        assert COLLECT.infer_production_impact("services/command_bus.py") == "high"

    def test_services_approval_high(self):
        """services/approval* → high(审批链)。"""
        assert COLLECT.infer_production_impact("services/approval_executor.py") == "high"

    def test_services_mfa_high(self):
        """services/mfa* → high(密钥链)。"""
        assert COLLECT.infer_production_impact("services/mfa_verifier.py") == "high"

    def test_database_migrate_high(self):
        """database/migrate → high(迁移)。"""
        assert COLLECT.infer_production_impact("database/migrate.py") == "high"

    def test_scripts_check_high(self):
        """scripts/check_* → high(门禁脚本)。"""
        assert COLLECT.infer_production_impact("scripts/check_restore_no_skip.py") == "high"

    def test_github_workflows_high(self):
        """.github/workflows* → high(CI 部署)。"""
        assert COLLECT.infer_production_impact(".github/workflows/release-gates.yml") == "high"

    def test_services_medium(self):
        """services/ 下非 high 路径 → medium。"""
        assert COLLECT.infer_production_impact("services/notifications.py") == "medium"

    def test_database_medium(self):
        """database/ 下非 migrate 路径 → medium。"""
        assert COLLECT.infer_production_impact("database/cache.py") == "medium"

    def test_low_default(self):
        """其它路径 → low。"""
        assert COLLECT.infer_production_impact("utils/flood_waiter.py") == "low"


# ════════════════════════════════════════════════════════════════
# C. Owner 推断(infer_owner)
# ════════════════════════════════════════════════════════════════


class TestInferOwner:
    """R66 P1-09: infer_owner 路径推断测试。"""

    def test_bots_team(self):
        """bots/ → bot-team。"""
        assert COLLECT.infer_owner("bots/admin_bot/handlers.py") == "bot-team"

    def test_restore_team(self):
        """services/restore* → restore-team。"""
        assert COLLECT.infer_owner("services/restore_orchestrator.py") == "restore-team"

    def test_backup_team(self):
        """services/backup* → backup-team。"""
        assert COLLECT.infer_owner("services/backup_engine.py") == "backup-team"

    def test_db_team_database_migrate(self):
        """database/migrate → db-team。"""
        assert COLLECT.infer_owner("database/migrate.py") == "db-team"

    def test_db_team_database_other(self):
        """database/ 其它路径 → db-team。"""
        assert COLLECT.infer_owner("database/cache.py") == "db-team"

    def test_commandbus_team(self):
        """services/command_bus → commandbus-team。"""
        assert COLLECT.infer_owner("services/command_bus.py") == "commandbus-team"

    def test_approval_team(self):
        """services/approval* → approval-team。"""
        assert COLLECT.infer_owner("services/approval_workflow.py") == "approval-team"

    def test_mfa_team(self):
        """services/mfa* → mfa-team。"""
        assert COLLECT.infer_owner("services/mfa_verifier.py") == "mfa-team"

    def test_platform_team_scripts_check(self):
        """scripts/check_* → platform-team。"""
        assert COLLECT.infer_owner("scripts/check_schema.py") == "platform-team"

    def test_platform_team_github_workflows(self):
        """.github/workflows* → platform-team。"""
        assert COLLECT.infer_owner(".github/workflows/ci.yml") == "platform-team"

    def test_unassigned_default(self):
        """未匹配路径 → unassigned。"""
        assert COLLECT.infer_owner("utils/flood_waiter.py") == "unassigned"


# ════════════════════════════════════════════════════════════════
# D. 日期提取(extract_due_date)
# ════════════════════════════════════════════════════════════════


class TestExtractDueDate:
    """R66 P1-09: extract_due_date 日期提取测试。"""

    def test_iso_date(self):
        """ISO 日期 'YYYY-MM-DD' → 规范化。"""
        assert COLLECT.extract_due_date("需要在 2025-12-31 前修复") == "2025-12-31"

    def test_by_full_date(self):
        """'by YYYY-MM-DD' → YYYY-MM-DD。"""
        assert COLLECT.extract_due_date("must be fixed by 2025-12-31") == "2025-12-31"

    def test_by_year_month(self):
        """'by YYYY-MM' → YYYY-MM。"""
        assert COLLECT.extract_due_date("by 2025-12") == "2025-12"

    def test_by_year_only(self):
        """'by YYYY' → YYYY。"""
        assert COLLECT.extract_due_date("fix by 2025") == "2025"

    def test_chinese_date(self):
        """中文日期 'YYYY年MM月DD日' → YYYY-MM-DD。"""
        assert COLLECT.extract_due_date("2025年12月31日 前修复") == "2025-12-31"

    def test_no_date_returns_empty(self):
        """无日期 → 空字符串。"""
        assert COLLECT.extract_due_date("random reason without date") == ""

    def test_empty_reason_returns_empty(self):
        """空 reason → 空字符串。"""
        assert COLLECT.extract_due_date("") == ""


# ════════════════════════════════════════════════════════════════
# E. AST 解析(collect_skips_from_source)
# ════════════════════════════════════════════════════════════════


class TestCollectSkipsFromSource:
    """R66 P1-09: collect_skips_from_source AST 解析测试。"""

    def test_decorator_skip_with_reason(self):
        """@pytest.mark.skip(reason=...) 装饰器 → 收集 reason。"""
        source = (
            "import pytest\n"
            "\n"
            "@pytest.mark.skip(reason='Telethon 不可用')\n"
            "def test_telegram_main_chain():\n"
            "    pass\n"
        )
        records = COLLECT.collect_skips_from_source(source, "tests/test_dummy.py")
        assert len(records) == 1
        r = records[0]
        assert r.file_path == "tests/test_dummy.py"
        assert r.test_name == "test_telegram_main_chain"
        assert r.reason == "Telethon 不可用"
        assert r.category == "telethon_dependency"
        assert r.marker_type == "decorator"
        # FunctionDef.lineno 指向 'def' 关键字所在行(第 4 行,装饰器在第 3 行)
        assert r.line == 4

    def test_decorator_skipif_with_reason(self):
        """@pytest.mark.skipif(condition, reason=...) → 收集 reason。"""
        source = (
            "import pytest\n"
            "\n"
            "@pytest.mark.skipif(True, reason='aiosqlite 未安装')\n"
            "def test_db():\n"
            "    pass\n"
        )
        records = COLLECT.collect_skips_from_source(source, "tests/test_dummy.py")
        assert len(records) == 1
        r = records[0]
        assert r.reason == "aiosqlite 未安装"
        assert r.category == "dependency_missing"
        assert r.marker_type == "decorator"

    def test_decorator_skip_no_parens(self):
        """@pytest.mark.skip(无括号) → reason 为空,但仍收集。"""
        source = (
            "import pytest\n"
            "\n"
            "@pytest.mark.skip\n"
            "def test_todo():\n"
            "    pass\n"
        )
        records = COLLECT.collect_skips_from_source(source, "tests/test_dummy.py")
        assert len(records) == 1
        r = records[0]
        assert r.reason == ""
        assert r.category == "uncategorized"

    def test_pytest_skip_call(self):
        """pytest.skip(reason) 函数体内调用 → 收集 reason。"""
        source = (
            "import pytest\n"
            "\n"
            "def test_conditional_skip():\n"
            "    if not True:\n"
            "        pytest.skip('条件不满足,暂时禁用')\n"
            "    pass\n"
        )
        records = COLLECT.collect_skips_from_source(source, "tests/test_dummy.py")
        assert len(records) == 1
        r = records[0]
        assert r.test_name == "test_conditional_skip"
        assert r.reason == "条件不满足,暂时禁用"
        assert r.category == "temporarily_disabled"
        assert r.marker_type == "call"

    def test_module_level_pytestmark(self):
        """pytestmark = pytest.mark.skipif(...) 模块级赋值。"""
        source = (
            "import pytest\n"
            "\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    True,\n"
            "    reason='CockroachDB 不可用',\n"
            ")\n"
            "\n"
            "def test_dummy():\n"
            "    pass\n"
        )
        records = COLLECT.collect_skips_from_source(source, "tests/test_dummy.py")
        assert len(records) == 1
        r = records[0]
        assert r.test_name == "<module>"
        assert r.reason == "CockroachDB 不可用"
        assert r.category == "crdb_dependency"
        assert r.marker_type == "module_pytestmark"

    def test_class_level_decorator(self):
        """类级 @pytest.mark.skipif 装饰器 → test_name 为类名。"""
        source = (
            "import pytest\n"
            "\n"
            "@pytest.mark.skip(reason='legacy 测试,历史遗留')\n"
            "class TestLegacyClass:\n"
            "    def test_one(self):\n"
            "        pass\n"
            "    def test_two(self):\n"
            "        pass\n"
        )
        records = COLLECT.collect_skips_from_source(source, "tests/test_dummy.py")
        assert len(records) == 1
        r = records[0]
        assert r.test_name == "TestLegacyClass"
        assert r.reason == "legacy 测试,历史遗留"
        assert r.category == "historical_legacy"
        assert r.marker_type == "decorator"

    def test_no_skips_returns_empty(self):
        """无 skip 标记的文件 → 空列表。"""
        source = (
            "import pytest\n"
            "\n"
            "def test_normal():\n"
            "    assert True\n"
            "\n"
            "class TestNormal:\n"
            "    def test_method(self):\n"
            "        pass\n"
        )
        records = COLLECT.collect_skips_from_source(source, "tests/test_dummy.py")
        assert records == []

    def test_multiple_skips_in_one_file(self):
        """一个文件含多个 skip 标记 → 全部收集。"""
        source = (
            "import pytest\n"
            "\n"
            "@pytest.mark.skip(reason='Telethon 不可用')\n"
            "def test_telegram():\n"
            "    pass\n"
            "\n"
            "@pytest.mark.skipif(True, reason='CRDB 未就绪')\n"
            "def test_crdb():\n"
            "    pass\n"
            "\n"
            "def test_runtime_skip():\n"
            "    pytest.skip('TODO: 修复后启用')\n"
        )
        records = COLLECT.collect_skips_from_source(source, "tests/test_dummy.py")
        assert len(records) == 3
        categories = [r.category for r in records]
        assert "telethon_dependency" in categories
        assert "crdb_dependency" in categories
        assert "temporarily_disabled" in categories

    def test_production_impact_inferred_for_record(self):
        """SkipRecord.production_impact 按文件路径推断正确。"""
        source = (
            "import pytest\n"
            "\n"
            "@pytest.mark.skip(reason='legacy')\n"
            "def test_dummy():\n"
            "    pass\n"
        )
        records = COLLECT.collect_skips_from_source(
            source, "services/restore_orchestrator.py"
        )
        assert len(records) == 1
        assert records[0].production_impact == "high"
        assert records[0].owner == "restore-team"

    def test_due_date_extracted_from_reason(self):
        """SkipRecord.due_date 从 reason 提取。"""
        source = (
            "import pytest\n"
            "\n"
            "@pytest.mark.skip(reason='需要在 2025-12-31 前修复 legacy')\n"
            "def test_dummy():\n"
            "    pass\n"
        )
        records = COLLECT.collect_skips_from_source(source, "tests/test_dummy.py")
        assert len(records) == 1
        assert records[0].due_date == "2025-12-31"

    def test_syntax_error_returns_empty(self):
        """含 SyntaxError 的文件 → 返回空列表(不抛异常)。"""
        source = "def broken(:\n    pass\n"
        records = COLLECT.collect_skips_from_source(source, "tests/bad.py")
        assert records == []


# ════════════════════════════════════════════════════════════════
# F. 汇总统计(build_summary)
# ════════════════════════════════════════════════════════════════


class TestBuildSummary:
    """R66 P1-09: build_summary 汇总统计测试。"""

    def test_empty_records(self):
        """空列表 → 全 0 统计。"""
        summary = COLLECT.build_summary([])
        assert summary["total"] == 0
        assert summary["by_category"] == {}
        assert summary["by_production_impact"] == {}
        assert summary["by_owner"] == {}
        assert summary["missing_owner"] == 0
        assert summary["missing_due_date"] == 0
        assert summary["critical_path_missing_due_date"] == 0

    def test_summary_counts_correct(self):
        """多条记录 → 正确计数 by_category / by_impact / by_owner。"""
        records = [
            COLLECT.SkipRecord(
                file_path="bots/admin_bot/handlers.py",
                test_name="test_a",
                reason="Telethon 不可用",
                category="telethon_dependency",
                production_impact="high",
                owner="bot-team",
                due_date="",
                line=1,
                marker_type="decorator",
            ),
            COLLECT.SkipRecord(
                file_path="services/restore_orchestrator.py",
                test_name="test_b",
                reason="CRDB 未就绪",
                category="crdb_dependency",
                production_impact="high",
                owner="restore-team",
                due_date="2025-12-31",
                line=2,
                marker_type="decorator",
            ),
            COLLECT.SkipRecord(
                file_path="utils/foo.py",
                test_name="test_c",
                reason="random",
                category="uncategorized",
                production_impact="low",
                owner="unassigned",
                due_date="",
                line=3,
                marker_type="call",
            ),
        ]
        summary = COLLECT.build_summary(records)
        assert summary["total"] == 3
        assert summary["by_category"] == {
            "telethon_dependency": 1,
            "crdb_dependency": 1,
            "uncategorized": 1,
        }
        assert summary["by_production_impact"] == {"high": 2, "low": 1}
        assert summary["by_owner"] == {"bot-team": 1, "restore-team": 1, "unassigned": 1}
        assert summary["missing_owner"] == 1
        assert summary["missing_due_date"] == 2
        assert summary["critical_path_missing_due_date"] == 1


# ════════════════════════════════════════════════════════════════
# G. JSON 输出(output_inventory)
# ════════════════════════════════════════════════════════════════


class TestOutputInventory:
    """R66 P1-09: output_inventory JSON 输出测试。"""

    def _make_records(self):
        return [
            COLLECT.SkipRecord(
                file_path="bots/admin_bot/handlers.py",
                test_name="test_a",
                reason="Telethon 不可用",
                category="telethon_dependency",
                production_impact="high",
                owner="bot-team",
                due_date="",
                line=1,
                marker_type="decorator",
            ),
        ]

    def test_output_to_file(self, tmp_path):
        """--output <file> → 写入文件,文件含完整 JSON 结构。"""
        records = self._make_records()
        summary = COLLECT.build_summary(records)
        out_file = tmp_path / "inv.json"

        # 进度信息走 stderr,不会污染 stdout
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            COLLECT.output_inventory(records, summary, out_file)

        assert out_file.exists()
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert "description" in data
        assert "generated_at" in data
        assert "summary" in data
        assert "skips" in data
        assert data["summary"]["total"] == 1
        assert len(data["skips"]) == 1
        skip = data["skips"][0]
        # 验证 SkipRecord 全字段都序列化
        for field in (
            "file_path", "test_name", "reason", "category",
            "production_impact", "owner", "due_date", "line", "marker_type",
        ):
            assert field in skip, f"skip 记录缺少字段: {field}"
        assert skip["file_path"] == "bots/admin_bot/handlers.py"
        assert skip["category"] == "telethon_dependency"

    def test_output_to_stdout(self):
        """output_path=None → JSON 输出到 stdout。"""
        records = self._make_records()
        summary = COLLECT.build_summary(records)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            COLLECT.output_inventory(records, summary, None)

        text = stdout_buf.getvalue()
        data = json.loads(text)
        assert data["summary"]["total"] == 1
        assert len(data["skips"]) == 1

    def test_output_creates_parent_dir(self, tmp_path):
        """输出路径父目录不存在时自动创建。"""
        records = self._make_records()
        summary = COLLECT.build_summary(records)
        nested = tmp_path / "nested" / "dir" / "inv.json"

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            COLLECT.output_inventory(records, summary, nested)

        assert nested.exists()


# ════════════════════════════════════════════════════════════════
# H. 端到端:CLI 调用
# ════════════════════════════════════════════════════════════════


class TestEndToEnd:
    """R66 P1-09: 通过 subprocess 调用脚本,验证 CLI 端到端。"""

    def test_script_runs_and_exits_zero(self, tmp_path):
        """脚本对合成 tests 目录运行,生成有效 JSON,退出码 0。"""
        import subprocess
        # 创建合成 tests 目录
        synthetic_tests = tmp_path / "tests"
        synthetic_tests.mkdir()
        (synthetic_tests / "test_a.py").write_text(
            "import pytest\n"
            "\n"
            "@pytest.mark.skip(reason='Telethon 不可用')\n"
            "def test_telegram():\n"
            "    pass\n"
            "\n"
            "@pytest.mark.skipif(True, reason='aiosqlite 未安装')\n"
            "def test_db():\n"
            "    pass\n"
            "\n"
            "def test_runtime():\n"
            "    pytest.skip('TODO: 修复后启用')\n",
            encoding="utf-8",
        )
        out_file = tmp_path / "out.json"
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "collect_skip_inventory.py"),
                "--tests-dir", str(synthetic_tests),
                "--output", str(out_file),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, f"脚本应退出 0(清单工具)。stderr: {result.stderr}"
        assert out_file.exists()
        data = json.loads(out_file.read_text(encoding="utf-8"))
        assert data["summary"]["total"] == 3
        categories = data["summary"]["by_category"]
        assert categories.get("telethon_dependency") == 1
        assert categories.get("dependency_missing") == 1
        assert categories.get("temporarily_disabled") == 1

    def test_script_runs_against_real_tests_dir(self, tmp_path):
        """脚本对真实 tests/ 目录运行,生成有效 JSON,退出码 0。"""
        import subprocess
        out_file = tmp_path / "skip_inventory_real.json"
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "collect_skip_inventory.py"),
                "--output", str(out_file),
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"脚本应退出 0。stderr: {result.stderr}"
        assert out_file.exists()
        data = json.loads(out_file.read_text(encoding="utf-8"))
        # 真实 tests/ 目录下应至少找到一些 skip(R36/R42 等已知 skip)
        assert data["summary"]["total"] > 0, "真实 tests/ 应至少找到一些 skip 标记"
        # 验证 JSON 结构完整
        assert "description" in data
        assert "generated_at" in data
        assert "summary" in data
        assert "skips" in data
        # 验证 summary 含所有字段
        for key in (
            "total", "by_category", "by_production_impact", "by_owner",
            "missing_owner", "missing_due_date", "critical_path_missing_due_date",
        ):
            assert key in data["summary"], f"summary 缺少字段: {key}"


# ════════════════════════════════════════════════════════════════
# I. Baseline 文件
# ════════════════════════════════════════════════════════════════


class TestBaselineFile:
    """R66 P1-09: docs/skip_inventory_baseline.json baseline 文件存在性。"""

    def test_baseline_file_exists_and_valid(self):
        """baseline 文件存在且 JSON 结构合规。"""
        path = REPO_ROOT / "docs" / "skip_inventory_baseline.json"
        assert path.exists(), "docs/skip_inventory_baseline.json 必须存在"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "categories" in data
        assert "last_updated" in data
        assert "policy" in data
        assert "allowed_no_due_date_categories" in data
        # 所有脚本推断的类别都应在 baseline 的 categories 中
        for cat in (
            "telethon_dependency", "crdb_dependency", "dependency_missing",
            "temporarily_disabled", "historical_legacy", "uncategorized",
        ):
            assert cat in data["categories"], f"baseline 缺少类别: {cat}"
        assert "dependency_missing" in data["allowed_no_due_date_categories"]
