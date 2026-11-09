"""Collect what already exists around Le Havre — businesses (Sirene) and local
services (BPE) — and the same for benchmark cities, to spot what is missing.

Usage:  python collect_territory.py               everything (~5-10 minutes)
        python collect_territory.py --skip-sirene  only population + BPE (fast)

Sources (free, no key needed):
- geo.api.gouv.fr — communes, population, coordinates
- INSEE Sirene stock file (data.gouv.fr, monthly) — every establishment in France.
  Only the needed columns are read, straight from the remote Parquet file.
- INSEE Melodi API, dataset DS_BPE (yearly) — shops, services, health, education…
"""
import argparse
import io
import math
import os
import sqlite3
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

from collect import DB_PATH, DISTANCE_KM  # also loads .env

# Areas compared: name -> INSEE code of the centre commune
AREAS = {"Le Havre": "76351", "Rouen": "76540", "Caen": "14118", "Brest": "29019"}
MAIN_AREA = "Le Havre"
# Départements that can intersect the circles around those cities
DEPARTEMENTS = ["14", "27", "29", "50", "61", "76", "22", "56", "60", "80"]
FLOW_SINCE = 2015    # first year for creations / closures
FLOW_UNTIL = datetime.now().year   # a few records carry future dates: ignore them

SIRENE_DATASET = "5b7ffc618b4c4169d30727e0"
BPE_URL = "https://api.insee.fr/melodi/data/DS_BPE"
BPE_RANGE = "https://api.insee.fr/melodi/range/DS_BPE"

SCHEMA = """
CREATE TABLE IF NOT EXISTS communes (
    code TEXT PRIMARY KEY, name TEXT, dep TEXT, population INTEGER, lat REAL, lon REAL);
CREATE TABLE IF NOT EXISTS area_communes (area TEXT, code TEXT, dist_km REAL, PRIMARY KEY (area, code));
CREATE TABLE IF NOT EXISTS naf_labels (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS sirene_naf (
    area TEXT, naf TEXT, active INTEGER, employer INTEGER, PRIMARY KEY (area, naf));
CREATE TABLE IF NOT EXISTS sirene_flow (
    area TEXT, naf TEXT, year INTEGER, created INTEGER, closed INTEGER, PRIMARY KEY (area, naf, year));
CREATE TABLE IF NOT EXISTS sirene_etab (
    siret TEXT PRIMARY KEY, naf TEXT, commune TEXT, created TEXT, closed TEXT,
    size TEXT, employer TEXT, head_office TEXT, name TEXT, person INTEGER);
CREATE TABLE IF NOT EXISTS bpe_types (code TEXT PRIMARY KEY, label TEXT, dom TEXT, sdom TEXT);
CREATE TABLE IF NOT EXISTS bpe_labels (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS bpe (area TEXT, type TEXT, year TEXT, count INTEGER, PRIMARY KEY (area, type));
CREATE TABLE IF NOT EXISTS bpe_city (city TEXT, type TEXT, year TEXT, count INTEGER, PRIMARY KEY (city, type));
CREATE TABLE IF NOT EXISTS pop_age (city TEXT, year TEXT, age TEXT, sex TEXT, pop REAL,
    PRIMARY KEY (city, year, age, sex));
CREATE TABLE IF NOT EXISTS territory_runs (run_at TEXT PRIMARY KEY, sirene_file TEXT, bpe_year TEXT, notes TEXT);
"""


def km(lat1, lon1, lat2, lon2):
    r = math.radians
    a = (math.sin(r(lat2 - lat1) / 2) ** 2
         + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))


