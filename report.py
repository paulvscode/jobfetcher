"""Build a self-contained HTML report from jobs.db.

Usage:  python report.py   →  writes report.html (open it in your browser)
"""
import html
import os
import sqlite3
from collections import defaultdict

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "jobs.db"))
OUT_PATH = os.getenv("REPORT_PATH", os.path.join(os.path.dirname(__file__), "report.html"))
TERRITORY = os.getenv("TERRITORY", "CLPE76002")
MIN_OFFERS = 30      # minimum offers over 12 months for a job to be ranked
MIN_JOBSEEKERS = 100  # minimum jobseekers for the "labour pool" ranking

GRANDS_DOMAINES = {
    "A": "Agriculture, pêche, espaces verts, animaux",
    "B": "Arts et façonnage d'ouvrages d'art",
    "C": "Banque, assurance, immobilier",
    "D": "Commerce, vente, grande distribution",
    "E": "Communication, média, multimédia",
    "F": "Construction, bâtiment, travaux publics",
    "G": "Hôtellerie-restauration, tourisme, loisirs",
    "H": "Industrie",
    "I": "Installation et maintenance",
    "J": "Santé",
    "K": "Services à la personne et à la collectivité",
    "L": "Spectacle",
    "M": "Support à l'entreprise",
    "N": "Transport et logistique",
}

# Tension factors (France Travail / DARES), with the business lead each one suggests.
# Order = priority when two factors have the same level.
FACTORS = {
    "MAIN_OEUVRE": ("Manque de main-d'œuvre",
                    "Former ou recruter : organisme de formation, cabinet de recrutement, intérim spécialisé"),
    "SPECIF_FORM_EMPL": ("Formation très spécifique",
                         "Organisme de formation ou d'apprentissage dédié à ce métier"),
    "COND_TRAVAIL": ("Conditions de travail contraignantes",
                     "Équipement, automatisation, ou sous-traitance de ces tâches"),
    "MISMATCH_GEO": ("Candidats éloignés des postes",
                     "Mobilité : transport, logement, recrutement sur un bassin plus large"),
    "INT_EMB": ("Très nombreuses embauches",
                "Externaliser le recrutement : intérim, RH, groupement d'employeurs"),
    "DUR_EMPL": ("Emplois peu durables (contrats courts)",
                 "Groupement d'employeurs, CDI intérimaire, services de fidélisation"),
    "ATTR_SALARIALE": ("Attractivité salariale", "Tension liée au niveau des salaires proposés"),
}
LEVELS = {1: "très faible", 2: "faible", 3: "moyenne", 4: "forte", 5: "très forte"}


# --- formatting helpers ----------------------------------------------------
def esc(x):
    return html.escape(str(x if x is not None else "—"))


def num(x):
    if x is None:
        return "—"
    if isinstance(x, float) and not x.is_integer():
        return f"{x:.2f}".replace(".", ",")
    return f"{x:,.0f}".replace(",", " ")


def growth(now, before):
    if not now or not before:
        return None
    return round(100 * (now - before) / before)


def sgn(p):
    return "—" if p is None else f"{p:+d} %"


def ratio(a, b):
    return round(a / b, 2) if a is not None and b else None


def prev_year(period):
    """'2026T2' -> '2025T2', '2025' -> '2024'."""
    return str(int(period[:4]) - 1) + period[4:]


def last_quarters(period, n):
    y, q = int(period[:4]), int(period[5])
    out = []
    for _ in range(n):
        out.append(f"{y}T{q}")
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def bar(value, max_value):
    w = 100 * value / max_value if max_value and value else 0
    return f'<div class="bar"><span style="width:{max(w, 0):.1f}%"></span></div>'


