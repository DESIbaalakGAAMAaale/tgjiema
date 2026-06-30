# TG文件传输系统 — 详细测试计划

## 环境信息
- VPS: tgjiema
- 数据库: CockroachDB + SQLite 缓存
- 部署方式: systemd 独立服务（7 个单元: up/idx/dsp/mon/admin_bot）
- 独立部署: Cloudflare Workers file_bot
- 5 个 Bot: Up Bot, Idx Bot, Dsp Bot, Mon Bot, Admin Bot

---

## 一、Up Bot（上传机器人）

### 1.1 单次上传流程
- [ ] 向 @UpBot 发送单个文档文件
- [ ] 文件被转发到 Active 槽位频道
- [ ] 弹出有效期选择按钮（∞/1天/7天/30天/90天）
- [ ] 选择有效期后弹出转发权限按钮（禁止转发/允许转发）
- [ ] 选择完成后写入 pending_uploads 表
- [ ] Idx Bot 轮询到后生成文件码
- [ ] 用户收到文件码消息
- [ ] 文件码格式符合: `{PREFIX}_{hash}_{ttl}p_{protect}`

### 1.2 批量上传流程
- [ ] 发送 `/start_upload` 进入批次模式
- [ ] 发送多个文件，每个文件返回"已接收{类型}"确认
- [ ] 发送 `/note 备注内容` 设置批次备注
- [ ] 发送媒体组（多图/多视频）自动聚合
- [ ] 发送 `/end_upload` 结束批次
- [ ] 返回汇总信息: "{N}个文件已接收，文件码将@IdxBot发送"
- [ ] 取消批次: `/cancel_upload`

### 1.3 批次上传进度显示
- [ ] 媒体组上传时显示"正在处理 N 个文件...已完成 X/N"
- [ ] 每 3 个文件或最后一个文件更新一次进度
- [ ] 部分文件失败时显示"（其中 X 个文件处理失败）"

### 1.4 权限控制
- [ ] 未授权用户发送文件返回"您没有上传权限"
- [ ] 被 ban 用户无法上传
- [ ] 上传频率过高返回"操作过于频繁,请稍后重试"

### 1.5 强制加群
- [ ] `FORCE_JOIN_CHANNEL_ID=0` 时跳过检查
- [ ] 设置频道后未加入用户被拒绝并显示加入链接
- [ ] 加入后自动恢复会话

### 1.6 速率限制
- [ ] 全局速率限制: 30次/秒
- [ ] 用户级别速率限制: 10次/分钟
- [ ] 超限返回友好提示

### 1.7 外部中继文件
- [ ] 接收 `EXTERNAL_RELAY:{user_id}:{external_code}` 格式文件
- [ ] 文件累积到存储频道
- [ ] 接收 `EXTERNAL_DONE:` 信号后批量写入 pending_uploads
- [ ] 60 秒超时自动 flush
- [ ] 防止重复 flush（safe_mode 竞争保护）

### 1.8 槽位轮转
- [ ] 文件轮转到当前 Active 窗口内的 3 个 A 槽
- [ ] round-robin 均匀分发
- [ ] 槽位刷新异常时回退到 MAIN_STORAGE_CHANNEL_ID

### 1.9 超时清理
- [ ] pending media group 超过 30 秒自动清理
- [ ] external buffer 超过 120 秒自动清理
- [ ] 清理时记录 WARNING 日志

---

## 二、Idx Bot（解码机器人）

### 2.1 内部码解码
- [ ] 发送文件码（含 FILE_CODE_PREFIX）
- [ ] 检查用户配额（免费 3 次/天，基础 20 次/天，高级无限）
- [ ] 检查外部码配额
- [ ] 检查文件码是否过期
- [ ] 检查文件是否被举报脱钩（status=detached）
- [ ] 检查用户是否被限制（blocked_users）
- [ ] 写入 jobs 表派发
- [ ] 返回"文件将由@DspBot发送给你"
- [ ] 附带举报按钮

### 2.2 外部码解码
- [ ] 发送格式: `{bot_username}:{code}`
- [ ] 通配符前缀匹配路由到正确 bot
- [ ] 无头码缓存命中自动路由
- [ ] 检查 bot 间间隔（per-bot interval）
- [ ] 通过中继池发送请求
- [ ] 排队等待中继返回结果
- [ ] 返回"正在查询外部文件请稍候查收"

### 2.3 外部码映射
- [ ] 外部码映射到系统码后直接走本地解码
- [ ] 映射记录持久化到 DB

