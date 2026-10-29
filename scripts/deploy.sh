#!/usr/bin/env bash
# Copy this project to the NAS with rsync.
#
#   scripts/deploy.sh         dry run: show what would change, transfer nothing
#   scripts/deploy.sh --go    actually transfer
#
# The target comes from NAS_TARGET (user@host:/path), read from deploy.local.env
# when that file exists, otherwise from the environment.
#
# Never deletes anything on the NAS (no --delete), and never sends secrets, data,
# backups or local tooling — see EXCLUDES. Works with the openrsync that ships
# with macOS, so it sticks to common flags.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/deploy.local.env"

GO=0
case "${1:-}" in
  "") ;;
  --go) GO=1 ;;
  -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) echo "Unknown argument: $1 (use --go to transfer, or no argument for a dry run)" >&2; exit 2 ;;
esac

# Read NAS_TARGET from the file without sourcing it, so the file can't run code.
if [[ -f "$ENV_FILE" ]]; then
  from_file="$(sed -n 's/^[[:space:]]*NAS_TARGET[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" \
    | tail -n 1 | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")"
  if [[ -n "$from_file" ]]; then
    NAS_TARGET="$from_file"
  fi
fi

if [[ -z "${NAS_TARGET:-}" ]]; then
  echo "NAS_TARGET is not set." >&2
  echo "  Create deploy.local.env from deploy.local.env.example, or export" >&2
  echo "  NAS_TARGET=user@host:/path before running this script." >&2
  exit 1
fi
if [[ ! "$NAS_TARGET" =~ ^[^@:[:space:]]+@[^:[:space:]]+:/[^[:space:]]+$ ]]; then
  echo "NAS_TARGET must look like user@host:/absolute/path (got: $NAS_TARGET)" >&2
  exit 1
fi

EXCLUDES=(
  .env ".env.*"
  data/ dev-data/ backups/ backup/
  .git/ ".venv*/" node_modules/ __pycache__/
  .claude/ .DS_Store
  "*.local.md" "*.local.env"
)

ARGS=(-av)
# The NAS needs .env.example to create its .env from; ".env.*" would drop it.
ARGS+=(--include=.env.example)
for pattern in "${EXCLUDES[@]}"; do
  ARGS+=("--exclude=$pattern")
done

if [[ $GO -eq 0 ]]; then
  echo "DRY RUN — nothing will be transferred. Re-run with --go to copy for real."
  echo "Target: $NAS_TARGET"
  echo
  rsync -n "${ARGS[@]}" "$ROOT/" "$NAS_TARGET/"
  exit 0
fi

echo "Copying to $NAS_TARGET"
rsync "${ARGS[@]}" "$ROOT/" "$NAS_TARGET/"

host="${NAS_TARGET%%:*}"
path="${NAS_TARGET#*:}"
cat <<EOF

Transfer complete. Nothing has been restarted. On the NAS, run:

  ssh $host
  cd $path
  sudo docker compose up -d --build
  sudo docker compose logs --tail 50
EOF
