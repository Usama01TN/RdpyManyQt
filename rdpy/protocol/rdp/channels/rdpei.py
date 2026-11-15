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
Input Virtual Channel Extension ("Microsoft::Windows::RDS::Input", MS-RDPEI):
multitouch and pen input. The server sends SC_READY with its protocol
version; the client answers CS_READY and then sends touch frames (up to 64
contacts) and, from protocol 2.0, pen frames (pressure, rotation, tilt,
eraser...). Mouse and keyboard keep using the usual input PDUs.
Ported from FreeRDP channels/rdpei (client/rdpei_main.c, rdpei_common.c):
same encodings (TWO / FOUR / EIGHT_BYTE integers), contact slots, contact
flags (begin: DOWN|INRANGE|INCONTACT, update: UPDATE|INRANGE|INCONTACT, end:
an update then UP, cancel: UP|CANCELED), contact rectangles of +/- 2 pixels.
Frames are sent as touch events arrive (FreeRDP batches them every 20 ms).
"""
from __future__ import absolute_import, division, print_function
from struct import pack, unpack_from
from threading import Lock
from rdpy.core.compat import monotonic
from rdpy.core import log

CHANNEL_NAME = u'Microsoft::Windows::RDS::Input'
EVENTID_SC_READY = 0x0001
EVENTID_CS_READY = 0x0002
EVENTID_TOUCH = 0x0003
EVENTID_SUSPEND_TOUCH = 0x0004
EVENTID_RESUME_TOUCH = 0x0005
EVENTID_DISMISS_HOVERING_CONTACT = 0x0006
EVENTID_PEN = 0x0008
RDPINPUT_HEADER_LENGTH = 6
RDPINPUT_PROTOCOL_V10 = 0x00010000
RDPINPUT_PROTOCOL_V101 = 0x00010001
RDPINPUT_PROTOCOL_V200 = 0x00020000
RDPINPUT_PROTOCOL_V300 = 0x00030000
SC_READY_MULTIPEN_INJECTION_SUPPORTED = 0x0001
CS_READY_FLAGS_SHOW_TOUCH_VISUALS = 0x00000001
CS_READY_FLAGS_DISABLE_TIMESTAMP_INJECTION = 0x00000002
CS_READY_FLAGS_ENABLE_MULTIPEN_INJECTION = 0x00000004
CONTACT_DATA_CONTACTRECT_PRESENT = 0x0001
CONTACT_DATA_ORIENTATION_PRESENT = 0x0002
CONTACT_DATA_PRESSURE_PRESENT = 0x0004
RDPINPUT_PEN_CONTACT_PENFLAGS_PRESENT = 0x0001
RDPINPUT_PEN_CONTACT_PRESSURE_PRESENT = 0x0002
RDPINPUT_PEN_CONTACT_ROTATION_PRESENT = 0x0004
RDPINPUT_PEN_CONTACT_TILTX_PRESENT = 0x0008
RDPINPUT_PEN_CONTACT_TILTY_PRESENT = 0x0010
RDPINPUT_CONTACT_FLAG_DOWN = 0x0001
RDPINPUT_CONTACT_FLAG_UPDATE = 0x0002
RDPINPUT_CONTACT_FLAG_UP = 0x0004
RDPINPUT_CONTACT_FLAG_INRANGE = 0x0008
RDPINPUT_CONTACT_FLAG_INCONTACT = 0x0010
RDPINPUT_CONTACT_FLAG_CANCELED = 0x0020
RDPINPUT_PEN_FLAG_BARREL_PRESSED = 0x0001
RDPINPUT_PEN_FLAG_ERASER_PRESSED = 0x0002
RDPINPUT_PEN_FLAG_INVERTED = 0x0004
MAX_CONTACTS = 64
MAX_PEN_CONTACTS = 4
FLAGS_BEGIN = RDPINPUT_CONTACT_FLAG_DOWN | RDPINPUT_CONTACT_FLAG_INRANGE | RDPINPUT_CONTACT_FLAG_INCONTACT
FLAGS_UPDATE = RDPINPUT_CONTACT_FLAG_UPDATE | RDPINPUT_CONTACT_FLAG_INRANGE | RDPINPUT_CONTACT_FLAG_INCONTACT
FLAGS_UP = RDPINPUT_CONTACT_FLAG_UP
FLAGS_CANCEL = RDPINPUT_CONTACT_FLAG_UP | RDPINPUT_CONTACT_FLAG_CANCELED
FLAGS_HOVER = RDPINPUT_CONTACT_FLAG_UPDATE | RDPINPUT_CONTACT_FLAG_INRANGE
FLAGS_HOVER_CANCEL = RDPINPUT_CONTACT_FLAG_UPDATE | RDPINPUT_CONTACT_FLAG_CANCELED
FLAGS_PEN_UP = RDPINPUT_CONTACT_FLAG_UP | RDPINPUT_CONTACT_FLAG_INRANGE


# ---- MS-RDPEI variable length integers (FreeRDP rdpei_common.c) ----

def twoByteUnsigned(value):
    if not 0 <= value <= 0x7FFF:
        raise ValueError(u'TWO_BYTE_UNSIGNED_INTEGER %d' % value)
    if value >= 0x7F:
        return pack('BB', ((value & 0x7F00) >> 8) | 0x80, value & 0xFF)
    return pack('B', value & 0x7F)


def twoByteSigned(value):
    negative = value < 0
    value = abs(int(value))
    if value > 0x3FFF:
        raise ValueError(u'TWO_BYTE_SIGNED_INTEGER %d' % value)
    if value >= 0x3F:
        return pack('BB', ((value & 0x3F00) >> 8) | (0x40 if negative else 0) | 0x80, value & 0xFF)
    return pack('B', (value & 0x3F) | (0x40 if negative else 0))


def fourByteUnsigned(value):
    value = int(value)
    for c, limit in ((0, 0x3F), (1, 0x3FFF), (2, 0x3FFFFF), (3, 0x3FFFFFFF)):
        if 0 <= value <= limit:
            first = (c << 6) | ((value >> (8 * c)) & 0x3F)
            return bytes(bytearray([first] + [(value >> (8 * i)) & 0xFF for i in range(c - 1, -1, -1)]))
    raise ValueError(u'FOUR_BYTE_UNSIGNED_INTEGER %d' % value)


def fourByteSigned(value):
    negative = value < 0
    value = abs(int(value))
    for c, limit in ((0, 0x1F), (1, 0x1FFF), (2, 0x1FFFFF), (3, 0x1FFFFFFF)):
        if value <= limit:
            first = (c << 6) | (0x20 if negative else 0) | ((value >> (8 * c)) & 0x1F)
            return bytes(bytearray([first] + [(value >> (8 * i)) & 0xFF for i in range(c - 1, -1, -1)]))
    raise ValueError(u'FOUR_BYTE_SIGNED_INTEGER %d' % value)


def eightByteUnsigned(value):
    """EIGHT_BYTE_UNSIGNED_INTEGER: 3-bit byte count + 5 high bits, then full bytes (MS-RDPEI 2.2.2.4)."""
    value = int(value)
    for c in range(8):
        if 0 <= value <= (1 << (5 + 8 * c)) - 1:
            first = (c << 5) | ((value >> (8 * c)) & 0x1F)
            return bytes(bytearray([first] + [(value >> (8 * i)) & 0xFF for i in range(c - 1, -1, -1)]))
    raise ValueError(u'EIGHT_BYTE_UNSIGNED_INTEGER %d' % value)


def eventPdu(eventId, body):
    """@summary: RDPINPUT_HEADER (eventId, pduLength with the header) + body"""
    return pack('<HI', eventId, RDPINPUT_HEADER_LENGTH + len(body)) + body


def _bounded(value):
    return max(-0x3FFF, min(0x3FFF, int(value)))


def touchFrame(contacts, frameOffsetMs):
    """
    @summary: RDPINPUT_TOUCH_FRAME (FreeRDP rdpei_write_touch_frame): contactCount,
    frameOffset in microseconds, then each contact with a +/- 2 pixel contact rectangle
    @param contacts: [(contactId, x, y, contactFlags, orientation|None, pressure|None)]
    """
    out = twoByteUnsigned(len(contacts)) + eightByteUnsigned(int(frameOffsetMs) * 1000)
    for contactId, x, y, flags, orientation, pressure in contacts:
        fields = CONTACT_DATA_CONTACTRECT_PRESENT
        if orientation is not None:
            fields |= CONTACT_DATA_ORIENTATION_PRESENT
        if pressure is not None:
            fields |= CONTACT_DATA_PRESSURE_PRESENT
        out += pack('B', contactId) + twoByteUnsigned(fields) + fourByteSigned(x) + fourByteSigned(y) + \
            fourByteUnsigned(flags)
        out += twoByteSigned(_bounded(x - 2)) + twoByteSigned(_bounded(y - 2)) + \
            twoByteSigned(_bounded(x + 2)) + twoByteSigned(_bounded(y + 2))
        if orientation is not None:
            out += fourByteUnsigned(min(359, max(0, int(orientation))))
        if pressure is not None:
            out += fourByteUnsigned(min(1024, max(0, int(pressure))))
    return out


def penFrame(contacts, frameOffset):
    """
    @summary: RDPINPUT_PEN_FRAME (FreeRDP rdpei_write_pen_frame)
    @param contacts: [(deviceId, x, y, contactFlags, penFlags|None, pressure|None, rotation|None,
                       tiltX|None, tiltY|None)]
    """
    out = twoByteUnsigned(len(contacts)) + eightByteUnsigned(int(frameOffset))
    for deviceId, x, y, flags, penFlags, pressure, rotation, tiltX, tiltY in contacts:
        fields = 0
        extra = b''
        if penFlags is not None:
            fields |= RDPINPUT_PEN_CONTACT_PENFLAGS_PRESENT
            extra += fourByteUnsigned(penFlags)
        if pressure is not None:
            fields |= RDPINPUT_PEN_CONTACT_PRESSURE_PRESENT
            extra += fourByteUnsigned(min(1024, max(0, int(pressure))))
        if rotation is not None:
            fields |= RDPINPUT_PEN_CONTACT_ROTATION_PRESENT
            extra += twoByteUnsigned(int(rotation) % 360)
        if tiltX is not None:
            fields |= RDPINPUT_PEN_CONTACT_TILTX_PRESENT
            extra += twoByteSigned(max(-90, min(90, int(tiltX))))
        if tiltY is not None:
            fields |= RDPINPUT_PEN_CONTACT_TILTY_PRESENT
            extra += twoByteSigned(max(-90, min(90, int(tiltY))))
        out += pack('B', deviceId) + twoByteUnsigned(fields) + fourByteSigned(x) + fourByteSigned(y) + \
            fourByteUnsigned(flags) + extra
    return out


class Rdpei(object):
    """
    @summary: The rdpei plugin. touch*() / pen*() may be called from any thread;
    they return False when the channel is not ready (the caller then uses mouse events).
    """

    def __init__(self):
        self.enabled = False                 # FreeRDP /multitouch
        self.version = RDPINPUT_PROTOCOL_V300
        self.features = 0
        self.maxTouchContacts = MAX_CONTACTS
        self.clientFeaturesMask = 0xFFFFFFFF
        self.suspended = False
        self._channel = None
        self._ready = False
        self._slots = {}                     # externalId -> contactId (FreeRDP rdpei_contact)
        self._contacts = {}                  # contactId -> last (x, y, flags)
        self._previousFrame = 0.0
        self._previousPen = 0.0
        self._lock = Lock()

    def channelFactory(self, channel):
        if not self.enabled:
            return None
        with self._lock:
            self._channel = channel
            self._ready = False
            self._slots.clear()
            self._contacts.clear()
            self.version = RDPINPUT_PROTOCOL_V300
        return _RdpeiChannel(self, channel)

    def isActive(self):
        with self._lock:
            return self._channel is not None and self._ready and not self.suspended

    def penSupported(self):
        return self.isActive() and self.version >= RDPINPUT_PROTOCOL_V200

    # ---- server PDUs ----

    def _scReady(self, handler, version, features):
        with self._lock:
            if handler.channel is not self._channel:
                return
            if self.version > version:
                self.version = version
            self.features = features
            flags = CS_READY_FLAGS_SHOW_TOUCH_VISUALS & self.clientFeaturesMask
            if self.version > RDPINPUT_PROTOCOL_V10:
                flags |= CS_READY_FLAGS_DISABLE_TIMESTAMP_INJECTION & self.clientFeaturesMask
            if features & SC_READY_MULTIPEN_INJECTION_SUPPORTED:
                flags |= CS_READY_FLAGS_ENABLE_MULTIPEN_INJECTION & self.clientFeaturesMask
            self._ready = True
        handler.channel.write(eventPdu(EVENTID_CS_READY, pack('<IIH', flags, self.version,
                                                                      self.maxTouchContacts)))
        log.info(u'touch input: multi-touch%s through MS-RDPEI %d.%d' % (
            ' and pen' if self.version >= RDPINPUT_PROTOCOL_V200 else '', self.version >> 16, self.version & 0xFFFF))

    def _closed(self, handler):
        with self._lock:
            if handler.channel is self._channel:
                self._channel = None
                self._ready = False

    # ---- touch ----

    def _send(self, eventId, body):
        with self._lock:
            channel = self._channel if self._ready and not self.suspended else None
        if channel is None:
            return False
        channel.write(eventPdu(eventId, body))
        return True

    def _frameOffset(self):
        now = monotonic()
        offset = 0 if not self._previousFrame else int((now - self._previousFrame) * 1000)
        self._previousFrame = now
        return max(0, offset)

    def touch(self, points):
        """
        @summary: One touch event (FreeRDP touch_begin / update / end / cancel)
        @param points: [(externalId, state, x, y)] state 'begin', 'update', 'end' or 'cancel';
        all the touch points of the event (Qt gives them all)
        @return: {bool} sent
        """
        if not self.isActive():
            return False
        frame, ending = [], []
        with self._lock:
            for externalId, state, x, y in points:
                contactId = self._slots.get(externalId)
                if contactId is None:
                    if state != 'begin':
                        continue
                    free = [i for i in range(self.maxTouchContacts) if i not in self._slots.values()]
                    if not free:
                        continue
                    contactId = self._slots[externalId] = free[0]
                    flags = FLAGS_BEGIN
                elif state == 'cancel':
                    flags = FLAGS_CANCEL
                    del self._slots[externalId]
                else:
                    flags = FLAGS_UPDATE
                    if state == 'end':
                        ending.append((contactId, int(x), int(y)))
                        del self._slots[externalId]
                frame.append((contactId, int(x), int(y), flags, None, None))
        if not frame:
            return False
        sent = self._sendTouchFrame(frame)
        if ending:  # FreeRDP touch_end: the update, then UP
            self._sendTouchFrame([(cid, x, y, FLAGS_UP, None, None) for cid, x, y in ending])
        return sent

    def _sendTouchFrame(self, contacts):
        """FreeRDP rdpei_send_touch_event_pdu: encodeTime = the frame offset (ms), one frame."""
        offset = self._frameOffset()
        return self._send(EVENTID_TOUCH, fourByteUnsigned(offset) + twoByteUnsigned(1) + touchFrame(contacts, offset))

    # ---- pen ----

    def pen(self, state, x, y, pressure=None, rotation=None, tiltX=None, tiltY=None, eraser=False, barrel=False,
            inverted=False, deviceId=0):
        """
        @summary: One pen event (FreeRDP pen_begin / update / end / hover_*)
        @param state: 'begin', 'update', 'end', 'hover', 'hover_cancel'
        @param pressure: {float} 0.0 - 1.0
        @return: {bool} sent
        """
        if not self.penSupported():
            return False
        flags = {'begin': FLAGS_BEGIN, 'update': FLAGS_UPDATE, 'end': FLAGS_PEN_UP, 'hover': FLAGS_HOVER,
                 'hover_cancel': FLAGS_HOVER_CANCEL}[state]
        penFlags = (RDPINPUT_PEN_FLAG_BARREL_PRESSED if barrel else 0) | \
                   (RDPINPUT_PEN_FLAG_ERASER_PRESSED if eraser else 0) | (RDPINPUT_PEN_FLAG_INVERTED if inverted else 0)
        now = monotonic()
        offset = 0 if not self._previousPen else int((now - self._previousPen) * 1000)
        self._previousPen = now
        contact = (deviceId, int(x), int(y), flags, penFlags,
                   None if pressure is None else int(round(float(pressure) * 1024)), rotation, tiltX, tiltY)
        return self._send(EVENTID_PEN, fourByteUnsigned(offset) + twoByteUnsigned(1) + penFrame([contact], 0))

    def describe(self):
        if self.isActive():
            return 'MS-RDPEI %d.%d%s' % (self.version >> 16, self.version & 0xFFFF,
                                        ', pen' if self.version >= RDPINPUT_PROTOCOL_V200 else '')
        return None


class _RdpeiChannel(object):
    def __init__(self, plugin, channel):
        self.plugin = plugin
        self.channel = channel

    def recv(self, data):
        data = bytes(data)
        if len(data) < RDPINPUT_HEADER_LENGTH:
            return
        eventId, pduLength = unpack_from('<HI', data, 0)
        if eventId == EVENTID_SC_READY and len(data) >= 10:
            version = unpack_from('<I', data, 6)[0]
            features = unpack_from('<I', data, 10)[0] if len(data) >= 14 else 0
            self.plugin._scReady(self, version, features)
        elif eventId == EVENTID_SUSPEND_TOUCH:
            self.plugin.suspended = True
            log.info(u'touch input: suspended by the server')
        elif eventId == EVENTID_RESUME_TOUCH:
            self.plugin.suspended = False
            log.info(u'touch input: resumed')

    def closed(self):
        self.plugin._closed(self)
