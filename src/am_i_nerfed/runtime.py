"""Local output locations, private directory creation, and terminal-only styling."""

import datetime as dt
import os
from pathlib import Path
import sys


def default_output(prefix):
    """Use a private home directory on Linux, or a local runs directory elsewhere."""
    root = Path.home() / ".am-i-nerfed" if sys.platform.startswith("linux") else Path("runs")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return root / (prefix + "-" + stamp)


def private_mkdir(path, exist_ok=False):
    """Create every missing directory with mode 0700; preserve existing parents.

    Existing directories are accepted only with exist_ok=True. Their permissions
    are preserved, so using an existing working directory does not chmod it.
    """
    path = Path(path)
    try:
        path.mkdir(mode=0o700)
    except FileNotFoundError:
        private_mkdir(path.parent, exist_ok=True)
        return private_mkdir(path, exist_ok=exist_ok)
    except FileExistsError:
        if not exist_ok or not path.is_dir() or path.is_symlink():
            raise
    else:
        path.chmod(0o700)
    return path


class Console:
    """Keep machine-readable artifacts plain; color only actual terminal streams."""

    def __init__(self, verbose=False, stdout=None, stderr=None):
        self.verbose = verbose
        self.stdout = sys.stdout if stdout is None else stdout
        self.stderr = sys.stderr if stderr is None else stderr

    @staticmethod
    def _color_enabled(stream):
        if os.environ.get("NO_COLOR") or os.environ.get("TERM", "").lower() == "dumb":
            return False
        try:
            return bool(stream.isatty())
        except (AttributeError, OSError, ValueError):
            return False

    def style(self, text, status, stream=None):
        text = str(text)
        stream = self.stdout if stream is None else stream
        color = {"MATCH": "32", "CHANGED": "31", "UNKNOWN": "33",
                 "ERROR": "31", "WARNING": "33"}.get(str(status).upper())
        if color and self._color_enabled(stream):
            return "\033[" + color + "m" + text + "\033[0m"
        return text

    def info(self, text):
        print(text, file=self.stdout, flush=True)

    def detail(self, text):
        if self.verbose:
            self.info(text)

    def warning(self, text):
        print(self.style(text, "WARNING", self.stderr), file=self.stderr, flush=True)

    def error(self, text):
        print(self.style(text, "ERROR", self.stderr), file=self.stderr, flush=True)

    def result(self, text, status):
        print(self.style(text, status), file=self.stdout, flush=True)
