"""Outlook-safe inline image handling.

Announcement bodies embed images as `data:` URIs, and Phase 0 measured single
images over 6 MB. Those cannot be shipped as data URIs inside email HTML: Outlook
will not render them and the message would blow past sane size limits.

So each image is decoded, *verified to actually be an image*, EXIF-rotated,
downscaled, compressed to fit a byte budget, and re-attached as a MIME `related`
part referenced by `cid:`. Anything that cannot be decoded, or that would push the
message over the total budget, is replaced with a visible note linking to the
official announcement -- never a broken image and never a failed digest.

This is deliberately not a media pipeline. One decode, one resize, one encode loop.
"""

from __future__ import annotations

import base64
import binascii
import io
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from PIL import Image, ImageOps, UnidentifiedImageError

from .settings import Settings

# Refuse absurd pixel counts before allocating. Pillow's own bomb check is a
# backstop; this is the explicit limit.
MAX_PIXELS = 60_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

_DATA_URI = re.compile(
    r"^data:(?P<mime>image/[a-z0-9.+-]+)?(?P<params>;[^,]*)?,(?P<payload>.*)$",
    re.IGNORECASE | re.DOTALL,
)

SUPPORTED_FORMATS = {"PNG", "JPEG", "GIF", "WEBP", "BMP", "TIFF", "MPO"}

FALLBACK_NOTE = (
    '<p style="margin:8px 0;padding:8px 10px;background:#f6f6f6;'
    'border-left:3px solid #FFCC00;font-size:13px;color:#444;">'
    '<a href="{url}" style="color:#57150B;text-decoration:underline;">'
    "Image available in the official announcement</a></p>"
)


@dataclass
class EmbeddedImage:
    cid: str
    content_type: str
    data: bytes
    width: int
    height: int
    original_bytes: int

    @property
    def final_bytes(self) -> int:
        return len(self.data)


@dataclass
class ImageStats:
    seen: int = 0
    embedded: int = 0
    omitted: int = 0
    original_bytes: int = 0
    final_bytes: int = 0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "seen": self.seen,
            "embedded": self.embedded,
            "omitted": self.omitted,
            "original_bytes": self.original_bytes,
            "final_bytes": self.final_bytes,
            "reasons": list(self.reasons),
        }


def decode_data_uri(src: str) -> bytes | None:
    """Extract raw bytes from a `data:` URI, or None if it is not usable."""
    match = _DATA_URI.match(src.strip())
    if not match:
        return None
    params = (match.group("params") or "").lower()
    payload = match.group("payload")
    if "base64" in params:
        # Editors wrap long data URIs; whitespace is not part of the payload.
        cleaned = "".join(payload.split())
        try:
            decoded = base64.b64decode(cleaned, validate=False)
        except (binascii.Error, ValueError):
            return None
        # b64decode with validate=False silently discards invalid characters, so
        # pure garbage decodes to b"". Treat that as undecodable.
        if cleaned and not decoded:
            return None
        return decoded
    from urllib.parse import unquote_to_bytes

    try:
        return unquote_to_bytes(payload)
    except Exception:
        return None


