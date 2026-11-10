"""`make reset-local`: put this checkout's local state back to a fresh clone's, deleting nothing.

    make reset-local                 # do it
    make reset-local DRY_RUN=1       # show what it would move; change nothing

Since PLAN PR 15c the payments ledger (`data/payments_ledger.jsonl`) is a source of truth
for debts, and the request records survive restarts. So `git restore scooter_db.json` on its
own is NOT a reset: it's a rollback (see AGENTS.md "What a rollback erases"), after which
reconcile rebuilds old debts from the ledger and `rollback-check` lists every past ride as
erased. A real reset moves all three aside together:

1. Refuses if anything holds the DB lock right now (a server or reconcile mid-transaction).
   Stop servers that use these files first: a running server keeps appending.
2. Moves into `local-backups/<UTC timestamp>/` (git-ignored): the live `scooter_db.json`
   (copied, then restored from git), everything in `data/` except `.gitkeep` (records,
   ledger, scenario runs), and the DB's `.lock` and leftover `.scooter_db.json.*.tmp` files.
3. Restores `scooter_db.json` from git (`git restore`, i.e. the committed file).

Nothing is deleted. To undo: move the files back from the backup directory.
It acts on the default paths only; if SCOOTER_DB_PATH / SCOOTER_RECORDS_PATH /
SCOOTER_PAYMENTS_LEDGER_PATH point elsewhere it says so and leaves those alone.
"""
import argparse
import datetime
import fcntl
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB_NAME = "scooter_db.json"
ENV_VARS = ("SCOOTER_DB_PATH", "SCOOTER_RECORDS_PATH", "SCOOTER_PAYMENTS_LEDGER_PATH")


def lock_is_held(lock_path):
    if not lock_path.exists():
        return False
    with open(lock_path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def plan(root):
    """[(source, destination name, how)] for everything that moves; how is 'move' or 'copy'."""
    items = [(root / DB_NAME, DB_NAME, "copy")] if (root / DB_NAME).exists() else []
    data = root / "data"
    if data.is_dir():
        items += [(p, f"data/{p.name}", "move") for p in sorted(data.iterdir()) if p.name != ".gitkeep"]
    items += [(p, p.name, "move") for p in sorted(root.glob(f"{DB_NAME}.lock"))]
    items += [(p, p.name, "move") for p in sorted(root.glob(f".{DB_NAME}.*.tmp"))]
    return items


def main(argv=None):
    parser = argparse.ArgumentParser(prog="make reset-local", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="show what would move; change nothing")
    parser.add_argument("--root", default=str(REPO), help=argparse.SUPPRESS)       # tests point it at a scratch repo
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    for name in ENV_VARS:
        if os.environ.get(name):
            print(f"reset-local: note: {name}={os.environ[name]} is set; only the default paths are reset")
    if lock_is_held(root / f"{DB_NAME}.lock"):
        print("reset-local: the DB lock is held (a server or reconcile is mid-transaction); stop it and retry",
              file=sys.stderr)
        return 1
    stamp = _stamp()
    backup = root / "local-backups" / stamp
    n = 0
    while backup.exists():                       # two resets in one second: never overwrite a backup
        n += 1
        backup = root / "local-backups" / f"{stamp}-{n}"
    items = plan(root)
    verb = "would" if args.dry_run else "will"
    print(f"reset-local: {verb} back up {len(items)} item(s) to {os.path.relpath(backup, root)}/ and restore "
          f"{DB_NAME} from git")
    for src, dest, how in items:
        print(f"  {how:4} {os.path.relpath(src, root)} -> {os.path.relpath(backup / dest, root)}")
    if args.dry_run:
        return 0
    (backup / "data").mkdir(parents=True)
    for src, dest, how in items:
        (shutil.copy2 if how == "copy" else shutil.move)(str(src), str(backup / dest))
    out = subprocess.run(["git", "restore", "--", DB_NAME], cwd=root, capture_output=True, text=True)
    if out.returncode != 0:
        print(f"reset-local: git restore failed: {out.stderr.strip()} (the backup is in {backup})", file=sys.stderr)
        return 1
    print(f"reset-local: done. {DB_NAME} is the committed file; data/ is empty. "
          f"Undo: move the files back from {os.path.relpath(backup, root)}/.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
