# tgjiema 项目全面代码审查报告

<aside>
⚠️

本报告基于对仓库 `maxiuquan/tgjiema`（默认分支 `master`）的深度源码审查。已对架构文档、配置、数据库/缓存层、五个 Bot 核心逻辑、中继池、Mon 调度器、管理后台与工具模块做了逐函数级别的走查，并在下方给出可复现、可定位的问题清单与修复建议。

</aside>

## 一、结论速览

本项目工程化程度较高（RU 优化、限流、退避、CSRF、心跳冗余都有考虑），但存在**几处足以动摇核心架构承诺的严重缺陷**——尤其是「影子频道冗余复制」这一整个架构的立身之本，实际上是**基于 `get_updates` 的不可靠实现 + 空实现的存量补齐**，即宣称的 3×45 频道冗余在历史文件层面并未真正建立。此外管理后台的 CSRF 兜底逻辑、进程内限流、若干配额/时间处理存在可利用或可导致数据不一致的问题。

| 级别 | 数量 | 代表问题 |
| --- | --- | --- |
| 🔴 严重 | 5 | 影子复制依赖 get_updates 不可靠；存量补齐空实现；故障切换后投递 404；全新部署建表顺序致初始化失败(N-C1)；备份/恢复与 schema 错配致 DR 整体失效(N-C2) |
| 🟠 高危 | 8 | 重启回滚运行时拓扑(N-H5)；后台 CSRF 兜底虚设；限流/CSRF 仅进程内内存；动态加中继账号不生效；外部码配额提前扣减；多账号验证码串码(N-H1)；中继双实现分叉(N-H2)；配置缓存 ^ 正则致路由/限流永空(N-H3) |
| 🟡 中危 | 20 | 缓存不一致致RENEW空转(N-M13)；重启覆盖轮转配置(N-M11)；拓扑缓存串 scope(N-M12)；降级冷却失效字段；工厂重置遗漏表；确定性 ID 熵；时间混用；配置漂移；后台 RU；file_id 可移植性；通知删除即消费；execute 恒返 1；config 缓存不失效；$regex 未转义；备份含明文密钥；删不存在文件误报成功等 |
| ⚪ 低危/质量 | 多处 | 宽泛异常吞没、私有方法越界调用、密钥经 Telegram 明文回显等 |

---

## 二、🔴 严重问题（Critical）

### C1. 影子频道复制依赖 `get_updates`，不可靠 → 冗余名存实亡

**位置**：`services/mon/scheduler.py` → `_fetch_new_messages()`（被 `replicate_all_active_to_shadows` 调用，是 Mon「写入/备份」职责的核心）。

```python
updates = await bot_instance.get_updates(
    offset=-100, allowed_updates=["channel_post"], timeout=5,
)
```

**问题**：

- Bot API 的 `get_updates` **只能取到 Telegram 更新队列中尚未确认的实时更新**，无法回溯读取频道历史消息；一旦某条 `channel_post` 更新被消费/确认，后续再也取不到。
- `offset=-100` 只是「取最近若干条缓存更新」，语义不适合做「游标 > last_synced 的增量同步」；并且每轮循环重复调用会不可控地推进 offset，造成漏读/重复。
- Mon 使用的是裸 `Bot`（无常驻 Updater）。若同一 Bot Token 在别处也有 `getUpdates`/webhook，会直接冲突报错。
- 净效果：**Active 频道的新文件大概率无法稳定复制到 Shadow 频道**，即宣称的「5 账号 × 9 频道 = 45 频道、3× 冗余」在文件层面并未真正建立。

**建议**：改用真实的 MTProto 客户端（项目已有 Telethon 中继账号）以频道历史遍历（`iter_messages` + `min_id` 游标）做复制，或在写入时（Up Bot 落频道那一刻）就 fan-out 到影子频道并记录 message_id 映射，而不是事后靠 Bot `get_updates` 回捞。

### C2. 新频道存量补齐是空实现（stub）

**位置**：`services/mon/scheduler.py` → `auto_fill_new_channels()` 依赖：

```python
@staticmethod
async def _fetch_all_media(bot_instance, channel_id, limit=200) -> list:
    # ptb 21.6 不支持迭代消息，返回空列表。
    return []

@staticmethod
async def _is_channel_nearly_empty(bot_instance, channel_id, threshold=3) -> bool:
    await bot_instance.get_chat(channel_id)
    return False   # 恒为 False
```

**问题**：`_is_channel_nearly_empty` 恒返回 `False`、`_fetch_all_media` 恒返回 `[]`，因此「新频道自动补齐存量文件」这一被文档称为「核心设计」的能力**完全不工作**。备用池补充的新频道、`lost` 恢复的频道、轮转唤醒的影子频道都不会得到历史文件。

**建议**：同 C1，用 Telethon 客户端实现历史遍历补齐；在实现前不应对外宣称「自动补齐」。

### C3. C1+C2 叠加 → 故障切换/轮转后投递命中空频道（404）

**位置**：`storage/delivery_resolver.py`（环形游走 + `demoted_to` 跳转） 与 `bots/dsp_bot.py`（投递回退）。

**问题**：投递侧的环形游走/降级跳转都假设「影子频道与 Active 频道内容一致」。但由于 C1/C2，影子/新频道可能并不含目标 `message_id`，封禁替换、降级、轮转发生后，对历史文件码的投递会命中不存在的消息而失败。这是把两个复制缺陷放大成**用户可见的取文件失败**的关键链路。

**建议**：在 delivery 侧对「目标频道是否确有该 msg_id」增加校验与跨频道兜底；根因仍需修复 C1/C2。

---

## 三、🟠 高危问题（High）

### H1. 管理后台 CSRF 兜底逻辑形同虚设

**位置**：`admin/__init__.py` → `_verify_csrf()`

```python
if cookie_token in _csrf_tokens.values():
    return cookie_token == form_token
# 服务重启后 cookie 失效，fallback:
if form_token in _csrf_tokens.values():
    return True   # ← 只要表单 token 在服务端存在即通过，不再校验 cookie
```

**问题**：后台用 HTTP Basic Auth，浏览器会对任意跨站请求自动附带凭证，因此 CSRF 防护尤为重要。但兜底分支只要 `form_token` 命中服务端集合即放行，**不再要求 cookie 与表单一致**，等于绕过了「双提交 Cookie」的核心校验。结合 token 是按用户名全局共享，削弱了防护强度。

**建议**：删除该兜底分支；始终强制 `cookie_token == form_token` 且 cookie 已注册；重启导致失效属预期，让用户重新加载页面即可。

### H2. 登录限流与 CSRF token 仅存进程内内存

**位置**：`admin/__init__.py`：`_login_failures`、`_csrf_tokens` 均为模块级 dict。

**问题**：若 uvicorn 以多 worker 运行，状态**按进程各自独立**：登录失败计数可被多 worker 轮询绕过（暴力破解限流失效），CSRF token 在 worker 间不一致导致合法请求被拒或校验错乱。

**建议**：将失败计数与 CSRF/会话状态放到共享存储（如 SQLite/Redis），或强制单 worker，并在文档中明确。

### H3. 动态添加中继账号后不会真正连接

**位置**：`services/relay_pool.py` → `add_account()`（由 `bots/admin_bot/handlers.py` 的 `/relay_add` 调用）。

**问题**：`add_account` 只 `append(instance)` 到池中，**未调用 `instance.start()`**。而回显文案却称「解码机器人将自动检测新账号并连接」。代码中未见自动检测/启动新实例的循环，因此新账号 `is_ready` 一直为 False，实际要等**整个 Bot 重启**才生效（这与 `/relay_set_api` 文案「下次重启生效」自相矛盾）。

**建议**：`add_account` 内 `await instance.start()`（或触发登录流程）；统一两条命令的行为与提示。

### H4. 外部码配额在投递成功前就被扣减

**位置**：`services/permission.py` → `check_decode_permission()`（`increment_user_quota_used` + `incr_request_count`）与 `bots/idx_bot.py` 的外部码链路。

**问题**：配额在权限校验阶段即扣减，但外部码走中继/目标 Bot 后续可能失败（`RELAY_ERROR`、超时等）。用户会「次数被扣、文件没到」，且外部码不校验本地文件记录，更易空扣。

**建议**：对失败路径做配额回补，或改为投递成功后再计费。

---

## 四、🟡 中危问题（Medium）

### M1. 降级冷却读取的是已不再写入的字段

**位置**：`services/mon/scheduler.py` → `run_degrade_check()` 读取 `active_slot.get("last_heartbeat")` 判断冷却。但 `mon_bot.py` 已注明「`last_heartbeat` 不再写入 CRDB」，心跳只写独立心跳表（`write_heartbeat`），而 `cells` 缓存里的 `last_heartbeat` 多为空/陈旧。结果：`elapsed` 计算被跳过，`degrade_count` 递增冷却（600s/1200s）基本失效，可能过快连锁降级。

**建议**：冷却判断改用心跳表的最近成功时间，或用内存 `_cell_fail_streak` 时间戳。

### M2. `factory_reset` 清空表不完整，留下悬挂数据

**位置**：`bots/admin_bot/handlers.py` → `_FACTORY_RESET_TABLES = [file_records, decode_logs, pending_uploads, users, backup_config]`。

**问题**：未包含 `codes`（解码主查表）、外部码映射、`jobs`、`spare_pool` 等。清空 `file_records` 却保留 `codes`，会产生「码还在、文件记录没了」的悬挂状态，解码路径行为不一致。

**建议**：明确重置范围并覆盖相关表，或在文档中说明保留项与后果。

### M3. 确定性 ID 仅消费 60 bit 熵

**位置**：`services/code_generator.py` → `_generate_deterministic_id()` 对 256-bit SHA256 只用 `(val >> (i*5)) % 36` 生成 12 位（约 60 bit）。docstring 宣称「数学上保证唯一」偏夸大；真正兜底靠 `time_ns()+pid`。同进程同纳秒并发是唯一碰撞点（`time_ns` 单调基本可避免），但建议下调宣称或加 DB 唯一约束+重试。

### M4. 时间处理 naive/aware 混用

**位置**：`database/relay_db.py` 冷却相关用 naive `datetime.utcnow()`，其他处用 `datetime.now(timezone.utc)` 与 SQLite `datetime('now')`。跨函数比较/`fromisoformat` 解析时存在 aware↔naive 混用隐患。

