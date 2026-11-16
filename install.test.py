#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Pascal Klose
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks that install_skills never replaces a reference file the project changed.

A reference has no extras file, so an edit in the project's copy is the only place its own text can
live. Overwriting it was silent, and it was merged before anyone saw the loss.

And that --claude only ever adds: a hook or an import line where there was none, never a change to
what a project's settings.json or CLAUDE.md already says.

    python3 install.test.py
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import install

FAILURES = []


def check(name, actual, expected):
    if actual == expected:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}\n       expected {expected!r}\n       got      {actual!r}")
        FAILURES.append(name)


def hook_names(project):
    """The hook scripts settings.json names, per matcher."""
    settings = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))

    return {group["matcher"]: [hook["command"].rsplit("/", 1)[-1].rstrip('"') for hook in group["hooks"]]
            for group in settings["hooks"]["PreToolUse"]}


def check_hooks(base):
    print("install_hooks:")
    project = base / "hooked"
    settings = project / ".claude" / "settings.json"

    written = install.install_hooks(project, {"rust"})
    check("a project without settings gets the file", written, [settings])
    check("the three hooks of every project, by matcher", hook_names(project), {
        "Bash": ["no-cd-into-repo-root.sh", "prefer-repo-scripts.sh"],
        "Edit|Write|MultiEdit": ["no-decorative-comments.sh"],
    })
    check("the command is the shared template",
          json.loads(settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
          install.HOOK_COMMAND.format(name="no-cd-into-repo-root.sh"))
    check("a second run writes nothing", install.install_hooks(project, {"rust"}), [])

    install.install_hooks(project, {"dotnet", "istarnova", "skill-map"})
    check("blocks enabled later add their hooks", hook_names(project)["Edit|Write|MultiEdit"],
          ["no-decorative-comments.sh", "load-the-skill.sh", "no-direct-logger.sh", "prefer-localized-grid.sh"])

    own = base / "own"
    (own / ".claude").mkdir(parents=True)
    mine = {"type": "command", "command": "bash .claude/hooks/prefer-repo-scripts.sh", "timeout": 3}
    (own / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(python3:*)"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash|Edit", "hooks": [mine]}], "Stop": [{"hooks": []}]},
    }), encoding="utf-8")
    install.install_hooks(own, set())
    after = json.loads((own / ".claude" / "settings.json").read_text(encoding="utf-8"))
    check("the other keys survive", after["permissions"], {"allow": ["Bash(python3:*)"]})
    check("another hook event survives", after["hooks"]["Stop"], [{"hooks": []}])
    check("a hook the project already names is neither changed nor entered twice",
          [hook for group in after["hooks"]["PreToolUse"] for hook in group["hooks"]
           if "prefer-repo-scripts.sh" in hook["command"]], [mine])
    check("the missing ones are added beside it", hook_names(own), {
        "Bash|Edit": ["prefer-repo-scripts.sh"],
        "Bash": ["no-cd-into-repo-root.sh"],
        "Edit|Write|MultiEdit": ["no-decorative-comments.sh"],
    })

    older = base / "older"
    (older / ".claude").mkdir(parents=True)
    old = {"type": "command", "command": install.OLD_HOOK_COMMANDS[0].format(name="prefer-repo-scripts.sh"), "timeout": 10}
    (older / ".claude" / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [old]}]}}), encoding="utf-8")
    install.install_hooks(older, set())
    commands = [hook["command"] for group in json.loads((older / ".claude" / "settings.json").read_text(
        encoding="utf-8"))["hooks"]["PreToolUse"] for hook in group["hooks"]]
    check("a command an earlier run wrote is brought up to date, once",
          commands.count(install.HOOK_COMMAND.format(name="prefer-repo-scripts.sh")), 1)
    check("and the old spelling is gone", old["command"] in commands, False)


def check_resolution(base):
    """Shims and hooks: DEVSCRIPTS, then ~/dev/tools/devscripts, then the project's .devscripts submodule."""
    print("resolution:")

    def checkout(path, label):
        (path / "shared" / "repo").mkdir(parents=True)
        (path / "shared" / "repo" / "hello.py").write_text(f"print({label!r})\n", encoding="utf-8")
        (path / "hooks").mkdir()
        (path / "hooks" / "hello.sh").write_text(f"echo {label}\n", encoding="utf-8")

    project = base / "resolving"
    home = base / "home"
    checkout(project / install.SUBMODULE, "submodule")
    (project / "Scripts").mkdir()
    (project / "Scripts" / "hello.py").write_text(install.SHIM.format(name="hello.py"), encoding="utf-8")
    checkout(home / "dev" / "tools" / "devscripts", "home")
    checkout(base / "elsewhere", "variable")

    def run(environment):
        env = {"PATH": os.environ["PATH"], "HOME": str(environment.get("HOME", base / "nohome")),
               "CLAUDE_PROJECT_DIR": str(project)}
        env.update({key: str(value) for key, value in environment.items()})
        shim = subprocess.run([sys.executable, str(project / "Scripts" / "hello.py")], env=env,
                              capture_output=True, text=True).stdout.strip()
        hook = subprocess.run(["bash", "-c", install.HOOK_COMMAND.format(name="hello.sh")], env=env,
                              capture_output=True, text=True).stdout.strip()
        return shim, hook

    check("DEVSCRIPTS wins", run({"DEVSCRIPTS": base / "elsewhere", "HOME": home}), ("variable", "variable"))
    check("then the shared checkout in the home directory", run({"HOME": home}), ("home", "home"))
    check("then the submodule", run({}), ("submodule", "submodule"))


