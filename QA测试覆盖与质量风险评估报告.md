# tgjiema 测试覆盖与质量风险评估报告

**评估人**：严过关（software-qa-engineer）
**评估对象**：tgjiema 项目（`F:\xiangmu\tgjiema`）
**评估日期**：2025-07-07
**评估方式**：静态代码抽查核实（未运行、未改动代码）。本文所有风险项均通过阅读源码 + grep 定位到具体 `file:line` 后确认，置信度见正文标注。

---

## 一、测试现状定级

### 1.1 成熟度：0 分位（无任何自动化测试）

| 维度 | 现状 | 证据 |
|------|------|------|
| 单元测试 | ❌ 无 | `grep "def test_|import pytest|import unittest"` 全仓零命中 |
| 集成测试 | ❌ 无 | `tests/` 目录仅含 `__pycache__`（编译产物），无 `.py` 源码 |
| 端到端测试 | ❌ 无 | 仅有 `test_plan.md` 手动 checklist，且矩阵中全部 Bot 用例标记为「待测试」 |
| 安全测试 | ❌ 无 | 无鉴权/注入/越权专项用例 |
| 混沌/容错测试 | ❌ 无 | 无故障注入、无并发竞态、无降级验证 |
| 测试框架/配置 | ❌ 无 | 无 `pyproject.toml` / `pytest.ini` / `conftest.py` / `tox.ini` |
| CI | ❌ 无 | 无 `.github/workflows`、无 `.gitlab-ci.yml` |
| 测试依赖 | ❌ 无 | `requirements.txt` 未含 `pytest` / `pytest-asyncio` 等 |

**结论**：当前测试成熟度处于 **Level 0（无）**。系统已具备较完整的人工 `test_plan.md`（覆盖 5 个 Bot + Web 后台 + 备份恢复等），但全部为「待测试」状态，仅有手工验证痕迹，无法回归、无法拦截回归缺陷。**本评估不做"已测试即安全"的假设——所有高危路径在代码层均未经断言保护。**

### 1.2 主要缺口分类

- **单元测试缺口**：纯逻辑函数（码提取/码生成、强制加群判定、查询条件翻译、缓存失效）无任何保护，是投入产出比最高的补测区。
- **集成测试缺口**：跨进程中继投递（up→idx→dsp）、CRDB/SQLite 双写一致性、备份恢复链路完全无保护。
- **端到端缺口**：用户真实路径（上传→解码→接收）仅靠人工 checklist，无自动化沙箱。
- **安全缺口**：最严重的 3 项风险（未鉴权中继摄入、SQL 注入、凭证泄漏）均无任何测试。
- **混沌缺口**：进程崩溃/连接断开/孤儿任务/缓存幽灵解码等韧性问题无验证。

---

## 二、质量风险登记册

> 严重程度：🔴严重（安全/数据完整性，阻塞发布）｜🟠中等（正确性/可用性，需修复或强测试）｜🟡低（技术债/可维护性）
> 置信度：**已核实**=本评估已读代码确认；**待核实**=依据原始风险清单，本次抽查未发现对应代码，建议回归确认。

