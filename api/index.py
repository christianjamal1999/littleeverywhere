"""Vercel entrypoint: every non-static request is rewritten here."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import video_fixes  # noqa: E402,F401
from app import app  # noqa: E402,F401
