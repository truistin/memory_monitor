#!/usr/bin/env python3
"""Run from a checkout without installation or changing the command's cwd."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from memory_monitor.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
