"""时间格式化工具函数"""

import datetime


def format_datetime(dt) -> str:
    if dt is None:
        return "N/A"
    if isinstance(dt, (datetime.datetime, float)):
        if isinstance(dt, float):
            if dt == 0:
                return "N/A"
            dt = datetime.datetime.fromtimestamp(dt, tz=datetime.UTC)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(dt, str):
        try:
            return datetime.datetime.fromisoformat(dt).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return dt
    return str(dt)