### 2.4 pending_uploads 轮询
- [ ] 检测到 new_upload 信号后查询
- [ ] 生成唯一文件码
- [ ] 写入 file_records 表
- [ ] 写入 codes 表
- [ ] 写入 code_cache 缓存
- [ ] 设置取件码过期时间
- [ ] 外部文件写入外部码映射
- [ ] 通知上传者文件码已生成
- [ ] 每 6 小时 flush 配额到 CRDB

### 2.5 配额管理
- [ ] 本地计数器累加（减少 CRDB RU）
- [ ] 定时同步到 CRDB（默认 5 分钟）
- [ ] Bot 关闭前强制同步
- [ ] 外部配额独立计数

### 2.6 举报功能
- [ ] 解码后消息附带"⚠️ 举报"按钮
- [ ] 点击举报推送给管理员
- [ ] 60 秒防抖生效
- [ ] 举报内容包括: 文件码、上传者、举报人、时间

### 2.7 媒体组缓冲
- [ ] 外部媒体组等待 MEDIA_GROUP_BUFFER_WAIT 秒（默认 3s）
- [ ] 收集完整媒体组后处理
- [ ] 解析 STORAGE_IDS 从 caption 提取

### 2.8 中继消息处理
- [ ] RELAY_DELIVER: 中继代发文件
- [ ] RELAY_RENEW: 记录已过期
- [ ] RELAY_ERROR: 返回错误给用户
- [ ] RELAY_BATCH: 批量媒体组处理

---

## 三、Dsp Bot（发送机器人）

### 3.1 单文件发送
- [ ] 从 jobs 表轮询到任务
- [ ] 尝试 file_id 直发（跨 bot 场景）
- [ ] 直发失败回退到 copy_message
- [ ] 发送成功后追加举报按钮
- [ ] 记录发送成功指标

### 3.2 批量文件发送（媒体组）
- [ ] 解析 batch_file_meta
- [ ] 按 PAGE_SIZE（默认 10）分页
- [ ] 每页以媒体组形式发送
- [ ] 多页时显示分页导航按钮
- [ ] 翻页按钮正常工作
- [ ] 翻页状态 TTL 5 分钟

### 3.3 频道路由
- [ ] 从 cells 表获取存储频道
- [ ] 发送失败时自动降级到下一个槽位
- [ ] 最多尝试 10 个降级槽位
- [ ] 环形降级: 沿环找下一个可用频道

### 3.4 并发控制
- [ ] 最多 SEND_CONCURRENCY（默认 25）个并发
- [ ] 4 个 worker 并发处理
- [ ] semaphore 等待超时重新入队

### 3.5 动态限速
- [ ] jobs < 10: 基础延迟 0.2s
- [ ] jobs > 30: 最大延迟 3.0s
- [ ] 中间值线性插值

### 3.6 死信处理
- [ ] retry_count >= 3 标记为 dead
- [ ] 每小时重试死信队列（最多 10 个）
- [ ] 死信记录审计日志

### 3.7 Dsp 侧降级（Mon 兜底）
- [ ] 60 秒内失败 3 次触发降级
- [ ] R100 槽位永不自降
- [ ] 降级后清除失败记录
- [ ] 异常时记录 ERROR 日志

### 3.8 频道限流器清理
- [ ] 定期清理超过 5 分钟未访问的条目
- [ ] 防止内存泄漏

---

## 四、Mon Bot（监控机器人）

### 4.1 心跳检测
- [ ] 每 60 秒对所有 active/shadow 槽位发心跳
- [ ] 心跳成功写入本地 SQLite（零 CRDB RU）
- [ ] 心跳失败累加 fail_streak
- [ ] 连续 3 次失败触发降级（180 秒）

### 4.2 封禁检测
- [ ] 匹配 BAN_KEYWORDS: "chat not found", "channel not found", "bot was kicked" 等
- [ ] 封禁时通知管理员
- [ ] 尝试从备用池补充频道
- [ ] 无备用时标记为 lost

### 4.3 自动降级
- [ ] Active → Lost, Shadow1 → Active, Shadow2 → Shadow1
- [ ] 使用 CRDB 事务保证原子性
- [ ] 写入 rotate_log 审计日志
- [ ] 冷却时间分级: 300s → 600s → 1200s
- [ ] R100 槽位仅告警不降级

### 4.4 文件同步
- [ ] Active 槽位新消息复制到 Shadow1/Shadow2
- [ ] 优先使用 copy_messages 批量接口
- [ ] 不支持时回退逐条 copy_message
- [ ] 游标本地缓存，每 5 次同步 flush CRDB

