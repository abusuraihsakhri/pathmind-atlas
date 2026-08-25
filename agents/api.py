"""
PathMind-Atlas FastAPI REST API (0.111+).
Exposes endpoints for Case Management, Slide QC, Synoptic Validation, Cryptographic Audit Trails, Prometheus Metrics, and Chat.
Architect: Dr. Abu Suraih Sakhri (@abusuraihsakhri)
"""

from __future__ import annotations
import json
import logging
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends, status
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from prometheus_client import Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _DummyMetric:
        def __init__(self, *args, **kwargs):
            self._labels = {}
            self._value = 0

        def inc(self, amount=1):
            self._value += amount

        def set(self, val):
            self._value = val

        def labels(self, **kwargs):
            return self

    Counter = Gauge = _DummyMetric

    def generate_latest():
        return b"# Prometheus metrics not installed in environment\n"


from agents.base import (
    assert_no_phi,
    SecurityException,
    ActorTier,
    AgentRole,
    default_audit_logger,
    AuditEventSchema
)
from agents.models import (
    init_db,
    get_session,
    Case,
    Slide,
    SynopticReport,
    Alert,
    CaseStatus,
    QCGrade,
    ValidationStatus,
    MarginStatus,
    AlertSeverity
)
from agents.qc_worker import SlideQCAgent, WSIMetrics, SlideQCResult
from agents.synoptic_worker import SynopticValidatorAgent, SynopticReportInput, SynopticValidationResult, CAPProtocol
from agents.supervisor import PathMindSupervisor, CaseReviewSummary
from agents.llm_factory import LLMFactory

logger = logging.getLogger("pathmind.api")

# ---------------------------------------------------------
# Prometheus Metrics
# ---------------------------------------------------------
METRIC_QC_PROCESSED = Counter("pathmind_qc_slides_total", "Total whole slide images analyzed for QC", ["grade"])
METRIC_SYNOPTIC_PROCESSED = Counter("pathmind_synoptic_reports_total", "Total synoptic reports validated", ["protocol", "status"])
METRIC_PHI_BLOCKED = Counter("pathmind_phi_blocked_total", "Total outbound/inbound requests blocked by PHI guardrail")
METRIC_ACTIVE_CASES = Gauge("pathmind_active_cases", "Total pathology cases in workflow")


# ---------------------------------------------------------
# App Lifespan & Initialization
# ---------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB tables
    init_db()
    logger.info("PathMind Atlas database and tables initialized.")
    yield


app = FastAPI(
    title="PathMind-Atlas",
    description="Autonomous Distributed Component Surgical Pathology Quality Control, Synoptic Reporting & Cryptographic Audit System",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global instances
supervisor = PathMindSupervisor(audit_logger=default_audit_logger)


# ---------------------------------------------------------
# Exception Handlers
# ---------------------------------------------------------
@app.exception_handler(SecurityException)
async def security_exception_handler(request: Request, exc: SecurityException):
    METRIC_PHI_BLOCKED.inc()
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "error": "SECURITY_PHI_VIOLATION",
            "message": str(exc),
            "detected_patterns": exc.detected_patterns
        }
    )


# ---------------------------------------------------------
# Schemas
# ---------------------------------------------------------
class CaseCreateRequest(BaseModel):
    accession_id_anonymized: str = Field(..., description="Anonymized surgical pathology accession number (e.g. S26-10492)")
    organ_system: str = Field(default="BREAST", description="Organ system e.g. BREAST, GI, PROSTATE, LUNG, SKIN")
    specimen_type: str = Field(..., description="E.g. Right Breast Lumpectomy, Colon Right Hemicolectomy")
    clinical_history_anonymized: Optional[str] = None
    assigned_pathologist_id: Optional[str] = "PATH_AUTO_01"


class ChatRequest(BaseModel):
    query: str
    case_id: Optional[str] = None


# ---------------------------------------------------------
# Endpoints
# ---------------------------------------------------------
@app.get("/health", tags=["System"])
def health():
    return {
        "status": "HEALTHY",
        "system": "PathMind-Atlas",
        "version": "1.0.0",
        "active_agents": ["PathMindSupervisor", "SlideQCAgent", "SynopticValidatorAgent", "CryptographicAuditor"],
        "phi_guard": "STRICT_ACTIVE",
        "hmac_audit": "ENABLED"
    }


@app.get("/metrics", tags=["System"])
def metrics():
    # Update active case count gauge
    try:
        session = get_session()
        count = session.query(Case).count()
        METRIC_ACTIVE_CASES.set(count)
        session.close()
    except Exception:
        pass
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------
# Case Management
# ---------------------------------------------------------
@app.post("/api/cases", status_code=status.HTTP_201_CREATED, tags=["Cases"])
def create_case(req: CaseCreateRequest):
    assert_no_phi(req.model_dump())
    session = get_session()
    
    existing = session.query(Case).filter(Case.accession_id_anonymized == req.accession_id_anonymized).first()
    if existing:
        session.close()
        raise HTTPException(status_code=400, detail=f"Accession ID '{req.accession_id_anonymized}' already exists.")

    new_case = Case(
        accession_id_anonymized=req.accession_id_anonymized,
        organ_system=req.organ_system.upper(),
        specimen_type=req.specimen_type,
        clinical_history_anonymized=req.clinical_history_anonymized,
        assigned_pathologist_id=req.assigned_pathologist_id,
        status=CaseStatus.PENDING_QC
    )
    session.add(new_case)
    session.commit()
    session.refresh(new_case)

    # Log to cryptographic audit
    default_audit_logger.log_event(
        actor_tier=ActorTier.CLINICIAN,
        agent_role=AgentRole.SUPERVISOR,
        action_name="create_case",
        target_resource_id=new_case.case_id,
        details={"accession_id": req.accession_id_anonymized, "organ_system": req.organ_system}
    )

    result = {
        "case_id": new_case.case_id,
        "accession_id_anonymized": new_case.accession_id_anonymized,
        "organ_system": new_case.organ_system,
        "specimen_type": new_case.specimen_type,
        "status": new_case.status.value,
        "created_at": new_case.created_at.isoformat() if new_case.created_at else None
    }
    session.close()
    return result


