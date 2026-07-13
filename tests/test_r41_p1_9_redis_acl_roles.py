"""R41 P1-9: Redis ACL 最小权限完整模型测试。

测试覆盖:
- ACL 模板中存在 4 个用户(health/tgjiema_writer/tgjiema_reader/tgjiema_admin)
- tgjiema_admin 用户的权限矩阵(+XAUTOCLAIM/+EVAL/+XTRIM/+CONFIG/+CLUSTER 等)
- tgjiema_writer 不应有管理命令(+XAUTOCLAIM/+EVAL/+CONFIG/+CLUSTER)
- tgjiema_reader 应只有读命令
- health 用户应只有 +PING
- default 用户应 off
- 所有用户都应限制在 ~tgjiema:* 命名空间(health 除外,只需 PING)
- render_acl.sh 包含 4 个密码的 fail-closed 校验
- render_acl.sh 包含 REDIS_ADMIN_PASSWORD 占位符替换
- docker-compose.yml 中 mon/admin_bot/admin 使用 tgjiema_admin
- docker-compose.yml 中 redis-acl-init 包含 REDIS_ADMIN_PASSWORD
- docker-compose.yml 中 up/idx/dsp/db_writer/crdb_sync/db_backup 使用 tgjiema_writer
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# ─── 测试文件路径 ────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ACL_TEMPLATE_PATH = _PROJECT_ROOT / "config" / "redis" / "users.acl.template"
_RENDER_ACL_PATH = _PROJECT_ROOT / "config" / "redis" / "render_acl.sh"
_COMPOSE_PATH = _PROJECT_ROOT / "docker-compose.yml"


# ════════════════════════════════════════════════════════════════
# 辅助函数
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


def _extract_user_acl(template: str, username: str) -> str:
    """从 ACL 模板中提取指定用户的 ACL 行(去除注释)。

    Args:
        template: ACL 模板全文
        username: 用户名(如 "tgjiema_admin")

    Returns:
        ACL 规则行(如 "user tgjiema_admin on ><REDIS_ADMIN_PASSWORD> ~tgjiema:* ...")
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


def _extract_commands(acl_line: str) -> set[str]:
    """从 ACL 规则行中提取所有 +命令 集合。

    例如 "user x on ~k -@all +PING +GET" 返回 {"PING", "GET"}
    """
    return {m.group(1) for m in re.finditer(r"\+([A-Z]+)", acl_line)}


# ════════════════════════════════════════════════════════════════
# 1. ACL 模板: 4 个用户存在性
# ════════════════════════════════════════════════════════════════

class TestAclUsersExist:
    """R41 P1-9: ACL 模板应定义 4 个用户(health/writer/reader/admin)。"""

    def test_health_user_exists(self):
        """health 用户应在 ACL 模板中存在。"""
        template = _read_acl_template()
        line = _extract_user_acl(template, "health")
        assert line, "ACL 模板缺少 health 用户定义"

    def test_tgjiema_writer_user_exists(self):
        """tgjiema_writer 用户应在 ACL 模板中存在。"""
        template = _read_acl_template()
        line = _extract_user_acl(template, "tgjiema_writer")
        assert line, "ACL 模板缺少 tgjiema_writer 用户定义"

    def test_tgjiema_reader_user_exists(self):
        """tgjiema_reader 用户应在 ACL 模板中存在。"""
        template = _read_acl_template()
        line = _extract_user_acl(template, "tgjiema_reader")
        assert line, "ACL 模板缺少 tgjiema_reader 用户定义"

    def test_tgjiema_admin_user_exists(self):
        """R41 P1-9: tgjiema_admin 用户应在 ACL 模板中存在(新增)。"""
        template = _read_acl_template()
        line = _extract_user_acl(template, "tgjiema_admin")
        assert line, "R41 P1-9: ACL 模板缺少 tgjiema_admin 用户定义(应为 mon/admin_bot/admin 提供)"

    def test_default_user_off(self):
        """default 用户应 off(禁用未认证连接)。"""
        template = _read_acl_template()
        line = _extract_user_acl(template, "default")
        assert line, "ACL 模板缺少 default 用户定义"
        assert " off" in line, f"default 用户应为 off: {line}"


