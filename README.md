# Havre Jobs — job market explorer (Le Havre + ~30 km)

Collects official labour-market statistics from France Travail into a local
SQLite database (`jobs.db`) and builds an HTML report (`report.html`) that
points to business opportunities: jobs in tension and why, growing jobs,
available labour pools, and the companies likely to hire.

## 1. Get API credentials (free)

1. Create an account on https://francetravail.io
2. Create an application ("Mes applications" → "Créer une application").
3. Subscribe that application to these APIs:
   - **Marché du travail v1** — statistics on offers, jobseekers, hires, tension
   - **La Bonne Boîte v2** — companies likely to hire
4. Copy the **client ID** and **client secret** into `.env` (see `.env.example`).

## 2. Install

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # then paste your ID and secret into .env
```

## 3. Run

```bash
python collect.py             # job market statistics, ~15 minutes, ~6,000 API calls
python collect_territory.py   # businesses (Sirene) and local services (BPE), ~3 minutes
python report.py              # writes report.html
python dashboard.py           # writes dashboard.html
python serve.py               # http://localhost:8000 (dashboard, report, establishment lists)
```

`collect_territory.py` needs no key. It counts, within 30 km of Le Havre, Rouen,
Caen and Brest, every active establishment by activity (Sirene, monthly) and every
shop, service and health professional by type (BPE, yearly), per 10,000
inhabitants. The dashboard's **Offre locale** tab shows what Le Havre has less of
than the other cities, and how many establishments that represents.

Quick test: `python collect.py --limit 50 --skip-lbb`.

### Gym market tab ("Salles de sport")

- **Competitors**: `gyms.json`, a hand-made list of the gyms and studios in the city of
  Le Havre (prices, plans, commitment, fees, perks, ratings, sources, date checked).
  Edit it when prices change, then re-run `python dashboard.py`.
- **Market**: sports facilities and population by age for the city itself
  (`python collect_territory.py --city-only`, a few seconds), gym openings and closures
  and large employers (Sirene), and sport jobs in Le Havre, Rouen, Caen, Brest,
  Normandy and France (`python collect.py --only-sport`, under a minute).
  Both are also part of the normal monthly runs. Jobs and activity codes are listed in `sport.py`.

### Data checks (`python validate.py`, about 2 minutes)

Runs every month in the workflow; results appear under "Qualité des données" in the gym tab
and as a ✓ / ! next to each gym name.

- **Prices**: every price taken from an official site must still appear on that site;
  a price that disappeared has probably changed. Prices marked `"uncertain": true`
  (directories, blogs) cannot be checked automatically: confirm them by phone.
- **Company register**: an active business must be registered at the gym's address
  (Sirene, through recherche-entreprises.api.gouv.fr). Catches gyms that closed, moved,
  or never existed. Small associations may have no SIRET.
- **Freshness**: each gym can carry its own `"checked": "YYYY-MM-DD"` (otherwise the file's
  date); warning after 90 days. Update it whenever you re-check a gym.
- **Plausibility**: ratings 1–5, believable prices, known units and perks, working links.
- **Statistics**: every territory has the latest quarter, no total jumps by more than 50 %,
  INSEE / Sirene / site audit present and recent.

`python validate.py --offline` runs only the checks that need no internet.

### Client section (one gym, encrypted on the public site)

- `club.json` holds the research and action plan for one client gym (identity, offer, HYROX,
  listings, AI-search tests, manual SEO findings, actions). It stays on your computer
  (`.gitignore`); the repository only holds `club.json.enc`. After each edit:
  `python club.py seal` (and `python club.py open` on another computer).
- `python site_audit.py` crawls the client's site and checks every competitor's site
  (titles, descriptions, H1, schema.org, FAQ, robots.txt and AI crawlers, llms.txt,
  sitemap, images, weight, speed). Add `PAGESPEED_API_KEY` (free Google key) for Core Web Vitals.
- `python site_crawl.py` explores the whole client site (sitemap + every internal link):
  page tree, own word count, inbound links, orphans, broken links, auto-generated descriptions.
  The dashboard turns it into the "Le site page par page" tree and the page-by-page fixes
  under "Recommandations actives" (the written recommendations are `recos` in club.json).
  The code that draws them (`club_crawl.js`) is packed into the encrypted section too, so the
  public page source shows neither the data nor the headings.
- The dashboard shows it as a fourth view of the gym tab. On GitHub Pages it is encrypted
  (AES-256) with `CLUB_PASSWORD`: visitors see a password box, decryption happens in the browser.
  Put the same `CLUB_PASSWORD` in `.env` and in the repository secrets. Without it, the section
  is left out of the public site.

The statistics are published **quarterly**; the weekly run mainly refreshes Sirene,
the website audit and the data checks. Each run refreshes everything; the API itself returns history back to 2020.

## Publish on GitHub Pages (free, data refreshed every week)

`python publish.py` builds a static website in `site/` (dashboard, report and
establishment lists; no keys, no database). Two GitHub workflows do it online:

| Workflow | When | Time | What it does |
|---|---|---|---|
| **Collect data (weekly)** — `collect.yml` | Mondays 04:00 UTC, or *Run workflow* | ~25 min | Collects everything, saves the database as an encrypted snapshot (artifact `data-snapshot`, kept 90 days), then publishes |
| **Publish site** — `publish.yml` | Every push to `main`, or *Run workflow* | a few minutes | Restores the latest snapshot, runs the data checks, rebuilds and deploys the site |

So editing `gyms.json` or the dashboard and pushing updates the site in a few minutes,
without collecting again. If no snapshot exists yet (first run, or none in 90 days),
*Publish site* collects the data itself once.

The snapshot (`python snapshot.py pack` / `unpack`) is gzip + AES-256 encrypted with
`CLUB_PASSWORD` (or `DATA_PASSWORD`): artifacts of a public repository can be downloaded
by anyone, and the database holds sole traders' names.

One-time setup:

1. Create a **public** repository on GitHub and push this project to it.
2. *Settings → Secrets and variables → Actions → New repository secret*: add
   `FT_CLIENT_ID`, `FT_CLIENT_SECRET` and `CLUB_PASSWORD` (same values as in `.env`).
3. *Settings → Pages → Build and deployment → Source*: choose **GitHub Actions**.
4. *Actions → Publish site → Run workflow* for the first run (~25 min, it collects the data).
   The site is then at `https://<your-user>.github.io/<repository>/`.

