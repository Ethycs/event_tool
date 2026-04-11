# Migration Guide: CLI Redesign

The CLI was restructured to use **subcommands** with positive-framing source filters
and consolidated cap parsing. Old flag names are no longer recognized — `argparse`
will print "unrecognized arguments" if you use them.

## Quick reference: old → new

### Source toggles (negative → positive)

| Old | New |
|---|---|
| `--no-telegram` | `--skip telegram` |
| `--no-discord` | `--skip discord` |
| `--no-gmail` | `--skip gmail` |
| `--no-signal` | `--skip signal` |
| `--no-web` | `--skip web` |
| `--no-telegram --no-web` | `--skip telegram,web` |
| (no equivalent — implicit) | `--only discord,gmail` (positive framing) |

`--only` and `--skip` are mutually exclusive.

### Cap flags (consolidated)

| Old | New |
|---|---|
| `--cap-discord 50` | `--cap discord=50` |
| `--cap-telegram 30` | `--cap telegram=30` |
| `--cap-discord 20 --cap-telegram 30 --cap-total 100` | `--cap discord=20,telegram=30 --cap total=100` |

### Pipeline toggles

| Old | New |
|---|---|
| `--no-analysis` | `--no-analyze` |
| `--no-ticktick` | `--no-sync` |

### Modal flags → subcommands

| Old | New |
|---|---|
| `--watch [--interval N]` | `event-harvester watch [--interval N]` |
| `--test-url URL` | `event-harvester web test URL` |
| `--add-source URL` | `event-harvester web add URL` |
| `--web-login` | `event-harvester web login` |
| `--reparse FILE` | `event-harvester recruiters reparse FILE` |
| `--grade-recruiters` | `event-harvester recruiters grade` (or harvest convenience flag) |
| `--auto-trash` | `event-harvester recruiters grade --auto-trash` |
| `--train-classifier` | `event-harvester classifier train` |
| `--save-labels FILE` | `event-harvester classifier train --out-labels FILE` |
| `--load-labels FILE --eval-classifier` | `event-harvester classifier eval --labels FILE` |
| `--save-eval DIR` | `event-harvester classifier eval --out-samples DIR` |
| `--serve` | `event-harvester serve` |

### Kept unchanged

- `--days N`
- `--save FILE` (raw harvested messages JSON)
- `--load FILE`
- `--report [FILE]`
- `--obsidian`
- `--dry-run`
- `--group-by-source`
- `-v / --verbose`

### New flags

| New | Description |
|---|---|
| `-q / --quiet` | Only show warnings and errors (mutex with `-v`) |
| `--version` | Print version and exit |

### Backward compat

Bare invocation still works — `event-harvester --days 14 --skip web` is
equivalent to `event-harvester harvest --days 14 --skip web`. The
`harvest` subcommand is implicit when no subcommand is given.

## Exit codes

- `0` — success
- `1` — user error (bad config, missing file, validation failure)
- `2` — system error (network failure, browser crash, unrecoverable)
- `130` — interrupted (Ctrl+C, standard SIGINT exit code)

## Examples

```bash
# Old
event-harvester --days 14 --no-telegram --no-web --cap-discord 30 --cap-total 80

# New
event-harvester --days 14 --skip telegram,web --cap discord=30,total=80
```

```bash
# Old
event-harvester --watch --interval 60 --no-discord

# New
event-harvester watch --interval 60 --skip discord
```

```bash
# Old
event-harvester --test-url https://lu.ma/discover

# New
event-harvester web test https://lu.ma/discover
```

```bash
# Old
event-harvester --train-classifier --save-labels labels.json

# New
event-harvester classifier train --out-labels labels.json
```
