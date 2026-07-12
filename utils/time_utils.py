"""时间格式化工具函数"""

import datetime


# 系统默认时区(UTC),用于格式化输出时标注时区
_UTC = datetime.timezone.utc


def format_datetime(dt, tz: datetime.timezone = None) -> str:
    """格式化时间为字符串。

    P3: 改进时区处理:
    - float 时间戳转为 UTC 后,格式化时标注 UTC 标识
    - ISO 字符串解析后保留时区信息
    - 可选 tz 参数将时间转换到指定时区后格式化

    Args:
        dt: datetime 对象、float 时间戳或 ISO 字符串
        tz: 可选目标时区(如 Asia/Shanghai),None 表示保留原时区
    """
    if dt is None:
        return "N/A"
    if isinstance(dt, float):
        if dt == 0:
            return "N/A"
        # float 时间戳按 UTC 解析
        dt = datetime.datetime.fromtimestamp(dt, tz=_UTC)
    elif isinstance(dt, int):
        if dt == 0:
            return "N/A"
        dt = datetime.datetime.fromtimestamp(dt, tz=_UTC)
    elif isinstance(dt, str):
        try:
            parsed = datetime.datetime.fromisoformat(dt)
            # 无时区信息的 ISO 字符串默认按 UTC 处理
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=_UTC)
            dt = parsed
        except (ValueError, TypeError):
            return dt
    elif not isinstance(dt, datetime.datetime):
        return str(dt)

    # P3: 时区转换(如转 Asia/Shanghai 显示)
    if tz is not None and dt.tzinfo is not None:
        dt = dt.astimezone(tz)

    # P3: 格式化时标注时区(UTC 或偏移)
    if dt.tzinfo is not None:
        offset = dt.strftime("%z")
        # 将 +0800 格式转为 +08:00
        if offset and len(offset) >= 5:
            offset = offset[:3] + ":" + offset[3:]
        return dt.strftime("%Y-%m-%d %H:%M:%S") + f" {offset}" if offset else dt.strftime("%Y-%m-%d %H:%M:%S")
    # naive datetime(无时区),原样输出
    return dt.strftime("%Y-%m-%d %H:%M:%S")
