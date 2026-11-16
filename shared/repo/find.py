#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Where is this symbol used?

The recon question before every change here, and it was a hand-built `grep -rn ... | grep -v obj/`
every time. Four things went wrong with that, reliably:

  * `obj/` and `bin/` bury the answer. This repository's source generators emit `*_Equatable.g.cs`,
    `*_FileExporter.g.cs` and a full second copy of the Razor output, so a symbol that appears in
    four real files appears in fourteen hits.
  * The interesting answer is usually "nowhere" - and that one is easy to get wrong by hand, because
    the type's own declaration always matches and looks like a usage.
  * Retyping the file filter. A CSS class lives in `.razor.css`, a JS interop function in `.js`, a
    package in a `.csproj` - and the answer to "is this still used" is wrong if the search only ever
    looked at `.cs`.
  * The cross-project sweep needs the absolute path to dev/projects/dotnet, retyped each time.

    python3 Scripts/find.py StringCrypter              this repository
    python3 Scripts/find.py Alpha Beta Gamma           several at once: is any of them still used
    python3 Scripts/find.py StringCrypter --all        every project under dev/projects/dotnet
    python3 Scripts/find.py FileItemId --lines         with the matching lines, not just the files
    python3 Scripts/find.py FileItem --show            print the declaring type's members
    python3 Scripts/find.py Customer --in Components/Pages/Orders --lines   only under that path
    python3 Scripts/find.py AutoFit --in ~/dev/tools/fluentui-blazor   a checkout next
                                                       door: --in takes an absolute path, which is
                                                       how the FluentUI and Istarnova sources are
                                                       read without a hand-built grep
    python3 Scripts/find.py --types --in Components/Pages/Orders/Detail   what each file declares,
                                                       and what it derives from (@inherits included)
    python3 Scripts/find.py --multiple-types           .cs files with more than one top-level type
    python3 Scripts/find.py fluent-card --type web    only css/scss/sass/js/ts
    python3 Scripts/find.py showDialog --type js,razor an explicit extension list
    python3 Scripts/find.py IDialogService --nuget     inside the packages this solution resolves
    python3 Scripts/find.py "sortDate" --text          any file type, no word boundary: markdown,
                                                       JSON, SQL, shell - where a symbol search
                                                       answers "nowhere" and is wrong about it
    python3 Scripts/find.py --tag FluentButton --with IconStart --without Title --empty
                                                       the buttons in .razor files that are only an
                                                       icon and have no Title: a question about one
                                                       tag, which a line search cannot ask once the
                                                       attributes are spread over several lines

"Does something like this already exist?" is a --text search, not a symbol search.

--tag reads Razor, it does not match it with a pattern. An opening tag ends at the first `>` outside
its attribute values, and in Razor a value regularly holds one: `OnClick="@(() => Save(item))"`, or a
nested string, `Content="@($"{a}: {b}")"`. A regex that stops at the first `>` cuts the tag in the
middle of a lambda and then reports the attributes behind it as missing - which is the wrong answer to
exactly the question --without asks. So the tag is scanned, quotes and `@( ... )` expressions skipped.

The symbol search matches on word boundaries, so it answers "where is X used" once you already know
X. It cannot answer "do we have anything for uploads": `find.py Upload` does not find
`UploadSettings`, because `Upload` is not the whole word. Asked that way it reports nothing, which
reads as "does not exist" and is how a second copy of an existing class gets written - see
AppUploadSettings, written on 2026-08-31 beside the UploadSettings that had been there for eleven
days. `find.py "Upload" --text` finds it in the first three hits, appsettings.json included.

The rule: reach for --text whenever the name you are searching for is the one you are about to
invent. You are looking for a concept, and the symbol search only knows identifiers.

