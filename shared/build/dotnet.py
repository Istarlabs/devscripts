#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""One entry point for the routine .NET chores in this repository.

Every one of these used to be an ad-hoc chain of `dotnet ... | grep ...`, which meant the paths were
retyped each time, the greps had to match the SDK's *German* output on this machine, and each unique
command line asked for permission again. Fixed paths, forced-English output, one command.

    python3 Scripts/dotnet.py verify                  build + tests + warnings on changed lines
    python3 Scripts/dotnet.py verify --quick          build only
    python3 Scripts/dotnet.py verify --baseline       compare the warning total against master
    python3 Scripts/dotnet.py build PATH|NAME         one project on its own, also one outside the solution
    python3 Scripts/dotnet.py warnings                warnings by code, project-level ones included
    python3 Scripts/dotnet.py warnings --code CS0168  every occurrence of one code
    python3 Scripts/dotnet.py test [--filter X]       tests only
    python3 Scripts/dotnet.py packages --vulnerable   known advisories, transitive included
    python3 Scripts/dotnet.py packages --outdated     available updates
    python3 Scripts/dotnet.py packages --project Api  one project instead of the solution
    python3 Scripts/dotnet.py packages --versions Microsoft.OpenApi   versions on nuget.org
    python3 Scripts/dotnet.py packages --set Pkg.A=1.2.0 Pkg.B=3.0.1   set versions in every project file
    python3 Scripts/dotnet.py projects                projects in the solution, with target framework
    python3 Scripts/dotnet.py sdk                     SDK, runtimes and what global.json pins
    python3 Scripts/dotnet.py script --list           the single-file C# scripts in Scripts/
    python3 Scripts/dotnet.py script NAME [args]      run one of them (`dotnet run Scripts/csharp/NAME.cs`)
    python3 Scripts/dotnet.py script --database pmr_x NAME   ... against another database, for scripts that write
    python3 Scripts/dotnet.py publish --dry-run       verify, pack every <PackageId> project into nupkg/, list them
    python3 Scripts/dotnet.py publish                 the same, then push to the feed in [nuget]
    python3 Scripts/dotnet.py publish --private       ... to the feed in [nuget.private] instead
    python3 Scripts/dotnet.py publish --version 2.0.0-rc.1   exactly this version
    python3 Scripts/dotnet.py android build           MAUI app as a debug .apk (debug-signed), [android] in project.toml
    python3 Scripts/dotnet.py android build --release         signed with [android.signing]
    python3 Scripts/dotnet.py android install [--release]     build, then adb install -r on the device
    python3 Scripts/dotnet.py android install --no-build      install the .apk already built
    python3 Scripts/dotnet.py android devices                 connect the configured device, list what adb sees
    python3 Scripts/dotnet.py android run                     start the installed app
    python3 Scripts/dotnet.py android dependencies            install the Android SDK platform the target framework needs
    python3 Scripts/dotnet.py android release [--ref TAG]     the release APK of one commit, built in [android.release]'s container
    python3 Scripts/dotnet.py android release --check         ... twice, failing unless both unsigned APKs are identical
    python3 Scripts/dotnet.py ef add AddSomething     migration, both projects filled in
    python3 Scripts/dotnet.py ef update               apply pending migrations to the dev database
    python3 Scripts/dotnet.py ef remove | list | script

