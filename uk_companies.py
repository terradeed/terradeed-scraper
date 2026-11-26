"""UK company lookups from the Companies House public register.

Source: Companies House "Find and update company information" service
``https://find-and-update.company-information.service.gov.uk``. Companies
House imposes no rules on how public register information is reused; the
reuser stays responsible for data protection law.

Company-level data only. Officer names, people with significant control,
dates of birth, and officer filing PDFs are never returned. Filing
descriptions that name a person are replaced with a neutral label.

No API key and no extra infrastructure. Pages are cached in process, calls to
the host are spaced apart, and HTTP 429 / 503 opens a cooldown instead of a
retry loop.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote_plus

import httpx

log = logging.getLogger("uk_companies")

CH_BASE = "https://find-and-update.company-information.service.gov.uk"
SOURCE = "companies-house"
LICENCE = "Companies-House-public-register"
LICENCE_NOTE = (
    "Companies House imposes no rules on reuse of public register information. "
    "Users remain responsible for data protection and other law."
)
ATTRIBUTION = "Source: Companies House public register."
PERSONAL_DATA_NOTE = (
    "Company-level data only. Officer and PSC names, dates of birth, and "
    "officer filing documents are omitted."
)
USER_AGENT = (
    "TerradeedLabs-UKCompanies/1.0 "
    "(+https://api.terradeed.co.uk; contact@terradeed.co.uk)"
)

MAX_FILINGS = 25
DEFAULT_FILINGS = 10
MAX_SEARCH_LIMIT = 20
DEFAULT_SEARCH_LIMIT = 10
PAGE_CACHE_TTL_SECONDS = 3600
SEARCH_CACHE_TTL_SECONDS = 900
MIN_UPSTREAM_INTERVAL_SECONDS = 0.5
HTTP_TIMEOUT_SECONDS = 15.0
MAX_HTTP_CACHE = 256

_NUMBER_RE = re.compile(r"^(?:[A-Z]{2}\d{6}|\d{8}|[A-Z]\d{7})$")

# Filing types that record a named officer, member, or person with significant
# control. Their descriptions and documents are never republished.
_OFFICER_TYPE_RE = re.compile(
    r"^(?:LL)?(?:AP0\d|TM0\d|CH0\d|PSC\d{2}|PSC0\d|RP0[4-9]|288[ABC]?|DISQ)",
    re.IGNORECASE,
)
_PSC_TYPE_RE = re.compile(r"^(?:LL)?PSC", re.IGNORECASE)
_PERSONAL_PHRASES = re.compile(
    r"as a (?:director|secretary|member|designated member|manager|judicial factor)"
    r"|termination of appointment|appointment of|director'?s details"
    r"|secretary'?s details|member'?s details|significant control"
    r"|change of particulars for",
    re.IGNORECASE,
)


class CompanyQueryError(ValueError):
    """Bad input. Maps to HTTP 400 and is never charged."""


class CompanyNotFound(LookupError):
    """No such company number. Maps to HTTP 404 and is never charged."""


class CompanyUpstreamError(RuntimeError):
    """Companies House unavailable or unreadable. Maps to HTTP 502."""


# ---------------------------------------------------------------------------
# Parsing helpers (pure, no network)
# ---------------------------------------------------------------------------

def _text(fragment: Optional[str]) -> Optional[str]:
    if fragment is None:
        return None
    stripped = re.sub(r"<[^>]+>", " ", fragment)
    value = " ".join(html_lib.unescape(stripped).split())
    return value or None


def _by_id(page: str, element_id: str) -> Optional[str]:
    match = re.search(
        rf'id="{re.escape(element_id)}"[^>]*>(.*?)</(?:span|dd|dt|strong|p|td|h1)>',
        page,
        re.S,
    )
    return _text(match.group(1)) if match else None


def _iso_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def normalise_company_number(value: Optional[str]) -> str:
    """Upper-case, strip spaces, and zero-pad short numeric or prefixed numbers."""
    if value is None or not str(value).strip():
        raise CompanyQueryError("number is required, for example 00445790 or SC540749")
    raw = re.sub(r"\s+", "", str(value)).upper()
    if raw.isdigit() and len(raw) < 8:
        raw = raw.zfill(8)
    else:
        match = re.fullmatch(r"([A-Z]{2})(\d{1,6})", raw)
        if match:
            raw = match.group(1) + match.group(2).zfill(6)
    if not _NUMBER_RE.match(raw):
        raise CompanyQueryError(
            "number must be a Companies House company number, for example 00445790 or SC540749"
        )
    return raw


def _panel(page: str) -> str:
    start = page.find("govuk-tabs__panel")
    if start < 0:
        return page
    end = page.find("<!-- Piwik -->", start)
    return page[start:end if end > start else len(page)]


def _column(panel: str, heading_pattern: str) -> Optional[str]:
    for block in panel.split('<div class="column-half">')[1:]:
        if re.search(heading_pattern, block, re.S | re.I):
            return block
    return None


def _strong_after(block: str, label: str) -> Optional[str]:
    match = re.search(rf"{label}\s*(?:<br>\s*)?<strong>(.*?)</strong>", block, re.S | re.I)
    return _iso_date(_text(match.group(1))) if match else None


def _accounts_block(panel: str) -> Optional[dict[str, Any]]:
    block = _column(panel, r"<strong>Accounts</strong>|Accounts overdue")
    if block is None:
        return None
    return {
        "next_made_up_to": (
            _strong_after(block, "Next accounts made up to")
            or _strong_after(block, "First accounts made up to")
        ),
        "next_due": _strong_after(block, "due by"),
        "last_made_up_to": _strong_after(block, "Last accounts made up to"),
        "overdue": "overdue" in block.lower(),
    }


def _confirmation_block(panel: str) -> Optional[dict[str, Any]]:
    block = _column(panel, r"Confirmation statement|Annual return")
    if block is None:
        return None
    return {
        "next_made_up_to": (
            _strong_after(block, "Next statement date")
            or _strong_after(block, "First statement date")
        ),
        "next_due": _strong_after(block, "due by"),
        "last_made_up_to": (
            _strong_after(block, "Last statement dated")
            or _strong_after(block, "Last annual return made up to")
        ),
        "overdue": "overdue" in block.lower(),
    }


def parse_company_overview(page: str) -> Optional[dict[str, Any]]:
    """Company overview page to a company-level record. None when no company is shown."""
    name_match = re.search(r'<h1 class="heading-xlarge"[^>]*>(.*?)</h1>', page, re.S)
    number_match = re.search(r'id="company-number">.*?<strong>(.*?)</strong>', page, re.S)
    if not name_match or not number_match:
        return None
    panel = _panel(page)

    status = _by_id(panel, "company-status-detail")
    status_block = re.search(r'id="company-status">(.*?)(?:<span|</dd>)', panel, re.S)
    status_main = _text(status_block.group(1)) if status_block else None
    status_detail = status.lstrip("\u2014-  ").strip() if status else None

    birth_label = _by_id(panel, "company-birth-type")
    created = _iso_date(_by_id(panel, "company-creation-date"))
    cessation_label = _by_id(panel, "cessation-date-label")
    cessation = _iso_date(_by_id(panel, "cessation-date"))

    sic_codes = []
    for raw in re.findall(r'id="sic\d+"[^>]*>(.*?)</span>', panel, re.S):
        text = _text(raw) or ""
        code, _, description = text.partition(" - ")
        sic_codes.append({"code": code.strip(), "description": description.strip() or None})

    previous_names = []
    for index in range(1, 100):
        name = _by_id(panel, f"previous-name-{index}")
        if name is None:
            break
        period = _by_id(panel, f"previous-date-{index}") or ""
        start, _, end = period.partition(" - ")
        previous_names.append({
            "name": name,
            "from": _iso_date(start),
            "to": _iso_date(end),
        })

    record = {
        "company_number": _text(number_match.group(1)),
        "name": _text(name_match.group(1)),
        "status": status_main.lower() if status_main else None,
        "status_detail": status_detail or None,
        "type": _by_id(panel, "company-type-value"),
        "date_of_creation": created,
        "date_of_creation_label": birth_label,
        "date_of_cessation": cessation,
        "date_of_cessation_label": cessation_label,
        "registered_office_address": _by_id(panel, "roa-address"),
        "sic_codes": sic_codes,
        "previous_names": previous_names,
        "accounts": _accounts_block(panel),
        "confirmation_statement": _confirmation_block(panel),
    }
    return record


def _filing_category(filing_type: str, description: str) -> str:
    upper = filing_type.upper()
    if _PSC_TYPE_RE.match(upper) or "significant control" in description.lower():
        return "persons-with-significant-control"
    if _OFFICER_TYPE_RE.match(upper) or _PERSONAL_PHRASES.search(description):
        return "officers"
    if upper.startswith(("AA", "LLAA")):
        return "accounts"
    if upper.startswith(("CS0", "AR0", "LLCS", "LLAR")):
        return "confirmation-statement"
    if upper.startswith(("MR0", "MG0", "LLMR")):
        return "charges"
    if upper.startswith(("AD0", "LLAD")):
        return "address"
    if upper.startswith(("SH", "CAP-SS")):
        return "capital"
    if upper.startswith(("RES", "RESOLUTIONS")):
        return "resolution"
    if upper.startswith(("NEWINC", "IN01", "LLIN")):
        return "incorporation"
    if upper.startswith(("LIQ", "AM", "COCOMP", "600", "4.", "2.")):
        return "insolvency"
    if upper.startswith(("DS0", "GAZ", "SOAS")):
        return "dissolution"
    if upper.startswith(("CERTNM", "NM0", "CONNOT")):
        return "change-of-name"
    return "other"


_REDACTED_LABELS = {
    "officers": "Officer filing (name omitted)",
    "persons-with-significant-control": "Person with significant control filing (name omitted)",
}


def parse_filing_history(page: str, limit: int = DEFAULT_FILINGS) -> list[dict[str, Any]]:
    table = re.search(r'id="fhTable".*?</table>', page, re.S)
    if not table:
        return []
    filings = []
    for row in re.findall(r"<tr>\s*<td class=\"nowrap\">(.*?)</tr>", table.group(0), re.S):
        cells = re.split(r"</td>\s*<td[^>]*>", row)
        if len(cells) < 3:
            continue
        date = _iso_date(_text(cells[0]))
        filing_type = (_text(cells[1]) or "").strip()
        description_html = re.sub(r'<span class="govuk-visually-hidden">.*?</span>', "", cells[2], flags=re.S)
        description = _text(description_html) or ""
        category = _filing_category(filing_type, description)
        doc_match = re.search(r'href="(/company/[^"]+/document\?format=pdf[^"]*)"', row)
        entry = {
            "date": date,
            "type": filing_type or None,
            "category": category,
            "description": description or None,
            "document_url": None,
        }
        if category in _REDACTED_LABELS:
            entry["description"] = _REDACTED_LABELS[category]
        elif doc_match:
            entry["document_url"] = CH_BASE + html_lib.unescape(doc_match.group(1))
        filings.append(entry)
        if len(filings) >= limit:
            break
    return filings


def parse_search_results(page: str, limit: int = DEFAULT_SEARCH_LIMIT) -> list[dict[str, Any]]:
    start = page.find("id='results'")
    if start < 0:
        start = page.find('id="results"')
    if start < 0:
        return []
    results = []
    for item in re.findall(r'<li class="type-company">(.*?)</li>', page[start:], re.S):
        link = re.search(r'href="/company/([A-Z0-9]+)"[^>]*>(.*?)</a>', item, re.S)
        if not link:
            continue
        number = link.group(1)
        meta = re.search(r'<p class="meta crumbtrail">(.*?)</p>', item, re.S)
        meta_text = _text(meta.group(1)) if meta else ""
        previous = re.search(r"Matching previous names:.*?<span>(.*?)</span>", item, re.S)
        paragraphs = re.findall(r"<p>(.*?)</p>", item, re.S)
        address = _text(paragraphs[-1]) if paragraphs else None
        dated = re.search(r"(Incorporated|Dissolved|Registered|Formed|Converted/closed) on (\d{1,2} \w+ \d{4})", meta_text or "")
        incorporated = None
        dissolved = None
        if dated:
            iso = _iso_date(dated.group(2))
            if dated.group(1) == "Dissolved":
                dissolved = iso
            else:
                incorporated = iso
        results.append({
            "company_number": number,
            "name": _text(link.group(2)),
            "date_of_creation": incorporated,
            "date_of_cessation": dissolved,
            "dissolved": dissolved is not None,
            "registered_office_address": address,
            "matched_previous_name": _text(previous.group(1)) if previous else None,
            "url": f"{CH_BASE}/company/{number}",
        })
        if len(results) >= limit:
            break
    return results


def _envelope() -> dict[str, Any]:
    return {
        "source": SOURCE,
        "licence": LICENCE,
        "licence_note": LICENCE_NOTE,
        "attribution": ATTRIBUTION,
        "personal_data": PERSONAL_DATA_NOTE,
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class _RateLimited(Exception):
    pass


class _UpstreamHTTP(Exception):
    pass


class CompanyService:
    """Company profile, filings, and name search without hammering Companies House."""

    def __init__(
        self,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        min_interval: float = MIN_UPSTREAM_INTERVAL_SECONDS,
        clock: Optional[Callable[[], datetime]] = None,
        mono: Optional[Callable[[], float]] = None,
    ):
        self._transport = transport
        self._min_interval = min_interval
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._mono = mono or time.monotonic
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._cooldown_until = 0.0
        self._cache: dict[str, tuple[float, Optional[str]]] = {}

    async def lookup(self, *, number: Optional[str], filings: int = DEFAULT_FILINGS) -> dict[str, Any]:
        company_number = normalise_company_number(number)
        try:
            filings = int(filings)
        except (TypeError, ValueError) as exc:
            raise CompanyQueryError("filings must be an integer from 0 to 25") from exc
        if filings < 0 or filings > MAX_FILINGS:
            raise CompanyQueryError("filings must be an integer from 0 to 25")

        overview_url = f"{CH_BASE}/company/{company_number}"
        page = await self._get(overview_url, PAGE_CACHE_TTL_SECONDS)
        if page is None:
            raise CompanyNotFound(f"No company found with number {company_number}")
        company = parse_company_overview(page)
        if company is None:
            raise CompanyUpstreamError("Companies House page could not be read")

        filing_rows: list[dict[str, Any]] = []
        filings_status = "skipped"
        if filings:
            try:
                history = await self._get(f"{overview_url}/filing-history", PAGE_CACHE_TTL_SECONDS)
                filing_rows = parse_filing_history(history or "", filings)
                filings_status = "ok"
            except CompanyUpstreamError:
                filings_status = "unavailable"

        result = _envelope()
        result.update({
            "company": company,
            "filings": filing_rows,
            "filings_status": filings_status,
            "source_url": overview_url,
            "retrieved_at": self._clock().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        })
        return result

    async def search(self, *, q: Optional[str], limit: int = DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
        query = " ".join((q or "").split())
        if len(query) < 2:
            raise CompanyQueryError("q is required: a company name or number, at least 2 characters")
        if len(query) > 160:
            raise CompanyQueryError("q must be 160 characters or fewer")
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise CompanyQueryError("limit must be an integer from 1 to 20") from exc
        if limit < 1 or limit > MAX_SEARCH_LIMIT:
            raise CompanyQueryError("limit must be an integer from 1 to 20")

        url = f"{CH_BASE}/search/companies?q={quote_plus(query)}"
        page = await self._get(url, SEARCH_CACHE_TTL_SECONDS)
        results = parse_search_results(page or "", limit)
        payload = _envelope()
        payload.update({
            "query": {"q": query, "limit": limit},
            "results": results,
            "source_url": url,
            "retrieved_at": self._clock().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        })
        return payload

    async def _get(self, url: str, ttl: float) -> Optional[str]:
        """Page HTML, or None for a 404. Raises CompanyUpstreamError otherwise."""
        now = self._mono()
        cached = self._cache.get(url)
        if cached and cached[0] > now:
            return cached[1]
        if self._cooldown_until > now:
            raise CompanyUpstreamError("Companies House is rate limiting; try again shortly")
        await self._pace()
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=HTTP_TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise CompanyUpstreamError(f"Companies House unavailable: {type(exc).__name__}") from exc
        log.info("uk companies upstream %s", response.status_code)
        if response.status_code in (429, 503):
            self._cooldown_until = self._mono() + 60.0
            raise CompanyUpstreamError("Companies House is rate limiting; try again shortly")
        if response.status_code == 404:
            self._remember(url, ttl, None)
            return None
        if response.status_code != 200:
            raise CompanyUpstreamError(f"Companies House returned HTTP {response.status_code}")
        text = response.text
        self._remember(url, ttl, text)
        return text

    def _remember(self, url: str, ttl: float, value: Optional[str]) -> None:
        self._cache[url] = (self._mono() + ttl, value)
        if len(self._cache) <= MAX_HTTP_CACHE:
            return
        now = self._mono()
        for key in [k for k, (expiry, _) in self._cache.items() if expiry <= now]:
            self._cache.pop(key, None)
        while len(self._cache) > MAX_HTTP_CACHE:
            self._cache.pop(next(iter(self._cache)))

    async def _pace(self) -> None:
        async with self._lock:
            now = self._mono()
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = self._mono()
