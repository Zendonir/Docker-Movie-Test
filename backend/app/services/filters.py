"""Gemeinsame Filterlogik für Dateiliste und Export."""

from __future__ import annotations

from sqlalchemy import or_

from ..models import MediaFile


def apply_filters(q, *, search: str | None, status: list[str] | None, ext: list[str] | None,
                  library_id: int | None, directory: str | None, only_new: bool, only_damaged: bool,
                  present: str, check_level: list[str] | None):
    if search:
        terms = [t for t in search.lower().split() if t][:8]
        for t in terms:
            like = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            q = q.where(MediaFile.name_lower.like(like, escape="\\"))
    if status:
        q = q.where(MediaFile.status.in_(status))
    if ext:
        q = q.where(MediaFile.extension.in_([e.lower().lstrip(".") for e in ext]))
    if library_id is not None:
        q = q.where(MediaFile.library_id == library_id)
    if directory is not None and directory != "":
        d = directory.strip("/")
        q = q.where(or_(MediaFile.directory == d, MediaFile.directory.like(d.replace("%", "\\%") + "/%", escape="\\")))
    if only_new:
        q = q.where(MediaFile.check_level == "none")
    if only_damaged:
        q = q.where(MediaFile.status.in_(("critical", "error")))
    if present == "yes":
        q = q.where(MediaFile.present.is_(True))
    elif present == "no":
        q = q.where(MediaFile.present.is_(False))
    if check_level:
        q = q.where(MediaFile.check_level.in_(check_level))
    return q
