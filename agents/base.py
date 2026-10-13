"""
PathMind-Atlas Base Security, PHI Guardrails, Cryptographic HMAC Audit Trail, and Action Framework.
Architect: Dr. Abu Suraih Sakhri (@abusuraihsakhri)
"""

from __future__ import annotations
import os
import re
import json
import hmac
import hashlib
import logging
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from datetime import datetime, timezone
from threading import RLock
from pydantic import BaseModel, Field

# Setup logger
logger = logging.getLogger("pathmind.security")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------
# Exceptions
# ---------------------------------------------------------
class SecurityException(Exception):
    """Raised when PHI or unvetted sensitive data is detected in outbound or inbound payload."""
    def __init__(self, message: str, detected_patterns: Optional[List[str]] = None):
        super().__init__(message)
        self.detected_patterns = detected_patterns or []


# ---------------------------------------------------------
# Role & Actor Enumerations
# ---------------------------------------------------------
class ActorTier(str, Enum):
    SYSTEM = "SYSTEM"
    AGENT = "AGENT"
    CLINICIAN = "CLINICIAN"
    PATHOLOGIST = "PATHOLOGIST"
    ADMIN = "ADMIN"


class AgentRole(str, Enum):
    SUPERVISOR = "PathMindSupervisor"
    QC_WORKER = "SlideQCAgent"
    SYNOPTIC_WORKER = "SynopticValidatorAgent"
    AUDITOR = "CryptographicAuditor"


# ---------------------------------------------------------
# PHI Guardrail Engine
# ---------------------------------------------------------
PHI_REGEX_PATTERNS = {
    "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
    "SSN_PREFIX": r"(?i)\b(?:SSN|SOCIAL\s+SECURITY(?:\s+NUMBER)?|SOC\s+SEC)[:=\s]+\d{3}-?\d{2}-?\d{4}\b",
    "PHONE_NUMBER": r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}\b",
    "EMAIL_ADDRESS": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "MRN_EXPLICIT": r"(?i)\b(?:MRN|MEDREC|MEDICAL\s+RECORD(?:\s+NUMBER)?|PATIENT\s+ID|CHART\s*#)[:=\s]+[A-Z0-9-]{4,15}\b",
    "PATIENT_NAME_KEY": r"(?i)\b(?:PATIENT\s+NAME|PT\s+NAME|PATIENT|NAME)[:=\s]+(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b",
    "DOB_KEY": r"(?i)\b(?:DOB|DATE\s+OF\s+BIRTH|BORN)[:=\s]+(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})\b",
    "STREET_ADDRESS": r"\b\d{1,5}\s+[A-Za-z0-9\.\s]{2,25}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Way|Court|Ct)\b",
    "ZIP_CODE_STRICT": r"\b\d{5}(?:-\d{4})?\b(?=.*(?:State|City|Address|Zip))",
    "CREDIT_CARD": r"\b(?:\d{4}[-\s]?){3}\d{4}\b",
    "INSURANCE_POLICY": r"(?i)\b(?:INSURANCE|POLICY|MEMBER\s+ID)[:=\s]+[A-Z0-9]{6,16}\b",
}

COMPILED_PHI_PATTERNS = {name: re.compile(pat) for name, pat in PHI_REGEX_PATTERNS.items()}


def assert_no_phi(data: Union[str, Dict[str, Any], List[Any]]) -> None:
    """
    Recursively scans string or structured data for potential Protected Health Information (PHI).
    Raises SecurityException if any violation pattern is identified.
    """
    if data is None:
        return

    text_to_scan = ""
    if isinstance(data, str):
        text_to_scan = data
    elif isinstance(data, (dict, list)):
        text_to_scan = json.dumps(data, default=str)
    else:
        text_to_scan = str(data)

    violations = []
    for pattern_name, regex in COMPILED_PHI_PATTERNS.items():
        matches = regex.findall(text_to_scan)
        if matches:
            violations.append(f"{pattern_name} (found {len(matches)} occurrence(s))")

    if violations:
        msg = f"CRITICAL PHI OUTBOUND VIOLATION: Blocked execution. Violations detected: {', '.join(violations)}"
        logger.error(msg)
        raise SecurityException(message=msg, detected_patterns=violations)


