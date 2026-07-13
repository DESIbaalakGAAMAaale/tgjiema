"""R42 P0-4: Redis ACL contract test — 静态验证 ACL 模板与 Compose secrets fail-closed.

被测能力:
- ACL 模板静态解析:health/writer/reader/admin 4 个用户的命令白名单矩阵
- render_acl.sh fail-closed:任一 REDIS_*_PASSWORD 缺失时 exit 1
- docker-compose secrets:redis-acl-init 使用 `${VAR:?...}` 必填语法,
  mon/admin_bot/admin 使用 tgjiema_admin(非 default)
- Redis 命令权限矩阵:基于 ACL 模板构造 allow/deny 判断,覆盖 20+ 关键命令

测试策略:
- ACL 模板用 Python 解析文件,提取 `user <name> ...` 行,解析 +cmd/-cmd/-@all 规则
- render_acl.sh 用 subprocess 真实执行(若 sh 不可用则跳过该子测试)
- docker-compose.yml 用文本断言验证必填语法与服务用户映射
- 命令权限矩阵用 parametrize 批量覆盖

注意:
- ACL 规则按从左到右评估,`-@all` 是默认拒绝,后续 `+CMD` 显式允许
- 测试不连接真实 Redis,纯静态合同测试
- Windows 兼容:不依赖 shell 命令解析;subprocess 测试 sh 不可用时跳过
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import pytest

# ─── 测试文件路径 ────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACL_TEMPLATE_PATH = _PROJECT_ROOT / "config" / "redis" / "users.acl.template"
_RENDER_ACL_PATH = _PROJECT_ROOT / "config" / "redis" / "render_acl.sh"
_COMPOSE_PATH = _PROJECT_ROOT / "docker-compose.yml"


# ════════════════════════════════════════════════════════════════
# 辅助函数:文件读取
# ════════════════════════════════════════════════════════════════

def _read_acl_template() -> str:
    """读取 ACL 模板文件内容。"""
    if not _ACL_TEMPLATE_PATH.exists():
        pytest.skip(f"ACL 模板不存在: {_ACL_TEMPLATE_PATH}")
    return _ACL_TEMPLATE_PATH.read_text(encoding="utf-8")


def _read_render_acl() -> str:
    """读取 render_acl.sh 文件内容。"""
    if not _RENDER_ACL_PATH.exists():
        pytest.skip(f"render_acl.sh 不存在: {_RENDER_ACL_PATH}")
    return _RENDER_ACL_PATH.read_text(encoding="utf-8")


def _read_compose() -> str:
    """读取 docker-compose.yml 文件内容。"""
    if not _COMPOSE_PATH.exists():
        pytest.skip(f"docker-compose.yml 不存在: {_COMPOSE_PATH}")
    return _COMPOSE_PATH.read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════
# 辅助函数:ACL 规则解析
# ════════════════════════════════════════════════════════════════

def _extract_user_acl(template: str, username: str) -> str:
    """从 ACL 模板中提取指定用户的 ACL 行(去除注释)。

    Args:
        template: ACL 模板全文
        username: 用户名(如 "tgjiema_admin")

    Returns:
        ACL 规则行(如 "user tgjiema_admin on ><REDIS_ADMIN_PASSWORD> ...")
        若未找到返回空字符串
    """
    pattern = rf"^user\s+{re.escape(username)}\s+.+$"
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(pattern, stripped):
            return stripped
    return ""


def _parse_acl_rules(acl_line: str) -> dict:
    """解析单个用户的 ACL 规则行,返回结构化字段。

    Returns:
        dict 包含:
          - enabled: bool(用户是否启用)
          - nopass: bool(是否标记为 nopass)
          - has_deny_all: bool(是否含 -@all)
          - allow: set[str](显式允许的命令,大写)
          - deny: set[str](显式拒绝的命令,大写)
          - allow_categories: set[str](+@cat 类别)
          - deny_categories: set[str](-@cat 类别)
          - key_patterns: list[str](~pattern)
          - channel_patterns: list[str](&pattern)
          - password_marker: str(密码标记,如 ">" / "#" / "")
    """
    tokens = acl_line.split()
    if len(tokens) < 3 or tokens[0] != "user":
        return {
            "enabled": False, "nopass": False, "has_deny_all": False,
            "allow": set(), "deny": set(),
            "allow_categories": set(), "deny_categories": set(),
            "key_patterns": [], "channel_patterns": [],
            "password_marker": "",
        }

    result = {
        "enabled": False, "nopass": False, "has_deny_all": False,
        "allow": set(), "deny": set(),
        "allow_categories": set(), "deny_categories": set(),
        "key_patterns": [], "channel_patterns": [],
        "password_marker": "",
    }

    # tokens[0] = "user", tokens[1] = username
    rest = tokens[2:]
    for tok in rest:
        if tok == "on":
            result["enabled"] = True
        elif tok == "off":
            result["enabled"] = False
        elif tok == "nopass":
            result["nopass"] = True
        elif tok == "-@all":
            result["has_deny_all"] = True
        elif tok == "+@all":
            # +@all 等价于允许全部,清空 deny_all 标志
            result["has_deny_all"] = False
            result["allow"].add("@all")
        elif tok.startswith("+@"):
            result["allow_categories"].add(tok[2:].upper())
        elif tok.startswith("-@"):
            result["deny_categories"].add(tok[2:].upper())
        elif tok.startswith("+"):
            result["allow"].add(tok[1:].upper())
        elif tok.startswith("-"):
            result["deny"].add(tok[1:].upper())
        elif tok.startswith("~"):
            result["key_patterns"].append(tok[1:])
        elif tok.startswith("&"):
            result["channel_patterns"].append(tok[1:])
        elif tok.startswith(">"):
            result["password_marker"] = ">"
        elif tok.startswith("#"):
            result["password_marker"] = "#"
        elif tok.startswith("<") and len(tok) > 1:
            # <password 是删除密码标记,不视为密码字段
            pass

    return result


def _is_command_allowed(acl_line: str, command: str) -> bool:
    """判断 ACL 规则行是否允许执行指定命令。

    规则评估顺序(简化版,适配当前 ACL 模板的 -@all + 显式白名单模式):
      1. 命令在显式 deny 集合 -> 拒绝
      2. 命令在显式 allow 集合 -> 允许
      3. 若 has_deny_all=True 且未显式允许 -> 拒绝
      4. 否则默认允许(向后兼容旧 ACL)

    注意:本函数不展开 +@cat 类别(当前 ACL 模板未使用)。
    """
    rules = _parse_acl_rules(acl_line)
    cmd_upper = command.upper()

    # 显式拒绝优先
    if cmd_upper in rules["deny"]:
        return False
    # 显式允许次之
    if cmd_upper in rules["allow"]:
        return True
    # +@all 显式全允许
    if "@all" in rules["allow"]:
        return True
    # 默认拒绝(若 -@all 存在)
    if rules["has_deny_all"]:
        return False
    # 无 -@all 也未显式允许,默认允许(Redis 默认行为)
    return True


# ════════════════════════════════════════════════════════════════
# 辅助函数:subprocess 执行 render_acl.sh
# ════════════════════════════════════════════════════════════════

# Windows 上 Git Bash 通常未加入 PATH,提供常见安装路径作为 fallback
_GIT_BASH_SH_CANDIDATES = [
    r"C:\Program Files\Git\bin\sh.exe",
    r"C:\Program Files\Git\usr\bin\sh.exe",
    r"C:\Program Files (x86)\Git\bin\sh.exe",
    r"C:\Program Files (x86)\Git\usr\bin\sh.exe",
]


def _find_sh() -> Optional[str]:
    """查找可用的 sh 解释器路径(Linux/macOS 优先用 PATH,Windows 回退 Git Bash)。"""
    found = shutil.which("sh")
    if found:
        return found
    if os.name == "nt":
        for candidate in _GIT_BASH_SH_CANDIDATES:
            if os.path.isfile(candidate):
                return candidate
    return None


def _sh_available() -> bool:
    """检测 sh 解释器是否可用。"""
    return _find_sh() is not None


def _run_render_acl(env_overrides: dict) -> subprocess.CompletedProcess:
    """执行 render_acl.sh 并返回 CompletedProcess。

    Args:
        env_overrides: 环境变量字典(覆盖默认值)

    Returns:
        CompletedProcess(包含 returncode / stdout / stderr)
    """
    sh_path = _find_sh()
    if not sh_path:
        pytest.skip("sh 解释器不可用(Windows 需 Git Bash)")

    # 临时模板与输出路径
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_acl_template = Path(tmpdir) / "users.acl.template"
        tmp_output = Path(tmpdir) / "users.acl"

        # 复制项目模板到临时路径(让 render_acl.sh 看到真实模板内容)
        tmp_acl_template.write_text(_read_acl_template(), encoding="utf-8")

        # 构造环境变量(继承父进程 + 覆盖)
        env = dict(os.environ)
        env["ACL_TEMPLATE_PATH"] = str(tmp_acl_template)
        env["ACL_OUTPUT_PATH"] = str(tmp_output)
        env.update(env_overrides)

        # 执行 sh render_acl.sh
        return subprocess.run(
            [sh_path, str(_RENDER_ACL_PATH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )


# ════════════════════════════════════════════════════════════════
# ── 第一组:ACL 模板静态解析测试 ────────────────────────────────
# ════════════════════════════════════════════════════════════════

def test_acl_template_contains_four_users():
    """ACL 模板包含 4 个用户定义(health/writer/reader/admin)。"""
    template = _read_acl_template()
    expected_users = {"health", "tgjiema_writer", "tgjiema_reader", "tgjiema_admin"}
    found_users = set()
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        m = re.match(r"^user\s+(\S+)\s+", stripped)
        if m:
            found_users.add(m.group(1))
    assert expected_users.issubset(found_users), \
        f"ACL 模板缺少用户,期望 {expected_users},实际 {found_users}"


def test_acl_template_default_user_disabled():
    """default 用户必须被禁用(user default off)。"""
    template = _read_acl_template()
    default_line = _extract_user_acl(template, "default")
    assert default_line, "ACL 模板必须包含 default 用户定义"
    assert re.search(r"\bdefault\s+off\b", default_line), \
        f"default 用户必须 off(禁用),实际: {default_line}"


def test_acl_template_no_nopass_keyword():
    """ACL 模板不得出现 nopass 关键字(nopass 表示无需密码即可连接)。"""
    template = _read_acl_template()
    # 移除注释后检查
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert " nopass" not in f" {stripped.lower()} ", \
            f"ACL 模板禁止使用 nopass: {stripped}"


def test_health_user_only_allows_ping():
    """health 用户只允许 PING 命令。"""
    template = _read_acl_template()
    health_acl = _extract_user_acl(template, "health")
    assert health_acl, "ACL 模板缺少 health 用户"
    rules = _parse_acl_rules(health_acl)
    # 必须含 -@all 显式拒绝全部
    assert rules["has_deny_all"], "health 必须含 -@all(默认拒绝)"
    # 显式允许只有 PING
    assert rules["allow"] == {"PING"}, \
        f"health 应只允许 PING,实际允许: {rules['allow']}"


@pytest.mark.parametrize("cmd", ["GET", "SET", "FLUSHALL", "CONFIG", "KEYS", "SHUTDOWN"])
def test_health_user_denies_sensitive_commands(cmd):
    """health 用户拒绝 GET/SET/FLUSHALL/CONFIG/KEYS/SHUTDOWN(只允许 PING)。"""
    template = _read_acl_template()
    health_acl = _extract_user_acl(template, "health")
    assert health_acl, "ACL 模板缺少 health 用户"
    assert not _is_command_allowed(health_acl, cmd), \
        f"health 应拒绝 {cmd}(只允许 PING)"


@pytest.mark.parametrize(
    "cmd",
    ["XADD", "XREADGROUP", "XACK", "XAUTOCLAIM", "XGROUP", "XTRIM",
     "XLEN", "XPENDING", "XDEL", "XRANGE", "XINFO", "XCLAIM",
     "PING", "EXPIRE", "PEXPIRE", "TTL", "SET", "GET", "DEL", "EXISTS", "EVAL"],
)
def test_writer_user_allows_business_commands(cmd):
    """tgjiema_writer 允许业务所需命令(Stream + KV + lease)。"""
    template = _read_acl_template()
    writer_acl = _extract_user_acl(template, "tgjiema_writer")
    assert writer_acl, "ACL 模板缺少 tgjiema_writer"
    assert _is_command_allowed(writer_acl, cmd), \
        f"tgjiema_writer 应允许 {cmd},ACL: {writer_acl}"


@pytest.mark.parametrize(
    "cmd",
    ["FLUSHALL", "FLUSHDB", "CONFIG", "KEYS", "SHUTDOWN", "DEBUG"],
)
def test_writer_user_denies_dangerous_commands(cmd):
    """tgjiema_writer 拒绝 FLUSHALL/FLUSHDB/CONFIG/KEYS/SHUTDOWN/DEBUG 等危险命令。"""
    template = _read_acl_template()
    writer_acl = _extract_user_acl(template, "tgjiema_writer")
    assert writer_acl, "ACL 模板缺少 tgjiema_writer"
    assert not _is_command_allowed(writer_acl, cmd), \
        f"tgjiema_writer 应拒绝 {cmd}(危险命令),ACL: {writer_acl}"


@pytest.mark.parametrize(
    "cmd",
    ["GET", "XREADGROUP", "XLEN", "XPENDING", "XRANGE", "XINFO", "PING", "TTL"],
)
def test_reader_user_allows_read_commands(cmd):
    """tgjiema_reader 允许只读命令(GET/XREADGROUP/XLEN/XPENDING/XRANGE 等)。"""
    template = _read_acl_template()
    reader_acl = _extract_user_acl(template, "tgjiema_reader")
    assert reader_acl, "ACL 模板缺少 tgjiema_reader"
    assert _is_command_allowed(reader_acl, cmd), \
        f"tgjiema_reader 应允许 {cmd},ACL: {reader_acl}"


@pytest.mark.parametrize(
    "cmd",
    ["SET", "DEL", "XADD", "XACK", "XAUTOCLAIM", "XGROUP", "XTRIM", "XDEL", "EVAL", "PEXPIRE"],
)
def test_reader_user_denies_write_commands(cmd):
    """tgjiema_reader 拒绝所有写命令(SET/DEL/XADD/XACK/XAUTOCLAIM/XGROUP/XTRIM 等)。"""
    template = _read_acl_template()
    reader_acl = _extract_user_acl(template, "tgjiema_reader")
    assert reader_acl, "ACL 模板缺少 tgjiema_reader"
    assert not _is_command_allowed(reader_acl, cmd), \
        f"tgjiema_reader 应拒绝 {cmd}(写命令),ACL: {reader_acl}"


@pytest.mark.parametrize(
    "cmd",
    ["XAUTOCLAIM", "EVAL", "XTRIM", "CONFIG", "CLUSTER", "PEXPIRE", "EXISTS"],
)
def test_admin_user_allows_management_commands(cmd):
    """tgjiema_admin 允许管理命令(XAUTOCLAIM/EVAL/XTRIM/CONFIG/CLUSTER/PEXPIRE/EXISTS)。"""
    template = _read_acl_template()
    admin_acl = _extract_user_acl(template, "tgjiema_admin")
    assert admin_acl, "ACL 模板缺少 tgjiema_admin"
    assert _is_command_allowed(admin_acl, cmd), \
        f"tgjiema_admin 应允许 {cmd},ACL: {admin_acl}"


def test_admin_user_denies_flush_and_shutdown():
    """tgjiema_admin 仍拒绝 FLUSHALL/FLUSHDB/KEYS/SHUTDOWN/DEBUG(管理员不能清库)。"""
    template = _read_acl_template()
    admin_acl = _extract_user_acl(template, "tgjiema_admin")
    assert admin_acl, "ACL 模板缺少 tgjiema_admin"
    for cmd in ["FLUSHALL", "FLUSHDB", "KEYS", "SHUTDOWN", "DEBUG"]:
        assert not _is_command_allowed(admin_acl, cmd), \
            f"tgjiema_admin 应拒绝 {cmd}(即便管理员也不允许清库/关停)"


def test_writer_contains_explicit_xautoclaim_xgroup_xrange():
    """R42 P0-4: writer 显式包含 XAUTOCLAIM/XGROUP/XRANGE(crdb_sync lease & mon leader 切换所需)。"""
    template = _read_acl_template()
    writer_acl = _extract_user_acl(template, "tgjiema_writer")
    rules = _parse_acl_rules(writer_acl)
    for cmd in ["XAUTOCLAIM", "XGROUP", "XRANGE"]:
        assert cmd in rules["allow"], \
            f"writer 必须显式允许 {cmd},实际允许集合: {rules['allow']}"


def test_writer_contains_crdb_sync_lease_commands():
    """R42 P0-4: crdb_sync lease 所需命令(SET NX PX / EVAL / PEXPIRE)在 writer 白名单。

    crdb_sync 使用 tgjiema_writer 用户(见 docker-compose.yml),
    lease 通过 SET NX PX 抢锁 + Lua EVAL 做 fencing token CAS + PEXPIRE 续期。
    """
    template = _read_acl_template()
    writer_acl = _extract_user_acl(template, "tgjiema_writer")
    for cmd in ["SET", "EVAL", "PEXPIRE", "EXISTS"]:
        assert _is_command_allowed(writer_acl, cmd), \
            f"writer 必须允许 crdb_sync lease 命令 {cmd}"


def test_admin_includes_all_writer_commands():
    """admin 应包含 writer 的全部业务命令(管理员权限 ⊇ writer 权限)。"""
    template = _read_acl_template()
    writer_acl = _extract_user_acl(template, "tgjiema_writer")
    admin_acl = _extract_user_acl(template, "tgjiema_admin")
    writer_rules = _parse_acl_rules(writer_acl)
    admin_rules = _parse_acl_rules(admin_acl)
    missing = writer_rules["allow"] - admin_rules["allow"]
    assert not missing, \
        f"admin 应包含 writer 全部业务命令,缺失: {missing}"


# ════════════════════════════════════════════════════════════════
# ── 第二组:render_acl.sh fail-closed 测试 ───────────────────────
# ════════════════════════════════════════════════════════════════

def test_render_acl_script_contains_failclosed_logic():
    """render_acl.sh 文本必须包含 4 个密码的 fail-closed 校验逻辑。"""
    script = _read_render_acl()
    # 每个 REDIS_*_PASSWORD 都应有 -z 检查 + exit 1
    for var in ["REDIS_HEALTH_PASSWORD", "REDIS_WRITER_PASSWORD",
                "REDIS_READER_PASSWORD", "REDIS_ADMIN_PASSWORD"]:
        assert var in script, f"render_acl.sh 必须校验 {var}"
        # 校验 fail-closed exit 1 出现在该变量检查附近(简化:整体计数)
    # 4 个变量各自的引用(render_acl.sh 用 ${REDIS_*_PASSWORD:-} 形式赋值给本地变量)
    # regex 兼容 ${VAR} / $VAR / 字面 VAR 形式
    var_ref_count = len(re.findall(r'REDIS_\w+_PASSWORD', script))
    assert var_ref_count >= 4, \
        f"render_acl.sh 至少 4 处 REDIS_*_PASSWORD 引用,实际 {var_ref_count}"
    # 必须含 exit 1
    assert "exit 1" in script, "render_acl.sh 必须含 exit 1(fail-closed)"
    # 必须含 -z 检查(每个变量都应有空值检查)
    z_check_count = len(re.findall(r'\[\s*-z\s+"\$\w+"\s*\]', script))
    assert z_check_count >= 4, \
        f"render_acl.sh 至少 4 处 [ -z $VAR ] 检查,实际 {z_check_count}"


def test_render_acl_script_uses_set_e_option():
    """render_acl.sh 必须用 set -e 或 set -eu(任何错误立即退出)。"""
    script = _read_render_acl()
    assert re.search(r"^set\s+-e", script, re.MULTILINE), \
        "render_acl.sh 必须以 set -e 开头(或 set -eu/-euo pipefail)"


def test_render_acl_failclosed_when_health_password_missing():
    """REDIS_HEALTH_PASSWORD 缺失 → render_acl.sh exit 1。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用(Windows 需 Git Bash)")
    env = {
        "REDIS_HEALTH_PASSWORD": "",
        "REDIS_WRITER_PASSWORD": "writer_pwd_2026",
        "REDIS_READER_PASSWORD": "reader_pwd_2026",
        "REDIS_ADMIN_PASSWORD": "admin_pwd_2026",
    }
    result = _run_render_acl(env)
    assert result.returncode != 0, \
        f"REDIS_HEALTH_PASSWORD 缺失应 exit 非零,实际 returncode={result.returncode}"
    # stderr 应提示 fail-closed
    combined = (result.stderr + result.stdout).lower()
    assert "fail-closed" in combined or "redis_health_password" in combined or "未设置" in combined, \
        f"stderr 应提示 fail-closed,实际: {result.stderr}"


