"""
TerraDeed Labs — Property Enrichment Module
Adds automatic enrichment from UK government open data APIs to /extract/property

REQUIRED ENV VARS:
- EPC_API_KEY: from https://epc.opendatacommunities.org (base64 encoded email:apikey)

NO ENV VARS NEEDED FOR:
- Environment Agency flood risk (open, no key)
- Historic England listed buildings (open, no key)
"""

import httpx
import os
import base64
import re
import math
from typing import Optional, Dict, Any


# ============================================================================
# GEOCODING HELPER — extract postcode from address string
# ============================================================================

UK_POSTCODE_REGEX = re.compile(
    r'([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})',
    re.IGNORECASE
)

def extract_postcode(address: str) -> Optional[str]:
    """Extract a UK postcode from an address string."""
    match = UK_POSTCODE_REGEX.search(address)
    if match:
        postcode = match.group(1).upper().strip()
        # Normalise spacing: ensure space before last 3 chars
        if ' ' not in postcode:
            postcode = postcode[:-3] + ' ' + postcode[-3:]
        return postcode
    return None


def _is_failed_response(response: httpx.Response) -> bool:
    """Return True if the upstream response should be treated as a failure."""
    if not response.is_success:
        return True
    try:
        body = response.json()
    except Exception:
        return False
    if isinstance(body, dict) and "error" in body:
        return True
    return False


def _failed_result(source: str, reason: str) -> Dict[str, Any]:
    return {
        "status": "failed",
        "source": source,
        "reason": reason,
    }


def _not_configured_result(source: str, reason: str) -> Dict[str, Any]:
    return {
        "status": "not_configured",
        "source": source,
        "reason": reason,
    }


# ============================================================================
# 1. FLOOD RISK — Environment Agency
# Free, no API key, Open Government Licence
# ============================================================================

