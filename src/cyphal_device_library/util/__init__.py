import asyncio
import logging
from collections.abc import Container, Sequence
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import can
import questionary
import rich.console
import rich.prompt
from rich.padding import Padding

if TYPE_CHECKING:
    from pycyphal.transport.can import CANTransport

logger = logging.getLogger(__name__)
T = TypeVar("T")


def configure_logging(console: rich.console.Console | None = None, filename: Path | str | None = None):
    from rich.logging import RichHandler

    root_logger = logging.getLogger()
    root_logger.setLevel("DEBUG")
    logging.getLogger("pycyphal").setLevel("INFO")
    logging.getLogger("pydsdl").setLevel("INFO")
    logging.getLogger("nunavut").setLevel("INFO")

    rich_handler = RichHandler(console=console, rich_tracebacks=True, tracebacks_show_locals=True, show_path=False)
    rich_handler.setFormatter(logging.Formatter("%(name)-20s %(message)s", datefmt="[%X]"))
    root_logger.addHandler(rich_handler)

    if filename:
        file_handler = logging.FileHandler(filename)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s.%(msecs)03d %(name)-30s %(levelname)-8s: %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        root_logger.addHandler(file_handler)


SUPPORTED_CAN_INTERFACES: list[str] = ["usbtingo", "pcan", "socketcan"]


async def select_can_channel(
    message: str = "Select a CAN channel",
    instruction: str | None = "Select from the list below.",
    exclude: Container[str] = (),
) -> str:
    """Select a CAN channel from available interfaces."""
    available_configurations = [
        config_str
        for config in can.detect_available_configs(SUPPORTED_CAN_INTERFACES)
        if (config_str := f"{config['interface']}:{config['channel']}") not in exclude
    ]

    def _sort_key(config_str: str) -> tuple[int, str, str]:
        iface, channel = config_str.split(":")
        return (SUPPORTED_CAN_INTERFACES.index(iface), iface, channel)

    available_configurations.sort(key=_sort_key)

    if not available_configurations:  # pragma: no cover
        rich.print("[red]✘[/red] No available CAN channels found. Please connect a CAN interface and try again.")
        raise RuntimeError("No available CAN channels found")

    question = questionary.select(message, instruction=instruction, choices=available_configurations)
    answer = await question.ask_async()
    if not answer:
        raise ValueError("No answer provided")

    return answer


def make_can_transport(iface: str, bitrate: int | list[int], node_id: int) -> "CANTransport":
    """
    Create a CAN transport for Cyphal communication.

    This function creates a CAN transport instance configured with the specified
    interface, bitrate(s), and node ID. It automatically handles bitrate configuration
    for both single and dual bitrate setups, and sets the appropriate MTU size.

    See [1] for more context on arguments.

    Args:
        iface: The CAN interface name (e.g., 'socketcan:can0' or 'usbtingo:')
        bitrate: CAN bitrate in bits per second. Can be:
            - A single integer for classic CAN
            - A list of two integers [arbitration, data] for CAN FD
        node_id: The Cyphal node ID to assign to this transport

    Returns:
        CANTransport: A configured CAN transport instance ready for use

    Raises:
        ValueError: If the bitrate list contains more than 2 values

    References:
        [1]: https://pycyphal.readthedocs.io/en/stable/api/pycyphal.application.html#pycyphal.application.make_transport
    """
    from pycyphal.application import make_transport
    from pycyphal.application.register import Natural16, Natural32, ValueProxy

    if not isinstance(bitrate, Sequence):
        bitrate = [bitrate]
    if len(bitrate) == 1:
        bitrate = [bitrate[0], bitrate[0]]
    if len(bitrate) != 2:
        raise ValueError("Only 2 bitrates are supported")

    mtu = 64 if bitrate[0] != bitrate[1] else 8

    config = {
        "uavcan.can.iface": ValueProxy(iface),
        "uavcan.can.mtu": ValueProxy(Natural16([mtu])),
        "uavcan.can.bitrate": ValueProxy(Natural32(bitrate)),
        "uavcan.node.id": ValueProxy(Natural16([node_id])),
    }
    return make_transport(config)


def spaces_to_padding(text: str) -> Padding:
    """Convert leading and trailing spaces in a string to padding.

    Args:
        text: The input string that may contain leading and/or trailing spaces

    Returns:
        A Padding object with the text content, with leading and trailing spaces converted to padding
    """
    trailing_spaces = len(text) - len(text.rstrip())
    leading_spaces = len(text) - len(text.lstrip())
    return Padding(text.strip(), (0, trailing_spaces, 0, leading_spaces))


async def async_prompt(prompt: rich.prompt.PromptBase[T], default: T = ...) -> T:
    return await asyncio.get_event_loop().run_in_executor(None, partial(prompt, default=default))