### 4.5 智能替补
- [ ] 新频道（last_synced_msg_id=0）自动补齐存量文件
- [ ] 最多拉取 200 条媒体消息
- [ ] 补齐后更新游标

### 4.6 活跃频道轮转
- [ ] 检查 file_count >= files_per_slot（默认 500）
- [ ] 检查 rotation time >= time_per_slot（默认 3600s）
- [ ] 使用 CRDB 事务原子切换窗口
- [ ] 当前窗口 active → shadow1
- [ ] 下一窗口 shadow1 → active

### 4.7 心跳缓存
- [ ] cells 全量缓存 120 秒
- [ ] 每 5 个周期强制重载
- [ ] 写入 cells 后主动失效

### 4.8 定期拓扑校验
- [ ] 每 10 轮校验一次
- [ ] 检查环形链表完整性
- [ ] 检查 next 指针有效性
- [ ] 检查三元组完整性

### 4.9 通知管理员
- [ ] 频道封禁告警
- [ ] 降级通知
- [ ] 轮转通知
- [ ] 连续失败 10 分钟后停止重试

---

## 五、Admin Bot（管理机器人）

### 5.1 用户管理
- [ ] `/status` 查看管理员状态
- [ ] `/user <id>` 查看用户详情
- [ ] `/users` 列出所有用户
- [ ] `/set_level <id> <level>` 设置会员等级
- [ ] `/ban <id>` 封禁用户
- [ ] `/unban <id>` 解封用户
- [ ] `/set_quota <id> <次数>` 设置解码配额
- [ ] `/set_external_quota <id> <次数>` 设置外部码配额

### 5.2 文件管理
- [ ] `/file <code>` 查看文件详情
- [ ] `/files` 列出文件（分页）
- [ ] `/delete_file <code>` 删除文件（脱钩）

### 5.3 中继管理
- [ ] `/relay_set_api <api_key>` 设置中继 API
- [ ] `/relay_add <phone> <api_hash>` 添加中继账号
- [ ] `/relay_remove <phone>` 移除中继账号
- [ ] `/relay_list` 列出中继账号
- [ ] `/relay_pending` 查看待处理中继
- [ ] `/relay_code` 查看中继码
- [ ] `/relay_reset_stats` 重置中继统计

### 5.4 配置管理
- [ ] `/set_storage_channel <id>` 修改主存储频道
- [ ] `/set_file_prefix <prefix>` 修改文件码前缀
- [ ] `/set_force_join <channel_id>` 修改强制加群
- [ ] `/set_username <bot_name> <username>` 修改 Bot 用户名
- [ ] `/set_quota_default <free> <basic> <premium>` 修改默认配额
- [ ] `/set_r2 <account_id> <access_key> <secret_key>` 配置 R2
- [ ] `/set_db_backup <enabled> <interval>` 配置数据库备份

### 5.5 码路由管理
- [ ] `/add_code_route <prefix> <bot>` 添加码前缀路由
- [ ] `/remove_code_route <prefix>` 移除码路由
- [ ] `/code_routes` 查看所有路由

### 5.6 Bot 间隔管理
- [ ] `/set_bot_interval <bot> <seconds>` 设置 bot 间间隔
- [ ] `/remove_bot_interval <bot>` 移除间隔
- [ ] `/bot_intervals` 查看所有间隔

### 5.7 备用池管理
- [ ] `/spare_add <channel_id> [account]` 添加备用频道
- [ ] `/spare_remove <channel_id>` 移除备用频道
- [ ] `/spare_list` 查看备用池

### 5.8 轮转配置
- [ ] `/rotation_set <window> <files> <seconds>` 设置轮转参数
- [ ] `/rotation_view` 查看当前轮转配置

### 5.9 拓扑查看
- [ ] `/topology` 查看当前拓扑状态

### 5.10 其他
- [ ] `/health` 查看系统健康状态
- [ ] `/logs` 查看解码日志
- [ ] `/factory_reset` 工厂重置
- [ ] `/purge_channel <channel_id>` 清空频道
- [ ] `/settings` 查看当前设置
- [ ] `/help` 显示所有命令说明
- [ ] 所有配置热更新，无需重启 Bot

---

## 六、环形冗余架构

### 6.1 拓扑初始化
- [ ] `generate_topology.py` 正确生成 topology.yaml
- [ ] `seed_topology.py` 正确写入 cells 表
- [ ] 15 个 Active + 30 个 Shadow = 45 频道
- [ ] R100 独立频道不入环

### 6.2 环形链表
- [ ] next_active_chat_id 形成完整单向环
- [ ] prev_slot_id 正确指向
- [ ] 降级后环形指针正确更新