Exit code is non-zero when a build or a test fails, so it can gate a commit.
"""
import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import android
from project import project

SCRIPTS = str(project.root / "Scripts")
REPO = str(project.root)
CSHARP = os.path.join(SCRIPTS, "csharp")
# Resolved on use, not on import. A module-level require() would refuse before argparse has even
# seen the subcommand, so a project without a solution file could not run the parts that need none -
# and importing this module to test its pure functions would exit instead.
TEST_PROJECT = project.path("dotnet.test_project")
# The migrations live in the EfCore project, the connection string in the web application - every
# `dotnet ef` line needs both, and neither ever changes.
EF_PROJECT = project.path("dotnet.ef_project")
STARTUP_PROJECT = project.path("dotnet.startup_project")

# The SDK is localized here; without this it says "Fehler"/"Warnung" and every parser drifts. The
# telemetry opt-out rides along so no build run through this script reports to Microsoft, whatever
# the machine's own environment says.
ENGLISH = {**os.environ, "DOTNET_CLI_UI_LANGUAGE": "en", "DOTNET_CLI_TELEMETRY_OPTOUT": "1"}

# How many direct ILogger calls the solution is allowed to hold. Zero since #654 took the last of 197
# out. Raising this is a deliberate line in a diff, which is the point - the hook only sees edits made
# through the tool, and a file arriving by any other route is what this catches.
LOGGING_BASELINE = project.get("dotnet.logging_baseline", 0)

NUGET_ORG_PACKAGES = "https://api.nuget.org/v3-flatcontainer/"


def feed_settings(private):
    """Feed, versions URL and key file of [nuget] (the official feed) or [nuget.private] (`--private`).

    Only a library publishes itself. Without the block asked for, the feed is None and `publish` refuses
    instead of guessing.
    """
    section = "nuget.private" if private else "nuget"
    feed = project.get(f"{section}.feed")
    default_packages = (NUGET_ORG_PACKAGES if "api.nuget.org" in (feed or "")
                        else (feed or "").replace("/v3/index.json", "/v3/package/"))
    packages = project.get(f"{section}.packages", default_packages)
    api_key_file = os.path.expanduser(project.get(f"{section}.api_key_file", "~/.config/nuget-api-key"))

    return section, feed, packages, api_key_file

# What `verify --against` and `publish` compare a branch to.
MAIN_BRANCH = project.get("repo.main_branch", "master")

# The character classes have to exclude the newline. [^(] matches one too, so a line without a
# position glued itself to the next line's path and the same warning was reported twice, once under
# a path that does not exist. MSBuild also prints a warning from a referenced project twice, once
# carrying the build node id ("21>/path/File.cs(67,53): ..."), so that prefix is dropped as well.
NODE_ID = r"(?:[ \t]*\d+>)?[ \t]*"
WARNING = re.compile(
    rf"^{NODE_ID}(?P<file>[^(\r\n]+)\((?P<line>\d+),\d+\): warning (?P<code>[A-Z]+\d+): (?P<text>[^\[\r\n]*)",
    re.MULTILINE)
# Project-level warnings (NU*, MSB*) carry no file position and would otherwise never be seen.
PROJECT_WARNING = re.compile(
    r"^(?!.*\(\d+,\d+\)).*?: warning (?P<code>[A-Z]+\d+): (?P<text>[^\[]+)(?:\[(?P<project>[^\]]+)\])?",
    re.MULTILINE)
# Any warning at all, by code and text: the trimmer's own ("ILLink : Trim analysis warning IL2026: ...") match
# neither pattern above, and in a trimmed Android build those are the ones that say what breaks at run time.
ANY_WARNING = re.compile(r"\bwarning (?P<code>[A-Z]+\d+): (?P<text>[^\[\r\n]*)")
ERROR = re.compile(
    rf"^{NODE_ID}(?P<file>[^(\r\n]+)\((?P<line>\d+),\d+\): error (?P<code>[A-Z]+\d+): (?P<text>[^\[\r\n]*)",
    re.MULTILINE)


def solution():
    return str(project.require_path("dotnet.solution"))


def need(value, key):
    """A configured path this command cannot do without, or a refusal naming the key to add."""
    if value is None:
        sys.stderr.write(f"refused: {project.config_file} has no {key!r}, which this command needs.\n")
        sys.exit(2)

    return str(value)


def run(command, **kwargs):
    # errors="replace": a `git diff` carries both sides, and a file committed years ago in Latin-1
    # puts a byte in there that is not UTF-8. Strict decoding turned that into a traceback out of
    # `verify` - a build check that cannot report on a branch because one old README has an "ü" in it
    # is worse than one that prints a replacement character in a line it only counts.
    return subprocess.run(command, cwd=REPO, env=ENGLISH, capture_output=True, text=True,
                          errors="replace", **kwargs)


def solution_projects():
    """Absolute paths of the projects in the solution, keyed by their file name stem."""
    content = Path(solution()).read_text(encoding="utf-8-sig")
    projects = {}

    for relative in sorted(set(re.findall(r'"([^"]+\.csproj)"', content))):
        path = os.path.normpath(os.path.join(REPO, relative.replace("\\", os.sep)))
        projects[os.path.basename(path)[:-len(".csproj")]] = path

    return projects


def resolve_project(name):
    """Accepts a full project name or any unambiguous fragment ("Api", "Repositories")."""
    projects = solution_projects()

    if name in projects:
        return projects[name]

    matches = [key for key in projects if name.lower() in key.lower()]

    if len(matches) == 1:
        return projects[matches[0]]

    if not matches:
        raise SystemExit(f"No project matches '{name}'. Known:\n  " + "\n  ".join(sorted(projects)))

    raise SystemExit(f"'{name}' is ambiguous:\n  " + "\n  ".join(sorted(matches)))


def build_properties():
    """`[dotnet.build_properties]` as `-p:Key=Value`, `~` expanded.

    For what a build needs from this machine rather than from the repository - where the Android SDK
    and the JDK live for a MAUI project - so neither a machine path ends up in a .csproj nor the build
    depends on environment variables a shell may or may not have.
    """
    configured = project.get("dotnet.build_properties") or {}

    return [f"-p:{key}={os.path.expanduser(str(value))}" for key, value in configured.items()]


def build(fatal_warnings=None, target=None, extra=()):
    """Build the solution - or one project, where `target` names it - returning its errors and warnings.

    `fatal_warnings` turns the listed codes into errors. It belongs here rather than in a build step
    of its own: a pipeline that guards a code with a separate `dotnet build` pays for the whole
    solution twice, which was 111 of 300 seconds on this repository's CI.
    """
    # Always a full build: an incremental one skips untouched projects and reports a warning count
    # that looks reassuringly low without meaning anything.
    command = ["dotnet", "build", target or solution(), "-v", "q", "--nologo", "--no-incremental"]
    command += build_properties()
    command += list(extra)

    if fatal_warnings:
        command.append(f"-warnaserror:{','.join(fatal_warnings)}")

    completed = run(command)
    output = completed.stdout + completed.stderr

    errors = [(m["file"].strip(), int(m["line"]), m["code"], m["text"].strip()) for m in ERROR.finditer(output)]
    warnings = {(m["file"].strip(), int(m["line"]), m["code"], m["text"].strip())
                for m in WARNING.finditer(output)}
    project = {(m["code"], m["text"].strip(), os.path.basename(m["project"] or "").strip())
               for m in PROJECT_WARNING.finditer(output)}

    # A restore that fails (a package downgrade, a feed that does not answer) prints errors without a
    # line number, which ERROR does not match. A non-zero exit with nothing parsed read as a clean build.
    if completed.returncode != 0 and not errors:
        errors = unparsed_errors(output, target or solution())

    return errors, warnings, project


UNPARSED_ERROR = re.compile(r"^(?P<file>.*?) ?: (?:error|Warning As Error:?) ?(?P<code>[A-Z]+\d+):? (?P<text>.*)$", re.M)


def unparsed_errors(output, fallback_file):
    """The errors of a failed build that carry no line number, or the last line of output if there are none."""
    found = [(m["file"].strip() or fallback_file, 0, m["code"], m["text"].strip())
             for m in UNPARSED_ERROR.finditer(output)]

    if found:
        return sorted(set(found))

    # The last lines of an MSBuild run are its summary (elapsed time, counts), which says nothing about why.
    summary = re.compile(r"^(Time Elapsed|Build FAILED|\d+ (Warning|Error)\(s\))", re.I)
    tail = [line.strip() for line in output.splitlines() if line.strip() and not summary.match(line.strip())][-5:]

    return [(fallback_file, 0, "BUILD", line) for line in tail] or [(fallback_file, 0, "BUILD", "dotnet build failed")]


def changed_lines(against):
    """Line ranges this branch touched, so a warning that was already there in a file we happened to
    edit is not reported as if we caused it."""
    ranges = {}

    for source in (["git", "diff", "-U0", f"{against}...HEAD"], ["git", "diff", "-U0", "HEAD"]):
        current = None

        for line in run(source).stdout.splitlines():
            if line.startswith("+++ b/"):
                current = os.path.normpath(os.path.join(REPO, line[6:].strip()))
            elif line.startswith("@@") and current:
                match = re.search(r"\+(\d+)(?:,(\d+))?", line)

                if match:
                    start = int(match[1])
                    ranges.setdefault(current, []).append((start, start + max(int(match[2] or 1), 1) - 1))

    return ranges


def changed_files(against):
    files = {line.strip() for line in run(["git", "diff", "--name-only", f"{against}...HEAD"]).stdout.splitlines()
             if line.strip()}
    files |= {line[3:].strip() for line in run(["git", "status", "--porcelain"]).stdout.splitlines()
              if line.strip()}

    return {os.path.normpath(os.path.join(REPO, f)) for f in files}


def uses_testing_platform():
    """Whether global.json switched `dotnet test` over to Microsoft.Testing.Platform.

    The .NET 10 SDK refuses VSTest for an MTP project (MSTest.Sdk 4 is one) until global.json says
    `"test": {"runner": "Microsoft.Testing.Platform"}`, and from then on `dotnet test` takes different
    arguments and prints a different summary.
    """
    try:
        with open(os.path.join(REPO, "global.json"), encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, ValueError):
        return False

    return settings.get("test", {}).get("runner") == "Microsoft.Testing.Platform"


def parse_testing_platform_summary(output):
    """The counts of an MTP run, from the block after "Test run summary:"."""
    start = output.rfind("Test run summary:")

    if start == -1:
        return None

    counts = dict(re.findall(r"^\s*(total|failed|succeeded|skipped):\s+(\d+)\s*$", output[start:], re.MULTILINE))

    if "total" not in counts:
        return None

    return {"failed": int(counts.get("failed", 0)),
            "passed": int(counts.get("succeeded", 0)),
            "skipped": int(counts.get("skipped", 0)),
            "total": int(counts["total"])}


TEST_LOG_KEPT = 20


def test_log_folder():
    return Path.home() / ".cache" / "devscripts" / "test-logs" / project.root.name


def save_test_log(output):
    """Keep the whole output of a test run, so a failure that does not come back can still be read.

    A run that fails once and passes on the next is gone from the terminal by the time anyone asks
    why. The logs live outside the repository (one folder per project), newest last in the name order.
    """
    folder = test_log_folder()
    folder.mkdir(parents=True, exist_ok=True)

    path = folder / f"{time.strftime('%Y%m%d-%H%M%S')}.log"
    path.write_text(output, encoding="utf-8")

    for old in sorted(folder.glob("*.log"))[:-TEST_LOG_KEPT]:
        old.unlink()

    return path


def run_tests(test_filter=None, detailed=True):
    """Run the tests with the output that names the assertion, and keep that output as a log.

    `detailed` stays in the signature for the callers that pass it; the assertion of a failed test is
    only written at this verbosity, and a failure nobody can read cannot be acted on.
    """
    if uses_testing_platform():
        # Everything after the project is handed to the test application, which exits on an option
        # it does not know - `--nologo` made every run end in "Zero tests ran".
        command = ["dotnet", "test", "--project", need(TEST_PROJECT, "dotnet.test_project"), "-v", "q",
                   "--output", "Detailed"]

        if test_filter:
            command += ["--filter", test_filter]

        completed = run(command)
        output = completed.stdout + completed.stderr
        save_test_log(output)

        return parse_testing_platform_summary(output), output

    # The assertion lives in the console *logger*, not in the msbuild verbosity: -v n prints more
    # build noise and not one word more about the test that failed.
    command = ["dotnet", "test", need(TEST_PROJECT, "dotnet.test_project"), "-v", "q", "--nologo",
               "--logger", "console;verbosity=detailed"]

    if test_filter:
        command += ["--filter", test_filter]

    completed = run(command)
    output = completed.stdout + completed.stderr
    save_test_log(output)

    # One line per test project: a solution or filter with several of them is the sum of its lines.
    summaries = re.findall(r"(?:Failed|Passed)!\s+-\s+Failed:\s+(\d+),\s+Passed:\s+(\d+),\s+Skipped:\s+(\d+),"
                           r"\s+Total:\s+(\d+)", output)

    if summaries:
        failed, passed, skipped, total = (sum(int(line[column]) for line in summaries) for column in range(4))

        return {"failed": failed, "passed": passed, "skipped": skipped, "total": total}, output

    # The detailed console logger replaces that one line with a block of its own, and prints a
    # count only for the outcomes that occurred.
    counts = {}

    for name, number in re.findall(r"^\s*(Total tests|Passed|Failed|Skipped):\s+(\d+)\s*$", output, re.MULTILINE):
        counts[name] = counts.get(name, 0) + int(number)

    if "Total tests" not in counts:
        return None, output

    return {"failed": int(counts.get("Failed", 0)),
            "passed": int(counts.get("Passed", 0)),
            "skipped": int(counts.get("Skipped", 0)),
            "total": int(counts["Total tests"])}, output


def build_target(name):
    """A project to build on its own: a path to a project file or a folder holding one, or a name in the solution.

    A project that is not in the solution - an Android app a CI runner has no workload for - is built
    by its path; everything else by the fragment `packages --project` takes.
    """
    path = Path(name) if Path(name).is_absolute() else Path(REPO) / name

    if path.is_dir():
        projects = sorted(path.glob("*.csproj"))

        if len(projects) != 1:
            raise SystemExit(f"{name} holds {len(projects)} project files, not one.")

        return str(projects[0])

    return str(path) if path.is_file() else resolve_project(name)


def command_build(arguments):
    target = build_target(arguments.project)

    print(f"Building {os.path.relpath(target, REPO)} ...")
    extra = [f"-t:{arguments.target}"] if arguments.target else []
    extra += [f"-p:{pair}" for pair in arguments.property]
    errors, warnings, project_warnings = build(target=target, extra=extra)

    if errors:
        print(f"\nBUILD FAILED - {len(errors)} error(s):\n")

        for path, line, code, text in errors[:max(30, arguments.limit)]:
            print(f"  {os.path.relpath(path, REPO)}:{line}  {code}  {text}")

        return 1

    print(f"  build clean, {len(warnings)} distinct warning(s)"
          + (f" + {len(project_warnings)} project-level" if project_warnings else ""))

    for path, line, code, text in sorted(warnings)[:arguments.limit]:
        print(f"    {os.path.relpath(path, REPO)}:{line}  {code}  {text[:110]}")

    return 0


def command_verify(arguments):
    fatal =arguments.fatal_warnings.split(",") if getattr(arguments, "fatal_warnings", None) \
        else project.get("dotnet.fatal_warnings", [])

    print("Building ..." + (f" ({','.join(fatal)} fatal)" if fatal else ""))
    errors, warnings, project_warnings = build(fatal)

    if errors:
        print(f"\nBUILD FAILED - {len(errors)} error(s):\n")

        for path, line, code, text in errors[:20]:
            print(f"  {os.path.relpath(path, REPO)}:{line}  {code}  {text}")

        return 1

    print(f"  build clean, {len(warnings)} distinct warning(s)"
          + (f" + {len(project_warnings)} project-level" if project_warnings else ""))

    for code, text, owner in sorted(project_warnings):
        marker = "  !!" if code.startswith("NU19") else "   -"
        print(f"{marker} {code} ({owner}): {text[:110]}")

    touched = changed_files(arguments.against)
    ranges = changed_lines(arguments.against)
    introduced = sorted(w for w in warnings
                        if any(start <= w[1] <= end for start, end in ranges.get(w[0], [])))
    nearby = sorted(w for w in warnings if w[0] in touched and w not in introduced)

    if introduced:
        print(f"\n  {len(introduced)} warning(s) ON LINES THIS BRANCH CHANGED:")

        for path, line, code, text in introduced:
            print(f"    {os.path.relpath(path, REPO)}:{line}  {code}  {text}")
    else:
        print(f"  no warnings on changed lines ({len(touched)} file(s) touched vs {arguments.against})")

    if nearby:
        print(f"  ({len(nearby)} pre-existing warning(s) elsewhere in those files)")

    if arguments.baseline:
        print(f"\nBuilding {arguments.against} for comparison ...")
        stash = run(["git", "stash", "push", "-u", "-q"])
        head = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
        run(["git", "checkout", "-q", arguments.against])

        try:
            _, base_warnings, _ = build()
            print(f"  {arguments.against}: {len(base_warnings)}   this branch: {len(warnings)}   "
                  f"delta: {len(warnings) - len(base_warnings):+d}")
        finally:
            run(["git", "checkout", "-q", head])

            if "No local changes" not in stash.stdout:
                run(["git", "stash", "pop", "-q"])

    if arguments.quick:
        return 0

    tests = command_test(arguments)

    if tests != 0:
        return tests

    logging = check_logging(arguments)

    if logging != 0:
        return logging

    grids = check_istarnova(arguments)

    if grids != 0:
        return grids

    licence = check_spdx(arguments)

    if licence != 0:
        return licence

    containerfiles = check_containerfiles(arguments)

    if containerfiles != 0:
        return containerfiles

    translations = check_translations()

    if translations != 0:
        return translations

    project_scripts = check_project_scripts()

    if project_scripts != 0:
        return project_scripts

    return check_db_init(arguments)


# Every name a logger goes by here, and every level. Not the generated Log* methods, which are the
# answer rather than the problem, and not the .Log.cs files, where the levels appear in the attribute.
DIRECT_LOGGER = re.compile(
    r"[A-Za-z_]*[Ll]ogger\??\.Log(?:Information|Warning|Error|Debug|Critical|Trace)\s*\(")


def direct_logger_calls():
    """Every direct ILogger call in the solution's own sources, as (path, line)."""
    found = []

    for folder, directories, files in os.walk(REPO):
        directories[:] = [name for name in directories
                          if name not in {"bin", "obj", ".git", "Data", "node_modules"}]

        for name in files:
            if not name.endswith((".cs", ".razor")) or name.endswith(".Log.cs"):
                continue

            path = os.path.join(folder, name)

            with open(path, encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    if DIRECT_LOGGER.search(line):
                        found.append((os.path.relpath(path, REPO), number))

    return found


def check_logging(arguments):
    """
    Report whether anything logs through ILogger directly instead of a generated [LoggerMessage].

    The hook only sees edits made through the tool; a file arriving by any other route needs this.
    The number may fall and never rise, so the baseline is a committed constant rather than a file
    somebody has to remember to refresh - lowering it is a deliberate line in a diff.
    """
    if arguments.no_logging_check:
        return 0

    print("\nChecking for direct ILogger calls ...")

    found = direct_logger_calls()

    if len(found) <= LOGGING_BASELINE:
        print(f"  {len(found)} direct call(s), baseline {LOGGING_BASELINE}")

        return 0

    print(f"  {len(found)} direct call(s), above the baseline of {LOGGING_BASELINE}:")

    for path, line in found[:20]:
        print(f"    {path}:{line}")

    print("\n  Use RepositoryLogger in a repository, ComponentLogger in a component, or a"
          "\n  {ClassName}.Log.cs partial with [LoggerMessage] - see .claude/CLAUDE.md.")

    return 1


# The opening tag of the raw grid. LocalizedDataGrid and FluentDataGridRow are different words.
RAW_DATA_GRID = re.compile(r"<FluentDataGrid(?=[\s>/]|$)")


def raw_data_grids(root=None):
    """Every raw <FluentDataGrid> opening tag in the .razor files under root, as (path, line)."""
    root = root or REPO
    found = []

    for folder, directories, files in os.walk(root):
        directories[:] = [name for name in directories
                          if name not in {"bin", "obj", ".git", "Data", "node_modules"}]

        for name in files:
            if not name.endswith(".razor"):
                continue

            path = os.path.join(folder, name)

            with open(path, encoding="utf-8", errors="replace") as handle:
                for number, line in enumerate(handle, 1):
                    if RAW_DATA_GRID.search(line):
                        found.append((os.path.relpath(path, root), number))

    return found


def check_istarnova(arguments):
    """
    Report raw <FluentDataGrid> in a project that uses the Istarnova building blocks.

    The raw grid draws its menus, empty message and paginator in English whatever the session speaks;
    LocalizedDataGrid does not. The hook only sees edits made through the tool, a file arriving by any
    other route needs this. A project without an [istarnova] block is not asked. The baseline is
    istarnova.raw_grid_baseline, zero unless the project says otherwise - raising it is a line in a diff.
    """
    # An empty [istarnova] table is a block too, so presence is the test, not truthiness.
    if arguments.no_istarnova_check or project.get("istarnova") is None:
        return 0

    baseline = project.get("istarnova.raw_grid_baseline", 0)

    print("\nChecking for raw FluentDataGrid ...")

    found = raw_data_grids()

    if len(found) <= baseline:
        print(f"  {len(found)} raw grid(s), baseline {baseline}")

        return 0

    print(f"  {len(found)} raw grid(s), above the baseline of {baseline}:")

    for path, line in found[:20]:
        print(f"    {path}:{line}")

    print("\n  Use LocalizedDataGrid (or LocalizedSearchableDataGrid) from Istarnova.Blazor.FluentUI - see the"
          "\n  blazor-ui skill, references/building-blocks.md.")

    return 1


def check_spdx(arguments):
    """
    Report the project's own source files that carry no SPDX licence lines.

    The header template and the generators write them; a file written some other way would not, and
    nothing else would notice. A project with no [spdx] block is not asked - importing spdx would
    refuse over the holder it has never named.
    """
    if arguments.no_spdx_check or not project.get("spdx"):
        return 0

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


PROJECT_REFERENCE = re.compile(r'<ProjectReference\s[^>]*?Include="([^"]+)"', re.IGNORECASE)
CONTAINERFILE_PROJECT_COPY = re.compile(r"^\s*COPY\s+\S+/([^/\s]+\.csproj)\s", re.IGNORECASE | re.MULTILINE)


def referenced_projects(entry):
    """The names of every project file the entry reaches through ProjectReference, itself included."""
    names = set()
    pending = [os.path.normpath(entry)]

    while pending:
        path = pending.pop()

        if os.path.basename(path) in names or not os.path.isfile(path):
            continue

        names.add(os.path.basename(path))

        with open(path, encoding="utf-8", errors="replace") as handle:
            for reference in PROJECT_REFERENCE.findall(handle.read()):
                pending.append(os.path.normpath(os.path.join(os.path.dirname(path), reference.replace("\\", "/"))))

    return names


def check_containerfiles(arguments):
    """
    Report a project a Containerfile's restore layer does not copy.

    The list of COPY lines is the restore graph and is written by hand, so it drifts: it once named a
    project that no longer existed and left out three that did, and the next two were added with a
    feature and not here. A project the layer misses is restored again in the publish step, so the
    build still works - it only stops being cached. Naming a project the graph does not reach costs a
    layer line and is allowed, so only a missing one fails.

    [containerfile] entries = [["Containerfile", "path/to/Web.csproj"], ...] in project.toml; a
    project with no such block is not asked.
    """
    entries = project.get("containerfile.entries")

    if arguments.no_containerfile_check or not entries:
        return 0

    print("\nChecking the restore layers of the Containerfiles ...")

    failed = False

    for containerfile, entry_project in entries:
        path = os.path.join(REPO, containerfile)

        if not os.path.isfile(path):
            print(f"  {containerfile}: not there, skipped")

            continue

        with open(path, encoding="utf-8", errors="replace") as handle:
            copied = {name for name in CONTAINERFILE_PROJECT_COPY.findall(handle.read())}

        missing = sorted(referenced_projects(os.path.join(REPO, entry_project)) - copied)

        if not missing:
            print(f"  {containerfile}: every project of {os.path.basename(entry_project)} is copied")

            continue

        failed = True
        print(f"  {containerfile}: {len(missing)} project(s) the restore layer does not copy:")

        for name in missing:
            print(f"    {name}")

    if failed:
        print("\n  Add a COPY line for each next to the others; the layer cache holds only what is copied.")

    return 1 if failed else 0


def check_translations():
    """
    Report translations the generator would accept and the application would show wrong.

    A translation contribution can touch nothing but the JSON sources, so this is the check it gets:
    placeholders that differ from the base text, unknown languages, a key without its base text.
    Missing and outdated translations are counted, not failed - a language is allowed to be incomplete.
    """
    script = os.path.join(SCRIPTS, "translations.py")

    if not project.get("localization") or not os.path.isfile(script):
        return 0

    print("\nChecking translations ...")

    result = subprocess.run([sys.executable, script, "status"], cwd=REPO, capture_output=True, text=True)

    for line in (result.stdout + result.stderr).strip().splitlines():
        print(f"  {line}")

    return result.returncode


def check_project_scripts():
    """
    Run the checks a project keeps in its own Scripts/ and wants every `verify` to include.

    What a project's folders, routes or layouts must agree on is a fact about that project, so the check
    cannot live here; but it is only worth anything if it runs with everything else instead of being
    remembered. `verify.checks` in project.toml names the scripts, relative to Scripts/. Each is run
    with this interpreter from the repository root, prints what it found, and fails by exit code.
    """
    names = project.get("verify.checks") or []

    for name in names:
        script = os.path.join(SCRIPTS, name)

        print(f"\nRunning {name} ...")

        if not os.path.isfile(script):
            print(f"  not found: verify.checks names {name}, Scripts/ has no such file")

            return 1

        result = subprocess.run([sys.executable, script], cwd=REPO, capture_output=True, text=True)

        for line in (result.stdout + result.stderr).strip().splitlines():
            print(f"  {line}")

        if result.returncode != 0:
            return result.returncode

    return 0


def check_db_init(arguments):
    """
    Report whether the db-init schema file still matches the model.

    That file is the whole self-hosted install path, and it had drifted 167 columns behind before
    anything noticed. Nobody edits it by hand, so the only way it stays correct is a check that runs
    with everything else.

    A project with no [db_init] block has no such file and is not asked about one - Istarnova's CI
    failed here, on a generator its repository correctly does not carry. A machine that cannot reach
    the development database reports "skipped" and does not fail either: the comparison is impossible
    there, and blocking a build over it would only teach people to pass --no-db-check by reflex.
    """
    generator = os.path.join(SCRIPTS, "gen_db_init_delta.py")

    if arguments.no_db_check or not project.get("db_init") or not os.path.isfile(generator):
        return 0

    print(f"\nChecking {project.get('db_init.schema_file', 'the db-init schema')} ...")

    result = subprocess.run([sys.executable, generator, "--check"],
                            cwd=REPO, capture_output=True, text=True)

    for line in (result.stdout + result.stderr).strip().splitlines():
        print(f"  {line}")

    if result.returncode == 0:
        report_new_schemas(getattr(arguments, "against", "master"))

    return result.returncode


def new_schemas_in(diff_text):
    """The schemas a diff of the db-init file adds, in the order it adds them."""
    return re.findall(r"^\+CREATE SCHEMA (?:IF NOT EXISTS )?(\w+);", diff_text, re.MULTILINE)


def report_new_schemas(against):
    """
    Say so when the db-init schema file gains a schema, because the application's login role has no
    rights on it until its role script has run again on every instance.

    Nothing fails here, and no database is asked: the role is what the script grants, and what an
    instance already granted cannot be known from this checkout. The first run of the pull request
    that adds a schema is when it is cheapest to remember; a Docker or Quadlet instance finds out
    from "permission denied for schema" in the first page that reads it.
    """
    role_script = project.get("db_init.app_role_script")
    schema_file = project.get("db_init.schema_file")

    if not role_script or not schema_file:
        return

    diff = subprocess.run(["git", "diff", "-U0", against, "--", schema_file],
                          cwd=REPO, capture_output=True, text=True)

    if diff.returncode != 0:
        return

    added = new_schemas_in(diff.stdout)

    if added:
        print(f"  new schema {', '.join(added)}: run {role_script} again on every instance, "
              "or the application role cannot read it")


def command_warnings(arguments):
    print("Building ...")
    errors, warnings, project = build()

    if errors:
        print(f"BUILD FAILED - {len(errors)} error(s)")

        return 1

    if arguments.code:
        matching = sorted(w for w in warnings if w[2] == arguments.code)
        print(f"\n{arguments.code}: {len(matching)} occurrence(s)\n")

        for path, line, _, text in matching:
            print(f"  {os.path.relpath(path, REPO)}:{line}  {text}")

        return 0

    print(f"\n{len(warnings)} distinct warning(s), by code:\n")

    for code, count in collections.Counter(code for _, _, code, _ in warnings).most_common():
        print(f"  {code:<12} {count:>4}")

    if project:
        print(f"\n{len(project)} project-level:\n")

        for code, text, owner in sorted(project):
            print(f"  {code:<12} {owner}: {text}")

    return 0


def command_test(arguments):
    print("\nTesting ...")

    result, output = run_tests(getattr(arguments, "filter", None))

    if result is None:
        print("  could not read the test summary:\n")
        print("\n".join(output.splitlines()[-15:]))

        return 1

    print(f"  {result['passed']} passed, {result['failed']} failed, {result['skipped']} skipped")

    if result["failed"]:
        print_failures(output)
        print(f"\n  whole output: {sorted(test_log_folder().glob('*.log'))[-1]}")

    return 1 if result["failed"] else 0


def print_failures(output):
    """Name each failing test and say what it asserted - "1 failed" alone cannot be acted on.

    The [FAIL] line carries a theory's arguments, which is the part that says *which* case went; the
    assertion ("expected 9, actual 10") follows in the Error Message block.
    """
    failures = [line.strip() for line in output.splitlines() if "[FAIL]" in line]

    for failure in failures:
        print(f"\n  {failure}")

    # Only the block right after a "Failed" line: a skipped test has an Error Message too, with no stack trace, and a
    # pattern that starts at any Error Message would run on from it into the next failure.
    for block in re.findall(r"^\s*Failed \S[^\n]*\n\s*Error Message:\s*\n(.*?)(?=^\s*Stack Trace:)", output,
                            re.MULTILINE | re.DOTALL):
        print("".join(f"    {line.strip()}\n" for line in block.splitlines() if line.strip()), end="")


def set_package_versions(assignments):
    """Set `Version` of each `ID=VERSION` in every project and props file of the repository.

    A package that no file references is an error, not a skip: a typo in the id would otherwise look like
    an upgrade that went through.
    """
    wanted = {}

    for assignment in assignments:
        identifier, separator, version = assignment.partition("=")

        if not separator or not identifier or not version:
            print(f"Not ID=VERSION: {assignment}")

            return 2

        wanted[identifier] = version

    files = [path for pattern in ("*.csproj", "*.props", "*.targets") for path in Path(REPO).rglob(pattern)
             if not {"bin", "obj", "node_modules", ".git"} & set(path.parts)]
    touched = collections.defaultdict(list)

    for path in sorted(files):
        text = path.read_text(encoding="utf-8-sig")
        original = text

        for identifier, version in wanted.items():
            pattern = (r'(<Package(?:Reference|Version)\s+Include="' + re.escape(identifier)
                       + r'"[^>]*?\bVersion=")([^"]*)(")')
            text, count = re.subn(pattern, lambda found: found[1] + version + found[3], text)

            if count:
                touched[identifier].append(path.relative_to(REPO))

        if text != original:
            path.write_text(text, encoding="utf-8")

    for identifier, version in wanted.items():
        if identifier not in touched:
            print(f"{identifier}: referenced nowhere")

            return 1

        print(f"{identifier} -> {version}: {len(touched[identifier])} file(s)")

    return 0


def command_packages(arguments):
    if arguments.set:
        return set_package_versions(arguments.set)

    if arguments.versions:
        # Which versions exist on nuget.org - needed to pin a patched one over a vulnerable transitive.
        completed = run(["dotnet", "package", "search", arguments.versions,
                         "--exact-match", "--take", "40", "--format", "table"])
        print((completed.stdout + completed.stderr).strip())

        return 0

    target = resolve_project(arguments.project) if arguments.project else solution()
    command = ["dotnet", "list", target, "package"]

    if arguments.vulnerable:
        command += ["--vulnerable", "--include-transitive"]
    elif arguments.outdated:
        command.append("--outdated")
    elif arguments.transitive:
        command.append("--include-transitive")

    completed = run(command)
    output = (completed.stdout + completed.stderr).strip()
    print(output if output else "(no output)")

    if arguments.versions:
        return 0

    if arguments.vulnerable and "has the following vulnerable packages" not in output:
        print("\nNo vulnerable packages reported.")

    return 0


def command_projects(_arguments):
    projects = solution_projects()
    print(f"{len(projects)} project(s) in the solution:\n")

    for name, path in sorted(projects.items()):
        frameworks = "?"

        if os.path.exists(path):
            content = Path(path).read_text(encoding="utf-8-sig")
            match = re.search(r"<TargetFrameworks?>([^<]+)<", content)
            frameworks = match[1] if match else "?"

        marker = " " if os.path.exists(path) else "!"
        print(f" {marker} {name:<52} {frameworks}")

    return 0


def command_ef(arguments):
    """EF Core commands with this repository's two projects filled in.

    Every `dotnet ef` line here carries the same --project/--startup-project pair, and the migrations
    folder is git-ignored on purpose, so the whole workflow is local and repetitive. The one thing
    worth remembering is not a flag: `migrations add` scaffolds every difference between the model and
    the last snapshot, which in this repository includes a backlog of columns already applied by hand.
    Read what it wrote before applying it — hence the reminder after add.
    """
    ef = ["dotnet", "ef",
          "--project", need(EF_PROJECT, "dotnet.ef_project"),
          "--startup-project", need(STARTUP_PROJECT, "dotnet.startup_project")]

    if arguments.action == "add":
        if not arguments.name:
            raise SystemExit("A migration needs a name: dotnet.py ef add AddSomething")

        command = ef[:2] + ["migrations", "add", arguments.name] + ef[2:]
    elif arguments.action == "remove":
        command = ef[:2] + ["migrations", "remove"] + ef[2:]
    elif arguments.action == "list":
        command = ef[:2] + ["migrations", "list"] + ef[2:]
    elif arguments.action == "script":
        command = ef[:2] + ["migrations", "script", "--idempotent"] + ef[2:]
    else:
        command = ef[:2] + ["database", "update"] + ef[2:]

        if arguments.name:
            command.append(arguments.name)

    completed = subprocess.run(command + list(arguments.arguments), cwd=REPO, env=ENGLISH)

    if arguments.action == "add" and completed.returncode == 0:
        print("\nRead the generated migration before applying it. `migrations add` scaffolds every\n"
              "difference between the model and the snapshot, and this repository carries columns that\n"
              "were applied by hand — those come along and fail on a database that already has them.\n"
              "Not what you asked for? `python3 Scripts/dotnet.py ef remove`.")

    return completed.returncode


def command_android(arguments):
    if arguments.action == "devices":
        android.connect()
        print(android.adb("devices", "-l").stdout.strip())

        return 0

    if arguments.action == "run":
        android.require_device()
        output = android.adb("shell", "monkey", "-p", android.application_id(), "-c",
                             "android.intent.category.LAUNCHER", "1").stdout

        print(output.strip().splitlines()[-1] if output.strip() else "started")

        return 0

    if arguments.action == "dependencies":
        return install_android_dependencies()

    if arguments.action == "release":
        return android_release(arguments.ref, arguments.check)

    configuration = "Release" if arguments.release else "Debug"
    apk = None if arguments.no_build else build_apk(configuration)

    if apk is None:
        apk = android.newest_apk(configuration)

    if apk is None:
        sys.stderr.write(f"refused: no {configuration} .apk built yet - run `android build` first.\n")

        return 1

    print(f"  {os.path.relpath(apk, REPO)}  ({apk.stat().st_size / 1024 / 1024:.1f} MiB)")

    if arguments.action == "build":
        return 0

    android.require_device()
    result = android.adb("install", "-r", str(apk), check=False)
    output = (result.stdout + result.stderr).strip()

    if result.returncode != 0 or "Success" not in output:
        # INSTALL_FAILED_UPDATE_INCOMPATIBLE: the installed copy was signed with another key - the debug
        # key against the release one. Android refuses it by design; uninstalling loses the app's data.
        print(f"INSTALL FAILED\n{output}")

        if "UPDATE_INCOMPATIBLE" in output:
            print(f"  signed with a different key than the installed copy: "
                  f"adb uninstall {android.application_id()} (this deletes its data), then install again")

        return 1

    print(f"  installed {android.application_id()}")

    return 0


def android_release(ref, check):
    """The release APK of one commit, built in the container of [android.release] from a clean clone of it.

    --check builds it a second time, in a second clone with an empty NuGet cache, and fails unless both unsigned
    APKs are the same bytes: the proof that anyone with the commit and the Containerfile gets this APK.
    """
    # Its lines come between podman's and the build's, which write straight to the terminal.
    sys.stdout.reconfigure(line_buffering=True)
    settings = android.release_settings()
    commit = subprocess.run(["git", "-C", REPO, "rev-parse", "--verify", f"{ref}^{{commit}}"],
                            capture_output=True, text=True)

    if commit.returncode != 0:
        sys.stderr.write(f"refused: {ref} is not a commit\n")

        return 2

    commit = commit.stdout.strip()
    work = Path(tempfile.mkdtemp(prefix="android-release-"))
    print(f"Release APK of {commit[:12]} ({ref}), working in {work}")

    try:
        first = build_release_in_container(settings, commit, work / "first", settings["nuget_volume"],
                                           sign=True)

        if first is None:
            return 1

        if check:
            fresh_volume = f"{settings['nuget_volume']}-check"
            subprocess.run(["podman", "volume", "rm", "-f", fresh_volume], capture_output=True)
            second = build_release_in_container(settings, commit, work / "second", fresh_volume, sign=False)
            subprocess.run(["podman", "volume", "rm", "-f", fresh_volume], capture_output=True)

            if second is None:
                return 1

            if unsigned_hash(first) != unsigned_hash(second):
                print(f"\nNOT REPRODUCIBLE: the two unsigned APKs differ - {first} and {second} are kept "
                      "for diffoscope")
                work = None

                return 1

            print(f"\nreproducible: both builds gave {unsigned_hash(first)}")

        target = android.app_project().parent / "bin" / "Release" / "reproducible" / commit[:12]
        target.mkdir(parents=True, exist_ok=True)

        for produced in first.iterdir():
            shutil.copy2(produced, target / produced.name)

        print(f"\n{(target / 'SHA256SUMS').read_text().strip()}\n  in {os.path.relpath(target, REPO)}")

        return 0
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)


def unsigned_hash(out):
    return hashlib.sha256((out / "companion-unsigned.apk").read_bytes()).hexdigest()


def build_release_in_container(settings, commit, directory, nuget_volume, sign):
    """Clone, build the toolchain image from that clone's Containerfile, run its build script; the output folder, or None."""
    source = directory / "src"
    out = directory / "out"
    out.mkdir(parents=True)

    for command in (["git", "clone", "--quiet", "--no-hardlinks", REPO, str(source)],
                    ["git", "-C", str(source), "checkout", "--quiet", "--detach", commit]):
        if subprocess.run(command).returncode != 0:
            return None

    image = settings["image"]
    print(f"\nToolchain image {image} from {settings['containerfile']} ...")

    if subprocess.run(["podman", "build", "-q", "-t", image, "-f", str(source / settings["containerfile"]),
                       str(source)], stdout=subprocess.DEVNULL).returncode != 0:
        return None

    # label=disable instead of :Z, which would relabel the signing key's folder on the host for good.
    command = ["podman", "run", "--rm", "--security-opt", "label=disable",
               "-v", f"{source}:/src", "-v", f"{out}:/out", "-v", f"{nuget_volume}:/root/.nuget/packages"]
    script = ["python3", f"/src/{settings['script']}", "--out", "/out"]

    if sign:
        signing = android.signing_files()

        for name, path in signing["files"].items():
            command += ["-v", f"{path}:/signing/{name}:ro"]

        script += ["--keystore", "/signing/keystore", "--alias", signing["alias"],
                   "--store-password-file", "/signing/store-password",
                   "--key-password-file", "/signing/key-password"]

    print(f"Building {commit[:12]} in the container{' (signed)' if sign else ''} ...")

    if subprocess.run([*command, image, *script]).returncode != 0:
        return None

    return out


def install_android_dependencies():
    """Install the Android SDK platform and build tools the target framework needs (accepts the SDK licences)."""
    csproj = android.app_project()
    framework = re.search(r"net\d+\.\d+-android", csproj.read_text(encoding="utf-8-sig"))

    if not framework:
        sys.stderr.write(f"refused: {csproj} has no net*-android target framework.\n")

        return 1

    command = ["dotnet", "build", str(csproj), "-t:InstallAndroidDependencies", "-f", framework.group(0),
               "-v", "q", "--nologo", "-p:AcceptAndroidSDKLicenses=True", *build_properties()]

    print(f"Installing the Android dependencies of {os.path.relpath(csproj, REPO)} ({framework.group(0)}) ...")
    completed = run(command)
    output = (completed.stdout + completed.stderr).strip()
    print("\n".join(output.splitlines()[-15:]))

    return completed.returncode


def build_apk(configuration):
    """Publish the MAUI project for Android and return the .apk, or None after printing why not."""
    csproj = android.app_project()
    framework = re.search(r"net\d+\.\d+-android", csproj.read_text(encoding="utf-8-sig"))

    if not framework:
        sys.stderr.write(f"refused: {csproj} has no net*-android target framework.\n")

        return None

    # Not `dotnet publish`: it produced a Release .apk whose runtime started but never registered the Java side
    # (UnsatisfiedLinkError in MauiApplication.n_onCreate at launch), while the SignAndroidPackage target gave a
    # working one from the same sources.
    command = ["dotnet", "build", str(csproj), "-t:SignAndroidPackage", "-f", framework.group(0), "-c", configuration,
               "-v", "q", "--nologo", "-p:AndroidPackageFormat=apk", *build_properties()]

    # A debug build deploys assemblies next to the app (fast deployment), which only works while the IDE
    # is attached. adb install needs them inside the .apk.
    if configuration == "Debug":
        command.append("-p:EmbedAssembliesIntoApk=true")
    else:
        command += android.signing_properties()

    print(f"Building {os.path.relpath(csproj, REPO)} ({configuration}, {framework.group(0)}) ...")
    before = time.time()
    completed = run(command)
    output = completed.stdout + completed.stderr

    if completed.returncode != 0:
        errors = [(m["file"].strip(), int(m["line"]), m["code"], m["text"].strip()) for m in ERROR.finditer(output)]
        errors = errors or unparsed_errors(output, str(csproj))
        print(f"\nBUILD FAILED - {len(errors)} error(s):\n")

        for path, line, code, text in sorted(set(errors))[:30]:
            print(f"  {os.path.relpath(path, REPO)}:{line}  {code}  {text}")

        return None

    warnings = sorted({(m["code"], m["text"].strip()) for m in ANY_WARNING.finditer(output)})

    if warnings:
        print(f"\n{len(warnings)} distinct warning(s):")

        for code, text in warnings[:40]:
            print(f"  {code}  {text}")

    apk = android.newest_apk(configuration)

    if apk is None:
        print("BUILD FAILED - the build succeeded but there is no .apk")

        return None

    # An unchanged project is an up-to-date build and leaves the .apk as it was.
    if apk.stat().st_mtime < before:
        print("  (up to date, the .apk is from an earlier build)")

    return apk


def csharp_scripts():
    """The single-file C# scripts in Scripts/csharp/, by their bare name."""
    if not os.path.isdir(CSHARP):
        return {}

    return {name[:-len(".cs")]: os.path.join(CSHARP, name)
            for name in sorted(os.listdir(CSHARP)) if name.endswith(".cs")}


def resolve_script(name):
    """Accepts a bare name, a file name, or a path relative to the repository."""
    scripts = csharp_scripts()

    if name in scripts:
        return scripts[name]

    candidate = name if os.path.isabs(name) else os.path.join(REPO, name)

    if os.path.isfile(candidate) and candidate.endswith(".cs"):
        return os.path.abspath(candidate)

    raise SystemExit(f"No C# script '{name}'. Known:\n  " + "\n  ".join(scripts) if scripts
                     else f"No C# script '{name}' and none in {CSHARP}.")


def command_script(arguments):
    scripts = csharp_scripts()

    if arguments.list or not arguments.name:
        if not scripts:
            print(f"No .cs scripts in {CSHARP}.")

            return 0

        print(f"{len(scripts)} C# script(s) in Scripts/csharp/:\n")

        for name, path in scripts.items():
            # The first comment line after the directives says what the script is for.
            purpose = ""

            for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
                if line.startswith("// ") and not line.startswith(("// File:", "// Created", "// Developer",
                                                                   "// Copyright", "// SPDX-")):
                    purpose = line[3:].strip()
                    break

            print(f"  {name:<34} {purpose}")

        return 0

    path = resolve_script(arguments.name)

    # `dotnet run file.cs` keys its build cache on the script file alone: edit a project the script
    # references and it happily re-runs the previous binary, so the run answers a question about the
    # code as it was. Touching the file first makes the reference build come along.
    os.utime(path, None)

    environment = dict(ENGLISH)

    if arguments.database:
        environment[project.env_name("CONNECTION")] = connection_string(arguments.database)

    # The configured database, for the scripts that only read. Deliberately a variable of its own:
    # the scripts that write refuse to run without {PREFIX}_CONNECTION, and filling that in by default
    # would point every one of them at the development record. Read-only scripts used to carry the
    # connection as a literal instead, which drifted the moment the [database] block changed.
    # Left out rather than failing when there is no [database] block or no ~/.pgpass line for it - a
    # script that never touches the database (the CI's model_create_script) must still run.
    if project.get("database"):
        try:
            environment[project.env_name("DEFAULT_CONNECTION")] = connection_string(
                project.env("DB_NAME", project.get("database.name", project.name)))
        except SystemExit:
            pass

    # Not captured: these talk to the database and can run for a while, so the output belongs on the
    # terminal as it happens rather than in one block at the end. Quiet verbosity because a script
    # drags the whole solution's build warnings along, which would bury its own output.
    # `--file`, because a repository with a .csproj in its root (a single-project game, say) makes a bare
    # `dotnet run script.cs` read the script as an argument to that project and fail with CS9298.
    command = ["dotnet", "run", "-v", "q", "--property:WarningLevel=0", "--file", path]

    return subprocess.run(command + list(arguments.arguments), cwd=REPO, env=environment).returncode