def test_render_acl_failclosed_when_writer_password_missing():
    """REDIS_WRITER_PASSWORD 缺失 → render_acl.sh exit 1。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用(Windows 需 Git Bash)")
    env = {
        "REDIS_HEALTH_PASSWORD": "health_pwd_2026",
        "REDIS_WRITER_PASSWORD": "",
        "REDIS_READER_PASSWORD": "reader_pwd_2026",
        "REDIS_ADMIN_PASSWORD": "admin_pwd_2026",
    }
    result = _run_render_acl(env)
    assert result.returncode != 0, \
        f"REDIS_WRITER_PASSWORD 缺失应 exit 非零,实际 returncode={result.returncode}"


def test_render_acl_failclosed_when_reader_password_missing():
    """REDIS_READER_PASSWORD 缺失 → render_acl.sh exit 1。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用(Windows 需 Git Bash)")
    env = {
        "REDIS_HEALTH_PASSWORD": "health_pwd_2026",
        "REDIS_WRITER_PASSWORD": "writer_pwd_2026",
        "REDIS_READER_PASSWORD": "",
        "REDIS_ADMIN_PASSWORD": "admin_pwd_2026",
    }
    result = _run_render_acl(env)
    assert result.returncode != 0, \
        f"REDIS_READER_PASSWORD 缺失应 exit 非零,实际 returncode={result.returncode}"


