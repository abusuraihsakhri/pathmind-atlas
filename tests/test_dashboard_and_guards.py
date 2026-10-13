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


def test_unknown_case_is_rejected_before_persistence(client):
    qc = client.post("/api/slides/qc", json={
        "case_id": "CASE-NOT-REGISTERED",
        "laplacian_blur_var": 1100,
    })
    assert qc.status_code == 404
    synoptic = client.post("/api/synoptic/validate", json={
        "case_id": "CASE-NOT-REGISTERED",
        "primary_tumor_site": "Breast",
        "histologic_type": "Carcinoma",
    })
    assert synoptic.status_code == 404
    assert client.post("/api/cases/CASE-NOT-REGISTERED/review").status_code == 404


def test_missing_breast_biomarkers_are_not_classified_valid(client):
    creation = client.post("/api/cases", json={
        "accession_id_anonymized": "S26-MISSING-BM",
        "organ_system": "BREAST",
        "specimen_type": "De-identified lumpectomy",
    })
    assert creation.status_code == 201
    result = client.post("/api/synoptic/validate", json={
        "case_id": creation.json()["case_id"],
        "protocol_type": "BREAST_INVASIVE",
        "primary_tumor_site": "Breast",
        "histologic_type": "Invasive carcinoma",
        "histologic_grade": "Grade 2",
        "tumor_size_cm": 1.5,
        "deep_margin_mm": 3.5,
        "pT": "pT1c",
        "pN": "pN0",
        "biomarkers": {},
    })
    assert result.status_code == 200
    assert result.json()["validation_status"] == "INCOMPLETE"
    assert any("MISSING_BREAST_BIOMARKERS" in v for v in result.json()["protocol_violations"])


def test_nonfinite_metrics_rejected_by_models():
    from pydantic import ValidationError
    from agents.qc_worker import WSIMetrics
    from agents.synoptic_worker import SynopticReportInput

    with pytest.raises(ValidationError):
        WSIMetrics(case_id="CASE-DEID", laplacian_blur_var=float("nan"))
    with pytest.raises(ValidationError):
        SynopticReportInput(
            case_id="CASE-DEID", primary_tumor_site="Breast",
            histologic_type="Carcinoma", tumor_size_cm=float("inf")
        )
