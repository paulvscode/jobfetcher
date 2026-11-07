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
    }
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
@media (max-width:600px){.spark{width:70px}td.l{min-width:160px}}
</style></head><body>
<header><div><h1>Tendances de l'emploi — Le Havre</h1><div class="sub" id="sub"></div></div>
<div class="seg" id="themeSeg" role="group" aria-label="Thème d'affichage">
<button data-mode="auto" aria-pressed="false">Auto</button><button data-mode="light" aria-pressed="false">Clair</button><button data-mode="dark" aria-pressed="false">Sombre</button></div></header>
<main>
<nav id="tabs"><button data-t="themes" class="on">Thèmes</button><button data-t="jobs">Métiers</button><button data-t="sectors">Secteurs d'activité</button><button data-t="local">Offre locale</button></nav>

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
  const isFocus = view.startsWith("focus:"), isLocal = view==="local";
  document.getElementById("v-themes").hidden = view!=="themes";
  document.getElementById("v-list").hidden = view==="themes" || isFocus || isLocal;
  document.getElementById("v-focus").hidden = !isFocus;
  document.getElementById("v-local").hidden = !isLocal;
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
