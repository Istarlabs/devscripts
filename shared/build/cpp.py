#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""One entry point for the routine C++/CMake chores in this repository.

The same reasoning as `rust.py`: every one of these was an ad-hoc chain of `cmake --build ... |
grep ...`, which meant retyping the flags, reading a stream whose shape changes between generators,
and asking for permission again on every unique command line.

    python3 Scripts/cpp.py verify                    build + ctest + clang-format + licence lines
    python3 Scripts/cpp.py verify --quick            build only
    python3 Scripts/cpp.py verify --against master   diff base for "warnings on changed lines"
    python3 Scripts/cpp.py build [--debug] [--clean] configure and build
    python3 Scripts/cpp.py warnings                  warnings by flag, most frequent first
    python3 Scripts/cpp.py warnings --flag=-Wshadow  every occurrence of one flag (the = is needed)
    python3 Scripts/cpp.py test [NAME] [--details]   ctest, with the failing output
    python3 Scripts/cpp.py format [--check]          clang-format over the sources
    python3 Scripts/cpp.py tidy [PATH ...]           clang-tidy over the sources, findings by check
    python3 Scripts/cpp.py tidy --check NAME         every finding of one check
    python3 Scripts/cpp.py sanitize [NAME]           the tests under ASan, UBSan and LeakSanitizer (a second build,
                                                     in <build_dir>/sanitize); `verify` runs it when [cpp] sanitize
                                                     is true, or with --sanitize
    python3 Scripts/cpp.py fuzz [TARGET]             libFuzzer with the sanitizers over the [cpp.fuzz] targets;
                                                     --seconds N, --reproduce FILE for one input
    python3 Scripts/cpp.py run -- --help             the built binary, arguments passed through
    python3 Scripts/cpp.py install                   cmake --install, and a desktop entry too when
                                                     [cpp.service_menu] names one
    python3 Scripts/cpp.py uninstall [--dry-run]     remove what install wrote (install_manifest.txt), e.g.
                                                     once the program comes from rpm-repo.system
    python3 Scripts/cpp.py package                  an RPM through CPack (Release, /usr), in <build_dir>/package
    python3 Scripts/cpp.py toolchain                compiler, cmake, standard, what was found
    python3 Scripts/cpp.py kde                      the KDE libraries on this machine: usable now, missing
                                                     their -devel, QML-only; * marks what this project uses
    python3 Scripts/cpp.py android build             a debug .apk with the Qt for Android toolchain, [android] in project.toml
    python3 Scripts/cpp.py android install           build, then adb install -r on the device
    python3 Scripts/cpp.py android install --no-build  install the .apk already built
    python3 Scripts/cpp.py android devices           connect the configured device, list what adb sees
    python3 Scripts/cpp.py android run               start the installed app

`verify` reports warnings on the lines this branch changed separately from the ones that were
already there, so a warning in a file you happened to touch is not read as yours.

**`verify` builds clean by default**, because a compiler is not cargo: it replays nothing. A warm
incremental build recompiles nothing and therefore prints no warnings at all, and "0 warnings" out
of a build that did no work is the kind of green that hides a red. `--incremental` opts out when the
project is big enough that the honest answer costs too much.

