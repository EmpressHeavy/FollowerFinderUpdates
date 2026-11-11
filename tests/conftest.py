"""Product tests are not a paid clone. The license gate is exercised in test_license_check.py."""
from pathlib import Path

import pytest

from src import license_check


@pytest.fixture(autouse=True)
def _license_open_for_product_tests(request, monkeypatch):
    if Path(str(request.node.path)).name == "test_license_check.py":
        return
    monkeypatch.setattr(license_check, "require", lambda *args, **kwargs: {"allow": True, "reason": "test"})
