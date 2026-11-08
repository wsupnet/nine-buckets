# Deploying on a NAS

[← README](README.md) · [User guide](docs/USER-GUIDE.md) ·
[Technical documentation](docs/DOCUMENTATION.md) · [Architecture](ARCHITECTURE.md)

Building from source on the NAS with Docker Compose, deployed with `git pull`. The paths below use `/volume1/docker/`; adjust them to your NAS.

---

## 1. Clone the repo and create the data folder

The code and the data live in separate trees:

```
/volume1/docker/apps/networth/              <- this repo (git clone; updated with git pull)
/volume1/docker/appdata/networth/data/      <- ALL your data -> /data
    networth.db, .session_secret               the live database and session secret
    backups/                                   nightly backups
```

On a NAS the data folder is deliberately **outside** the repo, so nothing an update does to the
code folder (including a NAS Docker app that replaces it) can touch it. Compose finds it through
`NETWORTH_DATA` in `.env` (step 3). Without that line it would use `data/` inside the repo.

```bash
sudo mkdir -p /volume1/docker/apps
cd /volume1/docker/apps
sudo git clone <your-repo-url> networth

sudo mkdir -p /volume1/docker/appdata/networth/data
```

Create the data folder yourself before the first start. If it's missing, Docker creates it
owned by root and the app can't write to it.