Exit code is non-zero when the build, the tests, clang-format or the licence check fail, so it can
gate a commit.
"""
import argparse
import collections
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import android
import gitdiff
import kdelibs
import repo
from project import project

REPO = str(project.root)

BINARY = project.get("cpp.binary", project.name)
BUILD_DIR = os.path.join(REPO, project.get("cpp.build_dir", "build"))
BUILD_TYPE = project.get("cpp.build_type", "RelWithDebInfo")
GENERATOR = project.get("cpp.generator", "")
CONFIGURE_ARGS = project.get("cpp.configure_args", [])

# `verify` also runs the tests under the sanitizers. A second full build, so it is the project's choice.
SANITIZE = bool(project.get("cpp.sanitize", False))

# Where `install` puts things. ~/.local rather than /usr/local, because that needs root and a
# desktop session carries ~/.local/bin in PATH.
INSTALL_ROOT = os.path.expanduser(project.get("cpp.install_root", "~/.local"))

# A desktop entry is not something every C++ project has, so it is opt-in through
# [cpp.service_menu]. Without that block `install` installs and stops.
SERVICE_MENU_SOURCE = project.path("cpp.service_menu.source")
SERVICE_MENU = os.path.expanduser(project.get("cpp.service_menu.target", ""))

# The chezmoi-managed collection the machine's other menus are kept in. Installing without copying
# there is how three menus ended up with fixes only the installed copy had.
MENU_COLLECTION = os.path.expanduser(project.get("cpp.service_menu.collection", ""))

SOURCE_SUFFIXES = tuple(project.get("cpp.source_suffixes",
                                    [".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx"]))

# Colour codes in a captured stream are noise in every parser downstream.
ENV = {**os.environ, "CLICOLOR": "0", "CLICOLOR_FORCE": "0", "NO_COLOR": "1"}

# gcc and clang both say `path:line:column: level: text`, and gcc puts the flag that produced a
# warning at the end in brackets. A linker error carries no position at all, which is why the
# position is optional here rather than assumed.
DIAGNOSTIC = re.compile(r"^(?P<path>[^\s].*?):(?P<line>\d+):(?P<column>\d+):\s+"
                        r"(?P<level>warning|error|fatal error):\s+(?P<text>.*)$")
FLAG = re.compile(r"\s*\[(?P<flag>-W[^\]]+)\]\s*$")
LINK_ERROR = re.compile(r"^(?:.*\bld\b.*|/usr/bin/ld|collect2)?:?\s*(?P<text>.*\b(?:undefined reference"
                        r"|cannot find|multiple definition)\b.*)$")


def run(command, **kwargs):
    kwargs.setdefault("env", ENV)

    return subprocess.run(command, cwd=REPO, capture_output=True, text=True, errors="replace", **kwargs)


def configure(build_dir=BUILD_DIR, args=None, build_type=None, environment=None):
    """cmake's configure step. Idempotent and cheap once the cache is warm, so it is not guarded -
    a build against a stale cache is a worse trade than a second of cmake.

    The defaults are the desktop build; `android` passes its own directory, arguments and environment
    and gets the same configure step with a toolchain file in front of it.
    """
    command = ["cmake", "-S", REPO, "-B", build_dir,
               f"-DCMAKE_BUILD_TYPE={build_type or BUILD_TYPE}",
               f"-DCMAKE_INSTALL_PREFIX={INSTALL_ROOT}",
               "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"]

    # -G only on a fresh cache: cmake refuses outright when it names a different generator than the
    # one the build directory was created with, so passing it unconditionally turns "someone ran
    # cmake by hand once" into a build that can no longer be configured.
    if not os.path.isfile(os.path.join(build_dir, "CMakeCache.txt")):
        if GENERATOR:
            command += ["-G", GENERATOR]
        elif shutil.which("ninja"):
            command += ["-G", "Ninja"]

    command += list(CONFIGURE_ARGS if args is None else args)

    completed = run(command, env=environment or ENV)

    if completed.returncode != 0:
        sys.stderr.write((completed.stderr or completed.stdout).strip() + "\n")
        sys.exit(completed.returncode)


def build(clean=False, build_dir=BUILD_DIR, target=None, prepare=None, environment=None):
    """Build, and return its (errors, warnings) as (path, line, flag, text) tuples.

    `path` is absolute so it can be compared against what `git diff` reports. `prepare` is the
    configure step to run first (the desktop one by default), `target` one cmake target instead of all.
    """
    (prepare or configure)()

    command = ["cmake", "--build", build_dir, "-j", str(os.cpu_count() or 4)]

    if target:
        command += ["--target", target]

    if clean:
        command.append("--clean-first")

    completed = run(command, env=environment or ENV)
    errors, warnings = [], []

    for line in (completed.stdout + completed.stderr).splitlines():
        match = DIAGNOSTIC.match(line)

        if not match:
            link = LINK_ERROR.match(line)

            if link and completed.returncode != 0 and link["text"]:
                errors.append(("", 0, "", link["text"].strip()))

            continue

        text = match["text"].strip()
        flag_match = FLAG.search(text)
        flag = flag_match["flag"] if flag_match else ""
        text = FLAG.sub("", text).strip()

        # A relative path in a diagnostic is relative to the directory the compiler ran in, which
        # for every generator here is the build directory, not the repository root.
        path = match["path"]
        path = path if os.path.isabs(path) else os.path.normpath(os.path.join(build_dir, path))
        entry = (path, int(match["line"]), flag, text)

        (errors if match["level"].endswith("error") else warnings).append(entry)

    # A build that failed without a single diagnostic is a broken invocation, not a clean build -
    # a missing generator, an unparsable CMakeLists, a compiler that is not there.
    if completed.returncode != 0 and not errors:
        sys.stderr.write((completed.stderr or completed.stdout).strip() + "\n")
        sys.exit(completed.returncode)

    return errors, sorted(set(warnings))


def show(entries, limit=20):
    for path, line, flag, text in entries[:limit]:
        where = f"{os.path.relpath(path, REPO)}:{line}" if path else "(no position)"
        print(f"    {where}  {flag}  {text.splitlines()[0]}")

    if len(entries) > limit:
        print(f"    ... and {len(entries) - limit} more")


def command_build(arguments):
    global BUILD_TYPE

    if getattr(arguments, "debug", False):
        BUILD_TYPE = "Debug"

    print(f"Building ({BUILD_TYPE}) ...")
    errors, warnings = build(clean=getattr(arguments, "clean", False))

    if errors:
        print(f"\nBUILD FAILED - {len(errors)} error(s):\n")
        show(errors)

        return 1

    print(f"  built, {len(warnings)} warning(s)")

    return 0


def command_verify(arguments):
    print(f"Building ({BUILD_TYPE}, {'incremental' if arguments.incremental else 'clean'}) ...")
    errors, warnings = build(clean=not arguments.incremental)

    if errors:
        print(f"\nBUILD FAILED - {len(errors)} error(s):\n")
        show(errors)

        return 1

    print(f"  no errors, {len(warnings)} warning(s)")

    if arguments.incremental and not warnings:
        print("  (incremental: a target that did not rebuild printed nothing - this is not proof)")

    split = gitdiff.split(REPO, arguments.against, warnings)

    if split is None:
        if warnings:
            print(f"  ({arguments.against} does not exist here - listing every warning instead)")
            show(warnings)
    else:
        introduced, nearby, touched = split

        if introduced:
            print(f"\n  {len(introduced)} warning(s) ON LINES THIS BRANCH CHANGED:")
            show(introduced, limit=len(introduced))
        else:
            print(f"  no warnings on changed lines ({len(touched)} file(s) touched vs {arguments.against})")

        if nearby:
            print(f"  ({len(nearby)} pre-existing warning(s) elsewhere in those files)")

    if arguments.quick:
        return 0

    tests = command_test(arguments)

    if tests != 0:
        return tests

    if (SANITIZE or arguments.sanitize) and not arguments.no_sanitize:
        sanitized = command_sanitize(arguments)

        if sanitized != 0:
            return sanitized

    formatting = check_formatting(arguments)

    if formatting != 0:
        return formatting

    return check_spdx(arguments)


def command_warnings(arguments):
    print(f"Building ({BUILD_TYPE}, clean) ...")
    errors, warnings = build(clean=not arguments.incremental)

    if errors:
        print(f"BUILD FAILED - {len(errors)} error(s)")
        show(errors)

        return 1

    if arguments.flag:
        matching = sorted(warning for warning in warnings if warning[2] == arguments.flag)
        print(f"\n{arguments.flag}: {len(matching)} occurrence(s)\n")
        show(matching, limit=len(matching))

        return 0

    print(f"\n{len(warnings)} warning(s), by flag:\n")

    for flag, count in collections.Counter(flag or "(no flag)" for _, _, flag, _ in warnings).most_common():
        print(f"  {flag:<40} {count:>4}")

    return 0


CTEST_SUMMARY = re.compile(r"(?P<percent>\d+)% tests passed, (?P<failed>\d+) tests failed "
                           r"out of (?P<total>\d+)")

# What a sanitizer or libFuzzer prints to say what it found, and the two lines the C++ runtime prints for an
# exception nothing caught (which a reader of files must never throw). Their text is English whatever the locale is,
# and the lines carry a "N: " in front when ctest repeats a test's output verbosely.
SANITIZER_REPORT = re.compile(r"^(?:\d+:\s)?(?P<line>(?:==\d+==)?ERROR: (?:Address|Leak)Sanitizer.*"
                              r"|.*runtime error: .*|SUMMARY: \w+: .*|==\d+==ERROR: libFuzzer: .*"
                              r"|terminate called after throwing .*|\s+what\(\):\s.*)$", re.MULTILINE)


def sanitizer_reports(output):
    """The lines of a test's or fuzzer's output that say what a sanitizer found, each once, in order.

    The stack under them is for the person who follows the finding; these are what tells them there is one. An
    exception that nothing caught is reported the same way: for a reader of files from outside it is a finding too.
    """
    lines = []

    for match in SANITIZER_REPORT.finditer(output):
        line = match["line"].strip()

        if line not in lines:
            lines.append(line)

    return lines


def command_test(arguments):
    print("\nTesting (ctest) ...")

    return run_ctest(BUILD_DIR, ENV, arguments)


def run_ctest(build_dir, environment, arguments):
    """ctest in one build directory, with that build's environment (the sanitize build needs its own)."""
    if not os.path.isfile(os.path.join(build_dir, "CTestTestfile.cmake")):
        print("  no tests registered - add_test() or enable_testing() in CMakeLists.txt")

        return 0

    command = ["ctest", "--test-dir", build_dir, "--output-on-failure"]
    name = getattr(arguments, "name", None)

    if name:
        command += ["-R", name]

    if getattr(arguments, "details", False):
        command.append("--verbose")

    completed = run(command, env=environment)
    output = completed.stdout + completed.stderr
    match = CTEST_SUMMARY.search(output)

    if not match:
        print("  could not read a ctest summary:\n")
        print("\n".join(output.splitlines()[-15:]))

        return 1

    total = int(match["total"])
    failed = int(match["failed"])
    print(f"  {total - failed} passed, {failed} failed out of {total}")

    if failed:
        print_failures(output, getattr(arguments, "details", False))

    return 1 if failed else 0


