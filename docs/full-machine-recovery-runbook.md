# 全新机恢复操作手册

## 适用场景
- 生产环境完全损坏(硬件故障/数据丢失/被攻击)
- 需要在新机器上从备份恢复完整服务
- RTO 目标: ≤ 30 分钟

## 前置条件
1. 新机器已安装:
   - Docker + docker-compose
   - Python 3.10+
   - git
   - systemd(Linux)
2. 已安全传输到新机器的文件:
   - .env.shared(公共配置)
   - .env.secrets.*(各服务的 secrets)
   - GitHub PAT(用于拉取代码)
3. 外部依赖可用:
   - CRDB Cloud 集群可用
   - R2 bucket 可访问
   - Telegram Bot Token 有效
   - Redis 可用(或在新机器启动)

## 恢复步骤

### Step 1: 准备环境
```bash
# 克隆代码
git clone https://github.com/maxiuquan/tgjiema.git
cd tgjiema

# 恢复 secrets 文件
# (从安全渠道传输 .env.shared 和 .env.secrets.* 到项目根目录)

# 配置 /etc/hosts(R2 DNS pinning)
sudo ./deploy_vps_per_bot.sh  # 会自动配置 /etc/hosts
```

### Step 2: 执行恢复
```bash
./scripts/full_machine_recovery.sh
```

### Step 3: 验证
1. 访问 Admin Web: https://your-domain/login
2. 向 Telegram bot 发送测试消息
3. 上传测试文件验证解码流程
4. 运行 `python scripts/export_ru_report.py --hours 1` 验证 RU 消耗

## 故障排查

### Redis 启动失败
```bash
docker-compose logs redis
# 检查端口冲突: netstat -tlnp | grep 6379
```

### Migration 失败
```bash
journalctl -u tgjiema-migration -n 100
# 检查 CRDB 连接: python -c "import asyncpg; asyncpg.connect(COCKROACHDB_URL)"
```

### CRDB 同步未完成
```bash
journalctl -u tgjiema-crdb_sync -n 100
# 手动触发同步: systemctl restart tgjiema-crdb_sync
```

### Admin Web 不可访问
```bash
journalctl -u tgjiema-admin -n 100
# 检查端口: netstat -tlnp | grep 8080
```

## 恢复后检查清单
- [ ] 所有 systemd 服务 active
- [ ] Admin Web 可登录
- [ ] Telegram bot 响应测试消息
- [ ] 文件上传/解码测试通过
- [ ] 72h 空载 RU 报告达标
- [ ] 备份计划已恢复
- [ ] 监控告警已恢复
