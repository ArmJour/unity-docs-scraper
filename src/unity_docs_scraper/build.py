"""Drive the conversion of a whole docs zip and write the output tree."""

from __future__ import annotations

import json
import multiprocessing as mp
import posixpath
import re
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .convert import LINK_PREFIX, SECTIONS, ZIP_ROOT, Page, convert_page, slugify

OUT_DIR = {"Manual": "manual", "ScriptReference": "scriptref"}
SECTION_LABEL = {"Manual": "manual", "ScriptReference": "scripting-api"}
SECTION_ALIASES = {
    "manual": "Manual", "scriptref": "ScriptReference",
    "scriptreference": "ScriptReference", "api": "ScriptReference",
}
_LINK_RE = re.compile(r"\]\(" + re.escape(LINK_PREFIX) + r"([^)\s]+)\)")


def parse_sections(spec: str) -> list[str]:
    out = []
    for part in spec.split(","):
        key = part.strip().lower()
        if not key:
            continue
        if key not in SECTION_ALIASES:
            raise ValueError(f"Unknown section {part!r}; choose from: manual, scriptref")
        sec = SECTION_ALIASES[key]
        if sec not in out:
            out.append(sec)
    return out or list(SECTIONS)


# --------------------------------------------------------------------------- #
# parallel conversion
# --------------------------------------------------------------------------- #

_zip: zipfile.ZipFile | None = None
_version = ""


def _init_worker(zip_path: str, version: str) -> None:
    global _zip, _version
    _zip, _version = zipfile.ZipFile(zip_path), version


def _convert_entry(name: str):
    try:
        page = convert_page(_zip.read(name), name, _version)
        return name, page, None
    except Exception as e:  # keep going; report at the end
        return name, None, f"{type(e).__name__}: {e}"


def _convert_all(zip_path: Path, version: str, names: list[str], workers: int):
    done = 0
    step = max(500, len(names) // 20)

    def tick():
        nonlocal done
        done += 1
        if done % step == 0 or done == len(names):
            sys.stderr.write(f"\r  converting: {done}/{len(names)} pages")
            sys.stderr.flush()

    if workers <= 1:
        _init_worker(str(zip_path), version)
        for n in names:
            yield _convert_entry(n)
            tick()
    else:
        ctx = mp.get_context("spawn")
        with ctx.Pool(workers, _init_worker, (str(zip_path), version)) as pool:
            for res in pool.imap_unordered(_convert_entry, names, chunksize=64):
                yield res
                tick()
    sys.stderr.write("\n")


# --------------------------------------------------------------------------- #
# output assembly
# --------------------------------------------------------------------------- #

@dataclass
class Doc:
    """One output file."""

    path: str                  # relative to the version dir, e.g. scriptref/Rigidbody.md
    page: Page
    members: list[Page] = field(default_factory=list)
    toc_path: str = ""


def _yaml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)


def _frontmatter(doc: Doc, version: str) -> str:
    p = doc.page
    url = f"https://docs.unity3d.com/{version}/Documentation/{p.section}/{p.stem}.html"
    fm = {
        "title": p.title, "source": url, "unity_version": version,
        "section": SECTION_LABEL[p.section], "type": p.kind,
    }
    if p.namespace:
        fm["namespace"] = p.namespace
    if doc.toc_path:
        fm["toc_path"] = doc.toc_path
    if p.summary:
        fm["summary"] = p.summary
    lines = [f"{k}: {_yaml_str(v)}" for k, v in fm.items()]
    return "---\n" + "\n".join(lines) + "\n---\n"


_TYPE_KINDS = {"class", "struct", "interface", "enum"}


def _is_type(page: Page) -> bool:
    return page.kind.split(" ")[-1] in _TYPE_KINDS