Sole traders listed under their own name appear as "Entrepreneur individuel" on
the public site. To show their names, add a repository variable or environment
variable `PUBLIC_PERSON_NAMES=1`.

## Settings (in .env)

| Variable | Default | Meaning |
|---|---|---|
| `TERRITORY` | `CLPE76002` | France Travail agency area of Le Havre (offers, jobseekers, hires) |
| `BMO_AREA` | `2806` | BMO employment basin of Le Havre (tension indicators) |
| `COMMUNE` | `76351` | INSEE code of the centre commune, for the company search |
| `DISTANCE_KM` | `30` | Company search radius |
| `BENCHMARKS` | Rouen, Caen, Brest, Normandie, France | Areas compared in the report (`TYPE:code`, comma-separated) |
| `LBB_JOBS` | `80` | Number of jobs searched on La Bonne Boîte |
| `DB_PATH` | `jobs.db` | SQLite file |

## What the report shows

- **Key figures**: offers over 12 months, active jobseekers, hires, number of jobs in tension.
- **Benchmarks**: offers per jobseeker compared with Rouen, Caen, Brest, Normandy, France.
- **Jobs in tension**: tension level (1–5) and its main cause — labour shortage,
  specific training, hard working conditions, distance, short contracts… — with
  the kind of business each cause points to.
- **Growing / declining jobs**: offers this year vs last year.
- **Labour pools**: many jobseekers, few offers — skills available to a new business.
- **Sectors (NAF)**, **grand domains**, **profile of offers** (contracts, company size, qualification).
- **Companies likely to hire** (La Bonne Boîte) for the jobs in tension: potential clients.

## How it works

- `ft_api.py` — OAuth tokens (one per API), rate limiting, retries.
- `collect.py` — for each of ~1,900 ROME jobs: offers in the area; then, for the
  jobs present, jobseekers, hires and tension; then sectors and companies.
- `report.py` — reads the database and writes a self-contained HTML page.

## Reading the numbers

Per-job figures are rounded to the nearest 10 by France Travail (statistical
secrecy). Tension measures **labour shortage**, not missing businesses: a job in
strong tension usually calls for training, recruitment or staffing services, or
for equipment that makes the job easier — not for one more competitor. To spot
missing businesses, cross this with establishment density from the Sirene database.