| # | 风险项 | 位置 (file:line) | 触发条件 | 后果 | 严重度 | 置信度 | 建议测试类型 |
|---|--------|------------------|----------|------|--------|--------|--------------|
| R1 | idx_bot 中继摄入接口**完全未鉴权** | `bots/idx_bot.py:272` `handle_relay_delivery`；`bots/idx_bot.py:1839` `_handle_relay_file_media` | 任意 Telegram 用户向 idx_bot 发送 `RELAY_DELIVER:` / `RELAY_FILE:` 格式消息（绕过 up_bot 白名单 `up_bot.py:847`） | 未授权向任意 `target_user_id` 投递任意文件，绕过配额/限速 | 🔴 | 已核实 | 单元（白名单判定）+ 集成（端到端投递拒绝） |
| R2 | `session.py` 写入路径遗留 `print()` 泄漏数据原文 | `database/session.py:25,27`（`_json_dumps`）；`:605,610,614,616`（`_safe_str`）；调用点 `:709,723,741,750` | 每次 `insert/update` 序列化时触发 | 文件载荷/PII 原文打印到 stdout（数据泄漏 + 噪声） | 🔴 | 已核实 | 单元（断言 stdout 不含载荷） |
| R3a | `restore_from_backup` 表/列名未白名单校验（SQL 注入面） | `services/db_backup.py:274`（`safe_name` 仅转义引号，不彻底）；`:282-284`（`cols = list(row.keys())` 直接拼入 SQL，零转义） | 攻击者伪造/污染备份 JSON 的列名（如 `x"; DROP TABLE y; --`） | 列名注入可破坏数据库；表名转义亦不稳健 | 🔴 | 已核实 | 单元（恶意列名应被拒/转义） |
| R3b | 备份对凭证脱敏为 `***REDACTED***`，恢复后中继/R2 不可用 | `services/db_backup.py:37,40-54`（`_redact_secrets` 写入占位符）；`restore_from_backup:230` 回写 | 全量备份恢复后，`api_hash`/`r2_secret_key`/`r2_access_key` 变为 `***REDACTED***` | 恢复即"废库"：中继账号与 R2 凭证失效，备份可恢复性被破坏 | 🔴 | 已核实（机制在 backup 侧） | 集成（备份→恢复→凭证可用） |
| R4a | `invalidate_code_entry` fire-and-forget 未持引用 | `database/cache.py:148-162`（`:159` `loop.create_task(store.delete(...))` 无引用） | 事件循环退出/任务被丢弃/并发失效 | SQLite 持久化缓存未失效 → **幽灵解码**（已下架/过期的码仍可解） | 🟠 | 已核实 | 单元（失效后必须查不到） |
| R4b | `factory_reset` 不清**内存缓存** | `bots/admin_bot/handlers.py:689-790`（清 CRDB + 本地 SQLite，但 `database/cache.py` 的 `_code_cache`/`_user_cache` 等内存 `QueryCache` 未清） | 执行 `/factory_reset` 后进程仍存活 | 旧码仍驻留内存，重置后**仍可解码** | 🟠 | 已核实 | 集成（重置后内存码不可解） |
| R5 | `D1Collection` `$or` 仅翻译 `$regex` 子条件，其余静默忽略 | `database/session.py:888-897`（count）、`:949-961`（find） | `$or` 内含 `$ne`/`$in`/`$gte` 等非 `$regex` 条件 | 整个 `$or` 条件被丢弃 → 返回**错误结果集**（正确性 bug） | 🟠 | 已核实 | 单元（断言翻译出全部子条件） |
| R6a | `relay_instance.shutdown` 不取消后台任务 | `services/relay_instance.py:1024-1026`（仅 `disconnect()`） | 关闭账号时 `_message_loop`(`:523`)、`_cleanup_cooldowns_loop`(`:157`) 仍在跑 | **孤儿任务泄漏**，连接已断但协程持续运行 | 🟠 | 已核实 | 单元（shutdown 后无存活后台任务） |
| R6b | `get_best_account` 假设 usage 含全部键，新账号 KeyError | `services/relay_pool.py:104-107`（`usage["avg_wait_ms"]` 等直接下标） | 新接入账号尚无 `relay_usage` 行 | `KeyError` 崩溃，负载均衡整体失效 | 🟠 | 已核实 | 单元（缺 usage 行应优雅降级） |
| R7a | `force_join` **fail-open**（异常即放行） | `utils/force_join.py:13,24,28,32,36` 全部 `return True` | 加群校验接口异常/超时/封禁 | 强制加群形同虚设，未加群用户被放行 | 🟠 | 已核实 | 单元（异常路径应按配置 fail-closed） |
| R7b | `settings` `extra="ignore"` 让错拼变量静默失效 | `config/settings.py:185` | 环境变量名拼写错误 | 配置被静默忽略，行为偏离预期且无告警 | 🟠 | 已核实 | 单元（错拼键应告警/报错） |
| R7c | 密钥含 `#` 被行内注释正则截断 | 原始清单指 `config/settings.py` | `.env` 密钥含 `#` | 截断导致凭证错误 | 🟡 | **待核实**（本次未找到对应正则实现，疑似误报或已移除；仍建议加防御测试） | 单元（含 `#` 密钥 round-trip） |
| R8a | `extract_code_and_bot_from_message` 命中 bot 名时把整条消息当码 | `services/code_generator.py:110-112`（`if bot: return text, bot`） | 消息中检测到 bot 用户名（含系统 bot） | 整条消息被当文件码返回，与 docstring 预期不符，解码错乱 | 🟠 | 已核实 | 单元（应提取纯码而非整条消息） |
| R8b | `generate_unique_code` 无查重 | `services/code_generator.py:176-178`（直接 `build_file_code`） | 并发生成 | 码碰撞概率虽低但无查重兜底（仅依赖主键，PK 冲突时写入失败用户无码） | 🟡 | 已核实 | 单元（查重/冲突重试） |
| R9 | 弃用 API（`asyncio.get_event_loop` 8 处）、空 stub、import 期副作用 | `services/relay_instance.py:273,405,528,535,597,632,644,662`；`utils/monitor.py:50-54`（`increment` 空 `pass`）；`config/settings.py:313`（`settings=Settings()` 导入即实例化）；`utils/dynamic_rate_limiter.py:39`（`assert` 用于生产校验） | Python 3.12+ / 运行时异常 / 缺失必填 env | 弃用告警、降级计数失效、`assert` 关闭优化后失真、缺 env 启动即崩 | 🟡 | 已核实 | 单元（用 `get_running_loop`；monitor 行为断言；import 不崩） |

