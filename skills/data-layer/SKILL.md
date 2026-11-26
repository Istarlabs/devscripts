---
name: data-layer
description: >
  Guide for the persistence layer of a project that uses the Istarnova.EfCore building blocks: entities, EF Core configurations,
  the DbContext, repositories (interface plus implementation), dependency-injection registration and migrations. Use when creating
  an entity, adding a table or a column, writing a repository, registering one in DI, wiring an EF Core configuration, storing an
  enum or JSON, adding a search over a list, or writing a database function or view. Covers the rules that never change (own key
  name, enums as text, jsonb, schemas, audit fields, tracked updates) and the pieces Istarnova already provides.
---

# Data layer (entities, EF Core, repositories, DI)

The base classes, the configuration helpers, the audit stamping and the repository contracts are **`Istarnova.EfCore`**
(the NuGet package; source in `Istarnova.EfCore/` of github.com/Istarlabs/Istarnova). Do not write a second `BaseEntity`, a second `BaseEntityConfiguration`, a second repository
interface or a second `RepositoryLogger`. What a project adds - which schemas it has, how it names a lookup, how it handles migrations - is in the
appendix below.

## 0. Conventions that always apply

- All identifiers and comments in **English**; one class per file; the file name is the class name; `#region` blocks and the standard file header.
- **Own key name:** `{Entity}Id`, never a bare `Id` (`GameAccount.GameAccountId`, `Banner.BannerId`). A foreign key is the other entity's key name
  (`BannerTypeId`) plus the navigation (`BannerType`). The property is `int` or `long` per table.
- Dates are **UTC**. Audit fields live on `BaseEntity` (`CreatedOn`, `CreatedBy`, `ModifiedOn`, `ModifiedBy`, `RowVersion`) - never re-declare them.
- A `string` that is not free text says what it is: `[StringSyntax(StringSyntaxAttribute.X)]` - `Json`, `Uri`, `GuidFormat`, `DateTimeFormat`.
- Collections are initialised to `[]`; navigations to a required parent are `= null!`.
- Use the **async** EF methods for reads; `Add`/`Update`/`Remove` need no `Async`.

## 1. Entity - `{Project}.Database.Entities/{Domain}/`

```csharp
public class Banner : BaseEntity           // Istarnova.EfCore.Entities.BaseEntity
{
    public int BannerId { get; set; }
    public BannerType BannerType { get; set; } = null!;
    public int BannerTypeId { get; set; }
    public ICollection<BannerFeaturedItem> FeaturedItems { get; set; } = [];
    public string Name { get; set; } = string.Empty;
}
```

Add the inverse `ICollection<>` navigation on the parent when there is a relationship. Properties are listed alphabetically.

## 2. Configuration - `{Project}.Database.Sql.Configurations/{Domain}/`

```csharp
public class BannerConfiguration : BaseEntityConfiguration<Banner>     // Istarnova.EfCore.Configurations
{
    protected override void Configure(EntityTypeBuilder<Banner> builder)
    {
        builder.ToTable("Banners", SchemaNames.MasterData);
        builder.HasKey(banner => banner.BannerId);
        builder.Property(banner => banner.Name).HasMaxLength(256).IsRequired();
        builder.HasOne(banner => banner.BannerType).WithMany().HasForeignKey(banner => banner.BannerTypeId);
        builder.HasIndex(banner => new { banner.BannerTypeId, banner.ExternalId }).IsUnique();
    }
}
```

The base class maps `CreatedBy`/`ModifiedBy`, gives the two timestamps a `now()` database default (EF still sends its value), indexes `CreatedOn` as
`IX_{Entity}_CreatedOn`, and ignores `RowVersion`. Overrides on the base:

