"""
Shared pytest configuration for pathmind-atlas test suite.
"""
import os


def pytest_configure(config):
    """Set default environment variables required by tests."""
    os.environ.setdefault("PATHMIND_HMAC_SECRET", "test-secret-key-for-pytest-min-32-chars-long")
