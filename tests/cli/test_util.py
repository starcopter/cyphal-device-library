from unittest.mock import AsyncMock, MagicMock, patch

import can
import pytest

from cyphal_device_library.util import select_can_channel


async def test_select_can_channel_virtual_interface():
    """Ensure we can select a PythonCAN virtual channel using the mocked prompt."""
    # Discover available virtual configurations. At least one should exist.
    available = [f"{cfg['interface']}:{cfg['channel']}" for cfg in can.detect_available_configs(["virtual"])]
    assert available, "Expected at least one PythonCAN virtual configuration to be available"

    selected = available[0]

    with (
        patch("cyphal_device_library.util.SUPPORTED_CAN_INTERFACES", ["virtual"]),
        patch(
            "questionary.select",
            return_value=MagicMock(ask_async=AsyncMock(return_value=selected)),
        ) as mock_select,
    ):
        result = await select_can_channel(
            message="Select a CAN channel",
            instruction="Select from the list below.",
        )

    assert result == selected
    # Confirm the prompt was shown with our message/instruction
    mock_select.assert_called_once()


async def test_select_can_channel_no_answer_raises():
    """If the prompt returns no answer, the helper should raise ValueError."""
    # Ensure there is at least one available configuration to avoid RuntimeError
    available = can.detect_available_configs(["virtual"])
    assert available, "Expected at least one PythonCAN virtual configuration to be available"

    with (
        patch("cyphal_device_library.util.SUPPORTED_CAN_INTERFACES", ["virtual"]),
        patch(
            "questionary.select",
            return_value=MagicMock(ask_async=AsyncMock(return_value=None)),
        ),
        pytest.raises(ValueError),
    ):
        await select_can_channel()
