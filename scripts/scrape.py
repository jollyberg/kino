#!/usr/bin/env python3
"""
Holt das Berliner Kinoprogramm und schreibt data.json für die Website.

Quellen:
  - indiekino.de  (Programmkinos + Multiplexe, Fassung pro Vorstellung)
  - berlin.de     (Ergänzung für Kinos, die bei indiekino fehlen, z.B. CinemaxX)
  - TMDB          (Originalsprache + Originaltitel, optional über TMDB_TOKEN)
  - OpenStreetMap (Koordinaten der Kinos, einmalig, wird zwischengespeichert)

Nur Python-Standardbibliothek, damit es überall ohne Installation läuft.
"""
import difflib, html, json, os, re, sys, time, unicodedata
import urllib.parse, urllib.request
from datetime import date, datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "cache")
CINEMA_CACHE = os.path.join(CACHE_DIR, "cinemas.json")
FILM_CACHE = os.path.join(CACHE_DIR, "films.json")
OUT = os.path.join(ROOT, "data.json")

UA = "kinotime/1.0 (private Kinoprogramm-Seite)"
TMDB_TOKEN = os.environ.get("TMDB_TOKEN", "").strip()

IK = "https://www.indiekino.de"
# Kinos, die bei indiekino fehlen und von berlin.de kommen: berlin.de-ID -> Name
BERLIN_DE_EXTRA = {
    "30176": {"name": "CinemaxX Potsdamer Platz", "web": "https://www.cinemaxx.de"},
}

META_VERSION = 3  # erhöhen, wenn neue Felder von den Filmseiten gelesen werden sollen
GERMAN_COUNTRIES = {"deutschland", "brd", "ddr", "österreich", "schweiz", "d", "a", "ch"}


# ---------------------------------------------------------------- Hilfen
def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(url, headers=None, tries=3):
    h = {"User-Agent": UA, "Accept-Language": "de"}
    h.update(headers or {})
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            log(f"  Fehler bei {url}: {e}")
            # bei "Too Many Requests" deutlich länger warten
            time.sleep(15 * (i + 1) if "429" in str(e) else 2 * (i + 1))
    return None


def text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, data, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)


def norm_title(t):
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


# ---------------------------------------------------------------- Fassungen
def norm_labels(raw):
    """Rohes Label -> Liste aus {OV, OmU, OmeU} oder ['DFmeU'] (deutsche Fassung engl. UT)."""
    if not raw:
        return []
    out = []
    for part in re.split(r"[/,]", raw):
        p = part.strip().lower()
        if not p:
            continue
        if p in ("ov", "of", "omov"):
            out.append("OV")
        elif p in ("omu", "omdu", "omdtu"):
            out.append("OmU")
        elif p in ("omeu", "omenglu", "omengu", "omu engl", "ome"):
            out.append("OmeU")
        elif p in ("dfmenglu", "dfmeu", "dfengu"):
            out.append("DFmeU")
        else:
            log(f"  Unbekanntes Fassungslabel: {raw!r}")
    return sorted(set(out))


# ---------------------------------------------------------------- indiekino
def parse_indiekino_program(page):
    """Liefert (kinos, vorstellungen) aus der Programmseite (alle Tage)."""
    cinemas, shows = {}, []
    days = re.split(r"<div\s+id='gp_\d+' class='gp\s+d d_", page)[1:]
    for block in days:
        day = block[:10]
        for pa in re.split(r"<div class='pa [^']*' id='pp_\d+_\d+'>", block)[1:]:
            m = re.search(r"<h3 class='kino ([^']*)'><a href='([^']+)'>([^<]+)</a>", pa)
            if not m:
                continue
            cls, url, name = m.groups()
            cid = url.rstrip("/").split("/")[-1]
            cinemas[cid] = {"name": html.unescape(name).strip(), "url": IK + url,
                            "multiplex": "multiplex" in cls, "source": "indiekino"}
            for t in re.findall(r"<div class='termin[^']*' id='i\d+'>(.*?)</div>", pa, re.S):
                tm = re.search(r"class='zi[^']*'>(\d{1,2}:\d{2})<", t)
                if not tm:
                    continue
                fas = re.search(r"class='fassung'>([^<]+)<", t)
                tit = re.search(r"<span class='titel[^']*'>(.*?)</span>", t, re.S)
                link = re.search(r"href='/film/de/([^']+)'", t)
                title = text(tit.group(1)) if tit else ""
                film_key = "ik:" + link.group(1) if link else "ikt:" + norm_title(title)
                tk = re.search(r"class='res_box'[^>]*>[^<]*<a href='(https?://[^']+)'", t)
                shows.append({"d": day, "t": tm.group(1).zfill(5), "c": cid, "f": film_key,
                              "title": title, "raw": fas.group(1) if fas else "",
                              "tk": html.unescape(tk.group(1)) if tk else None})
    return cinemas, shows


