"""
PathMind-Atlas Supervisor Agent (PathMindSupervisor).
Orchestrates multi-worker case reviews, enforces zero-PHI memory, and powers operations chat.
Architect: Dr. Abu Suraih Sakhri (@abusuraihsakhri)
"""

from __future__ import annotations
import json
import logging
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field

from agents.base import (
    assert_no_phi,
    ActorTier,
    AgentRole,
    secure_invoke,
    AuditLogger,
    default_audit_logger
)
from agents.models import (
    Case,
    Slide,
    SynopticReport,
    Alert,
    CaseStatus,
    QCGrade,
    ValidationStatus,
    MarginStatus,
    AlertSeverity,
    get_session
)
from agents.llm_factory import LLMFactory
from agents.qc_worker import SlideQCAgent, WSIMetrics, SlideQCResult
from agents.synoptic_worker import SynopticValidatorAgent, SynopticReportInput, SynopticValidationResult

logger = logging.getLogger("pathmind.supervisor")


class CaseReviewSummary(BaseModel):
    case_id: str
    accession_id_anonymized: str
    overall_status: CaseStatus
    slides_reviewed_count: int
    qc_pass_count: int
    qc_reject_count: int
    synoptic_reports_count: int
    validation_status: ValidationStatus
    margin_status: MarginStatus
    critical_alerts_count: int
    active_alerts: List[Dict[str, Any]]
    supervisor_consensus: str
    recommended_action: str


