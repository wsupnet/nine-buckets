# Deploying on a NAS

[← README](README.md) · [User guide](docs/USER-GUIDE.md) ·
[Technical documentation](docs/DOCUMENTATION.md) · [Architecture](ARCHITECTURE.md)

Building from source on the NAS with Docker Compose. The paths below use `/volume1/docker/`; adjust them to your NAS.

---

## 1. Copy the source to the NAS

Put the whole project folder on the NAS, for example at:

```
/volume1/docker/networth/
```

It must contain `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `.env.example`, and the
`app/`, `templates/` and `static/` folders. Copy it with `scripts/deploy.sh` (see **Update**
below — it works for the first copy too), or over SMB in File Manager.

Then create two folders. Both live **outside** the project folder on purpose: updating means
copying a fresh copy of the source over `/volume1/docker/networth/`, which replaces everything
inside it. Anything kept in there is deleted by an update — that is how a live database
disappears mid-upgrade.

```
/volume1/docker/networth-data/          <- the live database (survives folder replacement)
/volume1/docker/networth-backups/       <- backups             (survives folder replacement)
```

```bash
sudo mkdir -p /volume1/docker/networth-data /volume1/docker/networth-backups
```

> **Upgrading from a deployment that used `./data`?** Copy the database across *before* you
> start the new stack, or the app comes up on an empty database:
>
> ```bash
> sudo docker compose down
> sudo cp -a /volume1/docker/networth/data/. /volume1/docker/networth-data/
> ```
>
> Copy, don't move, until you've confirmed the app comes back with your data in it.

---

## 2. Find the uid that owns the folder — the step most deployments get wrong

The container writes the database into `/volume1/docker/networth-data`. A bind-mounted NAS
folder keeps **its own ownership**, so the container has to run as a user that can write to it.

SSH into the NAS (enable SSH in its settings first) and run:

```bash
ls -ln /volume1/docker/networth-data
```

The third and fourth columns are the numeric uid and gid, e.g. `1000  1000`.

Then either:

- set `user: "1000:1000"` in the compose file to match, **or**
- claim the folders for the default:
  `sudo chown -R 1000:1000 /volume1/docker/networth-data /volume1/docker/networth-backups`

If you get this wrong the app won't start, but it will tell you exactly what to do — it prints
the folder's actual owner, the uid it's running as, and both fixes. Check the container log if
it fails to start.

---

## 3. Create `.env` and set `user:`

**`.env`** — in the project folder, next to `docker-compose.yml`:

```bash
cd /volume1/docker/networth
cp .env.example .env
chmod 600 .env
```

Set `APP_PASSWORD` to your sign-in passphrase (generate one with `openssl rand -base64 24`) and
`TZ` to your timezone. Compose **refuses to start** if `APP_PASSWORD` is missing, and the app
refuses the placeholder. `.env` is never committed, and `scripts/deploy.sh` never sends or overwrites it.

**`user:`** in `docker-compose.yml` — set to the uid:gid from step 2.

---

## 4. Create the project in the Docker app

1. Open the NAS's Docker or container app and create a new **project** (compose stack).
2. Name it `networth`.
3. Set the path to `/volume1/docker/networth`.
4. Choose the option to use the existing `docker-compose.yml` in that folder. Don't paste the
   file into an editor — a pasted copy may not pick up `.env`.
5. Start it.

The first build takes a few minutes — it installs Python packages. Later starts are instant.

> If the Docker app's project UI won't accept a compose file that uses `build:`, or reports
> `Set APP_PASSWORD in .env` even though `.env` exists, deploy over SSH instead — it does the
> same thing and always reads `.env` from the project folder:
> ```bash
> cd /volume1/docker/networth
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

**Backups** land in `/volume1/docker/networth-backups/`, nightly, 30 days kept — outside the
project folder on purpose, so replacing that folder during an update can't delete them.

Two things to back up off the NAS entirely: `/volume1/docker/networth-data` (the live database)
and `/volume1/docker/networth-backups`. Or just use **History → Backups & export → Download**
now and then, which pulls a single self-contained file to whatever device you're on.

