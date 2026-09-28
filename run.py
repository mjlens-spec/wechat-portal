#!/usr/bin/env python3
"""Entry point used by the installed launcher (python -I run.py ...)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wechat_portal.cli import main  # noqa: E402

sys.exit(main())
