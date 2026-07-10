"""Telegram API 连接测试脚本 — 在 VPS 上运行,诊断 api_id/api_hash 问题。

测试内容:
1. connect() — 基础连接
2. is_user_authorized() — 检查授权状态
3. send_code_request() — 请求发送验证码(核心失败点)

运行方式:
    python scripts/test_telegram_api.py
"""
import asyncio
import os
import sys

# 加载 .env
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

from telethon import TelegramClient
from telethon.errors import ApiIdInvalidError


async def test_api():
    api_id = os.environ.get("RELAY_API_ID", "")
    api_hash = os.environ.get("RELAY_API_HASH", "")

    print("=" * 50)
    print("Telegram API 连接测试")
    print("=" * 50)
    print(f"api_id:  {api_id} (type={type(api_id).__name__})")
    print(f"api_hash: {api_hash[:10]}... (len={len(api_hash)})")
    print(f"VPS IP:  {os.popen('curl -s ifconfig.me').read().strip()}")
    print("=" * 50)

    # 转换 api_id 为 int
    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        print("\n❌ api_id 必须是数字!")
        return False

    temp_session = "/tmp/test_telegram_api"
    # 清理旧的测试 session
    for suffix in ("", "-journal"):
        p = temp_session + suffix
        if os.path.exists(p):
            os.remove(p)

    client = None
    try:
        print("\n[测试 1] 创建 TelegramClient...")
        client = TelegramClient(temp_session, api_id, api_hash, timeout=30)
        print("✅ TelegramClient 创建成功")

        print("\n[测试 2] connect()...")
        await client.connect()
        print("✅ connect 成功!")

        print("\n[测试 3] is_user_authorized()...")
        authorized = await client.is_user_authorized()
        print(f"✅ 已授权: {authorized}")

        if not authorized:
            phone = input("\n[测试 4] 输入手机号测试 send_code_request (如 +86138xxxx): ").strip()
            if phone:
                print(f"\n[测试 4] send_code_request({phone})...")
                try:
                    result = await client.send_code_request(phone)
                    print(f"✅ send_code_request 成功!")
                    print(f"   phone_code_hash: {result.phone_code_hash[:20]}...")
                except ApiIdInvalidError as e:
                    print(f"\n❌ send_code_request 失败: ApiIdInvalidError")
                    print(f"   错误详情: {e}")
                    print("\n可能原因:")
                    print("  1. api_id/api_hash 确实无效(从 my.telegram.org 重新申请)")
                    print("  2. VPS IP 被 Telegram 风控(换 IP 或等待)")
                    print("  3. 该 api_id 已被其他项目大量使用(创建新 App)")
                    return False
                except Exception as e:
                    print(f"\n❌ send_code_request 失败: {type(e).__name__}: {e}")
                    return False

        print("\n" + "=" * 50)
        print("✅ 全部测试通过!")
        print("=" * 50)
        return True

    except ApiIdInvalidError as e:
        print(f"\n❌ connect 失败: ApiIdInvalidError")
        print(f"   错误详情: {e}")
        print("\n可能原因:")
        print("  1. api_id/api_hash 确实无效(从 my.telegram.org 重新申请)")
        print("  2. VPS IP 被 Telegram 风控(换 IP 或等待)")
        print("  3. 该 api_id 已被其他项目大量使用(创建新 App)")
        return False
    except Exception as e:
        print(f"\n❌ 测试失败: {type(e).__name__}: {e}")
        return False
    finally:
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        # 清理测试 session
        for suffix in ("", "-journal"):
            p = temp_session + suffix
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    asyncio.run(test_api())
