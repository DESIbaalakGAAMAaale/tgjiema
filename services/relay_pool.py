"""中继账号池管理器 — 智能负载均衡 + 并发支持
- 从本地 SQLite 加载中继账号
- 每个账号独立 Telethon 客户端
- 智能负载均衡（三维度打分）
- 支持并发处理解码任务
"""
import asyncio
from datetime import datetime, date

from loguru import logger

from services.relay_instance import RelayInstance
from database.relay_db import get_relay_db


class RelayPool:
    """中继账号池管理器"""

    def __init__(self):
        self.instances: list[RelayInstance] = []
        self._lock = asyncio.Lock()
        self._initialized = False

    async def init(self):
        """从本地 SQLite 加载账号并创建实例"""
        if self._initialized:
            return
        db = await get_relay_db()
        accounts = await db.get_active_accounts()
        if not accounts:
            logger.info("[RelayPool] 本地无中继账号，使用单账号模式兼容")
            await self._init_from_settings()
            return
        for acct in accounts:
            instance = RelayInstance(
                account_id=acct["id"],
                api_id=acct["api_id"],
                api_hash=acct["api_hash"],
                phone=acct["phone"],
            )
            self.instances.append(instance)
        logger.info(f"[RelayPool] 从本地加载 {len(self.instances)} 个中继账号")
        self._initialized = True

    async def _init_from_settings(self):
        """从 .env 配置初始化（兼容旧单账号模式）"""
        from config import settings
        ids = [s.strip() for s in settings.RELAY_API_IDS.split(",") if s.strip()]
        hashes = [s.strip() for s in settings.RELAY_API_HASHES.split(",") if s.strip()]
        phones = [s.strip() for s in settings.RELAY_PHONES.split(",") if s.strip()]

        # 如果配置了多账号，先检查本地是否已有
        if len(phones) > 1:
            db = await get_relay_db()
            existing = await db.get_all_accounts()
            existing_phones = {a["phone"] for a in existing}
            new_phones = [p for p in phones if p not in existing_phones]
            for i, phone in enumerate(new_phones):
                api_id = int(ids[i]) if i < len(ids) else 0
                api_hash = hashes[i] if i < len(hashes) else ""
                if api_id and api_hash:
                    await db.add_account(api_id, api_hash, phone)
                    logger.info(f"[RelayPool] 新增中继账号: {phone}")
            # 重新加载
            accounts = await db.get_active_accounts()
            for acct in accounts:
                instance = RelayInstance(
                    account_id=acct["id"],
                    api_id=acct["api_id"],
                    api_hash=acct["api_hash"],
                    phone=acct["phone"],
                )
                self.instances.append(instance)
            logger.info(f"[RelayPool] 从配置加载 {len(self.instances)} 个中继账号")
        elif phones:
            # 单账号兼容模式
            api_id = int(ids[0]) if ids else 0
            api_hash = hashes[0] if hashes else ""
            phone = phones[0]
            if api_id and api_hash and phone:
                db = await get_relay_db()
                db_row = await db.add_account(api_id, api_hash, phone)
                instance = RelayInstance(
                    account_id=db_row,
                    api_id=api_id,
                    api_hash=api_hash,
                    phone=phone,
                )
                self.instances.append(instance)
                logger.info(f"[RelayPool] 单账号兼容模式: {phone}")

        self._initialized = True

    async def start_all(self):
        """启动所有中继实例"""
        if not self._initialized:
            await self.init()
        tasks = []
        for instance in self.instances:
            tasks.append(instance.start())
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            ready_count = sum(1 for r in results if not isinstance(r, Exception))
            logger.info(f"[RelayPool] 启动完成: {ready_count}/{len(self.instances)} 个账号就绪")
        else:
            logger.warning("[RelayPool] 没有可用的中继账号")

    async def get_best_account(self) -> RelayInstance | None:
        """
        智能选择最优账号（三维度负载均衡）:
        - avg_wait_ms (40%): 平均解码耗时，越小越好
        - today_requests (40%): 今日请求数，越少越好
        - last_request_gap (20%): 距上次请求的时间差，越大越好（冷却优先）
        """
        if not self.instances:
            return None
        db = await get_relay_db()
        scores = []
        for instance in self.instances:
            usage = await db.get_usage(instance.account_id)
            # avg_wait_ms 越小越好 -> 用倒数
            avg_wait = max(usage["avg_wait_ms"], 1)
            # today_requests 越少越好
            today_req = max(usage["today_requests"], 1)
            # last_request_gap 越大越好 -> 用倒数
            last_req = usage["last_request_at"]
            if last_req:
                try:
                    last_dt = datetime.fromisoformat(last_req.replace("Z", "+00:00"))
                    now_dt = datetime.now(last_dt.tzinfo)
                    gap_seconds = max((now_dt - last_dt).total_seconds(), 1)
                except (ValueError, TypeError):
                    gap_seconds = 1
            else:
                gap_seconds = 3600  # 从未请求过，给最高冷却分
            score = (avg_wait * 0.4) + (today_req * 0.4) + (3600 / max(gap_seconds, 1) * 0.4)
            scores.append((score, instance))
        # 按 score 升序排列，选最低的
        scores.sort(key=lambda x: x[0])
        chosen = scores[0][1]
        logger.debug(
            f"[RelayPool] 选择账号 {chosen.phone} (score={scores[0][0]:.2f}, "
            f"today_req={scores[0][0]}, avg_wait={chosen.account_id})"
        )
        return chosen

    async def release_account(self, instance: RelayInstance, duration_ms: int):
        """释放账号，更新使用统计"""
        db = await get_relay_db()
        await db.record_request(instance.account_id, duration_ms)
        await db.add_log(
            instance.account_id,
            action="decode_success",
            duration_ms=duration_ms,
        )
        logger.debug(
            f"[RelayPool] 账号 {instance.phone} 请求完成, 耗时={duration_ms}ms, "
            f"今日总请求={await self._get_today_req(instance)}"
        )

    async def _get_today_req(self, instance: RelayInstance) -> int:
        db = await get_relay_db()
        usage = await db.get_usage(instance.account_id)
        return usage["today_requests"]

    async def add_account(self, api_id: int, api_hash: str, phone: str) -> RelayInstance:
        """动态添加中继账号"""
        db = await get_relay_db()
        account_id = await db.add_account(api_id, api_hash, phone)
        instance = RelayInstance(
            account_id=account_id,
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
        )
        self.instances.append(instance)
        logger.info(f"[RelayPool] 动态添加中继账号: {phone}")
        return instance

    async def remove_account(self, phone: str) -> bool:
        """动态移除中继账号"""
        db = await get_relay_db()
        removed = await db.remove_account(phone)
        if removed:
            self.instances = [i for i in self.instances if i.phone != phone]
            logger.info(f"[RelayPool] 移除中继账号: {phone}")
        return removed

    async def get_pool_status(self) -> list[dict]:
        """获取账号池状态"""
        db = await get_relay_db()
        status = []
        for instance in self.instances:
            usage = await db.get_usage(instance.account_id)
            status.append({
                "phone": instance.phone,
                "is_ready": instance.is_ready,
                "is_busy": instance.is_busy,
                "relay_user_id": instance.relay_user_id,
                "today_requests": usage["today_requests"],
                "total_requests": usage["total_requests"],
                "avg_wait_ms": usage["avg_wait_ms"],
                "last_request_at": usage["last_request_at"],
            })
        return status

    async def shutdown(self):
        """关闭所有实例"""
        for instance in self.instances:
            await instance.shutdown()
        logger.info("[RelayPool] 所有中继账号已关闭")


# 全局单例
relay_pool = RelayPool()
