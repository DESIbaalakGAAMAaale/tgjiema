"""R41 P0-2 / P0-3: Redis ACL fail-closed 与 .gitignore 整改测试。

被测能力:
- P0-2: .gitignore 不再排除 .github/workflows/(已恢复推送)
- P0-3: render_acl.sh 在 REDIS_*_PASSWORD 任一缺失时立即 exit 1(fail-closed)
- P0-3: docker-compose.yml 不含字符串 "changeme"(不再有默认密码 fallback)

测试策略:
- render_acl.sh: 用 subprocess 模拟环境变量缺失场景,验证 exit code == 1
- docker-compose.yml: 读取全文断言不含 "changeme"
- .gitignore: 读取全文断言不含 ".github/workflows/" 排除规则

注意: render_acl.sh 是 sh 脚本,Windows 上需 sh 解释器(通常由 Git Bash 提供);
若环境无 sh,则跳过对应子测试,不阻塞其它断言。
"""
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_ACL = REPO_ROOT / "config" / "redis" / "render_acl.sh"
DOCKER_COMPOSE = REPO_ROOT / "docker-compose.yml"
GITIGNORE = REPO_ROOT / ".gitignore"

# ── P0-3 fail-closed: render_acl.sh 密码缺失时 exit 1 ──

# Windows 上 Git Bash 通常未加入 PATH,提供常见安装路径作为 fallback
_GIT_BASH_SH_CANDIDATES = [
    r"C:\Program Files\Git\bin\sh.exe",
    r"C:\Program Files\Git\usr\bin\sh.exe",
    r"C:\Program Files (x86)\Git\bin\sh.exe",
    r"C:\Program Files (x86)\Git\usr\bin\sh.exe",
]


def _find_sh() -> Optional[str]:
    """查找可用的 sh 解释器路径。

    优先用 PATH 中的 sh;Windows 上若未配置 PATH,fallback 到 Git Bash 默认安装路径。
    返回 sh 可执行文件路径,或 None(未找到)。
    """
    # 1) PATH 中的 sh(Linux/macOS/已配置的 Windows)
    found = shutil.which("sh")
    if found:
        return found
    # 2) Windows 上 Git Bash 默认安装路径
    if os.name == "nt":
        for candidate in _GIT_BASH_SH_CANDIDATES:
            if os.path.isfile(candidate):
                return candidate
    return None


def _sh_available() -> bool:
    """检测 sh 解释器是否可用。"""
    return _find_sh() is not None


