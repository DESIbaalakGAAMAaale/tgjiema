# tgjiema 修复跟踪清单（可勾选）

<aside>
🧭

本清单由《tgjiema 项目全面代码审查报告》十轮结论转化而来，按 **P0→P3** 优先级排序，每条含「定位 / 复现 / 修复 / 回归测试」。勾选框可直接用于跟踪修复进度。累计 **5 严重 / 8 高危 / ~21 中危 / 多项低危**。

</aside>

## P0 · 严重（阻断核心架构承诺，须最先修）

- [ ]  **[C1] 影子频道复制依赖 `get_updates`，不可靠**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`services/mon/scheduler.py::_fetch_new_messages()`（`replicate_all_active_to_shadows` 调用）。
**复现**：向 Active 频道持续投递新文件，观察 Shadow 频道长期缺失这些消息（`get_updates(offset=-100)` 无法回溯历史、消费即失）。
**修复**：改用 Telethon（已依赖 1.38.0）`iter_messages(min_id=游标)` 复制，或在 Up Bot 落频道那一刻就 fan-out 到影子频道并记录 message_id 映射。
**回归**：新增「Active 写入 N 条 → Shadow 应含同 N 条且 msg_id 映射存在」的集成测试。

</details>

- [ ]  **[C2] 新频道存量补齐是空实现（stub）**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`services/mon/scheduler.py::_fetch_all_media()`（恒返 `[]`）、`_is_channel_nearly_empty()`（恒返 `False`）。
**复现**：触发备用池替换/`lost` 恢复/影子晋升，新频道不会获得任何历史文件。
**修复**：用 Telethon 历史遍历实现补齐；实现前不对外宣称「自动补齐」。
**回归**：「空频道纳入后应补齐到基准文件数」测试。

</details>

- [ ]  **[C3] 故障切换/轮转后投递命中空频道（404）**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`storage/delivery_resolver.py`（环形游走/降级跳转）、`bots/dsp_bot.py`（投递回退）。
**复现**：对历史文件码，在其频道被封禁替换/降级/轮转后取件 → 命中不存在的 msg_id 失败。
**修复**：投递前校验「目标频道确有该 msg_id」并加跨频道兜底；根因仍需修 C1/C2。
**回归**：「频道替换后老文件码仍可取件」测试。

</details>

- [ ]  **[N-C1] DDL 建索引早于建表/加列 → 全新部署初始化失败**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`database/session.py` `DDL_STATEMENTS`（`connect()` 内循环无 try/except）中 `idx_file_records_updated_at` / `idx_codes_updated_at` / `idx_external_code_mapping_updated_at`。
**复现**：在**全新空库**执行 `init_db`，建索引时列/表尚不存在 → 抛错中断，服务起不来（已初始化实例因 DDL_VERSION 缓存跳过，故隐蔽）。
**修复**：把这三条 `updated_at` 索引移到 `MIGRATION_STATEMENTS`（已有容错），或调序为「先建表 → 再加列 → 最后建索引」，并给 DDL 主循环加容错日志。
**回归**：CI 用干净数据库跑一次完整 `init_db`。

</details>

- [ ]  **[N-C2] 备份/恢复主键·列·表白名单与真实 schema 大面积错配 → DR 失效**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`services/db_restore.py` `TABLE_PK`/`_ALLOWED_COLUMNS`/`_ALLOWED_TABLES` vs `database/session.py` DDL。
**复现**：从 R2 备份恢复，`users`(can_upload)、`cells`(demoted_to_channel_id 等)、`backup_config`(PK 应为 config_key)、`spare_pool`(PK channel_id)、`relay_accounts`(api_hash)、`code_bot_mapping`(PK code_prefix)、`rotation_config`/`external_code_mapping`、`kv_config`(表不存在) 等整表被跳过/报错，实际恢复行数≈0。
**修复**：按真实 DDL 重建三份白名单/主键映射，或基于 schema 元信息自动生成列映射。
**回归**：单测断言「每张备份表的列∈白名单且主键存在」+ 备份→恢复往返行数一致。

</details>

## P1 · 高危

