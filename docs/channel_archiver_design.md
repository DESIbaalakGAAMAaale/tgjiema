# 频道归档器（Channel Archiver）— 子项目设计文档

## 1. 项目定位

独立于主系统 `tgjiema` 之外运行的子项目，通过 Telegram Bot 读取目标频道中的历史媒体文件，智能分组后存入主系统数据库并生成文件码。

- **独立运行**：可单独启动，不依赖主系统的 5 个 Bot 进程
- **共享基础设施**：共用主系统的 CockroachDB、`code_generator`、`jobs` 表
- **只读操作**：Bot 加入频道后只读取消息，不发送、不修改、不删除

---

## 2. 核心流程

```
┌──────────────────┐     ┌─────────────────────┐     ┌──────────────────┐
│ ① 频道监听/同步   │ ──→ │ ② 智能分组判定       │ ──→ │ ③ 生成文件码存储   │
│ (archiver_bot)   │     │ (grouping engine)    │     │ (写入 codes 表)   │
└──────────────────┘     └─────────────────────┘     └──────────────────┘
                                                                │
                                                                ▼
                                                       ┌──────────────────┐
                                                       │ ④ 用户可取件      │
                                                       │ (复用 idx/dsp)   │
                                                       └──────────────────┘
```

### 阶段说明

| 阶段 | 描述 |
|------|------|
| ① 频道同步 | Archiver Bot 加入频道后，通过 `get_history` / `get_chat` 拉取历史消息，并持续监听新消息 |
| ② 智能分组 | 分析连续消息的 caption、media_group_id、页码标记，判定是否属于同一组 |
| ③ 存储归档 | 将分组后的媒体文件存储到主系统的 cells 频道，生成文件码写入 `codes` 表 |
| ④ 用户取件 | 复用主系统现有的 idx_bot 解码 + dsp_bot 派送流程，用户用文件码取件 |

---

## 3. 项目结构

```
tgjiema/
├── archiver/                        # 独立子项目目录
│   ├── __init__.py
│   ├── archiver_bot.py              # Bot 主入口（监听消息）
│   ├── channel_sync.py             # 历史消息同步引擎
│   ├── grouping_engine.py          # 智能分组核心逻辑
│   ├── archiver_storage.py         # 存入主系统的桥接模块
│   ├── config.py                   # 子项目独立配置
│   └── run.py                      # 独立启动入口
├── config/
│   └── archiver_channels.yaml      # 待归档频道配置
├── database/                       # 复用主系统
├── services/                       # 复用主系统
└── ...                             # 主系统其他文件不变
```

---

## 4. 智能分组引擎（核心）

### 4.1 分组规则

分组引擎对频道内连续消息进行分析，满足以下任一条件则判定为 **同一组**：

#### 规则 A：Telegram 原生 media_group_id
- Telegram 发送的媒体组自带 `media_group_id`
- **最可靠的判定依据**，直接按 `media_group_id` 分组

#### 规则 B：相同 caption（文字描述）
- 连续 N 条消息的 `caption` 完全相同（默认 N ≥ 3）
- 例如：连续 5 条视频的 caption 都是 `"某某教程"`，判定为一组

#### 规则 C：页码/编号标记
- caption 中包含连续编号模式（默认连续 ≥ 3 条）
- 支持的编号格式：
  - `1/5`、`2/5`、`3/5`...（分数格式）
  - `part1`、`part2`、`part3`...（part 格式）
  - `第1集`、`第2集`、`第3集`...（中文序号）
  - `#1`、`#2`、`#3`...（井号格式）
  - `01`、`02`、`03`...（纯数字前缀或后缀，需配合相同 caption 前缀）

#### 规则 D：文件类型 + 尺寸一致
- 连续消息的文件类型相同（全是视频/全是图片）
- 且文件大小在 ±30% 范围内
- 且 caption 为空或完全相同
- 默认连续 ≥ 5 条才触发

#### 规则 E：时间间隔
- 连续消息之间的时间间隔 ≤ 设定阈值（默认 5 秒），且 caption 相似度 ≥ 80%

### 4.2 分组优先级

```
media_group_id（最高） ＞ 页码标记 ＞ 相同 caption ＞ 文件类型一致 ＞ 时间间隔（最低）
```

### 4.3 分组算法流程

```
输入：频道消息列表（按时间排序）

1. 遍历消息，提取有媒体文件的消息及其 caption、media_group_id、时间戳
2. 按 media_group_id 预分组
3. 对未分组的消息，滑动窗口扫描：
   - 窗口大小动态扩大（3～10）
   - 对窗口内的消息依次判定规则 B/C/D/E
   - 命中任一规则 → 标记为一组
4. 剩余未分组消息作为「单文件」处理
5. 输出分组结果列表
```

### 4.4 配置参数

```yaml
# archiver_channels.yaml
channels:
  - channel_id: -1001234567890
    channel_name: "示例频道"
    grouping:
      min_group_size: 3          # 最少连续 N 条才触发分组
      max_group_size: 20         # 单组最多 N 条
      same_caption_min: 3        # 相同 caption 最少条数
      page_number_min: 3         # 页码编号最少条数
      same_type_min: 5           # 同类型最少条数
      time_gap_seconds: 5        # 时间间隔阈值
      caption_similarity: 0.8    # caption 相似度阈值
    sync:
      mode: "full"               # full(全量) | incremental(增量) | realtime(实时)
      batch_size: 100            # 每批拉取消息数
      start_from: 0              # 起始消息 ID，0=从最新开始
```

---

## 5. 与主系统的集成

### 5.1 写入流程

