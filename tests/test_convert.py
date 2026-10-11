from pathlib import Path

import pytest

from kb.convert import document_to_markdown, docx_to_markdown, html_to_markdown

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("name", ["entry_content", "article_fallback", "body_only"])
def test_html_to_markdown_matches_pre_move_snapshot(name: str) -> None:
    html = (FIXTURES / "html" / f"{name}.html").read_text(encoding="utf-8")
    expected = (FIXTURES / "expected" / f"{name}.md").read_text(encoding="utf-8")
    assert html_to_markdown(html) == expected


def test_html_strips_links_images_and_chrome() -> None:
    md = html_to_markdown((FIXTURES / "html" / "entry_content.html").read_text(encoding="utf-8"))
    assert "](" not in md
    assert "Related posts" not in md
    assert "Copyright" not in md
    assert "Home" not in md


def test_short_main_falls_through_to_article() -> None:
    md = html_to_markdown((FIXTURES / "html" / "article_fallback.html").read_text(encoding="utf-8"))
    assert md.startswith("# Pricing FAQ")


def test_docx_matches_pre_move_snapshot() -> None:
    expected = (FIXTURES / "expected" / "guide_docx.md").read_text(encoding="utf-8")
    assert docx_to_markdown(FIXTURES / "docs" / "guide.docx") == expected


def test_document_to_markdown_dispatch(tmp_path: Path) -> None:
    txt = tmp_path / "note.txt"
    txt.write_text("  hello  \n", encoding="utf-8")
    assert document_to_markdown(txt) == "hello"
    assert document_to_markdown(tmp_path / "image.png") is None
    assert document_to_markdown(FIXTURES / "docs" / "guide.docx").startswith("# Onboarding Guide")
