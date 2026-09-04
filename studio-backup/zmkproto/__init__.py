"""Generated ZMK Studio protobuf modules (zmk-studio-messages).

protoc emits flat imports (``import meta_pb2``), so this package directory is
put on sys.path before the modules are imported.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import meta_pb2  # noqa: E402,F401
import core_pb2  # noqa: E402,F401
import behaviors_pb2  # noqa: E402,F401
import keymap_pb2  # noqa: E402,F401
import studio_pb2  # noqa: E402,F401

__all__ = ["meta_pb2", "core_pb2", "behaviors_pb2", "keymap_pb2", "studio_pb2"]
