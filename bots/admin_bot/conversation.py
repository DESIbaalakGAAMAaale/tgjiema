import os
from services.sink_adapters.telegram_adapter import (
    safe_reply_text, safe_send_message, safe_edit_message_text,
)
from services.sink_adapters.telegram_helpers import (
    Update,
    ContextTypes,
)
# R65 P1-01: typed adapter 要求 UserMessage | ErrorEnvelope
from services.user_message import UserMessage
# R65 P1-04: 模块级 logger(observability allowlist 清零后,except 块需 logger.exception)
from loguru import logger
import re
import time
import datetime
from pathlib import Path




from config import settings
from database import (
    get_users_col, get_file_records_col,
    set_config,
    set_code_bot_route, delete_code_bot_route,
    set_code_bot_route_regex, delete_code_bot_route_regex,
    add_spare_channel, remove_spare,
    set_rotation_config,
    update_user_and_invalidate,
    add_relay_whitelist,
    set_bot_decode_interval, delete_bot_decode_interval,
)
from services.i18n import translate as _i18n_t
from utils.time_utils import format_datetime

from .menus import (
    _auth_required, _quota_display, _CONV_CANCEL_KEYBOARD,
    MEMBERSHIP_LEVELS, LEVEL_ALIAS,
)
from .display import _ensure_user


# 对话超时时间(秒):用户 5 分钟无响应自动清理对话状态
_CONV_TIMEOUT_SECONDS = 300


def _rename_auth_session(auth_path: str, target_path: str):
    """H-2/R-2: 将临时 session 文件原子替换为正式路径。

    admin_bot 用 *_auth 临时路径登录,成功后替换正式路径供 idx_bot 使用。
    R-2: 使用 os.replace 原子替换,避免先删后 rename 在崩溃时丢失 session。
    """
    try:
        auth_p = Path(auth_path)
        target_p = Path(target_path)
        # R-2: os.replace 原子替换(自动覆盖目标,无需先删)
        for suffix in ("", "-journal", "-wal", "-shm"):
            src = Path(str(auth_p) + suffix)
            dst = Path(str(target_p) + suffix)
            if src.exists():
                os.replace(src, dst)
    except Exception as e:
        from loguru import logger
        logger.warning(f"[Admin] _rename_auth_session 失败: {e}")


def _cleanup_auth_session(auth_path: str):
    """H-2: 清理临时 session 文件(对话取消/失败时调用)。"""
    try:
        auth_p = Path(auth_path)
        for suffix in ("", "-journal", "-wal", "-shm"):
            p = Path(str(auth_p) + suffix)
            if p.exists():
                p.unlink()
    except Exception:
        logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:_cleanup_auth_session'))


async def _conv_start(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, prompt: str):
    context.user_data["conv_state"] = state
    context.user_data["conv_data"] = {}
    context.user_data["conv_started_at"] = time.time()
    query = update.callback_query
    try:
        await safe_edit_message_text(query, UserMessage.from_raw_text(prompt), reply_markup=_CONV_CANCEL_KEYBOARD)
    except Exception:
        # 忽略 Message is not modified 错误(用户重复点击相同按钮)
        logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:_conv_start'))


async def _conv_ask(update: Update, context: ContextTypes.DEFAULT_TYPE, state: str, prompt: str):
    context.user_data["conv_state"] = state
    context.user_data["conv_started_at"] = time.time()
    context.user_data.setdefault("conv_data", {})
    await safe_reply_text(update.message, UserMessage.from_raw_text(prompt), reply_markup=_CONV_CANCEL_KEYBOARD)


