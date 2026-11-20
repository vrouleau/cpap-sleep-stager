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
| O2Ring raw recordings (no extension, e.g. `20261101214802`, in `%LOCALAPPDATA%\O2 Insight Pro\DATA\<device id>`) | built-in decoder | Same as the CSV, without exporting: 4-s SpO₂, pulse and motion |

For each 30-second epoch it computes features over a 2-minute window, then stages:

* **NREM**: metronomic breathing, low and steady pulse, no movement.
* **REM**: irregular breathing (rate *and* depth), more variable pulse, but no movement (atonia). It is suppressed in the first 45 min after sleep onset.
* **Awake**: seeded by movement or mask removal, then grown into neighbouring epochs whose pulse is above an **adaptive cut** that lies between *this night's* NREM pulse and its movement-confirmed wake pulse. Stretches of 4 minutes or more above the cut count as awake even without movement (lying awake quietly).

All thresholds are relative to the night itself (robust z-scores), so the rules adapt to you. It also works with only one source, though less accurately: CPAP-only cannot really separate quiet wake from REM, and O2Ring-only has no breathing data.

**CPAP only (no ring):** a long period of lying awake with the mask on leaves no movement or mask-off marker, but the breathing changes. On real nights with a known awake period it dropped from about 21 to 13 breaths/min and became about twice as irregular for hours, then switched back when sleep returned. So 20 minutes or more of breathing that is at least 15% slower than the night's sleep rate *and* irregular counts as awake, and the awake period grows through short sleep-like gaps (`bw_*` in `DEFAULTS`). REM is irregular too, but it does not slow down like this, and it is rarely that long.

### Your wake log

On a night's page, log the times you remember being awake (for example *01:30 to 05:15* after the dog woke you). They show as a dark band on the timeline, and the page reports how much of that time the app also staged as awake. It is the only ground truth you have without a sleep lab. Use it to judge how far to trust a night, and as data for tuning the rules. Log entries survive re-runs and re-uploads. API: `POST /api/nights/{id}/wake-log` with `{"start": "01:30", "end": "05:15"}`; times before noon count as the next morning.

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

* Drop the night's `*_BRP.edf` (+ `*_EVE.edf`) files and/or the O2Ring CSV or raw ring files, **or a zip of the whole `DATALOG` folder and/or O2 Insight Pro's `DATA` folder**. Nights are split automatically, noon to noon, and named after the evening they start. PLD/SAD/CSL files are ignored. Short CPAP sessions (under 30 min in all) more than 2 h away from the night's main recording, such as testing the machine or a mask in the afternoon or for a minute the next morning, are left out so the hours in between don't count as awake. The night page lists what was ignored. A new night with less than 1 h of recording in total is not imported at all (the upload reports it as skipped).
* **Auto-correct CPAP clock** (on by default): see below.
* Tick *Replace nights that already exist* to re-import a night.

### Clock sync and daylight saving time

The O2Ring takes its time from your phone on every sync, so the app treats it as the true clock and shifts the **ResMed** timestamps to match. ResMed machines never adjust for DST. The AirSense 10 clock has to be changed by hand. The AirSense 11 has no clock you can set and, according to user reports, never resyncs it over cellular, so it can also drift by minutes, sometimes tens of minutes over months. After a time change the CPAP is often exactly an hour off.

When a night has both devices, the app estimates the offset two ways:

1. **Mask on/off vs. ring motion** (primary). Each BRP file starts and ends when the mask goes on or comes off, and your hands move the ring at that moment. The app tries every shift up to ±2 h, covering drift plus a missed DST hour, and picks the one that puts the most ring motion on those instants. It's accurate to about the ring's 4-s sampling. Mid-night bathroom breaks give the sharpest markers. For a guaranteed marker, shake your ring hand when you press Start.
2. **SpO₂ response to apneas** (cross-check). The average finger SpO₂ low point should come about 30 s after a ResMed event ends. It only confirms the estimate to about ±15 s.

**Travelling:** don't touch the AirSense 11. Changing its time zone requires erasing its data, so leave it on home time. Your phone, and so the ring, switches to local time. The app first compares when each device's recording starts and ends to find the rough gap (for example +6 h in Paris), then refines it with the mask on/off markers. The timeline then shows the ring's local time. Two limits: CPAP-only nights reuse the last correction, so wear the ring on your first night away and your first night back. A shift of about 12 h or more (Asia or Australia from Toronto) can split one night into two dates.

The night page shows the result, including a ⏰ "missed DST" note when the shift is about 1 h, a ✈️ note when it's several hours, and an **Apply & re-run** button. With *Auto-correct CPAP clock* ticked on upload, a high-confidence correction is applied automatically. CPAP-only nights then reuse the correction from the closest earlier night that had both devices. You can also set *CPAP clock correction (s)* by hand, for example `-3600` when the machine is still on summer time.

### API

```
GET    /api/nights                  list + summaries
POST   /api/nights                  multipart: files=..., engine, offset (CPAP clock correction, s),
                                    auto_sync, dayfirst, age, gender, replace
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
python scripts/make_demo_data.py demo --ring-ahead -3600   # simulate a CPAP that missed DST
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
  staging/sync.py          CPAP vs O2Ring clock-offset detection (incl. DST)
  pipeline.py              upload → nights → analysis → DB
  main.py                  FastAPI routes, templates, JSON API
```
