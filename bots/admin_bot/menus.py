import functools
from services.sink_adapters.telegram_adapter import (
    safe_reply_text, safe_send_message, safe_edit_message_text,
)
from services.sink_adapters.telegram_helpers import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    ContextTypes,
)
# R65 P1-01: typed adapter 要求 UserMessage | ErrorEnvelope
from services.user_message import UserMessage




from config import settings
from services.i18n import translate as _i18n_t

TOKEN = settings.ADMIN_BOT_TOKEN
# B7: 确保 AUTHORIZED_USER_ID 为 int，防止 env 中 ADMIN_TELEGRAM_ID 为字符串时
# 与 from_user.id(int) 恒不等导致装饰器失效（int != str 永远为 True → 恒拒绝合法管理员）
AUTHORIZED_USER_ID = int(settings.ADMIN_TELEGRAM_ID) if settings.ADMIN_TELEGRAM_ID else 0

MEMBERSHIP_LEVELS = {"free": _i18n_t('bot.admin_bot.menus.s1'), "basic": _i18n_t('bot.admin_bot.menus.s2'), "premium": _i18n_t('bot.admin_bot.menus.s3')}
LEVEL_ALIAS = {
    "1": "free", "2": "basic", "3": "premium",
    _i18n_t('bot.admin_bot.menus.s4'): "free", _i18n_t('bot.admin_bot.menus.s5'): "basic", _i18n_t('bot.admin_bot.menus.s6'): "premium",
}


def _auth_required(func):
    # R43: 加 functools.wraps 保留原函数元数据(__wrapped__/__name__/__doc__),
    # 让外层装饰器(maintenance_middleware)的内省检测能识别到内层装饰器标记。
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or user.id != AUTHORIZED_USER_ID:
            await safe_reply_text(update.message, UserMessage.from_raw_text(_i18n_t('bot.admin_bot.menus.s22')))
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


def _quota_display(val: int) -> str:
    if val == -1:
        return _i18n_t('bot.admin_bot.menus.s7')
    return str(val)


BACK_BTN = [[InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s20'), callback_data="menu:main")]]


def _build_menu(menu_id: str) -> tuple[str, InlineKeyboardMarkup]:
    if menu_id == "main":
        text = _i18n_t('bot.admin_bot.menus.s8')
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s23'), callback_data="menu:sys"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s24'), callback_data="menu:user")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s25'), callback_data="menu:file"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s26'), callback_data="action:logs")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s27'), callback_data="menu:relay"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s28'), callback_data="menu:config")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s29'), callback_data="menu:code_route"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s30'), callback_data="menu:bot_limit")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s31'), callback_data="menu:topology"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s32'), callback_data="menu:spare")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s33'), callback_data="menu:rotation")],
        ]
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "sys":
        text = _i18n_t('bot.admin_bot.menus.s9')
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s34'), callback_data="action:status"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s35'), callback_data="action:health")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "user":
        text = _i18n_t('bot.admin_bot.menus.s10')
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s36'), callback_data="action:users"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s37'), callback_data="interactive:user_detail")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s38'), callback_data="interactive:set_level"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s39'), callback_data="interactive:ban")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s40'), callback_data="interactive:unban"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s41'), callback_data="interactive:set_quota")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s42'), callback_data="interactive:set_external_quota")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "file":
        text = _i18n_t('bot.admin_bot.menus.s11')
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s43'), callback_data="action:files"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s44'), callback_data="interactive:file_detail")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s45'), callback_data="interactive:delete_file"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s46'), callback_data="interactive:set_access_limit")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "relay":
        text = _i18n_t('bot.admin_bot.menus.s12')
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s47'), callback_data="action:relay_status"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s48'), callback_data="interactive:relay_add")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s49'), callback_data="interactive:relay_remove"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s50'), callback_data="action:relay_pending")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s51'), callback_data="menu:whitelist")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "whitelist":
        text = _i18n_t('bot.admin_bot.menus.s13')
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s52'), callback_data="action:relay_whitelist"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s53'), callback_data="action:collector_whitelist")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s54'), callback_data="interactive:collector_wl_add"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s55'), callback_data="interactive:collector_wl_remove")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "config":
        text = _i18n_t('bot.admin_bot.menus.s14')
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s56'), callback_data="action:settings")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s57'), callback_data="interactive:set_file_prefix"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s58'), callback_data="interactive:set_force_join")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s59'), callback_data="interactive:set_username"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s60'), callback_data="interactive:set_quota_default")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s61'), callback_data="interactive:set_r2"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s62'), callback_data="interactive:set_db_backup")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "code_route":
        text = (
            _i18n_t('bot.admin_bot.menus.s15')
        )
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s63'), callback_data="interactive:add_code_route"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s64'), callback_data="interactive:remove_code_route")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s65'), callback_data="interactive:add_code_route_regex"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s66'), callback_data="interactive:remove_code_route_regex")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s67'), callback_data="action:code_routes")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "bot_limit":
        text = (
            _i18n_t('bot.admin_bot.menus.s16')
        )
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s68'), callback_data="interactive:set_bot_interval"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s69'), callback_data="interactive:remove_bot_interval")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s70'), callback_data="action:bot_intervals")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "topology":
        text = _i18n_t('bot.admin_bot.menus.s17')
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s71'), callback_data="action:topology")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s72'), callback_data="interactive:cell_add"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s73'), callback_data="interactive:cell_remove")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "spare":
        text = _i18n_t('bot.admin_bot.menus.s18')
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s74'), callback_data="interactive:spare_add"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s75'), callback_data="interactive:spare_remove")],
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s76'), callback_data="action:spare_list")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    if menu_id == "rotation":
        text = _i18n_t('bot.admin_bot.menus.s19')
        kb = [
            [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s77'), callback_data="action:rotation_view"),
             InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s78'), callback_data="interactive:rotation_set")],
        ] + BACK_BTN
        return text, InlineKeyboardMarkup(kb)

    # 未知 menu_id 兜底返回主菜单
    return _build_menu("main")


_CONV_CANCEL_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton(_i18n_t('bot.admin_bot.menus.s21'), callback_data="conv:cancel")],
])