#!/usr/bin/env python3
"""Compatibility entry point; prefer `am-i-nerfed claude`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from am_i_nerfed.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["claude"] + sys.argv[1:]))