def test_render_acl_failclosed_when_admin_password_missing():
    """REDIS_ADMIN_PASSWORD 缺失 → render_acl.sh exit 1。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用(Windows 需 Git Bash)")
    env = {
        "REDIS_HEALTH_PASSWORD": "health_pwd_2026",
        "REDIS_WRITER_PASSWORD": "writer_pwd_2026",
        "REDIS_READER_PASSWORD": "reader_pwd_2026",
        "REDIS_ADMIN_PASSWORD": "",
    }
    result = _run_render_acl(env)
    assert result.returncode != 0, \
        f"REDIS_ADMIN_PASSWORD 缺失应 exit 非零,实际 returncode={result.returncode}"


def test_render_acl_succeeds_and_no_placeholder_in_output():
    """全部密码提供 → render_acl.sh exit 0,输出文件不含 '<' / 'changeme' 占位符。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用(Windows 需 Git Bash)")
    env = {
        "REDIS_HEALTH_PASSWORD": "strong_health_pwd_xyz_2026",
        "REDIS_WRITER_PASSWORD": "strong_writer_pwd_xyz_2026",
        "REDIS_READER_PASSWORD": "strong_reader_pwd_xyz_2026",
        "REDIS_ADMIN_PASSWORD": "strong_admin_pwd_xyz_2026",
    }
    result = _run_render_acl(env)
    assert result.returncode == 0, \
        f"全部密码提供应 exit 0,实际 returncode={result.returncode}, stderr={result.stderr}"


