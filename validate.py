"""Check that the figures in the dashboard are sound. Results go to jobs.db (table data_quality).

Usage:  python validate.py            all checks (needs internet for the gym checks)
        python validate.py --offline  only the database and gyms.json checks

Gyms (gyms.json):
  - price check:  every price taken from an official site must still appear on that site;
  - registry:     an active business must be registered at the gym's address (company
                  register, recherche-entreprises.api.gouv.fr, no key needed);
  - freshness:    each gym's data is dated ("checked"); warning after 90 days;
  - links and plausibility: pages load, ratings 1-5, prices in a believable range, known units.
Statistics (jobs.db):
  - every territory has the latest quarter, figures not older than expected,
    no total moving by more than 50 % in one quarter, BPE / population / Sirene / site audit present.
"""
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import date, datetime
from urllib.parse import urlparse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(HERE, "jobs.db"))
GYMS_PATH = os.path.join(HERE, "gyms.json")
API = "https://recherche-entreprises.api.gouv.fr/search"
UNITS = {"4sem": 13 / 12, "mois": 1, "semaine": 52 / 12, "an": 1 / 12, "seance": None, "carnet": None}
SPORT_NAF = ("93.1", "85.51", "85.52", "96.04", "86.90", "94.99", "93.29")
FRESH_DAYS, STALE_DAYS = 90, 180

session = requests.Session()
session.headers["User-Agent"] = "Mozilla/5.0 (compatible; HavreJobsValidate/1.0)"


def check(status, label, detail="", siret=None):
    c = {"s": status, "l": label, "v": detail}
    if siret:
        c["siret"] = siret
    return c


def label(est):
    """Registered name, plus the trade name when it adds something."""
    b = est.get("brand", "")
    return est["name"] + (f" ({b})" if b and squash(b) not in squash(est["name"]) else "")


def worst(items):
    order = ["bad", "warn", "info", "ok"]
    return min((i["s"] for i in items), key=order.index) if items else "info"