FAILED_TEST = re.compile(r"^\s*\d+ - (?P<name>\S+) \((?P<why>[^)]+)\)$", re.MULTILINE)


def print_failures(output, detailed=False):
    """Name each failing test - "1 failed" alone cannot be acted on.

    ctest lists them under "The following tests FAILED"; the assertion itself is in the test's own
    output, which --output-on-failure has already put in the stream.
    """
    for match in FAILED_TEST.finditer(output):
        print(f"\n  {match['name']}  ({match['why']})")

    reports = sanitizer_reports(output)

    if reports:
        print("\n  what the sanitizer found:")

        for line in reports[:6]:
            print(f"    {line}")

    if not detailed:
        print("\n  'test --details' repeats this with each test's full output")

        return

    for line in output.splitlines():
        if re.match(r"^\d+:\s", line) or "FAIL!" in line or "Assertion" in line:
            print(f"    {line}")


def source_files():
    """The project's own C++ sources, tracked or new, with ignored files left out."""
    return [path for path in repo.working_files() if path.endswith(SOURCE_SUFFIXES)]


def command_format(arguments):
    if arguments.check:
        return check_formatting(argparse.Namespace(no_format_check=False))

    style = os.path.join(REPO, ".clang-format")

    if not os.path.isfile(style) or not shutil.which("clang-format"):
        print("no .clang-format, or clang-format is not installed - nothing to do")

        return 0

    files = source_files()
    subprocess.run(["clang-format", "-i", *files], cwd=REPO, env=ENV, check=False)
    print(f"formatted {len(files)} file(s)")

    return 0


def check_formatting(arguments):
    """Report what clang-format would change. It is the one style question nobody should be arguing.

    A project with no .clang-format has not made that choice, and inventing one here would rewrite
    every file on the first run - so this reports that it was skipped rather than guessing a style.
    """
    if getattr(arguments, "no_format_check", False):
        return 0

    print("\nChecking formatting ...")

    if not os.path.isfile(os.path.join(REPO, ".clang-format")):
        print("  no .clang-format in the repository - skipped")

        return 0

    if not shutil.which("clang-format"):
        print("  clang-format is not installed - skipped")

        return 0

    files = source_files()
    completed = run(["clang-format", "--dry-run", "--Werror", *files])

    if completed.returncode == 0:
        print(f"  clang-format clean ({len(files)} file(s))")

        return 0

    # clang-format names the file as "path:line:col: <level>: ..." once per hunk, so the position
    # has to come off or the same file is counted several times. --Werror turns that level from
    # "warning" into "error", which is why the tag rather than the level is what is matched.
    changed = sorted({line.split(":")[0] for line in completed.stderr.splitlines()
                      if "-Wclang-format-violations" in line})
    print(f"  {len(changed)} file(s) clang-format would change:")

    for path in changed:
        print(f"    {path}")

    print("\n  python3 Scripts/cpp.py format writes it.")

    return 1


def check_spdx(arguments):
    """Report the project's own source files that carry no SPDX licence lines.

    Nothing adds them on its own, so a file written in a hurry would not have them and nothing else
    would notice.
    """
    if getattr(arguments, "no_spdx_check", False):
        return 0

    if project.get("spdx.holder") is None:
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


def binary_path():
    """Where the build put the binary. KDECMakeSettings collects them in bin/, a bare CMake project
    leaves them next to the target's own CMakeLists."""
    configured = project.get("cpp.binary_path")

    if configured:
        return os.path.join(BUILD_DIR, configured)

    for candidate in (os.path.join(BUILD_DIR, "bin", BINARY),
                      os.path.join(BUILD_DIR, BINARY),
                      os.path.join(BUILD_DIR, "src", BINARY)):
        if os.path.isfile(candidate):
            return candidate

    return None


# clang-tidy's own diagnostics: `path:line:column: warning: text [check-name]`, the check last in brackets.
TIDY_CHECK = re.compile(r"\s*\[(?P<check>[\w.-]+?)(?:,-warnings-as-errors)?\]\s*$")
TIDY_NOLINT = re.compile(r"Suppressed \d+ warnings \(.*?(?P<nolint>\d+) NOLINT\)")


def tidy_findings(output, build_dir):
    """run-clang-tidy's output as sorted (path, line, check, text) findings, each once, and the NOLINT count.

    A finding in a header is printed by every file that includes it; the set keeps it once. A header's
    NOLINT is counted the same way, so the count says how often one was honoured, which is enough to
    tell that they are there.
    """
    findings = set()

    for line in output.splitlines():
        match = DIAGNOSTIC.match(line)

        if not match:
            continue

        check = TIDY_CHECK.search(match["text"])
        path = match["path"] if os.path.isabs(match["path"]) else os.path.join(build_dir, match["path"])
        findings.add((os.path.normpath(path), int(match["line"]), check["check"] if check else match["level"],
                      TIDY_CHECK.sub("", match["text"]).strip()))

    return sorted(findings), sum(int(match["nolint"]) for match in TIDY_NOLINT.finditer(output))


