# ruff: noqa: E402
import os

from .util import download_and_compile_dsdl

os.environ.setdefault("PYCYPHAL_NO_IMPORT_HOOK", "1")
download_and_compile_dsdl()

del os, download_and_compile_dsdl


from .client import Client
from .registry import Register, Registry

__all__ = ["Client", "Register", "Registry"]