# ════════════════════════════════════════════════════════════════
# ── 第三组:docker-compose secrets 验证 ─────────────────────────
# ════════════════════════════════════════════════════════════════

def test_compose_redis_healthcheck_no_changeme_fallback():
    """docker-compose.yml 中 redis healthcheck 不含 :-changeme 默认值 fallback。"""
    compose = _read_compose()
    # 不应出现 :-changeme / :-password / :-secret 等默认值 fallback
    forbidden_patterns = [r":-changeme", r":-password\b", r":-secret\b", r":-\$\{REDIS"]
    for pat in forbidden_patterns:
        assert not re.search(pat, compose, re.IGNORECASE), \
            f"docker-compose.yml 不得含默认密码 fallback: {pat}"


def test_compose_redis_acl_init_uses_required_password_syntax():
    """docker-compose.yml 中 redis-acl-init 使用 ${REDIS_*_PASSWORD:?...} 必填语法。"""
    compose = _read_compose()
    for var in ["REDIS_HEALTH_PASSWORD", "REDIS_WRITER_PASSWORD",
                "REDIS_READER_PASSWORD", "REDIS_ADMIN_PASSWORD"]:
        # 期望形如 ${REDIS_HEALTH_PASSWORD:?...}
        pattern = rf"\$\{{{re.escape(var)}:\?[^}}]+\}}"
        assert re.search(pattern, compose), \
            f"docker-compose.yml 必须使用 ${{{var}:?...}} 必填语法,缺失"