**建议**：全项目统一使用 timezone-aware UTC。

### M5. 动态限速器的 sleep 是否在锁内需确认

**位置**：`utils/dynamic_rate_limiter.py` → `acquire()`。注释称「等待在锁外执行」，但需确认 `await asyncio.sleep(self._current_delay)` 的缩进确实位于 `async with self._lock` **之外**。若在锁内，则全局解码派发被串行化（吞吐塌陷）。为全局单例，影响面大。

**建议**：核对缩进；确保 sleep 在释放锁后执行。

### M6. 配置默认值与 `.env.example` 不一致

**位置**：`config/settings.py` `DB_BACKUP_INTERVAL_MINUTES` 默认 360，`.env.example` 写 60，注释却称「已对齐」。属文档/配置漂移，易误导运维。

### M7. 后台列表页每次都 `count_documents` + 正则搜索走 CRDB

**位置**：`admin/__init__.py` 的 `/users`、`/files`、`/logs`。与项目「SQLite First 省 RU」的整体策略相悖，后台高频访问会产生可观 RU；`$regex` 大表搜索也可能慢。

**建议**：后台只读页尽量走本地快照/加索引，或限制搜索。

---

## 五、⚪ 低危 / 代码质量

- **密钥经 Telegram 明文流转**：`/relay_set_api`、`/relay_add`（回显 `API_HASH[:8]`）、`/set_r2`（密钥作为命令参数）会把敏感信息留在聊天记录与日志中；虽提示「请手动删除聊天记录」，仍属敏感面。建议改为分步私聊+即时撤回，并确认 R2 密钥在 `backup_config` 是否加密存储（中继会话用 Fernet，R2 疑似明文）。
- **私有方法越界调用**：`mon_bot._recover_lost` 调用 `store._bump_cells_version()`（下划线私有）。
- **`_get_next_promotable` 存在被完全覆盖的重复分支**（`scheduler.py`），死代码。
- **大量 `except Exception: pass` / 宽泛吞没异常**：多个缓存写入、通知发送处静默失败，问题难定位（部分已有日志，部分没有）。
- **`force_join` 失败即拦截**：`get_chat_member` 抛异常时记 debug 并展示加群提示（fail-closed），瞬时错误会误挡正常用户。
- **`_parse_storage_ids_from_caption`** 对 `STORAGE_IDS` 只做 `isdigit` 过滤，无上限/去重保护（下游有 `sorted(set())` 兜底，风险低）。
- **`json`/`orjson` 混用**、函数内惰性 import 普遍（多为规避循环依赖，可接受但影响可读性）。
- **部分中文 f-string 文案疑似被截断/串行**（如 idx_bot help、status 文案），建议核对是否源文件即如此，避免用户看到断句。

---

## 六、安全审查小结

- ✅ 后台账号校验使用 `secrets.compare_digest`（防时序攻击）、cookie `httponly + samesite=strict`、登录限流有设计——方向正确。
- 🔴 CSRF 兜底绕过（H1）、多 worker 状态不共享（H2）需优先修复。
- 🟠 敏感密钥经聊天/日志明文流转，需收敛。
- 🟡 未见强制 HTTPS 相关约束（依赖部署层，建议在文档中明确要求反代启用 TLS）。

---

## 七、修复优先级建议

1. **P0**：重构影子复制与存量补齐（C1/C2），改用 Telethon 历史遍历或写入即 fan-out；在此之前不要对外承诺冗余能力。
2. **P0**：投递侧增加「目标频道是否含该 msg_id」的校验与跨频道兜底（C3）。
3. **P1**：修复后台 CSRF 兜底（H1）、状态共享（H2）、动态加中继账号即时 start（H3）、外部码失败回补配额（H4）。
4. **P2**：降级冷却字段（M1）、工厂重置表范围（M2）、确认限速器锁范围（M5）、时间统一 UTC（M4）、配置漂移（M6）、后台 RU（M7）。
5. **P3**：清理死代码、收敛异常吞没、密钥流转、私有方法调用等质量项。

---

## 八、审查覆盖与说明

<aside>
📌

本轮已**逐函数深度走查**的模块：`config/settings.py`、`run_all.py`、`database/{models,relay_db,cache}.py`、`services/{permission,code_generator,relay_pool}.py`、`services/mon/scheduler.py`、`storage/{delivery_resolver,r2}.py`、`bots/{up_bot(部分),dsp_bot(部分),idx_bot(部分),mon_bot}.py`、`bots/admin_bot/handlers.py(部分)`、`admin/__init__.py`、`utils/{flood_waiter,force_join,dynamic_rate_limiter,rate_limiter,per_channel_limiter,task_utils}.py`。

</aside>

> 说明：仓库体量约 600KB、40+ 源文件。以下超大模块建议按同等深度做二轮专项走查（本轮受单次处理量限制未逐行读完）：`database/session.py`（~86KB）、`database/cache_store.py`（~74KB）、`services/user_relay.py`（~54KB）、`services/relay_instance.py`、`bots/admin_bot/{conversation,callback,display,menus}.py`、`cf-workers/file-bot/src/index.js`，以及 `tests/`、部署脚本与 Dockerfile。上面的 P0/P1 结论不依赖这些文件，但完整「不留任何问题」的目标需要覆盖它们。
> 

> 需要我继续对上述剩余大模块做第二轮逐行走查、或把本报告拆成可跟踪的问题清单（每条含定位/复现/修复）时，告诉我即可。
> 

---

## 九、第二轮深度审查补充（中继解码 / DB 抽象层 / 缓存存储层 / CF Worker）

<aside>
🆕

本节为第二轮走查结果，已逐函数覆盖：`services/relay_instance.py`、`services/user_relay.py`、`database/session.py`（前半 + DDL/迁移）、`database/cache_store.py`（前半）、`cf-workers/file-bot/src/index.js`。以下为**新增**问题，编号以 N- 开头，避免与前文冲突。

</aside>

### N-C1（🔴 严重·仅影响全新部署）建索引顺序早于建表/加列 → 首次初始化直接失败

**位置**：`database/session.py` → `DDL_STATEMENTS`（由 `connect()` 中 `for sql in DDL_STATEMENTS: await self.execute(sql)` 顺序执行，**该循环未包 try/except**）。

**问题**：`DDL_STATEMENTS` 里把三条「增量同步索引」放在了错误的位置：

```python
"CREATE INDEX IF NOT EXISTS idx_file_records_updated_at ON file_records(updated_at)",
"CREATE INDEX IF NOT EXISTS idx_codes_updated_at ON codes(updated_at)",
"CREATE INDEX IF NOT EXISTS idx_external_code_mapping_updated_at ON external_code_mapping(updated_at)",
```

但在一张**全新数据库**上：

- `file_records` 的 `updated_at` 列并不在其 `CREATE TABLE` 里，而是靠 `MIGRATION_STATEMENTS`（在 DDL 循环**之后**才执行）补的；
- `codes` 与 `external_code_mapping` 两张表的 `CREATE TABLE` 语句在 DDL 列表中排在这些 `CREATE INDEX` **后面**，即建索引时表尚不存在。

因此首次部署执行到这几行会抛「column/relation does not exist」，而 DDL 循环没有 try/except 兜底 → **`init_db` 中断，全新部署起不来**。现有已初始化实例因 `DDL_VERSION` 命中缓存跳过 DDL，不会触发，所以问题隐蔽（只在干净环境复现）。

**建议**：把这三条 `updated_at` 索引移到 `MIGRATION_STATEMENTS`（其已有 try/except），或调整 DDL 顺序为「先所有建表 → 再迁移加列 → 最后建索引」；并给 DDL 主循环加与迁移一致的容错日志。

### N-H1（🟠 高危）多中继账号共用同一「验证码」全局键 → 并发登录串码

**位置**：`services/relay_instance.py`/`services/user_relay.py` → `_wait_for_admin_code()`。两者都轮询同一套全局配置键：

```python
await set_config("relay_auth_pending", "1")
code = await get_config("relay_auth_code")
```

**问题**：`relay_pool` 会为多个账号各起一个 `RelayInstance`。当**两个及以上账号同时需要登录**（首次授权/掉线重登）时，它们读取的是**同一个** `relay_auth_code`/`relay_auth_pending`，无法区分验证码属于哪个手机号 → 管理员提交的验证码会被错误实例消费，导致登录失败甚至触发 Telegram 风控。

**建议**：验证码键按账号维度隔离（如 `relay_auth_code:{phone}` / `relay_auth_pending:{phone}`），并在 `/relay_code` 提交时携带目标账号标识。

### N-H2（🟠 高危·可维护性/一致性）中继存在两套近乎重复但行为分叉的实现

**位置**：`services/user_relay.py`（`UserRelay`，约 1233 行）与 `services/relay_instance.py`（`RelayInstance`，约 835 行）。二者的 `_register_handlers/_message_loop/_make_decision/_flush_media_group_buffer/_detect_media_type/...` 高度重复，但**核心落库行为不一致**：

- `UserRelay._download_and_cache_one` → 直接 `send_file` 到**存储频道**并自行写 `file_records`（含 `pack_bot_file_id` 生成 file_id + `_self_heal_file_ids` 自修复）。
- `RelayInstance._download_and_cache_one` → 改为把媒体转发给 **Up Bot**（`caption="EXTERNAL_RELAY:{uid}:{code}"`），不由中继写记录。

`UserRelay.is_ready`/`relay_user_id` 已改成委托给 `relay_pool.instances`，说明 `UserRelay` 实为「过渡壳」，但其内部仍保留了**完整且独立的一整套事件处理器**。若它在任何路径被 `start()`，会与 `RelayInstance` 形成**双份 handler / 双份缓存**，同一外部码可能被处理两次（既进存储频道又进 Up Bot 链路）。即便当前未启用，两套逻辑分叉也意味着任何修复都要改两处，极易漏改。

**建议**：抽出共享基类/mixin，只保留一套 handler 与决策逻辑；`UserRelay` 若已废弃应删除或彻底降为 `relay_pool` 的薄封装（不注册自己的 handler）。

### N-M1（🟡 中危）`send_external_code` 的并发保护过早释放

**位置**：`services/relay_instance.py` → `send_external_code()`。`is_busy=True` 在 `async with self._lock` 内设置，`finally` 里 `is_busy=False` 紧跟 `_do_send_external_code` 返回即执行；但真正耗时的多页收集/翻页点击在 `_message_loop` 后台任务里，**发生在 is_busy 复位之后**。

