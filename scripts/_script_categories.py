"""R67 P1-08: scripts/ 目录跳过策略 — 区分 offline recovery tool 与测试脚本。

审计背景(R67 P1-08):
    tests/ 整体跳过合理(测试逃生舱),但 scripts/ 不应整体跳过 —
    scripts/ 中混合了三类脚本:
      1. **离线恢复/生产运维工具**(.sh 为主)— 生产事故时由 SRE 执行,
         触达生产数据,调用 restore 路径,生成签名生产证据。
         必须与生产代码同等接受 capability/approval/MFA 审查。
      2. **CI 静态门禁扫描器**(.py)— 本身就是 gate,扫描自己会产生噪声。
         整体跳过可以接受,但应按"显式文件 allowlist"而非"目录整体跳过"。
      3. **治理配置/验证脚本**(.sh)— 配置或验证 GitHub branch protection /
         ruleset / docker digest / 依赖哈希 / git source governance。
         触达生产治理状态,必须接受凭证/硬编码字符串/sink-boundary 审查。

整改方案:
    - 提供三份显式清单:OFFLINE_RECOVERY_TOOLS / GATE_SCANNERS /
      GOVERNANCE_SCRIPTS
    - 提供 `is_gate_scanner(path)` 用于判断脚本是否为 gate 自身(可跳过)
    - 提供 `is_skippable_script(path)` 用于判断脚本是否可整体跳过
      (仅 GATE_SCANNERS 可跳过;OFFLINE_RECOVERY_TOOLS 与
      GOVERNANCE_SCRIPTS 必须被扫描)
    - 旧版"scripts/ 整体跳过"的扫描器应迁移到本模块的细粒度判断

使用示例:
    from scripts._script_categories import is_skippable_script

    if is_skippable_script(rel_path):
        return  # 跳过 gate 自身或其辅助生成器
    # 否则继续扫描(包括 OFFLINE_RECOVERY_TOOLS 与 GOVERNANCE_SCRIPTS)
"""
from __future__ import annotations

from pathlib import PurePosixPath


# ════════════════════════════════════════════════════════════════
# 1. 离线恢复/生产运维工具 — 必须被扫描(P1-08 关注重点)
# ════════════════════════════════════════════════════════════════

# R67 P1-08: 这些是"真实运维入口"— 生产事故时由 SRE 执行,
# 触达生产数据,调用 restore 路径,或生成签名生产证据。
# 任何 scanner 不得跳过这些文件。
OFFLINE_RECOVERY_TOOLS: frozenset[str] = frozenset({
    "scripts/full_machine_recovery.sh",
    "scripts/blank_vps_recovery_test.sh",
    "scripts/chaos_bot_fault_injection.sh",
    "scripts/ru_72h_verification.sh",
    "scripts/soak_test_7day.sh",
})


# ════════════════════════════════════════════════════════════════
# 2. CI 静态门禁扫描器与生成器 — 可跳过自扫描(避免噪声)
# ════════════════════════════════════════════════════════════════

# R67 P1-08: 这些是 gate 自身或其辅助生成器,扫描自己会产生自引用噪声。
# 但是 — `check_restore_no_legacy_writer.py` 仍会扫描这些文件,
# 防止未来某个 helper 静默调用 legacy writer。
GATE_SCANNERS: frozenset[str] = frozenset({
    # 静态门禁扫描器
    "scripts/check_a11y_matrix_enforcement.py",
    "scripts/check_a11y_precheck.py",
    "scripts/check_branch_protection_contexts.py",
    "scripts/check_button_flow_real_ux.py",
    "scripts/check_button_handler_gate.py",
    "scripts/check_button_nonce_coverage.py",
    "scripts/check_commandbus_gate.py",
    "scripts/check_crdb_ru_72h_attribution.py",
    "scripts/check_crdb_ru_threshold.py",
    "scripts/check_effect_receipt_coverage.py",
    "scripts/check_error_codes.py",
    "scripts/check_error_codes_locale_schema.py",
    "scripts/check_error_protocol.py",
    "scripts/check_error_registry.py",
    "scripts/check_i18n_icu_precompile.py",
    "scripts/check_i18n_key_symmetry.py",
    "scripts/check_i18n_strict_export_boundary.py",
    "scripts/check_mfa_verifier_gate.py",
    "scripts/check_migration_manifest.py",
    "scripts/check_notification_legacy_send.py",
    "scripts/check_password_safety.py",
    "scripts/check_restore_no_legacy_writer.py",
    "scripts/check_restore_no_skip.py",
    "scripts/check_schema.py",
    "scripts/check_sink_import_boundary.py",
    "scripts/collect_skip_inventory.py",
    "scripts/scan_hardcoded_strings.py",
    "scripts/verify_attestation_semantics.py",
    "scripts/verify_file_records_status_index.py",
    "scripts/verify_i18n_keys.py",
    "scripts/verify_rc_3x.py",
    "scripts/verify_supply_chain.py",
    # 辅助生成器(inventory / baseline / allowlist / evidence)
    "scripts/export_admin_routes.py",
    "scripts/export_error_codes_frontend.py",
    "scripts/export_ru_report.py",
    "scripts/generate_a11y_matrix_cases.py",
    "scripts/generate_a11y_test_cases.py",
    "scripts/generate_button_handler_inventory.py",
    "scripts/generate_error_protocol_allowlist.py",
    "scripts/generate_migration_manifest.py",
    "scripts/generate_production_evidence.py",
    "scripts/generate_release_manifest.py",
    "scripts/generate_sbom.py",
    "scripts/migrate_i18n_strings.py",
    "scripts/migrate_list_to_stream.py",
    "scripts/pseudolocalize_test.py",
    "scripts/regenerate_scanner_whitelist_digests.py",
    # 本模块自身
    "scripts/_script_categories.py",
})


