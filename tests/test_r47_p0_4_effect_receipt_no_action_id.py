"""R47 P0-4 / R48 P0-4: Effect Receipt 无 action_id 拒绝 + request_hash 绑定 + 静态扫描测试。

被测目标:
- ``services.effect_receipts.compute_effect_request_hash``
- ``services.effect_receipts.validate_critical_effects_have_action_id``
- ``services.effect_receipts.EffectReceiptManager.check_receipt`` (expected_request_hash)
- ``services.effect_receipts.EffectReceiptManager.record_pending`` (request_hash 存储 + R48 应用层校验)
- ``services.effect_receipts_integration.with_effect_receipt`` (critical 无 action_id/params_fn 拒绝)
- ``services.effect_receipts_integration.EffectReceiptContext`` (critical 无 action_id/params 拒绝)

测试场景:
1. compute_effect_request_hash: 确定性 + 不同 payload 产生不同 hash
2. with_effect_receipt: critical effect 无 action_id → raise EffectReceiptError
3. with_effect_receipt: 非关键 effect 无 action_id → 直执(向后兼容)
4. EffectReceiptContext: critical effect 无 action_id → raise EffectReceiptError
5. EffectReceiptContext: 非关键 effect 无 action_id → 直执(不记录 receipt)
6. check_receipt: request_hash 不匹配 → 返回 None(不视为 completed)
7. check_receipt: request_hash 匹配 → 返回 completed receipt
8. record_pending: request_hash 存入 effect_receipts.request_hash 列
9. EffectReceiptContext: params 参数计算 request_hash 并绑定
10. validate_critical_effects_have_action_id: 静态扫描检测违规
11. R48 P0-4: critical effect 无 params/params_fn → raise EffectReceiptError
12. R48 P0-4: critical effect params_fn 异常 → raise EffectReceiptError
13. R48 P0-4: 非 critical effect 无 params → 允许(向后兼容)
14. R48 P0-4: record_pending critical effect 空 request_hash → raise ValueError
"""
import inspect
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

# ── Mock telegram 模块(避免依赖真实 telegram 库) ───────────────
sys.modules.setdefault("telegram", MagicMock())
sys.modules.setdefault("telegram.ext", MagicMock())

# ── 模块级 skip 检查 ────────────────────────────────────────────
from database import cache_store as _cs_module

if not inspect.isclass(_cs_module.CacheStore):
    pytest.skip(
        "database.cache_store.CacheStore 不可用(需要 aiosqlite + Python 3.10+)",
        allow_module_level=True,
    )

CacheStore = _cs_module.CacheStore


# ════════════════════════════════════════════════════════════════
# Fixture: 真实 SQLite 临时数据库
# ════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture
async def real_store():
    """创建一个使用临时文件数据库的 CacheStore 实例(隔离生产数据)。"""
    tmpdir = tempfile.mkdtemp(prefix="r47_p0_4_test_")
    db_path = Path(tmpdir) / "test_cache.db"
    original_path = _cs_module.DB_PATH
    original_store = getattr(_cs_module, "_store", None)
    _cs_module.DB_PATH = db_path
    try:
        s = CacheStore()
        await s.init()
        _cs_module._store = s
        yield s
        await s.close()
    finally:
        _cs_module.DB_PATH = original_path
        _cs_module._store = original_store
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest_asyncio.fixture
async def receipt_manager(real_store):
    """初始化 EffectReceiptManager 单例并返回,用例间隔离。"""
    from services import effect_receipts as _er_mod
    _er_mod._receipt_manager = None
    mgr = _er_mod.get_receipt_manager(real_store)
    yield mgr
    _er_mod._receipt_manager = None
    if real_store._db:
        await real_store._db.execute("DELETE FROM effect_receipts")
        await real_store._db.commit()


