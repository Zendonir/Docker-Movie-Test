from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..database import session_scope
from ..services.exports import ExportError, build_export

router = APIRouter(prefix="/api/export", tags=["Export"])


@router.get("")
def export(
    format: str = Query("csv", pattern="^(csv|json|html)$"),
    scope: str = Query("library", pattern="^(library|damaged|warnings|job|file|filter)$"),
    job_id: int | None = None,
    file_id: int | None = None,
    library_id: int | None = None,
    details: bool | None = None,
    delimiter: str = Query(";", pattern="^[;,\t]$"),
    inline: bool = False,
    # Filter (nur für scope=filter, entspricht der Dateiliste)
    q: str | None = Query(None, max_length=300),
    status: list[str] | None = Query(None),
    ext: list[str] | None = Query(None),
    directory: str | None = Query(None, max_length=4096),
    only_new: bool = False,
    only_damaged: bool = False,
    present: str = Query("yes", pattern="^(yes|no|all)$"),
    check_level: list[str] | None = Query(None),
):
    filters = {"search": q, "status": status, "ext": ext, "library_id": library_id, "directory": directory,
               "only_new": only_new, "only_damaged": only_damaged, "present": present, "check_level": check_level}

    def make(db):
        return build_export(db, format, scope, job_id, file_id, None if scope == "filter" else library_id, details,
                            delimiter, filters=filters)

    def gen():
        with session_scope() as db:
            stream, _, _ = make(db)
            yield from stream

    try:
        with session_scope() as db:
            _, media_type, filename = make(db)
    except ExportError as exc:
        raise HTTPException(400, str(exc)) from exc
    disp = "inline" if inline and format == "html" else "attachment"
    headers = {"Content-Disposition": f"{disp}; filename*=UTF-8''{quote(filename)}"}
    return StreamingResponse(gen(), media_type=media_type, headers=headers)
