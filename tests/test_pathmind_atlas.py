"""
PathMind-Atlas Comprehensive Pytest Suite.
Tests PHI Guardrails, Cryptographic HMAC Audit Trail, Database Models, Inference Engine,
Slide QC Agent, Synoptic Validator Agent, Supervisor Orchestrator, and FastAPI REST Endpoints.
Architect: Dr. Abu Suraih Sakhri (@abusuraihsakhri)
"""

import os
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agents.base import (
    assert_no_phi,
    SecurityException,
    sanitize_phi_text,
    HMACSigner,
    AuditLogger,
    ActionExecutor,
    secure_invoke,
    ActorTier,
    AgentRole,
    default_audit_logger
)
from agents.models import (
    Base,
    Case,
    Slide,
    SynopticReport,
    Alert,
    AuditEvent,
    Action,
    CaseStatus,
    QCGrade,
    MarginStatus,
    ValidationStatus,
    AlertSeverity,
    init_db,
    get_session
)
from agents.llm_factory import LLMFactory, LLMProvider
from agents.qc_worker import SlideQCAgent, WSIMetrics
from agents.synoptic_worker import SynopticValidatorAgent, SynopticReportInput, CAPProtocol
from agents.supervisor import PathMindSupervisor
from agents.api import app


# ---------------------------------------------------------
# Fixtures
# ---------------------------------------------------------
@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    """Sets up an isolated SQLite temporary database for tests."""
    db_file = tmp_path / "test_pathmind.db"
    db_url = f"sqlite:///{db_file}"
    os.environ["PATHMIND_DATABASE_URL"] = db_url
    init_db()
    yield



@pytest.fixture
def test_client():
    return TestClient(app)


# ---------------------------------------------------------
# 1. PHI Guardrail Tests
# ---------------------------------------------------------
def test_phi_guard_clean_data():
    clean_data = {
        "specimen": "Left breast core biopsy",
        "diagnosis": "Invasive mammary carcinoma",
        "margins": "Deep margin clear by 4.5mm",
        "cellularity": "75%"
    }
    # Should not raise exception
    assert_no_phi(clean_data)
    assert_no_phi("Invasive lobular carcinoma, Nottingham Grade 2.")


def test_phi_guard_catches_ssn():
    with pytest.raises(SecurityException) as exc_info:
        assert_no_phi("Specimen labeled for SSN: 000-12-3456 or 123-45-6789.")
    assert "SSN" in str(exc_info.value)


def test_phi_guard_catches_mrn():
    with pytest.raises(SecurityException) as exc_info:
        assert_no_phi("Slide case assigned to MRN: 94827491A for processing.")
    assert "MRN_EXPLICIT" in str(exc_info.value)


def test_phi_guard_catches_phone_and_email():
    with pytest.raises(SecurityException):
        assert_no_phi("Contact referring oncologist at dr.smith@hospital.org")

    with pytest.raises(SecurityException):
        assert_no_phi("Call pathology office at 415-555-2671 for frozen section.")


def test_sanitize_phi_text():
    dirty_text = "Referral email doctor@example.com for patient with SSN 123-45-6789."
    sanitized = sanitize_phi_text(dirty_text)
    assert "doctor@example.com" not in sanitized
    assert "123-45-6789" not in sanitized
    assert "[REDACTED:" in sanitized


# ---------------------------------------------------------
# 2. Cryptographic HMAC & Audit Trail Tests
# ---------------------------------------------------------
def test_hmac_signer():
    signer = HMACSigner(secret_key="TEST_SECURE_KEY_123")
    payload = {"case_id": "CASE-101", "grade": "PASS"}
    sig1 = signer.sign_payload(payload)
    sig2 = signer.sign_payload(payload)
    assert sig1 == sig2
    assert signer.verify_signature(payload, sig1) is True

    # Mutated payload fails signature
    mutated = {"case_id": "CASE-101", "grade": "REJECT"}
    assert signer.verify_signature(mutated, sig1) is False


def test_audit_logger_chain_and_integrity():
    logger = AuditLogger()
    ev1 = logger.log_event(
        actor_tier=ActorTier.AGENT,
        agent_role=AgentRole.QC_WORKER,
        action_name="evaluate_slide_qc",
        target_resource_id="SLD-001",
        details={"status": "PASS", "score": 98.5}
    )
    ev2 = logger.log_event(
        actor_tier=ActorTier.AGENT,
        agent_role=AgentRole.SYNOPTIC_WORKER,
        action_name="validate_synoptic_report",
        target_resource_id="SYN-001",
        details={"status": "VALID", "margins": "CLEAR"}
    )
    assert ev2.prev_hash == ev1.current_hash
    is_valid, reason = logger.verify_integrity()
    assert is_valid is True
    assert reason is None


