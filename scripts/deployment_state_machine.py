#!/usr/bin/env python3
"""R76 O10: Production Deployment State Machine.

整改背景(R76 终审报告 O10 / P0-08 / P0-09):
    R75 _promote-verified-rc.yml 的 Step o(创建 signed production tag)在 Step p
    (实际部署)之前执行,导致:
        - 部署失败时 production-v* tag 已被推送到仓库,产生"半完成状态"
        - 后续重试必须先手动删除 tag,违反"production tag 不可变"原则
        - 监控/告警/下游系统可能引用已存在但未真正部署的 tag

    本状态机将部署生命周期建模为显式状态转换:

        init ──▶ deploying ──▶ deployed ──▶ verified
                  │                │            │
                  └────────────────┴────────────┴──▶ failed (终态)

    仅当 ``verified`` 终态达成时,调用方才允许创建 production-v* tag。

状态定义:
    - ``init``        : Deployment record 已创建(status=queued)
    - ``deploying``   : Deploy hook 已触发,status=in_progress
    - ``deployed``    : /health 探针通过且 image_repo_digest 匹配,status=queued(等待业务探针)
    - ``verified``    : 业务探针(/api/v1/status)通过,status=success(终态,可创建 tag)
    - ``failed``      : 任一阶段失败,status=failure(终态,禁止创建 tag)

状态持久化:
    - 状态机将当前状态/transition history/部署 ID 写入 JSON 文件
      (默认 ``./deployment-state.json``),供:
        - CI 失败时定位失败阶段
        - 审计回放(transition timeline)
        - 防止重试时跳过阶段(必须从 init 重新开始)
    - 文件可被 ``--state-file`` 覆盖;CI 中每个 promotion run 使用独立路径

CLI:
    # 单命令模式(推荐 — CI 使用):
    python scripts/deployment_state_machine.py run \\
        --production-tag production-v1.0.0 \\
        --source-sha <40-hex> \\
        --image-repo-digest ghcr.io/owner/repo@sha256:<64-hex> \\
        --runtime-config-digest sha256:<64-hex> \\
        --deploy-hook-url https://deploy.example.com/hook \\
        --deploy-probe-url https://app.example.com \\
        --state-file ./deployment-state.json

    # 子命令模式(调试 / 阶段化执行):
    python scripts/deployment_state_machine.py init ...
    python scripts/deployment_state_machine.py deploy ...
    python scripts/deployment_state_machine.py verify ...
    python scripts/deployment_state_machine.py status ...
    python scripts/deployment_state_machine.py fail --reason "..."

退出码(fail-closed):
    0 — 终态为 verified(允许创建 production tag)
    1 — 终态为 failed,或任一阶段校验失败(禁止创建 production tag)
    2 — 参数错误 / 状态文件损坏 / 不可恢复的基础设施故障
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ════════════════════════════════════════════════════════════════
# 常量
# ════════════════════════════════════════════════════════════════

SCHEMA_VERSION = "deployment-state/v1"
SECRETLESS_CANDIDATE_SCHEMA = "secretless-candidate-manifest/v1"
SECRETLESS_PHASE_ENVELOPE_SCHEMA = "r73-sec5.15"
SECRETLESS_ROLLBACK_SCHEMA = "secretless-switch-contract/v1"
SECRETLESS_ROLLBACK_PHASE = "secretless_actual_rollback"

# 状态枚举(顺序即生命周期)
STATE_INIT = "init"
STATE_DEPLOYING = "deploying"
STATE_DEPLOYED = "deployed"
STATE_VERIFIED = "verified"
STATE_FAILED = "failed"

# 合法状态转换(有向图)
VALID_TRANSITIONS: dict[str, tuple[str, ...]] = {
    STATE_INIT: (STATE_DEPLOYING, STATE_FAILED),
    STATE_DEPLOYING: (STATE_DEPLOYED, STATE_FAILED),
    STATE_DEPLOYED: (STATE_VERIFIED, STATE_FAILED),
    STATE_VERIFIED: (),  # 终态
    STATE_FAILED: (),  # 终态
}

# 终态(不可再转换)
TERMINAL_STATES: frozenset[str] = frozenset({STATE_VERIFIED, STATE_FAILED})

# 探针默认参数
DEFAULT_PROBE_MAX_ATTEMPTS = 30
DEFAULT_PROBE_INTERVAL_SECONDS = 10
DEFAULT_HTTP_TIMEOUT_SECONDS = 10
DEFAULT_HOOK_TIMEOUT_SECONDS = 30

# 成功的 HTTP 状态码(deploy hook / probe)
SUCCESS_HTTP_CODES: frozenset[int] = frozenset({200, 201, 202})


# ════════════════════════════════════════════════════════════════
# 数据类
# ════════════════════════════════════════════════════════════════


@dataclass
class Transition:
    """状态转换记录(审计用)。"""

    from_state: str
    to_state: str
    timestamp: str  # RFC3339
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeploymentState:
    """部署状态机持久化结构。"""

    schema_version: str = SCHEMA_VERSION
    production_tag: str = ""
    source_sha: str = ""
    image_repo_digest: str = ""
    runtime_config_digest: str = ""
    candidate_manifest_sha256: str = ""
    source_database_identity: str = ""
    target_database_identity: str = ""
    rollback_source_identity: str = ""
    identity_restored: bool = False
    secretless_mode: bool = False
    deploy_hook_url: str = ""
    deploy_probe_url: str = ""
    current_state: str = STATE_INIT
    deployment_id: str = ""  # GitHub Deployment ID(init 阶段填充)
    created_at: str = ""
    updated_at: str = ""
    transitions: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DeploymentState:
        return cls(
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            production_tag=data.get("production_tag", ""),
            source_sha=data.get("source_sha", ""),
            image_repo_digest=data.get("image_repo_digest", ""),
            runtime_config_digest=data.get("runtime_config_digest", ""),
            candidate_manifest_sha256=data.get("candidate_manifest_sha256", ""),
            source_database_identity=data.get("source_database_identity", ""),
            target_database_identity=data.get("target_database_identity", ""),
            rollback_source_identity=data.get("rollback_source_identity", ""),
            identity_restored=data.get("identity_restored", False) is True,
            secretless_mode=data.get("secretless_mode", False) is True,
            deploy_hook_url=data.get("deploy_hook_url", ""),
            deploy_probe_url=data.get("deploy_probe_url", ""),
            current_state=data.get("current_state", STATE_INIT),
            deployment_id=data.get("deployment_id", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            transitions=list(data.get("transitions", [])),
            failure_reason=data.get("failure_reason", ""),
        )


# ════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════


def _now_iso() -> str:
    """当前 UTC 时间 RFC3339 字符串。"""
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return "sha256:" + digest


def _load_secretless_identity(
    candidate_manifest: Path,
    rollback_evidence: Path,
) -> dict[str, str]:
    """Load Step 14 identity and strictly unwrap the Step 13 phase envelope."""
    try:
        candidate = json.loads(candidate_manifest.read_text(encoding="utf-8"))
        rollback_envelope = json.loads(rollback_evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _fail_closed(2, f"secretless identity evidence invalid: {exc}")
    if not isinstance(candidate, dict) or not isinstance(rollback_envelope, dict):
        _fail_closed(2, "secretless identity evidence root must be an object")
    if candidate.get("schema_version") != SECRETLESS_CANDIDATE_SCHEMA:
        _fail_closed(2, "secretless candidate manifest schema mismatch")
    if candidate.get("kind") != "secretless-candidate-manifest":
        _fail_closed(2, "secretless candidate manifest kind mismatch")

    if rollback_envelope.get("schema_version") != SECRETLESS_PHASE_ENVELOPE_SCHEMA:
        _fail_closed(2, "secretless rollback phase envelope schema mismatch")
    if rollback_envelope.get("overall_passed") is not True:
        _fail_closed(2, "secretless rollback phase envelope did not pass")
    phases = rollback_envelope.get("phases")
    if not isinstance(phases, list) or len(phases) != 1:
        _fail_closed(2, "secretless rollback phase envelope must contain exactly one phase")
    phase = phases[0]
    if not isinstance(phase, dict):
        _fail_closed(2, "secretless rollback phase must be an object")
    if phase.get("phase") != SECRETLESS_ROLLBACK_PHASE or phase.get("status") != "pass":
        _fail_closed(2, "secretless rollback phase identity/status mismatch")
    phase_returncode = phase.get("returncode")
    if type(phase_returncode) is not int or phase_returncode != 0:
        _fail_closed(2, "secretless rollback phase returncode must be integer zero")
    rollback = phase.get("evidence")
    if not isinstance(rollback, dict):
        _fail_closed(2, "secretless rollback phase evidence must be an object")
    command = rollback.get("command")
    nested_returncode = command.get("returncode") if isinstance(command, dict) else None
    if type(nested_returncode) is not int or nested_returncode != 0:
        _fail_closed(2, "secretless rollback nested command returncode must be integer zero")
    if rollback.get("schema_version") != SECRETLESS_ROLLBACK_SCHEMA:
        _fail_closed(2, "secretless rollback evidence schema mismatch")
    if rollback.get("action") != "rollback":
        _fail_closed(2, "secretless rollback action mismatch")

    if candidate.get("source_sha") != rollback.get("head_sha"):
        _fail_closed(2, "candidate/rollback current SHA mismatch")
    if candidate.get("restore_operation_id") != rollback.get("operation_id"):
        _fail_closed(2, "candidate/rollback operation identity mismatch")
    source_identity = str(candidate.get("source_database_identity", ""))
    target_identity = str(candidate.get("target_database_identity", ""))
    if not source_identity or not target_identity or source_identity == target_identity:
        _fail_closed(2, "candidate source/target database identity invalid")
    if (
        rollback.get("source_identity") != source_identity
        or rollback.get("target_identity") != target_identity
    ):
        _fail_closed(2, "candidate/rollback database identities mismatch")
    active_before = rollback.get("active_before")
    active_after = rollback.get("active_after")
    if not isinstance(active_before, dict) or not isinstance(active_after, dict):
        _fail_closed(2, "rollback active identity evidence must be objects")
    if (
        rollback.get("status") != "success"
        or active_before.get("active_identity") != target_identity
        or active_after.get("active_identity") != source_identity
        or not isinstance(rollback.get("source_business_probe"), dict)
        or rollback["source_business_probe"].get("status") != "pass"
    ):
        _fail_closed(2, "rollback evidence does not prove source identity restoration")
    return {
        "source_sha": str(candidate["source_sha"]),
        "image_digest": str(candidate["image_digest"]),
        "runtime_config_digest": str(candidate["runtime_config_digest"]),
        "source_database_identity": source_identity,
        "target_database_identity": target_identity,
        "rollback_source_identity": str(active_after["active_identity"]),
        "candidate_manifest_sha256": _sha256_file(candidate_manifest),
    }


def _http_request(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
    headers: dict[str, str] | None = None,
) -> tuple[int, str, dict[str, str]]:
    """同步 HTTP 请求(仅依赖 stdlib urllib,避免 requests 依赖)。

    Returns:
        ``(http_code, body_text, response_headers_dict)``

    Raises:
        urllib.error.URLError: 网络错误(由调用方处理并标记 failed)
    """
    data: bytes | None = None
    req_headers: dict[str, str] = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body, dict(resp.headers.items())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except OSError as read_err:
            # R76 10.M: 删除 except Exception: pass — 读取错误响应体失败不阻断
            # 主流程(HTTPError 已捕获),但记录到 stderr 供审计
            print(
                f"[deployment_state_machine] warning: "
                f"failed to read HTTPError body: {read_err}",
                file=sys.stderr,
            )
        return e.code, body, dict(e.headers.items()) if e.headers else {}


def _gh_api(
    method: str,
    path: str,
    *,
    fields: dict[str, Any] | None = None,
    raw_fields: dict[str, str] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """调用 GitHub API(通过 gh CLI,避免重写鉴权)。

    Returns:
        解析后的 JSON dict。失败时 raise RuntimeError。
    """
    cmd: list[str] = ["gh", "api", path]
    for k, v in (fields or {}).items():
        cmd.extend(["-f", f"{k}={v}"])
    for k, v in (raw_fields or {}).items():
        cmd.extend(["--raw-field", f"{k}={v}"])
    env = os.environ.copy()
    if token:
        env["GH_TOKEN"] = token
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh api {path} failed (exit={result.returncode}): {result.stderr.strip()}"
        )
    out = result.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"gh api {path} returned non-JSON: {e}") from e


# ════════════════════════════════════════════════════════════════
# 状态机
# ════════════════════════════════════════════════════════════════


class DeploymentStateMachine:
    """Production Deployment 状态机。

    生命周期:
        init → deploying → deployed → verified
                                    ↘ failed (任一阶段失败)

    持久化:
        - ``state_file`` 在每次状态转换后同步写入
        - 文件损坏 / schema 不匹配 → fail-closed(退出码 2)
        - 终态(verified/failed)后不允许再转换
    """

    def __init__(self, state_file: Path, initial: DeploymentState | None = None):
        self.state_file = state_file
        if initial is not None:
            self.state = initial
        else:
            self.state = self._load_or_init()

    # ── 持久化 ──────────────────────────────────────────────────

    def _load_or_init(self) -> DeploymentState:
        """从 state_file 加载或新建初始状态。

        fail-closed:
            - 文件存在但 JSON 损坏 → 退出码 2
            - schema_version 不匹配 → 退出码 2
            - 文件不存在 → 返回初始空状态(由 init 命令填充)
        """
        if not self.state_file.exists():
            return DeploymentState(created_at=_now_iso(), updated_at=_now_iso())
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _fail_closed(2, f"state file {self.state_file} corrupted: {e}")
        if data.get("schema_version") != SCHEMA_VERSION:
            _fail_closed(
                2,
                f"state file schema_version mismatch: "
                f"expected {SCHEMA_VERSION!r}, got {data.get('schema_version')!r}",
            )
        return DeploymentState.from_dict(data)

    def _persist(self) -> None:
        """同步写入状态文件(fail-closed)。"""
        self.state.updated_at = _now_iso()
        try:
            self.state_file.write_text(
                json.dumps(self.state.to_dict(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError as e:
            _fail_closed(2, f"failed to persist state file {self.state_file}: {e}")

    # ── 状态转换 ────────────────────────────────────────────────

    def _transition(
        self,
        to_state: str,
        *,
        reason: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """执行状态转换(fail-closed — 非法转换立即退出)。

        Args:
            to_state: 目标状态(必须在 VALID_TRANSITIONS[current_state] 中)
            reason: 转换原因(写入 transition history)
            metadata: 附加元数据(写入 transition history)
        """
        current = self.state.current_state
        if to_state == current:
            return  # 幂等
        if current in TERMINAL_STATES:
            _fail_closed(
                1,
                f"cannot transition from terminal state {current!r} to {to_state!r}",
            )
        allowed = VALID_TRANSITIONS.get(current, ())
        if to_state not in allowed:
            _fail_closed(
                1,
                f"illegal transition {current!r} -> {to_state!r} "
                f"(allowed: {allowed})",
            )
        transition = Transition(
            from_state=current,
            to_state=to_state,
            timestamp=_now_iso(),
            reason=reason,
            metadata=metadata or {},
        )
        self.state.current_state = to_state
        self.state.transitions.append(asdict(transition))
        if to_state == STATE_FAILED and reason:
            self.state.failure_reason = reason
        self._persist()

    # ── 阶段 1: init ────────────────────────────────────────────

    def init_deployment(
        self,
        *,
        production_tag: str,
        source_sha: str,
        image_repo_digest: str,
        runtime_config_digest: str,
        deploy_hook_url: str,
        deploy_probe_url: str,
        secretless_identity: dict[str, str] | None = None,
    ) -> str:
        """init 阶段:创建 GitHub Deployment record(status=queued)。

        Args:
            production_tag: 待创建的 production-v* tag
            source_sha: RC source commit SHA(40-hex)
            image_repo_digest: ghcr.io/<repo>@sha256:<64-hex>
            runtime_config_digest: sha256:<64-hex>
            deploy_hook_url: 部署 webhook URL(POST 触发实际部署)
            deploy_probe_url: 部署后探针 base URL(GET /health, /api/v1/status)

        Returns:
            GitHub Deployment ID(字符串)。

        Fail-closed:
            - 任一参数为空 → 退出 1
            - gh api 创建失败 → 退出 1
        """
        if self.state.current_state != STATE_INIT:
            _fail_closed(
                1,
                f"init_deployment requires current_state={STATE_INIT!r}, "
                f"got {self.state.current_state!r}",
            )
        # 参数校验(fail-closed)
        for name, val in [
            ("production_tag", production_tag),
            ("source_sha", source_sha),
            ("image_repo_digest", image_repo_digest),
            ("runtime_config_digest", runtime_config_digest),
            ("deploy_hook_url", deploy_hook_url),
            ("deploy_probe_url", deploy_probe_url),
        ]:
            if not val:
                _fail_closed(1, f"init_deployment: {name} must not be empty")
        if len(source_sha) != 40 or not all(
            c in "0123456789abcdef" for c in source_sha.lower()
        ):
            _fail_closed(1, f"init_deployment: source_sha must be 40-hex, got {source_sha!r}")

        # 写入状态(尚未持久化,等 deployment_id 拿到后一起写)
        self.state.production_tag = production_tag
        self.state.source_sha = source_sha
        self.state.image_repo_digest = image_repo_digest
        self.state.runtime_config_digest = runtime_config_digest
        self.state.deploy_hook_url = deploy_hook_url
        self.state.deploy_probe_url = deploy_probe_url
        if secretless_identity:
            self.state.secretless_mode = True
            self.state.candidate_manifest_sha256 = secretless_identity["candidate_manifest_sha256"]
            self.state.source_database_identity = secretless_identity["source_database_identity"]
            self.state.target_database_identity = secretless_identity["target_database_identity"]
            self.state.rollback_source_identity = secretless_identity["rollback_source_identity"]
            self.state.identity_restored = (
                self.state.rollback_source_identity == self.state.source_database_identity
            )

        # Secretless CI 不创建真实 GitHub Deployment；使用 run-local protocol identity。
        if self.state.secretless_mode:
            deployment_id = (
                "secretless-" + self.state.candidate_manifest_sha256.removeprefix("sha256:")[:20]
            )
            self.state.deployment_id = deployment_id
            self._persist()
            print(f"  [init] Secretless deployment record created: id={deployment_id}")
            return deployment_id

        # 调用 GitHub API 创建 deployment record
        deploy_payload = {
            "production_tag": production_tag,
            "source_sha": source_sha,
            "image_repo_digest": image_repo_digest,
            "runtime_config_digest": runtime_config_digest,
            "promoted_at": _now_iso(),
        }
        try:
            resp = _gh_api(
                "POST",
                "repos/maxiuquan/tgjiema/deployments",
                fields={
                    "ref": production_tag,
                    "environment": "production",
                    "description": f"Promote {production_tag} to production (R76 O10)",
                    "required_contexts": "[]",
                    "auto_merge": "false",
                    "payload": json.dumps(deploy_payload),
                },
            )
        except RuntimeError as e:
            self._transition(
                STATE_FAILED,
                reason=f"init: gh api create deployment failed: {e}",
            )
            _fail_closed(1, str(e))

        deployment_id = str(resp.get("id", ""))
        if not deployment_id or deployment_id == "None":
            self._transition(
                STATE_FAILED,
                reason=f"init: gh api returned no deployment id (resp={resp})",
            )
            _fail_closed(1, f"init: gh api returned no deployment id (resp={resp})")

        self.state.deployment_id = deployment_id
        self._persist()
        print(f"  [init] GitHub Deployment created: id={deployment_id}")
        return deployment_id

    # ── 阶段 2: deploying ───────────────────────────────────────

    def trigger_deploy(self) -> None:
        """deploying 阶段:设置 status=in_progress,触发 deploy hook。

        Fail-closed:
            - hook URL 缺失 → 退出 1
            - hook 调用失败(网络/非 2xx)→ 转换到 failed
        """
        if self.state.current_state != STATE_INIT:
            _fail_closed(
                1,
                f"trigger_deploy requires current_state={STATE_INIT!r}, "
                f"got {self.state.current_state!r}",
            )
        deployment_id = self.state.deployment_id
        if not deployment_id:
            self._transition(STATE_FAILED, reason="deploy: missing deployment_id")
            _fail_closed(1, "deploy: missing deployment_id")

        # 设置 GitHub deployment status=in_progress；Secretless 使用本地协议状态。
        if not self.state.secretless_mode:
            try:
                _gh_api(
                    "POST",
                    f"repos/maxiuquan/tgjiema/deployments/{deployment_id}/statuses",
                    fields={
                        "state": "in_progress",
                        "description": "Deployment in progress (R76 O10)",
                    },
                )
            except RuntimeError as e:
                self._transition(
                    STATE_FAILED,
                    reason=f"deploy: gh api set in_progress failed: {e}",
                )
                _fail_closed(1, str(e))

        # 状态转换 init → deploying
        self._transition(STATE_DEPLOYING, reason="deploy hook triggered")
        print(f"  [deploying] Deployment {deployment_id} status=in_progress")

        # 触发 deploy hook
        hook_url = self.state.deploy_hook_url
        if not hook_url:
            self._mark_failed("deploy: DEPLOY_HOOK_URL not configured")
            _fail_closed(1, "deploy: DEPLOY_HOOK_URL not configured")

        payload = {
            "production_tag": self.state.production_tag,
            "source_sha": self.state.source_sha,
            "image_repo_digest": self.state.image_repo_digest,
            "runtime_config_digest": self.state.runtime_config_digest,
            "deployment_id": deployment_id,
        }
        try:
            http_code, body, _ = _http_request(
                "POST",
                hook_url,
                payload=payload,
                timeout=DEFAULT_HOOK_TIMEOUT_SECONDS,
            )
        except (urllib.error.URLError, OSError) as e:
            self._mark_failed(f"deploy: hook request failed (network): {e}")
            _fail_closed(1, f"deploy: hook request failed (network): {e}")

        if http_code not in SUCCESS_HTTP_CODES:
            self._mark_failed(
                f"deploy: hook returned HTTP {http_code} (body={body[:200]})"
            )
            _fail_closed(1, f"deploy: hook returned HTTP {http_code}")

        print(f"  [deploying] Deploy hook accepted: HTTP {http_code}")

    def _mark_failed(self, reason: str) -> None:
        """标记部署失败(GitHub status=failure + 状态机 -> failed)。"""
        deployment_id = self.state.deployment_id
        if deployment_id and not self.state.secretless_mode:
            try:
                _gh_api(
                    "POST",
                    f"repos/maxiuquan/tgjiema/deployments/{deployment_id}/statuses",
                    fields={
                        "state": "failure",
                        "description": f"Deployment failed (R76 O10): {reason[:200]}",
                    },
                )
            except RuntimeError as exc:
                reason = f"{reason}; github_status_failure={exc}"
        self._transition(STATE_FAILED, reason=reason)

    # ── 阶段 3: deployed (探针 + RepoDigest 校验) ──────────────

    def verify_platform_identity(
        self,
        *,
        container_name: str = "",
        docker_host: str = "",
    ) -> tuple[bool, str, str]:
        """R76 P1-06: 平台独立身份回读 — 从容器 runtime 取得实际 RepoDigest。

        整改背景:
            原 ``wait_for_deployed`` 从应用 ``/health`` 响应读取
            ``image_repo_digest`` 和 ``runtime_config_digest``,属于应用自报。
            被部署应用可回显期望值,无法证明实际运行的镜像身份。

        本方法从容器 runtime(docker inspect)独立读取实际 RepoDigest,
        与 expected ``image_repo_digest`` 比对,与应用探针分离验证。

        Args:
            container_name: 目标容器名(若空则从 deploy_probe_url 推断)
            docker_host: Docker daemon URL(默认从 DOCKER_HOST 环境变量)

        Returns:
            (ok, actual_repo_digest, error_msg)
            - ok=True: 平台身份验证通过
            - ok=False: 平台身份不匹配或无法读取(fail-closed)
        """
        expected_digest = self.state.image_repo_digest
        if not expected_digest:
            return False, "", "expected image_repo_digest is empty"

        # 推断容器名:优先使用 container_name,否则从 production_tag 推断
        target = container_name or self.state.production_tag.replace(
            "production-v", "tgjiema-"
        )

        # 调用 docker inspect 读取 .RepoDigests
        try:
            cmd = [
                "docker", "inspect",
                "--format", "{{json .RepoDigests}}",
                target,
            ]
            if docker_host:
                cmd = ["docker", "--host", docker_host] + cmd[1:]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0:
                return False, "", (
                    f"docker inspect failed (rc={result.returncode}): "
                    f"{result.stderr.strip()[:200]}"
                )
            repo_digests = json.loads(result.stdout.strip())
            if not repo_digests:
                return False, "", "docker inspect returned empty RepoDigests"
            # 查找与 expected_digest 匹配的 RepoDigest(大小写不敏感)
            expected_lower = expected_digest.lower()
            for rd in repo_digests:
                if rd.lower() == expected_lower:
                    return True, rd, ""
            # 未找到匹配,返回第一个供诊断
            actual = repo_digests[0]
            return False, actual, (
                f"platform identity mismatch: expected={expected_digest}, "
                f"actual={actual} — 容器实际运行的镜像与预期不符 (R76 P1-06)"
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError,
                json.JSONDecodeError) as exc:
            return False, "", f"docker inspect error: {exc}"

    def wait_for_deployed(
        self,
        *,
        max_attempts: int = DEFAULT_PROBE_MAX_ATTEMPTS,
        interval_seconds: int = DEFAULT_PROBE_INTERVAL_SECONDS,
    ) -> None:
        """deployed 阶段:轮询 /health(liveness) + 平台独立身份回读。

        R76 P1-06 整改:
            - /health 仅用于 liveness 检查(HTTP 200 + healthy=true)
            - image_repo_digest 从 docker inspect 独立读取(非应用自报)
            - runtime_config_digest 仍从 /health 读取(配置非镜像身份)

        Fail-closed:
            - 探针 URL 缺失 → 转换 failed
            - 探针超时(max_attempts 次后仍不健康)→ 转换 failed
            - 平台身份不匹配 → 转换 failed (R76 P1-06)
            - runtime_config_digest 不匹配 → 转换 failed
        """
        if self.state.current_state != STATE_DEPLOYING:
            _fail_closed(
                1,
                f"wait_for_deployed requires current_state={STATE_DEPLOYING!r}, "
                f"got {self.state.current_state!r}",
            )
        probe_url = self.state.deploy_probe_url
        if not probe_url:
            self._mark_failed("deployed: DEPLOY_PROBE_URL not configured")
            _fail_closed(1, "deployed: DEPLOY_PROBE_URL not configured")

        expected_digest = self.state.image_repo_digest
        expected_config = self.state.runtime_config_digest
        health_url = f"{probe_url.rstrip('/')}/health"

        for attempt in range(1, max_attempts + 1):
            try:
                http_code, body, _ = _http_request(
                    "GET",
                    health_url,
                    timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
                )
            except (urllib.error.URLError, OSError):
                print(f"  [deployed] probe attempt {attempt}/{max_attempts}: network error, retrying")
                time.sleep(interval_seconds)
                continue
            if http_code != 200:
                print(
                    f"  [deployed] probe attempt {attempt}/{max_attempts}: "
                    f"HTTP {http_code}, retrying"
                )
                time.sleep(interval_seconds)
                continue
            # R76 P1-06: /health 仅用于 liveness(HTTP 200 + 解析 JSON)
            # 不再从 /health 读取 image_repo_digest(应用自报不可信)
            try:
                health = json.loads(body)
            except json.JSONDecodeError:
                print(
                    f"  [deployed] probe attempt {attempt}/{max_attempts}: "
                    f"non-JSON body, retrying"
                )
                time.sleep(interval_seconds)
                continue
            # liveness 通过,现在做平台独立身份回读。Secretless 的 immutable
            # identity 已由 Step 14 从 Docker engine 读取并签名，状态机只接受该 manifest。
            if self.state.secretless_mode:
                platform_ok = bool(self.state.candidate_manifest_sha256)
                platform_digest = expected_digest
                platform_err = "" if platform_ok else "missing candidate manifest identity"
            else:
                platform_ok, platform_digest, platform_err = self.verify_platform_identity()
            if not platform_ok:
                print(
                    f"  [deployed] probe attempt {attempt}/{max_attempts}: "
                    f"platform identity verification failed: {platform_err}"
                )
                time.sleep(interval_seconds)
                continue
            # runtime_config_digest 仍从 /health 读取(配置非镜像身份)
            config_digest = str(health.get("runtime_config_digest", ""))
            if expected_config and config_digest != expected_config:
                print(
                    f"  [deployed] probe attempt {attempt}/{max_attempts}: "
                    f"runtime_config_digest mismatch (expected={expected_config}, "
                    f"actual={config_digest})"
                )
                time.sleep(interval_seconds)
                continue
            print(
                f"  [deployed] probe attempt {attempt}/{max_attempts}: "
                f"PASS — liveness + platform identity verified"
            )
            self._transition(
                STATE_DEPLOYED,
                reason="health probe (liveness) + platform identity verified (R76 P1-06)",
                metadata={
                    "attempt": attempt,
                    "image_repo_digest": platform_digest,
                    "runtime_config_digest": config_digest,
                    "identity_source": "docker_inspect",
                },
            )
            return

        self._mark_failed(
            f"deployed: probe failed after {max_attempts} attempts "
            f"(liveness or platform identity not matching)"
        )
        _fail_closed(
            1,
            f"deployed: probe failed after {max_attempts} attempts",
        )

    # ── 阶段 4: verified (业务探针) ─────────────────────────────

    def verify_business_probe(self) -> None:
        """verified 阶段:调用 /api/v1/status 业务探针。

        Fail-closed:
            - 业务探针 URL 缺失 → 转换 failed
            - 业务探针 HTTP 非 2xx → 转换 failed
            - 业务探针响应 status != "ok" 且 ready != True → 转换 failed
        """
        if self.state.current_state != STATE_DEPLOYED:
            _fail_closed(
                1,
                f"verify_business_probe requires current_state={STATE_DEPLOYED!r}, "
                f"got {self.state.current_state!r}",
            )
        probe_url = self.state.deploy_probe_url
        if not probe_url:
            self._mark_failed("verify: DEPLOY_PROBE_URL not configured")
            _fail_closed(1, "verify: DEPLOY_PROBE_URL not configured")

        biz_url = f"{probe_url.rstrip('/')}/api/v1/status"
        try:
            http_code, body, _ = _http_request(
                "GET",
                biz_url,
                timeout=DEFAULT_HTTP_TIMEOUT_SECONDS,
            )
        except (urllib.error.URLError, OSError) as e:
            self._mark_failed(f"verify: business probe network error: {e}")
            _fail_closed(1, f"verify: business probe network error: {e}")

        if http_code not in SUCCESS_HTTP_CODES:
            self._mark_failed(f"verify: business probe HTTP {http_code}")
            _fail_closed(1, f"verify: business probe HTTP {http_code}")

        try:
            biz = json.loads(body)
        except json.JSONDecodeError as e:
            self._mark_failed(f"verify: business probe non-JSON body: {e}")
            _fail_closed(1, f"verify: business probe non-JSON body: {e}")

        if biz.get("status") != "ok" and biz.get("ready") is not True:
            self._mark_failed(
                f"verify: business probe status={biz.get('status')!r}, "
                f"ready={biz.get('ready')!r}"
            )
            _fail_closed(
                1,
                f"verify: business probe failed (status={biz.get('status')!r})",
            )

        if self.state.secretless_mode and not self.state.identity_restored:
            self._mark_failed("verify: source database identity was not restored before deployment verdict")
            _fail_closed(1, "verify: source database identity was not restored")

        # 设置 GitHub deployment status=success
        deployment_id = self.state.deployment_id
        if deployment_id and not self.state.secretless_mode:
            try:
                _gh_api(
                    "POST",
                    f"repos/maxiuquan/tgjiema/deployments/{deployment_id}/statuses",
                    fields={
                        "state": "success",
                        "description": "Deployment verified (R76 O10): RepoDigest + business probe passed",
                    },
                )
            except RuntimeError as e:
                self._mark_failed(f"verify: GitHub success status update failed: {e}")
                _fail_closed(1, f"verify: GitHub success status update failed: {e}")

        self._transition(
            STATE_VERIFIED,
            reason="business probe passed + GitHub status=success",
        )
        print("  [verified] business probe passed + GitHub status=success")

    # ── 查询 ────────────────────────────────────────────────────

    def is_verified(self) -> bool:
        """是否处于 verified 终态(允许创建 production tag)。"""
        return self.state.current_state == STATE_VERIFIED

    def is_failed(self) -> bool:
        """是否处于 failed 终态。"""
        return self.state.current_state == STATE_FAILED


def _fail_closed(code: int, message: str) -> None:
    """fail-closed 退出(打印到 stderr 后 sys.exit)。"""
    print(f"::error::{message}", file=sys.stderr)
    sys.exit(code)


# ════════════════════════════════════════════════════════════════
# 完整生命周期编排
# ════════════════════════════════════════════════════════════════


def run_full_lifecycle(
    *,
    production_tag: str,
    source_sha: str,
    image_repo_digest: str,
    runtime_config_digest: str,
    deploy_hook_url: str,
    deploy_probe_url: str,
    state_file: Path,
    probe_max_attempts: int = DEFAULT_PROBE_MAX_ATTEMPTS,
    probe_interval: int = DEFAULT_PROBE_INTERVAL_SECONDS,
    secretless_identity: dict[str, str] | None = None,
) -> int:
    """运行完整部署生命周期: init → deploying → deployed → verified。

    Returns:
        0 — verified(可创建 production tag)
        1 — failed(禁止创建 production tag)
    """
    print("=== R76 O10: Production Deployment State Machine ===")
    print(f"  production_tag:        {production_tag}")
    print(f"  source_sha:            {source_sha}")
    print(f"  image_repo_digest:     {image_repo_digest}")
    print(f"  runtime_config_digest: {runtime_config_digest}")
    print(f"  deploy_hook_url:       {deploy_hook_url}")
    print(f"  deploy_probe_url:      {deploy_probe_url}")
    print(f"  state_file:            {state_file}")
    print()

    # 从 state_file 加载(若存在)或新建
    machine = DeploymentStateMachine(state_file=state_file)

    # 终态检查 — 防止重试跳过阶段
    if machine.is_verified():
        print("  [skip] state_file already in verified terminal state")
        return 0
    if machine.is_failed():
        print(
            f"  [blocked] state_file already in failed terminal state "
            f"(reason: {machine.state.failure_reason})",
            file=sys.stderr,
        )
        return 1

    # 阶段 1: init
    if machine.state.current_state == STATE_INIT and not machine.state.deployment_id:
        machine.init_deployment(
            production_tag=production_tag,
            source_sha=source_sha,
            image_repo_digest=image_repo_digest,
            runtime_config_digest=runtime_config_digest,
            deploy_hook_url=deploy_hook_url,
            deploy_probe_url=deploy_probe_url,
            secretless_identity=secretless_identity,
        )

    # 阶段 2: deploying
    if machine.state.current_state == STATE_INIT:
        machine.trigger_deploy()

    # 阶段 3: deployed
    if machine.state.current_state == STATE_DEPLOYING:
        machine.wait_for_deployed(
            max_attempts=probe_max_attempts,
            interval_seconds=probe_interval,
        )

    # 阶段 4: verified
    if machine.state.current_state == STATE_DEPLOYED:
        machine.verify_business_probe()

    # 终态校验
    if machine.is_verified():
        print()
        print("PASS: deployment state machine reached verified terminal state")
        print(f"  deployment_id: {machine.state.deployment_id}")
        print(f"  state_file:    {state_file}")
        return 0
    print(
        f"\nFAIL: deployment state machine did not reach verified "
        f"(current_state={machine.state.current_state!r}, "
        f"failure_reason={machine.state.failure_reason!r})",
        file=sys.stderr,
    )
    return 1


# ════════════════════════════════════════════════════════════════
# CLI
# ════════════════════════════════════════════════════════════════


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deployment_state_machine",
        description="R76 O10: Production Deployment State Machine",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path("./deployment-state.json"),
        help="State file path (accepted before or after the subcommand)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_state_file_option(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument(
            "--state-file",
            type=Path,
            default=argparse.SUPPRESS,
            help="State file path (accepted before or after the subcommand)",
        )

    # run — 完整生命周期
    run_p = sub.add_parser("run", help="Run full lifecycle: init -> deploying -> deployed -> verified")
    add_state_file_option(run_p)
    run_p.add_argument("--production-tag", required=True)
    run_p.add_argument("--source-sha", required=True)
    run_p.add_argument("--image-repo-digest", required=True)
    run_p.add_argument("--runtime-config-digest", required=True)
    run_p.add_argument("--deploy-hook-url", required=True)
    run_p.add_argument("--deploy-probe-url", required=True)
    run_p.add_argument("--candidate-manifest", type=Path, default=None)
    run_p.add_argument("--rollback-evidence", type=Path, default=None)
    run_p.add_argument("--probe-max-attempts", type=int, default=DEFAULT_PROBE_MAX_ATTEMPTS)
    run_p.add_argument("--probe-interval", type=int, default=DEFAULT_PROBE_INTERVAL_SECONDS)
    # R80 Step 15: 场景与期望结果(用于负向测试)
    run_p.add_argument(
        "--scenario", default="",
        help="故障场景(通过 POST /__scenario 设置到 simulator): success/runtime_config_drift/business_probe_failure 等",
    )
    run_p.add_argument(
        "--expect", default="",
        help="期望结果(failure = 期望生命周期失败,成功时 exit 0)",
    )

    # init — 仅创建 deployment record
    init_p = sub.add_parser("init", help="Create GitHub Deployment record (status=queued)")
    add_state_file_option(init_p)
    init_p.add_argument("--production-tag", required=True)
    init_p.add_argument("--source-sha", required=True)
    init_p.add_argument("--image-repo-digest", required=True)
    init_p.add_argument("--runtime-config-digest", required=True)
    init_p.add_argument("--deploy-hook-url", required=True)
    init_p.add_argument("--deploy-probe-url", required=True)

    # deploy — 触发 deploy hook
    deploy_p = sub.add_parser("deploy", help="Trigger deploy hook (init -> deploying)")
    add_state_file_option(deploy_p)

    # verify — 等待探针 + 业务探针
    verify_p = sub.add_parser("verify", help="Wait for probe + business probe (deploying/deployed -> verified)")
    add_state_file_option(verify_p)
    verify_p.add_argument("--probe-max-attempts", type=int, default=DEFAULT_PROBE_MAX_ATTEMPTS)
    verify_p.add_argument("--probe-interval", type=int, default=DEFAULT_PROBE_INTERVAL_SECONDS)

    # status — 查询当前状态
    status_p = sub.add_parser("status", help="Print current state and exit")
    add_state_file_option(status_p)

    # fail — 手动标记失败(调试用)
    fail_p = sub.add_parser("fail", help="Manually mark deployment as failed")
    add_state_file_option(fail_p)
    fail_p.add_argument("--reason", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        scenario = getattr(args, "scenario", "")
        expect = getattr(args, "expect", "")
        if scenario:
            hook_url = args.deploy_hook_url
            base_url = (
                hook_url.rsplit("/", 1)[0]
                if "/" in hook_url.split("//", 1)[-1]
                else hook_url
            )
            scenario_url = f"{base_url}/__scenario"
            try:
                scenario_payload = json.dumps({"scenario": scenario}).encode("utf-8")
                request = urllib.request.Request(
                    scenario_url,
                    data=scenario_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    if response.status != 200:
                        _fail_closed(2, f"scenario setup returned HTTP {response.status}")
                    print(
                        f"  [scenario] POST {scenario_url} → {response.status} "
                        f"(scenario={scenario})"
                    )
            except (urllib.error.URLError, OSError) as exc:
                _fail_closed(2, f"failed to configure scenario {scenario!r}: {exc}")

        if (args.candidate_manifest is None) != (args.rollback_evidence is None):
            _fail_closed(
                2,
                "--candidate-manifest and --rollback-evidence must be provided together",
            )
        secretless_identity = None
        if args.candidate_manifest is not None and args.rollback_evidence is not None:
            secretless_identity = _load_secretless_identity(
                args.candidate_manifest,
                args.rollback_evidence,
            )
            if args.source_sha != secretless_identity["source_sha"]:
                _fail_closed(2, "--source-sha does not match candidate manifest")
            expected_repo_digest = (
                "ghcr.io/secretless/tgjiema@" + secretless_identity["image_digest"]
            )
            if args.image_repo_digest != expected_repo_digest:
                _fail_closed(2, "--image-repo-digest does not match candidate manifest")
            if args.runtime_config_digest != secretless_identity["runtime_config_digest"]:
                _fail_closed(2, "--runtime-config-digest does not match candidate manifest")

        try:
            rc = run_full_lifecycle(
                production_tag=args.production_tag,
                source_sha=args.source_sha,
                image_repo_digest=args.image_repo_digest,
                runtime_config_digest=args.runtime_config_digest,
                deploy_hook_url=args.deploy_hook_url,
                deploy_probe_url=args.deploy_probe_url,
                state_file=args.state_file,
                probe_max_attempts=args.probe_max_attempts,
                probe_interval=args.probe_interval,
                secretless_identity=secretless_identity,
            )
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1

        if expect == "failure":
            try:
                terminal = json.loads(args.state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                print(
                    f"::error::expected-failure state file invalid: {exc}",
                    file=sys.stderr,
                )
                return 1
            failed_terminal = (
                rc == 1
                and terminal.get("current_state") == STATE_FAILED
                and bool(terminal.get("failure_reason"))
                and terminal.get("source_sha") == args.source_sha
                and (
                    secretless_identity is None
                    or (
                        terminal.get("identity_restored") is True
                        and terminal.get("rollback_source_identity")
                        == secretless_identity["source_database_identity"]
                    )
                )
            )
            if failed_terminal:
                print("  [expect=failure] lifecycle reached a validated failed terminal state")
                return 0
            print(
                "::error::expected-failure did not produce the required failed terminal state",
                file=sys.stderr,
            )
            return 1
        return rc

    if args.command == "init":
        machine = DeploymentStateMachine(state_file=args.state_file)
        machine.init_deployment(
            production_tag=args.production_tag,
            source_sha=args.source_sha,
            image_repo_digest=args.image_repo_digest,
            runtime_config_digest=args.runtime_config_digest,
            deploy_hook_url=args.deploy_hook_url,
            deploy_probe_url=args.deploy_probe_url,
        )
        return 0 if machine.state.deployment_id else 1

    if args.command == "deploy":
        machine = DeploymentStateMachine(state_file=args.state_file)
        machine.trigger_deploy()
        return 0

    if args.command == "verify":
        machine = DeploymentStateMachine(state_file=args.state_file)
        if machine.state.current_state == STATE_DEPLOYING:
            machine.wait_for_deployed(
                max_attempts=args.probe_max_attempts,
                interval_seconds=args.probe_interval,
            )
        if machine.state.current_state == STATE_DEPLOYED:
            machine.verify_business_probe()
        return 0 if machine.is_verified() else 1

    if args.command == "status":
        machine = DeploymentStateMachine(state_file=args.state_file)
        print(json.dumps(machine.state.to_dict(), indent=2, sort_keys=True))
        return 0

    if args.command == "fail":
        machine = DeploymentStateMachine(state_file=args.state_file)
        machine._mark_failed(args.reason)
        return 1

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
