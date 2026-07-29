"""R36 Batch 8 H8+B0-4: 统一交付清单 + CI workflow 测试。

覆盖:
- H8-1: services.yaml 存在且包含所有必需服务
- H8-2: docker-compose.yml 拆分为多服务 + 最小 secrets
- H8-3: services.yaml 与 docker-compose.yml 服务清单一致
- B0-4-1: CI workflow 在默认分支可见(on push to master/main)
- B0-4-2: CI workflow 包含故障注入测试 job
- B0-4-3: deploy-check.yml 验证 services.yaml 一致性
"""
import re
from pathlib import Path

import pytest


# ════════════════════════════════════════════════════════════════
# 1. services.yaml 测试
# ════════════════════════════════════════════════════════════════

def _yaml_available() -> bool:
    try:
        import yaml  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _yaml_available(), reason="PyYAML 不可用")
class TestServicesYaml:
    """R36 H8: services.yaml 统一服务清单测试。"""

    def test_services_yaml_exists(self):
        """config/services.yaml 文件存在。"""
        path = Path(__file__).parent.parent / "config" / "services.yaml"
        assert path.exists(), "config/services.yaml 必须存在(R36 H8)"

    def test_services_yaml_has_all_required_services(self):
        """services.yaml 包含所有 10 个必需服务。"""
        import yaml
        path = Path(__file__).parent.parent / "config" / "services.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        services = data.get("services", [])
        names = {s["name"] for s in services}
        required = {"migration", "db_writer", "crdb_sync", "up", "idx", "dsp",
                    "mon", "admin_bot", "admin", "db_backup"}
        missing = required - names
        assert not missing, f"services.yaml 缺少服务: {missing}"

    def test_migration_is_oneshot(self):
        """migration 服务标记为 oneshot。"""
        import yaml
        path = Path(__file__).parent.parent / "config" / "services.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        migration = [s for s in data["services"] if s["name"] == "migration"][0]
        assert migration.get("is_oneshot") is True

    def test_crdb_sync_has_crdb_secret(self):
        """crdb_sync 服务的 secrets 包含 COCKROACHDB_URL。"""
        import yaml
        path = Path(__file__).parent.parent / "config" / "services.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        crdb_sync = [s for s in data["services"] if s["name"] == "crdb_sync"][0]
        assert "COCKROACHDB_URL" in crdb_sync["secrets"]

    def test_db_backup_has_backup_kek_secret(self):
        """db_backup 服务的 secrets 包含 BACKUP_KEK。"""
        import yaml
        path = Path(__file__).parent.parent / "config" / "services.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        db_backup = [s for s in data["services"] if s["name"] == "db_backup"][0]
        assert "BACKUP_KEK" in db_backup["secrets"]

    def test_business_bots_have_migration_dep(self):
        """业务 Bot(up/idx/dsp/mon/admin_bot/admin)依赖 migration。"""
        import yaml
        path = Path(__file__).parent.parent / "config" / "services.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        business_bots = ["up", "idx", "dsp", "mon", "admin_bot", "admin"]
        for bot_name in business_bots:
            bot = [s for s in data["services"] if s["name"] == bot_name][0]
            assert bot.get("migration_dep") is True, f"{bot_name} 应依赖 migration"


# ════════════════════════════════════════════════════════════════
# 2. docker-compose.yml 测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _yaml_available(), reason="PyYAML 不可用")
class TestDockerComposeMultiService:
    """R36 H8: docker-compose.yml 多服务拆分测试。"""

    def test_compose_has_all_services(self):
        """docker-compose.yml 包含所有 10 个业务服务 + redis。"""
        import yaml
        path = Path(__file__).parent.parent / "docker-compose.yml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        services = set(data.get("services", {}).keys())
        required = {"redis", "migration", "db_writer", "crdb_sync", "up", "idx",
                    "dsp", "mon", "admin_bot", "admin", "db_backup"}
        missing = required - services
        assert not missing, f"docker-compose.yml 缺少服务: {missing}"

    def test_compose_uses_env_shared(self):
        """每个业务服务加载 .env.shared(不再共享完整 .env)。"""
        import yaml
        path = Path(__file__).parent.parent / "docker-compose.yml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        business_svcs = ["up", "idx", "dsp", "mon", "admin_bot", "admin",
                         "db_backup", "crdb_sync", "db_writer", "migration"]
        for svc_name in business_svcs:
            svc = data["services"].get(svc_name, {})
            env_files = svc.get("env_file", [])
            has_shared = any(".env.shared" in str(ef) for ef in env_files)
            assert has_shared, f"{svc_name} 必须加载 .env.shared"

    def test_compose_no_complete_env_loading(self):
        """docker-compose.yml 不加载完整 .env(仅 .env.shared + .env.secrets.<svc>)。"""
        path = Path(__file__).parent.parent / "docker-compose.yml"
        content = path.read_text(encoding="utf-8")
        # 不应有单独的 "- .env" 行(不带 .shared 或 .secrets)
        lines = content.split("\n")
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- .env") and not any(
                ext in stripped for ext in [".shared", ".secrets", ".example"]
            ):
                pytest.fail(f"发现完整 .env 加载: {stripped}")

    def test_compose_migration_depends_on(self):
        """业务服务依赖 migration service_completed_successfully。"""
        import yaml
        path = Path(__file__).parent.parent / "docker-compose.yml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        business_svcs = ["up", "idx", "dsp", "mon", "admin_bot", "admin"]
        for svc_name in business_svcs:
            svc = data["services"][svc_name]
            depends = svc.get("depends_on", {})
            assert "migration" in depends, f"{svc_name} 应依赖 migration"
            assert depends["migration"].get("condition") == "service_completed_successfully"

    def test_compose_redis_has_persistence(self):
        """Redis 容器配置了 AOF 持久化。"""
        import yaml
        path = Path(__file__).parent.parent / "docker-compose.yml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        redis = data["services"]["redis"]
        command = redis.get("command", "")
        assert "appendonly yes" in command
        assert "appendfsync everysec" in command
        assert "maxmemory-policy noeviction" in command


