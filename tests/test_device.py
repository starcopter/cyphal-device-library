"""Tests for Device client ownership."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from cyphal_device_library.device import Device


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.node.id = 1
    return client


@pytest.mark.asyncio
async def test_device_close_closes_client_by_default() -> None:
    client = _mock_client()
    device = Device(client, 42, discover_registers=False)
    await device.wait_for_initialization()

    device.close()

    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_device_close_skips_client_when_not_owner() -> None:
    client = _mock_client()
    device = Device(client, 42, discover_registers=False, owns_client=False)
    await device.wait_for_initialization()

    device.close()

    client.close.assert_not_called()


@pytest.mark.asyncio
async def test_device_context_manager_respects_client_ownership() -> None:
    client = _mock_client()
    client.__aenter__ = MagicMock(return_value=client)
    client.__aexit__ = MagicMock(return_value=None)

    async with Device(client, 42, discover_registers=False, owns_client=False) as device:
        assert device.node_id == 42

    client.__aenter__.assert_not_called()
    client.__aexit__.assert_not_called()
    client.close.assert_not_called()


@pytest.mark.asyncio
async def test_device_context_manager_closes_owned_client() -> None:
    client = _mock_client()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    async with Device(client, 42, discover_registers=False) as device:
        assert device.node_id == 42

    client.__aenter__.assert_called_once()
    client.__aexit__.assert_called_once()
    client.close.assert_called_once()