@pytest_asyncio.fixture
async def clean_tables(real_store):
    """每个用例前清空 effect_receipts 表。"""
    await real_store._db.execute("DELETE FROM effect_receipts")
    await real_store._db.commit()
    yield real_store
    await real_store._db.execute("DELETE FROM effect_receipts")
    await real_store._db.commit()


# ════════════════════════════════════════════════════════════════
# 辅助函数
# ════════════════════════════════════════════════════════════════

async def _get_receipt_field(store, action_id, effect_type, target, field):
    """查询 effect_receipts 表中指定字段。"""
    cursor = await store._db.execute(
        f"SELECT {field} FROM effect_receipts "
        "WHERE action_id = ? AND effect_type = ? AND target = ?",
        (action_id, effect_type, target),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


# ════════════════════════════════════════════════════════════════
# 1. compute_effect_request_hash 单元测试
# ════════════════════════════════════════════════════════════════

class TestComputeEffectRequestHash:
    """compute_effect_request_hash 行为测试。"""

    def test_deterministic_same_params(self):
        """相同 effect_type + 相同 params → 相同 hash(确定性)。"""
        from services.effect_receipts import compute_effect_request_hash
        h1 = compute_effect_request_hash("telegram_send", {"chat_id": 42, "text": "hi"})
        h2 = compute_effect_request_hash("telegram_send", {"text": "hi", "chat_id": 42})
        assert h1 == h2, "key 顺序不同应产生相同 hash(sort_keys=True)"
        assert len(h1) == 64, "SHA256 十六进制长度应为 64"

    def test_different_effect_type_different_hash(self):
        """不同 effect_type → 不同 hash。"""
        from services.effect_receipts import compute_effect_request_hash
        h1 = compute_effect_request_hash("telegram_send", {"chat_id": 42})
        h2 = compute_effect_request_hash("telegram_copy", {"chat_id": 42})
        assert h1 != h2, "不同 effect_type 应产生不同 hash"

    def test_different_params_different_hash(self):
        """不同 params → 不同 hash。"""
        from services.effect_receipts import compute_effect_request_hash
        h1 = compute_effect_request_hash("r2_put", {"key": "a", "size": 10})
        h2 = compute_effect_request_hash("r2_put", {"key": "b", "size": 10})
        assert h1 != h2, "不同 params 应产生不同 hash"

    def test_empty_params(self):
        """空 params / None → 不报错,返回确定性 hash。"""
        from services.effect_receipts import compute_effect_request_hash
        h1 = compute_effect_request_hash("restore", {})
        h2 = compute_effect_request_hash("restore", None)
        assert h1 == h2
        assert len(h1) == 64


# ════════════════════════════════════════════════════════════════
# 2. with_effect_receipt 装饰器: critical 无 action_id 拒绝
# ════════════════════════════════════════════════════════════════

class TestWithEffectReceiptCriticalNoActionId:
    """with_effect_receipt 装饰器: critical effect 无 action_id 拒绝。"""

    @pytest.mark.asyncio
    async def test_critical_effect_no_action_id_raises(
        self, receipt_manager, clean_tables,
    ):
        """critical effect(telegram_send) 无 action_id → raise EffectReceiptError。"""
        from services.effect_receipts_integration import with_effect_receipt
        from services.effect_receipts import EffectReceiptError

        @with_effect_receipt(
            "telegram_send", lambda *a, **kw: "chat:1",
            params_fn=lambda *a, **kw: {"chat_id": 1, "text": "hi"},
        )
        async def send_message(chat_id, text):
            return {"message_id": 1}

        with pytest.raises(EffectReceiptError, match="requires action_id"):
            await send_message(42, "hi")

    @pytest.mark.asyncio
    async def test_critical_effect_no_action_id_raises_for_each_critical_type(
        self, receipt_manager, clean_tables,
    ):
        """所有 critical effect_type 无 action_id 均拒绝执行。"""
        from services.effect_receipts_integration import with_effect_receipt
        from services.effect_receipts import EffectReceiptError, CRITICAL_EFFECT_TYPES

        for et in CRITICAL_EFFECT_TYPES:
            @with_effect_receipt(
                et, lambda *a, **kw: "target",
                params_fn=lambda *a, **kw: {"x": 1},
            )
            async def do_side_effect(x):
                return {"ok": True}

            with pytest.raises(EffectReceiptError, match="requires action_id"):
                await do_side_effect(1)

    @pytest.mark.asyncio
    async def test_non_critical_effect_no_action_id_direct_executes(
        self, receipt_manager, clean_tables,
    ):
        """非 critical effect 无 action_id → 直执(向后兼容)。"""
        from services.effect_receipts_integration import with_effect_receipt

        called = False

        @with_effect_receipt("r2_upload", lambda *a, **kw: "key:abc")
        async def upload(key, data):
            nonlocal called
            called = True
            return {"external_id": "r2_v1"}

        result = await upload("abc", b"data")

        assert called is True
        assert result == {"external_id": "r2_v1"}

    @pytest.mark.asyncio
    async def test_critical_effect_with_action_id_works(
        self, receipt_manager, clean_tables,
    ):
        """critical effect 有 action_id + params_fn → 正常走 receipt 流程。"""
        from services.effect_receipts_integration import with_effect_receipt

        store = clean_tables

        @with_effect_receipt(
            "telegram_send", lambda *a, **kw: f"chat:{a[0]}",
            params_fn=lambda *a, **kw: {"chat_id": a[0], "text": a[1]},
        )
        async def send_message(chat_id, text):
            return {"message_id": 999}

        result = await send_message(42, "hi", action_id="act_r47_1")

        assert result == {"message_id": 999}
        status = await _get_receipt_field(
            store, "act_r47_1", "telegram_send", "chat:42", "status",
        )
        assert status == "completed"


# ════════════════════════════════════════════════════════════════
# 3. EffectReceiptContext: critical 无 action_id 拒绝
# ════════════════════════════════════════════════════════════════

class TestEffectReceiptContextCriticalNoActionId:
    """EffectReceiptContext: critical effect 无 action_id 拒绝。"""

    @pytest.mark.asyncio
    async def test_critical_context_no_action_id_raises(
        self, receipt_manager, clean_tables,
    ):
        """critical effect EffectReceiptContext 无 action_id → raise。"""
        from services.effect_receipts_integration import EffectReceiptContext
        from services.effect_receipts import EffectReceiptError

        with pytest.raises(EffectReceiptError, match="requires action_id"):
            async with EffectReceiptContext(
                action_id="",
                effect_type="telegram_send",
                target="chat:1",
                params={"chat_id": 1, "text": "hi"},
            ):
                pass  # 不应执行到这里

    @pytest.mark.asyncio
    async def test_critical_context_none_action_id_raises(
        self, receipt_manager, clean_tables,
    ):
        """critical effect EffectReceiptContext action_id=None → raise。"""
        from services.effect_receipts_integration import EffectReceiptContext
        from services.effect_receipts import EffectReceiptError

        with pytest.raises(EffectReceiptError, match="requires action_id"):
            async with EffectReceiptContext(
                action_id=None,  # type: ignore[arg-type]
                effect_type="ban",
                target="users:1",
                params={"user_id": 1},
            ):
                pass

    @pytest.mark.asyncio
    async def test_non_critical_context_no_action_id_direct_executes(
        self, receipt_manager, clean_tables,
    ):
        """非 critical effect EffectReceiptContext 无 action_id → 直执(不记录)。"""
        from services.effect_receipts_integration import EffectReceiptContext

        store = clean_tables
        side_effect_done = False

        async with EffectReceiptContext(
            action_id="",
            effect_type="r2_upload",
            target="key:xyz",
        ) as receipt:
            # 不记录 receipt,直执
            side_effect_done = True
            receipt.set_external_id("r2_rev")

        assert side_effect_done is True

        # 验证未写入 effect_receipts(非关键无 action_id 不记录)
        cursor = await store._db.execute(
            "SELECT COUNT(*) FROM effect_receipts "
            "WHERE effect_type = 'r2_upload' AND target = 'key:xyz'"
        )
        row = await cursor.fetchone()
        assert row[0] == 0, "非关键无 action_id 不应写入 effect_receipts"

    @pytest.mark.asyncio
    async def test_critical_context_with_action_id_works(
        self, receipt_manager, clean_tables,
    ):
        """critical effect EffectReceiptContext 有 action_id + params → 正常记录。"""
        from services.effect_receipts_integration import EffectReceiptContext

        store = clean_tables
        async with EffectReceiptContext(
            action_id="act_r47_ctx",
            effect_type="purge",
            target="users:99",
            params={"user_id": 99, "reason": "spam"},
        ) as receipt:
            receipt.set_external_id("purge_ok")

        status = await _get_receipt_field(
            store, "act_r47_ctx", "purge", "users:99", "status",
        )
        assert status == "completed"


# ════════════════════════════════════════════════════════════════
# 4. request_hash 绑定 effect 参数
# ════════════════════════════════════════════════════════════════

class TestRequestHashBinding:
    """request_hash 绑定 effect 参数: 防止同 action_id 不同 payload 绕过。"""

    @pytest.mark.asyncio
    async def test_record_pending_stores_request_hash(
        self, receipt_manager, clean_tables,
    ):
        """record_pending 应将 request_hash 存入 effect_receipts.request_hash 列。"""
        from services.effect_receipts import compute_effect_request_hash

        store = clean_tables
        action_id = "act_hash_1"
        effect_type = "telegram_send"
        target = "chat:1"
        params = {"chat_id": 1, "text": "hello"}
        expected_hash = compute_effect_request_hash(effect_type, params)

        ok = await receipt_manager.record_pending(
            action_id, effect_type, target,
            request_hash=expected_hash,
        )
        assert ok is True

        stored = await _get_receipt_field(
            store, action_id, effect_type, target, "request_hash",
        )
        assert stored == expected_hash

    @pytest.mark.asyncio
    async def test_check_receipt_hash_mismatch_returns_none(
        self, receipt_manager, clean_tables,
    ):
        """check_receipt: request_hash 不匹配 → 返回 None(不视为 completed)。"""
        from services.effect_receipts import compute_effect_request_hash

        store = clean_tables
        action_id = "act_hash_mismatch"
        effect_type = "telegram_send"
        target = "chat:2"

        # 第一次:用 params A 记录 pending → completed
        hash_a = compute_effect_request_hash(effect_type, {"text": "AAA"})
        await receipt_manager.record_pending(
            action_id, effect_type, target, request_hash=hash_a,
        )
        await receipt_manager.record_completed(
            action_id, effect_type, target, external_id="msg_1",
        )

        # 验证:用相同 hash 检查 → 返回 completed
        result_match = await receipt_manager.check_receipt(
            action_id, effect_type, target, expected_request_hash=hash_a,
        )
        assert result_match is not None
        assert result_match["status"] == "completed"

        # 验证:用不同 hash(params B)检查 → 返回 None(不视为 completed)
        hash_b = compute_effect_request_hash(effect_type, {"text": "BBB"})
        result_mismatch = await receipt_manager.check_receipt(
            action_id, effect_type, target, expected_request_hash=hash_b,
        )
        assert result_mismatch is None, "request_hash 不匹配应返回 None"

    @pytest.mark.asyncio
    async def test_check_receipt_no_expected_hash_skips_validation(
        self, receipt_manager, clean_tables,
    ):
        """check_receipt: 不传 expected_request_hash → 跳过 hash 校验(向后兼容)。"""
        store = clean_tables
        action_id = "act_hash_compat"
        effect_type = "r2_upload"
        target = "key:compat"

        await receipt_manager.record_pending(
            action_id, effect_type, target, request_hash="some_hash",
        )
        await receipt_manager.record_completed(
            action_id, effect_type, target, external_id="r2_v1",
        )

        # 不传 expected_request_hash → 不校验,返回 completed
        result = await receipt_manager.check_receipt(
            action_id, effect_type, target,
        )
        assert result is not None
        assert result["status"] == "completed"
        assert result["request_hash"] == "some_hash"

    @pytest.mark.asyncio
    async def test_check_receipt_stored_hash_empty_skips_validation(
        self, receipt_manager, clean_tables,
    ):
        """check_receipt: 存储的 hash 为空 → 跳过校验(兼容旧数据)。"""
        store = clean_tables
        action_id = "act_hash_old"
        effect_type = "r2_upload"
        target = "key:old"

        # 不传 request_hash(模拟旧数据)
        await receipt_manager.record_pending(
            action_id, effect_type, target,
        )
        await receipt_manager.record_completed(
            action_id, effect_type, target, external_id="r2_old",
        )

        # 传 expected_request_hash 但存储为空 → 跳过校验,返回 completed
        result = await receipt_manager.check_receipt(
            action_id, effect_type, target,
            expected_request_hash="new_hash",
        )
        assert result is not None
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_context_with_params_binds_request_hash(
        self, receipt_manager, clean_tables,
    ):
        """EffectReceiptContext(params=...) 计算 request_hash 并存入 receipt。"""
        from services.effect_receipts_integration import EffectReceiptContext
        from services.effect_receipts import compute_effect_request_hash

        store = clean_tables
        params = {"chat_id": 42, "text": "hello"}
        expected_hash = compute_effect_request_hash("telegram_send", params)

        async with EffectReceiptContext(
            action_id="act_ctx_hash",
            effect_type="telegram_send",
            target="chat:42",
            params=params,
        ) as receipt:
            receipt.set_external_id("msg_42")

        stored_hash = await _get_receipt_field(
            store, "act_ctx_hash", "telegram_send", "chat:42", "request_hash",
        )
        assert stored_hash == expected_hash
        status = await _get_receipt_field(
            store, "act_ctx_hash", "telegram_send", "chat:42", "status",
        )
        assert status == "completed"

    @pytest.mark.asyncio
    async def test_context_params_mismatch_does_not_skip(
        self, receipt_manager, clean_tables,
    ):
        """EffectReceiptContext: 同 action_id 不同 params → 不跳过(重新执行)。"""
        from services.effect_receipts_integration import EffectReceiptContext

        store = clean_tables
        action_id = "act_ctx_mismatch"
        params_a = {"text": "AAA"}
        params_b = {"text": "BBB"}

        # 第一次: params A → completed
        async with EffectReceiptContext(
            action_id=action_id,
            effect_type="telegram_send",
            target="chat:77",
            params=params_a,
        ) as receipt:
            assert receipt.skipped is False
            receipt.set_external_id("msg_a")

        # 第二次: params B → 不应跳过(request_hash 不匹配)
        async with EffectReceiptContext(
            action_id=action_id,
            effect_type="telegram_send",
            target="chat:77",
            params=params_b,
        ) as receipt:
            assert receipt.skipped is False, "params 不同时不应跳过"


# ════════════════════════════════════════════════════════════════
# 5. 静态扫描 validate_critical_effects_have_action_id
# ════════════════════════════════════════════════════════════════

class TestValidateCriticalEffectsHaveActionId:
    """validate_critical_effects_have_action_id 静态扫描测试。"""

    def test_clean_repo_passes(self):
        """当前仓库扫描应通过(无违规)。"""
        from services.effect_receipts import validate_critical_effects_have_action_id
        project_root = str(Path(__file__).resolve().parent.parent)
        violations = validate_critical_effects_have_action_id(project_root)
        # 当前仓库生产代码(services/bots/admin)应无违规
        assert violations == [], (
            "当前仓库存在 critical effect 无 action_id 违规: "
            + "; ".join(
                f"{v['file']}:{v['line']}({v['call']},{v['effect_type']})"
                for v in violations
            )
        )

    def test_detects_critical_context_without_action_id(self, tmp_path):
        """检测 EffectReceiptContext critical effect 无 action_id(R48 同时检测无 params)。"""
        from services.effect_receipts import validate_critical_effects_have_action_id

        # 构造临时项目结构
        services_dir = tmp_path / "services"
        services_dir.mkdir()
        bad_file = services_dir / "bad.py"
        bad_file.write_text(
            "from services.effect_receipts_integration import EffectReceiptContext\n"
            "\n"
            "async def bad_call():\n"
            "    async with EffectReceiptContext(\n"
            "        action_id='',\n"
            "        effect_type='telegram_send',\n"
            "        target='chat:1',\n"
            "    ) as receipt:\n"
            "        pass\n"
        )

        violations = validate_critical_effects_have_action_id(str(tmp_path))
        # R48 P0-4: 无 action_id + 无 params → 2 处违规
        assert len(violations) == 2
        assert all(v["effect_type"] == "telegram_send" for v in violations)
        assert all(v["call"] == "EffectReceiptContext" for v in violations)
        reasons = " ".join(v["reason"] for v in violations)
        assert "action_id" in reasons
        assert "params" in reasons

    def test_detects_critical_with_effect_receipt_decorator(self, tmp_path):
        """检测 with_effect_receipt 装饰器用于 critical effect(R48 同时检测无 params_fn)。"""
        from services.effect_receipts import validate_critical_effects_have_action_id

        services_dir = tmp_path / "services"
        services_dir.mkdir()
        bad_file = services_dir / "decorated.py"
        bad_file.write_text(
            "from services.effect_receipts_integration import with_effect_receipt\n"
            "\n"
            "@with_effect_receipt('ban')\n"
            "async def ban_user(user_id):\n"
            "    return {'banned': True}\n"
        )

        violations = validate_critical_effects_have_action_id(str(tmp_path))
        # R48 P0-4: 装饰器违规 + 无 params_fn → 2 处违规
        assert len(violations) == 2
        assert all(v["effect_type"] == "ban" for v in violations)
        assert all(v["call"] == "with_effect_receipt" for v in violations)

    def test_non_critical_context_no_violation(self, tmp_path):
        """非 critical effect EffectReceiptContext 无 action_id/params → 不违规。"""
        from services.effect_receipts import validate_critical_effects_have_action_id

        services_dir = tmp_path / "services"
        services_dir.mkdir()
        ok_file = services_dir / "ok.py"
        ok_file.write_text(
            "from services.effect_receipts_integration import EffectReceiptContext\n"
            "\n"
            "async def ok_call():\n"
            "    async with EffectReceiptContext(\n"
            "        action_id='',\n"
            "        effect_type='r2_upload',\n"
            "        target='key:1',\n"
            "    ) as receipt:\n"
            "        pass\n"
        )

        violations = validate_critical_effects_have_action_id(str(tmp_path))
        assert violations == []

    def test_critical_context_with_action_id_no_violation(self, tmp_path):
        """critical effect EffectReceiptContext 有非空 action_id + params → 不违规。"""
        from services.effect_receipts import validate_critical_effects_have_action_id

        services_dir = tmp_path / "services"
        services_dir.mkdir()
        ok_file = services_dir / "ok.py"
        ok_file.write_text(
            "from services.effect_receipts_integration import EffectReceiptContext\n"
            "\n"
            "async def ok_call():\n"
            "    async with EffectReceiptContext(\n"
            "        action_id='act_123',\n"
            "        effect_type='telegram_send',\n"
            "        target='chat:1',\n"
            "        params={'chat_id': 1},\n"
            "    ) as receipt:\n"
            "        pass\n"
        )

        violations = validate_critical_effects_have_action_id(str(tmp_path))
        assert violations == []

    def test_critical_context_without_params_detected(self, tmp_path):
        """R48 P0-4: critical effect 有 action_id 但无 params → 1 处违规。"""
        from services.effect_receipts import validate_critical_effects_have_action_id

        services_dir = tmp_path / "services"
        services_dir.mkdir()
        bad_file = services_dir / "no_params.py"
        bad_file.write_text(
            "from services.effect_receipts_integration import EffectReceiptContext\n"
            "\n"
            "async def bad_call():\n"
            "    async with EffectReceiptContext(\n"
            "        action_id='act_456',\n"
            "        effect_type='telegram_send',\n"
            "        target='chat:1',\n"
            "    ) as receipt:\n"
            "        pass\n"
        )

        violations = validate_critical_effects_have_action_id(str(tmp_path))
        assert len(violations) == 1
        assert violations[0]["effect_type"] == "telegram_send"
        assert "params" in violations[0]["reason"]

    def test_tests_dir_not_scanned(self, tmp_path):
        """tests/ 目录不在扫描范围(测试代码可使用 critical 装饰器)。"""
        from services.effect_receipts import validate_critical_effects_have_action_id

        # 构造 tests/ 目录(不应被扫描)
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        test_file = tests_dir / "test_critical.py"
        test_file.write_text(
            "from services.effect_receipts_integration import with_effect_receipt\n"
            "\n"
            "@with_effect_receipt('telegram_send')\n"
            "async def send(msg):\n"
            "    return {'ok': True}\n"
        )

        violations = validate_critical_effects_have_action_id(str(tmp_path))
        assert violations == [], "tests/ 目录不应被扫描"


# ════════════════════════════════════════════════════════════════
# 6. R48 P0-4: critical effect 强制 params/params_fn + 异常处理
# ════════════════════════════════════════════════════════════════

class TestR48CriticalEffectParamsEnforcement:
    """R48 P0-4: critical effect 必须提供 params/params_fn,异常时拒绝执行。"""

    @pytest.mark.asyncio
    async def test_critical_decorator_no_params_fn_raises(
        self, receipt_manager, clean_tables,
    ):
        """R48: critical effect 装饰器无 params_fn → raise EffectReceiptError。"""
        from services.effect_receipts_integration import with_effect_receipt
        from services.effect_receipts import EffectReceiptError

        @with_effect_receipt("telegram_send", lambda *a, **kw: "chat:1")
        async def send_message(chat_id, text):
            return {"message_id": 1}

        with pytest.raises(EffectReceiptError, match="requires params or params_fn"):
            await send_message(42, "hi", action_id="act_r48_1")

    @pytest.mark.asyncio
    async def test_critical_context_no_params_raises(
        self, receipt_manager, clean_tables,
    ):
        """R48: critical effect 上下文无 params → raise EffectReceiptError。"""
        from services.effect_receipts_integration import EffectReceiptContext
        from services.effect_receipts import EffectReceiptError

        with pytest.raises(EffectReceiptError, match="requires params or params_fn"):
            async with EffectReceiptContext(
                action_id="act_r48_2",
                effect_type="telegram_send",
                target="chat:1",
            ):
                pass  # 不应执行到这里

    @pytest.mark.asyncio
    async def test_critical_decorator_params_fn_exception_raises(
        self, receipt_manager, clean_tables,
    ):
        """R48: critical effect params_fn 抛异常 → raise EffectReceiptError。"""
        from services.effect_receipts_integration import with_effect_receipt
        from services.effect_receipts import EffectReceiptError

        def _broken_params_fn(*a, **kw):
            raise RuntimeError("params 计算失败")

        @with_effect_receipt(
            "telegram_send", lambda *a, **kw: "chat:1",
            params_fn=_broken_params_fn,
        )
        async def send_message(chat_id, text):
            return {"message_id": 1}

        with pytest.raises(EffectReceiptError, match="params_fn failed"):
            await send_message(42, "hi", action_id="act_r48_3")

    @pytest.mark.asyncio
    async def test_non_critical_decorator_params_fn_exception_continues(
        self, receipt_manager, clean_tables,
    ):
        """R48: 非 critical effect params_fn 抛异常 → warning 并继续执行。"""
        from services.effect_receipts_integration import with_effect_receipt

        def _broken_params_fn(*a, **kw):
            raise RuntimeError("params 计算失败(非关键)")

        called = False

        @with_effect_receipt(
            "r2_upload", lambda *a, **kw: "key:abc",
            params_fn=_broken_params_fn,
        )
        async def upload(key, data):
            nonlocal called
            called = True
            return {"external_id": "r2_v1"}

        result = await upload("abc", b"data", action_id="act_r48_4")

        assert called is True
        assert result == {"external_id": "r2_v1"}

    @pytest.mark.asyncio
    async def test_non_critical_context_no_params_allows(
        self, receipt_manager, clean_tables,
    ):
        """R48: 非 critical effect 上下文无 params → 允许(向后兼容)。"""
        from services.effect_receipts_integration import EffectReceiptContext

        store = clean_tables
        side_effect_done = False

        async with EffectReceiptContext(
            action_id="act_r48_5",
            effect_type="r2_upload",
            target="key:abc",
        ) as receipt:
            side_effect_done = True
            receipt.set_external_id("r2_v1")

        assert side_effect_done is True
        status = await _get_receipt_field(
            store, "act_r48_5", "r2_upload", "key:abc", "status",
        )
        assert status == "completed"

    @pytest.mark.asyncio
    async def test_record_pending_critical_empty_hash_raises_value_error(
        self, receipt_manager, clean_tables,
    ):
        """R48: record_pending critical effect 空 request_hash → raise ValueError。"""
        from services.effect_receipts import EffectReceiptManager

        with pytest.raises(ValueError, match="request_hash 为空"):
            await receipt_manager.record_pending(
                "act_r48_6", "telegram_send", "chat:1",
                request_hash="",  # 空 hash
            )

    @pytest.mark.asyncio
    async def test_record_pending_non_critical_empty_hash_allowed(
        self, receipt_manager, clean_tables,
    ):
        """R48: record_pending 非 critical effect 空 request_hash → 允许。"""
        store = clean_tables
        ok = await receipt_manager.record_pending(
            "act_r48_7", "r2_upload", "key:abc",
            request_hash="",  # 非 critical 允许空 hash
        )
        assert ok is True
        status = await _get_receipt_field(
            store, "act_r48_7", "r2_upload", "key:abc", "status",
        )
        assert status == "pending"

    @pytest.mark.asyncio
    async def test_critical_decorator_with_params_fn_works(
        self, receipt_manager, clean_tables,
    ):
        """R48: critical effect 有 action_id + params_fn → 正常走 receipt 流程。"""
        from services.effect_receipts_integration import with_effect_receipt
        from services.effect_receipts import compute_effect_request_hash

        store = clean_tables

        @with_effect_receipt(
            "telegram_send", lambda *a, **kw: f"chat:{a[0]}",
            params_fn=lambda *a, **kw: {"chat_id": a[0], "text": a[1]},
        )
        async def send_message(chat_id, text):
            return {"message_id": 777}

        result = await send_message(42, "hello", action_id="act_r48_8")

        assert result == {"message_id": 777}
        status = await _get_receipt_field(
            store, "act_r48_8", "telegram_send", "chat:42", "status",
        )
        assert status == "completed"
        # 验证 request_hash 已存储
        expected_hash = compute_effect_request_hash(
            "telegram_send", {"chat_id": 42, "text": "hello"},
        )
        stored_hash = await _get_receipt_field(
            store, "act_r48_8", "telegram_send", "chat:42", "request_hash",
        )
        assert stored_hash == expected_hash
