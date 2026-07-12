# tgjiema SLO 指标定义（单一事实源）

> 最后更新: 2026-07-12

## 1. 业务 SLO

| SLO | 目标 | 门禁 | 数据源 | 告警阈值 |
|-----|------|------|--------|----------|
| 上传最终可取件率 | ≥99.9% | 按 upload_id 对账 | upload_sessions 表 | <99.5% |
| 内部码投递成功率 | ≥99.5% | 排除用户主动阻断 | delivery_receipts 表 | <99.0% |
| 外部码最终成功率 | ≥98% | 分 Bot/Relay 统计 | relay_spool 表 | <95% |
| 副本因子 | 每文件同组≥2 | Manifest 实际核验 | manifest + replication_tasks | <2 |
| 备份 RPO | ≤6小时 | 月度恢复演练 | db_backup 日志 | >12h |
| 恢复 RTO | ≤30分钟 | 全新机演练 | 恢复日志 | >60min |

## 2. 技术指标

### Redis
| 指标 | 告警阈值 | 查询命令 |
|------|----------|----------|
| Stream length | >10000 | XLEN tgjiema:writer:stream |
| PEL (pending) | >500 | XPENDING tgjiema:writer:stream |
| Oldest pending age | >300s | XPENDING ... IDLE |
| DLQ length | >50 | XLEN tgjiema:writer:dead |
| Memory | >80% maxmemory | INFO memory |
| AOF rewrite | 频率异常 | INFO persistence |

### SQLite
| 指标 | 告警阈值 | 查询方法 |
|------|----------|----------|
| Commit latency | >100ms | WAL 跟踪 |
| WAL size | >100MB | PRAGMA wal |
| Locked 频率 | >10/小时 | 日志统计 |
| Integrity check | 失败 | PRAGMA integrity_check |

### CRDB
| 指标 | 告警阈值 |
|------|----------|
| RU 消耗 | >日配额 80% |
| Pool saturation | >80% |
| Sync lag | >60s |
| Transaction retry | >100/分钟 |

### Telegram
| 指标 | 告警阈值 |
|------|----------|
| FloodWait 频率 | >10/小时 |
| 429 错误 | >5/分钟 |
| Channel access 失败 | >0 |
| Copy latency | >5s |
| Account 生存率 | <80% |

## 3. 监控仪表板建议
- Grafana 仪表板: Redis + SQLite + CRDB + Telegram 四面板
- 告警通道: admin_bot 推送 + 邮件
- 历史数据: Prometheus 30天保留

## 4. 日志规范
统一字段: trace_id / upload_id / file_code / job_id / message_id / transition_id / service / error_code
禁止: 高基数字段直接作为指标标签
