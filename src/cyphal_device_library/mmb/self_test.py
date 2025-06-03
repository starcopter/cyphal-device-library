import asyncio
from typing import AsyncGenerator

import rich.console
import rich.progress
import starcopter.aeric.mmb
from pycyphal.presentation import Subscriber
from rich.prompt import Confirm

from ..util import async_prompt, configure_logging
from .client import MMBClient


async def check_colors(client: MMBClient, console: rich.console.Console) -> bool:
    errors = 0

    async with client.temporary_node_id(1):
        white_green = await async_prompt(Confirm("Is the MMB flashing [bold]WHITE[/bold] and green?", console=console))

    if not white_green:
        console.print("[red]MMB not flashing [bold]WHITE[/bold] and green.[/red]")
        errors += 1

    async with client.temporary_node_id(3):
        red_green = await async_prompt(Confirm("Is the MMB flashing [bold]RED[/bold] and green?", console=console))

    if not red_green:
        console.print("[red]MMB not flashing [bold]RED[/bold] and green.[/red]")
        errors += 1

    return errors == 0


async def get_temperatures(
    sub: Subscriber[starcopter.aeric.mmb.SystemStatus_0_1],
    node_id: int | None = None,
    limit: int | None = None,
) -> AsyncGenerator[float, None]:
    i = 0
    async for msg, meta in sub:
        if node_id is not None and meta.source_node_id != node_id:
            continue
        yield (
            msg.outside_temperature.kelvin + msg.inside_temperature.kelvin + msg.stm32_core_temperature.kelvin
        ) / 3 - 273.15
        i += 1
        if limit is not None and i >= limit:
            return


async def check_brightness(
    client: MMBClient,
    console: rich.console.Console,
    confirm: bool = True,
    timeout: float = 30,
    temperature_rise: float = 5,
) -> bool:
    brightness_register = client.registry["navlight.brightness"]
    await brightness_register.set_value(10)

    if confirm and not await async_prompt(Confirm("Check brightness? Mind your eyes!", console=console)):
        return True

    with client.system_status_subscription() as sub:
        console.print("Measuring initial temperature...")
        temp_before = sum([temp async for temp in get_temperatures(sub, node_id=client.dut, limit=4)]) / 4

        get_time = asyncio.get_event_loop().time

        try:
            async with brightness_register.temporary_value(1000), asyncio.timeout(timeout):
                start_time = get_time()
                with rich.progress.Progress() as progress:
                    timeout_task = progress.add_task("Timeout", total=timeout)
                    rise_task = progress.add_task("Temperature rise", total=temperature_rise)
                    async for temp in get_temperatures(sub):
                        progress.update(timeout_task, completed=get_time() - start_time)
                        progress.update(rise_task, completed=temp - temp_before)
                        if temp > temp_before + temperature_rise:
                            break

        except asyncio.TimeoutError:
            console.print(f"[red]Temperature did not rise by {temperature_rise} K in {timeout} s.[/red]")
            return False

        else:
            console.print("[green]LEDs seem to work.[/green]")
            return True


async def main():
    console = rich.console.Console()
    configure_logging(console, "test.log")

    async with MMBClient() as client:
        await check_colors(client, console)
        await check_brightness(client, console, temperature_rise=3)


if __name__ == "__main__":
    asyncio.run(main())