def table(headers, rows, bar_col=None, num_cols=(), raw_cols=()):
    """rows: list of lists. bar_col: index of the numeric column drawn as a bar."""
    max_v = max((r[bar_col] or 0 for r in rows), default=0) if bar_col is not None else 0
    out = ["<div class='tw'><table><thead><tr>"]
    for i, h in enumerate(headers):
        cls = " class='n'" if i in num_cols else ""
        out.append(f"<th{cls}>{esc(h)}</th>")
    if bar_col is not None:
        out.append("<th class='bc'></th>")
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>")
        for i, c in enumerate(r):
            cls = " class='n'" if i in num_cols else ""
            if i in raw_cols:
                val = c
            elif isinstance(c, (int, float)) and not isinstance(c, bool) and i in num_cols:
                val = esc(num(c))
            else:
                val = esc(c)
            out.append(f"<td{cls}>{val}</td>")
        if bar_col is not None:
            out.append(f"<td class='bc'>{bar(r[bar_col], max_v)}</td>")
        out.append("</tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def chart(points, label):
    """Simple line chart. points: [(period, value)] in chronological order."""
    points = [(p, v) for p, v in points if v is not None]
    if len(points) < 2:
        return "<p class='muted'>Pas assez de données.</p>"
    w, h, pad = 640, 150, 34
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    step = (w - pad) / (len(points) - 1)
    xy = [(pad + i * step, h - 22 - (v - lo) / span * (h - 40)) for i, (_, v) in enumerate(points)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(xy))
    dots = "".join(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='2.5'><title>{esc(p)} : {num(v)}</title></circle>"
                   for (x, y), (p, v) in zip(xy, points))
    return (
        f"<figure class='chart'><figcaption>{esc(label)}</figcaption>"
        f"<svg viewBox='0 0 {w} {h}' role='img' aria-label='{esc(label)}'>"
        f"<path d='{path}' fill='none' stroke='var(--accent)' stroke-width='2'/>"
        f"<g fill='var(--accent)'>{dots}</g>"
        f"<text x='0' y='{xy[vals.index(hi)][1] + 4:.0f}' class='axis'>{num(hi)}</text>"
        f"<text x='0' y='{xy[vals.index(lo)][1] + 4:.0f}' class='axis'>{num(lo)}</text>"
        f"<text x='{pad}' y='{h - 4}' class='axis'>{esc(points[0][0])}</text>"
        f"<text x='{w}' y='{h - 4}' class='axis' text-anchor='end'>{esc(points[-1][0])}</text>"
        f"</svg></figure>")


# --- report ------------------------------------------------------------------
def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit("No jobs.db yet — run collect.py first.")
    conn = sqlite3.connect(DB_PATH)
    runs = conn.execute("SELECT * FROM runs ORDER BY run_at").fetchall()
    if not runs:
        raise SystemExit("No collection run recorded yet — run collect.py first.")
    last_run = runs[-1][0][:10]

    labels = {(t, c): l for t, c, l in conn.execute("SELECT type, code, label FROM activities")}
    terr_labels = dict(conn.execute("SELECT code, label FROM territories"))

    # S[(indicator, activity_type, activity, territory, nomenclature)][period] = value
    S = defaultdict(dict)
    for ind, at, act, ter, nom, per, val in conn.execute(
            "SELECT indicator, activity_type, activity, territory, nomenclature, period, value FROM stats"):
        S[(ind, at, act, ter, nom)][per] = val

    def get(ind, at, act, nom, per, ter=TERRITORY):
        return S.get((ind, at, act, ter, nom), {}).get(per)

    def latest_period(ind, nom):
        d = S.get((ind, "CUMUL", "CUMUL", TERRITORY, nom), {})
        return max(d) if d else None

    P_OFF = latest_period("offres", "TOFF-CUMUL12MOIS")
    P_DE = latest_period("demandeurs", "ABC")
    P_EMB = latest_period("embauches", "TOUT-TOUTE")
    if not P_OFF:
        raise SystemExit("No offer statistics in jobs.db — run collect.py again.")

    def hires12(at, act, ter=TERRITORY):
        vals = [get("embauches", at, act, "TOUT-TOUTE", p, ter) for p in last_quarters(P_EMB, 4)] if P_EMB else []
        vals = [v for v in vals if v is not None]
        return sum(vals) if vals else None

    # Tension: latest year per job
    tension = defaultdict(dict)
    years = [r[0] for r in conn.execute("SELECT DISTINCT year FROM tension ORDER BY year")]
    T_YEAR = years[-1] if years else None
    for rome, year, factor, level, score in conn.execute("SELECT * FROM tension WHERE year=?", (T_YEAR,)):
        tension[rome][factor] = (level, score)

    # --- per-job metrics ---------------------------------------------------
    jobs = []
    romes = {k[2] for k in S if k[0] == "offres" and k[1] == "ROME" and k[3] == TERRITORY}
    for r in romes:
        off12 = get("offres", "ROME", r, "TOFF-CUMUL12MOIS", P_OFF)
        off12_prev = get("offres", "ROME", r, "TOFF-CUMUL12MOIS", prev_year(P_OFF))
        de = get("demandeurs", "ROME", r, "ABC", P_DE) if P_DE else None
        t = tension.get(r, {})
        factors = [(f, t[f][0]) for f in FACTORS if f in t and t[f][0]]
        main_f = max(factors, key=lambda x: (x[1], -list(FACTORS).index(x[0])), default=None)
        jobs.append({
            "rome": r, "label": f"{labels.get(('ROME', r), r)} ({r})", "dom": r[0],
            "off12": off12, "off12_prev": off12_prev, "growth": growth(off12, off12_prev),
            "de": de, "ratio": ratio(off12, de), "hires12": hires12("ROME", r),
            "t_level": t.get("PERSPECTIVE", (None, None))[0],
            "t_score": t.get("PERSPECTIVE", (None, None))[1],
            "main_factor": main_f,
        })
    ranked = [j for j in jobs if (j["off12"] or 0) >= MIN_OFFERS]

    # --- headline figures --------------------------------------------------
    tot_off = get("offres", "CUMUL", "CUMUL", "TOFF-CUMUL12MOIS", P_OFF)
    tot_off_prev = get("offres", "CUMUL", "CUMUL", "TOFF-CUMUL12MOIS", prev_year(P_OFF))
    tot_de = get("demandeurs", "CUMUL", "CUMUL", "ABC", P_DE) if P_DE else None
    tot_de_prev = get("demandeurs", "CUMUL", "CUMUL", "ABC", prev_year(P_DE)) if P_DE else None
    tot_hires = hires12("CUMUL", "CUMUL")
    tense = sorted((j for j in ranked if (j["t_level"] or 0) >= 4),
                   key=lambda j: (-(j["t_score"] or 0), -(j["off12"] or 0)))

    # --- benchmarks -----------------------------------------------------------
    terrs = []
    for (ind, at, act, ter, nom) in S:
        if ind == "offres" and at == "CUMUL" and nom == "TOFF-CUMUL12MOIS" and ter not in terrs:
            terrs.append(ter)
    terrs.sort(key=lambda t: (t != TERRITORY, -(get("offres", "CUMUL", "CUMUL", "TOFF-CUMUL12MOIS", P_OFF, t) or 0)))
    bench_rows = []
    for t in terrs:
        o = get("offres", "CUMUL", "CUMUL", "TOFF-CUMUL12MOIS", P_OFF, t)
        o_prev = get("offres", "CUMUL", "CUMUL", "TOFF-CUMUL12MOIS", prev_year(P_OFF), t)
        d = get("demandeurs", "CUMUL", "CUMUL", "ABC", P_DE, t) if P_DE else None
        name = terr_labels.get(t, t)
        bench_rows.append([("➜ " if t == TERRITORY else "") + name, o, d, ratio(o, d), sgn(growth(o, o_prev))])
    my_ratio = ratio(tot_off, tot_de)

    # --- trends -------------------------------------------------------------
    q_off = sorted(S.get(("offres", "CUMUL", "CUMUL", TERRITORY, "TOFF"), {}).items())
    q_de = sorted(S.get(("demandeurs", "CUMUL", "CUMUL", TERRITORY, "ABC"), {}).items())

    # --- tables ---------------------------------------------------------------
    def factor_txt(j):
        if not j["main_factor"]:
            return "—", "—"
        f, lvl = j["main_factor"]
        return f"{FACTORS[f][0]} ({LEVELS.get(lvl, lvl)})", FACTORS[f][1]

    tense_rows = []
    for j in tense[:40]:
        ftxt, lead = factor_txt(j)
        tense_rows.append([j["label"], j["off12"], j["de"], j["ratio"], j["t_level"], ftxt, lead])

    growing = sorted((j for j in ranked if j["growth"] is not None and (j["off12_prev"] or 0) >= MIN_OFFERS),
                     key=lambda j: -j["growth"])
    grow_rows = [[j["label"], j["off12_prev"], j["off12"], sgn(j["growth"]), j["t_level"] or "—"]
                 for j in growing[:25] if j["growth"] > 0]
    decline_rows = [[j["label"], j["off12_prev"], j["off12"], sgn(j["growth"])]
                    for j in sorted(growing, key=lambda j: j["growth"])[:15] if j["growth"] < 0]

    pools = sorted((j for j in jobs if (j["de"] or 0) >= MIN_JOBSEEKERS and (j["ratio"] or 0) < 0.5),
                   key=lambda j: -j["de"])
    pool_rows = [[j["label"], j["de"], j["off12"] or 0, j["ratio"] or 0, j["hires12"]] for j in pools[:30]]

    top_rows = [[j["label"], j["off12"], sgn(j["growth"]), j["de"], j["ratio"], j["t_level"] or "—"]
                for j in sorted(ranked, key=lambda j: -(j["off12"] or 0))[:40]]

    dom = defaultdict(lambda: [0, 0, 0])
    for j in jobs:
        d = dom[j["dom"]]
        d[0] += j["off12"] or 0
        d[1] += j["off12_prev"] or 0
        d[2] += j["de"] or 0
    dom_rows = sorted(([GRANDS_DOMAINES.get(k, k), v[0], sgn(growth(v[0], v[1])), v[2], ratio(v[0], v[2])]
                       for k, v in dom.items()), key=lambda r: -r[1])

    nafs = {k[2] for k in S if k[0] == "offres" and k[1] == "NAF88" and k[3] == TERRITORY}
    naf_rows = []
    for n in nafs:
        o = get("offres", "NAF88", n, "TOFF-CUMUL12MOIS", P_OFF)
        if not o:
            continue
        o_prev = get("offres", "NAF88", n, "TOFF-CUMUL12MOIS", prev_year(P_OFF))
        naf_rows.append([f"{labels.get(('NAF88', n), n)} ({n})", o, sgn(growth(o, o_prev)), hires12("NAF88", n)])
    naf_rows.sort(key=lambda r: -r[1])

    # Profile of offers (breakdown of the area total)
    prof = defaultdict(list)
    for ctype, label, nombre, pct in conn.execute(
            "SELECT ctype, label, nombre, pct FROM caract WHERE indicator='offres' AND activity='CUMUL' "
            "AND territory=? AND nomenclature='TOFF-CUMUL12MOIS'", (TERRITORY,)):
        prof[ctype].append([label, nombre, f"{pct:.0f} %" if pct is not None else "—"])

    # Companies (La Bonne Boîte) for the most tense jobs
    comp = defaultdict(list)
    for rome, name, naf_label, city, hmin, hmax, pot, high in conn.execute(
            "SELECT rome, name, naf_label, city, headcount_min, headcount_max, hiring_potential, high_potential "
            "FROM companies ORDER BY hiring_potential DESC"):
        comp[rome].append((name, naf_label, city, hmin, hmax, pot, high))
    comp_blocks = []
    for j in [j for j in tense if j["rome"] in comp][:15]:
        rows = []
        for name, naf_label, city, hmin, hmax, pot, high in comp[j["rome"]][:6]:
            size = f"{hmin}–{hmax}" if hmax else "—"
            rows.append([name + (" ★" if high else ""), naf_label, city, size])
        comp_blocks.append(f"<h3>{esc(j['label'])} · {len(comp[j['rome']])} entreprises</h3>"
                           + table(["Entreprise", "Activité", "Commune", "Effectif"], rows))
    naf_comp = defaultdict(set)
    for rome, lst in comp.items():
        for c in lst:
            naf_comp[c[1] or "Non renseigné"].add(c[0])
    naf_comp_rows = sorted(([k, len(v)] for k, v in naf_comp.items()), key=lambda r: -r[1])[:20]

    # --- summary bullets ------------------------------------------------------
    def names(lst, n=5):
        return ", ".join(esc(j["label"].rsplit(" (", 1)[0]) for j in lst[:n]) or "—"

    shortage = [j for j in tense if j["main_factor"] and j["main_factor"][0] in ("MAIN_OEUVRE", "SPECIF_FORM_EMPL")]
    hardwork = [j for j in tense if j["main_factor"] and j["main_factor"][0] == "COND_TRAVAIL"]
    summary = [
        f"<li><b>Marché :</b> {num(tot_off)} offres sur 12 mois ({sgn(growth(tot_off, tot_off_prev))} sur un an) "
        f"pour {num(tot_de)} demandeurs d'emploi actifs, soit <b>{num(my_ratio)}</b> offre par demandeur.</li>",
        f"<li><b>Pénurie de candidats ou de formation</b> (piste : formation, recrutement, intérim spécialisé) : {names(shortage)}.</li>",
        f"<li><b>Métiers pénibles en tension</b> (piste : équipement, automatisation, sous-traitance) : {names(hardwork)}.</li>",
        f"<li><b>Métiers en forte croissance :</b> {names([j for j in growing if j['growth'] > 0])}.</li>",
        f"<li><b>Viviers de main-d'œuvre disponible</b> (beaucoup de candidats, peu d'offres) : {names(pools)}.</li>",
    ]

    def profile_table(ctype, title):
        rows = prof.get(ctype)
        if not rows:
            return ""
        return f"<h3>{esc(title)}</h3>" + table(["", "Offres", "Part"], rows, bar_col=1, num_cols=(1, 2))

    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Emploi Le Havre</title>
<style>
:root{{--bg:#f7f6f2;--card:#fff;--ink:#1d1d1b;--muted:#6b6a65;--line:#e4e2db;--accent:#1f6f8b}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151619;--card:#1e2024;--ink:#ececea;--muted:#9a9a96;--line:#2e3036;--accent:#5fb3cf}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:24px 16px 60px}}
h1{{font-size:26px;margin:0 0 4px}}h2{{font-size:19px;margin:40px 0 6px}}h3{{font-size:15px;margin:22px 0 6px}}
.muted{{color:var(--muted)}}.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:20px 0}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.kpi b{{display:block;font-size:26px}}.kpi span{{color:var(--muted);font-size:13px}}
.summary{{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:10px;padding:12px 18px 12px 34px}}
.summary li{{margin:6px 0}}
.tw{{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}}
table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}
th{{font-size:12px;color:var(--muted);font-weight:600;white-space:nowrap}}tr:last-child td{{border-bottom:none}}
.n{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}.bc{{width:18%;min-width:80px;vertical-align:middle}}
.bar{{height:10px;border-radius:5px}}.bar span{{display:block;height:100%;background:var(--accent);border-radius:5px;opacity:.75}}
.chart{{margin:0;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}}
.chart figcaption{{font-size:13px;color:var(--muted)}}.chart svg{{width:100%;height:auto}}
.axis{{font-size:11px;fill:var(--muted)}}.note{{font-size:13px;color:var(--muted);margin:4px 0 10px}}
.two{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px}}
</style></head><body><main>
<h1>Marché du travail — Le Havre et alentours</h1>
<p class="muted">Source : API Marché du travail et La Bonne Boîte (France Travail) · zone : {esc(terr_labels.get(TERRITORY, TERRITORY))}
· offres au {esc(P_OFF)} · demandeurs au {esc(P_DE)} · tension {esc(T_YEAR)} · collecte du {esc(last_run)}</p>

