# TG文件传输系统 — 测试结果与优化记录

## 测试结果

### ✅ 已通过

| 编号 | 测试项 | 状态 | 备注 |
|------|--------|------|------|
| 1.1 | Up Bot 单次上传流程 | ✅ 通过 | 有效期选择→转发权限→文件码生成，全流程正常 |
| 1.2 | Up Bot 批量上传流程 | ✅ 通过 | /start_upload → 多文件 → /end_upload → 汇总正常 |
| 1.4 | 强制加群 | ✅ 通过 | 正常 |
| 1.5 | 批次上传进度显示 | ✅ 通过 | 超时自动 flush，日志确认清理正常 |
| 2.1 | Idx Bot 内部码解码 | ✅ 通过 | 文件码解码→写jobs→Dsp发送，全流程正常 |
| 2.2 | Dsp Bot 单文件发送 | ✅ 通过 | 从jobs队列路由发送，正常 |
| 2.3 | Dsp Bot 批量文件发送 | ✅ 通过 | 媒体组发送正常，翻页正常 |
| 2.4 | Dsp Bot 频道路由 | ✅ 通过 | 日志显示路由到正确频道 |
| 2.5 | 配额管理 | ✅ 通过 | SQLite-first，配额递减正常 |
| 3.1 | Mon Bot 心跳检测 | ✅ 通过 | 45正常，轮转正常 |
| 3.2 | Mon Bot 自动降级 | ✅ 通过 | 日志显示轮转触发（a1→a3休眠，s4a→s6a唤醒） |
| 4.1 | Admin Bot 用户管理命令 | ✅ 通过 | /status, /health, /settings 正常 |
| 4.2 | Admin Bot 中继管理 | ✅ 通过 | /relay_list, /relay_pending 正常 |
| 4.3 | Admin Bot 配置管理 | ✅ 通过 | /settings, /topology, /rotation_view 正常 |
| 5.1 | 环形冗余架构 | ✅ 通过 | 拓扑显示正常，轮转配置正常 |

### ⏸️ 跳过/待测

| 编号 | 测试项 | 原因 |
|------|--------|------|
| 1.3 | 权限控制 | 需第二个账号测试 ban/unban |
| 1.6 | 速率限制 | 需快速发送15个文件 |
| 1.7 | 外部中继文件 | 需中继账号配置 |
| 1.8 | 槽位轮转 | 通过Mon Bot间接验证 |
| 1.9 | 超时清理 | 通过Mon Bot间接验证 |
| 2.6 | Dsp Bot 动态限速 | 日志未触发，需高并发测试 |
| 2.7 | Dsp Bot 死信处理 | 需模拟发送失败 |
| 3.3 | Mon Bot 封禁检测 | 需模拟频道封禁 |
| 3.4 | Mon Bot 自动降级 | 需模拟故障场景 |
| 3.5 | Mon Bot 文件同步 | 需模拟故障场景 |
| 3.6 | Mon Bot 智能替补 | 需模拟故障场景 |
| 3.7 | Mon Bot 活跃轮转 | 已自动触发，日志确认正常 |
| 3.8 | Mon Bot 拓扑校验 | 需手动触发 |
| 4.4 | Admin Bot 用户封禁/解封 | 需测试账号 |
| 4.5 | Admin Bot 配额管理 | 需测试账号 |
| 4.6 | Admin Bot 配置修改 | 需测试 |
| 4.7 | Admin Bot 文件管理 | 需测试 |
| 4.8 | Admin Bot 日志查看 | 需测试 |
| 4.9 | Admin Bot 工具命令 | 需测试 |
| 5.2 | 环形拓扑初始化 | 需模拟故障 |
| 5.3 | 环形链表 | 已通过Mon Bot日志确认 |
| 5.4 | 组内降级 | 需模拟故障 |
| 5.5 | 跨账号冗余 | 需多账号配置 |
| 5.6 | 三活态并行 | 已通过Mon Bot日志确认 |
| 6.1 | 数据库与缓存 | 需检查各Bot启动日志 |
| 6.2 | 配置文件 | 需检查config/ |
| 6.3 | 工具库 | 需逐个检查 |
| 6.4 | 边界情况 | 需模拟异常 |
| 6.5 | 部署运维 | 需检查部署脚本 |

