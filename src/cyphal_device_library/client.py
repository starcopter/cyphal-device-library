# Copyright (c) 2025 starcopter GmbH
# This software is distributed under the terms of the MIT License.
# Author: Lasse Fröhner <lasse@starcopter.com>

import asyncio
import logging
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

import pycyphal
import pycyphal.application
import uavcan.diagnostic
import uavcan.node
import uavcan.register
from pycyphal.application.file import FileServer
from pycyphal.application.node_tracker import Entry, NodeTracker

_logger = logging.getLogger(__name__)


class Client:
    UAVCAN_SEVERITY_TO_PYTHON = {
        uavcan.diagnostic.Severity_1.TRACE: logging.DEBUG,
        uavcan.diagnostic.Severity_1.DEBUG: logging.DEBUG,
        uavcan.diagnostic.Severity_1.INFO: logging.INFO,
        uavcan.diagnostic.Severity_1.NOTICE: logging.INFO,
        uavcan.diagnostic.Severity_1.WARNING: logging.WARNING,
        uavcan.diagnostic.Severity_1.ERROR: logging.ERROR,
        uavcan.diagnostic.Severity_1.CRITICAL: logging.CRITICAL,
        uavcan.diagnostic.Severity_1.ALERT: logging.CRITICAL,
    }

    def __init__(
        self,
        name: str,
        version: uavcan.node.Version_1_0 | None = None,
        uid: int | None = None,
        registry: Path | str | None = None,
        parallel_updates: int = 6,
    ) -> None:
        node_info_attrs = {"name": name}
        if version is not None:
            node_info_attrs["software_version"] = version
        if uid is not None:
            node_info_attrs["unique_id"] = uid.to_bytes(16, "big")
        self.node = pycyphal.application.make_node(pycyphal.application.NodeInfo(**node_info_attrs), registry)

        self.node_tracker = NodeTracker(self.node)
        self.node_tracker.get_info_priority = pycyphal.transport.Priority.LOW
        self.node_tracker.add_update_handler(self._log_node_changes)

        tempdir = TemporaryDirectory(prefix="cyphal-")
        self.node.add_lifetime_hooks(None, tempdir.cleanup)

        self.file_server = FileServer(self.node, [Path(tempdir.name).resolve()])

        self.sub_diagnostic_record = self.node.make_subscriber(uavcan.diagnostic.Record_1)
        self.node.add_lifetime_hooks(
            lambda: self.sub_diagnostic_record.receive_in_background(self.log_diagnostic_record),
            self.sub_diagnostic_record.close,
        )

        self.update_semaphore = asyncio.Semaphore(parallel_updates)
        self.node.heartbeat_publisher.mode = uavcan.node.Mode_1.OPERATIONAL

    def start(self) -> None:
        _logger.debug("starting Python node")
        self.node.start()

    def close(self) -> None:
        _logger.debug("closing Python node")
        self.node.close()

    def __enter__(self) -> "Client":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _log_node_changes(self, node_id: int, old_entry: Entry | None, new_entry: Entry | None) -> None:
        self.node.heartbeat_publisher.vendor_specific_status_code = len(self.node_tracker.registry)
        if old_entry is None and new_entry.info is None:
            _logger.info("Node %i appeared", node_id)
            return
        if new_entry is None:
            _logger.info("Node %i went dark", node_id)
            return
        if new_entry.info is None:
            _logger.info("Node %i restarted", node_id)
            return
        if new_entry.info is not None:
            _logger.info(
                "Node %i is %s",
                node_id,
                self.format_get_info_response(new_entry.info),
            )
            return
        _logger.warning("Node not understood: %i, %r, %r", node_id, old_entry, new_entry)

    @staticmethod
    def format_get_info_response(info: uavcan.node.GetInfo_1.Response) -> str:
        hardware_version_major = (
            chr(info.hardware_version.major + ord("A") - 1) if info.hardware_version.major > 0 else "0"
        )
        info_list = [
            f"{info.name.tobytes().decode()} {hardware_version_major}.{info.hardware_version.minor}",
            f"UID {info.unique_id.tobytes().hex()}",
            f"running v{info.software_version.major}.{info.software_version.minor}",
        ]
        if info.software_vcs_revision_id:
            width = 8 if info.software_vcs_revision_id <= 0xFFFFFFFF else 16
            info_list.append(f"Git Rev {info.software_vcs_revision_id:0{width}x}")
        if info.software_image_crc.size > 0:
            crc = int(info.software_image_crc[0])
            width = 8 if crc <= 0xFFFFFFFF else 16
            info_list.append(f"CRC {crc:0{width}x}")

        return ", ".join(info_list)

    async def execute_command(
        self, command: uavcan.node.ExecuteCommand_1.Request, server_node_id: int
    ) -> uavcan.node.ExecuteCommand_1.Response:
        client = self.node.make_client(uavcan.node.ExecuteCommand_1, server_node_id)
        loop = asyncio.get_event_loop()
        try:
            t_start = loop.time()
            result = await client.call(command)
            t_result = loop.time()
            if result is None:
                raise TimeoutError(f"{command} to {server_node_id} timed out")
            response, _meta = result
            _logger.debug(
                "%s to node %i returned in %i ms with %s.",
                command,
                server_node_id,
                1000 * (t_result - t_start),
                response,
            )
            return response
        finally:
            client.close()

    async def restart_node(self, node_to_restart: int, wait: bool = True, timeout: float = 1.0) -> float:
        _logger.info("Attempting to restart node %i", node_to_restart)
        loop = asyncio.get_event_loop()
        t_start = loop.time()
        response = await self.execute_command(
            uavcan.node.ExecuteCommand_1.Request(uavcan.node.ExecuteCommand_1.Request.COMMAND_RESTART), node_to_restart
        )
        t_response = loop.time()

        if response.status != uavcan.node.ExecuteCommand_1.Response.STATUS_SUCCESS:
            _logger.error(
                "Restart request failed in %i ms with status %i", 1000 * (t_response - t_start), response.status
            )
            raise RuntimeError(f"Restart request to node {node_to_restart} failed: {response}")

        _logger.info("Node %i responded to restart request in %i ms", node_to_restart, 1000 * (t_response - t_start))

        if not wait:
            return t_response - t_start

        _logger.debug("Waiting for node %i to check back in", node_to_restart)
        is_node_back_online = await self.wait_for_restart(node_to_restart, timeout)
        t_back_online = loop.time()

        if not is_node_back_online:
            raise TimeoutError(f"Node {node_to_restart} failed to get back online in {timeout:.3f} seconds")
        _logger.info("Node %i back online after %i ms", node_to_restart, 1000 * (t_back_online - t_start))

        return t_back_online - t_start

    async def wait_for_restart(self, node_id: int, timeout: float = 1.0) -> bool:
        restarted = asyncio.Event()

        def handler(updated_node_id: int, old_entry: Entry | None, new_entry: Entry | None) -> None:
            if updated_node_id == node_id and new_entry is not None and new_entry.info is None:
                restarted.set()
                _logger.debug("Restart detected for node %i", node_id)

        self.node_tracker.add_update_handler(handler)
        try:
            async with asyncio.timeout(timeout):
                await restarted.wait()
        except asyncio.TimeoutError:
            _logger.warning("Restart of node %i timed out after %.1f seconds", node_id, timeout)
            return False
        else:
            return True
        finally:
            # will actually be executed _before_ returning
            self.node_tracker.remove_update_handler(handler)

    async def update(self, node_id: int, image: Path | str, wait: bool = True, timeout: float = 10.0) -> float:
        _logger.info("Requesting an update to %s from node %i", image, node_id)
        loop = asyncio.get_event_loop()
        root = self.file_server.roots[0]
        image = Path(image).resolve()
        if not image.is_relative_to(root):
            shutil.copy(image, root / image.name)
            image = root / image.name

        assert image.is_relative_to(root), f"Image {image} is not a child of {root}"

        async with self.update_semaphore:
            t_start = loop.time()
            response = await self.execute_command(
                uavcan.node.ExecuteCommand_1.Request(
                    uavcan.node.ExecuteCommand_1.Request.COMMAND_BEGIN_SOFTWARE_UPDATE,
                    image.name,
                ),
                node_id,
            )
            t_response = loop.time()

            if response.status != uavcan.node.ExecuteCommand_1.Response.STATUS_SUCCESS:
                _logger.error(
                    "Software update request failed in %.1f s with status %i", t_response - t_start, response.status
                )
                raise RuntimeError(f"Software update request to node {node_id} failed: {response}")

            _logger.info("Node %i responded to update request in %i ms", node_id, 1000 * (t_response - t_start))

            if not wait:
                return t_response - t_start

            _logger.debug("Waiting for node %i to finish update", node_id)
            is_node_updated = await self.wait_for_update(node_id, timeout)
            t_updated = loop.time()

            if not is_node_updated:
                raise TimeoutError(f"Node {node_id} failed to update in {timeout:.3f} seconds")
            _logger.info("Node %i finished update in %i ms", node_id, 1000 * (t_updated - t_start))

            return t_updated - t_start

    async def wait_for_update(self, node_id: int, timeout: float = 10.0) -> bool:
        now = asyncio.get_event_loop().time
        start = now()
        has_restarted = await self.wait_for_restart(node_id, timeout)
        if not has_restarted:
            return False
        restart_delay = now() - start

        heartbeat_subscription = self.node.make_subscriber(uavcan.node.Heartbeat_1)
        software_update_complete = asyncio.Event()

        async def handler(heartbeat: uavcan.node.Heartbeat_1, transfer: pycyphal.transport.TransferFrom) -> None:
            if transfer.source_node_id == node_id and heartbeat.mode.value != uavcan.node.Mode_1.SOFTWARE_UPDATE:
                software_update_complete.set()

        heartbeat_subscription.receive_in_background(handler)

        try:
            async with asyncio.timeout(timeout - restart_delay):
                await software_update_complete.wait()
        except asyncio.TimeoutError:
            _logger.warning("Software Update of node %i timed out after %.1f seconds", node_id, timeout)
            return False
        else:
            return True
        finally:
            # will actually be executed _before_ returning
            heartbeat_subscription.close()

    async def log_diagnostic_record(
        self, record: uavcan.diagnostic.Record_1, transfer: pycyphal.transport.TransferFrom
    ) -> None:
        heartbeat, info = self.node_tracker.registry.get(transfer.source_node_id, (None, None))
        logging.getLogger("uavcan.diagnostic.record").log(
            level=self.UAVCAN_SEVERITY_TO_PYTHON[record.severity.value],
            msg=record.text.tobytes().decode("utf8", errors="replace"),
            extra={"record": record, "transfer": transfer, "heartbeat": heartbeat, "info": info},
        )
