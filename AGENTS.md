# AGENTS.md

This file defines repository-level engineering rules for Codex and human contributors.
All new changes in this repository must follow these rules.

## 1) Scope And Priority

- Runtime path priority: `dashboard/*`, `scripts/*`, `data/*`.
- Do not reintroduce removed split-stack paths (`edict/frontend`, `edict/backend`).
- Prefer minimal, local changes over broad refactors unless explicitly requested.

## 2) API And Business Boundaries

- In `dashboard/server.py`, route matching in `do_GET` / `do_POST` must only dispatch.
- New business logic must be implemented in dedicated helper functions, not inline in route branches.
- New helper names must use explicit prefixes: `handle_`, `load_`, `save_`, `build_`, `validate_`.

## 3) Data Safety Rules (Hard Requirement)

- Any write to files under `data/` must use atomic helpers from `scripts/file_lock.py`.
- Do not add direct `Path.write_text(...)` / `json.dump(...)` writes for runtime data paths.
- Keep task schema backward-compatible for existing dashboard fields.

## 4) Validation And Error Handling

- All external/user input must be validated before use.
- URL input must go through `validate_url` in `scripts/utils.py`.
- Name-like input should use safe-name checks.
- No bare `except:`. Catch specific exceptions and return structured errors.

## 5) Response And Logging Conventions

- POST-like mutating endpoints should return envelope:
  - success: `{"ok": true, ...}`
  - failure: `{"ok": false, "error": "..."}`
- Use module logger (`logging.getLogger(...)`) for service logs.
- Avoid `print` in server runtime code (CLI scripts may print user-facing output).

## 6) Python Style Baseline

- Target: Python 3.9+.
- New/modified non-trivial functions should include type hints.
- Public helper functions should have concise docstrings.
- Use `pathlib` for path handling; avoid manual string path concatenation.
- Keep functions focused; avoid adding more deeply nested route branches when helper extraction is possible.

## 7) Frontend (dashboard.html) Rules

- Keep existing visual language unless a redesign is explicitly requested.
- New UI behaviors must call existing API contracts first; avoid hidden one-off endpoints.
- Reuse existing CSS variables and naming patterns.

## 8) Testing And Verification

- Any logic change requires at least one verification step before finish.
- Minimum checks for Python changes:
  - `python3 -m py_compile <changed_python_files>`
- If route or state logic changes, add/update tests in `tests/` when feasible.

## 9) Forbidden Changes Without Explicit Request

- No destructive cleanup of unrelated files.
- No runtime behavior changes in `scripts/run_loop.sh` scheduling defaults.
- No data contract breaking changes for `live_status.json`, `tasks_source.json`, `agent_config.json`.

## 10) Commit Hygiene

- One commit should represent one coherent concern.
- Do not include unrelated dirty-worktree files in the same commit.
- Use Conventional Commit style (`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`).