@app.get("/api/cases", tags=["Cases"])
def list_cases():
    session = get_session()
    cases = session.query(Case).order_by(Case.created_at.desc()).all()
    results = [
        {
            "case_id": c.case_id,
            "accession_id_anonymized": c.accession_id_anonymized,
            "organ_system": c.organ_system,
            "specimen_type": c.specimen_type,
            "status": c.status.value,
            "slides_count": len(c.slides),
            "synoptic_reports_count": len(c.synoptic_reports),
            "alerts_count": len(c.alerts),
            "created_at": c.created_at.isoformat() if c.created_at else None
        }
        for c in cases
    ]
    session.close()
    return results


@app.get("/api/cases/{case_id}", tags=["Cases"])
def get_case_detail(case_id: str):
    assert_no_phi(case_id)
    session = get_session()
    c = session.query(Case).filter((Case.case_id == case_id) | (Case.accession_id_anonymized == case_id)).first()
    if not c:
        session.close()
        raise HTTPException(status_code=404, detail="Case not found.")

    res = {
        "case_id": c.case_id,
        "accession_id_anonymized": c.accession_id_anonymized,
        "organ_system": c.organ_system,
        "specimen_type": c.specimen_type,
        "status": c.status.value,
        "clinical_history": c.clinical_history_anonymized,
        "assigned_pathologist_id": c.assigned_pathologist_id,
        "slides": [
            {
                "slide_id": s.slide_id,
                "block_id": s.block_id,
                "stain_type": s.stain_type,
                "laplacian_blur_var": s.laplacian_blur_var,
                "tissue_fold_pct": s.tissue_fold_pct,
                "pen_mark_detected": s.pen_mark_detected,
                "air_bubble_pct": s.air_bubble_pct,
                "qc_grade": s.qc_grade.value,
                "qc_score": s.qc_score,
                "qc_comments": s.qc_comments
            }
            for s in c.slides
        ],
        "synoptic_reports": [
            {
                "report_id": r.report_id,
                "protocol_type": r.protocol_type,
                "primary_tumor_site": r.primary_tumor_site,
                "histologic_type": r.histologic_type,
                "histologic_grade": r.histologic_grade,
                "tumor_size_cm": r.tumor_size_cm,
                "closest_margin_name": r.closest_margin_name,
                "closest_margin_distance_mm": r.closest_margin_distance_mm,
                "margin_status": r.margin_status.value,
                "staging": f"{r.pT} {r.pN} {r.pM}",
                "validation_status": r.validation_status.value,
                "completeness_score": r.completeness_score,
                "protocol_violations": r.protocol_violations
            }
            for r in c.synoptic_reports
        ],
        "alerts": [
            {
                "alert_id": a.alert_id,
                "severity": a.severity.value,
                "category": a.category,
                "message": a.message,
                "is_acknowledged": a.is_acknowledged
            }
            for a in c.alerts
        ]
    }
    session.close()
    return res


# ---------------------------------------------------------
# Slide QC Endpoints
# ---------------------------------------------------------
@app.post("/api/slides/qc", response_model=SlideQCResult, tags=["Quality Control"])
def evaluate_slide_qc(metrics: WSIMetrics):
    result = supervisor.qc_worker.evaluate_slide(metrics, persist_to_db=True)
    METRIC_QC_PROCESSED.labels(grade=result.qc_grade.value).inc()
    return result


# ---------------------------------------------------------
# Synoptic Validation Endpoints
# ---------------------------------------------------------
@app.post("/api/synoptic/validate", response_model=SynopticValidationResult, tags=["Synoptic Reporting"])
def validate_synoptic_report(report_input: SynopticReportInput):
    result = supervisor.synoptic_worker.validate_report(report_input, persist_to_db=True)
    METRIC_SYNOPTIC_PROCESSED.labels(
        protocol=result.protocol_type.value,
        status=result.validation_status.value
    ).inc()
    return result


# ---------------------------------------------------------
# Case Review Supervisor Orchestration
# ---------------------------------------------------------
@app.post("/api/cases/{case_id}/review", response_model=CaseReviewSummary, tags=["Supervisor"])
def review_case_endpoint(case_id: str):
    return supervisor.review_case(case_id)


# ---------------------------------------------------------
# Cryptographic Audit Trail Endpoints
# ---------------------------------------------------------
@app.get("/api/audit", response_model=List[AuditEventSchema], tags=["Audit Trail"])
def get_audit_trail():
    return default_audit_logger.get_events()


@app.get("/api/audit/verify", tags=["Audit Trail"])
def verify_audit_integrity():
    is_valid, reason = default_audit_logger.verify_integrity()
    return {
        "integrity_verified": is_valid,
        "total_events": len(default_audit_logger.get_events()),
        "last_hash": default_audit_logger.last_hash,
        "detail": reason or "Cryptographic chain and all HMAC signatures are authentic and unmodified."
    }


# ---------------------------------------------------------
# Operations Chat Endpoint
# ---------------------------------------------------------
@app.post("/api/chat", tags=["Chat"])
def chat_endpoint(req: ChatRequest):
    reply = supervisor.chat_query(user_query=req.query, case_id=req.case_id)
    return {"query": req.query, "case_id": req.case_id, "response": reply}