@pytest.mark.parametrize(
    "env_overrides,case_desc",
    [
        # 全部缺失
        ({"REDIS_HEALTH_PASSWORD": "", "REDIS_WRITER_PASSWORD": "", "REDIS_READER_PASSWORD": ""},
         "all_missing"),
        # 仅 HEALTH 缺失
        ({"REDIS_HEALTH_PASSWORD": "", "REDIS_WRITER_PASSWORD": "w", "REDIS_READER_PASSWORD": "r"},
         "health_missing"),
        # 仅 WRITER 缺失
        ({"REDIS_HEALTH_PASSWORD": "h", "REDIS_WRITER_PASSWORD": "", "REDIS_READER_PASSWORD": "r"},
         "writer_missing"),
        # 仅 READER 缺失
        ({"REDIS_HEALTH_PASSWORD": "h", "REDIS_WRITER_PASSWORD": "w", "REDIS_READER_PASSWORD": ""},
         "reader_missing"),
    ],
    ids=lambda p: p if isinstance(p, str) else "env",
)
def test_render_acl_failclosed_on_missing_password(env_overrides, case_desc):
    """任一 REDIS_*_PASSWORD 为空 → exit 1。

    使用 subprocess 启动 sh render_acl.sh,并通过环境变量传递空密码。
    由于 render_acl.sh 在模板不存在分支会先报"模板不存在"退出,
    我们用一个不存在的模板路径,让脚本到达密码校验阶段:
      实际上 render_acl.sh 第一步先校验模板存在,缺失会 exit 1,
      但本题断言的是密码校验逻辑,我们提供任意存在的模板文件即可。

    为避免依赖容器内模板路径,我们:
      1. 临时创建一个 users.acl.template(含占位符)
      2. 通过 ACL_TEMPLATE_PATH / ACL_OUTPUT_PATH 环境变量指向临时路径
      3. 用 subprocess 启动 sh render_acl.sh
      4. 断言 returncode == 1 且 stderr 含 "fail-closed" 或 "REDIS_*_PASSWORD 未设置"
    """
    if not _sh_available():
        pytest.skip("sh 解释器不可用(Windows 需 Git Bash,其它平台需 /bin/sh)")

    # 准备临时模板与输出路径
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="r41_p0_3_test_")
    try:
        template_path = Path(tmpdir) / "users.acl.template"
        output_path = Path(tmpdir) / "users.acl"
        # 模板内容:三个占位符
        template_path.write_text(
            "user health on +@all -@all +ping > <REDIS_HEALTH_PASSWORD>\n"
            "user tgjiema_writer on +@write +@read ~* > <REDIS_WRITER_PASSWORD>\n"
            "user tgjiema_reader on +@read ~* > <REDIS_READER_PASSWORD>\n",
            encoding="utf-8",
        )

        # 构造子进程环境:继承当前环境,再覆盖 REDIS_*_PASSWORD
        env = os.environ.copy()
        env["ACL_TEMPLATE_PATH"] = str(template_path)
        env["ACL_OUTPUT_PATH"] = str(output_path)
        for k, v in env_overrides.items():
            if v == "":
                # 设为空字符串(脚本中 ${REDIS_HEALTH_PASSWORD:-} 取值为空)
                env[k] = ""
            else:
                env[k] = v

        # 执行 render_acl.sh
        sh_path = _find_sh()
        assert sh_path is not None, "sh 解释器不可用"
        # Windows 下 sh.exe 需要把脚本路径转换为 sh 友好的格式(/f/xiangmu/...)
        # 直接传 Windows 路径 sh.exe 也能识别(实测 Git Bash sh.exe 兼容 Windows 路径)
        result = subprocess.run(
            [sh_path, str(RENDER_ACL)],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        # 断言:exit 1
        assert result.returncode == 1, (
            f"期望 exit 1(fail-closed),实际 {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # 断言:stderr 含 fail-closed 标志或对应变量名
        combined = result.stderr + result.stdout
        assert "REDIS_" in combined and (
            "fail-closed" in combined or "未设置" in combined
        ), f"stderr 未含 fail-closed 标志\nstderr: {result.stderr}"

        # 断言:输出文件不应被生成(或若被生成也不应包含占位符已替换内容)
        # 由于脚本在密码校验阶段就 exit 1,正常情况下 output_path 不应存在
        if output_path.exists():
            content = output_path.read_text(encoding="utf-8")
            assert "<REDIS_" in content, (
                "fail-closed 失败:输出文件已被生成且占位符已替换,违反 fail-closed 语义"
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_render_acl_no_gen_password_fallback():
    """render_acl.sh 源码不应再含 gen_password 函数(fallback 逻辑已删除)。"""
    content = RENDER_ACL.read_text(encoding="utf-8")
    assert "gen_password" not in content, (
        "render_acl.sh 仍含 gen_password 函数,R41 P0-3 fail-closed 整改未完成"
    )
    # 同时确认含 fail-closed 关键字
    assert "fail-closed" in content or "exit 1" in content, (
        "render_acl.sh 未含 fail-closed 标志"
    )


# ── P0-3: docker-compose.yml 不含 changeme ──

def test_docker_compose_no_changeme_default():
    """docker-compose.yml 中不应含字符串 'changeme'(任何形式)。

    R40 实现含 ${REDIS_HEALTH_PASSWORD:-changeme_health_ping} 默认值,
    R41 P0-3 整改:移除所有 changeme 字样,改用 :? required 校验。
    """
    content = DOCKER_COMPOSE.read_text(encoding="utf-8")
    assert "changeme" not in content.lower(), (
        "docker-compose.yml 仍含 'changeme' 字符串,R41 P0-3 整改未完成"
    )


def test_docker_compose_redis_acl_init_uses_required_syntax():
    """redis-acl-init 服务的三个 REDIS_*_PASSWORD 应使用 ${VAR:?...} 语法。"""
    content = DOCKER_COMPOSE.read_text(encoding="utf-8")
    # 三个变量均应使用 :? 语法(缺失即失败)
    for var in ("REDIS_HEALTH_PASSWORD", "REDIS_WRITER_PASSWORD", "REDIS_READER_PASSWORD"):
        # 至少在 redis-acl-init 服务块中出现 ${VAR:?...} 形式
        pattern = f"${{{var}:?"
        assert pattern in content, (
            f"docker-compose.yml 未含 ${{{var}:? required 校验语法(应位于 redis-acl-init 服务块)"
        )


def test_docker_compose_redis_healthcheck_no_default():
    """redis 服务 healthcheck 用的 REDIS_HEALTH_PASSWORD 不应含 :- 默认值。"""
    content = DOCKER_COMPOSE.read_text(encoding="utf-8")
    # 不应含 :-changeme_health_ping 或类似 :- 默认值
    assert "REDIS_HEALTH_PASSWORD:-" not in content, (
        "docker-compose.yml 中 REDIS_HEALTH_PASSWORD 仍使用 :- 默认值语法"
    )


# ── P0-2: .gitignore 不再排除 .github/workflows/ ──

def test_gitignore_not_exclude_github_workflows():
    """.gitignore 不应含排除 .github/workflows/ 的规则。

    R40 P2-1 因 PAT 缺少 workflow scope 暂不推送,在 .gitignore 中加入
    `.github/workflows/` 排除规则;R41 P0-2 已补齐 scope,应恢复推送。
    """
    lines = GITIGNORE.read_text(encoding="utf-8").splitlines()
    excluded = []
    for ln in lines:
        stripped = ln.strip()
        # 跳过空行与注释行
        if not stripped or stripped.startswith("#"):
            continue
        # 检查是否有 .github/workflows/ 排除规则(可能是直接规则或前缀否定)
        if ".github/workflows/" in stripped and not stripped.startswith("!"):
            excluded.append(stripped)
    assert not excluded, (
        f".gitignore 仍含 .github/workflows/ 排除规则: {excluded}\n"
        "R41 P0-2 应已恢复推送 .github/workflows/"
    )


def test_gitignore_has_r41_p0_2_marker():
    """.gitignore 应含 R41 P0-2 注释标记(说明已恢复推送)。"""
    content = GITIGNORE.read_text(encoding="utf-8")
    assert "R41 P0-2" in content, (
        ".gitignore 未含 R41 P0-2 标记,无法追溯整改历史"
    )
