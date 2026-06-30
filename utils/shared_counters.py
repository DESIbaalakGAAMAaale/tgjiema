"""共享计数器 — 避免跨模块循环导入。
用于 admin_bot 状态面板的实时计数，由各 Bot 进程写入。
"""

# 全局状态计数器，各模块均可安全读写
status_counters: dict = {
    "total_users": 0,
    "total_files": 0,
    "active_files": 0,
    "today_decodes": 0,
}

# 初始化标记，admin_bot 用于判断是否需要首次 DB 查询
status_counters_initialized: bool = False

# 计数器加载时间戳，用于 TTL 过期刷新（防止数据过时）
status_counters_loaded_at: float = 0


# ─── E2: 用户码本地计数器 ───────────────────────────────────
# 进程内增量计数，避免每次 /my_codes 查 CRDB count_documents
_user_code_count_delta: dict[int, int] = {}  # user_id -> delta
_user_code_count_cleanup_at: float = 0


def incr_user_code_count(user_id: int, delta: int = 1):
    """用户生成新码时 +1（F1: 同时更新 active_files）"""
    _user_code_count_delta[user_id] = _user_code_count_delta.get(user_id, 0) + delta


def decr_user_code_count(user_id: int, delta: int = 1):
    """用户删除/下架码时 -1（F1: active_files 准确性修复）"""
    _user_code_count_delta[user_id] = _user_code_count_delta.get(user_id, 0) - delta
    # F1: 同步递减 active_files
    status_counters["active_files"] = max(0, status_counters.get("active_files", 0) - delta)


def get_user_code_count(user_id: int, base: int = 0) -> int:
    """获取用户的码总数(本地计数 + 基线)"""
    global _user_code_count_cleanup_at
    delta = _user_code_count_delta.get(user_id, 0)
    # 每小时清理一次零值条目，防止内存泄漏
    import time
    now = time.monotonic()
    if now - _user_code_count_cleanup_at > 3600:
        stale = [uid for uid, d in _user_code_count_delta.items() if d == 0]
        for uid in stale:
            del _user_code_count_delta[uid]
        _user_code_count_cleanup_at = now
    return max(0, base + delta)


def incr_total_users():
    """F1: 新用户注册时递增"""
    status_counters["total_users"] = status_counters.get("total_users", 0) + 1