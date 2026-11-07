"""Locating and downloading Unity's official offline documentation zips.

Unity publishes one ``UnityDocumentation.zip`` per editor release line
(``2022.3``, ``6000.0`` ...). It contains the same static HTML for every
version, so using it is far friendlier than crawling ~35k pages, and it gives
us one parser for every version.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ZIP_URL = "https://storage.googleapis.com/docscloudstorage/{version}/UnityDocumentation.zip"
USER_AGENT = "unity-docs-scraper/0.1 (+offline-docs-zip)"

# Release lines worth probing for `list-versions`.
_CANDIDATES = (
    ["2017.4", "2018.4", "2019.4", "2020.3", "2021.3", "2022.3", "2023.1", "2023.2"]
    + [f"6000.{i}" for i in range(0, 10)]
)

_VERSION_RE = re.compile(r"^(\d{4})\.(\d+)(?:\.\d+[a-z]*\d*)?$")


class SourceError(RuntimeError):
    pass


def normalize_version(raw: str) -> str:
    """'2022.3.10f1' / '6000.0.23f1' / '2022.3' -> '2022.3'."""
    raw = raw.strip()
    m = _VERSION_RE.match(raw)
    if not m:
        raise SourceError(
            f"Can't understand version {raw!r}. Use e.g. 2022.3, 6000.0 or a full "
            "editor version like 6000.0.23f1 (or 'latest')."
        )
    return f"{m.group(1)}.{m.group(2)}"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    p = Path(base) / "unity-docs-scraper"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _request(url: str, headers: dict | None = None) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})


def remote_size(version: str) -> int | None:
    """Size in bytes of the zip for ``version``, or None if it doesn't exist."""
    try:
        with urllib.request.urlopen(
            _request(ZIP_URL.format(version=version), {"Range": "bytes=0-0"}), timeout=30
        ) as r:
            return int(r.headers["Content-Range"].split("/")[1])
    except urllib.error.HTTPError as e:
        if e.code in (403, 404, 416):
            return None
        raise
    except (urllib.error.URLError, KeyError, ValueError) as e:
        raise SourceError(f"Could not reach the Unity docs server: {e}") from e


def available_versions() -> dict[str, int]:
    """Probe candidate release lines in parallel -> {version: zip size}."""
    with ThreadPoolExecutor(max_workers=8) as ex:
        sizes = list(ex.map(remote_size, _CANDIDATES))
    return {v: s for v, s in zip(_CANDIDATES, sizes) if s}


def resolve_version(raw: str) -> str:
    if raw.strip().lower() == "latest":
        found = available_versions()
        if not found:
            raise SourceError("Couldn't find any published documentation zips.")
        return max(found, key=lambda v: tuple(int(x) for x in v.split(".")))
    return normalize_version(raw)


def _progress(done: int, total: int) -> None:
    pct = 100 * done / total if total else 0
    sys.stderr.write(f"\r  downloading: {done / 1e6:7.1f} / {total / 1e6:.1f} MB ({pct:3.0f}%)")
    sys.stderr.flush()


def ensure_zip(version: str, refresh: bool = False) -> Path:
    """Return a local path to the docs zip, downloading (resumably) if needed."""
    dest = cache_dir() / f"{version}.zip"
    part = dest.with_suffix(".zip.part")
    if refresh:
        dest.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    if dest.exists():
        return dest

    total = remote_size(version)
    if total is None:
        known = ", ".join(available_versions()) or "none found"
        raise SourceError(
            f"No offline docs zip is published for {version!r}. "
            f"Available release lines: {known}"
        )

    have = part.stat().st_size if part.exists() else 0
    if have > total:
        part.unlink()
        have = 0
    if have < total:
        print(f"Fetching Unity {version} docs ({total / 1e6:.0f} MB) -> {dest}", file=sys.stderr)
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with urllib.request.urlopen(
                _request(ZIP_URL.format(version=version), headers), timeout=60
            ) as r, open(part, "ab" if have else "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
                    have += len(chunk)
                    _progress(have, total)
        except (urllib.error.URLError, TimeoutError) as e:
            raise SourceError(f"Download interrupted ({e}). Re-run to resume.") from e
        sys.stderr.write("\n")
    if part.stat().st_size != total:
        raise SourceError(f"Incomplete download ({part.stat().st_size}/{total} bytes). Re-run to resume.")
    part.rename(dest)
    return dest
