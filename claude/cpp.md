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

## Version

`project(NAME VERSION x.y)` with two numbers: the third is the git height, the commits since that line last
changed, as Nerdbank.GitVersioning gives it to the .NET projects. `rpm.py` counts it for the package; the
CMakeLists.txt counts it the same way for the binary, right after `project()`:

```cmake
# The third number is the git height; Scripts/rpm.py counts the same way, so the package and the binary agree.
find_package(Git QUIET)
set(VERSION_HEIGHT 0)
if(GIT_FOUND AND EXISTS "${CMAKE_SOURCE_DIR}/.git")
    execute_process(COMMAND "${GIT_EXECUTABLE}" log -1 --format=%H -G "project\\(.*VERSION" -- CMakeLists.txt
                    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" OUTPUT_VARIABLE VERSION_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(VERSION_COMMIT)
        set(VERSION_RANGE "${VERSION_COMMIT}..HEAD")
    else()
        set(VERSION_RANGE HEAD)
    endif()
    execute_process(COMMAND "${GIT_EXECUTABLE}" rev-list --count ${VERSION_RANGE}
                    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" OUTPUT_VARIABLE VERSION_HEIGHT OUTPUT_STRIP_TRAILING_WHITESPACE)
    # Every commit writes HEAD's reflog, so a build after one configures again and carries the new number.
    execute_process(COMMAND "${GIT_EXECUTABLE}" rev-parse --path-format=absolute --git-path logs/HEAD
                    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}" OUTPUT_VARIABLE HEAD_LOG OUTPUT_STRIP_TRAILING_WHITESPACE)
    if(EXISTS "${HEAD_LOG}")
        set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${HEAD_LOG}")
    endif()
endif()
set(PROJECT_VERSION_PATCH ${VERSION_HEIGHT})
set(PROJECT_VERSION "${PROJECT_VERSION_MAJOR}.${PROJECT_VERSION_MINOR}.${VERSION_HEIGHT}")
set(${PROJECT_NAME}_VERSION "${PROJECT_VERSION}")
```

A new minor or major is a change of that line, which starts the count at 0 again.

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
