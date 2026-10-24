from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.auth import COOKIE_NAME, check_session
from ..services.settings import load_settings


def require_auth(request: Request, db: Session = Depends(get_db)) -> None:
    settings = load_settings(db)
    if not settings.auth_enabled:
        return
    token = request.cookies.get(COOKIE_NAME)
    if not check_session(token, settings.auth_username):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Anmeldung erforderlich")