async def get_flood_risk(postcode: str) -> Dict[str, Any]:
    """
    Query Environment Agency flood risk data by postcode.
    Returns flood zone classification and risk levels.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            fmp_url = (
                f"https://environment.data.gov.uk/flood-map-for-planning/"
                f"api/v1/summary/postcode/{postcode.replace(' ', '')}"
            )
            response = await client.get(fmp_url)

            if _is_failed_response(response):
                return _failed_result(
                    "environment_agency",
                    f"Flood risk API returned {response.status_code}"
                )

            data = response.json()

            flood_zone = "1"
            river_risk = "very_low"
            surface_water_risk = "low"

            if isinstance(data, dict):
                items = data.get("features", data.get("items", []))
                if isinstance(items, list):
                    for item in items:
                        props = item.get("properties", item)
                        fz = props.get("floodZone", props.get("flood_zone", ""))
                        if fz:
                            flood_zone = str(fz)
                        rr = props.get("riverAndSeaRisk", props.get("risk_level", ""))
                        if rr:
                            river_risk = rr.lower().replace(" ", "_")
                elif isinstance(data, dict):
                    flood_zone = str(data.get("floodZone", data.get("flood_zone", "1")))
                    river_risk = data.get("riverAndSeaRisk", "unknown")

            return {
                "status": "ok",
                "flood_zone": flood_zone,
                "river_and_sea_risk": river_risk,
                "surface_water_risk": surface_water_risk,
                "source": "environment_agency",
            }

    except Exception as e:
        return _failed_result("environment_agency", str(e))


# ============================================================================
# 2. EPC DATA — DLUHC Non-Domestic EPC Register
# Free API key from epc.opendatacommunities.org
# ============================================================================

async def get_epc_data(postcode: str) -> Dict[str, Any]:
    """
    Query the non-domestic EPC register by postcode.
    Returns the most recent EPC certificate for the postcode area.
    Falls back to domestic if no non-domestic found.
    """
    epc_api_key = os.environ.get("EPC_API_KEY")
    if not epc_api_key:
        return _not_configured_result(
            "dluhc_epc_register",
            "EPC_API_KEY not configured"
        )

    try:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {epc_api_key}"
        }

        clean_postcode = postcode.replace(' ', '%20')

        async with httpx.AsyncClient(timeout=10) as client:
            nd_url = (
                f"https://epc.opendatacommunities.org/api/v1/non-domestic/search"
                f"?postcode={clean_postcode}&size=1"
            )
            response = await client.get(nd_url, headers=headers)

            if _is_failed_response(response):
                return _failed_result(
                    "dluhc_epc_register",
                    f"Non-domestic EPC API returned {response.status_code}"
                )

            data = response.json()
            rows = data.get("rows", [])

            if rows:
                cert = rows[0]
                return {
                    "status": "ok",
                    "rating": cert.get("asset-rating-band", cert.get("current-energy-rating")),
                    "score": _safe_int(cert.get("asset-rating", cert.get("current-energy-efficiency"))),
                    "floor_area_sqm": _safe_float(cert.get("floor-area", cert.get("total-floor-area"))),
                    "building_type": cert.get("property-type", cert.get("building-reference-number")),
                    "certificate_date": cert.get("lodgement-date", cert.get("lodgement-datetime")),
                    "address": cert.get("address", ""),
                    "source": "dluhc_epc_register_non_domestic",
                }

            # Fallback to domestic
            d_url = (
                f"https://epc.opendatacommunities.org/api/v1/domestic/search"
                f"?postcode={clean_postcode}&size=1"
            )
            response2 = await client.get(d_url, headers=headers)

            if _is_failed_response(response2):
                return _failed_result(
                    "dluhc_epc_register",
                    f"Domestic EPC API returned {response2.status_code}"
                )

            data2 = response2.json()
            rows2 = data2.get("rows", [])

            if rows2:
                cert2 = rows2[0]
                return {
                    "status": "ok",
                    "rating": cert2.get("current-energy-rating"),
                    "score": _safe_int(cert2.get("current-energy-efficiency")),
                    "floor_area_sqm": _safe_float(cert2.get("total-floor-area")),
                    "building_type": cert2.get("property-type"),
                    "certificate_date": cert2.get("lodgement-date"),
                    "address": cert2.get("address1", ""),
                    "source": "dluhc_epc_register_domestic",
                    "note": "No non-domestic EPC found; nearest domestic certificate shown"
                }

        return _failed_result(
            "dluhc_epc_register",
            "No EPC certificate found for this postcode"
        )

    except Exception as e:
        return _failed_result("dluhc_epc_register", str(e))


# ============================================================================
# 3. HERITAGE — Historic England Listed Buildings
# Free, no API key, ArcGIS Open Data Hub
# ============================================================================

async def get_heritage_data(lat: float, lng: float, radius_m: int = 500) -> Dict[str, Any]:
    """
    Query Historic England's listed buildings dataset by coordinates.
    Uses the ArcGIS FeatureServer REST API.
    Checks if any listed buildings are within radius_m of the site.
    """
    try:
        lat_delta = radius_m / 111320
        lng_delta = radius_m / (111320 * math.cos(math.radians(lat)))

        xmin = lng - lng_delta
        xmax = lng + lng_delta
        ymin = lat - lat_delta
        ymax = lat + lat_delta

        url = (
            "https://services-eu1.arcgis.com/ZOdPfBS3aqqDYPUQ/arcgis/rest/services/"
            "Listed_Buildings/FeatureServer/0/query"
        )

        params = {
            "where": "1=1",
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "Name,Grade,ListDate,NHLE_Link",
            "returnGeometry": "true",
            "f": "json",
            "resultRecordCount": 10
        }

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, params=params)

            if _is_failed_response(response):
                return _failed_result(
                    "historic_england_nhle",
                    f"Historic England API returned {response.status_code}"
                )

            data = response.json()
            features = data.get("features", [])

            if features:
                nearest = None
                nearest_distance = float('inf')

                for feature in features:
                    attrs = feature.get("attributes", {})
                    geom = feature.get("geometry", {})

                    if geom:
                        f_lat = geom.get("y", 0)
                        f_lng = geom.get("x", 0)
                        dist = _haversine(lat, lng, f_lat, f_lng)

                        if dist < nearest_distance:
                            nearest_distance = dist
                            nearest = attrs

                listed_buildings = []
                for f in features[:5]:
                    a = f.get("attributes", {})
                    listed_buildings.append({
                        "name": a.get("Name", "Unknown"),
                        "grade": a.get("Grade", "Unknown"),
                    })

                return {
                    "status": "ok",
                    "listed_building_on_site": nearest_distance < 50,
                    "listed_buildings_within_500m": len(features),
                    "nearest_listed": {
                        "name": nearest.get("Name", "Unknown") if nearest else None,
                        "grade": nearest.get("Grade", "Unknown") if nearest else None,
                        "distance_m": round(nearest_distance),
                    } if nearest else None,
                    "listed_buildings_nearby": listed_buildings,
                    "source": "historic_england_nhle",
                }
            else:
                return {
                    "status": "ok",
                    "listed_building_on_site": False,
                    "listed_buildings_within_500m": 0,
                    "nearest_listed": None,
                    "listed_buildings_nearby": [],
                    "source": "historic_england_nhle",
                    "note": f"No listed buildings within {radius_m}m"
                }

    except Exception as e:
        return _failed_result("historic_england_nhle", str(e))


# ============================================================================
# ORCHESTRATOR — runs all enrichment in parallel
# ============================================================================

async def enrich_property(
    address: str,
    postcode: Optional[str] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None
) -> Dict[str, Any]:
    """
    Run all enrichment sources in parallel.
    Requires at least a postcode or coordinates.
    """
    import asyncio

    if not postcode:
        postcode = extract_postcode(address)

    enrichment = {}
    tasks = []

    if postcode:
        tasks.append(("flood_risk", get_flood_risk(postcode)))

    if postcode:
        tasks.append(("epc", get_epc_data(postcode)))

    if lat and lng:
        tasks.append(("heritage", get_heritage_data(lat, lng)))

    if not tasks:
        return {
            "enrichment_status": "skipped",
            "data_complete": False,
            "failed_sources": [],
            "note": "No postcode or coordinates available for enrichment",
            "flood_risk": None,
            "epc": None,
            "heritage": None
        }

    results = await asyncio.gather(
        *[task[1] for task in tasks],
        return_exceptions=True
    )

    failed_sources = []

    for i, (name, _) in enumerate(tasks):
        result = results[i]
        if isinstance(result, Exception):
            enrichment[name] = _failed_result(name, str(result))
            failed_sources.append(name)
        else:
            enrichment[name] = result
            if result.get("status") in ("failed", "not_configured"):
                failed_sources.append(name)

    for source in ["flood_risk", "epc", "heritage"]:
        if source not in enrichment:
            enrichment[source] = _not_configured_result(
                source,
                f"Missing {'postcode' if source != 'heritage' else 'coordinates'} for {source} lookup"
            )
            failed_sources.append(source)

    all_failed = len(failed_sources) == len(tasks)

    enrichment["enrichment_status"] = "complete"
    enrichment["data_complete"] = not all_failed and len(failed_sources) == 0
    enrichment["failed_sources"] = failed_sources
    if all_failed:
        enrichment["note"] = "All enrichment sources failed or were not configured. Government data may be temporarily unavailable."

    return enrichment


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _safe_int(value) -> Optional[int]:
    """Safely convert a value to int."""
    if value is None:
        return None
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return None


def _safe_float(value) -> Optional[float]:
    """Safely convert a value to float."""
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in metres between two lat/lng points."""
    R = 6371000  # Earth radius in metres
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (math.sin(dphi / 2) ** 2 +
         math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c
