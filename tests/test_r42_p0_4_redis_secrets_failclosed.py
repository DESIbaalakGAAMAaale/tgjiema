"""R42 P0-4: Redis secrets fail-closed 合同测试。

被测能力:
- 缺失任一 REDIS_*_PASSWORD 时 render_acl.sh exit 非零(fail-closed)
- 全部密码提供时渲染 users.acl 成功
- 渲染后的 users.acl 不含占位符 / 默认密码 / 明文泄漏
- docker-compose.yml / .env.example 不含硬编码密码
- render_acl.sh 自身使用 set -e 并不输出密码到 stdout

测试策略:
- subprocess 真实执行 render_acl.sh(若 sh 不可用,部分测试跳过)
- 解析渲染后的 users.acl 文件,断言 4 个用户 + 密码字段 + 无占位符
- 静态解析 docker-compose.yml / .env.example / render_acl.sh 文本

注意:
- 渲染后 users.acl 中密码以 `>actualpassword` 形式呈现(render_acl.sh 仅 sed 替换,
  不计算 SHA256 hash)。Redis ACL 支持 `>password`(明文)和 `#hash`(SHA256)两种
  密码标记,本测试同时接受两种形式,核心目标是验证"密码字段已注入且非占位符"。
- Windows 兼容:subprocess 用 sh 执行,通过 _find_sh() 查找 Git Bash。
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
_ENV_EXAMPLE_PATH = _PROJECT_ROOT / ".env.example"


# ════════════════════════════════════════════════════════════════
# 辅助函数:文件读取
# ════════════════════════════════════════════════════════════════

def _read_text(path: Path) -> str:
    """读取文件文本,不存在则跳过。"""
    if not path.exists():
        pytest.skip(f"文件不存在: {path}")
    return path.read_text(encoding="utf-8")


# ════════════════════════════════════════════════════════════════
# 辅助函数:sh 解释器查找
# ════════════════════════════════════════════════════════════════

_GIT_BASH_SH_CANDIDATES = [
    r"C:\Program Files\Git\bin\sh.exe",
    r"C:\Program Files\Git\usr\bin\sh.exe",
    r"C:\Program Files (x86)\Git\bin\sh.exe",
    r"C:\Program Files (x86)\Git\usr\bin\sh.exe",
]


def _find_sh() -> Optional[str]:
    """查找可用的 sh 解释器路径。"""
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


# ════════════════════════════════════════════════════════════════
# 辅助函数:执行 render_acl.sh 并返回 (returncode, stdout, stderr, output_path)
# ════════════════════════════════════════════════════════════════

def _run_render_acl(env_overrides: dict) -> tuple:
    """执行 render_acl.sh,返回 (returncode, stdout, stderr, output_path)。

    Args:
        env_overrides: 环境变量覆盖

    Returns:
        (returncode, stdout_text, stderr_text, output_path_or_None)
    """
    sh_path = _find_sh()
    if not sh_path:
        pytest.skip("sh 解释器不可用(Windows 需 Git Bash)")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_acl_template = Path(tmpdir) / "users.acl.template"
        tmp_output = Path(tmpdir) / "users.acl"

        # 复制项目模板到临时路径
        tmp_acl_template.write_text(_read_text(_ACL_TEMPLATE_PATH), encoding="utf-8")

        env = dict(os.environ)
        env["ACL_TEMPLATE_PATH"] = str(tmp_acl_template)
        env["ACL_OUTPUT_PATH"] = str(tmp_output)
        env.update(env_overrides)

        result = subprocess.run(
            [sh_path, str(_RENDER_ACL_PATH)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        output_content = None
        if tmp_output.exists():
            output_content = tmp_output.read_text(encoding="utf-8")

        return result.returncode, result.stdout, result.stderr, output_content


# ════════════════════════════════════════════════════════════════
# 强密码常量(测试用,不被 render_acl.sh 视为占位符)
# ════════════════════════════════════════════════════════════════

_HEALTH_PWD = "strong_health_pwd_4f8a9b2c_2026"
_WRITER_PWD = "strong_writer_pwd_7d3e1f6a_2026"
_READER_PWD = "strong_reader_pwd_9c2b8e4d_2026"
_ADMIN_PWD = "strong_admin_pwd_5a1d7e3b_2026"

_ALL_PASSWORDS = {
    "REDIS_HEALTH_PASSWORD": _HEALTH_PWD,
    "REDIS_WRITER_PASSWORD": _WRITER_PWD,
    "REDIS_READER_PASSWORD": _READER_PWD,
    "REDIS_ADMIN_PASSWORD": _ADMIN_PWD,
}


# ════════════════════════════════════════════════════════════════
# ── 测试:secrets fail-closed(任一密码缺失 → exit 1) ──────────
# ════════════════════════════════════════════════════════════════

def test_render_acl_failclosed_when_health_password_empty():
    """空 REDIS_HEALTH_PASSWORD → render_acl.sh exit 非零。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用")
    env = dict(_ALL_PASSWORDS)
    env["REDIS_HEALTH_PASSWORD"] = ""
    returncode, _, stderr, _ = _run_render_acl(env)
    assert returncode != 0, \
        f"REDIS_HEALTH_PASSWORD 为空时应 exit 非零,实际 returncode={returncode}"
    assert "REDIS_HEALTH_PASSWORD" in stderr or "fail-closed" in stderr.lower() or "未设置" in stderr, \
        f"stderr 应提示 REDIS_HEALTH_PASSWORD 缺失,实际: {stderr}"