- [ ]  **[H1] 后台 CSRF 兜底逻辑虚设**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`admin/__init__.py::_verify_csrf()` 兜底分支 `if form_token in _csrf_tokens.values(): return True`。
**复现**：服务重启后 cookie 失效，仅凭 form_token 命中即放行，绕过双提交校验（Basic Auth 下浏览器自动带凭证，CSRF 面被放大）。
**修复**：删除兜底分支，始终强制 `cookie_token == form_token` 且 cookie 已注册。
**回归**：「cookie 与 form token 不一致必须 403」测试。

</details>

- [ ]  **[H2] 登录限流/CSRF/会话仅进程内内存**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`admin/__init__.py` 模块级 `_login_failures`、`_csrf_tokens`。
**复现**：多 worker/多副本下，失败计数可绕过、CSRF token 跨进程不一致。
**修复**：状态放共享存储（SQLite/Redis），或强制单 worker 并在文档标注。
**回归**：多 worker 集成测试或部署约束校验。

</details>

- [ ]  **[H3] 动态加中继账号不生效 + 两入口行为不一致**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`services/relay_pool.py::add_account()`（`/relay_add` 仅 append 不 start）vs `conversation.py::relay_set_api`（会 login）。
**复现**：用 `/relay_add` 加账号，`is_ready` 恒 False，要整机重启才生效（与提示文案矛盾）。
**修复**：`add_account` 内 `await instance.start()`，统一两入口行为与提示。
**回归**：「/relay_add 后账号 is_ready 应为 True」测试。

</details>

- [ ]  **[H4] 外部码配额在投递成功前就扣减**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`services/permission.py::check_decode_permission()` + `bots/idx_bot.py` 外部码链路。
**复现**：外部码走中继失败（RELAY_ERROR/超时），用户次数已扣、文件未到。
**修复**：失败路径回补配额，或改为投递成功后计费。
**回归**：「外部码失败 → 配额不变」测试。

</details>

- [ ]  **[N-H1] 多中继账号共用同一验证码全局键 → 并发登录串码**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`services/relay_instance.py`/`user_relay.py::_wait_for_admin_code()` 读同一 `relay_auth_code`/`relay_auth_pending`。
**复现**：两账号同时登录，管理员提交的验证码被错误实例消费。
**修复**：验证码键按账号隔离（`relay_auth_code:{phone}`），`/relay_code` 携带目标账号标识。
**回归**：「两账号并发登录各自取到正确验证码」测试。

</details>

- [ ]  **[N-H2] 中继两套实现分叉（UserRelay / RelayInstance）**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`services/user_relay.py`（~1233 行）与 `services/relay_instance.py`（~835 行）handler/决策逻辑高度重复但落库行为不一致。
**复现**：若 UserRelay 被 start，同一外部码可能被处理两次（既进存储频道又进 Up Bot）。
**修复**：抽共享基类/mixin，UserRelay 降为薄封装或删除，不注册自己的 handler。
**回归**：「同一外部码只被处理一次」测试。

</details>

- [ ]  **[N-H3] 配置缓存用 `^` 正则但底层当 LIKE 字面 → 路由/限流缓存永空**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`database/session.py::_refresh_bot_config_cache()` 的 `$regex "^code_bot_route:"` / `"^bot_decode_interval:"`（被翻成 `LIKE '%^...%'`）。
**复现**：设置非默认 Bot 路由/按 Bot 限速，10 分钟 TTL 刷新后被空结果覆盖；`resolve_bot_for_code` 恒返 default，`get_bot_decode_interval` 恒 0；idx_bot 通配符前缀路由（`get_all_code_bot_routes`）恒空为死路。
**修复**：去掉 `^`（用 `code_bot_route:`）或加专用 `LIKE 'prefix%'` 前缀查询；并为 `$regex→LIKE` 做通配符转义。
**回归**：「写入路由后 resolve_bot_for_code 返回对应 Bot」测试。

</details>

- [ ]  **[N-H5] 每次启动 seed 把 cells 运行时状态回滚到 topology.yaml 初始配置**

<details><summary>定位 / 复现 / 修复 / 回归</summary>

