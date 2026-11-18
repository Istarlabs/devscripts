# devscripts

One checkout of the repository chores that are the same in every project: build, test and warnings, the git and
GitHub workflow, symbol search, SPDX headers, translations, container deploys, database access. A project does not
copy them. It carries an eight-line shim per script in `Scripts/` and a `Scripts/project.toml` with what it differs
by, and every shim runs the one shared script.

```bash
python3 Scripts/dotnet.py verify          # build without incremental shortcuts, test, warnings on changed lines
python3 Scripts/repo.py pr-create --title "..." --file body.md
python3 Scripts/find.py OrderService      # where a symbol is declared and used
```

**Why:** these scripts used to be copies, one per project, and the copies drifted. The same `dotnet.py` differed by
almost 500 lines between two repositories, and a fix made in one reached the others by hand, which mostly meant
never. One checkout means a fix is made once.

It is opinionated and grew out of one person's projects: .NET/Blazor, Rust, C++/Qt/KDE, Godot, PHP and web projects
on Fedora Linux, worked on with [Claude Code](https://claude.com/claude-code). The scripts work without Claude Code;
the hooks, skills and guideline blocks are for those who use it.

## Requirements

- **Linux** and **Python 3.11+** (standard library only, `tomllib` for the config)
- **git**, and the [GitHub CLI](https://cli.github.com) `gh` for `repo.py`'s issue, pull request and CI commands
- the toolchain of whatever a project builds: the .NET SDK, cargo, CMake, Godot, npm, rpmbuild, podman (`deploy.py`,
  `ops/server.py`, and `php.py` when there is no local PHP), `psql` for `db.py`, adb for the Android commands
- **bash** and **jq** for the Claude Code hooks
- optional: [graphify](https://pypi.org/project/graphifyy/) (`uv tool install graphifyy`) for `graph.py`,
  [rust-script](https://rust-script.org) for `rust.py script`

A script that misses a tool or a config value refuses and names what is missing; it does not guess.

## Quick start

```bash
git clone https://github.com/Istarlabs/devscripts.git ~/dev/tools/devscripts
cd ~/dev/projects/SomeProject
python3 ~/dev/tools/devscripts/install.py .            # add --claude for the Claude Code parts
$EDITOR Scripts/project.toml                           # fill in what the starter guessed, uncomment what applies
python3 ~/dev/tools/devscripts/install.py .            # again: each filled-in block brings its scripts
python3 Scripts/spdx.py apply                          # only if the project uses [spdx]
```

`~/dev/tools/devscripts` is the default location. A checkout anywhere else works when `DEVSCRIPTS` points at it.

## What install.py changes in a project

Only files under the project, and only these:

- **`Scripts/<name>.py`**: one shim per script the config enables. A script the project wrote itself is kept and
  named in the summary; `--force` replaces it. Every script in `Scripts/` is made executable.
- **`Scripts/project.toml`**: written once, as a starter guessed from the repository (`.sln`, `Cargo.toml`,
  `CMakeLists.txt`); never overwritten after that.

With `--claude` (or `--skills` for the first item alone):

- **`.claude/skills/<name>/`**: the shared skills that apply, each with the project's own
  `.claude/skill-extras/<name>.md` appended. These are copies, because Claude Code reads them off disk; re-running
  is how a project picks up a change.
- **`.claude/settings.json`**: the hooks that apply, added under `hooks.PreToolUse`. Every other key and every hook
  the project wrote itself stays as it is.
- **`.claude/CLAUDE.md`**: written when the project has none, importing the guideline blocks (`claude/*.md`) for
  its kind. A later run only adds an import for a block enabled since; a hand-written `CLAUDE.md` is left alone and
  the summary names the lines it could add.
- **`.claude/skill-map`**: for a project with an `[istarnova]` block that has none yet.

With `--submodule`: `git submodule add` of this repository into `.devscripts/` (see below).

## Layout

```
devscripts/
├── install.py  system.py                            typed by hand, so they stay at the top
├── script_args.py                                    imported by scripts outside the checkout (`import script_args`)
├── shared/                                           what a project's shims point at
│   ├── lib/      project.py  gitdiff.py  servicemenu.py  android.py     imported, never run
│   ├── build/    dotnet.py  rust.py  cpp.py  godot.py  php.py  web.py  browser_extension.py  deploy.py  rpm.py
│   ├── repo/     repo.py  find.py  spdx.py  graph.py  logs.py
│   ├── data/     db.py  gen_db_init_delta.py  verify_db_init.py
│   └── l10n/     i18n.py  gen_resx.py  translations.py
├── ops/          server.py                           a server over ssh, not a project
└── hooks/  skills/  claude/  templates/  Scripts/
```

A shim finds its script by name in the folders of `shared/` and puts all of them (and the checkout itself) on
`sys.path`, which is why the scripts import each other flat (`from project import project`). A new shared script
goes into the folder of its concern and into `SHAREABLE` in `install.py`; a `*.test.py` lives next to the script it
checks.

## Where a shim finds the checkout

In this order: `DEVSCRIPTS`, then `~/dev/tools/devscripts`, then a `.devscripts/` submodule of the project. The
hooks look the same way.

A project other people clone adds the submodule with `install.py . --submodule`. Its maintainer keeps working
against the live checkout; a contributor or CI gets the pinned version after `git submodule update --init`. A fix
here reaches such a project when it bumps the submodule (`git -C .devscripts pull`, then commit). A project only one
person works on does without: the live checkout is the point.

## Putting a project on it, step by step

1. `python3 ~/dev/tools/devscripts/install.py . --claude` writes the starter `Scripts/project.toml`, the shims that
   config already enables and the Claude Code parts. The starter names the language the repository looks like, so
   the first run already brings that language's script and guidelines.
2. **Fill in `Scripts/project.toml`.** The starter guesses `name`, `env_prefix` and the solution; everything else is
   commented out. Uncomment what applies: a database, a resources project, a NuGet feed, a container unit.
3. **Run `install.py --claude` again.** Each block enables its scripts: `[database]` brings `db.py`,
   `[localization]` brings `gen_resx.py` and `translations.py`, `[i18n]` brings `i18n.py`, `[nuget]` turns on
   `dotnet.py publish`. It also brings the skills, hooks and guideline imports the block enables.
4. **Fill in `.claude/CLAUDE.md`**: what the repository is and what holds in it only. A project with its own areas
   writes a `.claude/skill-map`, which turns `load-the-skill.sh` on at the next run.
5. **Delete the project's old copies** of anything now shimmed, after checking what they could do that the shared
   one cannot. A feature only one copy had belongs in the shared script.
6. Verify: `python3 Scripts/dotnet.py verify` (or the project's language), and whatever else it shimmed.

A script that refuses and names a missing key is working correctly: add the key, do not edit the shared script.
`--only dotnet,find` overrides the config-driven selection.

`find.py` picks a language profile from the repository root (`*.sln`/`*.csproj` → dotnet, `CMakeLists.txt` → cpp,
`Cargo.toml` → rust, `pyproject.toml` → python, `package.json` → typescript; nothing found → dotnet). A profile
sets the extensions searched, the build directories skipped and the declaration pattern behind "declared in",
`--show` and `--types`. `[find]` in `project.toml` overrides any part: `profile`, `types`, `exclude` (globs),
`declaration` (a regex, `{symbol}` is the name). `--files` lists what a search would look at, and a wrong `--in`
names what is there instead. `--forward` (C++) also lists the forward declarations (`class X;`), which "declared
in" skips. `--multiple-types` stays a C# check.

`graph.py` is the overview of a repository: who depends on a type, what it touches, the path between two, which
types of another repository it uses (`uses OtherRepo`), from a knowledge graph of the code (graphify) instead of
from reading thousands of files. It needs no `project.toml` block; the project git-ignores `graphify-out/`.
`--in OtherRepo` asks about another repository, whose graph is kept under its `graphify-out/` when it git-ignores
that folder, otherwise under `~/.local/state/graphify/`, so that its checkout stays clean.

## project.toml

Everything a project differs by, and nothing else. A script that needs a value with no entry refuses
and names the key rather than guessing.

```toml
name = "MediaLibrary"
env_prefix = "MLIB"                 # MLIB_DB_HOST, MLIB_CONNECTION, MLIB_LOG_DIR

[dotnet]                            # dotnet.py
solution = "MediaLibrary.sln"
test_project = "..."                # `dotnet.py test`
ef_project = "..."                  # `dotnet.py ef`
startup_project = "..."
logging_baseline = 0
fatal_warnings = ["CS9057"]         # turned into errors by `verify`'s own build
[verify]                            # optional: the project's own checks, run by `dotnet.py verify` after the shared ones
checks = ["areas.py"]               # scripts under Scripts/, run from the repository root; a non-zero exit fails verify
[dotnet.build_properties]           # optional: -p:Key=Value on every build, `~` expanded - what a
AndroidSdkDirectory = "~/Android/Sdk"   # build needs from this machine (a MAUI project's SDK and JDK)

[rust]                              # rust.py
binary = "av1-convert"
install_root = "~/.local"
[rust.service_menu]                 # optional: menus `install` generates (format: servicemenu.py)
definitions = ["dolphin/av1-convert.menu.toml"]   # each becomes <name>.desktop
directory = "~/.local/share/kio/servicemenus"

[cpp]                               # cpp.py
binary = "mydialog"
build_dir = "build"                 # relative to the repository
build_type = "RelWithDebInfo"
install_root = "~/.local"           # becomes CMAKE_INSTALL_PREFIX
# generator = "Ninja"               # only used on a fresh cache; Ninja when it is installed
# configure_args = ["-DWITH_X=ON"]
# binary_path = "bin/mydialog"       # when `run` cannot find it under build/
[cpp.service_menu]                  # same shape as rust's

[rpm]                               # rpm.py: a package for an ssh-reachable RPM repository, any language (docstring has every key)
name = "gputaskman"
summary = "..."
license = "MIT"
version_file = "Cargo.toml"         # Cargo.toml, CMakeLists.txt or a .csproj/.props
build = ["cargo build --release"]   # shell commands; {stage} is the tree laid out as on the target system
files = { "target/release/gputaskman" = "/usr/bin/gputaskman" }
requires = ["kf6-kirigami"]         # what rpmbuild cannot see itself: QML modules, a .NET runtime

[nuget]                             # a library that publishes itself: `dotnet.py publish` packs every project with a <PackageId>, one version for all
feed = "https://api.nuget.org/v3/index.json"   # the official feed; versions come from its flat container
api_key_file = "~/.config/nuget-org-api-key"   # default ~/.config/nuget-api-key
[nuget.private]                     # optional: `dotnet.py publish --private`, same keys
feed = "https://nuget.example.invalid/v3/index.json"

[database]                          # db.py, and the db-init pair
host = "..."
enum_columns = [["schema", "Table", "Column"]]

[db_init]                           # gen_db_init_delta.py, verify_db_init.py
schema_file = "deploy/db-init/schema.sql"
model_script = "model_create_script"   # optional: repo.py push compares schema.sql with the model's CREATE script
watched_paths = ["Project.Database."]  # ... when the branch touches one of these (CI would fail otherwise)
app_role_script = "deploy/app-role.sql"  # optional: `dotnet.py verify` says when schema.sql gains a schema and this must run again

[logs]                              # logs.py
app_project = "MediaLibrary"

[browser_extension]                 # browser_extension.py; both keys optional
directory = "BrowserExtension"      # holds manifest.json
artifacts = "web-ext-artifacts"     # under it; the signed .xpi lands here

[android]                           # MAUI: `dotnet.py android`, `logs.py --android` (see android.py)
project = "MediaLibrary.App"
device = "192.168.1.50:37123"     # adb over WLAN, connected on demand; MLIB_ANDROID_DEVICE overrides
[android.signing]                   # --release only; passwords stay in files, not here
keystore = "~/.config/mlib-signing/mlib.keystore"
alias = "mlib"
store_password_file = "~/.config/mlib-signing/password"
key_password_file = "~/.config/mlib-signing/key-password"
# C++/Qt instead of MAUI: `cpp.py android` reads the same block, with no `project` (there is no .csproj)
# qt = "~/Qt/6.11.2/android_arm64_v8a"   # required: the Qt for Android prefix; must match the host Qt
# application_id = "net.example.app"     # required: what `install` and `run` address
# abi = "arm64-v8a"                      # optional, as are the rest (api = 28 is the minimum API level):
# qt_host_path = "/usr"                  # the host Qt for the cross build; qt_host_cmake_dir is found under lib64
# prefix = "~/CraftRoot"                 # where KF6 etc. built for Android live (CMAKE_PREFIX_PATH, ECM roots)
# sdk, ndk, build_type, configure_args   # sdk/ndk default to the environment, then ~/Android/Sdk and its newest NDK

[localization]                      # .NET: gen_resx.py, translations.py
resources_project = "..."
satellite_languages = ["de", "fr"]

[i18n]                              # Qt/KDE or Rust: i18n.py
domain = "mydialog"                  # the .pot/.po basename, and setApplicationDomain()
languages = ["de"]                  # without the source language: the i18n() argument is English
flavour = "kde"                     # optional: "rust" reads .rs with xgettext's Rust parser
sources = ["src"]                   # optional
bugs_address = "..."                # optional, into the .pot header

[spdx]                              # spdx.py
holder = "The MediaLibrary Contributors"
licence = "AGPL-3.0-or-later"

[deploy]                            # deploy.py
registry = "registry.example.invalid"
ssh_host = "deploy-host"
unit = "medialibrary"
# A second server: `deploy.py deploy --target production`. Every key but the registry may be overridden;
# `tunnel = "5000:localhost:5000"` opens an SSH tunnel (ssh -L) for the push and the tag listing, and
# makes the push and the pull on the server skip TLS verification, as the registry is plain HTTP on loopback.
# [deploy.targets.production]
# registry = "localhost:5000"
# ssh_host = "production"
# tunnel = "5000:localhost:5000"
#
# A copy of the application next to production, as a podman-compose stack instead of a unit:
# `tag` is the floating tag the server tracks (default `latest`), so a build for it never moves what production
# restarts from. `compose` is the directory on the server, relative to the ssh home, that holds `deploy/compose.yaml`
# (the stack's project name is that directory's name); `service` is the one service this target's image is.
# `deploy` pushes, copies deploy/compose.yaml, deploy/db-init and the routine dump there and recreates the service;
# `deploy.py reset --target dev` removes the stack and every volume and starts it empty; `recreate` builds the stack
# again and keeps the volumes, which is what a new published port needs (the containers share one pod, and a port
# belongs to the pod). The `.env` with the passwords belongs to the server and is never written, and neither is a
# `compose.override.yaml` there, which every compose call names with `-f` when it exists.
# [deploy.targets.dev]
# tag = "dev"
# compose = "services/medialibrary-dev"
# service = "app"
# container = "medialibrary-dev_app_1"

[repo]
main_branch = "master"
ci = true                  # false: no workflows; `repo.py ci` says so at once instead of polling for a run
merge_strategy = "merge"   # what `repo.py pr-merge` uses without --strategy: merge | squash | rebase

# The project's own labels, created by `repo.py init` and `repo.py labels` next to the shared ones
# (dependencies, epic, parked, ci) - one block per label, the areas of this project.
# [[repo.labels]]
# name = "area:ui"
# color = "d4c5f9"
# description = "QML pages and the controller"
```

`project.py` resolves it: the shim names the repository through `DEVSCRIPTS_PROJECT`, and a script
run straight out of this checkout searches upwards from the working directory instead.

## Rust scripts

Next to the Python and C# scripts, a single-file [rust-script](https://rust-script.org) program is an option
when Rust is the better tool (speed, a crate that already does the job). `rust.py` runs them, in any project
that has a `[rust]` block, whether or not it is a cargo crate:

```bash
python3 Scripts/rust.py script --list            # Scripts/rust/*.rs, with the first comment line as purpose
python3 Scripts/rust.py script NAME -- args      # rust-script --debug Scripts/rust/NAME.rs args
python3 Scripts/rust.py script NAME --release    # the optimised build, for one that crunches data
```

A debug build by default: a script runs once or twice, and rust-script's optimised build costs more compile
time than it saves. Dependencies go in the script's own `//! ```cargo` manifest block. Needs `rust-script` on
the PATH (`cargo install rust-script`).

## PHP and JavaScript / TypeScript / Vue

`php.py` (`[php]`) and `web.py` (`[web]`) are the two small ones, for a project that is PHP, JS, or both (a Nextcloud app):

```bash
python3 Scripts/php.py lint | test | verify   # php -l under [php] sources; phpunit (vendor/bin, else the system one on tests/)
python3 Scripts/web.py install | lint | typecheck | test | build | verify   # npm run <step>, a missing script is skipped
```

`php.py` runs the local `php`, else a throw-away podman `php:8.5-cli` with the project mounted. `web.py` knows no
tool: eslint, tsc, vue-tsc, vitest, vite are the project's devDependencies behind its `package.json` scripts.

## Browser extensions

`browser_extension.py` lints, builds and signs the Firefox add-on a project carries, in any project with a
`[browser_extension]` block:

```bash
python3 Scripts/browser_extension.py status      # name, version, signed versions, whether the credentials are set
python3 Scripts/browser_extension.py bump        # 1.4 -> 1.5 in manifest.json (or `bump 2.0`); only ever upwards
python3 Scripts/browser_extension.py lint
python3 Scripts/browser_extension.py sign        # lint, then Mozilla signs it unlisted; the .xpi lands in web-ext-artifacts/
python3 Scripts/browser_extension.py build       # an unsigned .zip, for Chrome or a manual upload
```

`upload` puts the signed `.xpi` of the current version on a Nextcloud over WebDAV, so the other machines can
install it: `Internal Programs/{Project}/{BrowserExtension}/{project}-ext-{version}.xpi`, missing folders created, the
same version replaced. Machines in between need it, a listed add-on does not. `NXT_CLD_USR` names the user, `NXT_CLD_KW_SEC_KEY` the KDE Wallet key that holds its app password
(Nextcloud → Settings → Security → App password; read with `kwallet-query`); `nextcloud_url` (else `DEVSCRIPTS_NEXTCLOUD_URL`), `nextcloud_folder` and `nextcloud_project` in the
`[browser_extension]` block override the rest.

Every upload needs a higher `version` than any before it, so `sign` refuses a version that already has a `.xpi` in
the artifacts folder and says to `bump`. The credentials are `AMO_JWT_ISSUER` and `AMO_JWT_SECRET` in the
environment (addons.mozilla.org → Developer Hub → API keys); they reach web-ext through `WEB_EXT_API_KEY` and
`WEB_EXT_API_SECRET`, not through the command line. `sign --listed` publishes in the public directory instead.
Install the result in Firefox through *Add-ons → gear → Install Add-on From File*; a Manifest V3 add-on's host
permissions are granted separately under *Add-ons → the add-on → Permissions*. `browser_extension.test.py` checks it.

## Branches that run in parallel

Two open branches that each regenerate the same file — a delta appended to a schema file, a project
inventory — conflict as soon as the first is merged. That is the cost of working in parallel, not a
mistake, and it is handled in two places:

```bash
python3 Scripts/repo.py sync                  # merge the main branch into this one; on a conflict the merge stays
                                              # open, resolve the files, `commit --path`, or `sync --abort`
python3 Scripts/repo.py test-branch test/x a b --carry   # one branch carrying several, for one round of testing;
                                              # --carry builds it although untracked files lie about
```

`sync` merges rather than rebases, so nothing already pushed is rewritten, and it ignores files nobody
has added. A test branch stays local and is thrown away with `--recreate`; a conflict resolved by hand
on it is lost with it.

## A change that skips the pull request

A change small enough that an issue and a PR are more overhead than the change is committed on a branch
and then landed:

```bash
python3 Scripts/repo.py land                  # master fast-forwards to this branch, is pushed, the branch is deleted
```

It refuses a dirty tree, a branch with nothing master lacks, and a branch master has moved past (`sync`
first). Fast-forward only: nothing is rewritten and nothing is merged behind your back.

## Before a repository goes public

```bash
python3 Scripts/repo.py leaks                 # gitleaks over the whole history and the working files
```

Both places, because a secret deleted from the tree is still readable in the history. The working files
are the ones a commit would take - tracked and new, ignored ones left out - so `build/`, `target/` and
`obj/` are not read. Secrets are redacted; one line per finding, exit 1 when there is one. Needs
`gitleaks` (`sudo dnf install gitleaks`).

## CI

`repo.py ci` is how a run is looked at — not three hand-built `gh` lines:

```bash
python3 Scripts/repo.py ci                    # the last runs of this branch, with durations
python3 Scripts/repo.py ci --watch            # wait for the run of the CURRENT COMMIT, then report
python3 Scripts/repo.py ci --timings          # seconds per step of the newest run, slowest first
```

`--watch` matches on the commit's sha rather than taking the newest run on record. A push takes a
moment to appear, and watching whatever was already there reports the *previous* run as this one's
result — which is how a red build reads as green. When the newest run is red, the failing steps are
printed without asking.

**`--timings` is the one to reach for when a pipeline feels slow**, because the answer is usually
dull and specific. Once it was two full builds of the same solution — a `-warnaserror` guard step
ahead of `verify`, which then rebuilt everything with `--no-incremental`. 207 of 300 seconds. The
guard belongs in `dotnet.fatal_warnings` instead, inside the build that was happening anyway.

**Do not sit and watch a run.** Push, carry on with something else, and check at the next natural
pause. A run is minutes; the only reason to block on one is when the next step genuinely cannot
start without its answer — a merge, usually. `cancel-in-progress` on a concurrency group means a
second push kills the first run rather than leaving it to finish for nothing.

A hosted runner has no `~/dev/tools`, so a project whose pipeline calls `Scripts/dotnet.py` needs the
checkout as well. With the submodule that is one line, nothing else:

```yaml
steps:
  - uses: actions/checkout@v4
    with:
      submodules: true
```

A project without the submodule checks this repository out in a second `actions/checkout` step (`repository:
Istarlabs/devscripts`, `path: .devscripts`) and sets `DEVSCRIPTS: ${{ github.workspace }}/.devscripts`. Without
either, every shim refuses and says so.

## ops/server.py

Looking after a machine over ssh: logs, systemd, podman, new rootless containers. Called by its path, not
installed into projects: a server is a machine, not a project. The machines are tables in
**`~/.config/devscripts/servers.toml`**, outside the checkout, so their names and layout never end up here:

```toml
[web]
ssh = "web"                      # the alias in ~/.ssh/config, never the address
data_directory = "services"      # where `new` keeps a service's data, under the home directory
after_new = "..."                # what `new` reminds you of afterwards (optional)
timezone = "Europe/Berlin"       # TZ of the containers `new` creates (optional)
```

What differs between machines belongs in that table, never in `server.py`. A three-line wrapper per machine
(`from server import main; main("web")`) saves typing the name.

```bash
python3 ~/dev/tools/devscripts/ops/server.py web logs app --grep "proxy|scanner" --since "-90min"
python3 ~/dev/tools/devscripts/ops/server.py web ps | stats | timers | status UNIT | cat UNIT
python3 ~/dev/tools/devscripts/ops/server.py web ps shop -a --ports     # one stack's containers, also the exited, with ports
python3 ~/dev/tools/devscripts/ops/server.py web logs shop_app_1 --container --grep "error|warn" --since -30min
python3 ~/dev/tools/devscripts/ops/server.py web exec shop_db_1 psql -U shop -tAc "select 1"
python3 ~/dev/tools/devscripts/ops/server.py web put compose.override.yaml services/shop/   # a file to the server, relative to ~
python3 ~/dev/tools/devscripts/ops/server.py web get services/shop/.env /tmp/shop.env       # and back
python3 ~/dev/tools/devscripts/ops/server.py web restart app --wait     # also start, stop; --wait: fail unless active
python3 ~/dev/tools/devscripts/ops/server.py web update [--apply]       # podman auto-update, a dry run by default
python3 ~/dev/tools/devscripts/ops/server.py web install app.container --restart   # scp, daemon-reload, restart
python3 ~/dev/tools/devscripts/ops/server.py web new something --image ghcr.io/vendor/something:latest \
    --port 1234:8080 --volume config:/config [--network N] [--dry-run]
```

Everything is rootless (sudo would ask for a password over ssh). `logs` takes the unit name, which is not always the
container name, and filters *entries*: a traceback is one journal line per line, so a line-wise grep returns the
message without its trace.

A container that is no unit - the services of a podman-compose stack - has no journal to read: `logs NAME --container`
reads `podman logs` instead, through the same entry grouping and the same `--grep`, `--exclude`, `--since` (`-30min`
is turned into the `30m` podman wants). `ps NAME` lists only the containers whose name contains `NAME` and leaves out the
failed-units list; `-a` adds the ones that exited (a compose migrator), `--ports` a column with the published ports.
For a project's own containers `deploy.py logs --target NAME` does the same by the target's container name.

`new` writes a quadlet unit: `%h/<data_directory>/<name>/…`, `:Z`, `TZ` when the machine sets one,
`AutoUpdate=registry`, `WantedBy=default.target`. It creates the data directories, refuses to overwrite an existing
unit, runs `daemon-reload`, starts the unit and waits for it to be active. What it cannot do is the proxy host and the
DNS record; `after_new` says which. `server.test.py` checks the grouping and the generated unit.

## system.py

The same questions asked of this machine instead of a server: the journal, systemd units, processes. Read-only, so
nothing needs sudo. Called by its path.

```bash
python3 ~/dev/tools/devscripts/system.py logs sshd                          # a unit: last 60 entries of the last 6 hours
python3 ~/dev/tools/devscripts/system.py logs --process steam --grep "error|fail" --exclude "vulkan|shader"
python3 ~/dev/tools/devscripts/system.py logs --kernel --grep "usb|xpad" --boot -1   # the kernel log of the previous boot
python3 ~/dev/tools/devscripts/system.py logs --priority err --since "-2h"  # everything at error level or worse
python3 ~/dev/tools/devscripts/system.py failed | timers | boots | units PATTERN | status UNIT | cat UNIT
python3 ~/dev/tools/devscripts/system.py ps PATTERN                         # processes by regex, with the unit each belongs to
python3 ~/dev/tools/devscripts/system.py blame                              # boot time and the slowest units
```

`logs` narrows by source (a unit, `--process NAME`, `--kernel`, or everything), by time (`--since`, or `--boot [N]` for
a whole boot), by `--priority`, then by `--grep` / `--exclude` - the entry filter of `server.py`, imported from it. A
unit is looked up as a user unit first and as a system unit second, so the scope does not have to be known; `--user` /
`--system` force one. `journalctl -k` only ever shows one boot, whatever `--since` says: an older kernel log is
`--kernel --boot -1`. `system.test.py` checks the journalctl call.

## hooks/

The Claude Code hooks, pointed at from each project's `.claude/settings.json`:

```json
"command": "d=\"${DEVSCRIPTS:-$HOME/dev/tools/devscripts}\"; [ -d \"$d/hooks\" ] || d=\"$CLAUDE_PROJECT_DIR/.devscripts\"; bash \"$d/hooks/prefer-repo-scripts.sh\""
```

No copy, so no drift; the checkout is found as the shims find it. They locate the repository through
`CLAUDE_PROJECT_DIR`.

`install.py --claude` enters them, chosen like the skills (`HOOKS` in `install.py`), and only ever adds:
a hook the file already names, every other key and the project's own hooks stay as they are.

| Hook | Entered for |
| --- | --- |
| `no-cd-into-repo-root.sh`, `prefer-repo-scripts.sh`, `no-decorative-comments.sh` | every project |
| `load-the-skill.sh` | a project with a `.claude/skill-map` |
| `no-direct-logger.sh` | `[dotnet]` |
| `prefer-localized-grid.sh` | `[istarnova]` |

The one thing that differs per project is which skill covers which files, and that is read from the project's own
`.claude/skill-map`: one `<glob> <skill>` per line for a folder, and `~<regex> <skill>` (no spaces in
the regex) for what an edit writes — logging and JSON are done in any file, so no folder names them.
Text files (`.md`, `.json`, `.txt`) are skipped for the content lines. Each hook has a `.test.sh`
beside it; run them after touching one.

`prefer-localized-grid.sh` refuses a raw `<FluentDataGrid>` in a `.razor` file, but only in a project with an
`[istarnova]` block (below): the raw grid draws its menus, empty message and paginator in English.

## claude/

A skill explains how to use something; it does not set the rules of a project. Those are in `CLAUDE.md`,
and most of them are the same for every project of one kind. They live here as one block per kind, and a
project's `.claude/CLAUDE.md` imports the ones it gets:

```
@~/dev/tools/devscripts/claude/base.md
@~/dev/tools/devscripts/claude/rust.md
```

Claude Code expands such a line, so this is a reference and not a copy: a rule is changed here once.
The path is outside the repository, so Claude Code asks once per project whether it may load the files.

| Block | Imported for |
| --- | --- |
| `base.md` | every project |
| `rust.md`, `cpp.md`, `dotnet.md`, `php.md`, `web.md` | the `project.toml` block of that name |
| `kde.md` | a `CMakeLists.txt` that asks for `KF6` |
| `blazor.md` | `[istarnova]` |

`install.py --claude` writes the file when there is none, with a section for what holds in that project
only, and never touches that section again. A later run adds the import for a block enabled since. A
`CLAUDE.md` written by hand, with no such import, is kept as it is; the summary names the lines it could
add. A new kind of project is a new block here and an entry in `CLAUDE_BLOCKS` in `install.py`.

## The Istarnova building blocks

[Istarnova](https://github.com/Istarlabs/Istarnova) is a .NET library whose Blazor part holds the EF Core
foundation, the circuit state, the localized grids, the navigation and the hosting pieces several applications
share (`Istarnova.EfCore`, `Istarnova.Blazor`, `Istarnova.Blazor.FluentUI`, `Istarnova.AspNetCore`). A project
that uses them says so:

```toml
[istarnova]
raw_grid_baseline = 0     # how many raw FluentDataGrid `dotnet.py verify` tolerates; zero unless said otherwise
```

That one block turns on, for the project:

- the skills `blazor-ui` (with `references/`), `data-layer` and, through `[dotnet]`, `logging` - written by
  `install.py --skills`, each with the project's `.claude/skill-extras/<name>.md` appended
- a default `.claude/skill-map` for the hooks' doorbell, when the project has none (plus the localization lines if it has
  `[localization]`)
- the `prefer-localized-grid.sh` hook, once `.claude/settings.json` points at it
- the raw-grid count in `dotnet.py verify` (`--no-istarnova-check` skips it)

What is specific to a project - its schemas, how it opens dialogs, whether migrations are committed - goes in the extras, never into
the shared skill.

## skills/

The half of a skill that is about the workflow rather than about one codebase. `install.py --skills`
copies each one into the project's `.claude/skills/` with the project's own
`.claude/skill-extras/<name>.md` appended after a `---`.

So: edit the shared skill here, or the project's extras file. Never
`.claude/skills/*/SKILL.md` in a project — that file is generated and the next `--skills` run
overwrites it.

**A file under `references/` is different: it has no extras file.** `--skills` replaces it only
while it is what the last run wrote (the digest is in `.claude/skills/.installed.json`, committed
with the project). A reference the project changed, or one with no record yet, is *kept* and named
in the summary; `--force` replaces it. The project text then belongs in the shared skill if it is
general, and stays a deliberate local edit if it is not. `python3 install.test.py` checks this.

**A skill can be gated on a `project.toml` block**, through `SKILL_BLOCKS` in `install.py`, the same
way a script is. `localization` needs `[localization]` and `i18n` needs `[i18n]`, so no project gets
both and a project gets neither unless it localizes. This is not tidiness: `localization` states
that every `.resx` is generated from JSON and must never be hand-edited, which in a gettext project
is four confident instructions about files that do not exist. A skill that does not apply is not
read sceptically — it is followed.

Gating only decides what a fresh `--skills` run writes. A skill installed before its gate existed
stays on disk until it is deleted.

## Writing a script that lives here

**A second language script shares what is not about the language.** `gitdiff.py` holds "which lines
did this branch change, and which of these warnings sit on them" — `rust.py` and `cpp.py` both
report warnings that way, and two copies of it would have drifted exactly the way this checkout
exists to stop.

**Two scripts for localization, on purpose.** `translations.py` generates `.resx` out of JSON;
`i18n.py` drives `xgettext`/`msgmerge` over `.po` files. They share the word and nothing else — in a
Qt/KDE program the English text *is* the `i18n()` argument, so there is no source file to generate
and no base language directory. Folding both behind `[localization]` would be one script doing two
unrelated things, which is the `logs.py` mistake below. Hence the separate `[i18n]` block, which is
also how `install.py` knows not to put a `.po` shim in a .NET project.

What `i18n.py` is actually worth is its `xgettext` call: KDE's `i18n()` family is twenty-odd
functions, and a string is extracted only if the function it sits in is named with a `-k` flag.
Miss one and those strings never reach a translator, silently.

`flavour = "rust"` swaps that call for `xgettext --language=Rust` over `.rs` files — gettext 0.26
parses Rust itself and its default keywords already name gettext-rs' functions, so there is no list
to keep there. The other half a Rust project misses is `ki18n_install(po)`: cargo installs a binary
and nothing else, so `rust.py install` compiles each `.po` into
`<install_root>/share/locale/<lang>/LC_MESSAGES/<domain>.mo`.

**`cpp.py verify` builds clean by default.** A compiler replays nothing: an incremental build that
recompiles nothing prints no warnings, and "0 warnings" out of a build that did no work is the kind
of green that hides a red. `--incremental` opts out where the honest answer costs too much.

**`cpp.py tidy` is not part of `verify`.** clang-tidy takes a few seconds per file and a project that
was never run through it starts with dozens of findings, so it is asked for, not imposed. It builds
first (clang-tidy needs the compile database and the generated moc/kcfg headers), checks only the
project's own translation units, counts a header's finding once rather than once per includer, and
says how many `NOLINT` it honoured — a wrong file pattern and a clean tree both print nothing from
`run-clang-tidy` itself, and only the counts tell them apart.

**Never `sys.path.insert(0, SCRIPTS)` to reach a sibling script.** In a project that uses this
checkout `Scripts/` holds *shims*, so that import runs one as `__main__` against the current
command's argv — `rust.py verify` died on `spdx.py: invalid choice: 'verify'` exactly that way. The
shim has already put this directory on the path, so a plain `import spdx` finds the real one.

## What does not belong here

A script about one project's domain. `unused.py`, `gen_project_map.py`, the importers and the
`fetch_*.py` stay in the repository they serve. The test: could a second project use it without
knowing what the first one is for?

Two that failed it and stayed behind, for the record:

- **An audit subcommand in `db.py`** that queried one application's own tables. It is a `.sql` file in
  that repository now, run by the generic `db.py query --file`.
- **A second `logs.py`** in a desktop tool. Same command-line surface as the shared one, on purpose,
  and a different implementation underneath: one file under `$XDG_STATE_HOME` against a directory
  per day discovered through NLog's configuration, and a different line format. What differs is the
  strategy, not a value, and folding both behind configuration would produce one script doing two
  unrelated things. If a third project wants the same, share the entry-filtering half and leave
  "where is the log" to each of them.

## Contributing and licence

Issues and pull requests are welcome. Each script has its checks next to it (`*.test.py`, `hooks/*.test.sh`);
run the ones for what you touched. Commits follow [Conventional Commits](https://www.conventionalcommits.org).

devscripts is licensed under the [GNU General Public License v3.0 or later](LICENSE.md).