async def _conv_end(context: ContextTypes.DEFAULT_TYPE):
    """清理对话状态。如果有临时 Telethon 客户端（relay_add 流程），一并断开连接。"""
    client = context.user_data.pop("_relay_temp_client", None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:_conv_end'))
    # H-2: 清理临时 session 文件(对话取消/超时时)
    auth_path = context.user_data.pop("_relay_auth_session_path", None)
    if auth_path:
        _cleanup_auth_session(auth_path)
    context.user_data.pop("conv_state", None)
    context.user_data.pop("conv_data", None)
    context.user_data.pop("conv_started_at", None)
    # 清理中继交互式流程中可能残留的 relay_phone（避免影响下次 relay_code 流程）
    context.user_data.pop("relay_phone", None)


@_auth_required
async def handle_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("conv_state")
    if not state:
        return

    # 对话超时检查:超过 5 分钟无响应自动清理
    started_at = context.user_data.get("conv_started_at", 0)
    if time.time() - started_at > _CONV_TIMEOUT_SECONDS:
        await _conv_end(context)
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s1')))
        return

    text = update.message.text.strip()
    data = context.user_data.get("conv_data", {})

    # ─── Helper: execute and end ─────────────────────────────────
    async def _end(msg: str):
        await _conv_end(context)
        await safe_reply_text(update.message, UserMessage.from_raw_text(msg))

    async def _ask(next_state: str, prompt: str, extra_data: dict | None = None):
        if extra_data:
            context.user_data["conv_data"].update(extra_data)
        context.user_data["conv_state"] = next_state
        # 刷新超时计时器,确保多轮对话不被误判超时
        context.user_data["conv_started_at"] = time.time()
        await safe_reply_text(update.message, UserMessage.from_raw_text(prompt), reply_markup=_CONV_CANCEL_KEYBOARD)

    # ─── 文件码前缀路由 ──────────────────────────────────────────
    if state == "add_code_route:prefix":
        await _ask("add_code_route:bot",
                    _i18n_t('bot.admin_bot.conversation.s2', text_lower=text.lower()),
                    {"prefix": text.lower()})

    elif state == "add_code_route:bot":
        bot_username = text.lstrip("@").lower()
        await set_code_bot_route(data["prefix"], bot_username)
        await _end(_i18n_t('bot.admin_bot.conversation.s3', data_prefix=data['prefix'], bot_username=bot_username))

    elif state == "remove_code_route:prefix":
        prefix = text.lower()
        await delete_code_bot_route(prefix)
        await _end(_i18n_t('bot.admin_bot.conversation.s4', prefix=prefix))

    # ─── 文件码正则路由（用于 40位hash / emoji 等非前缀式第三方码）───
    elif state == "add_code_route_regex:pattern":
        pattern = text
        # P2-7: 正则路由 ReDoS 防护 —— 校验长度、嵌套量词、可编译性
        if len(pattern) > 200:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s6')))
            return
        # 基础 ReDoS 检测:嵌套量词,如 (.*+)+、(.+)+
        if re.search(r'\([^)]*[+*?][^)]*\)[+*?]', pattern) or re.search(r'(.+.*|.*.+){2,}', pattern):
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s7')))
            return
        try:
            re.compile(pattern)
        except re.error as e:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s8', e=e)))
            return
        await _ask("add_code_route_regex:bot",
                    _i18n_t('bot.admin_bot.conversation.s5', pattern=pattern),
                    {"pattern": pattern})

    elif state == "add_code_route_regex:bot":
        bot_username = text.lstrip("@").lower()
        pattern = data["pattern"]
        try:
            route_id = await set_code_bot_route_regex(pattern, bot_username)
            await _end(_i18n_t('bot.admin_bot.conversation.s9', route_id=route_id, pattern=pattern, bot_username=bot_username))
        except ValueError as e:
            await _end(_i18n_t('bot.admin_bot.conversation.s10', e=e))

    elif state == "remove_code_route_regex:id":
        try:
            route_id = int(text.strip())
        except ValueError:
            await _end(_i18n_t('bot.admin_bot.conversation.s16'))
            return
        ok = await delete_code_bot_route_regex(route_id)
        if ok:
            await _end(_i18n_t('bot.admin_bot.conversation.s11', route_id=route_id))
        else:
            await _end(_i18n_t('bot.admin_bot.conversation.s12', route_id=route_id))

    # ─── Bot 解码间隔 ────────────────────────────────────────────
    elif state == "set_bot_interval:bot":
        await _ask("set_bot_interval:seconds",
                    _i18n_t('bot.admin_bot.conversation.s13', text_lstrip=text.lstrip('@')),
                    {"bot": text.lstrip("@").lower()})

    elif state == "set_bot_interval:seconds":
        try:
            interval = int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s19')))
            return
        if interval < 0:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s17')))
            return
        await set_bot_decode_interval(data["bot"], interval)
        msg = _i18n_t('bot.admin_bot.conversation.s14', data_bot=data['bot']) if interval == 0 else _i18n_t('bot.admin_bot.conversation.s15', data_bot=data['bot'], interval=interval)
        await _end(msg)

    elif state == "remove_bot_interval:bot":
        await delete_bot_decode_interval(text.lstrip("@").lower())
        await _end(_i18n_t('bot.admin_bot.conversation.s18', text_lstrip=text.lstrip('@')))

    # ─── 用户管理 ────────────────────────────────────────────────
    elif state == "user_detail:id":
        try:
            user_id = int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s22')))
            return
        users_col = get_users_col()
        user = await _ensure_user(user_id)
        level = user.get("membership_level", "free")
        await _end(
            _i18n_t('bot.admin_bot.conversation.s20', user_get_user_id=user.get('user_id'), user_get_username_or_N_A=user.get('username') or 'N/A', user_get_first_name_or_N_A=user.get('first_name') or 'N/A', MEMBERSHIP_LEVELS_get_level_level=MEMBERSHIP_LEVELS.get(level, level), if_user_get_is_banned_else='是 ❌' if user.get('is_banned') else '否 ✅', if_user_get_can_upload_else='是 ✅' if user.get('can_upload') else '否 ❌', quota_display_user_get_daily_decode_quota=_quota_display(user.get('daily_decode_quota')), user_get_quota_used_today_0=user.get('quota_used_today', 0), quota_display_user_get_external_decode_quota=_quota_display(user.get('external_decode_quota')), user_get_external_used_today_0=user.get('external_used_today', 0), format_datetime_user_get_created_at=format_datetime(user.get('created_at')), format_datetime_user_get_updated_at=format_datetime(user.get('updated_at')))
        )

    elif state == "set_level:user_id":
        try:
            int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s24')))
            return
        await _ask("set_level:level",
                    _i18n_t('bot.admin_bot.conversation.s21', text=text),
                    {"user_id": int(text)})

    elif state == "set_level:level":
        level = LEVEL_ALIAS.get(text.strip())
        if not level:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s25')))
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
        await _end(_i18n_t('bot.admin_bot.conversation.s23', user_id=user_id, MEMBERSHIP_LEVELS_level=MEMBERSHIP_LEVELS[level]))

    elif state == "ban:user_id":
        try:
            user_id = int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s28')))
            return
        users_col = get_users_col()
        await _ensure_user(user_id)
        await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": True, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}})
        await update_user_and_invalidate(user_id)
        await _end(_i18n_t('bot.admin_bot.conversation.s26', user_id=user_id))

    elif state == "unban:user_id":
        try:
            user_id = int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s30')))
            return
        users_col = get_users_col()
        await _ensure_user(user_id)
        await users_col.update_one({"user_id": user_id}, {"$set": {"is_banned": False, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}})
        await update_user_and_invalidate(user_id)
        await _end(_i18n_t('bot.admin_bot.conversation.s27', user_id=user_id))

    elif state == "set_quota:user_id":
        try:
            int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s32')))
            return
        await _ask("set_quota:quota",
                    _i18n_t('bot.admin_bot.conversation.s29', text=text),
                    {"user_id": int(text)})

    elif state == "set_quota:quota":
        try:
            quota = int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s34')))
            return
        users_col = get_users_col()
        await _ensure_user(data["user_id"])
        await users_col.update_one({"user_id": data["user_id"]}, {"$set": {"daily_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}})
        await update_user_and_invalidate(data["user_id"])
        from database.cache_store import invalidate_user_quota_cache
        await invalidate_user_quota_cache(data["user_id"])
        await _end(_i18n_t('bot.admin_bot.conversation.s31', data_user_id=data['user_id'], quota_display_quota=_quota_display(quota)))

    elif state == "set_external_quota:user_id":
        try:
            int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s37')))
            return
        await _ask("set_external_quota:quota",
                    _i18n_t('bot.admin_bot.conversation.s33', text=text),
                    {"user_id": int(text)})

    elif state == "set_external_quota:quota":
        try:
            quota = int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s38')))
            return
        users_col = get_users_col()
        await _ensure_user(data["user_id"])
        await users_col.update_one({"user_id": data["user_id"]}, {"$set": {"external_decode_quota": quota, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}})
        await update_user_and_invalidate(data["user_id"])
        from database.cache_store import invalidate_user_quota_cache
        await invalidate_user_quota_cache(data["user_id"])
        await _end(_i18n_t('bot.admin_bot.conversation.s35', data_user_id=data['user_id'], quota_display_quota=_quota_display(quota)))

    # ─── 文件管理 ────────────────────────────────────────────────
    elif state == "file_detail:code":
        file_code = text.strip()
        files_col = get_file_records_col()
        record = await files_col.find_one({"file_code": file_code})
        if record is None:
            await _end(_i18n_t('bot.admin_bot.conversation.s39', file_code=file_code))
            return
        file_types = record.get("file_types", {})
        if isinstance(file_types, str):
            import json
            file_types = json.loads(file_types) if file_types else {}
        type_desc = " ".join(_i18n_t('bot.admin_bot.conversation.s40', v=v, k=k) for k, v in sorted(file_types.items())) if file_types else _i18n_t('bot.admin_bot.conversation.s36')
        backups = record.get("backup_channel_msg_ids", [])
        if isinstance(backups, str):
            import json
            backups = json.loads(backups) if backups else []
        await _end(
            _i18n_t('bot.admin_bot.conversation.s41', file_code=file_code, record_get_uploader_id=record.get('uploader_id'), type_desc=type_desc, record_get_status_active=record.get('status', 'active'), record_get_request_count_0=record.get('request_count', 0), format_datetime_record_get_create_time=format_datetime(record.get('create_time')), record_get_primary_channel_id=record.get('primary_channel_id'), len_backups=len(backups))
            + (_i18n_t('bot.admin_bot.conversation.s42', record_get_note=record.get('note', '')) if record.get("note", "") else "")
        )

    elif state == "delete_file:code":
        file_code = text.strip()
        files_col = get_file_records_col()
        result = await files_col.update_one({"file_code": file_code}, {"$set": {"status": "deleted"}})
        if result.matched_count == 0:
            await _end(_i18n_t('bot.admin_bot.conversation.s43', file_code=file_code))
        else:
            await _end(_i18n_t('bot.admin_bot.conversation.s44', file_code=file_code))

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
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
        if not phone:
            await _end(_i18n_t('bot.admin_bot.conversation.s46'))
            return
        await set_config(f"relay_auth_code:{phone}", code)
        await set_config(f"relay_auth_pending:{phone}", "1")
        await _end(_i18n_t('bot.admin_bot.conversation.s45', code=code))

    elif state == "relay_password:password":
        password = text.strip()
        if not password:
            await _end(_i18n_t('bot.admin_bot.conversation.s48'))
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
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
        if not phone:
            await _end(_i18n_t('bot.admin_bot.conversation.s49'))
            return
        await set_config(f"relay_auth_password:{phone}", password)
        await _end(_i18n_t('bot.admin_bot.conversation.s47'))

    elif state == "relay_add:phone":
        from services.relay_pool import _normalize_phone
        from database.relay_db import get_relay_db
        from loguru import logger
        from telethon import TelegramClient

        phone = _normalize_phone(text.strip())
        api_id = settings.RELAY_API_ID
        api_hash = settings.RELAY_API_HASH
        if not api_id or not api_hash:
            await _end(
                _i18n_t('bot.admin_bot.conversation.s51')
            )
            return
        try:
            api_id_int = int(api_id)
        except (TypeError, ValueError):
            await _end(_i18n_t('bot.admin_bot.conversation.s55', api_id=api_id))
            return

        # 检查重复
        db = await get_relay_db()
        existing = await db.get_active_accounts()
        if any(a["phone"] == phone for a in existing):
            await _end(_i18n_t('bot.admin_bot.conversation.s52', phone=phone))
            return

        # H-2: 使用临时 session 路径,避免与 idx_bot 运行中的实例争用 session 文件
        # 登录成功后,断开连接,将临时 session 文件重命名为正式路径
        project_root = Path(__file__).resolve().parent.parent.parent
        session_path = str(project_root / "data" / f"relay_session_{phone}")
        auth_session_path = str(project_root / "data" / f"relay_session_{phone}_auth")
        # 清理可能残留的临时 session
        try:
            p = Path(auth_session_path)
            if p.exists():
                p.unlink()
        except Exception:
            logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
        client = TelegramClient(auth_session_path, api_id_int, api_hash)
        # H-2: 保存临时 session 路径到 user_data,供 _conv_end 清理
        context.user_data["_relay_auth_session_path"] = auth_session_path

        try:
            await client.connect()
        except Exception as e:
            logger.error(f"[Admin] relay_add connect 失败: {e}")
            try:
                await client.disconnect()
            except Exception:
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
            await _end(
                _i18n_t('bot.admin_bot.conversation.s56', e=e)
            )
            return

        # 如果 session 已授权(之前登录过),直接写入 DB
        if await client.is_user_authorized():
            try:
                me = await client.get_me()
            except Exception as e:
                logger.error(f"[Admin] relay_add get_me 失败: {e}")
                me = None
            await client.disconnect()
            # H-2: 清理临时 session 路径标记 + 重命名为正式路径
            context.user_data.pop("_relay_auth_session_path", None)
            _rename_auth_session(auth_session_path, session_path)
            try:
                account_id = await db.add_account(api_id_int, api_hash, phone)
            except RuntimeError as e:
                logger.error(f"[Admin] relay_add add_account(session) 失败: {e}")
                await _end(f"❌ 写入数据库失败: {e}")
                return
            logger.info(f"[Admin] relay_add: session已授权,直接写入DB (account_id={account_id})")
            try:
                from database.cache_store import get_cache_store
                await get_cache_store().notify_relay_change()
            except Exception:
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
            # 自动将该账号 user_id 加入中继白名单,免手动配置
            if me:
                try:
                    await db.update_relay_user_id(phone, me.id)
                    added = await add_relay_whitelist(me.id)
                    if added:
                        logger.info(f"[Admin] relay_add: 已自动加入中继白名单 (user_id={me.id})")
                except Exception as e:
                    logger.warning(f"[Admin] relay_add: 自动加白名单失败: {e}")
            masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
            user_info = f"\n  用户: {me.first_name} (@{me.username})" if me else ""
            await _end(_i18n_t('bot.admin_bot.conversation.s53', masked=masked, user_info=user_info))
            return

        # 发送验证码
        try:
            sent = await client.send_code_request(phone)
            phone_code_hash = sent.phone_code_hash
        except Exception as e:
            logger.error(f"[Admin] relay_add send_code_request 失败: {e}")
            try:
                await client.disconnect()
            except Exception:
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
            await _end(_i18n_t('bot.admin_bot.conversation.s57', e=e))
            return

        # 保存客户端引用和登录参数到 user_data
        context.user_data["_relay_temp_client"] = client
        masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
        await _ask(
            "relay_add:code",
            _i18n_t('bot.admin_bot.conversation.s50', masked=masked),
            {"phone": phone, "phone_code_hash": phone_code_hash,
             "api_id": api_id_int, "api_hash": api_hash,
             "auth_session_path": auth_session_path,
             "session_path": session_path}
        )

    elif state == "relay_add:code":
        from telethon.errors import (
            SessionPasswordNeededError, PhoneCodeExpiredError,
            PhoneCodeInvalidError, PhoneNumberBannedError,
            AuthRestartError,
        )
        from database.relay_db import get_relay_db
        from loguru import logger

        code = text.strip()
        if not code.isdigit() or len(code) not in (5, 6):
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s58')))
            return

        client = context.user_data.get("_relay_temp_client")
        if not client or not client.is_connected():
            await _end(_i18n_t('bot.admin_bot.conversation.s59'))
            return

        phone = data["phone"]
        phone_code_hash = data["phone_code_hash"]

        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            # 需要二步验证密码
            await _ask(
                "relay_add:password",
                _i18n_t('bot.admin_bot.conversation.s61')
            )
            return
        except PhoneCodeExpiredError:
            # 验证码过期,清理并结束
            try:
                await client.disconnect()
            except Exception:
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
            context.user_data.pop("_relay_temp_client", None)
            await _end(_i18n_t('bot.admin_bot.conversation.s62'))
            return
        except PhoneCodeInvalidError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s63')))
            return
        except PhoneNumberBannedError:
            try:
                await client.disconnect()
            except Exception:
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
            context.user_data.pop("_relay_temp_client", None)
            await _end(_i18n_t('bot.admin_bot.conversation.s64'))
            return
        except AuthRestartError:
            # Telegram 要求重新开始认证流程,需重新发送验证码
            try:
                await client.disconnect()
            except Exception:
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
            context.user_data.pop("_relay_temp_client", None)
            await _end(_i18n_t('bot.admin_bot.conversation.s65'))
            return
        except Exception as e:
            logger.error(f"[Admin] relay_add sign_in 失败: {e}")
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s137', error=e)))
            return

        # 登录成功,写入 DB
        me = await client.get_me()
        await client.disconnect()
        context.user_data.pop("_relay_temp_client", None)
        # H-2: 清理临时 session 路径标记(即将重命名,无需再被 _conv_end 清理)
        context.user_data.pop("_relay_auth_session_path", None)
        # H-2: 将临时 session 重命名为正式路径
        _rename_auth_session(data.get("auth_session_path", ""), data.get("session_path", ""))

        db = await get_relay_db()
        try:
            account_id = await db.add_account(data["api_id"], data["api_hash"], phone)
        except RuntimeError as e:
            # H-3: UNIQUE 冲突或其他 DB 错误
            logger.error(f"[Admin] relay_add add_account 失败: {e}")
            await _end(f"❌ 写入数据库失败: {e}")
            return
        logger.info(f"[Admin] relay_add: 登录成功,写入DB (account_id={account_id})")
        try:
            from database.cache_store import get_cache_store
            await get_cache_store().notify_relay_change()
        except Exception:
            logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
        # 自动将该账号 user_id 加入中继白名单,免手动配置
        try:
            await db.update_relay_user_id(phone, me.id)
            added = await add_relay_whitelist(me.id)
            if added:
                logger.info(f"[Admin] relay_add: 已自动加入中继白名单 (user_id={me.id})")
        except Exception as e:
            logger.warning(f"[Admin] relay_add: 自动加白名单失败: {e}")

        masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
        await _end(
            _i18n_t('bot.admin_bot.conversation.s54', masked=masked, me_first_name=me.first_name, me_username=me.username)
        )

    elif state == "relay_add:password":
        from database.relay_db import get_relay_db
        from loguru import logger

        password = text.strip()
        if not password:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s66')))
            return

        client = context.user_data.get("_relay_temp_client")
        if not client or not client.is_connected():
            await _end(_i18n_t('bot.admin_bot.conversation.s67'))
            return

        phone = data["phone"]

        try:
            await client.sign_in(password=password)
        except Exception as e:
            logger.error(f"[Admin] relay_add password sign_in 失败: {e}")
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s138', error=e)))
            return

        # 登录成功,写入 DB
        me = await client.get_me()
        await client.disconnect()
        context.user_data.pop("_relay_temp_client", None)
        # H-2: 清理临时 session 路径标记 + 重命名为正式路径
        context.user_data.pop("_relay_auth_session_path", None)
        _rename_auth_session(data.get("auth_session_path", ""), data.get("session_path", ""))

        db = await get_relay_db()
        try:
            account_id = await db.add_account(data["api_id"], data["api_hash"], phone)
        except RuntimeError as e:
            logger.error(f"[Admin] relay_add add_account(2FA) 失败: {e}")
            await _end(f"❌ 写入数据库失败: {e}")
            return
        logger.info(f"[Admin] relay_add: 二步验证成功,写入DB (account_id={account_id})")
        try:
            from database.cache_store import get_cache_store
            await get_cache_store().notify_relay_change()
        except Exception:
            logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
        # 自动将该账号 user_id 加入中继白名单,免手动配置
        try:
            await db.update_relay_user_id(phone, me.id)
            added = await add_relay_whitelist(me.id)
            if added:
                logger.info(f"[Admin] relay_add: 已自动加入中继白名单 (user_id={me.id})")
        except Exception as e:
            logger.warning(f"[Admin] relay_add: 自动加白名单失败: {e}")

        masked = phone[:3] + "****" + phone[-2:] if len(phone) > 5 else "***"
        await _end(
            _i18n_t('bot.admin_bot.conversation.s60', masked=masked, me_first_name=me.first_name, me_username=me.username)
        )

    elif state == "relay_remove:phone":
        from services.relay_pool import relay_pool, _normalize_phone
        from database.relay_db import get_relay_db
        from database import remove_relay_whitelist
        phone = _normalize_phone(text.strip())
        # 移除前先查询 relay_user_id,用于清理白名单(remove_account 会删除 DB 记录)
        db = await get_relay_db()
        relay_user_id = await db.get_relay_user_id(phone)
        removed = await relay_pool.remove_account(phone)
        if removed:
            try:
                from database.cache_store import get_cache_store
                await get_cache_store().notify_relay_change()
            except Exception:
                logger.exception(_i18n_t('diagnostics.r65.p1_04.swallowed_exception', file_func='bots/admin_bot/conversation.py:handle_conversation'))
            # 自动从白名单移除该账号的 user_id
            if relay_user_id:
                try:
                    await remove_relay_whitelist(relay_user_id)
                    logger.info(f"[Admin] relay_remove: 已自动移除白名单 (user_id={relay_user_id})")
                except Exception as e:
                    logger.warning(f"[Admin] relay_remove: 自动移除白名单失败: {e}")
            await _end(f"✅ 已移除中继账号: {phone[:3]}****{phone[-2:] if len(phone) > 5 else '***'}")
        else:
            await _end(_i18n_t('bot.admin_bot.conversation.s68', phone=phone))

    # ─── 白名单管理 ──────────────────────────────────────────────
    elif state == "collector_wl_add:user_id":
        from database import add_collector_whitelist
        try:
            user_id = int(text.strip())
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s71')))
            return
        added = await add_collector_whitelist(user_id)
        if added:
            await _end(_i18n_t('bot.admin_bot.conversation.s69', user_id=user_id))
        else:
            await _end(_i18n_t('bot.admin_bot.conversation.s70', user_id=user_id))

    elif state == "collector_wl_remove:user_id":
        from database import remove_collector_whitelist
        try:
            user_id = int(text.strip())
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s77')))
            return
        removed = await remove_collector_whitelist(user_id)
        if removed:
            await _end(_i18n_t('bot.admin_bot.conversation.s72', user_id=user_id))
        else:
            await _end(_i18n_t('bot.admin_bot.conversation.s73', user_id=user_id))

    elif state == "set_access_limit:code":
        file_code = text.strip()
        await _ask("set_access_limit:max",
                   _i18n_t('bot.admin_bot.conversation.s74', file_code=file_code),
                   {"file_code": file_code})

    elif state == "set_access_limit:max":
        try:
            max_requests = int(text)
            if max_requests < 0:
                raise ValueError
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s80')))
            return
        from database import update_file_record_and_invalidate
        try:
            await update_file_record_and_invalidate(
                data["file_code"], {"$set": {"max_requests": max_requests}}
            )
        except Exception as e:
            await _end(_i18n_t('bot.admin_bot.conversation.s81', e=e))
            return
        limit_text = _i18n_t('bot.admin_bot.conversation.s75', max_requests=max_requests) if max_requests > 0 else _i18n_t('bot.admin_bot.conversation.s76')
        await _end(_i18n_t('bot.admin_bot.conversation.s78', data_file_code=data['file_code'], limit_text=limit_text))

    elif state == "cell_add:slot_id":
        slot_id = text.strip()
        await _ask("cell_add:channel_id",
                   _i18n_t('bot.admin_bot.conversation.s79', slot_id=slot_id),
                   {"slot_id": slot_id})

    elif state == "cell_add:channel_id":
        try:
            channel_id = int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s84')))
            return
        await _ask("cell_add:account_name",
                   _i18n_t('bot.admin_bot.conversation.s82', channel_id=channel_id),
                   {"slot_id": data["slot_id"], "channel_id": channel_id})

    elif state == "cell_add:account_name":
        account_name = text.strip()
        if account_name == "0":
            account_name = ""
        await _ask("cell_add:status",
                   _i18n_t('bot.admin_bot.conversation.s83', account_name_or=account_name or '(无)'),
                   {"slot_id": data["slot_id"], "channel_id": data["channel_id"], "account_name": account_name})

    elif state == "cell_add:status":
        status = text.strip()
        if status == "0":
            status = "shadow1"
        if status not in ("active", "shadow1", "shadow2", "r100"):
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s85')))
            return
        from database.cache_store import get_cache_store
        store = get_cache_store()
        existing = await store.get_all_cells_local()
        if any(c.get("slot_id") == data["slot_id"] for c in existing):
            await _end(_i18n_t('bot.admin_bot.conversation.s86', data_slot_id=data['slot_id']))
            return
        if any(c.get("channel_id") == data["channel_id"] for c in existing):
            await _end(_i18n_t('bot.admin_bot.conversation.s87', data_channel_id=data['channel_id']))
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
                _i18n_t('bot.admin_bot.conversation.s88', data_slot_id=data['slot_id'], data_channel_id=data['channel_id'], data_account_name_or=data['account_name'] or '(无)', status=status)
            )
        except Exception as e:
            await _end(_i18n_t('bot.admin_bot.conversation.s89', e=e))

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
            await _end(_i18n_t('bot.admin_bot.conversation.s90', slot_id=slot_id))
            return
        if target.get("status") == "active":
            await _end(_i18n_t('bot.admin_bot.conversation.s91', slot_id=slot_id))
            return
        try:
            deleted = await store.delete_cell_local(slot_id)
            if deleted:
                from bots.admin_bot.display import invalidate_cells_cache
                await invalidate_cells_cache()
                await _end(_i18n_t('bot.admin_bot.conversation.s93', slot_id=slot_id))
            else:
                await _end(_i18n_t('bot.admin_bot.conversation.s94', slot_id=slot_id))
        except Exception as e:
            await _end(_i18n_t('bot.admin_bot.conversation.s95', e=e))

    # ─── 系统配置 ────────────────────────────────────────────────
    elif state == "set_file_prefix:prefix":
        prefix = text.strip()
        await set_config("file_code_prefix", prefix)
        await _end(_i18n_t('bot.admin_bot.conversation.s92', prefix=prefix))

    elif state == "set_force_join:channel_id":
        try:
            int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s97')))
            return
        await _ask("set_force_join:link",
                    _i18n_t('bot.admin_bot.conversation.s96', text=text),
                    {"channel_id": int(text)})

    elif state == "set_force_join:link":
        link = text.strip()
        if link == "0":
            link = ""
        await set_config("force_join_channel_id", str(data["channel_id"]))
        if link:
            await set_config("force_join_link", link)
        await _end(_i18n_t('bot.admin_bot.conversation.s100', data_channel_id=data['channel_id']) + (_i18n_t('bot.admin_bot.conversation.s103', link=link) if link else "") + _i18n_t('bot.admin_bot.conversation.s98'))

    elif state == "set_username:role":
        role = text.lower()
        if role not in ("upload", "decoder", "sender"):
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s101')))
            return
        await _ask("set_username:name",
                    _i18n_t('bot.admin_bot.conversation.s99', role=role),
                    {"role": role})

    elif state == "set_username:name":
        key_map = {"upload": "upload_bot_username", "decoder": "decoder_bot_username", "sender": "sender_bot_username"}
        key = key_map.get(data["role"])
        username = text.lstrip("@")
        await set_config(key, username)
        await _end(_i18n_t('bot.admin_bot.conversation.s102', data_role=data['role'], username=username))

    elif state == "set_quota_default:level":
        level = LEVEL_ALIAS.get(text.strip())
        if not level:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s107')))
            return
        await _ask("set_quota_default:quota",
                    _i18n_t('bot.admin_bot.conversation.s104', text=text),
                    {"level": level})

    elif state == "set_quota_default:quota":
        try:
            quota = int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s110')))
            return
        await set_config(f"quota_default_{data['level']}", str(quota))
        await _ask("set_quota_default:ext_quota",
                    _i18n_t('bot.admin_bot.conversation.s108', quota_display_quota=_quota_display(quota)),
                    {"quota": quota})

    elif state == "set_quota_default:ext_quota":
        try:
            ext_quota = int(text)
        except ValueError:
            ext_quota = 0
        if ext_quota > 0:
            await set_config(f"quota_external_{data['level']}", str(ext_quota))
        msg = _i18n_t('bot.admin_bot.conversation.s105', data_level=data['level'], quota_display_data_quota=_quota_display(data['quota']))
        if ext_quota > 0:
            msg += _i18n_t('bot.admin_bot.conversation.s109', quota_display_ext_quota=_quota_display(ext_quota))
        msg += _i18n_t('bot.admin_bot.conversation.s106')
        await _end(msg)

    elif state == "set_r2:account_id":
        await _ask("set_r2:access_key",
                    _i18n_t('bot.admin_bot.conversation.s111', text=text),
                    {"account_id": text.strip()})

    elif state == "set_r2:access_key":
        await _ask("set_r2:secret_key",
                    _i18n_t('bot.admin_bot.conversation.s112', text_8=text[:8]),
                    {"access_key": text.strip()})

    elif state == "set_r2:secret_key":
        await _ask("set_r2:bucket",
                    _i18n_t('bot.admin_bot.conversation.s113', text_8=text[:8]),
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
        await _end(_i18n_t('bot.admin_bot.conversation.s114', bucket_or=bucket or '(默认)'))

    elif state == "set_db_backup:interval":
        try:
            interval = int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s117')))
            return
        await _ask("set_db_backup:enabled",
                    _i18n_t('bot.admin_bot.conversation.s115', interval=interval),
                    {"interval": interval})

    elif state == "set_db_backup:enabled":
        on_off = text.strip().lower()
        if on_off not in ("on", "off"):
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s118')))
            return
        await set_config("db_backup_interval", str(data["interval"]))
        await set_config("db_backup_enabled", "true" if on_off == "on" else "false")
        await _end(_i18n_t('bot.admin_bot.conversation.s116', if_on_off_on_else='开启' if on_off == 'on' else '关闭', data_interval=data['interval']))

    # ─── 备用池 ────────────────────────────────────────────────
    elif state == "spare_add:channel_id":
        try:
            channel_id = int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s123')))
            return
        await _ask("spare_add:account_name",
                    _i18n_t('bot.admin_bot.conversation.s119', channel_id=channel_id),
                    {"channel_id": channel_id})

    elif state == "spare_add:account_name":
        account_name = text.strip()
        if account_name == "0":
            account_name = None
        await add_spare_channel(data["channel_id"], account_name)
        acc_info = _i18n_t('bot.admin_bot.conversation.s120', account_name=account_name) if account_name else _i18n_t('bot.admin_bot.conversation.s121')
        await _end(_i18n_t('bot.admin_bot.conversation.s122', data_channel_id=data['channel_id'], acc_info=acc_info))

    elif state == "spare_remove:channel_id":
        try:
            channel_id = int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s132')))
            return
        await remove_spare(channel_id)
        await _end(_i18n_t('bot.admin_bot.conversation.s124', channel_id=channel_id))

    # ─── 轮转配置 ──────────────────────────────────────────────
    elif state == "rotation_set:key":
        key = text.strip().lower()
        valid_keys = {"active_window_size", "files_per_slot", "time_per_slot"}
        if key not in valid_keys:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s133', key=key, join_sorted_valid_keys=', '.join(sorted(valid_keys)))))
            return
        labels = {
            "active_window_size": _i18n_t('bot.admin_bot.conversation.s125'),
            "files_per_slot": _i18n_t('bot.admin_bot.conversation.s126'),
            "time_per_slot": _i18n_t('bot.admin_bot.conversation.s127'),
        }
        await _ask("rotation_set:value",
                    _i18n_t('bot.admin_bot.conversation.s128', key=key, labels_key=labels[key]),
                    {"rotation_key": key})

    elif state == "rotation_set:value":
        try:
            int(text)
        except ValueError:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s136')))
            return
        db_key = f"rotation_{data['rotation_key']}"
        await set_rotation_config(db_key, text)
        label_map = {
            "active_window_size": _i18n_t('bot.admin_bot.conversation.s129'),
            "files_per_slot": _i18n_t('bot.admin_bot.conversation.s130'),
            "time_per_slot": _i18n_t('bot.admin_bot.conversation.s131'),
        }
        await _end(
            _i18n_t('bot.admin_bot.conversation.s134', label_map_get_data_rotation_key_data_rotation_key=label_map.get(data['rotation_key'], data['rotation_key']), text=text)
        )

    # 未知状态 → 清理
    else:
        await _conv_end(context)
        await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.conversation.s135')))