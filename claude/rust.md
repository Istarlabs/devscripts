# Shared guidelines: Rust

## Standards

- Edition 2024, `rustfmt` defaults, `clippy` clean. `python3 Scripts/rust.py verify` is the gate, and it
  runs before every commit; it separates the warnings on the lines this branch changed from the ones
  that were already there.
- **Logic lives in a library crate, front ends stay thin.** A workspace with a core crate and one
  binary crate per front end (a CLI, a TUI) is the shape to start from; what two front ends need goes
  into the core, what only one needs stays there. `gputaskman-rs` (core + ratatui TUI) and
  `dolphin-video-converter` (one crate per converter, `media-core` shared) are the examples.
- **Parsers and decision logic are pure functions with unit tests.** None of them may need a file, a
  device or a subprocess to be tested. A threshold changed without a test that fails first is a guess.
- **Anything from outside is an external interface, not a detail**: a child process, a file, `/proc`,
  a driver, a network reply. Never `unwrap()` on what came out of one; a missing or unreadable value is
  reported as unknown, not as zero.
- `anyhow` (or a plain message) for error context at the top level, `Result` below it. The context says
  what was being done to which thing, because that string is what the user reads.
- A file that grows a second responsibility gets split, not a banner comment.
- A dependency is added when it replaces real work, not for what a few lines do. Look up the crate's
  current API in its source or documentation before using it; do not write it from memory.

## Scripts

| Task | Command |
| --- | --- |
| Clippy, tests, rustfmt, licence lines | `python3 Scripts/rust.py verify` |
| Warnings by lint, or every hit of one lint | `python3 Scripts/rust.py warnings [--code clippy::x]` |
| Tests, with the failed assertion | `python3 Scripts/rust.py test [--filter NAME] [--details]` |
| Format | `python3 Scripts/rust.py fmt [--check]` |
| Dependencies, advisories, updates, one crate | `python3 Scripts/rust.py packages [--vulnerable\|--outdated\|--info NAME]` |
| Workspace members, toolchain | `python3 Scripts/rust.py crates`, `… toolchain` |
| Run it | `python3 Scripts/rust.py run -- ARGS` |
| Install the binary | `python3 Scripts/rust.py install [-p CRATE]` |
| A symbol in the sources of the crates this project resolves | `python3 Scripts/find.py Symbol --crates` |
