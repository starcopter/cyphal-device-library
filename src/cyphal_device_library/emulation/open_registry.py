"""Open register Access for emulated Cyphal nodes.

pycyphal's built-in ``uavcan.register.Access`` server returns an empty value for names
that are not already in the local registry. Emulated devices instead accept every
requested name: a first read creates the register with empty string data, a first write
creates it with the written value, and later accesses read or update the stored value.
"""

from __future__ import annotations

from typing import Any

from pycyphal.application.register import String, ValueConversionError
from pycyphal.presentation import ServiceRequestMetadata
from uavcan.register import Access_1 as Access


def install_auto_creating_register_access(node: Any) -> None:
    """Replace the node's Access server so unknown registers are created on demand.

    Must be called after ``node.start()`` so this handler replaces pycyphal's default
    Access task (``serve_in_background`` cancels the previous one).
    """
    auto_created: set[str] = set()

    def _lookup(name: str) -> Any | None:
        try:
            return node.registry[name]
        except KeyError:
            return None

    async def handle_access(request: Access.Request, _metadata: ServiceRequestMetadata) -> Access.Response:
        name = request.name.name.tobytes().decode("utf8", "ignore")
        is_write = not request.value.empty
        current = _lookup(name)

        if current is None:
            node.registry[name] = request.value if is_write else String("")
            auto_created.add(name)
            current = node.registry[name]
        elif is_write and current.mutable:
            try:
                current.assign(request.value)
                node.registry[name] = current
            except ValueConversionError:
                if name in auto_created:
                    del node.registry[name]
                    node.registry[name] = request.value
            current = node.registry[name]

        return Access.Response(
            mutable=current.mutable,
            persistent=current.persistent,
            value=current.value,
        )

    node.get_server(Access).serve_in_background(handle_access)