# ════════════════════════════════════════════════════════════════
# 3. 治理配置/验证脚本 — 必须被扫描(触达生产治理状态)
# ════════════════════════════════════════════════════════════════

# R67 P1-08: 这些脚本配置或验证生产 GitHub 治理(branch protection /
# ruleset / tag ruleset / docker digest / 依赖哈希 / git source governance)。
# 触达生产治理状态,必须接受凭证/硬编码字符串/sink-boundary 审查。
GOVERNANCE_SCRIPTS: frozenset[str] = frozenset({
    "scripts/configure_branch_protection.sh",
    "scripts/configure_branch_ruleset.sh",
    "scripts/configure_tag_ruleset.sh",
    "scripts/detect_branch_protection_contexts.sh",
    "scripts/verify_branch_protection.sh",
    "scripts/verify_branch_ruleset.sh",
    "scripts/verify_deps.sh",
    "scripts/verify_docker_digest.sh",
    "scripts/verify_git_source_governance.sh",
    "scripts/verify_tag_ruleset.sh",
})


# ════════════════════════════════════════════════════════════════
# 4. 公共 API
# ════════════════════════════════════════════════════════════════


def is_offline_recovery_tool(rel_path: str) -> bool:
    """判断给定相对路径是否为离线恢复/生产运维工具(必须被扫描)。

    Args:
        rel_path: 相对仓库根的 POSIX 路径(如 "scripts/full_machine_recovery.sh")

    Returns:
        True 表示该路径是离线恢复工具(任何 scanner 不得跳过)
    """
    return rel_path in OFFLINE_RECOVERY_TOOLS


def is_governance_script(rel_path: str) -> bool:
    """判断给定相对路径是否为治理配置/验证脚本(必须被扫描)。

    Args:
        rel_path: 相对仓库根的 POSIX 路径

    Returns:
        True 表示该路径是治理脚本(任何 scanner 不得跳过)
    """
    return rel_path in GOVERNANCE_SCRIPTS


def is_gate_scanner(rel_path: str) -> bool:
    """判断给定相对路径是否为 CI 静态门禁扫描器/生成器(可跳过自扫描)。

    Args:
        rel_path: 相对仓库根的 POSIX 路径

    Returns:
        True 表示该路径是 gate 自身或其辅助生成器(可跳过)
    """
    return rel_path in GATE_SCANNERS


def is_skippable_script(rel_path: str) -> bool:
    """判断 scripts/ 下的脚本是否可被 scanner 整体跳过。

    R67 P1-08 整改要点:
        - tests/ 整体跳过合理(测试逃生舱)
        - scripts/ 整体跳过不合理 — scripts/ 含真实运维入口
        - 仅 GATE_SCANNERS 可跳过(避免自引用噪声)
        - OFFLINE_RECOVERY_TOOLS 与 GOVERNANCE_SCRIPTS 必须被扫描

    Args:
        rel_path: 相对仓库根的 POSIX 路径

    Returns:
        True 表示该路径可跳过(仅 GATE_SCANNERS);
        False 表示该路径必须被扫描(OFFLINE_RECOVERY_TOOLS /
        GOVERNANCE_SCRIPTS / 未分类脚本)
    """
    # 标准化路径(避免前导 "./" 或反斜杠)
    normalized = _normalize_rel_path(rel_path)
    if not normalized.startswith("scripts/"):
        return False  # 非 scripts/ 文件不在本函数处理范围
    # 离线恢复工具:必须扫描
    if normalized in OFFLINE_RECOVERY_TOOLS:
        return False
    # 治理脚本:必须扫描
    if normalized in GOVERNANCE_SCRIPTS:
        return False
    # gate 自身或其辅助生成器:可跳过
    if normalized in GATE_SCANNERS:
        return True
    # 未分类的 scripts/ 文件 — 默认必须扫描(fail-closed,
    # 防止新加入 scripts/ 的运维脚本被默认跳过)
    return False


def get_required_scan_scripts() -> frozenset[str]:
    """返回所有必须被扫描的 scripts/ 文件清单。

    用于测试验证:任何 scanner 的跳过清单不得包含这些文件。
    """
    return OFFLINE_RECOVERY_TOOLS | GOVERNANCE_SCRIPTS


def _normalize_rel_path(rel_path: str) -> str:
    """标准化相对路径(POSIX,无前导 "./")。"""
    if not rel_path:
        return ""
    p = PurePosixPath(rel_path)
    parts = p.parts
    if parts and parts[0] in (".", "/"):
        parts = parts[1:]
    return "/".join(parts) if parts else ""
