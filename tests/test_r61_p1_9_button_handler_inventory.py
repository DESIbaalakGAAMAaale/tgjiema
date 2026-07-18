"""R61 P1-09 终审测试:按钮 handler 清单生成器 + 静态门禁 + sidecar 元数据。

审计需求 P1-09: 按钮安全基础设施已存在,但需要 PROVE 所有高风险 handler
都走 ButtonFlow/CommandBus,而不只是关键路径。

测试范围:
  1. test_generator_produces_nonempty_inventory:
     生成器在真实代码库上产出非空 inventory,包含已知高风险 handler
     (toggle_ban / delete_file / takedown_report / maintenance_action /
     _handle_restore_action / _handle_delete_file_action / _handle_report_action)
  2. test_generator_inventory_is_deterministic:
     生成器两次运行产出完全相同的 JSON(确定性,可 CI diff)
  3. test_gate_flags_synthetic_high_risk_bypass:
     门禁能 flag 合成的高风险 bypass(直接调 update_user_and_invalidate,
     未走 CommandBus)→ Rule A 违规
  4. test_gate_passes_commanbus_routed_handler:
     门禁能通过走 CommandBus 的 handler(make_ban_user_command + bus.execute)
  5. test_gate_rule_b_flags_missing_metadata:
     门禁 Rule B 能 flag sidecar 元数据缺失的高风险 handler
  6. test_gate_rule_c_flags_unsigned_callback:
     门禁 Rule C 能 flag 高风险 callback handler 未用签名绑定 API
  7. test_sidecar_metadata_fields_complete:
     所有 sidecar 条目字段齐全(10 个必需字段)
  8. test_baseline_ratchet_no_new_violations:
     默认 baseline 模式下,已知违规不触发 exit 1(ratchet 下降)
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


# ════════════════════════════════════════════════════════════════
# 辅助:动态加载脚本为独立模块
# ════════════════════════════════════════════════════════════════


def _load_generator_module():
    """加载 generate_button_handler_inventory.py 为独立模块。"""
    spec = importlib.util.spec_from_file_location(
        "_r61_p1_9_generator_test",
        SCRIPTS_DIR / "generate_button_handler_inventory.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_gate_module():
    """加载 check_button_handler_gate.py 为独立模块。"""
    spec = importlib.util.spec_from_file_location(
        "_r61_p1_9_gate_test",
        SCRIPTS_DIR / "check_button_handler_gate.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_inventory_artifact() -> dict:
    """加载已生成的 inventory JSON 产物。"""
    inventory_path = SCRIPTS_DIR / "button_handler_inventory.json"
    if not inventory_path.exists():
        pytest.skip(
            f"inventory 产物不存在: {inventory_path}\n"
            f"请先运行: python scripts/generate_button_handler_inventory.py"
        )
    return json.loads(inventory_path.read_text(encoding="utf-8"))


def _load_metadata_artifact() -> dict:
    """加载元数据 sidecar JSON 产物。"""
    metadata_path = SCRIPTS_DIR / "button_handler_metadata.json"
    if not metadata_path.exists():
        pytest.skip(f"元数据 sidecar 不存在: {metadata_path}")
    return json.loads(metadata_path.read_text(encoding="utf-8"))


# ════════════════════════════════════════════════════════════════
# 1. 生成器产出非空 inventory
# ════════════════════════════════════════════════════════════════


class TestGeneratorProducesNonemptyInventory:
    """生成器在真实代码库上产出非空 inventory,包含已知高风险 handler。"""

    def test_inventory_has_handlers_list(self):
        """inventory JSON 应包含 handlers 列表且非空。"""
        inventory = _load_inventory_artifact()
        assert "handlers" in inventory
        assert isinstance(inventory["handlers"], list)
        assert len(inventory["handlers"]) > 0, (
            "inventory handlers 列表不应为空"
        )

    def test_inventory_has_high_risk_handlers(self):
        """inventory 应包含至少 1 个高风险 handler。"""
        inventory = _load_inventory_artifact()
        high_risk = [h for h in inventory["handlers"] if h.get("is_high_risk")]
        assert len(high_risk) >= 1, (
            "应至少有 1 个高风险 handler(如 toggle_ban/delete_file/takedown_report)"
        )

    def test_inventory_contains_known_commanbus_handlers(self):
        """inventory 应包含已知的走 CommandBus 的高风险 handler。"""
        inventory = _load_inventory_artifact()
        handler_names = {h["handler"] for h in inventory["handlers"]}
        expected = {
            "toggle_ban",
            "delete_file",
            "takedown_report",
            "maintenance_action",
            "_handle_restore_action",
            "_handle_delete_file_action",
        }
        missing = expected - handler_names
        assert not missing, (
            f"inventory 缺少已知高风险 handler: {missing}\n"
            f"现有 handler: {sorted(handler_names)}"
        )

    def test_commanbus_handlers_routed_correctly(self):
        """走 CommandBus 的 handler 应标记 routes_through_command_bus=True。"""
        inventory = _load_inventory_artifact()
        for h in inventory["handlers"]:
            if h["handler"] in (
                "toggle_ban", "delete_file", "takedown_report",
                "maintenance_action",
                "_handle_restore_action", "_handle_delete_file_action",
            ):
                assert h["is_high_risk"], (
                    f"{h['handler']} 应为高风险"
                )
                assert h["routes_through_command_bus"], (
                    f"{h['handler']} 应走 CommandBus(routes_through_command_bus=True),"
                    f"实际: {h}"
                )

    def test_report_action_handler_flagged_as_bypass(self):
        """_handle_report_action 直接调破坏性 API,应标记 bypass_reason。"""
        inventory = _load_inventory_artifact()
        report_handler = None
        for h in inventory["handlers"]:
            if h["handler"] == "_handle_report_action":
                report_handler = h
                break
        assert report_handler is not None, "_handle_report_action 应在 inventory 中"
        assert report_handler["is_high_risk"], (
            "_handle_report_action 应为高风险(直接调 update_user_and_invalidate)"
        )
        assert report_handler["bypass_reason"] is not None, (
            "_handle_report_action 应有 bypass_reason(Rule A 违规)"
        )
        assert report_handler["calls_destructive_api"], (
            "_handle_report_action 应标记 calls_destructive_api=True"
        )


# ════════════════════════════════════════════════════════════════
# 2. 生成器确定性
# ════════════════════════════════════════════════════════════════


class TestGeneratorDeterministic:
    """生成器两次运行产出完全相同的 JSON(确定性,可 CI diff)。"""

    def test_two_runs_produce_identical_output(self):
        """连续两次调用 generate_inventory() 应产出完全相同的字典。"""
        mod = _load_generator_module()
        inv1 = mod.generate_inventory()
        inv2 = mod.generate_inventory()
        # 比较序列化后的 JSON 字符串(确保 key 顺序也一致)
        json1 = json.dumps(inv1, ensure_ascii=False, sort_keys=True)
        json2 = json.dumps(inv2, ensure_ascii=False, sort_keys=True)
        assert json1 == json2, (
            "生成器应确定性输出: 两次运行结果应完全相同"
        )

    def test_generated_at_is_deterministic_placeholder(self):
        """generated_at 应为固定占位符,不含时间戳。"""
        inventory = _load_inventory_artifact()
        assert inventory["generated_at"] == "R61-P1-09-deterministic", (
            f"generated_at 应为固定占位符,实际: {inventory['generated_at']}"
        )

    def test_handlers_sorted_deterministically(self):
        """handlers 列表应按 (file, line, handler, entry_type) 排序。"""
        inventory = _load_inventory_artifact()
        handlers = inventory["handlers"]
        keys = [
            (h["file"], h["line"], h["handler"], h["entry_type"])
            for h in handlers
        ]
        assert keys == sorted(keys), (
            "handlers 应按 (file, line, handler, entry_type) 确定性排序"
        )


# ════════════════════════════════════════════════════════════════
# 3. 门禁能 flag 合成的高风险 bypass
# ════════════════════════════════════════════════════════════════


class TestGateFlagsSyntheticBypass:
    """门禁能 flag 合成的高风险 bypass(直接调破坏性 API,未走 CommandBus)。"""

    def test_gate_flags_destructive_bypass(self, tmp_path):
        """合成的高风险 handler 直接调 update_user_and_invalidate → Rule A 违规。"""
        # 构造合成的 inventory(模拟生成器输出)
        synthetic_inventory = {
            "generated_at": "test",
            "handler_count": 1,
            "high_risk_count": 1,
            "handlers": [
                {
                    "handler": "bad_ban_handler",
                    "file": "bots/bad.py",
                    "line": 10,
                    "entry_type": "callback_sub_dispatcher",
                    "route_or_pattern": "ban:",
                    "action_type": "ban",
                    "is_high_risk": True,
                    "is_dispatcher": False,
                    "routes_through_command_bus": False,
                    "routes_through_button_flow": False,
                    "calls_destructive_api": True,
                    "destructive_api": "update_user_and_invalidate",
                    "uses_signed_token_api": False,
                    "parent_handler": "bad_callback",
                    "bypass_reason": "test bypass",
                },
            ],
        }
        # 空元数据(无 sidecar 条目,无 baseline)
        empty_metadata = {"handlers": {}, "baseline": {"violations": []}}

        gate = _load_gate_module()
        exit_code, new_violations, all_violations = gate.check(
            inventory=synthetic_inventory,
            metadata=empty_metadata,
            strict=True,
        )

        assert exit_code == 1, (
            f"合成的高风险 bypass 应 exit 1,实际 exit_code={exit_code}"
        )
        # 应有 Rule A 违规
        rule_a = [v for v in new_violations if v["rule"] == "A"]
        assert len(rule_a) >= 1, (
            f"应有至少 1 处 Rule A 违规,实际: {new_violations}"
        )
        assert rule_a[0]["violation_type"] == "RULE_A_DESTRUCTIVE_BYPASS"
        assert rule_a[0]["handler"] == "bad_ban_handler"

    def test_gate_flags_no_state_machine_bypass(self, tmp_path):
        """高风险 handler 既不走 CommandBus 也不调破坏性 API → Rule A 违规(NO_STATE_MACHINE)。"""
        synthetic_inventory = {
            "generated_at": "test",
            "handler_count": 1,
            "high_risk_count": 1,
            "handlers": [
                {
                    "handler": "mystery_high_risk",
                    "file": "bots/mystery.py",
                    "line": 5,
                    "entry_type": "fastapi_post",
                    "route_or_pattern": "/mystery/delete",
                    "action_type": "delete",
                    "is_high_risk": True,
                    "is_dispatcher": False,
                    "routes_through_command_bus": False,
                    "routes_through_button_flow": False,
                    "calls_destructive_api": False,
                    "destructive_api": None,
                    "uses_signed_token_api": False,
                    "parent_handler": None,
                    "bypass_reason": "test",
                },
            ],
        }
        empty_metadata = {"handlers": {}, "baseline": {"violations": []}}

        gate = _load_gate_module()
        exit_code, new_violations, all_violations = gate.check(
            inventory=synthetic_inventory,
            metadata=empty_metadata,
            strict=True,
        )

        assert exit_code == 1
        rule_a = [v for v in new_violations if v["rule"] == "A"]
        assert len(rule_a) >= 1
        assert rule_a[0]["violation_type"] == "RULE_A_NO_STATE_MACHINE"


# ════════════════════════════════════════════════════════════════
# 4. 门禁能通过走 CommandBus 的 handler
# ════════════════════════════════════════════════════════════════


class TestGatePassesCommandBusHandler:
    """门禁能通过走 CommandBus 的 handler(make_*_command + bus.execute)。"""

    def test_gate_passes_commanbus_routed_handler(self):
        """走 CommandBus 的高风险 handler → 无 Rule A 违规。"""
        synthetic_inventory = {
            "generated_at": "test",
            "handler_count": 1,
            "high_risk_count": 1,
            "handlers": [
                {
                    "handler": "good_ban_handler",
                    "file": "admin/__init__.py",
                    "line": 100,
                    "entry_type": "fastapi_post",
                    "route_or_pattern": "/users/{user_id}/ban",
                    "action_type": "ban",
                    "is_high_risk": True,
                    "is_dispatcher": False,
                    "routes_through_command_bus": True,
                    "routes_through_button_flow": False,
                    "calls_destructive_api": False,
                    "destructive_api": None,
                    "uses_signed_token_api": False,
                    "parent_handler": None,
                    "bypass_reason": None,
                },
            ],
        }
        # 提供完整 sidecar 元数据(Rule B 通过)
        good_metadata = {
            "handlers": {
                "good_ban_handler": {
                    "action_type": "ban",
                    "rbac": "PERM_USERS_BAN",
                    "mfa": "required",
                    "two_person_approval": "required",
                    "idempotency_key": ["action_id", "request_hash"],
                    "state_transitions": "pending->approved->executed",
                    "timeout": "86400s",
                    "cancellation": "supported",
                    "localization_confirmation": "admin.ban.confirm",
                    "audit_schema": "audit_log(action=ban)",
                },
            },
            "baseline": {"violations": []},
        }

        gate = _load_gate_module()
        exit_code, new_violations, all_violations = gate.check(
            inventory=synthetic_inventory,
            metadata=good_metadata,
            strict=True,
        )

        # FastAPI POST 端点不走 callback 签名(Rule C 不适用),
        # 走 CommandBus(Rule A 通过),有完整元数据(Rule B 通过)
        rule_a = [v for v in new_violations if v["rule"] == "A"]
        rule_b = [v for v in new_violations if v["rule"] == "B"]
        assert len(rule_a) == 0, (
            f"走 CommandBus 的 handler 不应有 Rule A 违规: {rule_a}"
        )
        assert len(rule_b) == 0, (
            f"有完整 sidecar 元数据的 handler 不应有 Rule B 违规: {rule_b}"
        )


# ════════════════════════════════════════════════════════════════
# 5. Rule B 能 flag sidecar 元数据缺失
# ════════════════════════════════════════════════════════════════


class TestGateRuleBFlagsMissingMetadata:
    """门禁 Rule B 能 flag sidecar 元数据缺失的高风险 handler。"""

    def test_rule_b_flags_missing_handler_entry(self):
        """高风险 handler 不在 sidecar 中 → RULE_B_METADATA_MISSING。"""
        synthetic_inventory = {
            "generated_at": "test",
            "handler_count": 1,
            "high_risk_count": 1,
            "handlers": [
                {
                    "handler": "undocumented_handler",
                    "file": "bots/new.py",
                    "line": 1,
                    "entry_type": "fastapi_post",
                    "route_or_pattern": "/purge",
                    "action_type": "purge",
                    "is_high_risk": True,
                    "is_dispatcher": False,
                    "routes_through_command_bus": True,
                    "routes_through_button_flow": False,
                    "calls_destructive_api": False,
                    "destructive_api": None,
                    "uses_signed_token_api": False,
                    "parent_handler": None,
                    "bypass_reason": None,
                },
            ],
        }
        empty_metadata = {"handlers": {}, "baseline": {"violations": []}}

        gate = _load_gate_module()
        exit_code, new_violations, _ = gate.check(
            inventory=synthetic_inventory,
            metadata=empty_metadata,
            strict=True,
        )

        rule_b = [v for v in new_violations if v["rule"] == "B"]
        assert len(rule_b) >= 1, (
            "缺少 sidecar 条目应触发 RULE_B_METADATA_MISSING"
        )
        assert rule_b[0]["violation_type"] == "RULE_B_METADATA_MISSING"
        assert "missing_fields" in rule_b[0]

    def test_rule_b_flags_incomplete_fields(self):
        """高风险 handler sidecar 条目字段不全 → RULE_B_FIELDS_INCOMPLETE。"""
        synthetic_inventory = {
            "generated_at": "test",
            "handler_count": 1,
            "high_risk_count": 1,
            "handlers": [
                {
                    "handler": "incomplete_handler",
                    "file": "bots/x.py",
                    "line": 1,
                    "entry_type": "fastapi_post",
                    "route_or_pattern": "/x",
                    "action_type": "delete",
                    "is_high_risk": True,
                    "is_dispatcher": False,
                    "routes_through_command_bus": True,
                    "routes_through_button_flow": False,
                    "calls_destructive_api": False,
                    "destructive_api": None,
                    "uses_signed_token_api": False,
                    "parent_handler": None,
                    "bypass_reason": None,
                },
            ],
        }
        # 故意缺少 mfa / two_person_approval / timeout 字段
        incomplete_metadata = {
            "handlers": {
                "incomplete_handler": {
                    "action_type": "delete",
                    "rbac": "PERM_DELETE",
                    # 缺 mfa
                    # 缺 two_person_approval
                    "idempotency_key": ["action_id"],
                    "state_transitions": "pending->executed",
                    # 缺 timeout
                    "cancellation": "not supported",
                    "localization_confirmation": "x.confirm",
                    "audit_schema": "audit_log",
                },
            },
            "baseline": {"violations": []},
        }

        gate = _load_gate_module()
        exit_code, new_violations, _ = gate.check(
            inventory=synthetic_inventory,
            metadata=incomplete_metadata,
            strict=True,
        )

        rule_b = [v for v in new_violations if v["rule"] == "B"]
        assert len(rule_b) >= 1, (
            "字段不全应触发 RULE_B_FIELDS_INCOMPLETE"
        )
        assert rule_b[0]["violation_type"] == "RULE_B_FIELDS_INCOMPLETE"
        missing = rule_b[0]["missing_fields"]
        assert "mfa" in missing
        assert "two_person_approval" in missing
        assert "timeout" in missing


# ════════════════════════════════════════════════════════════════
# 6. Rule C 能 flag 未签名 callback handler
# ════════════════════════════════════════════════════════════════


class TestGateRuleCFlagsUnsignedCallback:
    """门禁 Rule C 能 flag 高风险 callback handler 未用签名绑定 API。"""

    def test_rule_c_flags_unsigned_callback_handler(self):
        """高风险 callback handler 未用签名 API → RULE_C_NO_SIGNED_TOKEN。"""
        synthetic_inventory = {
            "generated_at": "test",
            "handler_count": 1,
            "high_risk_count": 1,
            "handlers": [
                {
                    "handler": "unsigned_callback",
                    "file": "bots/bad.py",
                    "line": 1,
                    "entry_type": "callback_sub_dispatcher",
                    "route_or_pattern": "delete:",
                    "action_type": "delete",
                    "is_high_risk": True,
                    "is_dispatcher": False,
                    "routes_through_command_bus": True,
                    "routes_through_button_flow": False,
                    "calls_destructive_api": False,
                    "destructive_api": None,
                    "uses_signed_token_api": False,
                    "parent_handler": "menu_callback",
                    "bypass_reason": None,
                },
            ],
        }
        complete_metadata = {
            "handlers": {
                "unsigned_callback": {
                    "action_type": "delete",
                    "rbac": "PERM_DELETE",
                    "mfa": "required",
                    "two_person_approval": "required",
                    "idempotency_key": ["action_id"],
                    "state_transitions": "pending->executed",
                    "timeout": "300s",
                    "cancellation": "supported",
                    "localization_confirmation": "delete.confirm",
                    "audit_schema": "audit_log",
                },
            },
            "baseline": {"violations": []},
        }

        gate = _load_gate_module()
        exit_code, new_violations, _ = gate.check(
            inventory=synthetic_inventory,
            metadata=complete_metadata,
            strict=True,
        )

        rule_c = [v for v in new_violations if v["rule"] == "C"]
        assert len(rule_c) >= 1, (
            "未用签名 API 的 callback handler 应触发 RULE_C_NO_SIGNED_TOKEN"
        )
        assert rule_c[0]["violation_type"] == "RULE_C_NO_SIGNED_TOKEN"

    def test_rule_c_passes_signed_callback_handler(self):
        """高风险 callback handler 用签名 API → 无 Rule C 违规。"""
        synthetic_inventory = {
            "generated_at": "test",
            "handler_count": 1,
            "high_risk_count": 1,
            "handlers": [
                {
                    "handler": "signed_callback",
                    "file": "bots/good.py",
                    "line": 1,
                    "entry_type": "callback_sub_dispatcher",
                    "route_or_pattern": "ban:",
                    "action_type": "ban",
                    "is_high_risk": True,
                    "is_dispatcher": False,
                    "routes_through_command_bus": True,
                    "routes_through_button_flow": True,
                    "calls_destructive_api": False,
                    "destructive_api": None,
                    "uses_signed_token_api": True,
                    "parent_handler": "menu_callback",
                    "bypass_reason": None,
                },
            ],
        }
        complete_metadata = {
            "handlers": {
                "signed_callback": {
                    "action_type": "ban",
                    "rbac": "PERM_BAN",
                    "mfa": "required",
                    "two_person_approval": "required",
                    "idempotency_key": ["action_id"],
                    "state_transitions": "pending->executed",
                    "timeout": "300s",
                    "cancellation": "supported",
                    "localization_confirmation": "ban.confirm",
                    "audit_schema": "audit_log",
                },
            },
            "baseline": {"violations": []},
        }

        gate = _load_gate_module()
        exit_code, new_violations, _ = gate.check(
            inventory=synthetic_inventory,
            metadata=complete_metadata,
            strict=True,
        )

        rule_c = [v for v in new_violations if v["rule"] == "C"]
        assert len(rule_c) == 0, (
            f"用签名 API 的 callback handler 不应有 Rule C 违规: {rule_c}"
        )

    def test_rule_c_skips_fastapi_post(self):
        """Rule C 不适用于 FastAPI POST 端点(走 CSRF + session,非 callback 签名)。"""
        synthetic_inventory = {
            "generated_at": "test",
            "handler_count": 1,
            "high_risk_count": 1,
            "handlers": [
                {
                    "handler": "web_endpoint",
                    "file": "admin/__init__.py",
                    "line": 1,
                    "entry_type": "fastapi_post",
                    "route_or_pattern": "/ban",
                    "action_type": "ban",
                    "is_high_risk": True,
                    "is_dispatcher": False,
                    "routes_through_command_bus": True,
                    "routes_through_button_flow": False,
                    "calls_destructive_api": False,
                    "destructive_api": None,
                    "uses_signed_token_api": False,
                    "parent_handler": None,
                    "bypass_reason": None,
                },
            ],
        }
        complete_metadata = {
            "handlers": {
                "web_endpoint": {
                    "action_type": "ban",
                    "rbac": "PERM_BAN",
                    "mfa": "required",
                    "two_person_approval": "required",
                    "idempotency_key": ["action_id"],
                    "state_transitions": "pending->executed",
                    "timeout": "300s",
                    "cancellation": "supported",
                    "localization_confirmation": "ban.confirm",
                    "audit_schema": "audit_log",
                },
            },
            "baseline": {"violations": []},
        }

        gate = _load_gate_module()
        exit_code, new_violations, _ = gate.check(
            inventory=synthetic_inventory,
            metadata=complete_metadata,
            strict=True,
        )

        rule_c = [v for v in new_violations if v["rule"] == "C"]
        assert len(rule_c) == 0, (
            f"FastAPI POST 端点不应触发 Rule C: {rule_c}"
        )


# ════════════════════════════════════════════════════════════════
# 7. sidecar 元数据字段齐全
# ════════════════════════════════════════════════════════════════


class TestSidecarMetadataFieldsComplete:
    """所有 sidecar 条目字段齐全(10 个必需字段)。"""

    REQUIRED_FIELDS = [
        "action_type",
        "rbac",
        "mfa",
        "two_person_approval",
        "idempotency_key",
        "state_transitions",
        "timeout",
        "cancellation",
        "localization_confirmation",
        "audit_schema",
    ]

    def test_sidecar_has_handlers_section(self):
        """sidecar 应包含 handlers 段且非空。"""
        metadata = _load_metadata_artifact()
        assert "handlers" in metadata
        assert isinstance(metadata["handlers"], dict)
        assert len(metadata["handlers"]) > 0, "handlers 段不应为空"

    def test_sidecar_handlers_have_all_required_fields(self):
        """每个 sidecar handler 条目应包含全部 10 个必需字段。"""
        metadata = _load_metadata_artifact()
        for handler_name, fields in metadata["handlers"].items():
            missing = [
                f for f in self.REQUIRED_FIELDS if f not in fields
            ]
            assert not missing, (
                f"sidecar handler '{handler_name}' 缺少字段: {missing}\n"
                f"现有字段: {sorted(fields.keys())}"
            )

    def test_sidecar_covers_all_known_high_risk_handlers(self):
        """sidecar 应覆盖 inventory 中所有高风险 handler(非 dispatcher)。"""
        inventory = _load_inventory_artifact()
        metadata = _load_metadata_artifact()
        high_risk_handlers = {
            h["handler"]
            for h in inventory["handlers"]
            if h.get("is_high_risk") and not h.get("is_dispatcher")
        }
        sidecar_handlers = set(metadata["handlers"].keys())
        missing = high_risk_handlers - sidecar_handlers
        assert not missing, (
            f"sidecar 缺少高风险 handler 条目: {missing}\n"
            f"sidecar 现有: {sorted(sidecar_handlers)}\n"
            f"inventory 高风险: {sorted(high_risk_handlers)}"
        )


# ════════════════════════════════════════════════════════════════
# 8. baseline ratchet 模式
# ════════════════════════════════════════════════════════════════


class TestBaselineRatchetMode:
    """默认 baseline 模式下,已知违规不触发 exit 1(ratchet 下降)。"""

    def test_baseline_mode_passes_with_known_violations(self):
        """真实代码库:默认模式 exit 0(已知违规在 baseline 中)。"""
        gate = _load_gate_module()
        inventory = _load_inventory_artifact()
        metadata = _load_metadata_artifact()

        exit_code, new_violations, all_violations = gate.check(
            inventory=inventory,
            metadata=metadata,
            strict=False,
        )

        # 默认模式:已知违规不触发 exit 1
        assert exit_code == 0, (
            f"默认 baseline 模式应 exit 0(已知违规在 baseline 中),"
            f"实际 exit_code={exit_code}, new_violations={new_violations}"
        )

    def test_strict_mode_fails_with_known_violations(self):
        """真实代码库:strict 模式 exit 1(忽略 baseline)。"""
        gate = _load_gate_module()
        inventory = _load_inventory_artifact()
        metadata = _load_metadata_artifact()

        exit_code, new_violations, all_violations = gate.check(
            inventory=inventory,
            metadata=metadata,
            strict=True,
        )

        # strict 模式:所有违规都算新增
        assert exit_code == 1, (
            f"strict 模式应 exit 1(存在违规),"
            f"实际 exit_code={exit_code}"
        )
        assert len(new_violations) > 0, "strict 模式应有违规"

    def test_baseline_violations_have_owner_reason_expiry(self):
        """baseline 中每条违规应包含 owner / reason / expiry 字段。"""
        metadata = _load_metadata_artifact()
        baseline = metadata.get("baseline", {})
        violations = baseline.get("violations", [])
        assert len(violations) > 0, "baseline 应有已知违规(_handle_report_action 等)"
        for v in violations:
            assert "key" in v, f"baseline 违规缺少 key: {v}"
            assert "rule" in v, f"baseline 违规缺少 rule: {v}"
            assert "owner" in v, f"baseline 违规缺少 owner: {v['key']}"
            assert "reason" in v, f"baseline 违规缺少 reason: {v['key']}"
            assert "expiry" in v, f"baseline 违规缺少 expiry: {v['key']}"

    def test_new_violation_not_in_baseline_triggers_exit_1(self):
        """新增违规(不在 baseline)在默认模式下也 exit 1。"""
        # 构造一个合成的新违规(不在 baseline 中)
        synthetic_inventory = {
            "generated_at": "test",
            "handler_count": 1,
            "high_risk_count": 1,
            "handlers": [
                {
                    "handler": "brand_new_bypass",
                    "file": "bots/new.py",
                    "line": 1,
                    "entry_type": "callback_sub_dispatcher",
                    "route_or_pattern": "purge:",
                    "action_type": "purge",
                    "is_high_risk": True,
                    "is_dispatcher": False,
                    "routes_through_command_bus": False,
                    "routes_through_button_flow": False,
                    "calls_destructive_api": True,
                    "destructive_api": "purge_data",
                    "uses_signed_token_api": False,
                    "parent_handler": "menu_callback",
                    "bypass_reason": "new bypass",
                },
            ],
        }
        # baseline 中无此违规
        empty_metadata = {"handlers": {}, "baseline": {"violations": []}}

        gate = _load_gate_module()
        exit_code, new_violations, _ = gate.check(
            inventory=synthetic_inventory,
            metadata=empty_metadata,
            strict=False,
        )

        assert exit_code == 1, (
            f"新增违规(不在 baseline)在默认模式下应 exit 1,"
            f"实际 exit_code={exit_code}"
        )
        assert len(new_violations) > 0, "应有新增违规"
