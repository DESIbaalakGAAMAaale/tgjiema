"""迁移脚本:将旧 Redis List 队列中的消息迁移到 Stream。

R34 P2 修复: 从旧 List (WRITER_QUEUE_KEY) 切换到 Stream 时,
旧 List 中可能残留未处理的消息。此脚本:
1. 读取旧 List 中的所有消息(LRANGE)
2. 对每条消息 XADD 到新 Stream(保留原 message_id 或生成新的)
3. 验证迁移数量
4. 删除旧 List key

使用方法:
    python scripts/migrate_list_to_stream.py [--dry-run]

注意: 迁移前应停止 db_writer 服务,避免并发消费。
"""
import argparse
import json
import os
import sys
import uuid

# 默认值(与 config/settings.py 中的默认值保持一致)
DEFAULT_LIST_KEY = "tgjiema:writer:queue"
DEFAULT_STREAM_KEY = "tgjiema:writer:stream"


def _load_settings():
    """从 config.settings 读取配置,失败时降级到环境变量/默认值。

    返回 (redis_url, list_key, stream_key)
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    redis_url = ""
    list_key = DEFAULT_LIST_KEY
    stream_key = DEFAULT_STREAM_KEY
    try:
        sys.path.insert(0, project_root)
        from config import settings
        redis_url = settings.REDIS_URL
        stream_key = settings.WRITER_STREAM_KEY
        list_key = getattr(settings, "WRITER_QUEUE_KEY", DEFAULT_LIST_KEY)
    except Exception as e:
        # 降级: 直接从环境变量读取(避免 settings 强制校验阻断迁移)
        print(f"[WARN] 无法加载 config.settings({e}),降级到环境变量", file=sys.stderr)
        redis_url = os.environ.get("REDIS_URL", "")
        list_key = os.environ.get("WRITER_QUEUE_KEY", DEFAULT_LIST_KEY)
        stream_key = os.environ.get("WRITER_STREAM_KEY", DEFAULT_STREAM_KEY)
    return redis_url, list_key, stream_key


def main():
    parser = argparse.ArgumentParser(
        description="将旧 Redis List 队列迁移到 Stream(R34 P2)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只检查不迁移(不执行 XADD/DEL)",
    )
    args = parser.parse_args()

    redis_url, list_key, stream_key = _load_settings()

    if not redis_url:
        print("[ERROR] 未配置 REDIS_URL,无法连接 Redis", file=sys.stderr)
        print("        请在 .env 中设置 REDIS_URL=redis://127.0.0.1:6379/0", file=sys.stderr)
        return 2

    print(f"[INFO] Redis URL: {redis_url}")
    print(f"[INFO] 旧 List key:   {list_key}")
    print(f"[INFO] 新 Stream key: {stream_key}")
    if args.dry_run:
        print("[INFO] --dry-run 模式: 仅检查,不执行迁移")
    print()

    try:
        import redis
    except ImportError:
        print("[ERROR] 未安装 redis 库,请先执行: pip install redis", file=sys.stderr)
        return 3

    try:
        client = redis.from_url(redis_url, decode_responses=True)
        client.ping()
    except Exception as e:
        print(f"[ERROR] Redis 连接失败: {e}", file=sys.stderr)
        return 4

    # 1. 检查旧 List 是否存在/类型是否正确
    list_type = client.type(list_key)
    if list_type == "none":
        print(f"[OK] 旧 List key '{list_key}' 不存在,无需迁移")
        return 0
    if list_type != "list":
        print(f"[ERROR] key '{list_key}' 类型为 '{list_type}',不是 list,无法迁移", file=sys.stderr)
        return 5

    list_len = client.llen(list_key)
    print(f"[INFO] 旧 List 长度: {list_len}")
    if list_len == 0:
        print("[OK] 旧 List 为空,清理空 key")
        if not args.dry_run:
            client.delete(list_key)
            print(f"[OK] 已删除空 List key: {list_key}")
        return 0

    # 2. LRANGE 读取所有消息(0 -1 表示从头到尾)
    messages = client.lrange(list_key, 0, -1)
    print(f"[INFO] 读取到 {len(messages)} 条消息")
    print()

    migrated = 0
    invalid = 0
    failed = 0
    stream_len_before = client.xlen(stream_key) if not args.dry_run else 0

    for idx, raw in enumerate(messages):
        # 3. 解析 JSON 验证有效性
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ValueError("message is not a dict")
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            invalid += 1
            print(f"[WARN] 第 {idx} 条消息 JSON 解析失败,跳过: {e}")
            print(f"       raw={raw!r}")
            continue

        # 如果消息没有 message_id,生成新的 UUID(用于幂等去重)
        if not msg.get("message_id"):
            msg["message_id"] = str(uuid.uuid4())
            print(f"[INFO] 第 {idx} 条消息无 message_id,已生成: {msg['message_id']}")

        if args.dry_run:
            migrated += 1
            continue

        # 4. XADD 到 Stream(字段名 "data" 与 database/redis_queue.push 保持一致)
        try:
            client.xadd(
                stream_key,
                {"data": json.dumps(msg, default=str, ensure_ascii=False)},
                id="*",  # Redis 自动生成有序 ID
            )
            migrated += 1
        except Exception as e:
            failed += 1
            print(f"[ERROR] 第 {idx} 条消息 XADD 失败: {e}", file=sys.stderr)
            print(f"        message_id={msg.get('message_id')}", file=sys.stderr)

    print()
    print("-" * 60)
    print("迁移报告")
    print("-" * 60)
    print(f"  旧 List 长度:          {list_len}")
    print(f"  成功迁移:              {migrated}")
    print(f"  跳过(无效 JSON):     {invalid}")
    print(f"  跳过(XADD 失败):     {failed}")
    if not args.dry_run:
        stream_len_after = client.xlen(stream_key)
        print(f"  Stream 长度(迁移前): {stream_len_before}")
        print(f"  Stream 长度(迁移后): {stream_len_after}")
        expected = stream_len_before + migrated
        if stream_len_after != expected:
            print(f"[WARN] Stream 长度校验失败: 期望 {expected},实际 {stream_len_after}")
        else:
            print(f"[OK] Stream 长度校验通过(+{migrated})")
    print()

    if args.dry_run:
        print("[INFO] --dry-run 模式: 未执行 XADD/DEL,旧 List 保持不变")
        return 0

    # 5. 安全检查: 有 XADD 失败时不删除旧 List,避免数据丢失
    if failed > 0:
        print(f"[WARN] 有 {failed} 条消息 XADD 失败,保留旧 List 以便排查", file=sys.stderr)
        print("        请排查失败原因后重新运行本脚本", file=sys.stderr)
        print("        (已迁移的消息会重复,但 message_id 相同可由 writer_inbox 幂等去重)", file=sys.stderr)
        return 6

    if migrated == 0:
        print("[WARN] 没有消息被迁移(全部无效),保留旧 List 以便排查")
        return 7

    # 6. 删除旧 List key(只在全部成功迁移后执行)
    try:
        deleted = client.delete(list_key)
        if deleted:
            print(f"[OK] 已删除旧 List key: {list_key}")
        else:
            print(f"[WARN] DEL 返回 0,key 可能已被其他进程删除: {list_key}")
    except Exception as e:
        print(f"[ERROR] 删除旧 List key 失败: {e}", file=sys.stderr)
        print(f"        请手动执行: redis-cli DEL {list_key}", file=sys.stderr)
        return 8

    # 7. 最终验证: LLEN 应为 0
    final_len = client.llen(list_key)
    if final_len == 0:
        print(f"[OK] 最终验证: 旧 List '{list_key}' 长度为 0")
    else:
        print(f"[WARN] 最终验证: 旧 List 长度仍为 {final_len}(可能有并发写入)")

    print()
    print("=" * 60)
    print(f"  迁移完成: {migrated} 条消息已从 List 迁移到 Stream")
    print(f"  旧 List key '{list_key}' 已删除")
    print(f"  新 Stream key '{stream_key}' 已就绪")
    print("=" * 60)
    print()
    print("下一步:")
    print(f"  1. 启动 db_writer 服务: systemctl start tgjiema-db_writer")
    print(f"  2. 查看消费状态: redis-cli XLEN {stream_key}")
    print(f"  3. 查看 pending: redis-cli XPENDING {stream_key} tgjiema-writer-group")
    return 0


if __name__ == "__main__":
    sys.exit(main())
