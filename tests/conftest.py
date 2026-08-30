"""Shared pytest configuration.

Ensures the repository root is on sys.path so tests can import top-level
packages such as ``scripts`` without requiring an editable install.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