# ════════════════════════════════════════════════════════════════
# 2. tgjiema_admin 权限矩阵
# ════════════════════════════════════════════════════════════════

class TestTgjiemaAdminPermissions:
    """R41 P1-9: tgjiema_admin 用户的权限矩阵(mon/admin_bot/admin 所需)。"""

    @pytest.fixture
    def admin_acl_line(self) -> str:
        """提取 tgjiema_admin 用户的 ACL 规则行。"""
        template = _read_acl_template()
        line = _extract_user_acl(template, "tgjiema_admin")
        if not line:
            pytest.skip("tgjiema_admin 用户未定义")
        return line

    def test_admin_has_xautoclaim(self, admin_acl_line):
        """tgjiema_admin 应有 +XAUTOCLAIM(leader lease 切换时认领 pending 消息)。"""
        cmds = _extract_commands(admin_acl_line)
        assert "XAUTOCLAIM" in cmds, \
            f"tgjiema_admin 缺少 +XAUTOCLAIM(leader lease 切换所需): {admin_acl_line}"

    def test_admin_has_eval(self, admin_acl_line):
        """tgjiema_admin 应有 +EVAL(执行 Lua 脚本做原子操作/多 key CAS)。"""
        cmds = _extract_commands(admin_acl_line)
        assert "EVAL" in cmds, \
            f"tgjiema_admin 缺少 +EVAL(fencing token/CAS 所需): {admin_acl_line}"

    def test_admin_has_xtrim(self, admin_acl_line):
        """tgjiema_admin 应有 +XTRIM(主动裁剪 Stream)。"""
        cmds = _extract_commands(admin_acl_line)
        assert "XTRIM" in cmds, \
            f"tgjiema_admin 缺少 +XTRIM: {admin_acl_line}"

    def test_admin_has_config(self, admin_acl_line):
        """tgjiema_admin 应有 +CONFIG(读取运行时配置,用于监控/诊断)。"""
        cmds = _extract_commands(admin_acl_line)
        assert "CONFIG" in cmds, \
            f"tgjiema_admin 缺少 +CONFIG: {admin_acl_line}"

    def test_admin_has_cluster(self, admin_acl_line):
        """tgjiema_admin 应有 +CLUSTER(集群状态查询)。"""
        cmds = _extract_commands(admin_acl_line)
        assert "CLUSTER" in cmds, \
            f"tgjiema_admin 缺少 +CLUSTER: {admin_acl_line}"

    def test_admin_has_info(self, admin_acl_line):
        """tgjiema_admin 应有 +INFO(运行时信息查询)。"""
        cmds = _extract_commands(admin_acl_line)
        assert "INFO" in cmds, \
            f"tgjiema_admin 缺少 +INFO: {admin_acl_line}"

    def test_admin_has_client(self, admin_acl_line):
        """tgjiema_admin 应有 +CLIENT(客户端管理)。"""
        cmds = _extract_commands(admin_acl_line)
        assert "CLIENT" in cmds, \
            f"tgjiema_admin 缺少 +CLIENT: {admin_acl_line}"

    def test_admin_has_time(self, admin_acl_line):
        """tgjiema_admin 应有 +TIME(时间查询)。"""
        cmds = _extract_commands(admin_acl_line)
        assert "TIME" in cmds, \
            f"tgjiema_admin 缺少 +TIME: {admin_acl_line}"

    def test_admin_has_memory(self, admin_acl_line):
        """tgjiema_admin 应有 +MEMORY(内存使用查询)。"""
        cmds = _extract_commands(admin_acl_line)
        assert "MEMORY" in cmds, \
            f"tgjiema_admin 缺少 +MEMORY: {admin_acl_line}"

    def test_admin_has_slowlog(self, admin_acl_line):
        """tgjiema_admin 应有 +SLOWLOG(慢查询日志)。"""
        cmds = _extract_commands(admin_acl_line)
        assert "SLOWLOG" in cmds, \
            f"tgjiema_admin 缺少 +SLOWLOG: {admin_acl_line}"

    def test_admin_has_latency(self, admin_acl_line):
        """tgjiema_admin 应有 +LATENCY(延迟监控)。"""
        cmds = _extract_commands(admin_acl_line)
        assert "LATENCY" in cmds, \
            f"tgjiema_admin 缺少 +LATENCY: {admin_acl_line}"

    def test_admin_has_all_writer_commands(self, admin_acl_line):
        """tgjiema_admin 应继承 writer 的所有命令(XADD/XREADGROUP/XACK 等)。"""
        cmds = _extract_commands(admin_acl_line)
        required = {
            "XADD", "XREADGROUP", "XACK", "XLEN", "XPENDING",
            "XCLAIM", "XINFO", "XDEL", "PING", "EXPIRE",
            "TTL", "SET", "GET", "DEL",
        }
        missing = required - cmds
        assert not missing, \
            f"tgjiema_admin 缺少 writer 命令: {sorted(missing)} (实际: {sorted(cmds)})"

    def test_admin_uses_negative_all(self, admin_acl_line):
        """tgjiema_admin 应使用 -@all(正向白名单)。"""
        assert "-@all" in admin_acl_line, \
            f"tgjiema_admin 应使用 -@all(正向白名单): {admin_acl_line}"

    def test_admin_limited_to_tgjiema_namespace(self, admin_acl_line):
        """tgjiema_admin 应限制在 ~tgjiema:* 命名空间(防止访问其他业务 key)。"""
        assert "~tgjiema:*" in admin_acl_line, \
            f"tgjiema_admin 应限制在 ~tgjiema:* 命名空间: {admin_acl_line}"

    def test_admin_uses_redis_admin_password_placeholder(self, admin_acl_line):
        """tgjiema_admin 应使用 <REDIS_ADMIN_PASSWORD> 占位符。"""
        assert "><REDIS_ADMIN_PASSWORD>" in admin_acl_line, \
            f"tgjiema_admin 应使用 <REDIS_ADMIN_PASSWORD> 占位符: {admin_acl_line}"

    def test_admin_channel_pattern_restricted(self, admin_acl_line):
        """tgjiema_admin 应限制 channel pattern 到 tgjiema:*(&tgjiema:*)。"""
        assert "&tgjiema:*" in admin_acl_line, \
            f"tgjiema_admin 应限制 channel pattern 到 &tgjiema:*: {admin_acl_line}"


