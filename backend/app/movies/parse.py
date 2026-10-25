"""Titel, Jahr und IMDb-ID aus Datei-/Ordnernamen und .nfo-Dateien ermitteln."""

from __future__ import annotations

import os
import re
import unicodedata

IMDB_ID = re.compile(r"\b(tt\d{7,9})\b")
YEAR = re.compile(r"(?<!\d)(19[0-9]{2}|20[0-9]{2})(?!\d)")
# typische Release-Bestandteile, ab denen der Titel endet
JUNK = re.compile(
    r"\b(2160p|1080p|1080i|720p|576p|480p|4k|uhd|hdr10?\+?|hdr|dv|dovi|sdr|bluray|blu-ray|bdrip|brrip|bdremux|remux|"
    r"web-?dl|web-?rip|webrip|web|hdtv|dvdrip|dvd|x264|x265|h\.?264|h\.?265|hevc|avc|xvid|divx|aac|ac3|eac3|dts|"
    r"dts-hd|truehd|atmos|ddp?5\.1|dd5\.1|5\.1|7\.1|german|deutsch|english|dl|ml|multi|dubbed|subbed|proper|repack|"
    r"extended|unrated|uncut|directors[ .]?cut|remastered|imax|internal|limited|complete)\b",
    re.I)
EDITION = re.compile(r"\s+[-–]\s+(director'?s cut|extended( edition| cut)?|special edition|kinofassung|"
                     r"sonderfassung|uncut|ultimate edition|remastered|teil \d+)\s*$", re.I)


def clean_title(raw: str) -> str:
    s = raw.replace("_", " ")
    if s.count(".") >= 2 and " " not in s.strip():
        s = s.replace(".", " ")  # Szene-Schreibweise Film.Name.2001
    s = re.sub(r"[\[{(].*?[\]})]", " ", s)  # Klammerzusätze
    s = EDITION.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" -–._([{")
    return s


def parse_name(name: str) -> tuple[str, int | None]:
    """'Kikis kleiner Lieferservice (1989)' -> ('Kikis kleiner Lieferservice', 1989)."""
    base = os.path.splitext(name)[0] if re.search(r"\.[A-Za-z0-9]{2,4}$", name) else name
    base = IMDB_ID.sub(" ", base)
    year = None
    title_part = base
    years = list(YEAR.finditer(base))
    # Jahr = letztes plausibles Jahr, nicht am Titelanfang (z. B. "2001 – Odyssee im Weltraum (1968)")
    for m in reversed(years):
        if m.start() > 0:
            year = int(m.group(1))
            title_part = base[:m.start()]
            break
    junk = JUNK.search(title_part)
    if junk and junk.start() > 0:
        title_part = title_part[:junk.start()]
    title = clean_title(title_part)
    if not title:
        title = clean_title(base)
    return title, year


def parse_file(rel_path: str) -> tuple[str, int | None]:
    """Titel/Jahr aus dem Dateinamen; Ordnername als Ersatz bei generischen Dateinamen."""
    filename = os.path.basename(rel_path)
    title, year = parse_name(filename)
    parent = os.path.basename(os.path.dirname(rel_path))
    generic = not title or len(title) <= 2 or title.lower() in ("movie", "film", "video", "main", "feature")
    if parent and (generic or year is None):
        p_title, p_year = parse_name(parent)
        if p_year and (generic or year is None) and p_title:
            if generic or normalize_title(p_title).startswith(normalize_title(title)[:6]):
                return p_title, p_year
    return title, year


def imdb_id_from_names(rel_path: str) -> str | None:
    m = IMDB_ID.search(os.path.basename(rel_path)) or IMDB_ID.search(os.path.dirname(rel_path))
    return m.group(1) if m else None


def imdb_id_from_nfo(abs_path: str, library_root: str) -> tuple[str | None, str | None]:
    """Sucht <datei>.nfo bzw. movie.nfo im selben Ordner. Rückgabe (imdb_id, nfo_pfad)."""
    from ..services.pathsec import is_within

    folder = os.path.dirname(abs_path)
    stem = os.path.splitext(os.path.basename(abs_path))[0]
    for cand in (os.path.join(folder, stem + ".nfo"), os.path.join(folder, "movie.nfo")):
        try:
            if not os.path.isfile(cand) or not is_within(cand, library_root):
                continue
            with open(cand, "rb") as fh:
                text = fh.read(512 * 1024).decode("utf-8", "replace")
        except OSError:
            continue
        m = re.search(r'<uniqueid[^>]*type="imdb"[^>]*>\s*(tt\d{7,9})', text, re.I) \
            or re.search(r"<imdbid>\s*(tt\d{7,9})", text, re.I) or IMDB_ID.search(text)
        if m:
            return m.group(1), cand
    return None, None


def normalize_title(s: str) -> str:
    """Vergleichsschlüssel: Umlaute ausgeschrieben, Akzente entfernt, nur Buchstaben/Ziffern."""
    s = s.lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()