# --- 1. communes and areas ------------------------------------------------------------
def load_communes(conn):
    print("Communes and population…")
    rows = []
    for dep in DEPARTEMENTS:
        r = requests.get(f"https://geo.api.gouv.fr/departements/{dep}/communes",
                         params={"fields": "code,nom,population,centre"}, timeout=60)
        r.raise_for_status()
        for c in r.json():
            if c.get("centre"):
                lon, lat = c["centre"]["coordinates"]
                rows.append((c["code"], c["nom"], dep, c.get("population") or 0, lat, lon))
    conn.executemany("INSERT OR REPLACE INTO communes VALUES (?,?,?,?,?,?)", rows)
    coords = {r[0]: (r[4], r[5]) for r in rows}
    conn.execute("DELETE FROM area_communes")
    for area, centre in AREAS.items():
        lat0, lon0 = coords[centre]
        for code, (lat, lon) in coords.items():
            d = km(lat0, lon0, lat, lon)
            if d <= DISTANCE_KM:
                conn.execute("INSERT INTO area_communes VALUES (?,?,?)", (area, code, round(d, 1)))
    conn.commit()
    for area, n, pop in conn.execute(
            "SELECT a.area, COUNT(*), SUM(c.population) FROM area_communes a JOIN communes c USING(code) "
            "GROUP BY a.area"):
        print(f"  {area}: {n} communes, {pop:,} inhabitants".replace(",", " "))


def area_map(conn):
    m = defaultdict(list)
    for area, code in conn.execute("SELECT area, code FROM area_communes"):
        m[code].append(area)
    return m


# --- 2. Sirene ------------------------------------------------------------------------
class HttpFile(io.RawIOBase):
    """Seekable read-only file over HTTP range requests (lets pyarrow read only some columns)."""

    def __init__(self, url, size):
        self.url, self.size, self.pos = url, size, 0
        self.session = requests.Session()

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        if n <= 0 or self.pos >= self.size:
            return b""
        end = min(self.pos + n, self.size) - 1
        for attempt in range(5):
            try:
                r = self.session.get(self.url, headers={"Range": f"bytes={self.pos}-{end}"}, timeout=180)
                if r.status_code == 206:
                    break
            except requests.RequestException:
                pass
            time.sleep(2 + 3 * attempt)
        else:
            raise IOError(f"Range request failed at byte {self.pos}")
        self.pos += len(r.content)
        return r.content

    def readinto(self, b):
        d = self.read(len(b))
        b[:len(d)] = d
        return len(d)


SIRENE_COLS = ["siret", "codeCommuneEtablissement", "etatAdministratifEtablissement", "dateCreationEtablissement",
               "dateDebut", "activitePrincipaleEtablissement", "nomenclatureActivitePrincipaleEtablissement",
               "trancheEffectifsEtablissement", "caractereEmployeurEtablissement", "etablissementSiege",
               "statutDiffusionEtablissement", "enseigne1Etablissement", "denominationUsuelleEtablissement"]


def sirene_url():
    r = requests.get(f"https://www.data.gouv.fr/api/1/datasets/{SIRENE_DATASET}/", timeout=60)
    r.raise_for_status()
    for res in r.json()["resources"]:
        t = res["title"]
        if "StockEtablissement " in t + " " and res.get("format") == "parquet" and "Historique" not in t \
                and "Liens" not in t:
            return res["url"], res["title"]
    raise SystemExit("Sirene StockEtablissement parquet not found on data.gouv.fr")


