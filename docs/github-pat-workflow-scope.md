# R39 P1-7: CI Workflow 复现性、必需检查与 GitHub PAT 工作流范围

## 背景

R39 终审发现: 仓库的 `.github/workflows` 在 HEAD 90e4cc2 上虽已存在,
但 "592 passed" 不是可复现的 required check,签名也仍为 unsigned。
本文档定义 CI workflow 在主分支保护下的必需检查清单、PAT 工作流范围
以及如何让 CI 通过结果对每次合并都具备可复现性与可追溯性。

---

## 1. 必需检查 (Required Status Checks)

主分支 (`master`/`main`) 必须开启 branch protection,
以下检查为 **required**(必须通过才能合并):

| 检查上下文 | 来源 workflow / job | 用途 |
| ---------- | ------------------- | ---- |
| `test (3.10)` | `.github/workflows/ci.yml` → `test` | Python 3.10 矩阵测试 |
| `test (3.11)` | `.github/workflows/ci.yml` → `test` | Python 3.11 矩阵测试 |
| `test (3.12)` | `.github/workflows/ci.yml` → `test` | Python 3.12 矩阵测试 |
| `lint` | `.github/workflows/ci.yml` → `lint` | flake8 语法/未定义名检查 |
| `deploy-check / verify-deploy` | `.github/workflows/deploy-check.yml` → `verify-deploy` | 部署配置三方一致性 |
| `release-gates` | `.github/workflows/ci.yml` → `release-gates` | Compose/migration/SBOM/依赖扫描 |

### 1.1 设置 branch protection (需管理员 PAT)

```bash
# 使用 fine-grained PAT (见 §2) 或 classic PAT with admin:repo_hook + repo
gh api repos/:owner/:repo/branches/main/protection -X PUT \
  -f required_status_checks[strict]=true \
  -f required_status_checks[contexts][]="test (3.10)" \
  -f required_status_checks[contexts][]="test (3.11)" \
  -f required_status_checks[contexts][]="test (3.12)" \
  -f required_status_checks[contexts][]="lint" \
  -f required_status_checks[contexts][]="deploy-check / verify-deploy" \
  -f required_status_checks[contexts][]="release-gates" \
  -f enforce_admins=true \
  -f required_pull_request_reviews[required_approving_review_count]=1 \
  -f required_pull_request_reviews[dismiss_stale_reviews]=true \
  -f required_pull_request_reviews[require_code_owner_reviews]=false \
  -f restrictions= \
  -f required_linear_history=true \
  -f allow_force_pushes=false \
  -f allow_deletions=false
```

关键参数说明:
- `required_status_checks[strict]=true`: 合并前必须与最新主分支合并(防 stale PR)
- `enforce_admins=true`: 管理员也受规则约束(防止绕过)
- `required_linear_history=true`: 禁止 merge commit,必须 rebase 或 squash(线性历史)
- `allow_force_pushes=false`: 禁止 force push(防历史篡改)
- `allow_deletions=false`: 禁止删除主分支

### 1.2 验证 required checks 已生效

```bash
# 查看当前分支保护规则
gh api repos/:owner/:repo/branches/main/protection

# 应返回 required_status_checks.contexts 包含上述 6 项
# 若返回 404,说明未配置 branch protection
```

---

## 2. GitHub PAT 工作流范围 (Fine-grained PAT)

CI workflow 中需要 PAT 的场景及对应最小权限:

| 场景 | PAT 名称 | 所需权限 | 用途 |
| ---- | -------- | -------- | ---- |
| Release asset 上传 | `tgjiema-release-upload` | `Contents: write`, `Actions: read` | cosign 签名制品上传到 Release |
| Branch protection 配置 | `tgjiema-admin-protection` | `Administration: write` | 一次性配置,配置后撤销 |
| Workflow trigger (手动触发) | `tgjiema-workflow-dispatch` | `Actions: write`, `Contents: read` | 手动触发 workflow_run |
| 依赖更新 (Dependabot) | 不需要 PAT | (Dependabot 内置 GITHUB_TOKEN) | 自动 PR |

