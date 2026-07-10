import time

import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from config import settings
from database import (
    get_users_col, get_file_records_col,
    set_config,
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


# 对话超时时间(秒):用户 5 分钟无响应自动清理对话状态
_CONV_TIMEOUT_SECONDS = 300


async def _conv_start(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, prompt: str):
    context.user_data["conv_state"] = state
    context.user_data["conv_data"] = {}
    context.user_data["conv_started_at"] = time.time()
    query = update.callback_query
    try:
        await query.edit_message_text(prompt, reply_markup=_CONV_CANCEL_KEYBOARD)
    except Exception:
        # 忽略 Message is not modified 错误(用户重复点击相同按钮)
        pass


async def _conv_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, prompt: str):
    context.user_data["conv_state"] = state
    context.user_data["conv_started_at"] = time.time()
    context.user_data.setdefault("conv_data", {})
    await update.message.reply_text(prompt, reply_markup=_CONV_CANCEL_KEYBOARD)


def _conv_end(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("conv_state", None)
    context.user_data.pop("conv_data", None)
    context.user_data.pop("conv_started_at", None)
    # 清理中继交互式流程中可能残留的 relay_phone（避免影响下次 relay_code 流程）
    context.user_data.pop("relay_phone", None)


async def _finalize_relay_login(context, client, phone, temp_session):
    """登录成功后的收尾工作:迁移 session 到持久化路径,写入 DB,通知 idx_bot。"""
    import os, shutil
    from loguru import logger
    from services.relay_pool import relay_pool

    try:
        # 迁移临时 session 到持久化路径(避免 idx_bot 重复登录)
        persistent_session = str(Path(__file__).parent.parent.parent / "data" / f"relay_session_{phone}")
        for suffix in ("", "-journal"):
            src = temp_session + suffix
            dst = persistent_session + suffix
            if os.path.exists(src):
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.move(src, dst)

        # 获取 api_id/api_hash 写入 DB
        api_id = client.api_id
        api_hash = client.api_hash
        await relay_pool.add_account(api_id, api_hash, phone)

        # 通知 idx_bot 同步新账号
        try:
            from database.cache_store import get_cache_store
            await get_cache_store().notify_relay_change()
        except Exception:
            pass

        # 清理 user_data 和临时客户端
        _conv_end(context)
        try:
            await client.disconnect()
        except Exception:
            pass
        logger.info(f"[Admin] 中继账号 {phone} 登录成功并已写入 DB")
    except Exception as e:
        logger.error(f"[Admin] finalize_relay_login 异常: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        # 清理临时 session
        for suffix in ("", "-journal"):
            try:
                p = temp_session + suffix
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        raise


async def _cleanup_relay_add(context):
    """清理中继添加流程中的临时客户端和 session 文件(超时/取消时调用)。"""
    import os
    client = context.user_data.get("relay_add_client")
    temp_session = context.user_data.get("relay_add_temp_session", "")
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    for suffix in ("", "-journal"):
        try:
            p = temp_session + suffix
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
    context.user_data.pop("relay_add_client", None)
    context.user_data.pop("relay_add_phone", None)
    context.user_data.pop("relay_add_phone_code_hash", None)
    context.user_data.pop("relay_add_temp_session", None)


@_auth_required
async def handle_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("conv_state")
    if not state:
        return

    # 对话超时检查:超过 5 分钟无响应自动清理
    started_at = context.user_data.get("conv_started_at", 0)
    if time.time() - started_at > _CONV_TIMEOUT_SECONDS:
        # 如果正在中继添加流程,清理临时客户端和 session 文件
        if state and state.startswith("relay_add:"):
            await _cleanup_relay_add(context)
        _conv_end(context)
        await update.message.reply_text("⏳ 对话已超时(5分钟无响应),请重新点击按钮开始操作。")
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
        # 刷新超时计时器,确保多轮对话不被误判超时
        context.user_data["conv_started_at"] = time.time()
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
        update_doc = {"$set": {"membership_level": level, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}}
        if level == "free":
            update_doc["$set"]["daily_decode_quota"] = settings.FREE_DAILY_QUOTA
            update_doc["$set"]["external_decode_quota"] = settings.FREE_EXTERNAL_DAILY_QUOTA
            update_doc["$set"]["can_upload"] = True
            update_doc["$set"]["external_used_today"] = 0
        elif level == "basic":
            update_doc["$set"]["daily_decode_quota"] = settings.BASIC_DAILY_QUOTA
            update_doc["$set"]["external_decode_quota"] = settings.BASIC_EXTERNAL_DAILY_QUOTA
            update_doc["$set"]["can_upload"] = True
            update_doc["$set"]["external_used_today"] = 0
        elif level == "premium":
            update_doc["$set"]["daily_decode_quota"] = settings.PREMIUM_DAILY_QUOTA
            update_doc["$set"]["external_decode_quota"] = settings.PREMIUM_EXTERNAL_DAILY_QUOTA
            update_doc["$set"]["can_upload"] = True
            update_doc["$set"]["external_used_today"] = 0
        await users_col.update_one({"user_id": user_id}, update_doc)
        await update_user_and_invalidate(user_id)
        from database.cache_store import invalidate_user_quota_cache
        await invalidate_user_quota_cache(user_id)
        await _end(f"✅ 用户 {user_id} 已设置为 {MEMBERSHIP_LEVELS[level]}")

    elif state == "ban:user_id":
        try:
            user_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字,请重新输入:")
            return
        users_col = get_users_col()
        await _ensure_user(user_id)
        await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": True, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}})
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
        await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": False, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}})
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
        await users_col.update_one({"user_id": data["user_id"]}, {"$set": {"daily_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}})
        await update_user_and_invalidate(data["user_id"])
        from database.cache_store import invalidate_user_quota_cache
        await invalidate_user_quota_cache(data["user_id"])
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
        await users_col.update_one({"user_id": data["user_id"]}, {"$set": {"external_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}})
        await update_user_and_invalidate(data["user_id"])
        from database.cache_store import invalidate_user_quota_cache
        await invalidate_user_quota_cache(data["user_id"])
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
        # 验证码写入 DB,由 relay_instance 自动读取
        phone = context.user_data.get("relay_phone", "")
        # R31-2: 交互式按钮入口未设置 relay_phone，从 relay pool 自动检测等待中的账号
        if not phone:
            try:
                from services.relay_pool import relay_pool
                from database import get_config as _get_cfg
                if relay_pool._initialized:
                    for inst in relay_pool.instances:
                        if await _get_cfg(f"relay_auth_pending:{inst.phone}") == "1":
                            phone = inst.phone
                            break
            except Exception:
                pass
        if not phone:
            await _end("❌ 无法确定中继账号，请使用 /relay_code <手机号> <验证码> 直接提交")
            return
        await set_config(f"relay_auth_code:{phone}", code)
        await set_config(f"relay_auth_pending:{phone}", "1")
        await _end(f"✅ 验证码 `{code}` 已提交\n中继实例将在几秒内自动获取并使用。")

    elif state == "relay_password:password":
        password = text.strip()
        if not password:
            await _end("❌ 密码不能为空")
            return
        phone = context.user_data.get("relay_phone", "")
        if not phone:
            try:
                from services.relay_pool import relay_pool
                from database import get_config as _get_cfg
                if relay_pool._initialized:
                    for inst in relay_pool.instances:
                        if await _get_cfg(f"relay_password_pending:{inst.phone}") == "1":
                            phone = inst.phone
                            break
            except Exception:
                pass
        if not phone:
            await _end("❌ 无法确定中继账号，请使用 /relay_password <手机号> <密码> 直接提交")
            return
        await set_config(f"relay_auth_password:{phone}", password)
        await _end(f"✅ 二步验证密码已提交\n中继实例将在几秒内自动获取并使用。")

    elif state == "relay_add:phone":
        from services.relay_pool import relay_pool, _normalize_phone
        phone = _normalize_phone(text.strip())
        api_id = settings.RELAY_API_ID
        api_hash = settings.RELAY_API_HASH
        if not api_id or not api_hash:
            await _end("❌ 中继 API 配置未设置\n请在 .env 文件中配置 RELAY_API_ID 和 RELAY_API_HASH\n（从 https://my.telegram.org 申请）")
            return
        try:
            api_id = int(api_id)
        except (TypeError, ValueError):
            await _end(f"❌ api_id 必须是数字,当前值: {api_id}")
            return

        # 使用临时客户端在 admin_bot 进程完成登录流程
        from telethon import TelegramClient
        from telethon.errors import ApiIdInvalidError, PhoneCodeInvalid, PhoneCodeExpired
        import tempfile, os

        temp_session = os.path.join(tempfile.gettempdir(), f"relay_login_{phone.lstrip('+')}_{int(time.time())}")
        client = None
        try:
            client = TelegramClient(temp_session, api_id, api_hash, timeout=30)
            await client.connect()
            sent = await client.send_code_request(phone)
            # 存储临时客户端状态到 user_data
            context.user_data["relay_add_client"] = client
            context.user_data["relay_add_phone"] = phone
            context.user_data["relay_add_phone_code_hash"] = sent.phone_code_hash
            context.user_data["relay_add_temp_session"] = temp_session
            masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
            await _conv_ask(update, context, "relay_add:code",
                            f"📱 验证码已发送到 {masked}\n\n"
                            f"请输入收到的验证码(5-6 位数字):")
        except ApiIdInvalidError:
            await _end("❌ api_id/api_hash 无效(Telegram 服务器拒绝)\n请检查 .env 中的 RELAY_API_ID 和 RELAY_API_HASH")
        except Exception as e:
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            for suffix in ("", "-journal"):
                try:
                    p = temp_session + suffix
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
            await _end(f"❌ 发送验证码失败: {e}")

    elif state == "relay_add:code":
        client = context.user_data.get("relay_add_client")
        phone = context.user_data.get("relay_add_phone", "")
        phone_code_hash = context.user_data.get("relay_add_phone_code_hash", "")
        temp_session = context.user_data.get("relay_add_temp_session", "")
        if not client:
            await _end("❌ 会话已过期,请重新从菜单选择添加中继账号")
            return
        code = text.strip().replace(" ", "")
        if not code.isdigit() or len(code) not in (5, 6):
            await update.message.reply_text("❌ 验证码应为 5-6 位数字,请重新输入:")
            return
        from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalid, PhoneCodeExpired
        try:
            await client.sign_in(phone, phone_code_hash, code)
            await _finalize_relay_login(context, client, phone, temp_session)
        except SessionPasswordNeededError:
            await _conv_ask(update, context, "relay_add:password",
                            "🔒 该账号开启了二步验证\n\n请输入二步验证密码:")
        except (PhoneCodeInvalid, PhoneCodeExpired) as e:
            await _cleanup_relay_add(context)
            await _end(f"❌ 验证码错误: {e}\n请重新从菜单选择添加中继账号")
        except Exception as e:
            await _cleanup_relay_add(context)
            await _end(f"❌ 登录失败: {e}\n请重新从菜单选择添加中继账号")

    elif state == "relay_add:password":
        client = context.user_data.get("relay_add_client")
        phone = context.user_data.get("relay_add_phone", "")
        temp_session = context.user_data.get("relay_add_temp_session", "")
        if not client:
            await _end("❌ 会话已过期,请重新从菜单选择添加中继账号")
            return
        password = text.strip()
        if not password:
            await update.message.reply_text("❌ 密码不能为空,请重新输入:")
            return
        try:
            await client.check_password(password)
            await _finalize_relay_login(context, client, phone, temp_session)
        except Exception as e:
            await _cleanup_relay_add(context)
            await _end(f"❌ 二步验证密码错误: {e}\n请重新从菜单选择添加中继账号")

    elif state == "relay_remove:phone":
        from services.relay_pool import relay_pool, _normalize_phone
        phone = _normalize_phone(text.strip())
        removed = await relay_pool.remove_account(phone)
        if removed:
            try:
                from database.cache_store import get_cache_store
                await get_cache_store().notify_relay_change()
            except Exception:
                pass
            await _end(f"✅ 已移除中继账号: {phone[:3]}****{phone[-2:] if len(phone) > 5 else '***'}")
        else:
            await _end(f"❌ 未找到该手机号的中继账号: {phone}")

    elif state == "set_access_limit:code":
        file_code = text.strip()
        await _ask("set_access_limit:max",
                   f"✅ 文件码已记录:{file_code}\n\n请输入最大访问次数(0=不限制):",
                   {"file_code": file_code})

    elif state == "set_access_limit:max":
        try:
            max_requests = int(text)
            if max_requests < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ 必须是非负整数(0=不限制),请重新输入:")
            return
        from database import update_file_record_and_invalidate
        try:
            await update_file_record_and_invalidate(
                data["file_code"], {"$set": {"max_requests": max_requests}}
            )
        except Exception as e:
            await _end(f"❌ 设置失败: {e}")
            return
        limit_text = f"{max_requests} 次" if max_requests > 0 else "不限制"
        await _end(f"✅ 文件码 {data['file_code']} 访问限制已设为 {limit_text}")

    elif state == "cell_add:slot_id":
        slot_id = text.strip()
        await _ask("cell_add:channel_id",
                   f"✅ 槽位ID已记录:{slot_id}\n\n请输入频道ID(数字,如 -1001234567890):",
                   {"slot_id": slot_id})

    elif state == "cell_add:channel_id":
        try:
            channel_id = int(text)
        except ValueError:
            await update.message.reply_text("❌ 频道ID必须是数字,请重新输入:")
            return
        await _ask("cell_add:account_name",
                   f"✅ 频道ID已记录:{channel_id}\n\n请输入账号名(不填则无,直接发送 0 跳过):",
                   {"slot_id": data["slot_id"], "channel_id": channel_id})

    elif state == "cell_add:account_name":
        account_name = text.strip()
        if account_name == "0":
            account_name = ""
        await _ask("cell_add:status",
                   f"✅ 账号名已记录:{account_name or '(无)'}\n\n请输入状态(active/shadow1/shadow2/r100,默认 shadow1,直接发送 0 跳过):",
                   {"slot_id": data["slot_id"], "channel_id": data["channel_id"], "account_name": account_name})

    elif state == "cell_add:status":
        status = text.strip()
        if status == "0":
            status = "shadow1"
        if status not in ("active", "shadow1", "shadow2", "r100"):
            await update.message.reply_text("❌ 状态必须是 active/shadow1/shadow2/r100 之一,请重新输入:")
            return
        from database.cache_store import get_cache_store
        store = get_cache_store()
        existing = await store.get_all_cells_local()
        if any(c.get("slot_id") == data["slot_id"] for c in existing):
            await _end(f"❌ slot_id {data['slot_id']} 已存在")
            return
        if any(c.get("channel_id") == data["channel_id"] for c in existing):
            await _end(f"❌ channel_id {data['channel_id']} 已被其他槽位占用")
            return
        import time as _time
        import datetime as _dt
        now_ts = _time.time()
        new_cell = {
            "slot_id": data["slot_id"],
            "channel_id": data["channel_id"],
            "status": status,
            "next_active_chat_id": None,
            "prev_slot_id": None,
            "demoted_to_channel_id": None,
            "account_name": data["account_name"],
            "is_r100": 1 if status == "r100" else 0,
            "last_heartbeat": None,
            "last_synced_msg_id": 0,
            "degrade_count": 0,
            "file_count": 0,
            "rotation_started_at": None,
            "updated_at": now_ts,
            "crdb_synced": 0,
        }
        try:
            await store.bulk_upsert_cells_local([new_cell])
            from bots.admin_bot.display import invalidate_cells_cache
            await invalidate_cells_cache()
            await _end(
                f"✅ 已添加槽位\n"
                f"  slot_id: {data['slot_id']}\n"
                f"  channel_id: {data['channel_id']}\n"
                f"  account: {data['account_name'] or '(无)'}\n"
                f"  status: {status}\n\n"
                f"其他 bot 将在 5-60 秒内感知变更。"
            )
        except Exception as e:
            await _end(f"❌ 添加失败: {e}")

    elif state == "cell_remove:slot_id":
        slot_id = text.strip()
        from database.cache_store import get_cache_store
        store = get_cache_store()
        existing = await store.get_all_cells_local()
        target = None
        for c in existing:
            if c.get("slot_id") == slot_id:
                target = c
                break
        if not target:
            await _end(f"❌ slot_id {slot_id} 不存在")
            return
        if target.get("status") == "active":
            await _end(f"❌ 拒绝移除 active 状态的槽位 {slot_id},请先等待轮转降级后再移除。")
            return
        try:
            deleted = await store.delete_cell_local(slot_id)
            if deleted:
                from bots.admin_bot.display import invalidate_cells_cache
                await invalidate_cells_cache()
                await _end(f"✅ 已移除槽位 {slot_id}\n其他 bot 将在 5-60 秒内感知变更。")
            else:
                await _end(f"❌ 移除失败: slot_id {slot_id} 不存在")
        except Exception as e:
            await _end(f"❌ 移除失败: {e}")

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
        # P2-4: R2 Secret Key 比照 relay api_hash 做 Fernet 加密存储
        from database.relay_db import encrypt as _encrypt_secret
        await set_config("r2_secret_key", _encrypt_secret(data["secret_key"]))
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