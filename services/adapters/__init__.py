"""Adapter spine (Model 3).

Importing this package registers every built-in adapter. A new adapter type is
added by writing its class in `impl.py` (or its own module) with
`@register_adapter` and importing it here — nothing else in the platform changes.
"""

from services.adapters.base import (
    CameraAdapter,
    CameraRef,
    PlaybackTarget,
    available_adapters,
    get_adapter,
    register_adapter,
)
from services.adapters.impl import (  # noqa: F401 - imported for registration
    FileAdapter,
    HlsAdapter,
    HttpAdapter,
    OnvifAdapter,
    RtspAdapter,
)

__all__ = [
    "CameraAdapter",
    "CameraRef",
    "PlaybackTarget",
    "available_adapters",
    "get_adapter",
    "register_adapter",
]