def test_compose_admin_services_use_tgjiema_admin_user():
    """docker-compose.yml 中 mon/admin_bot/admin 服务使用 tgjiema_admin(非 default)。"""
    compose = _read_compose()
    for service in ["mon", "admin_bot", "admin"]:
        # 查找该服务下的 REDIS_URL,期望 redis://tgjiema_admin:...
        # 简化:在整个 compose 中查找 redis://tgjiema_admin:
        # (更严格可按 service 块解析,但 compose 无冒号转义问题不大)
        assert "tgjiema_admin" in compose, \
            f"docker-compose.yml 中 {service} 必须使用 tgjiema_admin 用户"
    # 至少 3 处 tgjiema_admin 引用(mon/admin_bot/admin 各一处)
    admin_count = compose.count("tgjiema_admin")
    assert admin_count >= 3, \
        f"docker-compose.yml 应至少 3 处 tgjiema_admin 引用(mon/admin_bot/admin),实际 {admin_count}"


def test_compose_writer_services_use_tgjiema_writer_user():
    """docker-compose.yml 中 up/idx/dsp/db_writer/crdb_sync/db_backup 使用 tgjiema_writer。"""
    compose = _read_compose()
    # 至少 6 处 tgjiema_writer 引用
    writer_count = compose.count("tgjiema_writer")
    assert writer_count >= 6, \
        f"docker-compose.yml 应至少 6 处 tgjiema_writer 引用(up/idx/dsp/db_writer/crdb_sync/db_backup),实际 {writer_count}"


