"""Main text and titles from ordinary HTML pages."""

import pytest
import trafilatura
from trafilatura import core

from yggdrasil.tools.web.extract import extract_html

_BODY = "<p>这篇文章介绍了一座小型水力发电站，说明它如何用河水带动水轮机，以及附近居民如何维护设备。</p>"


@pytest.mark.parametrize(
    ("head", "heading", "expected"),
    [
        (
            '<meta name="twitter:title" content="推特标题"><meta property="og:title" content="分享 &amp; 标题">',
            "",
            "分享 & 标题",
        ),
        ('<meta property="og:title" content=" "><meta name="citation_title" content="论文标题">', "", "论文标题"),
        ('<meta name="twitter:title" content="推特标题">', "", "推特标题"),
        ("", "", "网站"),
        ("", "<h1>文章标题</h1>", "文章标题"),
    ],
)
def test_extracts_title_without_full_metadata(
    head: str, heading: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def expensive_metadata(*args: object, **kwargs: object) -> None:
        pytest.fail("title lookup should not call trafilatura.extract_metadata")

    monkeypatch.setattr(trafilatura, "extract_metadata", expensive_metadata)
    monkeypatch.setattr(core, "extract_metadata", expensive_metadata)
    html = (
        f'<html><head><title>网站</title><meta name="citation_author" content="作者">{head}</head>'
        f"<body><article>{heading}{_BODY}</article></body></html>"
    )

    title, text = extract_html(html, "https://example.org/article")

    assert title == expected
    assert "水力发电站" in text