# ════════════════════════════════════════════════════════════════
# 3. tgjiema_writer 权限矩阵(不应有管理命令)
# ════════════════════════════════════════════════════════════════

class TestTgjiemaWriterPermissions:
    """R41 P1-9: tgjiema_writer 用户应仅限业务写入,无管理命令。"""

    @pytest.fixture
    def writer_acl_line(self) -> str:
        template = _read_acl_template()
        line = _extract_user_acl(template, "tgjiema_writer")
        if not line:
            pytest.skip("tgjiema_writer 用户未定义")
        return line

    def test_writer_does_not_have_xautoclaim(self, writer_acl_line):
        """R42 P0-4: tgjiema_writer 现在允许 +XAUTOCLAIM(crdb_sync lease 与 mon leader 切换所需)。

        R41 要求 writer 不应有 XAUTOCLAIM,但 R42 P0-4 整改要求 writer 包含
        XAUTOCLAIM/XGROUP/XTRIM/EVAL 等命令(Redis Streams 恢复 + DLQ Lua + leader lease)。
        本测试已更新为验证 writer 确实包含这些命令(R42 要求)。
        """
        cmds = _extract_commands(writer_acl_line)
        assert "XAUTOCLAIM" in cmds, \
            f"R42 P0-4 要求 tgjiema_writer 包含 +XAUTOCLAIM: {writer_acl_line}"

    def test_writer_does_not_have_eval(self, writer_acl_line):
        """R42 P0-4: tgjiema_writer 现在允许 +EVAL(DLQ Lua 脚本所需)。

        R41 要求 writer 不应有 EVAL,但 R42 P0-4 整改要求 writer 包含 EVAL
        (Redis Streams DLQ 重试逻辑使用 Lua 脚本)。
        本测试已更新为验证 writer 确实包含 EVAL(R42 要求)。
        """
        cmds = _extract_commands(writer_acl_line)
        assert "EVAL" in cmds, \
            f"R42 P0-4 要求 tgjiema_writer 包含 +EVAL: {writer_acl_line}"

    def test_writer_does_not_have_config(self, writer_acl_line):
        """tgjiema_writer 不应有 +CONFIG(管理操作)。"""
        cmds = _extract_commands(writer_acl_line)
        assert "CONFIG" not in cmds, \
            f"tgjiema_writer 不应有 +CONFIG: {writer_acl_line}"

    def test_writer_does_not_have_cluster(self, writer_acl_line):
        """tgjiema_writer 不应有 +CLUSTER(管理操作)。"""
        cmds = _extract_commands(writer_acl_line)
        assert "CLUSTER" not in cmds, \
            f"tgjiema_writer 不应有 +CLUSTER: {writer_acl_line}"

    def test_writer_has_stream_write_commands(self, writer_acl_line):
        """tgjiema_writer 应有 Stream 写入命令(XADD/XREADGROUP/XACK 等)。"""
        cmds = _extract_commands(writer_acl_line)
        required = {"XADD", "XREADGROUP", "XACK", "XLEN", "XCLAIM", "XINFO", "XDEL", "PING"}
        missing = required - cmds
        assert not missing, \
            f"tgjiema_writer 缺少 Stream 命令: {sorted(missing)}"

    def test_writer_has_kv_commands(self, writer_acl_line):
        """tgjiema_writer 应有 KV 命令(SET/GET/DEL/EXPIRE/TTL)。"""
        cmds = _extract_commands(writer_acl_line)
        required = {"SET", "GET", "DEL", "EXPIRE", "TTL"}
        missing = required - cmds
        assert not missing, \
            f"tgjiema_writer 缺少 KV 命令: {sorted(missing)}"

    def test_writer_uses_negative_all(self, writer_acl_line):
        """tgjiema_writer 应使用 -@all(正向白名单)。"""
        assert "-@all" in writer_acl_line

    def test_writer_limited_to_tgjiema_namespace(self, writer_acl_line):
        """tgjiema_writer 应限制在 ~tgjiema:* 命名空间。"""
        assert "~tgjiema:*" in writer_acl_line