### 6.3 组内降级链路
- [ ] Active 失效 → Shadow1 提升为 Active
- [ ] Shadow1 失效 → Shadow2 提升为 Shadow1
- [ ] Shadow2 失效 → 无可降级目标
- [ ] 每组至少保留 2 个可用频道

### 6.4 跨账号冗余验证
- [ ] 任意一个账号被封，每组至少剩 2 个频道
- [ ] 无单点故障

### 6.5 三活态并行
- [ ] 同时激活 3 个 Active 窗口
- [ ] 窗口大小可配置（默认 3）

---

## 七、数据库与缓存层

### 7.1 CockroachDB
- [ ] cells 表: 环形拓扑存储
- [ ] codes 表: 取件码索引
- [ ] jobs 表: 派工队列
- [ ] rotate_log 表: 降级审计日志
- [ ] file_records 表: 文件记录
- [ ] users 表: 用户信息
- [ ] decode_logs 表: 解码日志

### 7.2 SQLite 缓存
- [ ] 心跳状态本地缓存
- [ ] 配额计数本地缓存
- [ ] code_cache 本地缓存
- [ ] 定期 flush 到 CRDB
- [ ] 进程重启后从 SQLite 恢复

### 7.3 数据一致性
- [ ] CRDB 事务保证原子性（降级、轮转）
- [ ] 本地缓存与 CRDB 最终一致
- [ ] 游标缓存定期 flush

---

## 八、配置文件

### 8.1 config/settings.py
- [ ] .env 环境变量正确加载
- [ ] 必填字段验证（5 个 Bot Token + CRDB URL）
- [ ] 账号频道配置解析正确
- [ ] 动态配置属性（db_backup_interval/enabled）

### 8.2 config/groups.yaml
- [ ] 5 个账号各 9 个频道配置
- [ ] R100 兜底频道配置

### 8.3 config/topology.yaml
- [ ] 自动生成，无需手动编辑
- [ ] Mon 配置: heartbeat_interval, heartbeat_timeout, degrade_cooldown

---

## 九、工具库

### 9.1 utils/rate_limiter.py
- [ ] 全局速率限制器
- [ ] 用户级别速率限制器

### 9.2 utils/dynamic_rate_limiter.py
- [ ] 根据 jobs 队列长度动态调整延迟
- [ ] 低负载/高负载/中间态三种模式

### 9.3 utils/per_channel_limiter.py
- [ ] 按频道独立限流
- [ ] 定期清理过期条目

### 9.4 utils/flood_waiter.py
- [ ] Telegram API Flood Wait 自动退避
- [ ] safe_copy_message / safe_send_message / safe_send_media_group

### 9.5 utils/force_join.py
- [ ] 强制加群检查
- [ ] 三个 Bot 引导文本

### 9.6 utils/file_utils.py
- [ ] 文件类型检测
- [ ] 媒体消息提取
- [ ] 文件元数据提取

### 9.7 utils/channel_selector.py
- [ ] 存储频道选择
- [ ] Active 槽位过滤

### 9.8 utils/code_decoder.py
- [ ] file_id 前缀识别（CAAC, AgAD, BAAC 等）

### 9.9 services/relay_pool.py
- [ ] 中继账号池管理
- [ ] 负载均衡权重计算
- [ ] 动态选择最优中继账号

### 9.10 services/code_generator.py
- [ ] 唯一文件码生成
- [ ] 文件码格式验证
- [ ] 从消息中提取码和 bot 名

### 9.11 services/permission.py
- [ ] 上传权限检查
- [ ] 解码权限检查
- [ ] 配额验证
- [ ] 用户创建/更新

### 9.12 services/db_backup.py / db_restore.py
- [ ] 定时数据库备份
- [ ] R2 云存储备份
- [ ] 数据库恢复

### 9.13 storage/r2.py
- [ ] Cloudflare R2 存储集成
- [ ] 备份文件上传

### 9.14 storage/delivery_resolver.py
- [ ] 解析最佳发送频道
- [ ] try_deliver 发送尝试

---

## 十、边界情况与异常场景

### 10.1 网络异常
- [ ] Telegram API 超时
- [ ] Flood Wait 触发
- [ ] 网络断开重连

### 10.2 数据异常
- [ ] 文件码不存在
- [ ] 文件码已过期
- [ ] 文件记录损坏
- [ ] jobs 表为空
- [ ] cells 表无数据

### 10.3 并发异常
- [ ] 多用户同时请求同一文件码
- [ ] 同一用户快速连续发送文件码
- [ ] 多个 Bot 同时写入同一记录

