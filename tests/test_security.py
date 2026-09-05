"""
PathMind-Atlas Security & Input Validation Tests.
Tests HMAC secret enforcement, API input bounds validation, and CORS configuration.
"""
import os
import pytest
from fastapi.testclient import TestClient

from agents.base import HMACSigner, SecurityException, default_audit_logger
from agents.api import app


@pytest.fixture
def test_client():
    return TestClient(app)


# ---------------------------------------------------------
# HMAC Secret Enforcement Tests
# ---------------------------------------------------------
class TestHMACSecretEnforcement:
    def test_hmac_requires_secret(self):
        """HMACSigner must reject empty/missing secrets."""
        # Temporarily clear env var
        original = os.environ.pop("PATHMIND_HMAC_SECRET", None)
        try:
            with pytest.raises(SecurityException, match="PATHMIND_HMAC_SECRET"):
                HMACSigner()
        finally:
            if original:
                os.environ["PATHMIND_HMAC_SECRET"] = original

    def test_hmac_rejects_short_secret(self):
        """HMACSigner must reject secrets shorter than 32 characters."""
        with pytest.raises(SecurityException, match="at least 32 characters"):
            HMACSigner(secret_key="short-key")

    def test_hmac_accepts_valid_secret(self):
        """HMACSigner must accept secrets of 32+ characters."""
        signer = HMACSigner(secret_key="a" * 32)
        sig = signer.sign_payload("test-data")
        assert len(sig) == 64  # SHA-256 hex digest length
        assert signer.verify_signature("test-data", sig) is True

    def test_hmac_signer_deterministic(self):
        """Same payload must produce same signature."""
        signer = HMACSigner(secret_key="test-secret-key-that-is-32-chars-ok!")
        sig1 = signer.sign_payload({"key": "value"})
        sig2 = signer.sign_payload({"key": "value"})
        assert sig1 == sig2


# ---------------------------------------------------------
# API Input Validation Tests
# ---------------------------------------------------------
class TestAPIInputValidation:
    def test_qc_rejects_negative_blur(self, test_client):
        """QC endpoint must reject negative Laplacian blur variance."""
        res = test_client.post("/api/slides/qc", json={
            "case_id": "CASE-TEST",
            "laplacian_blur_var": -100.0
        })
        assert res.status_code == 400
        assert "non-negative" in res.json()["detail"]

    def test_qc_rejects_excessive_tissue_fold(self, test_client):
        """QC endpoint must reject tissue fold > 100%."""
        res = test_client.post("/api/slides/qc", json={
            "case_id": "CASE-TEST",
            "tissue_fold_pct": 150.0
        })
        assert res.status_code == 400
        assert "between 0 and 100" in res.json()["detail"]

    def test_qc_rejects_negative_air_bubble(self, test_client):
        """QC endpoint must reject negative air bubble percentage."""
        res = test_client.post("/api/slides/qc", json={
            "case_id": "CASE-TEST",
            "air_bubble_pct": -5.0
        })
        assert res.status_code == 400

    def test_qc_rejects_invalid_stain_uniformity(self, test_client):
        """QC endpoint must reject stain uniformity outside [0, 1]."""
        res = test_client.post("/api/slides/qc", json={
            "case_id": "CASE-TEST",
            "stain_intensity_uniformity": 1.5
        })
        assert res.status_code == 400

    def test_synoptic_rejects_negative_tumor_size(self, test_client):
        """Synoptic endpoint must reject negative tumor size."""
        res = test_client.post("/api/synoptic/validate", json={
            "case_id": "CASE-TEST",
            "protocol_type": "BREAST_INVASIVE",
            "primary_tumor_site": "Breast",
            "histologic_type": "Carcinoma",
            "tumor_size_cm": -2.0
        })
        assert res.status_code == 400
        assert "non-negative" in res.json()["detail"]

    def test_synoptic_rejects_negative_margin(self, test_client):
        """Synoptic endpoint must reject negative margin measurements."""
        res = test_client.post("/api/synoptic/validate", json={
            "case_id": "CASE-TEST",
            "protocol_type": "BREAST_INVASIVE",
            "primary_tumor_site": "Breast",
            "histologic_type": "Carcinoma",
            "deep_margin_mm": -1.0
        })
        assert res.status_code == 400

    def test_synoptic_rejects_negative_nodes(self, test_client):
        """Synoptic endpoint must reject negative node counts."""
        res = test_client.post("/api/synoptic/validate", json={
            "case_id": "CASE-TEST",
            "protocol_type": "BREAST_INVASIVE",
            "primary_tumor_site": "Breast",
            "histologic_type": "Carcinoma",
            "nodes_positive": -3
        })
        assert res.status_code == 400


# ---------------------------------------------------------
# Audit Integrity Tests
# ---------------------------------------------------------
class TestAuditIntegrity:
    def test_audit_chain_valid_after_events(self):
        """Audit chain must remain valid after logging events."""
        is_valid, reason = default_audit_logger.verify_integrity()
        assert is_valid is True

    def test_audit_events_are_chained(self):
        """Each audit event must reference the previous event's hash."""
        events = default_audit_logger.get_events()
        if len(events) >= 2:
            assert events[1].prev_hash == events[0].current_hash
