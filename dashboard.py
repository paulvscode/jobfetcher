"""Build an interactive dashboard (dashboard.html) from jobs.db.

Usage:  python dashboard.py   →  writes dashboard.html (open it in your browser)

Lets you browse themes (tech, health, port & logistics…), jobs and business
sectors, with their quarterly trends since 2020.
"""
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime

from focus import FOCUS

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "jobs.db"))
OUT_PATH = os.getenv("DASHBOARD_PATH", os.path.join(os.path.dirname(__file__), "dashboard.html"))
TERRITORY = os.getenv("TERRITORY", "CLPE76002")

def clean(label):
    """Fix the Œ/œ characters that the France Travail referential sends as control codes."""
    return label.replace("", "œ").replace("", "Œ") if isinstance(label, str) else label


# Order of the tension factors sent to the page (PERSPECTIVE = overall level)
FACTORS = ["PERSPECTIVE", "MAIN_OEUVRE", "SPECIF_FORM_EMPL", "COND_TRAVAIL", "MISMATCH_GEO",
           "INT_EMB", "DUR_EMPL", "ATTR_SALARIALE"]


def load_local(conn):
    """Businesses (Sirene) and services (BPE) per area, from collect_territory.py. None if absent."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "area_communes" not in tables:
        return None
    pop = dict(conn.execute("SELECT a.area, SUM(c.population) FROM area_communes a "
                            "JOIN communes c USING(code) GROUP BY a.area"))
    ncom = dict(conn.execute("SELECT area, COUNT(*) FROM area_communes GROUP BY area"))
    areas = ["Le Havre"] + sorted(a for a in pop if a != "Le Havre")
    out = {"areas": [{"n": a, "pop": pop[a], "com": ncom[a]} for a in areas], "naf": [], "bpe": [], "div": {}}

    if "sirene_naf" in tables:
        labels = {c: clean(l) for c, l in conn.execute("SELECT code, label FROM naf_labels")}
        cnt = defaultdict(dict)
        for area, naf, active, employer in conn.execute("SELECT * FROM sirene_naf"):
            cnt[naf][area] = [active, employer]
        flow = defaultdict(dict)
        this_year = datetime.now().year
        years = [r[0] for r in conn.execute("SELECT DISTINCT year FROM sirene_flow WHERE year <= ? ORDER BY year",
                                            (this_year,))]
        out["full_years"] = [y for y in years if y < this_year]
        for area, naf, year, created, closed in conn.execute("SELECT * FROM sirene_flow"):
            flow[(naf, area)][year] = (created, closed)
        out["years"] = years
        for naf in sorted(set(cnt) | {k[0] for k in flow}):
            row = {"c": naf, "l": labels.get(naf, naf), "n": [cnt[naf].get(a, [0, 0]) for a in areas]}
            # creations / closures per year, for each area
            row["f"] = [[list(flow.get((naf, a), {}).get(y, (0, 0))) for y in years] for a in areas]
            out["naf"].append(row)
        out["div"] = {r[0]: clean(r[1]) for r in conn.execute("SELECT code, label FROM activities WHERE type='NAF88'")}
        run = conn.execute("SELECT sirene_file FROM territory_runs WHERE sirene_file IS NOT NULL "
                           "ORDER BY run_at DESC LIMIT 1").fetchone()
        out["sirene"] = run[0].split(" - ")[-1].replace("(format parquet)", "").strip() if run else ""

    if "bpe" in tables:
        lab = dict(conn.execute("SELECT code, label FROM bpe_labels"))
        cnt = defaultdict(dict)
        for area, typ, year, n in conn.execute("SELECT area, type, year, count FROM bpe"):
            cnt[typ][area] = n
            out["bpe_year"] = year
        for code, label, dom, sdom in conn.execute("SELECT * FROM bpe_types ORDER BY code"):
            if code in cnt:
                out["bpe"].append({"c": code, "l": label, "dom": dom, "sdom": sdom,
                                   "n": [cnt[code].get(a, 0) for a in areas]})
        out["bpe_labels"] = lab
    return out


GYMS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gyms.json")
# Territories compared in the gym tab (France Travail codes)
GYM_TERR = [TERRITORY, "CLPE76004", "CLPE14002", "CLPE29001", "28", "FR"]
SIZE_MIN = {"21": 50, "22": 100, "31": 200, "32": 250, "41": 500, "42": 1000, "51": 2000, "52": 5000, "53": 10000}


def load_quality(conn):
    """Results of validate.py (prices, company register, freshness, statistics), or None."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='data_quality'").fetchone():
        return None
    row = conn.execute("SELECT data FROM data_quality").fetchone()
    return json.loads(row[0]) if row else None


def load_club_section(conn):
    """Client file (club.json, see club.py) and the website audits (site_audit.py), or None."""
    from club import load_club
    club = load_club()
    if not club:
        return None
    audits = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='site_audit'").fetchone():
        for site, checked, raw in conn.execute("SELECT site, checked, data FROM site_audit"):
            d = json.loads(raw)
            d["checked"] = checked
            d.pop("sitemap_urls", None)
            audits[site] = d
    crawl = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='site_crawl'").fetchone():
        row = conn.execute("SELECT checked, data FROM site_crawl WHERE site=?", (club.get("name"),)).fetchone()
        if row:
            crawl = json.loads(row[1])
            crawl["checked"] = row[0]
    # The code that draws the crawl tree and recommendations travels inside the section,
    # so on the public site it is encrypted with the data.
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "club_crawl.js"), encoding="utf-8") as f:
        ui = f.read()
    return {"club": club, "audits": audits, "crawl": crawl, "ui": ui}


def load_gym(conn, labels, tension):
    """Data for the "Salles de sport" tab: competitors (gyms.json) and the centre city's figures."""
    from sport import SPORT_BPE, SPORT_NAF, SPORT_NAF88, SPORT_ROMES
    out = {"gyms": None}
    if os.path.exists(GYMS_PATH):
        with open(GYMS_PATH, encoding="utf-8") as f:
            out["gyms"] = json.load(f)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    # Centre cities (not +30 km)
    from collect_territory import AREAS
    cities = []
    if "communes" in tables:
        pop = dict(conn.execute("SELECT code, population FROM communes"))
        cities = [{"n": n, "code": c, "pop": pop.get(c, 0)} for n, c in AREAS.items()]
    out["cities"] = cities
    codes = [c["code"] for c in cities]

    this_year = datetime.now().year
    out["years"] = list(range(2015, this_year + 1))
    out["naf"] = []
    if "sirene_etab" in tables and cities:
        naf_labels = {c: clean(l) for c, l in conn.execute("SELECT code, label FROM naf_labels")}
        # the establishment list only covers the main area: other cities get None
        listed = {c for c in codes if conn.execute("SELECT 1 FROM sirene_etab WHERE commune=? LIMIT 1", (c,)).fetchone()}
        for naf, short in SPORT_NAF.items():
            n = [conn.execute("SELECT COUNT(*), SUM(employer='O') FROM sirene_etab WHERE naf=? AND commune=? "
                              "AND closed=''", (naf, c)).fetchone() if c in listed else (None, None) for c in codes]
            created = dict(conn.execute("SELECT CAST(substr(created,1,4) AS INT) y, COUNT(*) FROM sirene_etab "
                                        "WHERE naf=? AND commune=? GROUP BY y", (naf, codes[0])))
            closed = dict(conn.execute("SELECT CAST(substr(closed,1,4) AS INT) y, COUNT(*) FROM sirene_etab "
                                       "WHERE naf=? AND commune=? AND closed!='' GROUP BY y", (naf, codes[0])))
            out["naf"].append({"c": naf, "l": short, "full": naf_labels.get(naf, naf),
                               "n": [x[0] for x in n], "emp": [x[1] for x in n],
                               "f": [[created.get(y, 0), closed.get(y, 0)] for y in out["years"]]})
        # Big employers of the centre city: targets for company memberships
        rows = conn.execute(
            "SELECT e.name, e.naf, e.size FROM sirene_etab e WHERE e.commune=? AND e.closed='' "
            "AND e.size GLOB '[2-5][0-9]' AND e.person=0 AND e.siret NOT LIKE 'hidden-%' "
            "ORDER BY e.size DESC, e.name", (codes[0],)).fetchall()
        bands = {"50–99": 0, "100–249": 0, "250–999": 0, "1 000 et plus": 0}
        for _, _, size in rows:
            m = SIZE_MIN.get(size, 0)
            bands["50–99" if m < 100 else "100–249" if m < 250 else "250–999" if m < 1000 else "1 000 et plus"] += 1
        out["employers"] = {"bands": bands, "total": len(rows),
                            "top": [[clean(n), naf_labels.get(naf, naf), size] for n, naf, size in rows[:60]]}

    out["bpe"] = []
    if "bpe_city" in tables:
        cnt = defaultdict(dict)
        for city, typ, year, n in conn.execute("SELECT * FROM bpe_city"):
            cnt[typ][city] = n
            out["bpe_year"] = year
        out["bpe"] = [{"c": t, "l": l, "n": [cnt[t].get(c["n"], 0) for c in cities]} for t, l in SPORT_BPE.items()]

    out["ages"] = {}
    if "pop_age" in tables:
        ages = defaultdict(lambda: defaultdict(dict))
        for city, year, age, pop in conn.execute("SELECT city, year, age, pop FROM pop_age WHERE sex='_T'"):
            ages[city][year][age] = round(pop)
        out["ages"] = ages

    # Sport jobs: latest 12-month offers, a year earlier, jobseekers, hires over 12 months
    def figures(atype, act, terr):
        def series(ind, nom):
            return [v for _, v in conn.execute(
                "SELECT period, value FROM stats WHERE indicator=? AND activity_type=? AND activity=? "
                "AND territory=? AND nomenclature=? ORDER BY period", (ind, atype, act, terr, nom))]
        o, d, e = series("offres", "TOFF-CUMUL12MOIS"), series("demandeurs", "ABC"), series("embauches", "TOUT-TOUTE")
        if not (o or d or e):
            return None
        return [o[-1] if o else None, o[-5] if len(o) >= 5 else None, d[-1] if d else None,
                sum(e[-4:]) if e else None]
    jobs = []
    for code, short in SPORT_ROMES.items():
        t = tension.get(code)
        lvl = t[max(t)][0] if t else None
        jobs.append({"c": code, "l": short, "full": labels.get(("ROME", code), code), "t": lvl,
                     "v": [figures("ROME", code, terr) for terr in GYM_TERR]})
    for n in SPORT_NAF88:
        jobs.append({"c": n, "l": "Secteur : " + labels.get(("NAF88", n), n), "sector": True,
                     "v": [figures("NAF88", n, terr) for terr in GYM_TERR]})
    out["jobs"] = jobs
    terr = dict(conn.execute("SELECT code, label FROM territories"))
    out["terr"] = [terr.get(t, t).title() if t in ("28", "FR") else terr.get(t, t) for t in GYM_TERR]
    return out


def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit("No jobs.db yet — run collect.py first.")
    conn = sqlite3.connect(DB_PATH)
    labels = {(t, c): clean(l) for t, c, l in conn.execute("SELECT type, code, label FROM activities")}
    terr = dict(conn.execute("SELECT code, label FROM territories"))

    # series[(activity_type, activity)][key][period] = value
    wanted = {("offres", "TOFF"): "o", ("offres", "TOFF-CUMUL12MOIS"): "o12",
              ("demandeurs", "ABC"): "d", ("embauches", "TOUT-TOUTE"): "e"}
    series = defaultdict(lambda: defaultdict(dict))
    periods = set()
    for ind, at, act, nom, per, val in conn.execute(
            "SELECT indicator, activity_type, activity, nomenclature, period, value FROM stats "
            "WHERE territory=?", (TERRITORY,)):
        key = wanted.get((ind, nom))
        if key:
            series[(at, act)][key][per] = val
            periods.add(per)
    if not periods:
        raise SystemExit("No statistics in jobs.db — run collect.py first.")
    quarters = sorted(periods)

    def arr(d):
        return [d.get(q) for q in quarters]

    def item(at, code):
        s = series[(at, code)]
        return {"c": code, "l": labels.get((at, code), code),
                **{k: arr(s.get(k, {})) for k in ("o", "o12", "d", "e")}}

    tension = defaultdict(lambda: defaultdict(lambda: [None] * len(FACTORS)))
    for rome, year, factor, level, _ in conn.execute("SELECT * FROM tension"):
        if factor in FACTORS:
            tension[rome][year][FACTORS.index(factor)] = level

    jobs = []
    for (at, code) in series:
        if at != "ROME":
            continue
        it = item(at, code)
        if not any(it["o12"]) and not any(it["d"]):
            continue        # job absent from the area
        if code in tension:
            it["t"] = dict(sorted(tension[code].items()))
        jobs.append(it)
    sectors = [item(at, code) for (at, code) in series if at == "NAF88" and any(series[(at, code)]["o12"].values())]

    companies = defaultdict(list)
    for rome, name, naf_label, city, hmin, hmax, high in conn.execute(
            "SELECT rome, name, naf_label, city, headcount_min, headcount_max, high_potential "
            "FROM companies ORDER BY hiring_potential DESC"):
        if len(companies[rome]) < 15:
            companies[rome].append([name, naf_label, city, f"{hmin}–{hmax}" if hmax else "", high])

    local = load_local(conn)
    last_run = conn.execute("SELECT MAX(run_at) FROM runs").fetchone()[0] or ""
    data = {
        "q": quarters, "area": terr.get(TERRITORY, TERRITORY), "run": last_run[:10],
        "total": item("CUMUL", "CUMUL"), "jobs": jobs, "sectors": sectors,
        "companies": companies, "factors": FACTORS,
        "focus": [{"key": k, **v} for k, v in FOCUS.items()],
        "local": local,
        "gym": load_gym(conn, labels, tension),
        "quality": load_quality(conn),
    }
    # Client section: plain on your computer, encrypted on the public site (publish.py sets CLUB_ENCRYPT=1)
    section = load_club_section(conn)
    if section and os.getenv("CLUB_ENCRYPT") == "1":
        from club import encrypt, password
        pwd = password()
        if pwd:
            data["clubEnc"] = encrypt(json.dumps(section, ensure_ascii=False).encode(), pwd)
        else:
            print("CLUB_PASSWORD not set: client section left out of the public site")
    elif section:
        data["club"] = section
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(TEMPLATE.replace("__DATA__", payload))
    print(f"Dashboard written to {OUT_PATH} ({len(jobs)} jobs, {len(sectors)} sectors)")


TEMPLATE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tendances emploi Le Havre</title>
<script>try{const t=localStorage.getItem("theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t;}catch(e){}</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{color-scheme:light;--bg:#f6f5f1;--card:#fff;--ink:#1c1c1a;--muted:#67665f;--line:#e3e1d9;--soft:#eef3f5;
--accent:#1f6f8b;--on-accent:#fff;--up:#2e7d4f;--down:#b54a3c;--focus:#1f6f8b;
--tip-bg:#1c1c1a;--tip-ink:#fff;--shadow:0 1px 2px rgba(0,0,0,.04);
--lv1:rgba(46,125,79,.20);--lv2:rgba(46,125,79,.09);--lv3:rgba(120,120,120,.10);--lv4:rgba(208,138,44,.26);--lv5:rgba(181,74,60,.30);
--c1:#1f6f8b;--c2:#d08a2c;--c3:#7a5bb5;--c4:#2e7d4f;--c5:#b54a3c;--c6:#5d6b78}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#111316;--card:#1a1d21;--ink:#e9e9e6;--muted:#a09f99;--line:#2c3137;--soft:#222a31;
--accent:#62b6d3;--on-accent:#0c1a20;--up:#72c793;--down:#ee8f80;--focus:#8fd0e6;
--tip-bg:#e9e9e6;--tip-ink:#111316;--shadow:0 1px 2px rgba(0,0,0,.4);
--lv1:rgba(114,199,147,.22);--lv2:rgba(114,199,147,.10);--lv3:rgba(200,200,200,.08);--lv4:rgba(229,165,77,.28);--lv5:rgba(238,143,128,.32);
--c1:#62b6d3;--c2:#e5a54d;--c3:#ab92e4;--c4:#72c793;--c5:#ee8f80;--c6:#a3b1be}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#111316;--card:#1a1d21;--ink:#e9e9e6;--muted:#a09f99;--line:#2c3137;--soft:#222a31;
--accent:#62b6d3;--on-accent:#0c1a20;--up:#72c793;--down:#ee8f80;--focus:#8fd0e6;
--tip-bg:#e9e9e6;--tip-ink:#111316;--shadow:0 1px 2px rgba(0,0,0,.4);
--lv1:rgba(114,199,147,.22);--lv2:rgba(114,199,147,.10);--lv3:rgba(200,200,200,.08);--lv4:rgba(229,165,77,.28);--lv5:rgba(238,143,128,.32);
--c1:#62b6d3;--c2:#e5a54d;--c3:#ab92e4;--c4:#72c793;--c5:#ee8f80;--c6:#a3b1be}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{padding:20px 16px 0;max-width:1200px;margin:0 auto;display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
h1{font-size:22px;margin:0}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:999px;background:var(--card);padding:2px}
.seg button{border:none;background:none;color:var(--muted);font:inherit;font-size:13px;padding:5px 12px;border-radius:999px;cursor:pointer}
.seg button[aria-pressed="true"]{background:var(--soft);color:var(--ink);font-weight:600}
:focus-visible{outline:2px solid var(--focus);outline-offset:2px}
body,.card,.tw,input,select,.chip,.pin{transition:background-color .2s,border-color .2s,color .2s}
.sub{color:var(--muted);font-size:13px;margin-top:2px}
main{max-width:1200px;margin:0 auto;padding:12px 16px 60px}
nav{display:flex;gap:6px;margin:14px 0;flex-wrap:wrap}
nav button,.chip{border:1px solid var(--line);background:var(--card);color:var(--ink);padding:7px 14px;border-radius:999px;cursor:pointer;font:inherit}
nav button.on{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;box-shadow:var(--shadow)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.theme{cursor:pointer;transition:border-color .15s}.theme:hover,.theme.on{border-color:var(--accent)}
.theme h3{font-size:14px;margin:0 0 6px}.big{font-size:22px;font-weight:650;font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}.up{color:var(--up)}.down{color:var(--down)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:10px 0}
input,select{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--ink)}
input[type=search]{flex:1;min-width:180px}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%}th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
td.l{white-space:normal;min-width:220px}th{font-size:12px;color:var(--muted);font-weight:600;cursor:pointer;user-select:none}
tbody tr{cursor:pointer}tbody tr:hover{background:var(--soft)}tr.sel{background:var(--soft)}
.n{text-align:right;font-variant-numeric:tabular-nums}
.spark{width:110px;height:26px;display:block}
#detail{margin:16px 0}.dh{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap}
.dh h2{font-size:18px;margin:0}.kp{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}
.kp div{background:var(--soft);border-radius:10px;padding:10px}.kp b{display:block;font-size:19px;font-variant-numeric:tabular-nums}.kp span{font-size:12px;color:var(--muted)}
.cw{position:relative;height:280px}
.two{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;margin-top:14px}
.heat td{text-align:center;font-variant-numeric:tabular-nums;padding:5px 7px}.heat td.f{text-align:left}
.lv1{background:var(--lv1)}.lv2{background:var(--lv2)}.lv3{background:var(--lv3)}.lv4{background:var(--lv4)}.lv5{background:var(--lv5)}
code{background:var(--soft);padding:1px 5px;border-radius:4px}
.x{border:none;background:none;color:var(--muted);font-size:20px;cursor:pointer}
.note{font-size:12px;color:var(--muted);margin:6px 0}
.pin{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:8px;padding:5px 10px;cursor:pointer;font:inherit}
#cmp{margin-bottom:14px}#cmp .chips{display:flex;gap:6px;flex-wrap:wrap;margin:6px 0}
.chip{padding:4px 10px;font-size:12px}.chip i{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:999px;background:var(--soft);color:var(--ink);margin:1px 3px 1px 0;white-space:nowrap}
#gView{flex-shrink:0}
.st{display:inline-flex;width:18px;height:18px;border-radius:50%;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0;color:var(--ink)}
.st.up{background:var(--lv1)}.st.warn{background:var(--lv4)}.st.down{background:var(--lv5)}.st.muted{background:var(--lv3)}
.plain tbody tr{cursor:default}.plain tbody tr:hover{background:none}tr.me,.plain tbody tr.me:hover{background:var(--lv2)}
.pl{margin:6px 0 0;padding-left:20px}.pl li{margin-bottom:6px}
.chk{display:flex;gap:8px;align-items:flex-start;padding:6px 0;border-bottom:1px solid var(--line)}.chk:last-child{border-bottom:none}
.tree ul{list-style:none;margin:0;padding-left:16px;border-left:1px solid var(--line)}.tree>ul{border-left:none;padding-left:0}
.tree li{margin:3px 0}.tree summary{cursor:pointer}.tree .nd{display:inline-flex;gap:6px;align-items:baseline;flex-wrap:wrap}
.tree .pth{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all}.tree .meta{font-size:11px;color:var(--muted)}
.tree .grp{font-weight:600}.fl{font-size:11px;padding:0 6px;border-radius:999px;background:var(--lv4);white-space:nowrap}.fl.bad{background:var(--lv5)}.fl.ok{background:var(--lv1)}
.reco{border:1px solid var(--line);border-radius:10px;padding:12px;margin-bottom:10px;background:var(--card)}
.reco h4{margin:0 0 4px;font-size:14px}.reco .why{font-size:13px;margin:6px 0}
.paste{position:relative;margin-top:8px}.paste pre{background:var(--soft);border-radius:8px;padding:10px;max-height:320px;overflow:auto;font-size:12px;white-space:pre-wrap;word-break:break-word;margin:4px 0 0}
.paste .pin{position:absolute;right:6px;top:22px;font-size:12px;padding:2px 8px}
@media (max-width:600px){.spark{width:70px}td.l{min-width:160px}}
</style></head><body>
<header><div><h1>Tendances de l'emploi — Le Havre</h1><div class="sub" id="sub"></div></div>
<div class="seg" id="themeSeg" role="group" aria-label="Thème d'affichage">
<button data-mode="auto" aria-pressed="false">Auto</button><button data-mode="light" aria-pressed="false">Clair</button><button data-mode="dark" aria-pressed="false">Sombre</button></div></header>
<main>
<nav id="tabs"><button data-t="themes" class="on">Thèmes</button><button data-t="jobs">Métiers</button><button data-t="sectors">Secteurs d'activité</button><button data-t="local">Offre locale</button><button data-t="gym">Salles de sport</button></nav>

