import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import TypeVar

import rich.console
import rich.prompt

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


async def async_prompt(prompt: rich.prompt.PromptBase[T], default: T = ...) -> T:
    return await asyncio.get_event_loop().run_in_executor(None, partial(prompt, default=default))


if __name__ == "__main__":
    configure_logging()
    # download_and_install_standard_uavcan_namespace()
