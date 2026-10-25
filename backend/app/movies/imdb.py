"""IMDb-Datensätze (https://datasets.imdbws.com/): Download, lokaler Import, Abgleich.

Verwendet werden die offiziellen, frei herunterladbaren Datensätze (nur für private,
nicht-kommerzielle Nutzung, siehe https://developer.imdb.com/non-commercial-datasets/):

* title.basics.tsv.gz  – Titel, Jahr, Laufzeit, Genres
* title.ratings.tsv.gz – Bewertung und Anzahl der Stimmen
* title.akas.tsv.gz    – lokalisierte Titel (für deutsche Dateinamen; optional)

Der Import läuft in eine separate SQLite-Datei (``/config/imdb.db``), zuerst in eine
temporäre Datei, die erst nach erfolgreichem Abschluss atomar ersetzt wird. Danach
erfolgt jeder Abgleich lokal – ohne Anfragen an IMDb.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3
import ssl
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_config
from .parse import normalize_title

log = logging.getLogger(__name__)

BASE_URL = os.environ.get("IMDB_DATASET_URL", "https://datasets.imdbws.com")
FILES = {"basics": "title.basics.tsv.gz", "ratings": "title.ratings.tsv.gz", "akas": "title.akas.tsv.gz"}
TITLE_TYPES = {"movie", "tvMovie", "video", "tvSpecial"}
AKA_REGIONS = {"DE", "AT", "CH"}
ATTRIBUTION = "Information courtesy of IMDb (https://www.imdb.com). Used with permission."


def data_dir() -> Path:
    return get_config().config_dir / "imdb"


def db_path() -> Path:
    return get_config().config_dir / "imdb.db"


@dataclass
class ImdbState:
    running: bool = False
    phase: str | None = None
    rows: int = 0
    bytes_done: int = 0
    bytes_total: int | None = None
    message: str | None = None
    error: str | None = None
    started: float | None = None
    finished: float | None = None
    cancel: bool = False
    log: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"running": self.running, "phase": self.phase, "rows": self.rows, "bytes_done": self.bytes_done,
                "bytes_total": self.bytes_total, "message": self.message, "error": self.error}


STATE = ImdbState()


def tconst_int(t: str) -> int | None:
    try:
        return int(t[2:]) if t.startswith("tt") else None
    except ValueError:
        return None


def tconst_str(n: int) -> str:
    return f"tt{n:07d}"


# ------------------------------------------------------------------ Download
def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var)
        if path and os.path.isfile(path):
            ctx.load_verify_locations(path)
    return ctx


def download(include_akas: bool, progress=None) -> None:
    """Lädt die Datensätze nach /config/imdb (erst .part, dann umbenennen)."""
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    names = ["basics", "ratings"] + (["akas"] if include_akas else [])
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()))
    for key in names:
        fname = FILES[key]
        url = f"{BASE_URL.rstrip('/')}/{fname}"
        part = d / (fname + ".part")
        STATE.phase = f"Download {fname}"
        STATE.bytes_done, STATE.bytes_total = 0, None
        req = urllib.request.Request(url, headers={"User-Agent": "media-integrity-scanner/1.0"})
        with opener.open(req, timeout=60) as resp, open(part, "wb") as out:
            length = resp.headers.get("Content-Length")
            STATE.bytes_total = int(length) if length else None
            while True:
                if STATE.cancel:
                    raise InterruptedError("abgebrochen")
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                STATE.bytes_done += len(chunk)
                if progress:
                    progress()
        os.replace(part, d / fname)
        log.info("IMDb-Datensatz geladen: %s (%d Byte)", fname, STATE.bytes_done)


# ------------------------------------------------------------------ Import
SCHEMA = """
CREATE TABLE titles (id INTEGER PRIMARY KEY, type TEXT, title TEXT, original_title TEXT, year INTEGER,
                     runtime INTEGER, genres TEXT, title_de TEXT);
CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating REAL, votes INTEGER);
CREATE TABLE names (norm TEXT, id INTEGER, source TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _rows(path: Path):
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        header = fh.readline().rstrip("\n").split("\t")
        for line in fh:
            yield header, line.rstrip("\n").split("\t")


def _nn(v: str) -> str | None:
    return None if v == "\\N" else v


def import_datasets(progress=None) -> dict:
    d = data_dir()
    basics, ratings, akas = d / FILES["basics"], d / FILES["ratings"], d / FILES["akas"]
    if not basics.exists() or not ratings.exists():
        raise FileNotFoundError(f"Datensätze fehlen in {d} ({FILES['basics']}, {FILES['ratings']})")
    tmp = db_path().with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
    ids: set[int] = set()
    batch_t, batch_n = [], []
    STATE.phase, STATE.rows = "Import Titel", 0

    def flush() -> None:
        if batch_t:
            conn.executemany("INSERT INTO titles VALUES (?,?,?,?,?,?,?,NULL)", batch_t)
            batch_t.clear()
        if batch_n:
            conn.executemany("INSERT INTO names VALUES (?,?,?)", batch_n)
            batch_n.clear()

    for header, r in _rows(basics):
        STATE.rows += 1
        if STATE.cancel:
            raise InterruptedError("abgebrochen")
        if len(r) < 9 or r[1] not in TITLE_TYPES or r[4] == "1":
            continue
        tid = tconst_int(r[0])
        if tid is None:
            continue
        year = int(r[5]) if r[5].isdigit() else None
        runtime = int(r[7]) if r[7].isdigit() else None
        ids.add(tid)
        batch_t.append((tid, r[1], r[2], r[3], year, runtime, _nn(r[8])))
        norms = {normalize_title(r[2]), normalize_title(r[3])}
        batch_n.extend((n, tid, "title") for n in norms if n)
        if len(batch_t) >= 20000:
            flush()
            if progress:
                progress()
    flush()

    STATE.phase = "Import Bewertungen"
    batch_r = []
    for header, r in _rows(ratings):
        STATE.rows += 1
        tid = tconst_int(r[0]) if r else None
        if tid in ids and len(r) >= 3:
            try:
                batch_r.append((tid, float(r[1]), int(r[2])))
            except ValueError:
                continue
        if len(batch_r) >= 50000:
            conn.executemany("INSERT INTO ratings VALUES (?,?,?)", batch_r)
            batch_r.clear()
    conn.executemany("INSERT INTO ratings VALUES (?,?,?)", batch_r)

    aka_count = 0
    de_titles: dict[int, tuple[int, str]] = {}  # id -> (Priorität, Titel)
    if akas.exists():
        STATE.phase = "Import deutsche Titel"
        for header, r in _rows(akas):
            STATE.rows += 1
            if STATE.cancel:
                raise InterruptedError("abgebrochen")
            if len(r) < 8:
                continue
            if r[3] not in AKA_REGIONS and r[4] != "de":
                continue
            tid = tconst_int(r[0])
            if tid not in ids:
                continue
            n = normalize_title(r[2])
            if n:
                batch_n.append((n, tid, "aka"))
                aka_count += 1
            # Anzeigetitel: bevorzugt der deutsche "imdbDisplay"-Titel aus Deutschland
            prio = (0 if r[3] == "DE" else 1) + (0 if "imdbDisplay" in r[5] else 2) + (3 if r[6] != "\\N" else 0)
            if tid not in de_titles or prio < de_titles[tid][0]:
                de_titles[tid] = (prio, r[2])
            if len(batch_n) >= 50000:
                flush()
        flush()

    conn.executemany("UPDATE titles SET title_de = ? WHERE id = ?", [(t, i) for i, (_, t) in de_titles.items()])
    STATE.phase = "Index wird erstellt"
    conn.executescript("""
        CREATE TABLE names2 AS SELECT DISTINCT norm, id, source FROM names;
        DROP TABLE names; ALTER TABLE names2 RENAME TO names;
        CREATE INDEX ix_names_norm2 ON names(norm);
    """)
    stats = {"titles": len(ids), "ratings": conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0],
             "akas": aka_count, "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "dataset_date": time.strftime("%Y-%m-%d", time.gmtime(ratings.stat().st_mtime))}
    conn.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in stats.items()])
    conn.commit()
    conn.close()
    os.replace(tmp, db_path())
    return stats


