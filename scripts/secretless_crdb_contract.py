#!/usr/bin/env python3
"""Generate and validate the Secretless CockroachDB connection contract.

The Secretless stack deliberately splits CockroachDB's insecure RPC listener
(``localhost:26257``) from its SQL listener (``0.0.0.0:26258``).  Application
containers must only use the SQL listener.  This module is the single source
of truth for the generated connection settings used by Docker Compose.

R79 §10.2 / P0-03: the Secretless overlay overrides the base ``cockroachdb``
service key (single-CRDB topology) instead of adding a second
``cockroachdb-secretless`` service.  The resolved service graph contains
exactly one CockroachDB; every application role connects to it via
``cockroachdb:26258``.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


# R79 §10.2: 单 CRDB 拓扑 — 服务名即基础 compose 的 cockroachdb
CRDB_HOST = "cockroachdb"
CRDB_SERVICE = "cockroachdb"
CRDB_SQL_PORT = 26258
CRDB_DATABASE = "tgjiema"
CRDB_USER = "root"


def build_secretless_crdb_url() -> str:
    """Return the only SQL DSN allowed in the Secretless Compose network."""
    return (
        f"postgresql://{CRDB_USER}@{CRDB_HOST}:{CRDB_SQL_PORT}/"
        f"{CRDB_DATABASE}?sslmode=disable"
    )


def write_compose_env(destination: Path) -> Path:
    """Write the non-secret Compose environment consumed by every app role."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "\n".join(
            (
                f"SECRETLESS_CRDB_HOST={CRDB_HOST}",
                f"SECRETLESS_CRDB_SQL_PORT={CRDB_SQL_PORT}",
                f"SECRETLESS_CRDB_DATABASE={CRDB_DATABASE}",
                f"SECRETLESS_CRDB_URL={build_secretless_crdb_url()}",
                "",
            )
        ),
        encoding="utf-8",
    )
    return destination


def _environment_values(service: dict[str, Any]) -> list[str]:
    environment = service.get("environment", [])
    if isinstance(environment, dict):
        return [f"{key}={value}" for key, value in environment.items()]
    if isinstance(environment, list):
        return [str(value) for value in environment]
    raise ValueError("service environment must be a list or mapping")


def validate_compose_contract(compose_path: Path) -> list[str]:
    """Return all contract violations in *compose_path* without using Docker."""
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = document.get("services", {}) if isinstance(document, dict) else {}
    violations: list[str] = []

    crdb = services.get(CRDB_SERVICE)
    if not isinstance(crdb, dict):
        return [f"{CRDB_SERVICE} service is missing"] 

    # R79 §10.2: 禁止双 CRDB 拓扑回归 — 旧的第二套服务名不得再出现
    if "cockroachdb-secretless" in services:
        violations.append(
            "cockroachdb-secretless service must not exist — "
            "R79 §10.2 single-CRDB topology overrides the base cockroachdb key"
        )

    command = str(crdb.get("command", ""))
    if "--listen-addr=localhost:26257" not in command:
        violations.append("CRDB RPC listener must remain localhost:26257")
    if "--sql-addr=0.0.0.0:26258" not in command:
        violations.append("CRDB SQL listener must be 0.0.0.0:26258")

    healthcheck = crdb.get("healthcheck", {})
    health_test = " ".join(str(item) for item in healthcheck.get("test", []))
    if "--host=localhost:26258" not in health_test:
        violations.append("CRDB healthcheck must query SQL port 26258")
    if "SELECT 1" not in health_test or "CREATE DATABASE IF NOT EXISTS tgjiema" not in health_test:
        violations.append("CRDB healthcheck must prove SQL readiness and database creation")

    for name, service in services.items():
        if name == CRDB_SERVICE or not isinstance(service, dict):
            continue
        values = _environment_values(service)
        crdb_values = [value for value in values if value.startswith("COCKROACHDB_URL=")]
        if not crdb_values:
            continue
        required = {
            "SECRETLESS_CRDB_HOST=${SECRETLESS_CRDB_HOST:?SECRETLESS_CRDB_HOST must be set}",
            "SECRETLESS_CRDB_SQL_PORT=${SECRETLESS_CRDB_SQL_PORT:?SECRETLESS_CRDB_SQL_PORT must be set}",
            "SECRETLESS_CRDB_DATABASE=${SECRETLESS_CRDB_DATABASE:?SECRETLESS_CRDB_DATABASE must be set}",
            "SECRETLESS_CRDB_URL=${SECRETLESS_CRDB_URL:?SECRETLESS_CRDB_URL must be generated}",
            "COCKROACHDB_URL=${SECRETLESS_CRDB_URL:?SECRETLESS_CRDB_URL must be generated}",
        }
        missing = required.difference(values)
        if missing:
            violations.append(f"{name}: missing centralized CRDB environment: {sorted(missing)}")
        if any("localhost:26258" in value for value in crdb_values):
            violations.append(f"{name}: application CRDB DSN must not use localhost")
        if any("26257" in value for value in crdb_values):
            violations.append(f"{name}: application CRDB DSN must not use RPC port 26257")

    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, default=Path("docker-compose.secretless.yml"))
    parser.add_argument("--write-env", type=Path)
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args()

    if args.write_env:
        output = write_compose_env(args.write_env)
        print(f"wrote Secretless CRDB contract to {output}")
    if args.validate:
        violations = validate_compose_contract(args.compose)
        if violations:
            for violation in violations:
                print(f"ERROR: {violation}")
            return 1
        print("Secretless CRDB port contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
