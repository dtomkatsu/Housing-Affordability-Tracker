"""Shared HTML data-block patcher used by all *-updater.py scripts.

Every updater writes a tagged block of the form::

    /* TAG_DATA_START */
    const fooData = { ... };
    /* TAG_DATA_END */

into one or both of the dashboard HTML files (squarespace-single-file.html,
index.html).  Previously each updater defined its own ``PATCH_RE`` + ``patch_html``
pair — identical code, different tag.  This module gives them a single home.

Usage::

    from src.html_patcher import patch_data_block, patch_html_files

    new_block = build_my_block(data)
    patched_files = patch_html_files(DEFAULT_FILES, "CPI", new_block, dry_run=dry_run)
"""

from __future__ import annotations

import re
from pathlib import Path


def patch_data_block(html: str, tag: str, new_block: str) -> tuple[str, bool]:
    """Replace the ``/* TAG_DATA_START */ … /* TAG_DATA_END */`` section.

    Parameters
    ----------
    html:      Full HTML file contents.
    tag:       The tag name, e.g. ``"CPI"``, ``"GAS"``, ``"TFP"``.
    new_block: Replacement text including the START/END comment delimiters.

    Returns
    -------
    (new_html, patched) where *patched* is False when the markers were not found
    (the caller should warn; the original html is returned unchanged).
    """
    pattern = re.compile(
        rf"/\* {re.escape(tag)}_DATA_START \*/.*?/\* {re.escape(tag)}_DATA_END \*/",
        re.DOTALL,
    )
    if not pattern.search(html):
        return html, False
    return pattern.sub(lambda _: new_block, html, count=1), True


def patch_html_files(
    paths: list[Path],
    tag: str,
    new_block: str,
    *,
    dry_run: bool = False,
    encoding: str = "utf-8",
) -> list[Path]:
    """Read, patch, and write each HTML file in *paths*.

    Skips files that don't exist or don't contain the expected markers.
    Prints a status line for every file processed.

    Parameters
    ----------
    paths:     List of HTML file paths to patch (usually the two dashboard files).
    tag:       Data-block tag (e.g. ``"CPI"``).
    new_block: Full replacement block including delimiters.
    dry_run:   If True, print what would happen but don't write.
    encoding:  File encoding (default UTF-8).

    Returns
    -------
    List of Path objects that were successfully patched (or would have been
    patched in dry-run mode).
    """
    patched: list[Path] = []
    for target in paths:
        if not target.exists():
            print(f"  skip: {target} not found")
            continue
        html = target.read_text(encoding=encoding)
        new_html, ok = patch_data_block(html, tag, new_block)
        if not ok:
            print(f"  WARNING: {tag}_DATA markers not found in {target.name}")
            continue
        if dry_run:
            print(f"  [dry-run] would patch {target.name}")
        else:
            target.write_text(new_html, encoding=encoding)
            print(f"  patched {target.name}")
        patched.append(target)
    return patched
