from __future__ import annotations

from fastapi import APIRouter

from .. import __version__
from ..config import get_config
from ..scanners.base import SCANNER_VERSIONS
from ..scanners.tools import get_toolset
from ..services import repair
from ..services.pathsec import allowed_roots

router = APIRouter(prefix="/api/system", tags=["System"])


@router.get("")
def system_info():
    cfg = get_config()
    import os
    return {
        "version": __version__,
        "tools": get_toolset().as_dict(),
        "scanner_versions": {k.value: v for k, v in SCANNER_VERSIONS.items()},
        "media_roots": allowed_roots(),
        "config_dir": str(cfg.config_dir),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "tz": cfg.tz,
        "repair_enabled": repair.repair_enabled(),
        "media_writable": [r for r in allowed_roots() if os.access(r, os.W_OK)],
    }