def command_tidy(arguments):
    """clang-tidy over the project's own sources, with the findings once each, by check.

    It builds first: clang-tidy reads compile_commands.json, and a file that includes a generated
    header (moc, kconfig_compiler, ui) cannot be checked before that header exists. Only the
    translation units are passed; headers are checked through them, as far as .clang-tidy's
    HeaderFilterRegex lets them in - which is why a finding in a header is counted once, not once
    per file that includes it.
    """
    runner = shutil.which("run-clang-tidy")

    if not os.path.isfile(os.path.join(REPO, ".clang-tidy")) or not runner:
        print("no .clang-tidy, or run-clang-tidy is not installed (clang-tools-extra) - nothing to do")

        return 1

    print(f"Building ({BUILD_TYPE}) for the compile database ...")
    errors, _ = build()

    if errors:
        print(f"\nBUILD FAILED - {len(errors)} error(s):\n")
        show(errors)

        return 1

    units = [path for path in source_files() if path.endswith((".cpp", ".cc", ".cxx"))]

    if arguments.paths:
        wanted = [os.path.normpath(path) for path in arguments.paths]
        units = [path for path in units if any(path == want or path.startswith(want.rstrip("/") + "/")
                                               for want in wanted)]

    if not units:
        print("no source files to check" + (" under those paths" if arguments.paths else ""))

        return 1

    print(f"clang-tidy over {len(units)} file(s) ...")
    # run-clang-tidy takes regular expressions over the compile database's paths, so each file is
    # one anchored, escaped pattern.
    patterns = [re.escape(os.path.join(REPO, path)) + "$" for path in units]
    # Not -quiet: it drops the "Suppressed ... NOLINT" lines the count below is read from.
    completed = run([runner, "-p", BUILD_DIR, "-j", str(os.cpu_count() or 4), *patterns])
    output = completed.stdout + completed.stderr
    findings, nolint = tidy_findings(output, BUILD_DIR)

    if arguments.check:
        matching = [finding for finding in findings if finding[2] == arguments.check]
        print(f"\n{arguments.check}: {len(matching)} finding(s)\n")
        show(matching, limit=len(matching))

        return 1 if matching else 0

    if not findings:
        if completed.returncode != 0:
            print(output.strip()[-2000:])

            return completed.returncode

        print(f"  clean ({nolint} NOLINT honoured)")

        return 0

    print(f"\n{len(findings)} finding(s), by check ({nolint} NOLINT honoured):\n")

    for check, count in collections.Counter(finding[2] for finding in findings).most_common():
        print(f"  {check:<60} {count:>4}")

    print()
    show(findings, limit=arguments.limit)
    print("\n  'tidy --check NAME' lists every finding of one check")

    return 1


SANITIZER_FLAGS = "-fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer"


def sanitize_dir():
    return os.path.join(BUILD_DIR, "sanitize")


def sanitize_configure():
    """The configure step of the sanitize tree: the everyday one with the sanitizers in every flag.

    The linker flags too, because a test executable has to link the runtime; `-fno-sanitize-recover` makes the first
    report stop the program, so a test fails instead of printing and going on.
    """
    flags = [f"-DCMAKE_{kind}_FLAGS={SANITIZER_FLAGS}" for kind in ("C", "CXX", "EXE_LINKER", "SHARED_LINKER")]
    configure(sanitize_dir(), list(CONFIGURE_ARGS) + flags)


def sanitize_environment():
    """ENV plus the sanitizers' options: leaks reported, a stack with every undefined-behaviour finding, and the
    project's suppressions for what it does not own (a Qt or KDE library's own leaks, say)."""
    environment = dict(ENV)
    environment["ASAN_OPTIONS"] = "detect_leaks=1:strict_string_checks=1"
    environment["UBSAN_OPTIONS"] = "print_stacktrace=1:halt_on_error=1"
    suppressions = project.path("cpp.sanitize_suppressions")

    if suppressions is not None:
        environment["LSAN_OPTIONS"] = f"suppressions={suppressions}"

    return environment


def command_sanitize(arguments):
    """The tests under AddressSanitizer, UndefinedBehaviorSanitizer and LeakSanitizer, in <build_dir>/sanitize.

    What C++ leaves to the program and a Rust compiler rules out: a read or write outside an object, a signed
    overflow, a leak. They are found where a test goes. A test that never feeds a hostile file does not find what a
    hostile file does, so a reader of external files wants tests for that (and `fuzz`); what this adds is that such a
    test fails on the first report instead of passing by luck, and that undefined behaviour on the ordinary paths
    shows.

    Built incrementally in its own directory: the flags never change, so a warm build is as good as a clean one.
    """
    print(f"\nBuilding with ASan and UBSan ({BUILD_TYPE}) ...")
    errors, _ = build(clean=getattr(arguments, "clean", False), build_dir=sanitize_dir(), prepare=sanitize_configure)

    if errors:
        print(f"\nSANITIZE BUILD FAILED - {len(errors)} error(s):\n")
        show(errors)

        if any("san" in error[3].lower() for error in errors):
            print("\n  the sanitizer runtimes are separate packages: sudo dnf install libasan libubsan")

        return 1

    print("Testing under the sanitizers (ctest) ...")

    return run_ctest(sanitize_dir(), sanitize_environment(), arguments)


FUZZ_FLAGS = "-fsanitize=address,undefined,fuzzer-no-link -fno-sanitize-recover=undefined -fno-omit-frame-pointer"
LIBFUZZER_DONE = re.compile(r"^#(?P<runs>\d+)\s+DONE\s+cov: (?P<cov>\d+) ft: (?P<features>\d+) corp: (?P<corpus>\d+)",
                            re.MULTILINE)
LIBFUZZER_ARTIFACT = re.compile(r"^(?:crash|oom|timeout|leak|slow-unit)-")


def fuzz_dir():
    return os.path.join(BUILD_DIR, "fuzz")


def fuzz_configure():
    """The configure step of the fuzz tree: clang, the sanitizers, and the option that switches the fuzz targets on.

    `fuzzer-no-link` instruments the library for coverage without a second main(); the targets themselves link
    libFuzzer (`-fsanitize=fuzzer`), which the project's CMakeLists says per target.
    """
    if not shutil.which("clang++"):
        sys.exit("refused: clang++ is not installed - libFuzzer comes with clang (sudo dnf install clang compiler-rt).")

    option = project.get("cpp.fuzz.option", "BUILD_FUZZING")
    flags = [f"-DCMAKE_{kind}_FLAGS={FUZZ_FLAGS}" for kind in ("C", "CXX")]
    flags += [f"-DCMAKE_{kind}_LINKER_FLAGS=-fsanitize=address,undefined" for kind in ("EXE", "SHARED")]
    configure(fuzz_dir(), list(CONFIGURE_ARGS) + [f"-D{option}=ON", "-DCMAKE_C_COMPILER=clang",
                                                  "-DCMAKE_CXX_COMPILER=clang++", *flags])


def fuzz_binary(target):
    """The executable a fuzz target was built to, wherever the project's CMake put it."""
    for path in sorted(Path(fuzz_dir()).rglob(target)):
        if path.is_file() and os.access(path, os.X_OK) and "CMakeFiles" not in path.parts:
            return path

    return None


def fuzz_summary(output):
    """(runs, coverage, features, corpus size) of libFuzzer's closing line, or None when it did not get that far."""
    found = LIBFUZZER_DONE.findall(output)

    return tuple(int(value) for value in found[-1]) if found else None


def run_fuzzer(target, binary, seconds, reproduce=None):
    """One target for `seconds`, or one input through it. Returns the exit code; prints what was found."""
    work = Path(fuzz_dir()) / "corpus" / target
    artifacts = Path(fuzz_dir()) / "artifacts" / target
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    environment = sanitize_environment()
    environment["UBSAN_OPTIONS"] = "print_stacktrace=1"

    if reproduce:
        command = [str(binary), str(reproduce)]
    else:
        # The first directory is where libFuzzer keeps what it finds; the checked-in seeds are only read.
        command = [str(binary), str(work)]
        seeds = project.path("cpp.fuzz.seeds_dir")

        if seeds is not None and (seeds / target).is_dir():
            command.append(str(seeds / target))

        command += [f"-max_total_time={seconds}", f"-artifact_prefix={artifacts}/", "-rss_limit_mb=2048",
                    "-timeout=20", f"-max_len={project.get('cpp.fuzz.max_len', 65536)}"]

    # A second of slack: the file system's clock is coarser than time.time().
    started = time.time() - 1
    completed = run(command, env=environment)
    output = completed.stdout + completed.stderr
    summary = fuzz_summary(output)

    if completed.returncode == 0:
        if reproduce:
            print(f"  {target}: that input runs clean")
        elif summary:
            runs, coverage, features, corpus = summary
            print(f"  {target}: {seconds} s, {runs:,} runs, coverage {coverage}, {features} features, "
                  f"corpus {corpus} - nothing found")
        else:
            print(f"  {target}: ran for {seconds} s - nothing found (no statistics in its output)")

        return 0

    print(f"  {target}: FOUND SOMETHING (exit {completed.returncode})")

    for line in sanitizer_reports(output)[:6]:
        print(f"    {line}")

    new = sorted(path for path in artifacts.iterdir()
                 if LIBFUZZER_ARTIFACT.match(path.name) and path.stat().st_mtime >= started)

    for path in new:
        print(f"    input: {os.path.relpath(path, REPO)}")
        print(f"    again: python3 Scripts/cpp.py fuzz {target} --reproduce {os.path.relpath(path, REPO)}")

    if not new and not sanitizer_reports(output):
        print("\n".join(f"    {line}" for line in output.strip().splitlines()[-8:]))

    return 1


def command_fuzz(arguments):
    """libFuzzer with ASan and UBSan over the project's fuzz targets, in <build_dir>/fuzz.

    The targets are CMake targets, named in [cpp.fuzz] targets and added by the project's CMakeLists when the option of
    [cpp.fuzz] option is on. Each is a `LLVMFuzzerTestOneInput` over one reader. What libFuzzer finds on its way is
    kept in the build directory and used by the next run; the seeds a project checks in ([cpp.fuzz] seeds_dir, one
    folder per target) are read only. It finds what the tests did not think of, which is why it is not part of
    `verify`: it runs for as long as it is given.
    """
    targets = list(project.require("cpp.fuzz.targets"))

    if arguments.target and arguments.target not in targets:
        sys.exit(f"refused: {arguments.target!r} is not one of cpp.fuzz.targets ({', '.join(targets)}).")

    if arguments.reproduce and not arguments.target:
        sys.exit("refused: --reproduce needs the target the input belongs to.")

    status = 0

    for target in [arguments.target] if arguments.target else targets:
        print(f"\nBuilding {target} (clang, ASan, UBSan, libFuzzer) ...")
        errors, _ = build(build_dir=fuzz_dir(), target=target, prepare=fuzz_configure)

        if errors:
            print(f"\nBUILD FAILED - {len(errors)} error(s):\n")
            show(errors)

            return 1

        binary = fuzz_binary(target)

        if binary is None:
            sys.exit(f"refused: the build wrote no {target} under {os.path.relpath(fuzz_dir(), REPO)}.")

        status |= run_fuzzer(target, binary, arguments.seconds, arguments.reproduce)

    return status


def command_run(arguments):
    status = command_build(argparse.Namespace(debug=arguments.debug, clean=False))

    if status != 0:
        return status

    target = binary_path()

    if target is None:
        sys.exit(f"refused: no {BINARY} under {os.path.relpath(BUILD_DIR, REPO)} - "
                 f"set cpp.binary_path in Scripts/project.toml if it lands somewhere else.")

    passed = list(arguments.arguments)

    # argparse.REMAINDER keeps the `--` that separates this script's flags from the program's, and
    # passing it on makes the program read its first flag as a positional argument instead.
    if passed and passed[0] == "--":
        passed = passed[1:]

    # Not captured: this is a program with a user in front of it, so its output belongs on the
    # terminal as it happens rather than in one block at the end. The flush is what keeps the build
    # lines above it - they are buffered here, while the child writes to the terminal directly.
    sys.stdout.flush()

    return subprocess.run([target, *passed], cwd=REPO, env=os.environ).returncode


def command_install(arguments):
    """cmake --install into ~/.local, install the service menu, rebuild the KDE cache.

    The .desktop file has to be executable or KonqPopupMenu ignores it, and KDE only notices a new
    or changed menu after `kbuildsycoca6`; Dolphin itself needs no restart once that has run.

    The file is copied into the servicemenus collection as well, because that is the chezmoi-managed
    source of truth for every menu on this machine and its sync job copies *from* there.
    """
    if not arguments.menu_only:
        status = command_build(argparse.Namespace(debug=False, clean=False))

        if status != 0:
            return status

        completed = subprocess.run(["cmake", "--install", BUILD_DIR], cwd=REPO, env=ENV,
                                   capture_output=True, text=True)

        if completed.returncode != 0:
            sys.stderr.write((completed.stderr or completed.stdout).strip() + "\n")

            return completed.returncode

        print(f"installed into {INSTALL_ROOT}")

    if SERVICE_MENU_SOURCE is None or not SERVICE_MENU:
        return 0

    if not os.path.exists(SERVICE_MENU_SOURCE):
        print(f"no service menu at {os.path.relpath(SERVICE_MENU_SOURCE, REPO)} - skipped")

        return 0

    os.makedirs(os.path.dirname(SERVICE_MENU), exist_ok=True)
    shutil.copyfile(SERVICE_MENU_SOURCE, SERVICE_MENU)
    os.chmod(SERVICE_MENU, 0o755)
    print(f"installed {SERVICE_MENU}")

    if MENU_COLLECTION and os.path.isdir(MENU_COLLECTION):
        shutil.copyfile(SERVICE_MENU_SOURCE,
                        os.path.join(MENU_COLLECTION, os.path.basename(SERVICE_MENU)))
        print(f"copied it into {MENU_COLLECTION}")

    if shutil.which("kbuildsycoca6"):
        subprocess.run(["kbuildsycoca6"], cwd=REPO, env=ENV, capture_output=True)
        print("rebuilt the KDE service cache - the entry is there now")
    else:
        print("kbuildsycoca6 not found - the entry appears after the next KDE login")

    return 0


def manifest_targets(manifest_text, install_root):
    """The files of an install manifest that lie under the install root; anything else is not ours to delete."""
    root = os.path.join(os.path.realpath(install_root), "")
    lines = (line.strip() for line in manifest_text.splitlines())

    return [path for path in lines if path and os.path.realpath(path).startswith(root)]


def command_uninstall(arguments):
    """Remove what `install` put into cpp.install_root, as cmake recorded it in install_manifest.txt.

    Needed when the program comes from a package now: the copy under ~/.local stays in front of it - its
    binary first in PATH, and its desktop entry in front of /usr/share/applications, still naming the
    binary under ~/.local after that one is gone.
    """
    manifest = Path(BUILD_DIR) / "install_manifest.txt"

    if not manifest.is_file():
        sys.exit(f"refused: no {os.path.relpath(manifest, REPO)} - nothing was installed from this build directory")

    targets = manifest_targets(manifest.read_text(encoding="utf-8"), INSTALL_ROOT)
    removed = 0

    for path in targets:
        if not os.path.lexists(path):
            continue

        if arguments.dry_run:
            print(f"would remove {path}")
        else:
            os.remove(path)
            print(f"removed {path}")

        removed += 1

    if removed == 0:
        print(f"nothing left of the install in {INSTALL_ROOT}")
    elif not arguments.dry_run and shutil.which("kbuildsycoca6"):
        subprocess.run(["kbuildsycoca6"], cwd=REPO, env=ENV, capture_output=True)

    return 0


def command_package(_arguments):
    """An RPM, through the project's own CPack configuration.

    Built in a directory of its own (<build_dir>/package) because a package differs from the
    everyday build in the two settings that are baked in at configure time: Release rather than
    cpp.build_type, and a prefix of /usr rather than cpp.install_root. A desktop entry generated from
    the everyday build would name a binary under ~/.local, in a package that installs it under /usr.
    The tests are left out of it - `verify` is what runs them.
    """
    package_dir = os.path.join(BUILD_DIR, "package")
    command = ["cmake", "-S", REPO, "-B", package_dir, "-DCMAKE_BUILD_TYPE=Release",
               "-DCMAKE_INSTALL_PREFIX=/usr", "-DBUILD_TESTING=OFF"]

    if not os.path.isfile(os.path.join(package_dir, "CMakeCache.txt")):
        if GENERATOR:
            command += ["-G", GENERATOR]
        elif shutil.which("ninja"):
            command += ["-G", "Ninja"]

    command += list(CONFIGURE_ARGS)

    for step, description in ((command, "Configuring"),
                              (["cmake", "--build", package_dir, "-j", str(os.cpu_count() or 4)], "Building"),
                              (["cpack", "--config", os.path.join(package_dir, "CPackConfig.cmake"),
                                "-B", package_dir], "Packing")):
        print(f"{description} ...")
        completed = run(step)

        if completed.returncode != 0:
            sys.stderr.write((completed.stderr or completed.stdout).strip() + "\n")

            return completed.returncode

    packages = sorted(Path(package_dir).glob("*.rpm"), key=lambda path: path.stat().st_mtime)

    if not packages:
        sys.exit("refused: cpack reported success but wrote no .rpm - is CPACK_GENERATOR set to RPM?")

    newest = str(packages[-1])
    print(f"\n{os.path.relpath(newest, REPO)}")

    # What it asks of the machine it is installed on, so a missing run-time dependency is read here
    # rather than found out as a QML error on someone else's desktop.
    requires = run(["rpm", "-qpR", newest])

    if requires.returncode == 0:
        wanted = sorted({line.split()[0] for line in requires.stdout.splitlines()
                         if line.strip() and not line.startswith("rpmlib(")})
        print(f"requires {len(wanted)}: " + ", ".join(wanted))

    return 0


CACHE_ENTRY = re.compile(r"^(?P<key>[A-Za-z0-9_]+):(?P<type>\w+)=(?P<value>.*)$")


def command_toolchain(_arguments):
    for command in (["cmake", "--version"], ["ninja", "--version"], ["g++", "--version"],
                    ["clang++", "--version"], ["clang-format", "--version"]):
        if not shutil.which(command[0]):
            print(f"{command[0]}: not installed")
            continue

        result = run(command)
        first = (result.stdout or result.stderr).strip().splitlines()[0]

        # ninja answers with the bare number, which says nothing on a line of its own.
        print(first if command[0] in first else f"{command[0]} {first}")

    cache = Path(BUILD_DIR) / "CMakeCache.txt"

    if not cache.is_file():
        print(f"\nNo {os.path.relpath(cache, REPO)} yet - "
              "python3 Scripts/cpp.py build writes one.")

        return 0

    values, found = {}, []

    for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
        match = CACHE_ENTRY.match(line)

        if not match:
            continue

        if match["key"] in ("CMAKE_BUILD_TYPE", "CMAKE_CXX_COMPILER", "CMAKE_INSTALL_PREFIX",
                            "CMAKE_CXX_STANDARD", "CMAKE_GENERATOR"):
            values[match["key"]] = match["value"]
        # A find_package result points at the directory holding that package's own cmake files.
        # Matching on "_DIR" alone also catches every _INCLUDE_DIR, _BINARY_DIR and _SOURCE_DIR,
        # which say nothing about what was found.
        elif match["key"].endswith("_DIR") and "/cmake/" in match["value"]:
            found.append((match["key"][:-4], match["value"]))

    print(f"\n{os.path.relpath(cache, REPO)}:")

    for key, value in values.items():
        print(f"  {key:<22} {value}")

    if found:
        print(f"\n{len(found)} package(s) found by find_package:")

        for name, where in sorted(found):
            print(f"  {name:<22} {where}")

    return 0


def command_kde(_arguments):
    print(kdelibs.report(REPO))

    return 0


def expand(value):
    return os.path.expanduser(str(value))


def android_build_dir():
    return os.path.join(BUILD_DIR, "android")


def android_abi():
    return project.get("android.abi", "arm64-v8a")


def android_environment():
    """ENV plus the SDK and NDK the Qt toolchain file reads from the environment.

    project.toml wins over what the shell exports, so one project can pin another NDK than the machine
    default. With no NDK named anywhere the newest one under the SDK is taken.
    """
    environment = dict(ENV)
    sdk = expand(project.get("android.sdk") or environment.get("ANDROID_SDK_ROOT")
                 or environment.get("ANDROID_HOME") or "~/Android/Sdk")
    ndk = project.get("android.ndk") or environment.get("ANDROID_NDK_ROOT")

    if not ndk:
        installed = sorted(Path(sdk, "ndk").glob("*"),
                           key=lambda path: [int(part) for part in path.name.split(".") if part.isdigit()])

        if not installed:
            sys.exit(f"refused: no NDK under {sdk}/ndk - sdkmanager \"ndk;<version>\", or set android.ndk.")

        ndk = installed[-1]

    # ECM's Android.cmake (see android_configure) reads the target from the environment, as the KDE
    # container sets it: ANDROID_NDK, the architecture next to the ABI, and the minimum API level.
    architecture = {"arm64-v8a": "arm64", "armeabi-v7a": "arm"}.get(android_abi(), android_abi())
    environment.update(ANDROID_SDK_ROOT=sdk, ANDROID_HOME=sdk, ANDROID_NDK_ROOT=expand(ndk),
                       ANDROID_NDK=expand(ndk), ANDROID_ARCH=architecture, ANDROID_ARCH_ABI=android_abi(),
                       ANDROID_NDK_PLATFORM=f"android-{project.get('android.api', 28)}")

    return environment


def android_configure(environment):
    """The configure step for the Android build tree: the desktop one, behind Qt's toolchain file.

    A cross build needs the host's Qt tools next to the target's Qt, which is QT_HOST_PATH. On Fedora
    the host's cmake files are under lib64, so that directory is passed when it exists.
    """
    qt = Path(expand(project.require("android.qt")))
    toolchain = qt / "lib" / "cmake" / "Qt6" / "qt.toolchain.cmake"

    if not toolchain.is_file():
        sys.exit(f"refused: {toolchain} does not exist - android.qt is the Qt for Android prefix, "
                 "e.g. ~/Qt/6.11.2/android_arm64_v8a.")

    host = expand(project.get("android.qt_host_path", "/usr"))
    host_cmake = project.get("android.qt_host_cmake_dir")

    if host_cmake is None and os.path.isdir(os.path.join(host, "lib64", "cmake", "Qt6HostInfo")):
        host_cmake = os.path.join(host, "lib64", "cmake")

    args = [f"-DCMAKE_TOOLCHAIN_FILE={toolchain}", f"-DQT_HOST_PATH={host}",
            f"-DQT_ANDROID_ABIS={android_abi()}", "-DBUILD_TESTING=OFF"]

    if host_cmake:
        args.append(f"-DQT_HOST_PATH_CMAKE_DIR={expand(host_cmake)}")

    # Qt's toolchain file hands over to the NDK's own as soon as an NDK is known, which skips ECM's
    # Android.cmake - and with it ANDROID_SDK_COMPILE_API and the STL path, so androiddeployqt runs with
    # "android-" as its platform. Chainloading ECM's file puts them back. The prefix's copy goes first:
    # it is the one the frameworks there were built with.
    prefix = project.get("android.prefix")
    candidates = ([Path(expand(prefix), "share/ECM/toolchain/Android.cmake")] if prefix else []) \
        + [Path("/usr/share/ECM/toolchain/Android.cmake")]
    chainload = next((path for path in candidates if path.is_file()), None)

    if chainload:
        args.append(f"-DQT_CHAINLOAD_TOOLCHAIN_FILE={chainload}")

    # Where the KDE Frameworks built for Android live (Craft's root, say). ECM wants it three times: for
    # find_package, for the library search, and as the staging root androiddeployqt takes the APK's
    # content from. Without the last one that is CMAKE_INSTALL_PREFIX, which here is ~/.local, and
    # androiddeployqt then walks all of it (icon themes included) for longer than anyone waits.
    if prefix:
        args += [f"-DCMAKE_PREFIX_PATH={expand(prefix)}", f"-DECM_ADDITIONAL_FIND_ROOT_PATH={expand(prefix)}",
                 f"-DECM_APK_STAGING_ROOT_PATH={expand(prefix)}"]

    args += list(project.get("android.configure_args", []))
    configure(android_build_dir(), args, project.get("android.build_type", BUILD_TYPE), environment)


def android_targets(environment):
    """The target names of the configured Android build tree. Ninja's `help` lists them as `name: phony`."""
    completed = run(["cmake", "--build", android_build_dir(), "--target", "help"], env=environment)

    return {line.split(":")[0].strip() for line in completed.stdout.splitlines() if ":" in line}


def android_install_files(environment):
    """Install what the project's install rules hold for Android into the staging root.

    An APK is not made from an install, but some of what goes into it is: the Breeze icon theme
    kirigami_package_breeze_icons writes under the QML directory, where androiddeployqt picks it up
    from the prefix. So a project's own install rules on Android should be that and nothing else - an
    install(TARGETS) of the app itself would fail here, as it has not been built yet.
    """
    prefix = project.get("android.prefix")

    if not prefix:
        return

    completed = run(["cmake", "--install", android_build_dir(), "--prefix", expand(prefix)], env=environment)

    if completed.returncode != 0:
        sys.stderr.write((completed.stderr or completed.stdout).strip() + "\n")
        sys.exit(completed.returncode)


def newest_android_apk():
    """The .apk ECM's create-apk target wrote, or None. It sits in <target>_build_apk, named per ABI."""
    found = sorted((Path(android_build_dir()) / f"{BINARY}_build_apk").glob(f"{BINARY}*-{android_abi()}.apk"),
                   key=lambda path: path.stat().st_mtime)

    return found[-1] if found else None


def build_android(clean):
    """Build the .apk and return it, or None after printing why not."""
    environment = android_environment()
    print(f"Building {BINARY} for Android ({android_abi()}) ...")
    # The translations first, then the install, then the .apk: what install puts into the staging root
    # (the compiled .mo files, the icon theme) has to exist before androiddeployqt reads that root, and
    # ki18n_install's targets are part of ALL but not of create-apk. ALL itself is no way out: Qt adds an
    # .apk target of its own to it, which expects another layout than ECM's.
    android_configure(environment)
    errors, warnings = [], []

    if "pofiles" in android_targets(environment):
        errors, warnings = build(False, android_build_dir(), "pofiles", lambda: None, environment)

    if not errors:
        android_install_files(environment)
        more_errors, more_warnings = build(clean, android_build_dir(), f"create-apk-{BINARY}", lambda: None,
                                           environment)
        errors += more_errors
        warnings = sorted(set(warnings) | set(more_warnings))

    if errors:
        print(f"\nBUILD FAILED - {len(errors)} error(s):\n")
        show(errors)

        return None

    apk = newest_android_apk()

    if apk is None:
        print(f"BUILD FAILED - the build succeeded but there is no {BINARY}*-{android_abi()}.apk "
              f"under {os.path.relpath(android_build_dir(), REPO)}/{BINARY}_build_apk")

        return None

    print(f"  built, {len(warnings)} warning(s)")

    return apk


