import asyncio
import datetime
import re
import time

from loguru import logger
from config import settings
from database import (
    get_users_col, get_file_records_col, get_decode_logs_col,
    get_config, get_user_cached,
)
from database.models import make_user
from utils.monitor import metrics
from utils.time_utils import format_datetime

from .menus import _quota_display
from utils.shared_counters import status_counters as _status_counters
import utils.shared_counters as _shared_counters

# ── cells 缓存：admin 面板频繁刷新，加 60s 缓存避免每次点按钮都查 CRDB ──
# N-M12: 缓存键包含 status_filter + sort_key，避免不同视图共享错误缓存
# C2: _cells_cache 已迁移到 cache_store (ttl_cache 表),跨进程共享
_CELLS_CACHE_TTL = 60  # 秒


async def invalidate_cells_cache():
    """失效 cells 缓存(P1-14 factory_reset 调用)。

    C2: 清空 cache_store 中的 cells 缓存,跨进程生效。
    """
    from database.cache_store import get_cache_store
    await get_cache_store().cache_delete_prefix("cells_cache:")


def _make_cache_key_str(status_filter: dict | None, sort_key: str) -> str:
    """生成 cache_store 用的字符串缓存键。"""
    if status_filter:
        filter_str = ",".join(f"{k}={v}" for k, v in sorted(status_filter.items()))
        return f"cells_cache:{filter_str}:{sort_key}"
    return f"cells_cache:all:{sort_key}"


async def _get_cells_cached(status_filter: dict | None = None, sort_key: str = "slot_id") -> list[dict]:
    """带 60s 缓存的 cells 查询，admin 面板用。0 RU（复用 SQLite）或 1 RU（CRDB 兜底）。

    C2: 缓存迁移到 cache_store (ttl_cache 表),跨进程共享。
    """
    from database.cache_store import get_cache_store
    cache_key = _make_cache_key_str(status_filter, sort_key)
    # 1. 检查 cache_store TTL 缓存
    cached = await get_cache_store().cache_get(cache_key, _CELLS_CACHE_TTL)
    if cached is not None:
        return cached
    # 2. 优先走 SQLite 快照（0 RU）
    from database import get_cells_col
    from database.session import get_active_cells_local
    try:
        cells = await get_active_cells_local()
        if cells:
            await get_cache_store().cache_set(cache_key, cells)
            return cells
    except Exception as e:
        logger.warning(f"[Admin] cells缓存查询失败: {e}")
        pass
    # 3. CRDB 兜底（1 RU）
    col = get_cells_col()
    cells = await col.find(
        status_filter or {},
        sort=(sort_key, 1),
        projection=["slot_id", "channel_id", "status", "next_active_chat_id",
                     "prev_slot_id", "account_name", "is_r100",
                     "file_count", "rotation_started_at", "last_heartbeat",
                     "degrade_count", "created_at", "updated_at"],
    )
    await get_cache_store().cache_set(cache_key, cells)
    return cells


async def _ensure_user(user_id: int) -> dict:
    """获取或创建用户,走缓存。"""
    user = await get_user_cached(user_id)
    if user is None:
        user = make_user(user_id=user_id)
        users_col = get_users_col()
        await users_col.insert_one(user)
        # 写入缓存
        try:
            from database.cache import get_user_cache
            get_user_cache().set(f"user:{user_id}", user)
        except Exception:
            pass
    return user


