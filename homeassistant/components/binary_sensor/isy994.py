"""
Support for ISY994 binary sensors.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/binary_sensor.isy994/
"""

import asyncio
import logging
from datetime import timedelta
from typing import Callable  # noqa

from homeassistant.core import callback
from homeassistant.components.binary_sensor import BinarySensorDevice, DOMAIN
import homeassistant.components.isy994 as isy
from homeassistant.const import STATE_ON, STATE_OFF
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

UOM = ['2', '78']
STATES = [STATE_OFF, STATE_ON, 'true', 'false']

ISY_DEVICE_TYPES = {
    'moisture': ['16.8', '16.13', '16.14'],
    'opening': ['16.9', '16.6', '16.7', '16.2', '16.17', '16.20', '16.21'],
    'motion': ['16.1', '16.4', '16.5', '16.3']
}


# pylint: disable=unused-argument
def setup_platform(hass, config: ConfigType,
                   add_devices: Callable[[list], None], discovery_info=None):
    """Set up the ISY994 binary sensor platform."""
    if isy.ISY is None or not isy.ISY.connected:
        _LOGGER.error("A connection has not been made to the ISY controller")
        return False

    devices = []
    devices_by_nid = {}
    child_nodes = []

    for node in isy.filter_nodes(isy.SENSOR_NODES, units=UOM,
                                 states=STATES):
        if node.parent_node is None:
            device = ISYBinarySensorDevice(node)
            devices.append(device)
            devices_by_nid[node.nid] = device
        else:
            # We'll process the child nodes last, to ensure all parent nodes
            # have been processed
            child_nodes.append(node)

    for node in child_nodes:
        try:
            parent_device = devices_by_nid[node.parent_node.nid]
        except KeyError:
            _LOGGER.error("Node %s has a parent node %s, but no device "
                          "was created for the parent. Skipping.",
                          node.nid, node.parent_nid)
        else:
            device_type = _detect_device_type(node)
            if device_type in ['moisture', 'opening']:
                subnode_id = int(node.nid[-1])
                # Leak and door/window sensors work the same way with negative
                # nodes and heartbeat nodes
                if subnode_id == 4:
                    # Subnode 4 is the heartbeat node, which we will represent
                    # as a separate binary_sensor
                    device = ISYBinarySensorHeartbeat(node, parent_device)
                    parent_device.add_heartbeat_device(device)
                    devices.append(device)
                elif subnode_id == 2:
                    parent_device.add_negative_node(node)
            else:
                # We don't yet have any special logic for other sensor types,
                # so add the nodes as individual devices
                device = ISYBinarySensorDevice(node)
                devices.append(device)

    for program in isy.PROGRAMS.get(DOMAIN, []):
        try:
            status = program[isy.KEY_STATUS]
        except (KeyError, AssertionError):
            pass
        else:
            devices.append(ISYBinarySensorProgram(program.name, status))

    add_devices(devices)


def _detect_device_type(node) -> str:
    try:
        device_type = node.type
    except AttributeError:
        # The type attribute didn't exist in the ISY's API response
        return None

    split_type = device_type.split('.')
    for device_class, ids in ISY_DEVICE_TYPES.items():
        if '{}.{}'.format(split_type[0], split_type[1]) in ids:
            return device_class

    return None


def _is_val_unknown(val):
    """Determine if a number value represents UNKNOWN from PyISY."""
    return val == -1*float('inf')


class ISYBinarySensorDevice(isy.ISYDevice, BinarySensorDevice):
    """Representation of an ISY994 binary sensor device.

    Often times, a single device is represented by multiple nodes in the ISY,
    allowing for different nuances in how those devices report their on and
    off events. This class turns those multiple nodes in to a single Hass
    entity and handles both ways that ISY binary sensors can work.
    """

    def __init__(self, node) -> None:
        """Initialize the ISY994 binary sensor device."""
        super().__init__(node)
        self._negative_node = None
        self._heartbeat_device = None
        self._device_class_from_type = _detect_device_type(self._node)
        # pylint: disable=protected-access
        if _is_val_unknown(self._node.status._val):
            self._computed_state = None
        else:
            self._computed_state = bool(self._node.status._val)

    @asyncio.coroutine
    def async_added_to_hass(self) -> None:
        """Subscribe to the node and subnode event emitters."""
        yield from super().async_added_to_hass()

        self._node.controlEvents.subscribe(self._positive_node_control_handler)

        if self._negative_node is not None:
            self._negative_node.controlEvents.subscribe(
                self._negative_node_control_handler)

    def add_heartbeat_device(self, device) -> None:
        """Register a heartbeat device for this sensor.

        The heartbeat node beats on its own, but we can gain a little
        reliability by considering any node activity for this sensor
        to be a heartbeat as well.
        """
        self._heartbeat_device = device

    def _heartbeat(self) -> None:
        """Send a heartbeat to our heartbeat device, if we have one."""
        if self._heartbeat_device is not None:
            self._heartbeat_device.heartbeat()

    def add_negative_node(self, child) -> None:
        """Add a negative node to this binary sensor device.

        The negative node is a node that can receive the 'off' events
        for the sensor, depending on device configuration and type.
        """
        self._negative_node = child

        if not _is_val_unknown(self._negative_node):
            # If the negative node has a value, it means the negative node is
            # in use for this device. Therefore, we cannot determine the state
            # of the sensor until we receive our first ON event.
            self._computed_state = None

    def _negative_node_control_handler(self, event: object) -> None:
        """Handle an "On" control event from the "negative" node."""
        if event == 'DON':
            _LOGGER.debug("Sensor %s turning Off via the Negative node "
                          "sending a DON command", self.name)
            self._computed_state = False
            self.schedule_update_ha_state()
            self._heartbeat()

    def _positive_node_control_handler(self, event: object) -> None:
        """Handle On and Off control event coming from the primary node.

        Depending on device configuration, sometimes only On events
        will come to this node, with the negative node representing Off
        events
        """
        if event == 'DON':
            _LOGGER.debug("Sensor %s turning On via the Primary node "
                          "sending a DON command", self.name)
            self._computed_state = True
            self.schedule_update_ha_state()
            self._heartbeat()
        if event == 'DOF':
            _LOGGER.debug("Sensor %s turning Off via the Primary node "
                          "sending a DOF command", self.name)
            self._computed_state = False
            self.schedule_update_ha_state()
            self._heartbeat()

    # pylint: disable=unused-argument
    def on_update(self, event: object) -> None:
        """Ignore primary node status updates.

        We listen directly to the Control events on all nodes for this
        device.
        """
        pass

    @property
    def value(self) -> object:
        """Get the current value of the device.

        Insteon leak sensors set their primary node to On when the state is
        DRY, not WET, so we invert the binary state if the user indicates
        that it is a moisture sensor.
        """
        if self._computed_state is None:
            # Do this first so we don't invert None on moisture sensors
            return None

        if self.device_class == 'moisture':
            return not self._computed_state

        return self._computed_state

    @property
    def is_on(self) -> bool:
        """Get whether the ISY994 binary sensor device is on.

        Note: This method will return false if the current state is UNKNOWN
        """
        return bool(self.value)

    @property
    def state(self):
        """Return the state of the binary sensor."""
        if self._computed_state is None:
            return None
        return STATE_ON if self.is_on else STATE_OFF

    @property
    def device_class(self) -> str:
        """Return the class of this device.

        This was discovered by parsing the device type code during init
        """
        return self._device_class_from_type


