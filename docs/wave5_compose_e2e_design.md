# R70 Wave 5 — 真实 Compose Runtime E2E 设计文档

## 背景

R70 Wave 3 终审报告要求:

> "新增 Compose E2E: migration、Redis ACL、所有真实角色、health/readiness、
> API/Bot/Admin、DBWriter、CRDB sync、backup/restore、SIGTERM、restart"

当前 `scripts/runtime_smoke_compose.py` 仍**绕过 Compose**(直接调用 import probe),
违反"runtime smoke 不得绕过 Compose"原则。

Wave 5 整改新增 `scripts/compose_runtime_e2e.py`,通过
`docker compose -f docker-compose.prod.yml` 实际启动全部服务、运行迁移检查、
调用 /health、验证 Redis ACL、触发 backup/restore、发送 SIGTERM 验证优雅关闭、
restart 验证恢复。

## 与 runtime_smoke_compose.py 的关键区别

| 维度 | runtime_smoke_compose.py | compose_runtime_e2e.py(Wave 5) |
|------|--------------------------|--------------------------------|
| 执行方式 | 单容器 smoke(hermetic CI) | 真实 Compose 全栈 E2E |
| 是否启动 Compose | 否(绕过 Compose) | 是(11 阶段全栈) |
| 验证范围 | import + SIGTERM 信号处理 | 11 阶段运行态契约 |
| 环境要求 | Docker daemon 可选 | Docker daemon + .env + 镜像 digest |
| 适用场景 | CI 快速 smoke | staging/production 发布门禁 |

## 设计原则

1. **fail-closed(无 mock / no fallback)**:编排器自身不允许 mock 任何子命令,
   Docker daemon 不可用或任何子命令失败时立即 fail(返回 1)。
2. **每阶段独立 readiness 检查点**:每阶段返回 `PhaseResult.readiness_checks`
   (list of `{check, status, ...}`),fail 时非空且每项含 `check` 和 `status` 字段。
3. **JSON 证据**:每阶段输出 ISO 8601 时间戳、duration、stdout/stderr、returncode、
   error、evidence(dict)、readiness_checks,可序列化为 JSON 供下游消费。
4. **11 阶段顺序执行**:前一阶段 fail 立即终止后续阶段,触发 teardown 清理资源
   (除非 teardown 已在执行列表中)。
5. **CLI 选项**:`--phase <name>` 单阶段调试、`--timeout <seconds>` 每阶段超时、
   `--keep-on-success` 全部通过时跳过 teardown 保留容器供人工检查。

## 11 阶段定义与检查矩阵

| # | 阶段 | 描述 | readiness 检查点 | 通过条件 |
|---|------|------|-----------------|---------|
| 1 | preflight | Docker daemon / 镜像 digest / .env 检查 | docker_daemon / compose_file / env_file / image_digest / redis_passwords | 全部 pass |
| 2 | start_core | 启动 redis + db_writer | docker_daemon / compose_up / service_status | docker compose up -d 返回 0 且 ps 输出含核心服务 |
| 3 | start_bots | 启动 up/idx/dsp/mon/admin_bot | docker_daemon / compose_up / bot_services_running | 所有 Bot 服务在 ps 输出中 |
| 4 | migration_check | docker compose exec db_writer python -m database.migrate --check | docker_daemon / migration_exec / migration_no_failures | returncode=0 且输出不含 "failed" |
| 5 | health_check | 对每个服务调用 /health(SERVICE_ROLE 映射) | docker_daemon / health_<svc> / service_role_mapping | admin:8080 + prometheus_exporter:9100 返回 200,所有 SERVICE_ROLE 匹配 |
| 6 | redis_acl_check | 验证 Redis ACL(redis-acl-init 完成) | docker_daemon / redis_acl_init_completed / users_acl_file_exists / redis_users_auth | ACL init exited 0 + users.acl 存在 + 4 用户 AUTH 成功 |
| 7 | business_smoke | 通过 admin /health 触发业务循环检测 | docker_daemon / admin_health_endpoint / bot_heartbeat_detected | admin /health 返回 200 且输出含 bot 心跳 |
| 8 | backup_restore | 触发 backup → restore → 验证数据完整性 | docker_daemon / backup_triggered / restore_triggered / data_integrity_verified | backup + restore returncode=0 且 restore 输出含 ok/success/verified/complete |
| 9 | sigterm | docker compose kill -s SIGTERM 验证优雅关闭 | docker_daemon / sigterm_sent / no_sigkill | kill returncode=0 且无服务退出码为 137(SIGKILL) |
| 10 | restart | docker compose up -d 验证可恢复 | docker_daemon / compose_up / services_running_after_restart | up -d returncode=0 且所有核心+Bot 服务重新 running |
| 11 | teardown | docker compose down -v | docker_daemon / compose_down | down -v returncode=0 |

