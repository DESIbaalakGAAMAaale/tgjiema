"""在 VPS 上运行,验证 session 字符串是否可用。

如果导入后 is_user_authorized() 返回 True,
说明 VPS 可以跳过 send_code_request(用住宅 IP 生成的 session),绕过 IP 限制。

运行方式:
    python scripts/import_session.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from telethon import TelegramClient
from telethon.sessions import StringSession


async def test_session():
    if not os.path.exists("session_string.txt"):
        print("❌ session_string.txt 不存在")
        print("   请先运行: python scripts/export_session.py")
        print("   然后把 session_string.txt 复制到 VPS")
        return

    with open("session_string.txt") as f:
        session_str = f.read().strip()

    api_id = os.environ.get("RELAY_API_ID", "")
    api_hash = os.environ.get("RELAY_API_HASH", "")
    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        print("❌ api_id 必须是数字")
        return

    print("=" * 50)
    print("测试 Session 导入")
    print("=" * 50)

    temp_path = "/tmp/test_import_session"
    for suffix in ("", "-journal"):
        p = temp_path + suffix
        if os.path.exists(p):
            os.remove(p)

    client = StringSession(session_str)

    try:
        print("\n[测试 1] 用导入的 session 启动...")
        await client.start(
            api_id=api_id,
            api_hash=api_hash,
        )
        print("✅ 启动成功!")

        print("\n[测试 2] is_user_authorized()...")
        authorized = await client.is_user_authorized()
        print(f"✅ 已授权: {authorized}")

        if authorized:
            me = await client.get_me()
            print(f"\n[测试 3] 账号信息:")
            print(f"   姓名: {me.first_name}")
            print(f"   用户名: @{me.username}")
            print(f"   id: {me.id}")
            print(f"   手机号: {me.phone}")

            print("\n" + "=" * 50)
            print("✅ Session 可用! VPS 无需 send_code_request")
            print("=" * 50)
            print("\n接下来需要修改 relay_instance.py:")
            print("  在 start() 中,如果 session 字符串已导入,")
            print("  直接用 StringSession 启动,跳过 send_code_request")
        else:
            print("❌ Session 未授权(本地登录失败?)")

    except Exception as e:
        print(f"\n❌ 测试失败: {type(e).__name__}: {e}")
    finally:
        await client.disconnect()
        for suffix in ("", "-journal"):
            p = temp_path + suffix
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    asyncio.run(test_session())