def _infer_owner(page: Page, pages: dict[str, Page]) -> str:
    """Owning type of a member page that doesn't link back to it in its H1.

    ``Foo-ctor`` -> ``Foo``; ``Foo.Bar`` / ``Foo-bar`` -> ``Foo``. Only accepted
    when the candidate really is a type page, so delegates and namespaces stay put.
    """
    if page.kind != "member":
        return ""
    for sep in ("-", "."):
        if sep in page.stem:
            cand = pages.get(f"{page.section}/{page.stem.rsplit(sep, 1)[0]}")
            if cand is not None and cand is not page and _is_type(cand):
                return cand.key
    return ""


def _fill_enum_namespaces(top: list[Page]) -> None:
    """Enum pages don't state a namespace. Borrow it from sibling types whose
    file names share the same prefix (``Rendering.*`` -> ``UnityEngine.Rendering``)."""
    votes: dict[str, Counter] = defaultdict(Counter)
    ns_of_stem = {p.stem: p.namespace for p in top if p.namespace}
    for p in top:
        if p.namespace and "." in p.stem:
            votes[p.stem.rsplit(".", 1)[0]][p.namespace] += 1
    for p in top:
        if p.kind == "enum" and not p.namespace and "." in p.stem:
            prefix = p.stem.rsplit(".", 1)[0]
            if prefix in ns_of_stem:          # nested enum: inherits its containing type's namespace
                p.namespace = ns_of_stem[prefix]
            elif votes.get(prefix):
                p.namespace = votes[prefix].most_common(1)[0][0]
            else:
                p.namespace = prefix


def _member_order(class_page: Page, members: list[Page]) -> list[Page]:
    """Members in the order the class page's tables list them, then the rest."""
    by_key = {m.key: m for m in members}
    ordered: list[Page] = []
    for key in _LINK_RE.findall(class_page.body):
        m = by_key.pop(key, None)
        if m:
            ordered.append(m)
    ordered.extend(sorted(by_key.values(), key=lambda m: m.title.lower()))
    return ordered


def _doc_markdown(doc: Doc) -> str:
    p = doc.page
    parts = [f"# {p.title}"]
    if p.header:
        parts.append(f"*{p.header}*")
    if p.body:
        parts.append(p.body)
    if doc.members:
        parts.append("## Member reference")
        for m in doc.members:
            parts.append(f"### {m.title}")
            if m.header:
                parts.append(f"*{m.header}*")
            parts.append(m.body)
    return "\n\n".join(x for x in parts if x).rstrip() + "\n"


def _toc_index(toc: dict, version: str):
    """Manual toc.json -> ({stem: 'A > B > C'}, nested tree)."""
    paths: dict[str, str] = {}

    def walk(node, trail):
        title = node.get("title") or ""
        link = node.get("link")
        here = trail + ([title] if title and title != "root" else [])
        if link and link != "null" and link not in paths:
            paths[link] = " > ".join(here)
        for c in node.get("children") or []:
            walk(c, here)

    walk(toc, [])
    return paths


