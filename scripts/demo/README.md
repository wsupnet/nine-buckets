# Demo video

How `docs/demo/networth-demo.mp4` and `.gif` are made. Everything runs in Docker
against a **separate, throwaway container** filled with made-up data, so your real
database is never touched or shown.

| File | What it does |
|---|---|
| `seed-demo.py` | Adds fictional purchase lots and two years of monthly snapshots on top of the app's `SEED_DEMO` data |
| `make-sample-sheet.py` | Writes a small fictional trade-history workbook for the import scene |
| `record.mjs` | Drives the app in headless Chromium (real clicks, fake cursor, captions) and saves a screencast |
| `encode.py` | Turns the screencast frames into the MP4 and the README GIF |
| `Dockerfile` | Recorder image: Playwright's Chromium + ffmpeg |

## Regenerate it

Run from the repo root. Nothing is installed on the host besides Docker. The demo
container has its own image tag (`networth:demo`), volume and network, and only
listens on `127.0.0.1:3941`, so your real image and data are left alone. The
passphrase below is a throwaway for the demo container only.

```bash
# 1. Build and start the demo app on a fresh volume, with the sample data and live prices
docker build -t networth:demo .
docker network create networth-demo-net
docker run -d --name networth-demo --network networth-demo-net -p 127.0.0.1:3941:8000 \
  -v networth-demo-data:/data -e APP_PASSWORD=demo-passphrase-2026 -e SEED_DEMO=1 \
  -e TZ=UTC -e BACKUP_DIR=/data/backups networth:demo
# wait for "price-refresh ok" in: docker logs networth-demo

# 2. Add the fictional lots and history, and write the sample workbook
docker exec -i networth-demo python - < scripts/demo/seed-demo.py
docker exec -i networth-demo python - < scripts/demo/make-sample-sheet.py
mkdir -p demo-out && docker cp networth-demo:/tmp/demo-trades.xlsx demo-out/

# 3. Build the recorder and record (SHOTS=1 for a screenshot-per-scene dry run)
docker build -t networth-recorder scripts/demo
docker run --rm --network networth-demo-net -v "$PWD/scripts/demo:/rec/src:ro" -v "$PWD/demo-out:/out" \
  -e DEMO_SHEET=/out/demo-trades.xlsx \
  networth-recorder sh -c "cp src/record.mjs . && node record.mjs && python3 src/encode.py /out"

# 4. Copy the results into docs/demo/, then tear the demo down
cp demo-out/networth-demo.mp4 demo-out/networth-demo.gif docs/demo/
docker rm -f networth-demo && docker volume rm networth-demo-data && docker network rm networth-demo-net
rm -rf demo-out
```

The walkthrough edits balances, moves money, adds a lot, changes two targets and
takes a snapshot, so start from a fresh volume (step 1) before recording again.

## What the walkthrough avoids

Nothing at the moment. The issues the script used to steer around (editing a
property, undoing a target edit, cross-currency moves, reversing a holding move,
overselling, the Allocation design-note card and `$-91`-style negatives) are fixed,
so a re-recording is free to show them.
