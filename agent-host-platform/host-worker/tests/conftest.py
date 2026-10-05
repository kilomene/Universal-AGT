"""Pytest bootstrap: make the host-worker packages importable."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
