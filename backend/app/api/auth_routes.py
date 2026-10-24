from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.auth import COOKIE_NAME, SESSION_TTL, check_session, make_session, verify_password
from ..services.settings import load_settings
from .schemas import LoginIn

router = APIRouter(prefix="/api/auth", tags=["Anmeldung"])


@router.get("/status")
def status(request: Request, db: Session = Depends(get_db)):
    s = load_settings(db)
    ok = (not s.auth_enabled) or check_session(request.cookies.get(COOKIE_NAME), s.auth_username)
    return {"auth_enabled": s.auth_enabled, "authenticated": ok, "username": s.auth_username if ok else None}


@router.post("/login")
async def login(body: LoginIn, response: Response, request: Request, db: Session = Depends(get_db)):
    s = load_settings(db)
    if not s.auth_enabled:
        return {"authenticated": True}
    valid = body.username == s.auth_username and await asyncio.to_thread(verify_password, body.password,
                                                                         s.auth_password_hash)
    if not valid:
        await asyncio.sleep(1.0)  # Brute-Force bremsen
        raise HTTPException(401, "Benutzername oder Passwort falsch")
    response.set_cookie(COOKIE_NAME, make_session(body.username), max_age=SESSION_TTL, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https")
    return {"authenticated": True}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE_NAME)
    return {"authenticated": False}
