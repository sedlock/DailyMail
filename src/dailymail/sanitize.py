"""Defensive sanitization of author-supplied announcement HTML for email.

Three passes, in this order:

1. `prefilter()` -- nh3 (Rust/ammonia) with `data:` permitted, purely so inline
   images survive to be extracted. This is what removes `<script>`, `<style>`,
   `<iframe>`, `<form>` and friends *together with their content*. Delegating
   that to a real HTML parser avoids a whole class of suppression bug that a
   hand-rolled pass is prone to (an unclosed or void disallowed tag silently
   eating the rest of the body).
2. `transform()` -- our own rewrite pass, which by now only ever sees already
   safe markup. It normalizes links, drops unusable ones, filters inline styles
   against a property allowlist, and hands every `<img>` to a resolver that
   decides between a `cid:` reference and a source-link note.
3. `clean()` -- the final gatekeeper, with `data:` no longer allowed, so no data
   URI can reach the rendered email even if pass 2 had a defect.

The stored source in SQLite is never modified. Rendering uses this derivative.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser

import nh3

# Formatting worth preserving, per the tag census in Phase 0 §10.
ALLOWED_TAGS: set[str] = {
    "p", "br", "span", "div", "strong", "b", "em", "i", "u", "s", "strike",
    "sub", "sup", "small", "ul", "ol", "li", "a", "img", "h1", "h2", "h3",
    "h4", "h5", "h6", "blockquote", "hr", "pre", "code",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption",
    "figure", "figcaption", "dl", "dt", "dd",
}

# Tag *and* content removed, not merely unwrapped.
CLEAN_CONTENT_TAGS: set[str] = {
    "script", "style", "iframe", "object", "embed", "form", "input", "button",
    "select", "textarea", "option", "noscript", "svg", "math", "template",
    "video", "audio", "source", "track", "canvas", "applet", "frame",
    "frameset", "meta", "link", "base", "title", "head",
}

ALLOWED_ATTRIBUTES: dict[str, set[str]] = {
    "*": {"style", "align", "dir", "lang"},
    # `rel` is intentionally absent: nh3 manages it via link_rel, and it
    # rejects the attribute being allowlisted at the same time.
    "a": {"href", "target", "title"},
    "img": {"src", "alt", "width", "height", "title"},
    "td": {"colspan", "rowspan", "valign", "width"},
    "th": {"colspan", "rowspan", "valign", "width"},
    "table": {"width", "border", "cellpadding", "cellspacing"},
    "col": {"width", "span"},
}

# `cid` is allowed because our own pass is what puts it there, and only after an
# image has been decoded, validated and attached as a MIME part.
ALLOWED_URL_SCHEMES: set[str] = {"http", "https", "mailto", "tel", "cid"}

# Inline CSS properties that survive. Everything else -- notably anything able to
# fetch or execute -- is dropped.
ALLOWED_STYLE_PROPERTIES: set[str] = {
    "color", "background-color", "font-weight", "font-style", "font-size",
    "font-family", "text-align", "text-decoration", "text-transform",
    "margin-left", "margin-right", "margin-top", "margin-bottom",
    "padding-left", "padding-right", "padding-top", "padding-bottom",
    "width", "max-width", "height", "line-height", "vertical-align",
    "border", "border-color", "border-width", "border-style",
    "border-collapse", "list-style-type", "white-space",
}

_DANGEROUS_STYLE = re.compile(
    r"url\s*\(|expression\s*\(|javascript:|vbscript:|@import|behavior\s*:",
    re.IGNORECASE,
)

VOID_TAGS = {"br", "hr", "img", "col", "wbr"}

# Hosts we will promote to https:// when an author omits the scheme. Matches a
# bare `host.tld/path` shape; deliberately conservative.
_SCHEMELESS_HOST = re.compile(
    r"^(?:www\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+"
    r"(?:[:/?#].*)?$",
    re.IGNORECASE,
)

_SAFE_SCHEME = re.compile(r"^(https?|mailto|tel|cid):", re.IGNORECASE)
_ANY_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


@dataclass
class LinkStats:
    normalized: list[tuple[str, str]] = field(default_factory=list)
    stripped: list[str] = field(default_factory=list)
    dropped: int = 0


def normalize_href(raw: str, stats: LinkStats | None = None) -> str | None:
    """Return a usable absolute URL, or None if the link must be dropped.

    Phase 0 found five malformed hrefs in 1,756 bodies: scheme-less hosts such as
    `go.rowan.edu/x`, values padded with whitespace or a leading `&nbsp;`, and one
    `file://` path. Authors, not the platform, produced these.
    """
    if raw is None:
        return None
    href = raw.replace("\xa0", " ").strip()
    # Editors sometimes leave the entity itself in the attribute value.
    while href.lower().startswith("&nbsp;"):
        href = href[6:].strip()
    href = href.strip().rstrip(".,;")
    if not href:
        return None

    if href.startswith("#"):
        return None  # in-page anchors are meaningless in an email

    if _SAFE_SCHEME.match(href):
        return href

    if _ANY_SCHEME.match(href):
        # file://, javascript:, data:, ftp:, smb:, ... all unusable here.
        if stats is not None:
            stats.stripped.append(href)
        return None

    if href.startswith("//"):
        return "https:" + href

    if href.startswith("/"):
        # Site-relative: resolve against the announcer host.
        return "https://apps.rowan.edu" + href

    if _SCHEMELESS_HOST.match(href):
        fixed = "https://" + href
        if stats is not None:
            stats.normalized.append((href, fixed))
        return fixed

    if stats is not None:
        stats.stripped.append(href)
    return None


def filter_style(raw: str) -> str:
    """Keep only allowlisted, inert CSS declarations."""
    if not raw or _DANGEROUS_STYLE.search(raw):
        raw = _DANGEROUS_STYLE.sub("", raw or "")
    kept = []
    for declaration in raw.split(";"):
        name, separator, value = declaration.partition(":")
        if not separator:
            continue
        name = name.strip().lower()
        value = value.strip()
        if not value or name not in ALLOWED_STYLE_PROPERTIES:
            continue
        if _DANGEROUS_STYLE.search(value):
            continue
        kept.append(f"{name}:{value}")
    return ";".join(kept)


class _Transformer(HTMLParser):
    """Rewrites links and images; leaves structure for nh3 to police."""

    def __init__(self, image_resolver=None) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self.link_stats = LinkStats()
        self.images_seen = 0
        self._image_resolver = image_resolver
        # Tracks, for each open <a>, whether it degraded to a plain <span>
        # because its target was unusable. Without this the replacement element
        # never gets closed and nh3 nests the rest of the body inside it.
        self._anchor_stack: list[str] = []

    # -- helpers
    def _emit(self, text: str) -> None:
        self.out.append(text)

    def _attributes(self, tag: str, attrs: list) -> str:
        allowed = ALLOWED_ATTRIBUTES.get("*", set()) | ALLOWED_ATTRIBUTES.get(tag, set())
        rendered = []
        for name, value in attrs:
            name = (name or "").lower()
            if name not in allowed or value is None:
                continue
            if name.startswith("on"):
                continue
            if name == "style":
                value = filter_style(value)
                if not value:
                    continue
            rendered.append(f'{name}="{escape(value, quote=True)}"')
        return (" " + " ".join(rendered)) if rendered else ""

    # -- parser callbacks
    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in CLEAN_CONTENT_TAGS or tag not in ALLOWED_TAGS:
            # Already removed by the prefilter; ignore defensively.
            return

        if tag == "img":
            self._handle_image(attrs)
            return

        if tag == "a":
            source = dict((k.lower(), v) for k, v in attrs)
            href = normalize_href(source.get("href", ""), self.link_stats)
            if href is None:
                # Keep the link text, lose the unusable target. The prefilter has
                # often already stripped the bad href, so count it here too.
                self.link_stats.dropped += 1
                self._anchor_stack.append("span")
                self.out.append("<span>")
                return
            self._anchor_stack.append("a")
            rebuilt = [("href", href), ("target", "_blank"), ("rel", "noopener noreferrer")]
            if source.get("style"):
                rebuilt.append(("style", source["style"]))
            if source.get("title"):
                rebuilt.append(("title", source["title"]))
            self.out.append(f"<a{self._attributes('a', rebuilt)}>")
            return

        self._emit(f"<{tag}{self._attributes(tag, attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if tag in CLEAN_CONTENT_TAGS or tag not in ALLOWED_TAGS:
            return
        if tag == "img":
            self._handle_image(attrs)
            return
        self._emit(f"<{tag}{self._attributes(tag, attrs)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in CLEAN_CONTENT_TAGS or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag == "a":
            # Close whatever the opener actually emitted.
            opened = self._anchor_stack.pop() if self._anchor_stack else "a"
            self.out.append(f"</{opened}>")
            return
        self._emit(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._emit(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self._emit(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._emit(f"&#{name};")

    def handle_comment(self, data: str) -> None:  # drop comments entirely
        return

    # -- images
    def _handle_image(self, attrs: list) -> None:
        source = dict((k.lower(), v) for k, v in attrs)
        src = (source.get("src") or "").strip()
        self.images_seen += 1
        if self._image_resolver is None:
            return
        outcome = self._image_resolver(src, source)
        if outcome is None:
            return
        kind, payload = outcome
        if kind == "cid":
            alt = escape(source.get("alt") or "", quote=True)
            style = filter_style(source.get("style") or "") or "max-width:100%;height:auto"
            self.out.append(
                f'<img src="cid:{escape(payload, quote=True)}" alt="{alt}" '
                f'style="{escape(style, quote=True)}" width="100%">'
            )
        elif kind == "note":
            self.out.append(payload)

    def result(self) -> str:
        # Close any anchors the source left open.
        while self._anchor_stack:
            self.out.append(f"</{self._anchor_stack.pop()}>")
        return "".join(self.out)


def transform(html: str, *, image_resolver=None) -> tuple[str, LinkStats, int]:
    """First pass. Returns (rewritten_html, link_stats, images_seen)."""
    parser = _Transformer(image_resolver=image_resolver)
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:
        # A body this malformed still must not break the digest; fall through to
        # nh3 on the raw input, which strips everything it cannot parse safely.
        return html or "", parser.link_stats, parser.images_seen
    return parser.result(), parser.link_stats, parser.images_seen


def _nh3(html: str, *, schemes: set[str]) -> str:
    return nh3.clean(
        html or "",
        tags=ALLOWED_TAGS,
        clean_content_tags=CLEAN_CONTENT_TAGS,
        attributes={tag: set(values) for tag, values in ALLOWED_ATTRIBUTES.items()},
        url_schemes=schemes,
        link_rel="noopener noreferrer",
        strip_comments=True,
    )


def prefilter(html: str) -> str:
    """Pass 1. Removes unsafe tags and their content, keeping `data:` image srcs.

    `data:` is permitted here only so inline images survive long enough to be
    decoded and validated. It is forbidden again in `clean()`.
    """
    return _nh3(html, schemes=ALLOWED_URL_SCHEMES | {"data"})


def clean(html: str) -> str:
    """Pass 3: the strict allowlist gatekeeper. No `data:` beyond this point."""
    return _nh3(html, schemes=ALLOWED_URL_SCHEMES)


# --- density pass ------------------------------------------------------------

# Editors leave behind paragraphs holding nothing but a non-breaking space or a
# stray <br>. In an email they read as dead vertical space, so they go.
_EMPTY_BLOCK = re.compile(
    r"<(p|div)\b[^>]*>(?:\s|&nbsp;|&#160;|&#xa0;|<br\s*/?>)*</\1>",
    re.IGNORECASE,
)

# Classic Outlook ignores <style>, so it would apply its own ~1em paragraph
# margins and the digest would render far looser than designed. Inline a tight
# default on any block that does not already carry a margin.
_DEFAULT_MARGINS = {
    "p": "margin:0 0 9px 0",
    "h1": "margin:12px 0 6px 0",
    "h2": "margin:12px 0 6px 0",
    "h3": "margin:12px 0 6px 0",
    "h4": "margin:12px 0 6px 0",
    "h5": "margin:10px 0 5px 0",
    "h6": "margin:10px 0 5px 0",
    "ul": "margin:0 0 9px 0;padding-left:22px",
    "ol": "margin:0 0 9px 0;padding-left:22px",
    "li": "margin:0 0 3px 0",
    "blockquote": "margin:0 0 9px 12px",
    "table": "margin:0 0 9px 0",
}
_OPEN_TAG = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)((?:\s[^>]*)?)>")


def _inline_default_margins(html: str) -> str:
    def replace(match: re.Match) -> str:
        tag = match.group(1).lower()
        attrs = match.group(2) or ""
        default = _DEFAULT_MARGINS.get(tag)
        if not default:
            return match.group(0)
        style_match = re.search(r'style\s*=\s*"([^"]*)"', attrs, re.IGNORECASE)
        if style_match:
            existing = style_match.group(1)
            if "margin" in existing.lower():
                return match.group(0)
            merged = f"{default};{existing}" if existing.strip() else default
            attrs = (
                attrs[: style_match.start(1)] + merged + attrs[style_match.end(1) :]
            )
            return f"<{tag}{attrs}>"
        return f'<{tag}{attrs} style="{default}">'

    return _OPEN_TAG.sub(replace, html)


def tighten(html: str) -> str:
    """Cosmetic density pass. Runs after sanitization, so it only ever sees
    already-safe markup and cannot reintroduce anything unsafe."""
    previous = None
    current = html or ""
    # Nested empties (<div><p>&nbsp;</p></div>) need more than one sweep.
    while previous != current:
        previous = current
        current = _EMPTY_BLOCK.sub("", current)
    return _inline_default_margins(current)


def sanitize_body(html: str, *, image_resolver=None) -> tuple[str, LinkStats, int]:
    """Full pipeline. Returns (safe_html, link_stats, images_seen)."""
    prefiltered = prefilter(html)
    rewritten, stats, images = transform(prefiltered, image_resolver=image_resolver)
    return tighten(clean(rewritten)), stats, images