---

## 三、测试策略与优先级

### 3.1 测试金字塔与 Mock 策略

**推荐占比**（系统异步/并发/外部依赖重，重单测、轻 E2E）：

```
         ▲  E2E（~10%）：跨进程真实链路冒烟，用 Fake Bot/本地 CRDB
        ██  集成（~20%）：idx↔dsp 投递、备份恢复、缓存双写一致性
   ████████  单元（~70%）：纯逻辑 + 带 mock 的 handler/服务
```

**Mock 策略（核心原则：不连真网、不连真库、不触碰真实 Telegram/MTProto/R2）**：

| 外部依赖 | Mock 方式 | 说明 |
|----------|-----------|------|
| Telegram Bot API（python-telegram-bot） | `unittest.mock.AsyncMock` 包 `telegram.Bot`，手工构造 `telegram.Update` / `ContextTypes` | handler 单测用假 Update 驱动，断言 `bot.send_message` 被如何调用 |
| MTProto（Telethon `TelegramClient`） | `AsyncMock` 替 `TelethonClient` 方法（`send_file`/`get_messages`/`disconnect`） | 验证中继客户端调用与 shutdown 行为 |
| CockroachDB（asyncpg） | 集成测试用**本地 aiosqlite**（内存/临时文件）作为"可真实执行 SQL"的替身，按 `session.py` schema 建表；单测直接 mock `D1Collection`/`db_client` | 既测真实 SQL 语义（含 `$or` 翻译），又零 RU、零网络 |
| R2（`r2_storage`） | `AsyncMock` 的 `upload/download`/`configure` | 验证备份/恢复对 R2 的调用与凭证处理 |
| 缓存层（`cache_store`） | 单测用 `tmp_path` 下的独立 aiosqlite 实例 | 验证失效/幽灵解码 |

### 3.2 P0 必测清单（直接对应 🔴 高危，含伪代码级断言）

> 说明：以下测试**当前跑必失败**（因源码即存在对应缺陷），这正是"用失败测试证明 bug"的 QA 手法。先写测试 → 交 Engineer 修源码 → 测试转绿。

**R1 — 未鉴权中继摄入**
```python
async def test_relay_delivery_rejects_unauthorized_sender():
    # 构造来自"非白名单用户"的 RELAY_DELIVER 消息
    update = make_update(text="RELAY_DELIVER:12345:mfile_abc123", from_user="attacker")
    context = make_context(bot=AsyncMock())
    await handle_relay_delivery(update, context)
    # 断言：未创建任何投递 job；未向 target 发文件
    assert not dispatch_to_dsp_called()
    context.bot.send_message.assert_not_called()

async def test_relay_file_media_rejects_unauthorized_sender():
    update = make_update(caption="RELAY_FILE:99999:mfile_xyz", from_user="attacker")
    assert await _handle_relay_file_media(update, ctx) is False  # 应拒绝
```

**R2 — 写入路径 print 泄漏**
```python
def test_safe_str_does_not_print_payload(capsys):
    _safe_str({"secret_file": b"TOP_SECRET_BYTES"})
    out = capsys.readouterr().out
    assert "TOP_SECRET" not in out   # 数据原文不得出现在 stdout
```

**R3a — 备份恢复 SQL 注入**
```python
async def test_restore_rejects_malicious_column_name():
    evil_backup = {"tables": {"file_records": [
        {"id": 1, 'x"; DROP TABLE file_records; --': "pwned"}
    ]}}
    # 断言：恢复被拒或对标识符做白名单校验，绝不执行注入
    with pytest.raises((ValueError, AssertionError)):
        await restore_from_backup_from_dict(evil_backup)
```

**R3b — 凭证脱敏导致恢复不可用**
```python
async def test_backup_restore_preserves_secrets():
    backup = await db_backup.create_backup()        # 含真实 r2_secret_key
    await db_restore.restore_from_backup(backup.key)
    restored = await get_config("r2_secret_key")
    assert restored != "***REDACTED***"             # 恢复后凭证必须可用
```

