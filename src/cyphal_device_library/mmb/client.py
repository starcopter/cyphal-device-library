from contextlib import AbstractContextManager, closing

import starcopter.aeric.mmb
from pycyphal.presentation import Subscriber

from ..device_client import DeviceClient


class MMBClient(DeviceClient):
    DEFAULT_NAME = "com.starcopter.test.aeric.mmb"
    DEFAULT_DUT_NAME = "com.starcopter.aeric.mmb"

    def system_status_subscription(self) -> AbstractContextManager[Subscriber[starcopter.aeric.mmb.SystemStatus_0_1]]:
        assert self.registry["uavcan.pub.system_status.type"].value == "starcopter.aeric.mmb.SystemStatus.0.1"
        sub = self.get_subscription("system_status")
        assert sub.dtype == starcopter.aeric.mmb.SystemStatus_0_1
        return closing(sub)
