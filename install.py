#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Point a project at this checkout.

    python3 ~/dev/tools/devscripts/install.py ~/dev/projects/dotnet/Something
    python3 ~/dev/tools/devscripts/install.py . --only dotnet,find,repo
    python3 ~/dev/tools/devscripts/install.py . --skills
    python3 ~/dev/tools/devscripts/install.py . --claude

It writes one shim per script into the project's Scripts/ folder, so `python3 Scripts/dotnet.py`
keeps working and every allow rule, hook and habit built around that spelling stays valid. The shim
is the same eight lines for every script; what differs per project is Scripts/project.toml, which is
written here as a starter with the values guessed from the solution file.

Every script in Scripts/ ends up executable, kept ones included, so `Scripts/dotnet.py verify` works without `python3`.

Nothing is overwritten: a project that already has its own `Scripts/find.py` keeps it and is named in
the summary, so replacing it stays a decision rather than an accident. --force overrides that.

--submodule adds devscripts as a git submodule in .devscripts/, for a project others clone. The shims
and hooks look for DEVSCRIPTS first, then ~/dev/tools/devscripts, then .devscripts: here the live
checkout is used, a contributor or CI gets the pinned one (`git submodule update --init`, or
`actions/checkout` with `submodules: true`).

--claude is everything Claude Code reads in a project: the skills (as --skills), the hooks that apply,
entered into .claude/settings.json, and a first .claude/CLAUDE.md that imports the guideline blocks of
this checkout (claude/*.md) for the kind of project it is.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

SHARED = Path(__file__).resolve().parent

# The scripts a project can share, and the project.toml block each one is useless without. A project
# with no [database] has no business carrying a db.py that can only refuse, so the config decides
# what gets installed - and re-running after filling a block in adds what it enables.
SHAREABLE = {
    "browser_extension.py": "browser_extension",
    "cpp.py": "cpp",
    "db.py": "database",
    "deploy.py": "deploy",
    "dotnet.py": "dotnet",
    "find.py": None,
    "gen_db_init_delta.py": "db_init",
    "gen_resx.py": "localization",
    "godot.py": "godot",
    "graph.py": None,
    "i18n.py": "i18n",
    "logs.py": "logs",
    "php.py": "php",
    "repo.py": None,
    "rpm.py": "rpm",
    "rust.py": "rust",
    "spdx.py": "spdx",
    "translations.py": "localization",
    "web.py": "web",
    "verify_db_init.py": "db_init",
}

# Which project.toml block a skill needs, for the ones that are not about every project. `None` -
# or simply being absent - means it always applies, the way feature-workflow does.
SKILL_BLOCKS = {
    "blazor-ui": "istarnova",
    "csharp-15": "dotnet",
    "data-layer": "istarnova",
    "i18n": "i18n",
    "localization": "localization",
    "logging": "dotnet",
}

# The guideline blocks a project's CLAUDE.md imports (claude/<name>.md), in the order they are listed
# there, and what each one needs: a project.toml block, or "kde" for a CMake project that asks for KDE
# Frameworks. `None` applies to every project.
CLAUDE_BLOCKS = {
    "base": None,
    "rust": "rust",
    "cpp": "cpp",
    "kde": "kde",
    "dotnet": "dotnet",
    "blazor": "istarnova",
    "php": "php",
    "web": "web",
}

EDITS = "Edit|Write|MultiEdit"

# The hooks, the tool calls each one listens to, and what it needs - chosen like the skills, and for the
# same reason: a hook that does not apply refuses things that are fine. "skill-map" is a project with a
# .claude/skill-map, which is all load-the-skill.sh reads.
HOOKS = {
    "no-cd-into-repo-root.sh": ("Bash", None),
    "prefer-repo-scripts.sh": ("Bash", None),
    "no-decorative-comments.sh": (EDITS, None),
    "load-the-skill.sh": (EDITS, "skill-map"),
    "no-direct-logger.sh": (EDITS, "dotnet"),
    "prefer-localized-grid.sh": (EDITS, "istarnova"),
}

# The checkout a hook runs from, in the order the shims use: DEVSCRIPTS, the shared checkout in the
# home directory, then a .devscripts submodule of the project (a contributor or CI has only that one).
HOOK_COMMAND = ('d="${{DEVSCRIPTS:-$HOME/dev/tools/devscripts}}"; [ -d "$d/hooks" ] || d="$CLAUDE_PROJECT_DIR/.devscripts"; '
                'bash "$d/hooks/{name}"')

# What earlier runs wrote, replaced by HOOK_COMMAND on the next run. A command the project wrote itself is left alone.
OLD_HOOK_COMMANDS = ('bash "${{DEVSCRIPTS:-$HOME/dev/tools/devscripts}}/hooks/{name}"',)

SUBMODULE = ".devscripts"
SUBMODULE_URL = "https://github.com/Istarlabs/devscripts.git"

CLAUDE_MD = '''# Claude Code Instructions

<!-- What this repository is, in two or three sentences, and what the program must never do. -->

## Shared guidelines

Imported from the shared devscripts checkout, so a rule is changed there once and reaches every
project. `install.py --claude` adds the line for a block that `Scripts/project.toml` enables later.
Claude Code asks once per project whether these files outside the repository may be loaded.

{imports}

## This project

<!-- What holds here only: the architecture, the names that must not change, the rules a refactor
     quietly breaks. A rule in this section wins over a shared one. -->
'''

SHIM = '''#!/usr/bin/env python3
"""Shim: the real {name} lives in the shared devscripts checkout.

Generated by devscripts/install.py - the values this project differs by are in project.toml.
"""
import os
import runpy
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = os.environ.get("DEVSCRIPTS") or os.path.expanduser("~/dev/tools/devscripts")
if not os.path.isdir(os.path.join(SHARED, "shared")):
    SHARED = os.path.join(PROJECT, ".devscripts")
GROUPS = os.path.join(SHARED, "shared")
FOLDERS = sorted(entry.path for entry in os.scandir(GROUPS)
                 if entry.is_dir() and entry.name != "__pycache__") if os.path.isdir(GROUPS) else []
NAME = os.path.basename(os.path.abspath(__file__))
TARGET = next((os.path.join(folder, NAME) for folder in FOLDERS if os.path.isfile(os.path.join(folder, NAME))), None)

if TARGET is None:
    sys.exit(f"refused: {{NAME}} is not under {{GROUPS}} - run `git submodule update --init`, "
             "or point DEVSCRIPTS at a devscripts checkout.")

os.environ["DEVSCRIPTS_PROJECT"] = PROJECT
sys.path[:0] = [SHARED, *FOLDERS]
runpy.run_path(TARGET, run_name="__main__")
'''

STARTER = '''# What this project differs by. Everything else lives in the shared devscripts checkout.
name = "{name}"
env_prefix = "{prefix}"

{language}
# [database]
# host = "db.example.invalid"
# port = "5432"
# name = "{name}"
# user = "postgres"

# [logs]
# app_project = "{name}"

# [php]                            # PHP: `php.py lint|test|verify` (podman php image when there is no local php)
# sources = ["lib", "appinfo"]

# [web]                             # JS/TS/Vue: `web.py install|lint|typecheck|test|build|verify` via package.json scripts
# directory = "."

# [browser_extension]               # Firefox add-on: `browser_extension.py lint|build|bump|sign`
# directory = "BrowserExtension"    # holds manifest.json
# artifacts = "web-ext-artifacts"   # under it; the signed .xpi lands here

# [android]                         # MAUI app: `dotnet.py android build|install|run`, `logs.py --android`
# project = "{name}.App"
# device = "192.168.1.50:37123"   # adb over WLAN
# [android.signing]                 # --release; the passwords stay in files
# keystore = "~/.config/{lower}-signing/{lower}.keystore"
# alias = "{lower}"
# store_password_file = "~/.config/{lower}-signing/password"
# key_password_file = "~/.config/{lower}-signing/key-password"

# [localization]                    # .NET: .resx generated from Scripts/translations/*.json
# resources_project = "{name}.Resources"
# namespace = "{name}.Resources"
# base_language = "en"
# satellite_languages = ["de"]

# [i18n]                            # Qt/KDE or Rust: gettext .pot and .po under po/
# domain = "{lower}"
# languages = ["de"]
# flavour = "kde"                   # "rust" reads .rs instead of C++ sources

# [istarnova]                       # uses Istarnova.EfCore / Blazor / Blazor.FluentUI / AspNetCore: brings the
# raw_grid_baseline = 0             # blazor-ui and data-layer skills, a skill map, and the raw-grid check

[spdx]
holder = "{name} Contributors"
licence = "AGPL-3.0-or-later"
# exclude = ["{name}/wwwroot/lib/"]

# [deploy]
# registry = "registry.example.invalid"
# image = "{lower}"
# ssh_host = "deploy-host"
# unit = "{prefix_lower}"

# [db_init]
# schema_file = "deploy/db-init/schema.sql"
# routines = "Scripts/dump/PostgreSQL"
# app_role_script = "deploy/app-role.sql"

{other_languages}'''

# One of these is written uncommented - whichever the repository looks like - and the other two
# follow it commented out. A project that is two of them at once uncomments the second by hand;
# guessing that from the file system would be wrong as often as it was right.
LANGUAGES = {
    "dotnet": '''[dotnet]
solution = "{solution}"
# test_project = "Some.Tests/Some.Tests.csproj"
# ef_project = "Some.Database.EfCore"
# startup_project = "Some"
# logging_baseline = 0
''',
    "rust": '''[rust]
binary = "{lower}"
install_root = "~/.local"
# [rust.service_menu]
# definitions = ["dolphin/{lower}.menu.toml"]
# directory = "~/.local/share/kio/servicemenus"
''',
    "cpp": '''[cpp]
binary = "{lower}"
build_dir = "build"
build_type = "RelWithDebInfo"
install_root = "~/.local"
# generator = "Ninja"
# configure_args = []
# binary_path = "bin/{lower}"
# A reader of files from outside: `verify` also runs the tests under ASan and UBSan (a second build).
# sanitize = true
# sanitize_suppressions = "Scripts/lsan.supp"
# [cpp.service_menu]
# source = "dolphin/{lower}.desktop"
# target = "~/.local/share/kio/servicemenus/{lower}.desktop"
# [cpp.fuzz]
# targets = ["fuzz_{lower}"]
# option = "BUILD_FUZZING"
# seeds_dir = "fuzz/seeds"
''',
}


def configured_blocks(config):
    """The top-level tables project.toml declares. Empty for a project that has none yet."""
    if not config.is_file():
        return set()

    with open(config, "rb") as handle:
        values = tomllib.load(handle)

    return {key for key, value in values.items() if isinstance(value, dict)}


def detect_language(root):
    """What the repository looks like, by the file that only one kind of project has."""
    if sorted(root.glob("*.sln")) or sorted(root.glob("*.slnx")):
        return "dotnet"

    if (root / "Cargo.toml").is_file():
        return "rust"

    if (root / "CMakeLists.txt").is_file():
        return "cpp"

    return "dotnet"


def starter_config(root):
    solutions = sorted(root.glob("*.sln")) + sorted(root.glob("*.slnx"))
    language = detect_language(root)
    name = solutions[0].stem if solutions else root.name
    prefix = "".join(part[0] for part in _words(name)).upper() or name[:3].upper()
    values = dict(name=name, prefix=prefix, prefix_lower=prefix.lower(), lower=name.lower(),
                  solution=solutions[0].name if solutions else f"{name}.sln")

    others = "".join(
        "\n".join(f"# {line}" if line else "#" for line in LANGUAGES[other].format(**values).splitlines())
        + "\n\n"
        for other in LANGUAGES if other != language)

    return STARTER.format(language=LANGUAGES[language].format(**values),
                          other_languages=others.rstrip("\n") + "\n", **values)


def _words(name):
    """CamelCase split, so MediaLibraryManager suggests MLM rather than MED."""
    word = ""

    for character in name:
        if character.isupper() and word:
            yield word
            word = ""

        word += character

    if word:
        yield word


INSTALLED_RECORD = Path(".claude") / "skills" / ".installed.json"


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def install_skills(root, blocks, force=False):
    """Copy the shared skills in, each with the project's own appendix pasted on the end.

    A skill cannot be a shim - Claude Code reads .claude/skills/*/SKILL.md off disk - so this is a
    copy, and re-running it is how a project picks up a change. What the project adds lives in
    .claude/skill-extras/<name>.md and survives the copy, which is what keeps the shared half
    general without making it vague.

    A reference file is replaced only when it is what this script wrote last time (the digest is
    kept in .claude/skills/.installed.json) or was never changed. One the project edited, or one
    with no record, is kept and returned in the second list: a reference has no extras file, so an
    edit there is the only place the project's own text can live, and overwriting it is silent loss.

    Returns (written, kept).

    Gated on project.toml the same way the scripts are. A skill that does not apply is worse than a
    missing one: `localization` states that every .resx is generated from JSON and must never be
    hand-edited, which in a gettext project is four confident instructions about files that do not
    exist. An unsuitable skill is not read sceptically, it is followed.
    """
    source = SHARED / "skills"

    if not source.is_dir():
        return [], []

    written, kept = [], []
    record_path = root / INSTALLED_RECORD
    record = json.loads(record_path.read_text(encoding="utf-8")) if record_path.is_file() else {}

    for skill in sorted(path for path in source.iterdir() if (path / "SKILL.md").is_file()):
        required = SKILL_BLOCKS.get(skill.name)

        if required is not None and required not in blocks:
            continue

        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        extra = root / ".claude" / "skill-extras" / f"{skill.name}.md"

        if extra.is_file():
            text = text.rstrip("\n") + "\n\n---\n\n" + extra.read_text(encoding="utf-8").lstrip("\n")

        destination = root / ".claude" / "skills" / skill.name / "SKILL.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        written.append(destination)

        # What a skill refers to (references/) travels with it, so a link in SKILL.md is never dead.
        for other in sorted(path for path in skill.rglob("*") if path.is_file() and path.name != "SKILL.md"):
            target = destination.parent / other.relative_to(skill)
            data = other.read_bytes()
            key = target.relative_to(root).as_posix()
            current = target.read_bytes() if target.is_file() else None

            if current is not None and current != data and not force and _digest(current) != record.get(key):
                kept.append(target)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)

            if current != data:
                target.write_bytes(data)
                written.append(target)

            record[key] = _digest(data)

    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return written, kept


def install_skill_map(root, blocks):
    """Write .claude/skill-map for a project that uses the Istarnova building blocks and has none yet.

    The map says which skill covers which files, for the hooks' doorbell (load-the-skill.sh). A project
    that already has one keeps it: the folders it names are its own.
    """
    destination = root / ".claude" / "skill-map"

    if destination.exists() or "istarnova" not in blocks:
        return []

    templates = SHARED / "templates"
    text = (templates / "skill-map.istarnova").read_text(encoding="utf-8")

    if "localization" in blocks:
        text += (templates / "skill-map.localization").read_text(encoding="utf-8")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")

    return [destination]


def project_traits(root, blocks):
    """The project.toml blocks, plus what only the file system says about the project."""
    traits = set(blocks)
    cmake = root / "CMakeLists.txt"

    if cmake.is_file() and "KF6" in cmake.read_text(encoding="utf-8", errors="replace"):
        traits.add("kde")

    if (root / ".claude" / "skill-map").is_file():
        traits.add("skill-map")

    return traits


def install_hooks(root, traits):
    """Enter the hooks that apply into .claude/settings.json, pointing at this checkout.

    Only ever adds. A hook that is already named there - under any matcher, with any timeout - is the
    project's to keep as it is, and so is every other key of the file: permissions, env and the
    project's own hooks are not this script's business. Running it again after project.toml gained a
    block adds what that block enables.
    """
    path = root / ".claude" / "settings.json"
    settings = {}

    if path.is_file():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            sys.exit(f"refused: {path} is not valid JSON ({error}); no hook was entered")

    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        sys.exit(f"refused: {path} does not hold the settings object; no hook was entered")

    groups = settings.setdefault("hooks", {}).setdefault("PreToolUse", [])
    added = False

    for group in groups:
        for hook in group.get("hooks", []):
            for name in HOOKS:
                if hook.get("command") in (old.format(name=name) for old in OLD_HOOK_COMMANDS):
                    hook["command"] = HOOK_COMMAND.format(name=name)
                    added = True

    present = {name for group in groups for hook in group.get("hooks", []) for name in HOOKS
               if f"/hooks/{name}" in hook.get("command", "")}

    for name, (matcher, needs) in HOOKS.items():
        if name in present or (needs is not None and needs not in traits):
            continue

        group = next((entry for entry in groups if entry.get("matcher") == matcher), None)

        if group is None:
            group = {"matcher": matcher, "hooks": []}
            groups.append(group)

        group.setdefault("hooks", []).append(
            {"type": "command", "command": HOOK_COMMAND.format(name=name), "timeout": 10})
        added = True

    if not added:
        return []

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return [path]


def claude_import(name):
    """The import line for one guideline block: `~/...` when the checkout is under the home directory."""
    path = SHARED / "claude" / f"{name}.md"
    home = Path.home()

    return "@" + (f"~/{path.relative_to(home).as_posix()}" if path.is_relative_to(home) else path.as_posix())


def _imported_block(line):
    """The guideline block a line of a CLAUDE.md imports, or None for any other line."""
    text = line.strip()

    if not text.startswith("@") or " " in text:
        return None

    path = Path(text[1:])

    return path.stem if path.suffix == ".md" and path.parent.name == "claude" and path.stem in CLAUDE_BLOCKS else None


def install_claude_md(root, traits):
    """Write a first .claude/CLAUDE.md that imports the guideline blocks this kind of project gets.

    An import and not a copy: Claude Code expands `@path` lines of a CLAUDE.md, so the rules stay in
    this checkout and a change reaches every project, the way the hooks do. What the file holds beyond
    the imports is the project's own and is never touched again.

    A file that already imports blocks gets the lines for the ones enabled since, after the last one it
    has. A file with no such line was written by hand: it is kept as it is, and the missing imports are
    returned for the summary to name, so adding them stays the project's decision.

    Returns (written, kept, missing imports of a kept file).
    """
    wanted = [name for name, needs in CLAUDE_BLOCKS.items()
              if (needs is None or needs in traits) and (SHARED / "claude" / f"{name}.md").is_file()]
    candidates = [root / ".claude" / "CLAUDE.md", root / "CLAUDE.md"]
    destination = next((path for path in candidates if path.is_file()), None)

    if destination is None:
        destination = candidates[0]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(CLAUDE_MD.format(imports="\n".join(claude_import(name) for name in wanted)),
                               encoding="utf-8")
        return [destination], [], []

    lines = destination.read_text(encoding="utf-8").splitlines()
    imported = {index: _imported_block(line) for index, line in enumerate(lines) if _imported_block(line)}
    missing = [claude_import(name) for name in wanted if name not in imported.values()]

    if not imported:
        return [], [destination], missing

    if not missing:
        return [], [], []

    last = max(imported)
    lines[last + 1:last + 1] = missing
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return [destination], [], []


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project", help="The repository to point at this checkout")
    parser.add_argument("--only", help="Comma-separated script names instead of all of them")
    parser.add_argument("--skills", action="store_true",
                        help="Also (re-)write the shared skills into .claude/skills, each with this "
                             "project's .claude/skill-extras/<name>.md appended")
    parser.add_argument("--claude", action="store_true",
                        help="Everything Claude Code reads: the skills (as --skills), the hooks that "
                             "apply in .claude/settings.json, and a first .claude/CLAUDE.md importing "
                             "the guideline blocks for this kind of project")
    parser.add_argument("--submodule", nargs="?", const=SUBMODULE_URL, metavar="URL",
                        help=f"Also add devscripts as a git submodule in {SUBMODULE}/, for contributors and CI "
                             f"that have no shared checkout (default {SUBMODULE_URL})")
    parser.add_argument("--force", action="store_true", help="Overwrite files that are already there")
    arguments = parser.parse_args()

    root = Path(arguments.project).resolve()

    if not root.is_dir():
        sys.exit(f"refused: {root} is not a directory")

    if arguments.submodule and not (root / SUBMODULE).exists():
        added = subprocess.run(["git", "-C", str(root), "submodule", "add", arguments.submodule, SUBMODULE])

        if added.returncode != 0:
            sys.exit(f"refused: git submodule add {arguments.submodule} {SUBMODULE} failed")

    scripts = root / "Scripts"
    scripts.mkdir(exist_ok=True)

    config = scripts / "project.toml"
    new_config = not config.exists()

    # Before anything is chosen: the starter names the language the repository looks like, so the first
    # run already brings that language's script, skills, hooks and guidelines.
    if new_config:
        config.write_text(starter_config(root), encoding="utf-8")

    blocks = configured_blocks(config)
    wanted = [name for name, block in SHAREABLE.items() if block is None or block in blocks]

    if arguments.only:
        asked = [name if name.endswith(".py") else f"{name}.py"
                 for name in arguments.only.split(",")]
        unknown = [name for name in asked if name not in SHAREABLE]

        if unknown:
            sys.exit(f"refused: not shared here: {', '.join(unknown)}")

        wanted = asked

    written, kept = [], []

    for name in wanted:
        destination = scripts / name

        if destination.exists() and not force_allows(destination, arguments.force):
            kept.append(destination)
            destination.chmod(destination.stat().st_mode | 0o111)
            continue

        destination.write_text(SHIM.format(name=name), encoding="utf-8")
        destination.chmod(0o755)
        written.append(destination)

    if new_config:
        written.append(config)
    else:
        kept.append(config)

    changed, handwritten, missing = [], [], []

    if arguments.skills or arguments.claude:
        skills_written, changed = install_skills(root, blocks, arguments.force)
        written += skills_written
        written += install_skill_map(root, blocks)

    if arguments.claude:
        # After the skill map: its presence is what turns the skill doorbell on.
        traits = project_traits(root, blocks)
        written += install_hooks(root, traits)
        guidelines, handwritten, missing = install_claude_md(root, traits)
        written += guidelines

    for path in written:
        print(f"wrote  {path.relative_to(root)}")

    for path in kept:
        print(f"kept   {path.relative_to(root)}  (already there; --force replaces it)")

    for path in changed:
        print(f"kept   {path.relative_to(root)}  (changed in the project since the last install; "
              "--force replaces it, or move the text into the shared skill)")

    for path in handwritten:
        if missing:
            print(f"kept   {path.relative_to(root)}  (written by hand; it does not import the shared "
                  "guidelines - add these lines to use them)")

            for line in missing:
                print(f"         {line}")

    if config in written:
        print(f"\nFill in {config.relative_to(root)} - the commented-out blocks are the optional ones.")

    return 0


def force_allows(destination, force):
    """A shim this script wrote before is always replaceable; a project's own script is not."""
    if force:
        return True

    return "devscripts/install.py" in destination.read_text(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    sys.exit(main())
