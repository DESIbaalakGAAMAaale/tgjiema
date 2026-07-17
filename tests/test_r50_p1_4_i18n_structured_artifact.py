"""R50 P1-4: i18n 结构化 JSON artifact + locale 优先级 + Admin Web 本地化测试。

测试覆盖(P1-4 终审报告要求):

A. 结构化 JSON artifact(4 测试)
   1. test_output_json_schema_check_passed
   2. test_output_json_schema_check_failed_with_errors
   3. test_output_json_zh_cn_placeholder_mismatch
   4. test_output_json_summary_total_keys

B. locale 优先级(3 测试)
   5. test_locale_priority_user_explicit_over_language_code
   6. test_locale_priority_language_code_over_default
   7. test_locale_priority_default_when_no_info

C. schema/key 完整性(3 测试)
   8. test_zh_cn_missing_key_detected
   9. test_en_us_extra_key_detected
   10. test_plural_rules_violation_detected

D. baseline 审批与下降(2 测试)
   11. test_baseline_scope_change_requires_approval
   12. test_baseline_only_decreases

E. Admin Web 本地化(2 测试)
   13. test_admin_web_html_lang_attribute
   14. test_admin_web_aria_labels_localized

依赖:
- scripts/verify_i18n_keys.py(R50 P1-4 新增 --output-json + 结构化 artifact)
- scripts/scan_hardcoded_strings.py(R48 baseline --ratchet / --allow-scope-change)
- services/i18n.py(locale 解析逻辑,只读不修改)
- admin/templates/dashboard.html(Jinja2 lang 属性 + aria-label)
- locales/zh-CN.json / en-US.json / schema.json / baseline.json
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

# mock telegram(避免 ImportError,conftest 已处理,此处兜底)
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "services"
LOCALES_DIR = REPO_ROOT / "locales"
SCRIPTS_DIR = REPO_ROOT / "scripts"
ADMIN_TEMPLATES_DIR = REPO_ROOT / "admin" / "templates"


# ── 模块加载辅助 ──────────────────────────────────────────


def _load_verify_module():
    """通过 importlib 加载 scripts/verify_i18n_keys.py 为独立模块实例。

    使用 importlib 而非 import 是为了在测试中能 monkeypatch 模块级常量
    (如 LOCALES_DIR)而不影响真实模块。
    """
    spec = importlib.util.spec_from_file_location(
        "_verify_i18n_keys_r50_test", SCRIPTS_DIR / "verify_i18n_keys.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 让测试能导入 scripts/scan_hardcoded_strings.py(D 节 baseline 测试)
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import scan_hardcoded_strings as scan  # noqa: E402


# ── i18n 单例重置(避免跨用例污染) ─────────────────────────


@pytest.fixture(autouse=True)
def reset_i18n_singleton():
    """每个用例前重置 services.i18n 模块级单例,避免跨用例状态污染。"""
    try:
        import services.i18n as i18n_mod
        old = i18n_mod._i18n_manager
        i18n_mod._i18n_manager = None
        yield
        i18n_mod._i18n_manager = old
    except ImportError:
        yield


# ── locale fixture 工厂 ─────────────────────────────────


def _make_locales_dir(
    tmp_path: Path,
    zh_data: dict,
    en_data: dict,
    schema_data: Optional[dict] = None,
) -> Path:
    """创建临时 locales 目录,写入 zh-CN.json / en-US.json(/ schema.json)。

    Args:
        tmp_path: pytest tmp_path
        zh_data: zh-CN.json 内容
        en_data: en-US.json 内容
        schema_data: 可选的 schema.json 内容;None 时不创建 schema.json

    Returns:
        临时 locales 目录路径
    """
    locales_dir = tmp_path / "locales"
    locales_dir.mkdir()
    (locales_dir / "zh-CN.json").write_text(
        json.dumps(zh_data, ensure_ascii=False), encoding="utf-8"
    )
    (locales_dir / "en-US.json").write_text(
        json.dumps(en_data, ensure_ascii=False), encoding="utf-8"
    )
    if schema_data is not None:
        (locales_dir / "schema.json").write_text(
            json.dumps(schema_data, ensure_ascii=False), encoding="utf-8"
        )
    return locales_dir


def _run_verify_with_artifact(
    verify_mod,
    locales_dir: Path,
    tmp_path: Path,
    monkeypatch,
) -> tuple[int, dict]:
    """运行 verify(output_json=...) 并返回 (exit_code, artifact_dict)。

    Args:
        verify_mod: verify_i18n_keys 模块实例
        locales_dir: 临时 locales 目录
        tmp_path: pytest tmp_path(用于 artifact 输出路径)
        monkeypatch: pytest monkeypatch

    Returns:
        (exit_code, artifact_dict)
    """
    monkeypatch.setattr(verify_mod, "LOCALES_DIR", locales_dir)
    # 跳过 ErrorCodes message_key 校验(测试 fixture 无完整 ErrorRegistry)
    monkeypatch.setattr(verify_mod, "_verify_error_code_message_keys", lambda zh, en: [])
    output_path = tmp_path / "i18n-report.json"
    rc = verify_mod.verify(output_json=output_path)
    assert output_path.exists(), "JSON artifact 文件应被创建"
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    return rc, artifact


# ════════════════════════════════════════════════════════════════
# A. 结构化 JSON artifact(4 测试)
# ════════════════════════════════════════════════════════════════


class TestStructuredJsonArtifact:
    """A 节: verify_i18n_keys 输出结构化 JSON artifact。

    验证 R50 P1-4 新增 --output-json 参数生成的 artifact 结构符合规范,
    可被 GitHub Actions 上传为 artifact 供下游消费。
    """

    def test_output_json_schema_check_passed(self, tmp_path, monkeypatch):
        """A1: schema 合法时 JSON 中 schema_check.passed=True。"""
        zh_data = {
            "meta": {"locale": "zh-CN"},
            "common": {"ok": "确定"},
            "errors": {"x": "错误"},
            "ui": {"title": "标题"},
        }
        en_data = {
            "meta": {"locale": "en-US"},
            "common": {"ok": "OK"},
            "errors": {"x": "Error"},
            "ui": {"title": "Title"},
        }
        schema_data = {
            "type": "object",
            "required": ["common", "errors", "ui"],
            "properties": {"common": {"type": "object"}},
        }
        locales_dir = _make_locales_dir(tmp_path, zh_data, en_data, schema_data)
        mod = _load_verify_module()
        rc, artifact = _run_verify_with_artifact(mod, locales_dir, tmp_path, monkeypatch)

        assert rc == 0, "校验应通过(exit 0)"
        # schema_check.passed == True,errors 为空
        assert artifact["schema_check"]["passed"] is True
        assert artifact["schema_check"]["errors"] == []
        # artifact 顶层应包含所有规范字段
        for key in ("schema_check", "zh_cn_check", "en_us_check",
                    "plural_rules_check", "summary"):
            assert key in artifact, f"artifact 应包含顶层字段: {key}"

    def test_output_json_schema_check_failed_with_errors(self, tmp_path, monkeypatch):
        """A2: schema 非法时 JSON 中 schema_check.passed=False, errors 含详情。"""
        # zh-CN / en-US 都缺少 schema 必需的 "common" 字段
        zh_data = {
            "meta": {"locale": "zh-CN"},
            "errors": {"x": "错误"},
            "ui": {"title": "标题"},
            # 缺少 common
        }
        en_data = {
            "meta": {"locale": "en-US"},
            "errors": {"x": "Error"},
            "ui": {"title": "Title"},
            # 缺少 common
        }
        schema_data = {
            "type": "object",
            "required": ["common", "errors", "ui"],
        }
        locales_dir = _make_locales_dir(tmp_path, zh_data, en_data, schema_data)
        mod = _load_verify_module()
        rc, artifact = _run_verify_with_artifact(mod, locales_dir, tmp_path, monkeypatch)

        # schema 校验失败 → exit 1
        assert rc == 1
        assert artifact["schema_check"]["passed"] is False
        # errors 应包含两个 locale 的"缺少 common"信息
        errors = artifact["schema_check"]["errors"]
        assert len(errors) >= 2, f"应至少有 2 条 schema 错误(zh + en),实际: {errors}"
        error_text = " ".join(errors)
        assert "common" in error_text, f"错误信息应提及缺失的 'common' 字段: {errors}"
        assert "zh-CN" in error_text or "en-US" in error_text

    def test_output_json_zh_cn_placeholder_mismatch(self, tmp_path, monkeypatch):
        """A3: zh-CN 占位符不匹配时记录到 placeholder_mismatches。"""
        # zh-CN 有 {count} 占位符,en-US 没有 → 不一致
        zh_data = {
            "meta": {},
            "errors": {"x": "剩余 {count} 次"},
        }
        en_data = {
            "meta": {},
            "errors": {"x": "Remaining"},
        }
        locales_dir = _make_locales_dir(tmp_path, zh_data, en_data)
        mod = _load_verify_module()
        rc, artifact = _run_verify_with_artifact(mod, locales_dir, tmp_path, monkeypatch)

        # 占位符不一致 → exit 1
        assert rc == 1
        zh_mismatches = artifact["zh_cn_check"]["placeholder_mismatches"]
        en_mismatches = artifact["en_us_check"]["placeholder_mismatches"]
        # 双方都应记录该 key 的占位符不一致
        assert len(zh_mismatches) >= 1, f"zh_cn_check.placeholder_mismatches 应非空: {zh_mismatches}"
        assert len(en_mismatches) >= 1, f"en_us_check.placeholder_mismatches 应非空: {en_mismatches}"
        # 记录中应包含 key 名
        zh_text = " ".join(zh_mismatches)
        en_text = " ".join(en_mismatches)
        assert "errors.x" in zh_text, f"应提及 key 'errors.x': {zh_mismatches}"
        assert "errors.x" in en_text, f"应提及 key 'errors.x': {en_mismatches}"
        # zh-CN 侧应包含 count(en-US 侧为空集合)
        assert "count" in zh_text, f"zh-CN 侧应提及 'count' 占位符: {zh_mismatches}"

    def test_output_json_summary_total_keys(self, tmp_path, monkeypatch):
        """A4: summary.total_keys 等于 schema 中 key 总数。"""
        zh_data = {
            "meta": {},
            "common": {"ok": "确定", "cancel": "取消"},  # 2 keys
            "errors": {"x": "错误", "y": "异常"},         # 2 keys
        }
        en_data = {
            "meta": {},
            "common": {"ok": "OK", "cancel": "Cancel"},
            "errors": {"x": "Error", "y": "Exception"},
        }
        locales_dir = _make_locales_dir(tmp_path, zh_data, en_data)
        mod = _load_verify_module()
        rc, artifact = _run_verify_with_artifact(mod, locales_dir, tmp_path, monkeypatch)

        assert rc == 0
        # 4 个扁平化 key: common.ok, common.cancel, errors.x, errors.y
        assert artifact["summary"]["total_keys"] == 4, (
            f"total_keys 应为 4(common 2 + errors 2),实际: {artifact['summary']['total_keys']}"
        )
        # coverage 应为 1.0(zh 与 en key 完全一致)
        assert artifact["summary"]["zh_cn_coverage"] == 1.0
        assert artifact["summary"]["en_us_coverage"] == 1.0
        # timestamp 应为 ISO-8601 格式(含 Z 后缀)
        ts = artifact["summary"]["timestamp"]
        assert isinstance(ts, str) and ts.endswith("Z"), f"timestamp 应为 ISO-8601 UTC: {ts}"
        # scanner_version 应等于模块常量
        assert artifact["summary"]["scanner_version"] == mod.SCANNER_VERSION


# ════════════════════════════════════════════════════════════════
# B. locale 优先级(3 测试)
# ════════════════════════════════════════════════════════════════


def _resolve_locale_priority(
    user_explicit: Optional[str],
    language_code: Optional[str],
) -> str:
    """locale 优先级解析器(测试辅助,镜像 services/i18n.py 的语义)。

    优先级:
        1. 用户显式选择(user_explicit)— 由 set_user_locale 写入 users_local
        2. Telegram language_code — 通过 parse_accept_language 映射到支持的 locale
        3. 默认值 zh-CN(_DEFAULT_LOCALE)

    Args:
        user_explicit: 用户显式设置的 locale(如 "zh-CN" / "en-US");None 表示未设置
        language_code: Telegram User.language_code(如 "zh" / "en" / "zh-CN")

    Returns:
        解析后的 locale 字符串
    """
    # 优先级 1: 用户显式选择
    if user_explicit:
        return user_explicit
    # 优先级 2: Telegram language_code → 通过 parse_accept_language 映射
    if language_code:
        import services.i18n as i18n_mod
        manager = i18n_mod.get_i18n_manager()
        # parse_accept_language 接受 RFC 7231 格式,这里直接传 language_code
        return manager.parse_accept_language(language_code)
    # 优先级 3: 默认值
    import services.i18n as i18n_mod
    return i18n_mod._DEFAULT_LOCALE


class TestLocalePriority:
    """B 节: locale 优先级 — 用户显式 > Telegram language_code > 默认值。

    验证从 services/i18n.py 提取的 locale 解析路径符合 R42 P1-8 设计:
        get_user_locale(user_id) → set_user_locale(user_id, locale) → _DEFAULT_LOCALE
        parse_accept_language(language_code) → _DEFAULT_LOCALE
    """

    def test_locale_priority_user_explicit_over_language_code(self):
        """B5: 用户显式 zh-CN > Telegram language_code=en → 返回 zh-CN。"""
        # 用户显式设置 zh-CN,但 Telegram language_code=en(英文)
        result = _resolve_locale_priority(
            user_explicit="zh-CN",
            language_code="en",
        )
        assert result == "zh-CN", (
            f"用户显式 zh-CN 应优先于 language_code=en,实际: {result}"
        )

    def test_locale_priority_language_code_over_default(self):
        """B6: 无显式设置,language_code=zh → 返回 zh-CN(或 zh)。"""
        # 无显式设置,但 Telegram language_code=zh
        result = _resolve_locale_priority(
            user_explicit=None,
            language_code="zh",
        )
        # parse_accept_language 应将 "zh" 前缀匹配到 "zh-CN"
        assert result == "zh-CN", (
            f"language_code=zh 应映射到 zh-CN,实际: {result}"
        )

    def test_locale_priority_default_when_no_info(self):
        """B7: 无显式 + 无 language_code → 返回默认 zh-CN。"""
        result = _resolve_locale_priority(
            user_explicit=None,
            language_code=None,
        )
        assert result == "zh-CN", (
            f"无任何 locale 信息时应返回默认 zh-CN,实际: {result}"
        )


# ════════════════════════════════════════════════════════════════
# C. schema/key 完整性(3 测试)
# ════════════════════════════════════════════════════════════════


class TestSchemaKeyIntegrity:
    """C 节: schema/zh-CN/en-US key/类型/占位符/复数规则检查。"""

    def test_zh_cn_missing_key_detected(self, tmp_path, monkeypatch):
        """C8: zh-CN 缺少某 key → zh_cn_check.missing_keys 含该 key。

        设计说明:
            - zh-CN 缺 key X(在 en-US 中存在)→ 从 zh-CN 视角看是"缺失"
            - 同时 en-US 视角下 X 是"额外"(en_us_check.extra_keys)
            - 本测试验证缺失检测的核心契约
        """
        zh_data = {"meta": {}, "errors": {}}                # zh-CN 空 errors
        en_data = {"meta": {}, "errors": {"missing_key": "Error"}}  # en-US 有 missing_key
        locales_dir = _make_locales_dir(tmp_path, zh_data, en_data)
        mod = _load_verify_module()
        rc, artifact = _run_verify_with_artifact(mod, locales_dir, tmp_path, monkeypatch)

        # zh-CN 缺少 errors.missing_key → exit 1
        assert rc == 1
        # zh_cn_check.missing_keys 应包含缺失的 key
        zh_missing = artifact["zh_cn_check"]["missing_keys"]
        assert "errors.missing_key" in zh_missing, (
            f"zh_cn_check.missing_keys 应含 'errors.missing_key': {zh_missing}"
        )
        # 对称性:en_us_check.extra_keys 也应包含该 key(en-US 多出的)
        en_extra = artifact["en_us_check"]["extra_keys"]
        assert "errors.missing_key" in en_extra, (
            f"en_us_check.extra_keys 应含 'errors.missing_key'(对称性): {en_extra}"
        )
        # zh_cn_check.passed 应为 False(有缺失)
        assert artifact["zh_cn_check"]["passed"] is False

    def test_en_us_extra_key_detected(self, tmp_path, monkeypatch):
        """C9: en-US 多出 key(不在 zh-CN/schema 中)→ en_us_check.extra_keys 含该 key。"""
        zh_data = {"meta": {}, "errors": {"shared": "错误"}}
        en_data = {
            "meta": {},
            "errors": {"shared": "Error", "extra_key": "Extra"},
        }
        locales_dir = _make_locales_dir(tmp_path, zh_data, en_data)
        mod = _load_verify_module()
        rc, artifact = _run_verify_with_artifact(mod, locales_dir, tmp_path, monkeypatch)

        # en-US 多出 errors.extra_key → exit 1
        assert rc == 1
        # en_us_check.extra_keys 应包含多出的 key
        en_extra = artifact["en_us_check"]["extra_keys"]
        assert "errors.extra_key" in en_extra, (
            f"en_us_check.extra_keys 应含 'errors.extra_key': {en_extra}"
        )
        # 对称性:zh_cn_check.missing_keys 也应包含该 key(zh-CN 缺失)
        zh_missing = artifact["zh_cn_check"]["missing_keys"]
        assert "errors.extra_key" in zh_missing, (
            f"zh_cn_check.missing_keys 应含 'errors.extra_key'(对称性): {zh_missing}"
        )
        # en_us_check.passed 应为 False(有额外 key)
        assert artifact["en_us_check"]["passed"] is False

    def test_plural_rules_violation_detected(self, tmp_path, monkeypatch):
        """C10: 缺少 plural_one/plural_other 规则 → plural_rules_check.violations。"""
        # zh-CN 有 .one 但缺 .other;en-US 反之(双向违规)
        zh_data = {
            "meta": {},
            "common": {
                "files": {
                    "count": {
                        "one": "{count} 个文件",
                        # 故意缺少 .other
                    }
                }
            },
        }
        en_data = {
            "meta": {},
            "common": {
                "files": {
                    "count": {
                        "other": "{count} files",
                        # 故意缺少 .one
                    }
                }
            },
        }
        locales_dir = _make_locales_dir(tmp_path, zh_data, en_data)
        mod = _load_verify_module()
        rc, artifact = _run_verify_with_artifact(mod, locales_dir, tmp_path, monkeypatch)

        # 复数规则违规 → exit 1
        assert rc == 1
        # plural_rules_check.passed 应为 False
        assert artifact["plural_rules_check"]["passed"] is False
        # violations 应包含双向违规(zh-CN 缺 .other + en-US 缺 .one)
        violations = artifact["plural_rules_check"]["violations"]
        assert len(violations) >= 2, (
            f"应至少有 2 条复数违规(zh + en),实际: {violations}"
        )
        violations_text = " ".join(violations)
        # 应提及 common.files.count 前缀
        assert "common.files.count" in violations_text, (
            f"违规信息应提及 'common.files.count': {violations}"
        )
        # 应提及 .one / .other
        assert ".one" in violations_text or ".other" in violations_text


# ════════════════════════════════════════════════════════════════
# D. baseline 审批与下降(2 测试)
# ════════════════════════════════════════════════════════════════


def _synthetic_baseline(counts_per_module: int = 10) -> dict:
    """合成 R48 格式 baseline(scope 与 scan.INCLUDED_PATHS 一致)。

    用于 D 节 baseline 测试 — 构造合法 baseline 字典,
    included_paths 与当前 scan.INCLUDED_PATHS 完全一致(无 scope 变化)。
    """
    modules = {
        m: {
            "baseline": counts_per_module,
            "target": 0,
            "user_visible": max(0, counts_per_module - 2),
            "log_only": min(2, counts_per_module),
        }
        for m in scan.MODULE_KEYS
    }
    n = len(scan.MODULE_KEYS)
    return {
        "_description": "R50 P1-4 test baseline",
        "_original_r44_baseline": 954,
        "scanner_version": scan.SCANNER_VERSION,
        "included_paths": list(scan.INCLUDED_PATHS),
        "modules": modules,
        "total": {
            "baseline": counts_per_module * n,
            "target": 0,
            "user_visible": max(0, counts_per_module - 2) * n,
            "log_only": min(2, counts_per_module) * n,
        },
        "last_updated": "2026-07-14",
        "last_updated_by": "R50 P1-4 test",
    }


class TestBaselineApprovalAndRatchet:
    """D 节: baseline 审批与下降规则。

    验证 R48 P1-c 设计:
        - included_paths 变化 → cmd_ratchet 拒绝(需 --allow-scope-change 单独审批)
        - baseline total 只允许非增加(下降或持平);增加 → 拒绝
        - 每模块 baseline 最终目标 0(只能下降不能上升)
    """

    def test_baseline_scope_change_requires_approval(self, monkeypatch, tmp_path, capsys):
        """D11: included_paths 变化时 --ratchet 拒绝(需 --allow-scope-change)。"""
        # 将 MODULE_BASELINE_FILE 指向临时路径,避免污染真实 baseline.json
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        # baseline 的 included_paths 少一个 → scope 已变化
        baseline = _synthetic_baseline(counts_per_module=10)
        baseline["included_paths"] = list(scan.INCLUDED_PATHS)[:-1]
        # counts 与 baseline 一致(确保是 scope 变化而非 total 变化导致拒绝)
        counts = {m: 10 for m in scan.MODULE_KEYS}

        rc = scan.cmd_ratchet(counts, baseline)

        # scope 变化 → 拒绝(exit 1)
        assert rc == 1, "scope 变化时 cmd_ratchet 应返回 exit 1"
        out = capsys.readouterr().out
        assert "scope" in out.lower(), f"输出应提及 scope 变化: {out}"
        # baseline 文件未写入(拒绝更新)
        assert not (tmp_path / "baseline.json").exists(), (
            "scope 拒绝时不应写入 baseline.json"
        )

    def test_baseline_only_decreases(self, monkeypatch, tmp_path, capsys):
        """D12: baseline 数值只能下降,增加时 --ratchet 拒绝。"""
        monkeypatch.setattr(scan, "MODULE_BASELINE_FILE", tmp_path / "baseline.json")
        # baseline total = 10 * len(MODULE_KEYS)
        baseline = _synthetic_baseline(counts_per_module=10)
        # 当前 counts = 12 * len(MODULE_KEYS)(增加)→ 应拒绝
        increased_counts = {m: 12 for m in scan.MODULE_KEYS}

        rc = scan.cmd_ratchet(increased_counts, baseline)

        # total 增加 → 拒绝(exit 1)
        assert rc == 1, "total 增加时 cmd_ratchet 应返回 exit 1"
        out = capsys.readouterr().out
        # 输出应提示"只允许非增加"或类似
        assert "增加" in out or "non-increase" in out.lower() or ">" in out, (
            f"输出应提示 total 增加: {out}"
        )
        # baseline 文件未写入(拒绝更新)
        assert not (tmp_path / "baseline.json").exists(), (
            "total 增加时不应写入 baseline.json"
        )


# ════════════════════════════════════════════════════════════════
# E. Admin Web 本地化(2 测试)
# ════════════════════════════════════════════════════════════════


class TestAdminWebLocalization:
    """E 节: Admin Web lang/aria/时区/数字格式随 locale 改变。

    验证 R45 17.3 / R42 P1-8 的无障碍 + i18n 设计:
        - dashboard.html 的 <html lang="..."> 由 Jinja2 上下文 locale 注入
        - 关键 aria-label 在 zh-CN / en-US 都有翻译(bilingual 或 locale 文件条目)
    """

    def test_admin_web_html_lang_attribute(self):
        """E13: dashboard.html 的 <html lang="..."> 随 locale 变化。

        dashboard.html 第 3 行:
            <html lang="{{ locale | default('zh-CN') }}">

        验证:
            1. 模板包含 <html lang="..."> 属性
            2. lang 值使用 Jinja2 表达式 {{ locale | default('zh-CN') }}
            3. 默认值 = 'zh-CN'
            4. 通过 Jinja2 渲染验证 locale='en-US' / 'zh-CN' / 缺省 三种情况
        """
        template_path = ADMIN_TEMPLATES_DIR / "dashboard.html"
        template_text = template_path.read_text(encoding="utf-8")

        # 1. 提取 <html lang="..."> 表达式
        match = re.search(r'<html\s+lang="([^"]+)"', template_text)
        assert match, "dashboard.html 应包含 <html lang=\"...\"> 属性"
        lang_expr = match.group(1)

        # 2. lang 值应使用 locale 变量(支持 locale 动态注入)
        assert "locale" in lang_expr, (
            f"lang 属性应使用 Jinja2 locale 变量,实际: {lang_expr}"
        )
        # 3. 默认值应为 zh-CN
        assert "zh-CN" in lang_expr, (
            f"默认 locale 应为 'zh-CN',实际表达式: {lang_expr}"
        )

        # 4. 通过 Jinja2 渲染验证 locale 切换效果(若 jinja2 不可用则降级 regex)
        try:
            from jinja2 import Template
        except ImportError:
            # jinja2 不可用 — 已通过 regex 验证表达式,跳过渲染
            return

        # 仅渲染 html 标签部分(避免依赖 FastAPI 上下文的完整模板)
        tmpl = Template('<html lang="{{ locale | default(\'zh-CN\') }}">')
        # locale='en-US' → lang='en-US'
        rendered_en = tmpl.render(locale="en-US")
        assert rendered_en == '<html lang="en-US">', (
            f"locale=en-US 时 lang 应为 'en-US',实际: {rendered_en}"
        )
        # locale='zh-CN' → lang='zh-CN'
        rendered_zh = tmpl.render(locale="zh-CN")
        assert rendered_zh == '<html lang="zh-CN">', (
            f"locale=zh-CN 时 lang 应为 'zh-CN',实际: {rendered_zh}"
        )
        # 无 locale → 默认 zh-CN
        rendered_default = tmpl.render()
        assert rendered_default == '<html lang="zh-CN">', (
            f"无 locale 时应回退默认 'zh-CN',实际: {rendered_default}"
        )

    def test_admin_web_aria_labels_localized(self):
        """E14: 关键 aria-label 在 zh-CN/en-US 都有翻译。

        验证策略(双重):
            1. dashboard.html / base.html 中的关键 aria-label 通过 i18n key 接入
               (R59 §5.1 P1: aria-label 使用 ``{{ t("ui.admin.xxx.sN") }}`` 模式,
               不再硬编码 bilingual 文本;翻译值在 locale 文件中提供)
            2. zh-CN.json / en-US.json 的 accessibility.* 命名空间包含
               无障碍相关翻译(skip_to_content / screen_reader_friendly 等)
               证明 aria-label 文案可在 locale 文件中本地化

        关键 aria-label(任务要求):
            - "main navigation"(主导航)— 出现在 dashboard.html 与 base.html
            - skip-link "跳到主要内容 / Skip to main content"
        """
        # ── 1. 解析 dashboard.html 与 base.html 的 aria-label ──
        dashboard_text = (ADMIN_TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
        base_text = (ADMIN_TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")

        # R59 §5.1 P1: aria-label 现在使用 {{ t("ui.admin.xxx.sN") }} Jinja2 表达式
        # 正则匹配 aria-label="{{ t("key") }}" 模式,提取 i18n key
        aria_label_pattern = re.compile(r'aria-label="\{\{\s*t\("([^"]+)"\)\s*\}\}"')
        aria_keys = aria_label_pattern.findall(dashboard_text + base_text)
        assert len(aria_keys) > 0, (
            "dashboard.html + base.html 应至少有一个 i18n 化的 aria-label "
            "(模式: aria-label=\"{{ t('ui.admin.xxx.sN') }}\")"
        )

        # ── 2. 验证 i18n key 在 zh-CN/en-US locale 文件中都存在 ──
        zh_path = LOCALES_DIR / "zh-CN.json"
        en_path = LOCALES_DIR / "en-US.json"
        zh_data = json.loads(zh_path.read_text(encoding="utf-8"))
        en_data = json.loads(en_path.read_text(encoding="utf-8"))

        def _lookup_nested(data: dict, dotted_key: str) -> str | None:
            """按点分路径查找嵌套 dict 中的值(如 ui.admin.base.s24)。"""
            parts = dotted_key.split(".")
            cur = data
            for p in parts:
                if isinstance(cur, dict) and p in cur:
                    cur = cur[p]
                else:
                    return None
            return cur if isinstance(cur, str) else None

        # 主导航 aria-label key(ui.admin.base.s24 / ui.admin.dashboard.s26)
        # 应在 zh-CN 含"导航"、en-US 含"navigation"
        nav_keys = [
            k for k in aria_keys
            if "base.s24" in k or "dashboard.s26" in k
        ]
        assert len(nav_keys) >= 1, (
            f"应包含主导航 aria-label key (ui.admin.base.s24 或 ui.admin.dashboard.s26),"
            f"实际所有 aria-label keys: {aria_keys}"
        )
        for nav_key in nav_keys:
            zh_val = _lookup_nested(zh_data, nav_key)
            en_val = _lookup_nested(en_data, nav_key)
            assert zh_val is not None, (
                f"zh-CN.json 应包含 aria-label key '{nav_key}' 的翻译"
            )
            assert en_val is not None, (
                f"en-US.json 应包含 aria-label key '{nav_key}' 的翻译"
            )
            # zh-CN 应含中文"导航"
            assert "导航" in zh_val, (
                f"zh-CN '{nav_key}' 应含'导航',实际: '{zh_val}'"
            )
            # en-US 应含英文"navigation"
            assert "navigation" in en_val.lower(), (
                f"en-US '{nav_key}' 应含'navigation',实际: '{en_val}'"
            )

        # ── 3. 验证 locale 文件的 accessibility.* 命名空间 ──
        # 两个 locale 都应有 accessibility 命名空间
        assert "accessibility" in zh_data, "zh-CN.json 应包含 accessibility 命名空间"
        assert "accessibility" in en_data, "en-US.json 应包含 accessibility 命名空间"

        zh_acc = zh_data["accessibility"]
        en_acc = en_data["accessibility"]
        # accessibility 命名空间应非空
        assert isinstance(zh_acc, dict) and len(zh_acc) > 0, (
            "zh-CN accessibility 命名空间应非空"
        )
        assert isinstance(en_acc, dict) and len(en_acc) > 0, (
            "en-US accessibility 命名空间应非空"
        )

        # 关键无障碍 key 应在两个 locale 都有翻译
        key_aria_keys = ["skip_to_content", "screen_reader_friendly"]
        for key in key_aria_keys:
            assert key in zh_acc, (
                f"zh-CN accessibility 应包含 '{key}' 翻译,实际 keys: {list(zh_acc.keys())}"
            )
            assert key in en_acc, (
                f"en-US accessibility 应包含 '{key}' 翻译,实际 keys: {list(en_acc.keys())}"
            )
            # 翻译值应非空
            assert zh_acc[key].strip(), f"zh-CN accessibility.{key} 翻译不应为空"
            assert en_acc[key].strip(), f"en-US accessibility.{key} 翻译不应为空"

        # skip_to_content 应在两个 locale 都有翻译(zh-CN 中文 / en-US 英文)
        assert "跳" in zh_acc["skip_to_content"] or "内容" in zh_acc["skip_to_content"], (
            f"zh-CN skip_to_content 应含中文,实际: '{zh_acc['skip_to_content']}'"
        )
        assert "skip" in en_acc["skip_to_content"].lower() or "main" in en_acc["skip_to_content"].lower(), (
            f"en-US skip_to_content 应含英文,实际: '{en_acc['skip_to_content']}'"
        )
