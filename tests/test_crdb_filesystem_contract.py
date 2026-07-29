"""R79 §10.1 / P0-02 / P1-06 — 生产 CRDB 文件系统契约回归测试。

覆盖报告要求的四个场景:
    1. 非法写 ``/etc`` 必须失败(read-only rootfs 负测)
    2. 数据目录 ``/cockroach/cockroach-data`` 写入必须成功(named volume)
    3. 容器销毁重建(down + up)后 named volume 数据仍存在
    4. 容器销毁重建后 tmpfs(/cockroach/run)内容被清空

结构:
    - 纯单测(无需 docker,默认套件执行): docker diff 解析、越权路径判定、
      inspect 契约断言。
    - 集成测试(需 docker + ``TGJIEMA_CRDB_CONTRACT=1``,由 secretless CI
      专用步骤执行): 真实启动 docker-compose.yml 的 cockroachdb 服务并验证
      上述四个场景。未设置环境变量时 skip — 默认单元套件不拉取镜像、
      不启动容器;专用步骤中必须真实执行(禁止伪 PASS)。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from verify_crdb_filesystem_contract import (  # noqa: E402
    ALLOWED_WRITE_PREFIXES,
    find_diff_violations,
    parse_docker_diff,
    verify_inspect_contract,
    ContractEvidence,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
CONTRACT_ENV = "TGJIEMA_CRDB_CONTRACT"

# ════════════════════════════════════════════════════════════════
# 纯单测(无需 docker)
# ════════════════════════════════════════════════════════════════


class TestParseDockerDiff:
    """docker diff 输出解析。"""

    def test_parse_add_change_delete(self):
        output = "A /cockroach/run/server_fifo\nC /etc\nD /tmp/old\n"
        entries = parse_docker_diff(output)
        assert entries == [
            ("A", "/cockroach/run/server_fifo"),
            ("C", "/etc"),
            ("D", "/tmp/old"),
        ]

    def test_parse_empty_output(self):
        assert parse_docker_diff("") == []
        assert parse_docker_diff("\n\n") == []

    def test_parse_ignores_malformed_lines(self):
        output = "X /bogus\n  \nA /valid\n"
        assert parse_docker_diff(output) == [("A", "/valid")]


class TestFindDiffViolations:
    """允许写集合之外的 diff 条目必须被判定为违规。"""

    def test_allowed_prefixes_pass(self):
        entries = [
            ("A", "/cockroach/cockroach-data/logs/cockroach.log"),
            ("A", "/cockroach/run/server_fifo"),
            ("A", "/cockroach/certs/node.crt"),
            ("C", "/tmp/scratch"),
        ]
        assert find_diff_violations(entries) == []

    def test_exact_prefix_boundary(self):
        entries = [("A", prefix) for prefix in ALLOWED_WRITE_PREFIXES]
        assert find_diff_violations(entries) == []

    def test_rootfs_write_is_violation(self):
        entries = [("A", "/etc/crdb-pwned"), ("C", "/cockroach/cockroach.sh")]
        violations = find_diff_violations(entries)
        assert len(violations) == 2
        assert any("/etc/crdb-pwned" in v for v in violations)

    def test_sibling_prefix_not_confused(self):
        # /cockroach/runevil 不得匹配 /cockroach/run 前缀
        entries = [("A", "/cockroach/runevil/file")]
        violations = find_diff_violations(entries)
        assert len(violations) == 1

    def test_delete_is_violation_outside_allowed_set(self):
        entries = [("D", "/usr/bin/sh")]
        assert len(find_diff_violations(entries)) == 1


class TestInspectContract:
    """docker inspect 静态契约断言。"""

    def _inspect_doc(self, **overrides) -> dict:
        doc = {
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Tmpfs": {
                    "/tmp": "rw,nosuid,nodev,noexec,size=64m",
                    "/cockroach/run": "rw,nosuid,nodev,size=16m",
                    "/cockroach/certs": "rw,nosuid,nodev,noexec,size=16m",
                },
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
            },
            "Config": {"WorkingDir": "/cockroach/run"},
            "Mounts": [
                {"Type": "volume", "Destination": "/cockroach/cockroach-data"},
            ],
        }
        for key, value in overrides.items():
            doc[key] = value
        return doc

    def test_full_contract_passes(self):
        evidence = ContractEvidence(container="test")
        verify_inspect_contract(self._inspect_doc(), evidence)
        assert evidence.violations == []

    def test_writable_rootfs_fails(self):
        doc = self._inspect_doc()
        doc["HostConfig"]["ReadonlyRootfs"] = False  # R78 安全回退形态
        evidence = ContractEvidence(container="test")
        verify_inspect_contract(doc, evidence)
        assert any("readonly_rootfs" in v for v in evidence.violations)

    def test_missing_tmpfs_fails(self):
        doc = self._inspect_doc()
        del doc["HostConfig"]["Tmpfs"]["/cockroach/run"]
        evidence = ContractEvidence(container="test")
        verify_inspect_contract(doc, evidence)
        assert any("tmpfs_/cockroach/run" in v for v in evidence.violations)

    def test_missing_cap_drop_fails(self):
        doc = self._inspect_doc()
        doc["HostConfig"]["CapDrop"] = []
        evidence = ContractEvidence(container="test")
        verify_inspect_contract(doc, evidence)
        assert any("cap_drop_all" in v for v in evidence.violations)

    def test_wrong_working_dir_fails(self):
        doc = self._inspect_doc()
        doc["Config"]["WorkingDir"] = "/cockroach"
        evidence = ContractEvidence(container="test")
        verify_inspect_contract(doc, evidence)
        assert any("working_dir" in v for v in evidence.violations)


# ════════════════════════════════════════════════════════════════
# 集成测试(需 docker,由 TGJIEMA_CRDB_CONTRACT=1 启用)
# ════════════════════════════════════════════════════════════════

_docker_available = shutil.which("docker") is not None
_contract_enabled = os.environ.get(CONTRACT_ENV) == "1"


def _docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout, check=False,
    )


def _compose(*args: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return _docker("compose", "-f", str(COMPOSE_FILE), *args, timeout=timeout)


def _wait_healthy(container: str, timeout_s: int = 120) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        proc = _docker("inspect", "--format", "{{.State.Health.Status}}", container)
        if proc.stdout.strip() == "healthy":
            return True
        time.sleep(3)
    return False


@pytest.mark.skipif(
    not (_docker_available and _contract_enabled),
    reason="需要 docker 且 TGJIEMA_CRDB_CONTRACT=1",
)
class TestCrdbFilesystemContractIntegration:
    """真实容器契约测试(docker-compose.yml 的 cockroachdb 服务)。

    注意: 本类通过 down/up 重建容器验证持久化语义 — named volume
    跨重建保留,tmpfs 跨重建清空。不使用 ``docker restart``
    (restart 保留容器实例,tmpfs 不会清空,不符合契约语义)。
    """

    CONTAINER = "tgjiema-cockroachdb"

    @pytest.fixture(scope="class")
    def crdb_container(self):
        """启动 cockroachdb 并等待 healthy;类结束后清理探针文件。"""
        proc = _compose("up", "-d", "cockroachdb", timeout=300)
        assert proc.returncode == 0, f"compose up 失败: {proc.stdout}{proc.stderr}"
        assert self._wait_healthy_or_fail(), "cockroachdb 未在 120s 内 healthy"
        yield self.CONTAINER
        _docker(
            "exec", self.CONTAINER, "sh", "-c",
            "rm -f /cockroach/cockroach-data/.contract-data-marker",
            timeout=15,
        )

    def _wait_healthy_or_fail(self) -> bool:
        return _wait_healthy(self.CONTAINER, timeout_s=120)

    def _recreate(self) -> None:
        """down(不带 -v,保留 named volume)+ up — 容器实例重建。"""
        proc = _compose("down", timeout=120)
        assert proc.returncode == 0, f"compose down 失败: {proc.stderr}"
        proc = _compose("up", "-d", "cockroachdb", timeout=300)
        assert proc.returncode == 0, f"compose up 失败: {proc.stderr}"
        assert self._wait_healthy_or_fail(), "重建后 cockroachdb 未 healthy"

    def test_negative_write_etc_fails(self, crdb_container):
        """负测: read-only rootfs 上写 /etc 必须失败。"""
        proc = _docker(
            "exec", crdb_container, "sh", "-c", "touch /etc/.crdb-contract-pwn",
        )
        assert proc.returncode != 0, (
            "写 /etc 意外成功 — read_only rootfs 未生效(R78 安全回退形态)"
        )

    def test_data_volume_write_succeeds(self, crdb_container):
        """数据目录(named volume)写入必须成功。"""
        proc = _docker(
            "exec", crdb_container, "sh", "-c",
            "echo contract > /cockroach/cockroach-data/.contract-data-marker && "
            "cat /cockroach/cockroach-data/.contract-data-marker",
        )
        assert proc.returncode == 0, f"数据目录写失败: {proc.stderr}"
        assert "contract" in proc.stdout

    def test_tmpfs_write_succeeds(self, crdb_container):
        """tmpfs 工作目录写入必须成功(server_fifo 等运行时文件路径)。"""
        proc = _docker(
            "exec", crdb_container, "sh", "-c",
            "echo volatile > /cockroach/run/.contract-tmpfs-marker && "
            "cat /cockroach/run/.contract-tmpfs-marker",
        )
        assert proc.returncode == 0, f"tmpfs 工作目录写失败: {proc.stderr}"
        assert "volatile" in proc.stdout

    def test_data_survives_recreate_but_tmpfs_cleared(self, crdb_container):
        """重建后 named volume 数据存在、tmpfs 内容清空。"""
        # 先写入两类 marker
        proc = _docker(
            "exec", crdb_container, "sh", "-c",
            "echo persist > /cockroach/cockroach-data/.contract-data-marker && "
            "echo volatile > /cockroach/run/.contract-tmpfs-marker",
        )
        assert proc.returncode == 0
        # 重建容器(down 不带 -v → named volume 保留;tmpfs 随实例销毁)
        self._recreate()
        # named volume marker 必须存在
        proc = _docker(
            "exec", crdb_container, "sh", "-c",
            "cat /cockroach/cockroach-data/.contract-data-marker",
        )
        assert proc.returncode == 0 and "persist" in proc.stdout, (
            "重建后 named volume 数据丢失 — 持久化契约违反"
        )
        # tmpfs marker 必须消失
        proc = _docker(
            "exec", crdb_container, "sh", "-c",
            "test -f /cockroach/run/.contract-tmpfs-marker",
        )
        assert proc.returncode != 0, (
            "重建后 tmpfs 内容仍存在 — tmpfs 易失性契约违反"
        )

    def test_docker_diff_within_allowed_write_set(self, crdb_container):
        """docker diff 不得出现允许写集合之外的条目。"""
        proc = _docker("diff", crdb_container)
        assert proc.returncode == 0
        entries = parse_docker_diff(proc.stdout)
        violations = find_diff_violations(entries)
        assert violations == [], (
            f"docker diff 发现允许写集合之外的变更: {violations}"
        )

    def test_verify_script_passes(self, crdb_container, tmp_path):
        """scripts/verify_crdb_filesystem_contract.py 全量契约必须 PASS。"""
        out = tmp_path / "contract.json"
        proc = subprocess.run(
            [sys.executable,
             str(REPO_ROOT / "scripts" / "verify_crdb_filesystem_contract.py"),
             "--container", crdb_container, "--output", str(out)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        assert proc.returncode == 0, (
            f"verify_crdb_filesystem_contract 失败(rc={proc.returncode}): "
            f"{proc.stdout}{proc.stderr}"
        )
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["verdict"] == "PASS"
        assert doc["error_code"] is None
