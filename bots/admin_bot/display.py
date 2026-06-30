import asyncio
import datetime
import re

from loguru import logger
from config import settings
from database import (
    get_users_col, get_file_records_col, get_decode_logs_col,
    get_config, set_config,
    get_rotation_config,
    get_user_cached,
)
from database.models import make_user
from utils.monitor import metrics
from utils.storage_channel import get_active_storage_channel_id
from utils.time_utils import format_datetime

from .menus import _quota_display
from utils.shared_counters import status_counters as _status_counters
import utils.shared_counters as _shared_counters


async def _ensure_user(user_id: int) -> dict:
    """获取或创建用户,走缓存。"""
    user = await get_user_cached(user_id)
    if user is None:
        user = make_user(user_id=user_id)
        users_col = get_users_col()
        await users_col.insert_one(user)
    return user


async def _get_status_text() -> str:
    users_col = get_users_col()
    files_col = get_file_records_col()
    logs_col = get_decode_logs_col()
    # 首次启动时,用 DB 查询初始化
    if not _shared_counters.status_counters_initialized:
        _status_counters["total_users"] = await users_col.count_documents({})
        _status_counters["total_files"] = await files_col.count_documents({})
        _status_counters["active_files"] = await files_col.count_documents({"status": "active"})
        today = datetime.datetime.now(datetime.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        _status_counters["today_decodes"] = await logs_col.count_documents({"request_time": {"$gte": today.isoformat()}})
        _shared_counters.status_counters_initialized = True
    relay_pending = await get_config("relay_auth_pending")
    try:
        from services.relay_pool import relay_pool
        if not relay_pool._initialized:
            await relay_pool.init()
        pool_status = await relay_pool.get_pool_status()
        if pool_status:
            ready = sum(1 for p in pool_status if p["is_ready"])
            relay_status = f"✅ 账号池 {ready}/{len(pool_status)} 就绪"
        else:
            relay_status = "⏳ 等待验证码" if relay_pending == "1" else "✅ 就绪/未配置"
    except Exception:
        relay_status = "⏳ 等待验证码" if relay_pending == "1" else "✅ 就绪/未配置"
    active_channel = await get_active_storage_channel_id()
    msg = (
        f"📊 系统概览\n\n"
        f"👤 总用户数:{_status_counters['total_users']}\n"
        f"📁 总文件数:{_status_counters['total_files']}\n"
        f"✅ 活跃文件:{_status_counters['active_files']}\n"
        f"🔄 今日解码:{_status_counters['today_decodes']}\n"
        f"📤 发送成功:{metrics.send_success_count}\n"
        f"📤 发送失败:{metrics.send_fail_count}\n"
        f"\n📺 当前主存储频道:{active_channel}\n"
        f"\n🔐 用户中继:{relay_status}\n"
        f"\n🤖 机器人状态:\n"
    )
    for name, health in metrics.bots.items():
        status_icon = "✅" if health.is_running else "❌"
        msg += f"  {status_icon} {name}: {health.total_processed}次/ {health.total_errors}次错误\n"
    return msg


async def _get_health_text() -> str:
    msg = "🤖 机器人健康状态\n\n"
    if not metrics.bots:
        msg += "⚠️ 暂无 Bot 状态数据\n"
        msg += "(各 Bot 启动后会自动上报心跳)\n"
        msg += "\n📡 以下为各 Bot 运行状态:\n"
        bot_names = ["up_bot", "idx_bot", "dsp_bot", "mon_bot", "admin_bot"]
        for name in bot_names:
            bot = metrics.get_bot(name)
            if bot.is_running:
                msg += f"  ✅ {name}: 运行中 ({bot.total_processed}次, {bot.total_errors}次错误)\n"
            else:
                msg += f"  ⏳ {name}: 未上报/离线\n"
    else:
        for name, health in metrics.bots.items():
            status_icon = "✅" if health.is_running else "❌"
            last_ping = format_datetime(health.last_ping)
            msg += (
                f"{status_icon} {name}\n"
                f"  最后活跃:{last_ping}\n"
                f"  处理次数:{health.total_processed}\n"
                f"  错误次数:{health.total_errors}\n"
            )
    return msg


async def _get_topology_text() -> str:
    """显示环形冗余拓扑状态。"""
    active_channel = await get_active_storage_channel_id()
    try:
        from database import get_cells_col
        col = get_cells_col()
        cells = await col.find({}, sort=("slot_id", 1))
    except Exception:
        cells = []

    msg = f"🔗 环形冗余拓扑\n\n"
    msg += f"📌 当前主频道: {active_channel}\n"
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
                acc = c.get("account_name", "")
                fc = c.get("file_count") or 0
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
    pending = await get_config("relay_auth_pending")

    if not relay_pool._initialized:
        try:
            await relay_pool.init()
        except Exception:
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
    "storage_channel_id": "MAIN_STORAGE_CHANNEL_ID",
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
        ("storage_channel_id", "📺 主存储频道", "⚠️需重启"),
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

    r2_keys = [
        ("r2_account_id", "☁️ R2 账号ID"),
        ("r2_access_key", "🔑 R2 Access Key"),
        ("r2_secret_key", "🔒 R2 Secret Key"),
        ("r2_bucket", "🪣 R2 桶名"),
        ("r2_endpoint", "🔗 R2 Endpoint"),
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
    r2_configured = any(v for v in r2_vals if v)
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