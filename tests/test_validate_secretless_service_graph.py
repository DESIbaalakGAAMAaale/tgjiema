"""R79 §10.3 / P1-01 — secretless resolved service graph 硬门禁单测。

覆盖 §11 故障注入矩阵首行: resolved config 含两个 CRDB → gate 必须失败
(SECRETLESS_MULTIPLE_CRDB_SERVICES, exit 1),以及依赖图/DSN/隔离/加固
全部断言维度。所有用例使用合成 resolved config,无需 docker。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from validate_secretless_service_graph import (  # noqa: E402
    ERR_DEP_MISMATCH,
    ERR_DSN_MISMATCH,
    ERR_FORBIDDEN,
    ERR_HARDENING,
    ERR_MULTIPLE_CRDB,
    ERR_NO_CRDB,
    ERR_PROD_FORBIDDEN,
    export_service_graph,
    find_crdb_services,
    load_resolved,
    main,
    validate_production,
    validate_secretless,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "validate_secretless_service_graph.py"

CRDB_IMAGE = "cockroachdb/cockroach:v24.1.0@sha256:cc4e"
DSN = "postgresql://root@cockroachdb:26258/tgjiema?sslmode=disable"


def _crdb_service() -> dict:
    return {
        "image": CRDB_IMAGE,
        "read_only": True,
        "tmpfs": ["/tmp:rw,size=64m", "/cockroach/run", "/cockroach/certs"],
        "ports": [{"host_ip": "127.0.0.1", "published": "26258", "target": "26258"}],
    }


def _app_env() -> dict[str, str]:
    return {
        "SECRETLESS_MODE": "true",
        "SECRETLESS_CRDB_URL": DSN,
        "COCKROACHDB_URL": DSN,
    }


def _secretless_doc() -> dict:
    """合规的单 CRDB secretless resolved config。"""
    services: dict[str, dict] = {
        "cockroachdb": _crdb_service(),
        "redis": {"image": "redis:7-alpine"},
        "provider-sim": {"build": {"dockerfile": "Dockerfile"}},
        "minio": {"image": "minio/minio:RELEASE.2024-10-13T13-34-11Z"},
        "minio-init": {"image": "minio/mc:RELEASE.2024-10-02T08-27-28Z"},
    }
    for role in ("migration", "db_writer", "crdb_sync", "db_backup", "up", "idx", "dsp"):
        services[role] = {
            "build": {"dockerfile": "Dockerfile"},
            "environment": dict(_app_env()),
            "depends_on": {"cockroachdb": {"condition": "service_healthy"}},
        }
    return {"services": services}


class TestSingleCrdbAssertion:
    """§11 故障注入: resolved config 含两个 CRDB → gate 失败。"""

    def test_two_crdb_services_rejected(self):
        doc = _secretless_doc()
        doc["services"]["cockroachdb-secretless"] = _crdb_service()
        violations = validate_secretless(doc)
        assert violations
        assert violations[0][0] == ERR_MULTIPLE_CRDB

    def test_single_crdb_passes(self):
        assert validate_secretless(_secretless_doc()) == []

    def test_no_crdb_rejected(self):
        doc = _secretless_doc()
        del doc["services"]["cockroachdb"]
        violations = validate_secretless(doc)
        assert violations[0][0] == ERR_NO_CRDB

    def test_find_crdb_services_by_image(self):
        doc = _secretless_doc()
        assert find_crdb_services(doc) == ["cockroachdb"]


class TestDependencyAssertion:
    def test_missing_crdb_dependency_rejected(self):
        doc = _secretless_doc()
        doc["services"]["up"]["depends_on"] = {"redis": {"condition": "service_healthy"}}
        violations = validate_secretless(doc)
        assert any(code == ERR_DEP_MISMATCH and "up" in detail for code, detail in violations)

    def test_role_missing_from_graph_rejected(self):
        doc = _secretless_doc()
        del doc["services"]["dsp"]
        violations = validate_secretless(doc)
        assert any(code == ERR_DEP_MISMATCH and "dsp" in detail for code, detail in violations)


class TestDsnAssertion:
    def test_dsn_host_mismatch_rejected(self):
        doc = _secretless_doc()
        doc["services"]["up"]["environment"]["COCKROACHDB_URL"] = (
            "postgresql://root@other-host:26258/tgjiema?sslmode=disable"
        )
        violations = validate_secretless(doc)
        assert any(code == ERR_DSN_MISMATCH and "host" in detail for code, detail in violations)

    def test_dsn_port_mismatch_rejected(self):
        doc = _secretless_doc()
        doc["services"]["idx"]["environment"]["COCKROACHDB_URL"] = (
            "postgresql://root@cockroachdb:26257/tgjiema?sslmode=disable"
        )
        violations = validate_secretless(doc)
        assert any(code == ERR_DSN_MISMATCH and "26257" in detail for code, detail in violations)

    def test_dsn_divergence_across_services_rejected(self):
        doc = _secretless_doc()
        doc["services"]["dsp"]["environment"]["SECRETLESS_CRDB_URL"] = (
            "postgresql://root@cockroachdb:26258/other?sslmode=disable"
        )
        violations = validate_secretless(doc)
        assert any(code == ERR_DSN_MISMATCH for code, _ in violations)


class TestIsolationAssertion:
    def test_production_endpoint_rejected(self):
        doc = _secretless_doc()
        doc["services"]["up"]["environment"]["PROVIDER_BASE_URL"] = (
            "https://api.telegram.org"
        )
        violations = validate_secretless(doc)
        assert any(code == ERR_FORBIDDEN and "api.telegram.org" in detail
                   for code, detail in violations)

    def test_public_port_binding_rejected(self):
        doc = _secretless_doc()
        doc["services"]["cockroachdb"]["ports"] = [
            {"host_ip": "0.0.0.0", "published": "26258", "target": "26258"}
        ]
        violations = validate_secretless(doc)
        assert any(code == ERR_FORBIDDEN and "0.0.0.0" in detail
                   for code, detail in violations)

    def test_loopback_port_binding_allowed(self):
        assert validate_secretless(_secretless_doc()) == []


class TestHardeningAssertion:
    """override 继承证明: read_only + tmpfs 最小写集合。"""

    def test_read_only_missing_rejected(self):
        doc = _secretless_doc()
        doc["services"]["cockroachdb"]["read_only"] = False
        violations = validate_secretless(doc)
        assert any(code == ERR_HARDENING and "read_only" in detail
                   for code, detail in violations)

    def test_tmpfs_missing_rejected(self):
        doc = _secretless_doc()
        doc["services"]["cockroachdb"]["tmpfs"] = ["/tmp"]
        violations = validate_secretless(doc)
        assert any(code == ERR_HARDENING and "/cockroach/run" in detail
                   for code, detail in violations)


class TestProductionGraph:
    def test_provider_sim_rejected_in_production(self):
        doc = {"services": {"provider-sim": {}, "up": {"environment": {}}}}
        violations = validate_production(doc)
        assert any(code == ERR_PROD_FORBIDDEN for code, _ in violations)

    def test_secretless_mode_rejected_in_production(self):
        doc = {"services": {"up": {"environment": {"SECRETLESS_MODE": "true"}}}}
        violations = validate_production(doc)
        assert any(code == ERR_PROD_FORBIDDEN for code, _ in violations)

    def test_ci_minio_creds_rejected_in_production(self):
        doc = {"services": {"up": {"environment": {"CI_MINIO_ROOT_USER": "x"}}}}
        violations = validate_production(doc)
        assert any(code == ERR_PROD_FORBIDDEN for code, _ in violations)

    def test_clean_production_passes(self):
        doc = {
            "services": {
                "up": {"environment": {"APP_ENV": "production"}},
                "cockroachdb": {"image": CRDB_IMAGE},
            }
        }
        assert validate_production(doc) == []


class TestCliAndExport:
    def test_cli_exit_codes(self, tmp_path: Path):
        ok = tmp_path / "ok.json"
        ok.write_text(json.dumps(_secretless_doc()), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), str(ok)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0, proc.stderr + proc.stdout

        bad = tmp_path / "bad.json"
        bad_doc = _secretless_doc()
        bad_doc["services"]["cockroachdb-secretless"] = _crdb_service()
        bad.write_text(json.dumps(bad_doc), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), str(bad)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 1
        assert ERR_MULTIPLE_CRDB in proc.stdout

    def test_cli_unparseable_input(self, tmp_path: Path):
        bad = tmp_path / "bad.json"
        bad.write_text("not-json-not-yaml: [", encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), str(bad)],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 2

    def test_export_graph_artifact(self, tmp_path: Path):
        out = tmp_path / "graph.json"
        export_service_graph(_secretless_doc(), out)
        graph = json.loads(out.read_text(encoding="utf-8"))
        assert graph["crdb_service_count"] == 1
        names = {n["name"] for n in graph["services"]}
        assert {"cockroachdb", "up", "migration"} <= names
        assert any(
            e["from"] == "up" and e["to"] == "cockroachdb" for e in graph["dependency_edges"]
        )

    def test_load_resolved_yaml_fallback(self, tmp_path: Path):
        yaml_text = (
            "services:\n"
            "  cockroachdb:\n"
            "    image: cockroachdb/cockroach:v24.1.0\n"
            "    read_only: true\n"
        )
        p = tmp_path / "resolved.yml"
        p.write_text(yaml_text, encoding="utf-8")
        doc = load_resolved(p)
        assert doc["services"]["cockroachdb"]["read_only"] is True

    def test_main_module_entry(self, tmp_path: Path, capsys: pytest.CaptureFixture):
        ok = tmp_path / "ok.json"
        ok.write_text(json.dumps(_secretless_doc()), encoding="utf-8")
        assert main([str(ok)]) == 0
