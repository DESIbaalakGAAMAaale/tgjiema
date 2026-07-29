from __future__ import annotations

from pathlib import Path

from scripts.secretless_crdb_contract import (
    CRDB_SQL_PORT,
    build_secretless_crdb_url,
    validate_compose_contract,
    write_compose_env,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_secretless_compose_uses_sql_listener_for_health_and_clients() -> None:
    assert validate_compose_contract(REPO_ROOT / "docker-compose.secretless.yml") == []


def test_generated_crdb_contract_is_single_sql_dsn(tmp_path: Path) -> None:
    env_path = write_compose_env(tmp_path / "secretless-crdb.env")
    content = env_path.read_text(encoding="utf-8")
    assert f"SECRETLESS_CRDB_SQL_PORT={CRDB_SQL_PORT}" in content
    assert f"SECRETLESS_CRDB_URL={build_secretless_crdb_url()}" in content
    assert "localhost" not in build_secretless_crdb_url()


def test_wrong_healthcheck_port_is_rejected(tmp_path: Path) -> None:
    compose = (REPO_ROOT / "docker-compose.secretless.yml").read_text(encoding="utf-8")
    broken = compose.replace("--host=localhost:26258", "--host=localhost:26257", 1)
    broken_path = tmp_path / "broken-compose.yml"
    broken_path.write_text(broken, encoding="utf-8")

    assert "CRDB healthcheck must query SQL port 26258" in validate_compose_contract(broken_path)