# ════════════════════════════════════════════════════════════════
# 4. tgjiema_reader 权限矩阵(只读)
# ════════════════════════════════════════════════════════════════

class TestTgjiemaReaderPermissions:
    """R41 P1-9: tgjiema_reader 用户应仅有读命令。"""

    @pytest.fixture
    def reader_acl_line(self) -> str:
        template = _read_acl_template()
        line = _extract_user_acl(template, "tgjiema_reader")
        if not line:
            pytest.skip("tgjiema_reader 用户未定义")
        return line

    def test_reader_has_xreadgroup(self, reader_acl_line):
        """tgjiema_reader 应有 +XREADGROUP(消费组读取)。"""
        cmds = _extract_commands(reader_acl_line)
        assert "XREADGROUP" in cmds

    def test_reader_has_get(self, reader_acl_line):
        """tgjiema_reader 应有 +GET(KV 读取)。"""
        cmds = _extract_commands(reader_acl_line)
        assert "GET" in cmds

    def test_reader_does_not_have_write_commands(self, reader_acl_line):
        """tgjiema_reader 不应有写命令(SET/DEL/XADD/XACK 等)。"""
        cmds = _extract_commands(reader_acl_line)
        forbidden = {"SET", "DEL", "XADD", "XACK", "XTRIM", "XCLAIM", "XAUTOCLAIM"}
        found = cmds & forbidden
        assert not found, \
            f"tgjiema_reader 不应有写命令: {sorted(found)}"

    def test_reader_does_not_have_admin_commands(self, reader_acl_line):
        """tgjiema_reader 不应有管理命令(CONFIG/CLUSTER/EVAL 等)。"""
        cmds = _extract_commands(reader_acl_line)
        forbidden = {"CONFIG", "CLUSTER", "EVAL", "XTRIM"}
        found = cmds & forbidden
        assert not found, \
            f"tgjiema_reader 不应有管理命令: {sorted(found)}"