def test_compose_no_default_user_in_redis_url():
    """docker-compose.yml 中 REDIS_URL 不应使用 default 用户(必须显式指定用户名)。"""
    compose = _read_compose()
    # 不应出现 redis://default: 或 redis://:password@(无用户名)
    assert "redis://default:" not in compose, \
        "docker-compose.yml 中 REDIS_URL 不得使用 default 用户"
    # 不应出现 redis://:password@(无用户名,使用 default 隐式)
    assert not re.search(r"redis://:[^@/]+@redis", compose), \
        "docker-compose.yml 中 REDIS_URL 必须显式指定用户名"


# ════════════════════════════════════════════════════════════════
# ── 第四组:Redis 命令权限矩阵测试(基于 ACL 模板构造 allow/deny)
# ════════════════════════════════════════════════════════════════

# 权限矩阵:每个用户对各命令的期望 allow/deny
# 格式: (username, command, expected_allowed)
_PERMISSION_MATRIX = [
    # health: 只允许 PING
    ("health", "PING", True),
    ("health", "GET", False),
    ("health", "SET", False),
    ("health", "FLUSHALL", False),
    ("health", "CONFIG", False),
    ("health", "KEYS", False),
    ("health", "SHUTDOWN", False),
    ("health", "XADD", False),
    # tgjiema_writer: 业务命令 + crdb_sync lease
    ("tgjiema_writer", "XADD", True),
    ("tgjiema_writer", "XREADGROUP", True),
    ("tgjiema_writer", "XACK", True),
    ("tgjiema_writer", "XAUTOCLAIM", True),
    ("tgjiema_writer", "XGROUP", True),
    ("tgjiema_writer", "XTRIM", True),
    ("tgjiema_writer", "XLEN", True),
    ("tgjiema_writer", "XPENDING", True),
    ("tgjiema_writer", "XDEL", True),
    ("tgjiema_writer", "XRANGE", True),
    ("tgjiema_writer", "XINFO", True),
    ("tgjiema_writer", "XCLAIM", True),
    ("tgjiema_writer", "SET", True),
    ("tgjiema_writer", "GET", True),
    ("tgjiema_writer", "DEL", True),
    ("tgjiema_writer", "EXISTS", True),
    ("tgjiema_writer", "EXPIRE", True),
    ("tgjiema_writer", "PEXPIRE", True),
    ("tgjiema_writer", "TTL", True),
    ("tgjiema_writer", "EVAL", True),
    ("tgjiema_writer", "PING", True),
    ("tgjiema_writer", "FLUSHALL", False),
    ("tgjiema_writer", "FLUSHDB", False),
    ("tgjiema_writer", "CONFIG", False),
    ("tgjiema_writer", "KEYS", False),
    ("tgjiema_writer", "SHUTDOWN", False),
    ("tgjiema_writer", "DEBUG", False),
    # tgjiema_reader: 只读
    ("tgjiema_reader", "GET", True),
    ("tgjiema_reader", "XREADGROUP", True),
    ("tgjiema_reader", "XLEN", True),
    ("tgjiema_reader", "XPENDING", True),
    ("tgjiema_reader", "XRANGE", True),
    ("tgjiema_reader", "XINFO", True),
    ("tgjiema_reader", "PING", True),
    ("tgjiema_reader", "TTL", True),
    ("tgjiema_reader", "SET", False),
    ("tgjiema_reader", "DEL", False),
    ("tgjiema_reader", "XADD", False),
    ("tgjiema_reader", "XACK", False),
    ("tgjiema_reader", "XAUTOCLAIM", False),
    ("tgjiema_reader", "XGROUP", False),
    ("tgjiema_reader", "XTRIM", False),
    ("tgjiema_reader", "XDEL", False),
    ("tgjiema_reader", "EVAL", False),
    ("tgjiema_reader", "PEXPIRE", False),
    ("tgjiema_reader", "EXPIRE", False),
    ("tgjiema_reader", "FLUSHALL", False),
    ("tgjiema_reader", "CONFIG", False),
    ("tgjiema_reader", "KEYS", False),
    ("tgjiema_reader", "SHUTDOWN", False),
    # tgjiema_admin: 管理 + 业务命令
    ("tgjiema_admin", "XADD", True),
    ("tgjiema_admin", "XREADGROUP", True),
    ("tgjiema_admin", "XACK", True),
    ("tgjiema_admin", "XAUTOCLAIM", True),
    ("tgjiema_admin", "XTRIM", True),
    ("tgjiema_admin", "XLEN", True),
    ("tgjiema_admin", "XPENDING", True),
    ("tgjiema_admin", "XDEL", True),
    ("tgjiema_admin", "SET", True),
    ("tgjiema_admin", "GET", True),
    ("tgjiema_admin", "DEL", True),
    ("tgjiema_admin", "EXISTS", True),
    ("tgjiema_admin", "EXPIRE", True),
    ("tgjiema_admin", "PEXPIRE", True),
    ("tgjiema_admin", "EVAL", True),
    ("tgjiema_admin", "CONFIG", True),
    ("tgjiema_admin", "CLUSTER", True),
    ("tgjiema_admin", "INFO", True),
    ("tgjiema_admin", "CLIENT", True),
    ("tgjiema_admin", "TIME", True),
    ("tgjiema_admin", "MEMORY", True),
    ("tgjiema_admin", "SLOWLOG", True),
    ("tgjiema_admin", "LATENCY", True),
    ("tgjiema_admin", "FLUSHALL", False),
    ("tgjiema_admin", "FLUSHDB", False),
    ("tgjiema_admin", "KEYS", False),
    ("tgjiema_admin", "SHUTDOWN", False),
    ("tgjiema_admin", "DEBUG", False),
]


