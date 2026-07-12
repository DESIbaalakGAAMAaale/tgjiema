# R39 P2-4: 配置生成器单向生成(消除多份手工维护)

## 背景

R39 终审指出: "Config registry、Settings、services.yaml、Compose、systemd
应由生成器单向生成,禁止手工多份维护。"

当前仓库存在多处配置来源,容易不一致:
- `config/settings.py` — Python Settings(pydantic-settings)
- `config/registry.py` — 服务注册表
- `config/services.yaml` — 服务清单(YAML)
- `docker-compose.yml` — Compose 服务定义
- `deploy_vps_per_bot.sh` — systemd 服务定义(嵌入式)
- `.env.example` — 环境变量样例

任何一处手工修改不同步,会导致:
- Compose 与 systemd 服务清单不一致(deploy-check workflow 已校验)
- Settings 与 services.yaml 字段不匹配
- .env.example 缺失新增配置项

**整改**: 建立**单一事实源 → 多目标生成器**的单向数据流,
所有派生文件由生成器产出,禁止手工修改派生文件。

---

## 1. 单一事实源 (Single Source of Truth)

### 1.1 事实源定义

| 事实源 | 文件 | 内容 |
| ------ | ---- | ---- |
| 服务清单 | `config/services.yaml` | 所有服务的 name/role/oneshot/description |
| 拓扑配置 | `config/topology.yaml` | 频道冗余环、副本因子、cell 映射 |
| 分组配置 | `config/groups.yaml` | 拓扑组(1-5)定义 |
| 环境变量声明 | `config/settings.py` | 所有 ENV 变量的字段、类型、默认值 |
| Redis ACL | `config/redis/users.acl` | Redis 用户权限矩阵 |
| 依赖声明 | `requirements.txt` | Python 依赖(见 P2-2) |

### 1.2 派生文件(生成器产出,禁止手工修改)

| 派生文件 | 生成器 | 来源 |
| -------- | ------ | ---- |
| `docker-compose.yml` | `config/generate_compose.py`(待实现) | services.yaml |
| `deploy_vps_per_bot.sh` | `config/generate_systemd.py`(待实现) | services.yaml |
| `.env.example` | `config/generate_env_example.py`(待实现) | settings.py |
| `config/grafana-dashboard.json` | `config/generate_dashboard.py`(待实现) | services.yaml + metrics 定义 |
| `docs/least-privilege.md` | 手动 + 生成器校验 | services.yaml + redis ACL |

---

## 2. 生成器设计

### 2.1 生成器接口

所有生成器遵循统一接口:

```python
# config/generate_all.py
"""R39 P2-4: 配置生成器入口,从单一事实源生成所有派生文件。"""

import argparse
import sys
from pathlib import Path

GENERATORS = [
    ("compose", "config.generate_compose:main"),
    ("systemd", "config.generate_systemd:main"),
    ("env_example", "config.generate_env_example:main"),
    ("dashboard", "config.generate_dashboard:main"),
]

def main():
    parser = argparse.ArgumentParser(description="R39 P2-4: 配置生成器")
    parser.add_argument("--target", choices=["all"] + [g[0] for g in GENERATORS],
                        default="all", help="生成目标(默认 all)")
    parser.add_argument("--check", action="store_true",
                        help="校验模式:检查派生文件是否与事实源一致(不修改)")
    parser.add_argument("--dry-run", action="store_true",
                        help="打印将生成的内容,不写入文件")
    args = parser.parse_args()

    targets = GENERATORS if args.target == "all" else [g for g in GENERATORS if g[0] == args.target]
    failed = []
    for name, func_path in targets:
        module_name, func_name = func_path.split(":")
        try:
            mod = __import__(module_name, fromlist=[func_name])
            func = getattr(mod, func_name)
            ok = func(check=args.check, dry_run=args.dry_run)
            if not ok:
                failed.append(name)
        except Exception as e:
            print(f"[FAIL] {name}: {e}", file=sys.stderr)
            failed.append(name)

    if failed:
        print(f"\n生成失败: {failed}", file=sys.stderr)
        sys.exit(1)
    print(f"\n生成完成: {[t[0] for t in targets]}")
```

### 2.2 单个生成器示例(Compose)