Exit code is 1 when the symbol is not found at all, so it can gate a chain.
"""

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from project import project

REPO_ROOT = project.root
PROJECTS_ROOT = REPO_ROOT.parent

# Named groups, because "the frontend assets" is a question people ask more often than they want to
# spell out five extensions. --type also takes bare extensions, so the groups are a shortcut and
# never a limit.
TYPE_GROUPS = {
    "code": ["cs", "razor", "cshtml", "rs"],
    "web": ["css", "scss", "sass", "js", "ts"],
    "project": ["csproj", "props", "targets", "sln", "toml"],
    "cpp": ["cpp", "cc", "cxx", "c", "h", "hpp", "hh", "hxx", "qml", "cmake"],
    "python": ["py", "pyi", "toml"],
    "rust": ["rs", "toml"],
}

# One profile per language family: what to search, what never to search, how a declaration looks
# (`{symbol}` is the name, or `\w+` for "any declaration"), and the file that gives the family away.
# A project overrides any part in project.toml: find.profile, find.types, find.exclude, find.declaration.
PROFILES = {
    "dotnet": {
        "types": [extension for group in ("code", "web", "project") for extension in TYPE_GROUPS[group]],
        "exclude": ["!**/obj/**", "!**/bin/**", "!**/nupkg/**", "!**/coverage/**", "!**/*.g.cs"],
        "declaration": r"\b(?:class|interface|record|struct|enum)\s+(?:\w+\s+)*{symbol}\b|"
                       r"\b(?:public|private|protected|internal)\b.*\b{symbol}\s*[{;=(]",
        "markers": ["*.sln", "*.slnx", "*.csproj", "Directory.Build.props", "global.json"],
    },
    "cpp": {
        "types": TYPE_GROUPS["cpp"] + ["txt"],
        "exclude": ["!**/build/**", "!**/build-*/**", "!**/cmake-build-*/**", "!**/.cache/**", "!**/CMakeFiles/**"],
        "declaration": r"\b(?:class|struct|enum|union|namespace|typedef|using)\s+(?:\w+\s+)*{symbol}\b(?!\s*;)|"
                       r"^[\w:<>,*&~\s]+\b{symbol}\s*\([^;]*$|\bQ_PROPERTY\(.*\b{symbol}\b|\bid:\s*{symbol}\b",
        "forward": r"^\s*(?:class|struct|union|enum(?:\s+class|\s+struct)?)\s+{symbol}\s*(?::\s*[\w:\s]+)?;",
        "markers": ["CMakeLists.txt", "meson.build"],
    },
    "python": {
        "types": TYPE_GROUPS["python"] + ["cfg", "ini"],
        "exclude": ["!**/.venv/**", "!**/venv/**", "!**/__pycache__/**", "!**/*.egg-info/**", "!**/build/**",
                    "!**/dist/**", "!**/.tox/**"],
        "declaration": r"\b(?:class|def)\s+{symbol}\b|^{symbol}\s*[:=]",
        "markers": ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"],
    },
    "rust": {
        "types": TYPE_GROUPS["rust"],
        "exclude": ["!**/target/**"],
        "declaration": r"\b(?:struct|enum|trait|fn|type|const|static|mod|union|macro_rules!)\s+{symbol}\b",
        "markers": ["Cargo.toml"],
    },
    "typescript": {
        "types": ["ts", "tsx", "js", "jsx", "mjs", "cjs", "vue", "svelte", "css", "scss", "html", "json"],
        "exclude": ["!**/node_modules/**", "!**/dist/**", "!**/build/**", "!**/.next/**", "!**/coverage/**",
                    "!**/*.min.*", "!**/package-lock.json"],
        "declaration": r"\b(?:class|interface|type|enum|function|const|let|var|namespace)\s+{symbol}\b",
        "markers": ["package.json", "tsconfig.json"],
    },
}


def detect_profile(root: Path) -> str:
    """The configured profile, else the first whose marker file is in the repository root.

    Nothing recognised means dotnet, which is what this script did before it had profiles.
    """
    configured = project.get("find.profile")

    if configured:
        if configured not in PROFILES:
            sys.exit(f"find.profile {configured!r}: expected one of {', '.join(PROFILES)}")

        return configured

    for name, profile in PROFILES.items():
        if any(next(root.glob(marker), None) for marker in profile["markers"]):
            return name

    return "dotnet"


PROFILE_NAME = detect_profile(REPO_ROOT)
PROFILE = PROFILES[PROFILE_NAME]

# Which of those a project actually has. Searching a Rust crate for a symbol and being told
# "no match in cs, razor, cshtml" is an answer about the wrong question, and it reads as "not there".
DEFAULT_TYPES = project.get("find.types") or PROFILE["types"]

# Inside a NuGet package the searchable part is the documentation, the build logic and the static web
# assets. The DLLs are not text and the symbol is in the XML docs anyway.
NUGET_TYPES = ["xml", "props", "targets", "js", "css", "razor", "cs", "json"]

# rg honours .gitignore, which already drops obj/ and bin/ in a clean checkout. These cover the
# copies that are checked in anyway, or left behind in a directory that is not a git repository.
BUILD_EXCLUDES = PROFILE["exclude"] + [f"!{pattern}" for pattern in project.get("find.exclude", [])]

# Vendored frontend assets. Bootstrap alone is 22 tracked files here, and a hit inside its minified
# bundle answers no question anyone asked. --vendor puts them back.
VENDOR_EXCLUDES = ["!**/wwwroot/lib/**", "!**/node_modules/**", "!**/*.min.*", "!**/dist/**"]

DECLARATION = project.get("find.declaration") or PROFILE["declaration"]


def declaration_pattern(symbol: str | None = None, forward: bool = False) -> re.Pattern:
    """The profile's declaration regex for one symbol, or for any name when `symbol` is None.

    `forward` adds the forward declarations (--forward), which the declaration pattern leaves out.
    """
    name = re.escape(symbol) if symbol else r"\w+"
    pattern = DECLARATION.replace("{symbol}", name)

    if forward:
        pattern = f"(?:{pattern})|(?:{PROFILE['forward'].replace('{symbol}', name)})"

    return re.compile(pattern)


def resolve_types(requested: str | None) -> list[str]:
    """Turn --type into a list of extensions, expanding the named groups."""
    if not requested:
        return DEFAULT_TYPES

    extensions = []

    for entry in (part.strip().lstrip(".") for part in requested.split(",")):
        if not entry:
            continue

        extensions += TYPE_GROUPS.get(entry, [entry])

    if not extensions:
        sys.exit(f"--type {requested!r} resolved to nothing")

    return extensions


def resolve_paths(requested: list[str] | None, root: Path) -> list[Path]:
    """The roots to search: whatever --in named, or the whole tree when it named nothing.

    A relative path is taken from the repository root rather than from the caller's working
    directory - every other script here works that way, and the working directory is not something
    the caller should have to think about.
    """
    if not requested:
        return [root]

    resolved = []

    for entry in requested:
        candidate = Path(entry)
        candidate = candidate if candidate.is_absolute() else root / entry

        if not candidate.exists():
            sys.exit(missing_path_message(entry, candidate, root))

        resolved.append(candidate)

    return resolved


def missing_path_message(entry: str, candidate: Path, root: Path) -> str:
    """Say what is there instead: the entries beside the deepest part of the path that does exist."""
    parent = candidate.parent

    while not parent.exists() and parent != parent.parent:
        parent = parent.parent

    names = sorted(child.name + ("/" if child.is_dir() else "") for child in parent.iterdir()
                   if not child.name.startswith("."))
    close = difflib.get_close_matches(candidate.name, [name.rstrip("/") for name in names], n=3)
    shown = parent.relative_to(root) if parent.is_relative_to(root) else parent
    hint = f"  (did you mean: {', '.join(close)}?)" if close else ""

    return (f"--in {entry!r}: no such file or directory{hint}\n"
            f"under {shown}/: {', '.join(names[:40])}{' ...' if len(names) > 40 else ''}")


def reporting_root(roots: list[Path], default: Path) -> Path:
    """What the output names, and what the printed paths are shortened against.

    A --in path may point outside this repository - the FluentUI or Istarnova checkout, read for
    reference. Measured against the repository root those hits shorten to nothing: the header names a
    directory the search never touched, every path prints in full, and they all land in a group
    called "/". Output that looks broken is what sends somebody back to a hand-built grep.
    """
    outside = [entry for entry in roots if not entry.is_relative_to(default)]

    if not outside or len(outside) != len(roots):
        return default

    common = Path(os.path.commonpath([str(entry) for entry in outside]))

    return common if common.is_dir() else common.parent


def run_ripgrep(symbols: list[str], roots: list[Path], extensions: list[str], excludes: list[str],
                whole_word: bool = True, explicit: bool = False):
    """Every match for any of the symbols, as (path, line number, text).

    A root that is a file is searched without the extension filter. rg applies its globs to
    explicitly named files too, so a filtered run over `--in Something.razor.cs` silently returns
    nothing - and "no match" is exactly the answer this script must never get wrong.
    """
    directories = [root for root in roots if root.is_dir()]
    files = [root for root in roots if not root.is_dir()]

    matches = []

    if directories:
        matches += ripgrep(symbols, directories, extensions, excludes, whole_word, explicit)
    if files:
        matches += ripgrep(symbols, files, [], [], whole_word, explicit)

    return matches


def ripgrep(symbols: list[str], roots: list[Path], extensions: list[str], excludes: list[str],
            whole_word: bool = True, explicit: bool = False):
    """One ripgrep run over the given roots.

    Several symbols go into one run as several `-e` patterns rather than one alternation: they are
    literals, and building a regex out of them would make a `?` or a `.` in an identifier mean
    something it does not.

    `whole_word` off is the --text mode: a phrase inside a sentence, an attribute in the middle of a
    tag, a fragment of a path - none of which a word boundary would find.
    """
    # --with-filename because rg leaves the name out when it was given exactly one file to search,
    # and every line here is parsed as "path:line:text".
    command = ["rg", "--fixed-strings", "--line-number", "--with-filename",
               "--no-heading", "--color", "never"]

    # A path named with --in is searched even when git ignores it. Data/ holds the exports and dumps
    # every import question is actually about, and "no match" in a file the caller pointed at by hand
    # is the one answer this script must never give. Without --in the ignore rules stay on, or a sweep
    # would drown in dumps nobody asked about.
    if explicit:
        command.append("--no-ignore-vcs")

    if whole_word:
        command.append("--word-regexp")

    for extension in extensions:
        command += ["--glob", f"*.{extension}"]
    for exclude in excludes:
        command += ["--glob", exclude]
    for symbol in symbols:
        command += ["-e", symbol]

    command += ["--"] + [str(root) for root in roots]

    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode not in (0, 1):
        sys.exit(f"ripgrep failed: {result.stderr.strip()}")

    matches = []

    for line in result.stdout.splitlines():
        path, _, rest = line.partition(":")
        number, _, text = rest.partition(":")

        if number.isdigit():
            matches.append((Path(path), int(number), text.strip()))

    return matches


def skip_csharp_string(text: str, index: int) -> int:
    """The index after the C# string literal that opens at `index` (a double quote)."""
    position = index + 1

    while position < len(text):
        if text[position] == "\\":
            position += 2
            continue

        if text[position] == '"':
            return position + 1

        position += 1

    return len(text)