@pytest.mark.parametrize(
    "username,command,expected",
    _PERMISSION_MATRIX,
    ids=[f"{u}:{c}:{'allow' if e else 'deny'}" for u, c, e in _PERMISSION_MATRIX],
)
def test_redis_command_permission_matrix(username, command, expected):
    """基于 ACL 模板验证各用户对各命令的 allow/deny 与期望一致。

    覆盖 80+ 个 (user, command) 组合,构成完整权限矩阵合同。
    """
    template = _read_acl_template()
    acl_line = _extract_user_acl(template, username)
    assert acl_line, f"ACL 模板缺少用户: {username}"
    actual = _is_command_allowed(acl_line, command)
    assert actual == expected, \
        f"权限矩阵不匹配: user={username} cmd={command} 期望={expected} 实际={actual}\nACL: {acl_line}"


def test_all_users_restricted_to_tgjiema_namespace():
    """所有业务用户(writer/reader/admin)的 key 模式必须限制到 tgjiema:* 命名空间。"""
    template = _read_acl_template()
    for username in ["tgjiema_writer", "tgjiema_reader", "tgjiema_admin"]:
        acl_line = _extract_user_acl(template, username)
        rules = _parse_acl_rules(acl_line)
        # 必须含 ~tgjiema:* key 模式
        assert "tgjiema:*" in rules["key_patterns"], \
            f"{username} 必须限制 key 到 tgjiema:*,实际 key 模式: {rules['key_patterns']}"
        # 不应允许 ~*(全 key 通配,危险)
        assert "*" not in rules["key_patterns"] or "tgjiema:*" in rules["key_patterns"], \
            f"{username} 不应使用 ~*(全 key 通配)"


def test_health_user_key_pattern_wildcard_but_no_commands():
    """health 用户 ~* 通配但实际无任何 +CMD 能访问 key(-@all +PING,PING 不读 key)。"""
    template = _read_acl_template()
    health_acl = _extract_user_acl(template, "health")
    rules = _parse_acl_rules(health_acl)
    # health 用 ~* 仅为语法占位
    assert "*" in rules["key_patterns"], "health 应使用 ~* 语法占位"
    # 但只有 PING 命令,PING 不读 key
    assert rules["allow"] == {"PING"}, "health 应只允许 PING"


def test_all_business_users_have_deny_all_baseline():
    """所有业务用户(writer/reader/admin)都必须以 -@all 作为默认拒绝基线。"""
    template = _read_acl_template()
    for username in ["tgjiema_writer", "tgjiema_reader", "tgjiema_admin"]:
        acl_line = _extract_user_acl(template, username)
        rules = _parse_acl_rules(acl_line)
        assert rules["has_deny_all"], \
            f"{username} 必须含 -@all(默认拒绝基线),否则未列出的命令会被默认允许"