# ---- text helpers --------------------------------------------------------------------------
def plain(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper()
    return re.sub(r"\s+", " ", s).strip()


def squash(s):
    return re.sub(r"[^A-Z0-9]", "", plain(s))


ABBR = {"BD": "BOULEVARD", "AV": "AVENUE", "AVE": "AVENUE", "PL": "PLACE", "R": "RUE", "GEN": "GENERAL",
        "GAL": "GENERAL", "MAL": "MARECHAL", "DR": "DOCTEUR", "ST": "SAINT"}
GENERIC = {"RUE", "AVENUE", "BOULEVARD", "PLACE", "QUAI", "CHEMIN", "ALLEE", "IMPASSE", "LE", "LA", "LES", "DES",
           "DU", "DE", "HAVRE", "CENTRE", "COMMERCIAL", "BIS", "TER", "ESPACE", "COMPLEXE", "IMMEUBLE"}


def addr_key(a):
    """House numbers and significant street words of an address."""
    a = re.sub(r"\(.*?\)", " ", a)
    words = [ABBR.get(w, w) for w in re.findall(r"[A-Z0-9]+", plain(a))]
    nums = {int(n) for n in re.findall(r"\b(\d{1,4})\b", " ".join(words)) if not re.fullmatch(r"76\d{3}", n)}
    nums = {n for n in nums if n < 1000}
    toks = {w for w in words if len(w) >= 4 and not w.isdigit() and w not in GENERIC}
    return nums, toks


def addr_match(gym_addr, reg_addr):
    """'same' (number and street), 'street' (same street, other number) or None."""
    gn, gt = addr_key(gym_addr)
    rn, rt = addr_key(reg_addr)
    if not gt or len(gt & rt) < min(2, len(gt)):     # all street words, or two of them
        return None
    kind = lambda a: {ABBR.get(w, w) for w in words(a)} & {"RUE", "AVENUE", "BOULEVARD", "PLACE", "QUAI", "ALLEE", "IMPASSE"}
    if kind(gym_addr) and kind(reg_addr) and not kind(gym_addr) & kind(reg_addr):   # rue X ≠ place X
        return None
    return "same" if gn & rn else "street"


# ---- 1. prices on the official pages ---------------------------------------------------------
def page_text(url):
    r = session.get(url, timeout=25)
    r.raise_for_status()
    t = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", r.text)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    for a, b in (("&nbsp;", " "), ("&euro;", "€"), ("&#8364;", "€"), (" ", " "), (" ", " ")):
        t = t.replace(a, b)
    return re.sub(r"\s+", " ", t)


def price_pattern(p):
    ip, dec = int(p), round((p - int(p)) * 100)
    ipat = re.sub(r"(\d)(?=(\d{3})+$)", r"\1[\\s.]?", str(ip))       # 1100 -> 1[\s.]?100
    if dec == 0:
        body = rf"{ipat}(?:\s?[.,]\s?00|\s00)?"
    else:
        alt = rf"|{ipat}[.,]{dec // 10}" if dec % 10 == 0 else ""
        body = rf"(?:{ipat}\s?[.,\s]\s?{dec:02d}{alt})"
    return re.compile(rf"(?<![\d.,]){body}(?![\d])\s*(?:€|EUR|euros?)|€\s*{body}(?![\d])", re.I)


def check_prices(g):
    plans = [p for p in g.get("plans", []) if p.get("price")]
    if not g.get("official"):
        return check("info", "Prix", "source non officielle : prix marqués « ≈ », non vérifiables automatiquement"), []
    if not plans:
        return check("info", "Prix", "aucun prix publié"), []
    dom = urlparse(g["url"]).netloc.replace("www.", "")
    urls = [u for u in dict.fromkeys([g["url"]] + g.get("sources", [])) if urlparse(u).netloc.replace("www.", "") == dom]
    text, errors = "", []
    for u in urls[:4]:
        try:
            text += " " + page_text(u)
        except requests.RequestException as e:
            errors.append(f"{u} : {str(e)[:80]}")
        time.sleep(0.3)
    if not text.strip():
        return check("bad", "Prix", "pages officielles inaccessibles : " + "; ".join(errors)), []
    detail, found, checked = [], 0, 0
    for p in plans:
        if p.get("uncertain"):
            detail.append({"n": p["n"], "price": p["price"], "s": "info", "v": "≈ autre source"})
            continue
        checked += 1
        ok = bool(price_pattern(p["price"]).search(text))
        found += ok
        detail.append({"n": p["n"], "price": p["price"], "s": "ok" if ok else "warn",
                       "v": "trouvé sur le site" if ok else "introuvable sur la page"})
    if not checked:
        return check("info", "Prix", "seulement des prix approximatifs"), detail
    euros = text.count("€")
    if found == checked:
        st, v = "ok", f"{found}/{checked} prix retrouvés sur le site officiel"
    elif found == 0 and euros < 3:
        st, v = "info", "prix absents du texte des pages lues (affichage dynamique ou autre page) : à contrôler à la main"
    elif found == 0:
        st, v = "bad", f"aucun des {checked} prix retrouvé : tarifs probablement modifiés"
    else:
        st, v = "warn", f"{found}/{checked} prix retrouvés : certains tarifs ont pu changer"
    return check(st, "Prix", v), detail


# ---- 2. company register -------------------------------------------------------------------
def api(params):
    for attempt in range(4):
        r = session.get(API, params={**params, "per_page": 25}, timeout=30)
        if r.status_code == 429:
            time.sleep(2 + attempt * 2)
            continue
        r.raise_for_status()
        time.sleep(0.25)
        return r.json().get("results", [])
    return []


def establishments(results):
    for e in results:
        person = bool((e.get("complements") or {}).get("est_entrepreneur_individuel"))
        for m in e.get("matching_etablissements") or []:
            yield {"siret": m.get("siret"), "name": "entrepreneur individuel" if person else e.get("nom_complet", ""),
                   "naf": m.get("activite_principale") or "", "addr": m.get("adresse") or "",
                   "shown": "adresse non affichée" if person else (m.get("adresse") or "").title(),
                   "active": m.get("etat_administratif") == "A", "person": person,
                   "brand": "" if person else " ".join(m.get("liste_enseignes") or [])}


POOL = None
POSTCODES = ("76600", "76610", "76620")
POOL_NAF = ("93.13Z", "93.12Z", "93.11Z", "93.19Z", "85.51Z", "96.04Z")


def pool():
    """Every active sport business registered in Le Havre (about 700), loaded once."""
    global POOL
    if POOL is None:
        POOL = {}
        for naf in POOL_NAF:
            for cp in POSTCODES:
                page = 1
                while True:
                    r = session.get(API, params={"activite_principale": naf, "code_postal": cp, "etat_administratif": "A",
                                                 "per_page": 25, "page": page}, timeout=30)
                    if r.status_code == 429:
                        time.sleep(3)
                        continue
                    j = r.json()
                    for est in establishments(j.get("results", [])):
                        if est["addr"].endswith("LE HAVRE"):
                            POOL.setdefault(est["siret"], est)
                    if page >= j.get("total_pages", 1) or page >= 30:
                        break
                    page += 1
                    time.sleep(0.2)
        print(f"   ({len(POOL)} sport businesses registered in Le Havre)")
    return POOL


def words(s):
    return set(re.findall(r"[A-Z0-9]+", plain(s)))


def gym_keys(g):
    names = [re.sub(r"\(.*?\)", "", g["name"]).replace("Le Havre", "")] + ([g["brand"]] if g.get("brand") else [])
    return [n for n in names if len(squash(n)) >= 4]


def is_named(keys, est):
    target = est["name"] + " " + est["brand"]
    tw, ts = words(target), squash(target)
    return any(words(k) - {"LE", "LA", "DE", "CLUB", "CENTRE"} <= tw or (len(squash(k)) >= 6 and squash(k) in ts)
               for k in keys)


def check_registry(g):
    parts = [p.strip() for p in re.split(r"·", g.get("address", "")) if p.strip()]
    if not parts or parts == ["Le Havre"]:
        return check("warn", "Registre", "adresse inconnue : impossible de vérifier"), []
    keys, found = gym_keys(g), dict(pool())
    for part in parts:
        cp = re.search(r"\b(76\d{3})\b", part)
        q = re.sub(r"\(.*?\)|,?\s*76\d{3}", "", part).strip(" ,")
        num = re.search(r"\d", q)
        q = q[num.start():] if num else q            # "Complexe NEF, 51 rue…" -> "51 rue…"
        for cp in ([cp.group(1)] if cp else POSTCODES):
            for est in establishments(api({"q": q, "code_postal": cp})):
                found.setdefault(est["siret"], est)
    for k in keys:
        for est in establishments(api({"q": k, "code_postal": "76600"})):
            found.setdefault(est["siret"], est)
    rows = []
    for est in found.values():
        where = next((m for m in (addr_match(p, est["addr"]) for p in parts) if m), None)
        named = is_named(keys, est)
        sport = est["naf"].startswith(SPORT_NAF)
        if where or named:
            rows.append({**est, "where": where, "named": named, "sport": sport})
    # registered at the same number (sport activity or same name), or on the same street under the same name
    good = [r for r in rows if r["active"] and ((r["where"] == "same" and (r["sport"] or r["named"]))
                                                or (r["where"] == "street" and r["named"] and r["sport"]))]
    if good:
        best = sorted(good, key=lambda r: (not r["named"], r["where"] != "same", not r["sport"],
                                           not r["naf"].startswith("93.13")))[0]
        n = len({r["siret"] for r in good})
        return check("ok", "Registre", f"{label(best)}, SIRET {best['siret']}, NAF {best['naf']}"
                     + (f" (+{n - 1} autre(s))" if n > 1 else ""), best["siret"]), good
    street = [r for r in rows if r["active"] and r["where"] == "street" and r["sport"]]
    if street:
        gnum = min(addr_key(parts[0])[0] or {0})
        r = sorted(street, key=lambda r: abs(min(addr_key(r["addr"])[0] or {999}) - gnum))[0]
        return check("warn", "Registre", f"rien au numéro indiqué ; même rue : {label(r)}, {r['shown']}"), street
    named = [r for r in rows if r["active"] and r["named"]]
    if named:
        r = named[0]
        return check("warn", "Registre", f"nom trouvé à une autre adresse : {label(r)}, {r['shown']}"), named
    closed = [r for r in rows if not r["active"] and r["where"] == "same" and r["sport"]]
    if closed:
        return check("warn", "Registre", f"seulement une activité sportive fermée à cette adresse ({closed[0]['name']}, "
                     f"NAF {closed[0]['naf']}) : ancien exploitant, ou structure non immatriculée"), closed
    kind = g.get("kind", "")
    hint = " (une association sans salarié n'a pas toujours de SIRET)" if kind == "Association" or not g.get("brand") else ""
    return check("warn", "Registre", "aucune activité sportive enregistrée à cette adresse" + hint), []


# ---- 3-5. freshness, links, plausibility -------------------------------------------------------
def check_fresh(g, default):
    d = g.get("checked") or default
    try:
        age = (date.today() - date.fromisoformat(d)).days
    except ValueError:
        return check("bad", "Fraîcheur", f"date invalide : {d}")
    st = "ok" if age <= FRESH_DAYS else "warn" if age <= STALE_DAYS else "bad"
    return check(st, "Fraîcheur", f"relevé du {'/'.join(reversed(d.split('-')))} "
                                  f"(il y a {age} jour{'s' if age > 1 else ''})")


def check_link(url):
    if not url:
        return check("warn", "Lien", "aucun lien")
    try:
        r = session.get(url, timeout=25, allow_redirects=True)
        if r.status_code < 400:
            return check("ok", "Lien", f"{r.status_code}")
        return check("bad", "Lien", f"erreur {r.status_code} : {url}")
    except requests.RequestException as e:
        return check("bad", "Lien", f"inaccessible : {str(e)[:80]}")


def check_values(g, perks, cats):
    issues = []
    if g.get("rating") is not None and not 1 <= g["rating"] <= 5:
        issues.append(f"note {g['rating']} hors de 1–5")
    if g.get("reviews") is not None and (not isinstance(g["reviews"], int) or g["reviews"] < 0):
        issues.append(f"nombre d'avis invalide : {g['reviews']}")
    if g.get("fee") is not None and not 0 <= g["fee"] <= 300:
        issues.append(f"frais d'entrée improbables : {g['fee']} €")
    if g.get("cat") not in cats:
        issues.append(f"type inconnu : {g.get('cat')}")
    for k in g.get("perks", []):
        if k not in perks:
            issues.append(f"atout inconnu : {k}")
    for p in g.get("plans", []):
        u = p.get("unit")
        if u not in UNITS:
            issues.append(f"{p.get('n')} : unité inconnue « {u} »")
            continue
        if not isinstance(p.get("price"), (int, float)) or p["price"] <= 0:
            issues.append(f"{p.get('n')} : prix invalide")
            continue
        if u == "carnet" and not p.get("count"):
            issues.append(f"{p['n']} : carnet sans nombre de séances")
        if UNITS[u] and not 3 <= p["price"] * UNITS[u] <= 400:
            issues.append(f"{p['n']} : {round(p['price'] * UNITS[u])} €/mois, improbable")
        if u == "seance" and not 3 <= p["price"] <= 150:
            issues.append(f"{p['n']} : {p['price']} € la séance, improbable")
    return check("bad" if issues else "ok", "Cohérence", "; ".join(issues) or "valeurs plausibles")


def validate_gyms(online):
    with open(GYMS_PATH, encoding="utf-8") as f:
        G = json.load(f)
    perks, cats = set(G["perks"]), {"Low cost", "Fitness / musculation", "CrossFit / Hyrox",
                                    "Électrostimulation (EMS)", "Yoga / Pilates"}
    out = []
    for g in G["gyms"]:
        print("  ", g["name"])
        row = {"name": g["name"], "checks": [check_fresh(g, G["checked"]), check_values(g, perks, cats)], "plans": []}
        if online:
            try:
                pc, row["plans"] = check_prices(g)
            except Exception as e:          # a site being down must not stop the monthly run
                pc = check("info", "Prix", f"contrôle impossible : {str(e)[:100]}")
            try:
                rc, matches = check_registry(g)
            except Exception as e:
                rc, matches = check("info", "Registre", f"contrôle impossible : {str(e)[:100]}"), []
            if g.get("confirmed") and rc["s"] in ("warn", "bad", "info"):    # checked by hand in gyms.json
                rc = check("ok", "Registre", f"confirmé à la main : {g['confirmed']}")
            row["checks"] += [pc, rc, check_link(g.get("url"))]
            row["registry"] = [{"siret": m["siret"], "name": label(m), "naf": m["naf"], "addr": m["shown"]} for m in matches[:3]]
        row["s"] = worst(row["checks"])
        out.append(row)
    others = []
    if online:
        by_siret = {c["siret"]: g["name"] for g in out for c in g["checks"] if c.get("siret")}
        for o in G.get("others", []):
            print("  ", o["name"])
            try:
                rc, matches = check_registry({"name": o["name"], "address": o.get("address", "")})
            except Exception as e:
                rc = check("info", "Registre", f"contrôle impossible : {str(e)[:100]}")
            if rc.get("siret") in by_siret:     # same business as a gym already listed
                rc = check("warn", "Registre", f"même établissement que {by_siret[rc['siret']]} ({rc['v']}) : "
                           "doublon ou ancien nom, à retirer de la liste ?")
            others.append({"name": o["name"], "checks": [rc], "s": rc["s"]})
    return {"gyms": out, "others": others}


# ---- statistics ------------------------------------------------------------------------------
def quarter_end(p):
    y, q = int(p[:4]), int(p[-1])
    return date(y, q * 3, 30 if q in (2, 3) else 31)


def validate_stats(conn):
    res = []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    terrs = dict(conn.execute("SELECT code, label FROM territories")) if "territories" in tables else {}
    # expected publication lag in days (hires are published later than offers and jobseekers)
    lag = {"offres": (200, 300), "demandeurs": (200, 300), "embauches": (480, 600)}
    for ind, (ok_d, warn_d) in lag.items():
        rows = conn.execute("SELECT territory, MAX(period) FROM stats WHERE indicator=? GROUP BY territory", (ind,)).fetchall()
        if not rows:
            res.append(check("bad", f"France Travail — {ind}", "aucune donnée"))
            continue
        last = max(p for _, p in rows)
        behind = [terrs.get(t, t) for t, p in rows if p != last] + [terrs.get(t, t) for t in terrs if t not in dict(rows)]
        age = (date.today() - quarter_end(last)).days
        st = "bad" if behind else "ok" if age <= ok_d else "warn" if age <= warn_d else "bad"
        res.append(check(st, f"France Travail — {ind}",
                         f"dernier trimestre {last} ({age} jours)" + (f" ; en retard : {', '.join(behind)}" if behind else
                                                                      f", {len(rows)} territoires à jour")))
    # big jumps in the totals (all jobs, per territory)
    jumps = []
    for ind, nom in (("offres", "TOFF-CUMUL12MOIS"), ("demandeurs", "ABC"), ("embauches", "TOUT-TOUTE")):
        for t in terrs:
            v = conn.execute("SELECT period, value FROM stats WHERE indicator=? AND nomenclature=? AND territory=? "
                             "AND activity_type='CUMUL' ORDER BY period DESC LIMIT 2", (ind, nom, t)).fetchall()
            if len(v) == 2 and v[1][1]:
                ch = (v[0][1] - v[1][1]) / v[1][1]
                if abs(ch) > .5:
                    jumps.append(f"{ind} {terrs[t]} {v[1][0]}→{v[0][0]} : {ch:+.0%}")
    res.append(check("warn" if jumps else "ok", "Variations d'un trimestre à l'autre",
                     "; ".join(jumps) if jumps else "aucun total ne varie de plus de 50 %"))
    res.append(check("info", "Arrondis", "France Travail arrondit chaque chiffre par métier à la dizaine : "
                                         "en dessous de 50, lire comme un ordre de grandeur"))
    if "runs" in tables:
        r = conn.execute("SELECT run_at, calls, errors FROM runs ORDER BY run_at DESC LIMIT 1").fetchone()
        if r:
            age = (datetime.now() - datetime.fromisoformat(r[0][:19])).days
            st = "bad" if r[2] and r[2] > r[1] * .05 else "warn" if r[2] or age > 40 else "ok"
            res.append(check(st, "Dernière collecte", f"{r[0][:10]} ({age} jours), {r[1]} appels, {r[2]} erreurs"))
    # INSEE and Sirene
    if "bpe_city" in tables:
        c = conn.execute("SELECT COUNT(DISTINCT city), MAX(year) FROM bpe_city").fetchone()
        res.append(check("ok" if c[0] >= 4 else "warn", "INSEE — équipements (BPE)", f"{c[0]} villes, année {c[1]}"))
    else:
        res.append(check("warn", "INSEE — équipements (BPE)", "absent : python collect_territory.py --city-only"))
    if "pop_age" in tables:
        c = conn.execute("SELECT COUNT(DISTINCT city), MAX(year) FROM pop_age").fetchone()
        res.append(check("ok" if c[0] >= 4 else "warn", "INSEE — population par âge", f"{c[0]} villes, recensement {c[1]}"))
    if "sirene_etab" in tables:
        n, last = conn.execute("SELECT COUNT(*), MAX(created) FROM sirene_etab WHERE created <= ?",
                               (date.today().isoformat(),)).fetchone()
        age = (date.today() - date.fromisoformat(last)).days if last else 999
        res.append(check("ok" if age <= 60 else "warn", "Sirene — établissements",
                         f"{n:,} établissements ; création la plus récente : {last} ({age} jours)".replace(",", " ")))
    if "site_audit" in tables:
        rows = conn.execute("SELECT site, checked, data FROM site_audit").fetchall()
        errs = [s for s, _, d in rows if "error" in json.loads(d)]
        last = max((c for _, c, _ in rows), default="")
        age = (date.today() - date.fromisoformat(last)).days if last else 999
        res.append(check("warn" if errs or age > 40 else "ok", "Audit des sites",
                         f"{len(rows)} sites, du {last}" + (f" ; inaccessibles : {', '.join(errs)}" if errs else "")))
    return res


def main():
    online = "--offline" not in sys.argv
    conn = sqlite3.connect(DB_PATH)
    print("Statistics…")
    stats = validate_stats(conn)
    print("Gyms…" + ("" if online else " (offline)"))
    gyms = validate_gyms(online)
    data = {"checked": date.today().isoformat(), "online": online, "stats": stats, **gyms}
    conn.execute("CREATE TABLE IF NOT EXISTS data_quality (id INTEGER PRIMARY KEY CHECK (id=1), checked TEXT, data TEXT)")
    conn.execute("INSERT OR REPLACE INTO data_quality VALUES (1, ?, ?)", (data["checked"], json.dumps(data, ensure_ascii=False)))
    conn.commit()
    conn.close()
    all_checks = stats + [c for g in gyms["gyms"] + gyms["others"] for c in g["checks"]]
    counts = {s: sum(c["s"] == s for c in all_checks) for s in ("ok", "warn", "bad", "info")}
    print(f"Done: {counts['ok']} ok, {counts['warn']} to review, {counts['bad']} to fix, {counts['info']} for info.")
    for g in gyms["gyms"] + gyms["others"]:
        for c in g["checks"]:
            if c["s"] in ("bad", "warn"):
                print(f"  [{c['s']}] {g['name']} — {c['l']} : {c['v']}")
    for c in stats:
        if c["s"] in ("bad", "warn"):
            print(f"  [{c['s']}] {c['l']} : {c['v']}")


if __name__ == "__main__":
    main()