class PathMindSupervisor:
    """
    Supervisor Orchestrator for PathMind Atlas distributed component system.
    """
    def __init__(
        self,
        llm_factory: Optional[LLMFactory] = None,
        audit_logger: Optional[AuditLogger] = None
    ):
        self.llm = llm_factory or LLMFactory()
        self.audit_logger = audit_logger or default_audit_logger
        self.qc_worker = SlideQCAgent(llm_factory=self.llm, audit_logger=self.audit_logger)
        self.synoptic_worker = SynopticValidatorAgent(llm_factory=self.llm, audit_logger=self.audit_logger)

    def review_case(self, case_id: str) -> CaseReviewSummary:
        """
        Executes end-to-end distributed component case orchestration.
        """
        return secure_invoke(
            agent_role=AgentRole.SUPERVISOR,
            action_name="orchestrate_case_review",
            target_id=case_id,
            func=self._review_case_internal,
            case_id=case_id,
            actor_tier=ActorTier.AGENT,
            audit_logger=self.audit_logger
        )

    def _review_case_internal(self, case_id: str) -> CaseReviewSummary:
        assert_no_phi(case_id)
        session = get_session()

        db_case = session.query(Case).filter(Case.case_id == case_id).first()
        if not db_case:
            # Also try matching accession id
            db_case = session.query(Case).filter(Case.accession_id_anonymized == case_id).first()

        if not db_case:
            session.close()
            raise ValueError(f"Case '{case_id}' not found in database.")

        slides = session.query(Slide).filter(Slide.case_id == db_case.case_id).all()
        synoptic_reports = session.query(SynopticReport).filter(SynopticReport.case_id == db_case.case_id).all()
        alerts = session.query(Alert).filter(Alert.case_id == db_case.case_id, Alert.is_acknowledged == False).all()

        # Tally QC
        qc_pass = sum(1 for s in slides if s.qc_grade == QCGrade.PASS)
        qc_reject = sum(1 for s in slides if s.qc_grade == QCGrade.REJECT)
        qc_warn = sum(1 for s in slides if s.qc_grade == QCGrade.WARN)

        # Tally Synoptic
        val_status = ValidationStatus.VALID
        m_status = MarginStatus.NEGATIVE

        for sr in synoptic_reports:
            if sr.validation_status == ValidationStatus.CRITICAL_FLAG:
                val_status = ValidationStatus.CRITICAL_FLAG
            elif sr.validation_status == ValidationStatus.DISCORDANT and val_status != ValidationStatus.CRITICAL_FLAG:
                val_status = ValidationStatus.DISCORDANT
            elif sr.validation_status == ValidationStatus.INCOMPLETE and val_status not in [ValidationStatus.CRITICAL_FLAG, ValidationStatus.DISCORDANT]:
                val_status = ValidationStatus.INCOMPLETE

            if sr.margin_status == MarginStatus.POSITIVE:
                m_status = MarginStatus.POSITIVE
            elif sr.margin_status == MarginStatus.CLOSE and m_status != MarginStatus.POSITIVE:
                m_status = MarginStatus.CLOSE

        crit_alerts = [
            {
                "alert_id": a.alert_id,
                "severity": a.severity.value if hasattr(a.severity, "value") else str(a.severity),
                "category": a.category,
                "message": a.message
            }
            for a in alerts
        ]

        # Determine overall case status
        if any(a["severity"] in ["CRITICAL", "PANIC"] for a in crit_alerts):
            case_status = CaseStatus.CRITICAL_ALERT
            recommended = "HOLD CASE: Critical alerts detected. Address slide optical defects and/or synoptic margin/staging flags immediately."
        elif val_status == ValidationStatus.CRITICAL_FLAG or m_status == MarginStatus.POSITIVE:
            case_status = CaseStatus.CRITICAL_ALERT
            recommended = "HOLD CASE: Critical synoptic findings require pathologist review."
        elif qc_reject > 0:
            case_status = CaseStatus.QC_REJECTED
            recommended = "HOLD CASE: One or more Whole Slide Images failed QC. Request slide recut or optical rescan."
        elif qc_warn > 0:
            case_status = CaseStatus.QC_REJECTED
            recommended = "HOLD CASE: One or more slides require manual QC review before approval."
        elif not slides or not synoptic_reports:
            case_status = CaseStatus.VALIDATION_FAILED
            recommended = "HOLD CASE: Slide QC and at least one synoptic report are required for supervisor approval."
        elif val_status in [ValidationStatus.DISCORDANT, ValidationStatus.INCOMPLETE]:
            case_status = CaseStatus.VALIDATION_FAILED
            recommended = "SYNOPTIC REVIEW REQUIRED: Incomplete or discordant CAP core elements detected."
        else:
            case_status = CaseStatus.SUPERVISOR_APPROVED
            recommended = "APPROVED: Case passed all QC thresholds and CAP cancer protocol synoptic criteria."

        # Update DB case status
        db_case.status = case_status
        session.commit()

        # Generate consensus narrative via LLM
        prompt = (
            f"Synthesize supervisor pathology consensus for Case {db_case.case_id} ({db_case.accession_id_anonymized}):\n"
            f"- Organ System: {db_case.organ_system}, Specimen: {db_case.specimen_type}\n"
            f"- Slides: {len(slides)} total ({qc_pass} Pass, {qc_reject} Reject)\n"
            f"- Synoptic Reports: {len(synoptic_reports)} total (Status: {val_status.value}, Margin: {m_status.value})\n"
            f"- Active Alerts: {len(crit_alerts)}\n"
            f"- Overall Status: {case_status.value}"
        )
        llm_resp = self.llm.generate(prompt=prompt, system_prompt="You are the Lead Pathologist Supervisor AI.")
        consensus_text = llm_resp.content

        summary = CaseReviewSummary(
            case_id=db_case.case_id,
            accession_id_anonymized=db_case.accession_id_anonymized,
            overall_status=case_status,
            slides_reviewed_count=len(slides),
            qc_pass_count=qc_pass,
            qc_reject_count=qc_reject,
            synoptic_reports_count=len(synoptic_reports),
            validation_status=val_status,
            margin_status=m_status,
            critical_alerts_count=len(crit_alerts),
            active_alerts=crit_alerts,
            supervisor_consensus=consensus_text,
            recommended_action=recommended
        )

        session.close()
        return summary

    def chat_query(self, user_query: str, case_id: Optional[str] = None) -> str:
        """
        Interactive clinical assistant query with strict zero-PHI checks.
        """
        return secure_invoke(
            agent_role=AgentRole.SUPERVISOR,
            action_name="operations_chat_query",
            target_id=case_id or "GENERAL_QUERY",
            func=self._chat_internal,
            user_query=user_query,
            case_id=case_id,
            actor_tier=ActorTier.CLINICIAN,
            audit_logger=self.audit_logger
        )

    def _chat_internal(self, user_query: str, case_id: Optional[str] = None) -> str:
        assert_no_phi(user_query)
        context_lines = ["PathMind Atlas Real-Time Pathology Operations Context:"]

        if case_id:
            session = get_session()
            db_case = session.query(Case).filter((Case.case_id == case_id) | (Case.accession_id_anonymized == case_id)).first()
            if db_case:
                context_lines.append(f"- Case: {db_case.case_id} (Accession: {db_case.accession_id_anonymized})")
                context_lines.append(f"- Organ: {db_case.organ_system}, Specimen: {db_case.specimen_type}, Status: {db_case.status.value}")
                
                slides = session.query(Slide).filter(Slide.case_id == db_case.case_id).all()
                context_lines.append(f"- Slides ({len(slides)}): {', '.join([f'{s.slide_id}[{s.qc_grade.value}]' for s in slides])}")
                
                reports = session.query(SynopticReport).filter(SynopticReport.case_id == db_case.case_id).all()
                for r in reports:
                    context_lines.append(f"- Synoptic [{r.protocol_type}]: Status={r.validation_status.value}, Margin={r.margin_status.value}, pT={r.pT}, pN={r.pN}")
                
                alerts = session.query(Alert).filter(Alert.case_id == db_case.case_id, Alert.is_acknowledged == False).all()
                if alerts:
                    context_lines.append(f"- Active Alerts ({len(alerts)}): {'; '.join([f'[{a.severity.value}] {a.message}' for a in alerts])}")
            session.close()

        system_prompt = (
            "You are PathMind Supervisor, an AI surgical pathology assistant. "
            "Provide concise, evidence-based pathology QC and synoptic compliance guidance. "
            "Never generate or repeat PHI."
        )
        full_prompt = f"{chr(10).join(context_lines)}\n\nClinician Question: {user_query}\n\nPathology Response:"
        resp = self.llm.generate(prompt=full_prompt, system_prompt=system_prompt)
        return resp.content
