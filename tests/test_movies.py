"""Filmübersicht: Titel-/Jahr-Erkennung, IMDb-Import und -Abgleich, Sortierung, manuelle Zuordnung."""

import csv
import gzip
import http.server
import io
import threading
import time
from functools import partial

import pytest

from conftest import files_by_name, wait_job
import media_factory as mf

BASICS = [
    ("tt0097814", "movie", "Kiki's Delivery Service", "Majo no takkyûbin", "0", "1989", "\\N", "103", "Adventure,Animation,Drama"),
    ("tt0133093", "movie", "The Matrix", "The Matrix", "0", "1999", "\\N", "136", "Action,Sci-Fi"),
    ("tt0082096", "movie", "Das Boot", "Das Boot", "0", "1981", "\\N", "149", "Drama,War"),
    ("tt5675620", "tvSeries", "Das Boot", "Das Boot", "0", "2018", "\\N", "60", "Drama"),
    ("tt1375666", "movie", "Inception", "Inception", "0", "2010", "\\N", "148", "Action,Sci-Fi,Thriller"),
    ("tt0062622", "movie", "2001: A Space Odyssey", "2001: A Space Odyssey", "0", "1968", "\\N", "149", "Adventure,Sci-Fi"),
    ("tt0078748", "movie", "Alien", "Alien", "0", "1979", "\\N", "117", "Horror,Sci-Fi"),
    ("tt9999998", "movie", "Alien", "Alien", "0", "2020", "\\N", "90", "Drama"),
    ("tt0211915", "movie", "Amélie", "Le fabuleux destin d'Amélie Poulain", "0", "2001", "\\N", "122", "Comedy,Romance"),
    ("tt9999999", "movie", "Adult Thing", "Adult Thing", "1", "2000", "\\N", "90", "Adult"),
]
RATINGS = [("tt0097814", "7.8", "200000"), ("tt0133093", "8.7", "2100000"), ("tt0082096", "8.3", "270000"),
           ("tt1375666", "8.8", "2600000"), ("tt0062622", "8.3", "720000"), ("tt0078748", "8.5", "950000"),
           ("tt9999998", "5.1", "40"), ("tt0211915", "8.3", "800000")]
AKAS = [("tt0097814", "1", "Kikis kleiner Lieferservice", "DE", "\\N", "imdbDisplay", "\\N", "0"),
        ("tt0062622", "1", "2001: Odyssee im Weltraum", "DE", "\\N", "imdbDisplay", "\\N", "0"),
        ("tt0211915", "1", "Die fabelhafte Welt der Amélie", "DE", "\\N", "imdbDisplay", "\\N", "0"),
        ("tt0133093", "2", "Matrix", "DE", "\\N", "imdbDisplay", "\\N", "0")]


def write_tsv(path, header, rows):
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        fh.write("\t".join(header) + "\n")
        for r in rows:
            fh.write("\t".join(r) + "\n")


def make_datasets(d):
    d.mkdir(parents=True, exist_ok=True)
    write_tsv(d / "title.basics.tsv.gz", ["tconst", "titleType", "primaryTitle", "originalTitle", "isAdult",
                                          "startYear", "endYear", "runtimeMinutes", "genres"], BASICS)
    write_tsv(d / "title.ratings.tsv.gz", ["tconst", "averageRating", "numVotes"], RATINGS)
    write_tsv(d / "title.akas.tsv.gz", ["titleId", "ordering", "title", "region", "language", "types", "attributes",
                                        "isOriginalTitle"], AKAS)


FILES = {
    "Animation/Kikis kleiner Lieferservice (1989).mkv": "tt0097814",
    "Matrix.1999.German.DL.1080p.BluRay.x264-GRP.mkv": "tt0133093",
    "Das Boot (1981)/Das Boot (1981) – Director's Cut.mkv": "tt0082096",
    "Inception (2010)/movie.mkv": "tt1375666",
    "2001 - Odyssee im Weltraum (1968).mkv": "tt0062622",
    "Alien.mkv": "tt0078748",
    "Die fabelhafte Welt der Amélie (2001).mkv": "tt0211915",
    "Unbekannter Heimfilm (2015).mkv": None,
    "nfo/irgendwas.mkv": "tt1375666",
}


