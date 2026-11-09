"""Collect labour-market statistics for Le Havre (+ ~30 km) into a local SQLite database.

Usage:  python collect.py              full refresh (~15 minutes)
        python collect.py --limit 50   quick test on the first 50 jobs
        python collect.py --skip-lbb   skip the La Bonne Boîte company search

The statistics are published quarterly, so running this once a month is enough.
"""
import argparse
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from focus import focus_romes
from ft_api import FranceTravailClient
from sport import SPORT_NAF88, SPORT_ROMES


def load_env(path=".env"):
    """Tiny .env reader so there is no extra dependency."""
    full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(full):
        return
    for line in open(full, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

# --- Area ------------------------------------------------------------------
# France Travail agency area of Le Havre (statistics on offers, jobseekers, hires)
TERRITORY_TYPE = os.getenv("TERRITORY_TYPE", "CLPE")
TERRITORY = os.getenv("TERRITORY", "CLPE76002")
# BMO employment basin of Le Havre (recruitment-tension indicators)
BMO_AREA = os.getenv("BMO_AREA", "2806")
# Centre commune and radius for the company search
COMMUNE = os.getenv("COMMUNE", "76351")
DISTANCE_KM = int(os.getenv("DISTANCE_KM", "30"))
# Areas used as benchmarks in the report (type:code)
BENCHMARKS = os.getenv(
    "BENCHMARKS", "CLPE:CLPE76004,CLPE:CLPE14002,CLPE:CLPE29001,REG:28,NAT:FR").split(",")
LBB_JOBS = int(os.getenv("LBB_JOBS", "80"))      # jobs searched on La Bonne Boîte
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "jobs.db"))

