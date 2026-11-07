# unity-docs-scraper

Turn the Unity documentation for **any editor version** into Markdown (and optionally JSONL) that
LLMs can read, search and cite efficiently.

```bash
uv sync
uv run unity-docs versions                    # which editor versions have docs
uv run unity-docs scrape -v 6000.0            # -> ./unity-docs/6000.0/
uv run unity-docs scrape -v 2022.3.10f1 -o ~/docs -f md,jsonl
uv run unity-docs scrape -v latest -s scriptref --only '^ScriptReference/(Rigidbody|Physics)'
```

`-v` accepts `2022.3`, `6000.0`, a full editor version like `6000.0.23f1` (docs are published per
release line, so the patch part is ignored) or `latest`.

## How it works

It doesn't crawl. Unity publishes an official offline `UnityDocumentation.zip` for every release line,
and that contains the same static HTML for every version (2017.4 through 6000.x). The tool downloads
that zip once (350–560 MB, resumable, cached in `~/.cache/unity-docs-scraper/`), converts the pages in
parallel and writes the result. A full build takes about a minute.

Why not scrape docs.unity3d.com? Since Unity 6 those URLs redirect to a JS-heavy site (~1 MB per
page), and a crawl would be ~35k requests. The zip is one download, identical across versions, and
doesn't hammer Unity's servers.

## Output

```
unity-docs/6000.0/
  README.md            how to navigate (generated)
  manual/_index.md     full table of contents of the Manual
  manual/<page>.md     one file per Manual page (~3,100)
  scriptref/_index.md  every type by namespace with a one-line summary
  scriptref/<Type>.md  one file per type (~4,000), members merged in
  pages.jsonl          only with -f jsonl: one record per file, for RAG/embeddings
```

Choices that make it LLM-friendly:

- **One file per type for the Scripting API.** Unity's site has a separate page for every method and
  property (~30k pages). Here `Rigidbody.md` holds the overview plus a `### Rigidbody.AddForce` section
  for each member, with the exact C# declaration, parameters, return value, description and example.
  One read gets the whole picture; `grep '### Rigidbody.AddForce'` finds a member.
- **YAML front matter** on every file: `title`, `source` (canonical URL), `unity_version`, `type`
  (class/struct/enum/interface/delegate/page), `namespace`, `summary`, and `toc_path` for manual pages.
- **Clean Markdown**: fenced `csharp` code blocks, GFM tables, no site chrome (nav, feedback forms,
  tooltips, scripts). Headings are re-levelled so every file has a sane outline.
- **Working relative links** between pages (125k+ of them checked); anything not included points at the
  online docs. Images are dropped but their alt text is kept.
- **Indexes** so an agent can navigate without reading everything: `manual/_index.md` and
  `scriptref/_index.md`.

The full 6000.0 build is ~40 MB of text (about 10M tokens), so point an agent at the indexes and
`grep` rather than loading it whole. `--only REGEX` (matched against `Section/page-name`) builds a
subset; `-s manual` or `-s scriptref` builds one half.

## Options

| flag | meaning |
| --- | --- |
| `-v, --version` | editor version (required) |
| `-o, --out` | output directory (default `./unity-docs`; version goes in a subfolder) |
| `-s, --sections` | `manual`, `scriptref` or both (default) |
| `-f, --format` | `md` (default), `jsonl`, or `md,jsonl` |
| `--only REGEX` | only pages whose `Section/name` matches |
| `-j, --jobs` | parallel workers (default: CPUs − 1) |
| `--zip FILE` | use an existing `UnityDocumentation.zip` instead of downloading |
| `--refresh` | re-download the cached zip |

## Known limitations

- Only the English docs are supported (that's all the zip contains).
- Enum pages don't state their namespace in Unity's HTML; it's inferred from sibling types where
  possible, so ~500 global enums show up under "(no namespace listed)" in the API index.
- A few namespace/module overview pages (`UnityEngine.CoreModule` etc.) are kept as standalone files.
- File names that differ only by case would collide on case-insensitive filesystems (macOS/Windows
  defaults); the tool warns if that happens.

## Development

```bash
uv run --group dev pytest
```

Layout: `source.py` (version resolution and zip download), `convert.py` (one HTML page → Markdown),
`build.py` (parallel conversion, member merging, link rewriting, indexes, writers), `cli.py`.