**定位**：`run_all.py::main()` 无条件 `_auto_seed()` → `admin/seed_topology.py::seed(force=True)` 对已存在槽位 `update_one($set)` 覆盖 status/channel_id/next_active_chat_id 等。
**复现**：轮转/降级/备用池替换后重启 → 运行时状态被抹回创世配置，甚至复活已封频道。
**修复**：`auto_seed` 仅在 cells 为空时写入；已存在槽位只补缺失字段，绝不覆盖运行时字段。
**回归**：「改运行时状态 → 重启 → 状态不变」测试。

</details>

## P2 · 中危

- [ ]  **[M1] 降级冷却读已停写的 `last_heartbeat`** → 改读心跳表最近成功时间/内存时间戳。位置 `scheduler.py::run_degrade_check()`。
- [ ]  **[M2] `factory_reset` 清表不完整** → 覆盖 `codes`/`jobs`/`spare_pool`/外部码映射或文档说明保留项。位置 `admin_bot/handlers.py::_FACTORY_RESET_TABLES`。
- [ ]  **[M3] 确定性 ID 仅消费约 60bit 熵** → 下调「数学保证唯一」宣称，加 DB 唯一约束+重试。位置 `services/code_generator.py`。
- [ ]  **[M4] 时间 naive/aware 混用** → 全项目统一 timezone-aware UTC。位置 `database/relay_db.py` 等。
- [ ]  **[M5 / N-M5 / N-M8] `D1Collection` 写操作恒返 1，`matched_count`/`rowcount` 失真** → 解析 asyncpg 状态串（`UPDATE n`/`DELETE n`）回填真实计数。位置 `database/session.py::_execute`/`update_one`/`delete_one`。
- [ ]  **[M6] 配置默认值与 `.env.example` 漂移**（如 `DB_BACKUP_INTERVAL_MINUTES` 360 vs 60；`heartbeat_timeout` 90 vs 240）→ 对齐并去掉「已对齐」误导注释。
- [ ]  **[M7] 后台列表页每次 `count_documents`+正则走 CRDB** → 走本地快照/加索引/限制搜索。位置 `admin/__init__.py` `/users`·`/files`·`/logs`。
- [ ]  **[N-M1] `send_external_code` 并发保护过早释放** → busy 覆盖整段 exchange 或用信号量限并发。位置 `relay_instance.py`。
- [ ]  **[N-M2] 冷却等待持锁 sleep 阻塞整个实例** → sleep 移出锁，或按 `bot_username` 粒度冷却。位置 `relay_instance.py::send_external_code`。
- [ ]  **[N-M3] 跨账号 MTProto 生成的 file_id 可移植性** → 核实投递侧用法；改存 `chat_id+message_id` 由投递 Bot `copy_message`。位置 `user_relay.py::_extract_file_id`。
- [ ]  **[N-M4] 通知表「删除即消费」多消费者丢通知** → 通知带 consumer 维度或改「标记已读」。位置 `cache_store.py::has_new_upload/has_new_dsp_job`。
- [ ]  **[N-M6] `set_config`/`delete_config` 不失效 `get_config` 缓存** → 主动失效/回写并考虑跨进程通知。位置 `database/session.py`。
- [ ]  **[N-M7] `$regex→LIKE %v%` 未转义通配符** → 对 `%`/`_`/`\\` 转义并加 `ESCAPE`。位置 `session.py::find`/`count_documents`。
- [ ]  **[N-M9] 备份 JSON 含明文密钥上传 R2**（`r2_secret_key`/token/`api_hash`）→ 敏感列加密（Fernet）或排除，确保桶私有。位置 `services/db_backup.py`。
- [ ]  **[N-M10] 删不存在文件误报「已删除」**（N-M8 后果）→ 修 N-M8 后自愈。位置 `conversation.py` `delete_file`。
- [ ]  **[N-M11] 启动覆盖 DB 轮转配置 + `generate` 内嵌 `asyncio.run` 抛错被吞** → seed 步骤4 仅当不存在时初始化；generate 避免嵌套 run（复用当前 loop）。位置 `seed_topology.py`/`generate_topology.py`。
- [ ]  **[N-M12] 后台 `_get_cells_cached` 60s 缓存忽略过滤条件，拓扑/概览串 scope** → 缓存键纳入 `status_filter`/`sort_key`。位置 `admin_bot/display.py`。
- [ ]  **[N-M13] `deliver_cached` 删过期记录只删 CRDB 不失效缓存 → RENEW 空转** → 统一走带缓存失效封装（删 SQLite 本地表 + 失效内存）。位置 `user_relay.py`/`relay_instance.py::deliver_cached`。
- [ ]  **[N-M14] `/rotation_set active_window_size` 写 `rotation_active_window_size`，生成器只读 `active_window_size` → 调节永不生效** → 统一键名（推荐带前缀）+ 回归测试「后台写入键==生成器读取键」。位置 `handlers.py`/`seed_topology.py`/`generate_topology.py`/`groups.yaml`。
- [ ]  **[N-M15] 用户码基线计数写入错误容器 → `/my_codes` 计数错误、分页截断、E2「0 RU」失效** → 把 `count_documents` 基线显式作 `base` 传入；`incr`/`decr` 对 `active_files` 对称。位置 `utils/shared_counters.py`/`idx_bot.py::my_codes_command`。