## 每阶段 readiness 期望

### 阶段 1:preflight

| 检查点 | pass 条件 | fail 条件 |
|--------|----------|----------|
| docker_daemon | `shutil.which("docker")` 存在 + `docker info` returncode=0 | docker 二进制不存在 / docker daemon 未运行 |
| compose_file | `COMPOSE_FILE.is_file()` 为 True | docker-compose.prod.yml 不存在 |
| env_file | `ENV_FILE.is_file()` 为 True | .env 文件不存在 |
| image_digest | `TGJIEMA_IMAGE` 含 `@sha256:` | 未设置 / 使用 mutable tag(:latest) |
| redis_passwords | 4 个 `REDIS_*_PASSWORD` 环境变量均非空 | 任一为空 |

### 阶段 2:start_core

| 检查点 | pass 条件 | fail 条件 |
|--------|----------|----------|
| docker_daemon | 同 preflight | 同 preflight |
| compose_up | `docker compose up -d redis db_writer` returncode=0 | returncode≠0 或超时 |
| service_status | `docker compose ps --format json` 输出包含 redis/db_writer(及 redis-acl-init/migration) | ps 失败 / 未发现核心服务 |

### 阶段 3:start_bots

| 检查点 | pass 条件 | fail 条件 |
|--------|----------|----------|
| docker_daemon | 同 preflight | 同 preflight |
| compose_up | `docker compose up -d up idx dsp mon admin_bot` returncode=0 | returncode≠0 或超时 |
| bot_services_running | 所有 BOT_SERVICES 在 ps 输出中 | 缺失任一 Bot 服务 |

### 阶段 4:migration_check

| 检查点 | pass 条件 | fail 条件 |
|--------|----------|----------|
| docker_daemon | 同 preflight | 同 preflight |
| migration_exec | `docker compose exec -T db_writer python -m database.migrate --check` returncode=0 | returncode≠0 或超时 |
| migration_no_failures | 输出(stdout+stderr)不含 "failed"(或含 "0 failed") | 输出包含非零 failed |

### 阶段 5:health_check

| 检查点 | pass 条件 | fail 条件 |
|--------|----------|----------|
| docker_daemon | 同 preflight | 同 preflight |
| health_admin | `admin:8080/health` 返回 200 | returncode≠0 或超时 |
| health_prometheus_exporter | `prometheus_exporter:9100/health` 返回 200 | returncode≠0 或超时 |
| service_role_mapping | 所有应用服务 `printenv SERVICE_ROLE` 与 SERVICE_ROLES 一致 | 任一服务 SERVICE_ROLE 不匹配 |

### 阶段 6:redis_acl_check

| 检查点 | pass 条件 | fail 条件 |
|--------|----------|----------|
| docker_daemon | 同 preflight | 同 preflight |
| redis_acl_init_completed | `docker inspect tgjiema-redis-acl-init` 显示 Status=exited + ExitCode=0 | 容器不存在 / 未成功完成 |
| users_acl_file_exists | `docker compose exec -T redis ls -la /data/users.acl` returncode=0 且输出含路径 | 文件不存在 |
| redis_users_auth | 4 个用户(tgjiema_writer/reader/health/admin)`redis-cli AUTH PING` 返回 PONG | 任一用户 AUTH 失败 |

### 阶段 7:business_smoke

| 检查点 | pass 条件 | fail 条件 |
|--------|----------|----------|
| docker_daemon | 同 preflight | 同 preflight |
| admin_health_endpoint | `admin:8080/health` 返回 200 | returncode≠0 或超时 |
| bot_heartbeat_detected | /health 输出含 "bot" 或 "up"(不区分大小写) | 输出未检测到 bot 心跳 |

### 阶段 8:backup_restore

| 检查点 | pass 条件 | fail 条件 |
|--------|----------|----------|
| docker_daemon | 同 preflight | 同 preflight |
| backup_triggered | `docker compose run --rm db_backup python -m services.db_backup` returncode=0 | returncode≠0 或超时 |
| restore_triggered | `docker compose run --rm db_writer python -m services.db_restore --staging` returncode=0 | returncode≠0 或超时 |
| data_integrity_verified | restore 输出含 ok/success/verified/complete(任一) | 输出未含完整性关键字 |