async def _get_status_text() -> str:
    users_col = get_users_col()
    files_col = get_file_records_col()
    logs_col = get_decode_logs_col()
    # F5: 计数器 TTL 刷新 — 每 5 分钟从 SQLite 快照或 CRDB 重新加载
    now_ts = time.time()
    if not _shared_counters.status_counters_initialized or (now_ts - _shared_counters.status_counters_loaded_at) > 300:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        cached = await store.load_counter_snapshot()
        if cached and "total_users" in cached:
            for k, v in cached.items():
                # today_decodes 是累积值,不走缓存,下方直查 DB 保证按天统计
                if k != "today_decodes":
                    _status_counters[k] = v
        else:
            _status_counters["total_users"] = await users_col.count_documents({})
            _status_counters["total_files"] = await files_col.count_documents({})
            _status_counters["active_files"] = await files_col.count_documents({"status": "active"})
        # today_decodes 始终直查 DB(1 RU),避免跨天累积导致统计虚高
        today = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        _status_counters["today_decodes"] = await logs_col.count_documents({"request_time": {"$gte": today.isoformat()}})
        _shared_counters.status_counters_initialized = True
        _shared_counters.status_counters_loaded_at = now_ts
    try:
        from services.relay_pool import relay_pool
        if not relay_pool._initialized:
            await relay_pool.init()
        pool_status = await relay_pool.get_pool_status()
        # R31-2: 检查各中继实例的 pending 状态，而非读全局键
        relay_pending = "0"
        if relay_pool._initialized:
            for inst in relay_pool.instances:
                if await get_config(f"relay_auth_pending:{inst.phone}") == "1":
                    relay_pending = "1"
                    break
        if pool_status:
            ready = sum(1 for p in pool_status if p["is_ready"])
            relay_status = f"✅ 账号池 {ready}/{len(pool_status)} 就绪"
        else:
            relay_status = "⏳ 等待验证码" if relay_pending == "1" else "✅ 就绪/未配置"
    except Exception:
        relay_pending = await get_config("relay_auth_pending")
        relay_status = "⏳ 等待验证码" if relay_pending == "1" else "✅ 就绪/未配置"
    # 从环形拓扑获取当前活跃频道（走 60s 缓存，0 RU）
    active_cells_text = ""
    try:
        active_cells = await _get_cells_cached({"status": "active"})
        if active_cells:
            active_cells = [c for c in active_cells if c.get("status") == "active"]
            slots = [f"{c.get('slot_id')}" for c in active_cells[:5]]
            active_cells_text = ", ".join(slots)
    except Exception:
        active_cells_text = "读取失败"
    msg = (
        f"📊 系统概览\n\n"
        f"👤 总用户数:{_status_counters['total_users']}\n"
        f"📁 总文件数:{_status_counters['total_files']}\n"
        f"✅ 活跃文件:{_status_counters['active_files']}\n"
        f"🔄 今日解码:{_status_counters['today_decodes']}\n"
        f"\n🔄 活跃槽位:{active_cells_text}\n"
        f"\n🔐 用户中继:{relay_status}\n"
        f"\n🤖 机器人状态:\n"
    )
    from database.cache_store import get_all_bot_heartbeats
    hb_map = await get_all_bot_heartbeats()
    now = time.time()
    for _name in ["up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot"]:
        hb = hb_map.get(_name)
        if hb and (now - hb.get("last_ping", 0)) < 120:
            msg += f"  ✅ {_name}: {hb.get('total_processed', 0)}次/ {hb.get('total_errors', 0)}次错误\n"
        else:
            msg += f"  ⏳ {_name}: 未上报\n"
    return msg


async def _get_health_text() -> str:
    msg = "🤖 机器人健康状态\n\n"
    from database.cache_store import get_all_bot_heartbeats
    heartbeats = await get_all_bot_heartbeats()
    now = time.time()
    bot_names = ["up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot"]
    for name in bot_names:
        hb = heartbeats.get(name)
        if hb and (now - hb.get("last_ping", 0)) < 120:
            last_ping = format_datetime(hb.get("last_ping", 0))
            msg += (
                f"✅ {name}\n"
                f"  最后活跃:{last_ping}\n"
                f"  处理次数:{hb.get('total_processed', 0)}\n"
                f"  错误次数:{hb.get('total_errors', 0)}\n"
            )
        else:
            msg += f"⏳ {name}: 未上报/离线\n"
    return msg


