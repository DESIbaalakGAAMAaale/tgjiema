"""R71 Wave 6: Solo Founder Branch Ruleset — 测试套件。

R71 P1-01/02/03 (Wave 6) 整改背景:
    旧版治理配置(R67 P0-01 + R70 Wave 10)对 solo founder (@maxiuquan) 造成
    审批死锁:
      - R67 P0-01: required_reviewers=2(需要 2 名 reviewer,但只有 1 名开发者)
      - R70 Wave 10: required_reviewers=1 + require_code_owner_review=true
        (强制 CODEOWNERS 评审,但 CODEOWNERS=* @maxiuquan — 唯一维护者
        无法批准自己的 PR)
    同时 R70 ruleset strict_merge=false,允许 stale parent commit 通过 status
    check(违反 current-SHA binding 原则);且 required_status_checks 缺少
    R71 Wave 4/7 新增的 context(validate-oci-rootfs / bind-runtime-config 等)。

R71 Wave 6 整改(P1-01/02/03, Commit 6) + R72 P1-06 修订:
    1. .github/branch_ruleset.expected.json:
       - 用单一 "R71 Solo Founder Branch Ruleset" 替换两个旧 ruleset
       - required_reviewers=0(solo founder,无审批死锁)
       - require_code_owner_review=false(CODEOWNERS 保留但不阻断)
       - strict_merge=true(current-SHA,不允许 stale parent commit)
       - required_status_checks 含 29 个 context(仅 PR/master 事件可产生的 check;
         R72 P1-06 移除 8 个 tag-only/environment-only check,从 36 缩减到 29)
       - bypass_actors=[](无 admin bypass;紧急情况通过 record_break_glass.py)
    2. .github/branch_protection.expected.json:
       - required_approving_review_count: 0(solo founder)
       - require_code_owner_reviews: false
       - required_status_checks.strict: true
       - contexts 覆盖 29 个 release gates(R72 P1-06 移除 tag-only/environment-only)
    3. scripts/configure_branch_ruleset.sh:
       - 完全重写为单一 R71 Solo Founder Ruleset 配置
       - 保留 --dry-run / --help / 幂等性(PUT/POST)
       - 自检断言更新为 solo-founder 语义
    4. scripts/verify_branch_ruleset.sh:
       - 完全重写为 solo-founder 语义断言
       - 断言 required_reviewers == 0 / strict_merge == true / 29 contexts
    5. scripts/record_break_glass.py(新文件):
       - Break-glass 紧急手动 override 审计日志(JSONL 格式)
       - 强制 typed_confirmation == "BREAK-GLASS-EMERGENCY"
       - 校验 40-char hex SHA / GitHub Actions URL
       - append-only JSONL 文件,每行一个事件 JSON 对象

被测对象:
    - .github/branch_ruleset.expected.json(R71 Solo Founder Ruleset 基线)
    - .github/branch_protection.expected.json(BP 基线,0 approving reviews)
    - scripts/configure_branch_ruleset.sh(配置脚本 --dry-run 行为)
    - scripts/verify_branch_ruleset.sh(验证脚本静态断言)
    - scripts/record_break_glass.py(break-glass 审计日志 CLI)

测试覆盖矩阵(60+ 个测试):
    A. branch_ruleset.expected.json schema 与内容(10 个)
    B. branch_protection.expected.json solo-founder 语义(8 个)
    C. configure_branch_ruleset.sh 静态检查 + --dry-run(8 个)
    D. verify_branch_ruleset.sh solo-founder 断言(6 个)
    E. record_break_glass.py 模块结构与常量(10 个,含 R72 P1-07 常量与 issue_url 字段)
    F. record_break_glass.py 输入校验(8 个)
    G. record_break_glass.py JSONL 持久化(6 个,create_issue=False 模式)
    H. record_break_glass.py CLI 退出码(4 个,--no-create-issue 模式)
    I. record_break_glass.py 端到端验证(--no-create-issue 模式)
    J. R72 P1-07: create_github_issue() 函数(mocked gh CLI)(7 个)
    K. R72 P1-07: record_break_glass() create_issue=True(mocked)(3 个)
    L. R72 P1-07: CLI --no-create-issue / --repo 标志(3 个)

测试策略:
    - Windows 兼容(无 Docker / 无 gh CLI / 无 jq 时仍可运行)
    - 用 tmp_path 创建合成 JSONL 文件
    - 用 monkeypatch 替换 uuid / 时间戳(确定性)
    - 严格遵守 R71 整改规范(无 TODO / pass / 占位符 / WARN / skip 伪造成功)
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RULESET_EXPECTED_JSON = REPO_ROOT / ".github" / "branch_ruleset.expected.json"
BP_EXPECTED_JSON = REPO_ROOT / ".github" / "branch_protection.expected.json"
CONFIGURE_RULESET_SH = REPO_ROOT / "scripts" / "configure_branch_ruleset.sh"
VERIFY_RULESET_SH = REPO_ROOT / "scripts" / "verify_branch_ruleset.sh"
RECORD_BREAK_GLASS_PY = REPO_ROOT / "scripts" / "record_break_glass.py"

# R71 P1-02 / R72 P1-06: 29 个必需 status checks(仅 PR/master 事件可产生的 check)
# 与 .github/branch_ruleset.expected.json / configure_branch_ruleset.sh 保持一致
# R72 P1-06: 移除 8 个 tag-only/environment-only 的 check(compose-runtime-e2e /
# sign-image / publish-attestation / attestation-semantics-verify / verify-only-3x /
# migration-binding-gate / verify-rc-identity / production-promotion-gate)
EXPECTED_REQUIRED_CHECKS: list[str] = [
    "lint", "static-gates", "test",
    "docker-build", "docker-digest-verify", "compose-config",
    "redis-acl-matrix", "schema-diff", "restore-legacy-seal-gate",
    "i18n-strict-export-boundary-gate", "migration-manifest-gate",
    "button-flow-real-ux-gate",
    "backup-restore-drill", "sbom", "pip-audit", "trivy",
    "sign-artifacts", "verify-branch-protection", "verify-branch-ruleset",
    "verify-git-source-governance", "rc-continuity",
    "tag-ruleset-verify",
    "crdb-ru-72h-attribution-gate", "production-evidence",
    "oci-allowlist-verify",
    "validate-oci-rootfs", "runtime-smoke-compose",
    "bind-runtime-config", "release-summary",
]

# R71 Wave 4/7 新增的 context(必须出现在 ruleset 与 BP 中)
# R72 P1-06: 移除 Wave 2(compose-runtime-e2e)与 Wave 5(verify-rc-identity)—
#   它们是 tag-only / environment-only 的 check,不在 PR/master 事件产出
R71_NEW_CONTEXTS: list[str] = [
    "validate-oci-rootfs",  # Wave 4
    "bind-runtime-config",  # Wave 7
]

# BP contexts 使用 GitHub Actions 矩阵展开后的名称(如 "test (3.10)"),
# 而 ruleset 使用 bare job 名(如 "test")。本映射列出 BP 中以矩阵展开
# 形式出现的 context — 测试 BP 时应接受任一矩阵变体或 bare 名。
BP_MATRIX_EXPANDED_CONTEXTS: dict[str, list[str]] = {
    # ci.yml 的 test job 使用 python-version matrix [3.10, 3.11, 3.12]
    "test": ["test (3.10)", "test (3.11)", "test (3.12)"],
}


def _context_present_in_bp(ctx: str, bp_contexts: list[str]) -> bool:
    """检查 context 是否出现在 BP contexts 列表中(支持矩阵展开变体)。

    BP API 中,使用 strategy.matrix 的 job 会产生形如 "test (3.10)" 的
    矩阵展开 context,而 Ruleset API 中则使用 bare job 名 "test"。本函数
    对 BP contexts 检查时,既接受 bare 名,也接受矩阵变体。
    """
    if ctx in bp_contexts:
        return True
    variants = BP_MATRIX_EXPANDED_CONTEXTS.get(ctx)
    if variants:
        return any(v in bp_contexts for v in variants)
    return False


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════


def _bash_available() -> bool:
    """检查 bash 是否可用(CI 上始终可用,本地 Windows 可能无)。"""
    try:
        result = subprocess.run(
            ["bash", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _jq_available() -> bool:
    """检查 jq 是否可用(--dry-run 行为测试需要)。"""
    try:
        result = subprocess.run(
            ["jq", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


BASH_AVAILABLE = _bash_available()
JQ_AVAILABLE = _jq_available()

skip_if_no_bash = pytest.mark.skipif(
    not BASH_AVAILABLE,
    reason="bash 不可用(本地 Windows 环境;CI 上始终可用)",
)
skip_if_no_jq = pytest.mark.skipif(
    not JQ_AVAILABLE,
    reason="jq 不可用(本地 Windows 环境;CI 上始终可用)",
)


def _load_record_break_glass_module():
    """动态加载 scripts/record_break_glass.py 模块。

    使用 importlib 而非 sys.path 注入,避免污染全局 sys.modules。
    """
    module_name = "_record_break_glass_test_module"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, RECORD_BREAK_GLASS_PY)
    assert spec is not None, f"无法加载模块 spec: {RECORD_BREAK_GLASS_PY}"
    assert spec.loader is not None, f"模块 loader 为 None: {RECORD_BREAK_GLASS_PY}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ════════════════════════════════════════════════════════════════
# A. branch_ruleset.expected.json schema 与内容
# ════════════════════════════════════════════════════════════════


class TestBranchRulesetExpectedJson:
    """R71 Wave 6: branch_ruleset.expected.json 符合 solo-founder 语义。"""

    @pytest.fixture(scope="class")
    def expected(self) -> dict:
        assert RULESET_EXPECTED_JSON.exists(), (
            ".github/branch_ruleset.expected.json 必须存在"
        )
        return json.loads(RULESET_EXPECTED_JSON.read_text(encoding="utf-8"))

    def test_file_exists(self):
        """R71 P1-01: branch_ruleset.expected.json 文件存在。"""
        assert RULESET_EXPECTED_JSON.exists(), (
            ".github/branch_ruleset.expected.json 必须存在"
        )

    def test_ruleset_name_is_r71_solo_founder(self, expected: dict):
        """R71 P1-01: ruleset 名称为 'R71 Solo Founder Branch Ruleset'(单一 ruleset)。"""
        assert expected["name"] == "R71 Solo Founder Branch Ruleset", (
            "ruleset 名称必须为 'R71 Solo Founder Branch Ruleset' — "
            "R71 Wave 6 用单一 ruleset 替换 R67+R70 两个 ruleset"
        )

    def test_target_is_branch(self, expected: dict):
        """R71 P1-01: target == 'branch'。"""
        assert expected["target"] == "branch"

    def test_enforcement_is_active(self, expected: dict):
        """R71 P1-01: enforcement == 'active'(规则启用)。"""
        assert expected["enforcement"] == "active"

    def test_source_type_is_repository(self, expected: dict):
        """R71 P1-01: source_type == 'Repository'。"""
        assert expected["source_type"] == "Repository"

    def test_ref_name_includes_master_and_main(self, expected: dict):
        """R71 P1-01: 针对 refs/heads/master 与 refs/heads/main。"""
        includes = expected["conditions"]["ref_name"]["include"]
        assert "refs/heads/master" in includes
        assert "refs/heads/main" in includes

    def test_rules_contain_required_immutability_rules(self, expected: dict):
        """R71 P1-01: 包含 deletion / non_fast_forward / update / required_signatures /
        required_linear_history 规则。"""
        rule_types = [r["type"] for r in expected["rules"]]
        assert "deletion" in rule_types, "缺少 deletion 规则(禁止删除 master/main)"
        assert "non_fast_forward" in rule_types, "缺少 non_fast_forward 规则(禁止 force push)"
        assert "update" in rule_types, "缺少 update 规则(禁止直接 update)"
        assert "required_signatures" in rule_types, "缺少 required_signatures 规则(强制 GPG 签名)"
        assert "required_linear_history" in rule_types, (
            "缺少 required_linear_history 规则(禁止 merge commit,替代旧 BP strict=true)"
        )

    def test_pull_request_required_approving_review_count_is_zero(self, expected: dict):
        """R71 P1-01: pull_request.required_approving_review_count == 0(solo founder,无审批死锁)。

        这是 R71 Wave 6 的核心整改点 — 旧版 R67=2 / R70=1 对 solo founder 造成
        审批死锁(唯一维护者无法批准自己的 PR)。

        R71 fix: GitHub Rulesets API 字段名为 required_approving_review_count
        (非 required_reviewers;后者是 Branch Protection API 旧字段名)。
        """
        pr_rules = [r for r in expected["rules"] if r["type"] == "pull_request"]
        assert len(pr_rules) == 1, "应有 1 个 pull_request 规则"
        params = pr_rules[0]["parameters"]
        assert params["required_approving_review_count"] == 0, (
            "R71 P1-01: required_approving_review_count 必须为 0(solo founder,无审批死锁)— "
            f"实际: {params['required_approving_review_count']}"
        )

    def test_pull_request_require_code_owner_review_is_false(self, expected: dict):
        """R71 P1-01: require_code_owner_review == false(CODEOWNERS 保留但不阻断)。

        旧版 R70 设为 true,但 CODEOWNERS=* @maxiuquan,唯一维护者无法批准自己。
        """
        pr_rules = [r for r in expected["rules"] if r["type"] == "pull_request"]
        assert len(pr_rules) == 1
        params = pr_rules[0]["parameters"]
        assert params["require_code_owner_review"] is False, (
            "R71 P1-01: require_code_owner_review 必须为 false "
            "(CODEOWNERS 保留但不阻断 — solo founder 无法批准自己)"
        )

    def test_pull_request_other_semantics_preserved(self, expected: dict):
        """R71 P1-01: dismiss_stale_reviews_on_push / required_review_thread_resolution 保留。"""
        pr_rules = [r for r in expected["rules"] if r["type"] == "pull_request"]
        assert len(pr_rules) == 1
        params = pr_rules[0]["parameters"]
        assert params["dismiss_stale_reviews_on_push"] is True, (
            "dismiss_stale_reviews_on_push 应为 true(新 push 时作废旧 approval)"
        )
        assert params["required_review_thread_resolution"] is True, (
            "required_review_thread_resolution 应为 true(conversation 必须解决)"
        )

    def test_required_status_checks_strict_required_status_checks_policy_true(
        self, expected: dict
    ):
        """R71 P1-03: required_status_checks.strict_required_status_checks_policy == true(current-SHA)。

        旧版 R70 strict_merge=false(旧 BP 字段名),允许 stale parent commit 通过
        status check,违反 current-SHA binding 原则。R71 Wave 6 整改为 true。

        R71 fix: GitHub Rulesets API 字段名为 strict_required_status_checks_policy
        (非 strict_merge;后者是 Branch Protection API 旧字段名)。
        来源: go-github RequiredStatusChecksRuleParameters 结构体定义。
        """
        rsc_rules = [r for r in expected["rules"] if r["type"] == "required_status_checks"]
        assert len(rsc_rules) == 1, "应有 1 个 required_status_checks 规则"
        params = rsc_rules[0]["parameters"]
        assert params["strict_required_status_checks_policy"] is True, (
            "R71 P1-03: strict_required_status_checks_policy 必须为 true "
            "(current-SHA,不允许 stale parent commit)"
        )

    def test_required_status_checks_has_29_contexts(self, expected: dict):
        """R71 P1-02 / R72 P1-06: required_status_checks 覆盖 29 个 PR/master-event check。

        旧版 R70 只有 5 个 required checks,R71 扩展到 36 个。R72 P1-06 移除 8 个
        tag-only/environment-only 的 check(compose-runtime-e2e / sign-image /
        publish-attestation / attestation-semantics-verify / verify-only-3x /
        migration-binding-gate / verify-rc-identity / production-promotion-gate),
        因为它们不在 PR/master 事件产出 check,会造成合并死锁。

        R71 fix: Ruleset API 参数名为 required_status_checks(非 required_checks)。
        """
        rsc_rules = [r for r in expected["rules"] if r["type"] == "required_status_checks"]
        assert len(rsc_rules) == 1
        params = rsc_rules[0]["parameters"]
        contexts = [c["context"] for c in params["required_status_checks"]]
        assert len(contexts) >= 29, (
            f"R71 P1-02 / R72 P1-06: 至少需要 29 个 required_status_checks.contexts, "
            f"实际: {len(contexts)}"
        )

    def test_required_status_checks_includes_all_expected_contexts(self, expected: dict):
        """R71 P1-02: 所有期望的 29 个 context 都必须出现。"""
        rsc_rules = [r for r in expected["rules"] if r["type"] == "required_status_checks"]
        assert len(rsc_rules) == 1
        params = rsc_rules[0]["parameters"]
        contexts = [c["context"] for c in params["required_status_checks"]]
        for ctx in EXPECTED_REQUIRED_CHECKS:
            assert ctx in contexts, (
                f"R71 P1-02: required_status_checks 缺少 context: {ctx}"
            )

    def test_required_status_checks_includes_r71_new_contexts(self, expected: dict):
        """R71 P1-02: 特别验证 R71 Wave 4/7 新增的 context(R72 P1-06 移除 Wave 2/5)。"""
        rsc_rules = [r for r in expected["rules"] if r["type"] == "required_status_checks"]
        assert len(rsc_rules) == 1
        params = rsc_rules[0]["parameters"]
        contexts = [c["context"] for c in params["required_status_checks"]]
        for ctx in R71_NEW_CONTEXTS:
            assert ctx in contexts, (
                f"R71 P1-02: required_status_checks 缺少 R71 新增 context: {ctx} "
                f"(Wave 2/4/5/7)"
            )

    def test_bypass_actors_is_empty(self, expected: dict):
        """R71 P1-01: bypass_actors == [](禁止任何角色 bypass,包括 admin)。

        紧急情况通过 scripts/record_break_glass.py 审计日志记录,而非 admin bypass。
        """
        assert expected["bypass_actors"] == [], (
            "R71 P1-01: bypass_actors 必须为空 "
            "(禁止任何角色 bypass,包括 admin;紧急情况用 record_break_glass.py)"
        )

    def test_no_r67_or_r70_legacy_ruleset_names(self, expected: dict):
        """R71 P1-01: 不应再出现 R67 P0-01 / R70 governance-master-protect 旧 ruleset 名。

        旧版配置了两个 ruleset,新版用单一 R71 Solo Founder Ruleset 替换。
        """
        # 整个 JSON 文件的文本不应再含旧 ruleset 名
        text = RULESET_EXPECTED_JSON.read_text(encoding="utf-8")
        assert "R67 P0-01 Branch Immutability Ruleset" not in text, (
            "R71 Wave 6: 不应再含 R67 P0-01 旧 ruleset 名(已被 R71 Solo Founder 替换)"
        )
        assert "r70-governance-master-protect" not in text, (
            "R71 Wave 6: 不应再含 r70-governance-master-protect 旧 ruleset 名"
        )


# ════════════════════════════════════════════════════════════════
# B. branch_protection.expected.json solo-founder 语义
# ════════════════════════════════════════════════════════════════


class TestBranchProtectionExpectedJson:
    """R71 Wave 6: branch_protection.expected.json 符合 solo-founder 语义。"""

    @pytest.fixture(scope="class")
    def bp_expected(self) -> dict:
        assert BP_EXPECTED_JSON.exists(), (
            ".github/branch_protection.expected.json 必须存在"
        )
        return json.loads(BP_EXPECTED_JSON.read_text(encoding="utf-8"))

    def test_file_exists(self):
        """R71 P1-01: branch_protection.expected.json 文件存在。"""
        assert BP_EXPECTED_JSON.exists()

    def test_required_approving_review_count_is_zero(self, bp_expected: dict):
        """R71 P1-01: required_approving_review_count == 0(solo founder,无审批死锁)。

        旧版 R65 P1-12 设为 2,对 solo founder 造成审批死锁。
        """
        rpr = bp_expected["required_pull_request_reviews"]
        assert rpr["required_approving_review_count"] == 0, (
            "R71 P1-01: required_approving_review_count 必须为 0 "
            "(solo founder,无审批死锁)"
        )

    def test_require_code_owner_reviews_is_false(self, bp_expected: dict):
        """R71 P1-01: require_code_owner_reviews == false(CODEOWNERS 保留但不阻断)。"""
        rpr = bp_expected["required_pull_request_reviews"]
        assert rpr["require_code_owner_reviews"] is False, (
            "R71 P1-01: require_code_owner_reviews 必须为 false "
            "(CODEOWNERS 保留但不阻断)"
        )

    def test_required_status_checks_strict_true(self, bp_expected: dict):
        """R71 P1-03: required_status_checks.strict == true(current-SHA)。"""
        rsc = bp_expected["required_status_checks"]
        assert rsc["strict"] is True, (
            "R71 P1-03: required_status_checks.strict 必须为 true "
            "(current-SHA,不允许 stale parent commit)"
        )

    def test_required_status_checks_includes_all_expected_contexts(
        self, bp_expected: dict
    ):
        """R71 P1-02: BP required_status_checks.contexts 覆盖所有期望 context。

        注意:BP API 中使用 strategy.matrix 的 job 会产生形如 "test (3.10)"
        的矩阵展开 context,而 Ruleset API 使用 bare job 名 "test"。本测试
        对矩阵 job 既接受 bare 名,也接受矩阵变体(参见
        BP_MATRIX_EXPANDED_CONTEXTS)。
        """
        contexts = bp_expected["required_status_checks"]["contexts"]
        for ctx in EXPECTED_REQUIRED_CHECKS:
            assert _context_present_in_bp(ctx, contexts), (
                f"R71 P1-02: BP required_status_checks.contexts 缺少: {ctx} "
                f"(或其矩阵展开变体)"
            )

    def test_required_status_checks_includes_r71_new_contexts(
        self, bp_expected: dict
    ):
        """R71 P1-02: BP 包含 R71 Wave 4/7 新增的 context(R72 P1-06 移除 Wave 2/5)。

        R72 P1-06 移除的 tag-only/environment-only check(不在 BP 必需列表中):
        compose-runtime-e2e / sign-image / publish-attestation /
        attestation-semantics-verify / verify-only-3x / migration-binding-gate /
        verify-rc-identity / production-promotion-gate
        """
        contexts = bp_expected["required_status_checks"]["contexts"]
        for ctx in R71_NEW_CONTEXTS:
            assert ctx in contexts, (
                f"R71 P1-02: BP required_status_checks.contexts 缺少 R71 新增: {ctx}"
            )

    def test_enforce_admins_true(self, bp_expected: dict):
        """R71 P1-01: enforce_admins == true(admin 不能 bypass)。

        这是 defense-in-depth — 即使 solo founder,admin 也不能 bypass。
        紧急情况通过 record_break_glass.py 审计日志记录,而非 admin bypass。
        """
        assert bp_expected["enforce_admins"]["enabled"] is True, (
            "R71 P1-01: enforce_admins 必须为 true(admin 不能 bypass)"
        )

    def test_required_signatures_enabled(self, bp_expected: dict):
        """R71 P1-01: required_signatures.enabled == true(signed commits 必需)。"""
        assert bp_expected["required_signatures"]["enabled"] is True, (
            "R71 P1-01: required_signatures 必须启用(signed commits 必需)"
        )

    def test_required_conversation_resolution_true(self, bp_expected: dict):
        """R71 P1-01: required_conversation_resolution.enabled == true。"""
        assert bp_expected["required_conversation_resolution"]["enabled"] is True


# ════════════════════════════════════════════════════════════════
# C. configure_branch_ruleset.sh 静态检查 + --dry-run
# ════════════════════════════════════════════════════════════════


class TestConfigureBranchRulesetScript:
    """R71 Wave 6: configure_branch_ruleset.sh 静态检查 + --dry-run 行为。"""

    @pytest.fixture(scope="class")
    def script_content(self) -> str:
        assert CONFIGURE_RULESET_SH.exists(), (
            "scripts/configure_branch_ruleset.sh 必须存在"
        )
        return CONFIGURE_RULESET_SH.read_text(encoding="utf-8")

    def test_script_exists(self):
        """脚本文件存在。"""
        assert CONFIGURE_RULESET_SH.exists()

    def test_script_has_set_euo_pipefail(self, script_content: str):
        """脚本必须含 `set -euo pipefail`(严格模式)。"""
        assert "set -euo pipefail" in script_content, (
            "configure_branch_ruleset.sh 必须含 set -euo pipefail"
        )

    def test_script_uses_r71_solo_founder_ruleset_name(self, script_content: str):
        """R71 P1-01: 脚本使用单一 'R71 Solo Founder Branch Ruleset' 名称。"""
        assert "R71 Solo Founder Branch Ruleset" in script_content, (
            "configure_branch_ruleset.sh 应使用 R71 Solo Founder Branch Ruleset 名称"
        )

    def test_script_required_reviewers_default_zero(self, script_content: str):
        """R71 P1-01: REQUIRED_REVIEWERS 默认值 = 0(solo founder)。"""
        assert 'REQUIRED_REVIEWERS="${REQUIRED_REVIEWERS:-0}"' in script_content, (
            "R71 P1-01: REQUIRED_REVIEWERS 默认值必须为 0 "
            "(solo founder,无审批死锁)— 旧版 R67=2 / R70=1 已废弃"
        )

    def test_script_no_legacy_required_reviewers_2(self, script_content: str):
        """R71 P1-01: 不应再含 REQUIRED_REVIEWERS:-2(R67 旧默认值)。"""
        assert 'REQUIRED_REVIEWERS="${REQUIRED_REVIEWERS:-2}"' not in script_content, (
            "R71 Wave 6: 不应再含 R67 旧默认值 REQUIRED_REVIEWERS:-2"
        )

    def test_script_require_code_owner_review_false(self, script_content: str):
        """R71 P1-01: payload 中 require_code_owner_review 必须为 false。"""
        assert '"require_code_owner_review": false' in script_content, (
            "R71 P1-01: configure_branch_ruleset.sh payload 中 "
            "require_code_owner_review 必须为 false (solo founder)"
        )

    def test_script_strict_required_status_checks_policy_true(self, script_content: str):
        """R71 P1-03: payload 中 strict_required_status_checks_policy 必须为 true。

        R71 fix: Rulesets API 字段名为 strict_required_status_checks_policy
        (非 strict_merge;后者是 Branch Protection API 旧字段名)。
        """
        assert '"strict_required_status_checks_policy": true' in script_content, (
            "R71 P1-03: configure_branch_ruleset.sh payload 中 "
            "strict_required_status_checks_policy 必须为 true (current-SHA)"
        )

    def test_script_bypass_actors_empty(self, script_content: str):
        """R71 P1-01: payload 中 bypass_actors 必须为空数组。"""
        assert "bypass_actors: []" in script_content, (
            "R71 P1-01: configure_branch_ruleset.sh bypass_actors 必须为空数组"
        )

    def test_script_supports_dry_run(self, script_content: str):
        """脚本支持 --dry-run flag(不调用 gh api)。"""
        assert "--dry-run" in script_content
        assert "DRY_RUN=true" in script_content or 'DRY_RUN="true"' in script_content
        # dry-run 路径不应调用 gh api
        assert "不调用 gh api" in script_content or \
               "未调用任何 gh api" in script_content

    def test_script_supports_help_flag(self, script_content: str):
        """脚本支持 --help / -h flag。"""
        assert "--help" in script_content
        assert "-h" in script_content
        assert "print_help" in script_content

    def test_script_has_idempotency_check(self, script_content: str):
        """R71 P1-01: 脚本保留幂等性检查(EXISTING_RULESET_ID + PUT + POST)。"""
        assert "EXISTING_RULESET_ID" in script_content, (
            "configure_branch_ruleset.sh 应保留 EXISTING_RULESET_ID 幂等性检查"
        )
        assert "PUT" in script_content and "POST" in script_content, (
            "configure_branch_ruleset.sh 应保留 PUT/POST 幂等性逻辑"
        )

    def test_script_includes_29_required_checks(self, script_content: str):
        """R71 P1-02 / R72 P1-06: 脚本默认 REQUIRED_STATUS_CHECKS 包含全部 29 个 context。"""
        for ctx in EXPECTED_REQUIRED_CHECKS:
            assert ctx in script_content, (
                f"configure_branch_ruleset.sh 缺少 REQUIRED_STATUS_CHECKS context: {ctx}"
            )

    def test_script_includes_r71_new_contexts(self, script_content: str):
        """R71 P1-02: 脚本默认 REQUIRED_STATUS_CHECKS 包含 R71 Wave 4/7 新增 context。"""
        for ctx in R71_NEW_CONTEXTS:
            assert ctx in script_content, (
                f"configure_branch_ruleset.sh 缺少 R71 新增 context: {ctx}"
            )

    def test_script_validates_at_least_29_checks(self, script_content: str):
        """R71 P1-02 / R72 P1-06: 脚本校验 REQUIRED_STATUS_CHECKS 至少含 29 个 context
        (R72 P1-06 移除 8 个 tag-only/environment-only check,从 36 缩减到 29)。"""
        # 脚本中应有 "-lt 29" 校验(R72 P1-06 缩减)
        assert "-lt 29" in script_content, (
            "configure_branch_ruleset.sh 应校验 REQUIRED_STATUS_CHECKS 至少含 29 个 context"
        )

    def test_script_self_asserts_required_approving_review_count_zero(
        self, script_content: str
    ):
        """R71 P1-01: 脚本配置后自检断言 required_approving_review_count == 0。

        R71 fix: Rulesets API 字段名为 required_approving_review_count
        (非 required_reviewers;后者是 Branch Protection API 旧字段名)。
        """
        # 自检断言应在脚本中
        assert "required_approving_review_count" in script_content
        assert "add == 0" in script_content or "== 0" in script_content, (
            "configure_branch_ruleset.sh 应自检断言 "
            "required_approving_review_count == 0"
        )

    def test_script_self_asserts_strict_required_status_checks_policy_true(
        self, script_content: str
    ):
        """R71 P1-03: 脚本配置后自检断言 strict_required_status_checks_policy == true。

        R71 fix: Rulesets API 字段名为 strict_required_status_checks_policy
        (非 strict_merge;后者是 Branch Protection API 旧字段名)。
        """
        assert "strict_required_status_checks_policy" in script_content
        assert "add == true" in script_content, (
            "configure_branch_ruleset.sh 应自检断言 "
            "strict_required_status_checks_policy == true"
        )

    def test_script_no_legacy_r67_or_r70_ruleset_names(self, script_content: str):
        """R71 Wave 6: 脚本不应再含 R67 / R70 旧 ruleset 名。"""
        # 旧 R67 ruleset 名不应再出现(允许在注释中说明历史,但不应作为 RULESET_NAME)
        # 检查 RULESET_NAME 行不应含旧名
        for line in script_content.splitlines():
            stripped = line.strip()
            if stripped.startswith("RULESET_NAME="):
                assert "R67 P0-01 Branch Immutability Ruleset" not in stripped, (
                    "R71 Wave 6: RULESET_NAME 不应再含 R67 P0-01 旧 ruleset 名"
                )
                assert "r70-governance-master-protect" not in stripped, (
                    "R71 Wave 6: RULESET_NAME 不应再含 r70-governance-master-protect 旧名"
                )

    @skip_if_no_bash
    def test_script_syntax_ok(self):
        """bash 语法合法。"""
        result = subprocess.run(
            ["bash", "-n", str(CONFIGURE_RULESET_SH)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"bash -n 失败:\n{result.stderr}"

    @skip_if_no_bash
    def test_script_help_exits_zero(self):
        """--help flag 实际执行应 exit 0。"""
        result = subprocess.run(
            ["bash", str(CONFIGURE_RULESET_SH), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, (
            f"--help 应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "用法" in result.stdout or "usage" in result.stdout.lower()
        assert "--dry-run" in result.stdout

    @skip_if_no_bash
    @skip_if_no_jq
    def test_script_dry_run_exits_zero_without_gh(self):
        """--dry-run 模式应 exit 0,不调用 gh api(不需要鉴权)。"""
        result = subprocess.run(
            ["bash", str(CONFIGURE_RULESET_SH), "--dry-run",
             "test-owner", "test-repo"],
            capture_output=True, text=True, timeout=15,
            env={**__import__("os").environ,
                 "GH_TOKEN": "", "GITHUB_TOKEN": ""},
        )
        assert result.returncode == 0, (
            f"--dry-run 应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        # 输出应含 R71 Solo Founder 标识
        assert "R71 Solo Founder" in result.stdout or "DRY RUN" in result.stdout
        # 应含 strict_required_status_checks_policy: true
        assert '"strict_required_status_checks_policy": true' in result.stdout or \
               '"strict_required_status_checks_policy":true' in result.stdout
        # 应含 required_approving_review_count: 0
        assert '"required_approving_review_count": 0' in result.stdout or \
               '"required_approving_review_count":0' in result.stdout
        # 不应调用 gh api
        assert "gh auth" not in result.stderr


# ════════════════════════════════════════════════════════════════
# D. verify_branch_ruleset.sh solo-founder 断言
# ════════════════════════════════════════════════════════════════


class TestVerifyBranchRulesetScript:
    """R71 Wave 6: verify_branch_ruleset.sh 静态断言匹配 solo-founder 语义。"""

    @pytest.fixture(scope="class")
    def script_content(self) -> str:
        assert VERIFY_RULESET_SH.exists(), (
            "scripts/verify_branch_ruleset.sh 必须存在"
        )
        return VERIFY_RULESET_SH.read_text(encoding="utf-8")

    def test_script_exists(self):
        """脚本文件存在。"""
        assert VERIFY_RULESET_SH.exists()

    def test_script_has_set_euo_pipefail(self, script_content: str):
        """脚本必须含 `set -euo pipefail`(严格模式)。"""
        assert "set -euo pipefail" in script_content

    def test_script_uses_r71_solo_founder_ruleset_name(self, script_content: str):
        """R71 P1-01: 验证脚本使用 R71 Solo Founder Branch Ruleset 名称。"""
        assert "R71 Solo Founder Branch Ruleset" in script_content, (
            "verify_branch_ruleset.sh 应使用 R71 Solo Founder Branch Ruleset 名称"
        )

    def test_script_asserts_required_approving_review_count_zero(
        self, script_content: str
    ):
        """R71 P1-01: 验证脚本断言 required_approving_review_count == 0(solo founder)。

        旧版断言 >= 2(R67)/ >= 1(R70),造成 solo founder 审批死锁。

        R71 fix: Rulesets API 字段名为 required_approving_review_count
        (非 required_reviewers;后者是 Branch Protection API 旧字段名)。
        """
        # 应有断言:required_approving_review_count == 0(不再是 >= 2 或 >= 1)
        assert "required_approving_review_count" in script_content
        assert "add == 0" in script_content, (
            "R71 P1-01: verify_branch_ruleset.sh 应断言 "
            "required_approving_review_count == 0 "
            "(solo founder,无审批死锁)"
        )
        # 不应再有 >= 2 的旧断言(针对 required_approving_review_count)
        assert "required_approving_review_count >= 2" not in script_content, (
            "R71 Wave 6: verify_branch_ruleset.sh 不应再有 "
            "required_approving_review_count >= 2 旧断言"
        )

    def test_script_asserts_require_code_owner_review_false(self, script_content: str):
        """R71 P1-01: 验证脚本断言 require_code_owner_review == false。"""
        assert "require_code_owner_review" in script_content
        # 断言应明确 false(不是 true)
        assert "add == false" in script_content, (
            "R71 P1-01: verify_branch_ruleset.sh 应断言 "
            "require_code_owner_review == false (solo founder)"
        )

    def test_script_asserts_strict_required_status_checks_policy_true(
        self, script_content: str
    ):
        """R71 P1-03: 验证脚本断言 strict_required_status_checks_policy == true(current-SHA)。

        R71 fix: Rulesets API 字段名为 strict_required_status_checks_policy
        (非 strict_merge;后者是 Branch Protection API 旧字段名)。
        """
        assert "strict_required_status_checks_policy" in script_content
        assert "add == true" in script_content, (
            "R71 P1-03: verify_branch_ruleset.sh 应断言 "
            "strict_required_status_checks_policy == true"
        )

    def test_script_asserts_bypass_actors_empty(self, script_content: str):
        """R71 P1-01: 验证脚本断言 bypass_actors 为空。"""
        assert "bypass_actors" in script_content
        assert "length == 0" in script_content, (
            "R71 P1-01: verify_branch_ruleset.sh 应断言 bypass_actors 长度为 0"
        )

    def test_script_includes_29_expected_checks(self, script_content: str):
        """R71 P1-02 / R72 P1-06: 验证脚本 EXPECTED_REQUIRED_CHECKS 含全部 29 个 context。"""
        for ctx in EXPECTED_REQUIRED_CHECKS:
            assert ctx in script_content, (
                f"verify_branch_ruleset.sh 缺少 EXPECTED_REQUIRED_CHECKS context: {ctx}"
            )

    def test_script_includes_r71_new_contexts(self, script_content: str):
        """R71 P1-02: 验证脚本特别断言 R71 Wave 4/7 新增 context(R72 P1-06 移除 Wave 2/5)。"""
        for ctx in R71_NEW_CONTEXTS:
            assert ctx in script_content, (
                f"verify_branch_ruleset.sh 缺少 R71 新增 context: {ctx}"
            )

    def test_script_fail_closed_on_missing_ruleset(self, script_content: str):
        """R71 P1-01: 未找到 ruleset 必须 fail-closed(exit 1)。

        保留 R68 P0-02 fail-closed 行为 — 治理配置必须在合并前完成。
        """
        assert "未找到 branch ruleset" in script_content
        assert "exit 1" in script_content

    @skip_if_no_bash
    def test_script_syntax_ok(self):
        """bash 语法合法。"""
        result = subprocess.run(
            ["bash", "-n", str(VERIFY_RULESET_SH)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, f"bash -n 失败:\n{result.stderr}"

    @skip_if_no_bash
    def test_script_help_exits_zero(self):
        """--help flag 实际执行应 exit 0。"""
        result = subprocess.run(
            ["bash", str(VERIFY_RULESET_SH), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, (
            f"--help 应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


# ════════════════════════════════════════════════════════════════
# E. record_break_glass.py 模块结构与常量
# ════════════════════════════════════════════════════════════════


class TestRecordBreakGlassModuleStructure:
    """R71 P1-01: record_break_glass.py 模块结构与常量定义。"""

    @pytest.fixture(scope="class")
    def module(self):
        return _load_record_break_glass_module()

    def test_file_exists(self):
        """R71 P1-01: record_break_glass.py 文件存在。"""
        assert RECORD_BREAK_GLASS_PY.exists(), (
            "scripts/record_break_glass.py 必须存在(R71 Wave 6 新文件)"
        )

    def test_expected_typed_confirmation_constant(self, module):
        """R71 P1-01: EXPECTED_TYPED_CONFIRMATION == 'BREAK-GLASS-EMERGENCY'。"""
        assert module.EXPECTED_TYPED_CONFIRMATION == "BREAK-GLASS-EMERGENCY", (
            "EXPECTED_TYPED_CONFIRMATION 必须精确等于 'BREAK-GLASS-EMERGENCY' "
            "(大小写敏感,强制操作员显式确认紧急情况)"
        )

    def test_exit_code_constants(self, module):
        """R71 P1-01: 退出码常量 0/1/2。"""
        assert module.EXIT_SUCCESS == 0
        assert module.EXIT_VALIDATION_FAILURE == 1
        assert module.EXIT_CLI_ERROR == 2

    def test_required_fields_constant(self, module):
        """R71 P1-01: REQUIRED_FIELDS 列表含 6 个必填字段。"""
        assert "operator" in module.REQUIRED_FIELDS
        assert "sha" in module.REQUIRED_FIELDS
        assert "reason" in module.REQUIRED_FIELDS
        assert "risk" in module.REQUIRED_FIELDS
        assert "rollback_plan" in module.REQUIRED_FIELDS
        assert "typed_confirmation" in module.REQUIRED_FIELDS

    def test_issue_labels_constant(self, module):
        """R72 P1-07: ISSUE_LABELS 含 break-glass 与 audit 标签。"""
        assert "break-glass" in module.ISSUE_LABELS, (
            "ISSUE_LABELS 必须含 'break-glass' 标签(R72 P1-07: GitHub issue 主要审计源)"
        )
        assert "audit" in module.ISSUE_LABELS, (
            "ISSUE_LABELS 必须含 'audit' 标签(R72 P1-07: GitHub issue 主要审计源)"
        )

    def test_gh_cli_binary_constant(self, module):
        """R72 P1-07: GH_CLI_BINARY == 'gh'(用于 shutil.which 检查可用性)。"""
        assert module.GH_CLI_BINARY == "gh", (
            "GH_CLI_BINARY 必须为 'gh'(GitHub CLI 命令名)"
        )

    def test_gh_cli_timeout_constant(self, module):
        """R72 P1-07: GH_CLI_TIMEOUT_SEC 为正整数(防止子进程挂起)。"""
        assert isinstance(module.GH_CLI_TIMEOUT_SEC, int), (
            "GH_CLI_TIMEOUT_SEC 必须为整数"
        )
        assert module.GH_CLI_TIMEOUT_SEC > 0, (
            "GH_CLI_TIMEOUT_SEC 必须为正数(防止 gh issue create 子进程挂起)"
        )

    def test_break_glass_event_has_issue_url_field(self, module):
        """R72 P1-07: BreakGlassEvent dataclass 含 issue_url 字段(默认空字符串)。"""
        event = module.BreakGlassEvent(
            operator="maxiuquan",
            sha="a" * 40,
            reason="emergency production hotfix for CVE-XXXX",
            risk="high — security fix",
            rollback_plan="revert commit and rerun all gates",
            typed_confirmation="BREAK-GLASS-EMERGENCY",
        )
        assert hasattr(event, "issue_url"), (
            "BreakGlassEvent 必须含 issue_url 字段(R72 P1-07: GitHub issue URL)"
        )
        assert event.issue_url == "", (
            "issue_url 默认值必须为空字符串(尚未创建 issue 时)"
        )

    def test_break_glass_event_to_dict_includes_issue_url(self, module):
        """R72 P1-07: BreakGlassEvent.to_dict() 包含 issue_url 键。"""
        event = module.BreakGlassEvent(
            operator="maxiuquan",
            sha="a" * 40,
            reason="emergency production hotfix for CVE-XXXX",
            risk="high — security fix",
            rollback_plan="revert commit and rerun all gates",
            typed_confirmation="BREAK-GLASS-EMERGENCY",
        )
        event.issue_url = "https://github.com/maxiuquan/tgjiema/issues/42"
        data = event.to_dict()
        assert "issue_url" in data, (
            "to_dict() 必须包含 issue_url 键(R72 P1-07)"
        )
        assert data["issue_url"] == \
            "https://github.com/maxiuquan/tgjiema/issues/42"


# ════════════════════════════════════════════════════════════════
# F. record_break_glass.py 输入校验
# ════════════════════════════════════════════════════════════════


class TestRecordBreakGlassValidation:
    """R71 P1-01: record_break_glass.py 输入校验逻辑。"""

    @pytest.fixture
    def module(self):
        return _load_record_break_glass_module()

    @pytest.fixture
    def valid_event_kwargs(self) -> dict:
        """返回一个有效的 break-glass 事件参数字典。"""
        return {
            "operator": "maxiuquan",
            "sha": "abc123def4567890abcdef1234567890abcdef12",
            "reason": "emergency production hotfix for CVE-XXXX",
            "failed_checks": ["verify-rc-identity", "validate-oci-rootfs"],
            "run_url": "https://github.com/maxiuquan/tgjiema/actions/runs/1234567890",
            "risk": "high — bypassing RC identity verification for critical security fix",
            "rollback_plan": "revert commit abc123, rebuild RC, rerun all gates",
            "typed_confirmation": "BREAK-GLASS-EMERGENCY",
        }

    def test_valid_event_passes_validation(self, module, valid_event_kwargs):
        """R71 P1-01: 完整有效事件应通过校验。"""
        event = module.BreakGlassEvent(
            event_id="test-uuid-1234",
            timestamp="2026-07-21T10:00:00+00:00",
            **valid_event_kwargs,
        )
        result = module.validate_event(event)
        assert result.valid is True, (
            f"有效事件应通过校验,错误: {result.errors}"
        )
        assert result.errors == []

    def test_missing_operator_fails(self, module, valid_event_kwargs):
        """R71 P1-01: 缺失 operator 应校验失败。"""
        valid_event_kwargs["operator"] = ""
        event = module.BreakGlassEvent(
            event_id="test-uuid",
            timestamp="2026-07-21T10:00:00+00:00",
            **valid_event_kwargs,
        )
        result = module.validate_event(event)
        assert result.valid is False
        assert any("operator" in err for err in result.errors)

    def test_missing_sha_fails(self, module, valid_event_kwargs):
        """R71 P1-01: 缺失 sha 应校验失败。"""
        valid_event_kwargs["sha"] = ""
        event = module.BreakGlassEvent(
            event_id="test-uuid",
            timestamp="2026-07-21T10:00:00+00:00",
            **valid_event_kwargs,
        )
        result = module.validate_event(event)
        assert result.valid is False
        assert any("sha" in err for err in result.errors)

    def test_missing_reason_fails(self, module, valid_event_kwargs):
        """R71 P1-01: 缺失 reason 应校验失败。"""
        valid_event_kwargs["reason"] = ""
        event = module.BreakGlassEvent(
            event_id="test-uuid",
            timestamp="2026-07-21T10:00:00+00:00",
            **valid_event_kwargs,
        )
        result = module.validate_event(event)
        assert result.valid is False
        assert any("reason" in err for err in result.errors)

    def test_wrong_typed_confirmation_fails(self, module, valid_event_kwargs):
        """R71 P1-01: 错误的 typed_confirmation 应校验失败。"""
        valid_event_kwargs["typed_confirmation"] = "break-glass-emergency"  # 大小写错误
        event = module.BreakGlassEvent(
            event_id="test-uuid",
            timestamp="2026-07-21T10:00:00+00:00",
            **valid_event_kwargs,
        )
        result = module.validate_event(event)
        assert result.valid is False
        assert any("typed_confirmation" in err for err in result.errors)

    def test_invalid_sha_format_fails(self, module, valid_event_kwargs):
        """R71 P1-01: 无效 sha 格式(非 40-char hex)应校验失败。"""
        # 39 char(太短)
        valid_event_kwargs["sha"] = "a" * 39
        event = module.BreakGlassEvent(
            event_id="test-uuid",
            timestamp="2026-07-21T10:00:00+00:00",
            **valid_event_kwargs,
        )
        result = module.validate_event(event)
        assert result.valid is False
        assert any("sha" in err for err in result.errors)

    def test_invalid_sha_non_hex_fails(self, module, valid_event_kwargs):
        """R71 P1-01: 无效 sha(非 hex 字符)应校验失败。"""
        valid_event_kwargs["sha"] = "z" * 40  # 40 char 但非 hex
        event = module.BreakGlassEvent(
            event_id="test-uuid",
            timestamp="2026-07-21T10:00:00+00:00",
            **valid_event_kwargs,
        )
        result = module.validate_event(event)
        assert result.valid is False
        assert any("sha" in err for err in result.errors)

    def test_invalid_run_url_fails(self, module, valid_event_kwargs):
        """R71 P1-01: 无效 run_url(非 GitHub Actions URL)应校验失败。"""
        valid_event_kwargs["run_url"] = "https://example.com/some/path"
        event = module.BreakGlassEvent(
            event_id="test-uuid",
            timestamp="2026-07-21T10:00:00+00:00",
            **valid_event_kwargs,
        )
        result = module.validate_event(event)
        assert result.valid is False
        assert any("run_url" in err for err in result.errors)

    def test_empty_run_url_passes(self, module, valid_event_kwargs):
        """R71 P1-01: run_url 为空应通过校验(可选字段)。"""
        valid_event_kwargs["run_url"] = ""
        event = module.BreakGlassEvent(
            event_id="test-uuid",
            timestamp="2026-07-21T10:00:00+00:00",
            **valid_event_kwargs,
        )
        result = module.validate_event(event)
        assert result.valid is True, (
            f"run_url 为空时应通过校验(可选字段),错误: {result.errors}"
        )

    def test_valid_github_run_url_passes(self, module, valid_event_kwargs):
        """R71 P1-01: 合法 GitHub Actions run URL 应通过校验。"""
        valid_event_kwargs["run_url"] = \
            "https://github.com/maxiuquan/tgjiema/actions/runs/1234567890"
        event = module.BreakGlassEvent(
            event_id="test-uuid",
            timestamp="2026-07-21T10:00:00+00:00",
            **valid_event_kwargs,
        )
        result = module.validate_event(event)
        assert result.valid is True, (
            f"合法 GitHub Actions URL 应通过校验,错误: {result.errors}"
        )


# ════════════════════════════════════════════════════════════════
# G. record_break_glass.py JSONL 持久化
# ════════════════════════════════════════════════════════════════


class TestRecordBreakGlassJsonlPersistence:
    """R71 P1-01: record_break_glass.py JSONL append-only 持久化。"""

    @pytest.fixture
    def module(self):
        return _load_record_break_glass_module()

    def test_record_break_glass_writes_jsonl_file(
        self, module, tmp_path: Path, monkeypatch
    ):
        """R71 P1-01: record_break_glass 应写入 JSONL 文件(append-only)。"""
        # 固定 uuid 与时间戳以确定性
        monkeypatch.setattr(module.uuid, "uuid4", lambda: type(
            "UUID", (), {"__str__": lambda self: "test-uuid-1234"}
        )())
        monkeypatch.setattr(
            module, "_now_iso", lambda: "2026-07-21T10:00:00+00:00"
        )

        output_path = tmp_path / "break-glass-audit.jsonl"
        event, result = module.record_break_glass(
            operator="maxiuquan",
            sha="abc123def4567890abcdef1234567890abcdef12",
            reason="emergency production hotfix for CVE-XXXX",
            failed_checks=["verify-rc-identity", "validate-oci-rootfs"],
            run_url="https://github.com/maxiuquan/tgjiema/actions/runs/1234567890",
            risk="high — bypassing RC identity verification",
            rollback_plan="revert commit abc123, rebuild RC, rerun all gates",
            typed_confirmation="BREAK-GLASS-EMERGENCY",
            output_path=output_path,
            create_issue=False,
        )
        assert result.valid is True
        assert output_path.exists(), "JSONL 文件应被创建"

        # 解析 JSONL 文件 — 应有 1 行
        lines = output_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1, f"应有 1 行 JSONL,实际 {len(lines)} 行"

        data = json.loads(lines[0])
        assert data["event_id"] == "test-uuid-1234"
        assert data["timestamp"] == "2026-07-21T10:00:00+00:00"
        assert data["operator"] == "maxiuquan"
        assert data["sha"] == "abc123def4567890abcdef1234567890abcdef12"
        assert data["reason"] == "emergency production hotfix for CVE-XXXX"
        assert data["failed_checks"] == ["verify-rc-identity", "validate-oci-rootfs"]
        assert data["run_url"] == \
            "https://github.com/maxiuquan/tgjiema/actions/runs/1234567890"
        assert data["risk"] == "high — bypassing RC identity verification"
        assert data["rollback_plan"] == \
            "revert commit abc123, rebuild RC, rerun all gates"
        assert data["typed_confirmation"] == "BREAK-GLASS-EMERGENCY"

    def test_followup_required_always_true(self, module, tmp_path: Path, monkeypatch):
        """R71 P1-01: followup_required 总是 true(所有失败 gates 必须重跑)。"""
        monkeypatch.setattr(module.uuid, "uuid4", lambda: type(
            "UUID", (), {"__str__": lambda self: "uuid-followup"}
        )())
        monkeypatch.setattr(
            module, "_now_iso", lambda: "2026-07-21T10:00:00+00:00"
        )

        output_path = tmp_path / "break-glass-audit.jsonl"
        event, result = module.record_break_glass(
            operator="maxiuquan",
            sha="abc123def4567890abcdef1234567890abcdef12",
            reason="emergency production hotfix",
            failed_checks=["verify-rc-identity"],
            run_url="",
            risk="high — security fix",
            rollback_plan="revert commit and rerun all gates",
            typed_confirmation="BREAK-GLASS-EMERGENCY",
            output_path=output_path,
            create_issue=False,
        )
        assert result.valid is True
        assert event.followup_required is True, (
            "followup_required 必须始终为 true — 所有失败 gates 必须在 break-glass 后重跑"
        )

        # 验证 JSONL 文件中 followup_required=true
        data = json.loads(output_path.read_text(encoding="utf-8").strip())
        assert data["followup_required"] is True

    def test_multiple_events_appended(
        self, module, tmp_path: Path, monkeypatch
    ):
        """R71 P1-01: 多次调用应追加多行到同一 JSONL 文件(append-only)。"""
        # 第一次事件
        monkeypatch.setattr(module.uuid, "uuid4", lambda: type(
            "UUID", (), {"__str__": lambda self: "uuid-event-1"}
        )())
        monkeypatch.setattr(
            module, "_now_iso", lambda: "2026-07-21T10:00:00+00:00"
        )
        output_path = tmp_path / "break-glass-audit.jsonl"
        event1, result1 = module.record_break_glass(
            operator="maxiuquan",
            sha="a" * 40,
            reason="emergency event 1",
            failed_checks=["verify-rc-identity"],
            run_url="",
            risk="high",
            rollback_plan="rollback plan 1",
            typed_confirmation="BREAK-GLASS-EMERGENCY",
            output_path=output_path,
            create_issue=False,
        )
        assert result1.valid is True

        # 第二次事件
        monkeypatch.setattr(module.uuid, "uuid4", lambda: type(
            "UUID", (), {"__str__": lambda self: "uuid-event-2"}
        )())
        monkeypatch.setattr(
            module, "_now_iso", lambda: "2026-07-21T11:00:00+00:00"
        )
        event2, result2 = module.record_break_glass(
            operator="maxiuquan",
            sha="b" * 40,
            reason="emergency event 2",
            failed_checks=["validate-oci-rootfs"],
            run_url="",
            risk="medium",
            rollback_plan="rollback plan 2",
            typed_confirmation="BREAK-GLASS-EMERGENCY",
            output_path=output_path,
            create_issue=False,
        )
        assert result2.valid is True

        # JSONL 文件应有 2 行
        lines = output_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2, (
            f"应有 2 行 JSONL(append-only),实际 {len(lines)} 行"
        )
        data1 = json.loads(lines[0])
        data2 = json.loads(lines[1])
        assert data1["event_id"] == "uuid-event-1"
        assert data2["event_id"] == "uuid-event-2"
        assert data1["sha"] == "a" * 40
        assert data2["sha"] == "b" * 40

    def test_invalid_event_not_written(
        self, module, tmp_path: Path, monkeypatch
    ):
        """R71 P1-01: 校验失败的事件不应写入文件。"""
        monkeypatch.setattr(module.uuid, "uuid4", lambda: type(
            "UUID", (), {"__str__": lambda self: "uuid-should-not-write"}
        )())
        monkeypatch.setattr(
            module, "_now_iso", lambda: "2026-07-21T10:00:00+00:00"
        )

        output_path = tmp_path / "break-glass-audit.jsonl"
        # typed_confirmation 错误
        event, result = module.record_break_glass(
            operator="maxiuquan",
            sha="a" * 40,
            reason="emergency",
            failed_checks=[],
            run_url="",
            risk="high",
            rollback_plan="rollback",
            typed_confirmation="WRONG-CONFIRMATION",  # 错误
            output_path=output_path,
            create_issue=False,
        )
        assert result.valid is False, "校验应失败(typed_confirmation 错误)"
        # 文件不应被创建 / 不应被写入
        assert not output_path.exists(), (
            "校验失败的事件不应写入文件 — JSONL 文件不应被创建"
        )

    def test_jsonl_format_one_object_per_line(
        self, module, tmp_path: Path, monkeypatch
    ):
        """R71 P1-01: JSONL 格式正确(每行一个独立 JSON 对象,以 \\n 分隔)。"""
        monkeypatch.setattr(module.uuid, "uuid4", lambda: type(
            "UUID", (), {"__str__": lambda self: "uuid-format-test"}
        )())
        monkeypatch.setattr(
            module, "_now_iso", lambda: "2026-07-21T10:00:00+00:00"
        )

        output_path = tmp_path / "break-glass-audit.jsonl"
        event, result = module.record_break_glass(
            operator="maxiuquan",
            sha="abc123def4567890abcdef1234567890abcdef12",
            reason="emergency production hotfix for CVE-XXXX",
            failed_checks=["verify-rc-identity"],
            run_url="https://github.com/maxiuquan/tgjiema/actions/runs/123",
            risk="high — security fix",
            rollback_plan="revert commit and rerun all gates",
            typed_confirmation="BREAK-GLASS-EMERGENCY",
            output_path=output_path,
            create_issue=False,
        )
        assert result.valid is True

        content = output_path.read_text(encoding="utf-8")
        # 每行应是合法 JSON,以 \n 结尾
        assert content.endswith("\n"), "JSONL 文件应以 \\n 结尾"
        lines = content.strip().split("\n")
        for line in lines:
            data = json.loads(line)  # 应能成功解析
            assert isinstance(data, dict)
            assert "event_id" in data
            assert "timestamp" in data
            assert "operator" in data
            assert "sha" in data

    def test_failed_checks_parsed_from_csv(self, module):
        """R71 P1-01: --failed-checks 逗号分隔字符串正确解析为列表。"""
        # 测试 _parse_failed_checks 辅助函数
        result = module._parse_failed_checks("verify-rc-identity,validate-oci-rootfs")
        assert result == ["verify-rc-identity", "validate-oci-rootfs"]

        # 空字符串
        assert module._parse_failed_checks("") == []

        # 含空白
        assert module._parse_failed_checks(" a , b , c ") == ["a", "b", "c"]

        # 单个
        assert module._parse_failed_checks("single-check") == ["single-check"]


# ════════════════════════════════════════════════════════════════
# H. record_break_glass.py CLI 退出码
# ════════════════════════════════════════════════════════════════


class TestRecordBreakGlassCliExitCodes:
    """R71 P1-01: record_break_glass.py CLI 退出码 0/1/2。"""

    def test_cli_success_exit_zero(self, tmp_path: Path):
        """R71 P1-01: 有效事件 CLI 调用应 exit 0。"""
        output_path = tmp_path / "break-glass-audit.jsonl"
        result = subprocess.run(
            [
                sys.executable, str(RECORD_BREAK_GLASS_PY),
                "--operator", "maxiuquan",
                "--sha", "abc123def4567890abcdef1234567890abcdef12",
                "--reason", "emergency production hotfix for CVE-XXXX",
                "--failed-checks", "verify-rc-identity,validate-oci-rootfs",
                "--run-url", "https://github.com/maxiuquan/tgjiema/actions/runs/123",
                "--risk", "high — bypassing RC identity verification",
                "--rollback-plan", "revert commit abc123, rebuild RC, rerun all gates",
                "--typed-confirmation", "BREAK-GLASS-EMERGENCY",
                "--no-create-issue",
                "--output", str(output_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"有效事件应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert output_path.exists(), "JSONL 文件应被创建"

    def test_cli_validation_failure_exit_one(self, tmp_path: Path):
        """R71 P1-01: 校验失败应 exit 1(非 0 非 2)。"""
        output_path = tmp_path / "break-glass-audit.jsonl"
        result = subprocess.run(
            [
                sys.executable, str(RECORD_BREAK_GLASS_PY),
                "--operator", "maxiuquan",
                "--sha", "invalid-sha",  # 格式错误
                "--reason", "emergency",
                "--failed-checks", "verify-rc-identity",
                "--run-url", "https://github.com/maxiuquan/tgjiema/actions/runs/123",
                "--risk", "high",
                "--rollback-plan", "rollback",
                "--typed-confirmation", "BREAK-GLASS-EMERGENCY",
                "--no-create-issue",
                "--output", str(output_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1, (
            f"校验失败应 exit 1,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        # 文件不应被创建
        assert not output_path.exists(), (
            "校验失败时不应创建 JSONL 文件"
        )

    def test_cli_wrong_typed_confirmation_exit_one(self, tmp_path: Path):
        """R71 P1-01: 错误的 typed_confirmation 应 exit 1。"""
        output_path = tmp_path / "break-glass-audit.jsonl"
        result = subprocess.run(
            [
                sys.executable, str(RECORD_BREAK_GLASS_PY),
                "--operator", "maxiuquan",
                "--sha", "abc123def4567890abcdef1234567890abcdef12",
                "--reason", "emergency",
                "--failed-checks", "verify-rc-identity",
                "--run-url", "",
                "--risk", "high",
                "--rollback-plan", "rollback plan",
                "--typed-confirmation", "wrong-confirmation",  # 错误
                "--no-create-issue",
                "--output", str(output_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 1, (
            f"错误 typed_confirmation 应 exit 1,实际 {result.returncode}"
        )
        assert not output_path.exists()

    def test_cli_missing_required_arg_exit_two(self, tmp_path: Path):
        """R71 P1-01: 缺失必填 CLI 参数应 exit 2(argparse 错误)。"""
        result = subprocess.run(
            [
                sys.executable, str(RECORD_BREAK_GLASS_PY),
                # 缺失 --operator / --sha / --reason 等
                "--output", str(tmp_path / "break-glass-audit.jsonl"),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 2, (
            f"缺失必填参数应 exit 2(argparse 错误),实际 {result.returncode}"
        )


# ════════════════════════════════════════════════════════════════
# I. 端到端验证:scripts/record_break_glass.py 可被 CI 直接调用
# ════════════════════════════════════════════════════════════════


class TestRecordBreakGlassEndToEnd:
    """R71 P1-01: record_break_glass.py 端到端验证(CI 模拟)。"""

    def test_end_to_end_creates_valid_audit_record(self, tmp_path: Path):
        """R71 P1-01: 模拟 CI 紧急 override 场景,生成有效审计记录。

        场景:运维发现 CVE-XXXX,需在 verify-rc-identity 失败的情况下
        紧急 promote RC 到 production。先调用 record_break_glass.py
        记录审计事件,再执行实际 override 操作。
        """
        output_path = tmp_path / "break-glass-audit.jsonl"
        result = subprocess.run(
            [
                sys.executable, str(RECORD_BREAK_GLASS_PY),
                "--operator", "maxiuquan",
                "--sha", "abc123def4567890abcdef1234567890abcdef12",
                "--reason", "emergency production hotfix for CVE-XXXX",
                "--failed-checks", "verify-rc-identity,validate-oci-rootfs",
                "--run-url", "https://github.com/maxiuquan/tgjiema/actions/runs/1234567890",
                "--risk", "high — bypassing RC identity verification for critical security fix",
                "--rollback-plan", "revert commit abc123, rebuild RC, rerun all gates",
                "--typed-confirmation", "BREAK-GLASS-EMERGENCY",
                "--no-create-issue",
                "--output", str(output_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"端到端调用应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

        # 验证 JSONL 文件内容
        assert output_path.exists(), "JSONL 文件应被创建"
        lines = output_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])

        # 验证所有必填字段
        assert data["operator"] == "maxiuquan"
        assert data["sha"] == "abc123def4567890abcdef1234567890abcdef12"
        assert "CVE-XXXX" in data["reason"]
        assert data["failed_checks"] == ["verify-rc-identity", "validate-oci-rootfs"]
        assert data["run_url"] == \
            "https://github.com/maxiuquan/tgjiema/actions/runs/1234567890"
        assert "high" in data["risk"]
        assert "revert" in data["rollback_plan"]
        assert data["typed_confirmation"] == "BREAK-GLASS-EMERGENCY"
        assert data["followup_required"] is True

        # 验证 event_id 是合法 UUID
        import uuid as _uuid
        _uuid.UUID(data["event_id"])  # 应不抛异常

        # 验证 timestamp 是合法 ISO 8601
        import datetime as _dt
        _dt.datetime.fromisoformat(data["timestamp"])  # 应不抛异常

        # stdout 应输出 JSON 事件
        stdout_data = json.loads(result.stdout)
        assert stdout_data["event_id"] == data["event_id"]
        assert stdout_data["operator"] == "maxiuquan"


# ════════════════════════════════════════════════════════════════
# J. R72 P1-07: create_github_issue() 函数(mocked gh CLI)
# ════════════════════════════════════════════════════════════════


class TestCreateGithubIssue:
    """R72 P1-07: create_github_issue() 通过 gh CLI 创建主要审计源 issue。"""

    @pytest.fixture
    def module(self):
        return _load_record_break_glass_module()

    @pytest.fixture
    def valid_event(self, module):
        """返回一个已校验通过的 BreakGlassEvent(用于 issue 创建测试)。"""
        return module.BreakGlassEvent(
            event_id="test-uuid-issue-1234",
            timestamp="2026-07-24T10:00:00+00:00",
            operator="maxiuquan",
            sha="abc123def4567890abcdef1234567890abcdef12",
            reason="emergency production hotfix for CVE-XXXX",
            failed_checks=["verify-rc-identity", "validate-oci-rootfs"],
            run_url="https://github.com/maxiuquan/tgjiema/actions/runs/123",
            risk="high — bypassing RC identity verification",
            rollback_plan="revert commit abc123, rebuild RC, rerun all gates",
            typed_confirmation="BREAK-GLASS-EMERGENCY",
        )

    def test_issue_title_format(self, module, valid_event):
        """R72 P1-07: _format_issue_title 格式为 '[BREAK-GLASS] <operator> ... for <sha[:12]>'。"""
        title = module._format_issue_title(valid_event)
        assert title.startswith("[BREAK-GLASS]"), (
            "issue 标题必须以 '[BREAK-GLASS]' 开头"
        )
        assert "maxiuquan" in title, "issue 标题必须含 operator 名"
        assert "abc123def456" in title, (
            "issue 标题必须含 sha[:12](12 字符短 SHA)"
        )

    def test_issue_body_contains_all_audit_fields(self, module, valid_event):
        """R72 P1-07: _format_issue_body 包含所有审计字段。"""
        body = module._format_issue_body(valid_event)
        assert "maxiuquan" in body, "issue 正文必须含 operator"
        assert valid_event.sha in body, "issue 正文必须含完整 sha"
        assert "CVE-XXXX" in body, "issue 正文必须含 reason"
        assert "verify-rc-identity" in body, "issue 正文必须含 failed_checks"
        assert "validate-oci-rootfs" in body, "issue 正文必须含 failed_checks"
        assert valid_event.run_url in body, "issue 正文必须含 run_url"
        assert valid_event.risk in body, "issue 正文必须含 risk"
        assert valid_event.rollback_plan in body, "issue 正文必须含 rollback_plan"
        assert valid_event.event_id in body, "issue 正文必须含 event_id"
        assert "primary audit source" in body, (
            "issue 正文必须声明 GitHub issue 是主要审计源(R72 P1-07)"
        )

    def test_create_issue_fails_when_gh_not_found(self, module, valid_event, monkeypatch):
        """R72 P1-07: gh CLI 不可用时 create_github_issue 返回 (False, error)。"""
        monkeypatch.setattr(module.shutil, "which", lambda name: None)
        success, msg = module.create_github_issue(valid_event)
        assert success is False, (
            "gh CLI 不可用时 create_github_issue 应返回 False(fail-closed)"
        )
        assert "gh CLI" in msg or "未找到" in msg, (
            f"错误消息应说明 gh CLI 未找到,实际: {msg}"
        )

    def test_create_issue_succeeds_when_gh_returns_url(self, module, valid_event, monkeypatch):
        """R72 P1-07: gh CLI 返回 issue URL 时 create_github_issue 返回 (True, url)。"""
        monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/gh")

        expected_url = "https://github.com/maxiuquan/tgjiema/issues/42"

        class _FakeResult:
            returncode = 0
            stdout = expected_url + "\n"
            stderr = ""

        def _fake_run(cmd, **kwargs):
            return _FakeResult()

        monkeypatch.setattr(module.subprocess, "run", _fake_run)
        success, url = module.create_github_issue(valid_event)
        assert success is True, (
            f"gh CLI 成功时 create_github_issue 应返回 True,错误: {url}"
        )
        assert url == expected_url, (
            f"返回的 issue URL 应为 gh CLI stdout,期望 {expected_url},实际 {url}"
        )

    def test_create_issue_fails_when_gh_returns_nonzero(self, module, valid_event, monkeypatch):
        """R72 P1-07: gh CLI 返回非零退出码时 create_github_issue 返回 (False, error)。"""
        monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/gh")

        class _FakeResult:
            returncode = 1
            stdout = ""
            stderr = "auth error: not logged in"

        def _fake_run(cmd, **kwargs):
            return _FakeResult()

        monkeypatch.setattr(module.subprocess, "run", _fake_run)
        success, msg = module.create_github_issue(valid_event)
        assert success is False, (
            "gh CLI 非零退出码时 create_github_issue 应返回 False(fail-closed)"
        )
        assert "失败" in msg or "exit=1" in msg, (
            f"错误消息应含失败信息,实际: {msg}"
        )

    def test_create_issue_fails_on_timeout(self, module, valid_event, monkeypatch):
        """R72 P1-07: gh CLI 超时时 create_github_issue 返回 (False, error)。"""
        monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/gh")

        def _fake_run(cmd, **kwargs):
            raise module.subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        monkeypatch.setattr(module.subprocess, "run", _fake_run)
        success, msg = module.create_github_issue(valid_event)
        assert success is False, (
            "gh CLI 超时时 create_github_issue 应返回 False(fail-closed)"
        )
        assert "超时" in msg, f"错误消息应含超时信息,实际: {msg}"

    def test_create_issue_fails_when_stdout_not_url(self, module, valid_event, monkeypatch):
        """R72 P1-07: gh CLI 返回非 URL stdout 时 create_github_issue 返回 (False, error)。"""
        monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/gh")

        class _FakeResult:
            returncode = 0
            stdout = "some random text\n"
            stderr = ""

        def _fake_run(cmd, **kwargs):
            return _FakeResult()

        monkeypatch.setattr(module.subprocess, "run", _fake_run)
        success, msg = module.create_github_issue(valid_event)
        assert success is False, (
            "gh CLI 返回非 URL stdout 时应返回 False(fail-closed)"
        )
        assert "非 URL" in msg or "URL" in msg, (
            f"错误消息应说明输出非 URL,实际: {msg}"
        )


# ════════════════════════════════════════════════════════════════
# K. R72 P1-07: record_break_glass() with create_issue=True(mocked)
# ════════════════════════════════════════════════════════════════


class TestRecordBreakGlassWithIssueCreation:
    """R72 P1-07: record_break_glass() 在 create_issue=True 时的行为(mocked)。"""

    @pytest.fixture
    def module(self):
        return _load_record_break_glass_module()

    def test_issue_url_written_to_jsonl_when_issue_created(
        self, module, tmp_path: Path, monkeypatch
    ):
        """R72 P1-07: issue 创建成功后,issue_url 应写入 JSONL 副本。"""
        monkeypatch.setattr(module.uuid, "uuid4", lambda: type(
            "UUID", (), {"__str__": lambda self: "uuid-with-issue"}
        )())
        monkeypatch.setattr(
            module, "_now_iso", lambda: "2026-07-24T10:00:00+00:00"
        )
        monkeypatch.setattr(module.shutil, "which", lambda name: "/usr/bin/gh")

        expected_issue_url = "https://github.com/maxiuquan/tgjiema/issues/99"

        class _FakeResult:
            returncode = 0
            stdout = expected_issue_url + "\n"
            stderr = ""

        monkeypatch.setattr(
            module.subprocess, "run", lambda cmd, **kw: _FakeResult()
        )

        output_path = tmp_path / "break-glass-audit.jsonl"
        event, result = module.record_break_glass(
            operator="maxiuquan",
            sha="abc123def4567890abcdef1234567890abcdef12",
            reason="emergency production hotfix for CVE-XXXX",
            failed_checks=["verify-rc-identity"],
            run_url="https://github.com/maxiuquan/tgjiema/actions/runs/123",
            risk="high — security fix",
            rollback_plan="revert commit and rerun all gates",
            typed_confirmation="BREAK-GLASS-EMERGENCY",
            output_path=output_path,
            create_issue=True,
        )
        assert result.valid is True, (
            f"issue 创建成功时应通过校验,错误: {result.errors}"
        )
        assert event.issue_url == expected_issue_url, (
            f"event.issue_url 应为创建的 issue URL,期望 {expected_issue_url},"
            f"实际 {event.issue_url}"
        )

        # JSONL 副本中应含 issue_url
        data = json.loads(output_path.read_text(encoding="utf-8").strip())
        assert data["issue_url"] == expected_issue_url, (
            "JSONL 副本必须含 issue_url 字段(R72 P1-07)"
        )

    def test_jsonl_not_written_when_issue_creation_fails(
        self, module, tmp_path: Path, monkeypatch
    ):
        """R72 P1-07: issue 创建失败时,JSONL 副本不应被写入(fail-closed)。

        确保重试不会产生重复 JSONL 条目 — 只有 issue 创建成功后才写 JSONL。
        """
        monkeypatch.setattr(module.uuid, "uuid4", lambda: type(
            "UUID", (), {"__str__": lambda self: "uuid-fail-issue"}
        )())
        monkeypatch.setattr(
            module, "_now_iso", lambda: "2026-07-24T10:00:00+00:00"
        )
        # gh CLI 不可用 → issue 创建失败
        monkeypatch.setattr(module.shutil, "which", lambda name: None)

        output_path = tmp_path / "break-glass-audit.jsonl"
        event, result = module.record_break_glass(
            operator="maxiuquan",
            sha="abc123def4567890abcdef1234567890abcdef12",
            reason="emergency production hotfix for CVE-XXXX",
            failed_checks=["verify-rc-identity"],
            run_url="https://github.com/maxiuquan/tgjiema/actions/runs/123",
            risk="high — security fix",
            rollback_plan="revert commit and rerun all gates",
            typed_confirmation="BREAK-GLASS-EMERGENCY",
            output_path=output_path,
            create_issue=True,
        )
        assert result.valid is False, (
            "issue 创建失败时校验应失败(fail-closed)"
        )
        assert not output_path.exists(), (
            "issue 创建失败时 JSONL 副本不应被写入(fail-closed — "
            "确保重试不会产生重复 JSONL 条目)"
        )
        assert event.issue_url == "", (
            "issue 创建失败时 issue_url 应保持空字符串"
        )
        assert any("issue" in err.lower() for err in result.errors), (
            f"错误消息应说明 issue 创建失败,实际: {result.errors}"
        )

    def test_validation_failure_skips_issue_creation(
        self, module, tmp_path: Path, monkeypatch
    ):
        """R72 P1-07: 校验失败时不应尝试创建 issue(先校验再创建)。"""
        monkeypatch.setattr(module.uuid, "uuid4", lambda: type(
            "UUID", (), {"__str__": lambda self: "uuid-validation-fail"}
        )())
        monkeypatch.setattr(
            module, "_now_iso", lambda: "2026-07-24T10:00:00+00:00"
        )

        # 跟踪 create_github_issue 是否被调用
        issue_called = False

        def _spy_create_issue(event, repo=None):
            nonlocal issue_called
            issue_called = True
            return False, "should not be called"

        monkeypatch.setattr(module, "create_github_issue", _spy_create_issue)

        output_path = tmp_path / "break-glass-audit.jsonl"
        event, result = module.record_break_glass(
            operator="maxiuquan",
            sha="invalid-sha",  # 校验失败
            reason="emergency",
            failed_checks=[],
            run_url="",
            risk="high",
            rollback_plan="rollback",
            typed_confirmation="BREAK-GLASS-EMERGENCY",
            output_path=output_path,
            create_issue=True,
        )
        assert result.valid is False, "校验应失败(sha 格式错误)"
        assert not issue_called, (
            "校验失败时不应调用 create_github_issue(先校验再创建 issue)"
        )
        assert not output_path.exists(), "校验失败时不应创建 JSONL 文件"


# ════════════════════════════════════════════════════════════════
# L. R72 P1-07: CLI --no-create-issue / --repo 标志
# ════════════════════════════════════════════════════════════════


class TestRecordBreakGlassCliIssueFlags:
    """R72 P1-07: record_break_glass.py CLI --no-create-issue / --repo 标志。"""

    def test_cli_no_create_issue_flag_accepted(self, tmp_path: Path):
        """R72 P1-07: --no-create-issue 标志被接受且跳过 issue 创建(exit 0)。"""
        output_path = tmp_path / "break-glass-audit.jsonl"
        result = subprocess.run(
            [
                sys.executable, str(RECORD_BREAK_GLASS_PY),
                "--operator", "maxiuquan",
                "--sha", "abc123def4567890abcdef1234567890abcdef12",
                "--reason", "emergency production hotfix for CVE-XXXX",
                "--failed-checks", "verify-rc-identity",
                "--run-url", "https://github.com/maxiuquan/tgjiema/actions/runs/123",
                "--risk", "high — security fix",
                "--rollback-plan", "revert commit and rerun all gates",
                "--typed-confirmation", "BREAK-GLASS-EMERGENCY",
                "--no-create-issue",
                "--output", str(output_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"--no-create-issue 模式应 exit 0,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert output_path.exists(), "JSONL 文件应被创建"
        # issue_url 应为空(未创建 issue)
        data = json.loads(output_path.read_text(encoding="utf-8").strip())
        assert data["issue_url"] == "", (
            "--no-create-issue 模式下 issue_url 应为空字符串"
        )

    def test_cli_repo_flag_accepted(self, tmp_path: Path):
        """R72 P1-07: --repo 标志被接受(配合 --no-create-issue 使用,exit 0)。"""
        output_path = tmp_path / "break-glass-audit.jsonl"
        result = subprocess.run(
            [
                sys.executable, str(RECORD_BREAK_GLASS_PY),
                "--operator", "maxiuquan",
                "--sha", "abc123def4567890abcdef1234567890abcdef12",
                "--reason", "emergency production hotfix for CVE-XXXX",
                "--failed-checks", "verify-rc-identity",
                "--run-url", "https://github.com/maxiuquan/tgjiema/actions/runs/123",
                "--risk", "high — security fix",
                "--rollback-plan", "revert commit and rerun all gates",
                "--typed-confirmation", "BREAK-GLASS-EMERGENCY",
                "--no-create-issue",
                "--repo", "maxiuquan/tgjiema",
                "--output", str(output_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            f"--repo 标志应被接受,实际 {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert output_path.exists(), "JSONL 文件应被创建"

    def test_cli_help_lists_no_create_issue_flag(self):
        """R72 P1-07: --help 输出中应列出 --no-create-issue 标志。"""
        result = subprocess.run(
            [sys.executable, str(RECORD_BREAK_GLASS_PY), "--help"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "--no-create-issue" in result.stdout, (
            "--help 输出应包含 --no-create-issue 标志说明(R72 P1-07)"
        )
        assert "--repo" in result.stdout, (
            "--help 输出应包含 --repo 标志说明(R72 P1-07)"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
