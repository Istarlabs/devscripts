---
name: localization
description: >
  Guide for adding or changing localized text in this repository. Use when adding a user-facing string,
  creating/editing a .resx resource file, translating UI text, adding a new language entry, or localizing an
  enum's display names, or removing one that is no longer used. ALL languages are generator-driven:
  edit Scripts/translations/{Area}.json through Scripts/translations.py (add/update/remove — writes
  JSON + Designer + resx in one step) — never hand-edit any .resx. Also covers the
  resource file layout, the per-resource ItemGroup structure of the csproj,
  the .Designer.cs accessor, and the enum localization pattern
  (NetEscapades [EnumExtensions] + Istarnova [EnumExtras] + [LocalizedDescription], consumed via {Enum}Extensions helpers).
---

# Localization (Resources & Enum Display Names)

All user-facing text MUST come from resource files — **never hardcode** UI strings (code/comments stay English).

## Languages & files
- Which languages a project carries is `localization.satellite_languages` in `Scripts/project.toml`,
  beside `base_language` (`en`) and the `resources_project` they live in. That block is the single
  place the list is written down; `gen_resx.py` and `translations.py` both read it.
- **Every `.resx` file is generated** — base and satellites alike. The single source of truth is
  `Scripts/translations/{Area}.json`. **Never hand-edit any `.resx` file**; edit the JSON and regenerate.
- Naming: `{Area}Resources.resx` (base) + `{Area}Resources.{culture}.resx` per language.
- `python3 Scripts/translations.py areas` lists them with their key counts.
- Pick the most specific existing `{Area}` that fits; only create a new area file when none applies.

## JSON source format (`Scripts/translations/{Area}.json`)
An **ordered** map — order is mirrored into the generated `.resx`:
```json
{
  "// DataGrid Pagination": "",
  "PaginationText": {
    "en": "Page {0} of {1}",
    "de": "Seite {0} von {1}",
    "comment": "Pagination text template. {0} = current page, {1} = total pages"
  }
}
```
- Every key needs the **base language**; the generator fails hard without it. A satellite language may
  lack a key: the generator leaves it out of that `.resx` and the base text is shown at runtime.
  Fill in the languages you can; `translations.py status` shows what is still missing.
- Put **raw** values (`&`, `<`); the generator XML-escapes them. A translation must use the same
  `{0}`/`{1}` placeholders as the base text — `translations.py` refuses one that does not.
- `"comment"` is optional — a translator note, emitted as `<comment>` in every generated file. On a
  string with placeholders it is **not** optional in practice: `{0}` says nothing about what it holds,
  and a translator reordering a sentence has to know. `translations.py` takes it like a language and
  keeps an existing one when an update does not restate it, so a later translation fix cannot strip
  the explanation.
- **Placeholders are `{0}`, `{1}`, `{2}` — never named ones like `{count}`.** They are what
  `string.Format` reads; a named placeholder can only be substituted by hand, which is how strings
  end up carrying a chain of `.Replace(...)` at every call site.
- Keys starting with `"//"` are section markers, emitted as `<!-- ... -->` XML comments (value stays `""`).
- Keys are PascalCase and self-describing (e.g. `AccessLevelDownload`, not `Lbl1`).

## Adding, changing, removing a string — one script, three verbs

```bash
python3 Scripts/translations.py add    {Area} --from new-keys.json
python3 Scripts/translations.py update {Area} --from changed-keys.json
python3 Scripts/translations.py remove {Area} OldKey AnotherOldKey
python3 Scripts/translations.py areas
```

The payload is `{ "KeyName": { "en": "...", "de": "...", ... } }`. Every verb keeps all three places in
step — the JSON source, the `Designer.cs` accessors that `gen_resx.py` does **not** touch, and the
regenerated `.resx` files — by calling `gen_resx.py` itself at the end. Write the payload to a file
(the scratchpad is fine) rather than piping a heredoc: a fixed command can be allowlisted, arbitrary
inline code cannot, and that is what turns this into a permission prompt every single time.

Each verb refuses the wrong situation instead of doing something surprising:

- `add` will not overwrite an existing key — a typo in a key name must not replace a different string.
- `update` will not create a missing one — the same typo, from the other side.
- `remove` will not drop a key the source still references, and names the files that use it. `--force`
  when the callers really are gone.
- Unknown languages and unknown areas are rejected with the valid list.
- `update` changes only the languages it is given and keeps the rest.

Then commit the JSON, `translated-from/`, **and** all regenerated `.resx`.

### Missing and outdated translations

`Scripts/translations/translated-from/{lang}.json` records, per key, a short hash of the base text a
translation was made from. `add` and `update` write it for the languages they are given. So:

- **Changing a base text, give only the languages you actually retranslated.** Passing the old
  translation along again marks it current and hides that it no longer matches.
- `python3 Scripts/translations.py status [lang] [--list]` counts missing and outdated translations per
  language and fails on invalid entries (placeholders, unknown languages, missing base text).
- `python3 Scripts/translations.py mark-current [lang]` records the current base text for translations
  without a record — how a project starts tracking. `--overwrite` also clears outdated ones, only after
  someone checked them.

