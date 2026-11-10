"""Full crawl of the client's website: every page, its content and how the pages link together.

Usage:  python site_crawl.py              (crawls the site named in club.json)
        python site_crawl.py --if-missing (only if the database has no crawl yet)
        python site_crawl.py --dump F     (also writes every page's full text to the JSON file F)

Starts from the home page and the sitemap, then follows every internal link (same host, HTML only).
For each page it keeps: status, redirects, click depth from the home page, title, description,
headings, word count, a text excerpt, images, structured data, and its internal links.
Then it works out the inbound links of each page, the pages missing from the sitemap,
the sitemap pages that nothing links to (orphans), broken internal links and near-duplicate content.

Result: table site_crawl(site, checked, data) in jobs.db, one JSON row (encrypted with the rest
of the client section when the site is published).
"""
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, deque
from datetime import date
from urllib.parse import quote, unquote, urldefrag, urljoin, urlparse

import requests

from club import load_club, load_env
from site_audit import DB_PATH, Page, get, ld_types, sitemap_urls, robots_info

MAX_PAGES = 400
SKIP_PATH = re.compile(r"/(wp-admin|wp-json|wp-login|feed|xmlrpc|cart|panier|checkout|commander|my-account|mon-compte)(/|$)|"
                       r"\.(jpe?g|png|gif|webp|avif|svg|pdf|zip|mp4|mp3|css|js|ico|xml|txt|docx?|xlsx?)$", re.I)
DROP_QUERY = re.compile(r"^(utm_|fbclid|gclid|replytocom|share|amp)", re.I)


def norm(url):
    """One spelling per page: no fragment, no tracking parameters, trailing slash as WordPress writes it."""
    url = urldefrag(url)[0]
    u = urlparse(url)
    q = "&".join(p for p in u.query.split("&") if p and not DROP_QUERY.match(p))
    path = quote(unquote(u.path or "/"), safe="/%:@!$&'()*+,;=-._~")
    return f"{u.scheme}://{u.netloc.lower()}{path}" + (f"?{q}" if q else "")


def page_data(url, r, secs, depth):
    p = Page()
    p.feed(r.text)
    text = re.sub(r"\s+", " ", " ".join(p.text)).strip()
    types, local, faq = ld_types(p.ld)
    canon = next((l.get("href") for l in p.links if l.get("rel") == "canonical"), "")
    links = []
    for h in p.anchors:
        if h.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        links.append(norm(urljoin(r.url, h)))
    return {
        "url": url, "final": norm(r.url), "status": r.status_code, "secs": secs, "kb": round(len(r.content) / 1024),
        "depth": depth, "title": p.title, "desc": p.meta.get("description", ""), "robots": p.meta.get("robots", ""),
        "canonical": canon, "h1": p.h["h1"], "h2": p.h["h2"][:40], "h3": p.h["h3"][:40],
        "words": len(re.findall(r"\w{2,}", text)), "excerpt": text[:400],
        "imgs": len(p.imgs), "img_noalt": sum(1 for i in p.imgs if not i.get("alt", "").strip()),
        "schema": types, "faq": faq, "questions": sum(1 for h in p.h["h2"] + p.h["h3"] if h.endswith("?")),
        "og_image": bool(p.meta.get("og:image")), "modified": p.meta.get("article:modified_time", "")[:10],
        "_links": links, "_text": text,
    }


def crawl(start):
    host = urlparse(start).netloc.lower()
    base = f"{urlparse(start).scheme}://{host}"
    robots = robots_info(base)
    sm = set()
    for s in (robots["sitemaps"] or [base + "/sitemap.xml"])[:3]:
        sm |= {norm(u) for u in sitemap_urls(s)}
    home = norm(start)
    queue, seen, pages, external = deque([(home, 0)]), {home}, {}, Counter()
    for u in sorted(sm):  # sitemap pages nothing links to still get crawled (depth unknown until linked)
        if u not in seen:
            seen.add(u)
            queue.append((u, None))
    while queue and len(pages) < MAX_PAGES:
        url, depth = queue.popleft()
        try:
            r, secs = get(url)
        except requests.RequestException as e:
            pages[url] = {"url": url, "status": 0, "error": str(e)[:200], "depth": depth, "_links": []}
            continue
        if "text/html" not in r.headers.get("content-type", ""):
            continue
        d = page_data(url, r, secs, depth)
        d["redirects"] = len(r.history)
        pages[url] = d
        print(f"  {r.status_code} d={depth} {url}")
        for l in d["_links"]:
            lh = urlparse(l).netloc.lower()
            if lh != host and lh != "www." + host and "www." + lh != host:
                if lh:
                    external[lh] += 1
                continue
            if SKIP_PATH.search(urlparse(l).path) or "add-to-cart" in l or l in seen:
                continue
            seen.add(l)
            queue.append((l, None if depth is None else depth + 1))
        time.sleep(0.25)
    return home, sm, pages, external, robots


def depths(home, pages):
    """Click depth from the home page, through the links actually found."""
    dist, q = {home: 0}, deque([home])
    while q:
        u = q.popleft()
        for l in pages.get(u, {}).get("_links", []):
            l = pages[l]["final"] if l in pages and pages[l].get("final") else l
            if l in pages and l not in dist:
                dist[l] = dist[u] + 1
                q.append(l)
    return dist


