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

The statistics are published **quarterly**, so running `collect.py` once a month
is enough. Each run refreshes everything; the API itself returns history back to 2020.

## Publish on GitHub Pages (free, updated every month)

`python publish.py` builds a static website in `site/` (dashboard, report and
establishment lists; no keys, no database). The GitHub workflow in
`.github/workflows/publish.yml` does the whole thing every month on GitHub:
collect, build, publish.

One-time setup:

1. Create a **public** repository on GitHub and push this project to it.
2. *Settings → Secrets and variables → Actions → New repository secret*: add
   `FT_CLIENT_ID` and `FT_CLIENT_SECRET` (same values as in `.env`).
3. *Settings → Pages → Build and deployment → Source*: choose **GitHub Actions**.
4. *Actions → Collect data and publish → Run workflow* for the first run (~25 min).
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