## 修复记录

### 修复 1: admin_bot 事件循环错误
- **问题**: `RuntimeError: There is no current event loop in thread 'MainThread'`
- **原因**: 使用 `asyncio.get_event_loop()` 在 Python 3.12 已废弃
- **修复**: 改用 `asyncio.run()` + `async with app` 标准模式

### 修复 2: admin_bot 数据库未初始化
- **问题**: `AttributeError: 'NoneType' object has no attribute 'acquire'`
- **原因**: standalone 模式未调用 `init_db()`
- **修复**: 在 `_async_main()` 中调用 `_init()`

### 修复 3: Mon Bot `iter_messages` 不存在
- **问题**: `'Bot' object has no attribute 'iter_messages'`
- **原因**: pyrogram API 变更
- **修复**: 全部替换为 `get_chat_history()`

### 修复 4: Mon Bot 轮转 SQL 类型错误
- **问题**: `invalid input for query argument $2: '-1004400238063' ('str' object cannot be interpreted as an integer)`
- **原因**: `str(nxt)` 转换导致类型错误
- **修复**: 直接传 int

### 修复 5: 轮转通知过于频繁
- **问题**: 每次心跳都发送轮转通知到 Telegram
- **修复**: 改为每 3600 秒最多通知一次

### 修复 6: /health 只显示 admin_bot
- **问题**: 各 Bot 独立进程，metrics 不能跨进程共享
- **修复**: 引入 SQLite 共享心跳表 (`bot_heartbeat`)，各 Bot 启动时写入，admin_bot 读取

### 修复 7: /status 主存储频道显示不正确
- **问题**: 环形架构下不再有单一"主存储频道"
- **修复**: 改为显示环形拓扑活跃槽位

### 修复 8: 配额管理不走 SQLite
- **问题**: 配额检查走 CRDB，每次解码产生 RU
- **修复**: 
  - 配额存储改为 SQLite (`user_quota` 表)
  - 检查配额: SQLite → CRDB 兜底
  - 递增配额: 原子递增 SQLite
  - 后台任务每 6h 批量同步 SQLite → CRDB

### 修复 9: /settings 无响应
- **问题**: 异常被静默吞掉
- **修复**: 添加 try/except 返回具体错误信息

### 修复 10: /seetings 拼写错误
- **问题**: 用户使用 `/seetings`（少一个 t）
- **说明**: 正确命令是 `/settings`

## 待优化项

### 优化 1: 批次上传文件码提示格式
- **问题**: `/end_upload` 后返回"文件码已接受，@idx发送给您"，@idx 是纯文字不可点击
- **影响**: 用户体验不一致，单文件上传返回的可点击 @idx
- **建议**: 统一使用 MarkdownV2 或 HTML 格式，使 @username 可点击
- **位置**: `bots/up_bot.py` end_upload 函数中 `reply_text` 部分
- **优先级**: 低

### 优化 2: 单文件上传无法添加备注
- **问题**: 单文件上传没有备注功能，只有批次上传支持 `/note`
- **影响**: 用户无法为单文件添加备注信息
- **建议**: 单文件上传时也支持 `/note` 命令设置备注
- **位置**: `bots/up_bot.py` handle_file 函数
- **优先级**: 低

---

## 测试进度
- 已完成: 1.1, 1.2, 1.4, 1.5, 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 4.1, 4.2, 4.3, 5.1
- 待测: 权限控制、速率限制、外部中继、封禁检测、死信处理等需要特殊场景的测试
