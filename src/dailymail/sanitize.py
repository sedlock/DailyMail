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


# --- render-time colour policy ------------------------------------------------
#
# `filter_style` above is the *security* allowlist: it decides what CSS is inert
# enough to survive at all, and `color` legitimately is. What it cannot decide is
# whether an author's colour makes sense inside DailyMail's own design, and on
# 1 September 2026 one did not.
#
# Rowan announcement 6612, "Nominate the PROFessional(s) of the Month", wraps
# every paragraph of its body in:
#
#     <span style="color:rgb(90,19,0);">...</span>
#
# `rgb(90,19,0)` is `#5A1300` -- within three points of DailyMail's own accent
# `#57150B`, the colour the card's headline is painted in. In light rendering it
# merely looked odd. In Outlook mobile's dark mode, which force-inverts the
# design, the author's dark maroon and the design's dark maroon invert to the
# *same* peach, so the entire article rendered as if it were all headline. The
# control case that renders correctly, 6736 (OSEC), simply carries no `color`
# at all and inherits the body colour.
#
# So the renderer establishes a hard boundary around sanitized announcement
# content: source text colours are dropped and DailyMail's own are inlined.
# Structure, emphasis, lists, tables and links all still work -- only the
# palette is DailyMail's to choose, because only DailyMail knows what the
# surrounding card and the reader's dark mode are doing.
#
# The stored `FullBody` is untouched. This operates on the render derivative.

_COLOUR_PROPERTIES = frozenset({"color", "background-color"})

# Elements that directly contain body prose, and therefore need the body colour
# pinned on them rather than inherited across an unknown number of wrappers.
BODY_TEXT_TAGS = frozenset(
    {
        "p", "div", "li", "td", "th", "blockquote", "pre", "code", "caption",
        "dt", "dd", "figcaption", "small",
    }
)
BODY_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


@dataclass(frozen=True)
class BodyColorPolicy:
    """The colours DailyMail imposes on announcement body copy.

    Defaults mirror `email.html.j2`: `INK` for prose, `BROWN` for headings and
    links. Passing `strip_source_colors=False` disables the policy entirely,
    which is what the pure-sanitizer tests use to prove the security allowlist
    is unchanged.
    """

    body: str = "#1a1a1a"
    heading: str = "#57150B"
    link: str = "#57150B"
    strip_source_colors: bool = True


def strip_colour_declarations(style: str) -> str:
    """Remove `color` and `background-color`, keep every other declaration."""
    kept = []
    for declaration in (style or "").split(";"):
        name, separator, value = declaration.partition(":")
        if not separator:
            continue
        name, value = name.strip(), value.strip()
        if not name or not value or name.lower() in _COLOUR_PROPERTIES:
            continue
        kept.append(f"{name}:{value}")
    return ";".join(kept)


def apply_body_color_policy(html: str, policy: BodyColorPolicy) -> str:
    """Repaint sanitized body copy in DailyMail's palette. Idempotent.

    Runs after `clean()`, so it only ever sees markup a real HTML sanitizer has
    already approved and cannot reintroduce anything unsafe: it edits the value
    of one attribute and never the tag structure.
    """
    if not html or not policy.strip_source_colors:
        return html

    def replace(match: re.Match) -> str:
        tag = match.group(1).lower()
        attrs = match.group(2) or ""
        style_match = re.search(r'style\s*=\s*"([^"]*)"', attrs, re.IGNORECASE)
        existing = style_match.group(1) if style_match else ""
        cleaned = strip_colour_declarations(existing)

        if tag in BODY_HEADING_TAGS:
            forced = policy.heading
        elif tag == "a":
            forced = policy.link
        elif tag in BODY_TEXT_TAGS:
            forced = policy.body
        else:
            # `span`, `strong`, `em`, `u`, `img`, `ul`, `table`, ... Their colour
            # comes from the block they sit in, so pinning it here would only
            # add bytes. Their *source* colour is still gone.
            forced = None
        if forced:
            cleaned = f"{cleaned};color:{forced}" if cleaned else f"color:{forced}"

        if style_match:
            if cleaned:
                return (
                    f"<{tag}{attrs[: style_match.start(1)]}{cleaned}"
                    f"{attrs[style_match.end(1) :]}>"
                )
            remainder = (attrs[: style_match.start()] + attrs[style_match.end() :]).strip()
            return f"<{tag} {remainder}>" if remainder else f"<{tag}>"
        if cleaned:
            return f'<{tag}{attrs} style="{cleaned}">'
        return match.group(0)

    return _OPEN_TAG.sub(replace, html)


