# udevmon: notes for Claude sessions

Read `DEVELOPMENT.md` first: tests, the checks that keep CLI, completion, man
pages and docs in step, live testing, branches and releasing. `SPECIFICATIONS.md`
is the design (§12 lists every open idea); `README.md` is the user guide;
`CHANGELOG.md` the history.

## Quick reference

```
PYTHONPATH=src python3 -m unittest discover -s tests -t .
udevmon check                  # the user's real config
VENV/bin/python tools/docs.py build   # HTML manual; needs the docs extra
git log --oneline -5; git branch -a
```

## Rules

- **Branches:** work on a branch off `main`; commits there need no asking.
  Merging into `main`, tagging and pushing only when the user says so.
- **No backwards compatibility** while udevmon is preliminary: change formats
  and names in place, no aliases or migration code; fix the user's own config
  (`~/.config/udevmon/config.toml`, `config.d/`) by hand if it is affected.
- **A new option is not done** until `completion.py`, the man pages, README,
  SPECIFICATIONS and the changelog have it; the tests enforce most of this.
- **Live tests** use a private daemon (own config and `XDG_RUNTIME_DIR`), never
  the user's running service, and leave nothing behind (DEVELOPMENT.md, "Live
  testing"). Ask before restarting the real service.
- **Untracked notes and run books** go in files named `*.local.md` (ignored
  by Git); delete them once their work is done.
- **The owner's environment** (remotes, other machines, projects that use
  udevmon) is in `CLAUDE.local.md`, which Git ignores, when it exists.
