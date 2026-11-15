# coding=utf-8
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
#
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
#
# rdpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
"""
Location Virtual Channel Extension ("Microsoft::Windows::RDS::Location",
MS-RDPEL): the client gives its geographic position to the session (Windows
location services, maps, weather...).
Ported from FreeRDP channels/location/client/location_main.c and the
FOUR_BYTE_FLOAT / FOUR_BYTE_SIGNED_INTEGER encodings of
libfreerdp/utils/encoded_types.c. The server sends Server Ready; the client
answers Client Ready with the negotiated version (only when it has a location
source, like FreeRDP) and then sends its position (Base Location 3D) each time
it changes.
"""
from __future__ import absolute_import, division, print_function
from struct import pack, unpack_from
from math import modf, pow
from threading import Lock
from rdpy.core import log

CHANNEL_NAME = u'Microsoft::Windows::RDS::Location'
PDUTYPE_SERVER_READY = 0x0001
PDUTYPE_CLIENT_READY = 0x0002
PDUTYPE_BASE_LOCATION3D = 0x0003
PDUTYPE_LOCATION2D_DELTA = 0x0004
PDUTYPE_LOCATION3D_DELTA = 0x0005
LOCATION_HEADER_SIZE = 6
RDPLOCATION_PROTOCOL_VERSION_100 = 0x00010000
RDPLOCATION_PROTOCOL_VERSION_200 = 0x00020000
LOCATIONSOURCE_IP = 0x00
LOCATIONSOURCE_WIFI = 0x01
LOCATIONSOURCE_CELL = 0x02
LOCATIONSOURCE_GNSS = 0x03
FOUR_BYTE_SIGNED_INT_MAX = 0x1FFFFFFF
FOUR_BYTE_FLOAT_MAX = 0x3FFFFFF


def encodeFourByteSignedInteger(value):
    """@summary: FreeRDP freerdp_write_four_byte_signed_integer (MS-RDPEL 2.2.1.1)"""
    value = int(value)
    if not -FOUR_BYTE_SIGNED_INT_MAX <= value <= FOUR_BYTE_SIGNED_INT_MAX:
        raise ValueError(u'FOUR_BYTE_SIGNED_INTEGER out of range: %d' % value)
    sign = 1 if value < 0 else 0
    value = abs(value)
    for c, limit in ((0, 0x1F), (1, 0x1FFF), (2, 0x1FFFFF), (3, 0x1FFFFFFF)):
        if value <= limit:
            break
    first = ((c << 6) & 0xC0) | ((sign << 5) & 0x20) | ((value >> (8 * c)) & 0x1F)
    return bytes(bytearray([first] + [(value >> (8 * i)) & 0xFF for i in range(c - 1, -1, -1)]))


def encodeFourByteFloat(value):
    """@summary: FreeRDP freerdp_write_four_byte_float (MS-RDPEL 2.2.1.2): sign, decimal
    exponent (digits after the point, 0-7) and a 26-bit base"""
    value = float(value)
    if not -FOUR_BYTE_FLOAT_MAX <= value <= FOUR_BYTE_FLOAT_MAX:
        raise ValueError(u'FOUR_BYTE_FLOAT out of range: %r' % value)
    sign = 1 if value < 0 else 0
    aval = abs(value)
    frac = modf(aval)[0]
    exp = 0
    if frac != 0.0:
        maxfrac = frac * 10000000.0
        exp = 7
        for e, limit in enumerate((1.0, 10.0, 100.0, 1000.0, 10000.0, 100000.0, 1000000.0)):
            if maxfrac <= limit:
                exp = e
                break
    base = 0
    while exp >= 0:
        base = int(aval * pow(10.0, exp))
        if base <= 0x03FFFFFF:
            break
        exp -= 1
    if exp < 0:
        raise ValueError(u'FOUR_BYTE_FLOAT can not hold %r' % value)
    for c, limit in ((0, 0x03), (1, 0x03FF), (2, 0x03FFFF), (3, 0x03FFFFFF)):
        if base <= limit:
            break
    first = ((c << 6) & 0xC0) | ((sign << 5) & 0x20) | ((exp << 2) & 0x1C) | ((base >> (8 * c)) & 0x03)
    return bytes(bytearray([first] + [(base >> (8 * i)) & 0xFF for i in range(c - 1, -1, -1)]))


def pdu(pduType, body):
    """@summary: RDPLOCATION_HEADER (pduType, pduLength with the header) + body"""
    return pack('<HI', pduType, LOCATION_HEADER_SIZE + len(body)) + body


