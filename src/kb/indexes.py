"""Deterministic wiki indexes rebuilt from the filesystem."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from kb.fsutil import atomic_write_text

MASTER_INDEX = "_master-index.md"
TOPIC_INDEX = "_index.md"


def is_article(path: Path) -> bool:
    return path.suffix == ".md" and not path.name.startswith("_")


def article_title(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return path.stem


def topic_articles(wiki: Path) -> dict[str, list[Path]]:
    topics: dict[str, list[Path]] = {}
    if not wiki.exists():
        return topics
    for topic_dir in sorted(p for p in wiki.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))):
        articles = sorted(p for p in topic_dir.iterdir() if p.is_file() and is_article(p))
        if articles:
            topics[topic_dir.name] = articles
    return topics


def rebuild(wiki: Path, title: str, source: str | None, taxonomy: dict[str, str], today: date) -> dict[str, int]:
    """Rewrite the master index and every topic index; returns article counts per topic."""
    topics = topic_articles(wiki)
    counts = {topic: len(articles) for topic, articles in topics.items()}

    for topic, articles in topics.items():
        label = taxonomy.get(topic, topic)
        entries = sorted(((article_title(a), a.name) for a in articles), key=lambda t: t[0].lower())
        lines = [f"# {label}", "", f"{len(articles)} articles.", ""]
        lines += [f"- [{name}]({filename})" for name, filename in entries]
        atomic_write_text(wiki / topic / TOPIC_INDEX, "\n".join(lines) + "\n")

    total = sum(counts.values())
    lines = [f"# {title}", ""]
    if source:
        lines.append(f"**Source:** {source}")
    lines += [f"**Updated:** {today.isoformat()}", f"**Articles:** {total}", "", "| Topic | Articles |", "|---|---|"]
    lines += [f"| [{taxonomy.get(t, t)}]({t}/{TOPIC_INDEX}) | {n} |" for t, n in counts.items()]
    wiki.mkdir(parents=True, exist_ok=True)
    atomic_write_text(wiki / MASTER_INDEX, "\n".join(lines) + "\n")
    return counts