def indiekino_film_meta(slug):
    page = fetch(f"{IK}/film/de/{slug}")
    meta = {}
    if not page:
        return meta
    block = re.search(r"<p class='filmangaben'>(.*?)</p>", page, re.S)
    for line in re.split(r"<br\s*/?>", block.group(1) if block else ""):
        line = text(line)
        if line.startswith("Originaltitel:"):
            meta["orig"] = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Regie:"):
            meta["director"] = line.split(":", 1)[1].strip()
            continue
        if line.startswith("Genre:"):
            meta["genre"] = line.split(":", 1)[1].strip()
            continue
        m = re.match(r"^(.*?)\s*(\d{4})(?:,\s*(\d+)\s*min)?$", line)
        if m and "year" not in meta and ":" not in line:
            if m.group(1):
                meta["country"] = m.group(1).strip()
            meta["year"] = int(m.group(2))
            if m.group(3):
                meta["min"] = int(m.group(3))
    imdb = re.search(r"imdb\.com/title/(tt\d+)", page)
    if imdb:
        meta["imdb"] = imdb.group(1)
    meta["v"] = META_VERSION
    return meta


def indiekino_cinema_info(url):
    """Adresse, Koordinaten und Website von der Kinoseite (die Seite enthält eine Karte)."""
    page = fetch(url, {"Cookie": "ik_multiplexe=1"})
    if not page:
        return None, None, None, None
    m = re.search(r"<div class='film'><h2 class=''><a [^>]+>[^<]+</a></h2><p>(.*?)<br", page, re.S)
    addr = text(m.group(1)) if m else None
    ll = re.search(r"LatLng\(\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\)", page)
    lat, lng = (round(float(ll.group(1)), 5), round(float(ll.group(2)), 5)) if ll else (None, None)
    web = re.search(r"target='kinowebsite'[^>]*href='([^']+)'", page)
    return addr, lat, lng, (html.unescape(web.group(1)) if web else None)


# ---------------------------------------------------------------- berlin.de
DE_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2})")