def status() -> dict:
    d = data_dir()
    files = {k: (d / f).exists() for k, f in FILES.items()}
    meta: dict = {}
    if db_path().exists():
        try:
            with sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True) as c:
                meta = dict(c.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.Error as exc:
            meta = {"error": str(exc)}
    return {"available": bool(meta.get("titles")), "meta": meta, "files": files, "data_dir": str(d),
            "base_url": BASE_URL, "attribution": ATTRIBUTION, "state": STATE.to_dict()}


# ------------------------------------------------------------------ Abgleich
@dataclass
class Candidate:
    imdb_id: str
    title: str
    original_title: str
    year: int | None
    type: str
    runtime: int | None
    genres: str | None
    rating: float | None
    votes: int | None
    title_de: str | None = None
    score: float = 0.0

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Lookup:
    """Lesender Zugriff auf imdb.db (je Thread eine eigene Verbindung)."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True, check_same_thread=False)

    def close(self) -> None:
        self.conn.close()

    def get(self, imdb_id: str) -> Candidate | None:
        tid = tconst_int(imdb_id)
        if tid is None:
            return None
        row = self.conn.execute(
            "SELECT t.id, t.title, t.original_title, t.year, t.type, t.runtime, t.genres, r.rating, r.votes, t.title_de "
            "FROM titles t LEFT JOIN ratings r ON r.id = t.id WHERE t.id = ?", (tid,)).fetchone()
        return self._cand(row) if row else None

    @staticmethod
    def _cand(row) -> Candidate:
        return Candidate(tconst_str(row[0]), row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8],
                         row[9] if len(row) > 9 else None)

    def search(self, title: str, year: int | None, limit: int = 10) -> list[Candidate]:
        norm = normalize_title(title)
        if not norm:
            return []
        rows = self.conn.execute(
            "SELECT DISTINCT t.id, t.title, t.original_title, t.year, t.type, t.runtime, t.genres, r.rating, r.votes, "
            "t.title_de "
            "FROM names n JOIN titles t ON t.id = n.id LEFT JOIN ratings r ON r.id = t.id WHERE n.norm = ?",
            (norm,)).fetchall()
        out = []
        for row in rows:
            c = self._cand(row)
            score = 50.0
            if year and c.year:
                diff = abs(year - c.year)
                score += 40 if diff == 0 else 15 if diff == 1 else -60
            score += {"movie": 8, "tvMovie": 3, "video": 1}.get(c.type, 0)
            score += min((c.votes or 0) / 20000, 10)
            c.score = round(score, 2)
            out.append(c)
        out.sort(key=lambda c: (-c.score, -(c.votes or 0)))
        return out[:limit]


def best_match(cands: list[Candidate], year: int | None) -> tuple[Candidate | None, str]:
    """Wählt den besten Kandidaten. Rückgabe (Kandidat, Sicherheit: high|medium|low)."""
    plausible = [c for c in cands if c.score > 0]
    if not plausible:
        return None, "none"
    best = plausible[0]
    second = plausible[1] if len(plausible) > 1 else None
    exact_year = bool(year and best.year == year)
    if exact_year and (second is None or second.year != year or (best.votes or 0) > 10 * (second.votes or 0)):
        return best, "high"
    if year and best.year and abs(best.year - year) <= 1 and (second is None or best.score - second.score >= 15):
        return best, "medium"
    if not year and (second is None or (best.votes or 0) > 10 * (second.votes or 0)):
        return best, "low"
    if exact_year:
        return best, "medium"
    return None, "ambiguous"


def run_update(include_akas: bool, do_download: bool) -> dict:
    STATE.__init__()  # type: ignore[misc]
    STATE.running, STATE.started = True, time.monotonic()
    try:
        if do_download:
            download(include_akas)
        stats = import_datasets()
        STATE.message = f"IMDb-Daten importiert: {stats['titles']} Filme, {stats['ratings']} Bewertungen"
        return stats
    except InterruptedError:
        STATE.message = "Abgebrochen"
        raise
    except Exception as exc:
        STATE.error = f"{type(exc).__name__}: {exc}"
        log.exception("IMDb-Aktualisierung fehlgeschlagen")
        raise
    finally:
        STATE.running = False
        STATE.phase = None
        STATE.finished = time.monotonic()
        tmp = db_path().with_suffix(".db.tmp")
        if tmp.exists() and not STATE.message:
            tmp.unlink(missing_ok=True)


def clear_downloads() -> None:
    shutil.rmtree(data_dir(), ignore_errors=True)
