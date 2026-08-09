"""Bundled Google Workspace broker plugin.

The plugin side contains no Google credentials and performs no Google API
imports. It only validates local tool arguments and forwards allowed
operations to a local Unix-socket broker.
"""

from __future__ import annotations

from .plugin import register

__all__ = ["register"]
