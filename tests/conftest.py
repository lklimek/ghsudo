"""Fixtures shared by the GUI and ntfy test modules."""

from __future__ import annotations

import pytest

from ghsudo import __main__ as main


@pytest.fixture
def no_ntfy(monkeypatch):
    """No ntfy channel configured."""
    monkeypatch.setattr(main, "_load_ntfy_config", lambda: None)


@pytest.fixture
def quiet(monkeypatch):
    """Verbose mode off, whatever GHSUDO_DEBUG says."""
    monkeypatch.setattr(main, "_VERBOSE", False)