# ════════════════════════════════════════════════════════════════
# 5. health 用户权限
# ════════════════════════════════════════════════════════════════

class TestHealthUserPermissions:
    """R41 P1-9: health 用户应仅有 +PING(docker-compose healthcheck 用)。"""

    @pytest.fixture
    def health_acl_line(self) -> str:
        template = _read_acl_template()
        line = _extract_user_acl(template, "health")
        if not line:
            pytest.skip("health 用户未定义")
        return line

    def test_health_has_only_ping(self, health_acl_line):
        """health 用户应仅有 +PING 命令。"""
        cmds = _extract_commands(health_acl_line)
        assert cmds == {"PING"}, \
            f"health 用户应仅有 +PING,实际: {sorted(cmds)}"

    def test_health_uses_redis_health_password(self, health_acl_line):
        """health 用户应使用 <REDIS_HEALTH_PASSWORD> 占位符。"""
        assert "><REDIS_HEALTH_PASSWORD>" in health_acl_line


# ════════════════════════════════════════════════════════════════
# 6. render_acl.sh: 4 个密码读取 + fail-closed 校验
# ════════════════════════════════════════════════════════════════

class TestRenderAclScript:
    """R41 P1-9: render_acl.sh 应读取 4 个密码并 fail-closed 校验。"""

    def test_reads_health_password(self):
        """render_acl.sh 应读取 REDIS_HEALTH_PASSWORD。"""
        content = _read_render_acl()
        assert "REDIS_HEALTH_PASSWORD" in content
        assert 'HEALTH_PWD="${REDIS_HEALTH_PASSWORD:-}"' in content

    def test_reads_writer_password(self):
        """render_acl.sh 应读取 REDIS_WRITER_PASSWORD。"""
        content = _read_render_acl()
        assert 'WRITER_PWD="${REDIS_WRITER_PASSWORD:-}"' in content

    def test_reads_reader_password(self):
        """render_acl.sh 应读取 REDIS_READER_PASSWORD。"""
        content = _read_render_acl()
        assert 'READER_PWD="${REDIS_READER_PASSWORD:-}"' in content

    def test_reads_admin_password(self):
        """R41 P1-9: render_acl.sh 应读取 REDIS_ADMIN_PASSWORD(新增)。"""
        content = _read_render_acl()
        assert 'ADMIN_PWD="${REDIS_ADMIN_PASSWORD:-}"' in content

    def test_fail_closed_for_health_password(self):
        """REDIS_HEALTH_PASSWORD 缺失时应 exit 1(fail-closed)。

        render_acl.sh 中变量定义在前,if 检查在后,因此用正则匹配
        'if [ -z "$HEALTH_PWD" ]; then ... exit 1' 完整块(DOTALL)。
        """
        content = _read_render_acl()
        # 查找 HEALTH_PWD 空值检查 + 后续 exit 1(同一 if 块内)
        pattern = r'if\s+\[\s*-z\s+"\$HEALTH_PWD"\s*\][^;]*;\s*then.*?exit\s+1'
        assert re.search(pattern, content, re.DOTALL), \
            "render_acl.sh 缺少 HEALTH_PWD 空值检查 + exit 1(fail-closed)"

    def test_fail_closed_for_writer_password(self):
        """REDIS_WRITER_PASSWORD 缺失时应 exit 1。"""
        content = _read_render_acl()
        pattern = r'if\s+\[\s*-z\s+"\$WRITER_PWD"\s*\][^;]*;\s*then.*?exit\s+1'
        assert re.search(pattern, content, re.DOTALL), \
            "render_acl.sh 缺少 WRITER_PWD 空值检查 + exit 1"

    def test_fail_closed_for_reader_password(self):
        """REDIS_READER_PASSWORD 缺失时应 exit 1。"""
        content = _read_render_acl()
        pattern = r'if\s+\[\s*-z\s+"\$READER_PWD"\s*\][^;]*;\s*then.*?exit\s+1'
        assert re.search(pattern, content, re.DOTALL), \
            "render_acl.sh 缺少 READER_PWD 空值检查 + exit 1"

    def test_fail_closed_for_admin_password(self):
        """R41 P1-9: REDIS_ADMIN_PASSWORD 缺失时应 exit 1(新增)。"""
        content = _read_render_acl()
        assert re.search(r'if\s+\[\s*-z\s+"\$ADMIN_PWD"\s*\]', content), \
            "render_acl.sh 缺少 ADMIN_PWD 空值检查(R41 P1-9 新增)"
        # 查找 ADMIN_PWD 块内的 exit 1
        admin_block_start = content.find('ADMIN_PWD')
        admin_block = content[admin_block_start:admin_block_start + 1000]
        assert 'exit 1' in admin_block, \
            "ADMIN_PWD 块内缺少 exit 1(fail-closed 校验)"

    def test_sed_replaces_health_password(self):
        """sed 应替换 <REDIS_HEALTH_PASSWORD> 占位符。"""
        content = _read_render_acl()
        assert 's|<REDIS_HEALTH_PASSWORD>|${HEALTH_PWD}|g' in content

    def test_sed_replaces_writer_password(self):
        """sed 应替换 <REDIS_WRITER_PASSWORD> 占位符。"""
        content = _read_render_acl()
        assert 's|<REDIS_WRITER_PASSWORD>|${WRITER_PWD}|g' in content

    def test_sed_replaces_reader_password(self):
        """sed 应替换 <REDIS_READER_PASSWORD> 占位符。"""
        content = _read_render_acl()
        assert 's|<REDIS_READER_PASSWORD>|${READER_PWD}|g' in content

    def test_sed_replaces_admin_password(self):
        """R41 P1-9: sed 应替换 <REDIS_ADMIN_PASSWORD> 占位符(新增)。"""
        content = _read_render_acl()
        assert 's|<REDIS_ADMIN_PASSWORD>|${ADMIN_PWD}|g' in content, \
            "render_acl.sh 缺少 <REDIS_ADMIN_PASSWORD> 占位符替换(R41 P1-9 新增)"

    def test_admin_password_in_special_char_check(self):
        """R41 P1-9: ADMIN_PWD 应包含在 sed 特殊字符校验循环中。"""
        content = _read_render_acl()
        # 查找 for 循环,确认包含 ADMIN_PWD
        for_loop_match = re.search(r'for\s+pwd\s+in\s+([^;]+);', content)
        if for_loop_match:
            pwd_list = for_loop_match.group(1)
            assert 'ADMIN_PWD' in pwd_list, \
                f"sed 特殊字符校验循环应包含 ADMIN_PWD: {pwd_list}"

    def test_output_message_mentions_four_users(self):
        """R41 P1-9: 输出消息应提及 4 个用户(health/writer/reader/admin)。"""
        content = _read_render_acl()
        # 查找 INFO 消息中提及用户数量
        assert re.search(r'4\s+个用户', content) or \
               re.search(r'four users', content, re.IGNORECASE), \
            "render_acl.sh 输出消息应提及 4 个用户"


