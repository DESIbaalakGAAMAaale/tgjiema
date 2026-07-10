"""在本地电脑(住宅 IP)运行,导出 session 字符串。

Telegram 对数据中心 IP 限制 send_code_request,
所以先在本地完成登录,导出 session 后传到 VPS 使用。

运行方式:
    python scripts/export_session.py

输出:session_string.txt(复制到 VPS)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError


async def export_session():
    api_id = os.environ.get("RELAY_API_ID", "")
    api_hash = os.environ.get("RELAY_API_HASH", "")

    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        print("❌ api_id 必须是数字")
        return

    print("=" * 50)
    print("Telegram Session 导出工具")
    print("=" * 50)
    print(f"api_id:  {api_id}")
    print(f"api_hash: {api_hash[:10]}...")
    print()
    print("如果是 Windows 且没有 python-dotenv,请先设置环境变量:")
    print("  $env:RELAY_API_ID='你的api_id'")
    print("  $env:RELAY_API_HASH='你的api_hash'")
    print("=" * 50)

    client = StringSession()

    print("\n启动客户端,使用 StringSession(内存模式)...")
    await client.start(
        api_id=api_id,
        api_hash=api_hash,
    )

    session_str = client.session.save()
    print(f"\n✅ Session 导出成功!")
    print(f"   长度: {len(session_str)} 字符")

    with open("session_string.txt", "w") as f:
        f.write(session_str)
    print("   已保存到: session_string.txt")

    me = await client.get_me()
    print(f"\n账号信息: {me.first_name} (@{me.username}), id={me.id}")
    print(f"手机号: {me.phone}")

    await client.disconnect()
    print("\n接下来:")
    print("1. 把 session_string.txt 复制到 VPS(例如用 scp)")
    print("2. 在 VPS 上运行: python scripts/import_session.py")


if __name__ == "__main__":
    asyncio.run(export_session())
