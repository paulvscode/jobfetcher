"""Build the public website in site/ — for GitHub Pages or any static host.

Usage:  python publish.py

site/
  index.html          the dashboard
  report.html         the report
  etab/<NAF>.json     establishment lists used by the "Offre locale" tab

Nothing private goes into site/: no .env, no database. Sole traders listed under
their own name are shown as "Entrepreneur individuel" unless PUBLIC_PERSON_NAMES=1.
"""
import json
import os
import shutil
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.join(HERE, "site")
PUBLIC_PERSON_NAMES = os.getenv("PUBLIC_PERSON_NAMES", "0") == "1"


def main():
    shutil.rmtree(SITE, ignore_errors=True)
    os.makedirs(os.path.join(SITE, "etab"))
    # dashboard.py and report.py read their output path when imported
    os.environ["DASHBOARD_PATH"] = os.path.join(SITE, "index.html")
    os.environ["REPORT_PATH"] = os.path.join(SITE, "report.html")
    import dashboard
    import report
    import serve
    dashboard.main()
    report.main()

    conn = sqlite3.connect(f"file:{serve.DB_PATH}?mode=ro", uri=True)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    files = 0
    if "sirene_etab" in tables:
        codes = [r[0] for r in conn.execute("SELECT code FROM naf_labels")]
        for naf in codes:
            data = serve.establishments(naf, hide_people=not PUBLIC_PERSON_NAMES, conn=conn)
            with open(os.path.join(SITE, "etab", f"{naf}.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            files += 1
    conn.close()

    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(SITE) for f in fs)
    print(f"Site built in {SITE}: index.html, report.html, {files} establishment lists, {size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
