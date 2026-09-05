"""
PathMind-Atlas Slide Quality Control Agent (SlideQCAgent).
Evaluates Whole Slide Image (WSI) blur variance, tissue folds, air bubbles, pen marks, and staining artifacts.
Architect: Dr. Abu Suraih Sakhri (@abusuraihsakhri)
"""

from __future__ import annotations
import json
import logging
import uuid
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
    Alert,
    QCGrade,
    AlertSeverity,
    get_session
)
from agents.llm_factory import LLMFactory

logger = logging.getLogger("pathmind.qc_worker")


class WSIMetrics(BaseModel):
    slide_id: Optional[str] = None
    case_id: str
    block_id: str = "A1"
    stain_type: str = "H&E"
    resolution_mpp: float = 0.25
    laplacian_blur_var: float = Field(default=1200.0, description="Variance of Laplacian focus score. <300 = blurred.")
    tissue_fold_pct: float = Field(default=0.0, description="Percentage of tissue area occluded by folds.")
    pen_mark_detected: bool = Field(default=False, description="Marker or ink artifact obscuring tissue.")
    air_bubble_pct: float = Field(default=0.0, description="Coverslip air bubble coverage %.")
    stain_intensity_uniformity: float = Field(default=0.95, description="Uniformity of H&E/IHC chromogen (0 to 1).")


class SlideQCResult(BaseModel):
    slide_id: str
    case_id: str
    qc_grade: QCGrade
    qc_score: float
    flags: List[str]
    narrative_assessment: str
    recommended_action: str
    alerts_generated: List[Dict[str, Any]] = Field(default_factory=list)


