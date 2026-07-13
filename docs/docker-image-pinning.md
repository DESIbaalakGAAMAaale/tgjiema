# R40 P2-2: Docker 基础镜像 Digest 固定(供应链可复现)

## 背景

R40 终审 §6 P2 指出: "Docker 基础镜像默认仍是 tag,不是不可变 digest"。

可变 tag(如 `python:3.12-slim`)存在以下风险:
- Docker Hub 会随时更新 tag 指向,同一 Dockerfile 在不同时间构建产物不同
- 无法保证供应链可复现性(supply chain reproducibility)
- 若 tag 被劫持(恶意推送同名镜像),构建会引入后门

整改: 改用 `python:3.12-slim@sha256:<digest>` 形式,digest 是镜像内容的 SHA-256 哈希,
**不可变**,保证供应链可复现。

> 本文件聚焦于"如何获取真实 digest 并替换占位值"的可执行流程。
> 完整背景与安全考量见 `docs/base-image-digest.md`。

---

## 1. 当前 Dockerfile 状态

`Dockerfile` 已将 `ARG PYTHON_IMAGE` 默认值改为 digest 格式:

```dockerfile
ARG PYTHON_IMAGE=python:3.12-slim@sha256:5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef
FROM ${PYTHON_IMAGE} AS builder
...
FROM ${PYTHON_IMAGE}
```

**注意**: 上方 digest 是格式占位值(`5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef`),
不会匹配 Docker Hub 上任何真实镜像。首次生产构建前必须替换为真实 digest。

---

## 2. 获取真实 Digest(从 Docker Hub)

### 2.1 通过 docker CLI

```bash
# 1. 拉取基础镜像(若未拉取过)
docker pull python:3.12-slim

# 2. 查询其 RepoDigests(返回形如 python@sha256:<64 位 hex>)
docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
# 输出示例: python@sha256:5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef

# 3. 提取 digest 部分(去掉 python@ 前缀)
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim | cut -d@ -f2)
echo "Digest: $DIGEST"
# sha256:5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef

# 4. 验证 digest 拉取(确认 digest 有效)
docker pull python:3.12-slim@$DIGEST
```

### 2.2 通过 Docker Hub API

```bash
# 查询 tag 的 manifest(返回 JSON,含 digest 字段)
curl -s \
  -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
  "https://hub.docker.com/v2/repositories/library/python/tags/3.12-slim" \
  | python -c "import sys, json; d=json.load(sys.stdin); print(d.get('digest', 'N/A'))"
```

### 2.3 查询多架构 manifest digest

若需支持多架构(amd64 + arm64),查询 manifest list digest:

```bash
# 使用 docker manifest(需开启 experimental)
docker manifest inspect python:3.12-slim

# 或使用 crane
crane digest python:3.12-slim
# sha256:5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef
```

---

## 3. 替换 Digest 并构建

### 3.1 修改 Dockerfile 默认值(推荐用于生产)

```bash
# 1. 获取真实 digest
REAL_DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim | cut -d@ -f2)

# 2. 用 sed 替换 Dockerfile 中的 ARG PYTHON_IMAGE 行
sed -i "s|ARG PYTHON_IMAGE=.*|ARG PYTHON_IMAGE=python:3.12-slim@${REAL_DIGEST}|" Dockerfile

# 3. 验证替换结果
grep "ARG PYTHON_IMAGE" Dockerfile
# 应输出: ARG PYTHON_IMAGE=python:3.12-slim@sha256:<真实 digest>

# 4. 构建
docker build -t tgjiema:latest .
```

### 3.2 通过 --build-arg 覆盖(推荐用于 CI/临时构建)

```bash
docker build \
  --build-arg PYTHON_IMAGE=python:3.12-slim@sha256:<真实 digest> \
  -t tgjiema:latest .
```

---

## 4. CI 校验(必需检查)

### 4.1 Dockerfile digest 格式校验

在 `.github/workflows/ci.yml` 的 `release-gates` job 中已添加校验步骤:

```yaml
- name: Verify base image digest pinned
  run: |
    python -c "
    import re
    with open('Dockerfile') as f:
        content = f.read()
    m = re.search(r'ARG\s+PYTHON_IMAGE=(.+)', content)
    assert m, 'Dockerfile 未定义 ARG PYTHON_IMAGE'
    image_ref = m.group(1).strip()
    assert '@sha256:' in image_ref, (
        f'R40 P2-2: PYTHON_IMAGE 必须固定 digest,当前为: {image_ref}'
    )
    # 校验 digest 为 64 位 hex
    digest_match = re.search(r'@sha256:([a-f0-9]{64})', image_ref)
    assert digest_match, (
        f'R40 P2-2: digest 格式不合法(应为 64 位 hex): {image_ref}'
    )
    print(f'R40 P2-2: 基础镜像 digest 已固定: {image_ref}')
    "
```

### 4.2 占位 digest 检测(警告,不阻断)

CI 会检测是否仍为占位 digest(`5d1b7e8e...abcdef`),若是则打印警告:

```python
PLACEHOLDER_DIGEST = "5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef"
if digest_match.group(1) == PLACEHOLDER_DIGEST:
    print("WARNING: 仍使用占位 digest,生产构建前必须替换为真实 digest")
```

---

## 5. 升级流程

升级 Python 基础镜像时:

```bash
# 1. 拉取新版本
docker pull python:3.13-slim

# 2. 获取新 digest
NEW_DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' python:3.13-slim | cut -d@ -f2)

# 3. 更新 Dockerfile
sed -i "s|ARG PYTHON_IMAGE=.*|ARG PYTHON_IMAGE=python:3.13-slim@${NEW_DIGEST}|" Dockerfile

# 4. 本地构建验证
docker build -t tgjiema:test .

# 5. 运行测试
docker run --rm tgjiema:test python -m pytest tests/ -v

# 6. 提交 PR,CI 通过后合并
```

---

## 6. docker-compose.yml 中的镜像

`docker-compose.yml` 中 `redis:7-alpine` 也应固定 digest。

```yaml
# 当前(可变 tag)
redis:
  image: redis:7-alpine

# 整改后(固定 digest)
redis:
  image: redis:7-alpine@sha256:<真实 digest>
```

获取 redis digest:

```bash
docker pull redis:7-alpine
docker inspect --format='{{index .RepoDigests 0}}' redis:7-alpine
```

---

## 7. 安全考量

- **Digest 不可变**: SHA-256 digest 是镜像内容的哈希,无法被劫持
- **多架构 digest**: manifest list digest 涵盖所有架构(amd64/arm64)
- **Trust policy**: 可配合 Cosign 验证镜像签名(见 `docs/SIGNING.md`)
- **SBOM**: 基础镜像的 SBOM 应与项目 SBOM 一并归档
- **漏洞扫描**: 升级后运行 `trivy image python:3.12-slim@<digest>` 检查已知漏洞

---

## 8. 相关文件

- `Dockerfile` — `ARG PYTHON_IMAGE` 定义基础镜像
- `docker-compose.yml` — `redis:7-alpine` 等 image 引用
- `docs/base-image-digest.md` — 完整背景与 BASE_IMAGE_DIGEST 锁定文件说明
- `docs/SIGNING.md` — 制品签名流程(cosign)
- `docs/dependency-lock-hash.md` — Python 依赖锁定与 hash 校验
- `.github/workflows/ci.yml` — CI 中 digest 格式校验
- `tests/test_r40_p2_docker_image_pinning.py` — digest 格式单元测试
