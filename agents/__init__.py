"""
PathMind-Atlas: Autonomous Distributed Component Surgical Pathology QC & Synoptic Validation Engine.
Architect: Dr. Abu Suraih Sakhri (@abusuraihsakhri)
"""

from agents.base import (
    assert_no_phi,
    SecurityException,
    HMACSigner,
    AuditLogger,
    ActionExecutor,
    secure_invoke,
    AgentRole,
    ActorTier
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
    AlertSeverity,
    get_engine,
    get_session,
    init_db
)
from agents.llm_factory import LLMFactory, LLMProvider, LLMResponse
from agents.qc_worker import SlideQCAgent, SlideQCResult, WSIMetrics
from agents.synoptic_worker import SynopticValidatorAgent, SynopticValidationResult, CAPProtocol
from agents.supervisor import PathMindSupervisor, CaseReviewSummary

__version__ = "1.0.0"
__all__ = [
    "assert_no_phi",
    "SecurityException",
    "HMACSigner",
    "AuditLogger",
    "ActionExecutor",
    "secure_invoke",
    "AgentRole",
    "ActorTier",
    "Base",
    "Case",
    "Slide",
    "SynopticReport",
    "Alert",
    "AuditEvent",
    "Action",
    "CaseStatus",
    "QCGrade",
    "AlertSeverity",
    "get_engine",
    "get_session",
    "init_db",
    "LLMFactory",
    "LLMProvider",
    "LLMResponse",
    "SlideQCAgent",
    "SlideQCResult",
    "WSIMetrics",
    "SynopticValidatorAgent",
    "SynopticValidationResult",
    "CAPProtocol",
    "PathMindSupervisor",
    "CaseReviewSummary"
]
