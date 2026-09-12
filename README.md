# herdr-sprout

Official [herdr](https://herdr.dev) plugin for [sprout](https://github.com/simpros/sprout):
per-worktree Postgres isolation. On `worktree.created` it provisions a dedicated
`sprout_wt_*` database + login and injects credentials into a configurable env
file; on `worktree.removed` it drops them.

## Install

```bash
herdr plugin install simpros/herdr-sprout
herdr plugin config-dir sprout.worktree-db
```

Requires:

- herdr ≥ 0.8.0
- Python 3.10+
- `sprout` CLI with `worktree-db provision|drop` (sprout ≥ 0.5.0 / #85)
- A Postgres admin DSN with `CREATEROLE` (and ability to create databases)

### sprout CLI on Linux

The published `sprout-linux-x64` release asset is currently musl-linked and does
not run on typical Debian/Ubuntu glibc hosts ([sprout#117](https://github.com/simpros/sprout/issues/117)).
Until that is fixed, build from source:

```bash
git clone https://github.com/simpros/sprout.git
cd sprout
bun install
bun build apps/cli/src/index.ts --compile \
  --define SPROUT_CLI_VERSION='"from-source"' \
  --outfile ~/.local/bin/sprout
```

Point `config.json` → `cli` at that binary if it is not on `PATH`.

## Configure

```bash
CFG="$(herdr plugin config-dir sprout.worktree-db)"
cp config.example.json "$CFG/config.json"
cp secrets.example.env "$CFG/secrets.env"
chmod 600 "$CFG/secrets.env"
$EDITOR "$CFG/config.json" "$CFG/secrets.env"
```

### `secrets.env` (mode 0600)

| Variable | Purpose |
| --- | --- |
| `SPROUT_WORKTREE_ADMIN_URL` | Admin DSN used for `provision` / `drop` / `gc` (`CREATEROLE`) |
| `SPROUT_PREVIEW_OWNER_URL` | Optional; owner role for `attach-preview` |
| `SPROUT_URL` / `SPROUT_ADMIN_TOKEN` | Optional; needed for `sprout list` in preview mode |
| `SPROUT_PG_HOST` / `SPROUT_PG_PORT` | Optional host/port overrides when writing preview env |

Never put secrets in the world-readable `config.json`.

**Network note:** how the host reaches the admin DSN is the operator's problem.
Host-side herdr worktrees usually need a published or forwarded Postgres port
(for example a loopback forwarder in front of a PaaS-managed instance).

### `config.json`

Per-repo entries keyed by `main_repo` path (same idea as `tdi.worktree-setup`):

```json
{
  "cli": "sprout",
  "bun": "bun",
  "repos": [
    {
      "name": "myapp",
      "main_repo": "~/repositories/myapp",
      "worktrees_root": "~/.herdr/worktrees/myapp",
      "env_files": [".env"],
      "renames": {
        "PGHOST": "DATABASE_HOST",
        "PGPORT": "DATABASE_PORT",
        "PGDATABASE": "DATABASE_NAME",
        "PGUSER": "DATABASE_USER",
        "PGPASSWORD": "DATABASE_PASSWORD"
      },
      "requires_node_modules": true,
      "steps": [
        { "cmd": ["{bun}", "run", "db:migrate"] },
        { "cmd": ["{bun}", "run", "db:bootstrap"], "as_admin": true }
      ]
    }
  ]
}
```

- **Slug** comes from the worktree directory basename (sprout grammar: lowercase,
  `[^a-z0-9-]` → `-`, max 40), with a short suffix on collision. Prefer path
  basenames over branch names so multiple repos can share one Postgres.
- **`renames`** map sprout's canonical `PG*` / `DATABASE_URL` keys to app names.
- **`steps`** run after provision (migrate before bootstrap). Use `"as_admin": true`
  for steps that need `CREATEROLE`. When `requires_node_modules` is true and
  `node_modules` is missing, steps are skipped with a clear log line.
- Env writes are atomic (temp + rename). Re-provision keeps the existing password.

## Ordering with `.env`-copying setup plugins

Two plugins on `worktree.created` have **no ordering guarantee**. If a setup
plugin copies the main repo's `.env*` *after* this plugin injects credentials,
the worktree silently falls back to the shared database.

**Recommended** when a setup plugin is present: call provision as an ordered
step *after* the copies (two-phase, both idempotent). Symlink the CLI onto
your `PATH` once after install:

```bash
# managed checkout path appears in `herdr plugin list`
ln -sf /path/to/herdr-managed/herdr-sprout/bin/sprout-worktree-db ~/.local/bin/sprout-worktree-db
```

```toml
# in tdi.worktree-setup (or similar) config
steps = [
  'cp "$HERDR_MAIN_REPO"/.env* . 2>/dev/null || true',
  # DB + env injection only — must not be lost if install fails
  'sprout-worktree-db provision --worktree "$HERDR_WORKTREE" --no-steps --settle 0',
  'bun install',
  # re-ensure + migrate/bootstrap once deps exist
  'sprout-worktree-db provision --worktree "$HERDR_WORKTREE" --settle 0',
]
```

You can also run `herdr plugin action invoke provision --plugin sprout.worktree-db`
from a workspace context. The created hook still re-asserts injected keys after
a short settle window (`SPROUT_WT_SETTLE`, default `1.5` seconds) when a
competing writer is detected.

## Manual escape hatch

Event hooks never fire for worktrees that predate the plugin. Use paths
explicitly:

```bash
PLUGIN_ROOT=…   # herdr-managed checkout, or this repo when linked
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" provision --worktree /path/to/wt
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" drop --worktree /path/to/wt
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" gc --dry-run
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" status
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" attach-preview --worktree /path/to/wt
```

Herdr actions (from a workspace context): `provision`, `drop`, `gc`, `status`,
`attach-preview`.

## Teardown and GC

- `worktree.removed` drops only `sprout_wt_*` objects for dedicated databases.
- `drop` refuses shared preview databases (`attach-preview` mode) and says so.
- Out-of-band `git worktree remove` emits no herdr event. Each `provision` reaps
  tracked DBs for that repo whose path is absent from `git worktree list`.
- Schedule `gc` (supports `--dry-run`) for a full pass. With `psql` on `PATH`,
  `gc` also scans Postgres for `sprout_wt_*` orphans.

## Preview mode

Worktrees are usually created before a PR exists, so a dedicated DB is the
default. Once a preview deployment exists, `attach-preview` resolves
branch → MR/PR → `sprout list` and points the env file at
`sprout_<slug>_pr<id>` instead.

## Development

```bash
herdr plugin link /path/to/herdr-sprout
python3 -m unittest discover -s test -v
```
