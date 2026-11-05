"""UK public procurement search over official OCDS APIs.

Sources (Open Government Licence v3.0):

- Find a Tender release packages
  ``https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages``
- Contracts Finder OCDS search
  ``https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search``

No API key and no extra infrastructure. Responses are cached in process.
Uncached calls to each host are spaced apart, and HTTP 429 / 503 / Contracts
Finder's rate-limit 403 open a cooldown instead of being retried in a loop.
Personal contact fields are never copied into paid results.

Innovate UK competition pages are not republished: their terms allow OGL reuse
and also ask publishers to contact Innovate UK first. ``grants_stub`` is the
free v1 placeholder. The Chest is out of scope until each authority's reuse
statement is confirmed.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urljoin, urlparse

import httpx

log = logging.getLogger("uk_tenders")

FTS_API = "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages"
CF_API = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"

LICENCE = "OGL-UK-3.0"
ATTRIBUTION = (
    "Contains public sector information licensed under the "
    "Open Government Licence v3.0."
)
LICENCE_URL = "https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/"

USER_AGENT = (
    "TerradeedLabs-UKTenders/1.0 "
    "(+https://api.terradeed.co.uk; contact@terradeed.co.uk)"
)

DEFAULT_WINDOW_DAYS = 30
UPSTREAM_PAGE_SIZE = 100
MAX_PAGES_BROAD = 1
MAX_PAGES_FILTERED = 2
MAX_LIMIT = 25
DEFAULT_LIMIT = 25
CACHE_TTL_SECONDS = 900
MIN_UPSTREAM_INTERVAL_SECONDS = 1.0
HTTP_TIMEOUT_SECONDS = 20.0
MAX_RESULT_CACHE = 64
MAX_HTTP_CACHE = 128

PUBLIC_FIELDS = (
    "ocid",
    "title",
    "buyer",
    "value_gbp",
    "published",
    "deadline",
    "stage",
    "region_hints",
    "url",
    "source",
)

REGIONS = (
    "north-west",
    "north-east",
    "yorkshire-and-the-humber",
    "east-midlands",
    "west-midlands",
    "east-of-england",
    "london",
    "south-east",
    "south-west",
    "wales",
    "scotland",
    "northern-ireland",
    "england",
    "uk",
)

ENGLAND_REGIONS = {
    "north-west",
    "north-east",
    "yorkshire-and-the-humber",
    "east-midlands",
    "west-midlands",
    "east-of-england",
    "london",
    "south-east",
    "south-west",
}
DEVOLVED_REGIONS = {"wales", "scotland", "northern-ireland"}

STAGES = ("planning", "tender", "award")

# ITL1 / NUTS1 prefixes published on Find a Tender delivery addresses.
NUTS_PREFIX = {
    "UKC": "north-east",
    "UKD": "north-west",
    "UKE": "yorkshire-and-the-humber",
    "UKF": "east-midlands",
    "UKG": "west-midlands",
    "UKH": "east-of-england",
    "UKI": "london",
    "UKJ": "south-east",
    "UKK": "south-west",
    "UKL": "wales",
    "UKM": "scotland",
    "UKN": "northern-ireland",
}

REGION_NAMES = {
    "north west": "north-west",
    "north-west": "north-west",
    "northwest": "north-west",
    "greater manchester": "north-west",
    "merseyside": "north-west",
    "lancashire": "north-west",
    "cheshire": "north-west",
    "cumbria": "north-west",
    "north east": "north-east",
    "north-east": "north-east",
    "northeast": "north-east",
    "yorkshire and the humber": "yorkshire-and-the-humber",
    "yorkshire and humber": "yorkshire-and-the-humber",
    "yorkshire": "yorkshire-and-the-humber",
    "east midlands": "east-midlands",
    "east-midlands": "east-midlands",
    "west midlands": "west-midlands",
    "west-midlands": "west-midlands",
    "east of england": "east-of-england",
    "east-of-england": "east-of-england",
    "london": "london",
    "greater london": "london",
    "south east": "south-east",
    "south-east": "south-east",
    "southeast": "south-east",
    "south west": "south-west",
    "south-west": "south-west",
    "southwest": "south-west",
    "wales": "wales",
    "cymru": "wales",
    "scotland": "scotland",
    "northern ireland": "northern-ireland",
    "england": "england",
}

# Approximate outward-code areas. Used only when a delivery address has no
# region name or NUTS code. Overlapping areas follow the majority region.
POSTCODE_AREAS = {
    "M": "north-west",
    "OL": "north-west",
    "BL": "north-west",
    "SK": "north-west",
    "WN": "north-west",
    "PR": "north-west",
    "BB": "north-west",
    "FY": "north-west",
    "LA": "north-west",
    "L": "north-west",
    "WA": "north-west",
    "CH": "north-west",
    "CW": "north-west",
    "CA": "north-west",
    "NE": "north-east",
    "SR": "north-east",
    "DH": "north-east",
    "DL": "north-east",
    "TS": "north-east",
    "BD": "yorkshire-and-the-humber",
    "DN": "yorkshire-and-the-humber",
    "HD": "yorkshire-and-the-humber",
    "HG": "yorkshire-and-the-humber",
    "HU": "yorkshire-and-the-humber",
    "HX": "yorkshire-and-the-humber",
    "LS": "yorkshire-and-the-humber",
    "S": "yorkshire-and-the-humber",
    "WF": "yorkshire-and-the-humber",
    "YO": "yorkshire-and-the-humber",
    "DE": "east-midlands",
    "LE": "east-midlands",
    "LN": "east-midlands",
    "NG": "east-midlands",
    "NN": "east-midlands",
    "B": "west-midlands",
    "CV": "west-midlands",
    "DY": "west-midlands",
    "WS": "west-midlands",
    "WV": "west-midlands",
    "WR": "west-midlands",
    "ST": "west-midlands",
    "TF": "west-midlands",
    "HR": "west-midlands",
    "AL": "east-of-england",
    "CB": "east-of-england",
    "CM": "east-of-england",
    "CO": "east-of-england",
    "IP": "east-of-england",
    "LU": "east-of-england",
    "NR": "east-of-england",
    "PE": "east-of-england",
    "SG": "east-of-england",
    "SS": "east-of-england",
    "E": "london",
    "EC": "london",
    "N": "london",
    "NW": "london",
    "SE": "london",
    "SW": "london",
    "W": "london",
    "WC": "london",
    "BR": "london",
    "CR": "london",
    "DA": "london",
    "EN": "london",
    "HA": "london",
    "IG": "london",
    "KT": "london",
    "RM": "london",
    "SM": "london",
    "TW": "london",
    "UB": "london",
    "WD": "london",
    "BN": "south-east",
    "CT": "south-east",
    "GU": "south-east",
    "HP": "south-east",
    "ME": "south-east",
    "MK": "south-east",
    "OX": "south-east",
    "PO": "south-east",
    "RG": "south-east",
    "RH": "south-east",
    "SL": "south-east",
    "SO": "south-east",
    "TN": "south-east",
    "BA": "south-west",
    "BH": "south-west",
    "BS": "south-west",
    "DT": "south-west",
    "EX": "south-west",
    "GL": "south-west",
    "PL": "south-west",
    "SN": "south-west",
    "TA": "south-west",
    "TQ": "south-west",
    "TR": "south-west",
    "CF": "wales",
    "LD": "wales",
    "LL": "wales",
    "NP": "wales",
    "SA": "wales",
    "SY": "wales",
    "AB": "scotland",
    "DD": "scotland",
    "DG": "scotland",
    "EH": "scotland",
    "FK": "scotland",
    "G": "scotland",
    "HS": "scotland",
    "IV": "scotland",
    "KA": "scotland",
    "KW": "scotland",
    "KY": "scotland",
    "ML": "scotland",
    "PA": "scotland",
    "PH": "scotland",
    "TD": "scotland",
    "ZE": "scotland",
    "BT": "northern-ireland",
}

EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
# International or UK-leading numbers only, so contract values and year
# ranges in titles are left alone.
PHONE_RE = re.compile(
    r"(?:\+\d{1,3}[\s().-]*)(?:\d[\s().-]*){9,}\d"
    r"|(?:\b0\d[\s().-]*)(?:\d[\s().-]*){8,}\d"
)
POSTCODE_RE = re.compile(r"^([A-Z]{1,2})\d", re.IGNORECASE)
UPDATED_FROM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?Z?$")
CF_NOTICE_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

GRANTS_NOTE = (
    "Innovate UK competition text is not included in v1. The Innovation Funding "
    "Service terms allow Open Government Licence reuse and also ask publishers to "
    "contact Innovate UK before reproducing content "
    "(https://apply-for-innovation-funding.service.gov.uk/info/terms-and-conditions). "
    "The Chest is deferred until each authority's reuse statement is confirmed. "
    "This response is free and contains no notice text. Public search: "
    "https://apply-for-innovation-funding.service.gov.uk/competition/search"
)


class TenderQueryError(ValueError):
    """Caller sent a query the feed will not run."""


class TenderUpstreamError(RuntimeError):
    """Both official OCDS sources failed, so there is nothing to sell."""


def grants_stub() -> dict[str, Any]:
    """Free placeholder. Does not fetch or republish grant text."""
    return {
        "available": False,
        "licence": None,
        "attribution": None,
        "sources": [],
        "results": [],
        "note": GRANTS_NOTE,
    }


_CONTACT_KEYS = {"contactpoint", "email", "telephone", "faxnumber", "phone"}
_PERSON_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]{1,}(?:\s+[A-Za-z][A-Za-z'\-]{1,}){1,4}$")


def _contact_names(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() == "contactpoint" and isinstance(value, dict):
                name = value.get("name")
                if isinstance(name, str) and _PERSON_NAME_RE.fullmatch(name.strip()):
                    found.add(name.strip())
            else:
                _contact_names(value, found)
    elif isinstance(node, list):
        for item in node:
            _contact_names(item, found)


def _scrub_upstream(node: Any) -> Any:
    """Drop contact objects and redact emails, phones, and contact names before caching."""
    names = set()
    _contact_names(node, names)

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: walk(item)
                for key, item in value.items()
                if key.lower() not in _CONTACT_KEYS
            }
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, str):
            text = redact_text(value) or ""
            for name in names:
                text = re.sub(re.escape(name), "[redacted]", text, flags=re.IGNORECASE)
            return text
        return value

    return walk(node)


def redact_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    value = EMAIL_RE.sub("[redacted]", value)
    value = PHONE_RE.sub("[redacted]", value)
    return value


def _gbp(value: Any) -> Optional[int | float]:
    if not isinstance(value, dict):
        return None
    currency = (value.get("currency") or "GBP").upper()
    if currency != "GBP":
        return None
    amount = value.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    if isinstance(amount, float) and amount.is_integer():
        return int(amount)
    return amount


def _value_gbp(release: dict) -> Optional[int | float]:
    tender = release.get("tender") or {}
    amount = _gbp(tender.get("value"))
    if amount is not None:
        return amount
    for collection in (release.get("awards") or [], release.get("contracts") or []):
        for item in collection:
            if isinstance(item, dict):
                amount = _gbp(item.get("value"))
                if amount is not None:
                    return amount
    return None


def _stage(tags: Any) -> Optional[str]:
    lowered = {str(tag).lower() for tag in tags or []}
    if "award" in lowered or "contract" in lowered:
        return "award"
    if "tender" in lowered:
        return "tender"
    if "planning" in lowered:
        return "planning"
    return None


def _postcode_area(postal_code: str) -> Optional[str]:
    compact = postal_code.upper().replace(" ", "")
    match = POSTCODE_RE.match(compact)
    if not match:
        return None
    return match.group(1)


def _hints_from_address(address: dict) -> set[str]:
    hints: set[str] = set()
    region = (address.get("region") or "").strip()
    country = (address.get("countryName") or "").strip()
    postal_code = address.get("postalCode") or ""

    if region:
        compact = region.upper().replace(" ", "")
        if compact in {"UK", "GB", "UK0", "UKE0"} or region.lower() in {
            "united kingdom",
            "great britain",
            "uk",
            "any region",
            "national",
        }:
            hints.add("uk")
        else:
            nuts = compact[:3]
            if nuts in NUTS_PREFIX and (len(compact) == 3 or compact[3:].isalnum()):
                hints.add(NUTS_PREFIX[nuts])
            else:
                named = REGION_NAMES.get(region.lower())
                if named:
                    hints.add(named)

    if not hints and postal_code:
        area = _postcode_area(str(postal_code))
        mapped = POSTCODE_AREAS.get(area or "")
        if mapped:
            hints.add(mapped)

    if not hints and country:
        lowered = country.lower()
        if lowered == "england":
            hints.add("england")
        elif lowered == "scotland":
            hints.add("scotland")
        elif lowered in {"wales", "cymru"}:
            hints.add("wales")
        elif "northern ireland" in lowered:
            hints.add("northern-ireland")
        elif lowered in {"united kingdom", "uk", "great britain"}:
            hints.add("uk")
    return hints


def _delivery_addresses(release: dict) -> list[dict]:
    tender = release.get("tender") or {}
    addresses: list[dict] = []
    for item in tender.get("items") or []:
        if isinstance(item, dict):
            for address in item.get("deliveryAddresses") or []:
                if isinstance(address, dict):
                    addresses.append(address)
    for address in tender.get("deliveryAddresses") or []:
        if isinstance(address, dict):
            addresses.append(address)
    return addresses


def _buyer_party(release: dict) -> Optional[dict]:
    buyer = release.get("buyer") or {}
    buyer_id = buyer.get("id")
    fallback = None
    for party in release.get("parties") or []:
        if not isinstance(party, dict):
            continue
        roles = {str(role).lower() for role in party.get("roles") or []}
        if buyer_id and party.get("id") == buyer_id:
            return party
        if "buyer" in roles and fallback is None:
            fallback = party
    return fallback


def _buyer_name(release: dict) -> Optional[str]:
    party = _buyer_party(release)
    buyer = release.get("buyer") or {}
    name = None
    if party:
        identifier = party.get("identifier") or {}
        name = identifier.get("legalName") or party.get("name")
    if not name:
        name = buyer.get("name")
    if not isinstance(name, str):
        return None
    return redact_text(name.strip()) or None


def _region_hints(release: dict) -> list[str]:
    addresses = _delivery_addresses(release)
    if not addresses:
        party = _buyer_party(release)
        address = (party or {}).get("address")
        if isinstance(address, dict):
            addresses = [address]
    hints: set[str] = set()
    for address in addresses:
        hints.update(_hints_from_address(address))
    return sorted(hints)


def _notice_url(source: str, release: dict) -> str:
    release_id = str(release.get("id") or "")
    if source == "find-tender":
        return f"https://www.find-tender.service.gov.uk/Notice/{release_id}"
    match = CF_NOTICE_RE.match(release_id)
    notice_id = match.group(1) if match else release_id
    return f"https://www.contractsfinder.service.gov.uk/Notice/{notice_id}"


def normalise_release(source: str, release: dict) -> Optional[dict]:
    """Map one OCDS release to the public card. Returns None if it has no stage."""
    if not isinstance(release, dict):
        return None
    ocid = release.get("ocid")
    if not isinstance(ocid, str) or not ocid:
        return None
    stage = _stage(release.get("tag"))
    if stage is None:
        return None
    tender = release.get("tender") or {}
    title = tender.get("title") if isinstance(tender, dict) else None
    if not isinstance(title, str) or not title.strip():
        title = "Untitled notice"
    description = ""
    if isinstance(tender, dict) and isinstance(tender.get("description"), str):
        description = tender["description"]
    deadline = None
    if isinstance(tender, dict):
        period = tender.get("tenderPeriod") or {}
        if isinstance(period, dict) and isinstance(period.get("endDate"), str):
            deadline = period["endDate"]
    published = release.get("date") if isinstance(release.get("date"), str) else None
    public_title = redact_text(title.strip()) or "Untitled notice"
    buyer = _buyer_name(release)
    card = {
        "ocid": ocid,
        "title": public_title,
        "buyer": buyer,
        "value_gbp": _value_gbp(release),
        "published": published,
        "deadline": deadline,
        "stage": stage,
        "region_hints": _region_hints(release),
        "url": _notice_url(source, release),
        "source": source,
        "_haystack": " ".join(
            part
            for part in (
                public_title,
                buyer or "",
                redact_text(description) or "",
            )
            if part
        ).lower(),
    }
    return card


def _matches_query(card: dict, q: Optional[str]) -> bool:
    if not q:
        return True
    haystack = card.get("_haystack") or ""
    return all(token in haystack for token in q.lower().split())


def _matches_region(hints: list[str], region: str) -> bool:
    if region == "uk":
        return True
    hint_set = set(hints)
    if "uk" in hint_set:
        return True
    if region == "england":
        if "england" in hint_set or hint_set & ENGLAND_REGIONS:
            return True
        return False
    return region in hint_set


def _public_card(card: dict) -> dict:
    return {field: card[field] for field in PUBLIC_FIELDS}


def normalise_updated_from(value: str) -> str:
    text = value.strip()
    if not UPDATED_FROM_RE.fullmatch(text):
        raise TenderQueryError(
            "updated_from must be YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"
        )
    if text.endswith("Z"):
        text = text[:-1]
    if "T" not in text:
        text = text + "T00:00:00"
    # Reject impossible dates such as 2026-13-40 without accepting other shapes.
    try:
        datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise TenderQueryError(
            "updated_from must be YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS"
        ) from exc
    return text


def _freeze_updated_to(now: datetime) -> str:
    minute = (now.minute // 15) * 15
    floored = now.replace(minute=minute, second=0, microsecond=0)
    return floored.strftime("%Y-%m-%dT%H:%M:%S")


def _default_updated_from(updated_to: str) -> str:
    moment = datetime.strptime(updated_to, "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    day = (moment - timedelta(days=DEFAULT_WINDOW_DAYS)).date()
    return f"{day.isoformat()}T00:00:00"


def _encode_cursor(offset: int, query_hash: str, updated_to: str) -> str:
    raw = json.dumps(
        {"o": offset, "h": query_hash, "t": updated_to},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
        raise TenderQueryError("Invalid cursor")
    padded = cursor + ("=" * (-len(cursor) % 4))
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TenderQueryError("Invalid cursor") from exc
    if not isinstance(payload, dict):
        raise TenderQueryError("Invalid cursor")
    offset = payload.get("o")
    query_hash = payload.get("h")
    updated_to = payload.get("t")
    if not isinstance(offset, int) or offset < 0:
        raise TenderQueryError("Invalid cursor")
    if not isinstance(query_hash, str) or not query_hash:
        raise TenderQueryError("Invalid cursor")
    if not isinstance(updated_to, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", updated_to
    ):
        raise TenderQueryError("Invalid cursor")
    return payload


def _query_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class TenderService:
    """Search Find a Tender and Contracts Finder without hammering either host."""

    def __init__(
        self,
        *,
        transport: Optional[httpx.BaseTransport] = None,
        min_interval: float = MIN_UPSTREAM_INTERVAL_SECONDS,
        cache_ttl: float = CACHE_TTL_SECONDS,
        clock: Optional[Callable[[], datetime]] = None,
        mono: Optional[Callable[[], float]] = None,
    ):
        self._transport = transport
        self._min_interval = min_interval
        self._cache_ttl = cache_ttl
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._mono = mono or time.monotonic
        self._lock = asyncio.Lock()
        self._last_request: dict[str, float] = {}
        self._cooldown_until: dict[str, float] = {}
        self._http_cache: dict[str, tuple[float, dict]] = {}
        self._result_cache: dict[str, dict] = {}

    async def search(
        self,
        *,
        q: Optional[str] = None,
        region: Optional[str] = None,
        updated_from: Optional[str] = None,
        stage: Optional[str] = None,
        limit: int = DEFAULT_LIMIT,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        params = self._validate(
            q=q,
            region=region,
            updated_from=updated_from,
            stage=stage,
            limit=limit,
            cursor=cursor,
        )
        now = self._mono()
        if params["cursor"] is not None:
            entry = self._result_cache.get(params["cache_key"])
            if entry is None or entry["expires"] <= now:
                raise TenderQueryError(
                    "Cursor expired; repeat the search without a cursor"
                )
            offset = params["cursor"]["o"]
        else:
            entry = self._result_cache.get(params["cache_key"])
            if entry is None or entry["expires"] <= now:
                entry = await self._collect(params)
                entry["expires"] = now + self._cache_ttl
                self._store_results(params["cache_key"], entry)
            offset = 0

        page = entry["results"][offset : offset + params["limit"]]
        next_offset = offset + len(page)
        next_cursor = None
        if next_offset < len(entry["results"]):
            next_cursor = _encode_cursor(
                next_offset, params["query_hash"], params["updated_to"]
            )
        return {
            "licence": LICENCE,
            "attribution": ATTRIBUTION,
            "licence_url": LICENCE_URL,
            "sources": entry["sources"],
            "warnings": entry["warnings"],
            "query": {
                "q": params["q"],
                "region": params["region"],
                "updated_from": params["updated_from"],
                "updated_to": params["updated_to"],
                "stage": params["stage"],
                "limit": params["limit"],
            },
            "coverage": {
                "truncated": entry["truncated"],
                "pages_fetched": entry["pages_fetched"],
            },
            "results": page,
            "next_cursor": next_cursor,
        }

    def _validate(
        self,
        *,
        q: Optional[str],
        region: Optional[str],
        updated_from: Optional[str],
        stage: Optional[str],
        limit: int,
        cursor: Optional[str],
    ) -> dict[str, Any]:
        if q is not None:
            q = q.strip()
            if len(q) > 200:
                raise TenderQueryError("q must be 200 characters or fewer")
            if not q:
                q = None
        region_key = (region or "uk").strip().lower()
        if region_key not in REGIONS:
            raise TenderQueryError(
                "region must be one of: " + ", ".join(REGIONS)
            )
        stage_key = stage.strip().lower() if isinstance(stage, str) and stage.strip() else None
        if stage_key is not None and stage_key not in STAGES:
            raise TenderQueryError("stage must be tender, award, or planning")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise TenderQueryError("limit must be an integer from 1 to 25")
        if limit < 1 or limit > MAX_LIMIT:
            raise TenderQueryError("limit must be an integer from 1 to 25")

        decoded = _decode_cursor(cursor) if cursor else None
        if decoded is not None:
            updated_to = decoded["t"]
        else:
            updated_to = _freeze_updated_to(self._clock())

        # Cursor searches reuse the updated_to stored in the cursor. Omitting
        # updated_from resolves the same default window as the original search.
        if updated_from:
            updated_from_key = normalise_updated_from(updated_from)
        else:
            updated_from_key = _default_updated_from(updated_to)

        cache_key = json.dumps(
            {
                "q": q,
                "region": region_key,
                "updated_from": updated_from_key,
                "updated_to": updated_to,
                "stage": stage_key,
            },
            sort_keys=True,
        )
        query_hash = _query_hash(cache_key)
        if decoded is not None and decoded["h"] != query_hash:
            # The first search may have used an explicit updated_from that the
            # follow-up call also has to send. A mismatch here is a different query.
            raise TenderQueryError("Cursor does not match this query")

        return {
            "q": q,
            "region": region_key,
            "updated_from": updated_from_key,
            "updated_to": updated_to,
            "stage": stage_key,
            "limit": limit,
            "cursor": decoded,
            "cache_key": cache_key,
            "query_hash": query_hash,
            "local_filter": bool(q) or region_key != "uk",
        }

    def _store_results(self, key: str, entry: dict) -> None:
        self._result_cache[key] = entry
        if len(self._result_cache) <= MAX_RESULT_CACHE:
            return
        now = self._mono()
        expired = [item for item, value in self._result_cache.items() if value["expires"] <= now]
        for item in expired:
            self._result_cache.pop(item, None)
        while len(self._result_cache) > MAX_RESULT_CACHE:
            self._result_cache.pop(next(iter(self._result_cache)))

    async def _collect(self, params: dict) -> dict:
        stages = params["stage"] or "planning,tender,award"
        max_pages = MAX_PAGES_FILTERED if params["local_filter"] else MAX_PAGES_BROAD
        fts_url = FTS_API + "?" + urlencode(
            {
                "updatedFrom": params["updated_from"],
                "updatedTo": params["updated_to"],
                "stages": stages,
                "limit": UPSTREAM_PAGE_SIZE,
            }
        )
        cf_url = CF_API + "?" + urlencode(
            {
                "publishedFrom": params["updated_from"],
                "publishedTo": params["updated_to"],
                "stages": stages,
                "limit": UPSTREAM_PAGE_SIZE,
            }
        )
        fts, cf = await asyncio.gather(
            self._fetch_source("find-tender", fts_url, max_pages),
            self._fetch_source("contracts-finder", cf_url, max_pages),
        )
        warnings: list[str] = []
        sources: list[str] = []
        cards: list[dict] = []
        truncated = False
        pages_fetched: dict[str, int] = {}
        for name, outcome in (("find-tender", fts), ("contracts-finder", cf)):
            pages_fetched[name] = outcome["pages"]
            if outcome["error"]:
                warnings.append(outcome["error"])
                if outcome["truncated"]:
                    truncated = True
                continue
            sources.append(name)
            truncated = truncated or outcome["truncated"]
            for release in outcome["releases"]:
                card = normalise_release(name, release)
                if card is None:
                    continue
                if params["stage"] and card["stage"] != params["stage"]:
                    continue
                if not _matches_region(card["region_hints"], params["region"]):
                    continue
                if not _matches_query(card, params["q"]):
                    continue
                cards.append(card)

        if not sources:
            detail = "; ".join(warnings) or "upstream unavailable"
            raise TenderUpstreamError(
                "Find a Tender and Contracts Finder were unavailable: " + detail
            )

        deduped: dict[str, dict] = {}
        for card in cards:
            previous = deduped.get(card["ocid"])
            if previous is None or (card["published"] or "") >= (previous["published"] or ""):
                deduped[card["ocid"]] = card
        ordered = sorted(
            deduped.values(),
            key=lambda card: card["published"] or "",
            reverse=True,
        )
        return {
            "results": [_public_card(card) for card in ordered],
            "sources": sources,
            "warnings": warnings,
            "truncated": truncated,
            "pages_fetched": pages_fetched,
        }

    async def _fetch_source(self, source: str, first_url: str, max_pages: int) -> dict:
        releases: list[dict] = []
        url: Optional[str] = first_url
        pages = 0
        truncated = False
        try:
            while url and pages < max_pages:
                package = await self._get_json(url)
                batch = package.get("releases") or []
                if isinstance(batch, list):
                    releases.extend(item for item in batch if isinstance(item, dict))
                pages += 1
                link = (package.get("links") or {}).get("next")
                if isinstance(link, str) and link:
                    if pages >= max_pages:
                        truncated = True
                        url = None
                    else:
                        url = urljoin(url, link)
                else:
                    url = None
        except _RateLimited:
            return {
                "releases": [],
                "pages": pages,
                "truncated": True,
                "error": f"{source} rate limited; retry later",
            }
        except _UpstreamHTTP as exc:
            return {
                "releases": [],
                "pages": pages,
                "truncated": False,
                "error": f"{source} unavailable ({exc})",
            }
        return {
            "releases": releases,
            "pages": pages,
            "truncated": truncated,
            "error": None,
        }

    async def _get_json(self, url: str) -> dict:
        now = self._mono()
        cached = self._http_cache.get(url)
        if cached and cached[0] > now:
            return cached[1]

        host = urlparse(url).netloc
        if self._cooldown_until.get(host, 0) > now:
            raise _RateLimited(host)
        await self._pace(host)

        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=HTTP_TIMEOUT_SECONDS,
            headers=headers,
            follow_redirects=False,
        ) as client:
            response = await client.get(url)
        log.info("uk tenders upstream %s %s", response.status_code, host)

        if response.status_code in (429, 503) or (
            response.status_code == 403 and "contractsfinder" in host
        ):
            self._cooldown_until[host] = self._mono() + self._retry_after(response, host)
            raise _RateLimited(host)
        if response.status_code != 200:
            raise _UpstreamHTTP(f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise _UpstreamHTTP("malformed JSON") from exc
        if not isinstance(payload, dict):
            raise _UpstreamHTTP("malformed JSON")
        payload = _scrub_upstream(payload)
        self._remember_http(url, payload)
        return payload

    def _remember_http(self, url: str, payload: dict) -> None:
        self._http_cache[url] = (self._mono() + self._cache_ttl, payload)
        if len(self._http_cache) <= MAX_HTTP_CACHE:
            return
        now = self._mono()
        expired = [item for item, (expiry, _) in self._http_cache.items() if expiry <= now]
        for item in expired:
            self._http_cache.pop(item, None)
        while len(self._http_cache) > MAX_HTTP_CACHE:
            self._http_cache.pop(next(iter(self._http_cache)))

    async def _pace(self, host: str) -> None:
        async with self._lock:
            now = self._mono()
            wait = self._min_interval - (now - self._last_request.get(host, 0.0))
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request[host] = self._mono()

    @staticmethod
    def _retry_after(response: httpx.Response, host: str) -> float:
        default = 300.0 if "contractsfinder" in host else 60.0
        raw = response.headers.get("Retry-After")
        if not raw:
            return default
        try:
            seconds = float(raw)
        except ValueError:
            return default
        if seconds < 0:
            return default
        return min(seconds, 300.0)


class _RateLimited(Exception):
    pass


class _UpstreamHTTP(Exception):
    pass