def skip_expression(text: str, index: int) -> int:
    """The index after the balanced `( ... )` that opens at `index`, strings inside it skipped.

    The parentheses of a lambda and the quotes of an interpolated string both live in here, and
    neither is allowed to end the expression early.
    """
    depth = 0
    position = index

    while position < len(text):
        character = text[position]

        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1

            if depth == 0:
                return position + 1
        elif character == '"':
            position = skip_csharp_string(text, position)
            continue
        elif character == "'":
            # A char literal, '"' included: skipped as three characters, or four for an escape.
            position += 4 if text[position + 1:position + 2] == "\\" else 3
            continue

        position += 1

    return len(text)


def skip_attribute_value(text: str, index: int) -> int:
    """The index after the quoted attribute value that opens at `index`."""
    quote = text[index]
    position = index + 1

    while position < len(text):
        # `"@( ... )"` may hold double quotes of its own: the expression is skipped as a whole.
        if quote == '"' and text[position] == "@" and text[position + 1:position + 2] == "(":
            position = skip_expression(text, position + 1)
            continue

        if text[position] == quote:
            return position + 1

        position += 1

    return len(text)


def scan_tag(text: str, index: int) -> tuple[int, set[str]]:
    """Where the opening tag whose name ends at `index` ends, and the attributes it carries.

    Returns the index of its closing `>` and the attribute names, lower-cased: a boolean attribute
    (`IconOnly`) and one with a value (`Title="..."`) both count, `@bind-Value` and `@onclick` too.
    """
    names: set[str] = set()
    position = index

    while position < len(text):
        character = text[position]

        if character == ">":
            return position, names

        if character.isspace() or character == "/":
            position += 1
            continue

        if character in "\"'":
            position = skip_attribute_value(text, position)
            continue

        start = position

        while position < len(text) and text[position] not in " \t\r\n=/>\"'":
            position += 1

        names.add(text[start:position].lower())

        while position < len(text) and text[position].isspace():
            position += 1

        if text[position:position + 1] != "=":
            continue

        position += 1

        while position < len(text) and text[position].isspace():
            position += 1

        if text[position:position + 1] in ("\"", "'"):
            position = skip_attribute_value(text, position)
        elif text[position:position + 2] == "@(":
            position = skip_expression(text, position + 1)
        else:
            while position < len(text) and not text[position].isspace() and text[position] not in "/>":
                position += 1

    return len(text), names