class SlideQCAgent:
    """
    Autonomous Whole Slide Imaging (WSI) Quality Control Agent.
    """
    def __init__(self, llm_factory: Optional[LLMFactory] = None, audit_logger: Optional[AuditLogger] = None):
        self.llm = llm_factory or LLMFactory()
        self.audit_logger = audit_logger or default_audit_logger

    def evaluate_slide(self, metrics: WSIMetrics, persist_to_db: bool = True) -> SlideQCResult:
        """
        Executes secured slide quality control evaluation.
        """
        return secure_invoke(
            agent_role=AgentRole.QC_WORKER,
            action_name="evaluate_slide_qc",
            target_id=metrics.slide_id or f"NEW_SLIDE_{metrics.case_id}",
            func=self._evaluate_internal,
            metrics=metrics,
            persist_to_db=persist_to_db,
            actor_tier=ActorTier.AGENT,
            audit_logger=self.audit_logger
        )

    def _evaluate_internal(self, metrics: WSIMetrics, persist_to_db: bool = True) -> SlideQCResult:
        assert_no_phi(metrics.model_dump())

        flags: List[str] = []
        score = 100.0
        alerts: List[Dict[str, Any]] = []

        # 1. Blur evaluation (Laplacian Variance)
        if metrics.laplacian_blur_var < 250.0:
            flags.append("SEVERE_OUT_OF_FOCUS_BLUR")
            score -= 45.0
            alerts.append({
                "severity": AlertSeverity.CRITICAL,
                "category": "QC_BLUR",
                "message": f"Critical out-of-focus blur detected on slide {metrics.slide_id or 'NEW'} (Laplacian var: {metrics.laplacian_blur_var:.1f} < 250.0). Rescan required."
            })
        elif metrics.laplacian_blur_var < 550.0:
            flags.append("MODERATE_FOCUS_HAZINESS")
            score -= 20.0
            alerts.append({
                "severity": AlertSeverity.WARNING,
                "category": "QC_BLUR",
                "message": f"Sub-optimal focus detected on slide {metrics.slide_id or 'NEW'} (Laplacian var: {metrics.laplacian_blur_var:.1f})."
            })

        # 2. Tissue fold evaluation
        if metrics.tissue_fold_pct >= 12.0:
            flags.append("EXTENSIVE_TISSUE_FOLDING")
            score -= 40.0
            alerts.append({
                "severity": AlertSeverity.CRITICAL,
                "category": "QC_TISSUE_FOLD",
                "message": f"Severe tissue folding ({metrics.tissue_fold_pct:.1f}% area) exceeds 12.0% threshold. Recut requested."
            })
        elif metrics.tissue_fold_pct >= 4.0:
            flags.append("MINOR_TISSUE_FOLDING")
            score -= 15.0
            alerts.append({
                "severity": AlertSeverity.WARNING,
                "category": "QC_TISSUE_FOLD",
                "message": f"Tissue folding ({metrics.tissue_fold_pct:.1f}% area) may impede computer vision patch analysis."
            })

        # 3. Pen/Ink mark artifacts
        if metrics.pen_mark_detected:
            flags.append("PEN_MARK_OBSCURING_TISSUE")
            score -= 25.0
            alerts.append({
                "severity": AlertSeverity.WARNING,
                "category": "QC_PEN_MARK",
                "message": "Gross manual ink/pen marker artifact obscuring histological architecture."
            })

        # 4. Air bubble artifacts
        if metrics.air_bubble_pct >= 8.0:
            flags.append("LARGE_AIR_BUBBLE_ARTIFACT")
            score -= 30.0
            alerts.append({
                "severity": AlertSeverity.CRITICAL,
                "category": "QC_AIR_BUBBLE",
                "message": f"Significant coverslip air bubble entrapped ({metrics.air_bubble_pct:.1f}%). Remount required."
            })
        elif metrics.air_bubble_pct >= 2.5:
            flags.append("MINOR_AIR_BUBBLE")
            score -= 10.0

        # 5. Staining uniformity
        if metrics.stain_intensity_uniformity < 0.65:
            flags.append("POOR_STAIN_UNIFORMITY")
            score -= 25.0
            alerts.append({
                "severity": AlertSeverity.WARNING,
                "category": "QC_STAIN_ARTIFACT",
                "message": f"Uneven chromogen/eosin distribution (uniformity index {metrics.stain_intensity_uniformity:.2f} < 0.65)."
            })

        # Normalize score
        final_score = max(0.0, min(100.0, score))

        # Grade Determination
        if final_score >= 80.0 and not any("CRITICAL" in a["severity"].value if hasattr(a["severity"], "value") else "CRITICAL" in str(a["severity"]) for a in alerts):
            grade = QCGrade.PASS
            recommendation = "Slide meets all CAP/CLIA whole slide imaging quality metrics. Cleared for synoptic pathology analysis."
        elif final_score >= 50.0:
            grade = QCGrade.WARN
            recommendation = "Slide has non-fatal optical/tissue artifacts. Manual pathologist review advised before final sign-out."
        else:
            grade = QCGrade.REJECT
            recommendation = "Slide failed QC thresholds. Action required: Request laboratory slide recut or optical re-scan."

        # Prompt LLM for synthetic clinical explanation
        qc_prompt = (
            f"Generate an expert pathology QC summary for slide '{metrics.slide_id or 'SLD-NEW'}' with metrics:\n"
            f"- Stain: {metrics.stain_type}, Resolution: {metrics.resolution_mpp} mpp\n"
            f"- Laplacian Focus Var: {metrics.laplacian_blur_var:.1f}\n"
            f"- Tissue Fold: {metrics.tissue_fold_pct:.1f}%\n"
            f"- Pen Mark: {metrics.pen_mark_detected}\n"
            f"- Air Bubbles: {metrics.air_bubble_pct:.1f}%\n"
            f"- Overall Grade: {grade.value} (Score: {final_score:.1f}/100)\n"
            f"- Flags: {', '.join(flags) if flags else 'None'}"
        )
        llm_resp = self.llm.generate(prompt=qc_prompt, system_prompt="You are a clinical pathologist assessing digital whole slide image quality.")
        narrative = llm_resp.content

        slide_id = metrics.slide_id or f"SLD-{uuid.uuid4().hex[:8].upper()}"

        # Database Persistence
        if persist_to_db:
            try:
                session = get_session()
                # Resolve case_id if accession number provided
                resolved_case_id = metrics.case_id
                db_c = session.query(Case).filter((Case.case_id == metrics.case_id) | (Case.accession_id_anonymized == metrics.case_id)).first()
                if db_c:
                    resolved_case_id = db_c.case_id

                # Check if slide exists or create new
                db_slide = session.query(Slide).filter(Slide.slide_id == metrics.slide_id).first() if metrics.slide_id else None
                if not db_slide:
                    db_slide = Slide(
                        case_id=resolved_case_id,
                        block_id=metrics.block_id,
                        stain_type=metrics.stain_type,
                        resolution_mpp=metrics.resolution_mpp,
                        laplacian_blur_var=metrics.laplacian_blur_var,
                        tissue_fold_pct=metrics.tissue_fold_pct,
                        pen_mark_detected=metrics.pen_mark_detected,
                        air_bubble_pct=metrics.air_bubble_pct,
                        stain_intensity_uniformity=metrics.stain_intensity_uniformity,
                        qc_grade=grade,
                        qc_score=final_score,
                        qc_comments=narrative
                    )
                    session.add(db_slide)
                    session.flush()
                    slide_id = db_slide.slide_id
                else:
                    db_slide.case_id = resolved_case_id
                    db_slide.laplacian_blur_var = metrics.laplacian_blur_var
                    db_slide.tissue_fold_pct = metrics.tissue_fold_pct
                    db_slide.pen_mark_detected = metrics.pen_mark_detected
                    db_slide.air_bubble_pct = metrics.air_bubble_pct
                    db_slide.stain_intensity_uniformity = metrics.stain_intensity_uniformity
                    db_slide.qc_grade = grade
                    db_slide.qc_score = final_score
                    db_slide.qc_comments = narrative
                    slide_id = db_slide.slide_id

                # Persist alerts
                for alt in alerts:
                    db_alert = Alert(
                        case_id=resolved_case_id,
                        slide_id=slide_id,
                        severity=alt["severity"],
                        category=alt["category"],
                        message=alt["message"]
                    )
                    session.add(db_alert)

                session.commit()
                session.close()
            except Exception as e:
                logger.error(f"Failed to persist slide QC to database: {e}")


        return SlideQCResult(
            slide_id=slide_id,
            case_id=metrics.case_id,
            qc_grade=grade,
            qc_score=final_score,
            flags=flags,
            narrative_assessment=narrative,
            recommended_action=recommendation,
            alerts_generated=alerts
        )
