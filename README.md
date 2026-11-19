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

### Platforms

Linux is the target, and Fedora is the only system it is run on. The core is plain Python with the standard library
(`dotnet.py`, `repo.py`, `find.py`, `db.py`, `translations.py`, `gen_resx.py`, `spdx.py` and their kind) and has no reason
not to run on macOS; it has not been tried. What needs Linux:

- `system.py` (systemd: `journalctl`, `systemctl`), and the deploy and server scripts, which expect Podman and Quadlet
  units
- the packaging scripts (`rpm.py`, `godot.py desktop`, parts of `cpp.py`), which write RPMs, `.desktop` files and Unix
  file modes
- the Claude Code hooks, which are bash scripts; on Windows they need Git Bash or WSL
- the caches, which live in `~/.cache`

On Windows, use WSL. Nothing here has been run on macOS or Windows, so treat both as untested.

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

## Documentation

The manual is the [wiki](https://github.com/Istarlabs/devscripts/wiki).

| Page | What it covers |
| --- | --- |
| [Installation and setup](https://github.com/Istarlabs/devscripts/wiki/Installation-and-Setup) | What `install.py` writes into a project, where a shim finds this checkout, and how to put a project on it. |
| [Project configuration](https://github.com/Istarlabs/devscripts/wiki/Project-Configuration) | `Scripts/project.toml`: everything a project differs by, block by block. |
| [Languages and toolchains](https://github.com/Istarlabs/devscripts/wiki/Languages-and-Toolchains) | Rust scripts, PHP and JavaScript/TypeScript projects, and Firefox browser extensions. |
| [Git and GitHub workflow](https://github.com/Istarlabs/devscripts/wiki/Git-and-GitHub-Workflow) | Parallel branches, a change without a pull request, the check before a repository goes public, and CI. |
| [Machines and servers](https://github.com/Istarlabs/devscripts/wiki/Machines-and-Servers) | `ops/server.py` for a machine over ssh and `system.py` for this one. |
| [Claude Code integration](https://github.com/Istarlabs/devscripts/wiki/Claude-Code-Integration) | The hooks, the guideline blocks, the Istarnova building blocks and the shared skills. |
| [Contributing and design notes](https://github.com/Istarlabs/devscripts/wiki/Contributing-and-Design-Notes) | How the checkout is laid out, how to write a script that lives here, and what does not belong. |

## Contributing and licence

Issues and pull requests are welcome. Each script has its checks next to it (`*.test.py`, `hooks/*.test.sh`);
run the ones for what you touched. Commits follow [Conventional Commits](https://www.conventionalcommits.org).

devscripts is licensed under the [GNU General Public License v3.0 or later](LICENSE.md).