class ISYBinarySensorHeartbeat(isy.ISYDevice, BinarySensorDevice):
    """Representation of the battery state of an ISY994 sensor."""

    def __init__(self, node, parent_device) -> None:
        """Initialize the ISY994 binary sensor device."""
        super().__init__(node)
        self._computed_state = None
        self._parent_device = parent_device
        self._heartbeat_timer = None

    @asyncio.coroutine
    def async_added_to_hass(self) -> None:
        """Subscribe to the node and subnode event emitters."""
        yield from super().async_added_to_hass()

        self._node.controlEvents.subscribe(
            self._heartbeat_node_control_handler)

        # Start the timer on bootup, so we can change from UNKNOWN to ON
        self._restart_timer()

    def _heartbeat_node_control_handler(self, event: object) -> None:
        """Update the heartbeat timestamp when an On event is sent."""
        if event == 'DON':
            self.heartbeat()

    def heartbeat(self):
        """Mark the device as online, and restart the 25 hour timer.

        This gets called when the heartbeat node beats, but also when the
        parent sensor sends any events, as we can trust that to mean the device
        is online. This mitigates the risk of false positives due to a single
        missed heartbeat event.
        """
        self._computed_state = False
        self._restart_timer()
        self.schedule_update_ha_state()

    def _restart_timer(self):
        """Restart the 25 hour timer."""
        try:
            self._heartbeat_timer()
            self._heartbeat_timer = None
        except TypeError:
            # No heartbeat timer is active
            pass

        # pylint: disable=unused-argument
        @callback
        def timer_elapsed(now) -> None:
            """Heartbeat missed; set state to indicate dead battery."""
            self._computed_state = True
            self._heartbeat_timer = None
            self.schedule_update_ha_state()

        point_in_time = dt_util.utcnow() + timedelta(hours=25)
        _LOGGER.debug("Timer starting. Now: %s Then: %s",
                      dt_util.utcnow(), point_in_time)

        self._heartbeat_timer = async_track_point_in_utc_time(
            self.hass, timer_elapsed, point_in_time)

    # pylint: disable=unused-argument
    def on_update(self, event: object) -> None:
        """Ignore node status updates.

        We listen directly to the Control events for this device.
        """
        pass

    @property
    def value(self) -> object:
        """Get the current value of this sensor."""
        return self._computed_state

    @property
    def is_on(self) -> bool:
        """Get whether the ISY994 binary sensor device is on.

        Note: This method will return false if the current state is UNKNOWN
        """
        return bool(self.value)

    @property
    def state(self):
        """Return the state of the binary sensor."""
        if self._computed_state is None:
            return None
        return STATE_ON if self.is_on else STATE_OFF

    @property
    def device_class(self) -> str:
        """Get the class of this device."""
        return 'battery'

    @property
    def device_state_attributes(self):
        """Get the state attributes for the device."""
        attr = super().device_state_attributes
        attr['parent_entity_id'] = self._parent_device.entity_id
        return attr


class ISYBinarySensorProgram(isy.ISYDevice, BinarySensorDevice):
    """Representation of an ISY994 binary sensor program.

    This does not need all of the subnode logic in the device version of binary
    sensors.
    """

    def __init__(self, name, node) -> None:
        """Initialize the ISY994 binary sensor program."""
        super().__init__(node)
        self._name = name

    @property
    def is_on(self) -> bool:
        """Get whether the ISY994 binary sensor device is on."""
        return bool(self.value)