# --- render-time alignment policy ---------------------------------------------
#
# The sibling of the colour policy above, and for the same reason. `text-align`
# is inert, so the security allowlist keeps it -- but whether an author's
# alignment is *legible* inside DailyMail's card is not a security question, and
# on a narrow viewport one answer is clearly wrong.
#
# Rowan announcement 6846, the RCHGHR student-led discussion, sets
#
#     <p style="margin-left:0in;text-align:justify;">
#
# on every paragraph of its body. Full justification on a 390px Outlook mobile
# pane, with no hyphenation engine, opens rivers of whitespace between words; the
# control that reads correctly, 6926 (the September 11 memorial service), simply
# carries no style attribute at all and inherits the digest's own left alignment.
#
# So the digest owns normal prose alignment the same way it owns normal prose
# colour: source justification is dropped, arbitrary right alignment in ordinary
# prose is dropped, and every prose block is pinned to `left` explicitly -- which
# also stops an alignment set on a wrapper from leaking into the blocks inside
# it. The stored `FullBody` is untouched; this operates on the render derivative.
#
# Three things are deliberately NOT normalized, because there alignment carries
# meaning rather than decoration:
#
#   * table cells and the table chrome around them -- a right-aligned numeric
#     column is the author saying something true about the data;
#   * `figure`/`figcaption` and image layout;
#   * compact centred content. A centred one-line call to action or a centred
#     image caption is a deliberate visual choice and forcing it left looks
#     broken. A centred block holding a whole article is not compact, and is
#     treated as ordinary prose.

# Properties that exist only to support justification. None of them is in
# `ALLOWED_STYLE_PROPERTIES`, so `filter_style` already drops them -- this is the
# belt to that braces, so the policy stays correct if the allowlist ever widens.
_JUSTIFICATION_SUPPORT = frozenset({"text-align-last", "text-justify", "word-spacing"})

# Blocks that hold ordinary announcement prose. These get an explicit alignment.
PROSE_ALIGN_TAGS = frozenset(
    {"p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
     "dt", "dd", "pre", "small"}
)

# Structure whose alignment is the author saying something about the layout.
# Left entirely alone: neither stripped nor pinned.
STRUCTURAL_ALIGN_TAGS = frozenset(
    {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "col", "caption",
     "figure", "figcaption", "img"}
)

# A centred prose block is kept centred only while it stays compact. Measured on
# the block's own collapsed text, so a centred banner line survives and a centred
# article does not. 200 characters is roughly two printed lines at the digest's
# body size on the narrowest supported viewport.
COMPACT_CENTRE_CHARS = 200


@dataclass(frozen=True)
class BodyAlignmentPolicy:
    """The alignment DailyMail imposes on announcement body copy.

    `normalize=False` disables the policy entirely, which is what the pure
    sanitizer tests use to prove the security allowlist is unchanged -- and what
    the browser QA suite rebuilds the page with to prove its own assertions can
    actually fail.
    """

    prose: str = "left"
    normalize: bool = True
    compact_centre_chars: int = COMPACT_CENTRE_CHARS


def strip_alignment_declarations(style: str, *, drop_text_align: bool = True) -> str:
    """Remove alignment and its justification helpers, keep everything else."""
    kept = []
    for declaration in (style or "").split(";"):
        name, separator, value = declaration.partition(":")
        if not separator:
            continue
        name, value = name.strip(), value.strip()
        if not name or not value:
            continue
        lowered = name.lower()
        if lowered in _JUSTIFICATION_SUPPORT:
            continue
        if drop_text_align and lowered == "text-align":
            continue
        kept.append(f"{name}:{value}")
    return ";".join(kept)


def _declared_alignment(attrs: dict) -> str | None:
    """The element's own alignment, from `style` first and then `align=`."""
    style = attrs.get("style") or ""
    for declaration in style.split(";"):
        name, separator, value = declaration.partition(":")
        if separator and name.strip().lower() == "text-align":
            value = value.strip().lower()
            if value:
                return value
    align = (attrs.get("align") or "").strip().lower()
    return align or None