# Indicator endpoint -> (short name, nomenclature, nomenclature whose breakdown we keep)
INDICATORS = {
    "stat-offres": ("offres", "ORIGINEOFF", "TOFF-CUMUL12MOIS"),
    "stat-demandeurs": ("demandeurs", "CATCAND", "ABC"),
    "stat-embauches": ("embauches", "CATCANDxDUREEEMP", "TOUT-TOUTE"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    type TEXT, code TEXT, label TEXT, PRIMARY KEY (type, code));
CREATE TABLE IF NOT EXISTS territories (code TEXT PRIMARY KEY, label TEXT);
CREATE TABLE IF NOT EXISTS stats (
    indicator TEXT, activity_type TEXT, activity TEXT, territory TEXT,
    nomenclature TEXT, period TEXT, value REAL, pct REAL,
    PRIMARY KEY (indicator, activity_type, activity, territory, nomenclature, period));
CREATE TABLE IF NOT EXISTS caract (
    indicator TEXT, activity_type TEXT, activity TEXT, territory TEXT,
    nomenclature TEXT, period TEXT, ctype TEXT, code TEXT, label TEXT,
    nombre REAL, pct REAL, masked INTEGER,
    PRIMARY KEY (indicator, activity_type, activity, territory, nomenclature, ctype, code));
CREATE TABLE IF NOT EXISTS tension (
    rome TEXT, year TEXT, factor TEXT, level INTEGER, score REAL,
    PRIMARY KEY (rome, year, factor));
CREATE TABLE IF NOT EXISTS companies (
    rome TEXT, siret TEXT, name TEXT, naf TEXT, naf_label TEXT, city TEXT, postcode TEXT,
    headcount_min INTEGER, headcount_max INTEGER, hiring_potential REAL,
    high_potential INTEGER, lat REAL, lon REAL, collected_on TEXT,
    PRIMARY KEY (rome, siret));
CREATE TABLE IF NOT EXISTS runs (
    run_at TEXT PRIMARY KEY, territory TEXT, calls INTEGER, errors INTEGER, notes TEXT);
"""


def save_values(conn, indicator, keep_breakdown, values):
    """Store one indicator response (all periods); keep the breakdown of the latest period."""
    latest = {}
    if values:
        conn.execute("INSERT OR REPLACE INTO territories VALUES (?,?)",
                     (values[0]["codeTerritoire"], values[0].get("libTerritoire")))
    for v in values:
        key = (indicator, v["codeTypeActivite"], v["codeActivite"], v["codeTerritoire"],
               v["codeNomenclature"], v["codePeriode"])
        conn.execute("INSERT OR REPLACE INTO stats VALUES (?,?,?,?,?,?,?,?)",
                     key + (v.get("valeurPrincipaleNombre"), v.get("valeurSecondairePourcentage")))
        if v["codeNomenclature"] == keep_breakdown and v["codePeriode"] > latest.get("p", ""):
            latest = {"p": v["codePeriode"], "v": v}
    if latest:
        v = latest["v"]
        base = (indicator, v["codeTypeActivite"], v["codeActivite"], v["codeTerritoire"],
                v["codeNomenclature"])
        conn.execute("DELETE FROM caract WHERE indicator=? AND activity_type=? AND activity=? "
                     "AND territory=? AND nomenclature=?", base)
        for c in v.get("listeValeurParCaract") or []:
            conn.execute("INSERT OR REPLACE INTO caract VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         base + (v["codePeriode"], c.get("codeTypeCaract"), c.get("codeCaract"),
                                 c.get("libCaract"), c.get("nombre"), c.get("pourcentage"),
                                 1 if c.get("masque") else 0))


def fetch_many(client, jobs, label):
    """Run (endpoint, body) jobs in parallel threads; yields (job, values)."""
    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [(j, pool.submit(client.indicator, j[0], **j[1])) for j in jobs]
        for j, f in futures:
            yield j, f.result()
            done += 1
            if done % 200 == 0 or done == len(jobs):
                print(f"  {label}: {done}/{len(jobs)}", flush=True)


def body(ttype, tcode, atype, acode, nomenclature, period="TRIMESTRE"):
    return dict(codeTypeTerritoire=ttype, codeTerritoire=tcode, codeTypeActivite=atype,
                codeActivite=acode, codeTypePeriode=period, codeTypeNomenclature=nomenclature)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only the first N jobs (for testing)")
    ap.add_argument("--skip-lbb", action="store_true", help="skip the company search")
    ap.add_argument("--only-lbb", action="store_true",
                    help="only refresh the company search (uses statistics already in jobs.db)")
    ap.add_argument("--only-sport", action="store_true",
                    help="only collect the sport jobs in the benchmark areas (~3 minutes)")
    args = ap.parse_args()
    if args.only_sport:
        args.only_lbb = args.skip_lbb = True

    cid, secret = os.getenv("FT_CLIENT_ID"), os.getenv("FT_CLIENT_SECRET")
    if not cid or not secret:
        sys.exit("Missing FT_CLIENT_ID / FT_CLIENT_SECRET (put them in .env — see README).")
    client = FranceTravailClient(cid, secret)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    started = datetime.now()
    notes = []

    if not args.only_lbb:
        collect_stats(client, conn, args)
    if not args.only_lbb or args.only_sport:
        collect_sport_benchmarks(client, conn)

    # 6. Companies likely to hire (La Bonne Boîte), for the most relevant jobs
    if not args.skip_lbb:
        targets = pick_lbb_jobs(conn, LBB_JOBS)
        print(f"Companies likely to hire, for {len(targets)} jobs…")
        today = started.date().isoformat()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(zip(targets, pool.map(
                lambda r: client.companies(r, COMMUNE, DISTANCE_KM), targets)))
        for rome, items in results:
            conn.execute("DELETE FROM companies WHERE rome=?", (rome,))
            for c in items:
                loc = c.get("location") or {}
                conn.execute("INSERT OR REPLACE INTO companies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    rome, c.get("siret"), c.get("company_name"), c.get("naf"), c.get("naf_label"),
                    c.get("city"), c.get("postcode"), c.get("headcount_min"), c.get("headcount_max"),
                    c.get("hiring_potential"), 1 if c.get("is_high_potential") else 0,
                    loc.get("lat"), loc.get("lon"), today))
        conn.commit()

    if client.errors:
        notes.append(f"{client.errors} API calls failed")
    conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
                 (started.isoformat(timespec="seconds"), TERRITORY, client.calls, client.errors,
                  "; ".join(notes)))
    conn.commit()
    conn.close()
    mins = (datetime.now() - started).total_seconds() / 60
    print(f"Done in {mins:.1f} min: {client.calls} API calls, {client.errors} errors.")


def collect_stats(client, conn, args):
    # 1. Reference lists: jobs (ROME) and sectors (NAF)
    print("Loading job and sector lists…")
    acts = [a for a in client.referentiel("activites") if a["codeTypeActivite"] in ("ROME", "NAF88", "NAF21")]
    conn.executemany("INSERT OR REPLACE INTO activities VALUES (?,?,?)",
                     [(a["codeTypeActivite"], a["codeActivite"], a["libelleActivite"]) for a in acts])
    romes = sorted(a["codeActivite"] for a in acts if a["codeTypeActivite"] == "ROME")
    nafs = sorted(a["codeActivite"] for a in acts if a["codeTypeActivite"] == "NAF88")
    if args.limit:
        romes = romes[:args.limit]
    ttype = TERRITORY_TYPE

    # 2. Totals for the area and the benchmark areas
    print("Totals for the area and benchmarks…")
    jobs = []
    for t in [f"{ttype}:{TERRITORY}"] + BENCHMARKS:
        tt, tc = t.split(":")
        for ep, (_, nom, _) in INDICATORS.items():
            jobs.append((ep, body(tt, tc, "CUMUL", "CUMUL", nom)))
    for (ep, _), vals in fetch_many(client, jobs, "totals"):
        save_values(conn, INDICATORS[ep][0], INDICATORS[ep][2], vals)
    conn.commit()

    # 3. Offers for every job — tells us which jobs exist in the area
    print(f"Offers for {len(romes)} jobs…")
    active = []
    jobs = [("stat-offres", body(ttype, TERRITORY, "ROME", r, "ORIGINEOFF")) for r in romes]
    for (_, b), vals in fetch_many(client, jobs, "offers"):
        if vals:
            active.append(b["codeActivite"])
            save_values(conn, "offres", "TOFF-CUMUL12MOIS", vals)
    conn.commit()
    print(f"  {len(active)} jobs have data in this area.")

    # 4. Jobseekers, hires and tension for those jobs
    jobs = []
    for r in active:
        jobs.append(("stat-demandeurs", body(ttype, TERRITORY, "ROME", r, "CATCAND")))
        jobs.append(("stat-embauches", body(ttype, TERRITORY, "ROME", r, "CATCANDxDUREEEMP")))
        jobs.append(("stat-perspective-employeur",
                     body("BASBMO", BMO_AREA, "ROME", r, "TYPE_TENSION", period="ANNEE")))
    print(f"Jobseekers, hires and tension ({len(jobs)} calls)…")
    for (ep, _), vals in fetch_many(client, jobs, "details"):
        if ep == "stat-perspective-employeur":
            for v in vals:
                conn.execute("INSERT OR REPLACE INTO tension VALUES (?,?,?,?,?)",
                             (v["codeActivite"], v["codePeriode"], v["codeNomenclature"],
                              v.get("valeurPrincipaleNombre"), v.get("valeurPrincipaleDecimale")))
        else:
            save_values(conn, INDICATORS[ep][0], INDICATORS[ep][2], vals)
    conn.commit()

    # 5. Offers and hires by business sector
    jobs = []
    for n in nafs:
        jobs.append(("stat-offres", body(ttype, TERRITORY, "NAF88", n, "ORIGINEOFF")))
        jobs.append(("stat-embauches", body(ttype, TERRITORY, "NAF88", n, "CATCANDxDUREEEMP")))
    print(f"Sectors ({len(jobs)} calls)…")
    for (ep, _), vals in fetch_many(client, jobs, "sectors"):
        save_values(conn, INDICATORS[ep][0], INDICATORS[ep][2], vals)
    conn.commit()


def collect_sport_benchmarks(client, conn):
    """Sport jobs (sport.py) and the sport sector in the benchmark areas, for the gym tab."""
    jobs = []
    for t in BENCHMARKS:
        tt, tc = t.split(":")
        for r in SPORT_ROMES:
            for ep, (_, nom, _) in INDICATORS.items():
                jobs.append((ep, body(tt, tc, "ROME", r, nom)))
        for n in SPORT_NAF88:
            for ep in ("stat-offres", "stat-embauches"):
                jobs.append((ep, body(tt, tc, "NAF88", n, INDICATORS[ep][1])))
    print(f"Sport jobs in the benchmark areas ({len(jobs)} calls)…")
    for (ep, _), vals in fetch_many(client, jobs, "sport"):
        save_values(conn, INDICATORS[ep][0], INDICATORS[ep][2], vals)
    conn.commit()


def pick_lbb_jobs(conn, n):
    """Half the jobs with the most offers, half the most tense jobs (with real volume)."""
    latest = conn.execute("SELECT MAX(period) FROM stats WHERE indicator='offres' "
                          "AND activity_type='ROME' AND territory=?", (TERRITORY,)).fetchone()[0]
    by_volume = [r[0] for r in conn.execute(
        "SELECT activity FROM stats WHERE indicator='offres' AND activity_type='ROME' "
        "AND territory=? AND nomenclature='TOFF-CUMUL12MOIS' AND period=? ORDER BY value DESC",
        (TERRITORY, latest))]
    year = conn.execute("SELECT MAX(year) FROM tension").fetchone()[0]
    tense = [r[0] for r in conn.execute(
        "SELECT t.rome FROM tension t JOIN stats s ON s.activity=t.rome AND s.indicator='offres' "
        "AND s.activity_type='ROME' AND s.territory=? AND s.nomenclature='TOFF-CUMUL12MOIS' "
        "AND s.period=? WHERE t.factor='PERSPECTIVE' AND t.year=? AND s.value>=30 "
        "ORDER BY t.score DESC", (TERRITORY, latest, year))]
    picked = []
    for r in by_volume[:n // 2] + tense[:n]:
        if r not in picked:
            picked.append(r)
        if len(picked) >= n:
            break
    # Jobs of the dashboard focus sections, when present in the area
    present = {r[0] for r in conn.execute(
        "SELECT DISTINCT activity FROM stats WHERE indicator='offres' AND activity_type='ROME' "
        "AND territory=?", (TERRITORY,))}
    return picked + [r for r in focus_romes() if r in present and r not in picked]


if __name__ == "__main__":
    main()
