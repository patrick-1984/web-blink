#!/usr/bin/env python3
"""Run the bundled CLI without an editable installation."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from blink.cli import main

raise SystemExit(main())
