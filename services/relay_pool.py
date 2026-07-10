"""中继账号池管理器 — 智能负载均衡 + 并发支持
- 从本地 SQLite 加载中继账号
- 每个账号独立 Telethon 客户端
- 智能负载均衡（三维度打分）
- 支持并发处理解码任务
"""
import asyncio
from datetime import datetime, timezone

from loguru import logger

from services.relay_instance import RelayInstance
from database.relay_db import get_relay_db


def _normalize_phone(phone: str) -> str:
    """手机号规范化:去除空格,确保以 + 开头。"""
    phone = phone.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+"):
        phone = "+" + phone
    return phone


class RelayPool:
    """中继账号池管理器"""

    def __init__(self):
        self.instances: list[RelayInstance] = []
        self._lock = asyncio.Lock()
        self._initialized = False
        self._cleanup_task: asyncio.Task | None = None
        self._health_check_task: asyncio.Task | None = None

    async def init(self):
        """从本地 SQLite 加载账号并创建实例"""
        async with self._lock:
            if self._initialized:
                return
            db = await get_relay_db()
            accounts = await db.get_active_accounts()
            if not accounts:
                logger.warning("[RelayPool] 无中继账号，请通过 admin_bot /relay_add 添加")
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
        self._cleanup_task = asyncio.create_task(self._cleanup_cooldowns_loop())
        # A2: 启动健康检查循环(FloodWait 恢复 + 封禁探测)
        self._health_check_task = asyncio.create_task(self._health_check_loop())

    async def _cleanup_cooldowns_loop(self):
        """每 10 分钟清理一次过期冷却记录。"""
        while True:
            await asyncio.sleep(600)
            try:
                db = await get_relay_db()
                await db.cleanup_cooldowns()
            except Exception as e:
                logger.debug(f"[RelayPool] 清理冷却记录异常: {e}")

    async def _health_check_loop(self):
        """A2: 每 120 秒执行健康检查 + 恢复 FloodWait 到期的账号。

        - 清除已到期的 FloodWait 限制
        - 对不可用的账号(ban/FloodWait到期/断线)做主动探测
        - FloodWait 未到期的账号跳过(避免加重 Telegram 限制)
        - ban 账号定期尝试恢复(Telegram 可能解除临时限制)
        """
        while True:
            await asyncio.sleep(120)
            try:
                async with self._lock:
                    instances_snapshot = list(self.instances)
                for inst in instances_snapshot:
                    # 1. 清除已到期的 FloodWait
                    inst.clear_expired_floodwait()
                    # 2. 对不可用账号做健康检查(跳过正在操作的账号)
                    if not inst.is_ready and not inst.is_busy:
                        # 跳过 FloodWait 未到期的账号(避免加重 Telegram 限制)
                        if inst._floodwait_until > 0:
                            continue
                        try:
                            await inst.check_health()
                        except Exception as e:
                            logger.debug(f"[RelayPool] 健康检查异常 ({inst.phone}): {e}")
            except Exception as e:
                logger.debug(f"[RelayPool] 健康检查循环异常: {e}")

    async def get_survival_stats(self) -> tuple[int, int]:
        """返回 (存活账号数, 总账号数) 用于账号存活率监控"""
        async with self._lock:
            total = len(self.instances)
            alive = sum(1 for inst in self.instances if inst.is_ready)
        return alive, total

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
                    # SQLite datetime('now') 返回无时区的 UTC 时间,需显式补上 tzinfo
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    now_dt = datetime.now(timezone.utc)
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
        """动态添加中继账号(非阻塞模式,参考 Save-Restricted-Content-Bot-v3)。

        不在添加时调用任何 Telegram API,直接保存到 DB。
        完整登录(connect → send_code → sign_in → password)由 idx_bot 的
        instance.start() 异步完成,避免阻塞 admin_bot conversation handler。

        登录流程:
        - idx_bot 通过 reload_from_db 检测新账号
        - 调用 instance.start() 完成完整登录
        - 验证码通过 /relay_code 提交,密码通过 /relay_password 提交
        """
        # 防御性:确保 api_id 为 int(Telethon 要求)
        try:
            api_id = int(api_id)
        except (TypeError, ValueError):
            raise RuntimeError(f"api_id 必须是数字,当前值: {api_id}")
        if not api_hash or not isinstance(api_hash, str):
            raise RuntimeError("api_hash 不能为空且必须是字符串")

        # 手机号规范化:确保以 + 开头
        phone = _normalize_phone(phone)

        db = await get_relay_db()
        # 检查是否已存在
        existing = await db.get_active_accounts()
        if any(a["phone"] == phone for a in existing):
            raise RuntimeError(f"手机号 {phone} 已存在,请勿重复添加")

        # 直接写入 DB,不做 Telegram API 验证
        # api_id/api_hash 有效性由 idx_bot 的 instance.start() 在 connect 时验证
        account_id = await db.add_account(api_id, api_hash, phone)
        logger.info(f"[RelayPool] 动态添加中继账号: {phone}(account_id={account_id})")
        logger.info(f"[RelayPool] 账号 {phone} 已保存,等待 idx_bot 异步登录(验证码/密码通过管理机器人提交)")

        # 阶段3:通知 idx_bot 增量同步新账号
        try:
            from database.cache_store import get_cache_store
            await get_cache_store().notify_relay_change()
        except Exception as notify_err:
            logger.warning(f"[RelayPool] notify_relay_change 失败(非致命): {notify_err}")

        return RelayInstance(
            account_id=account_id,
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
        )

    async def remove_account(self, phone: str) -> bool:
        """动态移除中继账号"""
        phone = _normalize_phone(phone)
        db = await get_relay_db()
        removed = await db.remove_account(phone)
        if removed:
            # 先找到 instance 引用用于关闭连接
            removed_instance: RelayInstance | None = None
            async with self._lock:
                remaining = []
                for i in self.instances:
                    if i.phone == phone:
                        removed_instance = i
                    else:
                        remaining.append(i)
                self.instances = remaining
            # 在锁外关闭 TelegramClient 连接
            if removed_instance:
                try:
                    await removed_instance.shutdown()
                except Exception as e:
                    logger.warning(f"[RelayPool] 关闭 {phone} client 失败: {e}")
            logger.info(f"[RelayPool] 移除中继账号: {phone}")
        return removed

    async def reload_from_db(self):
        """C3: 从 DB 增量同步账号列表(不中断现有连接,不触发通知)。

        由 idx_bot 的 _watch_relay_change 后台任务在检测到 relay_change_notify 时调用。
        - 对比 DB 账号列表 vs 内存 instances
        - 移除 DB 中已删除的账号(shutdown 实例)
        - 添加 DB 中新增的账号(创建实例 + start)
        注意:此方法不调用 add_account/remove_account(避免 DB 重复写入),
        也不调用 notify_relay_change(避免循环通知)。
        """
        db = await get_relay_db()
        db_accounts = await db.get_active_accounts()
        db_phones = {a["phone"] for a in db_accounts}
        # 获取 DB 账号字典(phone → account)
        db_account_map = {a["phone"]: a for a in db_accounts}
        async with self._lock:
            current_phones = {i.phone for i in self.instances}
            to_remove = current_phones - db_phones
            to_add = db_phones - current_phones
        # 移除已删账号
        for phone in to_remove:
            try:
                removed_instance: RelayInstance | None = None
                async with self._lock:
                    remaining = []
                    for i in self.instances:
                        if i.phone == phone:
                            removed_instance = i
                        else:
                            remaining.append(i)
                    self.instances = remaining
                if removed_instance:
                    await removed_instance.shutdown()
                logger.info(f"[RelayPool] reload 移除账号: {phone}")
            except Exception as e:
                logger.warning(f"[RelayPool] reload 移除 {phone} 失败: {e}")
        # 添加新增账号
        for phone in to_add:
            acct = db_account_map[phone]
            try:
                instance = RelayInstance(
                    account_id=acct["id"],
                    api_id=acct["api_id"],
                    api_hash=acct["api_hash"],
                    phone=acct["phone"],
                )
                async with self._lock:
                    self.instances.append(instance)
                # 启动实例(可能需要验证码,未就绪不回滚——账号已存在于 DB)
                try:
                    await instance.start()
                except Exception as e:
                    logger.warning(f"[RelayPool] reload 启动 {phone} 异常(可能需要验证码): {e}")
                logger.info(f"[RelayPool] reload 添加账号: {phone} (ready={instance.is_ready})")
            except Exception as e:
                logger.warning(f"[RelayPool] reload 添加 {phone} 失败: {e}")

    async def get_pool_status(self) -> list[dict]:
        """获取账号池状态（DB 层信息，由 idx_bot 写入 status 字段）。

        H-1: 结合 status_updated_at 判断状态新鲜度,超过 5 分钟未更新标记为 stale。
        idx_bot 进程崩溃后,admin 侧读取会降级显示 ⚪ 陈旧,避免误显「正常」。
        """
        db = await get_relay_db()
        try:
            accounts = await db.get_active_accounts()
        except Exception:
            accounts = []
        # H-1: 状态新鲜度阈值(秒),超过此值未更新则标记为 stale
        STALE_THRESHOLD = 300  # 5 分钟
        now_utc = datetime.now(timezone.utc)
        status = []
        for acct in accounts:
            phone = acct["phone"]
            usage = await db.get_usage(acct["id"])
            raw_status = acct.get("status", "unknown")
            status_updated = acct.get("status_updated_at", "")
            # 判断新鲜度
            stale = False
            if status_updated:
                try:
                    # SQLite datetime('now') 格式: YYYY-MM-DD HH:MM:SS (无时区)
                    last_dt = datetime.fromisoformat(status_updated)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    age = (now_utc - last_dt).total_seconds()
                    if age > STALE_THRESHOLD:
                        stale = True
                except (ValueError, TypeError):
                    stale = True  # 解析失败视为陈旧
            else:
                stale = True  # 无时间戳视为陈旧
            status.append({
                "phone": phone,
                "account_id": acct["id"],
                "status": raw_status,
                "stale": stale,
                "status_info": acct.get("status_info", ""),
                "status_updated_at": status_updated,
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
        # 取消清理循环任务
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        # A2: 取消健康检查循环任务
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
        # 复位状态,允许后续重新 init
        async with self._lock:
            self.instances = []
            self._initialized = False
            self._cleanup_task = None
            self._health_check_task = None
        logger.info("[RelayPool] 所有中继账号已关闭")


# 全局单例
relay_pool = RelayPool()
