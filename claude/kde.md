# Shared guidelines: Qt and KDE (Kirigami)

`~/dev/projects/kde/oversteer-qt` is the project to model a new program on: its CMake layout, its
`main.cpp`, how it registers QML types. Look there before inventing a structure.

## Use what KDE already has

Reach for the KDE or Qt building block before drawing or writing one. `python3 Scripts/cpp.py kde`
lists the KDE libraries on this machine and which of them the project uses.

| For | Use |
| --- | --- |
| Pages, cards, actions, placeholders | Kirigami (`org.kde.kirigami`) |
| The About page | `FormCard.AboutPage` from Kirigami Addons, fed by `KAboutData` |
| Charts | KQuickCharts (`org.kde.quickcharts`), not a hand-drawn `Canvas` or `Shape` |
| Notifications | `KNotification::event` with a `<program>.notifyrc` named after the application |
| Crashes | `KCrash::initialize()`, with a bug address that is not bugs.kde.org |
| Settings | KConfig (`.kcfg`), and `KConfig.WindowStateSaver` for the window |
| Text the user reads | `i18n()`, English as the argument, German in `po/de` |

A QML module that is only loaded at run time still gets its `find_package(... REQUIRED)`: a build that
cannot find it would produce a program whose window fails.

## Standards

- ECM with `KDEInstallDirs`, `KDECMakeSettings`, `KDECompilerSettings`; the C++ standard is set after
  `KDECompilerSettings`, which pins it otherwise. No `KDEClangFormat`: it overwrites `.clang-format`.
- QML types are registered with `QML_ELEMENT` through `qt_add_qml_module`; no context properties. The
  folder of a registered class's header is an include directory.
- **Qt Quick renders through Vulkan**, selected in `main()` before the window loads: only after a
  `QVulkanInstance` probe succeeded, and never when `QSG_RHI_BACKEND` is set.
- **A model never holds the live values.** An array or list that is rebuilt with every update makes a
  `Repeater` or `ListView` destroy and recreate its delegates, which flickers and loses the scroll
  position. The model holds what is constant; the values reach the delegate through bindings, or the
  model is a `QAbstractListModel`.
- The desktop file's `Exec` is the full installed path: a Plasma session started by systemd does not
  have `~/.local/bin` in its `PATH`. The same holds for a helper program the application starts.
- The icon is the program's own SVG under `hicolor/scalable/apps`, named like the desktop file.
- What the program shows as missing is "n/a", never a zero that looks measured.

## Checks

- `python3 Scripts/i18n.py update` after touching an `i18n()` text, `… status` for what is untranslated.
- A window is checked by looking at it. An off-screen run (`QT_QPA_PLATFORM=offscreen`) proves that the
  QML loads, not that it looks right, and shader-based items such as KQuickCharts stay empty there.
- What was only compiled, such as a notification nobody triggered, is reported as not tested.
