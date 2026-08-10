"""Import every module from a clean export of HEAD, not the working tree.

**Why this exists.** On 2026-08-07 `main` was found to reference
`sources/web_modes/instagram.py`, a module that had never been committed. The
import lines had been staged with `git add <path>` in a dirty tree; the modules
they referred to were untracked and stayed behind. **31 modules failed to
import from a bare checkout** — `cli`, `pipeline`, `mcp_server`, `ui.app`,
`watch` — so a fresh clone could not run anything at all.

The suite was green throughout, and correctly so: pytest runs against the
working tree, where the untracked file exists and satisfies the import. No
test in this repo has ever loaded a clean checkout. That is the gap this
closes, and it is the same shape as every other defect that week — the code
was fine, the *deployment* was not.

    pixi run check-head          # HEAD
    pixi run python scripts/check_head_imports.py origin/main

Exits 1 and names the modules if any intra-package import fails. A missing
third-party dependency is an environment fact, not a broken commit, so only
failures naming `event_harvester` count.
"""

from __future__ import annotations

import importlib
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
PKG = "event_harvester"


def export(ref: str, dest: pathlib.Path) -> None:
    """Materialise `ref` as files. `git archive` writes only what is committed."""
    archive = subprocess.run(
        ["git", "archive", ref],
        cwd=REPO, stdout=subprocess.PIPE, check=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)


def check(src: pathlib.Path) -> list[str]:
    # Drop ONLY the repo's own src/. Filtering on the substring "event_tool"
    # also matches the pixi env living under it, and takes the stdlib with it.
    repo_src = (REPO / "src").resolve()
    sys.path = [p for p in sys.path if p and pathlib.Path(p).resolve() != repo_src]
    sys.path.insert(0, str(src))

    pkg = importlib.import_module(PKG)
    if not pathlib.Path(pkg.__file__).is_relative_to(src):
        raise SystemExit(
            f"resolved the working tree, not the export: {pkg.__file__}\n"
            "an editable install is shadowing it; this check proves nothing until fixed",
        )

    names = sorted(
        ".".join(p.relative_to(src).with_suffix("").parts).removesuffix(".__init__")
        for p in (src / PKG).rglob("*.py")
    )

    missing = []
    for name in names:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as e:
            if (e.name or "").startswith(PKG):
                missing.append(f"  {name}\n      no module named {e.name}")
        except Exception:  # noqa: BLE001 -- import-time side effects are not the subject
            pass
    print(f"{len(names)} modules in {PKG}", file=sys.stderr)
    return missing


def main() -> int:
    ref = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="eh-headcheck-"))
    try:
        export(ref, tmp)
        missing = check(tmp / "src")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if missing:
        print(f"\n{ref} does not import -- {len(missing)} module(s):")
        print("\n".join(missing))
        print("\nSomething imported is not committed. Check `git status` for "
              "untracked files the staged edits refer to.")
        return 1
    print(f"clean: {ref} imports from a bare checkout")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
