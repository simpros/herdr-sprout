# herdr-sprout

Official [herdr](https://herdr.dev) plugin for [sprout](https://github.com/simpros/sprout):
per-worktree Postgres isolation. Provisions a dedicated `sprout_wt_*` database
+ login and injects credentials into a configurable env file; on
`worktree.removed` it drops them.

Auto-provision on `worktree.created` is **off by default** (`auto_provision_on_create`).
Enable it for greenfield repos with no competing `.env` copy, or call the
ordered `provision` action after setup plugins (recommended when another plugin
copies `.env*` files).

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
  "auto_provision_on_create": false,
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
        {
          "cmd": ["{bun}", "run", "db:bootstrap"],
          "as_admin": true
        }
      ]
    }
  ]
}
```

- **`auto_provision_on_create`**: when `true`, the `worktree.created` hook runs
  provision (best-effort, no settle/sleep). Default `false` — prefer an ordered
  setup step when another plugin copies `.env` files.
- **Slug** is always `{repo}-{worktree-basename}-{sha1(realpath)[:5]}`
  (sprout grammar: lowercase, `[^a-z0-9-]` → `-`, max 40). Re-provision reuses
  the key already stored in state (so passwords stay stable). `--key` only seeds
  a first claim; if a row exists with a different key, provision fails closed
  (drop/forget first). Object names for GC come from **state**, not basename
  guesses. Drop without a state row remints with the same rule when repo config
  matches, otherwise requires `--key` (drop's escape hatch).
- **`renames`** map sprout's canonical `PG*` / `DATABASE_URL` keys to app names.
- **`steps`** run after provision (migrate before bootstrap). Use `"as_admin": true`
  to inject the admin user/password into the renamed `PGUSER`/`PGPASSWORD` keys.
  Optional `admin_env` adds extra env vars for that step only (no hard-coded
  app aliases in the runner). When `requires_node_modules` is true and
  `node_modules` is missing, steps are skipped; `status` reports
  `steps_status` as `ok` | `skipped` | `failed` (not a boolean).
- Env writes are atomic (temp + rename). Re-provision keeps the existing password.

### Worktree path (herdr ≥ 0.8)

Hooks and actions resolve the checkout path from, in order:

1. `HERDR_WORKTREE`
2. event `data.worktree.path`
3. event `data.workspace.worktree.checkout_path` (or `.path`)
4. context `worktree.checkout_path` (or `.path`) from `HERDR_PLUGIN_CONTEXT_JSON`

If none are present the hook logs and skips (fail closed).

## Ordering with `.env`-copying setup plugins

Two plugins on `worktree.created` have **no ordering guarantee**. Leave
`auto_provision_on_create` false and call provision as an ordered step *after*
the copies (two-phase, both idempotent). Symlink the CLI onto your `PATH` once
after install:

```bash
# managed checkout path appears in `herdr plugin list`
ln -sf /path/to/herdr-managed/herdr-sprout/bin/sprout-worktree-db ~/.local/bin/sprout-worktree-db
```

```toml
# in tdi.worktree-setup (or similar) config
steps = [
  'cp "$HERDR_MAIN_REPO"/.env* . 2>/dev/null || true',
  # DB + env injection only — must not be lost if install fails
  'sprout-worktree-db provision --worktree "$HERDR_WORKTREE" --no-steps',
  'bun install',
  # re-ensure + migrate/bootstrap once deps exist
  'sprout-worktree-db provision --worktree "$HERDR_WORKTREE"',
]
```

You can also run `herdr plugin action invoke provision --plugin sprout.worktree-db`
from a workspace context.

## Manual escape hatch

Event hooks never fire for worktrees that predate the plugin. Use paths
explicitly:

```bash
PLUGIN_ROOT=…   # herdr-managed checkout, or this repo when linked
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" provision --worktree /path/to/wt
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" drop --worktree /path/to/wt
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" drop --force --worktree /path/to/wt
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" gc --dry-run
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" gc --reclaim-leases
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" status
python3 "$PLUGIN_ROOT/bin/sprout-worktree-db" attach-preview --worktree /path/to/wt
```

Herdr actions (from a workspace context): `provision`, `drop`, `gc`, `status`,
`attach-preview`.

## Teardown and GC

- `worktree.removed` drops only `sprout_wt_*` objects for dedicated databases.
- `drop` on a preview attachment forgets the state claim and leaves the shared
  preview database intact (same as `--forget-only`).
- Out-of-band `git worktree remove` emits no herdr event. Schedule `gc`
  (supports `--dry-run`) for orphan cleanup. With `psql` on `PATH`, `gc` also
  scans Postgres for `sprout_wt_*` orphans. Live objects are taken from **state**
  (never guessed from basename alone).
- Drop takes an exclusive lease on the slug (`dropping` in state). A crash
  mid-drop leaves the lease until it expires (1h TTL) or you reclaim it:
  `drop --force --worktree …` / `gc --reclaim-leases`. Expired leases are also
  cleared automatically on the next `provision` / `drop` / `gc`.
- Each slug maps to at most one worktree path. GC never drops a DB while another
  live path still holds the same key (stale duplicate rows are forgotten only).

## Preview mode

Worktrees are usually created before a PR exists, so a dedicated DB is the
default. Once a preview deployment exists, **drop** the dedicated claim first,
then `attach-preview` (branch → MR/PR → `sprout list`) so the env file points
at `sprout_<slug>_pr<id>`. In-place dedicated→preview switches are refused:
the slug stays content-addressed, and drop is the only teardown of `sprout_wt_*`.

## Development

```bash
herdr plugin link /path/to/herdr-sprout
python3 -m unittest discover -s test -v
```

Package layout: `sprout_worktree_db/` (library) + `bin/sprout-worktree-db` (thin shim).