### 2.1 严禁授予的权限

以下权限**不应**授予任何 CI 使用 PAT:
- `Administration: write` (除一次性 branch protection 配置外)
- `Secrets: write` (防止窃取仓库 secrets)
- `Workflows: write` (防止修改 workflow 文件本身)
- `Packages: write` (本仓库不发布到 GitHub Packages)
- `Members: write` (组织成员管理)

### 2.2 Fine-grained PAT 创建步骤

1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
2. **Repository access**: Only select repositories → 仅选择 `tgjiema` 仓库
   - 切勿选择 "All repositories" (最小权限原则)
3. **Repository permissions**:
   - Release-upload PAT: `Contents: Read and write` + `Actions: Read-only`
   - Admin-protection PAT: `Administration: Read and write`(配置后立即撤销)
4. **Expiration**: 90 天(到期前轮转)
5. 生成后复制 token(仅显示一次),添加到 Repository Secrets:
   - `RELEASE_UPLOAD_TOKEN` (Release asset 上传)
   - **不要**将 admin-protection PAT 存到 secrets(一次性使用后撤销)

### 2.3 使用 GITHUB_TOKEN 而非 PAT 的场景

GitHub Actions 运行时自动注入 `GITHUB_TOKEN`(无需配置),
适用于以下场景:
- `actions/checkout@v4` (拉取代码)
- `actions/upload-artifact@v4` (上传临时制品)
- `actions/download-artifact@v4` (下载临时制品)
- 读取 PR 元数据、commit 信息

只有以下场景需要 PAT:
- 上传到 GitHub Release (永久存储,需 `Contents: write`)
- 修改 branch protection (需 `Administration: write`)
- 跨仓库触发 workflow (本仓库无此需求)

---

## 3. CI 可复现性保障

### 3.1 "592 passed" 不可复现的根因

R39 终审发现历史 CI 报告 "592 passed" 但无 required check,
原因可能为:
1. branch protection 未配置 required checks → PR 可在测试未通过时合并
2. CI workflow 在某次 commit 中被临时禁用或删除 → 历史绿勾不代表当前 HEAD
3. 测试矩阵未覆盖关键路径 → 592 个测试通过但核心功能未验证
4. `continue-on-error: true` 隐藏了失败 → SBOM/依赖扫描失败被忽略

### 3.2 可复现性整改

1. **branch protection 强制 required checks**: 见 §1.1,所有 6 项必须通过
2. **workflow 文件版本绑定**: CI workflow 本身纳入版本控制,任何修改需 PR review
3. **`continue-on-error` 透明化**:
   - `release-gates` 的 SBOM/依赖扫描目前为 `continue-on-error: true`
   - R39 P1-7: 这两个步骤改为 required(移除 `continue-on-error`)
   - 或在 step 内 `if: always()` 输出失败摘要,job 级别仍 fail-fast
4. **测试覆盖率门槛**:
   - 关键路径 (cache_store / db_writer / crdb_sync / delivery_resolver) 必须有对应测试
   - CI 中运行 `pytest tests/ --cov=database --cov=services --cov-fail-under=60`
5. **矩阵完整性**: Python 3.10/3.11/3.12 三个版本必须全部通过
   - 不允许跳过某个版本的矩阵项

### 3.3 验证 CI 在 HEAD 上可复现