class ImageProcessor:
    """Turns announcement image references into CID attachments."""

    def __init__(self, settings: Settings, *, http_client=None) -> None:
        self.settings = settings
        self.stats = ImageStats()
        self.images: list[EmbeddedImage] = []
        self._http = http_client
        self._counter = 0
        self._by_source: dict[str, str] = {}

    # -- public API ---------------------------------------------------------

    def resolver_for(self, submission_id: int | str, official_url: str):
        """Build the callback `sanitize.transform` uses for each `<img>`."""

        def resolve(src: str, attrs: dict):
            return self._resolve(src, submission_id, official_url)

        return resolve

    def total_bytes(self) -> int:
        return sum(image.final_bytes for image in self.images)

    # -- internals ----------------------------------------------------------

    def _note(self, official_url: str, reason: str) -> tuple[str, str]:
        self.stats.omitted += 1
        self.stats.reasons.append(reason)
        return ("note", FALLBACK_NOTE.format(url=official_url))

    def _resolve(self, src: str, submission_id, official_url: str):
        lowered = src.strip().lower()
        if lowered.startswith("cid:"):
            return None  # already an attachment reference

        self.stats.seen += 1

        if not lowered:
            # Either the author wrote <img> with no source, or the prefilter
            # removed one with an unsupported scheme (ftp:, data:text/html, ...).
            # Either way the reader should get a pointer to the source.
            return self._note(official_url, "image source unusable or removed")

        # Identical images recur across announcements; reuse the same part.
        if src in self._by_source:
            return ("cid", self._by_source[src])

        if lowered.startswith("data:"):
            raw = decode_data_uri(src)
            if raw is None:
                return self._note(official_url, "undecodable data URI")
        elif lowered.startswith("https://"):
            if not self.settings.image_download_external:
                return self._note(official_url, "external image download disabled")
            raw = self._download(src)
            if raw is None:
                return self._note(official_url, "external image download failed")
        elif lowered.startswith("http://"):
            # Plain HTTP: do not fetch. Link to the source instead.
            return self._note(official_url, "insecure http image source")
        else:
            return self._note(official_url, "unsupported image source")

        self.stats.original_bytes += len(raw)

        if self.total_bytes() >= self.settings.image_max_total_bytes:
            return self._note(official_url, "total image budget exhausted")

        processed = self._process(raw)
        if processed is None:
            return self._note(official_url, "image could not be decoded safely")

        data, width, height = processed
        if self.total_bytes() + len(data) > self.settings.image_max_total_bytes:
            return self._note(official_url, "would exceed total image budget")

        self._counter += 1
        cid = f"img{self._counter}.{submission_id}@dailymail.local"
        image = EmbeddedImage(
            cid=cid,
            content_type="image/jpeg",
            data=data,
            width=width,
            height=height,
            original_bytes=len(raw),
        )
        self.images.append(image)
        self.stats.embedded += 1
        self.stats.final_bytes += len(data)
        self._by_source[src] = cid
        return ("cid", cid)

    def _download(self, url: str) -> bytes | None:
        """Fetch an external image under strict size and time limits."""
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return None
        client = self._http
        close_after = False
        if client is None:
            import httpx

            client = httpx.Client(
                timeout=self.settings.image_external_timeout_seconds,
                follow_redirects=True,
                max_redirects=3,
            )
            close_after = True
        try:
            limit = self.settings.image_external_max_download_bytes
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return None
                content_type = (response.headers.get("content-type") or "").lower()
                if content_type and not content_type.startswith("image/"):
                    return None
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > limit:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except Exception:
            return None
        finally:
            if close_after:
                client.close()

    def _process(self, raw: bytes) -> tuple[bytes, int, int] | None:
        """Verify, orient, downscale and compress. None if not a usable image."""
        try:
            # verify() invalidates the object, so open twice: once to confirm the
            # bytes really are an image, once to actually decode.
            with Image.open(io.BytesIO(raw)) as probe:
                probe.verify()
                fmt = (probe.format or "").upper()
            if fmt not in SUPPORTED_FORMATS:
                return None

            with Image.open(io.BytesIO(raw)) as opened:
                if opened.width * opened.height > MAX_PIXELS:
                    return None
                image = ImageOps.exif_transpose(opened)
                image = image.convert("RGB") if image.mode != "RGB" else image.copy()
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            return None
        except Exception:
            return None

        settings = self.settings
        width_limit = settings.image_max_width_px
        if image.width > width_limit:
            ratio = width_limit / image.width
            image = image.resize(
                (width_limit, max(1, round(image.height * ratio))),
                Image.LANCZOS,
            )

        best = self._encode_within_budget(image)
        if best is None:
            return None
        data, final_image = best
        return data, final_image.width, final_image.height

    def _encode_within_budget(self, image: Image.Image):
        """Drop quality first, then dimensions, until the image fits.

        Quality floor and a minimum width keep flyer text legible rather than
        compressing an announcement into unreadability.
        """
        settings = self.settings
        budget = settings.image_max_bytes
        current = image
        min_width = 600

        while True:
            quality = settings.initial_quality_or_default()
            while quality >= settings.image_min_quality:
                buffer = io.BytesIO()
                current.save(
                    buffer,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                    progressive=True,
                )
                data = buffer.getvalue()
                if len(data) <= budget:
                    return data, current
                quality -= 8

            if current.width <= min_width:
                # Accept the smallest we could make it rather than dropping the
                # image entirely; the total-budget check still guards the message.
                return data, current

            new_width = max(min_width, int(current.width * 0.8))
            ratio = new_width / current.width
            current = current.resize(
                (new_width, max(1, round(current.height * ratio))), Image.LANCZOS
            )
