"""Resolve checkout resources independently of the caller's working directory."""
import os
from pathlib import Path


def project_root() -> Path:
    override = os.environ.get("GOVERN_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[3]