class _AlignmentTransformer(HTMLParser):
    """Rewrites alignment on prose blocks. Structure is never touched.

    Two phases, because a centred block's fate depends on how much text it turns
    out to hold and that is only known once it closes -- and a block that inherits
    centring from an ancestor depends on the *ancestor's* fate, which is later
    still. So parsing records one frame per prose element and emits a placeholder
    chunk; `result()` then resolves the frames parent-first and patches each
    placeholder in place.
    """

    def __init__(self, policy: BodyAlignmentPolicy) -> None:
        super().__init__(convert_charrefs=False)
        self._policy = policy
        self._out: list[str] = []
        # One entry per prose element, in document order, so a parent is always
        # resolved before any of its children.
        self._frames: list[dict] = []
        self._open_prose: list[int] = []   # indices into _frames
        self._stack: list[tuple[str, int | None]] = []   # (tag, frame index)

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _render(tag: str, attrs: dict) -> str:
        parts = [tag]
        for name, value in attrs.items():
            parts.append(f'{name}="{escape(value or "", quote=True)}"')
        return f"<{' '.join(parts)}>"

    def _count_text(self, text: str) -> None:
        length = len(" ".join(text.split()))
        if not length:
            return
        for index in self._open_prose:
            self._frames[index]["length"] += length

    # -- parser events ---------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        mapping = {
            name.lower(): (value if value is not None else "")
            for name, value in attrs
        }
        void = tag in VOID_TAGS

        if tag in STRUCTURAL_ALIGN_TAGS:
            # Structure keeps its own alignment: a right-aligned numeric column
            # is the author saying something true. Only the properties that exist
            # purely to justify text are removed.
            self._emit_untouched(tag, mapping, drop_text_align=False)
            if not void:
                self._stack.append((tag, None))
            return

        if tag not in PROSE_ALIGN_TAGS:
            # Inline elements and list containers. Nothing is pinned -- their
            # alignment comes from the block they sit in -- but a source
            # justification is still removed so nothing can inherit one.
            declared = _declared_alignment(mapping)
            self._emit_untouched(
                tag, mapping, drop_text_align=declared in ("justify", "right")
            )
            if not void:
                self._stack.append((tag, None))
            return

        parent = self._open_prose[-1] if self._open_prose else None
        index = len(self._frames)
        self._frames.append(
            {
                "tag": tag,
                "attrs": mapping,
                "own": _declared_alignment(mapping),
                "parent": parent,
                "chunk": len(self._out),
                "length": 0,
                "resolved": None,
            }
        )
        self._out.append("")          # placeholder, patched in result()
        if void:                      # no prose tag is void, but do not assume it
            return
        self._open_prose.append(index)
        self._stack.append((tag, index))

    def _emit_untouched(self, tag: str, mapping: dict, *, drop_text_align: bool) -> None:
        if mapping.get("style"):
            cleaned = strip_alignment_declarations(
                mapping["style"], drop_text_align=drop_text_align
            )
            if cleaned:
                mapping["style"] = cleaned
            else:
                mapping.pop("style")
        if drop_text_align and (mapping.get("align") or "").strip().lower() in (
            "justify", "right"
        ):
            mapping.pop("align")
        self._out.append(self._render(tag, mapping))

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        for position in range(len(self._stack) - 1, -1, -1):
            if self._stack[position][0] == tag:
                for _name, frame_index in self._stack[position:]:
                    if frame_index is not None and frame_index in self._open_prose:
                        self._open_prose.remove(frame_index)
                del self._stack[position:]
                break
        self._out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._count_text(data)
        self._out.append(escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self._count_text(" ")
        self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self._count_text(" ")
        self._out.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:  # comments never survive
        return

    # -- resolution ------------------------------------------------------

    def result(self) -> str:
        limit = self._policy.compact_centre_chars
        for frame in self._frames:
            own = frame["own"]
            parent = frame["parent"]
            if own is None:
                inherited = (
                    self._frames[parent]["resolved"] == "center"
                    if parent is not None
                    else False
                )
                wants_centre = inherited
            else:
                # `justify` and any arbitrary `right` in ordinary prose are the
                # whole point of this policy and are never honoured.
                wants_centre = own == "center"
            frame["resolved"] = (
                "center"
                if wants_centre and frame["length"] <= limit
                else self._policy.prose
            )
            attrs = dict(frame["attrs"])
            style = strip_alignment_declarations(attrs.get("style") or "")
            forced = f"text-align:{frame['resolved']}"
            attrs["style"] = f"{style};{forced}" if style else forced
            attrs.pop("align", None)
            self._out[frame["chunk"]] = self._render(frame["tag"], attrs)
        return "".join(self._out)


def apply_body_alignment_policy(html: str, policy: BodyAlignmentPolicy) -> str:
    """Pin sanitized body copy to the digest's own alignment. Idempotent.

    Runs after `clean()`, so it only ever sees markup a real HTML sanitizer has
    already approved: it edits one attribute per element and never invents,
    reorders or drops a tag.
    """
    if not html or not policy.normalize:
        return html
    parser = _AlignmentTransformer(policy)
    parser.feed(html)
    parser.close()
    return parser.result()


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


def sanitize_body(
    html: str,
    *,
    image_resolver=None,
    color_policy: BodyColorPolicy | None = None,
    alignment_policy: BodyAlignmentPolicy | None = None,
) -> tuple[str, LinkStats, int]:
    """Full pipeline. Returns (safe_html, link_stats, images_seen).

    `color_policy` and `alignment_policy` are the render-time presentation
    passes. Both are deliberately optional and off by default: sanitization
    decides what is *safe*, the policies decide what is *legible inside
    DailyMail's card*, and keeping them separate means the security allowlist can
    be tested without a palette or a viewport in scope.
    """
    prefiltered = prefilter(html)
    rewritten, stats, images = transform(prefiltered, image_resolver=image_resolver)
    safe = tighten(clean(rewritten))
    if color_policy is not None:
        safe = apply_body_color_policy(safe, color_policy)
    if alignment_policy is not None:
        safe = apply_body_alignment_policy(safe, alignment_policy)
    return safe, stats, images


# --- duplicate leading heading ------------------------------------------------
#
# Rowan bodies very often open by repeating their own subject as an <h2>. The
# digest already renders that subject as the card's headline, so the reader sees
# it twice. This removes the second one at RENDER TIME ONLY -- the stored source
# in SQLite is never touched, and `sanitize_body` returns a derivative.
#
# The rule is deliberately narrow. A first block that merely *starts* with the
# title and then says something is kept in full, as is anything carrying an
# image, a link, or any text the headline does not already contain. Losing a
# sentence would be a far worse defect than showing a title twice.

_QUOTES = str.maketrans(
    {"‘": "'", "’": "'", "“": '"', "”": '"',
     "–": "-", "—": "-", " ": " "}
)

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "div"}


