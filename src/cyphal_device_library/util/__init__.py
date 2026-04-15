import logging
import sys
from collections.abc import Awaitable, Callable, Container
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

import can
import rich.console
import rich.prompt
from rich.padding import Padding

from cyphal_device_library.util.questions import SelectQuestion

if TYPE_CHECKING:
    from pycyphal.transport.can import CANTransport

logger = logging.getLogger(__name__)
T = TypeVar("T")

can_transport_cyphal_node_id = None
can_transport_interface = None
can_transport_bitrate = None


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


SUPPORTED_CAN_INTERFACES: list[str] = ["usbtingo", "pcan"]
if sys.platform != "win32":  # TODO
    SUPPORTED_CAN_INTERFACES.append("socketcan")


def list_available_can_channels(exclude: Container[str] = ()) -> list[str]:
    """Return sorted available CAN interface/channel configurations.

    Example return values: ``socketcan:can0``, ``usbtingo:abc123``.
    """
    available_configurations = [
        config_str
        for config in can.detect_available_configs(SUPPORTED_CAN_INTERFACES)
        if (config_str := f"{config['interface']}:{config['channel']}") not in exclude
    ]

    def _sort_key(config_str: str) -> tuple[int, str, str]:
        iface, channel = config_str.split(":")
        return (SUPPORTED_CAN_INTERFACES.index(iface), iface, channel)

    available_configurations.sort(key=_sort_key)
    return available_configurations


async def select_can_channel(
    message: str = "Select a CAN channel",
    instruction: str | None = "Select from the list below.",
    exclude: Container[str] = (),
    question_caller: Callable[[SelectQuestion], Awaitable[str]] = SelectQuestion.ask,
) -> str:
    """Select a CAN channel from available interfaces."""
    available_configurations = list_available_can_channels(exclude=exclude)

    if not available_configurations:  # pragma: no cover
        rich.print("[red]✘[/red] No available CAN channels found. Please connect a CAN interface and try again.")
        raise RuntimeError("No available CAN channels found")

    question = SelectQuestion(message, instruction, available_configurations)
    answer = await question_caller(question)
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
    from pycyphal.transport.can import CANTransport

    bitrate_list: list[int]
    if isinstance(bitrate, int) or (len(bitrate) == 2 and bitrate[0] == bitrate[1]):
        # classic CAN
        bitrate_list = [bitrate, bitrate] if isinstance(bitrate, int) else [bitrate[0], bitrate[1]]
        mtu = 8
    elif len(bitrate) == 2:
        # CAN FD
        bitrate_list = [bitrate[0], bitrate[1]]
        mtu = 64
    else:
        raise ValueError("Only 2 bitrates are supported")

    config = {
        "uavcan.can.iface": ValueProxy(iface),
        "uavcan.can.mtu": ValueProxy(Natural16([mtu])),
        "uavcan.can.bitrate": ValueProxy(Natural32(bitrate_list)),
        "uavcan.node.id": ValueProxy(Natural16([node_id])),
    }

    global can_transport_cyphal_node_id, can_transport_interface, can_transport_bitrate
    can_transport_cyphal_node_id = node_id
    can_transport_interface = iface
    can_transport_bitrate = bitrate_list

    rich.print(
        f"[dim]Creating CAN transport with iface '{iface}', bitrate {bitrate_list}, mtu {mtu}, and node ID {node_id}[/dim]"
    )
    transport = make_transport(config)
    assert isinstance(transport, CANTransport)
    return transport


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