**Restore** a backup — easiest from the app: **History → Backups & export → Restore**, type
`RESTORE` to confirm. Your current data is saved as a `pre-restore` copy first, and the file is
integrity-checked before anything is overwritten, so it's reversible.

From the command line if the app won't start:

```bash
sudo docker compose down
cp /volume1/docker/networth-backups/networth-YYYY-MM-DD.db /volume1/docker/networth-data/networth.db
sudo docker compose up -d
```

### Update after changing the code

**1. Back up first, on the NAS.** Stop the container so the database isn't being written, then
copy the data folder:

```bash
cd /volume1/docker/networth
sudo docker compose stop
sudo cp -a /volume1/docker/networth-data /volume1/docker/networth-data.pre-update-$(date +%F)
```

Copy, don't move. Delete the dated copy once the updated app is back with your data in it.

**2. Copy the code from your computer** with `scripts/deploy.sh`. One-time setup:

```bash
cp deploy.local.env.example deploy.local.env
```

Then set `NAS_TARGET=user@your-nas:/volume1/docker/networth` in it. `deploy.local.env` is
git-ignored. The script uses rsync over SSH, so SSH must be enabled on the NAS and that user
must be able to write to the project folder.

Dry run first — it lists what would change and transfers nothing:

```bash
scripts/deploy.sh
```

If the list looks right, copy for real:

```bash
scripts/deploy.sh --go
```

The script never sends `.env`, `.env.*` (except `.env.example`), `data/`, `backups/`,
`backup/`, `.git/`, `.venv*`, `node_modules/`, `__pycache__/`, `.claude/`, `.DS_Store`,
`*.local.md` or `*.local.env`. It never deletes anything on the NAS either, so a file you remove
locally stays there until you delete it by hand. The NAS's own `.env` is left alone.

**3. Rebuild on the NAS.** After `--go`, the script prints these commands; it doesn't run them:

```bash
cd /volume1/docker/networth
sudo docker compose up -d --build
sudo docker compose logs --tail 50
```

If you copy over SMB in Finder instead, drag in `app/`, `templates/`, `static/`, `Dockerfile`,
`requirements.txt`, `.env.example` and `docker-compose.yml` — never your local `.env`.

If the app ever comes up **blank** after an update, don't re-enter anything: the data volume
didn't mount. The startup log says so explicitly, automatic backups pause themselves so your
good copies aren't overwritten, and **History → Backups & export** shows an item count against
every backup so you can see which ones still have your data in them.

As well as the folder copy in step 1 above, you can take an off-NAS copy before any update:
**History → Backups & export → Back up now**, then **Download**. Backups live in `/volume1/docker/networth-backups/`, deliberately
outside the project folder, so replacing the folder can't take them with it.

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
| `APP_PASSWORD is still the placeholder` | Step 3 not done | Set a real passphrase in `.env` |
| `Set APP_PASSWORD in .env` (compose error) | No `.env`, or the UI didn't read it | Create `.env` (step 3), or deploy over SSH from the project folder |
| Port 8000 already used | Something else on the NAS has it | Change to `"8001:8000"` in the compose file |
| Prices show "not fetched" | NAS can't reach the internet, or the ticker is wrong | Check outbound access; try Refresh all prices |
| App is blank after an update — everything gone | The `/data` volume didn't mount, so it started on an empty database | `docker logs networth` says so outright. Don't re-enter anything: **History → Backups & export**, restore the newest backup with a non-zero item count. Automatic backups pause while empty, so nothing was overwritten. |
| Restored a backup and a page 500s | Old backup, older schema | Fixed — restore now re-runs the schema migration. Rebuild if you're on an older image. |
| Signed out after every restart | `data` folder not persisting the session secret | The log names the path and the uid — usually `user:` doesn't match the folder owner (step 2) |
| `APP_PASSWORD is not set` after it had been working | Started with a different compose file, or without one | Run from the project folder: `cd /volume1/docker/networth && sudo docker compose up -d --build` |