def normalize_visible_text(html_or_text: str) -> str:
    """Fold visible text for the duplicate-title comparison.

    Accounts for exactly what the requirement allows: whitespace, HTML entities,
    curly versus straight quotes, case, and trivial trailing punctuation.
    """
    from html import unescape

    text = re.sub(r"<[^>]+>", " ", html_or_text or "")
    text = unescape(text).translate(_QUOTES)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip(" \t.:;,-–—!*")
    return text.casefold()


class _FirstBlock(HTMLParser):
    """Locate the first element-level block and record what it contains."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.start: int | None = None
        self.end: int | None = None
        self.tag: str | None = None
        self.depth = 0
        self.has_image = False
        self.has_link = False
        self.done = False
        self._leading_text: list[str] = []

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._line_starts[line - 1] + column

    def feed(self, data: str) -> None:  # noqa: D102
        self._data = data
        self._line_starts = [0]
        for index, char in enumerate(data):
            if char == "\n":
                self._line_starts.append(index + 1)
        super().feed(data)

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self.start is None:
            if tag in VOID_TAGS and tag not in _HEADING_TAGS:
                # A leading <br> or <img> is not a heading block; stop looking.
                self.done = True
                return
            if tag not in _HEADING_TAGS:
                self.done = True
                return
            self.start = self._offset()
            self.tag = tag
            self.depth = 1
            return
        if tag == "img":
            self.has_image = True
        elif tag == "a":
            self.has_link = True
        if tag not in VOID_TAGS:
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.start is None:
            self.done = True
            return
        if tag == "img":
            self.has_image = True

    def handle_endtag(self, tag):
        if self.done or self.start is None:
            return
        self.depth -= 1
        if self.depth == 0:
            offset = self._offset()
            closing = self._data.find(">", offset)
            self.end = (closing + 1) if closing != -1 else len(self._data)
            self.done = True

    def handle_data(self, data):
        if self.start is None and data.strip():
            self.done = True  # bare text before any block: leave the body alone


def suppress_duplicate_heading(html: str, subject: str) -> tuple[str, bool]:
    """Drop a leading block that only repeats `subject`. Returns `(html, removed)`.

    Operates on already-sanitized markup, so it can never reintroduce anything
    unsafe, and it is a pure function of its inputs.
    """
    if not html or not subject:
        return html or "", False
    target = normalize_visible_text(subject)
    if not target:
        return html, False

    parser = _FirstBlock()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - a malformed body simply keeps its heading
        return html, False

    if parser.start is None or parser.end is None:
        return html, False
    if parser.has_image or parser.has_link:
        return html, False
    if html[: parser.start].strip():
        return html, False  # something meaningful precedes it

    block = html[parser.start : parser.end]
    if normalize_visible_text(block) != target:
        return html, False

    return html[: parser.start] + html[parser.end :], True


def suppress_duplicate_text_heading(text: str, subject: str) -> tuple[str, bool]:
    """The same rule for the plain-text alternative."""
    if not text or not subject:
        return text or "", False
    target = normalize_visible_text(subject)
    if not target:
        return text, False
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if normalize_visible_text(line) == target:
            remaining = lines[index + 1 :]
            while remaining and not remaining[0].strip():
                remaining.pop(0)
            return "\n".join(remaining), True
        return text, False
    return text, False
