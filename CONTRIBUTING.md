# Contributing to PyMillCAM

Thanks for taking a look. PyMillCAM is in early public beta — bug
reports, DXF test cases, and small focused PRs are all welcome.

## Before you start

Please read [the Safety section in the README](README.md#safety).
Generated G-code drives real machines that can injure people; any
change that touches toolpath generation, post-processing, or macros
should be paired with manual simulator verification, not just unit
tests.

## Development setup

PyMillCAM uses [uv](https://docs.astral.sh/uv/) for dependency
management.

```bash
git clone https://github.com/andax/pymillcam.git
cd pymillcam
uv sync              # creates .venv, installs runtime + dev deps
uv run pymillcam     # sanity-check the app launches
```

## Running checks

All three must pass before a PR:

```bash
uv run pytest         # ~700 tests, fast (~2 s)
uv run ruff check .   # lint
uv run mypy src       # strict type-check
```

## Filing a bug

Open an issue using the **Bug report** template. Attach every file
that'll help reproduce:

- The `.pmc` project file (or a minimal one that reproduces the bug).
- The source `.dxf` if the bug involves imported geometry.
- The generated G-code when the problem is in the output.
- Your controller / firmware (UCCNC, GRBL, or otherwise).

## Submitting a PR

- Fork, branch off `main`, push to your fork, open a PR against
  `andax/pymillcam:main`.
- Keep commits focused. A one-bug / one-feature commit is easier to
  review and easier to revert if it turns out to break something on
  someone's machine.
- For UI changes, describe what you tested manually — type-checks and
  `pytest` don't catch visual regressions. If you changed a dialog,
  say which dialog you opened and confirmed works.
- For engine / post-processor changes, include a unit test that would
  have failed before the fix.
- Commit messages: short imperative subject line; body explains the
  *why*. The existing `git log` has the house style.

## Code style

- Type hints everywhere. `mypy src` is strict and must pass.
- Pydantic models for data structures; `@dataclass` only where
  Pydantic is overkill.
- No UI imports inside `core/` or `engine/`.
- Comments: default to none. If the *why* is non-obvious (a subtle
  invariant, a workaround for a specific bug, a controller quirk),
  a one-line comment is welcome. If the code is obviously readable,
  don't comment it.

## Scope

PyMillCAM targets 2D/2.5D CAM for hobby CNC routers and mills. Full
3D strategies, 4/5-axis, and production-grade feed optimisation are
explicitly out of scope — not because they're uninteresting but
because keeping the scope tight is what lets a small project ship.
Big-scope PRs are better raised as issues first so we can talk about
fit before you write the code.

## License

By contributing, you agree that your contribution is licensed under
the same GPL-3.0-or-later license as the rest of the project.
