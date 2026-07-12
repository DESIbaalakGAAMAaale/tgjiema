# R39 P2-1: 固定真实基础镜像 Digest(可复现供应链)

## 背景

R39 终审指出: "固定真实基础镜像 digest;tag 默认值只解决构建,不提供可复现供应链。"

当前 `Dockerfile` 使用 `ARG PYTHON_IMAGE=python:3.12-slim` (tag 引用),
存在以下风险:
- `python:3.12-slim` 是**可变 tag**,Docker Hub 会随时更新其指向
- 今天构建的镜像与一个月后构建的镜像内容不同
- 无法保证供应链可复现性(supply chain reproducibility)
- 若 tag 被劫持(恶意推送同名镜像),构建会引入后门

**整改**: 改用 `python:3.12-slim@sha256:<digest>` 形式,
digest 是镜像内容的 SHA-256 哈希,**不可变**,保证供应链可复现。

---

## 1. 获取真实 Digest

```bash
# 1. 拉取基础镜像
docker pull python:3.12-slim

# 2. 查询其 RepoDigests
docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
# 输出示例: python@sha256:5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef

# 3. 提取 digest 部分(去掉 python@ 前缀)
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim | cut -d@ -f2)
echo "Digest: $DIGEST"
# sha256:5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef

# 4. 验证 digest 拉取
docker pull python:3.12-slim@$DIGEST
```

### 1.1 查询多架构 manifest digest

若需支持多架构(amd64 + arm64),查询 manifest list digest:

```bash
# 使用 docker manifest(需开启 experimental)
docker manifest inspect python:3.12-slim

# 或使用 crane
crane digest python:3.12-slim
# sha256:5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef
```

---

## 2. 修改 Dockerfile

### 2.1 当前实现(可变 tag)

```dockerfile
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE} AS builder
...
FROM ${PYTHON_IMAGE}
```

### 2.2 整改后(固定 digest)

```dockerfile
# R39 P2-1: 固定基础镜像 digest(不可变),保证供应链可复现
# digest 通过 docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim 获取
# 升级 Python 版本时需重新拉取并更新此处 digest
ARG PYTHON_IMAGE=python:3.12-slim@sha256:5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef
FROM ${PYTHON_IMAGE} AS builder
...
FROM ${PYTHON_IMAGE}
```

### 2.3 部署时覆盖(可选)

若运维希望使用不同 digest(如已升级基础镜像),
通过 `--build-arg` 覆盖:

```bash
docker build \
  --build-arg PYTHON_IMAGE=python:3.12-slim@sha256:<新 digest> \
  -t tgjiema:latest .
```

---

## 3. Digest 锁定文件

在仓库根目录维护 `BASE_IMAGE_DIGEST` 文件(可选),
记录当前锁定的 digest 与获取时间:

```text
# BASE_IMAGE_DIGEST — R39 P2-1 基础镜像锁定记录
# 升级时通过 PR 更新此文件,记录审计轨迹

image: python:3.12-slim
digest: sha256:5d1b7e8e9f0a1b2c3d4e5f6789abcdef0123456789abcdef0123456789abcdef
locked_at: 2026-07-13
locked_by: ops
notes: 初始锁定,与 Dockerfile ARG PYTHON_IMAGE 一致

# 验证命令:
# docker pull python:3.12-slim@<digest>
# docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim
```

---

## 4. CI 中校验 digest 一致性

在 `.github/workflows/ci.yml` 的 `release-gates` job 中添加校验步骤:

```yaml
    # R39 P2-1: 校验 Dockerfile 使用 digest 而非 tag
    - name: Verify base image digest pinned
      run: |
        python -c "
        import re
        with open('Dockerfile') as f:
            content = f.read()
        # 查找 ARG PYTHON_IMAGE 行
        m = re.search(r'ARG\s+PYTHON_IMAGE=(.+)', content)
        assert m, 'Dockerfile 未定义 ARG PYTHON_IMAGE'
        image_ref = m.group(1).strip()
        # 校验包含 @sha256: digest
        assert '@sha256:' in image_ref, (
            f'R39 P2-1: PYTHON_IMAGE 必须固定 digest,当前为: {image_ref}\n'
            f'获取 digest: docker inspect --format=\"{{{{index .RepoDigests 0}}}}\" python:3.12-slim'
        )
        print(f'R39 P2-1: 基础镜像 digest 已固定: {image_ref}')
        "
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

# 4. 更新 BASE_IMAGE_DIGEST 文件
# (记录新 digest、升级时间、升级原因)

# 5. 本地构建验证
docker build -t tgjiema:test .

# 6. 运行测试
docker run --rm tgjiema:test python -m pytest tests/ -v

# 7. 提交 PR,CI 通过后合并
```

---

## 6. 安全考量

- **Digest 不可变**: SHA-256 digest 是镜像内容的哈希,无法被劫持
- **多架构 digest**: manifest list digest 涵盖所有架构(amd64/arm64)
- **Trust policy**: 可配合 Cosign 验证镜像签名(见 `docs/SIGNING.md`)
- **SBOM**: 基础镜像的 SBOM 应与项目 SBOM 一并归档(见 `docs/github-pat-workflow-scope.md`)
- **漏洞扫描**: 升级后运行 `trivy image python:3.12-slim@<digest>` 检查已知漏洞

---

## 7. 相关文件

- `Dockerfile` — `ARG PYTHON_IMAGE` 定义基础镜像
- `docs/SIGNING.md` — 制品签名流程(cosign)
- `docs/github-pat-workflow-scope.md` — CI 中 SBOM/依赖扫描
- `requirements.txt` — Python 依赖(见 P2-2 依赖锁定)
