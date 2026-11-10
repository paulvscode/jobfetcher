"""Technical SEO / GEO / AEO check of the gyms' websites, stored in jobs.db.

Usage:  python site_audit.py

- The client's site (from club.json, see club.py) is crawled page by page from its sitemap.
- Every competitor with an official page in gyms.json is checked on that page.
- Site-wide: robots.txt (and which AI crawlers it blocks), sitemap, llms.txt, HTTPS,
  compression, structured data (schema.org), and, if PAGESPEED_API_KEY is set,
  Google PageSpeed scores for mobile.

Results go to the table site_audit(site, checked, data) — one JSON row per site.
"""
import json
import os
import re
import sqlite3
import time
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from club import load_club, load_env

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(HERE, "jobs.db"))
GYMS_PATH = os.path.join(HERE, "gyms.json")
UA = "Mozilla/5.0 (compatible; HavreJobsSiteAudit/1.0)"
MAX_PAGES = 80
AI_BOTS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-SearchBot", "PerplexityBot",
           "Google-Extended", "CCBot", "Applebot-Extended", "Bingbot", "Googlebot"]
LOCAL_TYPES = {"LocalBusiness", "SportsActivityLocation", "ExerciseGym", "HealthClub", "SportsClub",
               "SportsOrganization", "Organization"}

session = requests.Session()
session.headers["User-Agent"] = UA


class Page(HTMLParser):
    """Collects what a search engine or an AI crawler reads from one HTML page."""
    SKIP = {"script", "style", "noscript", "svg", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.lang, self.meta, self.links = "", "", {}, []
        self.h = {"h1": [], "h2": [], "h3": []}
        self.imgs, self.anchors, self.ld, self.scripts, self.css = [], [], [], 0, 0
        self.text, self._stack, self._buf, self._ld = [], [], None, None

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "html":
            self.lang = a.get("lang", "")
        elif tag == "meta":
            k = (a.get("name") or a.get("property") or "").lower()
            if k:
                self.meta[k] = a.get("content", "")
        elif tag == "link":
            self.links.append(a)
            if "stylesheet" in a.get("rel", ""):
                self.css += 1
        elif tag == "img":
            self.imgs.append(a)
        elif tag == "a" and a.get("href"):
            self.anchors.append(a["href"])
        elif tag == "script":
            if a.get("src"):
                self.scripts += 1
            if a.get("type", "").lower() == "application/ld+json":
                self._ld = []
        if tag in ("title", "h1", "h2", "h3"):
            self._buf = []
        self._stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "script" and self._ld is not None:
            self.ld.append("".join(self._ld))
            self._ld = None
        if tag in ("title", "h1", "h2", "h3") and self._buf is not None:
            t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if tag == "title":
                self.title = self.title or t
            else:
                self.h[tag].append(t)
            self._buf = None
        while self._stack:
            if self._stack.pop() == tag:
                break

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
            return
        if self._buf is not None:
            self._buf.append(data)
        if not any(t in self.SKIP for t in self._stack):
            self.text.append(data)


def get(url, **kw):
    t = time.time()
    r = session.get(url, timeout=25, allow_redirects=True, **kw)
    return r, round(time.time() - t, 2)


def ld_types(blocks):
    """schema.org types and the local-business object found in the JSON-LD blocks."""
    types, local, faq = [], None, 0

    def walk(o):
        nonlocal local, faq
        if isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, dict):
            t = o.get("@type")
            for x in (t if isinstance(t, list) else [t] if t else []):
                types.append(x)
                if x in LOCAL_TYPES and (local is None or x != "Organization"):
                    local = o
                if x == "FAQPage":
                    faq += len(o.get("mainEntity") or [])
            for v in o.values():
                walk(v)
    for b in blocks:
        try:
            walk(json.loads(b))
        except ValueError:
            types.append("(JSON-LD invalide)")
    return sorted(set(types)), local, faq


def analyse(url, r, secs):
    p = Page()
    p.feed(r.text)
    words = len(re.findall(r"\w{2,}", " ".join(p.text)))
    host = urlparse(r.url).netloc
    internal = sum(1 for h in p.anchors if urlparse(urljoin(r.url, h)).netloc == host)
    types, local, faq = ld_types(p.ld)
    canon = next((l.get("href") for l in p.links if l.get("rel") == "canonical"), "")
    text = " ".join(p.text).lower()
    return {
        "url": url, "final": r.url, "status": r.status_code, "secs": secs, "kb": round(len(r.content) / 1024),
        "title": p.title, "desc": p.meta.get("description", ""), "robots": p.meta.get("robots", ""),
        "canonical": canon, "lang": p.lang, "viewport": "viewport" in p.meta,
        "og": bool(p.meta.get("og:title")), "og_image": bool(p.meta.get("og:image")),
        "generator": p.meta.get("generator", ""),
        "h1": p.h["h1"][:5], "h1n": len(p.h["h1"]), "h2n": len(p.h["h2"]), "h2": p.h["h2"][:25],
        "words": words, "imgs": len(p.imgs),
        "img_noalt": sum(1 for i in p.imgs if not i.get("alt", "").strip()),
        "img_lazy": sum(1 for i in p.imgs if i.get("loading") == "lazy"),
        "img_modern": sum(1 for i in p.imgs if re.search(r"\.(webp|avif)", i.get("src", "") + i.get("srcset", ""))),
        "links_in": internal, "links_out": len(p.anchors) - internal, "scripts": p.scripts, "css": p.css,
        "schema": types, "faq": faq,
        "local": {k: bool(local.get(k)) for k in ("address", "geo", "openingHoursSpecification", "telephone",
                                                  "aggregateRating", "priceRange", "sameAs", "image")} if local else None,
        "local_type": (local.get("@type") if local else ""),
        "questions": sum(1 for h in p.h["h2"] + p.h["h3"] if h.endswith("?")),
        "kw": {k: text.count(k) for k in ("le havre", "hyrox", "salle de sport", "musculation", "sans engagement",
                                         "tarif", "prix", "€")},
    }


