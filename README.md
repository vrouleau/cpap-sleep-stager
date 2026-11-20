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
* **Awake**: seeded by movement that means waking (the pulse jumps 5+ bpm, the movement is big, or it lasts more than one epoch; a lone twitch or small position change is not wake) or by mask removal, then grown into neighbouring epochs whose pulse is above an **adaptive cut** that lies between *this night's* NREM pulse and its movement-confirmed wake pulse. Stretches of 4 minutes or more above the cut count as awake even without movement (lying awake quietly). Inside sleep, a wake mark of 1 minute or less stays awake only with both a pulse jump of 8+ bpm and movement. Otherwise it is an *arousal*: you stay asleep, and it is counted per hour and shown as a tick on the timeline.

All thresholds are relative to the night itself (robust z-scores), so the rules adapt to you. It also works with only one source, though less accurately: CPAP-only cannot really separate quiet wake from REM, and O2Ring-only has no breathing data.

**Deep sleep (estimate, needs the ring):** within NREM, stretches with the night's lowest and steadiest pulse, very regular breathing and no movement, lasting 10 min or more, are marked *Deep*. It is a 4th row on the timeline, a tile and a column in the history. Breathing alone was tried and did not agree with the ring-based estimate at all, so CPAP-only nights get none. This is not validated against EEG, and pulse has a natural low in the early morning, so late bouts may be calm light sleep rather than true N3. Read it as "your calmest NREM".

**CPAP only (no ring):** a long period of lying awake with the mask on leaves no movement or mask-off marker, but the breathing changes. On real nights with a known awake period it dropped from about 21 to 13 breaths/min and became about twice as irregular for hours, then switched back when sleep returned. So 20 minutes or more of breathing that is at least 15% slower than the night's sleep rate *and* irregular counts as awake, and the awake period grows through short sleep-like gaps (`bw_*` in `DEFAULTS`). REM is irregular too, but it does not slow down like this, and it is rarely that long.

### Your wake log

On a night's page, log the times you remember being awake (for example *01:30 to 05:15* after the dog woke you). They show as a dark band on the timeline, and the page reports how much of that time the app also staged as awake. It is the only ground truth you have without a sleep lab. Use it to judge how far to trust a night, and as data for tuning the rules. Log entries survive re-runs and re-uploads. API: `POST /api/nights/{id}/wake-log` with `{"start": "01:30", "end": "05:15"}`; times before noon count as the next morning.

### Optional: `sleepecg` engine