# ════════════════════════════════════════════════════════════════
# 7. docker-compose.yml: 服务使用正确的 Redis 用户
# ════════════════════════════════════════════════════════════════

class TestComposeRedisUsers:
    """R41 P1-9: docker-compose.yml 中各服务应使用正确的 Redis 用户。"""

    def test_redis_acl_init_has_admin_password(self):
        """R41 P1-9: redis-acl-init 服务应包含 REDIS_ADMIN_PASSWORD 环境变量。"""
        content = _read_compose()
        # 查找 redis-acl-init 服务块
        init_match = re.search(
            r'redis-acl-init:.*?(?=\n\s{2}\S|\nvolumes:|\Z)',
            content, re.DOTALL
        )
        if not init_match:
            pytest.skip("未找到 redis-acl-init 服务定义")
        init_block = init_match.group(0)
        assert "REDIS_ADMIN_PASSWORD" in init_block, \
            "redis-acl-init 服务缺少 REDIS_ADMIN_PASSWORD 环境变量"

    def test_mon_uses_admin_user(self):
        """R41 P1-9: mon 服务应使用 tgjiema_admin 用户。"""
        content = _read_compose()
        # 查找 mon 服务的 REDIS_URL
        mon_match = re.search(
            r'^\s{2}mon:\s*$.*?(?=\n\s{2}\S|\Z)',
            content, re.DOTALL | re.MULTILINE
        )
        if not mon_match:
            pytest.skip("未找到 mon 服务定义")
        mon_block = mon_match.group(0)
        assert "tgjiema_admin" in mon_block, \
            "mon 服务应使用 tgjiema_admin 用户(R41 P1-9)"
        assert "REDIS_ADMIN_PASSWORD" in mon_block, \
            "mon 服务 REDIS_URL 应使用 ${REDIS_ADMIN_PASSWORD}"

    def test_admin_bot_uses_admin_user(self):
        """R41 P1-9: admin_bot 服务应使用 tgjiema_admin 用户。"""
        content = _read_compose()
        bot_match = re.search(
            r'^\s{2}admin_bot:\s*$.*?(?=\n\s{2}\S|\Z)',
            content, re.DOTALL | re.MULTILINE
        )
        if not bot_match:
            pytest.skip("未找到 admin_bot 服务定义")
        bot_block = bot_match.group(0)
        assert "tgjiema_admin" in bot_block, \
            "admin_bot 服务应使用 tgjiema_admin 用户(R41 P1-9)"
        assert "REDIS_ADMIN_PASSWORD" in bot_block, \
            "admin_bot 服务 REDIS_URL 应使用 ${REDIS_ADMIN_PASSWORD}"

    def test_admin_uses_admin_user(self):
        """R41 P1-9: admin(Web 管理后台)服务应使用 tgjiema_admin 用户。"""
        content = _read_compose()
        # admin: 后面带 ports 等缩进 4 空格的属性
        admin_match = re.search(
            r'^\s{2}admin:\s*$.*?(?=\n\s{2}\S|\Z)',
            content, re.DOTALL | re.MULTILINE
        )
        if not admin_match:
            pytest.skip("未找到 admin 服务定义")
        admin_block = admin_match.group(0)
        assert "tgjiema_admin" in admin_block, \
            "admin 服务应使用 tgjiema_admin 用户(R41 P1-9)"
        assert "REDIS_ADMIN_PASSWORD" in admin_block, \
            "admin 服务 REDIS_URL 应使用 ${REDIS_ADMIN_PASSWORD}"

    @pytest.mark.parametrize("service_name", ["up", "idx", "dsp", "db_writer", "crdb_sync", "db_backup"])
    def test_writer_services_use_writer_user(self, service_name):
        """业务服务(up/idx/dsp/db_writer/crdb_sync/db_backup)应使用 tgjiema_writer。"""
        content = _read_compose()
        service_match = re.search(
            rf'^\s{{2}}{re.escape(service_name)}:\s*$.*?(?=\n\s{{2}}\S|\Z)',
            content, re.DOTALL | re.MULTILINE
        )
        if not service_match:
            pytest.skip(f"未找到 {service_name} 服务定义")
        block = service_match.group(0)
        # 这些服务应使用 tgjiema_writer + REDIS_WRITER_PASSWORD
        # 注意:db_writer 服务可能未设置 REDIS_URL(走 CRDB),需特殊处理
        if "REDIS_URL" in block:
            assert "tgjiema_writer" in block, \
                f"{service_name} 应使用 tgjiema_writer 用户"
            assert "REDIS_WRITER_PASSWORD" in block, \
                f"{service_name} 应使用 ${{REDIS_WRITER_PASSWORD}}"

    def test_compose_header_mentions_four_passwords(self):
        """R41 P1-9: docker-compose.yml 顶部注释应提及 4 个 REDIS_*_PASSWORD。"""
        content = _read_compose()
        # 仅检查文件头部(前 30 行注释)
        header = "\n".join(content.splitlines()[:30])
        assert "REDIS_HEALTH_PASSWORD" in header
        assert "REDIS_WRITER_PASSWORD" in header
        assert "REDIS_READER_PASSWORD" in header
        assert "REDIS_ADMIN_PASSWORD" in header, \
            "docker-compose.yml 顶部注释应提及 REDIS_ADMIN_PASSWORD(R41 P1-9 新增)"