<section id="cmp" class="card" hidden>
 <div class="dh"><div><b>Comparaison</b> <span class="muted">· offres sur 12 mois glissants</span></div><button class="pin" id="cmpClear">Vider</button></div>
 <div class="chips" id="cmpChips"></div><div class="cw"><canvas id="cmpChart"></canvas></div>
</section>

<section id="v-themes"><div class="grid" id="themeGrid"></div></section>

<section id="v-focus" hidden></section>

<section id="v-local" hidden>
 <div class="card"><h2 style="margin:0 0 4px;font-size:18px">Ce qui existe déjà… et ce qui manque</h2>
 <p class="muted" style="margin:0" id="lIntro"></p></div>
 <div class="row" style="margin-top:14px">
  <div class="seg" id="lSrc"><button data-s="naf" aria-pressed="true">Entreprises (Sirene)</button><button data-s="bpe" aria-pressed="false">Services et commerces (BPE)</button></div>
  <input type="search" id="lq" placeholder="Rechercher une activité (ex. médecin, boulangerie, 86.90E)…">
  <select id="lLevel"><option value="div">Par division (88)</option><option value="naf" selected>Activité détaillée (732)</option></select>
  <select id="lDom"></select>
  <select id="lSort"><option value="gap">Trier : plus gros manque</option><option value="idx">Trier : indice le plus bas</option>
   <option value="hi">Trier : spécialités du Havre</option><option value="n">Trier : nombre</option><option value="net">Trier : solde créations</option></select>
  <label class="muted" id="lEmpWrap"><input type="checkbox" id="lEmp"> avec salariés uniquement</label>
  <label class="muted" id="lHoldWrap"><input type="checkbox" id="lHold" checked> masquer holdings, SCI, associations, panneaux solaires</label>
 </div>
 <ul class="summary" id="lSummary"></ul>
 <div class="tw" style="margin-top:12px"><table><thead id="lth"></thead><tbody id="ltb"></tbody></table></div>
 <p class="note" id="lcnt"></p>
 <p class="note">Indice = densité au Havre ÷ moyenne des villes de comparaison × 100 (100 = même niveau). « Manque » = nombre d'établissements à ajouter pour atteindre
 la densité moyenne des autres villes. Une activité sous-représentée n'est pas forcément une opportunité : vérifiez la demande (population, revenus, emplois) et la concurrence voisine.
 Sirene compte aussi les micro-entreprises peu actives : cochez « avec salariés » pour une vue plus prudente.</p>
</section>

