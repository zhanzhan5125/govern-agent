"""Source-checkout entrypoint; installation maps the canonical package to EvoAgent/."""
from pathlib import Path

__path__ = [str(Path(__file__).with_name("EvoAgent"))]
__version__ = "0.4.0"

if __name__ == "__main__":
    from evoagent.application.api import run
    run()
