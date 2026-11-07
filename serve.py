"""Serve the dashboard and the report on http://localhost:8000

Usage:  python serve.py [port]

Only dashboard.html, report.html and a read-only list of establishments are
served (never .env or jobs.db), and only on this computer (127.0.0.1). Pages are
read from disk on each request, so re-running dashboard.py / report.py updates
them — just refresh the browser.
"""
import json
import os
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(HERE, "jobs.db"))
PAGES = {"/": "dashboard.html", "/dashboard.html": "dashboard.html", "/report.html": "report.html"}


def establishments(naf, hide_people=False, conn=None):
    """Active establishments of one NAF code around Le Havre, biggest first (max 200).

    hide_people: replace sole traders' own names (and their SIRET) by a generic label,
    for public versions of the dashboard.
    """
    own = conn is None
    conn = conn or sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT e.siret, e.name, c.name, e.size, e.created, e.person FROM sirene_etab e "
        "LEFT JOIN communes c ON c.code = e.commune WHERE e.naf = ? AND e.closed = '' "
        "ORDER BY CASE WHEN e.size GLOB '[0-9][0-9]' THEN e.size ELSE '' END DESC, e.created LIMIT 200",
        (naf,)).fetchall()
    if own:
        conn.close()
    out = []
    for siret, name, commune, size, created, person in rows:
        if siret.startswith("hidden-"):
            siret = name = None
        elif hide_people and person == 1:
            siret, name = None, "Entrepreneur individuel"
        out.append({"siret": siret, "name": name, "commune": commune, "size": size, "created": created})
    return out


class Handler(BaseHTTPRequestHandler):
    def send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/api/etab":
            naf = (parse_qs(url.query).get("naf") or [""])[0]
            if not re.fullmatch(r"\d\d\.\d\d[A-Z]", naf):
                self.send_error(400, "naf must look like 86.90E")
                return
            try:
                data = establishments(naf)
            except sqlite3.Error:
                self.send_error(503, "No establishment data - run collect_territory.py")
                return
            self.send(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        name = PAGES.get(url.path)
        path = os.path.join(HERE, name) if name else None
        if not path or not os.path.exists(path):
            self.send_error(404, "Not found - try / or /report.html")
            return
        with open(path, "rb") as f:
            self.send(f.read(), "text/html; charset=utf-8")

    def log_message(self, fmt, *args):
        pass  # keep the console quiet


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Dashboard: http://localhost:{port}/   Report: http://localhost:{port}/report.html   (Ctrl+C to stop)")
    server.serve_forever()