def command_android(arguments):
    """build / install / run / devices, the shape of `dotnet.py android` on the CMake side.

    Debug only: ECM's create-apk target leaves a release .apk unsigned, and androiddeployqt takes a
    keystore password on the command line, where `ps` shows it. A release build waits until someone
    decides how that is handled.
    """
    # ponytail: debug builds only, add --release with a signing step.
    if arguments.action == "devices":
        android.connect()
        print(android.adb("devices", "-l").stdout.strip())

        return 0

    application_id = project.require("android.application_id")

    if arguments.action == "run":
        android.require_device()
        output = android.adb("shell", "monkey", "-p", application_id, "-c",
                             "android.intent.category.LAUNCHER", "1").stdout

        print(output.strip().splitlines()[-1] if output.strip() else "started")

        return 0

    apk = newest_android_apk() if arguments.no_build else build_android(arguments.clean)

    if apk is None:
        if arguments.no_build:
            sys.stderr.write("refused: no .apk built yet - run `android build` first.\n")

        return 1

    print(f"  {os.path.relpath(apk, REPO)}  ({apk.stat().st_size / 1024 / 1024:.1f} MiB)")

    if arguments.action == "build":
        return 0

    android.require_device()
    result = android.adb("install", "-r", str(apk), check=False)
    output = (result.stdout + result.stderr).strip()

    if result.returncode != 0 or "Success" not in output:
        print(f"INSTALL FAILED\n{output}")

        if "UPDATE_INCOMPATIBLE" in output:
            print(f"  signed with a different key than the installed copy: "
                  f"adb uninstall {application_id} (this deletes its data), then install again")

        return 1

    print(f"  installed {application_id}")

    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command")
    main_branch = project.get("repo.main_branch", "master")

    verify = subparsers.add_parser("verify", help="Build, tests, clang-format and the licence lines")
    verify.add_argument("--quick", action="store_true", help="Build only, skip the tests")
    verify.add_argument("--incremental", action="store_true",
                        help="Do not build clean first (faster, and blind to anything that did not rebuild)")
    verify.add_argument("--against", default=main_branch, help=f"Diff base (default: {main_branch})")
    verify.add_argument("name", nargs="?", help="Only tests whose name matches this")
    verify.add_argument("--details", action="store_true", help="Print each failing test's output")
    verify.add_argument("--no-format-check", action="store_true", help="Skip the clang-format comparison")
    verify.add_argument("--no-spdx-check", action="store_true", help="Skip the check for SPDX licence lines")
    verify.add_argument("--sanitize", action="store_true",
                        help="Also run the tests under ASan and UBSan, though [cpp] sanitize is not set")
    verify.add_argument("--no-sanitize", action="store_true",
                        help="Leave the sanitizer run out, though [cpp] sanitize is set")
    verify.set_defaults(handler=command_verify)

    build_command = subparsers.add_parser("build", help="Configure and build")
    build_command.add_argument("--debug", action="store_true", help=f"Debug instead of {BUILD_TYPE}")
    build_command.add_argument("--clean", action="store_true", help="Rebuild everything")
    build_command.set_defaults(handler=command_build)

    warnings = subparsers.add_parser("warnings", help="Warnings by flag, most frequent first")
    warnings.add_argument("--flag", help="List every occurrence of one flag: --flag=-Wunused-parameter (with the =, or argparse reads -W... as an option of its own)")
    warnings.add_argument("--incremental", action="store_true", help="Do not build clean first")
    warnings.set_defaults(handler=command_warnings)

    tests = subparsers.add_parser("test", help="Run the tests")
    tests.add_argument("name", nargs="?", help="Only tests whose name matches this")
    tests.add_argument("--details", action="store_true", help="Also print each failing test's output")
    tests.set_defaults(handler=command_test)

    format_command = subparsers.add_parser("format", help="clang-format over the sources")
    format_command.add_argument("--check", action="store_true",
                                help="Report what it would change, change nothing")
    format_command.set_defaults(handler=command_format)

    tidy = subparsers.add_parser("tidy", help="clang-tidy over the project's own sources, findings by check")
    tidy.add_argument("paths", nargs="*", help="Only these files or directories, repository-relative")
    tidy.add_argument("--check", help="List every finding of one check, e.g. bugprone-signed-bitwise")
    tidy.add_argument("--limit", type=int, default=40, help="How many findings to list (default 40)")
    tidy.set_defaults(handler=command_tidy)

    sanitize = subparsers.add_parser("sanitize", help="The tests under ASan, UBSan and LeakSanitizer, in <build_dir>/sanitize")
    sanitize.add_argument("name", nargs="?", help="Only tests whose name matches this")
    sanitize.add_argument("--details", action="store_true", help="Also print each failing test's output")
    sanitize.add_argument("--clean", action="store_true", help="Rebuild everything")
    sanitize.set_defaults(handler=command_sanitize)

    fuzz = subparsers.add_parser("fuzz", help="libFuzzer with ASan and UBSan over [cpp.fuzz] targets, in <build_dir>/fuzz")
    fuzz.add_argument("target", nargs="?", help="One target (default: all of them)")
    fuzz.add_argument("--seconds", type=int, default=30, help="How long each target runs (default 30)")
    fuzz.add_argument("--reproduce", metavar="FILE", help="Run this one input through the target, e.g. a crash-... file")
    fuzz.set_defaults(handler=command_fuzz)

    run_command = subparsers.add_parser("run", help="Build, then run the binary with the arguments passed through")
    run_command.add_argument("--debug", action="store_true", help="Build Debug first")
    run_command.add_argument("arguments", nargs=argparse.REMAINDER, help="Passed to the binary unchanged")
    run_command.set_defaults(handler=command_run)

    install = subparsers.add_parser("install", help=f"cmake --install into {INSTALL_ROOT}")
    install.add_argument("--menu-only", action="store_true", help="Only the .desktop file, no build")
    install.set_defaults(handler=command_install)

    uninstall = subparsers.add_parser("uninstall", help=f"Remove what install put into {INSTALL_ROOT}")
    uninstall.add_argument("--dry-run", action="store_true", help="Only list what would be removed")
    uninstall.set_defaults(handler=command_uninstall)

    subparsers.add_parser("package", help="An RPM through CPack: Release, prefix /usr, in <build_dir>/package") \
        .set_defaults(handler=command_package)

    subparsers.add_parser("toolchain", help="Compiler, cmake, standard and what find_package found") \
        .set_defaults(handler=command_toolchain)

    subparsers.add_parser("kde", help="The KDE libraries on this machine, and which this project uses") \
        .set_defaults(handler=command_kde)

    android_command = subparsers.add_parser("android", help="An Android .apk through Qt's toolchain file and ECM")
    android_command.add_argument("action", choices=["build", "install", "run", "devices"])
    android_command.add_argument("--no-build", action="store_true", help="install: use the .apk already built")
    android_command.add_argument("--clean", action="store_true", help="build: rebuild everything")
    android_command.set_defaults(handler=command_android)

    arguments = parser.parse_args()

    if arguments.command is None:
        parser.print_help()

        return 0

    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