def robots_info(base):
    out = {"exists": False, "sitemaps": [], "blocked": [], "all_blocked": False}
    try:
        r, _ = get(base + "/robots.txt")
    except requests.RequestException:
        return out
    if r.status_code != 200 or "<html" in r.text[:500].lower():
        return out
    out["exists"] = True
    groups, agents, rules = [], [], []
    for line in r.text.splitlines():
        line = line.split("#")[0].strip()
        if ":" not in line:
            continue
        k, v = (s.strip() for s in line.split(":", 1))
        k = k.lower()
        if k == "sitemap":
            out["sitemaps"].append(v)
        elif k == "user-agent":
            if rules:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(v.lower())
        elif k in ("disallow", "allow"):
            rules.append((k, v))
    if agents:
        groups.append((agents, rules))

    def blocks_all(agent):
        g = [r for a, r in groups if agent.lower() in a] or [r for a, r in groups if "*" in a]
        return any(k == "disallow" and v == "/" for rs in g for k, v in rs)
    out["blocked"] = [b for b in AI_BOTS if blocks_all(b)]
    out["all_blocked"] = blocks_all("*")
    return out


def sitemap_urls(url, depth=0):
    try:
        r, _ = get(url)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    locs = [re.sub(r"<!\[CDATA\[|\]\]>", "", x).strip() for x in re.findall(r"<loc>(.*?)</loc>", r.text, re.S)]
    if "<sitemapindex" in r.text and depth < 2:
        out = []
        for l in locs:
            out += sitemap_urls(l, depth + 1)
        return out
    return locs


def pagespeed(url):
    key = os.getenv("PAGESPEED_API_KEY")
    if not key:
        return None
    params = [("url", url), ("strategy", "mobile"), ("key", key)] + [
        ("category", c) for c in ("performance", "seo", "accessibility", "best-practices")]
    try:
        j = session.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params, timeout=120).json()
        lh = j["lighthouseResult"]
        a = lh["audits"]
        return {"scores": {k: round(v["score"] * 100) for k, v in lh["categories"].items()},
                "lcp": a["largest-contentful-paint"]["displayValue"], "cls": a["cumulative-layout-shift"]["displayValue"],
                "tbt": a["total-blocking-time"]["displayValue"], "weight": a["total-byte-weight"]["displayValue"],
                "field": (j.get("loadingExperience") or {}).get("overall_category")}
    except (requests.RequestException, KeyError, ValueError):
        return None


def site_checks(url):
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    out = {"robots": robots_info(base)}
    sm = out["robots"]["sitemaps"] or [base + "/sitemap.xml"]
    urls = []
    for s in sm[:3]:
        urls += sitemap_urls(s)
    out["sitemap_n"] = len(set(urls))
    out["sitemap_urls"] = sorted(set(urls))
    try:
        r, _ = get(base + "/llms.txt")
        out["llms"] = r.status_code == 200 and "<html" not in r.text[:300].lower()
    except requests.RequestException:
        out["llms"] = False
    try:
        r = session.get(f"http://{u.netloc}/", timeout=20, allow_redirects=True)
        out["https_redirect"] = r.url.startswith("https://")
        out["hsts"] = "strict-transport-security" in {k.lower() for k in r.headers}
        out["gzip"] = r.headers.get("content-encoding", "") in ("gzip", "br", "zstd")
        out["server"] = r.headers.get("server", "")
    except requests.RequestException:
        out["https_redirect"] = False
    return out


def audit(url, deep=False):
    print("  ", url, "(crawl)" if deep else "")
    try:
        r, secs = get(url)
    except requests.RequestException as e:
        return {"url": url, "error": str(e)[:200]}
    home = analyse(url, r, secs)
    site = site_checks(r.url)
    site["home"] = home
    site["pagespeed"] = pagespeed(r.url)
    if deep:
        pages = []
        for u in site["sitemap_urls"][:MAX_PAGES]:
            if u.rstrip("/") == r.url.rstrip("/"):
                continue
            try:
                pr, ps = get(u)
                if "text/html" in pr.headers.get("content-type", ""):
                    a = analyse(u, pr, ps)
                    for k in ("h2", "og", "og_image", "generator", "viewport", "lang", "local", "kw"):
                        a.pop(k, None)
                    pages.append(a)
            except requests.RequestException as e:
                pages.append({"url": u, "error": str(e)[:200]})
            time.sleep(0.3)
        site["pages"] = pages
    return site


def main():
    load_env()
    with open(GYMS_PATH, encoding="utf-8") as f:
        gyms = json.load(f)["gyms"]
    club = load_club() or {}
    targets = {}
    if club.get("site"):
        targets[club["name"]] = (club["site"], True)
    for g in gyms:
        if g.get("official") and g.get("url") and g["name"] not in targets:
            url = g["url"]
            if g.get("kind") != "Chaîne":  # an independent gym is judged on its own home page
                u = urlparse(url)
                url = f"{u.scheme}://{u.netloc}/"
            targets[g["name"]] = (url, False)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS site_audit (site TEXT PRIMARY KEY, checked TEXT, data TEXT)")
    print(f"Auditing {len(targets)} sites")
    for name, (url, deep) in targets.items():
        res = audit(url, deep)
        conn.execute("INSERT OR REPLACE INTO site_audit VALUES (?,?,?)",
                     (name, date.today().isoformat(), json.dumps(res, ensure_ascii=False)))
        conn.commit()
    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
