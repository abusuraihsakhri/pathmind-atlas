"""
PathMind-Atlas SQLAlchemy Database Models & Schemas.
Architect: Dr. Abu Suraih Sakhri (@abusuraihsakhri)
"""

from __future__ import annotations
import os
import json
import uuid
from functools import lru_cache
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any, List

from sqlalchemy import (
    create_engine,
    Column,
    String,
    Float,
    Integer,
    Boolean,
    DateTime,
    Text,
    ForeignKey,
    Enum as SQLEnum
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session

Base = declarative_base()


# ---------------------------------------------------------
# Enums
# ---------------------------------------------------------
class CaseStatus(str, Enum):
    PENDING_QC = "PENDING_QC"
    QC_REJECTED = "QC_REJECTED"
    QC_PASSED = "QC_PASSED"
    IN_SYNOPTIC_REVIEW = "IN_SYNOPTIC_REVIEW"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    SUPERVISOR_APPROVED = "SUPERVISOR_APPROVED"
    CRITICAL_ALERT = "CRITICAL_ALERT"
    COMPLETED = "COMPLETED"


class QCGrade(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    REJECT = "REJECT"


class AlertSeverity(str, Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    PANIC = "PANIC"


class MarginStatus(str, Enum):
    NEGATIVE = "NEGATIVE"
    CLOSE = "CLOSE"  # Typically <= 1mm or <= 2mm depending on organ
    POSITIVE = "POSITIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ValidationStatus(str, Enum):
    VALID = "VALID"
    INCOMPLETE = "INCOMPLETE"
    DISCORDANT = "DISCORDANT"
    CRITICAL_FLAG = "CRITICAL_FLAG"


# ---------------------------------------------------------
# Database Tables
# ---------------------------------------------------------
class Case(Base):
    __tablename__ = "cases"

    case_id = Column(String(64), primary_key=True, default=lambda: f"CASE-{uuid.uuid4().hex[:8].upper()}")
    accession_id_anonymized = Column(String(64), unique=True, nullable=False, index=True)
    organ_system = Column(String(64), nullable=False, default="GENERAL")
    specimen_type = Column(String(128), nullable=False)
    clinical_history_anonymized = Column(Text, nullable=True)
    status = Column(SQLEnum(CaseStatus), default=CaseStatus.PENDING_QC, nullable=False)
    assigned_pathologist_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    slides = relationship("Slide", back_populates="case", cascade="all, delete-orphan")
    synoptic_reports = relationship("SynopticReport", back_populates="case", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="case", cascade="all, delete-orphan")
    actions = relationship("Action", back_populates="case", cascade="all, delete-orphan")


class Slide(Base):
    __tablename__ = "slides"

    slide_id = Column(String(64), primary_key=True, default=lambda: f"SLD-{uuid.uuid4().hex[:8].upper()}")
    case_id = Column(String(64), ForeignKey("cases.case_id"), nullable=False, index=True)
    block_id = Column(String(32), nullable=False, default="A1")
    stain_type = Column(String(32), nullable=False, default="H&E")
    resolution_mpp = Column(Float, nullable=False, default=0.25)  # Microns per pixel (40x: ~0.25, 20x: ~0.50)
    
    # Quantitative QC Metrics
    laplacian_blur_var = Column(Float, nullable=False, default=1200.0)  # High = sharp, < 300 = blurred
    tissue_fold_pct = Column(Float, nullable=False, default=0.0)       # Area % covered by folds
    pen_mark_detected = Column(Boolean, nullable=False, default=False)
    air_bubble_pct = Column(Float, nullable=False, default=0.0)
    stain_intensity_uniformity = Column(Float, nullable=False, default=0.95)
    
    # QC Assessment
    qc_grade = Column(SQLEnum(QCGrade), default=QCGrade.PASS, nullable=False)
    qc_score = Column(Float, nullable=False, default=100.0)  # 0 to 100
    qc_comments = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    case = relationship("Case", back_populates="slides")


class SynopticReport(Base):
    __tablename__ = "synoptic_reports"

    report_id = Column(String(64), primary_key=True, default=lambda: f"SYN-{uuid.uuid4().hex[:8].upper()}")
    case_id = Column(String(64), ForeignKey("cases.case_id"), nullable=False, index=True)
    protocol_type = Column(String(64), nullable=False)  # BREAST, COLORECTAL, PROSTATE, LUNG, MELANOMA, etc.
    primary_tumor_site = Column(String(128), nullable=False)
    histologic_type = Column(String(128), nullable=False)
    histologic_grade = Column(String(64), nullable=True)  # e.g., Grade 1, 2, 3 or Gleason 4+3=7
    tumor_size_cm = Column(Float, nullable=True)
    
    # Margin evaluation
    deep_margin_mm = Column(Float, nullable=True)
    radial_margin_mm = Column(Float, nullable=True)
    mucosal_margin_mm = Column(Float, nullable=True)
    cervical_margin_mm = Column(Float, nullable=True)
    closest_margin_name = Column(String(64), nullable=True)
    closest_margin_distance_mm = Column(Float, nullable=True)
    margin_status = Column(SQLEnum(MarginStatus), default=MarginStatus.NEGATIVE, nullable=False)
    
    # Staging & Invasions
    nodes_examined = Column(Integer, nullable=True, default=0)
    nodes_positive = Column(Integer, nullable=True, default=0)
    pT = Column(String(16), nullable=True)
    pN = Column(String(16), nullable=True)
    pM = Column(String(16), nullable=True)
    lvi_present = Column(Boolean, nullable=True, default=False)
    pni_present = Column(Boolean, nullable=True, default=False)
    
    # Structured JSON stores for biomarkers and protocol discrepancies
    biomarkers_json = Column(Text, nullable=False, default="{}")
    protocol_violations_json = Column(Text, nullable=False, default="[]")
    
    # Status
    validation_status = Column(SQLEnum(ValidationStatus), default=ValidationStatus.VALID, nullable=False)
    completeness_score = Column(Float, nullable=False, default=100.0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    case = relationship("Case", back_populates="synoptic_reports")

    @property
    def biomarkers(self) -> Dict[str, Any]:
        try:
            return json.loads(self.biomarkers_json)
        except Exception:
            return {}

    @biomarkers.setter
    def biomarkers(self, value: Dict[str, Any]):
        self.biomarkers_json = json.dumps(value)

    @property
    def protocol_violations(self) -> List[str]:
        try:
            return json.loads(self.protocol_violations_json)
        except Exception:
            return []

    @protocol_violations.setter
    def protocol_violations(self, value: List[str]):
        self.protocol_violations_json = json.dumps(value)


class Alert(Base):
    __tablename__ = "alerts"

    alert_id = Column(String(64), primary_key=True, default=lambda: f"ALT-{uuid.uuid4().hex[:8].upper()}")
    case_id = Column(String(64), ForeignKey("cases.case_id"), nullable=False, index=True)
    slide_id = Column(String(64), nullable=True)
    report_id = Column(String(64), nullable=True)
    severity = Column(SQLEnum(AlertSeverity), default=AlertSeverity.WARNING, nullable=False)
    category = Column(String(64), nullable=False)  # QC_BLUR, POSITIVE_MARGIN, MISSING_BIOMARKER, STAGE_MISMATCH
    message = Column(Text, nullable=False)
    is_acknowledged = Column(Boolean, default=False, nullable=False)
    acknowledged_by = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    case = relationship("Case", back_populates="alerts")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    event_id = Column(String(64), primary_key=True)
    timestamp = Column(String(64), nullable=False)
    actor_tier = Column(String(32), nullable=False)
    agent_role = Column(String(64), nullable=False)
    action_name = Column(String(64), nullable=False)
    target_resource_id = Column(String(64), nullable=False)
    details_json = Column(Text, nullable=False)
    prev_hash = Column(String(64), nullable=False)
    current_hash = Column(String(64), nullable=False)
    hmac_signature = Column(String(64), nullable=False)


class Action(Base):
    __tablename__ = "actions"

    action_id = Column(String(64), primary_key=True, default=lambda: f"ACT-{uuid.uuid4().hex[:8].upper()}")
    case_id = Column(String(64), ForeignKey("cases.case_id"), nullable=False, index=True)
    agent_name = Column(String(64), nullable=False)
    action_type = Column(String(64), nullable=False)
    status = Column(String(32), default="SUCCESS", nullable=False)
    payload_json = Column(Text, nullable=False, default="{}")
    result_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    case = relationship("Case", back_populates="actions")


# ---------------------------------------------------------
# Database Initialization & Session Provider
# ---------------------------------------------------------
@lru_cache(maxsize=16)
def _engine_for_url(url: str):
    """Reuse connection pools for a given database URL."""
    return create_engine(
        url,
        echo=False,
        connect_args={"check_same_thread": False} if url.startswith("sqlite") else {}
    )


def get_engine(db_url: Optional[str] = None):
    url = db_url or os.getenv("PATHMIND_DATABASE_URL", "sqlite:///pathmind_atlas.db")
    return _engine_for_url(url)



def get_session(engine=None) -> Session:
    eng = engine or get_engine()
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    return SessionLocal()


def init_db(engine=None) -> None:
    eng = engine or get_engine()
    Base.metadata.create_all(bind=eng)
