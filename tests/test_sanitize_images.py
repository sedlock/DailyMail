"""HTML sanitization, link correction, and the inline-image pipeline."""

from __future__ import annotations

import base64
import io
import re

import pytest
from PIL import Image

from dailymail import sanitize, settings as settings_module
from dailymail.images import ImageProcessor, decode_data_uri
from dailymail.sanitize import filter_style, normalize_href, sanitize_body

SOURCE_URL = "https://apps.rowan.edu/RowanAnnouncer/Announcement?SubmissionId=1"


def data_uri(image: Image.Image, fmt: str = "PNG") -> str:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return "data:image/%s;base64,%s" % (
        fmt.lower(), base64.b64encode(buffer.getvalue()).decode()
    )


# --- sanitizer ---------------------------------------------------------------


@pytest.mark.parametrize(
    "html,banned",
    [
        ("<script>alert(1)</script><p>ok</p>", "alert"),
        ("<style>p{color:red}</style><p>ok</p>", "color:red"),
        ('<iframe src="https://evil"></iframe><p>ok</p>', "iframe"),
        ("<form><input name=x></form><p>ok</p>", "<form"),
        ('<object data="x"></object><p>ok</p>', "<object"),
        ('<embed src="x"><p>ok</p>', "<embed"),
        ('<svg onload="x()"></svg><p>ok</p>', "<svg"),
        ('<p onclick="steal()">ok</p>', "onclick"),
        ('<img src="x" onerror="alert(1)">', "onerror"),
        ('<a href="javascript:alert(1)">x</a>', "javascript:"),
        ("<video src=x></video><p>ok</p>", "<video"),
        ("<!-- secret comment --><p>ok</p>", "secret comment"),
    ],
)
def test_unsafe_content_removed(html, banned):
    out, _, _ = sanitize_body(html)
    assert banned not in out


def test_safe_formatting_preserved():
    html = (
        "<p>Para</p><h3>Heading</h3><ul><li>one</li><li>two</li></ul>"
        "<ol><li>first</li></ol><strong>bold</strong><em>it</em><u>u</u>"
        "<blockquote>quote</blockquote><hr>"
        "<table><tr><th>H</th></tr><tr><td>cell</td></tr></table>"
        "<sup>1</sup><br>"
    )
    out, _, _ = sanitize_body(html)
    # Block tags carry an inlined default margin after the density pass, so
    # match on the tag name rather than an exact "<p>".
    for tag in ("p", "h3", "ul", "li", "ol", "strong", "em", "u",
                "blockquote", "hr", "table", "td", "sup", "br"):
        assert re.search(rf"<{tag}[\s>]", out), tag
    for text in ("Para", "Heading", "one", "two", "first", "bold", "quote", "cell"):
        assert text in out


def test_text_of_dropped_links_is_kept():
    out, _, _ = sanitize_body('<a href="javascript:x()">important words</a>')
    assert "important words" in out
    assert "javascript" not in out


def test_utf8_and_entities_survive():
    out, _, _ = sanitize_body("<p>caf&eacute; \U0001f34e &nbsp; em&mdash;dash</p>")
    assert "café" in out
    assert "\U0001f34e" in out


# --- link normalization ------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("go.rowan.edu/example", "https://go.rowan.edu/example"),
        ("rowan.edu/scca", "https://rowan.edu/scca"),
        ("Go.Rowan.edu/EduAdventures", "https://Go.Rowan.edu/EduAdventures"),
        ("www.rowan.edu/x", "https://www.rowan.edu/x"),
        ("//cdn.example.com/a", "https://cdn.example.com/a"),
        ("/RowanAnnouncer/Home", "https://apps.rowan.edu/RowanAnnouncer/Home"),
        ("https://ok.example/y", "https://ok.example/y"),
        ("http://plain.example/y", "http://plain.example/y"),
        ("mailto:a@rowan.edu", "mailto:a@rowan.edu"),
        ("tel:8562564400", "tel:8562564400"),
        ("  https://padded.example/x  ", "https://padded.example/x"),
        ("\xa0https://nbsp.example/x", "https://nbsp.example/x"),
    ],
)
def test_scheme_less_and_padded_urls_corrected(raw, expected):
    assert normalize_href(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "file:///etc/passwd",
        "file://rowanads.rowan.edu/home/whiting/Desktop/AFT/whiting@rowan.edu",
        "javascript:alert(1)",
        "data:text/html,<script>x</script>",
        "vbscript:x",
        "smb://share/x",
        "#anchor",
        "",
        "   ",
    ],
)
def test_unusable_urls_stripped(raw):
    assert normalize_href(raw) is None