def test_audit_logger_tamper_detection():
    logger = AuditLogger()
    logger.log_event(
        actor_tier=ActorTier.AGENT,
        agent_role=AgentRole.QC_WORKER,
        action_name="evaluate_slide_qc",
        target_resource_id="SLD-001",
        details={"status": "PASS"}
    )
    logger.log_event(
        actor_tier=ActorTier.AGENT,
        agent_role=AgentRole.SUPERVISOR,
        action_name="approve_case",
        target_resource_id="CASE-101",
        details={"status": "APPROVED"}
    )

    # Tamper with first event details
    events = logger.get_events()
    events[0].details["status"] = "TAMPERED_INJECTED"

    is_valid, reason = logger.verify_integrity()
    assert is_valid is False
    assert "tampering detected" in reason.lower()


# ---------------------------------------------------------
# 3. Action Execution & Secure Invoke
# ---------------------------------------------------------
def test_secure_invoke_success():
    def compute_cell_density(patches: int, area_mm2: float) -> dict:
        return {"density": patches / area_mm2, "unit": "patches/mm2"}

    res = secure_invoke(
        agent_role=AgentRole.QC_WORKER,
        action_name="compute_density",
        target_id="SLD-TEST-1",
        func=compute_cell_density,
        patches=500,
        area_mm2=10.0
    )
    assert res["density"] == 50.0


def test_secure_invoke_blocks_phi():
    def dummy_func(text: str):
        return f"Processed: {text}"

    with pytest.raises(SecurityException):
        secure_invoke(
            agent_role=AgentRole.SUPERVISOR,
            action_name="dummy_action",
            target_id="CASE-PHI",
            func=dummy_func,
            text="Patient MRN: 99882211 patient data"
        )


# ---------------------------------------------------------
# 4. Database Models Tests
# ---------------------------------------------------------
def test_database_models_crud():
    session = get_session()
    c = Case(
        accession_id_anonymized="S26-9999",
        organ_system="BREAST",
        specimen_type="Lumpectomy"
    )
    session.add(c)
    session.commit()

    slide = Slide(
        case_id=c.case_id,
        block_id="A1",
        stain_type="H&E",
        laplacian_blur_var=1450.0,
        qc_grade=QCGrade.PASS,
        qc_score=99.0
    )
    session.add(slide)

    report = SynopticReport(
        case_id=c.case_id,
        protocol_type="BREAST_INVASIVE",
        primary_tumor_site="Upper outer quadrant",
        histologic_type="Invasive Ductal Carcinoma",
        margin_status=MarginStatus.NEGATIVE,
        validation_status=ValidationStatus.VALID
    )
    report.biomarkers = {"ER": "Positive", "PR": "Positive"}
    session.add(report)
    session.commit()

    retrieved = session.query(Case).filter(Case.case_id == c.case_id).first()
    assert retrieved is not None
    assert len(retrieved.slides) == 1
    assert retrieved.slides[0].laplacian_blur_var == 1450.0
    assert len(retrieved.synoptic_reports) == 1
    assert retrieved.synoptic_reports[0].biomarkers["ER"] == "Positive"
    session.close()


# ---------------------------------------------------------
# 5. Inference Engine Tests
# ---------------------------------------------------------
def test_llm_factory_mock():
    factory = LLMFactory(provider=LLMProvider.MOCK)
    qc_resp = factory.generate(prompt="Evaluate quality control focus sharpness and blur artifacts.")
    assert qc_resp.provider == LLMProvider.MOCK
    assert "PASS" in qc_resp.content or "sharpness" in qc_resp.content

    syn_resp = factory.generate(prompt="Validate CAP synoptic protocol requirements.")
    assert "VALID" in syn_resp.content or "protocol" in syn_resp.content


def test_llm_factory_blocks_phi_in_prompt():
    factory = LLMFactory(provider=LLMProvider.MOCK)
    with pytest.raises(SecurityException):
        factory.generate(prompt="Summarize chart for patient SSN: 000-11-2222.")


# ---------------------------------------------------------
# 6. Slide QC Agent Tests
# ---------------------------------------------------------
def test_slide_qc_agent_pass():
    agent = SlideQCAgent()
    metrics = WSIMetrics(
        case_id="CASE-QC-01",
        block_id="A1",
        laplacian_blur_var=1250.0,
        tissue_fold_pct=0.5,
        pen_mark_detected=False,
        air_bubble_pct=0.0
    )
    result = agent.evaluate_slide(metrics, persist_to_db=True)
    assert result.qc_grade == QCGrade.PASS
    assert result.qc_score >= 80.0
    assert len(result.flags) == 0
    assert len(result.alerts_generated) == 0


