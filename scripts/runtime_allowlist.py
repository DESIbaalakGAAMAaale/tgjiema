"""R69 P0-5 (Wave 3): 生产 OCI 镜像 runtime allowlist。

定义生产镜像中允许存在的文件/目录清单(allowlist 模式),以及明确禁止的
文件(blocklist 模式)。CI 在 docker-build 后通过 ``scripts/verify_oci_allowlist.py``
对最终镜像 filesystem 进行验证,确保:

  1. 所有生产入口依赖文件存在(无断裂 import);
  2. 无 tests/、开发脚本、密钥、缓存和本地文件;
  3. 无被禁止的 restore writer/入口(services/db_restore.py 不得存在);
  4. 无断裂 import;
  5. Python import smoke 成功(关键模块可导入)。

allowlist 与 blocklist 双重保险:
  - allowlist:显式列出生产镜像中应存在的顶层目录/文件
  - blocklist:显式列出生产镜像中不得存在的文件(即使 allowlist 漏配也被 blocklist 拦截)

R69 Wave 3 要求:
  - 不得依靠 .dockerignore 单点排除敏感文件
  - 显式区分 runtime 必需/migration/admin-only/test-only/scripts-only/offline recovery 文件
  - CI 检查对象必须是最终 OCI filesystem,而不是工作区

使用方法:
  from scripts.runtime_allowlist import (
      RUNTIME_ALLOWLIST, RUNTIME_BLOCKLIST,
      is_allowed, is_blocked, get_required_files,
  )

  # 在 CI 中验证镜像内容
  python scripts/verify_oci_allowlist.py --image <image_ref>
"""
from __future__ import annotations

from pathlib import PurePosixPath


# ════════════════════════════════════════════════════════════════
# 1. 生产 runtime allowlist — 允许进入生产 OCI 镜像的顶层目录/文件
# ════════════════════════════════════════════════════════════════

# R69 Wave 3: 这些目录/文件被 Dockerfile COPY 到 /app/,
# 是生产 runtime 启动 + 业务功能 + 迁移 + 备份恢复 + 治理验证所需的。
# 新增模块应在此处添加,否则 verify_oci_allowlist 会失败。
RUNTIME_ALLOWLIST: frozenset[str] = frozenset({
    # ── 应用入口 ──
    "app/run_all.py",                      # 多角色 standalone 入口(被 Compose 调用)
    "app/requirements.txt",                 # 依赖清单(供 import 检查参考,不参与运行)

    # ── 业务服务层(全部生产 runtime 必需) ──
    "app/services/",                        # services/ 目录(含子目录 mon/ 与 sink_adapters/)
    # 注意:services/db_restore.py 在 RUNTIME_BLOCKLIST 中显式排除,
    # 即使 COPY services/ ./services/ 复制了整个目录,后续 RUN rm 也会物理删除。

    # ── Bot handlers ──
    "app/bots/",                            # up_bot / idx_bot / dsp_bot / admin_bot / mon_bot 等

    # ── Admin Web ──
    "app/admin/",                           # Admin Web 入口

    # ── 配置层 ──
    "app/config/",                          # Settings / 环境变量

    # ── 数据库层 ──
    "app/database/",                        # cache_store / relay_db / session / migrate / migrations

    # ── 国际化 ──
    "app/locales/",                         # i18n 翻译文件

    # ── 通用工具 ──
    "app/utils/",                            # 通用 helper

    # ── 存储层 ──
    "app/storage/",                          # R2 / 本地存储

    # 注意:app/data/ 与 app/logs/ 由 Dockerfile RUN mkdir 创建,
    # 不是 COPY 目标,因此不在 allowlist 中。verify_oci_allowlist
    # 只验证 COPY 的代码完整性,不验证运行时创建的目录。
})


# ════════════════════════════════════════════════════════════════
# 2. 生产 runtime blocklist — 不得进入生产 OCI 镜像的文件/目录
# ════════════════════════════════════════════════════════════════

# R69 Wave 3: 这些文件/目录即使被 COPY 也不得出现在最终镜像中。
# CI verify_oci_allowlist 会对镜像 filesystem 检查这些路径是否存在,
# 任一存在即 fail-closed(不允许生产镜像含 legacy restore CLI/tests/scripts)。
#
# .dockerignore 是第一道防线(构建时排除),
# Dockerfile RUN rm 是第二道防线(物理删除),
# CI verify_oci_allowlist 是第三道防线(运行时验证)。
RUNTIME_BLOCKLIST: frozenset[str] = frozenset({
    # ── Legacy restore CLI(CLI-only,生产被 capability-sealed) ──
    # R69 Wave 2: 生产 runtime 写入器在 services/restore_writer.py(可用),
    # services/db_restore.py 仅作为 CLI/tests 入口,生产镜像不得包含。
    "app/services/db_restore.py",

    # ── 测试代码(测试逃生舱,绝不得进入生产) ──
    "app/tests/",
    "app/.pytest_cache/",
    "app/.coverage",
    "app/htmlcov/",
    "app/coverage.xml",
    "app/.tox/",
    "app/.mypy_cache/",
    "app/.ruff_cache/",

    # ── 运维脚本(运维入口,生产 runtime 不需要) ──
    "app/scripts/",

    # ── 文档/审计报告(生产镜像不需要) ──
    "app/docs/",
    "app/README.md",
    "app/CONTRIBUTING.md",
    "app/AGENTS.md",

    # ── CI/CD 配置(生产 runtime 不需要) ──
    "app/.github/",
    "app/.gitlab-ci.yml",
    "app/.circleci/",
    "app/.travis.yml",

    # ── IDE / 编辑器配置 ──
    "app/.vscode/",
    "app/.idea/",

    # ── Git 元数据(生产镜像不需要) ──
    "app/.git/",
    "app/.gitignore",

    # ── 构建产物 ──
    "app/build/",
    "app/dist/",
    "app/*.egg-info/",

    # ── 临时文件 ──
    "app/*.tmp",
    "app/*.bak",
    "app/*.orig",
    "app/*.log",   # 日志文件(运行时生成的 /app/logs/ 目录除外)

    # ── 数据库文件(运行时生成的除外) ──
    "app/*.db",    # SQLite 数据库文件(运行时生成的 /app/data/*.db 除外)
    "app/*.db-shm",
    "app/*.db-wal",
    "app/*.db-journal",

    # ── 环境变量文件(生产通过 systemd EnvironmentFile 注入) ──
    "app/.env",
    "app/.env.local",
    "app/.env.production",
    "app/.env.staging",
    # .env.example 与 .env.shared.example 可保留(供参考)
    # .env.secrets.<service> 不得进入镜像
    "app/.env.secrets",
})


# ════════════════════════════════════════════════════════════════
# 3. 关键生产入口依赖 — 用于 import smoke 测试
# ════════════════════════════════════════════════════════════════

# R69 Wave 3: 这些 Python 模块必须在最终镜像中可 import,
# 否则说明 COPY 缺文件或依赖断裂。
# verify_oci_allowlist 会在镜像内运行 `python -c "import <module>"` 验证。
RUNTIME_IMPORT_SMOKE: tuple[str, ...] = (
    # 应用入口
    "run_all",
    # 配置
    "config.settings",
    # 生产 runtime 写入器(R69 Wave 2)
    "services.restore_writer",
    # 严格三段式验证 + capability 签发
    "services.backup_dr_validate",
    # Restore orchestrator(蓝绿切换)
    "services.restore_orchestrator",
    # Restore backends(staging 写入)
    "services.restore_backends",
    # 备份引擎
    "services.db_backup",
    "services.backup_engine",
    # 数据库迁移
    "database.migrate",
    # Production guard
    "services._production_guard",
    # Backup schema
    "services.backup_schema",
    # 错误码
    "services.error_codes",
    # i18n
    "services.i18n",
    # 业务核心(部分代表模块)
    "services.command_bus",
    "services.permission",
    "services.rbac",
    "services.entitlements",
)


# ════════════════════════════════════════════════════════════════
# 4. 公共 API
# ════════════════════════════════════════════════════════════════


def is_allowed(path: str) -> bool:
    """判断给定路径是否在 allowlist 中。

    Args:
        path: 镜像内绝对路径(如 "/app/services/restore_writer.py")或
              相对 /app 的路径(如 "services/restore_writer.py")

    Returns:
        True 表示该路径在 allowlist 中允许存在
    """
    rel = _to_app_relative(path)
    if rel is None:
        return False
    for allowed in RUNTIME_ALLOWLIST:
        if allowed == rel:
            return True
        # 目录前缀匹配(如 "app/services/" 匹配 "app/services/restore_writer.py")
        if allowed.endswith("/") and rel.startswith(allowed):
            return True
    return False


def is_blocked(path: str) -> bool:
    """判断给定路径是否在 blocklist 中。

    Args:
        path: 镜像内绝对路径(如 "/app/services/db_restore.py")或
              相对 /app 的路径(如 "services/db_restore.py")

    Returns:
        True 表示该路径在 blocklist 中,不得存在于生产镜像
    """
    rel = _to_app_relative(path)
    if rel is None:
        return False
    for blocked in RUNTIME_BLOCKLIST:
        if blocked == rel:
            return True
        # 目录前缀匹配(如 "app/tests/" 匹配 "app/tests/test_foo.py")
        if blocked.endswith("/") and rel.startswith(blocked):
            return True
        # glob 匹配(如 "app/*.env" 匹配 "app/.env")
        if "*" in blocked:
            import fnmatch
            if fnmatch.fnmatch(rel, blocked):
                return True
    return False


def get_required_files() -> frozenset[str]:
    """返回生产镜像必须存在的文件清单(用于 CI 验证)。

    allowlist 中的目录会展开为目录存在性检查;具体文件存在性检查
    由 verify_oci_allowlist 在镜像内执行。
    """
    return RUNTIME_ALLOWLIST


def get_blocked_files() -> frozenset[str]:
    """返回生产镜像不得存在的文件清单(用于 CI 验证)。"""
    return RUNTIME_BLOCKLIST


def get_import_smoke_modules() -> tuple[str, ...]:
    """返回必须在镜像中可 import 的模块清单(用于 import smoke 测试)。"""
    return RUNTIME_IMPORT_SMOKE


def _to_app_relative(path: str) -> str | None:
    """将路径转换为相对 /app 的 POSIX 路径,带 "app/" 前缀。

    Args:
        path: 镜像内绝对路径或相对路径

    Returns:
        "app/<rel>" 形式字符串,或 None(不在 /app 下)
    """
    if not path:
        return None
    p = PurePosixPath(path)
    parts = p.parts
    # 绝对路径 /app/...
    if parts and parts[0] == "/":
        if len(parts) >= 2 and parts[1] == "app":
            return "/".join(parts[1:])
        return None
    # 相对路径:可能是 "app/..." 或 "services/..."
    if parts and parts[0] == "app":
        return "/".join(parts)
    if parts and parts[0] in ("services", "bots", "admin", "config", "database",
                              "locales", "utils", "storage", "tests", "scripts",
                              "docs", "data", "logs", "run_all.py", "requirements.txt"):
        return "app/" + "/".join(parts)
    return None
