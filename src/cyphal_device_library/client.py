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
from pycyphal.application.file import FileServer
from pycyphal.application.node_tracker import Entry, NodeTracker
from pycyphal.application.plug_and_play import CentralizedAllocator

from .util.logging import UAVCAN_SEVERITY_TO_PYTHON


class Client:
    """
    Cyphal Client base class.

    This class is a wrapper around a pycyphal.application.Node, providing a number of convenience methods for simple
    interactions with other nodes on the Cyphal bus.

    This client will:
    - create a pycyphal `NodeTracker` instance to watch for other nodes on the bus
    - create a pycyphal `FileServer` from a temporary directory to support software updates
    - subscribe to diagnostic records (logs) from other nodes, and log them to the Python logger

    Methods:
        execute_command: Execute a command on a remote node.
        restart_node: Restart a remote node and optionally wait for it to come back online.
        update: Update the software on a remote node and optionally wait for it to complete.
    """

    def __init__(
        self,
        name: str,
        *,
        version: uavcan.node.Version_1_0 | None = None,
        uid: int | None = None,
        transport: pycyphal.transport.Transport | None = None,
        registry: Path | str | None = None,
        parallel_updates: int = 6,
        logger: logging.Logger = logging.getLogger(__name__),
        pnp_server: bool = True,
    ) -> None:
        """Initialize a new Cyphal Client.

        Args:
            name: The name of the node.
            version: Optional software version of the node.
            uid: Optional unique identifier for the node.
            transport: The transport to use for the client. If not specified, a transport will be created from
                environment variables.
            registry: Optional path to the registry directory.
            parallel_updates: Maximum number of parallel software updates allowed.
            logger: The logger to use for logging. Defaults to the logger for this module.
        """
        node_info_attrs = {"name": name}
        if version is not None:
            node_info_attrs["software_version"] = version
        if uid is not None:
            node_info_attrs["unique_id"] = uid.to_bytes(16, "big")
        self.node = pycyphal.application.make_node(
            pycyphal.application.NodeInfo(**node_info_attrs), registry, transport=transport
        )

        self.logger = logger

        self.node_tracker = NodeTracker(self.node)
        self.node_tracker.get_info_priority = pycyphal.transport.Priority.LOW
        self.node_tracker.add_update_handler(self._log_node_changes)

        tempdir = TemporaryDirectory(prefix="cyphal-")
        self.node.add_lifetime_hooks(None, tempdir.cleanup)

        self.file_server = FileServer(self.node, [Path(tempdir.name).resolve()])

        self.sub_diagnostic_record = self.node.make_subscriber(uavcan.diagnostic.Record_1)
        self.node.add_lifetime_hooks(
            lambda: self.sub_diagnostic_record.receive_in_background(self._log_diagnostic_record),
            self.sub_diagnostic_record.close,
        )

        if pnp_server:
            self.pnp_allocator = CentralizedAllocator(self.node)

            def pnp_register_node(node_id: int, _old: Entry | None, entry: Entry | None) -> None:
                try:
                    unique_id = entry.info.unique_id.tobytes()
                except AttributeError:
                    unique_id = None
                self.pnp_allocator.register_node(node_id, unique_id)

            self.node_tracker.add_update_handler(pnp_register_node)
        else:
            self.pnp_allocator = None

        self.update_semaphore = asyncio.Semaphore(parallel_updates)
        self.node.heartbeat_publisher.mode = uavcan.node.Mode_1.OPERATIONAL

        self._nested_contexts = 0

    def start(self) -> None:
        """Start the Cyphal node and begin processing messages."""
        self.logger.debug("starting Python node")
        self.node.start()

    def close(self) -> None:
        """Close the Cyphal node and clean up resources."""
        self.logger.debug("closing Python node")
        self.node.close()

    def __enter__(self) -> "Client":
        """Context manager entry point.

        Returns:
            The Client instance.

        Example:
            >>> with Client("my_node") as client:
            ...     await client.restart_node(17)
        """
        if self._nested_contexts == 0:
            self.start()
        self._nested_contexts += 1
        self.logger.debug("entering nested context %i", self._nested_contexts)
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.logger.debug("exiting nested context %i", self._nested_contexts)
        self._nested_contexts -= 1
        if self._nested_contexts == 0:
            self.close()

    async def __aenter__(self) -> "Client":
        return self.__enter__()

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.__exit__(exc_type, exc_value, traceback)

    def _log_node_changes(self, node_id: int, old_entry: Entry | None, new_entry: Entry | None) -> None:
        self.node.heartbeat_publisher.vendor_specific_status_code = len(self.node_tracker.registry)
        if old_entry is None and new_entry.info is None:
            self.logger.info("Node %i appeared", node_id)
            return
        if new_entry is None:
            self.logger.info("Node %i went dark", node_id)
            return
        if new_entry.info is None:
            self.logger.info("Node %i restarted", node_id)
            return
        if new_entry.info is not None:
            self.logger.info(
                "Node %i is %s",
                node_id,
                self.format_get_info_response(new_entry.info),
            )
            return
        self.logger.warning("Node not understood: %i, %r, %r", node_id, old_entry, new_entry)

    @staticmethod
    def format_get_info_response(info: uavcan.node.GetInfo_1.Response) -> str:
        """Format a node's GetInfo response into a human-readable string.

        Args:
            info: The GetInfo response from a node.

        Returns:
            A formatted string containing node information including name, hardware version,
            UID, software version, and optional Git revision and CRC.
        """
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

    async def get_info(self, node_id: int) -> uavcan.node.GetInfo_1.Response:
        """Request information about a remote node.

        This method is completely independent from the NodeTracker.

        Args:
            node_id: The ID of the node to request information about.

        Returns:
            The response from the remote node.

        Raises:
            TimeoutError: If the request times out.
        """
        request = uavcan.node.GetInfo_1.Request()
        client = self.node.make_client(uavcan.node.GetInfo_1, node_id)
        try:
            result = await client.call(request)
            if result is None:
                raise TimeoutError(f"GetInfo request to node {node_id} timed out")
            response, _meta = result
            return response
        finally:
            client.close()

    async def execute_command(
        self, command: uavcan.node.ExecuteCommand_1.Request, server_node_id: int
    ) -> uavcan.node.ExecuteCommand_1.Response:
        """Execute a command on a remote node.

        This method will:
        1. create a temporary ExecuteCommand_1 client
        2. send the command
        3. wait for the response
        4. close the client
        5. return the response

        Note:
            The method does not raise an error on a bad status code. The caller should check the status field of the
            response to determine if the command was successful.

        Args:
            command: The command to execute.
            server_node_id: The ID of the node to execute the command on.

        Returns:
            The response from the remote node.

        Raises:
            TimeoutError: If the command execution times out.

        Example:
            >>> command = uavcan.node.ExecuteCommand_1.Request(uavcan.node.ExecuteCommand_1.Request.COMMAND_IDENTIFY)
            >>> response = await client.execute_command(command, 17)
            >>> print(response)
            uavcan.node.ExecuteCommand.Response.1.3(status=0, output='')
        """
        client = self.node.make_client(uavcan.node.ExecuteCommand_1, server_node_id)
        loop = asyncio.get_event_loop()
        try:
            t_start = loop.time()
            result = await client.call(command)
            t_result = loop.time()
            if result is None:
                raise TimeoutError(f"{command} to {server_node_id} timed out")
            response, _meta = result
            self.logger.debug(
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
        """Restart a remote node and optionally wait for it to come back online.

        Args:
            node_to_restart: The ID of the node to restart.
            wait: Whether to wait for the node to come back online.
            timeout: Maximum time to wait for the node to restart in seconds.

        Returns:
            The time taken for the restart operation in seconds.

        Raises:
            RuntimeError: If the restart request fails.
            TimeoutError: If waiting for the node to come back online times out.

        Example:
            >>> with Client("my_node") as client:
            ...     duration = await client.restart_node(17)
            ...     print(duration)
            0.07636671513319016
        """
        self.logger.info("Attempting to restart node %i", node_to_restart)
        loop = asyncio.get_event_loop()
        t_start = loop.time()
        response = await self.execute_command(
            uavcan.node.ExecuteCommand_1.Request(uavcan.node.ExecuteCommand_1.Request.COMMAND_RESTART), node_to_restart
        )
        t_response = loop.time()

        if response.status != uavcan.node.ExecuteCommand_1.Response.STATUS_SUCCESS:
            self.logger.error(
                "Restart request failed in %i ms with status %i", 1000 * (t_response - t_start), response.status
            )
            raise RuntimeError(f"Restart request to node {node_to_restart} failed: {response}")

        self.logger.info(
            "Node %i responded to restart request in %i ms", node_to_restart, 1000 * (t_response - t_start)
        )

        if not wait:
            return t_response - t_start

        self.logger.debug("Waiting for node %i to check back in", node_to_restart)
        is_node_back_online = await self.wait_for_restart(node_to_restart, timeout)
        t_back_online = loop.time()

        if not is_node_back_online:
            raise TimeoutError(f"Node {node_to_restart} failed to get back online in {timeout:.3f} seconds")
        self.logger.info("Node %i back online after %i ms", node_to_restart, 1000 * (t_back_online - t_start))

        return t_back_online - t_start

    async def wait_for_restart(self, node_id: int, timeout: float = 1.0) -> bool:
        """Wait for a node to restart and come back online.

        Args:
            node_id: The ID of the node to wait for.
            timeout: Maximum time to wait in seconds.

        Returns:
            True if the node restarted successfully, False if the operation timed out.
        """
        restarted = asyncio.Event()

        def handler(updated_node_id: int, old_entry: Entry | None, new_entry: Entry | None) -> None:
            if updated_node_id == node_id and new_entry is not None and new_entry.info is None:
                restarted.set()
                self.logger.debug("Restart detected for node %i", node_id)

        self.node_tracker.add_update_handler(handler)
        try:
            async with asyncio.timeout(timeout):
                await restarted.wait()
        except asyncio.TimeoutError:
            self.logger.warning("Restart of node %i timed out after %.1f seconds", node_id, timeout)
            return False
        else:
            return True
        finally:
            # will actually be executed _before_ returning
            try:
                self.node_tracker.remove_update_handler(handler)
            except ValueError:
                pass

    async def update(self, node_id: int, image: Path | str, wait: bool = True, timeout: float = 10.0) -> float:
        """Update the software on a remote node.

        Instruct a single node to update its software from a file.

        Args:
            node_id: The ID of the node to update.
            image: Path to the software image file.
            wait: Whether to wait for the update to complete.
            timeout: Maximum time to wait for the update to complete in seconds.

        Returns:
            The time taken for the update operation in seconds.

        Raises:
            RuntimeError: If the update request fails.
            TimeoutError: If waiting for the update to complete times out.

        Example:
            >>> with Client("my_node") as client:
            ...     duration = await client.update(9, "bin/com.starcopter.aeric.mmb-4.1-2.1.app.bin")
            ...     print(duration)
            1.6074032904580235
        """
        self.logger.info("Requesting an update to %s from node %i", image, node_id)
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
                status_enum = {
                    uavcan.node.ExecuteCommand_1.Response.STATUS_SUCCESS: "SUCCESS",
                    uavcan.node.ExecuteCommand_1.Response.STATUS_FAILURE: "FAILURE",
                    uavcan.node.ExecuteCommand_1.Response.STATUS_NOT_AUTHORIZED: "NOT AUTHORIZED",
                    uavcan.node.ExecuteCommand_1.Response.STATUS_BAD_COMMAND: "BAD COMMAND",
                    uavcan.node.ExecuteCommand_1.Response.STATUS_BAD_PARAMETER: "BAD PARAMETER",
                    uavcan.node.ExecuteCommand_1.Response.STATUS_BAD_STATE: "BAD STATE",
                    uavcan.node.ExecuteCommand_1.Response.STATUS_INTERNAL_ERROR: "INTERNAL ERROR",
                }.get(response.status, "unknown")
                self.logger.error(
                    "Software update request failed in %.1f s with status %i: %s",
                    t_response - t_start,
                    response.status,
                    status_enum,
                )
                raise RuntimeError(f"Software update request to node {node_id} failed: {status_enum}")

            self.logger.info("Node %i responded to update request in %i ms", node_id, 1000 * (t_response - t_start))

            if not wait:
                return t_response - t_start

            self.logger.debug("Waiting for node %i to finish update", node_id)
            is_node_updated = await self.wait_for_update(node_id, timeout)
            t_updated = loop.time()

            if not is_node_updated:
                raise TimeoutError(f"Node {node_id} failed to update in {timeout:.3f} seconds")
            self.logger.info("Node %i finished update in %i ms", node_id, 1000 * (t_updated - t_start))

            return t_updated - t_start

    async def wait_for_update(self, node_id: int, timeout: float = 10.0) -> bool:
        """Wait for a node to complete its software update.

        A node will be considered updated if it sends a heartbeat with mode different than `SOFTWARE_UPDATE`.

        Args:
            node_id: The ID of the node to wait for.
            timeout: Maximum time to wait in seconds.

        Returns:
            True if the update completed successfully, False if the operation timed out.
        """
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
            self.logger.warning("Software Update of node %i timed out after %.1f seconds", node_id, timeout)
            return False
        else:
            return True
        finally:
            # will actually be executed _before_ returning
            heartbeat_subscription.close()

    async def _log_diagnostic_record(
        self, record: uavcan.diagnostic.Record_1, transfer: pycyphal.transport.TransferFrom
    ) -> None:
        """Log a diagnostic record from a remote node.

        This is a handler function for a Subscriber[uavcan.diagnostic.Record_1].receive_in_background() call.

        Args:
            record: The diagnostic record to log.
            transfer: The transfer metadata associated with the record.
        """
        heartbeat, info = self.node_tracker.registry.get(transfer.source_node_id, (None, None))
        logging.getLogger("uavcan.diagnostic.record").log(
            level=UAVCAN_SEVERITY_TO_PYTHON[record.severity.value],
            msg=record.text.tobytes().decode("utf8", errors="replace"),
            extra={"record": record, "transfer": transfer, "heartbeat": heartbeat, "info": info},
        )
