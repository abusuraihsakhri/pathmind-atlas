"""Regression coverage for the local console, authentication, and review safety."""
import pytest
from fastapi.testclient import TestClient

from agents.api import app
from agents.models import init_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PATHMIND_DATABASE_URL", f"sqlite:///{tmp_path / 'console.db'}")
    monkeypatch.delenv("PATHMIND_API_KEY", raising=False)
    init_db()
    with TestClient(app) as test_client:
        yield test_client


def test_console_served_with_real_api_workflows(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert 'id="caseForm"' in response.text
    assert 'id="qcForm"' in response.text
    assert 'id="synForm"' in response.text
    assert '"/api/cases"' in response.text
    assert '"/api/audit/verify"' in response.text
    assert "DOSSIER-SIM-" not in response.text


def test_api_key_must_match_and_health_remains_available(client, monkeypatch):
    secret = "security-regression-test-api-key-32-characters"
    monkeypatch.setenv("PATHMIND_API_KEY", secret)
    assert client.get("/health").status_code == 200
    assert client.get("/api/audit/verify").status_code == 401
    assert client.get("/api/audit/verify", headers={"X-Pathmind-API-Key": "bad"}).status_code == 401
    assert client.get("/api/audit/verify", headers={"X-Pathmind-API-Key": secret}).status_code == 200


def test_incomplete_case_cannot_be_supervisor_approved(client):
    create = client.post("/api/cases", json={
        "accession_id_anonymized": "S26-EMPTY",
        "organ_system": "BREAST",
        "specimen_type": "De-identified tissue"
    })
    assert create.status_code == 201
    case_id = create.json()["case_id"]
    reviewed = client.post(f"/api/cases/{case_id}/review")
    assert reviewed.status_code == 200
    assert reviewed.json()["overall_status"] == "VALIDATION_FAILED"
    assert "required" in reviewed.json()["recommended_action"].lower()