def razor_tags(text: str, tag: str):
    """Every opening tag of `tag` in a Razor file, as (line number, tag text, attribute names, empty).

    `empty` says the element holds nothing: it closes itself (`/>`) or is closed straight away, with
    nothing but whitespace between the tags. That is what separates a button that is only an icon
    from one that carries a label, which an opening tag alone cannot tell.
    """
    opening = re.compile(rf"<{re.escape(tag)}(?=[\s/>])")

    for match in opening.finditer(text):
        end, names = scan_tag(text, match.end())
        tag_text = text[match.start():end + 1]
        empty = tag_text.endswith("/>") or re.match(rf"\s*</{re.escape(tag)}\s*>", text[end + 1:]) is not None

        yield text.count("\n", 0, match.start()) + 1, tag_text, names, empty


def razor_files(roots: list[Path], excludes: list[str], explicit: bool) -> list[Path]:
    """The .razor files under the roots; a root that is a file is taken as it is."""
    found = [root for root in roots if not root.is_dir()]
    directories = [root for root in roots if root.is_dir()]

    if directories:
        command = ["rg", "--files", "--glob", "*.razor"]

        if explicit:
            command.append("--no-ignore-vcs")

        for exclude in excludes:
            command += ["--glob", exclude]

        command += ["--"] + [str(root) for root in directories]

        result = subprocess.run(command, capture_output=True, text=True)

        if result.returncode not in (0, 1):
            sys.exit(f"ripgrep failed: {result.stderr.strip()}")

        found += [Path(line) for line in result.stdout.splitlines()]

    return sorted(found)


