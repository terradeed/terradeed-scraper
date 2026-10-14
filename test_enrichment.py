"""
Tests for enrichment.py
Covers failure handling, missing config, and orchestrator behaviour.
"""

import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from enrichment import (
    get_flood_risk,
    get_epc_data,
    get_heritage_data,
    enrich_property,
    _is_failed_response,
)


# ============================================================================
# _is_failed_response
# ============================================================================

def test_is_failed_response_non_2xx():
    resp = MagicMock()
    resp.is_success = False
    resp.status_code = 404
    assert _is_failed_response(resp) is True


def test_is_failed_response_200_with_error_body():
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = {"error": {"code": 400, "message": "Invalid URL"}}
    assert _is_failed_response(resp) is True


def test_is_failed_response_200_ok():
    resp = MagicMock()
    resp.is_success = True
    resp.json.return_value = {"features": []}
    assert _is_failed_response(resp) is False


# ============================================================================
# get_flood_risk
# ============================================================================

@pytest.mark.asyncio
async def test_flood_risk_non_2xx():
    mock_resp = MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 404

    with patch("enrichment.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=mock_resp)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await get_flood_risk("M1 1AE")
        assert result["status"] == "failed"
        assert result["source"] == "environment_agency"
        assert "404" in result["reason"]


@pytest.mark.asyncio
async def test_flood_risk_200_with_error_body():
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"error": {"code": 400, "message": "Invalid URL"}}

    with patch("enrichment.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=mock_resp)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await get_flood_risk("M1 1AE")
        assert result["status"] == "failed"
        assert result["source"] == "environment_agency"
        assert "200" in result["reason"]


# ============================================================================
# get_epc_data
# ============================================================================

@pytest.mark.asyncio
async def test_epc_missing_key():
    with patch.dict(os.environ, {}, clear=True):
        result = await get_epc_data("M1 1AE")
        assert result["status"] == "not_configured"
        assert result["source"] == "dluhc_epc_register"
        assert "EPC_API_KEY not configured" in result["reason"]


@pytest.mark.asyncio
async def test_epc_non_2xx():
    mock_resp = MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 503

    with patch.dict(os.environ, {"EPC_API_KEY": "fake_key"}):
        with patch("enrichment.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.get = AsyncMock(return_value=mock_resp)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await get_epc_data("M1 1AE")
            assert result["status"] == "failed"
            assert result["source"] == "dluhc_epc_register"
            assert "503" in result["reason"]


# ============================================================================
# get_heritage_data
# ============================================================================

@pytest.mark.asyncio
async def test_heritage_200_with_error_body():
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"error": {"code": 400, "message": "Invalid URL"}}

    with patch("enrichment.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.get = AsyncMock(return_value=mock_resp)
        MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await get_heritage_data(53.4803, -2.2428)
        assert result["status"] == "failed"
        assert result["source"] == "historic_england_nhle"
        assert "200" in result["reason"]


# ============================================================================
# enrich_property orchestrator
# ============================================================================

@pytest.mark.asyncio
async def test_enrich_property_all_sources_fail():
    """When every source returns failed/not_configured, the orchestrator must
    set data_complete=False, list all failed sources, and add a clear note."""

    with patch("enrichment.get_flood_risk", new=AsyncMock(return_value={
        "status": "failed", "source": "environment_agency", "reason": "404"
    })):
        with patch("enrichment.get_epc_data", new=AsyncMock(return_value={
            "status": "not_configured", "source": "dluhc_epc_register",
            "reason": "EPC_API_KEY not configured"
        })):
            with patch("enrichment.get_heritage_data", new=AsyncMock(return_value={
                "status": "failed", "source": "historic_england_nhle",
                "reason": "Invalid URL"
            })):
                result = await enrich_property(
                    address="123 Test Road, Manchester M1 1AE",
                    lat=53.4803,
                    lng=-2.2428
                )

    assert result["enrichment_status"] == "complete"
    assert result["data_complete"] is False
    assert set(result["failed_sources"]) == {
        "flood_risk", "epc", "heritage"
    }
    assert "All enrichment sources failed" in result["note"]