<div class="kpis">
<div class="kpi"><b>{num(tot_off)}</b><span>offres sur 12 mois ({sgn(growth(tot_off, tot_off_prev))} sur un an)</span></div>
<div class="kpi"><b>{num(tot_de)}</b><span>demandeurs d'emploi actifs (cat. A, B, C) ({sgn(growth(tot_de, tot_de_prev))})</span></div>
<div class="kpi"><b>{num(tot_hires)}</b><span>embauches sur 12 mois</span></div>
<div class="kpi"><b>{len(tense)}</b><span>métiers en tension forte ou très forte</span></div>
</div>

<h2>En résumé</h2>
<ul class="summary">{''.join(summary)}</ul>

<h2>Comparaison avec d'autres territoires</h2>
<p class="note">Plus le nombre d'offres par demandeur est élevé, plus les employeurs ont du mal à trouver des candidats.</p>
{table(["Territoire", "Offres 12 mois", "Demandeurs (ABC)", "Offres / demandeur", "Évolution offres"], bench_rows, num_cols=(1, 2, 3, 4))}

<h2>Évolution</h2>
<div class="two">{chart(q_off, "Offres déposées par trimestre")}{chart(q_de, "Demandeurs d'emploi actifs (A, B, C)")}</div>

<h2>Métiers en tension : où sont les pistes</h2>
<p class="note">Métiers avec au moins {MIN_OFFERS} offres sur 12 mois et une tension de recrutement forte (4) ou très forte (5) sur le bassin du Havre en {esc(T_YEAR)}.
Le facteur principal explique <i>pourquoi</i> les employeurs peinent à recruter, et oriente la piste : une pénurie de candidats appelle de la formation ou du recrutement, pas un concurrent de plus.</p>
{table(["Métier (ROME)", "Offres 12 mois", "Demandeurs", "Offres / demandeur", "Tension (1-5)", "Facteur principal", "Piste"], tense_rows, num_cols=(1, 2, 3, 4)) if tense_rows else "<p class='muted'>Pas de données de tension.</p>"}

