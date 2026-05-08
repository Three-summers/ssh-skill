import pytest

import ssh_execute


pytestmark = pytest.mark.unit


def test_existing_ssh_execute_module_has_no_interactive_session_dependency():
    names = set(dir(ssh_execute))

    assert "InteractiveDaemon" not in names
    assert "InteractiveSession" not in names
