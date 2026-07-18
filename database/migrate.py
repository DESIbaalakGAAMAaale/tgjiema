"""R59 P1: SQLite 版本化迁移框架。

替换 services/data_lifecycle.py 中 ``_ensure_command_approvals_table()`` 的惰性 DDL 模式,
将运行时 CREATE TABLE / ALTER TABLE 迁移到版本化 SQL 文件。

设计原则:
  - 版本化: 每个 migration 文件按 ``001_xxx.sql``, ``002_xxx.sql`` 编号,按文件名排序执行
  - 可回滚: 当前实现 up 方向(应用迁移);down 方向可通过新增降级 SQL 文件扩展
  - 可重复 dry-run: 重复执行不会产生副作用(IF NOT EXISTS + 严格白名单错误)
  - 幂等性: 已应用的 migration 通过 ``_migrations_applied`` 表记录,不会重复执行
  - 无第三方依赖: 纯 Python + aiosqlite,不引入 alembic/yoyo-migrations 等

``_migrations_applied`` 表结构(R60 P0-05 增强):
    version     TEXT PRIMARY KEY  — migration 文件名(如 '001_initial_schema.sql')
    sha256      TEXT NOT NULL     — SQL 文件内容 SHA-256(检测篡改,fail-closed)
    applied_at  TEXT NOT NULL     — 应用时间(ISO 8601 格式)
    duration_ms INTEGER           — 应用耗时(毫秒)

调用方式:
    # 在 _ensure_command_approvals_table() 中调用(兼容入口)
    from database.migrate import apply_migrations
    result = await apply_migrations(db=store._db)

    # 也可独立调用(如启动时一次性应用所有 migration)
    from database.migrate import apply_migrations
    from database.cache_store import get_cache_store
    store = get_cache_store()
    await store.init()
    result = await apply_migrations(db=store._db)

返回值:
    {
        "applied": [str],   — 本次新应用的 migration 文件名列表
        "skipped": [str],   — 已应用跳过的 migration 文件名列表
        "failed":  [str],   — 执行失败的 migration 文件名列表(非幂等错误)
    }
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from loguru import logger

from services.error_codes import AppError, ErrorCodes

# migration 文件目录(database/migrations/)
_MIGRATIONS_DIR: Path = Path(__file__).parent / "migrations"

# R61 P0-05: signed manifest listing all migrations + their SHA-256.
# Used as trust anchor for backfilling old _migrations_applied rows with empty sha256
# (trust-on-first-use: 篡改的 disk file 不能成为 trusted baseline)。
_MANIFEST_PATH: Path = _MIGRATIONS_DIR / "migration-manifest.json"

# R63 P0-04: cosign verify-blob 验证所需的常量。
# CI 在 sign-image job 中通过 cosign sign-blob --keyless 生成 detached signature,
# 签名材料与 release commit/tree 绑定。部署/迁移启动前必须 cosign verify-blob。
# 本地无 cosign 二进制或签名密钥时,通过 MIGRATION_MANIFEST_VERIFY=0 禁用验签
# (会输出 warning,但不阻断 — 仅用于本地开发/测试)。
_DEFAULT_CERT_ISSUER = "https://token.actions.githubusercontent.com"
# 仓库根目录(用于 git rev-parse 获取当前 HEAD/Tree SHA)
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent


def _is_manifest_verify_enabled() -> bool:
    """R63 P0-04: 检查是否启用 migration manifest 验签。

    通过环境变量 ``MIGRATION_MANIFEST_VERIFY`` 控制:
      - ``1`` / ``true`` / ``yes`` (大小写不敏感): 启用验签(CI 模式,fail-closed)
      - 未设置 / ``0`` / ``false`` / ``no``: 禁用验签(本地模式,warning 不阻断)

    CI 中应在 workflow 中设置 ``MIGRATION_MANIFEST_VERIFY=1`` 强制验签。
    本地开发/测试可不设置或显式设为 ``0``,会输出 warning 但不阻断迁移。

    Returns:
        True 表示启用验签(必须通过 cosign verify-blob + 签名文件存在性检查)
    """
    val = os.environ.get("MIGRATION_MANIFEST_VERIFY", "").strip().lower()
    return val in ("1", "true", "yes")


def _git_rev_parse(rev: str) -> str | None:
    """R63 P0-04: 执行 ``git rev-parse <rev>`` 获取 SHA。

    在仓库根目录执行 git 命令。若 git 不可用或不在 git 仓库中,返回 None
    (调用方应据此决定是 warn 还是 fail)。

    Args:
        rev: git revision spec,如 ``HEAD`` 或 ``HEAD^{tree}``

    Returns:
        40 字符 SHA 字符串,或 None(git 不可用/不在仓库中)
    """
    git_bin = shutil.which("git")
    if git_bin is None:
        return None
    try:
        result = subprocess.run(
            [git_bin, "rev-parse", rev],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    # 校验为 40 字符十六进制
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha.lower()):
        return None
    return sha


def _verify_manifest_head_tree_binding(data: dict[str, Any]) -> None:
    """R63 P0-04: 验证 manifest 绑定的 release_commit / tree_sha 与当前 git HEAD 一致。

    manifest 是 release artifact,必须绑定到具体的 commit + tree。
    若 manifest 的 release_commit/tree_sha 与当前 git HEAD/Tree 不一致,
    说明 manifest 是旧版本或被篡改 — 必须阻断迁移(fail-closed)。

    若 git 不可用或不在 git 仓库中(如解压部署),输出 warning 但不阻断
    (无法验证 = 无法阻断;此时应通过 MIGRATION_MANIFEST_VERIFY=1 + cosign
    verify-blob 保证 manifest 真实性)。

    Args:
        data: 已解析的 manifest JSON dict

    Raises:
        AppError(MIGRATION_MANIFEST_*): HEAD/Tree SHA 与 manifest 不一致
    """
    manifest_commit = str(data.get("release_commit", "")).strip()
    manifest_tree = str(data.get("tree_sha", "")).strip()
    if not manifest_commit or not manifest_tree:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING,
            params={"field": "release_commit/tree_sha", "reason": "empty"},
        )
    head_sha = _git_rev_parse("HEAD")
    tree_sha = _git_rev_parse("HEAD^{tree}")
    if head_sha is None or tree_sha is None:
        logger.warning(
            "[migrate] R63 P0-04: git 不可用或不在 git 仓库中, "
            "跳过 HEAD/Tree 绑定验证(无法验证 manifest 是否绑定到当前 commit) "
            "— 部署环境应通过 MIGRATION_MANIFEST_VERIFY=1 + cosign verify-blob 保证真实性"
        )
        return
    if head_sha != manifest_commit:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_BINDING_MISMATCH,
            params={
                "field": "release_commit",
                "expected": manifest_commit[:12],
                "actual": head_sha[:12],
            },
        )
    if tree_sha != manifest_tree:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_BINDING_MISMATCH,
            params={
                "field": "tree_sha",
                "expected": manifest_tree[:12],
                "actual": tree_sha[:12],
            },
        )
    logger.info(
        f"[migrate] R63 P0-04: manifest HEAD/Tree 绑定验证通过 "
        f"(commit={head_sha[:12]}..., tree={tree_sha[:12]}...)"
    )


def _verify_manifest_migration_set(data: dict[str, Any]) -> None:
    """R63 P0-04: 验证磁盘上的 migration 文件集合与 manifest 声明集合完全一致。

    要求 "磁盘集合 == manifest 集合",不允许:
      - 磁盘有但 manifest 没列出的 migration(漏项,可能跳过验签)
      - manifest 列出但磁盘不存在的 migration(多项,可能引用旧 manifest)

    Args:
        data: 已解析的 manifest JSON dict

    Raises:
        AppError(MIGRATION_MANIFEST_SET_MISMATCH): 磁盘集合与 manifest 集合不一致
    """
    manifest_versions = {
        str(entry["version"]) for entry in data.get("migrations", [])
        if "version" in entry
    }
    disk_versions = {
        f.name for f in _MIGRATIONS_DIR.glob("*.sql")
    } if _MIGRATIONS_DIR.exists() else set()
    missing_in_manifest = disk_versions - manifest_versions
    missing_on_disk = manifest_versions - disk_versions
    if missing_in_manifest:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SET_MISMATCH,
            params={
                "missing_in_manifest": sorted(missing_in_manifest),
                "missing_on_disk": [],
            },
        )
    if missing_on_disk:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SET_MISMATCH,
            params={
                "missing_in_manifest": [],
                "missing_on_disk": sorted(missing_on_disk),
            },
        )
    logger.info(
        f"[migrate] R63 P0-04: migration 集合一致性验证通过 "
        f"({len(manifest_versions)} 个 migration)"
    )


def _verify_manifest_cosign_signature(data: dict[str, Any]) -> None:
    """R63 P0-04: 通过 cosign verify-blob 验证 manifest 的 detached signature。

    要求:
      1. manifest JSON 中的 ``verification.signature_file`` / ``certificate_file``
         指向的文件必须存在(detached signature + certificate)
      2. ``cosign verify-blob`` 必须成功(签名有效 + 证书 identity/issuer 钉扎匹配)

    签名失败、签名文件缺失、证书 identity/issuer 不匹配 → raise AppError(fail-closed)。

    本地无 cosign 二进制或签名密钥时,应通过 ``MIGRATION_MANIFEST_VERIFY=0`` 禁用验签
    (会输出 warning)。CI 中必须启用。

    Args:
        data: 已解析的 manifest JSON dict

    Raises:
        AppError(MIGRATION_MANIFEST_SIGNATURE_INVALID): 签名文件缺失 / cosign 不可用 / 验签失败
    """
    verification = data.get("verification", {})
    if not isinstance(verification, dict):
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING,
            params={"field": "verification", "reason": "missing_or_not_dict"},
        )
    sig_rel = str(verification.get("signature_file", "")).strip()
    cert_rel = str(verification.get("certificate_file", "")).strip()
    issuer = str(
        verification.get("certificate_oidc_issuer", _DEFAULT_CERT_ISSUER)
    ).strip()
    if not sig_rel or not cert_rel:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING,
            params={
                "field": "signature_file/certificate_file",
                "reason": "empty",
            },
        )
    sig_path = _MIGRATIONS_DIR / sig_rel
    cert_path = _MIGRATIONS_DIR / cert_rel
    if not sig_path.exists():
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SIGNATURE_INVALID,
            params={
                "reason": "signature_file_not_found",
                "sig_file": sig_rel,
                "cert_file": cert_rel,
            },
        )
    if not cert_path.exists():
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SIGNATURE_INVALID,
            params={
                "reason": "certificate_file_not_found",
                "sig_file": sig_rel,
                "cert_file": cert_rel,
            },
        )
    cosign_bin = shutil.which("cosign")
    if cosign_bin is None:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SIGNATURE_INVALID,
            params={
                "reason": "cosign_binary_not_in_path",
                "sig_file": sig_rel,
                "cert_file": cert_rel,
            },
        )
    # 从 manifest 提取 certificate_identity_prefix,构造精确 identity
    # (CI 签名时使用的 workflow identity)
    identity_prefix = str(
        verification.get("certificate_identity_prefix", "")
    ).strip()
    if not identity_prefix:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_FIELD_MISSING,
            params={
                "field": "certificate_identity_prefix",
                "reason": "empty",
            },
        )
    # 通过 git 获取当前 ref,构造完整 identity
    # (与 CI 签名时使用的 identity 完全一致)
    head_sha = _git_rev_parse("HEAD")
    if head_sha is None:
        # 退化:只用 prefix 做前缀匹配(regexp 模式)
        identity_regexp = _escape_regexp(identity_prefix) + r".+"
        cmd = [
            cosign_bin, "verify-blob",
            "--certificate-identity-regexp", identity_regexp,
            "--certificate-oidc-issuer", issuer,
            "--certificate", str(cert_path),
            "--signature", str(sig_path),
            str(_MANIFEST_PATH),
        ]
    else:
        # 精确模式:prefix + 当前 ref(从 git symbolic-ref 获取)
        git_bin = shutil.which("git")
        ref_result = subprocess.run(
            [git_bin, "symbolic-ref", "HEAD"],
            cwd=str(_REPO_ROOT),
            capture_output=True, text=True, timeout=10, check=False,
        )
        if ref_result.returncode == 0:
            current_ref = ref_result.stdout.strip()
        else:
            # detached HEAD,退化到 regexp 模式
            current_ref = None
        if current_ref:
            identity = f"{identity_prefix}{current_ref}"
            cmd = [
                cosign_bin, "verify-blob",
                "--certificate-identity", identity,
                "--certificate-oidc-issuer", issuer,
                "--certificate", str(cert_path),
                "--signature", str(sig_path),
                str(_MANIFEST_PATH),
            ]
        else:
            identity_regexp = _escape_regexp(identity_prefix) + r".+"
            cmd = [
                cosign_bin, "verify-blob",
                "--certificate-identity-regexp", identity_regexp,
                "--certificate-oidc-issuer", issuer,
                "--certificate", str(cert_path),
                "--signature", str(sig_path),
                str(_MANIFEST_PATH),
            ]
    logger.info(
        f"[migrate] R63 P0-04: cosign verify-blob 验证 manifest 签名 "
        f"(manifest={_MANIFEST_PATH.name})"
    )
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SIGNATURE_INVALID,
            params={
                "reason": f"cosign_verify_blob_execution_failed: {e}",
                "sig_file": sig_rel,
                "cert_file": cert_rel,
            },
        ) from e
    if result.returncode != 0:
        raise AppError(
            ErrorCodes.MIGRATION_MANIFEST_SIGNATURE_INVALID,
            params={
                "reason": f"cosign_verify_blob_failed exit={result.returncode}",
                "sig_file": sig_rel,
                "cert_file": cert_rel,
            },
        )
    logger.info("[migrate] R63 P0-04: migration manifest cosign 验签通过")


def _escape_regexp(s: str) -> str:
    """转义字符串中的正则元字符,使其可作为字面量用于 regexp。

    Args:
        s: 待转义的字符串

    Returns:
        转义后的字符串(正则元字符已转义)
    """
    import re
    return re.escape(s)


def _strip_sql_comments(sql_content: str) -> str:
    """Remove SQL comments (``--`` line and ``/* */`` block) before splitting.

    R61 P1-02: 旧版 splitter 用字符串分割 + 行内 ``--`` 截断会破坏字符串字面量
    (例如 CHECK 约束中的 ``'a-b'`` 不会被破坏,但 ``--`` 出现在字符串内时被误截断)。
    新版只移除整行 ``--`` 注释 + 块注释,保留字符串字面量内的 ``--``。
    """
    import re
    # Remove /* */ block comments (DOTALL: . matches newline)
    result = re.sub(r'/\*.*?\*/', '', sql_content, flags=re.DOTALL)
    # Remove -- line comments (only when line starts with -- after lstrip;
    # 不处理行内 -- 以避免破坏字符串字面量中的 --,如 'a-b' 或 CHECK 约束)
    lines = []
    for line in result.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('--'):
            continue
        lines.append(line)
    return '\n'.join(lines)


def _split_sql_statements(sql_content: str) -> list[str]:
    """Split SQL using :func:`sqlite3.complete_statement` for proper parsing.

    R61 P1-02: 旧版用 naive 分号分割,无法处理字符串内的分号 / 触发器体 / CASE 表达式
    内的分号。新版改用 SQLite 原生 :func:`sqlite3.complete_statement` 判断语句边界
    (该函数考虑引号配对 + 分号结尾,与 sqlite3 CLI 行为一致)。

    处理规则:
      1. 移除块注释 ``/* */`` 和整行 ``--`` 注释(保留字符串字面量)
      2. 逐行累积到 buffer,直到 :func:`sqlite3.complete_statement` 返回 True
      3. 过滤空白语句

    Args:
        sql_content: SQL 文件原始内容

    Returns:
        SQL 语句列表(每条语句已 strip)
    """
    import sqlite3
    cleaned = _strip_sql_comments(sql_content)
    statements: list[str] = []
    buffer = ""
    for line in cleaned.splitlines():
        buffer += line + "\n"
        if sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt:
                statements.append(stmt)
            buffer = ""
    # Handle any remaining buffer (无尾分号的最后一条语句)
    remaining = buffer.strip()
    if remaining:
        statements.append(remaining)
    return statements


async def _should_skip_statement(db: Any, stmt: str) -> bool:
    """R61 P1-02: 判断单条 SQL 语句是否应跳过(幂等预检)。

    替换 R59 的 ``_is_ignorable_error`` 子串匹配机制(该机制会吞掉所有
    "duplicate column" / "already exists" 错误,无法区分"列已存在"与
    "约束已存在"等不同语义)。

    新版改为执行前 PRAGMA 预检:仅对 ``ALTER TABLE ... ADD COLUMN`` 语句,
    若目标列已存在则跳过(等价于 ``ADD COLUMN IF NOT EXISTS``,SQLite 原生不支持)。
    其他 DDL 错误(语法/约束/连接等)一律让事务 ROLLBACK(fail-closed)。

    Args:
        db: aiosqlite.Connection(处于 BEGIN IMMEDIATE 事务中)
        stmt: 待执行的 SQL 语句(已 strip)

    Returns:
        True 表示该语句应跳过(列已存在);False 表示应执行
    """
    import re
    # 仅匹配:ALTER TABLE <table> ADD COLUMN <column> ...
    # (不匹配 ALTER TABLE ... RENAME / DROP COLUMN 等)
    m = re.match(
        r'ALTER\s+TABLE\s+["\'`]?(\w+)["\'`]?\s+ADD\s+COLUMN\s+["\'`]?(\w+)["\'`]?',
        stmt,
        re.IGNORECASE,
    )
    if not m:
        return False
    table_name = m.group(1)
    column_name = m.group(2)
    # PRAGMA table_info 不支持参数绑定,table_name 来自受信任的 migration SQL(非用户输入)
    cursor = await db.execute(f"PRAGMA table_info({table_name})")
    existing_cols: set[str] = {str(row[1]) for row in await cursor.fetchall()}
    return column_name in existing_cols


async def _assert_migration_fingerprint(db: Any, version: str) -> None:
    """R61 P1-02: Post-execution schema fingerprint assertion.

    在 migration 所有语句执行完毕、COMMIT 之前,验证 schema 是否符合预期
    (defense in depth:即使 SQL 执行成功但 schema 漂移也应阻断)。
    任一验证失败 raise RuntimeError,触发事务 ROLLBACK,migration 不被记录为已应用。

    Args:
        db: aiosqlite.Connection(处于 BEGIN IMMEDIATE 事务中,DDL 已执行但未提交)
        version: migration 文件名(如 '003_rebuild_command_approvals.sql')

    Raises:
        RuntimeError: schema 指纹不匹配(缺失列 / 表不存在)
    """
    if version == "003_rebuild_command_approvals.sql":
        # Verify the rebuilt command_approvals table exists with all required columns
        cursor = await db.execute("PRAGMA table_info(command_approvals)")
        cols = {str(row[1]) for row in await cursor.fetchall()}
        required = {
            "id", "action_id", "approver_id", "approval_type", "decision",
            "request_hash", "mfa_receipt", "permission", "approved_at",
            "expires_at", "consumed_at", "revoked_at", "metadata_json",
        }
        if not required.issubset(cols):
            missing = required - cols
            raise RuntimeError(
                f"Migration {version} fingerprint mismatch: "
                f"missing columns {missing} (actual cols: {sorted(cols)})"
            )

    if version == "004_effect_receipts_request_hash_unique.sql":
        # R63 P1-03: Post-migration conservation + evidence-completeness assertion.
        # 防御纵深:004 migration SQL 内已有 CASE WHEN 守恒/证据断言(违反 CHECK →
        # ROLLBACK),此处再在 Python 层做一次跨表 COUNT 比对(SQLite SQL 内难以
        # 直接 RAISE 跨表断言,Python 层是最可靠的 fail-closed 位置)。
        # 守恒等式(rename 后):
        #   count(effect_receipts)               — strict winner 行(rename 后的新表)
        #   + count(effect_receipts_r62_quarantine)            — 非法隔离行
        #   + count(effect_receipts_r62_duplicates WHERE classification='duplicate') — 去重 loser 行
        #   == count(effect_receipts_invalid_r62)              — 旧表(rename 后保留取证)
        # 证据完整性:count(effect_receipts_r62_duplicates) == count(effect_receipts_invalid_r62)
        #   (每条原始 row 都在取证表有一行,不论分类)
        cursor = await db.execute("SELECT COUNT(*) FROM effect_receipts")
        strict_count = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_quarantine"
        )
        quarantine_count = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates "
            "WHERE classification = 'duplicate'"
        )
        duplicates_count = (await cursor.fetchone())[0]
        cursor = await db.execute(
            "SELECT COUNT(*) FROM effect_receipts_invalid_r62"
        )
        original_count = (await cursor.fetchone())[0]
        if strict_count + quarantine_count + duplicates_count != original_count:
            raise RuntimeError(
                f"Migration {version} conservation assertion failed: "
                f"strict({strict_count}) + quarantine({quarantine_count}) "
                f"+ duplicates({duplicates_count}) "
                f"!= original({original_count}) — rows silently lost"
            )
        cursor = await db.execute(
            "SELECT COUNT(*) FROM effect_receipts_r62_duplicates"
        )
        evidence_count = (await cursor.fetchone())[0]
        if evidence_count != original_count:
            raise RuntimeError(
                f"Migration {version} evidence completeness failed: "
                f"duplicates evidence table has {evidence_count} rows, "
                f"original has {original_count} — not every original row has evidence"
            )


def _load_migration_manifest() -> dict[str, dict[str, Any]]:
    """R61 P0-05 / R63 P0-04: 加载并验证 migration-manifest.json,返回 {version: entry} 映射。

    manifest 是签名的 trust anchor,列出每个 migration 文件的预期 SHA-256。
    用于 backfill 旧 ``_migrations_applied`` 行(stored_sha256 为空时),
    替代 R60 的"信任当前 disk file"TOFU(篡改的 disk file 不能成为 baseline)。

    R63 P0-04 整改要点:
      - 不再把未验签 JSON 称作 signed trust anchor — 加载时强制执行:
        ① HEAD/Tree 绑定验证(manifest release_commit/tree_sha 必须匹配当前 git)
        ② 磁盘 migration 集合 == manifest 集合(不允许漏项/多项)
        ③ 若 ``MIGRATION_MANIFEST_VERIFY=1``: cosign verify-blob 验证 detached signature
      - 验签失败、签名文件缺失、HEAD/Tree 不匹配、集合不一致 → raise(fail-closed)
      - 本地无 cosign 时可设 ``MIGRATION_MANIFEST_VERIFY=0`` 跳过 cosign 验签(warning)

    Returns:
        {version: {"sha256": str, "predecessor": str|None, ...}} 映射

    Raises:
        RuntimeError: manifest 文件不存在 / 解析失败 / HEAD/Tree 不匹配 /
                      集合不一致 / cosign 验签失败(fail-closed)
    """
    import json
    if not _MANIFEST_PATH.exists():
        raise RuntimeError(
            f"R61 P0-05: migration manifest not found at {_MANIFEST_PATH} "
            f"(required for trust-on-first-use backfill of old _migrations_applied rows)"
        )
    try:
        data = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"R61 P0-05: migration manifest parse failed: {e}"
        ) from e
    migrations = data.get("migrations", [])
    if not isinstance(migrations, list):
        raise RuntimeError(
            f"R61 P0-05: migration manifest 'migrations' field is not a list"
        )
    # R63 P0-04: 加载 manifest 作为 trust anchor 前必须验证完整性
    # (HEAD/Tree 绑定 + 磁盘集合一致性 + 可选 cosign 验签)
    _verify_manifest_head_tree_binding(data)
    _verify_manifest_migration_set(data)
    if _is_manifest_verify_enabled():
        _verify_manifest_cosign_signature(data)
    else:
        logger.warning(
            "[migrate] R63 P0-04: MIGRATION_MANIFEST_VERIFY 未启用, "
            "跳过 cosign verify-blob 验签 — 本地开发/测试模式,不验证 manifest 签名。"
            "CI 部署/迁移启动前必须设置 MIGRATION_MANIFEST_VERIFY=1 强制验签。"
        )
    return {str(entry["version"]): entry for entry in migrations if "version" in entry}


def _list_migration_files() -> list[Path]:
    """列出 migrations 目录下所有 .sql 文件,按文件名排序。

    Returns:
        排序后的 Path 列表(如 001_initial_schema.sql, 002_xxx.sql, ...)
    """
    if not _MIGRATIONS_DIR.exists():
        logger.warning(f"[migrate] migration 目录不存在: {_MIGRATIONS_DIR}")
        return []
    return sorted(_MIGRATIONS_DIR.glob("*.sql"))


def _compute_sha256(file_path: Path) -> str:
    """计算文件原始字节内容的 SHA-256 校验和(十六进制小写)。

    用于在应用 migration 时记录其 SQL 内容指纹,启动时比对以检测文件被篡改
    (R60 P0-05: fail-closed,篡改/删除的 migration 文件阻断服务启动)。
    """
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


async def _get_applied_versions(db: Any) -> dict[str, str]:
    """查询已应用的 migration 版本及其 SHA-256 校验和。

    若 ``_migrations_applied`` 表不存在则自动创建(首次运行,R60 新 schema)。
    若旧 schema(无 sha256 / duration_ms 列)已存在,通过 ALTER TABLE ADD COLUMN
    补列(向后兼容 R59 已部署实例);旧记录的 sha256 留空,由 ``apply_migrations``
    用当前文件内容回填。

    R60 P0-05 schema:
        version     TEXT PRIMARY KEY  — migration 文件名
        sha256      TEXT NOT NULL     — SQL 文件内容 SHA-256(检测篡改)
        applied_at  TEXT NOT NULL     — 应用时间(ISO 8601)
        duration_ms INTEGER           — 应用耗时(毫秒)

    Args:
        db: aiosqlite.Connection

    Returns:
        {version: sha256} 映射(已应用 migration 的文件名 → 校验和,空串表示旧记录未回填)
    """
    # 创建版本记录表(首次运行,R60 新 schema;对已存在表是 no-op)
    await db.execute(
        """CREATE TABLE IF NOT EXISTS _migrations_applied (
            version     TEXT PRIMARY KEY,
            sha256      TEXT NOT NULL,
            applied_at  TEXT NOT NULL,
            duration_ms INTEGER
        )"""
    )
    # 向后兼容: 旧 R59 schema 只有 (version, applied_at),补 sha256 / duration_ms 列。
    # CREATE TABLE IF NOT EXISTS 不会修改已存在表,需通过 PRAGMA 检测列是否缺失。
    cursor = await db.execute("PRAGMA table_info(_migrations_applied)")
    existing_cols: set[str] = {str(row[1]) for row in await cursor.fetchall()}
    if "sha256" not in existing_cols:
        # 旧表已有行无 sha256,先以可空列补上(不能对非空表加 NOT NULL),
        # 后续由 apply_migrations 回填当前文件校验和
        await db.execute(
            "ALTER TABLE _migrations_applied ADD COLUMN sha256 TEXT"
        )
    if "duration_ms" not in existing_cols:
        await db.execute(
            "ALTER TABLE _migrations_applied ADD COLUMN duration_ms INTEGER"
        )
    await db.commit()
    # 查询已应用的版本及其 sha256
    cursor = await db.execute("SELECT version, sha256 FROM _migrations_applied")
    rows = await cursor.fetchall()
    return {str(row[0]): (row[1] or "") for row in rows}


async def _apply_single_migration(db: Any, migration_file: Path) -> bool:
    """应用单个 migration 文件(R60 P0-05 / R61 P1-02: 显式事务 + 预检 + 指纹断言)。

    整个 migration(所有 DDL + 版本记录 INSERT)在单个 ``BEGIN IMMEDIATE`` 事务中
    执行,确保部分 DDL 或被篡改的 migration 文件不会被记录为已应用。

    R61 P1-02 改动(替换旧的 _is_ignorable_error 子串匹配):
      - 执行前用 ``_should_skip_statement`` PRAGMA 预检(仅 ALTER TABLE ADD COLUMN):
        目标列已存在则跳过(等价 ADD COLUMN IF NOT EXISTS)
      - 其他任何 DDL 错误(语法/约束/连接等)一律让事务 ROLLBACK(fail-closed)
      - 所有语句执行后、COMMIT 前调用 ``_assert_migration_fingerprint`` 验证 schema
        (defense in depth: SQL 执行成功但 schema 漂移也阻断)

    R61 P0-05 改动(防御纵深):
      - 版本记录 INSERT 改用 plain ``INSERT INTO``(原 ``INSERT OR REPLACE``)
        PRIMARY KEY 冲突 raise RuntimeError(不应发生 — apply_migrations 已跳过
        已应用版本;若发生说明存在并发写入或状态错乱)

    成功后将版本记录(含 SQL 内容 SHA-256 与耗时)写入 ``_migrations_applied`` 表
    (与 DDL 在同一事务内提交)。

    Args:
        db: aiosqlite.Connection
        migration_file: migration SQL 文件路径

    Returns:
        True 应用成功;False 应用失败(可恢复的执行/提交失败)

    Raises:
        RuntimeError: schema 指纹不匹配(P1-02)或 INSERT PRIMARY KEY 冲突(P0-05)
    """
    version = migration_file.name
    sql_content = migration_file.read_text(encoding="utf-8")
    sha256 = _compute_sha256(migration_file)
    statements = _split_sql_statements(sql_content)
    if not statements:
        logger.warning(f"[migrate] {version} 无可执行 SQL 语句,跳过")
        return True
    logger.info(
        f"[migrate] 应用 {version}({len(statements)} 条语句, sha256={sha256[:12]}...)"
    )
    start_ts = time.perf_counter()
    # R60 P0-05: 显式事务 — 单个 migration 的所有 DDL + 版本记录 INSERT 必须原子提交
    # R60 §ci-fix: except 中不直接 return False(AST 错误协议规则3),
    # 改用标志位在 except 外返回,保持 bool 契约同时满足 fail-closed
    begin_failed = False
    try:
        await db.execute("BEGIN IMMEDIATE")
    except Exception as e:
        logger.error(f"[migrate] {version} BEGIN IMMEDIATE 失败: {e}")
        begin_failed = True
    if begin_failed:
        return False
    commit_failed = False
    try:
        for stmt in statements:
            # R61 P1-02: 执行前 PRAGMA 预检(替换旧的 _is_ignorable_error 子串匹配)
            # 仅对 ALTER TABLE ADD COLUMN: 若列已存在则跳过(幂等,等价 IF NOT EXISTS)
            if await _should_skip_statement(db, stmt):
                logger.debug(
                    f"[migrate] {version} 语句跳过(目标列已存在,幂等预检): "
                    f"{stmt[:80]}..."
                )
                continue
            await db.execute(stmt)
        # R61 P1-02: schema 指纹断言 — 所有 DDL 执行成功后、COMMIT 前验证 schema
        # (defense in depth: SQL 执行成功但 schema 漂移也阻断,raise RuntimeError)
        await _assert_migration_fingerprint(db, version)
        # 记录为已应用(与 DDL 在同一事务内,确保原子)
        now_iso = _dt.datetime.now().isoformat()
        duration_ms = int((time.perf_counter() - start_ts) * 1000)
        # R61 P0-05: plain INSERT(非 INSERT OR REPLACE) — PRIMARY KEY 冲突应
        # 失败而非静默覆盖(apply_migrations 已跳过已应用版本,冲突=状态错乱)
        try:
            await db.execute(
                "INSERT INTO _migrations_applied "
                "(version, sha256, applied_at, duration_ms) VALUES (?, ?, ?, ?)",
                (version, sha256, now_iso, duration_ms),
            )
        except Exception as insert_err:
            raise RuntimeError(
                f"[migrate] {version} INSERT INTO _migrations_applied 失败 "
                f"(可能 PRIMARY KEY 冲突 — 版本已应用?并发写入?): {insert_err}"
            ) from insert_err
        await db.execute("COMMIT")
    except RuntimeError:
        # R61 P0-05 / P1-02: RuntimeError = 防御纵深失败(指纹不匹配 / PRIMARY KEY 冲突)
        # ROLLBACK 后重新抛出(不转换为 return False,确保调用方见到显式失败)
        try:
            await db.execute("ROLLBACK")
        except Exception as rollback_err:
            logger.error(f"[migrate] {version} ROLLBACK 失败: {rollback_err}")
        raise
    except Exception as e:
        # 其他执行/提交失败: 回滚,不记录为已应用
        logger.error(
            f"[migrate] {version} 事务提交/版本记录失败,执行 ROLLBACK: {e}"
        )
        try:
            await db.execute("ROLLBACK")
        except Exception as rollback_err:
            logger.error(f"[migrate] {version} ROLLBACK 失败: {rollback_err}")
        commit_failed = True
    if commit_failed:
        return False
    logger.info(
        f"[migrate] {version} 应用完成(耗时 {duration_ms}ms)"
    )
    return True


async def apply_migrations(db: Any = None) -> dict[str, list[str]]:
    """R59 P1: 应用所有未执行的 SQLite migration。

    本函数是迁移框架的主入口,执行流程:
      1. 获取数据库连接(参数传入或从 CacheStore 获取)
      2. 创建 ``_migrations_applied`` 版本记录表(首次运行,R60 新 schema)
      3. 列出 migrations 目录下所有 .sql 文件,按文件名排序
      4. R60 P0-05: 校验已应用 migration 文件 SHA-256(篡改/删除 → raise 阻断启动)
      5. 对每个未应用的 migration:
         a. 读取 SQL 内容并按分号分割为独立语句
         b. 在单个 BEGIN IMMEDIATE 事务中逐条执行,可忽略
            "duplicate column" / "already exists" 错误
         c. 非白名单错误立即 ROLLBACK 并终止该 migration,不记录为已应用
         d. 成功后(同一事务)写入 _migrations_applied 表(含 sha256 / duration_ms)
      6. 返回应用结果汇总;若 failed 非空则 raise(fail-closed,禁止继续服务)

    幂等性保证:
      - 已应用的 migration 不会重复执行(_migrations_applied 主键去重)
      - SQL 语句使用 IF NOT EXISTS / 白名单错误处理,重复执行无副作用
      - 支持多次 dry-run(重复调用 apply_migrations 不会产生副作用)
      - R60 P0-05: 启动时校验已应用 migration 文件 SHA-256,篡改/删除则 raise
      - R60 P0-05: 失败的 migration 必须 raise,禁止带失败结果继续服务

    Args:
        db: 可选的 aiosqlite.Connection。若为 None,从 CacheStore 获取连接。
            测试中可传入自定义连接以隔离测试。

    Returns:
        {
            "applied": [str],  — 本次新应用的 migration 文件名列表
            "skipped": [str],  — 已应用跳过的 migration 文件名列表
            "failed":  [str],  — 执行失败的 migration 文件名列表(非幂等错误)
        }
    """
    # 获取数据库连接
    own_connection = False
    if db is None:
        from database.cache_store import get_cache_store
        store = get_cache_store()
        if not store._db:
            # CacheStore 未初始化,尝试 init
            await store.init()
        db = store._db
        own_connection = True
    if db is None:
        logger.error("[migrate] 无法获取 SQLite 连接,迁移中止")
        return {"applied": [], "skipped": [], "failed": []}

    result: dict[str, list[str]] = {
        "applied": [],
        "skipped": [],
        "failed": [],
    }

    # 查询已应用版本
    try:
        applied_versions = await _get_applied_versions(db)
    except Exception as e:
        logger.error(f"[migrate] 查询已应用版本失败: {e}")
        return result

    # 列出所有 migration 文件
    migration_files = _list_migration_files()
    if not migration_files:
        logger.warning("[migrate] 无 migration 文件可执行")
        return result

    # R60 P0-05: 启动时校验已应用 migration 文件的 SHA-256(fail-closed)
    # 任何已应用 migration 的文件被修改或删除 → 阻断启动,禁止带篡改文件继续服务
    file_map: dict[str, Path] = {mf.name: mf for mf in migration_files}
    # R61 P0-05: 旧记录(stored_sha256 为空)的 backfill 改用签名 manifest 作为
    # trust anchor,而非"信任当前 disk file"(原 TOFU 使篡改的 disk file 成为 baseline)。
    # 仅在存在空 sha256 旧记录时才加载 manifest(避免全新库无 manifest 也能启动)。
    manifest = None
    needs_manifest = any(
        not stored_sha256 for stored_sha256 in applied_versions.values()
    )
    if needs_manifest:
        manifest = _load_migration_manifest()
    for version, stored_sha256 in applied_versions.items():
        if version not in file_map:
            raise RuntimeError(
                f"Migration file {version} has been modified or removed "
                f"(stored_sha256={stored_sha256 or '<empty>'}, "
                f"actual_sha256=None)"
            )
        actual_sha256 = _compute_sha256(file_map[version])
        if not stored_sha256:
            # R61 P0-05: 旧记录无 sha256 — 不再信任 disk file,改用 manifest 验证。
            # trust anchor 应是签名 manifest,而非可被篡改的 disk file。
            if manifest is None:
                # needs_manifest 为 False 但 stored_sha256 为空 — 逻辑不应发生,
                # 仍 fail-closed(强制要求 manifest)
                manifest = _load_migration_manifest()
            if version not in manifest:
                raise RuntimeError(
                    f"Migration {version} not listed in migration-manifest.json "
                    f"(cannot backfill empty stored_sha256 — unknown trust baseline)"
                )
            manifest_sha256 = manifest[version].get("sha256", "")
            if not manifest_sha256:
                raise RuntimeError(
                    f"Migration {version} manifest entry missing 'sha256' field"
                )
            if actual_sha256 != manifest_sha256:
                # disk file 的 sha256 与 manifest 不符 → 篡改 detected
                # (拒绝将篡改的 disk 作为 trusted baseline 回填)
                raise RuntimeError(
                    f"Migration file {version} has been tampered "
                    f"(disk sha256={actual_sha256}, "
                    f"manifest sha256={manifest_sha256}) — "
                    f"refusing to backfill empty stored_sha256 from untrusted disk"
                )
            # disk 与 manifest 一致 → 用 manifest 的 sha256 回填(trust anchor)
            await db.execute(
                "UPDATE _migrations_applied SET sha256 = ? WHERE version = ?",
                (manifest_sha256, version),
            )
            await db.commit()
            logger.info(
                f"[migrate] R61 P0-05: 补齐历史 migration {version} 的 sha256 "
                f"(from signed manifest, disk verified match)"
            )
        elif actual_sha256 != stored_sha256:
            raise RuntimeError(
                f"Migration file {version} has been modified or removed "
                f"(stored_sha256={stored_sha256}, "
                f"actual_sha256={actual_sha256})"
            )

    # 逐个应用未执行的 migration
    for mf in migration_files:
        version = mf.name
        if version in applied_versions:
            result["skipped"].append(version)
            continue
        success = await _apply_single_migration(db, mf)
        if success:
            result["applied"].append(version)
        else:
            result["failed"].append(version)
            # 遇到严重错误终止后续 migration(避免版本错位)
            logger.error(
                f"[migrate] {version} 应用失败,终止后续 migration(避免版本错位)"
            )
            break

    logger.info(
        f"[migrate] 迁移完成: 应用 {len(result['applied'])} 个, "
        f"跳过 {len(result['skipped'])} 个, 失败 {len(result['failed'])} 个"
    )
    # R60 P0-05: 失败必须 raise,禁止带失败结果继续提供服务(fail-closed)
    if result["failed"]:
        raise RuntimeError(
            f"[migrate] migration 应用失败,阻断启动: failed={result['failed']}"
        )
    return result