async def _get_topology_text() -> str:
    """显示环形冗余拓扑状态。"""
    try:
        cells = await _get_cells_cached()
    except Exception as e:
        logger.warning(f"[Admin] cells缓存查询失败: {e}")
        cells = []

    msg = "🔗 环形冗余拓扑\n\n"
    # 环形架构无主频道概念,显示第一个 active 槽位作为当前活跃频道
    active_cells = [c for c in cells if c.get("status") == "active"]
    if active_cells:
        msg += f"📌 活跃频道数: {len(active_cells)}\n"
    else:
        msg += "📌 活跃频道数: 0 (系统不可用)\n"
    msg += f"📊 总槽位数: {len(cells)}\n"

    # 统计
    active_count = len([c for c in cells if c.get("status") == "active"])
    lost_count = len([c for c in cells if c.get("status") == "lost"])
    shadow_count = len([c for c in cells if c.get("status") in ("shadow1", "shadow2")])
    r100_count = len([c for c in cells if c.get("status") == "r100"])
    msg += f"  🟢活跃: {active_count} | 🟡Shadow: {shadow_count} | 🔴R100: {r100_count}"
    if lost_count:
        msg += f" | ⚫失联: {lost_count}"
    msg += "\n\n"

    if cells:
        by_group = {}
        for c in cells:
            sid = c.get("slot_id", "")
            # 从 slot_id 提取组号,如 a1 → 1, s2a → 2
            m = re.match(r'[as](\d+)', sid)
            if m:
                gn = int(m.group(1))
                by_group.setdefault(gn, []).append(c)

        # 按账号汇总
        by_account = {}
        for c in cells:
            acc = c.get("account_name") or "未标注"
            if acc not in ("?", ""):
                by_account.setdefault(acc, []).append(c)

        if len(by_account) > 1:
            msg += "👤 账号分布:\n"
            for acc, acc_cells in sorted(by_account.items()):
                a_count = len([c for c in acc_cells if c.get("status") == "active"])
                msg += f"  {acc}: {len(acc_cells)}个频道 (活跃: {a_count})\n"
            msg += "\n"

        for gn in sorted(by_group.keys()):
            group = by_group[gn]
            status_icons = {"active": "🟢", "r100": "🔴", "shadow1": "🟡", "shadow2": "🟠", "lost": "⚫"}
            parts = []
            for c in group:
                st = c.get("status", "?")
                icon = status_icons.get(st, "⚪")
                parts.append(f"{icon}{c.get('slot_id')}: {c.get('channel_id')}")
            msg += f"  组{gn}: {' | '.join(parts)}\n"
    else:
        msg += "  (未加载拓扑,请运行 seed_topology.py)\n"

    # 轮转配置
    try:
        from database import get_rotation_config
        aws = await get_rotation_config("rotation_active_window_size") or "3"
        fps = await get_rotation_config("rotation_files_per_slot") or "500"
        tps = await get_rotation_config("rotation_time_per_slot") or "3600"
        msg += f"\n⏳ 轮转配置: {aws}活态 | {fps}文件 | {tps}秒"
    except Exception:
        pass

    return msg


async def _get_logs_page_text(page: int = 1) -> str:
    per_page = 15
    logs_col = get_decode_logs_col()
    total = await logs_col.count_documents({})
    skip = (page - 1) * per_page
    logs_data = await logs_col.find(sort=("request_time", -1), skip=skip, limit=per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)
    msg = f"📋 解码日志 (第{page}/{total_pages}页)\n\n"
    for log in logs_data:
        status_icon = "✅" if log.get("status") == "success" else "⏳" if log.get("status") == "queued" else "❌"
        fc = (log.get("file_code") or "")[:30]
        requester = log.get("requester_id", "?")
        t = format_datetime(log.get("request_time"))
        msg += f"{status_icon} [{t}] {fc} - 用户{requester}\n"
    if total_pages > 1:
        msg += f"\n使用 /logs {page+1} 查看下一页"
    return msg