def build(
    version: str,
    zip_path: Path,
    out_root: Path,
    sections: list[str],
    formats: set[str],
    only: str | None = None,
    workers: int = 1,
) -> dict:
    out = out_root / version
    zf = zipfile.ZipFile(zip_path)
    all_names = zf.namelist()
    only_re = re.compile(only, re.I) if only else None

    names, tocs = [], {}
    for sec in sections:
        prefix = f"{ZIP_ROOT}{sec}/"
        for n in all_names:
            if n.startswith(prefix) and n.endswith(".html") and "/docdata/" not in n:
                if only_re is None or only_re.search(f"{sec}/{n[len(prefix):-5]}"):
                    names.append(n)
        toc_name = f"{prefix}docdata/toc.json"
        if toc_name in all_names:
            tocs[sec] = json.loads(zf.read(toc_name))
    names.sort()
    zf.close()
    if not names:
        raise ValueError("No pages matched (check --sections / --only).")

    print(f"Converting {len(names)} pages from Unity {version} docs…", file=sys.stderr)
    pages: dict[str, Page] = {}
    failures: list[tuple[str, str]] = []
    skipped = 0
    for name, page, err in _convert_all(zip_path, version, names, workers):
        if err:
            failures.append((name, err))
        elif page is None:
            skipped += 1
        else:
            pages[page.key] = page

    # --- group members under their owning type --------------------------- #
    members: dict[str, list[Page]] = defaultdict(list)
    top: list[Page] = []
    for page in pages.values():
        owner = f"{page.section}/{page.parent}" if page.parent else _infer_owner(page, pages)
        if owner and owner in pages and owner != page.key:
            members[owner].append(page)
        else:
            top.append(page)
    top.sort(key=lambda p: (p.section, p.stem.lower()))
    _fill_enum_namespaces(top)

    manual_toc = _toc_index(tocs["Manual"], version) if "Manual" in tocs else {}
    docs: list[Doc] = []
    location: dict[str, tuple[str, str]] = {}   # page key -> (file, '#anchor' | '')
    for page in top:
        rel = f"{OUT_DIR[page.section]}/{page.stem}.md"
        if ".." in Path(rel).parts:
            continue
        doc = Doc(rel, page, toc_path=manual_toc.get(page.stem, "") if page.section == "Manual" else "")
        doc.members = _member_order(page, members.get(page.key, []))
        docs.append(doc)
        location[page.key] = (rel, "")
        for m in doc.members:
            location[m.key] = (rel, "#" + slugify(m.title))

    lower: dict[str, str] = {}
    collisions = 0
    for d in docs:
        if d.path.lower() in lower:
            collisions += 1
        lower[d.path.lower()] = d.path

    # --- write ------------------------------------------------------------ #
    for sec in sections:
        shutil.rmtree(out / OUT_DIR[sec], ignore_errors=True)
    for stale in ("pages.jsonl", "README.md"):
        (out / stale).unlink(missing_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    def rewrite(text: str, own_file: str) -> str:
        def sub(m):
            key = m.group(1)
            if key in location:
                target, anchor = location[key]
                rel = posixpath.relpath(target, posixpath.dirname(own_file))
                return f"]({rel}{anchor})"
            return f"](https://docs.unity3d.com/{version}/Documentation/{key}.html)"
        return _LINK_RE.sub(sub, text)

    jsonl = open(out / "pages.jsonl", "w", encoding="utf-8") if "jsonl" in formats else None
    total_chars = 0
    for doc in docs:
        body = rewrite(_doc_markdown(doc), doc.path)
        total_chars += len(body)
        if "md" in formats:
            dest = out / doc.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(_frontmatter(doc, version) + "\n" + body, encoding="utf-8")
        if jsonl:
            p = doc.page
            rec = {
                "id": doc.path[:-3], "title": p.title, "type": p.kind,
                "section": SECTION_LABEL[p.section], "namespace": p.namespace,
                "unity_version": version, "summary": p.summary,
                "toc_path": doc.toc_path, "member_count": len(doc.members),
                "source": f"https://docs.unity3d.com/{version}/Documentation/{p.section}/{p.stem}.html",
                "markdown": body,
            }
            jsonl.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if jsonl:
        jsonl.close()

    if "md" in formats:
        for sec in sections:
            sec_docs = [d for d in docs if d.page.section == sec]
            if not sec_docs:
                continue
            if sec == "Manual":
                text = _manual_index(sec_docs, tocs.get("Manual"), version)
            else:
                text = _api_index(sec_docs, version)
            (out / OUT_DIR[sec] / "_index.md").write_text(rewrite(text, f"{OUT_DIR[sec]}/_index.md"), encoding="utf-8")
        (out / "README.md").write_text(_readme(version, docs, sections), encoding="utf-8")

    return {
        "out": out, "docs": len(docs), "pages": len(pages), "skipped": skipped,
        "failures": failures, "chars": total_chars, "collisions": collisions,
    }


# --------------------------------------------------------------------------- #
# indexes
# --------------------------------------------------------------------------- #

def _manual_index(docs: list[Doc], toc: dict | None, version: str) -> str:
    present = {d.page.stem: d for d in docs}
    seen: set[str] = set()
    lines = [
        f"# Unity {version} Manual — table of contents", "",
        "Nested outline mirroring the official TOC. Each entry links to a Markdown page.", "",
    ]

    def walk(node, depth):
        title = (node.get("title") or "").strip()
        link = node.get("link")
        kids = node.get("children") or []
        if title and title != "root":
            if link and link != "null" and link in present and link not in seen:
                seen.add(link)
                lines.append(f"{'  ' * depth}- [{title}]({LINK_PREFIX}Manual/{link})")
            else:
                lines.append(f"{'  ' * depth}- {title}")
            depth += 1
        for c in kids:
            walk(c, depth)

    if toc:
        walk(toc, 0)
    orphans = sorted((d for d in docs if d.page.stem not in seen), key=lambda d: d.page.title.lower())
    if orphans:
        lines += ["", "## Pages not in the table of contents", ""]
        lines += [f"- [{d.page.title}]({LINK_PREFIX}Manual/{d.page.stem})" for d in orphans]
    return "\n".join(lines) + "\n"


def _api_index(docs: list[Doc], version: str) -> str:
    by_ns: dict[str, list[Doc]] = defaultdict(list)
    for d in docs:
        by_ns[d.page.namespace or "(no namespace listed)"].append(d)
    lines = [
        f"# Unity {version} Scripting API — index", "",
        "One entry per type (class/struct/enum/interface), grouped by namespace. "
        "Each file contains the type overview plus the full reference for every member.", "",
    ]
    for ns in sorted(by_ns, key=lambda n: (n.startswith("("), n.lower())):
        lines += [f"## {ns}", ""]
        for d in sorted(by_ns[ns], key=lambda d: d.page.title.lower()):
            p = d.page
            kind = f" *({p.kind})*" if p.kind and p.kind != "page" else ""
            tail = f" — {p.summary}" if p.summary else ""
            lines.append(f"- [{p.title}]({LINK_PREFIX}ScriptReference/{p.stem}){kind}{tail}")
        lines.append("")
    return "\n".join(lines)


def _readme(version: str, docs: list[Doc], sections: list[str]) -> str:
    n_manual = sum(d.page.section == "Manual" for d in docs)
    n_api = sum(d.page.section == "ScriptReference" for d in docs)
    n_members = sum(len(d.members) for d in docs)
    L = [f"# Unity {version} documentation (LLM-friendly Markdown)", ""]
    L.append("Generated from Unity's official offline documentation for this editor version, "
             "so everything here matches the API of Unity " + version + ".")
    L += ["", "## Layout", ""]
    if "Manual" in sections:
        L.append(f"- `manual/` — {n_manual} User Manual pages, one per file. Start at `manual/_index.md` (full TOC).")
    if "ScriptReference" in sections:
        L.append(f"- `scriptref/` — {n_api} Scripting API types ({n_members} members merged in). "
                 "Start at `scriptref/_index.md` (types by namespace, with one-line summaries).")
    L += [
        "- `pages.jsonl` — (only with `--format jsonl`) the same content, one JSON record per file, for RAG/embedding.",
        "",
        "## How to use",
        "",
        "- Each file starts with YAML front matter: `title`, `source` (canonical URL), `type`, "
        "`namespace`, `summary`, and for manual pages `toc_path`.",
        "- **Scripting API:** one file per type. A type's overview comes first, then `## Member reference` "
        "with a `### Type.Member` section for every property/method/event, each showing the exact "
        "C# declaration, parameters, return value, description and examples. "
        "To find a member, grep for `### Rigidbody.AddForce`.",
        "- Links between pages are relative `.md` links and work offline. Links to anything not "
        "included point at the online docs.",
        "- Images are dropped; their alt text is kept as `*[image: …]*`.",
        "",
    ]
    return "\n".join(L)