# ════════════════════════════════════════════════════════════════
# 3. 服务清单一致性测试
# ════════════════════════════════════════════════════════════════

@pytest.mark.skipif(not _yaml_available(), reason="PyYAML 不可用")
class TestServiceConsistency:
    """R36 H8: services.yaml / docker-compose.yml / deploy_vps_per_bot.sh 一致性。"""

    def test_compose_matches_services_yaml(self):
        """docker-compose 服务清单与 services.yaml 一致。"""
        import yaml
        # 读取 services.yaml
        sy_path = Path(__file__).parent.parent / "config" / "services.yaml"
        sy_data = yaml.safe_load(sy_path.read_text(encoding="utf-8"))
        sy_names = {s["name"] for s in sy_data["services"]}

        # 读取 docker-compose.yml
        dc_path = Path(__file__).parent.parent / "docker-compose.yml"
        dc_data = yaml.safe_load(dc_path.read_text(encoding="utf-8"))
        # 排除基础设施(redis / redis-acl-init / cockroachdb) — 这些是 compose 专用,
        # 在 services.yaml 的 `infrastructure` 节点定义,不参与 services 业务清单比对。
        # R75 P0-03: cockroachdb 作为 CI/开发环境单节点 CRDB 基础设施加入 compose,
        # 但不进入 services.yaml 的业务服务清单(systemd 通过外部 CRDB Cloud 部署)。
        dc_names = set(dc_data.get("services", {}).keys()) - {
            "redis", "redis-acl-init", "cockroachdb"
        }

        assert sy_names == dc_names, (
            f"服务清单不一致: services.yaml={sy_names}, docker-compose={dc_names}"
        )

    def test_systemd_includes_all_services(self):
        """deploy_vps_per_bot.sh 包含所有 services.yaml 中的服务。

        deploy 脚本使用 ${SVC_PREFIX}-<name> 变量替换(其中 SVC_PREFIX=tgjiema),
        因此同时匹配字面量 `tgjiema-<name>` 与变量形式 `${SVC_PREFIX}-<name>`,
        以及 SERVICES 数组中的 `"name:` 条目。
        """
        import yaml
        sy_path = Path(__file__).parent.parent / "config" / "services.yaml"
        sy_data = yaml.safe_load(sy_path.read_text(encoding="utf-8"))
        sy_names = {s["name"] for s in sy_data["services"]}

        deploy_path = Path(__file__).parent.parent / "deploy_vps_per_bot.sh"
        content = deploy_path.read_text(encoding="utf-8")

        for name in sy_names:
            literal = f"tgjiema-{name}"
            var_form = f"${{SVC_PREFIX}}-{name}"
            array_form = f'"{name}:'
            assert (
                literal in content
                or var_form in content
                or array_form in content
            ), f"deploy_vps_per_bot.sh 缺少服务 tgjiema-{name}(字面量/变量/数组形式均未找到)"


# ════════════════════════════════════════════════════════════════
# 4. CI workflow 测试(B0-4)
# ════════════════════════════════════════════════════════════════

class TestCIWorkflow:
    """R36 B0-4: CI workflow 在默认分支可见 + 故障注入测试。"""

    def test_ci_workflow_triggers_on_default_branch(self):
        """CI workflow 在 push 到 master/main 时触发。"""
        path = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
        content = path.read_text(encoding="utf-8")
        # on: push: branches: [master, main]
        assert "push:" in content
        assert "master" in content
        assert "main" in content

    def test_ci_workflow_triggers_on_pr(self):
        """CI workflow 在 PR 到 master/main 时触发。"""
        path = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
        content = path.read_text(encoding="utf-8")
        assert "pull_request:" in content

    def test_ci_has_fault_injection_job(self):
        """CI workflow 包含 fault-injection job。"""
        path = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
        content = path.read_text(encoding="utf-8")
        assert "fault-injection:" in content or "fault_injection:" in content
        assert "crash or fault or inject" in content

    def test_ci_includes_backup_crypto_verification(self):
        """CI workflow 包含 backup_crypto 验证步骤。"""
        path = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
        content = path.read_text(encoding="utf-8")
        assert "backup_crypto" in content
        assert "encrypt_payload" in content

    def test_ci_ast_check_includes_new_modules(self):
        """CI AST 检查包含 R36 新增模块。"""
        path = Path(__file__).parent.parent / ".github" / "workflows" / "ci.yml"
        content = path.read_text(encoding="utf-8")
        assert "services/backup_crypto.py" in content
        assert "services/crdb_sync_service.py" in content

    def test_deploy_check_validates_services_yaml(self):
        """deploy-check.yml 验证 services.yaml 一致性。"""
        path = Path(__file__).parent.parent / ".github" / "workflows" / "deploy-check.yml"
        content = path.read_text(encoding="utf-8")
        assert "services.yaml" in content
        assert "crdb_sync" in content
        assert "Type=oneshot" in content


# ════════════════════════════════════════════════════════════════
# 5. .gitignore 测试(审查报告不上传 GitHub)
# ════════════════════════════════════════════════════════════════

class TestGitignore:
    """审查报告不上传 GitHub(project_memory 约束)。"""

    def test_gitignore_excludes_review_reports(self):
        """ .gitignore 排除审查报告。"""
        path = Path(__file__).parent.parent / ".gitignore"
        content = path.read_text(encoding="utf-8")
        assert "审查" in content
        assert "*.md" in content or "审查报告" in content