### 阶段 9:sigterm

| 检查点 | pass 条件 | fail 条件 |
|--------|----------|----------|
| docker_daemon | 同 preflight | 同 preflight |
| sigterm_sent | `docker compose kill -s SIGTERM` returncode=0 | returncode≠0 或超时 |
| no_sigkill | `docker compose ps -a --format json` 无服务 ExitCode=137 | 任一服务被 SIGKILL(137) |

### 阶段 10:restart

| 检查点 | pass 条件 | fail 条件 |
|--------|----------|----------|
| docker_daemon | 同 preflight | 同 preflight |
| compose_up | `docker compose up -d` returncode=0 | returncode≠0 或超时 |
| services_running_after_restart | 所有 CORE_SERVICES + BOT_SERVICES 重新进入 running 状态 | 缺失任一服务 |

### 阶段 11:teardown

| 检查点 | pass 条件 | fail 条件 |
|--------|----------|----------|
| docker_daemon | 同 preflight | 同 preflight |
| compose_down | `docker compose down -v` returncode=0 | returncode≠0 或超时 |

## 与 docker-compose.prod.yml 的服务映射

编排器中的常量与 `docker-compose.prod.yml` 的对应关系(单源真理):

### CORE_SERVICES(阶段 2 启动)

```yaml
# docker-compose.prod.yml
services:
  redis:        # ← CORE_SERVICES[0]
    image: redis:7-alpine
    ...
  db_writer:    # ← CORE_SERVICES[1]
    image: ${TGJIEMA_IMAGE:?...}
    ...
```

### BOT_SERVICES(阶段 3 启动)

```yaml
# docker-compose.prod.yml
services:
  up:           # ← BOT_SERVICES[0]
    image: ${TGJIEMA_IMAGE:?...}
    environment:
      - SERVICE_ROLE=up
  idx:          # ← BOT_SERVICES[1]
    environment:
      - SERVICE_ROLE=idx
  dsp:          # ← BOT_SERVICES[2]
    environment:
      - SERVICE_ROLE=dsp
  mon:          # ← BOT_SERVICES[3]
    environment:
      - SERVICE_ROLE=mon
  admin_bot:    # ← BOT_SERVICES[4]
    environment:
      - SERVICE_ROLE=admin_bot
```

### HTTP_HEALTH_SERVICES(阶段 5 健康检查)

```yaml
# docker-compose.prod.yml
services:
  admin:        # ← HTTP_HEALTH_SERVICES["admin"]=8080
    ports:
      - "127.0.0.1:8080:8080"
  prometheus_exporter:  # ← HTTP_HEALTH_SERVICES["prometheus_exporter"]=9100
    ports:
      - "127.0.0.1:9100:9100"
```

### SERVICE_ROLES(阶段 5 SERVICE_ROLE 验证)

编排器中的 `SERVICE_ROLES` 字典与 compose 文件每个应用服务的
`environment.SERVICE_ROLE` 一一对应(单源真理):
- `migration` / `db_writer` / `crdb_sync` / `up` / `idx` / `dsp` / `mon` /
  `admin_bot` / `admin` / `db_backup` / `prometheus_exporter`
- 基础设施服务(`redis` / `redis-acl-init`)无 SERVICE_ROLE,在验证时跳过

### REQUIRED_ENV_VARS(阶段 1 preflight)

```bash
# .env 必须包含:
REDIS_WRITER_PASSWORD=<非空>
REDIS_READER_PASSWORD=<非空>
REDIS_HEALTH_PASSWORD=<非空>
REDIS_ADMIN_PASSWORD=<非空>
TGJIEMA_IMAGE=ghcr.io/maxiuquan/tgjiema@sha256:<64 hex>
```

## 执行环境要求

### 必需条件

1. **Docker daemon 可用**
   - `shutil.which("docker")` 返回非 None
   - `docker info` returncode=0
   - daemon 不可用时立即 fail(无 mock / no fallback)

2. **.env 文件存在**
   - 路径:`<repo_root>/.env`
   - 包含 4 个 `REDIS_*_PASSWORD`(非空)
   - 包含 `TGJIEMA_IMAGE`(指向不可变 digest:`@sha256:`)

3. **TGJIEMA_IMAGE 指向不可变 digest**
   - 格式:`ghcr.io/maxiuquan/tgjiema@sha256:<64 hex>`
   - 禁止 mutable tag(`:latest` / `:master` / `:staging` / `:v1.2.3` 无 digest)