def test_slide_qc_agent_severe_blur_and_folds():
    agent = SlideQCAgent()
    metrics = WSIMetrics(
        case_id="CASE-QC-02",
        block_id="B2",
        laplacian_blur_var=180.0,  # Severe blur
        tissue_fold_pct=15.0,      # Severe fold
        pen_mark_detected=True,    # Pen mark
        air_bubble_pct=9.0         # Air bubble
    )
    result = agent.evaluate_slide(metrics, persist_to_db=True)
    assert result.qc_grade == QCGrade.REJECT
    assert result.qc_score < 50.0
    assert "SEVERE_OUT_OF_FOCUS_BLUR" in result.flags
    assert "EXTENSIVE_TISSUE_FOLDING" in result.flags
    assert "PEN_MARK_OBSCURING_TISSUE" in result.flags
    assert "LARGE_AIR_BUBBLE_ARTIFACT" in result.flags
    assert len(result.alerts_generated) >= 3


# ---------------------------------------------------------
# 7. Synoptic Validator Agent Tests
# ---------------------------------------------------------
def test_synoptic_validator_valid_breast():
    agent = SynopticValidatorAgent()
    input_data = SynopticReportInput(
        case_id="CASE-SYN-01",
        protocol_type=CAPProtocol.BREAST_INVASIVE,
        primary_tumor_site="Right Breast, 10:00",
        histologic_type="Invasive Carcinoma of No Special Type",
        histologic_grade="Nottingham Grade 2",
        tumor_size_cm=1.8,
        deep_margin_mm=5.0,
        radial_margin_mm=3.2,
        nodes_examined=15,
        nodes_positive=0,
        pT="pT1c",
        pN="pN0",
        pM="cM0",
        biomarkers={"ER": "90%", "PR": "70%", "HER2": "0 (Negative)"}
    )
    result = agent.validate_report(input_data, persist_to_db=True)
    assert result.validation_status == ValidationStatus.VALID
    assert result.margin_status == MarginStatus.NEGATIVE
    assert result.completeness_score == 100.0
    assert len(result.alerts_generated) == 0


def test_synoptic_validator_positive_margin():
    agent = SynopticValidatorAgent()
    input_data = SynopticReportInput(
        case_id="CASE-SYN-02",
        protocol_type=CAPProtocol.BREAST_INVASIVE,
        primary_tumor_site="Left Breast",
        histologic_type="Invasive Lobular Carcinoma",
        histologic_grade="Grade 2",
        tumor_size_cm=2.5,
        deep_margin_mm=0.0,  # Positive margin!
        radial_margin_mm=4.0,
        nodes_examined=8,
        nodes_positive=0,
        pT="pT2",
        pN="pN0",
        biomarkers={"ER": "95%", "PR": "90%", "HER2": "1+"}
    )
    result = agent.validate_report(input_data, persist_to_db=True)
    assert result.margin_status == MarginStatus.POSITIVE
    assert result.validation_status == ValidationStatus.CRITICAL_FLAG
    assert any("POSITIVE_MARGIN" in a["category"] for a in result.alerts_generated)


def test_synoptic_validator_stage_discordance():
    agent = SynopticValidatorAgent()
    input_data = SynopticReportInput(
        case_id="CASE-SYN-03",
        protocol_type=CAPProtocol.COLORECTAL_RESECTION,
        primary_tumor_site="Cecum",
        histologic_type="Adenocarcinoma",
        histologic_grade="Moderately Differentiated",
        tumor_size_cm=4.2,
        radial_margin_mm=6.0,
        nodes_examined=18,
        nodes_positive=4,  # Positive nodes present!
        pT="pT3",
        pN="pN0",          # Discordant! Should be pN2a
        biomarkers={"MSI": "MSS (Stable)"}
    )
    result = agent.validate_report(input_data, persist_to_db=True)
    assert result.validation_status in [ValidationStatus.CRITICAL_FLAG, ValidationStatus.DISCORDANT]
    assert any("STAGE_DISCORDANCE" in a["category"] for a in result.alerts_generated)


