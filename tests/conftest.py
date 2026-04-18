"""Shared pytest fixtures and path setup for cam-flow tests."""

import os
import sys

# Make `src/` importable without requiring `pip install -e .`
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", "src"))
if SRC not in sys.path:
    sys.path.insert(0, SRC)
