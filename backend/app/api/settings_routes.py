from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.auth import hash_password
from ..services.settings import AppSettings, SECRET_KEYS, public_settings, update_settings, load_settings
from ..workers.worker import get_worker
from .schemas import PasswordIn

router = APIRouter(prefix="/api/settings", tags=["Einstellungen"])

READONLY = SECRET_KEYS | {"auth_username", "auth_enabled"}


@router.get("")
def get_settings(db: Session = Depends(get_db)):
    return public_settings(load_settings(db))


@router.put("")
def put_settings(changes: dict = Body(...), db: Session = Depends(get_db)):
    unknown = set(changes) - set(AppSettings.model_fields)
    if unknown:
        raise HTTPException(400, f"Unbekannte Einstellungen: {sorted(unknown)}")
    if set(changes) & READONLY:
        raise HTTPException(400, "Anmeldedaten bitte über /api/settings/auth ändern")
    try:
        s = update_settings(db, changes)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc
    db.commit()
    get_worker().wake()
    return public_settings(s)


@router.post("/auth")
def set_auth(body: PasswordIn, db: Session = Depends(get_db)):
    try:
        h = hash_password(body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    s = update_settings(db, {"auth_enabled": body.enable, "auth_username": body.username, "auth_password_hash": h})
    return public_settings(s)


@router.post("/auth/disable")
def disable_auth(db: Session = Depends(get_db)):
    s = update_settings(db, {"auth_enabled": False})
    return public_settings(s)