```bash
# 1. 拉取当前 HEAD
git fetch origin
git checkout main

# 2. 本地复现 CI 关键步骤
python -m pip install -r requirements.txt
pip install pytest pytest-asyncio aiosqlite pyyaml flake8
python -m pytest tests/ -v --tb=short --maxfail=5
flake8 . --count --select=E9,F63,F7,F82 --exclude=.git,__pycache__,docs

# 3. 验证 AST 语法(与 CI 一致)
python -c "
import ast, sys
files = [
    'database/cache_store.py', 'database/db_writer.py',
    'database/redis_queue.py', 'database/relay_db.py',
    'database/session.py', 'database/write_router.py',
    'database/dlq_worker.py', 'config/settings.py', 'config/registry.py',
    'services/db_backup.py', 'services/db_restore.py',
    'services/delivery_resolver.py', 'services/backup_schema.py',
    'services/backup_crypto.py', 'services/crdb_sync_service.py',
]
for f in files:
    try:
        ast.parse(open(f, encoding='utf-8').read())
        print(f'OK: {f}')
    except SyntaxError as e:
        print(f'FAIL: {f}: {e}'); sys.exit(1)
print('AST check passed')
"

# 4. 验证 Compose 配置
for svc in migration db_writer crdb_sync up idx dsp mon admin_bot admin db_backup prometheus_exporter; do
  touch ".env.secrets.${svc}"
done
touch .env.shared
docker compose config --quiet

# 5. 对比本地结果与 GitHub Actions 最近一次运行
gh run list --limit 5
gh run view <run-id> --log
```

---

## 4. 签名制品 (Signed Artifacts) 复核

R39 终审指出 "签名也仍为 unsigned",原因可能为:
1. `sign-artifacts` job 的 `continue-on-error: true` → 签名失败被忽略
2. cosign 未配置 OIDC keyless → 签名步骤被跳过
3. `if` 条件不满足 → 非 push 到 master/main 时不触发签名

### 4.1 整改方案

1. **移除 `continue-on-error`**: 签名失败必须 fail build
2. **OIDC keyless 必需**: `permissions.id-token: write` 必须配置
3. **签名产物验证**: 在 Release notes 中附验证命令
4. **签名 job 必须 required**: 添加到 branch protection required checks
   - 注意: `sign-artifacts` 仅在 push 到 master 时触发,不在 PR 上触发
   - 因此 required check 应为 `release-gates`(包含签名前置步骤)
   - `sign-artifacts` 在合并后运行,失败时通过 GitHub Status 反馈

### 4.2 签名验证命令(运维侧)

```bash
# 1. 从 GitHub Release 下载签名产物
gh release download v1.0.0 \
  --pattern "tgjiema-*.tar.gz" \
  --pattern "tgjiema-*.tar.gz.sha256" \
  --pattern "tgjiema-*.pem" \
  --pattern "tgjiema-*.sig"

# 2. 验证 cosign 签名
SHA=$(ls tgjiema-*.tar.gz | head -1 | sed 's/tgjiema-//;s/.tar.gz//')
cosign verify-blob \
  --certificate tgjiema-${SHA}.pem \
  --signature tgjiema-${SHA}.sig \
  --certificate-identity https://github.com/<org>/<repo>/.github/workflows/ci.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  tgjiema-${SHA}.tar.gz.sha256

# 3. 校验 tarball 完整性
sha256sum -c tgjiema-${SHA}.tar.gz.sha256

# 4. 比对签名 SHA 与 tag 对应的 commit
git fetch --tags origin
TAG_SHA=$(git rev-list -n 1 v1.0.0)
[ "$SHA" = "$TAG_SHA" ] && echo "✓ 签名绑定到 tag commit" || echo "✗ 签名 commit 不匹配"
```

---

## 5. 相关文件

- `.github/workflows/ci.yml` — CI 主 workflow (test/lint/fault-injection/release-gates/sign-artifacts)
- `.github/workflows/deploy-check.yml` — 部署配置校验 workflow
- `docs/github-pat-instructions.md` — R38 P1-6: HEAD 签名与 PAT 配置(本文档为其延伸)
- `docs/SIGNING.md` — cosign 签名完整流程
- `docs/least-privilege.md` — 最小权限原则
