# R38 P1-5: 备份 Manifest / Checksum 顺序规范

## 背景

R35 P1-7 实现的 bundle manifest 存在顺序缺陷:`backup_all_tables()` 在**脱敏之前**就构建了 manifest(含 checksum),然后 `_run_backup_loop()` 才调用 `_redact_secrets()` 脱敏。这导致:

- manifest 中的 `checksum_sha256` 基于未脱敏数据计算
- 实际上传到 R2 的 plaintext 是脱敏后的数据
- 恢复时 `verify_checksum()` 会失败(checksum 与 plaintext 不匹配)

## R38 P1-5 修正后的正确顺序

```
┌─────────────────────────────────────────────────────────────┐
│  1. 采集(backup_all_tables)                                 │
│     ├─ CRDB 表(SELECT * / 增量 watermark)                   │
│     ├─ SQLite 表(cache_store.db / relay_pool.db)            │
│     └─ 合并 all_tables                                       │
│                                                              │
│  2. 提取 metadata(pop _r38_p1_5_metadata,不写入 payload)    │
│                                                              │
│  3. 脱敏(_redact_secrets)                                    │
│     └─ 替换敏感字段为 ***REDACTED***                          │
│                                                              │
│  4. 序列化 plaintext(json.dumps,utf-8)                      │
│     └─ 此时 plaintext 是脱敏后的最终数据                      │
│                                                              │
│  5. 生成最终 manifest(_build_bundle_manifest)               │
│     ├─ checksum_sha256 = SHA256(plaintext)  ← 基于脱敏数据    │
│     ├─ commit_sha / schema_version / table_stats             │
│     ├─ backup_type / watermark / prev_watermark              │
│     └─ encryption = {encrypted: False}  ← 占位,稍后填充       │
│                                                              │
│  6. 加密(encrypt_payload)                                   │
│     ├─ 生成随机 DEK                                          │
│     ├─ AES-256-GCM 加密 plaintext                            │
│     ├─ KEK 包装 DEK → wrapped_dek                            │
│     └─ 返回 {ciphertext, wrapped_dek, nonce, key_id}         │
│                                                              │
│  7. 补充 manifest envelope(加密元信息)                       │
│     └─ manifest.encryption = {                               │
│            encrypted: True,                                  │
│            algorithm: "AES-256-GCM",                        │
│            wrapped_dek: <base64>,                           │
│            nonce: <base64>,                                 │
│            key_id: <sha256(kek)[:16]>  ← 不可逆标识          │
│        }                                                     │
│                                                              │
│  8. 原子上传(两次 R2 PUT)                                   │
│     ├─ db_backup/manifest_{ts}_{type}.json                   │
│     └─ db_backup/db_backup_{ts}_{type}.json (ciphertext)     │
│                                                              │
│  9. 保存 watermark(用于下次增量)                             │
└─────────────────────────────────────────────────────────────┘
```

## 关键不变量

1. **checksum 基于脱敏后的 plaintext** — 恢复时 `verify_checksum(ciphertext 解密后的 plaintext, manifest.checksum_sha256)` 必须返回 True
2. **manifest 与 ciphertext 分离存储** — manifest 是明文 JSON(含 wrapped_dek/nonce/key_id),ciphertext 是加密后的二进制
3. **key_id 不可逆** — manifest 中的 `key_id = sha256(kek)[:16]`,无法从 key_id 反推 KEK
4. **metadata 不写入 payload** — `_r38_p1_5_metadata` 在序列化前被 pop 出来,不会出现在备份文件中

## 恢复时校验流程

```
1. 下载 manifest_{ts}_{type}.json
2. 下载 db_backup_{ts}_{type}.json (ciphertext)
3. 从 manifest.encryption 提取 wrapped_dek / nonce / key_id
4. 用 KEK 解包 DEK,用 DEK 解密 ciphertext → plaintext
5. verify_checksum(plaintext, manifest.checksum_sha256) → 必须为 True
6. validate_manifest_on_restore(manifest, expected_schema_version)
7. 解析 plaintext 为 backup_data,执行表级恢复
```

## 相关文件

- `services/db_backup.py` — `backup_all_tables()` + `_run_backup_loop()` + `_build_bundle_manifest()`
- `services/backup_crypto.py` — `encrypt_payload()` / `decrypt_payload()` / `verify_checksum()`
- `services/backup_schema.py` — 表清单与 source 分组(单一事实源)
