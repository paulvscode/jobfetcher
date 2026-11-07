"""Minimal client for the two France Travail APIs this project uses:

- "Marché du travail v1" (stats-offres-demandes-emploi): official quarterly
  statistics per job (ROME) or sector (NAF) and per territory — offers,
  jobseekers, hires, recruitment-tension indicators.
- "La Bonne Boîte v2": companies likely to hire for a given job around a commune.
"""
import threading
import time

import requests

TOKEN_URL = "https://entreprise.francetravail.fr/connexion/oauth2/access_token"
STATS_URL = "https://api.francetravail.io/partenaire/stats-offres-demandes-emploi/v1"
LBB_URL = "https://api.francetravail.io/partenaire/labonneboite/v2/recherche"

SCOPES = {
    "stats": "api_stats-offres-demandes-emploiv1 offresetdemandesemploi",
    "lbb": "api_labonneboitev2 search office",
}
RATE = {"stats": 8.0, "lbb": 1.8}   # calls per second, under the 10/s and 2/s limits

# Error messages the stats API returns when a combination simply has no data
NO_DATA_HINTS = ("pas disponible", "Not Found", "pas pu trouver")


class FranceTravailClient:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self._tokens = {}                       # api -> (token, expiry)
        self._token_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._next_slot = {api: 0.0 for api in RATE}
        self.calls = 0
        self.errors = 0

    # --- plumbing ----------------------------------------------------------
    def _token(self, api):
        with self._token_lock:
            tok, exp = self._tokens.get(api, (None, 0))
            if tok and time.time() < exp - 30:
                return tok
            r = requests.post(
                TOKEN_URL,
                params={"realm": "/partenaire"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": SCOPES[api],
                },
                timeout=30,
            )
            if r.status_code == 400:
                raise RuntimeError(
                    f"France Travail refused the token for '{api}' ({r.text}). "
                    "Check FT_CLIENT_ID / FT_CLIENT_SECRET and that your application "
                    "is subscribed to this API on francetravail.io.")
            r.raise_for_status()
            data = r.json()
            self._tokens[api] = (data["access_token"], time.time() + int(data.get("expires_in", 1499)))
            return data["access_token"]

    def _throttle(self, api):
        with self._rate_lock:
            now = time.monotonic()
            slot = max(now, self._next_slot[api])
            self._next_slot[api] = slot + 1 / RATE[api]
        if slot > now:
            time.sleep(slot - now)

    def _request(self, api, method, url, **kw):
        r = None
        for attempt in range(6):
            self._throttle(api)
            r = requests.request(
                method, url, timeout=60,
                headers={"Authorization": f"Bearer {self._token(api)}", "Accept": "application/json"},
                **kw)
            self.calls += 1
            if r.status_code == 429 or r.status_code in (502, 503, 504):
                time.sleep(1 + 2 * attempt)     # rate limited / gateway hiccup: back off
                continue
            if r.status_code == 401:            # token expired early
                with self._token_lock:
                    self._tokens.pop(api, None)
                continue
            break
        r.encoding = "utf-8"
        return r

    # --- Marché du travail ---------------------------------------------------
    def referentiel(self, name):
        r = self._request("stats", "GET", f"{STATS_URL}/referentiel/{name}")
        r.raise_for_status()
        return r.json()[name]

    def indicator(self, endpoint, **body):
        """POST /indicateur/<endpoint>. Returns the list of values, [] when no data."""
        r = self._request("stats", "POST", f"{STATS_URL}/indicateur/{endpoint}", json=body)
        if r.ok:
            return r.json().get("listeValeursParPeriode") or []
        if r.status_code in (400, 404, 500) and any(h in r.text for h in NO_DATA_HINTS):
            return []
        self.errors += 1
        return []

    # --- La Bonne Boîte -------------------------------------------------------
    def companies(self, rome, citycode, distance_km, page_size=100):
        """Companies likely to hire for one ROME job, best hiring potential first."""
        r = self._request("lbb", "GET", LBB_URL, params={
            "rome": rome, "citycode": citycode, "distance": distance_km,
            "page_size": page_size, "sort_by": "hiring_potential", "sort_direction": "desc"})
        if not r.ok:
            self.errors += 1
            return []
        return r.json().get("items") or []
