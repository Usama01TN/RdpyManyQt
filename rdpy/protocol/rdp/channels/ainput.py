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
Advanced Input Virtual Channel Extension ("FreeRDP::Advanced::Input"): mouse
events with 32-bit coordinates, relative motion and high resolution wheels.
A FreeRDP extension, opened by FreeRDP based servers (shadow, proxy...). While
the channel is open with protocol version 1.x, every mouse and wheel event is
sent on it instead of the slow-path / fast-path input PDUs, exactly like
FreeRDP's client (client/common/client.c freerdp_client_send_button_event,
freerdp_client_send_wheel_event, freerdp_client_send_extended_button_event);
when it is not open the usual input PDUs are used.
Ported from FreeRDP channels/ainput (client/ainput_main.c,
include/freerdp/channels/ainput.h).
"""
from __future__ import absolute_import, division, print_function
from struct import pack, unpack_from
from threading import Lock
from rdpy.core.compat import monotonic
from rdpy.core import log

CHANNEL_NAME = u'FreeRDP::Advanced::Input'
MSG_AINPUT_VERSION = 0x01
MSG_AINPUT_MOUSE = 0x02
AINPUT_FLAGS_WHEEL = 0x0001
AINPUT_FLAGS_MOVE = 0x0004
AINPUT_FLAGS_DOWN = 0x0008
AINPUT_FLAGS_REL = 0x0010
AINPUT_FLAGS_HAVE_REL = 0x0020
AINPUT_FLAGS_BUTTON1 = 0x1000  # left
AINPUT_FLAGS_BUTTON2 = 0x2000  # right
AINPUT_FLAGS_BUTTON3 = 0x4000  # middle
AINPUT_XFLAGS_BUTTON1 = 0x0100
AINPUT_XFLAGS_BUTTON2 = 0x0200
AINPUT_VERSION_MAJOR = 1
AINPUT_VERSION_MINOR = 0
# rdpy button numbers -> ainput flags (1 left, 2 right, 3 middle, 4 / 5 extended)
_BUTTONS = {1: AINPUT_FLAGS_BUTTON1, 2: AINPUT_FLAGS_BUTTON2, 3: AINPUT_FLAGS_BUTTON3,
            4: AINPUT_XFLAGS_BUTTON1, 5: AINPUT_XFLAGS_BUTTON2}


def packMouse(flags, x, y, time=None):
    """
    @summary: MSG_AINPUT_MOUSE: UINT16 type, UINT64 time (ms), UINT64 flags, INT32 x, INT32 y
    """
    if time is None:
        time = int(monotonic() * 1000)
    return pack('<HQQii', MSG_AINPUT_MOUSE, int(time) & 0xFFFFFFFFFFFFFFFF, int(flags), int(x), int(y))


def buttonFlags(button, isPressed):
    """
    @summary: ainput flags of an rdpy pointer event (button 0 = move)
    """
    flags = 0
    if isPressed:
        flags |= AINPUT_FLAGS_DOWN
    if button in _BUTTONS:
        flags |= _BUTTONS[button]
    else:
        flags |= AINPUT_FLAGS_MOVE
    return flags


def wheelEvent(scroll, isHorizontal):
    """
    @summary: (flags, x, y) of a wheel step, scaled by 0x10000 like FreeRDP so high
    resolution wheels can be expressed
    """
    value = int(scroll) * 0x10000
    if isHorizontal:
        return AINPUT_FLAGS_WHEEL, value, 0
    return AINPUT_FLAGS_WHEEL, 0, value


class AInput(object):
    """
    @summary: The ainput plugin: DrdynvcLayer listener and input sender (any thread)
    """

    def __init__(self):
        self.enabled = True  # FreeRDP loads it by default; only FreeRDP servers open it
        self.majorVersion = 0
        self.minorVersion = 0
        self._channel = None
        self._lock = Lock()

    def channelFactory(self, channel):
        """@summary: DrdynvcLayer listener"""
        if not self.enabled:
            return None
        return _AInputChannel(self, channel)

    def isActive(self):
        """@return: {bool} input goes through the channel (open, protocol 1.x)"""
        with self._lock:
            return self._channel is not None and self.majorVersion == AINPUT_VERSION_MAJOR

    def sendMouse(self, flags, x, y):
        """
        @summary: Send one mouse event (FreeRDP ainput_send_input_event)
        @return: {bool} sent; False -> use the default input PDUs
        """
        with self._lock:
            channel = self._channel
            if channel is None or self.majorVersion != AINPUT_VERSION_MAJOR:
                return False
        channel.write(packMouse(flags, x, y))
        return True

    def sendPointer(self, x, y, button, isPressed, relative=False):
        """@summary: pointer move / button event; @return: {bool} sent"""
        flags = buttonFlags(button, isPressed)
        if relative:
            flags |= AINPUT_FLAGS_REL
        return self.sendMouse(flags, x, y)

    def sendWheel(self, scroll, isHorizontal=False):
        """@summary: wheel step (signed); @return: {bool} sent"""
        flags, x, y = wheelEvent(scroll, isHorizontal)
        return self.sendMouse(flags, x, y)

    # channel callbacks
    def _opened(self, handler):
        with self._lock:
            self._channel = handler.channel
            self.majorVersion = self.minorVersion = 0

    def _version(self, handler, major, minor):
        with self._lock:
            if handler.channel is self._channel:
                self.majorVersion, self.minorVersion = major, minor
        if major == AINPUT_VERSION_MAJOR:
            log.info(u'advanced input: mouse events use the FreeRDP::Advanced::Input channel (%d.%d)'
                     % (major, minor))
        else:
            log.warning(u'advanced input: unsupported version %d.%d, using the input PDUs' % (major, minor))

    def _closed(self, handler):
        with self._lock:
            if handler.channel is self._channel:
                self._channel = None
                self.majorVersion = self.minorVersion = 0

    def describe(self):
        if self.isActive():
            return u'active (%d.%d)' % (self.majorVersion, self.minorVersion)
        return None


class _AInputChannel(object):

    def __init__(self, plugin, channel):
        self.plugin = plugin
        self.channel = channel
        plugin._opened(self)

    def recv(self, data):
        data = bytes(data)
        if len(data) < 2:
            return
        msgType = unpack_from('<H', data, 0)[0]
        if msgType == MSG_AINPUT_VERSION and len(data) >= 10:
            major, minor = unpack_from('<II', data, 2)
            self.plugin._version(self, major, minor)

    def closed(self):
        self.plugin._closed(self)
