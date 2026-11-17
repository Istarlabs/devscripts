# Shared guidelines: C++ (CMake)

## Standards

- `python3 Scripts/cpp.py verify` is the gate: build without warnings, `ctest`, `clang-format`, licence
  lines. It runs before every commit.
- **The repository's own `.clang-format` decides the layout**, and `cpp.py format` applies it. Braces on
  every `if`, `else` and loop.
- The program without `main()` is a library target, so the tests link the same code as the binary.
- Non-trivial logic leaves a test behind (`add_test`), or at least a `static_assert` beside a
  `constexpr` function.
- Anything from outside (a file, a device, a child process, a network packet) is checked before it is
  used; a missing value is unknown, not zero.
- A header declares what its users need and nothing else; a file that grows a second responsibility is
  split.

## Scripts

| Task | Command |
| --- | --- |
| Build, tests, format, licence lines | `python3 Scripts/cpp.py verify [--quick]` |
| Configure and build | `python3 Scripts/cpp.py build [--debug] [--clean]` |
| Warnings by flag, or every hit of one flag | `python3 Scripts/cpp.py warnings [--flag=-Wshadow]` |
| Tests, with the failing output | `python3 Scripts/cpp.py test [NAME] [--details]` |
| Format | `python3 Scripts/cpp.py format [--check]` |
| Run the built binary | `python3 Scripts/cpp.py run -- ARGS` |
| Install into the configured prefix | `python3 Scripts/cpp.py install` |
| Compiler, CMake, what was found | `python3 Scripts/cpp.py toolchain` |
