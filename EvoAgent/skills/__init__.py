"""Compatibility for the existing user-moved Skill modules, without moving them back."""
from pathlib import Path

__path__ = [str(Path(__file__).resolve().parents[2] / "skills")]
