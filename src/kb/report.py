"""Markdown run reports written to sites/<site>/runs/."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from kb.fsutil import atomic_write_text


def _section(title: str, items: list[str], limit: int = 50) -> list[str]:
    if not items:
        return []
    lines = ["", f"## {title} ({len(items)})", ""]
    lines += [f"- {item}" for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"- ... and {len(items) - limit} more")
    return lines


def write_run_report(
    runs_dir: Path,
    site_id: str,
    run_id: str,
    when: datetime,
    status: str,
    counts: dict[str, int],
    sections: dict[str, list[str]],
    notes: list[str] | None = None,
) -> Path:
    lines = [
        f"# Sync report: {site_id}",
        "",
        f"**Run:** {run_id}",
        f"**Finished:** {when.isoformat(timespec='seconds')}",
        f"**Status:** {status}",
        "",
        "| Metric | Count |",
        "|---|---|",
    ]
    lines += [f"| {name} | {value} |" for name, value in counts.items()]
    for title, items in sections.items():
        lines += _section(title, items)
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]

    path = runs_dir / f"{when:%Y%m%d-%H%M%S}-{run_id}.md"
    atomic_write_text(path, "\n".join(lines) + "\n")
    return path
