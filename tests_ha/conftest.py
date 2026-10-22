"""Home Assistant harness tests (run locally; CI runs only tests/).

Needs pytest-homeassistant-custom-component. Run from the repo root:

    python -m pytest tests_ha -q -p no:cacheprovider
"""
import sys

import pytest
import pytest_socket

if sys.platform == "win32":
    # Windows' proactor event loop needs an AF_INET socketpair, which the
    # harness's socket guard blocks. Local-only workaround.
    pytest_socket.disable_socket = lambda *args, **kwargs: None


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield
