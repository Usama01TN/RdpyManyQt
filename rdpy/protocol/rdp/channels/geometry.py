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
Geometry Tracking Virtual Channel Extension
("Microsoft::Windows::RDS::Geometry::v08.01", MS-RDPEGT): the server tells
where the window playing a redirected video is (its top level window
rectangle, its client area relative to it, and the visible region). Used by
the video channel (MS-RDPEVOR) to place the video.
Ported from FreeRDP channels/geometry/client/geometry_main.c.
"""
from __future__ import absolute_import, division, print_function
from struct import error, unpack_from
from threading import Lock
from rdpy.core import log

CHANNEL_NAME = u'Microsoft::Windows::RDS::Geometry::v08.01'
GEOMETRY_UPDATE = 1
GEOMETRY_CLEAR = 2
RDH_RECTANGLE = 1
INT16_MAX = 0x7FFF


class MappedGeometry(object):
    """A mapping (FreeRDP MAPPED_GEOMETRY)."""

    def __init__(self, mappingId):
        self.mappingId = mappingId
        self.topLevelId = 0
        self.left = self.top = self.right = self.bottom = 0
        self.topLevelLeft = self.topLevelTop = self.topLevelRight = self.topLevelBottom = 0
        self.boundingRect = (0, 0, 0, 0)  # x, y, width, height
        self.rects = []  # [(x, y, width, height)]

    @property
    def origin(self):
        """@return: (x, y) of the content on the desktop (FreeRDP video: topLevel + client offset)"""
        return self.topLevelLeft + self.left, self.topLevelTop + self.top


def _rect(x, y, right, bottom):
    if abs(x) > INT16_MAX or abs(y) > INT16_MAX:
        raise ValueError(u'rectangle origin out of range')
    w, h = right - x, bottom - y
    if abs(w) > INT16_MAX or abs(h) > INT16_MAX or x > right or y > bottom:
        raise ValueError(u'invalid rectangle')
    return x, y, w, h


def readRgnData(data, offset, length):
    """@summary: FreeRDP geometry_read_RGNDATA: (boundingRect, rects)"""
    if length < 32:
        raise ValueError(u'RGNDATA too short')
    dwSize, iType, count, _ = unpack_from('<IIII', data, offset)
    if dwSize != 32:
        raise ValueError(u'RGNDATAHEADER.dwSize %d' % dwSize)
    if iType != RDH_RECTANGLE:
        raise ValueError(u'RGNDATAHEADER.iType %d' % iType)
    bounding = _rect(*unpack_from('<iiii', data, offset + 16))
    if (length - 32) // 16 < count:
        raise ValueError(u'RGNDATA: %d rectangles do not fit' % count)
    rects = [_rect(*unpack_from('<iiii', data, offset + 32 + 16 * i)) for i in range(count)]
    return bounding, rects


class Geometry(object):
    """
    @summary: The geometry plugin; listeners get ('update', MappedGeometry, isNew) and
    ('clear', MappedGeometry) events
    """

    def __init__(self):
        self.enabled = False  # FreeRDP /geometry (and /video)
        self.remoteVersion = 0
        self.geometries = {}  # mappingId -> MappedGeometry
        self._listeners = []
        self._lock = Lock()

    def addListener(self, callback):
        self._listeners.append(callback)

    def get(self, mappingId):
        with self._lock:
            return self.geometries.get(mappingId)

    def channelFactory(self, channel):
        if not self.enabled:
            return None
        return _GeometryChannel(self, channel)

    def _notify(self, *event):
        for callback in list(self._listeners):
            try:
                callback(*event)
            except Exception as e:
                log.warning(u'Geometry listener failed: %r' % (e,))

    def recvPdu(self, data):
        """FreeRDP geometry_recv_pdu."""
        if len(data) < 4:
            raise ValueError(u'Short geometry PDU')
        length = unpack_from('<I', data, 0)[0]
        if length < 24 or len(data) < length:  # FreeRDP: the header (24 bytes) at least
            raise ValueError(u'Invalid geometry PDU length %d' % length)
        version, mappingId, updateType, _flags = unpack_from('<IQII', data, 4)
        self.remoteVersion = version
        if updateType == GEOMETRY_CLEAR:
            with self._lock:
                geometry = self.geometries.pop(mappingId, None)
            if geometry is not None:
                self._notify('clear', geometry)
            return
        if updateType != GEOMETRY_UPDATE:
            return
        with self._lock:
            geometry = self.geometries.get(mappingId)
        isNew = geometry is None
        g = MappedGeometry(mappingId) if isNew else geometry
        if len(data) < 24 + 48:
            raise ValueError(u'Short geometry update.')
        pos = 24
        g.topLevelId = unpack_from('<Q', data, pos)[0]
        left, top, right, bottom = unpack_from('<iiii', data, pos + 8)
        if left > right or top > bottom:
            raise ValueError(u'Invalid geometry rectangle.')
        tl, tt, tr, tb = unpack_from('<iiii', data, pos + 24)
        if tl > tr or tt > tb:
            raise ValueError(u'Invalid top level rectangle.')
        geometryType, cbGeometryBuffer = unpack_from('<II', data, pos + 40)
        if geometryType != 0x02:
            raise ValueError(u'GeometryType 0x%x' % geometryType)
        if len(data) < pos + 48 + cbGeometryBuffer:
            raise ValueError(u'Truncated geometry buffer.')
        g.left, g.top, g.right, g.bottom = left, top, right, bottom
        g.topLevelLeft, g.topLevelTop, g.topLevelRight, g.topLevelBottom = tl, tt, tr, tb
        if cbGeometryBuffer > 0:
            g.boundingRect, g.rects = readRgnData(data, pos + 48, cbGeometryBuffer)
        else:
            g.rects = []
        if isNew:
            with self._lock:
                self.geometries[mappingId] = g
        self._notify('update', g, isNew)

    def describe(self):
        return ('%d mapping(s)' % len(self.geometries)) if self.geometries else None


class _GeometryChannel(object):
    def __init__(self, plugin, channel):
        self.plugin = plugin
        self.channel = channel

    def recv(self, data):
        try:
            self.plugin.recvPdu(bytes(data))
        except (ValueError, error) as e:
            log.warning(u'Geometry: bad PDU: %s' % e)

    def closed(self):
        """
        :return:
        """
