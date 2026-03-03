"""Basic tests for the ghsudo package."""

import ghsudo


def test_version_exists():
    """Verify that __version__ is defined and non-empty."""
    assert hasattr(ghsudo, "__version__")
    assert ghsudo.__version__