@pytest.fixture
def movie_lib(client, app_env, base_media):
    src = mf.make_multi_mkv(app_env["tmp"] / "src" / "multi.mkv", 2)
    for rel in FILES:
        mf.copy(src, app_env["media"] / rel)
    (app_env["media"] / "nfo" / "irgendwas.nfo").write_text(
        '<movie><title>Inception</title><uniqueid type="imdb" default="true">tt1375666</uniqueid></movie>',
        encoding="utf-8")
    j = client.post("/api/jobs", json={"task": "incremental", "methods": ["quick"]}).json()
    wait_job(client, j["id"])
    return app_env


def wait_match(client, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get("/api/movies/imdb").json()
        if not st["match"]["running"] and not st["state"]["running"]:
            return st
        time.sleep(0.2)
    raise AssertionError("Zuordnung hängt")


def import_local(client, app_env):
    make_datasets(app_env["config"] / "imdb")
    r = client.post("/api/movies/imdb/update", json={"download": False})
    assert r.status_code == 202, r.text
    time.sleep(0.3)
    st = wait_match(client)
    assert st["available"], st
    return st


def movies(client, **params):
    return client.get("/api/movies", params={"page_size": 100, **params}).json()


def test_parse_names():
    from app.movies.parse import parse_file
    assert parse_file("Filme/Kikis kleiner Lieferservice (1989).mkv") == ("Kikis kleiner Lieferservice", 1989)
    assert parse_file("Matrix.1999.German.DL.1080p.BluRay.x264-GRP.mkv") == ("Matrix", 1999)
    assert parse_file("Inception (2010)/movie.mkv") == ("Inception", 2010)
    assert parse_file("2001 - Odyssee im Weltraum (1968).mkv") == ("2001 - Odyssee im Weltraum", 1968)
    assert parse_file("Das Boot (1981) – Director's Cut.mkv") == ("Das Boot", 1981)
    assert parse_file("Alien.mkv") == ("Alien", None)


def test_list_without_imdb_data(client, movie_lib):
    st = client.get("/api/movies/imdb").json()
    assert not st["available"] and "Information courtesy of IMDb" in st["attribution"]
    r = movies(client)
    assert r["total"] == len(FILES)
    item = next(i for i in r["items"] if i["filename"].startswith("Kikis"))
    assert item["title"] in ("Kikis kleiner Lieferservice", item["filename"])
    assert [a["language"] for a in item["audio"]] == ["ger", "ger", "eng"]
    assert [a["codec"] for a in item["audio"]] == ["ac3", "dts", "aac"]
    assert [s["language"] for s in item["subtitles"]] == ["ger", "eng"]
    assert item["video"]["width"] == 320 and item["rating"] is None


def test_import_match_sort_filter(client, movie_lib):
    st = import_local(client, movie_lib)
    assert st["meta"]["titles"] == "8" and st["meta"]["akas"] == "4"  # Serie und Erwachseneninhalt nicht übernommen
    r = movies(client, sort="rating", order="desc")
    by_file = {i["filename"]: i for i in r["items"]}
    for rel, imdb_id in FILES.items():
        name = rel.rsplit("/", 1)[-1]
        assert by_file[name]["imdb_id"] == imdb_id, (name, by_file[name])
    kiki = by_file["Kikis kleiner Lieferservice (1989).mkv"]
    assert kiki["rating"] == 7.8 and kiki["votes"] == 200000 and kiki["match_source"] == "title_year"
    assert kiki["match_confidence"] == "high" and kiki["imdb_url"].endswith("/tt0097814/")
    assert {g["label"] for g in kiki["genres"]} == {"Abenteuer", "Animation", "Drama"}
    assert by_file["irgendwas.mkv"]["match_source"] == "nfo"
    assert by_file["Alien.mkv"]["match_confidence"] == "low"  # kein Jahr: wahrscheinlichster Titel
    # Sortierung nach Bewertung absteigend, Filme ohne Bewertung am Ende
    ratings = [i["rating"] for i in r["items"]]
    rated = [x for x in ratings if x is not None]
    assert rated == sorted(rated, reverse=True) and ratings[-1] is None and ratings[0] == 8.8
    asc = [i["rating"] for i in movies(client, sort="rating", order="asc")["items"]]
    assert [x for x in asc if x is not None] == sorted(rated) and asc[-1] is None
    years = [i["year"] for i in movies(client, sort="year", order="asc")["items"] if i["year"]]
    assert years == sorted(years)
    titles = [i["title"] for i in movies(client, sort="title")["items"]]
    assert titles.index("Das Boot") < titles.index("Inception")  # Artikel "Das" wird beim Sortieren ignoriert ("Boot")
    assert titles.index("Die fabelhafte Welt der Amélie") > titles.index("Das Boot")  # "fabelhafte" > "boot"
    # Filter
    assert {i["imdb_id"] for i in movies(client, min_rating=8.5)["items"]} == {"tt0133093", "tt1375666", "tt0078748"}
    assert movies(client, genre="Horror")["total"] == 1
    assert movies(client, audio_lang="de")["total"] == len(FILES)  # "de" = "ger"
    assert movies(client, audio_lang="fre")["total"] == 0
    assert movies(client, matched="no")["total"] == 1
    assert movies(client, q="lieferservice")["total"] == 1
    assert movies(client, q="takkyubin")["total"] == 0 and movies(client, q="majo no")["total"] == 1  # Originaltitel
    amelie = next(i for i in r["items"] if i["imdb_id"] == "tt0211915")
    assert amelie["title"] == "Die fabelhafte Welt der Amélie"  # deutscher Titel bevorzugt
    assert amelie["original_title"] == "Le fabuleux destin d'Amélie Poulain"
    fac = client.get("/api/movies/facets").json()
    assert fac["matched"] == len(FILES) - 1 and fac["rated"] == len(FILES) - 1
    assert any(g["label"] == "Science-Fiction" for g in fac["genres"])
    # CSV-Export mit Umlauten
    raw = client.get("/api/movies/export.csv", params={"sort": "rating", "order": "desc"}).content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw), delimiter=";"))
    assert rows[0][3] == "IMDb-Bewertung" and rows[1][3] == "8,8"
    assert any("Amélie" in r[0] or "Amélie" in r[-1] for r in rows)