def search_tags(arguments, roots: list[Path], excludes: list[str], root: Path) -> int:
    """--tag: the opening tags of one component that carry (--with) and lack (--without) attributes."""
    required = {name.lower() for name in arguments.with_attributes or []}
    forbidden = {name.lower() for name in arguments.without_attributes or []}
    matches = []

    for path in razor_files(roots, excludes, arguments.paths is not None):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for number, tag_text, names, empty in razor_tags(text, arguments.tag):
            if required <= names and not (forbidden & names) and (empty or not arguments.empty):
                matches.append((path, number, " ".join(tag_text.split())))

    conditions = [f"with {', '.join(sorted(arguments.with_attributes))}"] if required else []
    conditions += [f"without {', '.join(sorted(arguments.without_attributes))}"] if forbidden else []
    conditions += ["with no content"] if arguments.empty else []
    wanted = f"<{arguments.tag}> {' '.join(conditions)}".rstrip()

    if not matches:
        print(f"{wanted}: no match under {', '.join(str(entry) for entry in roots)} in razor")
        return 1

    files = {path for path, _, _ in matches}

    print(f"{wanted}: {len(matches)} match(es) in {len(files)} file(s) under {root}")
    print("\nfound in")

    for project_name, entries in sorted(group_by_project(matches, root).items()):
        print_group(project_name, entries, True)

    return 0


