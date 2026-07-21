"""R69 Wave 7: 真实运行态 smoke 测试。

验证 Wave 7 整改:
    1. 旧 check_compose_runtime_smoke.py 已重命名为 check_compose_static_rules.py
       (消除命名误导 — 静态 lint 不得命名为 runtime smoke)
    2. 新 scripts/runtime_smoke_compose.py 是真实运行态 smoke:
       - 实际启动 Docker 容器
       - 验证模块 import + SIGTERM 处理
       - 验证 restart 恢复
       - 扫描日志发现隐藏故障
    3. .github/workflows/release-gates.yml 添加 runtime-smoke-compose CI job,
       作为 release-summary 的 required dependency
    4. .github/workflows/deploy-check.yml 的 "Minimal runtime smoke" 步骤
       已重命名为 "Minimal compose config validation"(消除命名误导)
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# ════════════════════════════════════════════════════════════════
# Wave 7: 静态 lint 重命名(消除 "runtime smoke" 命名误导)
# ════════════════════════════════════════════════════════════════

class TestR69Wave7StaticRulesRenamed:
    """R69 Wave 7: 静态 lint 不得命名为 runtime smoke。"""

    def test_check_compose_static_rules_exists(self):
        """新文件 check_compose_static_rules.py 存在(已重命名)。"""
        new_script = REPO_ROOT / "scripts" / "check_compose_static_rules.py"
        assert new_script.exists(), (
            "R69 Wave 7: scripts/check_compose_static_rules.py 不存在 — "
            "原 check_compose_runtime_smoke.py 应已重命名"
        )

    def test_check_compose_runtime_smoke_removed(self):
        """旧文件 check_compose_runtime_smoke.py 不应再存在(已重命名)。"""
        old_script = REPO_ROOT / "scripts" / "check_compose_runtime_smoke.py"
        assert not old_script.exists(), (
            "R69 Wave 7: scripts/check_compose_runtime_smoke.py 仍存在 — "
            "应已重命名为 check_compose_static_rules.py(消除命名误导)"
        )

    def test_static_rules_docstring_honest(self):
        """check_compose_static_rules.py docstring 必须诚实声明是静态规则门禁。"""
        script = REPO_ROOT / "scripts" / "check_compose_static_rules.py"
        content = script.read_text(encoding="utf-8")
        # R69 Wave 7: docstring 必须明确声明是"静态规则门禁"
        assert "静态规则门禁" in content, (
            "check_compose_static_rules.py 必须在 docstring 中诚实声明为 "
            "静态规则门禁(R69 Wave 7)"
        )
        # R69 Wave 7: docstring 必须说明重命名原因(历史背景)
        assert "R69 Wave 7" in content, (
            "check_compose_static_rules.py 必须在 docstring 中说明 R69 Wave 7 "
            "重命名背景(消除命名误导)"
        )


# ════════════════════════════════════════════════════════════════
# Wave 7: 真实运行态 smoke 脚本存在且符合契约
# ════════════════════════════════════════════════════════════════

class TestR69Wave7RuntimeSmokeScript:
    """R69 Wave 7: scripts/runtime_smoke_compose.py 是真实运行态 smoke。"""

    def test_runtime_smoke_script_exists(self):
        """scripts/runtime_smoke_compose.py 存在(真实运行态 smoke)。"""
        script = REPO_ROOT / "scripts" / "runtime_smoke_compose.py"
        assert script.exists(), (
            "R69 Wave 7: scripts/runtime_smoke_compose.py 不存在 — "
            "真实运行态 smoke 脚本必须存在"
        )

    def test_runtime_smoke_docstring_honest(self):
        """runtime_smoke_compose.py docstring 必须诚实声明能力与边界。"""
        script = REPO_ROOT / "scripts" / "runtime_smoke_compose.py"
        content = script.read_text(encoding="utf-8")
        # 必须诚实声明是"真实运行态 smoke"
        assert "真实运行态 smoke" in content or "真实运行态 Compose smoke" in content, (
            "runtime_smoke_compose.py docstring 必须诚实声明为真实运行态 smoke"
        )
        # 必须声明 hermetic CI 可执行(不需要真实 secrets)
        assert "hermetic" in content.lower(), (
            "runtime_smoke_compose.py 必须声明是 hermetic(可在 CI 执行,不需真实 secrets)"
        )
        # 必须声明生产真实功能不在本脚本范围(诚实边界)
        assert "不在本脚本范围" in content or "不在本脚本" in content, (
            "runtime_smoke_compose.py 必须声明生产真实功能(CRDB/Telegram/R2)"
            "不在本脚本范围(诚实边界声明)"
        )

    def test_runtime_smoke_implements_sigterm_verification(self):
        """runtime_smoke_compose.py 必须实现 SIGTERM 处理验证。"""
        script = REPO_ROOT / "scripts" / "runtime_smoke_compose.py"
        content = script.read_text(encoding="utf-8")
        # R69 Wave 7: 必须验证 SIGTERM 处理
        assert "SIGTERM" in content, (
            "runtime_smoke_compose.py 必须验证 SIGTERM 信号处理"
        )
        assert "signal.signal" in content, (
            "runtime_smoke_compose.py 必须在 smoke 容器内注册 SIGTERM handler"
        )
        # 必须有 SIGKILL 检测(docker stop -t 超时后会发 SIGKILL)
        assert "SIGKILL" in content or "137" in content, (
            "runtime_smoke_compose.py 必须检测 SIGKILL(exit 137)— "
            "SIGTERM 未在 stop_timeout 内被处理时会触发 SIGKILL"
        )

    def test_runtime_smoke_implements_restart_recovery(self):
        """runtime_smoke_compose.py 必须实现 restart 恢复验证。"""
        script = REPO_ROOT / "scripts" / "runtime_smoke_compose.py"
        content = script.read_text(encoding="utf-8")
        # R69 Wave 7: 必须有 restart 验证
        assert "restart" in content.lower(), (
            "runtime_smoke_compose.py 必须实现 restart 恢复验证"
        )

    def test_runtime_smoke_implements_log_scanning(self):
        """runtime_smoke_compose.py 必须实现日志扫描(发现隐藏故障)。"""
        script = REPO_ROOT / "scripts" / "runtime_smoke_compose.py"
        content = script.read_text(encoding="utf-8")
        # R69 Wave 7: 必须扫描日志中的 ImportError / ModuleNotFoundError / unhandled exception
        assert "ModuleNotFoundError" in content, (
            "runtime_smoke_compose.py 必须扫描日志中的 ModuleNotFoundError"
        )
        assert "ImportError" in content, (
            "runtime_smoke_compose.py 必须扫描日志中的 ImportError"
        )
        assert "unhandled exception" in content.lower() or "Unhandled" in content, (
            "runtime_smoke_compose.py 必须扫描日志中的 unhandled exception"
        )

    def test_runtime_smoke_critical_modules_in_production_image(self):
        """smoke 中导入的 CRITICAL_MODULES 必须在生产镜像中存在。

        生产镜像通过 Dockerfile COPY + RUN rm 排除:
          - scripts/(整个目录被删除)
          - tests/(整个目录被删除)
          - docs/(整个目录被删除)
          - services/db_restore.py(legacy restore CLI)
        因此 CRITICAL_MODULES 不能引用这些路径下的模块。
        """
        script = REPO_ROOT / "scripts" / "runtime_smoke_compose.py"
        content = script.read_text(encoding="utf-8")
        # 提取 CRITICAL_MODULES 列表
        match = re.search(
            r"CRITICAL_MODULES\s*=\s*\[(.*?)\]",
            content,
            re.DOTALL,
        )
        assert match, "runtime_smoke_compose.py 必须定义 CRITICAL_MODULES 列表"
        modules_block = match.group(1)
        # 提取所有字符串字面量
        modules = re.findall(r'"([^"]+)"', modules_block)
        assert len(modules) >= 10, (
            f"CRITICAL_MODULES 至少应有 10 个模块,实际 {len(modules)} 个"
        )
        # R69 Wave 7: 不允许引用 scripts/ 下的模块(生产镜像已删除 scripts/)
        for mod in modules:
            assert not mod.startswith("scripts."), (
                f"CRITICAL_MODULES 不应包含 scripts.* 模块({mod})— "
                "scripts/ 目录在生产镜像中已被 Dockerfile RUN rm 删除"
            )
            # 每个模块的文件路径必须存在于仓库中
            parts = mod.split(".")
            file_path = REPO_ROOT / Path(*parts).with_suffix(".py")
            pkg_path = REPO_ROOT / Path(*parts) / "__init__.py"
            assert file_path.exists() or pkg_path.exists(), (
                f"CRITICAL_MODULES 引用的模块 {mod} 文件不存在: {file_path}"
            )

    def test_runtime_smoke_default_cmd_fail_closed_check(self):
        """runtime_smoke_compose.py 必须验证镜像默认 CMD fail-closed。"""
        script = REPO_ROOT / "scripts" / "runtime_smoke_compose.py"
        content = script.read_text(encoding="utf-8")
        # R69 Wave 7 + P0-3: 必须验证默认 CMD 在 APP_ENV=production 下 exit 1
        assert "fail_closed" in content or "fail-closed" in content, (
            "runtime_smoke_compose.py 必须实现默认 CMD fail-closed 检查"
        )
        assert "exit 1" in content, (
            "runtime_smoke_compose.py 必须断言默认 CMD exit code = 1(fail-closed)"
        )


# ════════════════════════════════════════════════════════════════
# Wave 7: CI workflow 集成
# ════════════════════════════════════════════════════════════════

class TestR69Wave7CIIntegration:
    """R69 Wave 7: CI workflow 必须集成 runtime-smoke-compose job。"""

    def test_release_gates_has_runtime_smoke_job(self):
        """.github/workflows/release-gates.yml 必须有 runtime-smoke-compose job。"""
        rg = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = rg.read_text(encoding="utf-8")
        # R69 Wave 7: 必须有 runtime-smoke-compose job 定义
        assert re.search(r"^\s+runtime-smoke-compose:\s*$", content, re.MULTILINE), (
            "release-gates.yml 必须定义 runtime-smoke-compose job(R69 Wave 7)"
        )
        # 必须调用 runtime_smoke_compose.py
        assert "runtime_smoke_compose.py" in content, (
            "release-gates.yml 必须调用 scripts/runtime_smoke_compose.py"
        )

    def test_release_summary_needs_runtime_smoke(self):
        """release-summary 必须把 runtime-smoke-compose 列入 needs。"""
        rg = REPO_ROOT / ".github" / ".github"
        # 用 yaml 解析验证
        import yaml
        rg = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        with rg.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
        release_summary = data["jobs"]["release-summary"]
        assert "runtime-smoke-compose" in release_summary["needs"], (
            "release-summary.needs 必须包含 runtime-smoke-compose(R69 Wave 7)— "
            "runtime smoke 失败必须阻断 release"
        )
        # env 必须传递 RUNTIME_SMOKE_COMPOSE result
        assert "RUNTIME_SMOKE_COMPOSE" in release_summary["env"], (
            "release-summary.env 必须传递 RUNTIME_SMOKE_COMPOSE(R69 Wave 7)— "
            "用于 fail-closed 校验"
        )

    def test_release_summary_checks_runtime_smoke_result(self):
        """release-summary 必须在 fail-closed 检查列表中包含 runtime-smoke-compose。"""
        rg = REPO_ROOT / ".github" / "workflows" / "release-gates.yml"
        content = rg.read_text(encoding="utf-8")
        # R69 Wave 7: for entry 循环必须包含 runtime-smoke-compose
        assert "runtime-smoke-compose=${RUNTIME_SMOKE_COMPOSE}" in content, (
            "release-summary 的 fail-closed 检查列表必须包含 "
            "runtime-smoke-compose=${RUNTIME_SMOKE_COMPOSE}"
        )

    def test_deploy_check_renamed_minimal_runtime_smoke(self):
        """deploy-check.yml 的 "Minimal runtime smoke" 已重命名消除误导。"""
        dc = REPO_ROOT / ".github" / "workflows" / "deploy-check.yml"
        content = dc.read_text(encoding="utf-8")
        # R69 Wave 7: 原命名 "Minimal runtime smoke" 误导(此步骤只做 config 解析)
        # 应重命名为反映其真实能力的名称
        assert "Minimal compose config validation" in content, (
            "deploy-check.yml 必须将 'Minimal runtime smoke' 重命名为 "
            "'Minimal compose config validation'(R69 Wave 7 消除命名误导)"
        )


# ════════════════════════════════════════════════════════════════
# Wave 7: CODEOWNERS 更新
# ════════════════════════════════════════════════════════════════

class TestR69Wave7Codeowners:
    """R69 Wave 7: CODEOWNERS 必须包含新文件。"""

    def test_codeowners_has_static_rules(self):
        """CODEOWNERS 必须包含 check_compose_static_rules.py(已重命名)。"""
        co = REPO_ROOT / ".github" / "CODEOWNERS"
        content = co.read_text(encoding="utf-8")
        assert "check_compose_static_rules.py" in content, (
            "CODEOWNERS 必须包含 check_compose_static_rules.py(R69 Wave 7 重命名后)"
        )
        # 旧文件不应再被引用
        assert "check_compose_runtime_smoke.py" not in content, (
            "CODEOWNERS 不应再引用 check_compose_runtime_smoke.py(已重命名)"
        )

    def test_codeowners_has_runtime_smoke(self):
        """CODEOWNERS 必须包含 runtime_smoke_compose.py(新文件)。"""
        co = REPO_ROOT / ".github" / "CODEOWNERS"
        content = co.read_text(encoding="utf-8")
        assert "runtime_smoke_compose.py" in content, (
            "CODEOWNERS 必须包含 runtime_smoke_compose.py(R69 Wave 7 新文件)"
        )