[`sleepecg`](https://github.com/cbrnr/sleepecg)'s `wrn-gru-mesa` model (wake/REM/NREM) can serve as a prior. The respiratory and motion evidence is then fused on top. The catch is that sleepecg was trained on ECG R-peaks, and the O2Ring only exports a smoothed pulse rate. Here beat times are rebuilt by integrating that pulse rate, which keeps heart-rate level and slow variability but loses true beat-to-beat HRV. Treat it as an experiment and compare it against the heuristic on your own nights. To enable it, build with `WITH_SLEEPECG=true` (keras on the jax CPU backend, image about 600 MB larger). Age and sex are model inputs, and you can set them on upload.

## Run it

Prebuilt images are published to the GitHub container registry for Intel/AMD and ARM, so a NAS or home server does not have to build anything. You only need [`docker-compose.yml`](docker-compose.yml):

```bash
mkdir sleep-stager && cd sleep-stager
curl -O https://raw.githubusercontent.com/vrouleau/cpap-sleep-stager/main/docker-compose.yml
docker compose pull && docker compose up -d
# → http://<nas-address>:8000
```

Update the same way (`docker compose pull && docker compose up -d`). Your data stays in `./data`.

| Image tag | What it is |
|---|---|
| `latest` | the newest release (recommended) |
| `0.2.0`, `0.2` | a fixed release, to stay on or roll back to |
| `edge` | the current `main` branch |
| `…-sleepecg` | any of the above with the optional sleepecg engine (Intel/AMD only, ~600 MB larger) |

**TerraMaster (TOS):** install *Docker Manager* (or *Docker Engine*) from the App Center, put `docker-compose.yml` in a shared folder (e.g. `/Volume1/docker/sleep-stager`), then run the two commands above there over SSH, or create a project from that file in Docker Manager's *Compose* section. Set `TZ` in the file to your time zone.

To build from source instead: clone the repo and run `docker compose up -d --build`.

### Releases

Every push to `main` runs the tests and publishes `edge`. Tagging a version publishes `latest` and the version tags once the tests pass, and creates a GitHub Release with notes generated from the commits. Cut releases with the script (Git Bash on Windows, or any shell with git and the GitHub CLI):

```bash
scripts/release.sh patch   # v0.1.0 -> v0.1.1: fixes
scripts/release.sh minor   # v0.1.1 -> v0.2.0: new features
scripts/release.sh major   # v0.2.0 -> v1.0.0: breaking changes
scripts/release.sh minor --dry-run   # show the version and changes, touch nothing
```

It refuses to tag unless you are on `main` with nothing uncommitted or unpushed, CI passed on that exact commit, and there are commits since the last release. It then shows the changes, asks for confirmation, and pushes the tag. GitHub does the rest.

Data lives in `./data` (`sleep.db` plus the original uploaded files, kept so a night can be re-analysed after an algorithm change or a clock-offset fix). SQLite is the default. Set `DATABASE_URL` to use Postgres (add `psycopg[binary]` to `requirements.txt`).

### Dashboard

A sidebar (a tab strip on phones) with four sections:

* **Sleep trends** (home): averages for the range, then sleep stages per night (deep, light NREM, REM, awake), sleep window (falling asleep to final awakening), sleep efficiency, awake after falling asleep, and pulse while asleep (O2Ring nights).
* **CPAP trends**: AHI by event type (obstructive, central, hypopnea, unclassified, counted only while asleep), usage with a 4 h line, mask breaks, breathing rate while asleep, and on O2Ring nights ODI 4% and lowest SpO₂.
* **Nights**: every night in a table; open one for its timeline.
* **Data import**: the upload below, plus the most recent imports.

One date-range control above the charts scopes the tiles, every chart and the table: last 7, 30 or 90 days, last 12 months, this month, last month, this year, all nights, or a custom range. It is kept in the URL, so switching tabs keeps it. Ranges over 3 months show weekly averages. Hover a bar or point for its values, and click it to open that night. Colours for stages and event types were checked for colour-blind separation in light and dark mode.

### Uploading

* **Drag in whole folders**, or use *Choose folder*: the ResMed SD card (or its `DATALOG` folder) and/or O2 Insight Pro's `DATA` folder (`%LOCALAPPDATA%\O2 Insight Pro\DATA`). Loose `*_BRP.edf` / `*_EVE.edf` files, O2Ring CSVs or a zip work too. The browser keeps only what the analysis uses (it skips `STR.edf`, `SETTINGS`, PLD/SAD/CSL and the rest), shows what it found per night before uploading, and leaves out nights you already have unless *Replace* is ticked, so re-inserting the card only uploads the new nights. Uploads go in batches, oldest night first, with progress and a per-night result list. Nights are split automatically, noon to noon, and named after the evening they start. PLD/SAD/CSL files are ignored. Short CPAP sessions (under 30 min in all) more than 2 h away from the night's main recording, such as testing the machine or a mask in the afternoon or for a minute the next morning, are left out so the hours in between don't count as awake. The night page lists what was ignored. A new night with less than 1 h of recording in total is not imported at all (the upload reports it as skipped).
* **Auto-correct CPAP clock** (on by default): see below.
* Tick *Replace nights that already exist* to re-import a night.

### Clock sync and daylight saving time

The O2Ring takes its time from your phone on every sync, so the app treats it as the true clock and shifts the **ResMed** timestamps to match. ResMed machines never adjust for DST. The AirSense 10 clock has to be changed by hand. The AirSense 11 has no clock you can set and, according to user reports, never resyncs it over cellular, so it can also drift by minutes, sometimes tens of minutes over months. After a time change the CPAP is often exactly an hour off.

When a night has both devices, the app estimates the offset like this:

1. **Morning anchor.** You take the mask and the ring off within a minute or two of each other, so the gap between the last CPAP session's end and the ring's last reading shows the clock error to within a few minutes. When that gap is within 10 min of a whole hour (no error, or a missed DST change), the search below is narrowed to ±2 min around that hour. Without it, nights with few mask on/off markers could lock onto a chance cluster of movement 45 or 95 min away. If the gap is not near a whole hour (drift, or the ring kept on after the mask), the full search is used.
2. **Mask on/off vs. ring motion**. Each BRP file starts and ends when the mask goes on or comes off, and your hands move the ring at that moment. The app tries every shift up to ±2 h, covering drift plus a missed DST hour, and picks the one that puts the most ring motion on those instants. It's accurate to about the ring's 4-s sampling. Mid-night bathroom breaks give the sharpest markers. For a guaranteed marker, shake your ring hand when you press Start.
3. **SpO₂ response to apneas** (cross-check). The average finger SpO₂ low point should come about 30 s after a ResMed event ends. It only confirms the estimate to about ±15 s.

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