def sanitize_phi_text(text: str) -> str:
    """
    Masks recognized PHI patterns with cryptographic anonymization tags [REDACTED:<TYPE>].
    """
    sanitized = text
    for pattern_name, regex in COMPILED_PHI_PATTERNS.items():
        sanitized = regex.sub(f"[REDACTED:{pattern_name}]", sanitized)
    return sanitized


# ---------------------------------------------------------
# Cryptographic HMAC-SHA256 Signer & Audit Trail
# ---------------------------------------------------------
class HMACSigner:
    """
    Cryptographic signer for audit trails and inter-agent communication verification.
    """
    def __init__(self, secret_key: Optional[str] = None):
        key = secret_key or os.getenv("PATHMIND_HMAC_SECRET")
        if not key:
            raise SecurityException(
                "HMAC secret key must be provided via PATHMIND_HMAC_SECRET environment variable "
                "or secret_key parameter. Refusing to use a hardcoded default in production."
            )
        if len(key) < 32:
            raise SecurityException("HMAC secret key must be at least 32 characters for adequate security.")
        self._secret = key.encode("utf-8")

    def sign_payload(self, payload: Union[Dict[str, Any], str, bytes]) -> str:
        """Computes HMAC-SHA256 signature for given data."""
        if isinstance(payload, dict):
            # Deterministic JSON canonicalization
            raw_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        elif isinstance(payload, str):
            raw_bytes = payload.encode("utf-8")
        else:
            raw_bytes = payload

        h = hmac.new(self._secret, raw_bytes, hashlib.sha256)
        return h.hexdigest()

    def verify_signature(self, payload: Union[Dict[str, Any], str, bytes], expected_sig: str) -> bool:
        """Constant-time verification of HMAC signature."""
        computed = self.sign_payload(payload)
        return hmac.compare_digest(computed, expected_sig)


class AuditEventSchema(BaseModel):
    event_id: str
    timestamp: str
    actor_tier: ActorTier
    agent_role: str
    action_name: str
    target_resource_id: str
    details: Dict[str, Any]
    prev_hash: str
    current_hash: str
    hmac_signature: str


class AuditLogger:
    """
    Cryptographically chained, tamper-evident audit logger.
    """
    def __init__(self, signer: Optional[HMACSigner] = None):
        self.signer = signer or HMACSigner()
        self._lock = RLock()
        self._last_hash = "GENESIS_BLOCK_PATHMIND_ATLAS_2026"
        self._events: List[AuditEventSchema] = []

    @property
    def last_hash(self) -> str:
        with self._lock:
            return self._last_hash

    def log_event(
        self,
        actor_tier: ActorTier,
        agent_role: Union[AgentRole, str],
        action_name: str,
        target_resource_id: str,
        details: Dict[str, Any]
    ) -> AuditEventSchema:
        """
        Creates a tamper-evident audit record, ensures zero-PHI in details,
        computes SHA-256 chain hash and HMAC-SHA256 signature.
        """
        with self._lock:
            # Ensure zero PHI before committing to immutable log
            assert_no_phi(details)
            assert_no_phi(target_resource_id)

            now_str = datetime.now(timezone.utc).isoformat()
            event_id = hashlib.sha256(f"{self._last_hash}:{action_name}:{now_str}".encode()).hexdigest()[:16]

            role_str = agent_role.value if isinstance(agent_role, AgentRole) else str(agent_role)

            data_to_hash = {
                "event_id": event_id,
                "timestamp": now_str,
                "actor_tier": actor_tier.value,
                "agent_role": role_str,
                "action_name": action_name,
                "target_resource_id": target_resource_id,
                "details": details,
                "prev_hash": self._last_hash
            }

            canonical_bytes = json.dumps(data_to_hash, sort_keys=True, separators=(",", ":")).encode("utf-8")
            current_hash = hashlib.sha256(canonical_bytes).hexdigest()
            hmac_sig = self.signer.sign_payload(current_hash)

            event = AuditEventSchema(
                event_id=event_id,
                timestamp=now_str,
                actor_tier=actor_tier,
                agent_role=role_str,
                action_name=action_name,
                target_resource_id=target_resource_id,
                details=details,
                prev_hash=self._last_hash,
                current_hash=current_hash,
                hmac_signature=hmac_sig
            )

            self._last_hash = current_hash
            self._events.append(event)
            return event

    def verify_integrity(self) -> Tuple[bool, Optional[str]]:
        """
        Verifies the full cryptographic hash chain and HMAC signatures.
        Returns (True, None) if completely intact, or (False, reason).
        """
        with self._lock:
            expected_prev = "GENESIS_BLOCK_PATHMIND_ATLAS_2026"
            for idx, ev in enumerate(self._events):
                if ev.prev_hash != expected_prev:
                    return False, f"Broken chain link at index {idx}: expected prev_hash {expected_prev}, got {ev.prev_hash}"

                data_to_hash = {
                    "event_id": ev.event_id,
                    "timestamp": ev.timestamp,
                    "actor_tier": ev.actor_tier.value if hasattr(ev.actor_tier, "value") else str(ev.actor_tier),
                    "agent_role": ev.agent_role,
                    "action_name": ev.action_name,
                    "target_resource_id": ev.target_resource_id,
                    "details": ev.details,
                    "prev_hash": ev.prev_hash
                }
                canonical_bytes = json.dumps(data_to_hash, sort_keys=True, separators=(",", ":")).encode("utf-8")
                recomputed_hash = hashlib.sha256(canonical_bytes).hexdigest()

                if recomputed_hash != ev.current_hash:
                    return False, f"Data tampering detected at event {ev.event_id} (index {idx})"

                if not self.signer.verify_signature(ev.current_hash, ev.hmac_signature):
                    return False, f"Invalid HMAC signature at event {ev.event_id} (index {idx})"

                expected_prev = ev.current_hash

            return True, None

    def get_events(self) -> List[AuditEventSchema]:
        with self._lock:
            return list(self._events)