> **Already running an older version?** See
> [Upgrading from the APPDATA layout](#upgrading-from-the-appdata-layout) before you pull.

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

In the repo folder, next to `docker-compose.yml`. If you already have a `.env`, skip the `cp` (it would overwrite it) and just set `NETWORTH_DATA`:

```bash
cd /volume1/docker/apps/networth
sudo cp .env.example .env
sudo chmod 600 .env
```

Set:

- `APP_PASSWORD` — your sign-in passphrase. Optional: leave it empty and one is generated on
  first start and printed in the log (see step 5).
- `NETWORTH_DATA=/volume1/docker/appdata/networth/data` — the one folder that holds your data.
- `TZ` — your timezone.

The app refuses a short or placeholder passphrase. If you forget `NETWORTH_DATA`, the app starts on a new, empty database in the repo's
`data/` folder, and the startup log says so. `.env` is git-ignored, so `git pull` never touches it.

---

## 4. Create the project in the Docker app

1. Open the NAS's Docker or container app and create a new **project** (compose stack).
2. Name it `networth` (the compose file sets `name: networth` too).
3. Set the path to `/volume1/docker/apps/networth`.
4. Choose the option to use the existing `docker-compose.yml` in that folder. Don't paste the
   file into an editor — a pasted copy may not pick up `.env`.
5. Start it.

The first build takes a few minutes — it installs Python packages. Later starts are instant.

> If the Docker app's project UI won't accept a compose file that uses `build:`, or ignores your
> `.env` (the log shows a generated passphrase or a new, empty database), deploy over SSH
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

Sign in with the passphrase from your `.env`. If you left it empty, the generated one is in the
log (`sudo docker logs networth`) and in `/volume1/docker/appdata/networth/data/.app_password`.

To reach it from your phone off the home network, use Tailscale on the NAS rather than
port-forwarding 8000 to the internet.

---

## Verifying it's healthy

```bash
sudo docker ps --filter name=networth        # should say (healthy)
sudo docker logs -f networth                 # follow the log
```

A healthy startup logs these lines. Check the host folder and that the item count isn't 0:

```
networth: Using the database at /data/networth.db (host folder: /volume1/docker/appdata/networth/data), 42 item(s)
networth.jobs: price refresh: every 24h (on start: True)
networth.jobs: backups: every 24h into /data/backups, keeping 30 days
networth.jobs: backup ok: {'file': 'networth-YYYY-MM-DD.db', ...}
```

On the very first start it says `No database found … so a new, empty one was created` instead.
That's expected once. If you see it later, stop and check `NETWORTH_DATA`.

---

## Day to day

**Backups** land in `/volume1/docker/appdata/networth/data/backups/`, nightly, 30 days kept,
outside the repo on purpose, so nothing an update does to the code can delete them. Each backup
is a single self-contained `.db` file written with SQLite's `VACUUM INTO`; nothing else should
appear beside it.

To back up off the NAS entirely, copy one folder: `/volume1/docker/appdata/networth/data`
(the live database and its backups). Or just use **History → Backups &
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
sudo cp $D/backups/networth-YYYY-MM-DD.db $D/networth.db
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
isn't the one it used before. The startup log says so explicitly and names the host folder,
automatic backups pause themselves so your good copies aren't overwritten, and **History →
Backups & export** shows an item count against every backup so you can see which ones still
have your data in them. Check that `NETWORTH_DATA` in `.env` is
`/volume1/docker/appdata/networth/data`.

As well as the folder copy in step 1 above, you can take an off-NAS copy before any update:
**History → Backups & export → Back up now**, then **Download**.

`scripts/deploy.sh` (rsync from your computer) still works as a fallback for a NAS folder that
isn't a git clone. Don't use it on the clone: it would leave the working tree out of step with
git.

### Upgrading from the APPDATA layout

Older versions used two folders, found through `APPDATA` in `.env`:
`/volume1/docker/appdata/networth/data` and `/volume1/docker/appdata/networth/backups`. The
current version uses **one** folder, found through `NETWORTH_DATA`, with backups inside it. The
name changed because Windows defines `APPDATA` for every user, which silently overrode `.env`.

The live database stays exactly where it is; only the backups folder moves. Do it once:

```bash
cd /volume1/docker/apps/networth
sudo docker compose down
A=/volume1/docker/appdata/networth

# 1. Safety copy of everything, before touching anything.
sudo cp -a $A $A.pre-upgrade-$(date +%F)

# 2. Move the backups into the data folder (refuses if data/backups already exists).
[ -e $A/data/backups ] && echo "STOP: $A/data/backups already exists" || sudo mv $A/backups $A/data/backups
```

3. Edit `.env`: delete the `APPDATA=` line and add
   `NETWORTH_DATA=/volume1/docker/appdata/networth/data`.

4. Pull and start:

   ```bash
   sudo git pull
   sudo docker compose up -d --build
   sudo docker compose logs --tail 30
   ```

5. Check the log says `Using the database at … (host folder: /volume1/docker/appdata/networth/data)`
   with your usual item count, and that **History → Backups & export** lists your old backups.
   Then delete the `networth.pre-upgrade-…` copy by hand.

If you skip step 3, the app starts on an empty database in the repo's `data/` folder, and the
log has a boxed warning telling you exactly which line to add. Nothing of yours is changed or
overwritten in that case: stop the app, do step 3, and start it again.

### Clean up stray `-wal` / `-shm` files in the backups folder

Backups taken before the switch to `VACUUM INTO` were WAL-mode files, and opening one left
`-wal`/`-shm` files beside it that retention never removed. New backups don't do this, and
retention now removes any sidecars along with each old `.db`. To clear the leftovers once,
on the NAS — dry run first, it only lists the files that have no matching `.db`:

```bash
sudo sh -c 'cd /volume1/docker/appdata/networth/data/backups && for f in *.db-wal *.db-shm; do [ -e "$f" ] || continue; [ -e "${f%-*}" ] || echo "$f"; done'
```

Then delete exactly those:

```bash
sudo sh -c 'cd /volume1/docker/appdata/networth/data/backups && for f in *.db-wal *.db-shm; do [ -e "$f" ] || continue; [ -e "${f%-*}" ] || rm -v -- "$f"; done'
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
| Log: `.env still sets APPDATA=… (the old layout)` | `.env` from before the one-folder layout | [Upgrading from the APPDATA layout](#upgrading-from-the-appdata-layout) |
| Log: `No database found … new, empty one was created` after the first day | `NETWORTH_DATA` missing or pointing at the wrong folder | Stop the app; set `NETWORTH_DATA` (step 3). Don't enter data into the empty one |
| `APP_PASSWORD is still the placeholder` | Step 3 not done | Set a real passphrase in `.env`, or delete the line to have one generated |
| Port 8000 already used | Something else on the NAS has it | Change to `"8001:8000"` in the compose file |
| Prices show "not fetched" | NAS can't reach the internet, or the ticker is wrong | Check outbound access; try Refresh all prices |
| App is blank after an update — everything gone | `/data` isn't your data folder, so it started on an empty database | `docker logs networth` says so outright. Don't re-enter anything: **History → Backups & export**, restore the newest backup with a non-zero item count. Automatic backups pause while empty, so nothing was overwritten. |
| Restored a backup and a page 500s | Old backup, older schema | Fixed — restore now re-runs the schema migration. Rebuild if you're on an older image. |
| Signed out after every restart | Data folder not persisting the session secret | The log names the path and the uid — usually `user:` doesn't match the folder owner (step 2) |
| Your passphrase stopped working; log says a passphrase was generated | `.env` wasn't read: started with a different compose file, or from another folder | Run from the project folder: `cd /volume1/docker/apps/networth && sudo docker compose up -d --build`. |