<h2>Métiers en croissance</h2>
<p class="note">Évolution des offres sur 12 mois par rapport à l'année précédente (métiers avec au moins {MIN_OFFERS} offres).</p>
{table(["Métier (ROME)", "Offres an -1", "Offres 12 mois", "Évolution", "Tension"], grow_rows, num_cols=(1, 2, 3, 4))}

<h2>Viviers de main-d'œuvre disponible</h2>
<p class="note">Beaucoup de demandeurs d'emploi, peu d'offres (moins d'une offre pour deux demandeurs). Une entreprise qui a besoin de ces profils recrutera facilement ici,
et ces personnes sont aussi des créateurs d'entreprise potentiels.</p>
{table(["Métier (ROME)", "Demandeurs", "Offres 12 mois", "Offres / demandeur", "Embauches 12 mois"], pool_rows, bar_col=1, num_cols=(1, 2, 3, 4))}

<h2>Métiers les plus demandés</h2>
{table(["Métier (ROME)", "Offres 12 mois", "Évolution", "Demandeurs", "Offres / demandeur", "Tension"], top_rows, bar_col=1, num_cols=(1, 2, 3, 4, 5))}

<div class="two">
<div><h2>Par grand domaine</h2>
{table(["Domaine", "Offres 12 mois", "Évolution", "Demandeurs", "Offres / dem."], dom_rows, num_cols=(1, 2, 3, 4))}</div>
<div><h2>Métiers en recul</h2>
{table(["Métier (ROME)", "Offres an -1", "Offres 12 mois", "Évolution"], decline_rows, num_cols=(1, 2, 3))}</div>
</div>

<h2>Secteurs d'activité des employeurs</h2>
<p class="note">Offres et embauches par secteur (NAF, 88 divisions). Seules les offres dont le secteur de l'employeur est renseigné sont comptées, d'où un total inférieur.</p>
{table(["Secteur (NAF)", "Offres 12 mois", "Évolution", "Embauches 12 mois"], naf_rows[:35], bar_col=1, num_cols=(1, 2, 3))}

<h2>Profil des offres</h2>
<div class="two"><div>{profile_table("TYPECTR", "Type de contrat")}{profile_table("TAILLEETAB", "Taille de l'établissement")}</div>
<div>{profile_table("NIVQUAL", "Qualification")}{profile_table("NIVEXP", "Expérience demandée")}</div></div>

<h2>Entreprises susceptibles de recruter</h2>
<p class="note">La Bonne Boîte (France Travail) estime le potentiel d'embauche de chaque établissement à {esc(os.getenv("DISTANCE_KM", "30"))} km autour du Havre.
Utile pour savoir qui sont vos futurs clients (formation, recrutement, sous-traitance). ★ = fort potentiel.</p>
{"".join(comp_blocks) or "<p class='muted'>Pas de données (lancez collect.py sans --skip-lbb).</p>"}
{("<h3>Secteurs des entreprises qui recrutent</h3>" + table(["Activité", "Entreprises"], naf_comp_rows, bar_col=1, num_cols=(1,))) if naf_comp_rows else ""}

<h2>Méthode et limites</h2>
<p class="note">Les chiffres par métier sont arrondis à la dizaine par France Travail (secret statistique) : les petits métiers apparaissent à 0 ou 10.
« Offres » = toutes les offres diffusées par France Travail et ses partenaires. Les demandeurs sont un stock (fin de trimestre), les offres et embauches un cumul sur 12 mois.
La zone est l'agence France Travail du Havre ; la tension est mesurée sur le bassin d'emploi BMO du Havre.
Une tension forte signale surtout un manque de main-d'œuvre, pas un manque d'entreprises : pour repérer les activités absentes, croiser avec la densité d'établissements (base Sirene).</p>
</main></body></html>"""

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Report written to {OUT_PATH}")


if __name__ == "__main__":
    main()