### Doing it by hand
Only for what the script deliberately leaves alone — section markers, or a rename you want to review
key by key:
1. Edit `Scripts/translations/{Area}.json`.
2. Run `python3 Scripts/gen_resx.py`.
3. **New/renamed/removed keys only**: update `{Area}Resources.Designer.cs` — otherwise the strongly-typed
   accessor won't compile. Value-only changes don't need it.
4. Commit the JSON **and** all regenerated `.resx`.
- A new area = a new `{Area}.json` (filename stem = area name) **plus** a minimal base `{Area}.resx` to clone the
  header from (copy an existing base file, delete its `<data>` entries) — the script needs it on first run.
  `translations.py new-area {Area} [--folder Name]` does all of that, csproj group included. A project may
  group its areas into subfolders of the resources project; the scripts find an area by its base `.resx`.
  The compiler names an embedded resource after its folder, so the `Designer.cs` base name carries it
  (`{Namespace}.{Folder}.{Area}`) while the class stays in the plain namespace.
- After any generator change, regenerate and confirm `python3 Scripts/repo.py diff` on the `.resx` is empty (output is byte-stable).

## csproj structure
One `<ItemGroup Label="{Area}Resources">` **per resource**, containing in this order:
1. `<EmbeddedResource Update="{Area}Resources.resx">` with `<Generator>PublicResXFileCodeGenerator</Generator>`
   and `<LastGenOutput>{Area}Resources.Designer.cs</LastGenOutput>`;
2. `<Compile Update="{Area}Resources.Designer.cs">` with `DesignTime`/`AutoGen`/`DependentUpon`;
3. one `<EmbeddedResource Update="{Area}Resources.{lang}.resx">` with `<DependentUpon>` per satellite,
   in the order `satellite_languages` lists them.
Keep new languages/areas inside the matching labeled group — no loose entries in shared ItemGroups.

## Using a string
- In components/code: `{Area}Resources.{Key}` (strongly typed).
- Prefer the typed accessor over `ResourceManager.GetString(...)`.

### A string with values in it

`string.Format(CultureInfo.CurrentCulture, Res.Key, first, second)` — never a chain of `.Replace(...)`.
One allocation instead of one per placeholder, and it is what the `{0}` in the resource is for.

**In Razor, build it in the code-behind** and bind the property. Two ways this goes wrong in markup,
both of which compile and are visible only by looking at the page:

```razor
@* the expression ends at the line break, and the rest is printed as text *@
@Res.PacksOverPeriod
    .Replace("{count}", count.ToString())

@* the inner quote closes the attribute where the string was meant to start *@
<FluentAccordionItem Heading="@string.Join(", ", names)">
```

Both belong in the `.razor.cs` as a named property, which also gives the string a place to be explained.

## Enum localization pattern
Enums carry two generator attributes plus one per member:
```csharp
[EnumExtensions]                                  // NetEscapades.EnumGenerators — fast ToString/TryParse
[EnumExtras]                                      // Istarnova source generator — display helpers from [LocalizedDescription]
public enum OrderStatus
{
    [LocalizedDescription(nameof(OrderStatusResources.Scheduled), typeof(OrderStatusResources))]
    Scheduled = 0,
    [LocalizedDescription(nameof(OrderStatusResources.Confirmed), typeof(OrderStatusResources))]
    Confirmed = 1,
}
```
- `[LocalizedDescription(nameof(Res.Key), typeof(Res))]` ties each member to a resource key; pass the
  key via `nameof(...)` so renames are caught at compile time.
- Always give explicit numeric values (used as DB/contract values).

### Using a localized enum — the EnumExtras generated helpers
Both generators emit partial members into a single `{Enum}Extensions` class. Call it directly — **no `EnumEx<T>`,
no passing `ResourceManager` around**:
- `{Enum}Extensions.GetDisplayList()` — localized `{value, display}` list for select boxes.
- `{Enum}Extensions.GetDisplayName(value)` — localized display string for one member.
- `{Enum}Extensions.TryParse(text, out value, ignoreCase: true)` — parse back from a string.

`EnumExtras` reads the `[LocalizedDescription]` metadata at compile time and resolves the current culture at
runtime, so the resource lookup is automatic. The reflection-based `EnumEx<T>` is **legacy** — keep it
working where it is still used, but don't write new code against it.

## Enum VALUES vs. DISPLAY
- The enum *identifier/number* is invariant (stored, sent over the wire) — never localize it.
- Only the **display name** is localized via the resource key.

## Checklist
0. Placeholders? `{0}`/`{1}` plus a `"comment"` saying what each one holds, consumed via `string.Format`.
1. Key with the base language, plus every language you can, added to `Scripts/translations/{Area}.json`.
2. `python3 Scripts/gen_resx.py` run; base + satellite `.resx` regenerated and committed with the JSON.
3. New key? `.Designer.cs` regenerated / in sync.
4. New area/language file? csproj entry inside the resource's labeled ItemGroup.
5. Referenced via the typed accessor, or via `[LocalizedDescription]` for enums.
