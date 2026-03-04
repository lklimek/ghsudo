"""ghsudo — GitHub Sudo: re-execute commands with an elevated GitHub token."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ghsudo")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
