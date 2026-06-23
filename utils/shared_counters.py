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