# Global default audit logger
default_audit_logger = AuditLogger()


# ---------------------------------------------------------
# Action Execution Framework & Secure Invoke
# ---------------------------------------------------------
class ActionExecutor:
    """
    Executes operations under strict security sandboxing, PHI validation,
    and automatic audit event generation.
    """
    def __init__(self, audit_logger: Optional[AuditLogger] = None):
        self.audit_logger = audit_logger or default_audit_logger

    def execute(
        self,
        actor_tier: ActorTier,
        agent_role: Union[AgentRole, str],
        action_name: str,
        target_resource_id: str,
        operation: Callable[..., Any],
        *args,
        **kwargs
    ) -> Any:
        """
        Validates arguments for zero-PHI, executes operation, validates result for zero-PHI,
        and logs cryptographic audit record.
        """
        # Pre-execution check
        assert_no_phi({"args": args, "kwargs": kwargs})

        start_time = datetime.now(timezone.utc)
        status = "SUCCESS"
        error_msg = None
        result = None

        try:
            result = operation(*args, **kwargs)
            # Post-execution check
            assert_no_phi(result)
            return result
        except SecurityException as se:
            status = "SECURITY_BLOCKED"
            error_msg = type(se).__name__
            raise
        except Exception as ex:
            status = "FAILED"
            error_msg = type(ex).__name__
            raise
        finally:
            end_time = datetime.now(timezone.utc)
            duration_ms = (end_time - start_time).total_seconds() * 1000.0

            log_details = {
                "status": status,
                "duration_ms": round(duration_ms, 2),
                "error": error_msg,
                "has_result": result is not None
            }

            self.audit_logger.log_event(
                actor_tier=actor_tier,
                agent_role=agent_role,
                action_name=action_name,
                target_resource_id=target_resource_id,
                details=log_details
            )


def secure_invoke(
    agent_role: Union[AgentRole, str],
    action_name: str,
    target_id: str,
    func: Callable[..., Any],
    *args,
    actor_tier: ActorTier = ActorTier.AGENT,
    audit_logger: Optional[AuditLogger] = None,
    **kwargs
) -> Any:
    """
    Functional entry point for securing any agent invocation.
    """
    executor = ActionExecutor(audit_logger=audit_logger)
    return executor.execute(
        actor_tier=actor_tier,
        agent_role=agent_role,
        action_name=action_name,
        target_resource_id=target_id,
        operation=func,
        *args,
        **kwargs
    )