```
分组结果
    │
    ▼
archiver_storage.py（桥接模块）
    │
    ├─→ 调用主系统 up_bot 逻辑：将媒体文件转发到 Active A 槽
    │       （使用 forward_messages 保留 file_id）
    │
    ├─→ 调用 code_generator.generate_unique_code() 生成文件码
    │
    ├─→ 写入 codes 表（make_code_entry）
    │       batch_msg_ids: 该组所有消息的 ID（JSON 数组）
    │       batch_file_meta: 原始频道信息 + 分组元数据
    │
    └─→ 写入 file_records 表（make_file_record）
```

### 5.2 关键 API 调用

| 操作 | 使用的主系统模块 |
|------|-----------------|
| 获取当前 Active A 槽 | `database.get_active_cells()` |
| 转发消息到存储频道 | `bot.forward_message()` / `bot.copy_message()` |
| 生成文件码 | `services.code_generator.generate_unique_code()` |
| 写入 codes 表 | `database.make_code_entry()` + `get_codes_col()` |
| 归档日志 | `database.make_rotate_log()` 复用，记入 rotate_log 表 |

### 5.3 数据模型复用

归档产生的文件码记录与用户手动上传完全一致，用户通过现有的 idx_bot / dsp_bot 流程取件时 **无需任何改动**。

---

## 6. 运行模式

### 6.1 运行模式

| 模式 | 说明 | 触发方式 |
|------|------|---------|
| **实时模式** | Bot 加入频道后持续监听新消息，实时分组归档 | `python -m archiver.run --mode realtime` |
| **历史同步** | 一次性拉取频道全部历史消息并归档 | `python -m archiver.run --mode full` |
| **增量同步** | 从上次同步位置继续拉取 | `python -m archiver.run --mode incremental` |

### 6.2 启动命令

```bash
# 独立运行（不启动主系统）
python -m archiver.run

# 与主系统一起运行（docker-compose 中新增一个 service）
docker-compose up archiver
```

### 6.3 管理命令

```
/start_sync <channel_id>     # 开始同步指定频道
/stop_sync <channel_id>      # 停止同步
/status                      # 查看所有频道同步状态
/group_test <msg_id>         # 测试分组：显示从某条消息开始的分组结果
```

---

## 7. 数据库变更

### 7.1 新增集合/表

```
archiver_channels          # 归档频道配置与同步状态
  - channel_id: int (PK)
  - channel_name: str
  - last_synced_msg_id: int
  - total_synced: int
  - total_groups: int
  - sync_status: str (idle|syncing|error)
  - config: json (分组参数)
  - created_at: datetime
  - updated_at: datetime

archiver_group_log         # 分组归档日志
  - group_id: str (PK)
  - channel_id: int
  - file_code: str
  - msg_ids: list[int]
  - group_rule: str (media_group|caption|page_number|same_type|time_gap)
  - file_count: int
  - created_at: datetime
```

### 7.2 codes 表扩展现有字段

codes 表的 `batch_file_meta` 字段（JSON 字符串）增加归档相关元数据：

```json
{
  "source": "archiver",
  "channel_id": -1001234567890,
  "channel_name": "示例频道",
  "group_rule": "page_number",
  "original_msg_ids": [101, 102, 103, 104, 105]
}
```

---

## 8. 技术选型

| 项目 | 选择 | 理由 |
|------|------|------|
| Bot 框架 | python-telegram-bot | 与主系统统一 |
| 数据库 | CockroachDB | 复用主系统数据库 |
| 异步 | asyncio | 与主系统一致 |
| 日志 | loguru | 与主系统一致 |
| 配置 | YAML + .env | YAML 管频道，.env 管 Token |

---

## 9. 部署

### docker-compose.yml 新增

```yaml
  archiver:
    build: .
    command: python -m archiver.run
    environment:
      - ARCHIVER_BOT_TOKEN=${ARCHIVER_BOT_TOKEN}
      - COCKROACHDB_URL=${COCKROACHDB_URL}
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
    restart: unless-stopped
```

### .env 新增

```
ARCHIVER_BOT_TOKEN=123456:ABC-DEF1234ghijkl
```

---

## 10. FAQ / 边界情况

| 问题 | 处理方式 |
|------|---------|
| 频道消息量巨大（数万条） | 分批拉取 + 断点续传，每批 100 条，记录 last_synced_msg_id |
| 分组判定模糊（如相同 caption 但中间有广告） | 允许 1 条「杂质消息」插入（可配置），超过则断开分组 |
| 超大文件（>2GB Telegram 限制） | 记录日志并跳过，标记为 `skipped` |
| 频道内已有主系统的 A/S 槽频道 | 内部频道自动跳过，通过 cells 表查询判断 |
| 重复归档 | 通过 msg_id 去重，已归档的消息不再处理 |
| Bot 权限不足（非管理员） | 启动时检测权限，无读取权限则报错退出 |
| 消息被删除 | forward_message 会失败，记录为 `deleted` 并跳过 |
| 混合媒体组（图片+视频混合） | 保留混合分组，file_types 记录各类型数量 |

---

## 11. 开发路线

| 阶段 | 内容 | 产出 |
|------|------|------|
| P1 | 基础框架搭建 | archiver_bot 可启动、可拉取消息 |
| P2 | 分组引擎实现 | 5 条分组规则全部实现 |
| P3 | 主系统集成 | 文件转发 + 码生成 + codes 写入 |
| P4 | 管理命令 | /start_sync、/status 等 |
| P5 | 配置与部署 | archiver_channels.yaml、docker-compose 集成 |