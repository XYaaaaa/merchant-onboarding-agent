import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent))

from onboarding.api.app import create_app  # noqa: E402


@pytest.fixture
def make_client():
    def _make(bugs=()):
        return TestClient(create_app(bugs=frozenset(bugs)))
    return _make