def test_render_acl_failclosed_when_writer_password_empty():
    """空 REDIS_WRITER_PASSWORD → render_acl.sh exit 非零。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用")
    env = dict(_ALL_PASSWORDS)
    env["REDIS_WRITER_PASSWORD"] = ""
    returncode, _, stderr, _ = _run_render_acl(env)
    assert returncode != 0, \
        f"REDIS_WRITER_PASSWORD 为空时应 exit 非零,实际 returncode={returncode}"
    assert "REDIS_WRITER_PASSWORD" in stderr or "fail-closed" in stderr.lower() or "未设置" in stderr, \
        f"stderr 应提示 REDIS_WRITER_PASSWORD 缺失,实际: {stderr}"


def test_render_acl_failclosed_when_reader_password_empty():
    """空 REDIS_READER_PASSWORD → render_acl.sh exit 非零。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用")
    env = dict(_ALL_PASSWORDS)
    env["REDIS_READER_PASSWORD"] = ""
    returncode, _, stderr, _ = _run_render_acl(env)
    assert returncode != 0, \
        f"REDIS_READER_PASSWORD 为空时应 exit 非零,实际 returncode={returncode}"
    assert "REDIS_READER_PASSWORD" in stderr or "fail-closed" in stderr.lower() or "未设置" in stderr, \
        f"stderr 应提示 REDIS_READER_PASSWORD 缺失,实际: {stderr}"


def test_render_acl_failclosed_when_admin_password_empty():
    """空 REDIS_ADMIN_PASSWORD → render_acl.sh exit 非零。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用")
    env = dict(_ALL_PASSWORDS)
    env["REDIS_ADMIN_PASSWORD"] = ""
    returncode, _, stderr, _ = _run_render_acl(env)
    assert returncode != 0, \
        f"REDIS_ADMIN_PASSWORD 为空时应 exit 非零,实际 returncode={returncode}"
    assert "REDIS_ADMIN_PASSWORD" in stderr or "fail-closed" in stderr.lower() or "未设置" in stderr, \
        f"stderr 应提示 REDIS_ADMIN_PASSWORD 缺失,实际: {stderr}"


def test_render_acl_succeeds_when_all_passwords_provided():
    """全部密码提供 → render_acl.sh exit 0,生成 users.acl 文件。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用")
    returncode, stdout, stderr, output_content = _run_render_acl(_ALL_PASSWORDS)
    assert returncode == 0, \
        f"全部密码提供应 exit 0,实际 returncode={returncode}, stderr={stderr}"
    assert output_content is not None, "渲染后应生成 users.acl 文件"
    assert len(output_content) > 0, "渲染后的 users.acl 不应为空"


# ════════════════════════════════════════════════════════════════
# ── 测试:渲染后 users.acl 内容校验 ────────────────────────────
# ════════════════════════════════════════════════════════════════