**问题**：`is_busy` 只覆盖「发送那一刻」，并不能阻止在上一单仍在翻页时向同一实例派发新单；跨不同 `bot_username` 时二者共用同一个 Telethon `_client`，缺乏对整段会话的串行保护。

**建议**：把忙碌状态持续到整段 exchange 结束（在 `_cleanup_exchange`/`_process_all_collected` 完成后再复位），或用信号量限制单实例并发会话数。

### N-M2（🟡 中危）冷却等待持锁 sleep，阻塞整个实例

**位置**：`services/relay_instance.py` → `send_external_code()`：`await asyncio.sleep(cooldown)` 处于 `async with self._lock` 且 `is_busy=True` 期间。冷却期内该实例完全无法接新单（即便冷却只针对某个外部 bot）。建议冷却等待移出锁，或按 `bot_username` 粒度冷却而非锁全实例。

### N-M3（🟡 中危·需验证）跨账号生成的 file_id 可移植性

**位置**：`services/user_relay.py` → `_extract_file_id()` 用 `telethon.utils.pack_bot_file_id` 从**用户账号(MTProto)** 消息生成 file_id，写入 `file_ids`/`batch_file_meta`。这些 file_id 交给**投递 Bot** 再次 `send` 时不一定通用（file_id 通常与具体 bot 绑定）。前文已述 dsp 侧多用 `copy_message` 兜底，但凡依赖 `file_ids` 直发的路径可能报 `wrong file_id`。**建议**：核实投递侧是否真正使用这些 file_id；若使用，改为存 `chat_id+message_id` 由投递 Bot `copy_message`。

### N-M4（🟡 中危）通知表「删除即消费」在多进程消费者下会丢通知

**位置**：`database/cache_store.py` → `has_new_upload()`/`has_new_dsp_job()` 用 `DELETE ... rowcount>0` 作为「有新任务」信号。若某类 Bot 以**多进程/多副本**消费同一张通知表，第一个 `DELETE` 会清空**所有**通知，其余消费者永远收不到 → 任务感知丢失。当前每类 Bot 单进程时可用，但与 H2（多 worker）叠加会放大。**建议**：通知表带 consumer 维度或改用「标记已读」而非全表删除。

### N-M5（🟡 中危）DB 抽象层 `execute` 恒返回 1，`matched_count`/`rowcount` 语义不可靠

**位置**：`database/session.py` → `D1Collection._execute` 恒 `return 1`，忽略 asyncpg 返回的 `UPDATE n`/`DELETE n` 计数。凡依据 `UpdateResult.matched_count` 或受影响行数判断「是否更新成功/是否命中」的调用点都会拿到失真结果（例如「幂等 upsert 是否新建」「删除是否命中」）。**建议**：解析 asyncpg 状态串（`conn.execute` 返回如 `"UPDATE 3"`）回填真实计数。

### N-L1（⚪ 低危·安全）CF Worker Webhook 未校验 Telegram secret token

**位置**：`cf-workers/file-bot/src/index.js` → `fetch()` 对任意 `content-type: application/json` 且含 `message` 的 POST 直接处理，未校验 `X-Telegram-Bot-Api-Secret-Token`。任何知道 Worker URL 的人都可伪造 update 触发 `sendMessage`（以该 bot 名义发垃圾/被用于放大）。虽为引导 bot、影响有限，仍建议在 `setWebhook` 时设置 secret_token 并在入口校验请求头。

> 说明：CF Worker 源码中形如 `https://t.me/...`、`https://api.telegram.org/...` 的双花括号是本次网页抓取工具对 URL 的压缩痕迹，**并非源码真实内容**，不作为缺陷。
> 

### N-L2（⚪ 质量）DDL/迁移与中继逻辑的重复与噪声

- `MIGRATION_STATEMENTS` 混用 `ADD COLUMN IF NOT EXISTS` 与裸 `ADD COLUMN`（如 `file_records/codes ADD COLUMN updated_at`、`cells ADD COLUMN demoted_to_channel_id`），靠 try/except 吞「已存在」异常并打「可忽略」日志——可用但会掩盖真实迁移失败，建议区分「预期已存在」与「真异常」。
- 中继两实现间 `_detect_media_type/_extract_number/_extract_wait_seconds/_make_decision` 等逐字重复（见 N-H2）。
- `asyncpg.create_pool(statement_cache_size=256)`：若部署在事务级连接池（如 PgBouncer transaction 模式）后面，预处理语句缓存会失效报错，建议在文档中标注直连要求或按需关闭缓存。

### 第二轮覆盖说明

<aside>
📌