<section id="v-gym" hidden>
 <div class="card"><div class="dh"><div><h2 style="margin:0 0 4px;font-size:18px">Salles de sport — ville du Havre</h2>
  <p class="muted" style="margin:0" id="gIntro"></p></div>
  <div class="seg" id="gView" role="group" aria-label="Vue"><button data-v="comp" aria-pressed="true">Concurrents</button><button data-v="rank" aria-pressed="false">Classement</button><button data-v="market" aria-pressed="false">Marché</button><button data-v="club" aria-pressed="false" id="gClubBtn" hidden>Focus LH Sport Club</button></div></div>
  <div class="kp" id="gKpi"></div>
  <b>Pistes pour développer une salle existante</b><ul id="gIdeas" style="margin:6px 0 0;padding-left:20px"></ul></div>

 <div id="g-comp">
  <div class="row" style="margin-top:14px"><input type="search" id="gq" placeholder="Rechercher une salle, une adresse, un atout (ex. sauna)…">
   <select id="gCat"></select>
   <select id="gSort"><option value="price">Trier : prix croissant</option><option value="rating">Trier : meilleure note</option>
    <option value="reviews">Trier : nombre d'avis</option><option value="perks">Trier : nombre d'atouts</option><option value="name">Trier : nom</option></select></div>
  <div class="tw"><table><thead><tr><th>Salle</th><th>Type</th><th class="n">Prix dès</th><th>Engagement</th><th class="n">Frais d'entrée</th><th class="n">Note</th><th>Atouts</th></tr></thead>
   <tbody id="gtb"></tbody></table></div>
  <p class="note" id="gcnt"></p>
  <div class="two">
   <div class="card"><b>Prix d'entrée par mois</b> <span class="muted">(formule la moins chère, ramenée au mois)</span><div class="cw" id="gPriceWrap"><canvas id="gPrice"></canvas></div></div>
   <div class="card"><b>Qui propose quoi</b> <span class="muted">(du plus rare au plus courant)</span><div class="tw" style="margin-top:8px;max-height:420px;overflow:auto"><table><thead><tr><th>Atout</th><th class="n">Salles</th><th>Lesquelles</th></tr></thead><tbody id="gPerks"></tbody></table></div></div>
  </div>
  <div class="two">
   <div class="card"><b>À surveiller</b><ul id="gWatch" style="padding-left:20px"></ul></div>
   <div class="card"><b>Autres lieux recensés</b> <span class="muted">(pas d'offre détaillée trouvée)</span><ul id="gOthers" style="padding-left:20px"></ul></div>
  </div>
  <div class="card" style="margin-top:14px" id="gQual"></div>
  <p class="note" id="gAbout"></p>
 </div>

 <div id="g-rank" hidden>
  <div class="card" style="margin-top:14px"><div class="dh"><div><b>Score sur 100</b> <span class="muted">— réglez l'importance de chaque critère</span></div>
   <div class="row" style="margin:0"><select id="rMode"><option value="all">Classement général</option><option value="cat">Classement par type</option></select>
   <select id="rCat"></select><button class="pin" id="rReset">Poids par défaut</button></div></div>
   <div class="kp" id="rWeights"></div></div>
  <div class="tw" style="margin-top:12px"><table><thead><tr><th class="n">#</th><th>Salle</th><th style="min-width:150px">Score</th><th class="n">Avis</th><th class="n">Prix</th><th class="n">Services</th><th class="n">Flexibilité</th><th class="n">Données</th></tr></thead>
   <tbody id="rtb"></tbody></table></div>
  <div class="card" style="margin-top:14px"><b>Ce qui fait chaque score</b> <span class="muted">(points apportés par chaque critère)</span><div class="cw" id="rChartWrap"><canvas id="rChart"></canvas></div></div>
  <div class="card" style="margin-top:14px"><b>Comment le score est calculé</b>
   <ul style="margin:6px 0 0;padding-left:20px" class="note">
    <li><b>Avis</b> : note Google corrigée par le nombre d'avis (une salle à 5/5 avec 3 avis ne passe pas devant une salle à 4,9 avec 400 avis). Moyenne bayésienne avec 50 avis « fictifs » à la note moyenne de la ville, puis 3,5/5 = 0 point et 5/5 = 100 points. Nombre d'avis inconnu : compté comme 10.</li>
    <li><b>Prix</b> : comparé aux salles du même type (low cost, fitness, CrossFit, EMS, yoga/Pilates), car un cours coaché ne se compare pas à un plateau en libre accès. Prix médian du type = 50 points, moitié moins cher = 100, 50 % plus cher = 0. Tarifs réduits exclus.</li>
    <li><b>Services</b> : nombre d'atouts recensés, rapporté à la salle qui en a le plus (= 100). Attention : les chaînes publient plus d'informations, les indépendants peuvent être sous-évalués.</li>
    <li><b>Flexibilité</b> : formule sans engagement (40), offre d'essai (20), pause d'abonnement (20), frais d'entrée offerts (20) ou ≤ 20 € (10).</li>
    <li>Un critère inconnu (pas de prix ou pas de note publiés) compte pour 50/100, une valeur neutre. « Données » = part des informations clés connues (note, nombre d'avis, prix, frais, engagement, horaires) : en dessous de 50 %, le score est fragile.</li>
   </ul><p class="note" id="rUnscored"></p></div>
 </div>

 <div id="g-club" hidden></div>

 <div id="g-market" hidden>
  <div class="two">
   <div class="card"><b>Offre sportive pour 10 000 habitants</b> <span class="muted">(ville seule, sans les communes voisines)</span><div class="cw"><canvas id="gDens"></canvas></div></div>
   <div class="card"><div class="dh"><b>Ouvertures et fermetures au Havre</b><select id="gFlowNaf"></select></div><div class="cw"><canvas id="gFlow"></canvas></div></div>
  </div>
  <div class="tw" style="margin-top:14px"><table><thead id="gDth"></thead><tbody id="gDtb"></tbody></table></div>
  <p class="note">Sirene = établissements actifs déclarés au Havre (y compris coachs indépendants en micro-entreprise) ; la liste détaillée ne couvre que la zone du Havre, d'où « — » pour les autres villes. BPE = équipements recensés par l'INSEE, comparables entre villes. Indice = densité au Havre ÷ moyenne de Rouen, Caen et Brest × 100. « Manque » = nombre à ajouter pour atteindre cette moyenne.</p>
  <div class="two">
   <div class="card"><b>Âge des habitants</b> <span class="muted" id="gAgeSub"></span><div class="cw"><canvas id="gAge"></canvas></div><p class="note" id="gAgeNote"></p></div>
   <div class="card"><b>Entreprises de 50 salariés et plus dans la ville</b> <span class="muted">(cibles pour des abonnements entreprise)</span>
    <div class="kp" id="gEmpK"></div><div class="tw" style="max-height:330px;overflow:auto"><table><thead><tr><th>Établissement</th><th>Activité</th><th class="n">Salariés</th></tr></thead><tbody id="gEmp"></tbody></table></div></div>
  </div>
  <h3>Métiers du sport : Le Havre, Normandie et autres villes</h3>
  <div class="tw"><table><thead id="gJth"></thead><tbody id="gJtb"></tbody></table></div>
  <p class="note">Source France Travail. Le Havre = zone de l'agence France Travail (ville et communes voisines), pas la ville seule. Chiffres arrondis à la dizaine : à lire comme des ordres de grandeur.
   Tension (1 à 5) mesurée sur le bassin d'emploi du Havre. Offres/dem. = offres sur 12 mois par demandeur d'emploi : plus c'est bas, plus il est facile de recruter.</p>
 </div>
</section>

<section id="v-list" hidden>
 <div class="row"><input type="search" id="q" placeholder="Rechercher un métier ou un secteur…">
 <select id="dom"></select>
 <select id="sort"><option value="o12">Trier : offres 12 mois</option><option value="g">Trier : croissance sur un an</option>
 <option value="d">Trier : demandeurs</option><option value="r">Trier : offres par demandeur</option><option value="t">Trier : tension</option></select>
 <label class="muted"><input type="checkbox" id="minv" checked> au moins 30 offres</label></div>
 <div class="tw"><table><thead id="th"></thead><tbody id="tb"></tbody></table></div>
 <p class="note" id="cnt"></p>
</section>

<section id="detail" class="card" hidden></section>
<p class="note">Source : France Travail — API Marché du travail et La Bonne Boîte. Chiffres par métier arrondis à la dizaine (secret statistique) :
les petits volumes sont approximatifs. « Offres » = offres déposées (France Travail + partenaires). « Demandeurs » = demandeurs d'emploi actifs (catégories A, B, C) en fin de trimestre.</p>
</main>
<script>
const D = __DATA__;
const Q = D.q, FN = {PERSPECTIVE:"Tension globale",MAIN_OEUVRE:"Manque de main-d'œuvre",SPECIF_FORM_EMPL:"Formation spécifique",
 COND_TRAVAIL:"Conditions de travail",MISMATCH_GEO:"Candidats éloignés",INT_EMB:"Intensité d'embauche",DUR_EMPL:"Contrats courts",ATTR_SALARIALE:"Attractivité salariale"};
const DOMS = {A:"Agriculture, espaces verts",B:"Arts et façonnage",C:"Banque, assurance, immobilier",D:"Commerce, vente",E:"Communication, média",
 F:"Construction, BTP",G:"Hôtellerie-restauration, tourisme",H:"Industrie",I:"Installation, maintenance",J:"Santé",K:"Services à la personne",
 L:"Spectacle",M:"Support à l'entreprise",N:"Transport, logistique"};
// Themes: job codes (ROME prefixes) + business sectors (NAF divisions)
const NOT_TECH = ["M1808","M1809","M1888","M1890","M1891","M1893"];
const THEMES = [
 {n:"Tech & informatique", rome:["M18"], ex:NOT_TECH, naf:["58","61","62","63"]},
 {n:"Port, transport & logistique", rome:["N"], naf:["49","50","51","52","53"]},
 {n:"Industrie", rome:["H"], naf:["10","11","19","20","22","23","24","25","27","28","29","30","33"]},
 {n:"Énergie & environnement", rome:["H1225","H1230","H1237","H1241","K23"], naf:["35","36","37","38","39"]},
 {n:"Santé", rome:["J"], naf:["86","87"]},
 {n:"Services à la personne & social", rome:["K"], naf:["88","96","97"]},
 {n:"BTP & construction", rome:["F"], naf:["41","42","43"]},
 {n:"Installation & maintenance", rome:["I"], naf:["33","95"]},
 {n:"Hôtellerie, restauration, tourisme", rome:["G"], naf:["55","56","79","93"]},
 {n:"Commerce & vente", rome:["D"], naf:["45","46","47"]},
 {n:"Support aux entreprises", rome:["M"], ex:["M18"], naf:["69","70","71","73","74","78","82"]},
 {n:"Banque, assurance, immobilier", rome:["C"], naf:["64","65","66","68"]},
];
const C = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const readPal = () => ["--c1","--c2","--c3","--c4","--c5","--c6"].map(C);
let PAL = readPal();
const fmt = v => v==null ? "—" : Math.round(v).toLocaleString("fr-FR");
const pct = v => v==null || !isFinite(v) ? "—" : (v>0?"+":"")+Math.round(v)+" %";
const cls = v => v==null ? "" : v>0 ? "up" : v<0 ? "down" : "";
const esc = s => String(s ?? "—").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const lastIdx = a => { for (let i=a.length-1;i>=0;i--) if (a[i]!=null) return i; return -1; };
const matches = (code, pre, ex=[]) => pre.some(p=>code.startsWith(p)) && !ex.some(p=>code.startsWith(p));

function sumSeries(items, k){
  return Q.map((_,i)=>{ let s=null; for (const it of items){ const v=it[k][i]; if (v!=null) s=(s||0)+v; } return s; });
}
function stats(it){
  const io = lastIdx(it.o12), id = lastIdx(it.d), ie = lastIdx(it.e);
  const o12 = io>=0 ? it.o12[io] : null, prev = io>=4 ? it.o12[io-4] : null;
  const d = id>=0 ? it.d[id] : null, dprev = id>=4 ? it.d[id-4] : null;
  let e12=null; if (ie>=0) for (let i=Math.max(0,ie-3);i<=ie;i++) if (it.e[i]!=null) e12=(e12||0)+it.e[i];
  const ty = it.t ? Object.keys(it.t).sort().pop() : null;
  return {o12, prev, g: o12!=null && prev ? 100*(o12-prev)/prev : null, d, gd: d!=null && dprev ? 100*(d-dprev)/dprev : null,
          r: o12!=null && d ? o12/d : null, e12, t: ty ? it.t[ty][0] : null, ty};
}
const TOT = D.total, TS = stats(TOT);
document.getElementById("sub").textContent =
 `Zone : agence France Travail ${D.area} · données ${Q[0]} → ${Q[Q.length-1]} · collecte du ${D.run} · ${fmt(TS.o12)} offres sur 12 mois (${pct(TS.g)})`;

// ---- themes -------------------------------------------------------------
const themeData = THEMES.map(t=>{
  const jobs = D.jobs.filter(j=>matches(j.c, t.rome, t.ex||[]));
  const secs = D.sectors.filter(s=>t.naf.includes(s.c));
  const agg = {c:t.n, l:t.n, o:sumSeries(jobs,"o"), o12:sumSeries(jobs,"o12"), d:sumSeries(jobs,"d"), e:sumSeries(jobs,"e")};
  return {...t, jobs, secs, agg, s: stats(agg)};
});
function spark(a, w=110, h=26){
  const pts = a.map((v,i)=>[i,v]).filter(p=>p[1]!=null); if (pts.length<2) return "";
  const vs = pts.map(p=>p[1]), lo=Math.min(...vs), hi=Math.max(...vs), sp=(hi-lo)||1;
  const d = pts.map((p,k)=>`${k?"L":"M"}${(p[0]/(a.length-1)*w).toFixed(1)},${(h-3-(p[1]-lo)/sp*(h-6)).toFixed(1)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="var(--accent)" stroke-width="1.6"/></svg>`;
}
document.getElementById("themeGrid").innerHTML = themeData.map((t,i)=>`
 <div class="card theme" data-i="${i}"><h3>${esc(t.n)}</h3>
 <div class="big">${fmt(t.s.o12)} <span class="muted" style="font-size:12px;font-weight:400">offres / 12 mois</span></div>
 <div><span class="${cls(t.s.g)}">${pct(t.s.g)}</span> <span class="muted">sur un an · ${fmt(t.s.d)} demandeurs</span></div>
 ${spark(t.agg.o12, 200, 34).replace('class="spark"','class="spark" style="width:100%;height:34px;margin-top:6px"')}</div>`).join("");
document.getElementById("themeGrid").onclick = e=>{
  const c = e.target.closest(".theme"); if (!c) return;
  document.querySelectorAll(".theme").forEach(x=>x.classList.toggle("on", x===c));
  showTheme(themeData[+c.dataset.i]);
};

// ---- charts ---------------------------------------------------------------
let chart, cmpChart, mode = "o12", firstLoad = true;
const tipColors = () => ({backgroundColor:C("--tip-bg"), titleColor:C("--tip-ink"), bodyColor:C("--tip-ink"), borderColor:C("--line"), borderWidth:1});
function lineChart(canvas, sets, opts={}){
  return new Chart(canvas, {type:"line", data:{labels:Q, datasets:sets.map((s,i)=>({label:s.label, data:s.data, yAxisID:s.axis||"y",
    _pal:(s.pal ?? i)%6, borderColor:PAL[(s.pal ?? i)%6], backgroundColor:PAL[(s.pal ?? i)%6], borderWidth:2, pointRadius:0, pointHoverRadius:4,
    spanGaps:true, tension:.25, borderDash:s.dash||[]}))},
   options:{responsive:true, maintainAspectRatio:false, interaction:{mode:"index", intersect:false},
    plugins:{legend:{labels:{color:C("--muted"), boxWidth:12}}, tooltip:{...tipColors(), callbacks:{label:c=>`${c.dataset.label} : ${fmt(c.raw)}`}}},
    scales:{x:{ticks:{color:C("--muted"), maxTicksLimit:10}, grid:{display:false}},
     y:{ticks:{color:C("--muted")}, grid:{color:C("--line")}, beginAtZero:true, title:{display:!!opts.y, text:opts.y||"", color:C("--muted")}},
     ...(opts.y2 ? {y2:{position:"right", ticks:{color:C("--muted")}, grid:{display:false}, beginAtZero:true, title:{display:true, text:opts.y2, color:C("--muted")}}} : {})}}});
}
function drawDetailChart(it){
  if (chart) chart.destroy();
  const sets = mode==="o12"
    ? [{label:"Offres (12 mois glissants)", data:it.o12}, {label:"Demandeurs actifs", data:it.d, axis:"y2", pal:1, dash:[5,4]}]
    : [{label:"Offres du trimestre", data:it.o}, {label:"Embauches du trimestre", data:it.e, pal:3}, {label:"Demandeurs actifs", data:it.d, axis:"y2", pal:1, dash:[5,4]}];
  chart = lineChart(document.getElementById("dChart"), sets, {y: mode==="o12" ? "offres" : "offres / embauches", y2:"demandeurs"});
}

// ---- comparison -------------------------------------------------------------
const pinned = [];
function pin(it){
  if (pinned.find(p=>p.c===it.c && p.l===it.l)) return;
  if (pinned.length>=6) pinned.shift();
  pinned.push(it); drawCmp();
}
function drawCmp(){
  const box = document.getElementById("cmp"); box.hidden = !pinned.length;
  document.getElementById("cmpChips").innerHTML = pinned.map((p,i)=>`<span class="chip" data-i="${i}"><i style="background:var(--c${i%6+1})"></i>${esc(p.l)} ✕</span>`).join("");
  if (cmpChart) cmpChart.destroy();
  if (pinned.length) cmpChart = lineChart(document.getElementById("cmpChart"), pinned.map(p=>({label:p.l, data:p.o12})));
}
document.getElementById("cmpChips").onclick = e=>{ const c=e.target.closest(".chip"); if(c){ pinned.splice(+c.dataset.i,1); drawCmp(); } };
document.getElementById("cmpClear").onclick = ()=>{ pinned.length=0; drawCmp(); };

// ---- detail panel -------------------------------------------------------------
function kpis(s, withT=true){
  return `<div class="kp">
   <div><b>${fmt(s.o12)}</b><span>offres sur 12 mois</span></div>
   <div><b class="${cls(s.g)}">${pct(s.g)}</b><span>offres vs an -1 (${fmt(s.prev)})</span></div>
   <div><b>${fmt(s.d)}</b><span>demandeurs actifs (${pct(s.gd)} sur un an)</span></div>
   <div><b>${s.r==null?"—":s.r.toFixed(2).replace(".",",")}</b><span>offres par demandeur (zone : ${TS.r.toFixed(2).replace(".",",")})</span></div>
   <div><b>${fmt(s.e12)}</b><span>embauches sur 12 mois</span></div>
   ${withT && s.t ? `<div><b>${s.t} / 5</b><span>tension de recrutement ${s.ty}</span></div>` : ""}</div>`;
}
function openDetail(html, it){
  const el = document.getElementById("detail");
  el.innerHTML = `<div class="dh"><div><h2>${esc(it.l)}</h2><div class="muted">${it.c && it.c!==it.l ? esc(it.c) : ""}</div></div>
   <div><button class="pin" id="pinBtn">+ Comparer</button> <select id="mode"><option value="o12">12 mois glissants</option><option value="q">Par trimestre</option></select>
   <button class="x" id="closeD" aria-label="Fermer">×</button></div></div>${html}`;
  el.hidden = false;
  document.getElementById("mode").value = mode;
  document.getElementById("mode").onchange = e=>{ mode=e.target.value; drawDetailChart(it); };
  document.getElementById("pinBtn").onclick = ()=>pin(it);
  document.getElementById("closeD").onclick = ()=>{ el.hidden=true; document.querySelectorAll(".theme.on,tr.sel").forEach(x=>x.classList.remove("on","sel")); };
  drawDetailChart(it);
  if (!firstLoad) el.scrollIntoView({behavior:"smooth", block:"start"});
  firstLoad = false;
}
function memberTable(items, kind){
  const rows = items.map(it=>({it, s:stats(it)})).filter(x=>x.s.o12 || x.s.d || x.s.e12).sort((a,b)=>(b.s.o12||0)-(a.s.o12||0));
  const job = kind==="Métier";
  return `<div class="tw"><table><thead><tr><th>${kind}</th><th class="n">Offres 12 m</th><th class="n">Évol.</th>
   ${job ? '<th class="n">Demandeurs</th><th class="n">Tension</th>' : '<th class="n">Embauches 12 m</th>'}<th>Tendance</th></tr></thead><tbody>
   ${rows.slice(0,60).map(({it,s})=>`<tr data-k="${job?"j":"s"}" data-c="${it.c}"><td class="l">${esc(it.l)}</td><td class="n">${fmt(s.o12)}</td>
   <td class="n ${cls(s.g)}">${pct(s.g)}</td>${job ? `<td class="n">${fmt(s.d)}</td><td class="n">${s.t??"—"}</td>` : `<td class="n">${fmt(s.e12)}</td>`}
   <td>${spark(it.o12)}</td></tr>`).join("")}</tbody></table></div>`;
}
function showTheme(t){
  const top = t.jobs.map(j=>({j,s:stats(j)})).filter(x=>(x.s.prev||0)>=30 && x.s.g!=null);
  const ups = top.filter(x=>x.s.g>0).sort((a,b)=>b.s.g-a.s.g).slice(0,5), downs = top.filter(x=>x.s.g<0).sort((a,b)=>a.s.g-b.s.g).slice(0,5);
  const li = x=>`<li>${esc(x.j.l)} <span class="${cls(x.s.g)}">${pct(x.s.g)}</span> <span class="muted">(${fmt(x.s.prev)} → ${fmt(x.s.o12)})</span></li>`;
  openDetail(`${kpis(t.s,false)}<div class="cw"><canvas id="dChart"></canvas></div>
   <div class="two"><div><b>En hausse sur un an</b><ul>${ups.map(li).join("")||"<li class='muted'>—</li>"}</ul></div>
   <div><b>En baisse sur un an</b><ul>${downs.map(li).join("")||"<li class='muted'>—</li>"}</ul></div></div>
   <div class="two"><div><b>Métiers du thème</b> <span class="muted">(${t.jobs.length})</span>${memberTable(t.jobs,"Métier")}</div>
   <div><b>Secteurs d'activité liés</b>${memberTable(t.secs,"Secteur")}</div></div>
   <p class="note">Total du thème = somme des métiers (chacun arrondi à la dizaine). Cliquez sur une ligne pour voir le détail.</p>`, t.agg);
}
function showJob(j){
  const s = stats(j);
  let heat = "";
  if (j.t){
    const ys = Object.keys(j.t).sort().slice(-6);
    heat = `<div><b>Tension de recrutement</b> <span class="muted">(1 très faible → 5 très forte, bassin du Havre)</span>
     <div class="tw"><table class="heat"><thead><tr><th></th>${ys.map(y=>`<th>${y}</th>`).join("")}</tr></thead><tbody>
     ${D.factors.map((f,k)=>`<tr><td class="f">${FN[f]}</td>${ys.map(y=>{const v=j.t[y][k]; return `<td class="lv${v||0}">${v??"—"}</td>`}).join("")}</tr>`).join("")}
     </tbody></table></div></div>`;
  }
  const co = D.companies[j.c] || [];
  const comp = co.length ? `<div><b>Entreprises susceptibles de recruter</b> <span class="muted">(La Bonne Boîte, ★ = fort potentiel)</span>
    <div class="tw"><table><thead><tr><th>Entreprise</th><th>Activité</th><th>Commune</th><th class="n">Effectif</th></tr></thead><tbody>
    ${co.map(c=>`<tr><td class="l">${esc(c[0])}${c[4]?" ★":""}</td><td class="l">${esc(c[1])}</td><td>${esc(c[2])}</td><td class="n">${esc(c[3]||"—")}</td></tr>`).join("")}</tbody></table></div></div>` : "";
  openDetail(`${kpis(s)}<div class="cw"><canvas id="dChart"></canvas></div>${heat||comp ? `<div class="two">${heat}${comp}</div>` : ""}`, j);
}
function showSector(x){ openDetail(`${kpis(stats(x),false)}<div class="cw"><canvas id="dChart"></canvas></div>
  <p class="note">Par secteur, seules les offres dont le secteur de l'employeur est renseigné sont comptées.</p>`, x); }
document.getElementById("detail").addEventListener("click", e=>{
  const tr = e.target.closest("tr[data-c]"); if (!tr) return;
  if (tr.dataset.k==="j") showJob(D.jobs.find(j=>j.c===tr.dataset.c)); else showSector(D.sectors.find(s=>s.c===tr.dataset.c));
});

// ---- list views (jobs / sectors) -----------------------------------------------
let view = "themes", sortKey = "o12";
const domSel = document.getElementById("dom");
function setDomOptions(){
  domSel.innerHTML = view==="jobs"
    ? `<option value="">Tous les domaines</option>${Object.entries(DOMS).map(([k,v])=>`<option value="${k}">${k} · ${v}</option>`).join("")}`
    : `<option value="">Tous les secteurs</option>`;
  domSel.hidden = view!=="jobs";
}
function renderList(){
  const src = view==="jobs" ? D.jobs : D.sectors, q = document.getElementById("q").value.trim().toLowerCase();
  const dom = domSel.value, minv = document.getElementById("minv").checked;
  let rows = src.map(it=>({it, s:stats(it)})).filter(({it,s})=>
    (!q || it.l.toLowerCase().includes(q) || it.c.toLowerCase().includes(q)) && (!dom || it.c.startsWith(dom)) && (!minv || (s.o12||0)>=30));
  const k = document.getElementById("sort").value;
  rows.sort((a,b)=>(b.s[k] ?? -1e9)-(a.s[k] ?? -1e9));
  const kind = view==="jobs" ? "Métier" : "Secteur";
  document.getElementById("th").innerHTML = `<tr><th>${kind}</th><th class="n">Offres 12 m</th><th class="n">Évol. 1 an</th><th class="n">Demandeurs</th>
   <th class="n">Offres/dem.</th>${view==="jobs"?'<th class="n">Tension</th>':'<th class="n">Embauches 12 m</th>'}<th>Offres depuis ${Q[0].slice(0,4)}</th></tr>`;
  document.getElementById("tb").innerHTML = rows.slice(0,200).map(({it,s})=>`<tr data-c="${it.c}"><td class="l">${esc(it.l)} <span class="muted">${esc(it.c)}</span></td>
   <td class="n">${fmt(s.o12)}</td><td class="n ${cls(s.g)}">${pct(s.g)}</td><td class="n">${fmt(s.d)}</td>
   <td class="n">${s.r==null?"—":s.r.toFixed(2).replace(".",",")}</td><td class="n">${view==="jobs" ? (s.t??"—") : fmt(s.e12)}</td><td>${spark(it.o12)}</td></tr>`).join("");
  document.getElementById("cnt").textContent = `${rows.length} résultat(s)${rows.length>200?" — 200 premiers affichés":""}.`;
}
document.getElementById("tb").onclick = e=>{
  const tr = e.target.closest("tr"); if (!tr) return;
  document.querySelectorAll("tr.sel").forEach(x=>x.classList.remove("sel")); tr.classList.add("sel");
  if (view==="jobs") showJob(D.jobs.find(j=>j.c===tr.dataset.c)); else showSector(D.sectors.find(s=>s.c===tr.dataset.c));
};
["q","dom","sort","minv"].forEach(id=>document.getElementById(id).addEventListener("input", renderList));
document.getElementById("tabs").onclick = e=>{
  const b = e.target.closest("button"); if (!b) return;
  view = b.dataset.t;
  document.querySelectorAll("#tabs button").forEach(x=>x.classList.toggle("on", x===b));
  const isFocus = view.startsWith("focus:"), isLocal = view==="local", isGym = view==="gym";
  document.getElementById("v-themes").hidden = view!=="themes";
  document.getElementById("v-list").hidden = view==="themes" || isFocus || isLocal || isGym;
  document.getElementById("v-focus").hidden = !isFocus;
  document.getElementById("v-local").hidden = !isLocal;
  document.getElementById("v-gym").hidden = !isGym;
  if (isGym){ document.getElementById("detail").hidden = true; renderGym(); return; }
  if (isLocal){ document.getElementById("detail").hidden = true; setDomLocal(); renderLocal(); return; }
  if (isFocus){ document.getElementById("detail").hidden = true; renderFocus(D.focus.find(f=>"focus:"+f.key===view)); return; }
  document.getElementById("detail").hidden = true;
  document.getElementById("q").placeholder = view==="jobs" ? "Rechercher un métier (ex. développeur, soudeur, M1805)…" : "Rechercher un secteur (ex. informatique, 62)…";
  if (view!=="themes"){ setDomOptions(); renderList(); }
};

// ---- focus sections (see focus.py) --------------------------------------------
const tabs = document.getElementById("tabs");
D.focus.forEach(f=>{ const b=document.createElement("button"); b.dataset.t="focus:"+f.key;
  b.textContent = f.title.split(" et ")[0]; tabs.appendChild(b); });
let focusChart;
function renderFocus(f){
  const byCode = Object.fromEntries(D.jobs.map(j=>[j.c,j]));
  const fams = f.families.map(([name, codes])=>{
    const jobs = codes.map(c=>byCode[c]).filter(Boolean);
    const agg = {c:name, l:name, o:sumSeries(jobs,"o"), o12:sumSeries(jobs,"o12"), d:sumSeries(jobs,"d"), e:sumSeries(jobs,"e")};
    return {name, codes, jobs, agg, s:stats(agg)};
  });
  const all = fams.flatMap(x=>x.jobs);
  const tot = {c:f.title, l:f.title, o:sumSeries(all,"o"), o12:sumSeries(all,"o12"), d:sumSeries(all,"d"), e:sumSeries(all,"e")};
  const ts = stats(tot), core = byCode[f.core], cst = core ? stats(core) : null;
  const secs = D.sectors.filter(s=>f.naf.includes(s.c));
  const label = c => (byCode[c]||{}).l || c;
  const r2 = v => v==null ? "—" : v.toFixed(2).replace(".",",");

  // automatic reading
  const famG = fams.filter(x=>(x.s.prev||0)>=30 && x.s.g!=null).sort((a,b)=>b.s.g-a.s.g);
  const withS = all.map(j=>({j,s:stats(j)}));
  const crowded = withS.filter(x=>(x.s.d||0)>=30).sort((a,b)=>(a.s.r??0)-(b.s.r??0)).slice(0,4);
  const openJobs = withS.filter(x=>(x.s.o12||0)>=20).sort((a,b)=>(b.s.r??0)-(a.s.r??0)).slice(0,4);
  const nm = x => esc(x.j.l);
  const reading = [
    core ? `<li><b>${esc(core.l)}</b> : ${fmt(cst.o12)} offres sur 12 mois (${pct(cst.g)} sur un an) pour ${fmt(cst.d)} demandeurs d'emploi,
       soit ${r2(cst.r)} offre par demandeur.${cst.o12!=null && cst.o12<50 ? " Petit volume, arrondi à la dizaine : à lire comme un ordre de grandeur." : ""}</li>`
         : `<li class="muted">${esc(f.core)} : pas de données dans la zone.</li>`,
    `<li>Ensemble du domaine : <b>${fmt(ts.o12)}</b> offres sur 12 mois (<span class="${cls(ts.g)}">${pct(ts.g)}</span>) pour ${fmt(ts.d)} demandeurs,
       soit <b>${r2(ts.r)}</b> offre par demandeur contre ${r2(TS.r)} tous métiers confondus${ts.r!=null && ts.r < TS.r ? " : plus de concurrence entre candidats que la moyenne." : "."}</li>`,
    famG.length ? `<li>Famille la plus dynamique : <b>${esc(famG[0].name)}</b> (${pct(famG[0].s.g)}) ; la moins dynamique : ${esc(famG[famG.length-1].name)} (${pct(famG[famG.length-1].s.g)}).</li>` : "",
    openJobs.length ? `<li>Le plus d'offres par candidat : ${openJobs.map(x=>`${nm(x)} (${r2(x.s.r)})`).join(", ")}.</li>` : "",
    crowded.length ? `<li>Le plus de candidats par offre : ${crowded.map(x=>`${nm(x)} (${r2(x.s.r)})`).join(", ")}.
       Beaucoup de profils disponibles : terrain propice au freelance, aux collectifs ou à une agence qui les fédère.</li>` : "",
  ].join("");

  // companies across the cluster, deduplicated by name
  const comp = {};
  for (const c of new Set(all.map(j=>j.c))) for (const x of (D.companies[c]||[])) {
    comp[x[0]] = comp[x[0]] || {x, jobs:new Set()}; comp[x[0]].jobs.add(c);
  }
  const compRows = Object.values(comp).sort((a,b)=>b.jobs.size-a.jobs.size || (b.x[4]-a.x[4])).slice(0,40);

  const dot = i => `<i style="display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--c${i%6+1});margin-right:6px"></i>`;
  const famRows = fams.map((x,i)=>`<tr data-f="${i}"><td class="l">${dot(i)}${esc(x.name)} <span class="muted">(${x.jobs.length}/${x.codes.length})</span></td>
     <td class="n">${fmt(x.s.o12)}</td><td class="n ${cls(x.s.g)}">${pct(x.s.g)}</td><td class="n">${fmt(x.s.d)}</td><td class="n">${r2(x.s.r)}</td>
     <td class="n">${fmt(x.s.e12)}</td><td>${spark(x.agg.o12)}</td></tr>`).join("");
  const jobRows = fams.map(x=>`<tr><th colspan="7" style="background:var(--soft);cursor:default">${esc(x.name)}</th></tr>` + x.codes.map(c=>{
     const j = byCode[c];
     if (!j) return `<tr style="cursor:default"><td class="l muted">${esc(c)} — pas de données dans la zone</td><td colspan="6"></td></tr>`;
     const s = stats(j);
     return `<tr data-k="j" data-c="${c}"><td class="l">${esc(j.l)} <span class="muted">${c}</span>${c===f.core?" <b>★</b>":""}</td><td class="n">${fmt(s.o12)}</td>
       <td class="n ${cls(s.g)}">${pct(s.g)}</td><td class="n">${fmt(s.d)}</td><td class="n">${r2(s.r)}</td><td class="n">${s.t??"—"}</td><td>${spark(j.o12)}</td></tr>`;
   }).join("")).join("");

  document.getElementById("v-focus").innerHTML = `
   <div class="card"><h2 style="margin:0 0 4px;font-size:18px">${esc(f.title)}</h2><p class="muted" style="margin:0">${esc(f.intro)}</p>
   ${kpis(ts,false)}<ul style="margin:0;padding-left:20px">${reading}</ul></div>
   <div class="card" style="margin-top:14px"><div class="dh"><b>Évolution sur 12 mois glissants</b>
     <select id="fMode"><option value="fam">Offres par famille</option><option value="tot">Total : offres vs demandeurs</option><option value="core">Métier central : offres vs demandeurs</option></select></div>
     <div class="cw"><canvas id="fChart"></canvas></div></div>
   <h3>Familles de métiers</h3><p class="note">Cliquez sur une famille pour sa tendance et ses métiers.</p>
   <div class="tw" id="famTable"><table><thead><tr><th>Famille</th><th class="n">Offres 12 m</th><th class="n">Évol.</th><th class="n">Demandeurs</th>
     <th class="n">Offres/dem.</th><th class="n">Embauches 12 m</th><th>Tendance</th></tr></thead><tbody>${famRows}</tbody></table></div>
   <h3>Tous les métiers</h3><p class="note">★ = métier central. Cliquez sur un métier pour son détail (tendance, tension, entreprises).</p>
   <div class="tw" id="focusJobs"><table><thead><tr><th>Métier</th><th class="n">Offres 12 m</th><th class="n">Évol.</th><th class="n">Demandeurs</th>
     <th class="n">Offres/dem.</th><th class="n">Tension</th><th>Tendance</th></tr></thead><tbody>${jobRows}</tbody></table></div>
   <div class="two" id="focusMore"><div><h3>Secteurs d'activité liés</h3>${memberTable(secs,"Secteur")}</div>
   <div><h3>Entreprises susceptibles de recruter</h3>
   ${compRows.length ? `<div class="tw"><table><thead><tr><th>Entreprise</th><th>Commune</th><th>Pour les métiers</th></tr></thead><tbody>
     ${compRows.map(r=>`<tr style="cursor:default"><td class="l">${esc(r.x[0])}${r.x[4]?" ★":""}<div class="muted" style="font-size:12px">${esc(r.x[1])}</div></td>
     <td>${esc(r.x[2])}</td><td class="l" style="font-size:12px">${[...r.jobs].map(c=>esc(label(c))).join(" · ")}</td></tr>`).join("")}</tbody></table></div>`
     : "<p class='muted'>Pas encore de données : lancez <code>python collect.py --only-lbb</code>.</p>"}</div></div>`;

  const draw = m=>{
    if (focusChart) focusChart.destroy();
    const sets = m==="fam"
      ? fams.map((x,i)=>({x,i})).filter(o=>o.x.jobs.length).map(o=>({label:o.x.name, data:o.x.agg.o12, pal:o.i, dash:o.i>=PAL.length?[5,4]:[]}))
      : m==="tot" ? [{label:"Offres (12 mois glissants)", data:tot.o12}, {label:"Demandeurs actifs", data:tot.d, axis:"y2", pal:1, dash:[5,4]}]
      : core ? [{label:"Offres (12 mois glissants)", data:core.o12}, {label:"Demandeurs actifs", data:core.d, axis:"y2", pal:1, dash:[5,4]}] : [];
    focusChart = lineChart(document.getElementById("fChart"), sets, m==="fam" ? {} : {y:"offres", y2:"demandeurs"});
  };
  draw("fam");
  document.getElementById("fMode").onchange = e=>draw(e.target.value);
  document.getElementById("famTable").onclick = e=>{
    const tr = e.target.closest("tr[data-f]"); if (!tr) return; const x = fams[+tr.dataset.f];
    openDetail(`${kpis(x.s,false)}<div class="cw"><canvas id="dChart"></canvas></div><div style="margin-top:14px">${memberTable(x.jobs,"Métier")}</div>`, x.agg);
  };
  const rowClick = e=>{
    const tr = e.target.closest("tr[data-c]"); if (!tr) return;
    if (tr.dataset.k==="s") showSector(D.sectors.find(s=>s.c===tr.dataset.c)); else showJob(byCode[tr.dataset.c]);
  };
  document.getElementById("focusJobs").onclick = rowClick;
  document.getElementById("focusMore").onclick = rowClick;
}



// ---- local supply: businesses (Sirene) and services (BPE) -------------------------------
const L = D.local;
// Activities that inflate counts without being businesses to create: holdings, property SCIs, associations, home solar panels
const HOLD = ["64.20Z","64.30Z","68.20A","68.20B","70.10Z","68.10Z","94.99Z","35.11Z"];
let lSrc = "naf";
const BPE_DOMS = {A:"Services aux particuliers",B:"Commerces",C:"Enseignement",D:"Santé et action sociale",E:"Transports et déplacements",F:"Sports, loisirs et culture",G:"Tourisme"};
function localRows(){
  if (!L) return [];
  const A = L.areas, emp = document.getElementById("lEmp").checked;
  const yrs = L.years || [], full = L.full_years || yrs, last3 = full.slice(-3);   // last 3 full years
  if (lSrc==="bpe"){
    const dom = document.getElementById("lDom").value;
    return L.bpe.filter(b=>!dom || b.dom===dom).map(b=>({c:b.c, l:b.l, sub:(L.bpe_labels||{})[b.sdom]||"", n:b.n, kind:"bpe"}));
  }
  const level = document.getElementById("lLevel").value, hide = document.getElementById("lHold").checked;
  const dom = document.getElementById("lDom").value;
  let src = L.naf.filter(x=>!(hide && HOLD.includes(x.c)) && (!dom || x.c.slice(0,2)===dom));
  const val = x => x.n.map(v=>v[emp?1:0]);
  const flows = x => { const yi = last3.map(y=>yrs.indexOf(y)); let c=0, f=0; for (const i of yi){ c+=x.f[0][i][0]; f+=x.f[0][i][1]; } return [c,f]; };
  if (level==="naf") return src.map(x=>({c:x.c, l:x.l, n:val(x), fl:flows(x), series:x.f[0], kind:"naf"}));
  const g = {};
  for (const x of src){
    const k = x.c.slice(0,2);
    g[k] = g[k] || {c:k, l:L.div[k]||k, n:A.map(()=>0), fl:[0,0], series:yrs.map(()=>[0,0]), kind:"div", members:[]};
    val(x).forEach((v,i)=>g[k].n[i]+=v);
    const f = flows(x); g[k].fl[0]+=f[0]; g[k].fl[1]+=f[1];
    x.f[0].forEach((v,i)=>{ g[k].series[i][0]+=v[0]; g[k].series[i][1]+=v[1]; });
    g[k].members.push(x);
  }
  return Object.values(g);
}
function enrich(r){
  const A = L.areas, dens = r.n.map((v,i)=>v/A[i].pop*1e4);
  const bench = dens.slice(1), avg = bench.reduce((a,b)=>a+b,0)/bench.length;
  const idx = avg ? 100*dens[0]/avg : null;
  const gap = Math.round(avg*A[0].pop/1e4 - r.n[0]);
  const net = r.fl ? r.fl[0]-r.fl[1] : null;
  return {...r, dens, avg, idx, gap, net};
}
const d1 = v => v==null ? "—" : (v<10 ? v.toFixed(2) : v.toFixed(1)).replace(".",",");
function renderLocal(){
  if (!L){ document.getElementById("lIntro").innerHTML = "Pas encore de données : lancez <code>python collect_territory.py</code> puis <code>python dashboard.py</code>."; return; }
  const A = L.areas, isB = lSrc==="bpe";
  document.getElementById("lIntro").textContent =
   `Établissements (Sirene, ${L.sirene||"—"}) et équipements (BPE ${L.bpe_year||"—"}) dans un rayon de 30 km autour de chaque ville, rapportés à 10 000 habitants. `
   + A.map(a=>`${a.n} : ${fmt(a.pop)} hab. (${a.com} communes)`).join(" · ");
  document.getElementById("lLevel").hidden = isB;
  document.getElementById("lEmpWrap").hidden = isB; document.getElementById("lHoldWrap").hidden = isB;
  [...document.getElementById("lSort").options].forEach(o=>{ if (o.value==="net") o.hidden = isB; });
  const norm = t => t.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();   // accent-insensitive
  const q = norm(document.getElementById("lq").value.trim());
  const minExpected = isB ? 3 : 10;
  let rows = localRows().map(enrich).filter(r=>(!q || norm(r.l).includes(q) || norm(r.c).includes(q))
     && (r.n[0] >= minExpected || r.avg*A[0].pop/1e4 >= minExpected));
  const k = document.getElementById("lSort").value;
  const key = {gap:r=>r.gap, idx:r=>-(r.idx??1e9), hi:r=>(r.idx??0), n:r=>r.n[0], net:r=>r.net??-1e9}[k];
  rows.sort((a,b)=>key(b)-key(a));
  // summary
  const pool = rows.filter(r=>r.idx!=null);
  const miss = [...pool].filter(r=>r.idx<80 && r.gap>0).sort((a,b)=>b.gap-a.gap).slice(0,6);
  const spec = [...pool].filter(r=>r.idx>130).sort((a,b)=>b.idx-a.idx).slice(0,5);
  const short = r => esc(r.l.length>60 ? r.l.slice(0,58)+"…" : r.l);
  document.getElementById("lSummary").innerHTML =
    `<li><b>Le plus gros manque</b> par rapport à ${A.slice(1).map(a=>a.n).join(", ")} : ${miss.map(r=>`${short(r)} (≈ ${fmt(r.gap)} de moins, indice ${Math.round(r.idx)})`).join(" ; ")||"—"}.</li>
     <li><b>Spécialités du Havre</b> (bien plus présentes qu'ailleurs) : ${spec.map(r=>`${short(r)} (indice ${Math.round(r.idx)})`).join(" ; ")||"—"}.</li>`;
  // table
  const flowCols = !isB;
  document.getElementById("lth").innerHTML = `<tr><th>${isB?"Équipement":"Activité"}</th><th class="n">Le Havre</th><th class="n">/10 000 hab.</th>
    ${A.slice(1).map(a=>`<th class="n">${esc(a.n)}</th>`).join("")}<th class="n">Indice</th><th class="n">Manque</th>
    ${flowCols?'<th class="n">Créations 3 ans</th><th class="n">Solde</th><th>Créations / an</th>':""}</tr>`;
  document.getElementById("ltb").innerHTML = rows.slice(0,300).map(r=>`<tr data-c="${esc(r.c)}">
    <td class="l">${esc(r.l)} <span class="muted">${esc(r.c)}</span>${r.sub?`<div class="muted" style="font-size:12px">${esc(r.sub)}</div>`:""}</td>
    <td class="n">${fmt(r.n[0])}</td><td class="n">${d1(r.dens[0])}</td>${r.dens.slice(1).map(v=>`<td class="n muted">${d1(v)}</td>`).join("")}
    <td class="n ${r.idx==null?"":r.idx<80?"down":r.idx>125?"up":""}">${r.idx==null?"—":Math.round(r.idx)}</td>
    <td class="n">${r.gap>0?fmt(r.gap):"—"}</td>
    ${flowCols?`<td class="n">${fmt(r.fl[0])}</td><td class="n ${cls(r.net)}">${r.net>0?"+":""}${fmt(r.net)}</td><td>${spark(r.series.slice(0,(L.full_years||[]).length).map(v=>v[0]))}</td>`:""}</tr>`).join("");
  document.getElementById("lcnt").textContent = `${rows.length} ligne(s)${rows.length>300?" — 300 premières affichées":""}. Densités des autres villes en gris (pour 10 000 hab.).`;
  window._lrows = rows;
}
function setDomLocal(){
  const sel = document.getElementById("lDom");
  sel.innerHTML = lSrc==="bpe"
    ? `<option value="">Tous les domaines</option>${Object.entries(BPE_DOMS).map(([k,v])=>`<option value="${k}">${v}</option>`).join("")}`
    : `<option value="">Toutes les divisions</option>${Object.entries((L&&L.div)||{}).sort().map(([k,v])=>`<option value="${k}">${k} · ${esc(v.length>50?v.slice(0,48)+"…":v)}</option>`).join("")}`;
}
async function showLocal(r){
  const A = L.areas, isB = lSrc==="bpe";
  let html = `<div class="kp"><div><b>${fmt(r.n[0])}</b><span>au Havre (+30 km)</span></div>
    <div><b>${d1(r.dens[0])}</b><span>pour 10 000 hab. (autres villes : ${d1(r.avg)})</span></div>
    <div><b class="${r.idx<80?"down":r.idx>125?"up":""}">${r.idx==null?"—":Math.round(r.idx)}</b><span>indice (100 = moyenne des autres)</span></div>
    <div><b>${r.gap>0?fmt(r.gap):"0"}</b><span>établissements manquants pour la moyenne</span></div>
    ${isB?"":`<div><b>${fmt(r.fl[0])} / ${fmt(r.fl[1])}</b><span>créations / fermetures ${(L.full_years||[]).slice(-3)[0]}–${(L.full_years||[]).slice(-1)[0]}</span></div>`}</div>
    <div class="two" style="margin-top:0"><div><b>Densité pour 10 000 habitants</b><div class="cw" style="height:220px"><canvas id="lBar"></canvas></div></div>
    ${isB?"":`<div><b>Créations et fermetures par an (Le Havre)</b><div class="cw" style="height:220px"><canvas id="dChart"></canvas></div></div>`}</div>`;
  const sector = r.kind==="div" ? D.sectors.find(s=>s.c===r.c) : r.kind==="naf" ? D.sectors.find(s=>s.c===r.c.slice(0,2)) : null;
  if (sector){ const ss = stats(sector);
    html += `<p class="note">Emploi dans la division ${esc(sector.c)} (${esc(sector.l)}) : ${fmt(ss.o12)} offres sur 12 mois (${pct(ss.g)}), ${fmt(ss.e12)} embauches.</p>`; }
  if (r.kind==="div") html += `<div style="margin-top:10px"><b>Activités de la division</b><div class="tw"><table><thead><tr><th>Activité</th><th class="n">Le Havre</th><th class="n">Indice</th><th class="n">Manque</th></tr></thead><tbody>
    ${r.members.map(m=>enrich({c:m.c,l:m.l,n:m.n.map(v=>v[document.getElementById("lEmp").checked?1:0])})).sort((a,b)=>b.gap-a.gap).slice(0,40)
     .map(m=>`<tr style="cursor:default"><td class="l">${esc(m.l)} <span class="muted">${m.c}</span></td><td class="n">${fmt(m.n[0])}</td><td class="n">${m.idx==null?"—":Math.round(m.idx)}</td><td class="n">${m.gap>0?fmt(m.gap):"—"}</td></tr>`).join("")}</tbody></table></div></div>`;
  if (r.kind==="naf") html += `<div style="margin-top:10px" id="etabList"><b>Établissements au Havre (+30 km)</b> <span class="muted">chargement…</span></div>`;
  const el = document.getElementById("detail");
  el.innerHTML = `<div class="dh"><div><h2>${esc(r.l)}</h2><div class="muted">${esc(r.c)}</div></div><button class="x" id="closeD" aria-label="Fermer">×</button></div>${html}`;
  el.hidden = false;
  document.getElementById("closeD").onclick = ()=>{ el.hidden = true; };
  if (chart) chart.destroy();
  chart = new Chart(document.getElementById("lBar"), {type:"bar", data:{labels:A.map(a=>a.n), datasets:[{label:"pour 10 000 hab.", data:r.dens, _pal:0, backgroundColor:PAL[0], borderColor:PAL[0], borderRadius:4}]},
    options:{responsive:true, maintainAspectRatio:false, indexAxis:"y", plugins:{legend:{display:false, labels:{color:C("--muted")}}, tooltip:{...tipColors(), callbacks:{label:c=>d1(c.raw)}}},
     scales:{x:{ticks:{color:C("--muted")}, grid:{color:C("--line")}, beginAtZero:true}, y:{ticks:{color:C("--muted")}, grid:{display:false}}}}});
  if (!isB){
    const yrs = L.years;
    if (window._lflow) window._lflow.destroy();
    const partial = yrs.length > (L.full_years||yrs).length;
    window._lflow = new Chart(document.getElementById("dChart"), {type:"bar", data:{labels:yrs.map((y,i)=>partial && i===yrs.length-1 ? y+" (en cours)" : y), datasets:[
      {label:"Créations", data:r.series.map(v=>v[0]), _pal:3, backgroundColor:PAL[3], borderColor:PAL[3], borderRadius:3},
      {label:"Fermetures", data:r.series.map(v=>v[1]), _pal:4, backgroundColor:PAL[4], borderColor:PAL[4], borderRadius:3}]},
      options:{responsive:true, maintainAspectRatio:false, plugins:{legend:{labels:{color:C("--muted"), boxWidth:12}}, tooltip:{...tipColors()}},
       scales:{x:{ticks:{color:C("--muted")}, grid:{display:false}}, y:{ticks:{color:C("--muted")}, grid:{color:C("--line")}, beginAtZero:true}}}});
  }
  el.scrollIntoView({behavior:"smooth", block:"start"});
  if (r.kind==="naf"){
    const box = document.getElementById("etabList");
    const search = `https://annuaire-entreprises.data.gouv.fr/rechercher?activite_principale=${encodeURIComponent(r.c)}&cp_dep=76`;
    try {
      // Published site: static etab/<NAF>.json files; local server: /api/etab
      let list = null;
      for (const u of [`etab/${encodeURIComponent(r.c)}.json`, `/api/etab?naf=${encodeURIComponent(r.c)}`]) {
        try { const res = await fetch(u); if (res.ok) { list = await res.json(); break; } } catch(err) {}
      }
      if (!list) throw 0;
      const SIZE = {"00":"0","01":"1-2","02":"3-5","03":"6-9","11":"10-19","12":"20-49","21":"50-99","22":"100-199","31":"200-249","32":"250-499","41":"500-999","42":"1000-1999"};
      box.innerHTML = `<b>Établissements actifs au Havre (+30 km)</b> <span class="muted">(${list.length}${list.length>=200?" premiers":""}, plus grands d'abord)</span>
       <div class="tw"><table><thead><tr><th>Établissement</th><th>Commune</th><th class="n">Effectif</th><th>Créé en</th></tr></thead><tbody>
       ${list.map(e=>`<tr style="cursor:default"><td class="l">${e.siret ? `<a href="https://annuaire-entreprises.data.gouv.fr/etablissement/${e.siret}" target="_blank" rel="noopener" style="color:var(--accent)">${esc(e.name||e.siret)}</a>` : `<span class="muted">${esc(e.name || "non diffusible")}</span>`}</td>
        <td>${esc(e.commune)}</td><td class="n">${esc(SIZE[e.size]||"—")}</td><td>${esc((e.created||"").slice(0,4))}</td></tr>`).join("")}</tbody></table></div>`;
    } catch(e) {
      box.innerHTML = `<b>Établissements</b> — liste indisponible ici. <a href="${search}" target="_blank" rel="noopener" style="color:var(--accent)">Voir sur l'Annuaire des entreprises</a>.`;
    }
  }
}
document.getElementById("lSrc").onclick = e=>{ const b=e.target.closest("button"); if(!b) return; lSrc=b.dataset.s;
  document.querySelectorAll("#lSrc button").forEach(x=>x.setAttribute("aria-pressed", x===b)); setDomLocal(); renderLocal(); };
["lq","lLevel","lDom","lSort","lEmp","lHold"].forEach(id=>document.getElementById(id).addEventListener("input", renderLocal));
document.getElementById("ltb").onclick = e=>{ const tr=e.target.closest("tr[data-c]"); if(!tr) return;
  document.querySelectorAll("#ltb tr.sel").forEach(x=>x.classList.remove("sel")); tr.classList.add("sel");
  showLocal(window._lrows.find(r=>r.c===tr.dataset.c)); };

// ---- gym market: competitors (gyms.json) and figures for the centre city (sport.py) ----------
const G = D.gym || {}, GJ = G.gyms;
const PERMONTH = {"4sem":13/12, "mois":1, "semaine":52/12, "an":1/12};
const UNIT = {"4sem":"/4 sem.", "mois":"/mois", "semaine":"/semaine", "an":"/an", "seance":"/séance", "carnet":""};
const GSIZE = {"21":"50–99","22":"100–199","31":"200–249","32":"250–499","41":"500–999","42":"1 000–1 999","51":"2 000–4 999","52":"5 000–9 999","53":"10 000+"};
const eur = v => v==null ? "—" : v.toLocaleString("fr-FR", {minimumFractionDigits: v%1 ? 2 : 0, maximumFractionDigits: 2}) + " €";
const median = a => { if (!a.length) return null; const s=[...a].sort((x,y)=>x-y), m=s.length>>1; return s.length%2 ? s[m] : (s[m-1]+s[m])/2; };
const share = (part, tot) => tot ? 100*part/tot : null;
let gView = "comp", gReady = false;
const gCharts = {};
function gChart(id, cfg){ if (gCharts[id]) gCharts[id].destroy(); gCharts[id] = new Chart(document.getElementById(id), cfg); return gCharts[id]; }
const barOpts = (horizontal, fmtv) => ({responsive:true, maintainAspectRatio:false, indexAxis: horizontal ? "y" : "x",
  plugins:{legend:{labels:{color:C("--muted"), boxWidth:12}}, tooltip:{...tipColors(), callbacks:{label:c=>`${c.dataset.label} : ${fmtv(c.raw)}`}}},
  scales:{x:{ticks:{color:C("--muted")}, grid:{color:C("--line"), display: horizontal}, beginAtZero:true},
          y:{ticks:{color:C("--muted")}, grid:{color:C("--line"), display: !horizontal}, beginAtZero:true}}});

function gymInfo(g, i){
  const subs = (g.plans||[]).filter(p=>PERMONTH[p.unit] && !p.reduced && !p.side).map(p=>({p, m:p.price*PERMONTH[p.unit]})).sort((a,b)=>a.m-b.m);
  const sess = (g.plans||[]).filter(p=>p.unit==="seance" || p.unit==="carnet").map(p=>p.unit==="carnet" ? p.price/(p.count||1) : p.price);
  return {...g, i, month: subs.length ? subs[0].m : null, unc: subs.length ? !!subs[0].p.uncertain : false,
          session: sess.length ? Math.min(...sess) : null, np: (g.perks||[]).length};
}
const GYMS = GJ ? GJ.gyms.map(gymInfo) : [];
const CATS = [...new Set(GYMS.map(g=>g.cat))];
const priceLabel = g => g.month!=null ? `${g.unc?"≈ ":""}${Math.round(g.month)} €<span class="muted">/mois</span>`
  : g.session!=null ? `${eur(g.session)}<span class="muted">/séance</span>` : `<span class="muted">non publié</span>`;
const perkName = k => (GJ && GJ.perks[k]) || k;
const tags = ks => ks.map(k=>`<span class="tag">${esc(perkName(k))}</span>`).join("");

// figures for the centre city
function cityDens(row){        // row.n = counts per city (Le Havre first)
  const A = G.cities || [], dens = row.n.map((v,i)=>v!=null && A[i] && A[i].pop ? v/A[i].pop*1e4 : null);
  const others = dens.slice(1).filter(v=>v!=null), avg = others.length ? others.reduce((a,b)=>a+b,0)/others.length : null;
  return {dens, avg, idx: avg ? 100*dens[0]/avg : null, gap: avg!=null && A[0] ? Math.round(avg*A[0].pop/1e4 - row.n[0]) : null};
}
const AGES = [["Y_LT15","0–14"],["Y15T24","15–24"],["Y25T39","25–39"],["Y40T54","40–54"],["Y55T64","55–64"],["Y65T79","65–79"],["Y_GE80","80+"]];
function ageData(city){
  const a = (G.ages||{})[city] || {}, ys = Object.keys(a).sort();
  return {ys, first: a[ys[0]] || {}, last: a[ys[ys.length-1]] || {}};
}
const jobFig = (j, t) => (j.v||[])[t] || null;     // [offers 12 m, a year before, jobseekers, hires 12 m]
const ratio = f => f && f[0]!=null && f[2] ? f[0]/f[2] : null;
const r2 = v => v==null ? "—" : v.toFixed(2).replace(".",",");

function gymIdeas(){
  const out = [];
  const fit = GYMS.filter(g=>(g.cat==="Low cost" || g.cat==="Fitness / musculation") && g.month!=null);
  const ms = fit.map(g=>g.month), med = median(ms);
  const low = fit.filter(g=>g.month<=28).map(g=>g.name.replace(/ \(.*\)/,""));
  const box = GYMS.filter(g=>g.cat==="CrossFit / Hyrox").flatMap(g=>(g.plans||[]).filter(p=>PERMONTH[p.unit] && !p.reduced).map(p=>p.price*PERMONTH[p.unit]));
  const ems = GYMS.filter(g=>g.cat.startsWith("Électro") && g.month!=null).map(g=>g.month);
  if (ms.length) out.push(`<b>Prix</b> : les abonnements fitness vont de ${Math.round(Math.min(...ms))} à ${Math.round(Math.max(...ms))} €/mois (médiane ${Math.round(med)} €).
    ${low.length ? `Le bas du marché est tenu par ${low.join(", ")}. ` : ""}Une salle existante gagne rarement sur le prix face au low cost : elle gagne sur l'accompagnement, l'ambiance et les créneaux.
    ${box.length ? `Les box CrossFit facturent ${Math.round(Math.min(...box))} à ${Math.round(Math.max(...box))} €/mois` : ""}${ems.length ? ` et l'électrostimulation plus de ${Math.round(Math.min(...ems)/10)*10} €/mois` : ""} : le coaching se vend cher.`);
  if (GJ){
    const cnt = Object.keys(GJ.perks).map(k=>({k, n:GYMS.filter(g=>(g.perks||[]).includes(k)).length})).sort((a,b)=>a.n-b.n);
    const rare = cnt.filter(x=>x.n<=2);
    if (rare.length) out.push(`<b>Atouts rares au Havre</b> : ${rare.map(x=>`${esc(perkName(x.k)).toLowerCase()} (${x.n ? x.n+" salle"+(x.n>1?"s":"") : "aucune"})`).join(", ")}.
      Ce sont les moyens les plus simples de se démarquer ; plusieurs coûtent peu (pause d'abonnement, tarif senior, essai à petit prix, cours enfants).`);
  }
  const f120 = (G.bpe||[]).find(b=>b.c==="F120"), A = G.cities||[];
  if (f120 && A.length){ const d = cityDens(f120);
    if (d.idx!=null && d.idx<85) out.push(`<b>Ville sous-équipée</b> : ${fmt(f120.n[0])} salles de remise en forme recensées (BPE ${G.bpe_year||""}) pour ${fmt(A[0].pop)} habitants,
      soit ${d1(d.dens[0])} pour 10 000 contre ${d1(d.avg)} en moyenne à ${A.slice(1).map(a=>a.n).join(", ")}. Il manquerait environ ${fmt(d.gap)} salles pour atteindre leur niveau :
      une partie des Havrais ne fréquente aucune salle, c'est la clientèle à aller chercher.`); }
  const lh = ageData("Le Havre"), others = A.slice(1).map(a=>ageData(a.n));
  if (lh.ys.length>1){
    const s65 = x => (x.Y65T79||0)+(x.Y_GE80||0);
    const n65 = s65(lh.last), g65 = share(n65 - s65(lh.first), s65(lh.first)), gT = share((lh.last._T||0)-(lh.first._T||0), lh.first._T);
    const sh = share(n65, lh.last._T), osh = others.map(o=>share(s65(o.last), o.last._T)).filter(v=>v!=null);
    const seniorG = GYMS.filter(g=>(g.perks||[]).includes("senior")).map(g=>g.name);
    const apa = D.gym.jobs.filter(j=>j.c==="G1220" || j.c==="G1215").map(j=>jobFig(j,0)).filter(Boolean);
    out.push(`<b>Seniors</b> : ${fmt(n65)} Havrais ont 65 ans ou plus (${Math.round(sh)} % des habitants${osh.length ? `, contre ${Math.round(Math.min(...osh))}–${Math.round(Math.max(...osh))} % dans les autres villes` : ""}),
      ${pct(g65)} depuis ${lh.ys[0]} alors que la population totale évolue de ${pct(gT)}. ${seniorG.length ? `Seule${seniorG.length>1?"s":""} ${seniorG.join(", ")} affiche${seniorG.length>1?"nt":""} un tarif senior` : "Aucune salle n'affiche de tarif senior"},
      et aucune offre « sport santé » n'apparaît dans les offres publiées. Pistes : créneaux en journée, renforcement et équilibre, partenariat avec médecins et kinés (activité physique sur ordonnance).
      ${apa.length ? `Les éducateurs APA / sport santé restent rares localement (${fmt(apa.reduce((s,f)=>s+(f[2]||0),0))} demandeurs d'emploi).` : ""}`);
  }
  const E = G.employers;
  if (E && E.total) out.push(`<b>Entreprises</b> : ${fmt(E.total)} établissements de 50 salariés et plus dans la ville, dont ${fmt(E.bands["250–999"]+E.bands["1 000 et plus"])} de 250 et plus.
    Une offre entreprise (tarif négocié avec le CSE, séances sur site ou à la pause déjeuner, défis entre équipes) apporte des membres par groupes entiers.`);
  const coach = D.gym.jobs.filter(j=>j.c==="G1204" || j.c==="G1217");
  const sum = (t,k) => coach.reduce((s,j)=>s+((jobFig(j,t)||[])[k]||0),0);
  if (coach.length && sum(0,2)) out.push(`<b>Recruter des coachs</b> : ${fmt(sum(0,2))} demandeurs d'emploi éducateurs sportifs ou moniteurs de salle pour ${fmt(sum(0,0))} offres sur 12 mois dans la zone du Havre
    (Normandie : ${fmt(sum(4,2))} pour ${fmt(sum(4,0))}). Les candidats ne manquent pas : renforcer le coaching en salle est faisable rapidement.`);
  const gymNaf = (G.naf||[]).find(n=>n.c==="93.13Z"), yrs = G.years||[], cur = new Date().getFullYear();
  if (gymNaf){
    const idx = yrs.map((y,i)=>[y,i]).filter(([y])=>y<cur).slice(-3);
    const c = idx.reduce((s,[,i])=>s+gymNaf.f[i][0],0), f = idx.reduce((s,[,i])=>s+gymNaf.f[i][1],0);
    out.push(`<b>Concurrence</b> : ${c} salles de sport créées et ${f} fermées dans la ville en ${idx[0][0]}–${idx[idx.length-1][0]} (Sirene).
      ${(GJ && GJ.watch||[]).length ? `À surveiller : ${GJ.watch.map(w=>esc(w.name)).join(", ")}. ` : ""}Le bon moment pour fidéliser : engagement souple, parrainage, suivi des nouveaux inscrits pendant leurs premières semaines.`);
  }
  const indep = GYMS.filter(g=>g.rating>=4.8 && g.reviews>=100).sort((a,b)=>b.reviews-a.reviews).slice(0,4);
  const chainLow = GYMS.filter(g=>g.rating!=null && g.rating<4.3 && g.reviews>=100);
  if (indep.length) out.push(`<b>Avis Google</b> : les salles les mieux notées ont 4,8 à 5/5 avec plus de 100 avis (${indep.map(g=>`${esc(g.name)} ${g.rating.toFixed(1).replace(".",",")} · ${g.reviews} avis`).join(" ; ")})
    ${chainLow.length ? `, quand ${chainLow.map(g=>esc(g.name)).join(" et ")} plafonne${chainLow.length>1?"nt":""} à ${Math.max(...chainLow.map(g=>g.rating)).toFixed(1).replace(".",",")}` : ""}.
    Demander un avis après les premières séances est l'outil le moins cher pour être choisi.`);
  return out;
}

function renderGym(){
  if (!GJ){ document.getElementById("gIntro").innerHTML = "Pas de fichier <code>gyms.json</code> : relevé des concurrents absent."; return; }
  if (!gReady){
    gReady = true;
    const A = G.cities||[], lh = ageData("Le Havre");
    const fit = GYMS.filter(g=>(g.cat==="Low cost" || g.cat==="Fitness / musculation") && g.month!=null).map(g=>g.month);
    const f120 = (G.bpe||[]).find(b=>b.c==="F120"), d120 = f120 ? cityDens(f120) : null;
    const s65 = lh.ys.length ? (lh.last.Y65T79||0)+(lh.last.Y_GE80||0) : null;
    document.getElementById("gIntro").textContent = `${GYMS.length} salles et studios détaillés, relevé du ${GJ.checked.split("-").reverse().join("/")} (sites des salles, annuaires, avis Google). `
      + `Chiffres de la ville seule : ${A.length ? fmt(A[0].pop)+" habitants" : ""}. Pour l'emploi, zone de l'agence France Travail et Normandie.`;
    document.getElementById("gKpi").innerHTML = `
      <div><b>${GYMS.length + (GJ.others||[]).length}</b><span>lieux recensés (${GYMS.length} détaillés)</span></div>
      <div><b>${fit.length ? Math.round(median(fit))+" €" : "—"}</b><span>prix médian d'un abonnement fitness, par mois</span></div>
      ${d120 ? `<div><b class="${d120.idx<80?"down":""}">${d1(d120.dens[0])}</b><span>salles de remise en forme pour 10 000 hab. (autres villes : ${d1(d120.avg)})</span></div>` : ""}
      ${s65!=null ? `<div><b>${Math.round(share(s65, lh.last._T))} %</b><span>d'habitants de 65 ans et plus (${fmt(s65)})</span></div>` : ""}
      ${G.employers ? `<div><b>${fmt(G.employers.total)}</b><span>entreprises de 50 salariés et plus</span></div>` : ""}`;
    document.getElementById("gIdeas").innerHTML = gymIdeas().map(x=>`<li style="margin-bottom:6px">${x}</li>`).join("");
    document.getElementById("gCat").innerHTML = `<option value="">Tous les types</option>${CATS.map(c=>`<option>${esc(c)}</option>`).join("")}`;
    document.getElementById("gWatch").innerHTML = (GJ.watch||[]).map(w=>`<li><b>${esc(w.name)}</b> — ${esc(w.text)} <a href="${esc(w.src)}" target="_blank" rel="noopener" style="color:var(--accent)">source</a></li>`).join("");
    document.getElementById("gOthers").innerHTML = (GJ.others||[]).map(o=>`<li>${esc(o.name)} <span class="muted">· ${esc(o.cat)} · ${esc(o.address)}</span></li>`).join("");
    renderQuality();
    document.getElementById("gAbout").innerHTML = `${esc(GJ.about)} ${(GJ.market_sources||[]).map(m=>`<a href="${esc(m.src)}" target="_blank" rel="noopener" style="color:var(--accent)">${esc(m.label)}</a>`).join(" · ")}`;
    ["gq","gCat","gSort"].forEach(id=>document.getElementById(id).addEventListener("input", renderGymTable));
    document.getElementById("gtb").onclick = e=>{
      if (e.target.closest("a")) return;
      const tr = e.target.closest("tr[data-i]"); if (!tr) return;
      const next = tr.nextElementSibling;
      if (next && next.classList.contains("gx")) { next.remove(); tr.classList.remove("sel"); return; }
      tr.classList.add("sel"); tr.insertAdjacentHTML("afterend", gymDetail(GYMS[+tr.dataset.i]));
    };
    document.getElementById("gView").onclick = e=>{ const b=e.target.closest("button"); if (!b) return; gView=b.dataset.v;
      document.querySelectorAll("#gView button").forEach(x=>x.setAttribute("aria-pressed", x===b)); showGymView(); };
    document.getElementById("gFlowNaf").innerHTML = (G.naf||[]).map(n=>`<option value="${n.c}">${esc(n.l)}</option>`).join("");
    document.getElementById("gFlowNaf").oninput = drawGymFlow;
    const cb = document.getElementById("gClubBtn");
    if (D.club || D.clubEnc) cb.hidden = false;
  }
  showGymView();
}
function showGymView(){
  document.getElementById("g-comp").hidden = gView!=="comp";
  document.getElementById("g-market").hidden = gView!=="market";
  document.getElementById("g-rank").hidden = gView!=="rank";
  document.getElementById("g-club").hidden = gView!=="club";
  if (gView==="comp") { renderGymTable(); drawGymPrices(); renderGymPerks(); }
  else if (gView==="rank") renderRank();
  else if (gView==="club") renderClub();
  else renderGymMarket();
}
function renderGymTable(){
  const norm = t => String(t||"").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const q = norm(document.getElementById("gq").value.trim()), cat = document.getElementById("gCat").value, k = document.getElementById("gSort").value;
  let rows = GYMS.filter(g=>(!cat || g.cat===cat) && (!q || norm([g.name, g.address, g.cat, g.kind, g.notes, ...(g.perks||[]).map(perkName)].join(" ")).includes(q)));
  const key = {price:g=>-(g.month ?? (g.session!=null ? 500+g.session : 1e6)), rating:g=>(g.rating??0)*1e4+(g.reviews??0), reviews:g=>g.reviews??-1, perks:g=>g.np, name:g=>0}[k];
  rows.sort((a,b)=>key(b)-key(a) || a.name.localeCompare(b.name,"fr"));
  document.getElementById("gtb").innerHTML = rows.map(g=>`<tr data-i="${g.i}">
    <td class="l"><b>${esc(g.name)}</b> ${qIcon(g)}<div class="muted" style="font-size:12px">${esc(g.address)}</div></td>
    <td class="l" style="min-width:140px">${esc(g.cat)}<div class="muted" style="font-size:12px">${esc(g.kind)}</div></td>
    <td class="n">${priceLabel(g)}</td><td class="l" style="min-width:150px;font-size:12px">${esc(g.commit||"—")}</td>
    <td class="n">${g.fee==null ? "—" : g.fee===0 ? "offerts" : eur(g.fee)}</td>
    <td class="n">${g.rating!=null ? `★ ${g.rating.toFixed(1).replace(".",",")}<div class="muted" style="font-size:12px">${g.reviews!=null ? fmt(g.reviews)+" avis" : esc(g.rating_src)}</div>` : "—"}</td>
    <td class="l" style="min-width:240px">${tags(g.perks||[])}</td></tr>`).join("");
  document.getElementById("gcnt").textContent = `${rows.length} salle(s). Cliquez sur une ligne pour les formules, horaires et sources. ≈ = prix d'annuaire ou « à partir de », à vérifier. Prix dès = formule la moins chère ramenée au mois (4 semaines × 13 / 12).`;
}
function gymDetail(g){
  const plans = (g.plans||[]).map(p=>`<tr style="cursor:default"><td class="l">${esc(p.n)}${p.uncertain?' <span class="muted">(à vérifier)</span>':""}</td>
    <td class="n">${eur(p.price)}<span class="muted">${p.unit==="carnet" ? ` (${p.count} séances)` : UNIT[p.unit]||""}</span></td>
    <td class="n">${PERMONTH[p.unit] && p.unit!=="mois" ? Math.round(p.price*PERMONTH[p.unit])+" €/mois" : p.unit==="carnet" && p.count ? eur(Math.round(p.price/p.count*100)/100)+"/séance" : ""}</td>
    <td class="l" style="font-size:12px">${esc(p.commit||"")}</td><td class="l" style="font-size:12px">${esc(p.inc||"")}</td></tr>`).join("");
  return `<tr class="gx" style="cursor:default;background:var(--soft)"><td colspan="7" style="white-space:normal">
    ${plans ? `<div class="tw" style="margin:4px 0 10px"><table><thead><tr><th>Formule</th><th class="n">Prix</th><th class="n">Équivalent</th><th>Engagement</th><th>Inclus</th></tr></thead><tbody>${plans}</tbody></table></div>`
      : `<p class="muted">Aucun tarif publié trouvé.</p>`}
    ${g.hours ? `<div><b>Horaires</b> : ${esc(g.hours)}</div>` : ""}
    ${g.notes ? `<div style="margin-top:4px">${esc(g.notes)}</div>` : ""}
    ${g.rating!=null ? `<div class="muted" style="margin-top:4px">Note : ${esc(g.rating_src)}</div>` : ""}
    <div style="margin-top:6px;font-size:12px">Sources ${g.official ? "(dont site officiel)" : "(annuaires / comparatifs)"} : ${(g.sources||[]).map((s,i)=>`<a href="${esc(s)}" target="_blank" rel="noopener" style="color:var(--accent)">${i+1}</a>`).join(" · ")}</div>
    ${QG[g.name] ? `<div style="margin-top:8px"><b>Contrôles du ${esc(fdate(QUAL.checked))}</b>${QG[g.name].checks.map(c=>`<div class="chk">${stIcon(c.s)}<div style="font-size:12px"><b>${esc(c.l)}</b> : ${esc(c.v)}</div></div>`).join("")}
      ${(QG[g.name].plans||[]).filter(p=>p.s!=="ok").map(p=>`<div class="muted" style="font-size:12px">${esc(p.n)} (${eur(p.price)}) : ${esc(p.v)}</div>`).join("")}</div>` : ""}
  </td></tr>`;
}
// ---- data quality (validate.py) ----------------------------------------------------------
const QUAL = D.quality || null;
const QG = Object.fromEntries(((QUAL && QUAL.gyms) || []).map(g=>[g.name, g]));
const fdate = d => String(d||"").split("-").reverse().join("/");
function qIcon(g){
  const q = QG[g.name]; if (!q) return "";
  const bad = q.checks.filter(c=>c.s==="bad"||c.s==="warn");
  const tip = bad.length ? bad.map(c=>`${c.l} : ${c.v}`).join("\n") : "Contrôles passés : prix, registre des entreprises, fraîcheur, lien, cohérence";
  return `<span title="${esc(tip)}" style="vertical-align:middle">${stIcon(q.s==="info" ? "ok" : q.s)}</span>`;
}
function renderQuality(){
  const el = document.getElementById("gQual");
  if (!QUAL){ el.innerHTML = `<b>Qualité des données</b><p class="muted">Pas encore de contrôle : lancez <code>python validate.py</code>.</p>`; return; }
  const G = QUAL.gyms || [], by = l => G.map(g=>g.checks.find(c=>c.l===l)).filter(Boolean);
  const cnt = (l, s) => by(l).filter(c=>c.s===s).length;
  const priced = by("Prix").filter(c=>c.s!=="info" || !/non officielle|aucun prix/.test(c.v));
  const COLS = ["Prix","Registre","Fraîcheur","Lien","Cohérence"];
  const cell = c => c ? `<td class="l" style="font-size:12px;min-width:150px"><div style="display:flex;gap:6px;align-items:flex-start">${stIcon(c.s)}<span>${esc(c.v)}</span></div></td>` : "<td>—</td>";
  el.innerHTML = `<div class="dh"><div><b>Qualité des données</b> <span class="muted">— contrôles automatiques du ${esc(fdate(QUAL.checked))}</span></div></div>
   <div class="kp">
    <div><b>${cnt("Prix","ok")} / ${priced.length}</b><span>salles dont les prix ont été retrouvés sur le site officiel (${G.length-priced.length} sans prix officiel publié)</span></div>
    <div><b>${cnt("Registre","ok")} / ${by("Registre").length}</b><span>salles confirmées au registre des entreprises, à leur adresse</span></div>
    <div><b>${cnt("Fraîcheur","ok")} / ${by("Fraîcheur").length}</b><span>relevés de moins de 90 jours</span></div>
    <div><b>${(QUAL.stats||[]).filter(c=>c.s==="ok").length} / ${(QUAL.stats||[]).filter(c=>c.s!=="info").length}</b><span>contrôles des statistiques réussis</span></div>
   </div>
   <details><summary style="cursor:pointer"><b>Détail par salle</b> <span class="muted">(à revoir en premier)</span></summary>
    <div class="tw" style="margin-top:8px"><table class="plain"><thead><tr><th>Salle</th>${COLS.map(c=>`<th>${c}</th>`).join("")}</tr></thead><tbody>
    ${[...G].sort((a,b)=>["bad","warn","info","ok"].indexOf(a.s)-["bad","warn","info","ok"].indexOf(b.s)).map(g=>`<tr><td class="l"><b>${esc(g.name)}</b></td>${COLS.map(l=>cell(g.checks.find(c=>c.l===l))).join("")}</tr>`).join("")}
    </tbody></table></div></details>
   ${(QUAL.others||[]).length ? `<details style="margin-top:8px"><summary style="cursor:pointer"><b>Autres lieux recensés</b> <span class="muted">(existence au registre)</span></summary>
     ${QUAL.others.map(o=>`<div class="chk">${stIcon(o.s)}<div style="font-size:12px"><b>${esc(o.name)}</b> : ${esc(o.checks[0].v)}</div></div>`).join("")}</details>` : ""}
   <details style="margin-top:8px"><summary style="cursor:pointer"><b>Statistiques</b> <span class="muted">(France Travail, INSEE, Sirene, audit des sites)</span></summary>
     ${(QUAL.stats||[]).map(c=>`<div class="chk">${stIcon(c.s)}<div style="font-size:12px"><b>${esc(c.l)}</b> : ${esc(c.v)}</div></div>`).join("")}</details>
   <p class="note">Prix : chaque tarif relevé sur un site officiel doit encore y figurer (sinon il a probablement changé). Registre : une activité doit être immatriculée à l'adresse de la salle (base Sirene via recherche-entreprises.api.gouv.fr) ; les associations sans salarié n'ont pas toujours de numéro SIRET. Fraîcheur : relevé à refaire après 90 jours. Les prix « ≈ » viennent d'annuaires et ne sont pas vérifiables automatiquement : à confirmer par téléphone.</p>`;
}
function drawGymPrices(){
  const rows = GYMS.filter(g=>g.month!=null).sort((a,b)=>a.month-b.month);
  document.getElementById("gPriceWrap").style.height = (rows.length*26+60)+"px";
  gChart("gPrice", {type:"bar", data:{labels:rows.map(g=>g.name.replace(/ \(.*\)/,"")), datasets:CATS.map((c,ci)=>({label:c, _pal:ci%6, backgroundColor:PAL[ci%6], borderColor:PAL[ci%6], borderRadius:4,
      data:rows.map(g=>g.cat===c ? Math.round(g.month) : null), skipNull:true}))},
    options:{...barOpts(true, v=>v+" €/mois"), scales:{x:{stacked:true, ticks:{color:C("--muted")}, grid:{color:C("--line")}, beginAtZero:true}, y:{stacked:true, ticks:{color:C("--muted"), autoSkip:false}, grid:{display:false}}}}});
}
function renderGymPerks(){
  const rows = Object.keys(GJ.perks).map(k=>({k, gs:GYMS.filter(g=>(g.perks||[]).includes(k))})).sort((a,b)=>a.gs.length-b.gs.length);
  document.getElementById("gPerks").innerHTML = rows.map(r=>`<tr style="cursor:default"><td class="l">${esc(perkName(r.k))}${r.gs.length<=2 ? ' <span class="tag" style="background:var(--lv4)">rare</span>' : ""}</td>
    <td class="n">${r.gs.length} / ${GYMS.length}</td><td class="l" style="font-size:12px">${r.gs.map(g=>esc(g.name.replace(/ \(.*\)/,""))).join(", ") || '<span class="muted">aucune trouvée</span>'}</td></tr>`).join("");
}
function drawGymFlow(){
  const n = (G.naf||[]).find(x=>x.c===document.getElementById("gFlowNaf").value); if (!n) return;
  const yrs = G.years, cur = new Date().getFullYear();
  gChart("gFlow", {type:"bar", data:{labels:yrs.map(y=>y===cur ? y+" (en cours)" : y), datasets:[
      {label:"Créations", data:n.f.map(v=>v[0]), _pal:3, backgroundColor:PAL[3], borderColor:PAL[3], borderRadius:3},
      {label:"Fermetures", data:n.f.map(v=>v[1]), _pal:4, backgroundColor:PAL[4], borderColor:PAL[4], borderRadius:3}]}, options:barOpts(false, v=>v)});
}
// ---- gym ranking: a score out of 100 from reviews, price, services and flexibility ----------
const PARTS = [["rev","Avis",35],["price","Prix",25],["serv","Services",25],["flex","Flexibilité",15]];
const W_DEFAULT = Object.fromEntries(PARTS.map(([k,,w])=>[k,w]));
let W = {...W_DEFAULT}, rReady = false;
try { const s = JSON.parse(localStorage.getItem("gymWeights")||"null"); if (s && PARTS.every(([k])=>typeof s[k]==="number")) W = s; } catch(e) {}
const clamp = v => Math.max(0, Math.min(100, v));
function gymScores(){
  const rated = GYMS.filter(g=>g.rating!=null), C = rated.reduce((s,g)=>s+g.rating,0)/(rated.length||1), M = 50;
  const groups = {};
  const pkey = g => g.cat + (g.month!=null ? "|m" : "|s"), pval = g => g.month ?? g.session;
  GYMS.forEach(g=>{ if (pval(g)!=null) (groups[pkey(g)] = groups[pkey(g)] || []).push(pval(g)); });
  const maxPerks = Math.max(1, ...GYMS.map(g=>g.np));
  return GYMS.map(g=>{
    const p = {};
    if (g.rating!=null){ const v = g.reviews ?? 10; p.rev = clamp(((v*g.rating + M*C)/(v+M) - 3.5)/1.5*100); }
    if (pval(g)!=null){ const arr = groups[pkey(g)], med = median(arr);
      p.price = arr.length<2 ? 50 : clamp(50 + 100*(med - pval(g))/med); }
    p.serv = 100*g.np/maxPerks;
    const has = k => (g.perks||[]).includes(k);
    p.flex = (has("nocommit")?40:0) + (has("trial")?20:0) + (has("freeze")?20:0) + (g.fee===0 ? 20 : g.fee!=null && g.fee<=20 ? 10 : 0);
    const wsum = PARTS.reduce((s,[k])=>s+W[k],0);      // unknown rating or price = neutral 50
    const contrib = Object.fromEntries(PARTS.map(([k])=>[k, wsum ? W[k]*(p[k] ?? 50)/wsum : 0]));
    const score = wsum ? Object.values(contrib).reduce((a,b)=>a+b,0) : null;
    const facts = [g.rating!=null, g.reviews!=null, pval(g)!=null, g.fee!=null, !!g.commit, !!g.hours];
    return {g, p, score, contrib, data: Math.round(100*facts.filter(Boolean).length/facts.length)};
  });
}
function renderRank(){
  if (!rReady){
    rReady = true;
    document.getElementById("rCat").innerHTML = `<option value="">Tous les types</option>${CATS.map(c=>`<option>${esc(c)}</option>`).join("")}`;
    document.getElementById("rWeights").innerHTML = PARTS.map(([k,l])=>`<div><label for="w-${k}"><b id="wv-${k}"></b><span>${l}</span></label>
      <input type="range" id="w-${k}" min="0" max="100" step="5" style="width:100%;padding:0" aria-label="Poids ${l}"></div>`).join("");
    PARTS.forEach(([k])=>document.getElementById("w-"+k).addEventListener("input", e=>{ W[k] = +e.target.value; saveW(); renderRank(); }));
    document.getElementById("rReset").onclick = ()=>{ W = {...W_DEFAULT}; saveW(); renderRank(); };
    ["rMode","rCat"].forEach(id=>document.getElementById(id).addEventListener("input", renderRank));
    const others = (GJ.others||[]).map(o=>o.name);
    document.getElementById("rUnscored").textContent = others.length ? `Non notés faute d'informations publiées : ${others.join(", ")}.` : "";
  }
  const tot = PARTS.reduce((s,[k])=>s+W[k],0) || 1;
  PARTS.forEach(([k])=>{ document.getElementById("w-"+k).value = W[k]; document.getElementById("wv-"+k).textContent = Math.round(100*W[k]/tot)+" %"; });
  const mode = document.getElementById("rMode").value, cat = document.getElementById("rCat").value;
  let rows = gymScores().filter(r=>r.score!=null && (!cat || r.g.cat===cat)).sort((a,b)=>b.score-a.score);
  if (mode==="cat") rows.sort((a,b)=>CATS.indexOf(a.g.cat)-CATS.indexOf(b.g.cat) || b.score-a.score);
  const cell = (v, sub) => `<td class="n">${v==null ? "—" : Math.round(v)}${sub ? `<div class="muted" style="font-size:11px">${sub}</div>` : ""}</td>`;
  let last = null, rank = 0;
  document.getElementById("rtb").innerHTML = rows.map(r=>{
    const g = r.g; let head = "";
    if (mode==="cat" && g.cat!==last){ last = g.cat; rank = 0; head = `<tr><th colspan="8" style="background:var(--soft);cursor:default">${esc(g.cat)}</th></tr>`; }
    rank++;
    const price = g.month!=null ? `${g.unc?"≈ ":""}${Math.round(g.month)} €/mois` : g.session!=null ? `${eur(g.session)}/séance` : "";
    return head + `<tr style="cursor:default"><td class="n">${rank}</td>
      <td class="l"><b>${esc(g.name)}</b><div class="muted" style="font-size:12px">${esc(g.cat)} · ${esc(g.kind)}</div></td>
      <td><div style="display:flex;align-items:center;gap:8px"><b style="font-size:17px;min-width:28px;font-variant-numeric:tabular-nums">${Math.round(r.score)}</b>
        <div style="flex:1;height:8px;background:var(--soft);border-radius:4px;min-width:70px"><div style="width:${r.score}%;height:100%;background:var(--accent);border-radius:4px"></div></div></div></td>
      ${cell(r.p.rev, g.rating!=null ? `${g.rating.toFixed(1).replace(".",",")}★ · ${g.reviews!=null ? fmt(g.reviews)+" avis" : "nb inconnu"}` : "pas de note")}
      ${cell(r.p.price, price || "non publié")}${cell(r.p.serv, `${g.np} atouts`)}${cell(r.p.flex, "")}
      <td class="n ${r.data<50?"down":""}">${r.data} %</td></tr>`;
  }).join("");
  const sorted = [...rows].sort((a,b)=>b.score-a.score);
  document.getElementById("rChartWrap").style.height = (sorted.length*26+70)+"px";
  gChart("rChart", {type:"bar", data:{labels:sorted.map(r=>r.g.name.replace(/ \(.*\)/,"")),
      datasets:PARTS.map(([k,l],i)=>({label:l, _pal:i, backgroundColor:PAL[i], borderColor:PAL[i], data:sorted.map(r=>Math.round(r.contrib[k]*10)/10)}))},
    options:{...barOpts(true, v=>(v+"").replace(".",",")+" pts"),
      scales:{x:{stacked:true, max:100, ticks:{color:C("--muted")}, grid:{color:C("--line")}, beginAtZero:true}, y:{stacked:true, ticks:{color:C("--muted"), autoSkip:false}, grid:{display:false}}}}});
}
function saveW(){ try { localStorage.setItem("gymWeights", JSON.stringify(W)); } catch(e) {} }

function renderGymMarket(){
  const A = G.cities||[];
  const rows = [...(G.naf||[]).map(n=>({...n, src:"Sirene"})), ...(G.bpe||[]).map(b=>({...b, src:"BPE "+(G.bpe_year||"")}))].map(r=>({...r, ...cityDens(r)}));
  const pick = ["93.13Z","F120","85.51Z","F114","F129","F127","F101"], sel = pick.map(c=>rows.find(r=>r.c===c)).filter(r=>r && r.avg!=null);
  gChart("gDens", {type:"bar", data:{labels:sel.map(r=>r.c==="93.13Z" ? "Salles de sport (Sirene)" : r.l),
      datasets:A.map((a,i)=>({label:a.n, _pal:i, backgroundColor:PAL[i], borderColor:PAL[i], borderRadius:3, data:sel.map(r=>r.dens[i])}))},
    options:barOpts(true, v=>d1(v)+" pour 10 000 hab.")});
  document.getElementById("gDth").innerHTML = `<tr><th>Activité ou équipement</th><th>Source</th><th class="n">Le Havre</th><th class="n">/10 000 hab.</th>
    ${A.slice(1).map(a=>`<th class="n">${esc(a.n)}</th>`).join("")}<th class="n">Indice</th><th class="n">Manque</th></tr>`;
  document.getElementById("gDtb").innerHTML = rows.map(r=>`<tr style="cursor:default"><td class="l">${esc(r.l)} <span class="muted">${esc(r.c)}</span></td><td class="muted">${esc(r.src)}</td>
    <td class="n">${fmt(r.n[0])}</td><td class="n">${d1(r.dens[0])}</td>${r.dens.slice(1).map(v=>`<td class="n muted">${d1(v)}</td>`).join("")}
    <td class="n ${r.idx==null?"":r.idx<80?"down":r.idx>125?"up":""}">${r.idx==null?"—":Math.round(r.idx)}</td><td class="n">${r.gap>0?fmt(r.gap):"—"}</td></tr>`).join("");
  drawGymFlow();
  // ages
  const lh = ageData("Le Havre"), oth = A.slice(1).map(a=>ageData(a.n)).filter(o=>o.ys.length);
  if (lh.ys.length){
    const shares = x => AGES.map(([k])=>share(x[k]||0, x._T));
    const avgO = AGES.map((_,i)=>{ const v = oth.map(o=>shares(o.last)[i]); return v.reduce((a,b)=>a+b,0)/(v.length||1); });
    document.getElementById("gAgeSub").textContent = `(part des habitants en ${lh.ys[lh.ys.length-1]}, INSEE)`;
    gChart("gAge", {type:"bar", data:{labels:AGES.map(a=>a[1]), datasets:[
        {label:"Le Havre", _pal:0, backgroundColor:PAL[0], borderColor:PAL[0], borderRadius:3, data:shares(lh.last)},
        {label:`Moyenne ${A.slice(1).map(a=>a.n).join(", ")}`, _pal:5, backgroundColor:PAL[5], borderColor:PAL[5], borderRadius:3, data:avgO}]},
      options:barOpts(false, v=>(Math.round(v*10)/10).toString().replace(".",",")+" %")});
    const ch = k => pct(share((lh.last[k]||0)-(lh.first[k]||0), lh.first[k]));
    document.getElementById("gAgeNote").textContent = `Au Havre entre ${lh.ys[0]} et ${lh.ys[lh.ys.length-1]} : population ${ch("_T")}, 25–39 ans ${ch("Y25T39")}, 40–54 ans ${ch("Y40T54")}, 65–79 ans ${ch("Y65T79")}, 80 ans et plus ${ch("Y_GE80")}.`;
  }
  // employers
  const E = G.employers;
  if (E){
    document.getElementById("gEmpK").innerHTML = Object.entries(E.bands).map(([k,v])=>`<div><b>${fmt(v)}</b><span>${k} salariés</span></div>`).join("");
    document.getElementById("gEmp").innerHTML = E.top.map(e=>`<tr style="cursor:default"><td class="l">${esc(e[0])}</td><td class="l" style="font-size:12px">${esc(e[1])}</td><td class="n">${esc(GSIZE[e[2]]||"—")}</td></tr>`).join("");
  }
  // jobs
  const T = G.terr||[];
  document.getElementById("gJth").innerHTML = `<tr><th rowspan="2">Métier</th><th colspan="6" style="text-align:center">${esc(T[0])} (zone France Travail)</th><th colspan="3" style="text-align:center">${esc(T[4])}</th><th colspan="4" style="text-align:center">Offres par demandeur</th></tr>
    <tr><th class="n">Offres 12 m</th><th class="n">Évol.</th><th class="n">Demandeurs</th><th class="n">Offres/dem.</th><th class="n">Embauches 12 m</th><th class="n">Tension</th>
    <th class="n">Offres 12 m</th><th class="n">Demandeurs</th><th class="n">Offres/dem.</th>${[1,2,3,5].map(i=>`<th class="n">${esc(T[i])}</th>`).join("")}</tr>`;
  document.getElementById("gJtb").innerHTML = G.jobs.map(j=>{
    const h = jobFig(j,0) || [], n = jobFig(j,4) || [];
    const g = h[0]!=null && h[1] ? share(h[0]-h[1], h[1]) : null;
    return `<tr style="cursor:default"><td class="l">${j.sector ? `<b>${esc(j.l)}</b>` : esc(j.l)} <span class="muted">${esc(j.c)}</span></td>
      <td class="n">${fmt(h[0])}</td><td class="n ${cls(g)}">${pct(g)}</td><td class="n">${fmt(h[2])}</td><td class="n">${r2(ratio(h))}</td><td class="n">${fmt(h[3])}</td><td class="n">${j.t ?? "—"}</td>
      <td class="n">${fmt(n[0])}</td><td class="n">${fmt(n[2])}</td><td class="n">${r2(ratio(n))}</td>${[1,2,3,5].map(i=>`<td class="n muted">${r2(ratio(jobFig(j,i)))}</td>`).join("")}</tr>`;
  }).join("");
}

// ---- client club: position, SEO / GEO / AEO audit, action plan ------------------------------
// On the public site the section is AES-encrypted (D.clubEnc) and decrypted here with the password.
let CLUB = D.club || null;
const ST = {ok:["✓","up","Bon"], warn:["!","warn","À améliorer"], bad:["✗","down","À corriger"], info:["i","muted","Pour info"]};
const stIcon = s => `<span class="st ${ST[s][1]}" title="${ST[s][2]}" aria-label="${ST[s][2]}">${ST[s][0]}</span>`;
const link = (u, t) => `<a href="${esc(u)}" target="_blank" rel="noopener" style="color:var(--accent)">${esc(t)}</a>`;

async function unlockClub(pwd){
  const b = D.clubEnc, dec = s => Uint8Array.from(atob(s), c=>c.charCodeAt(0));
  const base = await crypto.subtle.importKey("raw", new TextEncoder().encode(pwd), "PBKDF2", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({name:"PBKDF2", salt:dec(b.salt), iterations:b.it, hash:"SHA-256"},
    base, {name:"AES-GCM", length:256}, false, ["decrypt"]);
  const pt = await crypto.subtle.decrypt({name:"AES-GCM", iv:dec(b.iv)}, key, dec(b.ct));
  return JSON.parse(new TextDecoder().decode(pt));
}
function clubLogin(){
  const el = document.getElementById("g-club");
  el.innerHTML = `<div class="card" style="max-width:440px;margin:14px auto"><b>Focus LH Sport Club</b>
    <p class="muted" style="margin:6px 0 10px">Cette section est chiffrée. Saisissez le mot de passe pour l'afficher : elle est déchiffrée dans votre navigateur, rien n'est envoyé.</p>
    <form id="cForm" class="row" style="margin:0"><input type="password" id="cPwd" autocomplete="current-password" placeholder="Mot de passe" required style="flex:1" aria-label="Mot de passe">
    <button class="pin" type="submit">Ouvrir</button></form><p class="note" id="cMsg" role="status"></p></div>`;
  const go = async pwd => {
    try { CLUB = await unlockClub(pwd); try { sessionStorage.setItem("clubPwd", pwd); } catch(e) {}
      renderClub(); }
    catch(e) { document.getElementById("cMsg").textContent = "Mot de passe incorrect."; }
  };
  document.getElementById("cForm").onsubmit = e=>{ e.preventDefault(); go(document.getElementById("cPwd").value); };
  let saved = null; try { saved = sessionStorage.getItem("clubPwd"); } catch(e) {}
  if (saved) go(saved);
}

function seoChecks(a){
  if (!a || a.error || !a.home) return [];
  const h = a.home, r = a.robots || {}, L = h.local || {};
  const len = (s, lo, hi) => !!s && s.length>=lo && s.length<=hi;
  const alt = h.imgs ? 1 - h.img_noalt/h.imgs : 1;
  const lt = [].concat(h.local_type||[]).filter(Boolean), isLocal = lt.some(t=>t!=="Organization");
  const LK = {address:"adresse", geo:"GPS", openingHoursSpecification:"horaires", telephone:"téléphone", priceRange:"prix", sameAs:"réseaux"};
  const ps = a.pagespeed && a.pagespeed.scores;
  const c = [
    ["https","HTTPS", a.https_redirect && a.hsts ? "ok" : a.https_redirect ? "warn" : "bad", a.https_redirect ? (a.hsts ? "HTTPS forcé, HSTS actif" : "HTTPS forcé, sans HSTS") : "pas de redirection vers HTTPS"],
    ["title","Balise title", len(h.title,30,65) ? "ok" : h.title ? "warn" : "bad", h.title ? `${h.title.length} caractères : « ${h.title} »` : "absente"],
    ["desc","Meta description", len(h.desc,70,160) ? "ok" : h.desc ? "warn" : "bad", h.desc ? `${h.desc.length} caractères (idéal 70 à 160)` : "absente"],
    ["h1","Titre H1 unique", h.h1n===1 ? "ok" : "bad", h.h1n ? `${h.h1n} H1 : ${h.h1.join(" | ")}` : "aucun H1 sur la page"],
    ["mobile","Mobile et langue", h.viewport && h.lang ? "ok" : "warn", `viewport ${h.viewport?"oui":"non"}, langue « ${h.lang||"?"} »`],
    ["canon","URL canonique", h.canonical ? "ok" : "warn", h.canonical || "absente"],
    ["og","Aperçu réseaux sociaux", h.og && h.og_image ? "ok" : "warn", h.og ? (h.og_image ? "titre et image Open Graph" : "pas d'image Open Graph") : "pas d'Open Graph"],
    ["local","Fiche locale (schema.org)", isLocal && L.address && L.openingHoursSpecification ? "ok" : isLocal || L.address ? "warn" : "bad",
      lt.length ? `${lt.join(", ")} : ${Object.entries(LK).map(([k,l])=>(L[k]?"✓ ":"✗ ")+l).join(", ")}` : "aucune donnée d'établissement"],
    ["faq","FAQ balisée", h.faq ? "ok" : "warn", h.faq ? `${h.faq} questions (FAQPage)` : h.questions ? `${h.questions} intertitres en question, sans balisage FAQPage` : "aucune FAQ"],
    ["sitemap","Plan du site", a.sitemap_n ? "ok" : "bad", a.sitemap_n ? `${fmt(a.sitemap_n)} adresses` : "introuvable"],
    ["ai","Robots IA autorisés", !(r.blocked||[]).length && !r.all_blocked ? "ok" : "warn", (r.blocked||[]).length ? "bloqués : "+r.blocked.join(", ") : "aucun robot IA bloqué"+(r.exists ? "" : " (pas de robots.txt)")],
    ["llms","Fichier llms.txt", a.llms ? "ok" : "warn", a.llms ? "présent" : "absent : résumé du site destiné aux IA"],
    ["alt","Images décrites (alt)", alt>=.9 ? "ok" : alt>=.6 ? "warn" : "bad", `${h.imgs-h.img_noalt} sur ${h.imgs}`],
    ["img","Images WebP/AVIF", h.imgs && h.img_modern/h.imgs>=.5 ? "ok" : "warn", `${h.img_modern} sur ${h.imgs}`],
    ["weight","Poids de la page", h.kb<=150 && h.scripts<=20 ? "ok" : h.kb<=300 ? "warn" : "bad", `${h.kb} Ko de HTML, ${h.scripts} scripts, ${h.css} feuilles de style`],
    ["speed","Temps de réponse", h.secs<=0.8 ? "ok" : h.secs<=1.5 ? "warn" : "bad", `${String(h.secs).replace(".",",")} s pour le HTML`],
  ];
  if (ps) c.push(["ps","PageSpeed mobile", ps.performance>=90 ? "ok" : ps.performance>=50 ? "warn" : "bad", `performance ${ps.performance}, SEO ${ps.seo}, accessibilité ${ps.accessibility}, LCP ${a.pagespeed.lcp}`]);
  const kw = h.kw || {};
  c.push(["same","Liens vers les profils (sameAs)", L.sameAs ? "ok" : "warn", L.sameAs ? "le site relie ses profils (réseaux, annuaires) à l'établissement" : "aucun lien structuré vers les profils de l'établissement"]);
  c.push(["qs","Intertitres en forme de question", h.questions>=3 ? "ok" : h.questions ? "warn" : "bad", `${h.questions||0} sur la page d'accueil`]);
  c.push(["price","Prix affichés sur l'accueil", (kw["€"]||0)>0 ? "ok" : "warn", (kw["€"]||0)>0 ? `${kw["€"]} montant(s) en euros` : "aucun prix : les moteurs de réponse vont le chercher ailleurs"]);
  return c.map(([k,l,s,v])=>({k,l,s,v,cat:CHECK_CAT[k]||"SEO"}));
}
const CHECK_CAT = {ai:"GEO", llms:"GEO", local:"GEO", same:"GEO", faq:"AEO", qs:"AEO", price:"AEO"};
const seoScore = cs => cs.length ? Math.round(100*cs.reduce((t,c)=>t+(c.s==="ok"?1:c.s==="warn"?.5:0),0)/cs.length) : null;

function pageIssues(a){             // the client's own site, crawled page by page
  const all = a.pages || [], P = all.filter(p=>!p.error && p.status===200);
  const path = p => p.url.replace(/^https?:\/\/[^/]+/, "") || "/";
  const tax = P.filter(p=>/\/(secteur|domaine|categorie-produit|category|tag)\//.test(p.url));
  const content = P.filter(p=>!tax.includes(p));
  const starts = {};
  content.forEach(p=>{ if (p.desc && p.desc.length>40){ const k = p.desc.slice(0,45); (starts[k] = starts[k] || []).push(p); } });
  const dup = Object.values(starts).filter(v=>v.length>2).flat();
  const odd = P.filter(p=>/[\u{1D400}-\u{1D7FF}]/u.test(p.url));
  const rows = [
    ["Pages lues", P.length+1, "info", `accueil + ${P.length} pages du plan du site`],
    ["Erreurs ou pages introuvables", all.length-P.length, all.length-P.length ? "bad" : "ok", all.filter(p=>p.error || p.status!==200).map(path)],
    ["Pages de contenu sans meta description", content.filter(p=>!p.desc).length, content.some(p=>!p.desc) ? "warn" : "ok", content.filter(p=>!p.desc).map(path)],
    ["Descriptions qui commencent pareil", dup.length, dup.length ? "warn" : "ok", dup.map(path)],
    ["Pages sans H1 ou avec plusieurs H1", content.filter(p=>p.h1n!==1).length, content.some(p=>p.h1n!==1) ? "warn" : "ok", content.filter(p=>p.h1n!==1).map(p=>`${path(p)} (${p.h1n})`)],
    ["Pages d'étiquettes ou de catégories indexables", tax.length, tax.length>5 ? "warn" : "ok", tax.map(path)],
    ["Adresses en lettres décoratives", odd.length, odd.length ? "warn" : "ok", odd.map(path)],
    ["Pages lentes (plus de 1 s)", P.filter(p=>p.secs>1).length, P.some(p=>p.secs>1) ? "warn" : "ok", P.filter(p=>p.secs>1).map(path)],
  ];
  return rows;
}

function clubPosition(){        // where the client stands on each criterion of the ranking
  const S = gymScores().filter(r=>r.score!=null), me = S.find(r=>r.g.name===CLUB.club.gym);
  if (!me) return null;
  const rankOf = (arr, v, desc=true) => 1 + arr.filter(x=>desc ? x>v : x<v).length;
  const others = S.filter(r=>r!==me);
  const rows = PARTS.map(([k,l])=>{ const vals = others.map(r=>r.p[k]).filter(v=>v!=null), v = me.p[k];
    return {l, v, med: median(vals), rank: v==null ? null : rankOf(vals, v), n: vals.length+1}; });
  const sameCat = GYMS.filter(g=>g.cat===me.g.cat && g.month!=null && g!==me.g).map(g=>g.month);
  const rated = GYMS.filter(g=>g.reviews!=null);
  return {me, rank: rankOf(others.map(r=>r.score), me.score), n: S.length, rows,
    catMed: median(sameCat), revRank: rankOf(rated.filter(g=>g!==me.g).map(g=>g.reviews), me.g.reviews), nRated: rated.length};
}

function renderClub(){
  const el = document.getElementById("g-club");
  if (!CLUB){ if (D.clubEnc && !document.getElementById("cForm")) clubLogin(); return; }
  if (CLUB.ui && !window.crawlSection) (0, eval)(CLUB.ui);   // crawl UI code ships inside the encrypted section
  const K = CLUB.club, A = CLUB.audits || {}, mine = A[K.name] || {}, pos = clubPosition();
  const checks = seoChecks(mine);
  const engines = [...new Set((K.search||[]).map(s=>s.engine||"Moteur IA"))];
  const byEng = engines.map(e=>{ const t = (K.search||[]).filter(s=>(s.engine||"Moteur IA")===e); return {e, n:t.length, c:t.filter(s=>s.cited).length}; });
  const g = pos ? pos.me.g : GYMS.find(x=>x.name===K.gym) || {};
  const comp = Object.entries(A).map(([n,a])=>({n, cs:seoChecks(a), a})).filter(x=>x.cs.length).map(x=>({...x, sc:seoScore(x.cs)})).sort((a,b)=>b.sc-a.sc);
  const ul = xs => `<ul class="pl">${xs.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>`;
  const AREA = {SEO:"--c1", GEO:"--c3", AEO:"--c2", HYROX:"--c4", Offre:"--c6", Avis:"--c5"};
  const row = (st, title, text) => `<div class="chk">${stIcon(st)}<div><b style="font-size:13px">${title}</b><div class="muted" style="font-size:12px">${text}</div></div></div>`;
  const of = p => checks.filter(c=>c.cat===p), man = p => (K.manual_seo||[]).filter(m=>(m[3]||"SEO")===p);
  const score = items => { const v = items.filter(x=>x!=="info"); return v.length ? Math.round(100*v.reduce((t,x)=>t+(x==="ok"?1:x==="warn"?.5:0),0)/v.length) : null; };
  const tests = K.search || [];
  const PS = {SEO: score([...of("SEO").map(c=>c.s), ...(mine.pages ? pageIssues(mine).map(r=>r[2]) : []), ...man("SEO").map(m=>m[0])]),
              GEO: score([...of("GEO").map(c=>c.s), ...man("GEO").map(m=>m[0]), ...(K.presence||[]).map(p=>p.status)]),
              AEO: score([...of("AEO").map(c=>c.s), ...man("AEO").map(m=>m[0]), ...tests.map(t=>t.cited ? "ok" : "bad")])};
  const compTable = (keys, heads) => {
    const rows = comp.map(x=>{ const by = Object.fromEntries(x.cs.map(c=>[c.k,c])); return {...x, by, sc: score(keys.map(k=>by[k] ? by[k].s : "info"))}; }).sort((a,b)=>(b.sc??-1)-(a.sc??-1));
    return `<div class="tw" style="margin-top:8px"><table class="plain"><thead><tr><th>Site</th><th class="n">Score</th>${heads.map(h=>`<th style="text-align:center">${h}</th>`).join("")}</tr></thead><tbody>
      ${rows.map(x=>`<tr class="${x.n===K.name?"me":""}"><td>${esc(x.n)}</td><td class="n"><b>${x.sc ?? "—"}</b></td>${keys.map(k=>`<td style="text-align:center" title="${esc(x.by[k] ? x.by[k].v : "")}">${x.by[k] ? stIcon(x.by[k].s) : "—"}</td>`).join("")}</tr>`).join("")}
      </tbody></table></div><p class="note">✓ bon · ! à améliorer · ✗ à corriger. Survolez une case pour le détail. Les chaînes partagent un site national : leur score reflète surtout le travail de leur siège.</p>`; };
  const intro = t => `<p class="muted" style="margin:0 0 10px">${t}</p>`;
  const aiLink = q => `https://www.google.fr/search?q=${encodeURIComponent(q)}&hl=fr&gl=fr&udm=50`;
  const panes = {
   SEO: `${intro("<b>SEO</b> (référencement naturel) : être bien classé dans les résultats classiques de Google. Technique du site, contenus, pages à corriger.")}
    <div class="two" style="margin-top:0">
     <div class="card"><b>Page d'accueil</b>${of("SEO").map(c=>row(c.s, esc(c.l), esc(c.v))).join("")}
      ${mine.pagespeed ? "" : `<p class="note">Vitesse réelle sur mobile (Core Web Vitals) : ajoutez une clé gratuite PAGESPEED_API_KEY pour la mesurer à chaque mise à jour.</p>`}</div>
     <div class="card"><b>Tout le site</b> <span class="muted">(pages du plan du site)</span>
      ${mine.pages ? pageIssues(mine).map(r=>`<div class="chk">${stIcon(r[2])}<div style="flex:1"><b style="font-size:13px">${esc(r[0])} : ${r[1]}</b>
        ${Array.isArray(r[3]) ? (r[3].length ? `<details><summary class="muted" style="font-size:12px;cursor:pointer">voir les pages</summary><div class="muted" style="font-size:12px;word-break:break-all">${r[3].map(esc).join("<br>")}</div></details>` : "")
          : `<div class="muted" style="font-size:12px">${esc(r[3])}</div>`}</div></div>`).join("") : '<p class="muted">Lancez <code>python site_audit.py</code>.</p>'}
      <b style="display:block;margin-top:12px">Constats relevés à la main</b>${man("SEO").map(m=>row(m[0], esc(m[1]), esc(m[2]))).join("")}</div>
    </div>
    <div class="card" style="margin-top:14px"><b>Comparaison avec les sites des concurrents</b> <span class="muted">(page d'accueil, ou page du club pour les chaînes)</span>
     ${compTable(["title","desc","h1","canon","alt","img","weight","speed"], ["Title","Description","H1","Canonique","Alt","WebP","Poids","Vitesse"])}</div>`,
   GEO: `${intro("<b>GEO</b> (Generative Engine Optimization) : être connu et cité par les IA génératives (ChatGPT, Gemini, Perplexity, Copilot, Google Mode IA). Elles s'appuient sur des sources concordantes : fiche Google, annuaires, presse, données structurées.")}
    <div class="kp">${byEng.map(x=>`<div><b class="${x.c?"up":"down"}">${x.c} / ${x.n}</b><span>réponses qui citent le club — ${esc(x.e)}</span></div>`).join("")}</div>
    <div class="two" style="margin-top:0">
     <div class="card"><b>Le site est-il lisible par les IA ?</b>${of("GEO").map(c=>row(c.s, esc(c.l), esc(c.v))).join("")}
      <b style="display:block;margin-top:12px">Constats relevés à la main</b>${man("GEO").map(m=>row(m[0], esc(m[1]), esc(m[2]))).join("")}</div>
     <div class="card"><b>Cohérence des informations en ligne</b> <span class="muted">(les IA croisent ces sources)</span>
      ${(K.presence||[]).map(p=>row(p.status, link(p.url, p.src), esc(p.note))).join("")}</div>
    </div>
    <div class="card" style="margin-top:14px"><b>Comparaison avec les concurrents</b>${compTable(["ai","llms","local","same"], ["Robots IA","llms.txt","Fiche locale","Liens profils"])}</div>`,
   AEO: `${intro("<b>AEO</b> (Answer Engine Optimization) : être <i>la</i> réponse aux questions précises (« combien coûte… », « où s'entraîner pour HYROX… ») dans les réponses IA de Google, les extraits en vedette et la recherche vocale. Il faut des réponses courtes, factuelles et balisées sur le site.")}
    <div class="two" style="margin-top:0">
     <div class="card"><b>Le site répond-il aux questions ?</b>${of("AEO").map(c=>row(c.s, esc(c.l), esc(c.v))).join("")}
      <b style="display:block;margin-top:12px">Constats relevés à la main</b>${man("AEO").map(m=>row(m[0], esc(m[1]), esc(m[2]))).join("")}</div>
     <div class="card"><b>Comparaison avec les concurrents</b>${compTable(["faq","qs","price"], ["FAQ balisée","Questions","Prix"])}</div>
    </div>
    <div class="card" style="margin-top:14px"><b>Questions testées</b> <span class="muted">— « Tester » ouvre Google Mode IA en français, localisé en France (et selon votre position)</span>
     <div class="tw" style="margin-top:8px"><table class="plain"><thead><tr><th>Question</th><th>Moteur</th><th>Cité</th><th>Réponse</th><th>Sources renvoyées</th><th></th></tr></thead><tbody>
      ${tests.map(t=>`<tr><td class="l">« ${esc(t.q)} »<div class="muted" style="font-size:11px">${esc(fdate(t.date))}</div></td><td class="l" style="font-size:12px;min-width:110px">${esc(t.engine||"Moteur IA")}</td><td>${stIcon(t.cited ? "ok" : "bad")}</td>
        <td class="l" style="font-size:12px">${esc(t.who)}</td><td class="l" style="font-size:12px;min-width:200px">${esc(t.links||"")}</td><td>${link(aiLink(t.q), "Tester")}</td></tr>`).join("")}</tbody></table></div>
     <p class="note">${esc(K.search_note)}</p></div>`
  };
  el.innerHTML = `
  <div class="card" style="margin-top:14px"><div class="dh"><div><h2 style="margin:0 0 4px;font-size:18px">${esc(K.name)} — diagnostic</h2>
    <p class="muted" style="margin:0">Relevé du ${esc(K.checked.split("-").reverse().join("/"))} · audit technique du ${esc((mine.checked||"").split("-").reverse().join("/"))} · ${link(K.site, K.site.replace(/^https?:\/\//,"").replace(/\/$/,""))}</p></div></div>
   <p style="margin:10px 0 0">${esc(K.summary)}</p>
   <div class="kp">
    ${pos ? `<div><b>${pos.rank}<span style="font-size:13px;display:inline"> / ${pos.n}</span></b><span>au classement général (score ${Math.round(pos.me.score)}/100)</span></div>` : ""}
    <div><b>${g.rating!=null ? g.rating.toFixed(1).replace(".",",")+" ★" : "—"}</b><span>${fmt(g.reviews)} avis Google${pos ? `, ${pos.revRank}ᵉ en nombre d'avis sur ${pos.nRated}` : ""}</span></div>
    <div><b>${g.month!=null ? Math.round(g.month)+" €" : "—"}</b><span>prix d'entrée par mois (médiane du type : ${pos && pos.catMed!=null ? Math.round(pos.catMed)+" €" : "—"})</span></div>
    <div><b style="font-size:15px">${["SEO","GEO","AEO"].map(p=>`<span class="${PS[p]<60?"down":""}" style="display:block">${PS[p] ?? "—"} % <span class="muted" style="font-size:11px;font-weight:400">${p}</span></span>`).join("")}</b><span>scores de visibilité (détail plus bas)</span></div>
    <div><b style="font-size:15px">${byEng.map(x=>`<span class="${x.c ? "up" : "down"}" style="display:block">${x.c} / ${x.n} <span class="muted" style="font-size:11px;font-weight:400">${esc(x.e.replace(/ \(.*\)/,""))}</span></span>`).join("")}</b>
     <span>réponses IA testées qui citent le club</span></div>
   </div></div>

  <div class="two">
   <div class="card"><b class="up">Ce que le club fait mieux que les concurrents</b>${ul(K.better||[])}</div>
   <div class="card"><b class="down">Ce que le club fait moins bien</b>${ul(K.worse||[])}</div>
  </div>

  <div class="two">
   <div class="card"><b>Position sur chaque critère du classement</b> <span class="muted">(points sur 100)</span>
    ${pos ? `<div class="tw" style="margin-top:8px"><table class="plain"><thead><tr><th>Critère</th><th class="n">Le club</th><th class="n">Médiane des autres</th><th class="n">Rang</th></tr></thead><tbody>
     ${pos.rows.map(r=>`<tr><td>${esc(r.l)}</td><td class="n"><b>${r.v==null ? "—" : Math.round(r.v)}</b></td><td class="n">${r.med==null ? "—" : Math.round(r.med)}</td>
       <td class="n ${r.rank!=null && r.rank<=3 ? "up" : r.rank!=null && r.rank>r.n/2 ? "down" : ""}">${r.rank==null ? "—" : r.rank+" / "+r.n}</td></tr>`).join("")}
     </tbody></table></div><p class="note">Même calcul que l'onglet Classement, avec les poids choisis.</p>` : `<p class="muted">Club absent de gyms.json.</p>`}</div>
   <div class="card"><b>Prix et réputation</b> <span class="muted">(salles de fitness et low cost ; taille = nombre d'avis)</span><div class="cw"><canvas id="cBub"></canvas></div></div>
  </div>

  <div class="card" style="margin-top:14px"><b>HYROX</b>${ul(K.hyrox.facts||[])}
   <div class="two" style="margin-top:4px">
    <div><b style="font-size:13px">Ce que donne l'affiliation</b><p style="margin:6px 0">${esc(K.hyrox.affiliation)}</p>
     <b style="font-size:13px">Concurrence locale</b><p style="margin:6px 0">${esc(K.hyrox.local)}</p></div>
    <div><b style="font-size:13px">Prochaines courses en France</b><div class="tw" style="margin-top:6px"><table class="plain"><tbody>
     ${(K.hyrox.races||[]).map(r=>`<tr><td>${link(r[2], r[0])}</td><td>${esc(r[1])}</td></tr>`).join("")}</tbody></table></div>
     <p class="note">Une course proche dans le temps = un objectif à proposer aux adhérents (programme de préparation, déplacement en groupe).</p></div>
   </div></div>

  <div class="two">
   <div class="card"><b>Fiche d'identité</b><div class="tw" style="margin-top:8px"><table class="plain"><tbody>
    ${(K.identity||[]).map(r=>`<tr><td style="vertical-align:top"><b>${esc(r[0])}</b></td><td class="l">${esc(r[1])} ${r[2] ? link(r[2], "source") : ""}</td></tr>`).join("")}</tbody></table></div></div>
   <div class="card"><b>Offre</b>${ul(K.offer||[])}
    <b>Ce que disent les avis</b><div class="two" style="margin-top:4px;gap:10px">
     <div><span class="up" style="font-size:13px">Points forts</span>${ul(K.reviews.pos||[])}</div><div><span class="down" style="font-size:13px">Points de vigilance</span>${ul(K.reviews.neg||[])}</div></div></div>
  </div>

  <h3>Visibilité en ligne</h3>
  <div class="seg" id="cPane" role="group" aria-label="Volet" style="margin-bottom:10px">${["SEO","GEO","AEO"].map(p=>`<button data-p="${p}" aria-pressed="${p===cPane}">${p} · ${PS[p] ?? "—"} %</button>`).join("")}</div>
  ${["SEO","GEO","AEO"].map(p=>`<div class="cpane" data-p="${p}" ${p===cPane ? "" : "hidden"}>${panes[p]}</div>`).join("")}

  ${crawlSection(K, CLUB.crawl, mine)}

  <h3>Plan d'action</h3>
  <div class="tw"><table class="plain"><thead><tr><th class="n">Priorité</th><th>Domaine</th><th>Action</th><th>Pourquoi</th><th>Comment</th><th>Effort</th></tr></thead><tbody>
   ${(K.actions||[]).sort((a,b)=>a.p-b.p).map(x=>`<tr><td class="n"><b>${x.p}</b></td><td><span class="tag" style="background:color-mix(in srgb, var(${AREA[x.area]||"--c6"}) 22%, transparent)">${esc(x.area)}</span></td>
     <td class="l" style="min-width:180px"><b>${esc(x.t)}</b></td><td class="l" style="font-size:12px">${esc(x.why)}</td><td class="l" style="font-size:12px;min-width:280px">${esc(x.how)}</td><td class="l" style="font-size:12px;min-width:90px">${esc(x.effort)}</td></tr>`).join("")}
  </tbody></table></div>
  <p class="note">Priorité 1 = ce mois-ci, 2 = ce trimestre, 3 = dans les six mois. L'audit technique se met à jour à chaque collecte hebdomadaire ; le reste de la section (fichier club.json) est un relevé daté à refaire chaque trimestre.</p>

  ${(K.methods||[]).length ? `<div class="card" style="margin-top:14px"><b>Méthode : comment chaque test a été fait</b>
   ${K.methods.map(m=>`<details style="margin-top:8px"><summary style="cursor:pointer"><b style="font-size:13px">${esc(m.t)}</b></summary>${ul(m.items)}</details>`).join("")}</div>` : ""}`;
  document.getElementById("cPane").onclick = e=>{ const btn = e.target.closest("button"); if (!btn) return; cPane = btn.dataset.p;
    document.querySelectorAll("#cPane button").forEach(x=>x.setAttribute("aria-pressed", x===btn));
    document.querySelectorAll(".cpane").forEach(d=>d.hidden = d.dataset.p!==cPane); };
  bindCrawl();
  drawClubChart();
}
let cPane = "SEO";
function drawClubChart(){
  if (!CLUB || !document.getElementById("cBub")) return;
  const me = CLUB.club.gym, pts = GYMS.filter(g=>(g.cat==="Fitness / musculation" || g.cat==="Low cost") && g.month!=null && g.rating!=null);
  const pt = g => ({x:Math.round(g.month), y:g.rating, r:4+Math.sqrt(g.reviews ?? 10)/1.6, n:g.name.replace(/ \(.*\)/,"")});
  gChart("cBub", {type:"bubble", data:{datasets:[
      {label:"Concurrents", _pal:5, backgroundColor:PAL[5], borderColor:PAL[5], data:pts.filter(g=>g.name!==me).map(pt)},
      {label:me, _pal:0, backgroundColor:PAL[0], borderColor:PAL[0], data:pts.filter(g=>g.name===me).map(pt)}]},
    options:{responsive:true, maintainAspectRatio:false,
      plugins:{legend:{labels:{color:C("--muted"), boxWidth:12}}, tooltip:{...tipColors(), callbacks:{label:c=>`${c.raw.n} : ${c.raw.x} €/mois, ${String(c.raw.y).replace(".",",")} ★`}}},
      scales:{x:{title:{display:true, text:"prix d'entrée (€/mois)", color:C("--muted")}, ticks:{color:C("--muted")}, grid:{color:C("--line")}},
              y:{title:{display:true, text:"note Google", color:C("--muted")}, ticks:{color:C("--muted")}, grid:{color:C("--line")}, suggestedMin:3.8, suggestedMax:5}}}});
}

// ---- light / dark switch ------------------------------------------------------------
function recolorCharts(){
  PAL = readPal();
  const muted = C("--muted"), line = C("--line");
  for (const ch of Object.values(Chart.instances)) {
    for (const ds of ch.data.datasets) { ds.borderColor = ds.backgroundColor = PAL[ds._pal ?? 0]; }
    const o = ch.options;
    o.plugins.legend.labels.color = muted;
    Object.assign(o.plugins.tooltip, tipColors());
    for (const sc of Object.values(o.scales)) {
      sc.ticks.color = muted;
      if (sc.title) sc.title.color = muted;
      if (sc.grid && sc.grid.display !== false) sc.grid.color = line;
    }
    ch.update("none");
  }
}
function setTheme(mode){
  if (mode === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = mode;
  try { localStorage.setItem("theme", mode); } catch(e) {}
  document.querySelectorAll("#themeSeg button").forEach(b=>b.setAttribute("aria-pressed", b.dataset.mode===mode));
  recolorCharts();
}
document.getElementById("themeSeg").onclick = e=>{ const b=e.target.closest("button"); if (b) setTheme(b.dataset.mode); };
document.querySelector(`#themeSeg button[data-mode="${document.documentElement.dataset.theme || "auto"}"]`).setAttribute("aria-pressed", "true");
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", ()=>{ if (!document.documentElement.dataset.theme) recolorCharts(); });

// Open the tech theme by default; #jobs, #sectors or #focus:<key> opens a tab directly
document.querySelector('.theme[data-i="0"]').click();
const hashTab = document.querySelector(`#tabs button[data-t="${decodeURIComponent(location.hash.slice(1))}"]`);
if (hashTab) hashTab.click();
tabs.addEventListener("click", e=>{ const b=e.target.closest("button"); if (b) history.replaceState(null, "", "#"+b.dataset.t); });
</script></body></html>
"""

if __name__ == "__main__":
    main()