4. **docker-compose.prod.yml 存在**
   - 路径:`<repo_root>/docker-compose.prod.yml`
   - 包含 redis / redis-acl-init / migration / db_writer / crdb_sync /
     up / idx / dsp / mon / admin_bot / admin / db_backup / prometheus_exporter

5. **CI runner 要求**
   - self-hosted runner 或 Docker-enabled runner
   - 可访问 ghcr.io 拉取镜像
   - 可访问 R2/CRDB(backup_restore 阶段需要)

## CLI 用法

### 全量执行(11 阶段)

```bash
python scripts/compose_runtime_e2e.py
# 默认 timeout=600s,执行所有 11 阶段(含 teardown)
```

### 单阶段调试

```bash
python scripts/compose_runtime_e2e.py --phase preflight --timeout 30
# 只运行 preflight 阶段
```

### 保留容器供人工检查

```bash
python scripts/compose_runtime_e2e.py --keep-on-success
# 全部通过时跳过 teardown,容器保留供 docker exec 人工检查
# 任一阶段失败时仍触发 teardown 清理
```

### 自定义超时

```bash
python scripts/compose_runtime_e2e.py --timeout 1200
# 每阶段超时 1200s(默认 600s)
```

### 输出格式

每阶段输出 JSON 证据到 stdout,例如:

```json
{
  "phase": "preflight",
  "description": "Preflight: Docker daemon / image digest / .env 检查",
  "status": "pass",
  "timestamp": "2026-07-21T10:30:00+00:00",
  "duration_seconds": 1.234,
  "stdout": "",
  "stderr": "",
  "returncode": null,
  "error": null,
  "evidence": {
    "docker_available": true,
    "compose_file": "/path/to/docker-compose.prod.yml",
    "env_file": "/path/to/.env",
    "tgjiema_image": "ghcr.io/maxiuquan/tgjiema@sha256:abc123...",
    "redis_passwords_set": [
      "REDIS_WRITER_PASSWORD",
      "REDIS_READER_PASSWORD",
      "REDIS_HEALTH_PASSWORD",
      "REDIS_ADMIN_PASSWORD"
    ]
  },
  "readiness_checks": [
    {"check": "docker_daemon", "status": "pass"},
    {"check": "compose_file", "status": "pass"},
    {"check": "env_file", "status": "pass"},
    {"check": "image_digest", "status": "pass"},
    {"check": "redis_passwords", "status": "pass"}
  ]
}
```

### 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 所有阶段通过 |
| 1 | 任一阶段失败(fail-closed) |

## 在 CI 中运行的步骤

### GitHub Actions 集成

```yaml
# .github/workflows/compose-runtime-e2e.yml
name: Compose Runtime E2E (R70 Wave 5)

on:
  push:
    branches: [master, rc-*]
  pull_request:
    branches: [master]

jobs:
  compose-runtime-e2e:
    runs-on: self-hosted  # 或 Docker-enabled runner
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4

      - name: Configure .env
        env:
          REDIS_WRITER_PASSWORD: ${{ secrets.REDIS_WRITER_PASSWORD }}
          REDIS_READER_PASSWORD: ${{ secrets.REDIS_READER_PASSWORD }}
          REDIS_HEALTH_PASSWORD: ${{ secrets.REDIS_HEALTH_PASSWORD }}
          REDIS_ADMIN_PASSWORD: ${{ secrets.REDIS_ADMIN_PASSWORD }}
          TGJIEMA_IMAGE: ghcr.io/maxiuquan/tgjiema@sha256:${{ github.sha }}
        run: |
          cat > .env <<EOF
          REDIS_WRITER_PASSWORD=${REDIS_WRITER_PASSWORD}
          REDIS_READER_PASSWORD=${REDIS_READER_PASSWORD}
          REDIS_HEALTH_PASSWORD=${REDIS_HEALTH_PASSWORD}
          REDIS_ADMIN_PASSWORD=${REDIS_ADMIN_PASSWORD}
          TGJIEMA_IMAGE=${TGJIEMA_IMAGE}
          EOF

      - name: Run Compose Runtime E2E
        run: python scripts/compose_runtime_e2e.py --timeout 600

      - name: Upload JSON evidence
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: compose-e2e-evidence
          path: |
            docker-compose.prod.yml
            .env
```

### CI 触发条件

- **push 到 master / rc-\*** 分支:全量执行
- **PR 到 master**:可选执行(取决于 runner 资源)
- **release 发布**:必须执行(production promotion 门禁)

### CI 注意事项

