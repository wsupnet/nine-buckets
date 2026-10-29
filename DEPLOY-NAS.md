# Deploying on a NAS

[← README](README.md) · [User guide](docs/USER-GUIDE.md) ·
[Technical documentation](docs/DOCUMENTATION.md) · [Architecture](ARCHITECTURE.md)

Building from source on the NAS with Docker Compose, deployed with `git pull`. The paths below use `/volume1/docker/`; adjust them to your NAS.

---

## 1. Clone the repo and create the data folders

The code and the data live in separate trees:

```
/volume1/docker/apps/networth/              <- this repo (git clone; updated with git pull)
/volume1/docker/appdata/networth/data/      <- the live database and session secret -> /data
/volume1/docker/appdata/networth/backups/   <- backups                              -> /backups
```

The data folders are deliberately **outside** the repo, so nothing an update does to the code
folder can touch them. Compose finds them through `APPDATA` in `.env` (step 3) and mounts
`$APPDATA/networth/data` and `$APPDATA/networth/backups`.

```bash
sudo mkdir -p /volume1/docker/apps
cd /volume1/docker/apps
sudo git clone <your-repo-url> networth

sudo mkdir -p /volume1/docker/appdata/networth/data /volume1/docker/appdata/networth/backups
```

Create the data folders yourself before the first start. If they're missing, Docker creates
them owned by root and the app can't write to them.

> **Moving an existing deployment** from `/volume1/docker/networth` (with its data in
> `/volume1/docker/networth-data` and `/volume1/docker/networth-backups`)? Stop the old stack
> and copy the data across *before* starting the new one, or the app comes up on an empty
> database:
>
> ```bash
> cd /volume1/docker/networth && sudo docker compose down
> sudo cp -a /volume1/docker/networth-data/.    /volume1/docker/appdata/networth/data/
> sudo cp -a /volume1/docker/networth-backups/. /volume1/docker/appdata/networth/backups/
> sudo cp -a /volume1/docker/networth/.env      /volume1/docker/apps/networth/.env
> ```
>
> Then add `APPDATA=/volume1/docker/appdata` to the copied `.env` (step 3). Copy, don't move,
> until the app is back with your data in it; then delete the old folders by hand.

---

## 2. Find the uid that owns the folders — the step most deployments get wrong

The container writes into `/volume1/docker/appdata/networth/data`. A bind-mounted NAS folder
keeps **its own ownership**, so the container has to run as a user that can write to it.

SSH into the NAS (enable SSH in its settings first) and run:

```bash
ls -ln /volume1/docker/appdata/networth
```

The third and fourth columns are the numeric uid and gid, e.g. `1000  1000`.

`docker-compose.yml` runs as `user: "1000:1000"`. The simplest fix is to give the folders to
that uid:

```bash
sudo chown -R 1000:1000 /volume1/docker/appdata/networth
```

You can change `user:` in the compose file instead, but it's a tracked file: a local edit there
makes the next `git pull` stop with a conflict. Chowning the folders avoids that.

If you get this wrong the app won't start, but it will tell you exactly what to do — it prints
the folder's actual owner, the uid it's running as, and both fixes. Check the container log if
it fails to start.

---

## 3. Create `.env`

In the repo folder, next to `docker-compose.yml`. If you copied your old `.env` across in
step 1, skip the `cp` (it would overwrite it) and just add `APPDATA`:

```bash
cd /volume1/docker/apps/networth
sudo cp .env.example .env
sudo chmod 600 .env
```

Set:

- `APP_PASSWORD` — your sign-in passphrase (generate one with `openssl rand -base64 24`).
- `APPDATA=/volume1/docker/appdata` — the parent of `networth/data` and `networth/backups`.
- `TZ` — your timezone.

Compose **refuses to start** if `APP_PASSWORD` or `APPDATA` is missing, and the app refuses the
placeholder passphrase. `.env` is git-ignored, so `git pull` never touches it.

---

## 4. Create the project in the Docker app

1. Open the NAS's Docker or container app and create a new **project** (compose stack).
2. Name it `networth` (the compose file sets `name: networth` too).
3. Set the path to `/volume1/docker/apps/networth`.
4. Choose the option to use the existing `docker-compose.yml` in that folder. Don't paste the
   file into an editor — a pasted copy may not pick up `.env`.
5. Start it.

The first build takes a few minutes — it installs Python packages. Later starts are instant.

> If the Docker app's project UI won't accept a compose file that uses `build:`, or reports
> `Set APP_PASSWORD in .env` / `APPDATA is not set` even though `.env` exists, deploy over SSH
> instead — it does the same thing and always reads `.env` from the project folder:
> ```bash
> cd /volume1/docker/apps/networth
> sudo docker compose up -d --build
> ```

---

## 5. Open it

```
http://<your-nas-ip>:8000
```

Sign in with the passphrase from your `.env`.

To reach it from your phone off the home network, use Tailscale on the NAS rather than
port-forwarding 8000 to the internet.

---

## Verifying it's healthy

```bash
sudo docker ps --filter name=networth        # should say (healthy)
sudo docker logs -f networth                 # follow the log
```

A healthy startup logs three lines:

```
networth.jobs: price refresh: every 24h (on start: True)
networth.jobs: backups: every 24h into /backups, keeping 30 days
networth.jobs: backup ok: {'file': 'networth-YYYY-MM-DD.db', ...}
```

---

## Day to day

**Backups** land in `/volume1/docker/appdata/networth/backups/`, nightly, 30 days kept —
outside the repo on purpose, so nothing an update does to the code can delete them. Each backup
is a single self-contained `.db` file written with SQLite's `VACUUM INTO`; nothing else should
appear beside it.

