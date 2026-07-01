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
            logger.warning("[RelayPool] 无中继账号，请通过 admin_bot /relay_set_api 添加")
            self._initialized = True
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
        # 启动定期清理过期冷却记录
        asyncio.create_task(self._cleanup_cooldowns_loop())

    async def _cleanup_cooldowns_loop(self):
        """每 10 分钟清理一次过期冷却记录。"""
        while True:
            await asyncio.sleep(600)
            try:
                db = await get_relay_db()
                await db.cleanup_cooldowns()
            except Exception as e:
                logger.debug(f"[RelayPool] 清理冷却记录异常: {e}")

    async def get_best_account(self) -> RelayInstance | None:
        """
        智能选择最优账号（三维度负载均衡）:
        - avg_wait_ms (40%): 平均解码耗时，越小越好
        - today_requests (40%): 今日请求数，越少越好
        - last_request_gap (20%): 距上次请求的时间差，越大越好（冷却优先）
        
        权重和归一化因子可从 settings 配置项覆盖。
        """
        if not self.instances:
            return None
        # 筛选就绪的实例
        ready_instances = [i for i in self.instances if i.is_ready]
        if not ready_instances:
            logger.warning("[RelayPool] 没有就绪的中继账号")
            return None
        from config import settings
        # 权重配置（可覆盖）
        w_avg = getattr(settings, 'RELAY_WEIGHT_AVG_WAIT', 0.4)
        w_today = getattr(settings, 'RELAY_WEIGHT_TODAY_REQ', 0.4)
        w_gap = getattr(settings, 'RELAY_WEIGHT_GAP', 0.2)
        # 归一化因子（可覆盖）
        norm_avg_wait = getattr(settings, 'RELAY_NORM_AVG_WAIT', 1000.0)
        norm_today_req = getattr(settings, 'RELAY_NORM_TODAY_REQ', 50000.0)
        norm_gap = getattr(settings, 'RELAY_NORM_GAP', 3600.0)
        
        db = await get_relay_db()
        scores = []
        for instance in ready_instances:
            usage = await db.get_usage(instance.account_id)
            # avg_wait_ms 越小越好 -> 归一化后取倒数
            avg_wait = max(usage["avg_wait_ms"], 1)
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
                gap_seconds = norm_gap * 2  # 从未请求过，给最高冷却分
            # 加权评分：越低越好
            score = (
                (avg_wait / norm_avg_wait) * w_avg +
                (today_req / norm_today_req) * w_today +
                (norm_gap / max(gap_seconds, 1)) * w_gap
            )
            scores.append((score, instance))
        # 按 score 升序排列，选最低的
        scores.sort(key=lambda x: x[0])
        chosen = scores[0][1]
        # 重新查询被选中账号的统计数据用于日志
        chosen_usage = await db.get_usage(chosen.account_id)
        logger.debug(
            f"[RelayPool] 选择账号 {chosen.phone} "
            f"(today_req={chosen_usage.get('today_requests', 0)}, "
            f"avg_wait={chosen_usage.get('avg_wait_ms', 0):.0f}ms)"
        )
        return chosen

    async def release_account(self, instance: RelayInstance, duration_ms: int):
        """释放账号，更新使用统计"""
        db = await get_relay_db()
        try:
            await db.record_request(instance.account_id, duration_ms)
            await db.add_log(
                instance.account_id,
                action="decode_success",
                duration_ms=duration_ms,
            )
        except Exception as e:
            logger.error(f"[RelayPool] release_account 失败: {e}")
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
        async with self._lock:
            self.instances.append(instance)
        logger.info(f"[RelayPool] 动态添加中继账号: {phone}")
        return instance

    async def remove_account(self, phone: str) -> bool:
        """动态移除中继账号"""
        db = await get_relay_db()
        removed = await db.remove_account(phone)
        if removed:
            async with self._lock:
                self.instances = [i for i in self.instances if i.phone != phone]
            logger.info(f"[RelayPool] 移除中继账号: {phone}")
        return removed

    async def get_pool_status(self) -> list[dict]:
        """获取账号池状态"""
        db = await get_relay_db()
        status = []
        async with self._lock:
            instances_snapshot = list(self.instances)
        for instance in instances_snapshot:
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
        """关闭所有实例（并发关闭）"""
        async with self._lock:
            instances = list(self.instances)
        await asyncio.gather(*(i.shutdown() for i in instances), return_exceptions=True)
        logger.info("[RelayPool] 所有中继账号已关闭")


# 全局单例
relay_pool = RelayPool()