仍建议后续三轮覆盖：`database/session.py` 后半（`update_one`/批量 upsert/同步循环）、`database/cache_store.py` 后半（cells 本地表读写与 CRDB 双写一致性）、`bots/admin_bot/{conversation,callback,display,menus}.py`（[callback.py](http://callback.py) 本轮抓取 504 超时未取到）、`services/{db_backup,db_restore}.py`、`admin/seed_topology.py` 与模板、部署脚本/Dockerfile/compose、`tests/`。N-C1/N-H1/N-H2 结论不依赖这些文件。

</aside>

---

## 十、第三轮深度审查补充（DB 抽象层后半 / 配置缓存 / cells 本地表 / 后台回调）

<aside>
🆕

本节已逐函数覆盖：`database/session.py` 后半（`D1Collection` 查询构造器 + 配置/路由/外部码映射）、`database/cache_store.py` 后半（cells 本地表/快照/热表 CRUD）、`bots/admin_bot/callback.py`（本轮已取到，完整 358 行）。编号续 N- 。

</aside>

### N-H3（🟠 高危）配置缓存刷新用了 `^` 正则锤点，但底层当 LIKE 字面处理 → 文件码路由 & 按 Bot 限流在热路径上永远为空

**位置**：`database/session.py` → `_refresh_bot_config_cache()`：

```python
all_routes = await _backup_config_col.find({"config_key": {"$regex": "^code_bot_route:"}})
all_intervals = await _backup_config_col.find({"config_key": {"$regex": "^bot_decode_interval:"}})
```

**问题**：`D1Collection` 的 `$regex` 并非真正正则，而是翻译成 `LIKE '%' || value || '%'`（见 `find`/`count_documents`：`params.append(f"%{v['$regex']}%")`）。因此 `^code_bot_route:` 会变成 `LIKE '%^code_bot_route:%'`，其中 `^` 被当作**字面字符**。而实际 `config_key` 形如 `code_bot_route:qqfile`（不含 `^`），所以**永远匹配不到任何行** → `_code_bot_routes_cache` / `_bot_decode_interval_cache` 刷新后恒为空。

**影响（双重）**：

- `resolve_bot_for_code()` 始终返回 `default_bot`，非默认 Bot 的外部码会被路由到错误的解码 Bot → 解码失败。
- `get_bot_decode_interval()` 始终返回 0 → 按 Bot 的解码限速不生效。
- 更隐蔽的是：`set_code_bot_route` 会先写内存缓存，但 TTL（10 分钟）到期后 `get_all_code_bot_routes`/`resolve_bot_for_code` 触发刷新，用空结果**反覆盖掉**本进程刚设的路由；跨进程（采集器写、idx 读）则从未生效。后台 `action:code_routes` 重启后也会显示空，而 `get_all_bot_decode_intervals`（不走该缓存、在 Python 侧 `startswith` 过滤）却能正常显示 → 「后台看得到配置、实际不生效」的强错位。

**建议**：去掉 `^`（改用 `code_bot_route:`），或为前缀查询新增专用 `startswith`/`LIKE 'prefix%'` 路径；并为 `$regex` 转 LIKE 做锤点与通配符转义处理。

### N-M6（🟡 中危）`set_config`/`delete_config` 不失效 `get_config` 缓存 → 后台改配最长 10 分钟才生效

**位置**：`database/session.py`：`get_config()` 走 `get_config_cached()`（10 分钟 TTL），但 `set_config()`/`delete_config()` 只调 `_set_config`/改写行，**未失效配置缓存**。后台不少操作（如 `set_storage_channel`、`set_file_prefix`、`set_force_join`、R2/备份参数等）都走 `set_config` → 修改后已缓存该键的进程最长 10 分钟读到旧值，且跨进程不一致（`set_code_bot_route`/`set_bot_decode_interval` 有局部刷内存，但通用 `set_config` 没有）。

**建议**：`set_config`/`delete_config` 内主动失效/回写 `get_config_cache`（并考虑跨进程通知）。

### N-M7（🟡 中危）`$regex` 转 `LIKE %v%` 未转义通配符，语义与正则不符

**位置**：`database/session.py` → `find`/`count_documents` 的 `$regex` 分支直接 `f"%{value}%"`。若搜索词含 `%`/`_`（LIKE 通配符）会被当通配符解释 → 后台 `/users`、`/files` 搜索可能返回过宽结果（如输 `%` 匹配全部）；且与「正则」命名语义不符，易误用。建议对 `%`/`_`/`\\` 做转义并附 `ESCAPE` 子句。

### N-M8（🟡 中危·确认 N-M5）写操作返回值不一致，`update_one`/`delete_one` 恒报成功

**位置**：`database/session.py`：`update_one` 恒 `return UpdateResult(1)`、`delete_one` 恒 `return True`（即使 `WHERE` 未命中任何行）；而 `delete_many`/`count_documents` 却正确解析了 asyncpg 状态串。同一抽象层内行为不一致：凡依 `update_one().matched_count`/`delete_one()` 判断「是否真正命中/删除」的业务都会被误导。建议两者也解析 `UPDATE n`/`DELETE n` 回填真实计数（补充 N-M5）。

### N-L3（⚪ 低危·可移植性）`callback.py` 用 `datetime.UTC`（3.11+），与其他处 `timezone.utc` 不一致

**位置**：`bots/admin_bot/callback.py` → `_handle_report_action` 中 `_dt.datetime.now(_dt.UTC)`。`datetime.UTC` 仅 Python 3.11+ 可用；若运行时为 3.10 及以下，一旦点击「举报→封禁」会抛 `AttributeError`。其余代码普遍用 `timezone.utc`，建议统一。

### N-L4（⚪ 低危·加固不一致）SQL 构造器列/键名未参数化

**位置**：`D1Collection.find_one/find/update_one/delete_*/count_documents` 均将 dict 键 `k` 直接拼入 SQL（`f"{k} = ${n}"`、`f"{k} = {k} + ${n}"`），仅值参数化。目前键均来自代码常量，风险低；但 `find` 只对 `sort[0]` 做了白名单正则校验，**对 where/set 键未校验**，属加固不一致。若今后有任何键来自外部输入即成注入面，建议统一对标识符做白名单校验。

### N-L5（⚪ 质量）cells 版本号基于毫秒时间戳，同毫秒多次变更可能漏感知

**位置**：`cache_store.py` → `_rebuild_cells_snapshot`/`_bump_cells_version` 用 `version = int(now*1000)`；`has_cells_change` 以 `version > last_version` 判断。同一毫秒内的多次 cells 变更会得到相同 version，其他 Bot 可能漏感知最后一次变更（低频、影响小）。建议用单调递增计数器而非时间戳作版本。

<aside>
📌

后台权限面：`callback.py` 的 `menu_callback`/`_handle_report_action` 均正确校验 `user.id == AUTHORIZED_USER_ID`（单管理员），未发现越权。仍待覆盖：`admin_bot/{conversation,display,menus}.py`、`services/{db_backup,db_restore}.py`、`admin/seed_topology.py` 与模板、部署脚本/Dockerfile/compose、`tests/`。

</aside>

---

## 十一、第四轮深度审查补充（后台会话处理器 / 备份与恢复）

<aside>
🆕

本节已逐函数覆盖：`bots/admin_bot/conversation.py`（完整 498 行）、`services/db_backup.py`（完整）、`services/db_restore.py`（完整）。发现一处**灾备（DR）整体失效**的严重问题。

</aside>

### N-C2（🔴 严重）备份/恢复的主键、列白名单、表白名单与真实 schema 大面积错配 → DR 几乎恢复不了任何表

**位置**：`services/db_restore.py` 的 `TABLE_PK` / `_ALLOWED_COLUMNS` / `_ALLOWED_TABLES` 与 `database/session.py` 建表 DDL 不一致。`restore_table` 一旦遇到白名单外的列/表/主键就**整表跳过或报错**。逐表核对（均为 `db_backup` 实际备份的表）：

- **`backup_config`**：`TABLE_PK` 写 `key`，真实主键是 `config_key`；`config_key`/`config_value` 不在列白名单；且 `backup_config` 不在 `_ALLOWED_TABLES` → 整表无法恢复（而它正是存储频道/密钥/路由等关键配置的表）。
- **`users`**：`can_upload` 不在 `_ALLOWED_COLUMNS` → `_sanitize_column` 抛 `ValueError` → 整表跳过。
- **`cells`**：`demoted_to_channel_id`/`account_name`/`last_synced_msg_id`/`degrade_count`/`file_count` 都不在列白名单 → 整表跳过。
- **`spare_pool`**：`TABLE_PK` 写 `id`，真实主键是 `channel_id`；`is_used`/`account_name` 也不在白名单 → 失败。
- **`relay_accounts`**：`api_hash`/`is_active`/`last_login_at` 不在白名单（白名单只有 `api_hash_encrypted`）→ 整表跳过。
- **`code_bot_mapping`**：`TABLE_PK` 写 `prefix`，真实是 `code_prefix`；且不在 `_ALLOWED_TABLES` → 无法恢复。
- **`rotation_config` / `external_code_mapping`**：被备份但不在恢复的 `ALL_TABLES`/主键表/表白名单 → 永不恢复。
- **`kv_config`**：备份与恢复都引用了**不存在的表**（真实 KV 在 SQLite 的 `kv_store`）。

**影响**：一旦 CRDB 丢失需从 R2 恢复，`users`、`cells`、`backup_config`、`spare_pool`、`relay_accounts`、`code_bot_mapping` 等**几乎全部关键表都会被静默跳过**，恢复会报一堆错误日志但「完成」，实际恢复行数接近零。对一个以冗余/可恢复为卖点的项目，这是致命的。

**建议**：根据真实 DDL 重建三份白名单/主键映射，并加单元测试“每张备份表的列∈白名单且主键存在”；或改为基于 schema 元信息自动生成列映射。

### N-H4（🟠 高危）备份不含 `file_records`/`codes`，而其声称的“从频道重建索引”恢复路径实为空实现

**位置**：`services/db_backup.py`：`BACKUP_TABLES = SMALL_TABLES`，显式将 `file_records`/`codes`/`decode_logs`/`jobs` 归为 `_LARGE_TABLES` 跳过，注释称「可从 Telegram 频道重新索引」。

**问题**：`file_records`/`codes` 是「文件码 → 频道/消息_id」的唯一映射；而「从频道重建索引」依赖的 `auto_fill_new_channels`/`_fetch_all_media` 已在 C2 证实为空实现。因此一旦 CRDB 丢失，**最核心的解码映射既没备份、也无法重建** → 历史文件码全部失效。建议：至少按 updated_at 增量备份 `codes`/`file_records`，或真正实现频道重索引后再声称可恢复。

### N-M9（🟡 中危·安全）备份 JSON 含明文密钥上传 R2

**位置**：`db_backup` 备份 `backup_config`（含 `r2_secret_key`、`r2_access_key`、各种 token）与 `relay_accounts`（`api_hash`），以**明文**写入 R2 上的 `db_backup/*.json` 与 `latest_backup_config.json`。若 R2 桶/凭证泄露，所有密钥一并泄露。且 restore 白名单叫 `api_hash_encrypted`、DDL/备份里却是 `api_hash` 明文，命名与实现矛盾（看似曾打算加密但未落地）。建议：备份前对敏感列加密（Fernet）或排除，并确保 R2 桶私有。

### N-M10（🟡 中危·N-M8 的具体后果）删除不存在的文件码会误报“已删除”

**位置**：`bots/admin_bot/conversation.py` → `delete_file:code`：`result = await files_col.update_one(...)` 后用 `result.matched_count == 0` 判断「不存在」。但如 N-M8，`update_one` 恒返回 `matched_count=1`，所以**删除一个根本不存在的文件码会显示“✅ 文件已删除”**，管理员被误导。修好 N-M8（真实行数）后此处自愈。

### H3 修正与补充（两条“加中继账号”入口行为不一致）

本轮发现：交互式菜单路径 `relay_set_api`（`conversation.py`）在 `add_account` 后**确实调用了 `login_with_credentials` 登录**，失败时 `remove_account` 回滚；而 H3 指出的 `/relay_add` 命令路径（`handlers.py`）仍只 `append` 不 `start`。因此**两条“加中继账号”入口行为不一致**（一个立即登录、一个需重启），建议统一到同一实现。

<aside>
📌

仍待覆盖：`admin_bot/{display,menus}.py`、`admin/seed_topology.py` 与 HTML 模板、`config/{generate_topology.py,topology.yaml,groups.yaml}`、部署脚本/Dockerfile/compose、`tests/`、`session.py`/`cache_store.py` 尾部。前四轮的 P0/P1 结论不依赖这些文件。

</aside>

---

## 十二、第五轮深度审查补充（启动/自愈拓扑 · 后台展示层 · 拓扑生成器）

<aside>
🆕

本节已逐函数覆盖：`run_all.py`（启动编排 + 子进程自愈）、`admin/seed_topology.py`（`seed`/`auto_seed`）、`config/generate_topology.py`（拓扑生成 + 轮转参数加载）、`bots/admin_bot/display.py`（后台各视图文本）、`bots/admin_bot/menus.py`（菜单/权限装饰器）。发现一处**重启即回滚运行时拓扑**的高危问题。

</aside>

### N-H5（🟠 高危，生产环境可升级为严重）每次启动都会把 cells 运行时状态重置回 topology.yaml 初始配置

**位置**：`run_all.py` → `main()` 开头无条件调用 `_auto_seed()` → `asyncio.run(auto_seed())` → `admin/seed_topology.py::seed(force=True)`。

**问题**：`seed()` 对每个已存在槽位构造 `update_data`（含 `channel_id`、`status`、`next_active_chat_id`、`prev_slot_id`、`account_name`、`is_r100`），只要与 topology.yaml 的**初始**配置不同就 `update_one($set)` 覆盖。而 `cells.status`/`channel_id` 等正是 Mon 运行时轮转的**活状态**（active 窗口滑动、shadow→active 晋升、降级、`lost` 标记、备用池替换被封频道后改写的 channel_id）。因此：

- 每次整机重启/重新部署，**所有运行时轮转结果被抹掉、回到创世配置**；
- 被封禁后由备用池替换的频道，其 `channel_id` 会被**改回原已封频道** → 复活坏频道、投递命中失败；
- 已 `lost`/降级的槽位被重置为 active/shadow → 可能把不可用频道重新当作主频道。

叠加 C1/C2（复制/补齐本就失效），这让「重启」成为又一条破坏冗余状态的途径。

**建议**：`auto_seed` 仅在 cells 表为空（全新部署）时写入；对已存在槽位只补齐缺失字段，**绝不覆盖 status/channel_id/next_active_chat_id 等运行时字段**；或以显式开关控制，默认幂等 upsert-if-absent。

### N-M11（🟡 中危）启动 seed 用 topology.yaml 的默认 mon 覆盖 DB 轮转配置；自动重生成时 DB 轮转参数读不到

**位置**：`admin/seed_topology.py::seed()` 步骤4 无条件 `set_rotation_config(...)`（值来自 topology.yaml 的 mon）；`config/generate_topology.py::_load_rotation_from_db_or_env()` 用 `asyncio.run(_load())`。

**问题**：

- `auto_seed` 已在 `asyncio.run(...)` 事件循环内运行，`generate()`（同步）内部再调 `asyncio.run(_load())` 会抛 `RuntimeError: asyncio.run() cannot be called from a running event loop`，被就地 try/except 吞掉 → **自动重生成 topology.yaml 时永远读不到 DB 轮转参数，回退 .env/默认值**（仅刷一行警告日志）。
- seed 步骤4 随后又把 topology.yaml 里（可能是默认值的）mon 参数写回 `rotation_config` → **每次重启都可能用默认值覆盖管理员经后台调过的 DB 轮转参数**。

**建议**：generate 内避免嵌套 `asyncio.run`（复用当前 loop 或传入已加载配置）；seed 步骤4 改为「仅当 rotation_config 不存在时初始化」，不覆盖已有值。

### N-M12（🟡 中危）后台 `_get_cells_cached` 的 60s 全局缓存忽略过滤条件，拓扑/概览两个 scope 串用

**位置**：`bots/admin_bot/display.py` → `_get_cells_cached(status_filter, sort_key)`。

**问题**：缓存是模块级单一 `_cells_cache`，**不区分 `status_filter`/`sort_key`**；命中缓存时无视入参直接返回上次结果，且 SQLite 快速分支走 `get_active_cells_local()`。于是 `_get_status_text()`（传 `{"status":"active"}`）与 `_get_topology_text()`（传空、期望**全部**槽位）**共用同一份缓存**：谁先在 60s 窗口内触发，另一个就拿到错误 scope 的数据 → 拓扑视图的 shadow/r100/lost 统计与分组可能失真（例如只看到 active 槽位）。

**建议**：缓存键纳入 `status_filter`/`sort_key`；或拓扑查全量、概览查 active，各自独立缓存。

### N-L6（⚪ 低危）轮转配置键名不一致 → 后台「活态窗口」恒显示默认值

**位置**：seed/生成器写入并读取 `active_window_size`，但 `display._get_topology_text` 读的是 `get_rotation_config("rotation_active_window_size")`（多了 `rotation_` 前缀，取不到 → 恒回退 `"3"`）；而 `rotation_files_per_slot`/`rotation_time_per_slot` 两键是一致的。**建议**：统一键名。

<aside>
✅

本轮正向确认：`menus.py` 的 `_auth_required` 对所有交互入口正确校验 `user.id == AUTHORIZED_USER_ID`；`generate_topology.py` 的配对算法（5 账号滑动窗口、每组 A/S1/S2 三不同账号）、频道重复检测、每账号频道数须为 3 倍数、账号用量验证等校验较完善；`run_all.py` 子进程崩溃自愈带窗口限流+冷却，设计合理。

</aside>

<aside>
📌

仍待覆盖：`config/{groups.yaml,topology.yaml}`（静态数据）、`db_backup`/`db_restore` 的 R2 上传下载细节、`admin/templates/*.html`、`admin/migrations/*.sql`、部署脚本/Dockerfile/compose、`tests/`、`session.py`/`cache_store.py` 尾部剩余行。

</aside>

---

## 十三、第六轮深度审查补充（本地队列/同步回写 · 中继翻页与交付 · 缓存一致性）

<aside>
🆕

本节已读完：`database/session.py`（1261→尾，jobs 本地队列/死信/批量回写/备用池/轮转配置/`get_active_cells_local`）、`database/cache_store.py`（1426→尾，codes/users/external 本地表 + 日志/变更缓冲）、`services/user_relay.py`（711→尾，`_make_decision`/`_click_button`/`_process_all_collected`/`deliver_cached`）、`services/relay_instance.py`（656→尾）。新增 1 中危 + 1 低危，并坐实/强化多条旧结论。

</aside>

### N-M13（🟡 中危）`deliver_cached` 删除过期文件记录时只删 CRDB、不失效缓存 → 陈旧空记录被缓存命中、RENEW 无法真正重建

**位置**：`services/user_relay.py::deliver_cached()` 与 `services/relay_instance.py::deliver_cached()`：当 `file_ids` 为空（记录过期）时直接 `get_file_records_col().delete_one({"file_code": code})` 删 CRDB，随后发 `RELAY_RENEW`。

**问题**：热路径读取走 `get_file_record_cached()`（内存→SQLite 本地 `file_records`），而这里的删除**既未调 `update_file_record_and_invalidate`、也未删 SQLite 本地表/失效内存缓存**。因此：

- 内存缓存（TTL 内）与 SQLite 本地 `file_records` 仍保留那条 file_ids 为空的陈旧记录；
- 同一个码再次请求时 `get_file_record_cached` 仍命中空记录 → 又走 deliver_cached 空分支 → 反复 RELAY_RENEW，而非真正重新拉取/重建（SQLite 本地表直到下次 upsert 才会被覆盖）。

**建议**：删除路径统一走带缓存失效的封装（同时删 SQLite 本地表 + `get_file_record_cache().invalidate(f"file:{code}")`），确保 CRDB/SQLite/内存三层一致。

### N-L7（⚪ 低危）`enqueue_job` fire-and-forget 任务未保留引用 + 计数器语义混淆

**位置**：`database/session.py::enqueue_job()`。`asyncio.ensure_future(_sync_new_job_to_crdb(...))` 的返回任务**未被任何变量引用**，可能在完成前被 GC（Python 官方建议保留强引用）；且失败仅 `logger.debug` → CRDB `jobs` 审计表可能静默漂移。另 `status_counters["total_files"]/"active_files"` 在**每次派工**时递增，把「派工任务数」当「文件数」统计 → 后台 /status 数值虚高。**建议**：用集合保留任务引用并加完成回调；计数器语义与真实文件/任务区分。

<aside>
🔍

**本轮坐实/强化的旧结论**：

- **N-M12 升级为确定**：`get_active_cells_local()` 末行 `return [c for c ... if c.get("status") == "active"]` 证实它**仅返回 active 单元**，故后台拓扑视图经该 SQLite 快路径必然丢失 shadow/r100/lost。
- **N-M3 落地**：两个 `deliver_cached` 均直接 `send_file(解码Bot, fid)` 发存储 file_id（由用户号 MTProto 生成），跨 Bot 可移植性风险真实存在。
- **N-M8 佐证**：`get_and_reset_dead_jobs` 用 `result.matched_count > 0` 判定是否重置，而 CRDB `update_one` 恒返 1 → 判定恒真。
- **N-M6 佐证**：代码里存在专用的 `set_config_and_invalidate`，说明普通 `set_config` 不失效缓存是已知裂缝，依赖调用方选对函数。
</aside>

<aside>
✅

本轮正向确认：SQLite-first 本地队列设计合理——`dequeue_jobs` 用 CTE 原子取任务 + 5s 超时保护 + 死信重试上限 2 次；`bulk_update_request_counts`/`batch_update_cells_dirty`/`batch_update_jobs_status` 用 CASE WHEN 批量回写有效压 RU（参数下标拼接正确，已逐行核对）；备用池 CRUD 完整；`update_file_record_and_invalidate` 双写对 `$set/$inc/$push` 处理正确（含 list 反序列化）。

</aside>

<aside>
📌

仍待覆盖：`config/{groups.yaml,topology.yaml}`（静态数据）、`admin/templates/*.html`、`admin/migrations/*.sql`、`utils/` 部分未读模块（`admin_notify/file_utils/monitor/storage_channel/time_utils` 等）、部署脚本/Dockerfile/compose、`tests/`。

</aside>

---

## 十四、第七轮深度审查补充（Bot 尾部收束 · 轮转配置键错配 · 用户码管理）

<aside>
🆕

本节已读完：`bots/up_bot.py`（657→尾，外部中继缓冲/flush、应用装配）、`bots/dsp_bot.py`（639→尾，任务循环/启动同步/回写装配）、`bots/idx_bot.py`（679→1429，举报回调 + `我的文件码` 全套管理）、`bots/admin_bot/handlers.py`（679→尾，工厂重置/码路由/限流/备用池/轮转配置命令），并回读 `admin/seed_topology.py`、`config/generate_topology.py`、`services/mon/scheduler.py` 以坐实轮转键与降级/补齐逻辑。新增 1 中危 + 2 低危，并**将 N-L6 升级为确定性功能失效**。

</aside>

### N-M14（🟡 中危，N-L6 的实锤升级）后台 `/rotation_set active_window_size` 写入的键，生成/初始化侧永远读不到 → 活跃窗口调节是静默空操作

**位置**：`bots/admin_bot/handlers.py::rotation_set()` vs `admin/seed_topology.py::seed()` 步骤4 vs `config/generate_topology.py::_load_rotation_from_db_or_env()`。

**问题**：三处对同一「活跃窗口」参数使用了**两个不同的 DB 键**：

- 后台命令：`rotation_set` 取 `key∈{active_window_size, files_per_slot, time_per_slot}` 后统一拼 `db_key = f"rotation_{key}"`，即写入 `rotation_active_window_size`；`rotation_view` 也读 `rotation_active_window_size`（自洽）。
- 生成/初始化：`seed()` 步骤4 写入的键是 **`active_window_size`（无 `rotation_` 前缀）**；`generate._load_rotation_from_db_or_env` 的 `db_keys` 映射同样读 **`active_window_size`（无前缀）**。

逐参数核对：

| 参数 | 后台写/读键 | seed/generate 键 | 是否一致 |
| --- | --- | --- | --- |
| active_window_size | rotation_active_window_size | active_window_size | ❌ 错配 |
| files_per_slot | rotation_files_per_slot | rotation_files_per_slot | ✅ 一致 |
| time_per_slot | rotation_time_per_slot | rotation_time_per_slot | ✅ 一致 |

**净效果**：管理员执行 `/rotation_set active_window_size 5` 只会写 `rotation_active_window_size`，而真正决定「每组几个活跃频道」的拓扑生成器只读 `active_window_size` → **该调节永远不生效**；偏偏 `/rotation_view` 与后台展示层（N-L6）都读带前缀的键,会把这个幽灵值**回显成「已生效」**,形成「看得到、调得动、就是不起作用」的强误导。这也暴露 `seed()` 自身键名不统一——同一函数里 `active_window_size` 无前缀、另两个却带 `rotation_` 前缀。

**建议**：全项目统一为单一键名（推荐 `rotation_active_window_size`），或在 `rotation_set` 对 `active_window_size` 特判去前缀；并加「后台写入键 == 生成器读取键」的回归测试。

### N-L8（⚪ 低危）`我的文件码` 计数增减不对称 → 下架/上架循环后计数长期偏低、分页截断

**位置**：`bots/idx_bot.py::my_code_confirm_status_callback()`。下架时 `decr_user_code_count(user.id, 1)`，但**恢复上架分支没有对应的 `incr`**；而基线 `codes_col.count_documents({"uploader_id": user.id})` 统计的是**全部状态**（含 offline）。因此每经历一次「下架→上架」，`get_user_code_count` 就少 1 且不回补 → `my_codes_command` 的 `total_rows`/`total_pages` 偏小,靠后的文件码可能**无法翻页访问**（直到计数跌到 ≤0 才重新按 `count_documents` 基线化）。**建议**：上架分支对称 `incr_user_code_count`，或计数语义明确「仅活跃码」并让基线查询同样带 `status` 过滤。

### N-L9（⚪ 低危）举报防抖字典无限增长

**位置**：`bots/idx_bot.py::_report_debounce`（模块级 `dict[str,float]`，键为 `{reporter_id}:{file_code}`）。60 秒防抖只写不清理，长期运行会随「举报人×文件码」组合无界增长（轻微内存泄漏）。**建议**：惰性清理过期项或改用带 TTL/上限的结构（如 `cachetools.TTLCache`）。

<aside>
🔍

**本轮坐实/强化的旧结论**：

- **C2（存量补齐空实现）再获实证**：`scheduler._is_channel_nearly_empty` 里 `await get_chat(channel_id)` 后注明「无法迭代消息，保守返回 False」→ **恒返回 False**，`auto_fill_new_channels` 的空频道检测形同虚设，与前述 `_fetch_all_media` 恒返 `[]` 相互印证。
- **N-M11 强化为「任何调用路径都读不到 DB」**：`generate()` 是同步函数、却在 `asyncio.run(seed(...))`（CLI）与 `auto_seed`（启动）两条**均处于运行中事件循环**的路径里被调用，其内部 `asyncio.run(_load())` 必然抛 `RuntimeError` 被吞 → DB 轮转参数在**两条路径下都读不到**；且步骤4 注释写「如果不存在」，代码却**无条件** `set_rotation_config` 覆盖。
- **M2（工厂重置）佐证**：`for table in _FACTORY_RESET_TABLES: if table not in _FACTORY_RESET_TABLES: continue` 是**恒假的自证白名单**（表名本就来自该常量），注释所称「防 SQL 注入」名不副实（当前安全仅因表名为硬编码常量）。
</aside>

<aside>
✅

本轮正向确认：`up_bot` 外部中继缓冲有 `flushed` 幂等标志 + 60s 安全超时兜底,并复用首个文件的存储频道 `channel_id`（PRE-15）避免 `primary_channel_id` 与批量 msg_id 错位；`dsp_bot` 启动即从 CRDB 补齐遗漏 job + 每 6h 轻量同步 + 每 120s 回写 + 多个清理循环,离线韧性设计良好；`idx_bot` 的「我的文件码」全套 CRUD **每一处都用 `find_one({code, uploader_id})` 做属主校验**（无横向越权）,且改动后同时失效内存缓存 + SQLite 持久缓存 + 用户码列表缓存,缓存卫生到位；`scheduler` 降级冷却按 `degrade_count` 阶梯放大（默认→600→1200s）+ `validate_topology` 校验 next 指针有效性/重复/可达性,健壮。

</aside>

<aside>
📌

仍待覆盖：`config/{groups.yaml,topology.yaml}`（静态数据）、`admin/templates/*.html`、`admin/migrations/disable_crdb_ttl.sql`、`utils/` 剩余模块（`admin_notify/file_utils/monitor/shared_counters/storage_channel/time_utils/_fix_encoding`）、`tests/test_idle_ru_regressions.py`、部署脚本/Dockerfile/compose、`test_results.md`、`docs/channel_archiver_design.md`。前七轮的 P0/P1 结论不依赖这些文件。

</aside>

---

## 十五、第八轮深度审查补充（工具模块 · 配置数据 · 测试与部署）

<aside>
🆕

本节已读完：`utils/{shared_counters, admin_notify, storage_channel, time_utils, monitor, file_utils}.py`、`config/groups.yaml`、`docker-compose.yml`、`Dockerfile`、`tests/test_idle_ru_regressions.py`（`utils/_fix_encoding.py` 在当前 master 不存在/404，已从覆盖清单剔除）。新增 1 中危 + 2 低危，并坐实 N-M14、收窄 N-L3/H2 的触发条件。

</aside>

### N-M15（🟡 中危）用户码「基线计数」被写进错误的容器 → `get_user_code_count` 永远读不到基线 → `/my_codes` 计数错误、分页截断、E2「0 RU」优化失效

位置：`utils/shared_counters.py::get_user_code_count()` 与 `bots/idx_bot.py::my_codes_command()`。

问题：

- `get_user_code_count(user_id, base=0)` 只返回 `max(0, base + _user_code_count_delta.get(user_id, 0))`，即「进程内增量 dict + 入参 base」。
- 但 idx_bot 实际调用是 `get_user_code_count(user.id)`，**未传 base**；当结果 ≤0 时它去 `count_documents` 求真实基线，却把基线写进 `status_counters[f"user_code_count:{user.id}"]`——一个 `get_user_code_count` **根本不读**的另一个字典。

净效果：

1. **基线缓存彻底失效**：只要 `delta==0`，每次 `/my_codes` 都重新 `count_documents`（E2 想省的 CRDB RU 并没省下）。
2. **更糟**：用户上传新码后 `incr_user_code_count` 令 `delta=1`，此后 `get_user_code_count` 返回 1（而非「真实基线+1」）→ `total_rows=1` → `total_pages=1` → 拥有 **>12 个码**的用户在 `/my_codes` 里只能看到第 1 页，后面的码**无法翻页访问**（直到 delta 再次跌到 ≤0 才回落 `count_documents` 基线）。

附带：`incr_user_code_count` 的 docstring 写「F1: 同时更新 active_files」，但函数体**并未动** `active_files`；只有 `decr` 递减 `active_files`。于是新码上传不加、下架却减 → 全局 `active_files` 单调下滑并被 `max(0,..)` 夹到 0，`/status` 的 active_files 长期失真（与 N-L7 计数语义混淆同源）。

建议：把 `count_documents` 基线作为 `base` 显式传入（或让 `get_user_code_count` 读取持久化基线）；`incr`/`decr` 对 `active_files` 对称处理；补「上传 N 个码后 `/my_codes` 的 total_pages 正确」的回归测试。

### N-L10（⚪ 低危）`file_utils` 语音消息类型不一致：一处判为 `audio`、另一处判为 `voice`

位置：`utils/file_utils.py`。`detect_file_type()`/`extract_file_meta()` 把 `msg.voice` 归为 `"audio"`，而 `extract_media_info()` 把 `msg.voice` 归为 `"voice"`。同一条语音消息在不同落库/投递路径会得到不同 `type` 字符串，可能造成按类型统计或分支处理不一致。建议统一。

### N-L11（⚪ 低危·加固）容器以 root 运行

位置：`Dockerfile` 未声明 `USER`，运行时以 root 身份跑全部 Bot；`docker-compose.yml` 也未加 `user`/`cap_drop`。admin web 与全部 Bot 同容器，建议增设非 root 用户并最小化权限。

<aside>
🔍

**本轮坐实/修正的旧结论**：

- **N-M14 再获实证**：`config/groups.yaml` 的 `mon` 段键名就是 `active_window_size`（无前缀）、`rotation_files_per_slot`、`rotation_time_per_slot`（有前缀）——「活跃窗口」键**从配置源头起**就与后台 `rotation_` 前缀不一致，坐实错配是贯穿数据源的设计裂缝而非单点笔误。
- **N-L3 触发条件收窄**：`Dockerfile` 用 `python:3.12-slim`，默认容器部署里 `datetime.UTC` 可用，故 N-L3（`datetime.UTC` 需 3.11+）**仅在裸机 Python ≤3.10 运行时才触发**。
- **H2 触发条件收窄**：`docker-compose` 单容器、admin 端口绑 `127.0.0.1:8080`（需反代外露），默认单 worker 部署下「多 worker 状态不共享」不显现；一旦横向扩容/多 worker 即复现。
- **N-M6 佐证**：`utils/storage_channel.set_active_storage_channel_id` 走 `set_config` 且只更新本模块 60s 缓存，未失效 session 层 `get_config` 的 10 分钟缓存 → 跨进程切换主存储频道最长可滞后约 10 分钟。
- **配置漂移（M6 类）**：`groups.yaml` 的 `heartbeat_timeout=90`，而 `scheduler._load_mon_config` 默认 `heartbeat_timeout=240`，二者不一致，实际取值取决于 topology.yaml/DB 覆盖，易误判超时阈值。
</aside>

<aside>
✅

本轮正向确认：`admin_notify.send_to_admin` 明确用 **Admin Bot Token** 发送举报消息，保证 `report:ban/detach/block` 回调落到注册了处理器的 Admin Bot（注释与实现一致，正确规避「按钮点击无反应」）；`storage_channel` 读缓存带 `asyncio.Lock` + 60s TTL + settings 兜底；`monitor` 为单例 `SystemMetrics`、读写计数加锁；`docker-compose` 带健康检查 + admin 端口仅绑 `127.0.0.1` + 多阶段 slim 镜像，工程化良好；`tests/` 对 idle-RU 同步路径（批量 `execute_raw` 回写、只读 pending）有断言保护。

</aside>

<aside>
⚠️

**测试覆盖缺口**：`tests/` 仅 `test_idle_ru_regressions.py` 两个用例，只覆盖 jobs 同步；且其 `_FakeUpdateResult.matched_count` **恒为 1**，等于把 N-M8/N-M5「execute 恒返 1」的错误假设**固化进测试**，无法发现该缺陷。所有 P0/P1 路径（C1/C2/C3 复制与补齐、N-C1 建表顺序、N-C2 备份恢复错配、N-H1 验证码串码、N-H3 路由缓存空、N-H5 重启回滚拓扑、N-M14 轮转键错配）**均无任何测试覆盖**。建议按缺陷清单补关键路径回归测试。

</aside>

<aside>
📌

仍待覆盖：`config/topology.yaml`（运行时生成的静态快照）、`admin/templates/*.html`、`admin/migrations/disable_crdb_ttl.sql`、`docs/channel_archiver_design.md`、`bots/mon_bot.py` 685→708 极尾、`bots/idx_bot.py` 1429→1874 尾段、`bots/{up_bot,dsp_bot}` 前半未逐行段。前八轮的 P0/P1 结论不依赖这些文件。注：`utils/_fix_encoding.py` 在当前 master 不存在（404），已从清单剔除。

</aside>

---

## 十六、第九轮深度审查补充（idx_bot 尾段收束 · CRDB TTL 迁移 · 依赖清单）—— 代码逻辑走查收口

<aside>
🆕

本节已读完：`bots/idx_bot.py`（1429→尾，外部码/媒体路由 + handler 注册 + `_code_changes_sync_loop` 批量回写）、`admin/migrations/disable_crdb_ttl.sql`、`requirements.txt`。**无新增高/中危**，新增 1 低危（N-L12），多项旧结论获终局实证。**至此仓库全部 Python 业务逻辑模块已逐函数走查完毕**，仅剩静态/模板/文档类文件。

</aside>

### N-L12（⚪ 低危·运维）关闭 CRDB TTL 的迁移是手动脚本、未接入自动初始化 → 全新部署仍被昂贵 TTL job 拖累

位置：`admin/migrations/disable_crdb_ttl.sql`。该脚本把 `decode_logs`/`jobs` 的 `ttl_expiration_expression` 改成「+100 年」、`ttl_job_cron` 改 `@yearly`，注释称原 TTL job「每小时消耗数百万 RU」。问题：此脚本**需管理员手动执行**，未接入 `init_db`/DDL 自动应用 → 全新部署若忘记跑，CRDB 行级 TTL job 仍按默认高频运行（注释估计 ~300 万 RU/小时）；且关闭 TTL 后 `decode_logs`/`jobs` 在 CRDB 侧改为「靠 Python 端清理」，需确认确有对应删除逻辑，否则两表在 CRDB 无界增长。建议：将该迁移纳入自动迁移流程（幂等执行）并补 Python 侧过期清理。

<aside>
🔍

**本轮终局坐实的旧结论**：

- **N-H3 三重实证**：`idx_bot.handle_message` 的通配符前缀路由 `routes = await get_all_code_bot_routes()` 依赖同一套恒空缓存 → **通配符匹配是死路**；`handle_external_code` 里 `resolve_bot_for_code(code, bot_username)` 恒返 `default_bot`；即「无头码/前缀路由」在热路径上整体失效，与 N-H3 根因完全一致。
- **C1/C2 根因终局确认**：`requirements.txt` 锁 `python-telegram-bot==21.6`，印证 C2 注释「ptb 21.6 不支持迭代消息」；而依赖里**已含 `telethon==1.38.0`** → 用 MTProto 账号做真实历史遍历复制/补齐「本就可行、只是没用上」，修复 C1/C2 **无需引入新依赖**。
- **质量项确认为真**：`idx_bot` 外部码用户提示存在**源码级截断/串行的中文文案**（如「文件{code} 已缓正在发请查收」「正在查询外部文件请稍候查收」「机器@{bot} 未找请检查…」），用户实际会看到断句 → 坐实第五节「疑似被截断文案」为真实缺陷，建议全量校对用户可见文案。
</aside>

<aside>
✅

本轮正向确认：`_code_changes_sync_loop` 的 CASE WHEN 批量 `UPDATE codes`（code 落 `$1,$3,$5…`、值落 `$2,$4…`、`WHERE code IN(...)` 取奇数位占位符）**参数下标已逐行核对正确**，note/expiry/status 三类分组回写语义正确且有效压 RU；媒体组缓冲 `_media_group_buffer` + `_flush_media_group_buffer` 定时聚合 + `_cleanup_media_groups` 定期清理；关闭前 `sync_quotas_to_crdb` 落盘、`start`/`stop` 生命周期规范；心跳(30s)/清理(60s)/热表增量同步(120s)/decode-log flush/request-count flush/quota-sync 等后台任务齐备，离线-优先设计一致。

</aside>

<aside>
📌

**审查收口说明**：截至第九轮，仓库**全部 Python 业务逻辑模块（5 个 Bot + 中继池 + Mon 调度 + DB/缓存抽象层 + 后台 + 工具层 + 启动编排 + 备份恢复）均已逐函数走查**。剩余未读仅为**静态/模板/文档类**、不含可执行业务逻辑：`config/topology.yaml`（运行时生成的静态快照）、`admin/templates/*.html`（Jinja 展示模板）、`docs/channel_archiver_design.md`（设计文档）、`bots/mon_bot.py` 685→708 极尾、`bots/{up_bot,dsp_bot}` 前半非核心装配段。前九轮的全部 P0/P1/P2 结论完整且稳定，不依赖上述剩余文件。**建议下一步：把本报告 5 严重 / 8 高危 / 20 中危 / 多项低危拆成可跟踪的问题清单（每条含定位/复现步骤/修复补丁），进入修复阶段。**

</aside>

---

## 十七、第十轮深度审查补充（归档器设计文档 · Mon 心跳尾段 · 后台 HTML 模板）—— 全量审查完成

<aside>
🆕

本节已读完：`docs/channel_archiver_design.md`、`bots/mon_bot.py`（640→708，心跳检测/停止/状态日志）、`admin/templates/dashboard.html`。`base.html`/`login.html` 在 master 不存在（404，模板为独立整页、无 `{% extends %}`）。**无新增高/中危**，新增 1 项设计级低危（N-L13），确认 Web 看板层无明显 XSS。**至此仓库全部可获取文件审查完成。**

</aside>

### N-L13（⚪ 低危·设计文档）归档器（Channel Archiver）设计沿用 Bot API `get_history` 拉历史 → 与 C1/C2 同根，按现有 ptb 依赖不可实现

位置：`docs/channel_archiver_design.md` 阶段①「频道同步」与 §5.2「关键 API 调用」。文档把「频道历史遍历」建立在 `get_history`/`forward_message` 上，且技术选型明确写「Bot 框架 = python-telegram-bot」。但如 C1/C2 及第九轮所证，**ptb 21.6 无法迭代频道历史消息**（`_fetch_all_media` 恒返空即因此）。因此该子项目若真按文档用 ptb 实现，其最核心的「历史归档」会与主系统的影子复制/存量补齐一样落空。建议：归档器改用 **Telethon**（项目已依赖 `1.38.0`）做历史遍历，并在文档中修正技术选型。注：文档描述的 `archiver/` 子项目在当前 master **尚未落地代码**，故为设计级提示、不计入运行时缺陷计数。

<aside>
🔍

**本轮坐实的旧结论**：

- **系统性误区第三次印证**：团队反复把「历史同步」架在 Bot API 上——C1 用 `get_updates`、C2 是空实现、本归档器设计又用 `get_history`。三处同根，修复方向统一为 **Telethon/MTProto** 历史遍历。
- **mon_bot 尾段坐实零-RU 心跳**：心跳检测 `get_chat` 成功即 `write_heartbeat(ok=True)` **仅写本地 SQLite、零 RU**，与「SQLite-first 省 RU」策略一致；封禁错误经 `_is_ban_error` → `_handle_channel_ban`，普通错误（flood）仅记 `_cell_fail_streak` 不降级——降级判定确实不读这里的内存 streak，与 **N-M1**（冷却读已停写的 `last_heartbeat` 字段）互相呼应。
</aside>

<aside>
✅

本轮正向确认：`admin/templates/dashboard.html` 为**独立整页模板**（无 base 继承），全部动态值走 Jinja  `变量`  表达式（FastAPI Jinja2 默认 **autoescape 开启**）、未见 `| safe` 过滤器，看板页无明显 XSS 面；`mon._report_status` 仅从内存缓存读拓扑健康、不查 DB。

</aside>

<aside>
🏁

**审查完成声明**：截至第十轮，`maxiuquan/tgjiema` 仓库中**所有可获取的文件均已审查完毕**——全部 Python 业务逻辑模块逐函数走查，配置（YAML）、SQL 迁移、依赖清单、docker-compose/Dockerfile、测试、设计文档、后台 HTML 看板模板均已覆盖。仅以下项无法核对（非本仓库可读缺陷）：`config/topology.yaml` 为运行时生成的实例私有快照（不含逻辑）；后台 `users/files/logs` 列表页模板以常见文件名抓取均 404（可能内联在 Python 或命名不同）——因此这些列表页对**用户可控字段（用户名/文件码/caption）的 XSS 只能确认「dashboard 已 autoescape」**，建议自查其余列表页是否统一 autoescape、杜绝 `| safe`。

</aside>

### 最终结论

本次十轮审查累计 **🔴 5 严重 / 🟠 8 高危 / 🟡 20 中危 / ⚪ 多项低危**。核心裂缝稳定收敛为三条主线：

1. **架构承诺 vs 实际实现的最大裂缝**：影子频道冗余复制（C1 `get_updates` 不可靠 / C2 空实现 / C3 投递 404）+ 备份恢复与 schema 全面错配致 DR 失效（N-C2）+ 全新部署建表顺序致初始化失败（N-C1）。宣称的「3× 冗余、可恢复」在历史文件层面并未真正建立。
2. **运维/安全高危**：后台 CSRF 兜底虚设（H1）、限流/CSRF/会话仅进程内内存（H2）、配置缓存 `^` 正则致路由与限流永空（N-H3）、重启回滚运行时拓扑（N-H5）、多账号验证码串码（N-H1）。
3. **数据一致性中危群**：`execute` 恒返 1（N-M5/N-M8）、config/存储频道缓存不失效（N-M6）、轮转键错配（N-M14）、用户码计数容器错放（N-M15）、缓存不一致致 RENEW 空转（N-M13）等。

**建议进入修复阶段**：将上述全部条目转为可跟踪问题清单（每条含「文件定位 + 复现步骤 + 修复补丁 + 回归测试」），按 P0（C1/C2/C3/N-C1/N-C2）→ P1（H1/H2/N-H3/N-H5/H3/H4/N-H1/N-H2）→ P2/P3 顺序推进；并优先补齐 P0/P1 路径的自动化回归测试（当前测试甚至把「execute 恒返 1」的错误假设固化进 mock）。

[tgjiema 修复跟踪清单（可勾选）](tgjiema%20%E4%BF%AE%E5%A4%8D%E8%B7%9F%E8%B8%AA%E6%B8%85%E5%8D%95%EF%BC%88%E5%8F%AF%E5%8B%BE%E9%80%89%EF%BC%89%205765fbeabb6a4d2a90c871572fdc7fa6.md)

---

## 十八、修复复查（第十一轮）—— 逐条核验修复效果 + 新发现问题

<aside>
🔬

本轮针对你「已按清单修复」的说明,重新拉取 master **当前**源码,对 P0/P1 及重点 P2 逐条核验,并审查修复是否引入新问题。**总体结论**:确有一批关键项修好了(N-C1/N-C2/H1/H3/H4/N-H3/N-H5/M1/M7/N-M5·8·10/N-M11/N-M14 等),但 **P0 架构核心 C1/C2 仍未改动**,且发现 **2 个由「修复」本身引入的新问题**——其中 **F-5 直接导致所有中继账号无法登录**。

</aside>

### 18.1 修复核验结果一览

| 编号 | 问题 | 复查结论 | 证据（master 当前源码） |
| --- | --- | --- | --- |
| C1 | 影子复制依赖 get_updates | ❌ 未修 | `scheduler._fetch_new_messages` 仍 `get_updates(offset=-100)`,注释仍写「使用 getUpdates API」 |
| C2 | 存量补齐空实现 | ❌ 未修 | `_fetch_all_media` 仍 `return []`；`_is_channel_nearly_empty` 仍恒 `return False` |
| C3 | 切换后投递 404 | ✅ 已修（留隐患 F-2） | `_process_single_job` 环形降级(≤10) + 耗尽后回退原始存储频道 `job.storage_channel_id` |
| N-C1 | 建表顺序致初始化失败 | ✅ 已修 | 三条 `updated_at` 索引已移入带 try/except 的 `MIGRATION_STATEMENTS` |
| N-C2 | 备份恢复与 schema 错配 | ✅ 已修 | `db_restore` 重写:PK(config_key/channel_id/code_prefix/external_code)、列/表白名单全部对齐、去除不存在的 kv_config、改 ON CONFLICT upsert |
| H1 | CSRF 兜底虚设 | ✅ 已修 | `_verify_csrf` 删兜底,强制 `cookie∈_csrf_tokens.values()` 且 `cookie==form` |
| H2 | 限流/CSRF 仅进程内 | ❌ 未修 | `_login_failures`/`_csrf_tokens`/`_count_cache` 仍为模块级 dict,多 worker 不共享 |
| H3 | 动态加中继账号不启动 | ✅ 已修 | `relay_pool.add_account` 内已 `await instance.start()` |
| H4 | 外部码配额提前扣减 | ✅ 已修 | `check_decode_permission` 移除提前 `increment_user_quota_used`,改由投递成功后计费 |
| N-H1 | 多账号验证码串码 | 🔴 修复引入回归(F-5) | 中继侧读 `relay_auth_code:{phone}`,但管理端 `/relay_code` 仍写全局 `relay_auth_code` → 键名不一致 |
| N-H2 | 中继双实现分叉 | ❓ 未能确认 | `user_relay.py` 本轮抓取超时(504);`relay_pool` 仅实例化 `RelayInstance` |
| N-H3 | 配置缓存 ^ 正则致路由空 | ✅ 已修 | `_refresh_bot_config_cache` 去掉 `^`,改 `$regex:"code_bot_route:"` |
| N-H5 | 重启回滚运行时拓扑 | ✅ 已修 | `auto_seed` 仅 `cell_count==0` 才写;`seed` 对已存在槽位 skip 不覆盖 |
| M1 | 降级冷却读已停写字段 | ✅ 已修 | `run_degrade_check(all_cells, cell_fail_streak)` 改用内存 fail_streak |
| M2 | 工厂重置遗漏表 | ❌ 未修(F-4) | `_FACTORY_RESET_TABLES` 仅 5 表,文案却宣称清空 codes/external_code_mapping/jobs/spare_pool 共 9 表 |
| M7 | 后台 count 每次走 CRDB | ✅ 已修 | `/users`·`/files`·`/logs` 无筛选时走 60s `_count_cache` |
| N-M5/8/10 | execute/update_one/delete_one 恒成功 | ✅ 已修 | 均解析 asyncpg `"UPDATE/DELETE N"` 回填真实计数;`UpdateResult(matched_count)` |
| N-M6 | config 缓存不失效 | ⚠️ 仅部分修(F-1) | `set_config`/`delete_config` 失效了 L1 内存,但未失效 L2 SQLite 兜底 |
| N-M7 | $regex 未转义通配符 | ❌ 未修 | 后台搜索仍 `{"$regex": search}`,`%`/`_` 未转义 |
| N-M11 | 覆盖轮转配置+嵌套 [asyncio.run](http://asyncio.run) | ✅ 已修 | 轮转配置仅 `existing_val is None` 才写;`generate()` 直接调用不再嵌套 run |
| N-M14 | 轮转键名错配 | ✅ 已修 | `seed` 写入带前缀的 `rotation_active_window_size`,与后台键统一 |
| N-M1 | send_external_code busy 过早释放 | ✅ 已修（留隐患 F-3） | busy 现覆盖到 `_process_all_collected`/`_cleanup_exchange` 结束 |

### 18.2 🔴 修复过程中引入的新问题（回归）

#### F-5（🔴 严重·回归）中继验证码键名两侧不一致 → 所有中继账号永久无法登录

**位置**：`services/relay_instance.py::_wait_for_admin_code()` ↔ `bots/admin_bot/handlers.py::relay_code()`。

**问题**：为修 N-H1,中继侧把验证码键改成了**按手机号隔离**:

```python
# relay_instance.py（读）
code = await get_config(f"relay_auth_code:{self.phone}")
```

但管理端提交验证码的命令**仍写全局键、且不带手机号**:

```python
# admin_bot/handlers.py（写）
await set_config("relay_auth_code", code)   # ← 没有 :{phone}
```

两侧键名不再匹配 → `_wait_for_admin_code` 轮询 100 次(5 分钟)永远读不到 → **任何中继账号(单个或多个)首次授权/掉线重登都会超时失败**。这比原 N-H1(多账号串码,单账号仍可用)更严重:现在**连单账号都登不上**。且 `/relay_code` 未接收手机号参数,多账号待授权时管理员也无法指定目标。

**修复**：`/relay_code` 改为 `/relay_code <手机号> <验证码>` 并写 `relay_auth_code:{phone}`;或提供「当前待授权手机号」列表让管理员选择;务必保证**写入键 == 中继读取键**并补回归测试。

#### F-3（🟠 高危·回归）`is_busy` 在异常/占用早退路径未复位 → 中继账号永久 busy

**位置**：`services/relay_instance.py::send_external_code()` / `_do_send_external_code()`。

**问题**：`send_external_code` 在锁内置 `self.is_busy = True` 后调用 `_do_send_external_code`;而后者有两条**提前 return False** 的分支**都不复位 is_busy**:

```python
if key in self._bot_exchange:      # bot 被占用
    return False                   # ← is_busy 仍为 True
...
except Exception as e:             # get_entity/send_message 抛异常
    return False                   # ← is_busy 仍为 True
```

只有**成功路径**才会经 `_process_all_collected`/`_cleanup_exchange` 复位 `is_busy=False`。因此一旦 `get_entity`/`send_message` 抛异常(网络抖动、目标 bot 不可达等常见情况),该中继实例 `is_busy` **永久为 True**,此后 `send_external_code` 首个 `if self.is_busy: return False` 会**拒绝该账号后续所有请求**,直到进程重启。

**修复**：用 `try/finally` 或在两条早退分支显式 `self.is_busy = False`(注意成功建立 exchange 后才交由清理路径复位)。

### 18.3 仍未修复 / 需继续推进

<aside>
⛔

- **C1 / C2（🔴 P0 架构核心,未动）**:影子复制仍靠 `get_updates`、存量补齐仍空实现。依赖清单已含 `telethon==1.38.0`,改用 MTProto 历史遍历**无需引入新依赖**——这是整个「3× 冗余」承诺能否成立的根,建议最优先。C3 虽已加兜底,但根因未除时其可靠性仍受限(见 F-2)。
- **H2（🟠）**:后台限流/CSRF 仍进程内,多 worker 需共享存储或强制单 worker。
- **M2 / F-4（🟡）**:工厂重置实际清表与文案不符,会留悬挂数据。
- **N-M6 二级缓存(F-1)、N-M7($regex 转义)**:仍需补。
</aside>

### 18.4 本轮延伸审查的其他新发现

- **F-1（🟡 中危）config 二级缓存未失效**：`set_config`/`delete_config` 只调 `get_config_cache().invalidate()` 失效 L1 内存,但 `get_config_cached` 的 **L2(SQLite `store`)** 未被失效。改配置后 L1 miss → L2 命中旧值直接返回,配置更新在本进程可能长期不生效。建议 set/delete 时一并 `store.set(cache_key, val)` 或删除 L2 条目。
- **F-2（🟡 中危·潜在)环形降级复用同一 msg_id 跨频道 copy**：`_process_single_job` 环形降级用**同一个 `msg_id`** 去 shadow/降级频道 `copy_message`。但 `copy_message` 在目标频道会生成**全新且不同的 msg_id**,shadow 频道里同一 `msg_id` 对应的并非同一文件(甚至不存在)→ 可能**投递错文件或 404**。唯一可靠的是「回退原始存储频道」那一条。根因与 N-M3(msg_id/file_id 跨频道不可移植)同源;在 C1/C2 未修、shadow 实际为空时该路径尚不致害,一旦 C1/C2 修好、shadow 有内容,此隐患会显性化。
- **F-6（⚪ 低危)db_restore 显式插入 SERIAL id**：`restore_table` 仅对 `decode_logs.id` 排除,`jobs`/`rotate_log`/`pending_uploads` 的 SERIAL `id` 仍按备份值显式插入,恢复后自增序列可能未推进 → 后续 `INSERT` 有主键冲突风险。建议恢复后 `setval` 序列,或统一排除 SERIAL 列由库端生成。

### 18.5 修复进度小结（复查后）

<aside>
📊

**P0 严重(5)**:✅ 已修 3(C3/N-C1/N-C2) · ❌ 未修 2(**C1/C2**)。

**P1 高危(8)**:✅ 已修 5(H1/H3/H4/N-H3/N-H5) · ❌ 未修 1(H2) · ❓ 待确认 1(N-H2) · 🔴 **回归 1(N-H1→F-5)**。

**P2 抽查**:✅ 多数已修(M1/M7/N-M5·8·10/N-M11/N-M14/N-M1) · ⚠️ 部分(N-M6) · ❌ 未修(M2/N-M7)。

**新增回归/发现**:🔴 F-5(中继登录全断) · 🟠 F-3(账号永久 busy) · 🟡 F-1(config L2 陈旧)/F-2(msg_id 跨频道)/F-4(=M2) · ⚪ F-6(SERIAL 序列)。

**下一步优先级**:①F-5(一行键名即修,却阻断整个中继)→ ②C1/C2(用 Telethon 落地真复制)→ ③F-3 → ④H2/M2/N-M6/N-M7。原始清单上方「一、结论速览」的计数为**修复前基线**,以本节为最新状态。

</aside>