Two things to back up off the NAS entirely: `/volume1/docker/appdata/networth/data` (the live
database) and `/volume1/docker/appdata/networth/backups`. Or just use **History → Backups &
export → Download** now and then, which pulls a single self-contained file to whatever device
you're on.

**Restore** a backup — easiest from the app: **History → Backups & export → Restore**, type
`RESTORE` to confirm. Your current data is saved as a `pre-restore` copy first, and the file is
integrity-checked before anything is overwritten, so it's reversible.

From the command line if the app won't start. Keep the current database aside first, and
remove its `-wal`/`-shm`: a leftover WAL from the old database would be replayed on top of the
restored one.

```bash
cd /volume1/docker/apps/networth
sudo docker compose down
D=/volume1/docker/appdata/networth/data
sudo cp -a $D/networth.db $D/networth.db.before-restore-$(date +%F)
sudo rm -f $D/networth.db-wal $D/networth.db-shm
sudo cp /volume1/docker/appdata/networth/backups/networth-YYYY-MM-DD.db $D/networth.db
sudo docker compose up -d
```

### Update

**1. Back up first, on the NAS.** Stop the container so the database isn't being written, then
copy the data folder:

```bash
cd /volume1/docker/apps/networth
sudo docker compose stop
sudo cp -a /volume1/docker/appdata/networth/data \
           /volume1/docker/appdata/networth/data.pre-update-$(date +%F)
```

Copy, don't move. Delete the dated copy once the updated app is back with your data in it.

**2. Pull and rebuild:**

```bash
cd /volume1/docker/apps/networth
sudo git pull
sudo docker compose up -d --build
sudo docker compose logs --tail 50
```

`git pull` only changes tracked files. `.env` and the data folders are never in the repo, so
an update can't overwrite or delete them. If `git pull` stops with "local changes would be
overwritten", someone edited a tracked file on the NAS (usually `docker-compose.yml`): see
`git status`, then put the setting in `.env` or the folder ownership instead (step 2).

If the app ever comes up **blank** after an update, don't re-enter anything: the data volume
didn't mount. The startup log says so explicitly, automatic backups pause themselves so your
good copies aren't overwritten, and **History → Backups & export** shows an item count against
every backup so you can see which ones still have your data in them. Check that `APPDATA` in
`.env` points at `/volume1/docker/appdata`.

As well as the folder copy in step 1 above, you can take an off-NAS copy before any update:
**History → Backups & export → Back up now**, then **Download**.

`scripts/deploy.sh` (rsync from your computer) still works as a fallback for a NAS folder that
isn't a git clone. Don't use it on the clone: it would leave the working tree out of step with
git.

### Clean up stray `-wal` / `-shm` files in the backups folder

Backups taken before the switch to `VACUUM INTO` were WAL-mode files, and opening one left
`-wal`/`-shm` files beside it that retention never removed. New backups don't do this, and
retention now removes any sidecars along with each old `.db`. To clear the leftovers once,
on the NAS — dry run first, it only lists the files that have no matching `.db`:

```bash
sudo sh -c 'cd /volume1/docker/appdata/networth/backups && for f in *.db-wal *.db-shm; do [ -e "$f" ] || continue; [ -e "${f%-*}" ] || echo "$f"; done'
```

Then delete exactly those:

```bash
sudo sh -c 'cd /volume1/docker/appdata/networth/backups && for f in *.db-wal *.db-shm; do [ -e "$f" ] || continue; [ -e "${f%-*}" ] || rm -v -- "$f"; done'
```

Sidecars that still have their `.db` are left alone; they go when that backup is deleted or
ages out.

**Clear all data** and start fresh (keeps your nine buckets):

```bash
sudo docker compose run --rm networth python -m app.reset --yes
```

---

## If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Container restarts in a loop | Placeholder password, or unwritable data folder | `docker logs networth` — the error names the exact fix |
| `PermissionError: '/srv/app/main.py'` | Source files copied to the NAS have owner-only permissions, baked into an older image | Rebuild: `docker compose up -d --build`. The Dockerfile now normalises file modes. |
| `data directory is not writable` | `user:` doesn't match the folder owner | Step 2 |
| `APPDATA is not set` (compose error) | `.env` missing `APPDATA`, or the UI didn't read `.env` | Add `APPDATA=/volume1/docker/appdata` to `.env` (step 3), or deploy over SSH |
| `APP_PASSWORD is still the placeholder` | Step 3 not done | Set a real passphrase in `.env` |
| `Set APP_PASSWORD in .env` (compose error) | No `.env`, or the UI didn't read it | Create `.env` (step 3), or deploy over SSH from the project folder |
| Port 8000 already used | Something else on the NAS has it | Change to `"8001:8000"` in the compose file |
| Prices show "not fetched" | NAS can't reach the internet, or the ticker is wrong | Check outbound access; try Refresh all prices |
| App is blank after an update — everything gone | The `/data` volume didn't mount, so it started on an empty database | `docker logs networth` says so outright. Don't re-enter anything: **History → Backups & export**, restore the newest backup with a non-zero item count. Automatic backups pause while empty, so nothing was overwritten. |
| Restored a backup and a page 500s | Old backup, older schema | Fixed — restore now re-runs the schema migration. Rebuild if you're on an older image. |
| Signed out after every restart | Data folder not persisting the session secret | The log names the path and the uid — usually `user:` doesn't match the folder owner (step 2) |
| `APP_PASSWORD is not set` after it had been working | Started with a different compose file, or without one | Run from the project folder: `cd /volume1/docker/apps/networth && sudo docker compose up -d --build` |