def load_sirene(conn):
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    url, title = sirene_url()
    size = int(requests.head(url, allow_redirects=True, timeout=60).headers["Content-Length"])
    print(f"Sirene: {title}")
    meta = pq.ParquetFile(HttpFile(url, size)).metadata
    n = meta.num_row_groups
    in_area = area_map(conn)
    wanted = pa.array(sorted(in_area))
    local = threading.local()

    def read_group(i):
        if not hasattr(local, "pf"):
            local.pf = pq.ParquetFile(HttpFile(url, size), metadata=meta, pre_buffer=True)
        t = local.pf.read_row_group(i, columns=SIRENE_COLS)
        t = t.filter(pc.is_in(t["codeCommuneEtablissement"], value_set=wanted))
        return t.to_pylist()

    active = defaultdict(lambda: [0, 0])
    flows = defaultdict(lambda: [0, 0])
    etab = []
    done = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for rows in pool.map(read_group, range(n)):
            for e in rows:
                naf = e["activitePrincipaleEtablissement"]
                if not naf or e["nomenclatureActivitePrincipaleEtablissement"] != "NAFRev2":
                    continue
                state = e["etatAdministratifEtablissement"]
                created = str(e["dateCreationEtablissement"] or "")       # dates come as date objects
                closed = str(e["dateDebut"] or "") if state == "F" else ""
                for area in in_area[e["codeCommuneEtablissement"]]:
                    if state == "A":
                        a = active[(area, naf)]
                        a[0] += 1
                        a[1] += e["caractereEmployeurEtablissement"] == "O"
                    if created[:4].isdigit() and FLOW_SINCE <= int(created[:4]) <= FLOW_UNTIL:
                        flows[(area, naf, int(created[:4]))][0] += 1
                    if closed[:4].isdigit() and FLOW_SINCE <= int(closed[:4]) <= FLOW_UNTIL:
                        flows[(area, naf, int(closed[:4]))][1] += 1
                    if area == MAIN_AREA and (state == "A" or (closed[:4].isdigit() and int(closed[:4]) >= FLOW_SINCE)):
                        public = e["statutDiffusionEtablissement"] == "O"   # respect opt-outs
                        etab.append((e["siret"] if public else None, naf, e["codeCommuneEtablissement"],
                                     created, closed, e["trancheEffectifsEtablissement"],
                                     e["caractereEmployeurEtablissement"], e["etablissementSiege"],
                                     (e["enseigne1Etablissement"] or e["denominationUsuelleEtablissement"]) if public else None))
            done += 1
            if done % 40 == 0 or done == n:
                print(f"  row groups: {done}/{n}", flush=True)

    conn.execute("DELETE FROM sirene_naf")
    conn.execute("DELETE FROM sirene_flow")
    conn.execute("DELETE FROM sirene_etab")
    conn.executemany("INSERT INTO sirene_naf VALUES (?,?,?,?)", [(a, n_, v[0], v[1]) for (a, n_), v in active.items()])
    conn.executemany("INSERT INTO sirene_flow VALUES (?,?,?,?,?)",
                     [(a, n_, y, v[0], v[1]) for (a, n_, y), v in flows.items()])
    # Non-public establishments have no SIRET stored: give them a placeholder key
    conn.executemany("INSERT INTO sirene_etab VALUES (?,?,?,?,?,?,?,?,?,?)",
                     [(e[0] or f"hidden-{i}",) + e[1:] + ((0 if e[8] else None),) for i, e in enumerate(etab)])
    conn.commit()
    tot = conn.execute("SELECT area, SUM(active), SUM(employer) FROM sirene_naf GROUP BY area").fetchall()
    for area, a, emp in tot:
        print(f"  {area}: {a:,} active establishments ({emp:,} with employees)".replace(",", " "))
    return title


NAME_COLS = ["siren", "statutDiffusionUniteLegale", "denominationUniteLegale", "sigleUniteLegale",
             "denominationUsuelle1UniteLegale", "nomUniteLegale", "nomUsageUniteLegale", "prenomUsuelUniteLegale"]