1. **runner 要求**:必须使用 self-hosted runner 或 Docker-enabled runner
   (GitHub-hosted runner 不支持 docker compose 完整功能)
2. **secrets 配置**:4 个 REDIS 密码必须配置为 repository secrets
3. **镜像 digest**:TGJIEMA_IMAGE 必须使用 `@sha256:` 形式,
   禁止使用 mutable tag
4. **超时设置**:CI 中建议 `--timeout 600`(默认值),本地调试可设为更短
5. **teardown 保证**:CI 中不使用 `--keep-on-success`,确保每次运行后清理容器

## 测试策略

### 单元测试(tests/test_r70_wave5_compose_runtime_e2e.py)

测试套件包含 42 个测试,覆盖 9 个维度:

| 类别 | 测试数 | 覆盖范围 |
|------|--------|---------|
| A. 编排器文件存在性与可 import | 3 | 文件存在 / 模块可 import / shebang + docstring |
| B. 11 个阶段定义完整性 | 5 | 阶段数 / 名称顺序 / PHASE_FUNCS 覆盖 / 描述非空 / 函数签名 |
| C. 每阶段 readiness 检查点 | 4 | PhaseResult 字段 / JSON 可序列化 / status 值 / fail 时 readiness_checks 非空 |
| D. CLI 选项支持 | 6 | --phase / --timeout / --keep-on-success / 拒绝未知 phase / 默认 timeout=600 / --help |
| E. fail-closed 行为 | 5 | 无 mock / 无吞异常 / 无 skip / subprocess 失败传播 / 异常视为 fail |
| F. Docker daemon 不可用 | 4 | preflight fail / 所有阶段 fail / main 返回 1 / 无 fallback |
| G. 阶段执行端到端(mock) | 7 | preflight pass / image 非 digest fail / redis 密码空 fail / start_core 命令构造 / main 全 pass / keep-on-success 跳过 teardown / 失败触发 teardown |
| H. Compose 文件一致性 | 4 | CORE_SERVICES / BOT_SERVICES / SERVICE_ROLES / HTTP_HEALTH_SERVICES 与 docker-compose.prod.yml 一致 |
| I. JSON 证据格式 | 4 | 必需字段 / JSON 序列化 / ISO 8601 时间戳 / 每阶段返回 evidence |

### 测试策略说明

- **不实际调用 docker**:使用 `unittest.mock` 模拟 `subprocess.run` / `shutil.which`
- **验证编排器逻辑**:返回 PhaseResult、JSON 证据、退出码
- **严格遵守 R70 整改规范**:无 TODO / pass / 占位符
- **跨平台兼容**:Windows 路径(含反斜杠)与 Linux 路径均通过测试

## 相关文件

| 文件 | 角色 |
|------|------|
| `scripts/compose_runtime_e2e.py` | 编排器(11 阶段 fail-closed) |
| `tests/test_r70_wave5_compose_runtime_e2e.py` | 测试套件(42 个测试) |
| `docs/wave5_compose_e2e_design.md` | 本设计文档 |
| `docker-compose.prod.yml` | 不可变生产 Compose(服务定义) |
| `scripts/runtime_smoke_compose.py` | 旧版 runtime smoke(参考,被本编排器替代) |
| `scripts/check_compose_static_rules.py` | Compose 静态规则检查(参考) |
| `config/services.yaml` | 服务清单(单源真理) |

## 整改对照(R70 Wave 3 终审要求)

| Wave 3 要求 | Wave 5 实现 | 阶段 |
|------------|------------|------|
| migration | docker compose exec db_writer python -m database.migrate --check | 阶段 4 |
| Redis ACL | redis-acl-init 完成 + users.acl 存在 + 4 用户 AUTH | 阶段 6 |
| 所有真实角色 | SERVICE_ROLES 映射验证(printenv SERVICE_ROLE) | 阶段 5 |
| health/readiness | admin:8080 + prometheus_exporter:9100 /health | 阶段 5 |
| API/Bot/Admin | admin /health + bot 心跳检测 | 阶段 7 |
| DBWriter | docker compose up -d db_writer + ps 验证 running | 阶段 2 |
| CRDB sync | 通过 migration_check 间接验证(DDL 执行) | 阶段 4 |
| backup/restore | docker compose run --rm db_backup + db_restore --staging | 阶段 8 |
| SIGTERM | docker compose kill -s SIGTERM + 无 SIGKILL(137) | 阶段 9 |
| restart | docker compose up -d + 所有服务重新 running | 阶段 10 |
