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
- **What a file says about itself is a claim, not a fact.** A size, a count or an offset in a header is computed with
  in 64 bits (a product of two 32-bit numbers wraps), checked against what was actually read, and bounded before
  anything is allocated for it. Inflate with a buffer of the size you decided on, not with `qUncompress`, which grows
  for as long as the stream goes on. A damaged file ends in an error: nothing it holds may throw out of a reader, index
  past a buffer, or overflow a signed number. Rust's compiler rules most of that out; here nothing does.
- **A reader of files from outside gets two things**: tests that feed it hostile input (a count that wraps, a size
  that lies, a file cut short), and a fuzz target (`cpp.py fuzz`). The tests under the sanitizers (`cpp.py sanitize`,
  `[cpp] sanitize = true` for `verify` to run it) are what keeps a fix fixed; the fuzzer is what finds the next one.
- A header declares what its users need and nothing else; a file that grows a second responsibility is
  split.

## Scripts

| Task | Command |
| --- | --- |
| Build, tests, format, licence lines | `python3 Scripts/cpp.py verify [--quick]` |
| Configure and build | `python3 Scripts/cpp.py build [--debug] [--clean]` |
| Warnings by flag, or every hit of one flag | `python3 Scripts/cpp.py warnings [--flag=-Wshadow]` |
| Tests, with the failing output | `python3 Scripts/cpp.py test [NAME] [--details]` |
| Tests under ASan, UBSan, LeakSanitizer | `python3 Scripts/cpp.py sanitize [NAME]` |
| Fuzz a reader with libFuzzer | `python3 Scripts/cpp.py fuzz [TARGET] [--seconds N] [--reproduce FILE]` |
| Format | `python3 Scripts/cpp.py format [--check]` |
| Run the built binary | `python3 Scripts/cpp.py run -- ARGS` |
| Install into the configured prefix | `python3 Scripts/cpp.py install` |
| Compiler, CMake, what was found | `python3 Scripts/cpp.py toolchain` |