## P3 · 低危 / 质量

- [ ]  **[N-L1] CF Worker Webhook 未校验 `X-Telegram-Bot-Api-Secret-Token`** → setWebhook 设 secret_token 并在入口校验。`cf-workers/file-bot/src/index.js`。
- [ ]  **[N-L2] DDL/迁移 `ADD COLUMN` 混用裸/IF NOT EXISTS + 靠 try/except 吞异常** → 区分「预期已存在」与「真异常」；标注 `statement_cache_size` 直连要求。
- [ ]  **[N-L3] `datetime.UTC` 仅 3.11+**（裸机 ≤3.10 触发）→ 统一 `timezone.utc`。`admin_bot/callback.py`。
- [ ]  **[N-L4] SQL 列/键名未参数化（仅值参数化）** → 对标识符统一白名单校验。`D1Collection`。
- [ ]  **[N-L5] cells 版本号用毫秒时间戳，同毫秒多改漏感知** → 改单调递增计数器。`cache_store.py`。
- [ ]  **[N-L7] `enqueue_job` fire-and-forget 未保留任务引用 + 计数语义混淆** → 保留引用+完成回调；区分任务数与文件数。`session.py`。
- [ ]  **[N-L8] 「我的文件码」上架无对称 `incr` → 计数偏低、分页截断** → 上架分支补 `incr_user_code_count` 或基线查询带 status 过滤。`idx_bot.py`。
- [ ]  **[N-L9] 举报防抖字典无限增长** → 惰性清理/TTLCache。`idx_bot.py::_report_debounce`。
- [ ]  **[N-L10] `file_utils` voice 类型不一致（audio vs voice）** → 统一映射。
- [ ]  **[N-L11] 容器以 root 运行** → Dockerfile 加非 root `USER`，compose 加 `cap_drop`。
- [ ]  **[N-L12] 关闭 CRDB TTL 的迁移是手动脚本未自动化** → 纳入幂等自动迁移 + 补 Python 侧过期清理。`admin/migrations/disable_crdb_ttl.sql`。
- [ ]  **[N-L13] 归档器设计用 Bot API `get_history`（不可行）** → 文档改用 Telethon。`docs/channel_archiver_design.md`。
- [ ]  **[质量] 密钥经 Telegram 明文回显 / 私有方法越界调用 / 大量 `except Exception: pass` / `force_join` fail-closed / 中文文案源码级截断 / 测试覆盖缺口（仅 2 用例且把 execute 恒返 1 固化进 mock）** → 逐项收敛，优先补 P0/P1 路径回归测试。

---

<aside>
✅

全部条目源自十轮逐函数审查，覆盖仓库所有可获取文件。修复建议顺序：P0（C1/C2/C3/N-C1/N-C2）→ P1 → P2 → P3，并优先补齐 P0/P1 路径的自动化回归测试。

</aside>