**R4a — 缓存失效 fire-and-forget**
```python
async def test_invalidate_code_entry_persists():
    await cache_store.put("code:abc", {...})
    invalidate_code_entry("abc")
    await asyncio.sleep(0)  # 让 fire-and-forget 任务有机会跑
    assert await cache_store.get("code:abc") is None  # 持久化缓存必须已删
```

**R4b — factory_reset 不清内存缓存**
```python
async def test_factory_reset_clears_in_memory_cache():
    _code_cache.cache["code:old"] = {...}           # 模拟重置前已缓存
    await factory_reset(make_admin_update(), ctx)
    assert "code:old" not in _code_cache.cache      # 内存码必须被清
```

**R5 — `$or` 翻译正确性**
```python
def test_d1collection_or_translates_all_operators():
    q = {"$or": [{"status": {"$ne": "dead"}}, {"expired": False}]}
    sql, params = D1Collection("codes")._build_where(q)
    assert "status != " in sql and "expired" in sql   # 非 $regex 子条件不得被丢弃
```

### 3.3 P1 / P2 测试清单（按模块）

**P1（🟠，模块级，需在首轮迭代内补齐）**
- `up_bot` 中继白名单正向/负向（已核实有校验，需固化行为）。
- `force_join` 决策矩阵：正常已加群放行 / 未加群拒绝 / 接口异常按配置 fail-closed（R7a）。
- `code_generator.extract_code_and_bot_from_message`：命中 bot 名时返回纯码而非整条消息（R8a）；`generate_unique_code` 冲突重试（R8b）。
- `relay_pool.get_best_account` 缺 usage 行优雅降级（R6b）；`relay_instance.shutdown` 取消全部后台任务（R6a）。
- `db_backup._redact_secrets` 仅脱敏敏感键、非敏感键保持（R3b 边界）。
- `settings` 错拼键告警（R7b）；含 `#` 密钥 round-trip（R7c 待核实项）。
- 缓存层其余 fire-and-forget 失效点一致性。
- `monitor.increment` 行为断言（非空 `pass` stub，R9）。

**P2（🟡，技术债/韧性，按版本节奏）**
- 端到端：上传→解码→接收 真链路冒烟（Fake Bot + 本地 aiosqlite）。
- 混沌：CRDB 中断时投递兜底；并发码生成竞态；孤儿任务在进程退出期的泄漏检测。
- 可维护性：`asyncio.get_event_loop` → `asyncio.get_running_loop()`（R9）；`import` 期 `Settings()` 副作用隔离（R9）；生产 `assert` 改为显式校验（R9）。
- `test_plan.md` 中其余「待测试」矩阵项逐步自动化。

---

## 四、落地建议

### 4.1 框架与依赖（加入 `requirements.txt` / 新 `pyproject.toml`）
- `pytest`、`pytest-asyncio`（异步测试）、`pytest-cov`（覆盖率）、`pytest-mock`（mock 辅助）。
- 可选：`asynctest`（或直接使用 `unittest.mock.AsyncMock`，Python 3.8+ 已内置，无需 asynctest）。
- 数据库替身：复用已依赖的 `aiosqlite`（内存模式）作为 CRDB 本地替身；如需更贴近 CRDB 语义，可引入 `testcontainers`（CockroachDB 容器）做集成层，但单测不依赖。

### 4.2 目录结构
```
tests/
├── conftest.py              # 共享 fixtures：fake Update/Context、mock bot、tmp SQLite
├── unit/
│   ├── test_code_generator.py
│   ├── test_force_join.py
│   ├── test_session_query.py        # $or 翻译、_safe_str 无泄漏
│   ├── test_cache_invalidation.py
│   └── test_relay_pool.py
├── integration/
│   ├── test_relay_delivery_auth.py   # R1 端到端拒绝
│   ├── test_db_backup_restore.py     # R3 SQLi + 凭证
│   └── test_factory_reset.py         # R4b 内存缓存
└── e2e/
    └── test_upload_decode_receive.py
```

### 4.3 CI 建议
- GitHub Actions / GitLab CI：PR 触发 `pytest` + `pytest-cov`，门禁覆盖率（见 4.4）。
- 异步测试标记：`pytest -q --asyncio-mode=auto`。
- 失败即阻断合并；P0 测试（未鉴权、SQLi、幽灵解码）设为 **blocking**。

### 4.4 覆盖率门槛（建议分阶段）
- 首轮：核心安全/正确性路径（R1–R5 相关函数）**100% 语句覆盖**；全仓**≥40%**。
- 稳定期：全仓**≥70% 语句 / ≥60% 分支**；`database/`、`services/`、`bots/` 关键 handler 不低于 80%。