async def _get_users_page_text(search: str = "", page: int = 1) -> str:
    per_page = 10
    users_col = get_users_col()
    query = {}
    if search:
        if search.isdigit():
            query["user_id"] = int(search)
        else:
            query["$or"] = [
                {"username": {"$regex": search, "$options": "i"}},
                {"first_name": {"$regex": search, "$options": "i"}},
            ]
    total = await users_col.count_documents(query)
    skip = (page - 1) * per_page
    users = await users_col.find(query, sort=("created_at", -1), skip=skip, limit=per_page)
    total_pages = max(1, (total + per_page - 1) // per_page)
    msg = f"👤 用户列表 (第{page}/{total_pages}页,共{total}人)\n"
    if search:
        msg += f"🔍 搜索:{search}\n"
    msg += "\n"
    for u in users:
        level_icon = {"free": "🆓", "basic": "🥇", "premium": "👑"}.get(u.get("membership_level", "free"), "🆓")
        ban_icon = "🔒" if u.get("is_banned") else ""
        name = u.get("username") or u.get("first_name") or f"ID:{u.get('user_id')}"
        msg += f"{level_icon}{ban_icon} {u.get('user_id')} - @{name}\n"
    if total_pages > 1 and search:
        msg += f"\n使用 /users {search} {page+1} 查看下一页"
    elif total_pages > 1:
        msg += f"\n使用 /users {page+1} 查看下一页"
    return msg


async def _get_relay_status_text() -> str:
    from services.relay_pool import relay_pool
    # R31-2: 检查各中继实例的 pending 状态，而非读全局键
    pending = "0"
    try:
        if relay_pool._initialized:
            for inst in relay_pool.instances:
                if await get_config(f"relay_auth_pending:{inst.phone}") == "1":
                    pending = "1"
                    break
    except Exception:
        pending = await get_config("relay_auth_pending")

    if not relay_pool._initialized:
        try:
            await relay_pool.init()
        except Exception as e:
            logger.warning(f"[Admin] 中继池初始化失败: {e}")
            pass

    msg = "🔐 中继账号池状态\n\n"
    pool_status = await relay_pool.get_pool_status()
    if not pool_status:
        msg += "⚠️ 无中继账号\n"
        msg += "请使用下方按钮配置中继账号\n"
    else:
        msg += f"账号池: {len(pool_status)} 个账号\n\n"
        for i, ps in enumerate(pool_status, 1):
            ready = "✅" if ps["is_ready"] else "❌"
            busy = "🔴" if ps["is_busy"] else "⚪"
            phone = ps["phone"]
            masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
            msg += f"{i}. {ready}{busy} {masked}\n"
            msg += f"   今日请求: {ps['today_requests']}, 累计: {ps['total_requests']}, 平均: {ps['avg_wait_ms']:.0f}ms\n"
        ready_count = sum(1 for p in pool_status if p["is_ready"])
        msg += f"\n就绪: {ready_count}/{len(pool_status)}"

    if pending == "1":
        msg += "\n⚠️ 正在等待验证码,请通过 /relay_code 提交"

    return msg


_CONFIG_SETTINGS_MAP = {
    "file_code_prefix": "FILE_CODE_PREFIX",
    "force_join_channel_id": "FORCE_JOIN_CHANNEL_ID",
    "force_join_link": "FORCE_JOIN_CHANNEL_LINK",
    "upload_bot_username": "UPLOAD_BOT_USERNAME",
    "decoder_bot_username": "DECODER_BOT_USERNAME",
    "sender_bot_username": "SENDER_BOT_USERNAME",
    "quota_default_free": "FREE_DAILY_QUOTA",
    "quota_external_free": "FREE_EXTERNAL_DAILY_QUOTA",
    "quota_default_basic": "BASIC_DAILY_QUOTA",
    "quota_external_basic": "BASIC_EXTERNAL_DAILY_QUOTA",
    "quota_default_premium": "PREMIUM_DAILY_QUOTA",
    "quota_external_premium": "PREMIUM_EXTERNAL_DAILY_QUOTA",
    "r2_account_id": "R2_ACCOUNT_ID",
    "r2_access_key": "R2_ACCESS_KEY_ID",
    "r2_secret_key": "R2_SECRET_ACCESS_KEY",
    "r2_bucket": "R2_BUCKET_NAME",
    "r2_endpoint": "R2_ENDPOINT",
    "db_backup_interval": "DB_BACKUP_INTERVAL_MINUTES",
    "db_backup_enabled": "DB_BACKUP_ENABLED",
}


def _config_fallback(key: str) -> str:
    attr_name = _CONFIG_SETTINGS_MAP.get(key)
    if attr_name:
        val = getattr(settings, attr_name, None)
        if val is not None:
            str_val = str(val)
            if str_val and str_val not in ("0", "-1000000000000"):
                return str_val
    return settings.get_config_default(key)


async def _get_configs_text() -> str:
    cfg_keys = [
        ("file_code_prefix", "📝 文件码前缀", "⚠️需重启"),
        ("force_join_channel_id", "🔒 强制加群频道", "✅热更新"),
        ("force_join_link", "🔗 加群链接", "✅热更新"),
        ("upload_bot_username", "📤 上传机器人", "✅热更新"),
        ("decoder_bot_username", "🔓 解码机器人", "✅热更新"),
        ("sender_bot_username", "📨 发送机器人", "✅热更新"),
    ]

    quota_keys = [
        ("quota_default_free", "🆓 免费用户日配额", "✅热更新"),
        ("quota_external_free", "🆓 免费外部码配额", "✅热更新"),
        ("quota_default_basic", "🥇 基础会员日配额", "✅热更新"),
        ("quota_external_basic", "🥇 基础外部码配额", "✅热更新"),
        ("quota_default_premium", "👑 高级会员日配额", "✅热更新"),
        ("quota_external_premium", "👑 高级外部码配额", "✅热更新"),
    ]

    backup_keys = [
        ("db_backup_interval", "💾 DB备份间隔(分钟)", "✅热更新"),
        ("db_backup_enabled", "💾 DB备份", "✅热更新"),
    ]

    msg = "⚙️ 系统配置\n\n"

    msg += "📌 基础配置\n"
    for key, label, indicator in cfg_keys:
        val = await get_config(key)
        if not val:
            val = _config_fallback(key)
        display = val if val else "❌ 未配置"
        msg += f"  {label}:{display} {indicator}\n"

    msg += "\n🎫 默认配额\n"
    for key, label, indicator in quota_keys:
        val = await get_config(key)
        if not val:
            val = _config_fallback(key)
        try:
            display = _quota_display(int(val)) if val else "未配置"
        except (ValueError, TypeError):
            display = str(val) if val else "未配置"
        msg += f"  {label}:{display} {indicator}\n"

    r2_keys_to_check = ["r2_account_id", "r2_access_key", "r2_secret_key"]
    r2_vals = await asyncio.gather(*(get_config(k) for k in r2_keys_to_check), return_exceptions=True)
    # 注意: return_exceptions=True 时 Exception 实例是 truthy,必须显式判断 isinstance(v, str)
    r2_configured = any(isinstance(v, str) and v for v in r2_vals)
    if not r2_configured:
        r2_check = lambda k: _config_fallback(k) != settings.get_config_default(k)
        r2_configured = any(r2_check(k) for k in r2_keys_to_check)
    msg += f"\n☁️ R2 备份:{'✅ 已配置' if r2_configured else '❌ 未配置'} ⚠️需重启\n"

    for key, label, indicator in backup_keys:
        val = await get_config(key)
        if not val:
            val = _config_fallback(key)
        display = val if val else "未配置"
        if key == "db_backup_enabled":
            display = "✅ 开启" if display.lower() in ("true", "1", "on") else "❌ 关闭"
        msg += f"  {label}:{display} {indicator}\n"

    msg += "\n使用 /set_* 命令修改配置,或点击菜单按钮操作。"
    return msg