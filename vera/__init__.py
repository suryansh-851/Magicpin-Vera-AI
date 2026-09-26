"""Vera — merchant AI assistant for the magicpin AI Challenge."""

import os
from pathlib import Path

# Load KEY=VALUE pairs from a local .env (gitignored) so secrets never live in code.
# Real environment variables (e.g. set in the Render dashboard) take precedence.
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
