# R38 P1-6: CI Workflow HEAD 签名与 GitHub PAT 配置说明

## 背景

R38 P1-6 要求 release 制品(commit / tag / image)的签名绑定到**当前 HEAD commit**,
而非临时分支或可变引用。本文档说明 CI workflow 如何获取 HEAD SHA、如何配置 GitHub PAT
(Personal Access Token)以授权签名 job 上传制品到 Release。

---

## 1. CI Workflow 中的 HEAD SHA 来源

`.github/workflows/ci.yml` 的 `sign-artifacts` job 通过 GitHub Actions 内置变量获取 HEAD:

```yaml
sign-artifacts:
  runs-on: ubuntu-latest
  needs: [test, release-gates]
  if: github.event_name == 'push' && (github.ref == 'refs/heads/master' || github.ref == 'refs/heads/main')
  steps:
    - uses: actions/checkout@v4
      # actions/checkout 默认检出触发 workflow 的 commit(即 HEAD),fetch-depth: 0 可获取完整历史

    - name: Create release artifact (source tarball)
      run: |
        # ${{ github.sha }} = HEAD commit 的完整 SHA(40 字符)
        git archive --format=tar.gz --prefix=tgjiema-${{ github.sha }}/ HEAD -o tgjiema-${{ github.sha }}.tar.gz
        sha256sum tgjiema-${{ github.sha }}.tar.gz > tgjiema-${{ github.sha }}.tar.gz.sha256
```

### 关键点

| 变量 | 值 | 说明 |
| ---- | -- | ---- |
| `github.sha` | HEAD commit SHA | 触发 workflow 的 commit(40 字符 hex),不可变 |
| `github.ref` | `refs/heads/main` | 触发分支的完整引用 |
| `github.event_name` | `push` | 触发事件类型 |

- `github.sha` 是 GitHub 平台注入的**不可变**值,即使分支后续被 force-push,
  该次 workflow 运行的 `github.sha` 仍指向原始 commit
- 签名产物文件名包含完整 SHA(如 `tgjiema-a1b2c3d4....tar.gz`),便于追溯

---

## 2. GitHub PAT 配置(用于 Release 上传)

`sign-artifacts` job 当前仅上传为 GitHub Actions Artifact(临时制品,90 天过期)。
若需将签名产物附加到 GitHub Release(永久存储),需配置 PAT:

### 2.1 创建 Fine-grained PAT

1. 访问 GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
2. 点击 **Generate new token**
3. 配置:
   - **Token name**: `tgjiema-release-upload`
   - **Expiration**: 90 天(到期前轮转)
   - **Repository access**: Only select repositories → 选择 `tgjiema` 仓库
   - **Permissions**:
     - Repository permissions → **Contents**: Read and write(上传 Release asset)
     - Repository permissions → **Actions**: Read-only(读取 workflow run)
4. 生成后复制 token(仅显示一次)

### 2.2 添加到 Repository Secrets

1. 仓库页面 → Settings → Secrets and variables → Actions
2. **New repository secret**
   - Name: `RELEASE_UPLOAD_TOKEN`
   - Value: 粘贴上一步生成的 PAT
3. 保存

### 2.3 在 CI 中使用(可选扩展)

若需将签名产物上传到 GitHub Release,在 `sign-artifacts` job 末尾添加:

```yaml
    - name: Upload to GitHub Release
      if: startsWith(github.ref, 'refs/tags/v')
      env:
        GH_TOKEN: ${{ secrets.RELEASE_UPLOAD_TOKEN }}
      run: |
        # 仅 tag 推送时上传到 Release(tag 触发时 github.ref = refs/tags/v*)
        gh release upload "${GITHUB_REF#refs/tags/}" \
          tgjiema-${{ github.sha }}.tar.gz \
          tgjiema-${{ github.sha }}.tar.gz.sha256 \
          tgjiema-${{ github.sha }}.pem \
          tgjiema-${{ github.sha }}.sig \
          --clobber
```

**注意**: 当前 workflow 仅在 `push` 到 master/main 时触发签名,**不在 tag 推送时触发**。
如需 tag 触发签名,需在 `on:` 中添加 `tags: ['v*']`。

---

## 3. HEAD 签名验证流程

运维下载签名制品后,验证签名绑定的 commit 与当前 HEAD 一致:

```bash
# 1. 从文件名提取 SHA(或从 manifest 的 commit_sha 字段读取)
ARCHIVE="tgjiema-a1b2c3d4e5f6....tar.gz"
SIGNED_SHA="${ARCHIVE#tgjiema-}"      # 去掉前缀
SIGNED_SHA="${SIGNED_SHA%.tar.gz}"     # 去掉后缀
echo "签名绑定的 commit: $SIGNED_SHA"

# 2. 验证签名(cosign verify-blob)
cosign verify-blob \
  --certificate tgjiema-$SIGNED_SHA.pem \
  --signature tgjiema-$SIGNED_SHA.sig \
  --certificate-identity https://github.com/<org>/<repo>/.github/workflows/ci.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  tgjiema-$SIGNED_SHA.tar.gz.sha256

# 3. 校验 tarball SHA256
sha256sum -c tgjiema-$SIGNED_SHA.tar.gz.sha256

# 4. 比对签名 SHA 与远程 HEAD
git fetch origin
REMOTE_HEAD=$(git rev-parse origin/main)
if [ "$SIGNED_SHA" = "$REMOTE_HEAD" ]; then
  echo "✓ 签名 commit 与远程 HEAD 一致"
else
  echo "✗ 签名 commit ($SIGNED_SHA) 与远程 HEAD ($REMOTE_HEAD) 不一致"
  echo "  可能原因: 签名后有新 commit 推送(正常),或签名被篡改(需排查)"
fi
```

---

## 4. 安全注意事项

1. **PAT 最小权限**: 仅授予 `Contents: write` + `Actions: read`,不要授予 `admin` 权限
2. **PAT 轮转**: Fine-grained PAT 最长 1 年,建议 90 天轮转一次
3. **Secret 不入日志**: GitHub Actions 自动 mask `secrets.*` 的值,但避免在 echo 中拼接
4. **OIDC 优先**: cosign 签名使用 GitHub OIDC keyless(无需 PAT),PAT 仅用于 Release asset 上传
5. **HEAD 不可变**: `github.sha` 由平台注入,workflow 运行中不可篡改

---

## 5. 相关文件

- `.github/workflows/ci.yml` — `sign-artifacts` job(HEAD SHA 签名)
- `docs/SIGNING.md` — 签名流程完整文档(含 cosign 命令)
- `docs/backup-manifest-order.md` — 备份 manifest 中的 commit_sha 字段