- `DetectsConcurrentEdits => true` - a save is refused when the row changed since it was read (PostgreSQL's `xmin`). **Opt-in**: a table read through a `RETURNS TABLE`
  function has no `xmin`. Copy an edit onto the tracked row with `context.Entry(existing).ApplyEdit(edited)` and hand the new `RowVersion` back after the save.
- `QueryFilter` - a filter on every query of the entity.
- `CreatedOnIndexName` - for a table created by hand before its configuration existed.
- **Seed rows** go through `SeedableBaseEntityConfiguration<T>`: override `GetSeedData()`; use the inherited `SeedDate` for the audit fields (a changing value makes EF see a
  model change on every build). A project that already has seeded rows keeps its date by overriding `SeedDate`. Never `HasData` in `Configure`.

`builder.ToTable(name, schema)` always names the schema - a table without it lands in `public`. The schema names are constants in one class.

The property helpers below (`HasStringToEnumConversion()`, `HasDecimalColumn()`, `HasJsonColumn()`) are extension methods in
**`Istarnova.EfCore.Extensions`**, not in `.Configurations`: a configurations project needs `global using Istarnova.EfCore.Extensions;`
beside `global using Istarnova.EfCore.Configurations;`, or every call fails with `CS1061`.

**Iron rule - enums are stored as strings, never as ints:** `builder.Property(e => e.Status).HasStringToEnumConversion().HasMaxLength(20)`.
Why it is not negotiable: the data leaves the system (a data request, a CSV, a dump), and `Type = 1` is meaningless to whoever receives it; queries read
`WHERE "Status" = 'Done'` without the source; and renumbering an enum can never silently reinterpret stored rows. If the number carries real meaning (an
ordering, an external code), that belongs in its own column. Converting an existing int column later needs a **hand-written** migration - the scaffolded
`AlterColumn` casts to the numeric text and corrupts rows.

**Decimals** go through `HasDecimalColumn()` (18,4 by default). **JSON** is a `jsonb` column (`HasJsonColumn()`), the property stays `string?`, marked
`[StringSyntax(StringSyntaxAttribute.Json)]`; as `text` the database cannot validate, index or query it. What goes **in** has to read without the application:
self-describing keys, plain values, no type codes, nothing a query filters on (that earns a column). `jsonb` returns the text in its own layout, so compare two
documents by content (`JsonNode.DeepEquals`), not as strings.

## 3. The context - `{Project}.Database.EfCore/ApplicationDbContext.cs`

- `DbSet<{Entity}>` per entity, in the schema's region.
- `OnModelCreating`: `modelBuilder.ApplyConfigurationsFromAssembly(typeof(SchemaNames).Assembly)` (or the per-schema `Configure{Schema}Tables` extension the project
  uses - the appendix says which).
- Stamp on save, in both `SaveChanges` and `SaveChangesAsync`: `AuditStamper.Apply(ChangeTracker, CurrentUserId)` before `base`. `CurrentUserId` is set by whoever
  opens the context for a user (null: only the timestamps are stamped). The creation stamp of an update is kept.
- Register with `services.AddDbContextFactory<ApplicationDbContext>(...)`. PostgreSQL is the only provider.

## 4. Repository contract - `{Project}.Core.Services.Abstractions/{Domain}/`

`public interface I{Entity}Repository : IRepository<{Entity}, {KeyType}>, IQueryableRepository<{Entity}>` (`Istarnova.EfCore.Repositories`). `IBaseRepository<T>` gives
`CreateAsync`, `UpdateAsync`, `DeleteAsync`, `GetAllAsync`, `Count`; `IRepository` adds `GetAsync(id)`, `DeleteByIdAsync(id)` and the indexer (also with two or three keys).
Add only the **domain-specific** queries. A repository that is not a CRUD repository (queries and one write operation) just declares what it does.

## 5. Repository implementation - `{Project}.Core.Services.Repositories/{Domain}/`

- **`IDbContextFactory<ApplicationDbContext>`, not a scoped context.** A fresh context per method: `await using var context = await _factory.CreateDbContextAsync(ct);`.
  Blazor circuits are long-lived, so the factory avoids cross-circuit concurrency bugs.
- Log through `RepositoryLogger` (`LogRepositoryCreated`, `LogRepositoryNotFound`, ...; EventIds 2000 to 2079), see the **logging** skill. Never `ILogger.LogError(...)` directly.
- Prefer projection and `IQueryable` filtering before materialisation; never expose the context upward. A DTO for the UI, not the entity.
- **Never `context.Update(item)` on what the caller hands over.** Load the row `AsTracking()`, return `false` when it is gone, then
  `context.Entry(existing).ApplyEdit(item)`. Attaching the caller's object writes every column (a field someone else edited meanwhile is overwritten), lets a stale
  navigation win over a changed key, and turns a missing row into a concurrency exception instead of "not found".
- `SetValues` copies scalars only; child collections are saved through their own repository.
- **`ExecuteUpdate` / `ExecuteDelete` skip `SaveChanges`** - no audit stamp, no concurrency check. A write a person triggers goes through `SaveChanges`. Bulk statements stay
  right for server-side work (thumbnails, log retention, an import rollback).
- A transaction around several saves: `context.Database.CreateExecutionStrategy().ExecuteAsync(...)` with `context.ChangeTracker.Clear()` at the start (retry on failure is on).

## 6. Registration

`AddRepositories()` in the repositories project is the one place: `services.AddScoped<I{Entity}Repository, {Entity}Repository>();`. Seeders that fill reference data from a
file implement a small `ICatalogSeeder`-like contract and run at startup, idempotent, matched by the game's or the domain's own identity - never as a migration.

## 7. Migrations

`python3 Scripts/dotnet.py ef add Name` / `ef update` fill in the project pair. Read the generated migration before applying it: it scaffolds every difference between model
and snapshot, including columns that were applied by hand. Whether migrations are committed is the project's decision (appendix). A reverted migration drops the schemas it
created - **re-run the application-role script afterwards** or the application's restricted role has no rights in them.

## Database functions, views and procedures

- **Always mapped through EF, never called via raw `SqlQuery`.** SQL lives tracked in `Scripts/sql/{name}.sql`, applied by `db.py apply`. A **view** for a parameterless
  projection (keyless entity, `HasNoKey()`, `ToView(...)`); a **function** when parameters shape the computation (a method on the context, registered with
  `HasCustomDbFunction<TContext>(name, schema)` from `Istarnova.EfCore.Extensions`; a function's result rows are a keyless entity with `ToNullableTable()` or
  `HasCustomDbFunctionAndToTableNullify`); a **procedure** when logic would otherwise be duplicated across SQL objects. Repositories compose LINQ over the result.
- **Heavy computation belongs in the database**, not in C#: aggregations, statistics, correlations over whole tables are SQL (`COUNT(*) FILTER`, window functions). Loading entity
  lists to `GroupBy`/`Average` them is the anti-pattern. Client-side stays fine for display shaping and for a handful of values.
- **Free-text search over more than one field is a database function** - the answer to "what can this record be found by" must be the same for the page, the API, an importer
  and anything else. `ILIKE`, never `LIKE`; an empty term yields nothing. The repository implements `ISearchQueryableRepository<T>` (a query a grid pages) and the grid
  `LocalizedSearchableDataGrid` offers the search box.

## Checklist

1. Entity (`BaseEntity`, `{Entity}Id`, alphabetical, inverse navigation on the parent).
2. `{Entity}Configuration : BaseEntityConfiguration<>` - schema, key, string enums, lengths, indexes.
3. `DbSet` (and the registration of the configuration if the project does it per schema).
4. `I{Entity}Repository` in Abstractions.
5. `{Entity}Repository` with the context factory and the shared logger.
6. DI in `AddRepositories()`.
7. Migration; the application-role script if a schema is new.
8. A test for the logic that is not a straight read (see the project's test setup).
