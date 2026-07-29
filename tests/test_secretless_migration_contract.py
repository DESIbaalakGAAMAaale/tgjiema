"""Secretless migration service contract tests (R79 §10.2 / P0-03).

Validates the ``docker-compose.secretless.yml`` migration service contract.

Strategy:
    1. If ``artifacts/secretless-e2e/compose-resolved.json`` exists (produced
       by ``docker compose config --format json`` in CI), use it as the
       authoritative resolved service graph.
    2. Otherwise fall back to parsing ``docker-compose.yml`` and
       ``docker-compose.secretless.yml`` YAML directly. Compose override
       merge semantics are emulated shallowly: the overlay's
       ``services.migration`` keys win over the base for the fields under
       test (environment / depends_on).

These tests never require Docker to be running.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.compose_yaml import load_compose_yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
OVERLAY_COMPOSE = REPO_ROOT / "docker-compose.secretless.yml"
RESOLVED_JSON = REPO_ROOT / "artifacts" / "secretless-e2e" / "compose-resolved.json"

pytestmark = pytest.mark.skipif(
    not OVERLAY_COMPOSE.exists(),
    reason="docker-compose.secretless.yml not present in repo",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_env_list(env_list):
    """Parse a Compose environment list of ``KEY=value`` strings into a dict.

    Values may be Compose interpolation expressions such as
    ``${SECRETLESS_CRDB_URL:?msg}``; they are preserved verbatim.
    """
    result = {}
    if not env_list:
        return result
    for item in env_list:
        if not isinstance(item, str):
            continue
        if "=" in item:
            key, _, value = item.partition("=")
            result[key] = value
    return result


def _load_yaml(path: Path) -> dict:
    return load_compose_yaml(path) or {}


def _load_resolved() -> dict | None:
    if RESOLVED_JSON.exists():
        try:
            data = json.loads(RESOLVED_JSON.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if isinstance(data, dict) and isinstance(data.get("services"), dict):
            return data
    return None


def get_services() -> dict:
    """Return the resolved-ish services mapping.

    Prefers the CI-produced resolved JSON; otherwise merges base + overlay
    at the service level (overlay service dicts override base service dicts
    for the keys they define, mirroring Compose override semantics for the
    fields under test).
    """
    resolved = _load_resolved()
    if resolved is not None:
        return resolved["services"]

    base = _load_yaml(BASE_COMPOSE)
    overlay = _load_yaml(OVERLAY_COMPOSE)
    services: dict = dict(base.get("services") or {})
    for name, svc in (overlay.get("services") or {}).items():
        existing = services.get(name)
        if isinstance(existing, dict) and isinstance(svc, dict):
            merged = dict(existing)
            merged.update(svc)
            services[name] = merged
        else:
            services[name] = svc
    return services


def get_migration_service() -> dict:
    services = get_services()
    assert "migration" in services, "migration service missing from compose"
    migration = services["migration"]
    assert isinstance(migration, dict), "migration service must be a mapping"
    return migration


def get_migration_env() -> dict:
    """Return migration environment as a dict.

    Handles both the YAML list form (``- KEY=value``) and the resolved JSON
    mapping form (``{KEY: value}``).
    """
    migration = get_migration_service()
    env = migration.get("environment")
    if isinstance(env, dict):
        return {str(k): ("" if v is None else str(v)) for k, v in env.items()}
    if isinstance(env, list):
        return parse_env_list(env)
    return {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_migration_service_role():
    """Migration service exists and SERVICE_ROLE=migration is defined.

    The overlay does not redeclare SERVICE_ROLE; it is inherited from the
    base ``docker-compose.yml``. Accept either source.
    """
    services = get_services()
    assert "migration" in services, "migration service must exist"

    env = get_migration_env()
    if env.get("SERVICE_ROLE") == "migration":
        return

    # Fall back to base compose (overlay merge inherits this key).
    base = _load_yaml(BASE_COMPOSE)
    base_migration = (base.get("services") or {}).get("migration") or {}
    base_env = base_migration.get("environment")
    base_env_dict = (
        base_env
        if isinstance(base_env, dict)
        else parse_env_list(base_env or [])
    )
    assert base_env_dict.get("SERVICE_ROLE") == "migration", (
        "SERVICE_ROLE=migration must be defined in migration environment "
        "(overlay or base docker-compose.yml)"
    )


def test_migration_app_env_test():
    env = get_migration_env()
    assert env.get("APP_ENV") == "test", (
        f"APP_ENV must be 'test' in migration environment, got {env.get('APP_ENV')!r}"
    )


def test_migration_secretless_mode():
    env = get_migration_env()
    assert env.get("SECRETLESS_MODE") == "true", (
        "SECRETLESS_MODE must be 'true' in migration environment, "
        f"got {env.get('SECRETLESS_MODE')!r}"
    )


def test_migration_cockroachdb_url_matches_secretless():
    """COCKROACHDB_URL must be sourced from SECRETLESS_CRDB_URL.

    In the YAML overlay this appears as the substitution pattern
    ``${SECRETLESS_CRDB_URL:...}``; in resolved JSON it is the literal
    expanded DSN, in which case SECRETLESS_CRDB_URL must equal it.
    """
    env = get_migration_env()
    crdb_url = env.get("COCKROACHDB_URL")
    assert crdb_url, "COCKROACHDB_URL must be set in migration environment"

    if "${SECRETLESS_CRDB_URL" in crdb_url:
        return  # YAML substitution pattern — contract satisfied.

    # Resolved JSON path: the expanded value must match SECRETLESS_CRDB_URL.
    secretless_url = env.get("SECRETLESS_CRDB_URL")
    assert secretless_url, (
        "SECRETLESS_CRDB_URL must be set when COCKROACHDB_URL is resolved"
    )
    assert crdb_url == secretless_url, (
        "COCKROACHDB_URL must equal SECRETLESS_CRDB_URL in resolved config"
    )


def test_migration_crdb_host_is_cockroachdb():
    """SECRETLESS_CRDB_HOST must reference the single ``cockroachdb`` service.

    Overlay form: ``${SECRETLESS_CRDB_HOST:?...}`` (host injected by CI runner
    and asserted elsewhere to be ``cockroachdb``). Resolved form: literal
    hostname that must be ``cockroachdb`` and must NOT be
    ``cockroachdb-secretless``.
    """
    env = get_migration_env()
    host = env.get("SECRETLESS_CRDB_HOST")
    assert host, "SECRETLESS_CRDB_HOST must be set in migration environment"
    assert "cockroachdb-secretless" not in host, (
        "SECRETLESS_CRDB_HOST must not reference the retired "
        f"cockroachdb-secretless service, got {host!r}"
    )
    if not host.startswith("${"):
        assert host == "cockroachdb", (
            f"SECRETLESS_CRDB_HOST must be 'cockroachdb', got {host!r}"
        )


def test_migration_crdb_port_is_26258():
    """SECRETLESS_CRDB_SQL_PORT must reference SQL port 26258."""
    env = get_migration_env()
    port = env.get("SECRETLESS_CRDB_SQL_PORT")
    assert port, "SECRETLESS_CRDB_SQL_PORT must be set in migration environment"
    if port.startswith("${"):
        # YAML substitution — the runner injects 26258; assert the variable
        # name is the secretless SQL port (not the RPC port 26257).
        assert "SECRETLESS_CRDB_SQL_PORT" in port
        return
    assert str(port) == "26258", (
        f"SECRETLESS_CRDB_SQL_PORT must be 26258, got {port!r}"
    )


def test_migration_depends_on_single_cockroachdb():
    """migration depends_on must include cockroachdb with service_healthy."""
    migration = get_migration_service()
    depends_on = migration.get("depends_on")
    assert isinstance(depends_on, dict), (
        "migration.depends_on must be the long-form mapping with conditions"
    )
    assert "cockroachdb" in depends_on, (
        "migration must depend on the single 'cockroachdb' service"
    )
    assert "cockroachdb-secretless" not in depends_on, (
        "migration must not depend on retired cockroachdb-secretless service"
    )
    condition = depends_on["cockroachdb"]
    if isinstance(condition, dict):
        assert condition.get("condition") == "service_healthy", (
            "cockroachdb dependency must use condition: service_healthy"
        )
    else:
        pytest.fail(
            "cockroachdb dependency must specify condition: service_healthy"
        )


def test_no_cockroachdb_secretless_service():
    """No service named ``cockroachdb-secretless`` may exist anywhere."""
    base = _load_yaml(BASE_COMPOSE)
    overlay = _load_yaml(OVERLAY_COMPOSE)
    base_services = base.get("services") or {}
    overlay_services = overlay.get("services") or {}
    assert "cockroachdb-secretless" not in base_services, (
        "docker-compose.yml must not define cockroachdb-secretless service"
    )
    assert "cockroachdb-secretless" not in overlay_services, (
        "docker-compose.secretless.yml must not define "
        "cockroachdb-secretless service (single CRDB topology, R79 §10.2)"
    )


def test_no_production_secret_references():
    """Migration env must not reference production secret namespaces."""
    env = get_migration_env()
    forbidden_prefixes = ("secrets.TEST_", "secrets.R2_", "secrets.COCKROACHDB_")
    offenders = []
    for key, value in env.items():
        for prefix in forbidden_prefixes:
            if prefix in str(value):
                offenders.append(f"{key}={value}")
    assert not offenders, (
        "migration environment must not reference production secrets "
        f"(secrets.TEST_* / secrets.R2_* / secrets.COCKROACHDB_*): {offenders}"
    )


def test_migration_entrypoint_is_migration_runner():
    """Base compose migration service must dispatch to migration_runner.

    R70 Wave 2 removed the explicit ``command`` override; dispatch happens
    via ``docker/entrypoint.py`` keyed on ``SERVICE_ROLE=migration``. Accept
    either an explicit command/entrypoint referencing migration_runner, or
    the SERVICE_ROLE-based dispatch contract.
    """
    base = _load_yaml(BASE_COMPOSE)
    base_migration = (base.get("services") or {}).get("migration")
    assert isinstance(base_migration, dict), (
        "base docker-compose.yml must define a migration service"
    )

    command = base_migration.get("command")
    entrypoint = base_migration.get("entrypoint")
    candidates = []
    for field in (command, entrypoint):
        if isinstance(field, str):
            candidates.append(field)
        elif isinstance(field, list):
            candidates.append(" ".join(str(x) for x in field))

    if any("migration_runner" in c for c in candidates):
        return

    # Fallback contract: SERVICE_ROLE=migration drives docker/entrypoint.py
    # to exec `python -m services.migration_runner`.
    base_env = base_migration.get("environment")
    base_env_dict = (
        base_env
        if isinstance(base_env, dict)
        else parse_env_list(base_env or [])
    )
    assert base_env_dict.get("SERVICE_ROLE") == "migration", (
        "base migration service must either reference migration_runner in "
        "command/entrypoint or set SERVICE_ROLE=migration for "
        "docker/entrypoint.py dispatch"
    )

    entrypoint_py = REPO_ROOT / "docker" / "entrypoint.py"
    if entrypoint_py.exists():
        text = entrypoint_py.read_text(encoding="utf-8")
        assert "migration_runner" in text, (
            "docker/entrypoint.py must map SERVICE_ROLE=migration to "
            "services.migration_runner"
        )
