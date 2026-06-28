import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import settings
from database import (
    get_users_col, get_file_records_col,
    get_config, set_config,
    set_code_bot_route, delete_code_bot_route,
    set_bot_decode_interval, delete_bot_decode_interval,
    add_spare_channel, remove_spare,
    set_rotation_config,
    update_user_and_invalidate,
)
from utils.time_utils import format_datetime

from .menus import (
    _auth_required, _quota_display, _CONV_CANCEL_KEYBOARD,
    MEMBERSHIP_LEVELS, LEVEL_ALIAS,
)
from .display import _ensure_user


async def _conv_start(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, prompt: str):
    context.user_data["conv_state"] = state
    context.user_data["conv_data"] = {}
    query = update.callback_query
    await query.edit_message_text(prompt, reply_markup=_CONV_CANCEL_KEYBOARD)


async def _conv_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, prompt: str):
    context.user_data["conv_state"] = state
    context.user_data.setdefault("conv_data", {})
    await update.message.reply_text(prompt, reply_markup=_CONV_CANCEL_KEYBOARD)


def _conv_end(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("conv_state", None)
    context.user_data.pop("conv_data", None)


@_auth_required
async def handle_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("conv_state")
    if not state:
        return

    text = update.message.text.strip()
    data = context.user_data.get("conv_data", {})

    # ─── Helper: execute and end ─────────────────────────────────
    async def _end(msg: str):
        _conv_end(context)
        await update.message.reply_text(msg)

    async def _ask(next_state: str, prompt: str, extra_data: dict | None = None):
        if extra_data:
            context.user_data["conv_data"].update(extra_data)
        context.user_data["conv_state"] = next_state
        await update.message.reply_text(prompt, reply_markup=_CONV_CANCEL_KEYBOARD)

    # ─── 文件码前缀路由 ──────────────────────────────────────────
    if state == "add_code_route:prefix":
        await _ask("add_code_route:bot",
                    f"✅ 前缀已记录:`{text.lower()}`\n\n请输入目标机器人用户名(不需要 @):",
                    {"prefix": text.lower()})

    elif state == "add_code_route:bot":
        bot_username = text.lstrip("@").lower()
        await set_code_bot_route(data["prefix"], bot_username)
        await _end(f"✅ 文件码路由已设置\n  前缀:`{data['prefix']}`\n  目标机器人:@{bot_username}")

    elif state == "remove_code_route:prefix":
        prefix = text.lower()
        await delete_code_bot_route(prefix)
        await _end(f"✅ 文件码前缀路由已删除:`{prefix}`")

    # ─── Bot 解码间隔 ────────────────────────────────────────────
    elif state == "set_bot_interval:bot":
        await _ask("set_bot_interval:seconds",
                    f"✅ Bot 已记录:@{text.lstrip('@')}\n\n请输入解码间隔秒数(输入 0 取消限制):",
                    {"bot": text.lstrip("@").lower()})

    elif state == "set_bot_interval:seconds":
        try:
            interval = int(text)
        except ValueError:
            await update.message.reply_text("❌ 请输入有效的数字(秒数),例如:3")
            return
        if interval < 0:
            await update.message.reply_text("❌ 间隔秒数不能为负数,请重新输入:")
            return
        await set_bot_decode_interval(data["bot"], interval)
        msg = f"✅ 已取消 @{data['bot']} 的解码间隔限制" if interval == 0 else f"✅ @{data['bot']} 的解码间隔已设为 {interval} 秒"
        await _end(msg)

    elif state == "remove_bot_interval:bot":
        await delete_bot_decode_interval(text.lstrip("@").lower())
        await _end(f"✅ 已删除 @{text.lstrip('@')} 的解码间隔配置")

    # ─── 用户管理 ────────────────────────────────────────────────
    elif state == "user_detail:id":
        try:
            user_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字,请重新输入:")
            return
        users_col = get_users_col()
        user = await _ensure_user(user_id)
        level = user.get("membership_level", "free")
        await _end(
            f"👤 用户详情\n\n"
            f"🆔 ID:{user.get('user_id')}\n"
            f"📝 用户名:@{user.get('username') or 'N/A'}\n"
            f"👤 昵称:{user.get('first_name') or 'N/A'}\n"
            f"🏅 会员等级:{MEMBERSHIP_LEVELS.get(level, level)}\n"
            f"🔒 是否封禁:{'是 ❌' if user.get('is_banned') else '否 ✅'}\n"
            f"📤 允许上传:{'是 ✅' if user.get('can_upload') else '否 ❌'}\n"
            f"📅 解码配额:{_quota_display(user.get('daily_decode_quota'))}/天\n"
            f"📊 今日已用:{user.get('quota_used_today', 0)}次\n"
            f"🌐 外部码配额:{_quota_display(user.get('external_decode_quota'))}/天\n"
            f"🌐 外部已用:{user.get('external_used_today', 0)}次\n"
            f"📅 注册时间:{format_datetime(user.get('created_at'))}\n"
            f"🔄 更新时间:{format_datetime(user.get('updated_at'))}"
        )

    elif state == "set_level:user_id":
        try:
            int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字,请重新输入:")
            return
        await _ask("set_level:level",
                    f"✅ 用户已记录:{text}\n\n请输入会员等级(1=免费 / 2=基础 / 3=高级):",
                    {"user_id": int(text)})

    elif state == "set_level:level":
        level = LEVEL_ALIAS.get(text.strip())
        if not level:
            await update.message.reply_text("❌ 请输入 1(免费)、2(基础)或 3(高级):")
            return
        user_id = data["user_id"]
        users_col = get_users_col()
        await _ensure_user(user_id)
        update_doc = {"$set": {"membership_level": level, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}}
        if level == "free":
            update_doc["$set"]["daily_decode_quota"] = settings.FREE_DAILY_QUOTA
            update_doc["$set"]["external_decode_quota"] = settings.FREE_EXTERNAL_DAILY_QUOTA
            update_doc["$set"]["can_upload"] = True
        elif level == "basic":
            update_doc["$set"]["daily_decode_quota"] = settings.BASIC_DAILY_QUOTA
            update_doc["$set"]["external_decode_quota"] = settings.BASIC_EXTERNAL_DAILY_QUOTA
            update_doc["$set"]["can_upload"] = True
        elif level == "premium":
            update_doc["$set"]["daily_decode_quota"] = settings.PREMIUM_DAILY_QUOTA
            update_doc["$set"]["external_decode_quota"] = settings.PREMIUM_EXTERNAL_DAILY_QUOTA
            update_doc["$set"]["can_upload"] = True
        await users_col.update_one({"user_id": user_id}, update_doc)
        await update_user_and_invalidate(user_id)
        await _end(f"✅ 用户 {user_id} 已设置为 {MEMBERSHIP_LEVELS[level]}")

    elif state == "ban:user_id":
        try:
            user_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字,请重新输入:")
            return
        users_col = get_users_col()
        await _ensure_user(user_id)
        await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": True, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}})
        await update_user_and_invalidate(user_id)
        await _end(f"✅ 用户 {user_id} 已封禁")

    elif state == "unban:user_id":
        try:
            user_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字,请重新输入:")
            return
        users_col = get_users_col()
        await _ensure_user(user_id)
        await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": False, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}})
        await update_user_and_invalidate(user_id)
        await _end(f"✅ 用户 {user_id} 已解封")

    elif state == "set_quota:user_id":
        try:
            int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字,请重新输入:")
            return
        await _ask("set_quota:quota",
                    f"✅ 用户已记录:{text}\n\n请输入每日解码配额(-1 为不限,0 为禁止):",
                    {"user_id": int(text)})

    elif state == "set_quota:quota":
        try:
            quota = int(text)
        except ValueError:
            await update.message.reply_text("❌ 配额必须是数字,请重新输入(-1 为不限):")
            return
        users_col = get_users_col()
        await _ensure_user(data["user_id"])
        await users_col.update_one({"user_id": data["user_id"]}, {"$set": {"daily_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}})
        await update_user_and_invalidate(data["user_id"])
        await _end(f"✅ 用户 {data['user_id']} 每日解码配额已设为 {_quota_display(quota)}")

    elif state == "set_external_quota:user_id":
        try:
            int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字,请重新输入:")
            return
        await _ask("set_external_quota:quota",
                    f"✅ 用户已记录:{text}\n\n请输入外部码配额(-1 不限,0 禁止):",
                    {"user_id": int(text)})

    elif state == "set_external_quota:quota":
        try:
            quota = int(text)
        except ValueError:
            await update.message.reply_text("❌ 配额必须是数字,请重新输入(-1 不限,0 禁止):")
            return
        users_col = get_users_col()
        await _ensure_user(data["user_id"])
        await users_col.update_one({"user_id": data["user_id"]}, {"$set": {"external_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.UTC).isoformat()}})
        await update_user_and_invalidate(data["user_id"])
        await _end(f"✅ 用户 {data['user_id']} 外部码配额已设为 {_quota_display(quota)}")

    # ─── 文件管理 ────────────────────────────────────────────────
    elif state == "file_detail:code":
        file_code = text.strip()
        files_col = get_file_records_col()
        record = await files_col.find_one({"file_code": file_code})
        if record is None:
            await _end(f"❌ 文件码 {file_code} 不存在")
            return
        file_types = record.get("file_types", {})
        if isinstance(file_types, str):
            import json
            file_types = json.loads(file_types) if file_types else {}
        type_desc = " ".join(f"{v}个{k}" for k, v in sorted(file_types.items())) if file_types else "外部缓存文件"
        backups = record.get("backup_channel_msg_ids", [])
        if isinstance(backups, str):
            import json
            backups = json.loads(backups) if backups else []
        await _end(
            f"📁 文件详情\n\n"
            f"🔑 文件码:{file_code}\n"
            f"👤 上传者:{record.get('uploader_id')}\n"
            f"📦 文件类型:{type_desc}\n"
            f"📊 状态:{record.get('status', 'active')}\n"
            f"📈 请求次数:{record.get('request_count', 0)}\n"
            f"📅 创建时间:{format_datetime(record.get('create_time'))}\n"
            f"📺 主频道:{record.get('primary_channel_id')}\n"
            f"🔄 备份数:{len(backups)}个频道"
            + (f"\n📝 备注:{record.get('note', '')}" if record.get("note", "") else "")
        )

    elif state == "delete_file:code":
        file_code = text.strip()
        files_col = get_file_records_col()
        result = await files_col.update_one({"file_code": file_code}, {"$set": {"status": "deleted"}})
        if result.matched_count == 0:
            await _end(f"❌ 文件码 {file_code} 不存在")
        else:
            await _end(f"✅ 文件 {file_code} 已删除")

    # ─── 中继 ────────────────────────────────────────────────────
    elif state == "relay_code:code":
        code = text.strip()
        # 验证码写入 DB,由 idx_bot 的 relay_instance 自动读取
        await set_config("relay_auth_code", code)
        await set_config("relay_auth_pending", "1")
        await _end(f"✅ 验证码 `{code}` 已提交\n中继实例将在几秒内自动获取并使用。")

    elif state == "relay_set_api:api_id":
        try:
            int(text)
        except ValueError:
            await update.message.reply_text("❌ API_ID 必须是数字,请重新输入:")
            return
        await _ask("relay_set_api:api_hash",
                    f"✅ API_ID 已记录:{text}\n\n第二步:请输入 API_HASH:",
                    {"api_id": text.strip()})

    elif state == "relay_set_api:api_hash":
        await _ask("relay_set_api:phone",
                    f"✅ API_HASH 已记录:{text}\n\n第三步:请输入手机号(含区号,如 +8613800138000):",
                    {"api_hash": text.strip()})

    elif state == "relay_set_api:phone":
        from services.relay_pool import relay_pool
        phone = text.strip()
        api_id = int(data["api_id"])
        api_hash = data["api_hash"]
        instance = await relay_pool.add_account(api_id, api_hash, phone)
        # 开始登录
        try:
            await instance.login_with_credentials(api_id, api_hash, phone)
            await _end(
                f"✅ 中继账号已添加并登录成功\n"
                f"  API_ID: {api_id}\n"
                f"  API_HASH: {api_hash[:8]}...\n"
                f"  手机号: {phone}\n\n"
                f"该账号已加入账号池,可立即使用。"
            )
        except RuntimeError as e:
            await relay_pool.remove_account(phone)
            await _end(f"❌ 登录失败: {e}")

    # ─── 系统配置 ────────────────────────────────────────────────
    elif state == "set_storage_channel:id":
        try:
            channel_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 频道ID必须是数字,请重新输入:")
            return
        await set_config("storage_channel_id", str(channel_id))
        await _end(f"✅ 主存储频道已设为 {channel_id}\n⚠️ 需重启所有机器人后生效")

    elif state == "set_file_prefix:prefix":
        prefix = text.strip()
        await set_config("file_code_prefix", prefix)
        await _end(f"✅ 文件码前缀已设为 {prefix}\n⚠️ 需重启 up_bot 后生效")

    elif state == "set_force_join:channel_id":
        try:
            int(text)
        except ValueError:
            await update.message.reply_text("❌ 频道ID必须是数字,请重新输入:")
            return
        await _ask("set_force_join:link",
                    f"✅ 频道已记录:{text}\n\n请输入加群链接(如无可直接发送 0 跳过):",
                    {"channel_id": int(text)})

    elif state == "set_force_join:link":
        link = text.strip()
        if link == "0":
            link = ""
        await set_config("force_join_channel_id", str(data["channel_id"]))
        if link:
            await set_config("force_join_link", link)
        await _end(f"✅ 强制加群频道已设为 {data['channel_id']}" + (f"\n🔗 链接:{link}" if link else "") + " ✅热更新")

    elif state == "set_username:role":
        role = text.lower()
        if role not in ("upload", "decoder", "sender"):
            await update.message.reply_text("❌ 角色必须是 upload、decoder 或 sender,请重新输入:")
            return
        await _ask("set_username:name",
                    f"✅ 角色已记录:{role}\n\n请输入 @用户名(不需要 @):",
                    {"role": role})

    elif state == "set_username:name":
        key_map = {"upload": "upload_bot_username", "decoder": "decoder_bot_username", "sender": "sender_bot_username"}
        key = key_map.get(data["role"])
        username = text.lstrip("@")
        await set_config(key, username)
        await _end(f"✅ {data['role']} 机器人用户名已设为 @{username} ✅热更新")

    elif state == "set_quota_default:level":
        level = LEVEL_ALIAS.get(text.strip())
        if not level:
            await update.message.reply_text("❌ 请输入 1(免费)、2(基础)或 3(高级):")
            return
        await _ask("set_quota_default:quota",
                    f"✅ 等级已记录:{text}\n\n请输入每日默认解码配额(-1 为不限):",
                    {"level": level})

    elif state == "set_quota_default:quota":
        try:
            quota = int(text)
        except ValueError:
            await update.message.reply_text("❌ 配额必须是数字(-1 表示不限),请重新输入:")
            return
        await set_config(f"quota_default_{data['level']}", str(quota))
        await _ask("set_quota_default:ext_quota",
                    f"✅ 解码配额已记录:{_quota_display(quota)}\n\n请输入外部码默认配额(-1 不限,0 禁止,直接发送 0 跳过):",
                    {"quota": quota})

    elif state == "set_quota_default:ext_quota":
        try:
            ext_quota = int(text)
        except ValueError:
            ext_quota = 0
        if ext_quota > 0:
            await set_config(f"quota_external_{data['level']}", str(ext_quota))
        msg = f"✅ {data['level']} 日配额已设为 {_quota_display(data['quota'])}"
        if ext_quota > 0:
            msg += f",外部码配额 {_quota_display(ext_quota)}"
        msg += " ✅热更新"
        await _end(msg)

    elif state == "set_r2:account_id":
        await _ask("set_r2:access_key",
                    f"✅ 账号ID已记录:{text}\n\n第二步:请输入 R2 Access Key ID:",
                    {"account_id": text.strip()})

    elif state == "set_r2:access_key":
        await _ask("set_r2:secret_key",
                    f"✅ Access Key 已记录:{text[:8]}...\n\n第三步:请输入 R2 Secret Access Key:",
                    {"access_key": text.strip()})

    elif state == "set_r2:secret_key":
        await _ask("set_r2:bucket",
                    f"✅ Secret Key 已记录:{text[:8]}...\n\n第四步:请输入桶名(Bucket Name,直接发送 0 跳过):",
                    {"secret_key": text.strip()})

    elif state == "set_r2:bucket":
        bucket = text.strip()
        if bucket == "0":
            bucket = ""
        await set_config("r2_account_id", data["account_id"])
        await set_config("r2_access_key", data["access_key"])
        await set_config("r2_secret_key", data["secret_key"])
        if bucket:
            await set_config("r2_bucket", bucket)
        await _end(f"✅ R2 备份配置已保存\n  Bucket: {bucket or '(默认)'}\n⚠️ 需重启后生效")

    elif state == "set_db_backup:interval":
        try:
            interval = int(text)
        except ValueError:
            await update.message.reply_text("❌ 间隔分钟数必须是数字,请重新输入:")
            return
        await _ask("set_db_backup:enabled",
                    f"✅ 间隔已记录:{interval} 分钟\n\n请输入开关状态(on / off):",
                    {"interval": interval})

    elif state == "set_db_backup:enabled":
        on_off = text.strip().lower()
        if on_off not in ("on", "off"):
            await update.message.reply_text("❌ 请输入 on 或 off:")
            return
        await set_config("db_backup_interval", str(data["interval"]))
        await set_config("db_backup_enabled", "true" if on_off == "on" else "false")
        await _end(f"✅ DB 自动备份已{'开启' if on_off == 'on' else '关闭'},间隔 {data['interval']} 分钟 ✅热更新")

    # ─── 备用池 ────────────────────────────────────────────────
    elif state == "spare_add:channel_id":
        try:
            channel_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 频道ID必须是数字,请重新输入:")
            return
        await _ask("spare_add:account_name",
                    f"✅ 频道ID已记录:{channel_id}\n\n请输入账号名(不填则作为通用备用池,直接发送 0 跳过):",
                    {"channel_id": channel_id})

    elif state == "spare_add:account_name":
        account_name = text.strip()
        if account_name == "0":
            account_name = None
        await add_spare_channel(data["channel_id"], account_name)
        acc_info = f" (账号: {account_name})" if account_name else " (通用备用池)"
        await _end(f"✅ 备用频道已添加\n  频道ID: {data['channel_id']}{acc_info}")

    elif state == "spare_remove:channel_id":
        try:
            channel_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 频道ID必须是数字,请重新输入:")
            return
        await remove_spare(channel_id)
        await _end(f"✅ 已从备用池移除频道: {channel_id}")

    # ─── 轮转配置 ──────────────────────────────────────────────
    elif state == "rotation_set:key":
        key = text.strip().lower()
        valid_keys = {"active_window_size", "files_per_slot", "time_per_slot"}
        if key not in valid_keys:
            await update.message.reply_text(
                f"❌ 无效参数: {key}\n有效参数: {', '.join(sorted(valid_keys))}\n请重新输入:"
            )
            return
        labels = {
            "active_window_size": "活跃窗口大小(每组几个活跃频道)",
            "files_per_slot": "每频道文件数",
            "time_per_slot": "每频道活跃时间(秒)",
        }
        await _ask("rotation_set:value",
                    f"✅ 参数已选择:{key} ({labels[key]})\n\n请输入值(数字):",
                    {"rotation_key": key})

    elif state == "rotation_set:value":
        try:
            int(text)
        except ValueError:
            await update.message.reply_text("❌ 值必须是数字,请重新输入:")
            return
        db_key = f"rotation_{data['rotation_key']}"
        await set_rotation_config(db_key, text)
        label_map = {
            "active_window_size": "活跃窗口大小",
            "files_per_slot": "每频道文件数",
            "time_per_slot": "每频道时间(秒)",
        }
        await _end(
            f"✅ 轮转配置已更新\n  {label_map.get(data['rotation_key'], data['rotation_key'])}: {text}\n\n"
            f"Mon Bot 将在下一轮自动加载新配置。"
        )

    # 未知状态 → 清理
    else:
        _conv_end(context)
        await update.message.reply_text("⏳ 对话已超时,请重新点击按钮开始操作。")