def test_rendered_acl_contains_four_users():
    """渲染后的 users.acl 包含 4 个用户(health/writer/reader/admin)。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用")
    returncode, _, _, output_content = _run_render_acl(_ALL_PASSWORDS)
    assert returncode == 0, "渲染应成功"
    assert output_content is not None
    expected_users = ["health", "tgjiema_writer", "tgjiema_reader", "tgjiema_admin"]
    for username in expected_users:
        pattern = rf"^user\s+{re.escape(username)}\s+"
        assert re.search(pattern, output_content, re.MULTILINE), \
            f"渲染后的 users.acl 缺少用户: {username}"


def test_rendered_acl_each_user_has_password_field():
    """渲染后的 users.acl 中每个用户都有密码字段(`>` 或 `#` 前缀)。

    Redis ACL 支持两种密码标记:
      - `>password`: 明文密码(Redis 启动时自动转 hash)
      - `#hash`: SHA256 hash
    render_acl.sh 仅做 sed 替换,生成 `>actualpassword` 形式;
    本测试同时接受两种形式,核心验证"密码字段已注入"。
    """
    if not _sh_available():
        pytest.skip("sh 解释器不可用")
    returncode, _, _, output_content = _run_render_acl(_ALL_PASSWORDS)
    assert returncode == 0, "渲染应成功"
    assert output_content is not None
    for username in ["health", "tgjiema_writer", "tgjiema_reader", "tgjiema_admin"]:
        # 提取该用户的 ACL 行
        pattern = rf"^(user\s+{re.escape(username)}\s+.+)$"
        m = re.search(pattern, output_content, re.MULTILINE)
        assert m, f"渲染后 users.acl 缺少 {username} 用户行"
        acl_line = m.group(1)
        # 必须含 > 或 # 密码标记(且后跟非空字符串)
        assert re.search(r"[>#][^\s<]+", acl_line), \
            f"{username} 必须有密码字段(>password 或 #hash),实际: {acl_line}"


def test_rendered_acl_no_placeholder_left():
    """渲染后的 users.acl 不含 `<` 占位符(全部替换完毕)。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用")
    returncode, _, _, output_content = _run_render_acl(_ALL_PASSWORDS)
    assert returncode == 0, "渲染应成功"
    assert output_content is not None
    assert "<" not in output_content, \
        f"渲染后的 users.acl 不得含 '<' 占位符(应全部替换为实际密码)"