def parse_berlin_de_cinema(cid, page):
    shows = []
    for li in re.split(r"<!--start: trefferlistekino_erstezeile-->", page)[1:]:
        trig = re.search(r'js-accordion__trigger">(.*?)</span>', li, re.S)
        link = re.search(r'filmdetail\.php/(\d+)/', li)
        if not trig or not link:
            continue
        full = text(trig.group(1))
        m = re.match(r"(.*?)\s*\(([^()]*)\)\s*$", full)
        title, title_label = (m.group(1), m.group(2)) if m and norm_labels(m.group(2)) else (full, "")
        for row in re.findall(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>", li, re.S):
            dm = DE_DATE.search(row[0])
            if not dm:
                continue
            d = date(2000 + int(dm.group(3)), int(dm.group(2)), int(dm.group(1))).isoformat()
            for tpart in text(row[1]).split(","):
                tm = re.search(r"(\d{1,2}:\d{2})(?:\s*\(([^)]*)\))?", tpart)
                if not tm:
                    continue
                shows.append({"d": d, "t": tm.group(1).zfill(5), "c": "bd_" + cid,
                              "f": "bd:" + link.group(1), "title": title,
                              "raw": tm.group(2) or title_label, "tk": None})
    return shows


def berlin_de_film_meta(fid):
    page = fetch(f"https://www.berlin.de/kino/_bin/filmdetail.php/{fid}/")
    meta = {}
    if not page:
        return meta
    t = text(page)
    for key, pat in [("director", r"Regie:\s*(.+?)\s+(?:Länge|FSK|Land|Darsteller|Genre):"),
                     ("genre", r"Genre:\s*(.+?)\s+(?:Darsteller|Regie|Länge|FSK|Land):"),
                     ("min", r"Länge:\s*(\d+)\s*min"), ("country", r"Land:\s*([^:]+?)\s+(?:[A-ZÄÖÜ][a-zäöü]+:|$)"),
                     ("start", r"Filmstart:\s*(\d{1,2}\.\d{1,2}\.\d{2})")]:
        m = re.search(pat, t)
        if m:
            meta[key] = int(m.group(1)) if key == "min" else m.group(1).strip()
    if "start" in meta:
        meta["year"] = 2000 + int(meta.pop("start")[-2:])
    meta["v"] = META_VERSION
    return meta


def berlin_de_address(cid):
    page = fetch(f"https://www.berlin.de/kino/_bin/kinodetail.php/{cid}")
    if not page:
        return None, None
    m = re.search(r"Adresse:.*?<dd[^>]*>(.*?)</dd>", page, re.S) or re.search(r"Adresse:(.*?)Telefon", page, re.S)
    addr = text(m.group(1)) if m else None
    if addr:
        addr = re.sub(r"(\d)(\d{5})\s", r"\1, \2 ", addr)  # "Str. 510785" -> "Str. 5, 10785"
        addr = re.sub(r"\s*-\s*[A-ZÄÖÜa-zäöü\- ]+$", "", addr)
    return addr, page


# ---------------------------------------------------------------- TMDB
def tmdb(path, **params):
    if not TMDB_TOKEN:
        return None
    url = "https://api.themoviedb.org/3" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    body = fetch(url, {"Authorization": "Bearer " + TMDB_TOKEN, "Accept": "application/json"})
    try:
        return json.loads(body) if body else None
    except Exception:
        return None


TMDB_VERSION = 4  # erhöhen, damit Filme neu bei TMDB gesucht werden

PREFIX = re.compile(r"^(?:atmos|dolby atmos|imax|d-box|4dx|3d|ov|omu|preview|premiere|vorpremiere|"
                    r"sneak|classic sneak|sneak preview)\s*[:,]\s*", re.I)
SUFFIX = re.compile(r"\s*(?:\b3D\b|\(director'?s cut\)|-\s*extended version|-\s*das kinoerlebnis|"
                    r"\(\d{4}\))\s*$", re.I)
# Veranstaltungen ohne festen Film: nicht bei TMDB suchen (sonst Zufallstreffer)
GENERIC = re.compile(r"sneak|überraschungsfilm|\bquiz|kurzfilm|shorts attack|showcase|best of|kino & kaffee|"
                     r"spatzenkino|^filme von|filmprogramm|\blive\s*:|nt live|national theatre|met opera", re.I)


def clean_title(t):
    t = (t or "").strip()
    for _ in range(3):
        t2 = SUFFIX.sub("", PREFIX.sub("", t)).strip()
        if t2 == t:
            break
        t = t2
    return t


def tmdb_year(r):
    try:
        return int((r.get("release_date") or "")[:4])
    except ValueError:
        return None


def similarity(query, cand):
    """Ähnlichkeit 0..1 zwischen Suchbegriff und TMDB-Titel/Originaltitel."""
    q = norm_title(query)
    best = 0.0
    for t in (cand.get("title"), cand.get("original_title")):
        if t:
            best = max(best, difflib.SequenceMatcher(None, q, norm_title(t)).ratio())
    return best


def tmdb_enrich(meta, title):
    """Sucht den Film bei TMDB. Lieber kein Treffer als ein falscher."""
    meta["tmdb_checked"] = True
    meta["tmdb_v"] = TMDB_VERSION
    for k in ("lang", "langs", "tmdb", "title_en", "wd", "wd_checked"):
        meta.pop(k, None)
    if meta.get("orig_src") == "tmdb":
        meta["orig"] = meta.get("orig_source")
    meta.pop("orig_src", None)
    if GENERIC.search(title or ""):
        return
    res = None
    if meta.get("imdb"):
        r = tmdb(f"/find/{meta['imdb']}", external_source="imdb_id")
        if r and r.get("movie_results"):
            res = r["movie_results"][0]
    queries = []
    for q in (meta.get("orig"), title, (title or "").split(" - ")[0]):
        q = clean_title(q)
        if q and q not in queries:
            queries.append(q)
    year = meta.get("year")
    for q in queries:
        if res:
            break
        if year:
            # Erstveröffentlichung im Jahr (nicht Wiederaufführungen)
            r = tmdb("/search/movie", query=q, primary_release_year=year, language="de-DE")
            for cand in (r or {}).get("results", [])[:5]:
                if similarity(q, cand) >= 0.6:
                    res = cand
                    break
            if res:
                break
        # Ohne Jahresfilter: nur sehr ähnliche Titel, bei bekanntem Jahr mit passendem Jahr (±1)
        r = tmdb("/search/movie", query=q, language="de-DE")
        for cand in (r or {}).get("results", [])[:8]:
            cy = tmdb_year(cand)
            if year and not (cy and abs(cy - year) <= 1):
                continue
            if similarity(q, cand) >= 0.85:
                res = cand
                break
    if res:
        meta["lang"] = res.get("original_language")
        if res.get("original_title"):
            meta["orig_source"] = meta.get("orig")
            meta["orig"] = res["original_title"]
            meta["orig_src"] = "tmdb"
        meta["tmdb"] = res.get("id")
        det = tmdb(f"/movie/{res.get('id')}", language="en-US")
        if det:
            meta["title_en"] = det.get("title")
            if det.get("imdb_id"):
                meta["imdb"] = det["imdb_id"]
            langs = [l.get("iso_639_1") for l in det.get("spoken_languages") or []
                     if l.get("iso_639_1") and l.get("iso_639_1") != "xx"]
            lang = meta["lang"] if meta["lang"] != "xx" else None
            if lang in langs:
                langs.remove(lang)
            meta["langs"] = ([lang] if lang else []) + langs
            if not meta.get("min") and det.get("runtime"):
                meta["min"] = det["runtime"]


def guess_lang_from_country(meta):
    c = (meta.get("country") or "").lower()
    if not c:
        return None
    parts = {p.strip() for p in re.split(r"[/,]| und ", c) if p.strip()}
    return "de" if parts and parts <= GERMAN_COUNTRIES else None


def is_direct_booking(url):
    """True, wenn der Link zu einer bestimmten Vorstellung führt (nicht nur zur Kinoseite)."""
    return bool(re.search(r"kinoheld\.de/.*/vorstellung/\d+|cinetixx\.de/.*eventList/\d+|"
                          r"[?&](show|showId|event|eventId|vorstellung)=\d+|/(event|show|booking|ticket)s?/\d+", url, re.I))


# ---------------------------------------------------------------- Wikidata
def wikidata_languages(imdb_ids):
    """Originalsprache(n) laut Wikidata (Eigenschaft P364) je IMDb-ID, als ISO-Codes."""
    out = {}
    ids = sorted(set(imdb_ids))
    for i in range(0, len(ids), 80):
        values = " ".join(f'"{x}"' for x in ids[i:i + 80])
        q = ("SELECT ?imdb ?code WHERE { VALUES ?imdb { " + values + " } "
             "?film wdt:P345 ?imdb; wdt:P364 ?lang. ?lang wdt:P218 ?code. }")
        body = fetch("https://query.wikidata.org/sparql?" + urllib.parse.urlencode({"query": q, "format": "json"}),
                     {"Accept": "application/sparql-results+json"})
        if body is None:
            return None  # Wikidata nicht erreichbar: nichts ändern
        try:
            for b in json.loads(body)["results"]["bindings"]:
                out.setdefault(b["imdb"]["value"], set()).add(b["code"]["value"])
        except Exception:
            return None
        time.sleep(1)
    return {k: sorted(v) for k, v in out.items()}


def german_countries(country):
    """Alle genannten Produktionsländer deutschsprachig? (None = keine Angabe)"""
    c = (country or "").lower()
    parts = {p.strip() for p in re.split(r"[/,]| und ", c) if p.strip()}
    if not parts:
        return None, False
    return parts <= GERMAN_COUNTRIES, bool(parts & GERMAN_COUNTRIES)


def german_original(tmdb_lang, langs, wd, country):
    """Ist ein Film ohne Fassungsangabe vermutlich im deutschen Original?
    "yes"   = sicher deutschsprachig  -> Badge OV
    "maybe" = Deutsch kommt vor oder es gibt keine Sprachinfo -> Badge OV?
    "no"    = alle Quellen nennen eine andere Sprache -> Synchronfassung"""
    only_de_country, some_de_country = german_countries(country)
    if wd:
        if wd == ["de"] or ("de" in wd and tmdb_lang == "de"):
            return "yes"
    elif tmdb_lang == "de":
        return "yes"
    elif tmdb_lang is None and only_de_country:
        return "yes"
    if "de" in wd or "de" in (langs or []) or tmdb_lang == "de" or some_de_country:
        return "maybe"
    if not wd and not tmdb_lang and only_de_country is None:
        return "maybe"  # gar keine Sprachinfo
    return "no"


# ---------------------------------------------------------------- Geocoding
def geocode(addr):
    m = re.search(r"([^,]*\d+[a-z]?(?:[-/]\d+)?)\s*,\s*(\d{5})\s+Berlin", addr)
    if m:
        addr = f"{m.group(1).strip()}, {m.group(2)} Berlin"
    q = urllib.parse.urlencode({"q": addr, "format": "json", "limit": 1, "countrycodes": "de"})
    body = fetch("https://nominatim.openstreetmap.org/search?" + q)
    time.sleep(1.1)  # Nutzungsregeln: max. 1 Anfrage/Sekunde
    try:
        r = json.loads(body or "[]")
        return (round(float(r[0]["lat"]), 5), round(float(r[0]["lon"]), 5)) if r else (None, None)
    except Exception:
        return (None, None)


# ---------------------------------------------------------------- Ablauf
def main():
    cinemas = load(CINEMA_CACHE, {})
    films = load(FILM_CACHE, {})

    log("indiekino: Programm (mit Multiplexen) laden …")
    page = fetch(IK + "/kinoprogramm/de/berlin/", {"Cookie": "ik_multiplexe=1"})
    if not page:
        sys.exit("indiekino nicht erreichbar – Abbruch, alte data.json bleibt bestehen.")
    ik_cinemas, shows = parse_indiekino_program(page)
    log(f"  {len(ik_cinemas)} Kinos, {len(shows)} Vorstellungen")
    if len(ik_cinemas) < 30 or len(shows) < 300:
        # Seite kaputt oder Layout geändert: lieber alte Daten behalten als eine leere App
        sys.exit(f"Zu wenig Daten von indiekino ({len(ik_cinemas)} Kinos, {len(shows)} Vorstellungen) – "
                 "Abbruch, alte data.json bleibt bestehen.")

    for cid, c in ik_cinemas.items():
        old = cinemas.get(cid, {})
        c.update({k: old[k] for k in ("addr", "lat", "lng", "web") if old.get(k) is not None})
        if not c.get("addr") or c.get("lat") is None or "web" not in old:
            c["addr"], c["lat"], c["lng"], c["web"] = indiekino_cinema_info(c["url"])
            time.sleep(0.3)
        cinemas[cid] = c

    for bid, cfg in BERLIN_DE_EXTRA.items():
        name = cfg["name"]
        log(f"berlin.de: {name} …")
        addr, bpage = berlin_de_address(bid)
        cid = "bd_" + bid
        old = cinemas.get(cid, {})
        cinemas[cid] = {"name": name, "url": f"https://www.berlin.de/kino/_bin/kinodetail.php/{bid}",
                        "multiplex": True, "source": "berlin.de", "addr": old.get("addr") or addr,
                        "web": cfg.get("web"),
                        "lat": old.get("lat"), "lng": old.get("lng")}
        if bpage:
            s = parse_berlin_de_cinema(bid, bpage)
            log(f"  {len(s)} Vorstellungen")
            shows += s

    for cid, c in cinemas.items():
        if c.get("lat") is None and c.get("addr"):
            log(f"Koordinaten: {c['name']} ({c['addr']})")
            c["lat"], c["lng"] = geocode(c["addr"])
    save(CINEMA_CACHE, cinemas)

    # Filmdaten (nur neue Filme abrufen)
    needed = {}
    for s in shows:
        needed.setdefault(s["f"], s["title"])
    for key, title in needed.items():
        meta = films.get(key)
        if meta is not None and meta.get("v", 1) < META_VERSION:
            meta = None  # Quellseiten neu lesen und neu bei TMDB suchen
        if meta is None:
            log(f"Film: {title}")
            if key.startswith("ik:"):
                meta = indiekino_film_meta(key[3:])
            elif key.startswith("bd:"):
                meta = berlin_de_film_meta(key[3:])
                time.sleep(1)  # berlin.de begrenzt die Abfragefrequenz
            else:
                meta = {"v": META_VERSION}
                y = re.search(r"\((\d{4})\)\s*$", title)
                if y:
                    meta["year"] = int(y.group(1))
            meta.setdefault("title", re.sub(r"\s*\(\d{4}\)\s*$", "", title))
            time.sleep(0.15)
        if TMDB_TOKEN and meta.get("tmdb_v", 1) < TMDB_VERSION:
            tmdb_enrich(meta, meta.get("title") or title)
        films[key] = meta
    # Originalsprache laut Wikidata ergänzen (zuverlässiger als TMDB bei Koproduktionen)
    todo = [m["imdb"] for k, m in films.items() if k in needed and m.get("imdb") and not m.get("wd_checked")]
    if todo:
        log(f"Wikidata: {len(todo)} Filme …")
        wd = wikidata_languages(todo)
        if wd is not None:
            for k, m in films.items():
                if m.get("imdb") in todo:
                    m["wd"] = wd.get(m["imdb"], [])
                    m["wd_checked"] = True
    save(FILM_CACHE, films)

    # Ausgabe zusammenbauen
    today = datetime.now().date()
    out_shows, used_c, used_f = [], set(), set()
    for s in shows:
        if date.fromisoformat(s["d"]) < today:
            continue
        o = {"d": s["d"], "t": s["t"], "c": s["c"], "f": s["f"], "v": norm_labels(s["raw"])}
        if s.get("tk"):
            # Kinoheld-Links von indiekino öffnen sonst die nackte Widget-Ansicht
            o["tk"] = re.sub(r"[?&](target|mode|appView)=[^&]*", "", s["tk"]).replace("?&", "?").rstrip("?&")
            if "kinoheld.de" in o["tk"] and "?" not in o["tk"]:
                o["tk"] = o["tk"].replace("&", "?", 1)
            if is_direct_booking(s["tk"]):
                o["tkd"] = 1
        out_shows.append(o)
        used_c.add(s["c"]); used_f.add(s["f"])
    out_shows.sort(key=lambda x: (x["d"], x["t"]))

    out_films = {}
    for k in used_f:
        m = films.get(k, {})
        lang = m.get("lang") or guess_lang_from_country(m)
        langs = [l for l in (m.get("langs") or ([m["lang"]] if m.get("lang") else [])) if l != "xx"]
        wd = m.get("wd") or []
        de_orig = german_original(lang if m.get("lang") else None, langs, wd, m.get("country"))
        if de_orig == "yes" and len(wd) == 1:
            langs = wd + [l for l in langs if l not in wd]
        orig = m.get("orig")
        if de_orig == "yes" and m.get("lang") and m.get("lang") != "de":
            # deutschsprachiger Film, den TMDB einem anderen Land zuordnet: deutscher Titel ist der Originaltitel
            orig = clean_title(m.get("title") or "") or orig
        out_films[k] = {"title": m.get("title") or needed.get(k, ""), "orig": orig,
                        "lang": lang, "de": de_orig, "langs": langs,
                        "min": m.get("min"), "year": m.get("year"), "dir": m.get("director"),
                        "genre": m.get("genre"), "tmdb": m.get("tmdb"), "en": m.get("title_en")}

    out_cinemas = {k: {"name": c["name"], "addr": c.get("addr"), "lat": c.get("lat"), "lng": c.get("lng"),
                       "multiplex": c.get("multiplex", False), "url": c.get("url"), "web": c.get("web")}
                   for k, c in cinemas.items() if k in used_c}

    save(OUT, {"generated": datetime.now().isoformat(timespec="minutes"),
               "tmdb": bool(TMDB_TOKEN), "cinemas": out_cinemas, "films": out_films, "shows": out_shows}, compact=True)
    nolang = sum(1 for f in out_films.values() if not f["lang"])
    nocoord = [c["name"] for c in out_cinemas.values() if c["lat"] is None]
    log(f"Fertig: {len(out_shows)} Vorstellungen, {len(out_films)} Filme ({nolang} ohne Sprache), "
        f"{len(out_cinemas)} Kinos. Ohne Koordinaten: {nocoord or 'keine'}")


if __name__ == "__main__":
    main()