def load_names(conn):
    """Company names (from the StockUniteLegale file) for the Le Havre establishments without a sign name."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    cols = [r[1] for r in conn.execute("PRAGMA table_info(sirene_etab)")]
    if "person" not in cols:        # databases created before this column existed
        conn.execute("ALTER TABLE sirene_etab ADD COLUMN person INTEGER")

    r = requests.get(f"https://www.data.gouv.fr/api/1/datasets/{SIRENE_DATASET}/", timeout=60)
    url = next(res["url"] for res in r.json()["resources"]
               if "StockUniteLegale " in res["title"] + " " and res.get("format") == "parquet"
               and "Historique" not in res["title"])
    size = int(requests.head(url, allow_redirects=True, timeout=60).headers["Content-Length"])
    sirens = sorted({s[:9] for (s,) in conn.execute(
        "SELECT siret FROM sirene_etab WHERE person IS NULL AND siret NOT LIKE 'hidden-%'")})
    print(f"Company names for {len(sirens):,} businesses…".replace(",", " "))
    meta = pq.ParquetFile(HttpFile(url, size)).metadata
    wanted = pa.array(sirens)
    local = threading.local()

    def read_group(i):
        if not hasattr(local, "pf"):
            local.pf = pq.ParquetFile(HttpFile(url, size), metadata=meta, pre_buffer=True)
        t = local.pf.read_row_group(i, columns=NAME_COLS)
        return t.filter(pc.is_in(t["siren"], value_set=wanted)).to_pylist()

    names = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for rows in pool.map(read_group, range(meta.num_row_groups)):
            for u in rows:
                if u["statutDiffusionUniteLegale"] != "O":
                    continue
                person = " ".join(x for x in (u["prenomUsuelUniteLegale"],
                                              u["nomUsageUniteLegale"] or u["nomUniteLegale"]) if x)
                company = (u["denominationUniteLegale"] or u["denominationUsuelle1UniteLegale"]
                           or u["sigleUniteLegale"])
                if company or person:
                    # person = 1 when the only name is the owner's own name (sole trader)
                    names[u["siren"]] = (company or person, 0 if company else 1)
    todo = conn.execute("SELECT siret FROM sirene_etab WHERE person IS NULL AND siret NOT LIKE 'hidden-%'").fetchall()
    conn.executemany("UPDATE sirene_etab SET name=?, person=? WHERE siret=?",   # siret is the primary key: fast
                     [names[s[:9]] + (s,) for (s,) in todo if s[:9] in names])
    conn.commit()
    print(f"  {len(names):,} names found".replace(",", " "))


def load_naf_labels(conn):
    """NAF rev2 sub-class labels (732 codes), from the France Travail referential."""
    from ft_api import FranceTravailClient
    cid, secret = os.getenv("FT_CLIENT_ID"), os.getenv("FT_CLIENT_SECRET")
    if not cid or not secret:
        print("  (no France Travail credentials: activity labels skipped)")
        return
    acts = FranceTravailClient(cid, secret).referentiel("activites")
    labels = {f"{a['codeActivite'][:2]}.{a['codeActivite'][2:]}": a["libelleActivite"]   # "8610Z" -> "86.10Z"
              for a in acts if a["codeTypeActivite"] == "NAF732"}
    conn.executemany("INSERT OR REPLACE INTO naf_labels VALUES (?,?)", labels.items())
    conn.commit()
    print(f"  {len(labels)} activity labels")


# --- 3. BPE -----------------------------------------------------------------------------
def load_bpe(conn):
    print("BPE (local services and shops)…")
    rng = requests.get(BPE_RANGE, timeout=60).json()["range"]
    dims = {d["concept"]["code"]: d["values"] for d in rng}
    year = dims["TIME_PERIOD"][0]["code"]
    for key in ("FACILITY_DOM", "FACILITY_SDOM"):
        conn.executemany("INSERT OR REPLACE INTO bpe_labels VALUES (?,?)",
                         [(v["code"], v["label"]["fr"]) for v in dims[key]])
    conn.executemany("INSERT OR REPLACE INTO bpe_types VALUES (?,?,?,?)",
                     [(v["code"], v["label"]["fr"], v["code"][:1], v["code"][:2]) for v in dims["FACILITY_TYPE"]])
    in_area = area_map(conn)
    codes = sorted(in_area)
    counts = defaultdict(int)
    for i in range(0, len(codes), 40):
        batch = codes[i:i + 40]
        params = [("GEO", f"COM-{c}") for c in batch] + [("maxResult", 100000)]
        for attempt in range(5):
            r = requests.get(BPE_URL, params=params, timeout=120)
            if r.status_code == 429:
                time.sleep(10 + 10 * attempt)
                continue
            r.raise_for_status()
            break
        for o in r.json().get("observations", []):
            d = o["dimensions"]
            com = d["GEO"].rsplit("-", 1)[-1]
            v = o["measures"]["OBS_VALUE_NIVEAU"]["value"] or 0
            for area in in_area.get(com, []):
                counts[(area, d["FACILITY_TYPE"])] += int(v)
        time.sleep(2.1)     # Melodi allows ~30 calls per minute
        print(f"  communes: {min(i + 40, len(codes))}/{len(codes)}", flush=True)
    conn.execute("DELETE FROM bpe")
    conn.executemany("INSERT INTO bpe VALUES (?,?,?,?)", [(a, t, year, n) for (a, t), n in counts.items()])
    conn.commit()
    return year


def melodi(url, geos):
    """One Melodi call for a few communes, with retries on the rate limit."""
    params = [("GEO", f"COM-{c}") for c in geos] + [("maxResult", 100000)]
    for attempt in range(5):
        r = requests.get(url, params=params, timeout=120)
        if r.status_code == 429:
            time.sleep(10 + 10 * attempt)
            continue
        r.raise_for_status()
        return r.json().get("observations", [])
    r.raise_for_status()


def load_city(conn):
    """The centre communes alone (not +30 km): services (BPE) and population by age."""
    print("Centre cities: services and population by age…")
    city = {code: name for name, code in AREAS.items()}
    year = requests.get(BPE_RANGE, timeout=60).json()["range"]
    year = {d["concept"]["code"]: d["values"] for d in year}["TIME_PERIOD"][0]["code"]
    rows = []
    for o in melodi(BPE_URL, city):
        d = o["dimensions"]
        rows.append((city[d["GEO"].rsplit("-", 1)[-1]], d["FACILITY_TYPE"], year,
                     int(o["measures"]["OBS_VALUE_NIVEAU"]["value"] or 0)))
    conn.execute("DELETE FROM bpe_city")
    conn.executemany("INSERT OR REPLACE INTO bpe_city VALUES (?,?,?,?)", rows)
    time.sleep(2.1)
    rows = []
    for o in melodi("https://api.insee.fr/melodi/data/DS_RP_POPULATION_PRINC", city):
        d = o["dimensions"]
        if d.get("RP_MEASURE") == "POP":
            rows.append((city[d["GEO"].rsplit("-", 1)[-1]], d["TIME_PERIOD"], d["AGE"], d["SEX"],
                         o["measures"]["OBS_VALUE_NIVEAU"]["value"]))
    conn.execute("DELETE FROM pop_age")
    conn.executemany("INSERT OR REPLACE INTO pop_age VALUES (?,?,?,?,?)", rows)
    conn.commit()
    print(f"  {len(rows)} population figures")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-sirene", action="store_true", help="skip the Sirene business register")
    ap.add_argument("--skip-bpe", action="store_true", help="skip the BPE services database")
    ap.add_argument("--names-only", action="store_true", help="only (re)load company names")
    ap.add_argument("--city-only", action="store_true",
                    help="only the centre cities' services and population by age (seconds)")
    args = ap.parse_args()
    if args.names_only:
        conn = sqlite3.connect(DB_PATH)
        load_names(conn)
        return
    if args.city_only:
        conn = sqlite3.connect(DB_PATH)
        conn.executescript(SCHEMA)
        load_city(conn)
        return
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    started = datetime.now()
    load_communes(conn)
    sirene_title = bpe_year = None
    if not args.skip_bpe:
        bpe_year = load_bpe(conn)
        load_city(conn)
    if not args.skip_sirene:
        load_naf_labels(conn)
        sirene_title = load_sirene(conn)
        load_names(conn)
    conn.execute("INSERT OR REPLACE INTO territory_runs VALUES (?,?,?,?)",
                 (started.isoformat(timespec="seconds"), sirene_title, bpe_year, ""))
    conn.commit()
    conn.close()
    print(f"Done in {(datetime.now() - started).total_seconds() / 60:.1f} min.")


if __name__ == "__main__":
    main()
