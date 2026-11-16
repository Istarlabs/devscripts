# Shared guidelines: Blazor with the Istarnova building blocks

The EF Core foundation, the circuit state, the localized grids, the navigation and the hosting pieces
live in the Istarnova repository (`Istarnova.EfCore`, `Istarnova.Blazor`, `Istarnova.Blazor.FluentUI`,
`Istarnova.AspNetCore`), not in this project.

- **Look in Istarnova before writing a helper, a grid, a dialog or a base class.** The `istarnova`
  skill maps what is there; `python3 Scripts/graph.py uses Istarnova` lists what this project already
  uses, and `--in Istarnova` asks about that repository.
- **No raw `<FluentDataGrid>`.** It draws its menus, its empty message and its paginator in English;
  the localized grid from `Istarnova.Blazor.FluentUI` is the one to use. `prefer-localized-grid.sh`
  refuses the raw one, and `dotnet.py verify` counts them.
- Markup in `.razor`, code-behind in `.razor.cs`, component styling in `.razor.css`.
- The `blazor-ui` and `data-layer` skills hold the patterns; read the one for the area before the
  first edit there. What is specific to this project (its schemas, how it opens dialogs, whether
  migrations are committed) is in `.claude/skill-extras/`, never in the shared skill.
- New, changed or removed text for the user: `python3 Scripts/translations.py add|update|remove {Area}`,
  never the JSON, the Designer file and the `.resx` by hand.
