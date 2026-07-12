# 制品签名流程(R37 P2-1)

本文档说明 TG文件解码器 项目的 release 制品 / 镜像签名流程,基于
[Sigstore cosign](https://github.com/sigstore/cosign) 的 keyless 签名方案,
借助 GitHub OIDC 颁发短期证书(无长期密钥保管负担)。

---

## 1. 背景与目标

R37 商用终审要求:

- **所有 release tag / image HEAD 必须可签名验证**,防止供应链篡改
- **不引入长期密钥**(降低泄露风险),优先使用 OIDC keyless
- **签名产物**(`.sig` + `.pem`)随 artifact 一同发布,供运维/下游校验

CI 已在 `.github/workflows/ci.yml` 添加 `sign-artifacts` job,
在 `master` / `main` 推送时自动触发签名。

---

## 2. Release 制品签名流程(cosign sign-blob keyless)

针对源代码 tarball 等非镜像制品:

```bash
# 1. 创建发布制品(SHA256SUMS 清单)
git archive --format=tar.gz --prefix=tgjiema-<sha>/ HEAD -o tgjiema-<sha>.tar.gz
sha256sum tgjiema-<sha>.tar.gz > tgjiema-<sha>.tar.gz.sha256

# 2. 使用 cosign keyless 签名(GitHub Actions OIDC 身份自动注入)
cosign sign-blob --yes \
  --output-certificate tgjiema-<sha>.pem \
  --output-signature tgjiema-<sha>.sig \
  tgjiema-<sha>.tar.gz.sha256

# 产物:
#   tgjiema-<sha>.tar.gz          — 源码 tarball
#   tgjiema-<sha>.tar.gz.sha256   — SHA256 摘要
#   tgjiema-<sha>.pem             — Sigstore 颁发的证书(含 OIDC 身份)
#   tgjiema-<sha>.sig             — 签名值(base64)
```

CI 工作流:`.github/workflows/ci.yml` → `sign-artifacts` job
依赖:`needs: [test, release-gates]`,只有测试 + 门禁通过才签名。

---

## 3. 镜像签名流程(cosign sign --keyless <image>)

针对推送到 GHCR / Docker Hub 的容器镜像:

```bash
# 1. 登录镜像仓库(GHCR 示例)
echo "${GITHUB_TOKEN}" | docker login ghcr.io -u <user> --password-stdin

# 2. 推送镜像(完整 digest 引用)
docker push ghcr.io/<org>/tgjiema:<tag>@sha256:<digest>

# 3. cosign keyless 签名镜像(GitHub OIDC 身份)
cosign sign --yes \
  --identity-token <github_oidc_token> \
  ghcr.io/<org>/tgjiema:<tag>@sha256:<digest>

# 4. 也可用 recursive 签名多架构 manifest
cosign sign --yes --recursive \
  ghcr.io/<org>/tgjiema:<tag>@sha256:<manifest_digest>
```

**说明**: 镜像签名时务必引用 `@sha256:<digest>`,不要仅引用 `:tag`,
避免 tag 被覆盖后签名指向过时镜像。

---

## 4. 验证签名流程(cosign verify)

### 4.1 验证制品(blob)签名

```bash
# 校验源码 tarball 的 SHA256SUMS 文件签名
cosign verify-blob \
  --certificate tgjiema-<sha>.pem \
  --signature tgjiema-<sha>.sig \
  --certificate-identity https://github.com/<org>/<repo>/.github/workflows/ci.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  tgjiema-<sha>.tar.gz.sha256

# 通过后再核对 tarball 自身的 SHA256
sha256sum -c tgjiema-<sha>.tar.gz.sha256
```

校验链:
1. `cosign verify-blob` 通过 OIDC issuer + identity 验证证书合法性
2. 用证书公钥验签 `.sig` 是否对应 `.sha256` 内容
3. `sha256sum -c` 核对 tarball 实际摘要是否与 `.sha256` 一致

### 4.2 验证镜像签名

```bash
cosign verify \
  --certificate-identity https://github.com/<org>/<repo>/.github/workflows/ci.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  ghcr.io/<org>/tgjiema:<tag>@sha256:<digest>
```

通过则返回 `Verified OK`,失败则报错并阻止下游部署。

运维在拉取镜像后应强制执行验证:

```bash
# deploy_vps_per_bot.sh 中可加入
if ! cosign verify --certificate-identity ... $IMAGE; then
  echo "镜像签名验证失败,拒绝部署"
  exit 1
fi
```

---

## 5. 密钥管理注意事项

### 5.1 Keyless 模式(推荐)

- **无长期密钥**: Sigstore Rekor 自动记录签名证书,证书有效期 10 分钟
- **OIDC 身份绑定**: 证书中嵌入 GitHub workflow 路径 + ref,
  验证时通过 `--certificate-identity` 限定来源
- **审计**: 所有签名记录可公开查询 [Rekor](https://rekor.sigstore.dev)

### 5.2 必要时使用 KMS / Hardware Key

当 keyless 不满足合规要求时,可切换到 KMS 后端:

```bash
# 使用 Google Cloud KMS
cosign sign-blob --key gcpkms://projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k> \
  --output-signature out.sig file.txt

# 使用硬件令牌(YubiKey)
cosign sign-blob --key yubikey:///<slot> file.txt
```

切换时:
- 在 GitHub Secrets 配置 KMS 凭证(而非明文密钥)
- 验证命令使用 `--key` 而非 `--certificate-identity`

### 5.3 凭证保管

- **GitHub OIDC token**: 由 Actions 自动颁发,无需手动管理
- **GHCR PAT**: 仅 `packages:write` 权限,保存在 GitHub Secrets 中
- **KMS 凭证**: 单独 Service Account,限定 `cloudkms.cryptoKeyVersions.useAsymmetricSign`
- **私钥备份(如使用)**: KMS 主备 / HSM 镜像 / 离线保管柜,严禁入仓库

### 5.4 签名失败处理

- `sign-artifacts` job 设置 `continue-on-error: true`,避免签名故障阻塞发布
- 但发布前运维必须确认 `.sig` / `.pem` 已成功上传到 artifact
- 若签名失败,应:
  1. 检查 OIDC token 是否获取成功(`id-token: write` 权限)
  2. 检查 cosign 版本是否与 Rekor 兼容(本仓固定 `v2.2.3`)
  3. 重新触发 workflow 或本地手动签名

---

## 6. CI 集成位置

| 工作流文件 | Job | 触发条件 |
| ---------- | --- | ------- |
| `.github/workflows/ci.yml` | `sign-artifacts` | push 到 `master` / `main` 且 test + release-gates 通过 |

签名产物保留 90 天(`retention-days: 90`),可下载归档。

---

## 7. 验证清单(运维)

部署 / 升级前必检:

- [ ] 下载 tarball + `.sha256` + `.sig` + `.pem` 四件套
- [ ] `cosign verify-blob` 通过
- [ ] `sha256sum -c` 通过
- [ ] 镜像(若使用)有 cosign 签名,且 `cosign verify` 通过
- [ ] Rekor 检索记录与 Git commit SHA 一致

任意一项失败 → 拒绝部署,触发事件响应。