### 10.4 资源异常
- [ ] CRDB 连接池耗尽
- [ ] SQLite 缓存满
- [ ] 磁盘空间不足
- [ ] 内存泄漏检测

### 10.5 权限异常
- [ ] 未登录用户访问
- [ ] 被 ban 用户操作
- [ ] 配额用尽用户操作
- [ ] 非法管理员命令

---

## 十一、部署与运维

### 11.1 部署脚本
- [ ] `deploy_vps_per_bot.sh` 独立服务部署
- [ ] `deploy.ps1` Windows 部署
- [ ] 7 个 systemd 单元正常启动

### 11.2 进程管理
- [ ] 各 Bot 独立运行互不影响
- [ ] 崩溃后 systemd 自动重启
- [ ] 重启冷却期生效（RESTART_COOLDOWN）

### 11.3 监控指标
- [ ] metrics.upload_count
- [ ] metrics.decode_count
- [ ] metrics.send_success_count
- [ ] metrics.send_fail_count
- [ ] metrics.increment("mon.degrade")

### 11.4 日志
- [ ] loguru 统一日志
- [ ] 各 Bot 日志分离
- [ ] 降级/轮转/封禁操作审计日志

---

## 测试结果记录

| 测试项 | 状态 | 备注 |
|--------|------|------|
| 一、Up Bot | 待测试 | |
| 1.1 单次上传 | 待测试 | |
| 1.2 批量上传 | 待测试 | |
| 1.3 批次进度 | 待测试 | |
| 1.4 权限控制 | 待测试 | |
| 1.5 强制加群 | 待测试 | |
| 1.6 速率限制 | 待测试 | |
| 1.7 外部中继 | 待测试 | |
| 1.8 槽位轮转 | 待测试 | |
| 1.9 超时清理 | 待测试 | |
| 二、Idx Bot | 待测试 | |
| 2.1 内部码解码 | 待测试 | |
| 2.2 外部码解码 | 待测试 | |
| 2.3 外部码映射 | 待测试 | |
| 2.4 pending 轮询 | 待测试 | |
| 2.5 配额管理 | 待测试 | |
| 2.6 举报功能 | 待测试 | |
| 2.7 媒体组缓冲 | 待测试 | |
| 2.8 中继消息 | 待测试 | |
| 三、Dsp Bot | 待测试 | |
| 3.1 单文件发送 | 待测试 | |
| 3.2 批量发送 | 待测试 | |
| 3.3 频道路由 | 待测试 | |
| 3.4 并发控制 | 待测试 | |
| 3.5 动态限速 | 待测试 | |
| 3.6 死信处理 | 待测试 | |
| 3.7 Dsp 侧降级 | 待测试 | |
| 3.8 限流器清理 | 待测试 | |
| 四、Mon Bot | 待测试 | |
| 4.1 心跳检测 | 待测试 | |
| 4.2 封禁检测 | 待测试 | |
| 4.3 自动降级 | 待测试 | |
| 4.4 文件同步 | 待测试 | |
| 4.5 智能替补 | 待测试 | |
| 4.6 活跃轮转 | 待测试 | |
| 4.7 心跳缓存 | 待测试 | |
| 4.8 拓扑校验 | 待测试 | |
| 4.9 通知管理员 | 待测试 | |
| 五、Admin Bot | 待测试 | |
| 5.1-5.10 各项管理 | 待测试 | |
| 六、环形架构 | 待测试 | |
| 6.1-6.5 拓扑/环形/降级 | 待测试 | |
| 七、数据库 | 待测试 | |
| 7.1-7.3 CRDB/SQLite/一致性 | 待测试 | |
| 八、配置 | 待测试 | |
| 8.1-8.3 settings/groups/topology | 待测试 | |
| 九、工具库 | 待测试 | |
| 9.1-9.14 各模块 | 待测试 | |
| 十、边界情况 | 待测试 | |
| 10.1-10.5 异常场景 | 待测试 | |
| 十一、部署运维 | 待测试 | |
| 11.1-11.4 部署/监控/日志 | 待测试 | |

---

## 已完成测试

| 测试项 | 状态 | 备注 |
|--------|------|------|
| /start 引导 | ✅ 通过 | 三个 bot 正常返回 |
| 上传 → 解码 → 接收 | ✅ 通过 | 完整流程正常 |
| 内部码识别 | ✅ 通过 | 消息中含 FILE_CODE_PREFIX 即识别 |
| 文件码提取 | ✅ 通过 | 整条消息包含文件码也能提取 |
| DSP 延迟修复 | ✅ 通过 | copy_message 方案已部署 |
