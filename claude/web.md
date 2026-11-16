# Shared guidelines: JavaScript, TypeScript, Vue

- `python3 Scripts/web.py verify` is the gate: lint, type check, tests and build, each as the
  `package.json` script of that name; a script the project does not have is skipped. It runs before
  every commit.
- `web.py` knows no tool. ESLint, `tsc`, `vue-tsc`, Vitest and Vite are the project's own
  devDependencies behind those scripts; a new check is a new `package.json` script, not a new command
  line.
- A dependency is added when it replaces real work, not for what the platform or a few lines already do.

| Task | Command |
| --- | --- |
| Lint, type check, tests, build | `python3 Scripts/web.py verify` |
| One step | `python3 Scripts/web.py install\|lint\|typecheck\|test\|build` |