def packable_projects():
    """The projects that declare a <PackageId>, in solution order. What they reference without one is
    bundled into their packages; several of them are published together, under one version."""
    candidates = [path for _, path in sorted(solution_projects().items())
                  if os.path.exists(path)
                  and "<PackageId>" in Path(path).read_text(encoding="utf-8-sig")]

    if not candidates:
        raise SystemExit("No project declares a <PackageId>, so there is nothing to publish.")

    return candidates


def version_file(project_file):
    """Where a project's <Version> is written down: the .csproj itself, else the nearest Directory.Build.props
    above it inside the repository, which is where a set of packages released together keeps its one version."""
    if re.search(r"<Version>[^<]*</Version>", Path(project_file).read_text(encoding="utf-8-sig")):
        return project_file

    folder = Path(project_file).resolve().parent
    root = Path(REPO).resolve()

    while True:
        props = folder / "Directory.Build.props"

        if props.is_file() and re.search(r"<Version>[^<]*</Version>", props.read_text(encoding="utf-8-sig")):
            return str(props)

        if folder == root or folder.parent == folder:
            return project_file

        folder = folder.parent


def version_key(version):
    """Sort key in SemVer precedence: numbers compare as numbers, and a release outranks its prereleases
    (1.1.0 > 1.1.0-preview.10 > 1.1.0-preview.9). Build metadata after '+' does not count."""
    core, _, prerelease = version.split("+")[0].partition("-")
    numbers = tuple(int(part) for part in core.split("."))

    if not prerelease:
        return numbers, (1,)

    identifiers = tuple((0, int(part), "") if part.isdigit() else (1, 0, part)
                        for part in prerelease.split("."))

    return numbers, (0,) + identifiers


