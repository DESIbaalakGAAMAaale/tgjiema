# 项目规则

## 项目架构（长期记忆）— 环形冗余架构 v2
- 本项目共 6 个主进程：up / idx / dsp / mon / admin_bot / file_bot
  - **Up Bot (up_bot)**：预铺15个A槽 + 接收用户上传文件 → 转发到当前 Active A 槽
  - **Idx Bot (idx_bot)**：生成文件码 + 解码（内部码/外部码）→ 写 jobs 派工表
  - **Dsp Bot (dsp_bot)**：从 jobs 表轮询任务 → 从环形 cells 获取存储频道 → 媒体组发送给用户（唯一出口）
  - **Mon Bot (mon_bot)**：频道健康监控 + 自动降级(Active→Shadow1→Shadow2) + 环形指针推进
  - **Admin Bot (admin_bot)**：管理员机器人，管理配置、用户、重置等（与 mon 分离）
  - **File Bot (file_bot)**：引导机器人，回复功能引导文本，指引用户使用正确的 Bot
- 三个 backup_bot 已移除，备份逻辑被环形冗余（每个 Active 自带 Shadow1/Shadow2）替代
- 15 个 A 槽(active) + 30 个 S 槽(shadow) = 45 个频道，配置在 config/topology.yaml
- cells 表维护环形链表：cells.next_active_chat_id 形成单向环
- 两级自动降级：Active → Shadow1 → Shadow2 (Mon 自动触发)
- R100 槽位：永不自降，仅手动接管

## 文件发送规则（长期记忆）
- 向用户发送文件时，必须以媒体组（media_group）格式批量发送，保持相册/媒体组形式
- 解码第三方机器人时，必须保持原媒体格式（视频、照片、音频等类型），不走下载->重新上传的方案，直接使用 file_id 传递
- 所有向用户发送文件的操作，必须经过 Dsp Bot 的 jobs 表队列机制，Idx Bot 解码后只写 jobs 表不直发

## 数据库
- CockroachDB，不变
- 新增 4 张表：cells（环形拓扑）、codes（取件码索引）、jobs（派工队列）、rotate_log（降级审计）
- 保留原有表不变

## 部署流程
- 拓扑配置编辑 **config/groups.yaml**（只需填每组的 3 个 channel_id，共 45 行）
- 运行 `python config/generate_topology.py` 自动生成 topology.yaml（环形指针自动计算）
- 运行 `python admin/seed_topology.py` 写入数据库 cells 表
- Docker 部署：`docker-compose up -d --build`
- 一键部署：`.\deploy.ps1`

## Git 提交规则
- 每次完成代码修改后，**自动执行 git 提交和推送**到 GitHub，不需要用户提醒
- 提交前先 `git status` 查看变更，再用 `git diff --stat` 了解改了什么
- 提交信息用中文，简洁描述本次修改内容
- 如果用户要求推送，立即执行 `git add .; git commit -m "描述"; git push`（PowerShell 兼容语法）

## admin_bot 功能 
- 代理中继账号添加及提交中继账号验证码
- 增加频道配置，包括主存储频道、文件码前缀、强制加群频道等
- 管理用户相关操作
- 以及其他可以热更新热配置的操作
- 设置功能按钮，方便管理
- 使用 `/help` 查看所有模块及命令说明

## file_bot 功能
- 引导机器人，用户发送任何消息均返回功能引导文本
- 默认文本从 .env 的 UPLOAD_BOT_USERNAME / DECODER_BOT_USERNAME / SENDER_BOT_USERNAME 拼接
- 引导文本可通过 admin_bot 的 `/set_filebot_msg` 热更新，无需重启
- 无数据库写入，零 RU 消耗