def resolved_packages(root: Path) -> tuple[list[Path], int]:
    """The packages this solution actually resolves, at the versions it resolved them to.

    Read from every `project.assets.json` under the root rather than from the global cache: that cache
    is 22 GB and carries ten versions of the same package, so searching it whole answers a question
    nobody asked. The assets file names the exact directory per package, and the packageFolders entry
    names the cache root, so nothing here is hardcoded to one machine.
    """
    directories = {}

    for assets_file in root.glob("**/obj/project.assets.json"):
        try:
            assets = json.loads(assets_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        folders = [Path(folder) for folder in assets.get("packageFolders", {})]

        for identifier, library in assets.get("libraries", {}).items():
            if library.get("type") != "package" or "path" not in library:
                continue

            for folder in folders:
                candidate = folder / library["path"]

                if candidate.is_dir():
                    directories[identifier] = candidate
                    break

    return sorted(directories.values()), len(directories)


def group_by_project(matches, root: Path) -> dict[str, list]:
    """Group matches by their top-level project directory, which is how the answer is read."""
    grouped = defaultdict(list)

    for path, number, text in matches:
        relative = path.relative_to(root) if path.is_relative_to(root) else path
        project = relative.parts[0] if len(relative.parts) > 1 else "."
        grouped[project].append((relative, number, text))

    return grouped


def group_by_package(matches, package_directories: list[Path]) -> dict[str, list]:
    """Group matches by the package they came out of, named `package/version`."""
    grouped = defaultdict(list)

    for path, number, text in matches:
        for directory in package_directories:
            if path.is_relative_to(directory):
                name = f"{directory.parent.name}/{directory.name}"
                grouped[name].append((path.relative_to(directory), number, text))
                break

    return grouped


TYPE_DECLARATION = re.compile(r"\b(class|interface|record|struct|enum)\s+\w")

# A Blazor component says what it derives from in markup, not in C#.
RAZOR_DECLARATION = re.compile(r"^\s*@(inherits|implements|typeparam|page|attribute)\b")


def type_declarations(path: Path) -> list[str]:
    """What a file declares, and what it derives from, as one line each.

    The base list is regularly wrapped onto the following lines here - `EntityFilesSection` spends
    three lines on its own declaration - so a declaration is joined up to its opening brace before
    it is printed. `.razor` files answer the same question with `@inherits`, which is why they are
    read too: for a component, that line *is* the base class.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []

    declarations = []
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if path.suffix == ".razor":
            if RAZOR_DECLARATION.match(line):
                declarations.append((index + 1, stripped))

            index += 1
            continue

        if stripped.startswith("//") or not TYPE_DECLARATION.search(stripped):
            index += 1
            continue

        # Join until the body opens. The cap is there so a false positive - the words in a comment
        # or a string - cannot swallow the rest of the file.
        parts = [stripped]
        start = index

        while "{" not in parts[-1] and index - start < 5 and index + 1 < len(lines):
            index += 1
            parts.append(lines[index].strip())

        text = " ".join(parts).split("{")[0].strip()
        declarations.append((start + 1, " ".join(text.split())))
        index += 1

    return declarations


def print_declarations(paths: list[Path], root: Path, forward: bool = False) -> int:
    """The type declarations of every file under the given paths."""
    if PROFILE_NAME != "dotnet":
        return print_profile_declarations(paths, root, forward)

    files = sorted({
        candidate
        for path in paths
        for candidate in ([path] if path.is_file() else path.rglob("*"))
        if candidate.is_file()
        and candidate.suffix in (".cs", ".razor")
        and not any(part in ("obj", "bin") for part in candidate.parts)
        and not candidate.name.endswith(".g.cs")
    })

    printed = 0

    for path in files:
        declarations = type_declarations(path)

        if not declarations:
            continue

        print(f"\n  {path.relative_to(root) if path.is_relative_to(root) else path}")

        for number, text in declarations:
            print(f"    {number:>5}  {text}")
            printed += 1

    if printed == 0:
        print("no type declaration found")

    return 0 if printed else 1


def print_profile_declarations(paths: list[Path], root: Path, forward: bool = False) -> int:
    """--types outside .NET: every line the profile's declaration pattern matches, per file."""
    pattern = declaration_pattern(forward=forward)
    printed = 0

    for path in files_under(paths, DEFAULT_TYPES, BUILD_EXCLUDES):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        found = [(number, line.strip()) for number, line in enumerate(lines, start=1) if pattern.search(line)]

        if found:
            print(f"\n  {path.relative_to(root) if path.is_relative_to(root) else path}")

        for number, text in found:
            print(f"    {number:>5}  {text}")
            printed += 1

    if printed == 0:
        print("no declaration found")

    return 0 if printed else 1


def files_under(paths: list[Path], extensions: list[str], excludes: list[str], explicit: bool = True) -> list[Path]:
    """The files rg would search under the paths, honouring the ignore rules and the profile."""
    found = [path for path in paths if not path.is_dir()]
    directories = [path for path in paths if path.is_dir()]

    if directories:
        command = ["rg", "--files"] + (["--no-ignore-vcs"] if explicit else [])
        command += [f"--glob=*.{extension}" for extension in extensions]
        command += [f"--glob={exclude}" for exclude in excludes]
        result = subprocess.run(command + ["--"] + [str(path) for path in directories],
                                capture_output=True, text=True)

        if result.returncode not in (0, 1):
            sys.exit(f"ripgrep failed: {result.stderr.strip()}")

        found += [Path(line) for line in result.stdout.splitlines()]

    return sorted(found)


def list_files(paths: list[Path], extensions: list[str], excludes: list[str], root: Path, explicit: bool) -> int:
    """--files: what is under the paths, so nobody falls back to `ls` and sees build output."""
    files = files_under(paths, extensions, excludes, explicit)

    for path in files:
        print(path.relative_to(root) if path.is_relative_to(root) else path)

    return 0 if files else 1


FILE_SCOPED_NAMESPACE = re.compile(r"^namespace\s+[\w.]+\s*;", re.MULTILINE)


def top_level_types(path: Path) -> list[tuple[int, str]]:
    """The types a .cs file declares at namespace level, nested types left out.

    Told apart by indentation: a file-scoped namespace puts them in column 0, a block namespace one
    level in. That is what separates a second class in the file from a private record inside the
    first one, and from `foreach (var record in records)`, which the plain --types scan counts.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    indent = 0 if FILE_SCOPED_NAMESPACE.search(content) or "namespace" not in content else 4
    declarations = []

    for number, line in enumerate(content.splitlines(), start=1):
        leading = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        if leading != indent or stripped.startswith(("//", "*", "///")):
            continue

        if TYPE_DECLARATION.search(stripped.split("(")[0].split("//")[0]):
            declarations.append((number, stripped.split("{")[0].strip()))

    return declarations


# Held to a lower standard on purpose: single-file scripts are throwaway by nature and cannot be
# split. A project adds its own exemptions through find.multiple_types_exclude in project.toml.
MULTIPLE_TYPES_EXCLUDES = ["!Scripts/**"] + [
    f"!{pattern}" for pattern in project.get("find.multiple_types_exclude", [])]


def print_multiple_types(paths: list[Path], root: Path) -> int:
    """Every tracked .cs file that declares more than one top-level type.

    The file list comes from rg, so git-ignored directories such as Data/ stay out as well.
    """
    command = ["rg", "--files", "--glob", "*.cs"] + [
        f"--glob={exclude}" for exclude in BUILD_EXCLUDES + MULTIPLE_TYPES_EXCLUDES
    ]
    result = subprocess.run(command + ["--"] + [str(path) for path in paths], capture_output=True, text=True)
    files = sorted(Path(line) for line in result.stdout.splitlines())

    found = 0

    for path in files:
        declarations = top_level_types(path)

        if len(declarations) < 2:
            continue

        found += 1
        print(f"\n  {path.relative_to(root) if path.is_relative_to(root) else path}")

        for number, text in declarations:
            print(f"    {number:>5}  {text}")

    print(f"\n{found} file(s) declare more than one top-level type")

    return 1 if found else 0


def find_declaration(symbol: str, matches) -> Path | None:
    """The file that declares the symbol, so it can be told apart from the files that use it."""
    pattern = declaration_pattern(symbol)

    for path, _, text in matches:
        if pattern.search(text):
            return path

    return None


def print_forward_declarations(symbols: list[str], matches, root: Path) -> None:
    """--forward: the lines that only name a type, `class X;`, which the declaration search skips."""
    patterns = [re.compile(PROFILE["forward"].replace("{symbol}", re.escape(symbol))) for symbol in symbols]
    found = [(path, number, text) for path, number, text in matches if any(p.search(text) for p in patterns)]

    print(f"\nforward-declared in  ({len(found)})" if found else "\nno forward declaration")

    for path, number, text in found:
        print(f"    {path.relative_to(root) if path.is_relative_to(root) else path}:{number}  {text}")


def show_members(path: Path, root: Path, forward: bool = False) -> None:
    """The shape of a type, without opening the file: its declaration and public members."""
    print(f"\n  {path.relative_to(root) if path.is_relative_to(root) else path}")

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()

        if PROFILE_NAME == "dotnet":
            wanted = stripped.startswith(("public ", "internal ")) or " class " in stripped
        else:
            wanted = declaration_pattern(forward=forward).search(line) is not None

        if wanted:
            print(f"    {number:>5}  {stripped}")


def print_group(title: str, entries: list, with_lines: bool) -> None:
    print(f"\n  {title}  ({len(entries)})")

    if with_lines:
        for relative, number, text in entries:
            print(f"    {relative}:{number}  {text}")
    else:
        for relative in sorted({relative for relative, _, _ in entries}):
            print(f"    {relative}")


def search_nuget(arguments, extensions: list[str]) -> int:
    directories, count = resolved_packages(REPO_ROOT)

    if not directories:
        sys.exit("no project.assets.json found - build the solution first")

    matches = run_ripgrep(arguments.symbol, directories, extensions, BUILD_EXCLUDES)
    wanted = ", ".join(arguments.symbol)

    if not matches:
        print(f"{wanted}: no match in the {count} package(s) this solution resolves")
        return 1

    grouped = group_by_package(matches, directories)
    print(f"{wanted}: {len(matches)} match(es) in {len(grouped)} of the {count} resolved package(s)")

    for package, entries in sorted(grouped.items()):
        print_group(package, entries, arguments.lines)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "symbol",
        nargs="*",
        help="One or more types, members, CSS classes or constants to look for. "
             "Several are searched in one pass, which is what 'is any of these still referenced' "
             "wants. Optional with --types",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Search every project under dev/projects/dotnet, not only this repository",
    )
    parser.add_argument(
        "--type",
        help=f"Extensions or a group ({', '.join(TYPE_GROUPS)}), comma separated. Default: all groups",
    )
    parser.add_argument(
        "--nuget",
        action="store_true",
        help="Search inside the NuGet packages this solution resolves, instead of its source",
    )
    parser.add_argument(
        "--in",
        dest="paths",
        action="append",
        metavar="PATH",
        help="Restrict the search to these files or directories (repeatable), relative to the repository root",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Search any file type for the text as it stands, not for a symbol: no word boundary, "
             "and markdown, JSON, SQL, shell and the rest included. This is the mode for 'where does "
             "this string appear', which a symbol search answers with a wrong 'nowhere'",
    )
    parser.add_argument("--vendor", action="store_true", help="Include wwwroot/lib, node_modules and minified bundles")
    parser.add_argument("--lines", action="store_true", help="Print the matching lines, not only the files")
    parser.add_argument("--show", action="store_true", help="Print the members of the declaring type")
    parser.add_argument(
        "--types",
        action="store_true",
        help="Print what each file declares and what it derives from, instead of searching",
    )

    parser.add_argument(
        "--multiple-types",
        action="store_true",
        help="List the .cs files that declare more than one top-level type; exit code 1 when there are any",
    )
    parser.add_argument(
        "--tag",
        metavar="NAME",
        help="List the opening tags of one component in .razor files, reading each tag as Razor (a `>` "
             "inside an attribute value does not end it) instead of matching it line by line. Narrow "
             "them with --with and --without",
    )
    parser.add_argument(
        "--with",
        dest="with_attributes",
        action="append",
        metavar="ATTRIBUTE",
        help="With --tag: only tags that carry this attribute (repeatable, all must be there)",
    )
    parser.add_argument(
        "--without",
        dest="without_attributes",
        action="append",
        metavar="ATTRIBUTE",
        help="With --tag: only tags that lack this attribute (repeatable, any of them present rules a tag out)",
    )

    parser.add_argument(
        "--empty",
        action="store_true",
        help="With --tag: only elements that hold nothing, `<Tag ... />` or `<Tag ...></Tag>`: a button "
             "that is only an icon, as against one that carries a label",
    )

    parser.add_argument(
        "--forward",
        action="store_true",
        help="Also list the forward declarations (`class X;`), which 'declared in' leaves out on purpose: "
             "a header that only names the type is a dependency worth seeing. Applies to the search, "
             "--show and --types. C++ only",
    )
    parser.add_argument(
        "--files",
        action="store_true",
        help="List the files under --in (default: the repository) that a search would look at, build "
             "output and ignored directories left out. --type narrows it, --text lists every file type",
    )

    arguments = parser.parse_args()

    if (arguments.with_attributes or arguments.without_attributes or arguments.empty) and not arguments.tag:
        parser.error("--with, --without and --empty need --tag")

    if arguments.forward and "forward" not in PROFILE:
        parser.error(f"--forward: the {PROFILE_NAME!r} profile has no forward declarations")

    if arguments.files:
        root = PROJECTS_ROOT if arguments.all else REPO_ROOT
        roots = resolve_paths(arguments.paths, root)
        extensions = resolve_types(arguments.type) if arguments.type or not arguments.text else []
        excludes = BUILD_EXCLUDES if arguments.vendor else BUILD_EXCLUDES + VENDOR_EXCLUDES

        return list_files(roots, extensions, excludes, reporting_root(roots, root), arguments.paths is not None)

    if arguments.multiple_types and PROFILE_NAME != "dotnet":
        parser.error(f"--multiple-types is a C# check; this repository's profile is {PROFILE_NAME!r}")

    if arguments.multiple_types:
        root = PROJECTS_ROOT if arguments.all else REPO_ROOT

        return print_multiple_types(resolve_paths(arguments.paths, root), root)

    if arguments.types:
        root = PROJECTS_ROOT if arguments.all else REPO_ROOT

        return print_declarations(resolve_paths(arguments.paths, root), root, arguments.forward)

    if arguments.tag:
        root = PROJECTS_ROOT if arguments.all else REPO_ROOT
        roots = resolve_paths(arguments.paths, root)
        excludes = BUILD_EXCLUDES if arguments.vendor else BUILD_EXCLUDES + VENDOR_EXCLUDES

        return search_tags(arguments, roots, excludes, reporting_root(roots, root))

    if not arguments.symbol:
        parser.error("a symbol is required unless --types or --tag is given")

    if arguments.nuget:
        return search_nuget(arguments, resolve_types(arguments.type) if arguments.type else NUGET_TYPES)

    root = PROJECTS_ROOT if arguments.all else REPO_ROOT

    # --text searches every file type unless one was named: the point of it is the files a symbol
    # search does not look at.
    extensions = resolve_types(arguments.type) if arguments.type or not arguments.text else []
    excludes = BUILD_EXCLUDES if arguments.vendor else BUILD_EXCLUDES + VENDOR_EXCLUDES
    roots = resolve_paths(arguments.paths, root)
    root = reporting_root(roots, root)

    matches = run_ripgrep(arguments.symbol, roots, extensions, excludes,
                          whole_word=not arguments.text, explicit=arguments.paths is not None)
    wanted = ", ".join(arguments.symbol)

    if not matches:
        searched = ", ".join(str(entry) for entry in roots)
        where = f" in {', '.join(extensions)}" if extensions else ""
        print(f"{wanted}: no match under {searched}{where}")
        return 1

    # The declaration/usage split needs one symbol to be about, and needs to have looked everywhere:
    # under --in the declaring file may simply be outside the paths that were asked about, and with
    # several symbols "declared in" has no single answer.
    declaration = (
        find_declaration(arguments.symbol[0], matches)
        if len(arguments.symbol) == 1 and not arguments.paths and not arguments.text
        else None
    )
    users = {path for path, _, _ in matches if path != declaration}

    print(
        f"{wanted}: {len(matches)} match(es) in "
        f"{len(users) + (1 if declaration else 0)} file(s) under {root}"
    )

    if declaration:
        relative = declaration.relative_to(root) if declaration.is_relative_to(root) else declaration
        print(f"\ndeclared in\n  {relative}")

    if users:
        # A text search has no declaration to contrast against, and the line is the answer there -
        # nobody greps for a phrase to be told which files hold it.
        print("\nfound in" if arguments.text else "\nreferenced in")

        for project, entries in sorted(group_by_project([m for m in matches if m[0] != declaration], root).items()):
            print_group(project, entries, arguments.lines or arguments.text)
    else:
        print("\nreferenced nowhere else - nothing depends on it")

    if arguments.forward:
        print_forward_declarations(arguments.symbol, matches, root)

    if arguments.show and declaration:
        show_members(declaration, root, arguments.forward)

    return 0


if __name__ == "__main__":
    sys.exit(main())
