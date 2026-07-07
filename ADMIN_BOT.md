# 管理员机器人操作文档

## TLS 要求

管理后台（FastAPI）**无内置 TLS**，生产环境**强烈建议**前置 Caddy 或 Nginx 反代启用 HTTPS。

### 为什么需要 TLS

- 管理后台使用 HTTP Basic Auth，凭据经 HTTP 明文传输 → 可被中间人截获
- CSRF Cookie 默认 `Secure=False`（兼容无 TLS 的本地开发），但纯 HTTP 下写操作（修改会员、封禁、删文件等）的安全性大幅降低
- 若强行 `Secure=True` 而前端为 HTTP，浏览器会**拒绝设置 Cookie** → 所有管理后台写操作返回 `403 CSRF token 验证失败`

### 启用方式

#### 自动（推荐）：deploy_tls_caddy.sh

部署脚本会自动检测 Caddy 是否已安装；已安装则生成模板。也可手动执行：

```bash
sudo bash deploy_tls_caddy.sh your-domain.com
```

脚本会自动：
1. 检测 `.env` 中的 `ADMIN_WEB_HOST` / `ADMIN_WEB_PORT`
2. 生成 Caddyfile 反代配置（`localhost:8080` → HTTPS）
3. 自动 Let's Encrypt 证书
4. 设置 `CSRF_COOKIE_SECURE=1`

#### 手动：部署脚本内置提示

`deploy_vps.sh`（第十步）和 `deploy_vps_per_bot.sh`（第九步）末尾均包含 TLS 配置检查块——当检测到 `caddy` 已安装时自动生成 `/etc/caddy/Caddyfile.tgjiema` 模板，否则给出安装提示。

#### CSRF Cookie Secure 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `CSRF_COOKIE_SECURE` | `false`（即 `0`） | 设为 `1` / `true` / `yes` 启用 `Secure` Cookie |

```ini
# .env
CSRF_COOKIE_SECURE=1
```

> **本地开发**：默认关闭 Secure → HTTP `localhost` 可正常登录和操作。
> **生产部署**：启用 TLS 后务必设 `CSRF_COOKIE_SECURE=1`。

### 反代架构

```
浏览器 ──[HTTPS]──→ Caddy (:443) ──[HTTP]──→ FastAPI (:8080 / 127.0.0.1)
                        │
                  Let's Encrypt
                   自动证书
```

> **⚠ 警告**：HTTP 下管理凭据明文传输，**仅限可信内网**使用。公网部署无 TLS 的管理后台存在严重安全隐患。

---

## 部署配置

在 `.env` 中配置以下内容：

```ini
# 管理员机器人 Token（从 @BotFather 获取）
ADMIN_BOT_TOKEN=your_bot_token_here

# 管理员用户的 Telegram User ID（用于权限校验）
# 获取方式：给 @userinfobot 或 @getidsbot 发送任意消息即可看到
ADMIN_TELEGRAM_ID=123456789
```

配置完成后启动系统：

```bash
python3 run_all.py
```

系统会自动启动管理员机器人。只有 `ADMIN_TELEGRAM_ID` 指定的用户可以使用该机器人。

---

## 命令列表

### 系统状态

| 命令 | 说明 |
|------|------|
| `/start` | 显示帮助菜单 |
| `/status` | 显示系统概览（用户数、文件数、今日解码、机器人状态） |
| `/health` | 显示所有机器人的健康状态和最后活跃时间 |

---

### 用户管理

| 命令 | 说明 |
|------|------|
| `/user <id>` | 查看用户详情（会员等级、配额、封禁状态等） |
| `/users [关键词] [页码]` | 用户列表，支持按用户名/昵称搜索 |
| `/set_level <id> <free\|basic\|premium>` | 设置用户会员等级 |
| `/ban <id>` | 封禁用户（禁止使用系统） |
| `/unban <id>` | 解封用户 |
| `/set_quota <id> <数量>` | 设置每日解码配额（-1 为不限） |
| `/set_external_quota <id> <数量>` | 设置外部码每日配额（-1 不限，0 禁止） |

#### 会员等级说明

| 等级 | 说明 | 默认解码配额 |
|------|------|------------|
| `free` | 免费用户 | 3次/天（可配置） |
| `basic` | 基础会员 | 20次/天（可配置） |
| `premium` | 高级会员 | 不限（可配置） |

---

### 文件管理

| 命令 | 说明 |
|------|------|
| `/file <code>` | 查看文件详情（类型、上传者、状态、请求次数、备份信息） |
| `/files [搜索] [页码]` | 文件列表，支持按文件码搜索 |
| `/delete_file <code>` | 软删除文件（状态设为 deleted） |

---

### 日志

| 命令 | 说明 |
|------|------|
| `/logs [页码]` | 查看解码日志，按时间倒序排列 |

---

## 使用示例

```bash
# 查看系统状态
/status

# 查看用户 123456 的信息
/user 123456

# 将用户 123456 设为高级会员
/set_level 123456 premium

# 搜索用户名包含 "test" 的用户
/users test

# 查看第2页用户列表
/users 2

# 查看文件码 tgwenjian_abc123_2p_1v
/file tgwenjian_abc123_2p_1v

# 查看第3页解码日志
/logs 3
```
