"""Declarative plugin system for AISummary.

Plugins are DATA, not code: a folder with a plugin.json manifest that contributes
a persona/prompt, permissions, and (later) an offline knowledge pack. The host
executes them, so a plugin cannot run arbitrary code or reach the network —
which keeps the "nothing leaves your machine" guarantee true by construction.
"""
from . import loader

__all__ = ["loader"]
