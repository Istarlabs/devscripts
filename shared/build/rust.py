#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""One entry point for the routine Rust chores in this repository.

Every one of these used to be an ad-hoc chain of `cargo ... | grep ...`, which meant retyping the
flags, reading a human-readable stream that changes between toolchains, and asking for permission
again on every unique command line. Fixed flags, JSON diagnostics, one command.

    python3 Scripts/rust.py verify                    clippy + tests + fmt + licence lines
    python3 Scripts/rust.py verify --quick            clippy only
    python3 Scripts/rust.py verify --against master   diff base for "warnings on changed lines"
    python3 Scripts/rust.py warnings                  warnings by lint, most frequent first
    python3 Scripts/rust.py warnings --code clippy::needless_return    every occurrence of one lint
    python3 Scripts/rust.py test [--filter name] [--details]           tests only
    python3 Scripts/rust.py fmt [--check]             rustfmt over the crate
    python3 Scripts/rust.py packages                  the dependency tree, direct deps first
    python3 Scripts/rust.py packages --vulnerable     cargo audit, advisories in the lock file
    python3 Scripts/rust.py packages --outdated       cargo outdated
    python3 Scripts/rust.py packages --info anyhow    what crates.io has on one crate
    python3 Scripts/rust.py crates                    the workspace members, edition and MSRV
    python3 Scripts/rust.py toolchain                 rustc, cargo, components, what is pinned
    python3 Scripts/rust.py run -- --analyze-only FILE    cargo run, arguments passed through
    python3 Scripts/rust.py script --list             the single-file rust-script programs in Scripts/rust/
    python3 Scripts/rust.py script NAME [args]        run one of them (`rust-script Scripts/rust/NAME.rs`)
    python3 Scripts/rust.py install                   the binary into ~/.local/bin, the .mo files
                                                      when there is an [i18n] block, and the menus
                                                      [rust.service_menu] lists, generated from their
                                                      definitions (servicemenu.py)
    python3 Scripts/rust.py install -p NAME           another workspace member's binary; run and
                                                      `run -p NAME` take it the same way

`verify` reports warnings on the lines this branch changed separately from the ones that were
already there, so a clippy lint in a file you happened to touch is not read as yours.