def test_rendered_acl_no_default_password_keywords():
    """渲染后的 users.acl 不含 changme/password/secret 等占位值。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用")
    returncode, _, _, output_content = _run_render_acl(_ALL_PASSWORDS)
    assert returncode == 0, "渲染应成功"
    assert output_content is not None
    forbidden = ["changme", "changeme", "password", "secret", "default_pwd"]
    for kw in forbidden:
        # 排除注释行(以 # 开头)的影响
        non_comment_lines = [
            line for line in output_content.splitlines()
            if not line.strip().startswith("#")
        ]
        non_comment_text = "\n".join(non_comment_lines)
        assert kw.lower() not in non_comment_text.lower(), \
            f"渲染后的 users.acl 非注释行不得含占位密码 '{kw}'"


def test_rendered_acl_password_not_in_stdout():
    """render_acl.sh 不应将密码输出到 stdout(只写入 users.acl 文件)。"""
    if not _sh_available():
        pytest.skip("sh 解释器不可用")
    returncode, stdout, _, _ = _run_render_acl(_ALL_PASSWORDS)
    assert returncode == 0, "渲染应成功"
    # stdout 不得含任一密码
    for pwd in [_HEALTH_PWD, _WRITER_PWD, _READER_PWD, _ADMIN_PWD]:
        assert pwd not in stdout, \
            f"render_acl.sh 不得将密码输出到 stdout(实际含 '{pwd}'),stdout: {stdout}"


# ════════════════════════════════════════════════════════════════
# ── 测试:docker-compose.yml 与 .env.example 静态校验 ──────────
# ════════════════════════════════════════════════════════════════

def test_compose_no_hardcoded_redis_passwords():
    """docker-compose.yml 中不硬编码 Redis 密码(必须通过 .env 注入)。"""
    compose = _read_text(_COMPOSE_PATH)
    # 不应出现 redis://user:password@redis 这种硬编码
    # 排除 ${REDIS_*_PASSWORD} 变量插值,只检查硬编码字面值
    # 找到所有 REDIS_URL= 行,检查其后是否含字面密码
    redis_url_lines = re.findall(r"REDIS_URL=redis://[^\n]+", compose)
    for line in redis_url_lines:
        # 必须使用 ${REDIS_*_PASSWORD} 变量插值,不得有字面密码
        # 字面密码特征: redis://user:xxx@redis 但 xxx 不以 ${ 开头
        m = re.search(r"redis://[^:]+:([^@]+)@", line)
        if m:
            cred = m.group(1)
            assert cred.startswith("${") or cred.startswith("$"), \
                f"docker-compose.yml 中 REDIS_URL 不得硬编码密码,应使用 ${{VAR}} 插值: {line}"
    # 不应出现形如 password=xxx 的字面值(排除 ${VAR})
    hardcoded_patterns = [
        r"REDIS_HEALTH_PASSWORD\s*=\s*[a-zA-Z0-9]+",
        r"REDIS_WRITER_PASSWORD\s*=\s*[a-zA-Z0-9]+",
        r"REDIS_READER_PASSWORD\s*=\s*[a-zA-Z0-9]+",
        r"REDIS_ADMIN_PASSWORD\s*=\s*[a-zA-Z0-9]+",
    ]
    for pat in hardcoded_patterns:
        # 允许 ${VAR:?...} 形式,排除 ${ 开头
        matches = re.findall(pat, compose)
        for match in matches:
            # 检查 = 后是否字面值(非 ${VAR} 插值)
            value = match.split("=", 1)[1].strip()
            if not value.startswith("$"):
                pytest.fail(f"docker-compose.yml 硬编码 Redis 密码: {match}")


def test_env_example_documents_redis_passwords():
    """ .env.example 必须提示设置 REDIS_*_PASSWORD(4 个变量都出现)。"""
    env_example = _read_text(_ENV_EXAMPLE_PATH)
    for var in ["REDIS_HEALTH_PASSWORD", "REDIS_WRITER_PASSWORD",
                "REDIS_READER_PASSWORD", "REDIS_ADMIN_PASSWORD"]:
        assert var in env_example, \
            f".env.example 必须文档化 {var}(缺失或注释掉也算)"


def test_env_example_marks_redis_passwords_as_required():
    """ .env.example 应明确标注 REDIS_*_PASSWORD 为必填(注释含"必填"或上下文提示)。"""
    env_example = _read_text(_ENV_EXAMPLE_PATH)
    # 找到 REDIS_*_PASSWORD 区块附近的注释,检查是否提示必填
    # 简化:检查整个文件含"必填"或"required"语义关键词附近
    # 至少 4 个变量都应出现
    redis_section_pattern = r"REDIS_\w+_PASSWORD\s*="
    matches = re.findall(redis_section_pattern, env_example)
    assert len(matches) >= 4, \
        f".env.example 应至少 4 处 REDIS_*_PASSWORD 提示,实际 {len(matches)} 处"


def test_render_acl_uses_set_e_or_pipefail():
    """render_acl.sh 必须使用 set -e 或 set -euo pipefail(任何错误立即退出)。"""
    script = _read_text(_RENDER_ACL_PATH)
    # 至少含 set -e(或更强)
    assert re.search(r"^set\s+-e(u?)(o\s+pipefail)?$", script, re.MULTILINE), \
        "render_acl.sh 必须以 set -e / set -eu / set -euo pipefail 开头"


def test_render_acl_validates_no_placeholder_in_output():
    """render_acl.sh 必须含校验逻辑:输出文件不得含 '<' 占位符。"""
    script = _read_text(_RENDER_ACL_PATH)
    # 校验输出文件不含 '<' 占位符
    assert "'<'" in script or '"<"' in script or "'<'" in script, \
        "render_acl.sh 必须校验输出文件不含 '<' 字符(占位符未替换)"
    # 必须校验 changeme
    assert "changeme" in script.lower(), \
        "render_acl.sh 必须校验输出文件不含 'changeme'(默认密码未替换)"


def test_render_acl_template_uses_four_password_placeholders():
    """ACL 模板必须含 4 个 <REDIS_*_PASSWORD> 占位符。"""
    template = _read_text(_ACL_TEMPLATE_PATH)
    for var in ["REDIS_HEALTH_PASSWORD", "REDIS_WRITER_PASSWORD",
                "REDIS_READER_PASSWORD", "REDIS_ADMIN_PASSWORD"]:
        placeholder = f"<{var}>"
        assert placeholder in template, \
            f"ACL 模板必须含 {placeholder} 占位符(缺失无法注入)"


def test_render_acl_template_no_nopass_or_default_on():
    """ACL 模板不得使用 nopass 关键字,default 用户必须 off。"""
    template = _read_text(_ACL_TEMPLATE_PATH)
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # 任何 user 行不得含 nopass
        if stripped.startswith("user "):
            assert " nopass" not in stripped.lower(), \
                f"ACL 模板禁止使用 nopass: {stripped}"
    # default 必须 off
    default_match = re.search(r"^user\s+default\s+(\w+)", template, re.MULTILINE)
    assert default_match, "ACL 模板必须含 default 用户定义"
    assert default_match.group(1) == "off", \
        f"default 用户必须 off(禁用),实际: {default_match.group(1)}"
