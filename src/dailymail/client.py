"""HTTP access to the one endpoint DailyMail needs: `ActionGetHomeData`.

Read-only by construction: this module can build exactly one request shape, and
that request is a data-fetch screen action. None of Rowan's write endpoints
(Phase 0 §4.3) are reachable from here, and detail pages are never fetched --
which also avoids triggering Rowan's own `ActionSaveVisitorClicks` write.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass

import httpx

from . import config, validate
from .discovery import RuntimeVersions
from .errors import TlsError, TransportError, ValidationError
from .validate import ParsedResponse


@dataclass
class AudienceResult:
    audience: str
    total_count: int
    announcements: list[dict]
    categories: list[dict]
    pages_fetched: int
    module_version_changed: bool


def build_http_client(ssl_context: ssl.SSLContext) -> httpx.Client:
    return httpx.Client(
        verify=ssl_context,
        timeout=config.HTTP_TIMEOUT_SECONDS,
        headers={"User-Agent": config.USER_AGENT},
        follow_redirects=False,
    )


def _request_body(
    versions: RuntimeVersions,
    *,
    audience: str,
    target_date: str,
    start_index: int,
    max_records: int,
) -> dict:
    """The exact payload shape observed in Phase 0 §4.1."""
    return {
        "versionInfo": {
            "moduleVersion": versions.module_version,
            "apiVersion": versions.api_version,
        },
        "viewName": config.VIEW_NAME,
        "inputParameters": {
            "Filters": {
                "Audience": audience,
                "StartDate": target_date,
                # Sentinel selects single-day mode rather than a range query.
                "EndDate": config.SINGLE_DAY_END_DATE_SENTINEL,
                "CategoryIds": {"List": [], "EmptyListItem": {}},
                "SelectedCategories": {"List": [], "EmptyListItem": 0},
                "keywords": "",
            },
            "StartIndex": start_index,
            "MaxRecords": max_records,
            "IsCategoryUpdate": False,
        },
    }


class AnnouncerClient:
    def __init__(self, http: httpx.Client, versions: RuntimeVersions) -> None:
        self._http = http
        self._versions = versions

    def fetch_page(
        self,
        *,
        audience: str,
        target_date: str,
        start_index: int,
        max_records: int,
    ) -> ParsedResponse:
        if audience not in config.REQUEST_AUDIENCES:
            # Defence in depth: Rowan answers an unknown audience with a
            # misleading partial result instead of an error, so we refuse to put
            # an unrecognised value on the wire at all.
            raise ValidationError(
                f"refusing to request audience {audience!r}; allowed values are "
                f"{list(config.REQUEST_AUDIENCES)}"
            )
        if max_records <= 0:
            raise ValidationError("MaxRecords must be positive")

        url = f"{config.BASE_URL}/{config.GET_HOME_DATA_PATH}"
        body = _request_body(
            self._versions,
            audience=audience,
            target_date=target_date,
            start_index=start_index,
            max_records=max_records,
        )
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
            # Presence is what Rowan checks; the value is a public OutSystems
            # platform constant discovered at runtime.
            "X-CSRFToken": self._versions.csrf_token,
        }

        try:
            response = self._http.post(url, json=body, headers=headers)
        except ssl.SSLError as exc:
            raise TlsError(f"TLS failure contacting Rowan: {exc}") from exc
        except httpx.ConnectError as exc:
            # httpx wraps certificate problems in ConnectError.
            if isinstance(exc.__cause__, ssl.SSLError) or "certificate" in str(exc).lower():
                raise TlsError(f"TLS failure contacting Rowan: {exc}") from exc
            raise TransportError(f"connection failed: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransportError(f"request failed: {type(exc).__name__}: {exc}") from exc

        try:
            payload = response.json()
        except ValueError as exc:
            raise TransportError(
                f"response was not parseable JSON (HTTP {response.status_code}, "
                f"{len(response.content)} bytes)"
            ) from exc

        return validate.check_structural(
            payload,
            http_status=response.status_code,
            content_type=response.headers.get("content-type", ""),
            context=f"{audience} {target_date} @{start_index}",
        )

    def fetch_audience(
        self, *, audience: str, target_date: str, page_size: int = config.PAGE_SIZE
    ) -> AudienceResult:
        """Page until the unique record count matches the server's TotalCount.

        Phase 0 showed a single large `MaxRecords` returns everything, but we
        page for real so no day can silently exceed a hardcoded window.
        """
        collected: dict[str, dict] = {}
        categories: list[dict] = []
        total_count: int | None = None
        module_version_changed = False
        pages = 0
        start_index = 0

        while True:
            parsed = self.fetch_page(
                audience=audience,
                target_date=target_date,
                start_index=start_index,
                max_records=page_size,
            )
            pages += 1
            module_version_changed = module_version_changed or parsed.module_version_changed

            if total_count is None:
                total_count = parsed.total_count
                categories = parsed.categories
            elif parsed.total_count != total_count:
                raise ValidationError(
                    f"V2 [{audience}]: TotalCount changed mid-pagination "
                    f"({total_count} -> {parsed.total_count}); the upstream dataset "
                    "moved while we were reading it. Re-run."
                )

            before = len(collected)
            page_ids: set[str] = set()
            for record in parsed.announcements:
                submission_id = record.get("Submission", {}).get("Id")
                if submission_id is None:
                    raise ValidationError(
                        f"V6 [{audience}]: a record has no Submission.Id"
                    )
                key = str(submission_id)
                if key in page_ids:
                    # Report rather than absorb: silently de-duplicating here
                    # would make the uniqueness guarantee definitional instead
                    # of verified, and could mask a short page.
                    raise ValidationError(
                        f"V6 [{audience}]: SubmissionId {key} appeared twice within "
                        f"the page at StartIndex={start_index}"
                    )
                page_ids.add(key)
                collected.setdefault(key, record)

            if len(collected) >= total_count:
                break
            if not parsed.announcements:
                raise ValidationError(
                    f"V2 [{audience}]: page at StartIndex={start_index} returned no "
                    f"records but only {len(collected)} of {total_count} have been "
                    "collected"
                )
            if len(collected) == before:
                raise ValidationError(
                    f"V2 [{audience}]: page at StartIndex={start_index} added no new "
                    f"records ({len(collected)}/{total_count} collected); pagination "
                    "is not advancing"
                )

            start_index += page_size
            if pages >= config.MAX_PAGES:
                raise ValidationError(
                    f"V2 [{audience}]: exceeded {config.MAX_PAGES} pages while "
                    f"collecting {total_count} records"
                )

        return AudienceResult(
            audience=audience,
            total_count=total_count or 0,
            announcements=list(collected.values()),
            categories=categories,
            pages_fetched=pages,
            module_version_changed=module_version_changed,
        )
