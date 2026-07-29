"""R84 integration tests for the immutable RC tag creation guard.

The tests use real repositories, commits, fetches, tags, an SSH signing key, and a
bare remote. Only narrow failure injection uses git wrapper executables; the
repository operations themselves are never mocked.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "create_rc_tag.sh"
BASH = shutil.which("bash")
GIT = shutil.which("git")
SSH_KEYGEN = shutil.which("ssh-keygen")
pytestmark = pytest.mark.skipif(
    not all((BASH, GIT, SSH_KEYGEN)),
    reason="bash, git, and ssh-keygen are required for real Git integration tests",
)


@dataclass
class RepoHarness:
    remote: Path
    repo: Path
    key: Path
    allowed_signers: Path

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [GIT, *args],
            cwd=self.repo,
            text=True,
            capture_output=True,
            check=check,
        )

    def run_guard(
        self,
        tag: str = "rc-v9.9.9",
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        return subprocess.run(
            [BASH, str(SCRIPT), tag],
            cwd=self.repo,
            text=True,
            capture_output=True,
            env=merged_env,
            timeout=30,
            check=False,
        )


@pytest.fixture
def repo(tmp_path: Path) -> RepoHarness:
    remote = tmp_path / "origin.git"
    work = tmp_path / "work"
    key = tmp_path / "rc_signing_key"
    allowed = tmp_path / "allowed_signers"

    subprocess.run([GIT, "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run([GIT, "clone", str(remote), str(work)], check=True, capture_output=True)
    subprocess.run(
        [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
    )
    public_key = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
    allowed.write_text(f"rc-test {public_key}\n", encoding="utf-8")

    harness = RepoHarness(remote=remote, repo=work, key=key, allowed_signers=allowed)
    for name, value in (
        ("user.name", "RC Test"),
        ("user.email", "rc-test@example.invalid"),
        ("gpg.format", "ssh"),
        ("user.signingkey", str(key)),
        ("gpg.ssh.allowedSignersFile", str(allowed)),
    ):
        harness.git("config", name, value)

    harness.git("switch", "-c", "master")
    (work / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    harness.git("add", "tracked.txt")
    harness.git("commit", "-m", "test: baseline")
    harness.git("push", "-u", "origin", "master")
    return harness


def _commit(repo: RepoHarness, text: str) -> str:
    (repo.repo / "tracked.txt").write_text(text, encoding="utf-8")
    repo.git("add", "tracked.txt")
    repo.git("commit", "-m", f"test: {text.strip()}")
    return repo.git("rev-parse", "HEAD").stdout.strip()


def _wrapper_dir(tmp_path: Path, body: str) -> Path:
    wrapper = tmp_path / "bin"
    wrapper.mkdir()
    git_wrapper = wrapper / "git"
    git_wrapper.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    git_wrapper.chmod(git_wrapper.stat().st_mode | stat.S_IXUSR)
    return wrapper


def _path_with(wrapper: Path) -> dict[str, str]:
    return {"PATH": str(wrapper) + os.pathsep + os.environ.get("PATH", "")}


def test_rejects_non_master_branch(repo: RepoHarness) -> None:
    repo.git("switch", "-c", "feature")
    result = repo.run_guard()
    assert result.returncode == 31
    assert "NOT_ON_MASTER: feature" in result.stderr


def test_rejects_dirty_worktree(repo: RepoHarness) -> None:
    (repo.repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    result = repo.run_guard()
    assert result.returncode == 33
    assert "WORKTREE_NOT_CLEAN" in result.stderr


def test_rejects_head_behind_origin_master(repo: RepoHarness, tmp_path: Path) -> None:
    other = tmp_path / "other"
    subprocess.run([GIT, "clone", str(repo.remote), str(other)], check=True, capture_output=True)
    subprocess.run([GIT, "config", "user.name", "Other"], cwd=other, check=True)
    subprocess.run([GIT, "config", "user.email", "other@example.invalid"], cwd=other, check=True)
    (other / "tracked.txt").write_text("remote ahead\n", encoding="utf-8")
    subprocess.run([GIT, "add", "tracked.txt"], cwd=other, check=True)
    subprocess.run([GIT, "commit", "-m", "test: remote ahead"], cwd=other, check=True)
    subprocess.run([GIT, "push", "origin", "master"], cwd=other, check=True)

    result = repo.run_guard()
    assert result.returncode == 32
    assert "HEAD_NOT_ORIGIN_MASTER" in result.stderr


def test_rejects_head_ahead_origin_master(repo: RepoHarness) -> None:
    _commit(repo, "local ahead\n")
    result = repo.run_guard()
    assert result.returncode == 32
    assert "HEAD_NOT_ORIGIN_MASTER" in result.stderr


def test_rejects_local_same_name_tag(repo: RepoHarness) -> None:
    repo.git("tag", "rc-v9.9.9")
    result = repo.run_guard()
    assert result.returncode == 34
    assert "LOCAL_TAG_ALREADY_EXISTS" in result.stderr


def test_rejects_remote_same_name_tag(repo: RepoHarness) -> None:
    repo.git("tag", "rc-v9.9.9")
    repo.git("push", "origin", "refs/tags/rc-v9.9.9")
    repo.git("tag", "-d", "rc-v9.9.9")
    result = repo.run_guard()
    assert result.returncode == 35
    assert "REMOTE_TAG_ALREADY_EXISTS" in result.stderr


@pytest.mark.parametrize("tag", ["v1.0.0", "rc-v1.0", "rc-v1.0.0-rc1", "rc-vx.1.2"])
def test_rejects_invalid_tag_name(repo: RepoHarness, tag: str) -> None:
    result = repo.run_guard(tag)
    assert result.returncode == 30
    assert "INVALID_RC_TAG_NAME" in result.stderr


def test_rejects_missing_signing_key(repo: RepoHarness) -> None:
    repo.git("config", "--unset", "user.signingkey")
    result = repo.run_guard()
    assert result.returncode != 0
    assert repo.git(
        "show-ref",
        "--verify",
        "--quiet",
        "refs/tags/rc-v9.9.9",
        check=False,
    ).returncode != 0


def test_rejects_verify_tag_failure(repo: RepoHarness, tmp_path: Path) -> None:
    real_git = Path(GIT).as_posix()
    body = f'if [ "$1" = "verify-tag" ]; then exit 97; fi\nexec "{real_git}" "$@"'
    wrapper = _wrapper_dir(tmp_path, body)
    result = repo.run_guard(env=_path_with(wrapper))
    assert result.returncode == 97


def test_rejects_lightweight_tag_injected_after_sign(repo: RepoHarness, tmp_path: Path) -> None:
    real_git = Path(GIT).as_posix()
    body = (
        f'if [ "$1" = "tag" ] && [ "$2" = "-s" ]; then exec "{real_git}" tag "$4" "$5"; fi\n'
        f'if [ "$1" = "verify-tag" ]; then exit 0; fi\nexec "{real_git}" "$@"'
    )
    wrapper = _wrapper_dir(tmp_path, body)
    result = repo.run_guard(env=_path_with(wrapper))
    assert result.returncode == 37
    assert "TAG_IS_NOT_ANNOTATED" in result.stderr


def test_rejects_peeled_sha_mismatch_injected_after_sign(repo: RepoHarness, tmp_path: Path) -> None:
    real_git = Path(GIT).as_posix()
    body = (
        f'if [ "$1" = "rev-parse" ] && [ "$2" = "rc-v9.9.9^{{}}" ]; then '
        f'echo 0000000000000000000000000000000000000000; exit 0; fi\n'
        f'exec "{real_git}" "$@"'
    )
    wrapper = _wrapper_dir(tmp_path, body)
    result = repo.run_guard(env=_path_with(wrapper))
    assert result.returncode == 36
    assert "TAG_PEELED_SHA_MISMATCH" in result.stderr


def test_clean_exact_master_creates_verified_annotated_tag(repo: RepoHarness) -> None:
    head = repo.git("rev-parse", "HEAD").stdout.strip()
    result = repo.run_guard()
    assert result.returncode == 0, result.stderr
    tag_object = repo.git("rev-parse", "rc-v9.9.9").stdout.strip()
    peeled = repo.git("rev-parse", "rc-v9.9.9^{}").stdout.strip()
    assert peeled == head
    assert tag_object != peeled
    assert repo.git("cat-file", "-t", "rc-v9.9.9").stdout.strip() == "tag"
    assert repo.git("verify-tag", "rc-v9.9.9").returncode == 0
    assert f"tag_object={tag_object}" in result.stdout
    assert f"source_sha={head}" in result.stdout
