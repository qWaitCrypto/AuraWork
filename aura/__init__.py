from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

__version__ = "0.1.0.dev0"

try:
    __version__ = version("aura")
except PackageNotFoundError:
    pass