```python
# config/generate_compose.py
"""R39 P2-4: 从 services.yaml 生成 docker-compose.yml。"""

import yaml
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

def generate_compose(services_yaml_path: Path) -> str:
    """从 services.yaml 生成 docker-compose.yml 内容。"""
    with open(services_yaml_path) as f:
        data = yaml.safe_load(f)
    services = data.get("services", [])

    compose = {
        "version": "3.8",
        "services": {},
        "networks": {"tgjiema-net": {"driver": "bridge"}},
    }
    for svc in services:
        name = svc["name"]
        is_oneshot = svc.get("is_oneshot", False)
        role = svc.get("role", "bot")
        # 根据模板生成 service 定义
        compose["services"][name] = {
            "build": ".",
            "command": f"python -m bots.{name}" if role == "bot" else f"python -m services.{name}",
            "env_file": [".env.shared", f".env.secrets.{name}"],
            "restart": "no" if is_oneshot else "unless-stopped",
            "networks": ["tgjiema-net"],
            "volumes": ["./data:/app/data", "./logs:/app/logs"],
        }
    return yaml.dump(compose, default_flow_style=False, sort_keys=True)


def main(check: bool = False, dry_run: bool = False) -> bool:
    """生成或校验 docker-compose.yml。"""
    services_yaml = REPO_ROOT / "config" / "services.yaml"
    compose_yaml = REPO_ROOT / "docker-compose.yml"
    generated = generate_compose(services_yaml)
    if dry_run:
        print(generated)
        return True
    if check:
        existing = compose_yaml.read_text()
        if existing != generated:
            print(f"[MISMATCH] docker-compose.yml 与 services.yaml 不一致")
            return False
        return True
    compose_yaml.write_text(generated)
    return True
```

### 2.3 校验模式(--check)

生成器支持 `--check` 模式,仅校验派生文件是否与事实源一致:

```bash
# 校验所有派生文件
python -m config.generate_all --check

# 校验单个
python -m config.generate_compose --check
```

---

## 3. CI 集成

在 `.github/workflows/ci.yml` 的 `release-gates` job 中添加校验步骤:

```yaml
    # R39 P2-4: 校验派生配置文件与事实源一致
    - name: Verify generated config files are up-to-date
      run: |
        python -m config.generate_all --check
      continue-on-error: false  # 不一致时 fail build
```

若校验失败,CI 输出:
```
[MISMATCH] docker-compose.yml 与 services.yaml 不一致
请运行: python -m config.generate_all
```

---

## 4. 工作流

### 4.1 修改服务的标准流程

1. **修改事实源**: 编辑 `config/services.yaml` 添加新服务
2. **运行生成器**: `python -m config.generate_all`
3. **提交所有变更**: `git add config/services.yaml docker-compose.yml deploy_vps_per_bot.sh`
4. **PR review**: 审核事实源变更,派生文件由生成器保证一致
5. **CI 校验**: `--check` 模式确保派生文件已更新

### 4.2 禁止的操作

- ❌ 直接修改 `docker-compose.yml`(应改 services.yaml 后生成)
- ❌ 直接修改 `deploy_vps_per_bot.sh` 中的服务定义(应改 services.yaml 后生成)
- ❌ 直接修改 `.env.example`(应改 settings.py 后生成)
- ✅ 修改 `config/services.yaml`(事实源)
- ✅ 修改 `config/settings.py`(事实源)
- ✅ 修改 `config/topology.yaml`(事实源)

### 4.3 派生文件标注

在派生文件头部添加生成器标注:

```yaml
# docker-compose.yml
# ⚠️ R39 P2-4: 本文件由 config/generate_compose.py 自动生成,禁止手工修改
# 事实源: config/services.yaml
# 生成命令: python -m config.generate_compose
# 最后生成: 2026-07-13T12:00:00Z
version: "3.8"
...
```

```bash
# deploy_vps_per_bot.sh
# ⚠️ R39 P2-4: 本文件由 config/generate_systemd.py 自动生成,禁止手工修改
# 事实源: config/services.yaml
# 生成命令: python -m config.generate_systemd
...
```

---

## 5. 迁移计划

R39 P2-4 当前仅提供文档说明,真正实现分阶段进行:

| 阶段 | 内容 | 状态 |
| ---- | ---- | ---- |
| 1 | 定义事实源清单(services.yaml/settings.py) | ✅ 已存在 |
| 2 | 实现生成器骨架(generate_all.py) | 🔲 待实现 |
| 3 | 实现 Compose 生成器 | 🔲 待实现 |
| 4 | 实现 systemd 生成器 | 🔲 待实现 |
| 5 | 实现 .env.example 生成器 | 🔲 待实现 |
| 6 | CI 集成 --check 校验 | 🔲 待实现 |
| 7 | 派生文件添加生成器标注 | 🔲 待实现 |
| 8 | 文档培训 + 工作流切换 | 🔲 待实现 |

迁移过程中保持现有 deploy-check workflow 的三方一致性校验作为兜底。

---

## 6. 相关文件

- `config/services.yaml` — 服务清单(事实源)
- `config/settings.py` — 环境变量声明(事实源)
- `config/topology.yaml` — 拓扑配置(事实源)
- `config/registry.py` — 服务注册表(运行时使用)
- `docker-compose.yml` — Compose 配置(派生)
- `deploy_vps_per_bot.sh` — systemd 部署脚本(派生)
- `.env.example` — 环境变量样例(派生)
- `.github/workflows/deploy-check.yml` — 三方一致性校验(现有兜底)
