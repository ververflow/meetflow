"""Allow `python -m meetflow`."""
from __future__ import annotations

import os
import sys

# pythonw.exe has no console — sys.stdout/stderr are None, which crashes Click
# and logging. Redirect to devnull.
if sys.stdout is None:
    _devnull = open(os.devnull, "w")  # noqa: SIM115
    sys.stdout = _devnull
    sys.stderr = _devnull

from meetflow.cli import cli

if __name__ == "__main__":
    cli()
