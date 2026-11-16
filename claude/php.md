# Shared guidelines: PHP

- `python3 Scripts/php.py verify` is the gate: `php -l` over the sources named under `[php]`, then
  PHPUnit (the project's `vendor/bin`, else the system one on `tests/`). It runs before every commit.
- `php.py` uses the local `php` and falls back to a throw-away podman `php:8.5-cli` with the project
  mounted, so nothing has to be installed for a lint.
- Input from a request, a file or another service is validated where it enters; a query is
  parameterized, never assembled from strings.

| Task | Command |
| --- | --- |
| Lint, tests | `python3 Scripts/php.py verify` |
| Only one of them | `python3 Scripts/php.py lint`, `… test` |
