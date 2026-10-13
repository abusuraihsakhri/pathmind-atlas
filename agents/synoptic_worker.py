"""
PathMind-Atlas Synoptic Validator Agent (SynopticValidatorAgent).
Validates College of American Pathologists (CAP) cancer protocol compliance, margin clearances, and AJCC staging consistency.
Architect: Dr. Abu Suraih Sakhri (@abusuraihsakhri)
"""

from __future__ import annotations
import json
import logging
import uuid
from enum import Enum
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
    SynopticReport,
    Alert,
    ValidationStatus,
    MarginStatus,
    AlertSeverity,
    get_session
)
from agents.llm_factory import LLMFactory

logger = logging.getLogger("pathmind.synoptic_worker")


class CAPProtocol(str, Enum):
    BREAST_INVASIVE = "BREAST_INVASIVE"
    COLORECTAL_RESECTION = "COLORECTAL_RESECTION"
    PROSTATE_RADICAL = "PROSTATE_RADICAL"
    LUNG_RESECTION = "LUNG_RESECTION"
    MELANOMA_EXCISION = "MELANOMA_EXCISION"
    GENERAL_CANCER = "GENERAL_CANCER"


class SynopticReportInput(BaseModel):
    case_id: str
    report_id: Optional[str] = None
    protocol_type: CAPProtocol = CAPProtocol.BREAST_INVASIVE
    primary_tumor_site: str
    histologic_type: str
    histologic_grade: Optional[str] = None
    tumor_size_cm: Optional[float] = None
    
    # Margins
    deep_margin_mm: Optional[float] = None
    radial_margin_mm: Optional[float] = None
    mucosal_margin_mm: Optional[float] = None
    cervical_margin_mm: Optional[float] = None
    closest_margin_name: Optional[str] = None
    closest_margin_distance_mm: Optional[float] = None
    
    # Nodal & AJCC Staging
    nodes_examined: Optional[int] = 0
    nodes_positive: Optional[int] = 0
    pT: Optional[str] = None
    pN: Optional[str] = None
    pM: Optional[str] = None
    
    # Invasions
    lvi_present: Optional[bool] = False
    pni_present: Optional[bool] = False
    
    # Biomarkers
    biomarkers: Dict[str, Any] = Field(default_factory=dict)


class SynopticValidationResult(BaseModel):
    report_id: str
    case_id: str
    protocol_type: CAPProtocol
    validation_status: ValidationStatus
    completeness_score: float
    margin_status: MarginStatus
    closest_margin_summary: str
    staging_summary: str
    protocol_violations: List[str]
    narrative_summary: str
    alerts_generated: List[Dict[str, Any]] = Field(default_factory=list)