def check_claude_md(base, shared):
    print("install_claude_md:")
    blocks = shared / "claude"
    blocks.mkdir(parents=True)

    for name in ("base", "rust", "cpp", "kde", "dotnet"):
        (blocks / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")

    def line(name):
        return f"@{(blocks / name).as_posix()}.md"

    project = base / "guided"
    project.mkdir()
    (project / "CMakeLists.txt").write_text("find_package(KF6 6.8 REQUIRED COMPONENTS Kirigami)\n", encoding="utf-8")
    traits = install.project_traits(project, {"cpp"})
    check("a CMake project asking for KDE Frameworks is a KDE project", traits, {"cpp", "kde"})

    target = project / ".claude" / "CLAUDE.md"
    written, kept, missing = install.install_claude_md(project, traits)
    imports = [text for text in target.read_text(encoding="utf-8").splitlines() if text.startswith("@")]
    check("the first run writes the file", (written, kept, missing), ([target], [], []))
    check("it imports the blocks of this kind of project, in order", imports, [line("base"), line("cpp"), line("kde")])
    check("a block with no file in the checkout is not imported",
          install.install_claude_md(base / "web-only", {"web"})[0][0].read_text(encoding="utf-8").count("@"), 1)

    target.write_text(target.read_text(encoding="utf-8") + "\nThe project's own rule.\n", encoding="utf-8")
    check("a second run writes nothing", install.install_claude_md(project, traits), ([], [], []))

    written, kept, missing = install.install_claude_md(project, traits | {"rust"})
    text = target.read_text(encoding="utf-8")
    imports = [entry for entry in text.splitlines() if entry.startswith("@")]
    check("a block enabled later is added after the imports that are there",
          imports, [line("base"), line("cpp"), line("kde"), line("rust")])
    check("the project's own text survives", text.endswith("The project's own rule.\n"), True)

    manual = base / "manual"
    manual.mkdir()
    (manual / "CLAUDE.md").write_text("# Mine\n\nSee @README for the rest.\n", encoding="utf-8")
    written, kept, missing = install.install_claude_md(manual, {"rust"})
    check("a hand-written file is kept", (written, kept), ([], [manual / "CLAUDE.md"]))
    check("and left exactly as it was", (manual / "CLAUDE.md").read_text(encoding="utf-8"),
          "# Mine\n\nSee @README for the rest.\n")
    check("its missing imports are named", missing, [line("base"), line("rust")])
    check("no second file appears beside it", (manual / ".claude" / "CLAUDE.md").exists(), False)


def main():
    with tempfile.TemporaryDirectory() as folder:
        base = pathlib.Path(folder)
        shared = base / "shared"
        project = base / "project"
        reference = shared / "skills" / "demo" / "references" / "dialogs.md"
        copy = project / ".claude" / "skills" / "demo" / "references" / "dialogs.md"

        reference.parent.mkdir(parents=True)
        (shared / "skills" / "demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
        reference.write_text("shared v1\n", encoding="utf-8")
        install.SHARED = shared

        print("install_skills:")

        written, kept = install.install_skills(project, set())
        check("first install writes the reference", copy.read_text(encoding="utf-8"), "shared v1\n")
        check("first install keeps nothing", kept, [])

        reference.write_text("shared v2\n", encoding="utf-8")
        written, kept = install.install_skills(project, set())
        check("an untouched copy follows the shared file", copy.read_text(encoding="utf-8"), "shared v2\n")
        check("an untouched copy is not reported as kept", kept, [])

        copy.write_text("shared v2\nproject text\n", encoding="utf-8")
        reference.write_text("shared v3\n", encoding="utf-8")
        written, kept = install.install_skills(project, set())
        check("an edited copy survives", copy.read_text(encoding="utf-8"), "shared v2\nproject text\n")
        check("an edited copy is named", kept, [copy])

        written, kept = install.install_skills(project, set(), force=True)
        check("--force replaces it", copy.read_text(encoding="utf-8"), "shared v3\n")

        (project / install.INSTALLED_RECORD).unlink()
        copy.write_text("older, unrecorded\n", encoding="utf-8")
        written, kept = install.install_skills(project, set())
        check("a differing copy with no record is kept", copy.read_text(encoding="utf-8"), "older, unrecorded\n")
        check("a differing copy with no record is named", kept, [copy])

        check_hooks(base)
        check_resolution(base)
        check_claude_md(base, shared)

    if FAILURES:
        print(f"\n{len(FAILURES)} failed")
        return 1

    print("\nall passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