def test_file_url_stripped_end_to_end():
    html = '<p><a href="file://rowanads.rowan.edu/home/x">Local file</a></p>'
    out, stats, _ = sanitize_body(html)
    assert "file://" not in out
    assert "Local file" in out


def test_scheme_less_correction_end_to_end():
    out, stats, _ = sanitize_body('<a href="go.rowan.edu/petpreparedness2026">Register</a>')
    assert 'href="https://go.rowan.edu/petpreparedness2026"' in out
    assert ("go.rowan.edu/petpreparedness2026",
            "https://go.rowan.edu/petpreparedness2026") in stats.normalized


def test_links_get_target_and_rel():
    out, _, _ = sanitize_body('<a href="https://x.example/y">link</a>')
    assert 'target="_blank"' in out
    assert 'rel="noopener noreferrer"' in out


# --- inline styles -----------------------------------------------------------


@pytest.mark.parametrize(
    "style,expected",
    [
        ("color:red;background:url(javascript:x)", "color:red"),
        ("width:99.7%", "width:99.7%"),
        ("behavior:url(#x);color:#333", "color:#333"),
        ("font-weight:bold;position:fixed", "font-weight:bold"),
        ("background-image:url(http://evil/x.png)", ""),
        ("expression(alert(1))", ""),
        ("text-align:center;font-size:14px", "text-align:center;font-size:14px"),
    ],
)
def test_style_property_allowlist(style, expected):
    assert filter_style(style) == expected


def test_dangerous_style_never_reaches_output():
    out, _, _ = sanitize_body('<p style="background:url(javascript:alert(1))">x</p>')
    assert "javascript" not in out
    assert "url(" not in out


# --- structure integrity -----------------------------------------------------


def test_dropped_anchor_does_not_nest_the_rest_of_the_document():
    html = ('<a href="javascript:x">bad</a><p>tail must be a sibling</p>')
    out, _, _ = sanitize_body(html)
    assert out.count("<span>") == out.count("</span>")
    assert out.index("tail must be a sibling") > out.index("</span>")


def test_void_disallowed_tag_does_not_swallow_the_body():
    """A regression guard: <input> is disallowed and has no closing tag."""
    html = "<p>before</p><form><input name=a></form><p>after</p><p>and more</p>"
    out, _, _ = sanitize_body(html)
    assert "before" in out
    assert "after" in out
    assert "and more" in out


def test_unclosed_tags_are_rebalanced():
    out, _, _ = sanitize_body("<p>one<p>two<strong>three")
    assert "one" in out and "two" in out and "three" in out


# --- image pipeline ----------------------------------------------------------


@pytest.fixture
def image_settings(settings_obj):
    return settings_obj


def _process(html, settings, submission_id=1):
    processor = ImageProcessor(settings)
    out, links, seen = sanitize_body(
        html, image_resolver=processor.resolver_for(submission_id, SOURCE_URL)
    )
    return out, processor


def test_data_uri_becomes_a_cid_attachment(image_settings):
    html = f'<p><img src="{data_uri(Image.new("RGB", (400, 200), (10, 90, 200)))}"></p>'
    out, processor = _process(html, image_settings)
    assert "data:image" not in out
    assert 'src="cid:' in out
    assert processor.stats.embedded == 1
    assert len(processor.images) == 1
    assert processor.images[0].content_type == "image/jpeg"
    assert processor.images[0].cid in out


def test_oversized_image_is_downscaled(image_settings):
    big = Image.new("RGB", (5000, 2500), (200, 40, 40))
    out, processor = _process(f'<img src="{data_uri(big)}">', image_settings)
    embedded = processor.images[0]
    assert embedded.width == image_settings.image_max_width_px
    assert embedded.height == pytest.approx(
        image_settings.image_max_width_px // 2, abs=2
    )
    assert embedded.final_bytes <= image_settings.image_max_bytes