def analyse_site(home, sm, pages, external):
    final = {u: (p.get("final") or u) for u, p in pages.items()}
    inbound = Counter()
    broken = []
    for u, p in pages.items():
        for l in set(p.get("_links", [])):
            if l in pages:
                tgt = final[l]
                if tgt != final[u]:
                    inbound[tgt] += 1
                if pages[l].get("status", 0) >= 400 or pages[l].get("status") == 0:
                    broken.append([u, l, pages[l].get("status", 0)])
    dist = depths(home, pages)
    shingles = {}
    for u, p in pages.items():
        p["depth"] = dist.get(u)
        p["inbound"] = inbound.get(final[u], 0)
        p["in_sitemap"] = u in sm or final[u] in sm
        t = p.get("_text", "")
        if p.get("status") == 200 and len(t) > 200:
            shingles[u] = hashlib.md5(re.sub(r"\W+", "", t.lower())[:3000].encode()).hexdigest()
    dup = Counter(shingles.values())
    for u, h in shingles.items():
        pages[u]["dup"] = dup[h] > 1
    ok = [p for p in pages.values() if p.get("status") == 200 and final[p["url"]] == p["url"]]

    # Menu, footer and review widget: the 6-word sequences and the links found on most pages.
    # What is left is each page's own content and its own links.
    grams = {p["url"]: set(_grams(p.get("_text", "").split())) for p in ok}
    gc = Counter(g for s in grams.values() for g in s)
    lc = Counter(l for p in ok for l in set(p.get("_links", [])))
    cut = max(2, 0.6 * len(ok))
    tmpl, tmpl_links = {g for g, n in gc.items() if n >= cut}, {l for l, n in lc.items() if n >= cut}
    for p in ok:
        w = p.get("_text", "").split()
        keep = [True] * len(w)
        for i, g in enumerate(_grams(w)):
            if g in tmpl:
                keep[i:i + 6] = [False] * 6
        own = " ".join(x for x, k in zip(w, keep) if k)
        p["uwords"] = len(re.findall(r"\w{2,}", own))
        p["excerpt"] = own[:400]
        desc = re.sub(r"\s+", " ", p.get("desc", "")).strip()
        p["desc_auto"] = bool(desc) and desc[:50] in re.sub(r"\s+", " ", p.get("_text", ""))
        p["to"] = sorted({final.get(l, l) for l in p.get("_links", []) if l not in tmpl_links and l in pages
                          and final.get(l, l) != p["url"]})
    return {
        "template_links": sorted(final.get(l, l) for l in tmpl_links if l in pages),
        "home": home, "pages_n": len(pages), "ok_n": len(ok), "sitemap_n": len(sm),
        "not_in_sitemap": sorted(p["url"] for p in ok if not p["in_sitemap"] and "noindex" not in p.get("robots", "")),
        "orphans": sorted(p["url"] for p in ok if p["inbound"] == 0 and p["url"] != home),
        "sitemap_bad": sorted(u for u in sm if u in pages and (pages[u].get("status") != 200 or final[u] != u)),
        "broken": broken[:100], "external": external.most_common(25),
        "words_total": sum(p.get("words", 0) for p in ok),
    }


def _grams(words):
    return (" ".join(words[i:i + 6]) for i in range(max(0, len(words) - 5)))


def menu(url):
    """Main menu of the home page (WordPress menu items), as [label, link] pairs."""
    try:
        r, _ = get(url)
    except requests.RequestException:
        return []
    out = []
    for h, t in re.findall(r'<li[^>]*class="[^"]*menu-item[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S):
        t = re.sub(r"<[^>]+>|\s+", " ", t).strip()
        if t and [t, norm(urljoin(r.url, h))] not in out:
            out.append([t, norm(urljoin(r.url, h))])
    return out


def main():
    load_env()
    club = load_club() or {}
    if not club.get("site"):
        print("No client site in club.json: nothing to crawl.")
        return
    if "--if-missing" in sys.argv:
        conn = sqlite3.connect(DB_PATH)
        has = conn.execute("SELECT 1 FROM sqlite_master WHERE name='site_crawl'").fetchone() and             conn.execute("SELECT 1 FROM site_crawl WHERE site=?", (club["name"],)).fetchone()
        conn.close()
        if has:
            print("Crawl already in the database: skipped.")
            return
    t = time.time()
    print("Crawling", club["site"])
    home, sm, pages, external, robots = crawl(club["site"])
    summary = analyse_site(home, sm, pages, external)
    summary["menu"] = menu(home)
    summary["robots_blocked"] = robots.get("blocked", [])
    summary["secs"] = round(time.time() - t)
    if "--dump" in sys.argv:
        path = sys.argv[sys.argv.index("--dump") + 1]
        with open(path, "w", encoding="utf-8") as f:
            json.dump({u: {k: v for k, v in p.items() if k != "_links"} for u, p in pages.items()},
                      f, ensure_ascii=False, indent=1)
    for p in pages.values():
        p["links_in_n"] = len(set(p.pop("_links", [])))
        p.pop("_text", None)
    summary["pages"] = sorted(pages.values(), key=lambda p: p["url"])
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS site_crawl (site TEXT PRIMARY KEY, checked TEXT, data TEXT)")
    conn.execute("INSERT OR REPLACE INTO site_crawl VALUES (?,?,?)",
                 (club["name"], date.today().isoformat(), json.dumps(summary, ensure_ascii=False)))
    conn.commit()
    conn.close()
    print(f"Done: {summary['pages_n']} URLs, {summary['ok_n']} pages, {summary['secs']} s.")


if __name__ == "__main__":
    main()