def baseLocation3D(latitude, longitude, altitude=0, speed=None, heading=None, accuracy=None, source=None):
    """@summary: PDUTYPE_BASE_LOCATION3D (FreeRDP location_send_base_location3d); speed,
    heading, horizontal accuracy and source are sent together or not at all"""
    body = encodeFourByteFloat(latitude) + encodeFourByteFloat(longitude) + \
        encodeFourByteSignedInteger(int(round(altitude)))
    if source is not None:
        body += encodeFourByteFloat(speed or 0.0) + encodeFourByteFloat(heading or 0.0) + \
            encodeFourByteFloat(accuracy or 0.0) + pack('<B', int(source))
    return pdu(PDUTYPE_BASE_LOCATION3D, body)


class Location(object):
    """
    @summary: The location plugin. setPosition() (any thread) gives the position;
    with no position the channel stays silent, as FreeRDP without a LocationStart.
    """

    def __init__(self):
        self.enabled = False
        self.serverVersion = 0
        self.clientVersion = 0
        self._position = None   # (latitude, longitude, altitude, speed, heading, accuracy, source)
        self._handler = None
        self._sent = None
        self._lock = Lock()

    def channelFactory(self, channel):
        if not self.enabled:
            return None
        self._handler = _LocationChannel(self, channel)
        return self._handler

    def setPosition(self, latitude, longitude, altitude=0, speed=None, heading=None, accuracy=None,
                    source=LOCATIONSOURCE_IP):
        """@summary: The current position (degrees, metres); sent once the channel is ready"""
        with self._lock:
            self._position = (float(latitude), float(longitude), int(round(altitude or 0)),
                              speed, heading, accuracy, source)
        handler = self._handler
        if handler is not None and handler.serverReady and not handler.ready:
            self._ready(handler)  # the position came after Server Ready
        else:
            self._flush()

    def _ready(self, handler):
        with self._lock:
            ready = handler is self._handler and self._position is not None
        if ready:
            handler.channel.write(pdu(PDUTYPE_CLIENT_READY, pack('<II', self.clientVersion, 0)))
            handler.ready = True
            log.info(u'location: the session receives this computer\'s position')
            self._sent = None
            self._flush()
        else:
            log.info(u'location: no position available, nothing is sent')

    def _flush(self):
        handler = self._handler
        with self._lock:
            position = self._position
        if handler is None or not getattr(handler, 'ready', False) or position is None or position == self._sent:
            return
        lat, lon, alt, speed, heading, accuracy, source = position
        withExtra = self.clientVersion >= RDPLOCATION_PROTOCOL_VERSION_200 and None not in (speed, heading, accuracy)
        handler.channel.write(baseLocation3D(lat, lon, alt, speed, heading, accuracy, source if withExtra else None))
        self._sent = position

    def _closed(self, handler):
        if handler is self._handler:
            self._handler = None

    def describe(self):
        if self._sent is not None:
            return '%.5f, %.5f' % (self._sent[0], self._sent[1])
        return None


class _LocationChannel(object):
    def __init__(self, plugin, channel):
        self.plugin = plugin
        self.channel = channel
        self.ready = False
        self.serverReady = False

    def recv(self, data):
        data = bytes(data)
        if len(data) < LOCATION_HEADER_SIZE:
            return
        pduType, pduLength = unpack_from('<HI', data, 0)
        if pduLength < LOCATION_HEADER_SIZE:
            return
        if pduType == PDUTYPE_SERVER_READY and pduLength >= LOCATION_HEADER_SIZE + 4 and len(data) >= 10:
            version = unpack_from('<I', data, 6)[0]
            plugin = self.plugin
            plugin.serverVersion = version
            # FreeRDP location_on_data_received version choice
            if version in (RDPLOCATION_PROTOCOL_VERSION_100, RDPLOCATION_PROTOCOL_VERSION_200):
                plugin.clientVersion = version
            else:
                plugin.clientVersion = RDPLOCATION_PROTOCOL_VERSION_200 \
                    if version > RDPLOCATION_PROTOCOL_VERSION_200 else RDPLOCATION_PROTOCOL_VERSION_100
            self.serverReady = True
            plugin._ready(self)

    def closed(self):
        self.plugin._closed(self)


class QtPositionSource(object):
    """
    @summary: Position from Qt Positioning (QGeoPositionInfoSource, Qt 5 / 6), when the
    binding ships it; feeds Location.setPosition from the GUI thread
    """

    def __init__(self, location):
        self.location = location
        self.source = None

    def start(self):
        from ManyQt.QtPositioning import QGeoPositionInfoSource
        self.source = QGeoPositionInfoSource.createDefaultSource(None)
        if self.source is None:
            log.warning(u'location: no position source on this computer, use --location LAT,LON')
            return False
        self.source.positionUpdated.connect(self._update)
        self.source.startUpdates()
        return True

    def _update(self, info):
        c = info.coordinate()
        alt = c.altitude()
        self.location.setPosition(c.latitude(), c.longitude(), 0 if alt != alt else alt)