def test_manual_match_survives_refresh(client, movie_lib):
    import_local(client, movie_lib)
    fid = files_by_name(client)["Unbekannter Heimfilm (2015).mkv"]["id"]
    cands = client.get(f"/api/movies/{fid}/candidates", params={"q": "Alien"}).json()["items"]
    assert [c["imdb_id"] for c in cands][:2] == ["tt0078748", "tt9999998"]
    r = client.put(f"/api/movies/{fid}/imdb", json={"imdb_id": "tt9999998"})
    assert r.status_code == 200 and r.json()["rating"] == 5.1
    client.post("/api/movies/match")
    wait_match(client)
    item = next(i for i in movies(client)["items"] if i["file_id"] == fid)
    assert item["imdb_id"] == "tt9999998" and item["manual"] and item["match_source"] == "manual"
    # ausdrücklich "keine Zuordnung"
    alien = files_by_name(client)["Alien.mkv"]["id"]
    client.put(f"/api/movies/{alien}/imdb", json={"imdb_id": None})
    item = next(i for i in movies(client)["items"] if i["file_id"] == alien)
    assert item["imdb_id"] is None and item["rating"] is None
    client.delete(f"/api/movies/{alien}/imdb")
    item = next(i for i in movies(client)["items"] if i["file_id"] == alien)
    assert item["imdb_id"] == "tt0078748" and not item["manual"]
    assert client.put(f"/api/movies/{fid}/imdb", json={"imdb_id": "rm -rf"}).status_code == 422


def test_download_from_server(client, movie_lib, tmp_path, monkeypatch):
    srv_dir = tmp_path / "srv"
    make_datasets(srv_dir)
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(srv_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    from app.movies import imdb
    monkeypatch.setattr(imdb, "BASE_URL", f"http://127.0.0.1:{httpd.server_address[1]}")
    try:
        assert client.post("/api/movies/imdb/update", json={"download": True}).status_code == 202
        time.sleep(0.3)
        st = wait_match(client)
        assert st["available"] and all(st["files"].values()), st
        assert not list((movie_lib["config"] / "imdb").glob("*.part"))
        assert movies(client, sort="rating", order="desc")["items"][0]["rating"] == 8.8
    finally:
        httpd.shutdown()


def test_download_failure_keeps_old_data(client, movie_lib, monkeypatch):
    import_local(client, movie_lib)
    from app.movies import imdb
    monkeypatch.setattr(imdb, "BASE_URL", "http://127.0.0.1:9")  # nicht erreichbar
    client.post("/api/movies/imdb/update", json={"download": True})
    time.sleep(0.5)
    st = wait_match(client)
    assert st["state"]["error"] and st["available"]  # alte Daten bleiben nutzbar
    assert movies(client, sort="rating", order="desc")["items"][0]["rating"] == 8.8