class SynopticValidatorAgent:
    """
    Autonomous College of American Pathologists (CAP) Synoptic Protocol & AJCC Staging Validator.
    """
    def __init__(self, llm_factory: Optional[LLMFactory] = None, audit_logger: Optional[AuditLogger] = None):
        self.llm = llm_factory or LLMFactory()
        self.audit_logger = audit_logger or default_audit_logger

    def validate_report(self, report_input: SynopticReportInput, persist_to_db: bool = True) -> SynopticValidationResult:
        """
        Executes secured synoptic report validation.
        """
        return secure_invoke(
            agent_role=AgentRole.SYNOPTIC_WORKER,
            action_name="validate_synoptic_report",
            target_id=report_input.report_id or f"NEW_REPORT_{report_input.case_id}",
            func=self._validate_internal,
            report_input=report_input,
            persist_to_db=persist_to_db,
            actor_tier=ActorTier.AGENT,
            audit_logger=self.audit_logger
        )

    def _validate_internal(self, report_input: SynopticReportInput, persist_to_db: bool = True) -> SynopticValidationResult:
        assert_no_phi(report_input.model_dump())

        violations: List[str] = []
        alerts: List[Dict[str, Any]] = []
        completeness = 100.0

        # 1. Required Core Elements Check
        if not report_input.primary_tumor_site or report_input.primary_tumor_site.strip() == "":
            violations.append("MISSING_PRIMARY_TUMOR_SITE")
            completeness -= 20.0

        if not report_input.histologic_type or report_input.histologic_type.strip() == "":
            violations.append("MISSING_HISTOLOGIC_TYPE")
            completeness -= 20.0

        if not report_input.histologic_grade:
            violations.append("MISSING_HISTOLOGIC_GRADE")
            completeness -= 10.0

        if report_input.tumor_size_cm is None:
            violations.append("MISSING_TUMOR_SIZE")
            completeness -= 15.0

        if not report_input.pT:
            violations.append("MISSING_PATHOLOGIC_T_STAGE")
            completeness -= 15.0

        if not report_input.pN:
            violations.append("MISSING_PATHOLOGIC_N_STAGE")
            completeness -= 15.0

        # 2. Margin Clearance Analysis
        # Determine closest margin distance
        all_margins = []
        if report_input.deep_margin_mm is not None:
            all_margins.append(("Deep Margin", report_input.deep_margin_mm))
        if report_input.radial_margin_mm is not None:
            all_margins.append(("Radial Margin", report_input.radial_margin_mm))
        if report_input.mucosal_margin_mm is not None:
            all_margins.append(("Mucosal Margin", report_input.mucosal_margin_mm))
        if report_input.cervical_margin_mm is not None:
            all_margins.append(("Cervical Margin", report_input.cervical_margin_mm))
        if report_input.closest_margin_distance_mm is not None:
            name = report_input.closest_margin_name or "Closest Specified Margin"
            all_margins.append((name, report_input.closest_margin_distance_mm))

        if not all_margins:
            violations.append("MISSING_MARGIN_MEASUREMENTS")
            margin_status = MarginStatus.NOT_APPLICABLE
            closest_dist = None
            closest_name = "Unknown"
            margin_summary = "No quantitative surgical margin clearance documented."
            completeness -= 15.0
        else:
            sorted_margins = sorted(all_margins, key=lambda x: x[1])
            closest_name, closest_dist = sorted_margins[0]

            if closest_dist == 0.0:
                margin_status = MarginStatus.POSITIVE
                margin_summary = f"POSITIVE margin at {closest_name} (tumor ink contact, 0.0 mm)."
                violations.append("POSITIVE_SURGICAL_MARGIN")
                alerts.append({
                    "severity": AlertSeverity.PANIC,
                    "category": "POSITIVE_MARGIN",
                    "message": f"CRITICAL PATHOLOGY ALERT: Positive surgical margin at {closest_name} (0.0 mm clearance). Incomplete resection flagged."
                })
            elif closest_dist < 2.0:
                margin_status = MarginStatus.CLOSE
                margin_summary = f"CLOSE margin at {closest_name} ({closest_dist:.1f} mm clearance < 2.0 mm)."
                alerts.append({
                    "severity": AlertSeverity.WARNING,
                    "category": "CLOSE_MARGIN",
                    "message": f"Close surgical margin detected: {closest_name} is {closest_dist:.1f} mm from inked edge."
                })
            else:
                margin_status = MarginStatus.NEGATIVE
                margin_summary = f"NEGATIVE margins. Closest is {closest_name} at {closest_dist:.1f} mm (> 2.0 mm clear)."

        # 3. AJCC Staging Consistency Verification
        staging_notes = []
        nodes_exam = report_input.nodes_examined or 0
        nodes_pos = report_input.nodes_positive or 0

        if nodes_pos > nodes_exam:
            violations.append("DISCORDANT_NODE_COUNT")
            alerts.append({
                "severity": AlertSeverity.CRITICAL,
                "category": "STAGE_DISCORDANCE",
                "message": f"Critical count error: Positive nodes ({nodes_pos}) exceeds total nodes examined ({nodes_exam})."
            })

        if nodes_pos > 0 and report_input.pN in ["pN0", "pNX", "pN0(i+)"]:
            violations.append("DISCORDANT_PN_STAGE")
            alerts.append({
                "severity": AlertSeverity.CRITICAL,
                "category": "STAGE_DISCORDANCE",
                "message": f"Staging discordance: {nodes_pos} positive lymph nodes found but pN stage is coded as '{report_input.pN}'."
            })

        if nodes_exam == 0 and report_input.pN and report_input.pN.startswith("pN") and report_input.pN not in ["pNX", "pN0"]:
            violations.append("UNVERIFIED_PN_STAGE")
            alerts.append({
                "severity": AlertSeverity.WARNING,
                "category": "STAGE_DISCORDANCE",
                "message": f"pN stage coded as '{report_input.pN}' with zero regional lymph nodes examined."
            })

        # Tumor size vs pT staging heuristic (Breast/Colorectal)
        if report_input.tumor_size_cm is not None and report_input.pT:
            size = report_input.tumor_size_cm
            pt = report_input.pT.upper()
            if report_input.protocol_type == CAPProtocol.BREAST_INVASIVE:
                if size > 5.0 and pt.startswith("PT1"):
                    violations.append("DISCORDANT_PT_STAGE_SIZE")
                    alerts.append({
                        "severity": AlertSeverity.CRITICAL,
                        "category": "STAGE_DISCORDANCE",
                        "message": f"Tumor size {size} cm is discordant with pT stage '{report_input.pT}' (size > 5cm is typically pT3)."
                    })
                elif size <= 2.0 and pt.startswith("PT3"):
                    violations.append("DISCORDANT_PT_STAGE_SIZE")
                    alerts.append({
                        "severity": AlertSeverity.WARNING,
                        "category": "STAGE_DISCORDANCE",
                        "message": f"Tumor size {size} cm is small for pT stage '{report_input.pT}'."
                    })

        staging_summary = f"pT: {report_input.pT or 'Unspecified'}, pN: {report_input.pN or 'Unspecified'} ({nodes_pos}/{nodes_exam} nodes), pM: {report_input.pM or 'cM0'}"

        # 4. Protocol-Specific Biomarker Verification
        bm = report_input.biomarkers or {}
        if report_input.protocol_type == CAPProtocol.BREAST_INVASIVE:
            required_bm = ["ER", "PR", "HER2"]
            missing_bm = [k for k in required_bm if k not in bm and f"{k}_status" not in bm and f"{k}_percent" not in bm]
            if missing_bm:
                violations.append(f"MISSING_BREAST_BIOMARKERS: {', '.join(missing_bm)}")
                completeness -= (len(missing_bm) * 5.0)
                alerts.append({
                    "severity": AlertSeverity.WARNING,
                    "category": "MISSING_BIOMARKER",
                    "message": f"CAP Breast Protocol: Mandatory biomarker panel missing elements: {', '.join(missing_bm)}."
                })
        elif report_input.protocol_type == CAPProtocol.COLORECTAL_RESECTION:
            if "MSI" not in bm and "MMR" not in bm and "MSI_status" not in bm:
                violations.append("MISSING_CRC_MSI_STATUS")
                completeness -= 5.0
                alerts.append({
                    "severity": AlertSeverity.WARNING,
                    "category": "MISSING_BIOMARKER",
                    "message": "CAP Colorectal Protocol: Mismatch Repair (MMR) / Microsatellite Instability (MSI) status not documented."
                })

        final_completeness = max(0.0, min(100.0, completeness))

        # Determine final status
        has_panic = any("PANIC" in (a["severity"].value if hasattr(a["severity"], "value") else str(a["severity"])) for a in alerts)
        has_critical = any("CRITICAL" in (a["severity"].value if hasattr(a["severity"], "value") else str(a["severity"])) for a in alerts)

        if has_panic or has_critical:
            val_status = ValidationStatus.CRITICAL_FLAG
        elif any("DISCORDANT" in v for v in violations):
            val_status = ValidationStatus.DISCORDANT
        elif violations or final_completeness < 85.0:
            val_status = ValidationStatus.INCOMPLETE
        else:
            val_status = ValidationStatus.VALID

        # Prompt LLM for narrative synoptic analysis
        llm_prompt = (
            f"Review synoptic cancer protocol compliance for case '{report_input.case_id}':\n"
            f"- Protocol: {report_input.protocol_type.value}\n"
            f"- Histology: {report_input.histologic_type} ({report_input.histologic_grade or 'Grade N/A'})\n"
            f"- Margins: {margin_summary}\n"
            f"- Staging: {staging_summary}\n"
            f"- Violations: {', '.join(violations) if violations else 'None'}\n"
            f"- Status: {val_status.value} (Completeness: {final_completeness:.1f}%)"
        )
        llm_resp = self.llm.generate(prompt=llm_prompt, system_prompt="You are an expert surgical pathologist and CAP cancer protocol compliance auditor.")
        narrative = llm_resp.content

        report_id = report_input.report_id or f"SYN-{uuid.uuid4().hex[:8].upper()}"

        # Database persistence
        if persist_to_db:
            try:
                session = get_session()
                # Resolve case_id if accession number provided
                resolved_case_id = report_input.case_id
                db_c = session.query(Case).filter((Case.case_id == report_input.case_id) | (Case.accession_id_anonymized == report_input.case_id)).first()
                if db_c:
                    resolved_case_id = db_c.case_id

                db_report = session.query(SynopticReport).filter(SynopticReport.report_id == report_input.report_id).first() if report_input.report_id else None
                if not db_report:
                    db_report = SynopticReport(
                        case_id=resolved_case_id,
                        protocol_type=report_input.protocol_type.value,
                        primary_tumor_site=report_input.primary_tumor_site,
                        histologic_type=report_input.histologic_type,
                        histologic_grade=report_input.histologic_grade,
                        tumor_size_cm=report_input.tumor_size_cm,
                        deep_margin_mm=report_input.deep_margin_mm,
                        radial_margin_mm=report_input.radial_margin_mm,
                        mucosal_margin_mm=report_input.mucosal_margin_mm,
                        cervical_margin_mm=report_input.cervical_margin_mm,
                        closest_margin_name=closest_name if all_margins else None,
                        closest_margin_distance_mm=closest_dist if all_margins else None,
                        margin_status=margin_status,
                        nodes_examined=report_input.nodes_examined,
                        nodes_positive=report_input.nodes_positive,
                        pT=report_input.pT,
                        pN=report_input.pN,
                        pM=report_input.pM,
                        lvi_present=report_input.lvi_present,
                        pni_present=report_input.pni_present,
                        biomarkers_json=json.dumps(report_input.biomarkers),
                        protocol_violations_json=json.dumps(violations),
                        validation_status=val_status,
                        completeness_score=final_completeness
                    )
                    session.add(db_report)
                    session.flush()
                    report_id = db_report.report_id
                else:
                    db_report.case_id = resolved_case_id
                    db_report.protocol_violations_json = json.dumps(violations)
                    db_report.validation_status = val_status
                    db_report.completeness_score = final_completeness
                    db_report.margin_status = margin_status
                    report_id = db_report.report_id

                # Save generated alerts
                for alt in alerts:
                    db_alert = Alert(
                        case_id=resolved_case_id,
                        report_id=report_id,
                        severity=alt["severity"],
                        category=alt["category"],
                        message=alt["message"]
                    )
                    session.add(db_alert)

                session.commit()
                session.close()
            except Exception as e:
                logger.error(f"Failed to persist synoptic report to database: {e}")


        return SynopticValidationResult(
            report_id=report_id,
            case_id=report_input.case_id,
            protocol_type=report_input.protocol_type,
            validation_status=val_status,
            completeness_score=final_completeness,
            margin_status=margin_status,
            closest_margin_summary=margin_summary,
            staging_summary=staging_summary,
            protocol_violations=violations,
            narrative_summary=narrative,
            alerts_generated=alerts
        )