def test_image_stays_within_per_image_budget(image_settings):
    noisy = Image.new("RGB", (3000, 2000))
    pixels = noisy.load()
    for x in range(0, 3000, 3):
        for y in range(0, 2000, 3):
            pixels[x, y] = ((x * 7) % 256, (y * 13) % 256, (x + y) % 256)
    out, processor = _process(f'<img src="{data_uri(noisy)}">', image_settings)
    assert processor.images[0].final_bytes <= image_settings.image_max_bytes


def test_exif_orientation_respected(image_settings):
    """A portrait image tagged as rotated must come out portrait-corrected."""
    image = Image.new("RGB", (400, 200), (20, 120, 60))
    buffer = io.BytesIO()
    exif = image.getexif()
    exif[274] = 6  # rotate 90 CW
    image.save(buffer, format="JPEG", exif=exif)
    uri = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()
    out, processor = _process(f'<img src="{uri}">', image_settings)
    embedded = processor.images[0]
    assert embedded.width == 200 and embedded.height == 400


@pytest.mark.parametrize(
    "src",
    [
        "data:image/png;base64,NOT!VALID!BASE64",
        "data:image/png;base64," + base64.b64encode(b"definitely not an image").decode(),
        "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode(),
        "http://insecure.example/a.png",
        "ftp://example/a.png",
    ],
)
def test_undecodable_or_unsupported_image_falls_back_to_a_link(src, image_settings):
    out, processor = _process(f'<img src="{src}">', image_settings)
    assert "Image available in the official announcement" in out
    assert SOURCE_URL in out
    assert processor.stats.omitted == 1
    assert processor.images == []


def test_total_image_budget_enforced(settings_obj):
    tight = type(settings_obj)(**{**settings_obj.__dict__, "image_max_total_bytes": 4096})
    html = "".join(
        f'<img src="{data_uri(Image.new("RGB", (1200, 800), (i * 30 % 255, 60, 90)))}">'
        for i in range(4)
    )
    out, processor = _process(html, tight)
    assert processor.total_bytes() <= 4096 or processor.stats.embedded == 0
    assert processor.stats.omitted >= 1
    assert "Image available in the official announcement" in out


def test_identical_images_share_one_attachment(image_settings):
    uri = data_uri(Image.new("RGB", (300, 150), (7, 7, 7)))
    out, processor = _process(f'<img src="{uri}"><img src="{uri}">', image_settings)
    assert processor.stats.seen == 2
    assert len(processor.images) == 1


def test_rgba_is_flattened_not_rejected(image_settings):
    rgba = Image.new("RGBA", (300, 200), (255, 0, 0, 120))
    out, processor = _process(f'<img src="{data_uri(rgba)}">', image_settings)
    assert processor.stats.embedded == 1


def test_external_https_image_downloaded_and_embedded(image_settings):
    import httpx

    buffer = io.BytesIO()
    Image.new("RGB", (500, 300), (30, 60, 120)).save(buffer, format="PNG")
    payload = buffer.getvalue()

    def handler(request):
        return httpx.Response(200, content=payload,
                              headers={"content-type": "image/png"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    processor = ImageProcessor(image_settings, http_client=client)
    out, _, _ = sanitize_body(
        '<img src="https://cdn.example/flyer.png">',
        image_resolver=processor.resolver_for(1, SOURCE_URL),
    )
    assert processor.stats.embedded == 1
    assert 'src="cid:' in out


def test_external_download_failure_falls_back_to_link(image_settings):
    import httpx

    def handler(request):
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    processor = ImageProcessor(image_settings, http_client=client)
    out, _, _ = sanitize_body(
        '<img src="https://cdn.example/missing.png">',
        image_resolver=processor.resolver_for(1, SOURCE_URL),
    )
    assert "Image available in the official announcement" in out
    assert processor.stats.omitted == 1


def test_external_download_size_limit_enforced(settings_obj):
    import httpx

    limited = type(settings_obj)(
        **{**settings_obj.__dict__, "image_external_max_download_bytes": 100}
    )
    buffer = io.BytesIO()
    Image.new("RGB", (900, 900)).save(buffer, format="PNG")

    def handler(request):
        return httpx.Response(200, content=buffer.getvalue(),
                              headers={"content-type": "image/png"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    processor = ImageProcessor(limited, http_client=client)
    out, _, _ = sanitize_body(
        '<img src="https://cdn.example/big.png">',
        image_resolver=processor.resolver_for(1, SOURCE_URL),
    )
    assert processor.stats.embedded == 0
    assert "Image available in the official announcement" in out


def test_non_image_content_type_rejected(image_settings):
    import httpx

    def handler(request):
        return httpx.Response(200, content=b"<html>nope</html>",
                              headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    processor = ImageProcessor(image_settings, http_client=client)
    out, _, _ = sanitize_body(
        '<img src="https://cdn.example/page.html">',
        image_resolver=processor.resolver_for(1, SOURCE_URL),
    )
    assert processor.stats.embedded == 0


def test_decode_data_uri_variants():
    assert decode_data_uri("data:image/png;base64,aGk=") == b"hi"
    assert decode_data_uri("data:image/png;base64, aG\nk= ") == b"hi"
    assert decode_data_uri("data:image/png,hi") == b"hi"
    assert decode_data_uri("https://example/x.png") is None
    assert decode_data_uri("data:image/png;base64,!!!") is None


def test_real_phase_zero_image_compresses(image_settings, employee_fixture):
    """The 1.2 MB inline image from announcement 6602 must shrink dramatically."""
    body = next(
        r["FullBody"] for r in employee_fixture["Announcements"]
        if r["Submission"]["Id"] == "6602"
    )
    if "data:image" not in body:
        pytest.skip("fixture image was truncated")
    out, processor = _process(body, image_settings, submission_id=6602)
    assert "data:image" not in out
    if processor.stats.embedded:
        image = processor.images[0]
        assert image.final_bytes < image.original_bytes / 4
        assert image.width <= image_settings.image_max_width_px


# --- density pass ------------------------------------------------------------


def test_empty_paragraphs_removed():
    """CKEditor leaves &nbsp;-only paragraphs; they are dead vertical space."""
    out, _, _ = sanitize_body(
        "<p>real</p><p>&nbsp;</p><p><br>&nbsp;</p><p>more</p><div>  </div>"
    )
    assert "real" in out and "more" in out
    assert not re.search(r"<p[^>]*>(?:\s|&nbsp;|<br\s*/?>)*</p>", out)


def test_nested_empty_blocks_removed():
    out, _, _ = sanitize_body("<div><p>&nbsp;</p></div><p>kept</p>")
    assert "kept" in out
    assert "&nbsp;" not in out


def test_paragraphs_get_inline_margins_for_outlook():
    """Classic Outlook ignores <style>, so spacing must be inline."""
    out, _, _ = sanitize_body("<p>one</p><h3>head</h3><ul><li>item</li></ul>")
    for match in re.finditer(r"<p([^>]*)>", out):
        assert "margin" in match.group(1)
    assert re.search(r"<h3[^>]*margin", out)
    assert re.search(r"<ul[^>]*margin", out)
    assert re.search(r"<li[^>]*margin", out)


def test_author_margins_are_not_overridden():
    out, _, _ = sanitize_body('<p style="margin-left:40px">indented</p>')
    assert "margin-left:40px" in out
    assert "margin:0 0 9px 0" not in out


def test_author_styles_preserved_alongside_injected_margin():
    out, _, _ = sanitize_body('<p style="color:red">tinted</p>')
    assert "color:red" in out
    assert "margin:0 0 9px 0" in out


def test_density_pass_cannot_reintroduce_unsafe_content():
    hostile = (
        '<p>ok</p><script>alert(1)</script><p>&nbsp;</p>'
        '<a href="javascript:x">b</a><iframe src=x></iframe>'
    )
    out, _, _ = sanitize_body(hostile)
    for banned in ("script", "alert", "javascript:", "<iframe"):
        assert banned not in out
