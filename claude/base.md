# Shared guidelines: every project

Imported by a project's `.claude/CLAUDE.md` from the shared devscripts checkout. Change a rule here,
once; what holds in one project only belongs in that project's own section.

## Language

Everything is written in English: identifiers, comments, log messages, CLI text, commit messages,
and whatever goes to GitHub. What the user reads in the interface is the exception: it is translated,
English first and German second, through the project's localization (never a hard-coded German string).

## Comments

The default is no comment, and the budget is small. Two things earn one:

1. **Knowledge from outside the repository**: what a foreign tool, driver or protocol actually does.
   None of that can be derived from the code.
2. **Meaning and choice where the code cannot say it**: why this threshold, bound or tolerance. A bare
   number reads as a guess; with its source it reads as a decision.

What earns nothing: restating the line below, narrating the next step, defending a decision nobody
challenged, history git already has. `no-decorative-comments.sh` refuses banner comments; a section
that needs a banner needs a module or a file of its own.

## Repository scripts: use them before writing a command chain

`Scripts/` holds the recurring chores as shims; the real scripts live in the shared checkout
(`~/dev/tools/devscripts`, or wherever `DEVSCRIPTS` points), so a fix reaches every project at once.
What a project differs by is `Scripts/project.toml`: change a value there, never in the shared script.
A script that refuses and names a missing key is working correctly.

| Task | Command |
| --- | --- |
| Where a symbol is used, and whether at all | `python3 Scripts/find.py Symbol` |
| Where a piece of text appears, in any file type | `python3 Scripts/find.py "some text" --text` |
| How the parts hang together | `python3 Scripts/graph.py status\|build\|around Name\|path "A" "B"` |
| Branch, commit, push, issues, labels, PRs, merges | `python3 Scripts/repo.py --help` |
| SPDX licence headers | `python3 Scripts/spdx.py check\|apply` |

- **`find.py` before writing anything new.** Search for the concept and its synonyms, not only the
  name you would pick; rebuilding what exists three folders over is the most common mistake.
- **Never hand-build a `git` or `gh` line.** `repo.py` carries the guards: no branching off a dirty
  tree, no commit to the main branch without `--allow-master`, no merge GitHub does not report as
  mergeable.
- **Write body files with the Write tool, never a heredoc.** `repo.py` takes commit messages, issue
  and PR bodies through `--file`. A heredoc makes the command line unique, so no allow rule can match it.
- **A missing action on an existing script? Add it** in the shared checkout, with its `*.test.py`. A
  brand-new script for a new kind of chore deserves a heads-up first.

The hooks live in the shared checkout too; `.claude/settings.json` points at them. Edit them there and
run the `.test.sh` beside the one you touched.

## This machine

- The shell aliases `ls` to `eza` and `cat` to `bat`: write `\ls` and `\cat` whenever the output is
  parsed or piped.
- `gh` has no `--no-pager`, and `git` takes it only before the subcommand. `GH_PAGER=cat` and
  `GIT_PAGER=cat` work everywhere; `repo.py` sets both.
- The tools' output is localized (German): never `grep` a human-readable build or git message, use the
  scripts, which read the machine-readable forms.
- Do not prefix commands with `cd`. The repository is the working directory and it stays that between
  calls; a hook refuses it.

## Workflow

Issue, branch, commit, pull request, through `repo.py`; the `feature-workflow` skill has the steps.
Before writing a line, say in two or three lines whether the thing already exists, which existing
feature is the nearest pattern, and what the smallest version is.

For a genuinely small, self-contained change, ask whether it goes straight to the main branch rather
than assuming so. Branch names `feat/…`, `fix/…`, `chore/…`; conventional commit subjects; the commit
body says why, not what the diff already shows.

Report what was verified and what was not. A change that only compiled was not tested.
