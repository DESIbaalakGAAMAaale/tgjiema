# CRDB RU 验收文档

## 门禁标准(R44 7.3)

| 指标 | 目标 | 阻断阈值 |
|------|------|----------|
| 业务角色 RU/天 | 0 | > 0 阻断 |
| 总空载 RU/天 | ≤ 20(理想) | > 100 阻断 |
| 连续 72h 空载 | 从 CRDB Cloud 官方 Metrics 导出 | > 500 RU/天阻断 |

## 验收步骤

### 1. 配置 CRDB Cloud API
```bash
export CRDB_CLOUD_API_KEY="your-api-key"
export CRDB_CLOUD_CLUSTER_ID="your-cluster-id"
```

### 2. 运行 72h 报告
```bash
# 部署后等待 72 小时,确保无业务操作
python scripts/export_ru_report.py --hours 72 --output ru_report_72h.json
```

### 3. 检查报告
- `verdict` 应为 `PASS`
- `business_idle_ru_per_day` 应为 0
- `total_idle_ru_per_day` 应 ≤ 20(理想)或 ≤ 100(硬上限)

## RU 优化措施(R44 7.2)

1. **version 字段原子递增**: SQLite 层 MAX(version)+1,避免并发碰撞
2. **tombstone soft_delete**: 添加 is_tombstone 列,优先 UPDATE 而非 DELETE
3. **local_only 表不写 dirty_outbox**: 从源头跳过 CRDB 同步
4. **migration/backup/restore RU 单独统计**: 不混入业务空载
5. **RU collector 失败输出 unknown**: 三态(official/unknown/failed)+ freshness

## 故障注入测试

### 72h 故障注入
```bash
# 模拟网络分区
docker network disconnect tgjiema_default tgjiema-crdb_sync-1
# 等待 1 小时
# 恢复
docker network connect tgjiema_default tgjiema-crdb_sync-1
# 验证数据一致性
python -m pytest tests/test_crdb_convergence.py -v
```

### 新机恢复演练
```bash
# 在新机器上执行
./scripts/full_machine_recovery.sh
# 验证 RTO ≤ 30 分钟
```