---

## 五、智能路由判定（发布门禁结论）

### 5.1 是否阻塞发布？
**结论：是，当前源码质量风险构成"阻塞发布"（block release）。** 依据：
- 🔴 项中 R1（未鉴权任意投递）、R3a（SQL 注入）、R3b（恢复即废库）、R2（数据泄漏）均为安全/数据完整性级缺陷，且**无任何测试保护**，上线即暴露攻击面与数据事故面。
- 🟠 项 R4/R5/R6/R7/R8 构成可复现的正确性与可用性缺陷（幽灵解码、错误结果集、崩溃、强制加群失效）。

### 5.2 责任归属与先后序（智能路由）

| 风险 | 责任归属 | 动作 | 路由目标 |
|------|----------|------|----------|
| R1 未鉴权中继摄入 | **Engineer（修源码）** + QA（写 P0 测试） | 在 `handle_relay_delivery`/`_handle_relay_file_media` 增加发送者白名单/身份校验（对齐 `up_bot.py:847` 机制）；QA 先写失败测试证明缺陷 | Engineer 先修，QA 测试转绿 |
| R2 写入 print 泄漏 | **Engineer** | 移除 `_json_dumps`/`_safe_str` 的 `print`，改用 `loguru` debug 或删除 | Engineer |
| R3a SQL 注入 | **Engineer** | `restore_from_backup` 对表名/列名做白名单校验 + 参数化标识符 | Engineer |
| R3b 凭证脱敏 | **Engineer** | 备份保留凭证加密副本/KMS，恢复时回填而非写 `REDACTED` | Engineer |
| R4a 缓存失效 | **Engineer** | `invalidate_code_entry` 持任务引用或改 `await`（按调用上下文） | Engineer |
| R4b factory_reset | **Engineer** | 重置时同步清空 `database/cache.py` 内存 `QueryCache` | Engineer |
| R5 `$or` 翻译 | **Engineer** | 补全 `$or` 非 `$regex` 子条件翻译 | Engineer |
| R6a/R6b 崩溃/泄漏 | **Engineer** | `shutdown` 取消后台任务；`get_best_account` 用 `.get(k, default)` | Engineer |
| R7a/R7b 安全配置 | **Engineer** | `force_join` 异常按配置 fail-closed；`settings` 错拼键告警 | Engineer |
| R8a/R8b 码逻辑 | **Engineer** | 提取纯码；生成查重/冲突重试 | Engineer |
| R9 技术债 | QA 记录为 tech-debt，**不阻塞**发布 | 排期清理；其中 `import` 期崩溃（R9）若缺 env 即崩，建议 Engineer 隔离 | NoOne（跟踪） |
| R7c 待核实项 | QA 先补防御测试确认 | 若测试通过则关闭；若失败则转 Engineer | 视测试结果定 |

**QA 自身责任**：本评估交付后，QA 应立即落地 §4 框架与 `tests/` 骨架，并优先将 §3.2 的 P0 失败测试入库（这些测试在源码修复前必然失败，正是缺陷证据）。QA 不参与修源码，仅以测试驱动 Engineer 修复。

### 5.3 一句话判定
> **上线前必须先由 Engineer 修复 R1–R8（🔴+🟠）源码，并由 QA 以 P0 测试套件（当前预期失败）锁定回归；R9 作为技术债跟踪、不阻塞发布。在 R1–R5 未修复且无测试保护前，禁止发布。**

---

## 附：本次核实证据索引（file:line）
- R1：`bots/idx_bot.py:272,1839`；对照 `bots/up_bot.py:847-857`（有白名单）
- R2：`database/session.py:25,27,605,610,614,616,709,723,741,750`
- R3：`services/db_backup.py:37,40-54,274,282-284,230`；`services/db_restore.py`（恢复链路）
- R4：`database/cache.py:148-162,159`；`bots/admin_bot/handlers.py:689-790`
- R5：`database/session.py:888-897,949-961`
- R6：`services/relay_instance.py:1024-1026,157,523`；`services/relay_pool.py:104-107`
- R7：`utils/force_join.py:13,24,28,32,36`；`config/settings.py:185,313`
- R8：`services/code_generator.py:110-112,176-178`
- R9：`services/relay_instance.py:273,405,528,535,597,632,644,662`；`utils/monitor.py:50-54`；`utils/dynamic_rate_limiter.py:39`

*注：本报告为 QA 评审结论，未修改任何源码、未编写完整测试套件，仅提供伪代码级示例与可执行建议。*