Exit code is non-zero when the build, the tests, rustfmt or the licence check fail, so it can gate a
commit.
"""
import argparse
import collections
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import gitdiff
import servicemenu
from project import project

SCRIPTS = str(project.root / "Scripts")
REPO = str(project.root)

BINARY = project.get("rust.binary", project.name)
PACKAGE_PATH = project.get("rust.package_path", ".")  # a workspace member: crates/<name>

# Where the binary lands. ~/.local rather than cargo's default ~/.cargo/bin, because a desktop
# session does not reliably carry the latter in PATH.
INSTALL_ROOT = os.path.expanduser(project.get("rust.install_root", "~/.local"))

# A desktop entry is not something every Rust crate has, so it is opt-in through
# [rust.service_menu]. Without that block `install` puts the binary in place and stops. The entries
# are written from definitions (see servicemenu.py) when `install` runs, so no finished .desktop file
# is kept anywhere - keeping copies in step is how three menus once ended up with fixes only the
# installed copy had.
SERVICE_MENU_DEFINITIONS = [project.root / path for path in project.get("rust.service_menu.definitions", [])]
SERVICE_MENU_DIRECTORY = os.path.expanduser(project.get("rust.service_menu.directory", ""))

# A menu whose entries depend on something only its binary knows (the quality a user configured in
# the label) cannot be a static definition. Its binary writes it: a command per entry, run after the
# definitions, with {bin} standing for the directory the binaries are installed to.
SERVICE_MENU_GENERATORS = project.get("rust.service_menu.generators", [])

# gettext, when the project carries an [i18n] block. cargo installs a binary and nothing else, so
# unlike a KDE project with ki18n_install(po) there is nobody to compile the .po files - `install`
# does it, see install_translations().
I18N_DOMAIN = project.get("i18n.domain", "")
I18N_LANGUAGES = project.get("i18n.languages", [])
PO_DIR = project.root / project.get("i18n.po_dir", "po")

# Colour codes in a captured stream are noise in every parser downstream, and the JSON message
# format carries the rendered text with them unless this is set.
ENV = {**os.environ, "CARGO_TERM_COLOR": "never"}


def run(command, **kwargs):
    # errors="replace": `git diff` carries both sides of a file, and a byte that is not UTF-8 in
    # some old blob should not turn a build check into a traceback.
    return subprocess.run(command, cwd=REPO, env=ENV, capture_output=True, text=True,
                          errors="replace", **kwargs)


def diagnostics(command):
    """Run a cargo command with JSON messages and return its compiler diagnostics.

    Each entry is (path, line, lint, message). `path` is absolute so it can be compared against the
    paths `git diff` reports; `lint` is the code cargo gives ("clippy::needless_return",
    "unused_variables") or "" for the ones that carry none.
    """
    completed = run(command + ["--message-format", "json"])
    errors, warnings = [], []

    for line in completed.stdout.splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        if message.get("reason") != "compiler-message":
            continue

        payload = message["message"]
        level = payload.get("level")

        if level not in ("error", "warning"):
            continue

        code = (payload.get("code") or {}).get("code") or ""
        spans = [span for span in payload.get("spans", []) if span.get("is_primary")]
        span = spans[0] if spans else None
        path = os.path.normpath(os.path.join(REPO, span["file_name"])) if span else ""
        entry = (path, span["line_start"] if span else 0, code, payload.get("message", "").strip())

        (errors if level == "error" else warnings).append(entry)

    # A cargo failure with no diagnostic at all is a broken invocation, not a clean build - the
    # manifest is unparsable, the toolchain is missing, clippy is not installed.
    if completed.returncode != 0 and not errors:
        sys.stderr.write((completed.stderr or completed.stdout).strip() + "\n")
        sys.exit(completed.returncode)

    return errors, sorted(set(warnings))


def check(all_targets=True):
    """Clippy over the crate: its own lints plus everything rustc would say anyway.

    Cargo replays the diagnostics of a unit it did not have to rebuild, so this stays correct on a
    warm target/ - but only for units it knows about. --all-targets brings tests and examples along,
    which is where a warning otherwise hides until CI.
    """
    command = ["cargo", "clippy", "--all-targets"] if all_targets else ["cargo", "clippy"]

    return diagnostics(command)


def command_verify(arguments):
    print("Checking (cargo clippy) ...")
    errors, warnings = check()

    if errors:
        print(f"\nBUILD FAILED - {len(errors)} error(s):\n")

        for path, line, code, text in errors[:20]:
            where = f"{os.path.relpath(path, REPO)}:{line}" if path else "(no position)"
            print(f"  {where}  {code}  {text.splitlines()[0]}")

        return 1

    print(f"  no errors, {len(warnings)} warning(s)")

    split = gitdiff.split(REPO, arguments.against, warnings)

    if split is not None:
        introduced, nearby, touched = split

        if introduced:
            print(f"\n  {len(introduced)} warning(s) ON LINES THIS BRANCH CHANGED:")

            for path, line, code, text in introduced:
                print(f"    {os.path.relpath(path, REPO)}:{line}  {code}  {text.splitlines()[0]}")
        else:
            print(f"  no warnings on changed lines ({len(touched)} file(s) touched vs {arguments.against})")

        if nearby:
            print(f"  ({len(nearby)} pre-existing warning(s) elsewhere in those files)")
    elif warnings:
        print(f"  ({arguments.against} does not exist here - listing every warning instead)")

        for path, line, code, text in warnings[:20]:
            print(f"    {os.path.relpath(path, REPO)}:{line}  {code}  {text.splitlines()[0]}")

    if arguments.quick:
        return 0

    tests = command_test(arguments)

    if tests != 0:
        return tests

    formatting = check_formatting(arguments)

    if formatting != 0:
        return formatting

    return check_spdx(arguments)


def check_formatting(arguments):
    """Report what rustfmt would change. It is the one style question nobody should be arguing."""
    if arguments.no_fmt_check:
        return 0

    print("\nChecking formatting ...")

    completed = run(["cargo", "fmt", "--check"])

    if completed.returncode == 0:
        print("  rustfmt clean")

        return 0

    # rustfmt names the file and the line as "Diff in src/main.rs:45:", one line per hunk, so the
    # position has to come off or the same file is counted once per hunk.
    files = sorted({match["path"] for match in
                    (re.match(r"Diff in (?P<path>.+?)(?::\d+)?:?$", line)
                     for line in completed.stdout.splitlines() if line.startswith("Diff in "))
                    if match})
    print(f"  {len(files)} file(s) rustfmt would change:")

    for path in files:
        print(f"    {os.path.relpath(path, REPO)}")

    print("\n  python3 Scripts/rust.py fmt writes it.")

    return 1


def check_spdx(arguments):
    """
    Report the project's own source files that carry no SPDX licence lines.

    Nothing adds them on its own, so a file written in a hurry would not have them and nothing else
    would notice.
    """
    if arguments.no_spdx_check:
        return 0

    # No sys.path juggling: the project's Scripts folder holds a *shim*, and importing that runs it
    # as __main__ against this command's argv. The shim already put this checkout on the path.
    import spdx

    print("\nChecking SPDX licence lines ...")

    found = spdx.missing()

    if not found:
        print("  every source file carries them")

        return 0

    print(f"  {len(found)} file(s) without them:")

    for path in found[:20]:
        print(f"    {path}")

    print("\n  python3 Scripts/spdx.py apply adds them.")

    return 1


def command_warnings(arguments):
    print("Checking (cargo clippy) ...")
    errors, warnings = check()

    if errors:
        print(f"BUILD FAILED - {len(errors)} error(s)")

        return 1

    if arguments.code:
        matching = sorted(w for w in warnings if w[2] == arguments.code)
        print(f"\n{arguments.code}: {len(matching)} occurrence(s)\n")

        for path, line, _, text in matching:
            print(f"  {os.path.relpath(path, REPO)}:{line}  {text.splitlines()[0]}")

        return 0

    print(f"\n{len(warnings)} warning(s), by lint:\n")

    for code, count in collections.Counter(code or "(no lint)" for _, _, code, _ in warnings).most_common():
        print(f"  {code:<40} {count:>4}")

    return 0


TEST_SUMMARY = re.compile(r"^test result: \w+\. (?P<passed>\d+) passed; (?P<failed>\d+) failed; "
                          r"(?P<ignored>\d+) ignored", re.MULTILINE)


def command_test(arguments):
    print("\nTesting ...")

    test_filter = getattr(arguments, "filter", None)
    command = ["cargo", "test", "--all-targets", "--no-fail-fast"]

    if test_filter:
        command.append(test_filter)

    completed = run(command)
    output = completed.stdout + completed.stderr

    # One summary line per test binary (unit tests, each integration test, doc tests), so they are
    # added up rather than read one at a time.
    summaries = [match.groupdict() for match in TEST_SUMMARY.finditer(output)]

    if not summaries:
        print("  could not read a test summary:\n")
        print("\n".join(output.splitlines()[-15:]))

        return 1

    passed = sum(int(summary["passed"]) for summary in summaries)
    failed = sum(int(summary["failed"]) for summary in summaries)
    ignored = sum(int(summary["ignored"]) for summary in summaries)

    print(f"  {passed} passed, {failed} failed, {ignored} ignored")

    if failed:
        print_failures(output, getattr(arguments, "details", False))

    return 1 if failed else 0


def print_failures(output, detailed=False):
    """Name each failing test - "1 failed" alone cannot be acted on.

    The `failures:` block at the end lists the names; the assertion that actually failed is printed
    further up, under `---- name stdout ----`, which is what --details fetches.
    """
    names = []

    for block in re.findall(r"^failures:\n(.*?)(?:\n\n|\Z)", output, re.MULTILINE | re.DOTALL):
        names += [line.strip() for line in block.splitlines()
                  if line.strip() and not line.strip().startswith("----")]

    for name in dict.fromkeys(names):
        print(f"\n  {name}")

    if not detailed:
        print("\n  'test --details' repeats this with the assertion that failed")

        return

    for name, body in re.findall(r"^---- (\S+) stdout ----\n(.*?)(?=^----|\Z)", output,
                                 re.MULTILINE | re.DOTALL):
        print(f"\n  {name}")

        for line in body.strip().splitlines():
            print(f"    {line}")


def command_fmt(arguments):
    if arguments.check:
        return check_formatting(argparse.Namespace(no_fmt_check=False))

    return subprocess.run(["cargo", "fmt"], cwd=REPO, env=ENV).returncode


def cargo_subcommand(name):
    """Whether `cargo NAME` exists - the audit and outdated subcommands are separate installs."""
    return shutil.which(f"cargo-{name}") is not None


def command_packages(arguments):
    if arguments.info:
        completed = run(["cargo", "info", arguments.info])
        print((completed.stdout + completed.stderr).strip())

        return completed.returncode

    if arguments.vulnerable:
        if not cargo_subcommand("audit"):
            print("cargo audit is not installed: cargo install cargo-audit")

            return 1

        return subprocess.run(["cargo", "audit"], cwd=REPO, env=ENV).returncode

    if arguments.outdated:
        if not cargo_subcommand("outdated"):
            print("cargo outdated is not installed: cargo install cargo-outdated")

            return 1

        return subprocess.run(["cargo", "outdated", "--root-deps-only"], cwd=REPO, env=ENV).returncode

    depth = [] if arguments.transitive else ["--depth", "1"]
    completed = run(["cargo", "tree", "--edges", "normal"] + depth)
    print((completed.stdout + completed.stderr).strip())

    return completed.returncode


def metadata():
    completed = run(["cargo", "metadata", "--no-deps", "--format-version", "1"])

    if completed.returncode != 0:
        sys.exit(completed.stderr.strip())

    return json.loads(completed.stdout)


def package_directory(name):
    """The directory of one workspace member and the binaries it builds."""
    packages = metadata()["packages"]

    for package in packages:
        if package["name"] == name:
            binaries = [target["name"] for target in package["targets"] if "bin" in target["kind"]]

            return Path(package["manifest_path"]).parent, binaries

    sys.exit(f"No workspace member '{name}'. Known: {', '.join(sorted(p['name'] for p in packages))}")


def command_crates(_arguments):
    packages = metadata()["packages"]
    print(f"{len(packages)} crate(s) in the workspace:\n")

    for package in sorted(packages, key=lambda entry: entry["name"]):
        kinds = ", ".join(sorted({kind for target in package["targets"] for kind in target["kind"]}))
        print(f"  {package['name']:<24} {package['version']:<10} edition {package['edition']}"
              f"   MSRV {package.get('rust_version') or '-':<8} {kinds}")

    return 0


def command_toolchain(_arguments):
    for command in (["rustc", "--version"], ["cargo", "--version"], ["cargo", "clippy", "--version"],
                    ["rustfmt", "--version"]):
        result = run(command)
        print(f"{result.stdout.strip() or result.stderr.strip()}")

    pinned = os.path.join(REPO, "rust-toolchain.toml")

    if os.path.exists(pinned):
        print(f"\nrust-toolchain.toml:\n{Path(pinned).read_text(encoding='utf-8').strip()}")
    else:
        print("\nNo rust-toolchain.toml - whatever rustup has as default is what builds this.")

    print("\nInstalled toolchains:")
    print(run(["rustup", "toolchain", "list"]).stdout.strip() or "  (rustup not available)")

    return 0


def command_run(arguments):
    # Not captured: this drives ffmpeg and can run for minutes, so the output belongs on the
    # terminal as it happens rather than in one block at the end.
    profile = ["--release"] if arguments.release else []
    passed = list(arguments.arguments)

    # argparse.REMAINDER keeps the `--` that separates this script's flags from the binary's, and
    # passing it on makes clap read the first `--analyze-only` as a file name instead of a flag.
    if passed and passed[0] == "--":
        passed = passed[1:]

    # A workspace has more than one binary, and cargo refuses to guess: -p names the member, and
    # without it rust.package_path says which one is this project's own.
    if arguments.package:
        target = ["-p", arguments.package]
    elif PACKAGE_PATH != ".":
        target = ["--manifest-path", str(Path(REPO) / PACKAGE_PATH / "Cargo.toml")]
    else:
        target = []

    command = ["cargo", "run", "-q"] + target + profile + ["--"] + passed

    return subprocess.run(command, cwd=REPO, env=ENV).returncode


def rust_scripts():
    """The single-file rust-script programs in Scripts/rust/, by their bare name."""
    directory = Path(SCRIPTS) / "rust"

    if not directory.is_dir():
        return {}

    return {path.stem: path for path in sorted(directory.glob("*.rs"))}


def script_purpose(path):
    """The first comment line that is not the header or the shebang says what the script is for."""
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("// ") and not line.startswith(("// File:", "// Created", "// Developer",
                                                           "// Copyright", "// SPDX-")):
            return line[3:].strip()

    return ""


def command_script(arguments):
    scripts = rust_scripts()

    if arguments.list or not arguments.name:
        if not scripts:
            print(f"No .rs scripts in {Path(SCRIPTS) / 'rust'}.")

            return 0

        print(f"{len(scripts)} rust-script program(s) in Scripts/rust/:\n")

        for name, path in scripts.items():
            print(f"  {name:<34} {script_purpose(path)}")

        return 0

    if shutil.which("rust-script") is None:
        raise SystemExit("rust-script not found - `cargo install rust-script`.")

    if arguments.name in scripts:
        path = scripts[arguments.name]
    else:
        candidate = Path(arguments.name) if os.path.isabs(arguments.name) else Path(REPO) / arguments.name

        if not (candidate.is_file() and candidate.suffix == ".rs"):
            known = "\n  ".join(scripts) if scripts else "none"

            raise SystemExit(f"No rust script '{arguments.name}'. Known:\n  {known}")

        path = candidate

    # A debug build: a script is written to be run once or twice, and rust-script's default optimised
    # build costs more compile time than it ever saves. --release for the one that crunches data.
    profile = [] if arguments.release else ["--debug"]
    passed = list(arguments.arguments)

    if passed and passed[0] == "--":
        passed = passed[1:]

    # Not captured: output belongs on the terminal as it happens, not in one block at the end.
    return subprocess.run(["rust-script", *profile, str(path), *passed], cwd=REPO, env=ENV).returncode


def install_translations():
    """Compile po/<language>/<domain>.po into <root>/share/locale/<language>/LC_MESSAGES.

    That path is not a choice: it is where gettext looks, relative to the prefix a program passes to
    bindtextdomain(). Nothing here runs when the project has no [i18n] block.
    """
    if not I18N_DOMAIN or not PO_DIR.is_dir():
        return 0

    if shutil.which("msgfmt") is None:
        print("msgfmt not found - the translations were not installed")

        return 0

    for language in I18N_LANGUAGES:
        source = PO_DIR / language / f"{I18N_DOMAIN}.po"

        if not source.is_file():
            print(f"no {source.relative_to(project.root)} - skipped")
            continue

        target = Path(INSTALL_ROOT) / "share" / "locale" / language / "LC_MESSAGES" / f"{I18N_DOMAIN}.mo"
        target.parent.mkdir(parents=True, exist_ok=True)
        completed = run(["msgfmt", "--check-format", "-o", str(target), str(source)])

        if completed.returncode != 0:
            sys.stderr.write(completed.stderr)

            return completed.returncode

        print(f"installed {target}")

    return 0


def command_install(arguments):
    """Put the binary where the service menu expects it, generate the menus, rebuild the KDE cache.

    `cargo install` defaults to ~/.cargo/bin, which a Dolphin session does not reliably have in
    PATH - hence --root ~/.local. The .desktop file has to be executable or KonqPopupMenu ignores
    it, and KDE only notices a new or changed menu after `kbuildsycoca6`; Dolphin itself needs no
    restart once that has run.
    """
    path, binaries = PACKAGE_PATH, [BINARY]

    if arguments.package:
        path, binaries = package_directory(arguments.package)

    if not arguments.menu_only:
        completed = subprocess.run(["cargo", "install", "--path", str(path), "--root", INSTALL_ROOT,
                                    "--force"], cwd=REPO, env=ENV)

        if completed.returncode != 0:
            return completed.returncode

        for binary in binaries:
            print(f"installed {os.path.join(INSTALL_ROOT, 'bin', binary)}")

        if code := install_translations():
            return code

    return install_menus()


def install_menus():
    """Write every menu definition as `<name>.desktop` into the servicemenus directory.

    All of them every time, whichever binary was just installed: a definition names the binaries by
    their directory, so regenerating is cheap and a changed menu needs no other step.
    """
    if not (SERVICE_MENU_DEFINITIONS and SERVICE_MENU_DIRECTORY) and not SERVICE_MENU_GENERATORS:
        return 0

    if SERVICE_MENU_DEFINITIONS and not SERVICE_MENU_DIRECTORY:
        print("rust.service_menu.definitions needs rust.service_menu.directory")

        return 1

    if SERVICE_MENU_DEFINITIONS:
        os.makedirs(SERVICE_MENU_DIRECTORY, exist_ok=True)

    for definition in SERVICE_MENU_DEFINITIONS:
        target = Path(SERVICE_MENU_DIRECTORY) / (definition.name.removesuffix(".menu.toml") + ".desktop")

        try:
            text = servicemenu.render(tomllib.loads(definition.read_text(encoding="utf-8")),
                                      Path(INSTALL_ROOT) / "bin", definition.name)
        except (OSError, ValueError) as problem:  # tomllib.TOMLDecodeError is a ValueError
            print(f"{definition.relative_to(project.root)}: {problem}")

            return 1

        target.write_text(text, encoding="utf-8")
        target.chmod(0o755)
        print(f"installed {target}")

    for generator in SERVICE_MENU_GENERATORS:
        completed = subprocess.run(shlex.split(generator.replace("{bin}", str(Path(INSTALL_ROOT) / "bin"))),
                                   cwd=REPO, env=ENV)

        if completed.returncode != 0:
            print(f"{generator}: exit {completed.returncode}")

            return completed.returncode

    if shutil.which("kbuildsycoca6"):
        subprocess.run(["kbuildsycoca6"], cwd=REPO, env=ENV, capture_output=True)
        print("rebuilt the KDE service cache - the entry is there now")
    else:
        print("kbuildsycoca6 not found - the entry appears after the next KDE login")

    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command")

    verify = subparsers.add_parser("verify", help="Clippy, tests, rustfmt and the licence lines")
    verify.add_argument("--quick", action="store_true", help="Clippy only, skip the tests")
    verify.add_argument("--against", default=project.get("repo.main_branch", "master"),
                        help=f"Diff base (default: {project.get('repo.main_branch', 'master')})")
    verify.add_argument("--filter", help="Passed to cargo test")
    verify.add_argument("--details", action="store_true", help="Print the assertion a failing test reported")
    verify.add_argument("--no-fmt-check", action="store_true", help="Skip the rustfmt comparison")
    verify.add_argument("--no-spdx-check", action="store_true", help="Skip the check for SPDX licence lines")
    verify.set_defaults(handler=command_verify)

    warnings = subparsers.add_parser("warnings", help="Warnings by lint, most frequent first")
    warnings.add_argument("--code", help="List every occurrence of one lint, e.g. clippy::needless_return")
    warnings.set_defaults(handler=command_warnings)

    tests = subparsers.add_parser("test", help="Run the tests")
    tests.add_argument("filter", nargs="?", help="Only tests whose name contains this")
    tests.add_argument("--details", action="store_true",
                       help="Also print the assertion each failing test reported")
    tests.set_defaults(handler=command_test)

    fmt = subparsers.add_parser("fmt", help="rustfmt over the crate")
    fmt.add_argument("--check", action="store_true", help="Report what it would change, change nothing")
    fmt.set_defaults(handler=command_fmt)

    packages = subparsers.add_parser("packages", help="Dependencies, advisories and updates")
    packages.add_argument("--vulnerable", action="store_true", help="cargo audit over Cargo.lock")
    packages.add_argument("--outdated", action="store_true", help="cargo outdated for the direct dependencies")
    packages.add_argument("--transitive", action="store_true", help="The whole tree, not only direct dependencies")
    packages.add_argument("--info", metavar="CRATE", help="What crates.io has on one crate")
    packages.set_defaults(handler=command_packages)

    run_command = subparsers.add_parser("run", help="cargo run with the arguments passed through")
    run_command.add_argument("--release", action="store_true", help="Build optimized first")
    run_command.add_argument("-p", "--package", metavar="NAME",
                             help="A workspace member other than rust.package_path")
    run_command.add_argument("arguments", nargs=argparse.REMAINDER, help="Passed to the binary unchanged")
    run_command.set_defaults(handler=command_run)

    script = subparsers.add_parser("script", help="Run a single-file rust-script program from Scripts/rust/")
    script.add_argument("name", nargs="?", help="Bare name, file name or path; omitted: list them")
    script.add_argument("--list", action="store_true", help="List the scripts and what each is for")
    script.add_argument("--release", action="store_true", help="Optimised build instead of the quick debug one")
    script.add_argument("arguments", nargs=argparse.REMAINDER, help="Passed to the script unchanged")
    script.set_defaults(handler=command_script)

    install = subparsers.add_parser("install", help="Install to ~/.local/bin and register the service menu")
    install.add_argument("--menu-only", action="store_true", help="Only the .desktop file, no rebuild")
    install.add_argument("-p", "--package", metavar="NAME",
                         help="Install another workspace member's binary instead of the project's own")
    install.set_defaults(handler=command_install)

    subparsers.add_parser("crates", help="Workspace members with edition and MSRV") \
        .set_defaults(handler=command_crates)
    subparsers.add_parser("toolchain", help="rustc, cargo, clippy, rustfmt and what is pinned") \
        .set_defaults(handler=command_toolchain)

    arguments = parser.parse_args()

    if arguments.command is None:
        parser.print_help()

        return 0

    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
