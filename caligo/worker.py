"""Batch entry point when the experiment location is explicitly local."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from caligo.remote import main

main()
