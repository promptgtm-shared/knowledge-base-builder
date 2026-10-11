from kb.urls import assign_slug, base_slug, local_key, normalize_url, site_id_from_url


def test_normalize_equates_case_fragment_and_trailing_slash() -> None:
    assert normalize_url("HTTPS://Example.com/Path/#frag") == normalize_url("https://example.com/Path")
    assert normalize_url("https://example.com/Path") == "https://example.com/Path"


def test_normalize_sorts_query_and_drops_default_port() -> None:
    assert normalize_url("https://example.com:443/a?b=2&a=1") == "https://example.com/a?a=1&b=2"
    assert normalize_url("http://example.com:8080/a") == "http://example.com:8080/a"


def test_normalize_root() -> None:
    assert normalize_url("https://example.com") == "https://example.com/"
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_site_id_from_url() -> None:
    assert site_id_from_url("https://www.Example.com/sitemap.xml") == "example-com"


def test_colliding_paths_get_distinct_stable_slugs() -> None:
    keys = [normalize_url(u) for u in ("https://example.com/a/b", "https://example.com/a-b", "https://example.com/a--b")]
    taken: set[str] = set()
    slugs = []
    for k in keys:
        s = assign_slug(k, taken)
        taken.add(s)
        slugs.append(s)
    assert slugs[0] == "a--b"
    assert slugs[1] == "a-b"
    assert slugs[2].startswith("a-b-") and len(set(slugs)) == 3

    taken2: set[str] = set()
    again = []
    for k in keys:
        s = assign_slug(k, taken2)
        taken2.add(s)
        again.append(s)
    assert again == slugs


def test_root_and_non_ascii_and_long_paths() -> None:
    assert base_slug(normalize_url("https://example.com/")) == "index"
    assert base_slug(normalize_url("https://example.com/caf%C3%A9/über")) == "cafe--uber"
    long = base_slug(normalize_url("https://example.com/" + "x" * 400))
    assert len(long) <= 130
    assert all(c.isalnum() or c in "-_" for c in long)


def test_only_non_ascii_segment_gets_hash_slug() -> None:
    slug = base_slug(normalize_url("https://example.com/日本語"))
    assert slug.startswith("page-")


def test_windows_reserved_names() -> None:
    assert base_slug(normalize_url("https://example.com/con")) == "con-page"


def test_local_key_slug_uses_stem() -> None:
    assert base_slug(local_key("Q3 Report.docx")) == "q3-report"


def test_thousand_urls_unique() -> None:
    taken: set[str] = set()
    for i in range(1000):
        url = f"https://example.com/blog/{i % 50}/post-{i}" if i % 3 else f"https://example.com/blog-{i % 50}--post-{i}"
        s = assign_slug(normalize_url(url), taken)
        assert s not in taken
        taken.add(s)


def test_slug_never_starts_with_underscore() -> None:
    assert base_slug(normalize_url("https://example.com/_index")) == "index"
    assert base_slug(normalize_url("https://example.com/__master-index")) == "master-index"