# ---------------------------------------------------------
# 8. Supervisor Orchestration Tests
# ---------------------------------------------------------
def test_supervisor_review_workflow():
    init_db()
    session = get_session()
    c = Case(
        accession_id_anonymized="S26-8888",
        organ_system="BREAST",
        specimen_type="Left Breast Lumpectomy"
    )
    session.add(c)
    session.commit()
    target_case_id = c.case_id
    session.close()

    supervisor = PathMindSupervisor()
    
    # 1. Evaluate Slide QC
    supervisor.qc_worker.evaluate_slide(
        WSIMetrics(case_id=target_case_id, block_id="A1", laplacian_blur_var=1300.0, tissue_fold_pct=0.0)
    )

    # 2. Evaluate Synoptic Report
    supervisor.synoptic_worker.validate_report(
        SynopticReportInput(
            case_id=target_case_id,
            protocol_type=CAPProtocol.BREAST_INVASIVE,
            primary_tumor_site="Left Breast",
            histologic_type="Invasive Ductal Carcinoma",
            histologic_grade="Grade 1",
            tumor_size_cm=1.2,
            deep_margin_mm=4.0,
            radial_margin_mm=2.5,
            nodes_examined=6,
            nodes_positive=0,
            pT="pT1c",
            pN="pN0",
            biomarkers={"ER": "98%", "PR": "95%", "HER2": "Negative"}
        )
    )

    # 3. Review Case
    summary = supervisor.review_case(target_case_id)
    assert summary.overall_status == CaseStatus.SUPERVISOR_APPROVED
    assert summary.slides_reviewed_count >= 1
    assert summary.qc_pass_count >= 1
    assert summary.synoptic_reports_count >= 1
    assert summary.margin_status == MarginStatus.NEGATIVE
    assert summary.critical_alerts_count == 0


def test_supervisor_chat():
    supervisor = PathMindSupervisor()
    reply = supervisor.chat_query("What are the CAP criteria for margin clearance in invasive breast carcinoma?")
    assert len(reply) > 10


# ---------------------------------------------------------
# 9. FastAPI REST API Integration Tests
# ---------------------------------------------------------
def test_fastapi_health(test_client):
    res = test_client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "HEALTHY"


def test_fastapi_metrics(test_client):
    res = test_client.get("/metrics")
    assert res.status_code == 200
    assert "pathmind" in res.text or "Prometheus" in res.text



def test_fastapi_cases_workflow(test_client):
    # 1. Create Case
    payload = {
        "accession_id_anonymized": "S26-7777",
        "organ_system": "BREAST",
        "specimen_type": "Mastectomy",
        "clinical_history_anonymized": "BRCA1 positive high risk"
    }
    res = test_client.post("/api/cases", json=payload)
    assert res.status_code == 201
    case_id = res.json()["case_id"]

    # 2. Get Case Details
    res = test_client.get(f"/api/cases/{case_id}")
    assert res.status_code == 200
    assert res.json()["accession_id_anonymized"] == "S26-7777"

    # 3. Submit Slide QC
    qc_payload = {
        "case_id": case_id,
        "block_id": "A1",
        "stain_type": "H&E",
        "laplacian_blur_var": 1100.0,
        "tissue_fold_pct": 1.0,
        "pen_mark_detected": False,
        "air_bubble_pct": 0.0
    }
    res = test_client.post("/api/slides/qc", json=qc_payload)
    assert res.status_code == 200
    assert res.json()["qc_grade"] == "PASS"

    # 4. Submit Synoptic Report
    syn_payload = {
        "case_id": case_id,
        "protocol_type": "BREAST_INVASIVE",
        "primary_tumor_site": "Left Breast",
        "histologic_type": "Invasive Ductal Carcinoma",
        "histologic_grade": "Grade 2",
        "tumor_size_cm": 2.1,
        "deep_margin_mm": 4.5,
        "radial_margin_mm": 3.0,
        "nodes_examined": 10,
        "nodes_positive": 0,
        "pT": "pT2",
        "pN": "pN0",
        "biomarkers": {"ER": "Positive", "PR": "Positive", "HER2": "Negative"}
    }
    res = test_client.post("/api/synoptic/validate", json=syn_payload)
    assert res.status_code == 200
    assert res.json()["validation_status"] == "VALID"

    # 5. Orchestrate Case Review
    res = test_client.post(f"/api/cases/{case_id}/review")
    assert res.status_code == 200
    assert res.json()["overall_status"] == "SUPERVISOR_APPROVED"

    # 6. Check Audit Trail & Integrity
    res = test_client.get("/api/audit")
    assert res.status_code == 200
    assert len(res.json()) > 0

    res = test_client.get("/api/audit/verify")
    assert res.status_code == 200
    assert res.json()["integrity_verified"] is True

    # 7. Chat endpoint
    res = test_client.post("/api/chat", json={"query": "Is Case S26-7777 approved?", "case_id": case_id})
    assert res.status_code == 200
    assert "response" in res.json()


def test_fastapi_blocks_phi(test_client):
    payload = {
        "accession_id_anonymized": "S26-LEAK-01",
        "organ_system": "BREAST",
        "specimen_type": "Lumpectomy",
        "clinical_history_anonymized": "Patient SSN is 111-22-3333 with DOB 04/12/1980"
    }
    res = test_client.post("/api/cases", json=payload)
    assert res.status_code == 400
    assert res.json()["error"] == "SECURITY_PHI_VIOLATION"
