"""Convert one Unity docs HTML page into clean, LLM-friendly Markdown.

The converter is deliberately bespoke (rather than a generic html2md) so that
Unity-specific constructs - API signatures, member tables, code examples,
tooltips, "switch to manual" chrome - come out compact and unambiguous.

Internal links are emitted as ``unity-link:<Section>/<stem>`` placeholders;
the build step rewrites them to relative ``.md`` paths once it knows where
every page ended up.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, NavigableString, Tag

ZIP_ROOT = "Documentation/en/"
SECTIONS = ("Manual", "ScriptReference")
LINK_PREFIX = "unity-link:"

# Elements that never carry documentation content.
_NOISE = (
    "script, style, noscript, .suggest, .scrollToFeedback, .loading, .nextprev, "
    ".breadcrumbs, .switch-link, .search-words, .tooltiptext, .footer-wrapper, "
    ".tooltipMoreInfoLink, button, form, nav, iframe"
)
_BLOCK_TAGS = {
    "p", "div", "section", "article", "ul", "ol", "li", "pre", "table", "blockquote",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "dl", "dt", "dd", "figure", "center",
}
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_CALLOUT_CLASSES = {"tip", "note", "warning", "important", "caution"}
_LANG_BY_CLASS = {
    "codeexamplecs": "csharp", "codeexamplejs": "javascript", "codeexample": "csharp",
}
_WS = re.compile(r"\s+")
_KIND_RE = re.compile(r"^(?P<kind>[\w ]+?)\s+in\s+(?P<ns>[\w.]+)$")


@dataclass
class Page:
    """A converted page, before it is placed in the output tree."""

    section: str                 # 'Manual' | 'ScriptReference'
    stem: str                    # path inside the section, no extension
    title: str
    body: str                    # Markdown without the H1
    summary: str = ""
    kind: str = ""               # class / struct / enum / interface / member / page ...
    namespace: str = ""
    header: str = ""             # one-line "class in X · Inherits from ..." markdown
    parent: str = ""             # for member pages: stem of the owning type

    @property
    def key(self) -> str:
        return f"{self.section}/{self.stem}"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _clean_text(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _heading_text(tag: Tag) -> str:
    """'Rigidbody' + '.AddForce' spread over nodes -> 'Rigidbody.AddForce'."""
    text = " ".join(tag.stripped_strings)
    text = re.sub(r"\s+([.,:)>\]])", r"\1", text)
    text = re.sub(r"([(<\[])\s+", r"\1", text)
    return _clean_text(text)


def slugify(text: str) -> str:
    """GitHub-style heading anchor."""
    s = re.sub(r"[^\w\- ]", "", text.lower())
    return s.strip().replace(" ", "-")


def _truncate(s: str, n: int = 220) -> str:
    s = _clean_text(s)
    if len(s) <= n:
        return s
    cut = s[:n]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if end > n * 0.5:
        return cut[: end + 1]
    return cut.rsplit(" ", 1)[0] + "…"


def _classes(tag: Tag) -> set[str]:
    c = tag.get("class") or []
    return {x.lower() for x in (c if isinstance(c, list) else c.split())}


def _pad(text: str) -> tuple[str, str]:
    """Whitespace to keep outside of an inline wrapper like [..](..) / **..**."""
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return lead, trail


def _wrap(text: str, mark: str) -> str:
    core = text.strip()
    if not core:
        return text
    lead, trail = _pad(text)
    return f"{lead}{mark}{core}{mark}{trail}"


def _quote(text: str) -> str:
    return "\n".join(("> " + l) if l.strip() else ">" for l in text.split("\n"))


# --------------------------------------------------------------------------- #
# the converter
# --------------------------------------------------------------------------- #

class _Converter:
    def __init__(self, zip_path: str, version: str):
        self.zip_dir = posixpath.dirname(zip_path)
        self.version = version
        self.shift = 0          # heading level offset, set once the page is analysed

    # ---- links ----------------------------------------------------------- #
    def href(self, raw: str | None) -> str | None:
        """Resolve a link to a placeholder / absolute URL; None = drop link."""
        raw = (raw or "").strip()
        if not raw or raw.startswith("#") or raw.lower().startswith(("javascript:", "data:")):
            return None
        if re.match(r"^(https?:)?//|^mailto:", raw, re.I):
            return raw
        path = raw.split("#", 1)[0].split("?", 1)[0]
        if not path:
            return None
        full = posixpath.normpath(posixpath.join(self.zip_dir, path))
        if full.startswith(ZIP_ROOT):
            rest = full[len(ZIP_ROOT):]
            head = rest.split("/", 1)[0]
            if head in SECTIONS and rest.endswith(".html"):
                return f"{LINK_PREFIX}{rest[:-5]}"
            return f"https://docs.unity3d.com/{self.version}/Documentation/{rest}"
        return None

    # ---- inline ---------------------------------------------------------- #
    def inline(self, node, in_cell: bool = False) -> str:
        if isinstance(node, NavigableString):
            if node.__class__.__name__ in ("Comment", "Doctype", "CData", "ProcessingInstruction"):
                return ""
            return _WS.sub(" ", str(node))
        if not isinstance(node, Tag):
            return ""
        name = node.name
        if name == "br":
            return " " if in_cell else "  \n"
        if name == "img":
            alt = _clean_text(node.get("alt", ""))
            return f"*[image: {alt}]*" if alt else ""

        def inner() -> str:
            return "".join(self.inline(c, in_cell) for c in node.children)

        if name == "a":
            text = inner()
            url = self.href(node.get("href"))
            if not text.strip():
                return ""
            if not url:
                return text
            lead, trail = _pad(text)
            return f"{lead}[{text.strip()}]({url}){trail}"
        if name in ("strong", "b"):
            return _wrap(inner(), "**")
        if name in ("em", "i"):
            return _wrap(inner(), "*")
        if name in ("code", "tt", "kbd", "samp") or "varname" in _classes(node):
            text = _clean_text(node.get_text())
            if not text:
                return ""
            fence = "``" if "`" in text else "`"
            return f"{fence}{text}{fence}"
        if name in ("p", "div", "li", "dd", "dt", "ul", "ol") or name in _HEADINGS:
            # block inside an inline context (e.g. table cells): flatten with spaces
            return " " + inner() + " "
        return inner()

    def inline_run(self, nodes, in_cell: bool = False) -> str:
        s = "".join(self.inline(n, in_cell) for n in nodes)
        s = re.sub(r"[ \t]+", " ", s)
        s = re.sub(r" *\n *", "\n", s)
        return s.strip()

    # ---- blocks ---------------------------------------------------------- #
    def blocks(self, parent: Tag) -> list[str]:
        out: list[str] = []
        run: list = []

        def flush():
            if run:
                text = self.inline_run(run)
                if text:
                    out.append(text)
                run.clear()

        for child in parent.children:
            if isinstance(child, Tag) and child.name in _BLOCK_TAGS:
                flush()
                out.extend(self.block(child))
            else:
                run.append(child)
        flush()
        return out

    def block(self, tag: Tag) -> list[str]:
        name = tag.name
        cls = _classes(tag)
        if name in _HEADINGS:
            level = max(2, min(6, int(name[1]) + self.shift))
            text = _heading_text(tag)
            return [f"{'#' * level} {text}"] if text else []
        if name == "p":
            text = self.inline_run(tag.children)
            return [text] if text else []
        if name == "pre":
            return [self.code_block(tag)]
        if name in ("ul", "ol"):
            return [self.list(tag)] if tag.find("li") else []
        if name == "table":
            t = self.table(tag)
            return [t] if t else []
        if name == "hr":
            return ["---"]
        if name == "blockquote":
            inner = "\n\n".join(self.blocks(tag))
            return [_quote(inner)] if inner else []
        if name == "dt":
            text = self.inline_run(tag.children)
            return [f"**{text}**"] if text else []
        if name in ("dl", "dd", "li"):
            return self.blocks(tag)
        # div / section / article / figure / center ...
        if "signature-cs" in cls or "sig-block" in cls:
            return self.signature(tag)
        if cls & _CALLOUT_CLASSES:
            inner = "\n\n".join(self.blocks(tag))
            label = sorted(cls & _CALLOUT_CLASSES)[0].capitalize()
            return [_quote(f"**{label}:** {inner}")] if inner else []
        return self.blocks(tag)

    def signature(self, tag: Tag) -> list[str]:
        """API declaration: plain text in a fenced block (links removed)."""
        sig = BeautifulSoup(str(tag), "lxml")
        for h in sig.find_all(["h2", "h3"]):
            h.decompose()
        for s in sig.select("span[style*=red]"):
            s.decompose()
        text = _clean_text(sig.get_text(" "))
        text = re.sub(r"\s+([,;)\]>])", r"\1", text)
        text = re.sub(r"([(\[<])\s+", r"\1", text)
        text = re.sub(r"\s+\(", "(", text)
        if not text:
            return []
        return [f"**Declaration**\n\n```csharp\n{text}\n```"]

    def code_block(self, tag: Tag) -> str:
        pre = BeautifulSoup(str(tag), "lxml").find("pre")
        for br in pre.find_all("br"):
            br.replace_with("\n")
        code = pre.get_text()
        code = code.replace("\xa0", " ").replace("\r\n", "\n").strip("\n")
        code = "\n".join(line.rstrip() for line in code.split("\n"))
        lang = ""
        for src in [tag, *tag.find_all("code")]:
            for c in _classes(src):
                if c in _LANG_BY_CLASS:
                    lang = _LANG_BY_CLASS[c]
                elif c.startswith(("language-", "lang-")):
                    lang = c.split("-", 1)[1]
            if lang:
                break
        fence = "~~~~" if "```" in code else "```"
        return f"{fence}{lang}\n{code}\n{fence}"

    def list(self, tag: Tag) -> str:
        ordered = tag.name == "ol"
        lines: list[str] = []
        n = 0
        for li in tag.find_all("li", recursive=False):
            n += 1
            marker = f"{n}. " if ordered else "- "
            pad = " " * len(marker)
            parts: list[str] = []
            run: list = []

            def flush():
                if run:
                    t = self.inline_run(run)
                    if t:
                        parts.append(t)
                    run.clear()

            for c in li.children:
                if isinstance(c, Tag) and c.name in ("ul", "ol"):
                    flush()
                    parts.append(self.list(c))
                elif isinstance(c, Tag) and c.name in _BLOCK_TAGS:
                    flush()
                    parts.extend(self.block(c))
                else:
                    run.append(c)
            flush()
            if not parts:
                continue
            first, *rest = "\n".join(parts).split("\n")
            item = [marker + first] + [(pad + l if l.strip() else l) for l in rest]
            lines.append("\n".join(item))
        return "\n".join(lines)

    def table(self, tag: Tag) -> str:
        rows: list[list[str]] = []
        for tr in tag.find_all("tr"):
            if tr.find_parent("table") is not tag:
                continue
            cells = tr.find_all(["th", "td"], recursive=False)
            if not cells:
                continue
            vals: list[str] = []
            for c in cells:
                v = self.inline_run(c.children, in_cell=True).replace("|", "\\|").replace("\n", " ")
                vals.append(_clean_text(v))
                span = str(c.get("colspan", "1"))
                vals.extend([""] * (int(span) - 1 if span.isdigit() and int(span) > 1 else 0))
            rows.append(vals)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        head, body = rows[0], rows[1:]  # GFM needs a header: first row is promoted if needed
        lines = ["| " + " | ".join(head) + " |", "|" + "|".join([" --- "] * width) + "|"]
        lines += ["| " + " | ".join(r) + " |" for r in body]
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def convert_page(html: str | bytes, zip_path: str, version: str) -> Page | None:
    """Convert one page. Returns None for pages with no documentation content."""
    if not zip_path.startswith(ZIP_ROOT):
        return None
    rest = zip_path[len(ZIP_ROOT):]
    section, _, stem_html = rest.partition("/")
    if section not in SECTIONS or not stem_html.endswith(".html") or "/docdata/" in zip_path:
        return None
    stem = stem_html[:-5]

    soup = BeautifulSoup(html, "lxml")
    root = soup.select_one("#content-wrap")
    h1 = root.find("h1") if root else None
    if root is None or h1 is None:
        return None
    for el in root.select(_NOISE):
        el.decompose()

    title = _heading_text(h1)
    if not title:  # some member pages ship with an empty <h1>; the <title> still has the name
        page_title = soup.title.get_text() if soup.title else ""
        title = _clean_text(page_title.split(":", 1)[-1]) if ":" in page_title else stem

    # Member pages (Rigidbody.AddForce, Rigidbody-mass) link back to their type in the H1.
    parent = ""
    link = h1.find("a", href=True)
    if section == "ScriptReference" and link:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(zip_path), link["href"]))
        prefix = ZIP_ROOT + "ScriptReference/"
        if target.startswith(prefix) and target.endswith(".html"):
            parent = target[len(prefix):-5]
    # Constructors and blank-H1 pages don't link back; the build step infers their
    # owner from the file name, but they are still members as far as layout goes.
    member_like = section == "ScriptReference" and bool(
        parent or not _heading_text(h1) or stem.endswith("-ctor")
    )

    conv = _Converter(zip_path, version)

    # Header block: "class in UnityEngine / Inherits from: Component / Implemented in: ..."
    kind = "page" if section == "Manual" else ""
    namespace = ""
    header_bits: list[str] = []
    head_div = h1.parent if h1.parent is not root and "mb20" in _classes(h1.parent) else None
    if head_div is not None:
        for p in head_div.find_all("p"):
            text = _clean_text(p.get_text(" "))
            if not text or text == "/":
                continue
            m = _KIND_RE.match(text)
            if m and not text.lower().startswith(("inherits", "implemented", "implements")):
                kind, namespace = m.group("kind").strip().lower(), m.group("ns")
                header_bits.append(text)
            elif text.lower() in ("enumeration", "enum"):  # enums state no namespace
                kind = "enum"
                header_bits.append("enum")
            else:
                header_bits.append(re.sub(r"\s*:\s*", ": ", conv.inline_run(p.children), count=1))
        head_div.decompose()
    else:
        h1.decompose()

    # Re-level headings so the shallowest one sits directly under the page's H1
    # (Unity's class pages jump straight to H3). Member pages are merged under
    # their type's "### Type.Member" heading, so theirs start one level deeper.
    levels = [
        int(h.name[1]) for h in root.find_all(_HEADINGS)
        if not h.find_parent(class_=re.compile(r"sig-block"))
    ]
    if levels:
        conv.shift = (4 if member_like else 2) - min(levels)

    # A short summary for indexes.
    summary = ""
    desc_h = next((h for h in root.find_all(["h2", "h3"]) if _heading_text(h).lower() == "description"), None)
    first_p = desc_h.find_next("p") if desc_h else root.find("p")
    if first_p:
        text = first_p.get_text(" ")  # space-joined so adjacent inline tags don't fuse
        text = re.sub(r"\s+([.,;:!?)\]])", r"\1", text)
        summary = _truncate(re.sub(r"([(\[])\s+", r"\1", text))

    body = "\n\n".join(conv.blocks(root))
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    if not body and not header_bits:
        return None
    if section == "ScriptReference" and not kind:
        if member_like:
            kind = "member"
        elif re.search(r"```csharp\n[^\n]*\bdelegate\b", body):
            kind = "delegate"
        else:
            kind = "page"

    return Page(
        section=section, stem=stem, title=title, body=body, summary=summary,
        kind=kind, namespace=namespace, header=" · ".join(header_bits), parent=parent,
    )
