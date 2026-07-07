# TG文件解码器

基于 Telegram Bot 的文件存储与解码分发系统，采用环形冗余架构，支持高可用文件上传、取件码生成、跨频道中继解码。

## 架构

环形冗余 v2 架构，5 个独立 Bot 进程 + Web 管理后台：

| 进程 | 说明 |
|------|------|
| **Up Bot** | 预铺 15 个 Active 槽位，接收用户上传文件，转发到当前 Active 存储频道 |
| **Idx Bot** | 生成文件码 / 解码（内部码 + 外部码），写入 jobs 派工表 |
| **Dsp Bot** | 轮询 jobs 表，从环形 cells 获取存储频道，以媒体组形式发送给用户（唯一出口） |
| **Mon Bot** | 频道健康监控，自动降级（Active → Shadow1 → Shadow2），环形指针推进 |
| **Admin Bot** | 管理员机器人，管理配置、用户、中继账号、系统监控等 |
| **File Bot** | 独立部署于 Cloudflare Workers，提供入口引导（零成本、零运维） |

详细架构说明参见 [ARCH_OVERVIEW.md](ARCH_OVERVIEW.md)。

## 环形冗余拓扑

- 5 个 Telegram 账号 × 9 个频道 = 45 个频道 = 15 组
- 每组包含 Active + Shadow1 + Shadow2，各来自不同账号
- 两级自动降级：Active → Shadow1 → Shadow2（Mon 自动触发）
- R100 兜底频道：永不自降，独立存档
- 任意一个账号被封，每组至少剩余 2 个频道，无单点故障

## 技术栈

- **语言**: Python 3.12+
- **框架**: Pyrofork / Telethon（MTProto 客户端）
- **数据库**: CockroachDB（主库）+ SQLite（本地缓存）
- **对象存储**: Cloudflare R2（数据库备份）
- **部署**: systemd 独立服务（VPS）+ Cloudflare Workers（File Bot）
- **容器化**: Docker / Docker Compose（可选）

## 快速开始

### 前置条件

- Python 3.12+
- CockroachDB 集群（免费版即可）
- 5 个 Telegram 机器人 Token（从 @BotFather 创建）
- 5 个 Telegram 用户账号，各创建 9 个频道
- 1 个 R100 兜底频道
- Telegram API 密钥（从 https://my.telegram.org 申请）
- Cloudflare R2 存储桶（可选，用于数据库备份）

### 部署

```bash
# 1. 克隆仓库
git clone https://github.com/maxiuquan/tgjiema.git
cd tgjiema

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，填入 Bot Token、频道 ID、数据库连接等

# 3. 一键部署（自动安装依赖、创建 systemd 服务）
sudo bash deploy_vps_per_bot.sh
```

部署后可通过 `systemctl status tgjiema-*` 查看各服务状态。

### Docker 部署

```bash
docker compose up -d
```

### File Bot 部署

File Bot 独立部署于 Cloudflare Workers，代码位于 `cf-workers/file-bot/`。将 `src/index.js` 内容粘贴到 Cloudflare Dashboard 的 Workers 编辑器中，配置环境变量即可。

## 配置

所有配置通过 `.env` 文件管理，详见 `.env.example`。关键配置项：

| 变量 | 说明 |
|------|------|
| `UPLOAD_BOT_TOKEN` | 上传 Bot Token |
| `DECODER_BOT_TOKEN` | 解码 Bot Token |
| `SENDER_BOT_TOKEN` | 分发 Bot Token |
| `ADMIN_BOT_TOKEN` | 管理 Bot Token |
| `MON_BOT_TOKEN` | 监控 Bot Token |
| `COCKROACHDB_URL` | CockroachDB 连接地址 |
| `ACCOUNT_1_CHANNELS` ~ `ACCOUNT_5_CHANNELS` | 5 个账号的频道 ID |
| `R100_CHANNEL` | R100 兜底频道 ID |
| `RELAY_ENCRYPTION_KEY` | 中继账号加密密钥 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Web 管理后台登录凭据 |

## 管理后台

Admin Bot 提供 Telegram 内联管理界面，支持：

- 系统状态监控（/status、/health）
- 用户管理（/user、/users、/set_level、/ban、/unban、/set_quota）
- 文件管理（/file、/files、/delete_file）
- 中继账号管理（/relay_add、/relay_list、/relay_remove 等）
- 解码日志查询（/logs）

Web 管理后台默认监听 `127.0.0.1:8080`，提供用户、文件、日志的 Web 界面。

详细说明参见 [ADMIN_BOT.md](ADMIN_BOT.md)。

## 项目结构

```
.
├── admin/              # Web 管理后台
├── bots/               # Bot 主逻辑
│   ├── up_bot.py       # 上传 Bot
│   ├── idx_bot.py      # 解码 Bot
│   ├── dsp_bot.py      # 分发 Bot
│   ├── mon_bot.py      # 监控 Bot
│   └── admin_bot/      # 管理 Bot
├── cf-workers/         # Cloudflare Workers（File Bot）
├── config/             # 配置（拓扑生成、settings）
├── database/           # 数据库层（CRDB + SQLite）
├── services/           # 业务服务（码生成、备份、监控调度、中继池）
├── storage/            # 存储层（R2、分发解析）
├── utils/              # 工具函数
├── .env.example        # 环境变量模板
├── deploy_vps_per_bot.sh  # VPS 部署脚本
├── deploy_vps.sh          # VPS 部署脚本（备用）
├── docker-compose.yml     # Docker 编排
├── Dockerfile             # Docker 镜像
├── run_all.py             # 本地开发启动入口
├── ARCH_OVERVIEW.md       # 架构详解
└── ADMIN_BOT.md           # 管理 Bot 操作文档
```

## 测试（P0 回归测试套件）

仓库内置基于 pytest 的回归测试套件（`tests/`），覆盖批次二/三已修复的 P0/P1 安全缺陷：中继投递白名单 fail-closed、备份恢复防 SQL 注入与保留真实密钥、日志脱敏、缓存失效持久化、强制加群 fail-closed、媒体类型词表统一、MonBot 优雅退出等。

### 运行

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

### 设计说明

- `tests/conftest.py` 在 import 任何业务模块**之前**，通过 `sys.modules` 注入桩模块（loguru / asyncpg / telethon* / telegram* / aiosqlite / database.cache_store / storage.r2 / storage.delivery_resolver / config），屏蔽本机未安装的 python-telegram-bot / telethon / asyncpg / aiosqlite 等重型依赖，使纯逻辑回归测试可在**无外部服务**（无需 CockroachDB / R2 / Telegram API）的沙箱中真实跑通。
- `telegram.error` 注入的是**真实异常类**（BadRequest / Forbidden / NetworkError / TimedOut 等），因为 `utils/force_join.py` 在 `except` 子句中直接使用，MagicMock 不能作为异常捕获类型。
- `config` 注入测试桩（字段与 `config/settings.py` 对齐）。原因：项目 `config/settings.py` 使用 class-based `Config` 且 `extra = "warn"`，与当前 pydantic v2 不兼容（`extra` 仅接受 allow/forbid/ignore）；为避免改动生产源码，测试桩保持源码零修改。
- 生产依赖仍来自 `requirements.txt`，本套件不修改它。

## 许可

MIT License