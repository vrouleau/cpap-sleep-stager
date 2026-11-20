# cpap-sleep-stager

Self-hosted web app that turns a **ResMed CPAP SD card** (`BRP.edf` flow trace + `EVE.edf` events) and/or a **Wellue O2Ring** CSV export into a timestamped **Awake / NREM / REM** timeline. Every night is stored in a database so you can browse your history and trends.

![Night view](docs/night.png)

> **Not a medical device.** Staging from breathing + pulse + motion is an *estimate*. Without EEG, the hardest call is telling REM from quiet wakefulness. Use it to look at trends, not to diagnose anything.

## What it does

| Input | Read with | Used for |
|---|---|---|
| `DATALOG/YYYYMMDD/*_BRP.edf` | [`pyedflib`](https://github.com/holgern/pyedflib), with a built-in fallback reader for odd ResMed headers | Breath detection at 25 Hz: rate, breath-to-breath interval variability, tidal-amplitude variability, mask-on/off |
| `*_EVE.edf` | pyedflib (EDF+ annotations) | Apneas / hypopneas / RERAs, used for event markers and AHI over *sleep* time |
| O2Ring CSV (O2 Insight Pro / ViHealth export) | [`o2ring-analyzer`](https://pypi.org/project/o2ring-analyzer/) | Pulse, SpO₂ and motion, plus its ODI3/ODI4, T90, nadir and desaturation events |

For each 30-second epoch it computes features over a 2-minute window, then stages:

* **NREM**: metronomic breathing, low and steady pulse, no movement.
* **REM**: irregular breathing (rate *and* depth), more variable pulse, but no movement (atonia). It is suppressed in the first 45 min after sleep onset.
* **Awake**: seeded by movement or mask removal, then grown into neighbouring epochs whose pulse is above an **adaptive cut** that lies between *this night's* NREM pulse and its movement-confirmed wake pulse. Stretches of 4 minutes or more above the cut count as awake even without movement (lying awake quietly).

All thresholds are relative to the night itself (robust z-scores), so the rules adapt to you. It also works with only one source, though less accurately: CPAP-only cannot really separate quiet wake from REM, and O2Ring-only has no breathing data.

### Optional: `sleepecg` engine

[`sleepecg`](https://github.com/cbrnr/sleepecg)'s `wrn-gru-mesa` model (wake/REM/NREM) can serve as a prior. The respiratory and motion evidence is then fused on top. The catch is that sleepecg was trained on ECG R-peaks, and the O2Ring only exports a smoothed pulse rate. Here beat times are rebuilt by integrating that pulse rate, which keeps heart-rate level and slow variability but loses true beat-to-beat HRV. Treat it as an experiment and compare it against the heuristic on your own nights. To enable it, build with `WITH_SLEEPECG=true` (keras on the jax CPU backend, image about 600 MB larger). Age and sex are model inputs, and you can set them on upload.

## Run it

```bash
git clone https://github.com/vrouleau/cpap-sleep-stager
cd cpap-sleep-stager
docker compose up -d --build
# → http://localhost:8000
```

Data lives in `./data` (`sleep.db` plus the original uploaded files, kept so a night can be re-analysed after an algorithm change or a clock-offset fix). SQLite is the default. Set `DATABASE_URL` to use Postgres (add `psycopg[binary]` to `requirements.txt`).

### Uploading

* Drop the night's `*_BRP.edf` (+ `*_EVE.edf`) files and/or the O2Ring CSV, **or a zip of the whole `DATALOG` folder**. Nights are split automatically, noon to noon, and named after the evening they start. PLD/SAD/CSL files are ignored.
* **O2Ring clock offset**: the ring and the CPAP keep their own clocks. If the desaturations don't line up with the apnea markers, set an offset in seconds and hit *Re-run*.
* Tick *Replace nights that already exist* to re-import a night.

### API

```
GET    /api/nights                  list + summaries
POST   /api/nights                  multipart: files=..., engine, offset, dayfirst, age, gender, replace
GET    /api/nights/{id}             summary + per-epoch features/stages + events
GET    /api/nights/{id}/segments    merged Awake/NREM/REM runs with start/end
GET    /api/nights/{id}/epochs.csv  per-epoch export
DELETE /api/nights/{id}
```

Bulk import from the SD card, for example from a homelab cron job:

```bash
cd /media/SDCARD && zip -qr /tmp/sd.zip DATALOG
curl -F files=@/tmp/sd.zip -F files=@O2Ring_export.csv http://sleep.local:8000/api/nights
```

## Development

```bash
pip install -r requirements.txt pytest httpx
python scripts/make_demo_data.py demo --nights 5   # synthetic BRP/EVE/CSV + truth_*.csv
DATA_DIR=./data uvicorn app.main:app --reload
pytest -q
```

The tests check readers, grouping, the API round trip and staging agreement on **synthetic** nights (about 95% combined). Synthetic data follows the same physiology the rules assume, so this confirms the plumbing, not real-world accuracy. Tuning knobs are in `app/staging/engine.py` (`DEFAULTS`).

```
app/
  ingest/resmed.py         BRP/EVE readers (pyedflib + fallback)
  ingest/o2ring.py         o2ring-analyzer wrapper
  staging/features.py      breath detection + per-epoch features
  staging/engine.py        heuristic staging (+ fusion with an ML prior)
  staging/sleepecg_engine.py optional sleepecg prior
  pipeline.py              upload → nights → analysis → DB
  main.py                  FastAPI routes, templates, JSON API
```