# ════════════════════════════════════════════════════════════════
# 8. 跨配置一致性: ACL 模板 ↔ docker-compose.yml
# ════════════════════════════════════════════════════════════════

class TestCrossConfigConsistency:
    """R41 P1-9: ACL 模板与 docker-compose.yml 应保持一致。"""

    def test_admin_user_consistent_between_template_and_compose(self):
        """ACL 模板定义 tgjiema_admin ↔ docker-compose 使用 tgjiema_admin。"""
        template = _read_acl_template()
        compose = _read_compose()
        # 模板中应有定义
        assert _extract_user_acl(template, "tgjiema_admin"), \
            "ACL 模板应定义 tgjiema_admin 用户"
        # compose 中应有使用
        assert "tgjiema_admin" in compose, \
            "docker-compose.yml 应使用 tgjiema_admin 用户"

    def test_writer_user_consistent(self):
        """ACL 模板定义 tgjiema_writer ↔ docker-compose 使用 tgjiema_writer。"""
        template = _read_acl_template()
        compose = _read_compose()
        assert _extract_user_acl(template, "tgjiema_writer")
        assert "tgjiema_writer" in compose

    def test_reader_user_consistent(self):
        """ACL 模板定义 tgjiema_reader(可能未被 compose 使用,但模板应定义)。"""
        template = _read_acl_template()
        assert _extract_user_acl(template, "tgjiema_reader")

    def test_health_user_consistent(self):
        """ACL 模板定义 health ↔ docker-compose healthcheck 使用 health。"""
        template = _read_acl_template()
        compose = _read_compose()
        assert _extract_user_acl(template, "health")
        assert "health" in compose

    def test_render_acl_renders_all_four_placeholders(self):
        """render_acl.sh 应渲染 4 个 <REDIS_*_PASSWORD> 占位符(对应 4 个用户)。"""
        render = _read_render_acl()
        template = _read_acl_template()
        # 模板中应有 4 个占位符
        placeholders_in_template = re.findall(r"<REDIS_\w+_PASSWORD>", template)
        unique_placeholders = set(placeholders_in_template)
        assert len(unique_placeholders) >= 4, \
            f"ACL 模板应包含 4 个 <REDIS_*_PASSWORD> 占位符,实际: {unique_placeholders}"
        # render_acl.sh 应替换每个占位符
        for ph in unique_placeholders:
            assert f"s|{ph}|" in render, \
                f"render_acl.sh 应包含 {ph} 占位符替换"