def next_version(published, declared):
    """The version the next publish gets.

    The last number of the highest version on the feed goes up by one - 1.1.0-preview.2 becomes
    1.1.0-preview.3, 1.1.0 becomes 1.1.1. A <Version> in the .csproj above everything published wins
    instead, which is how 1.2.0 or 2.0.0 is chosen on purpose, and how a prerelease line ends in its
    release.
    """
    highest = max(published, key=version_key, default=None)

    if highest is None or version_key(declared) > version_key(highest):
        return declared

    last_number = list(re.finditer(r"\d+", highest.split("+")[0]))[-1]

    return (f"{highest[:last_number.start()]}{int(last_number[0]) + 1}"
            f"{highest[last_number.end():].split('+')[0]}")


def published_versions(packages, package_id):
    """Every version of the package on the feed; none when the feed has never seen it."""
    try:
        with urllib.request.urlopen(f"{packages}{package_id.lower()}/index.json",
                                    timeout=15) as response:
            return json.load(response)["versions"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return []

        raise SystemExit(f"Could not read the versions on the feed: HTTP {error.code}")
    except urllib.error.URLError as error:
        raise SystemExit(f"Could not reach the feed: {error.reason}")


def write_version(project_file, version):
    """Record the published version where it is declared, so the repository says what is out there."""
    content = Path(project_file).read_text(encoding="utf-8-sig")
    updated, count = re.subn(r"<Version>[^<]*</Version>", f"<Version>{version}</Version>",
                             content, count=1)

    if count:
        Path(project_file).write_text(updated, encoding="utf-8")


def command_publish(arguments):
    section, feed, packages, api_key_file = feed_settings(arguments.private)

    if not feed:
        sys.stderr.write(f"refused: {project.config_file} has no '{section}.feed', "
                         "so there is nowhere to publish to.\n")

        return 2

    project_files = packable_projects()

    # Read the key before anything slow runs, so a missing file fails in a second, not after the tests.
    api_key = None

    if not arguments.dry_run:
        try:
            api_key = Path(api_key_file).read_text(encoding="utf-8").strip()
        except OSError:
            api_key = ""

        if not api_key:
            raise SystemExit(f"No API key in {api_key_file}.")

    # Decided up front as well: the feed has to be reachable for the push anyway.
    declared = {}

    for project_file in project_files:
        properties = run(["dotnet", "msbuild", project_file, "-getProperty:PackageId", "-getProperty:Version"])

        try:
            declared[project_file] = json.loads(properties.stdout)["Properties"]
        except (ValueError, KeyError):
            raise SystemExit(properties.stdout + properties.stderr)

    declared_versions = {entry["Version"] for entry in declared.values()}

    if len(declared_versions) != 1:
        raise SystemExit("The packages declare different versions (" + ", ".join(sorted(declared_versions)) +
                         "); they are published together, so give them one, e.g. in Directory.Build.props.")

    declared_version = declared_versions.pop()
    published = {entry["PackageId"]: published_versions(packages, entry["PackageId"]) for entry in declared.values()}
    everything = [version for versions in published.values() for version in versions]
    version = arguments.version or next_version(everything, declared_version)
    highest = max(everything, key=version_key, default="nothing yet")
    print(f"Version {version}  (feed: {highest}, declared: {declared_version}), "
          f"{len(project_files)} package(s): {', '.join(published)}")

    taken = [package_id for package_id, versions in published.items() if version in versions]

    if taken:
        raise SystemExit(f"{version} is already on the feed for {', '.join(taken)}.")

    if not arguments.skip_verify:
        arguments.against = MAIN_BRANCH
        arguments.baseline = False
        arguments.quick = False
        arguments.no_db_check = True

        if command_verify(arguments) != 0:
            print("\nNot publishing: verify failed.")

            return 1

    output_directory = os.path.join(REPO, "nupkg")
    version_property = [f"-p:Version={version}"]
    built = []

    for project_file in project_files:
        # The path is asked of MSBuild rather than read from the pack output: pack prints its "Successfully
        # created package" line only when it actually wrote the file, not when an identical one was there.
        properties = run(["dotnet", "msbuild", project_file,
                          "-getProperty:PackageId", "-getProperty:PackageVersion"] + version_property)

        try:
            identity = json.loads(properties.stdout)["Properties"]
        except (ValueError, KeyError):
            print(properties.stdout + properties.stderr)

            return 1

        package = os.path.join(output_directory, f"{identity['PackageId']}.{identity['PackageVersion']}.nupkg")

        print(f"\nPacking {os.path.relpath(project_file, REPO)} as {identity['PackageVersion']} ...")
        completed = run(["dotnet", "pack", project_file, "-c", "Release", "-o", output_directory,
                         "--nologo", "-v", "q"] + version_property)

        if completed.returncode != 0 or not os.path.isfile(package):
            print("\n".join((completed.stdout + completed.stderr).splitlines()[-15:]))

            return 1

        print(f"  {os.path.relpath(package, REPO)}")

        with zipfile.ZipFile(package) as archive:
            for name in sorted(archive.namelist()):
                if name.startswith(("lib/", "analyzers/", "build/", "buildTransitive/")):
                    print(f"    {name}")

        built.append(package)

    if arguments.dry_run:
        print("\n--dry-run: not pushed.")

        return 0

    print(f"\nPushing to {feed} ...")

    for package in built:
        # The key goes in as an argument because `dotnet nuget push` takes it no other way; it is never
        # printed, and only the status lines of the output are shown.
        completed = run(["dotnet", "nuget", "push", package, "--source", feed, "--api-key", api_key])
        output = (completed.stdout + completed.stderr).replace(api_key, "***")
        print(f"  {os.path.basename(package)}")

        for line in output.splitlines():
            if re.search(r"Created|Conflict|Unauthorized|pushed|error", line):
                print(f"    {line.strip()}")

        if completed.returncode != 0:
            if "409" in output or "Conflict" in output:
                print("\nThat version is already on the feed. Bump <Version> or pass --version.")
            elif "401" in output:
                print(f"\nThe feed refused the key in {api_key_file}.")

            pushed = built[:built.index(package)]
            print("\nStopped. " + (f"Already pushed: {', '.join(os.path.basename(path) for path in pushed)} - "
                                   "push the rest with `dotnet nuget push`, not a new version."
                                   if pushed else "Nothing was pushed."))

            return completed.returncode

    if version != declared_version:
        for path in sorted({version_file(project_file) for project_file in project_files}):
            write_version(path, version)
            print(f"\n<Version> in {os.path.relpath(path, REPO)} is now {version} - commit it.")

    return 0


def connection_string(database):
    """An Npgsql connection string for one database on the configured server.

    The scripts that write take their target through {PREFIX}_CONNECTION, and typing one out means
    putting the password in the command line - where it stays, in the shell history and in every log
    of what was run. Host, port and user come from the same place the other scripts read them, the
    [database] block and its environment overrides; the password comes out of ~/.pgpass, which is
    where libpq already keeps it.
    """
    host = project.env("DB_HOST", project.get("database.host", "localhost"))
    port = str(project.env("DB_PORT", project.get("database.port", "5432")))
    user = project.env("DB_USER", project.get("database.user", os.environ.get("USER", "postgres")))

    return (f"Host={host};Port={port};Database={database};Username={user};"
            f"Password={pgpass_password(host, port, database, user)}")


def pgpass_password(host, port, database, user):
    """The password ~/.pgpass holds for this connection.

    libpq's own format and matching rules: five colon-separated fields, `*` matches anything, and a
    backslash escapes a colon or a backslash inside a field. The first matching line wins.
    """
    pgpass = os.path.expanduser(os.environ.get("PGPASSFILE", "~/.pgpass"))

    if not os.path.exists(pgpass):
        raise SystemExit(f"--database needs a password and {pgpass} does not exist.")

    for line in Path(pgpass).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        fields = re.split(r"(?<!\\):", line)

        if len(fields) != 5:
            continue

        fields = [field.replace("\\:", ":").replace("\\\\", "\\") for field in fields]

        if all(stored in ("*", given)
               for stored, given in zip(fields[:4], [host, port, database, user])):
            return fields[4]

    raise SystemExit(f"No line in {pgpass} matches {user}@{host}:{port}/{database}.")


def command_sdk(_arguments):
    print(run(["dotnet", "--version"]).stdout.strip() and f"SDK in use: {run(['dotnet', '--version']).stdout.strip()}")

    global_json = os.path.join(REPO, "global.json")

    if os.path.exists(global_json):
        print(f"\nglobal.json:\n{Path(global_json).read_text(encoding='utf-8-sig').strip()}")

    print("\nInstalled SDKs:")
    print(run(["dotnet", "--list-sdks"]).stdout.strip())
    print("\nInstalled runtimes:")

    for line in run(["dotnet", "--list-runtimes"]).stdout.splitlines():
        if line.startswith(("Microsoft.NETCore.App", "Microsoft.AspNetCore.App")):
            print(f"  {line}")

    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command")

    verify = subparsers.add_parser("verify", help="Build, then tests, reporting warnings on changed lines")
    verify.add_argument("--quick", action="store_true", help="Build only, skip the tests")
    verify.add_argument("--baseline", action="store_true", help="Also build the diff base and compare totals")
    verify.add_argument("--against", default=MAIN_BRANCH,
                        help=f"Diff base (default: {MAIN_BRANCH})")
    verify.add_argument("--filter", help="Passed to dotnet test")
    verify.add_argument("--no-db-check", action="store_true",
                        help="Skip the deploy/db-init/schema.sql comparison")
    verify.add_argument("--no-logging-check", action="store_true",
                        help="Skip the count of direct ILogger calls")
    verify.add_argument("--no-istarnova-check", action="store_true",
                        help="Skip the count of raw FluentDataGrid in a project that uses Istarnova")
    verify.add_argument("--no-spdx-check", action="store_true",
                        help="Skip the check for SPDX licence lines")
    verify.add_argument("--no-containerfile-check", action="store_true",
                        help="Skip the check that the Containerfiles copy every project of their graph")
    verify.add_argument("--fatal-warnings",
                        help="Comma-separated warning codes to fail the build on, e.g. CS9057. "
                             "Overrides dotnet.fatal_warnings in project.toml")
    verify.set_defaults(handler=command_verify)

    build_one = subparsers.add_parser("build", help="Build one project on its own, in or out of the solution")
    build_one.add_argument("project", help="A path to a project or its folder, or a name from the solution")
    build_one.add_argument("--limit", type=int, default=20, help="How many warnings to list (default 20)")
    build_one.add_argument("--target", help="An MSBuild target other than Build, e.g. SignAndroidPackage")
    build_one.add_argument("--property", action="append", default=[], metavar="KEY=VALUE",
                           help="An MSBuild property, repeatable")
    build_one.set_defaults(handler=command_build)

    publish = subparsers.add_parser("publish", help="Verify, pack and push the package to the feed")
    publish.add_argument("--dry-run", action="store_true",
                         help="Pack and list the package, do not push")
    publish.add_argument("--version",
                         help="Exactly this version instead of the next one after the feed's highest")
    publish.add_argument("--skip-verify", action="store_true",
                         help="Pack without building and testing first")
    publish.add_argument("--private", action="store_true",
                         help="Push to [nuget.private] instead of the official feed in [nuget]")
    publish.add_argument("--filter", help=argparse.SUPPRESS)
    publish.set_defaults(handler=command_publish)

    warnings = subparsers.add_parser("warnings", help="Warnings by code, project-level ones included")
    warnings.add_argument("--code", help="List every occurrence of one code, e.g. CS0168")
    warnings.set_defaults(handler=command_warnings)

    tests = subparsers.add_parser("test", help="Run the test project")
    tests.add_argument("--filter", help="Passed to dotnet test")
    tests.add_argument("--details", action="store_true",
                       help="Also print the assertion each failing test reported")
    tests.set_defaults(handler=command_test)

    packages = subparsers.add_parser("packages", help="Package references and advisories")
    packages.add_argument("--vulnerable", action="store_true", help="Known advisories, transitive included")
    packages.add_argument("--outdated", action="store_true", help="Available updates")
    packages.add_argument("--transitive", action="store_true", help="Include transitive references")
    packages.add_argument("--project", help="One project instead of the solution (fragment is enough)")
    packages.add_argument("--versions", metavar="PACKAGE_ID",
                          help="Versions available on nuget.org for one package")
    packages.add_argument("--set", metavar="ID=VERSION", nargs="+",
                          help="Set the version of packages in every project and props file")
    packages.set_defaults(handler=command_packages)

    ef = subparsers.add_parser("ef", help="EF Core migrations and database update")
    ef.add_argument("action", choices=["add", "remove", "update", "list", "script"],
                    help="add NAME | remove | update [NAME] | list | script")
    ef.add_argument("name", nargs="?", help="Migration name for add, or the target migration for update")
    ef.add_argument("arguments", nargs=argparse.REMAINDER, help="Passed to dotnet ef unchanged")
    ef.set_defaults(handler=command_ef)

    script = subparsers.add_parser("script", help="Run a single-file C# script from Scripts/")
    script.add_argument("name", nargs="?", help="Script name without .cs, or a path")
    script.add_argument("arguments", nargs=argparse.REMAINDER, help="Passed to the script unchanged")
    script.add_argument("--list", action="store_true", help="List the scripts instead of running one")
    script.add_argument("--database", metavar="NAME",
                        help="Point the script at this database through {PREFIX}_CONNECTION, before the "
                             "script name (password read from ~/.pgpass, never from the command line). "
                             "Without it only {PREFIX}_DEFAULT_CONNECTION is set - the configured "
                             "database, for scripts that only read")
    script.set_defaults(handler=command_script)

    android_parser = subparsers.add_parser("android", help="MAUI app: signed .apk, adb install, start")
    android_parser.add_argument("action", choices=["build", "install", "devices", "run", "dependencies", "release"])
    android_parser.add_argument("--release", action="store_true", help="Release configuration, signed with [android.signing]")
    android_parser.add_argument("--no-build", action="store_true", help="install: use the .apk already built")
    android_parser.add_argument("--ref", default="HEAD",
                                help="release: the commit or tag to build, from a clean clone (default HEAD)")
    android_parser.add_argument("--check", action="store_true",
                                help="release: build twice and fail unless the unsigned APKs are identical")
    android_parser.set_defaults(handler=command_android)

    subparsers.add_parser("projects", help="Projects in the solution with their target framework") \
        .set_defaults(handler=command_projects)
    subparsers.add_parser("sdk", help="SDK, runtimes and what global.json pins") \
        .set_defaults(handler=command_sdk)

    arguments = parser.parse_args()

    if arguments.command is None:
        parser.print_help()

        return 0

    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
