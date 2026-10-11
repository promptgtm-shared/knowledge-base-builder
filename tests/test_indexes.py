from datetime import date
from pathlib import Path

from kb import indexes


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_rebuild_lists_articles_sorted_by_title(tmp_path: Path) -> None:
    write(tmp_path / "guides" / "b.md", "# Zebra guide\n\nbody")
    write(tmp_path / "guides" / "a.md", "# alpha guide\n\nbody")
    write(tmp_path / "guides" / "_index.md", "stale")
    write(tmp_path / "news" / "n.md", "no heading at all")
    (tmp_path / "empty").mkdir()

    counts = indexes.rebuild(tmp_path, "Example KB", "https://example.com/sitemap.xml", {"guides": "Guides"}, date(2026, 4, 1))
    assert counts == {"guides": 2, "news": 1}

    topic = (tmp_path / "guides" / "_index.md").read_text(encoding="utf-8")
    assert topic == "# Guides\n\n2 articles.\n\n- [alpha guide](a.md)\n- [Zebra guide](b.md)\n"
    assert "- [n](n.md)" in (tmp_path / "news" / "_index.md").read_text(encoding="utf-8")

    master = (tmp_path / "_master-index.md").read_text(encoding="utf-8")
    assert master.startswith("# Example KB\n\n**Source:** https://example.com/sitemap.xml\n**Updated:** 2026-04-01\n**Articles:** 3\n")
    assert "| [Guides](guides/_index.md) | 2 |" in master and "| [news](news/_index.md) | 1 |" in master
    assert "empty" not in master


def test_rebuild_on_missing_wiki(tmp_path: Path) -> None:
    assert indexes.rebuild(tmp_path / "wiki", "KB", None, {}, date(2026, 1, 1)) == {}
    assert (tmp_path / "wiki" / "_master-index.md").exists()
