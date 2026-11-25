"""Tempo backend: private planning service consumed only by the Home Assistant integration."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tempo-backend")  # single source: pyproject.toml
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"
