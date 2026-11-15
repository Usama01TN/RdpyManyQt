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
Display Control Virtual Channel Extension (MS-RDPEDISP): the remote desktop
follows the size of the client window without reconnecting (FreeRDP
/dynamic-resolution).
The server opens the "Microsoft::Windows::RDS::DisplayControl" dynamic
channel and sends its capabilities (most monitors, largest area). When the
window is resized the client sends a monitor layout; the server then changes
the resolution of the session and says so with an RDPGFX ResetGraphics (graphics
pipeline) or a deactivation-reactivation sequence (legacy bitmap updates).
Ported from FreeRDP channels/disp/client/disp_main.c and client/X11/xf_disp.c
(at most one layout every 500 ms, the last size wins).
Protocol reference: [MS-RDPEDISP]
"""
from __future__ import absolute_import, division, print_function
from struct import pack, unpack_from
from time import time
from rdpy.core import log

CHANNEL_NAME = u'Microsoft::Windows::RDS::DisplayControl'
# PDU types (MS-RDPEDISP 2.2.1.1)
DISPLAYCONTROL_PDU_TYPE_MONITOR_LAYOUT = 0x00000002
DISPLAYCONTROL_PDU_TYPE_CAPS = 0x00000005
DISPLAYCONTROL_MONITOR_LAYOUT_SIZE = 40
DISPLAYCONTROL_MONITOR_PRIMARY = 0x00000001
# Limits (MS-RDPEDISP 2.2.2.2.1)
MIN_MONITOR_SIZE = 200
MAX_MONITOR_SIZE = 8192
MIN_PHYSICAL_SIZE = 10  # millimetres
MAX_PHYSICAL_SIZE = 10000
DESKTOP_SCALE_FACTORS = (100, 500)  # percent, inclusive range
DEVICE_SCALE_FACTORS = (100, 140, 180)
# Orientation (degrees)
ORIENTATION_LANDSCAPE = 0
ORIENTATION_PORTRAIT = 90
ORIENTATION_LANDSCAPE_FLIPPED = 180
ORIENTATION_PORTRAIT_FLIPPED = 270
# FreeRDP RESIZE_MIN_DELAY_NS: one layout every 500 ms at most
RESIZE_MIN_DELAY = 0.5


def normalizeSize(width, height):
    """
    @summary: A size the server accepts (FreeRDP disp_send_display_control_monitor_layout_pdu):
    even width, both in 200 - 8192
    @return: {tuple} (width, height)
    """
    width = int(width) - int(width) % 2
    width = min(max(width, MIN_MONITOR_SIZE), MAX_MONITOR_SIZE)
    height = min(max(int(height), MIN_MONITOR_SIZE), MAX_MONITOR_SIZE)
    return width, height


def physicalSize(width, height, dpi=None):
    """
    @summary: Physical size in millimetres of a monitor of width x height pixels
    @param dpi: {float|None} pixels per inch of the local screen (FreeRDP assumes 75 when unknown)
    @return: {tuple} (widthMm, heightMm), (0, 0) when out of the range the server accepts
    """
    dpi = float(dpi or 75.0)
    w, h = int(round(width / dpi * 25.4)), int(round(height / dpi * 25.4))
    if not (MIN_PHYSICAL_SIZE <= w <= MAX_PHYSICAL_SIZE and MIN_PHYSICAL_SIZE <= h <= MAX_PHYSICAL_SIZE):
        return 0, 0
    return w, h


def validScaleFactors(desktopScaleFactor, deviceScaleFactor):
    """@return: {bool} the server uses the scale factors (MS-RDPEDISP 2.2.2.2.1)"""
    return (DESKTOP_SCALE_FACTORS[0] <= desktopScaleFactor <= DESKTOP_SCALE_FACTORS[1] and
            deviceScaleFactor in DEVICE_SCALE_FACTORS)


class MonitorLayout(object):
    """
    @summary: DISPLAYCONTROL_MONITOR_LAYOUT (MS-RDPEDISP 2.2.2.2.1)
    """

    def __init__(self, width, height, left=0, top=0, primary=True, physicalWidth=0, physicalHeight=0,
                 orientation=ORIENTATION_LANDSCAPE, desktopScaleFactor=100, deviceScaleFactor=100):
        self.width, self.height = normalizeSize(width, height)
        self.left, self.top = int(left), int(top)
        self.flags = DISPLAYCONTROL_MONITOR_PRIMARY if primary else 0
        self.physicalWidth, self.physicalHeight = int(physicalWidth), int(physicalHeight)
        self.orientation = int(orientation)
        self.desktopScaleFactor = int(desktopScaleFactor)
        self.deviceScaleFactor = int(deviceScaleFactor)

    def pack(self):
        return pack('<IiiIIIIIII', self.flags, self.left, self.top, self.width, self.height,
                    self.physicalWidth, self.physicalHeight, self.orientation,
                    self.desktopScaleFactor, self.deviceScaleFactor)

    def key(self):
        return (self.flags, self.left, self.top, self.width, self.height, self.physicalWidth,
                self.physicalHeight, self.orientation, self.desktopScaleFactor, self.deviceScaleFactor)

    def __repr__(self):
        return u'<MonitorLayout %dx%d at %d,%d%s scale %d%%/%d%%>' % (
            self.width, self.height, self.left, self.top, u' primary' if self.flags else u'',
            self.desktopScaleFactor, self.deviceScaleFactor)


def buildMonitorLayoutPdu(monitors, maxMonitors=None):
    """
    @summary: DISPLAYCONTROL_MONITOR_LAYOUT_PDU (MS-RDPEDISP 2.2.2.2)
    @param monitors: [MonitorLayout]
    @param maxMonitors: {int|None} MaxNumMonitors of the server caps
    @return: {bytes}
    """
    monitors = list(monitors)
    if maxMonitors:
        monitors = monitors[:maxMonitors]
    body = pack('<II', DISPLAYCONTROL_MONITOR_LAYOUT_SIZE, len(monitors))
    body += b''.join(m.pack() for m in monitors)
    return pack('<II', DISPLAYCONTROL_PDU_TYPE_MONITOR_LAYOUT, 8 + len(body)) + body


class _DisplayControlChannel(object):
    """Handler of one DisplayControl dynamic channel (FreeRDP DISP_PLUGIN callbacks)."""

    def __init__(self, owner, channel):
        self._owner = owner
        self.channel = channel

    def recv(self, data):
        if len(data) < 8:
            raise ValueError(u'display control PDU too short')
        pduType, length = unpack_from('<II', data, 0)
        if length < 8 or length > len(data):
            raise ValueError(u'display control PDU length %d invalid' % length)
        if pduType == DISPLAYCONTROL_PDU_TYPE_CAPS:
            if length < 20:
                raise ValueError(u'display control caps too short')
            maxMonitors, factorA, factorB = unpack_from('<III', data, 8)
            self._owner._onCaps(self, maxMonitors, factorA, factorB)
        else:
            log.warning(u'display control: unexpected PDU type %d' % pduType)

    def closed(self):
        self._owner._onClosed(self)


class DisplayControl(object):
    """
    @summary: Client side of the display control channel: the resolution follows
    requestSize() (FreeRDP xfDispContext). Network thread only.
    """

    def __init__(self):
        self.enabled = True
        self.desktopScaleFactor = 100
        self.deviceScaleFactor = 100
        self.orientation = ORIENTATION_LANDSCAPE
        self.maxMonitors = 0
        self.maxAreaFactorA = 0
        self.maxAreaFactorB = 0
        self._handler = None
        self._capsReceived = False
        self._target = None  # MonitorLayout waiting to be sent
        self._lastSent = None  # key() of the last layout sent
        self._lastSentTime = 0.0
        self._timer = None
        self._capsCallback = None
        self._clock = time

    # ---- plugin ----

    def channelFactory(self, channel):
        """@summary: DrdynvcLayer listener: accept the channel when enabled"""
        if not self.enabled:
            return None
        handler = _DisplayControlChannel(self, channel)
        if self._handler is not None:
            log.debug(u'display control: channel re-opened')
        self._handler = handler
        self._capsReceived = False
        return handler

    def setCapsCallback(self, callback):
        """@param callback: callable(maxMonitors, factorA, factorB) once the channel is usable"""
        self._capsCallback = callback

    def _onCaps(self, handler, maxMonitors, factorA, factorB):
        if handler is not self._handler:
            return
        self.maxMonitors = maxMonitors
        self.maxAreaFactorA = factorA
        self.maxAreaFactorB = factorB
        self._capsReceived = True
        log.info(u'display control: the remote resolution follows the window (up to %d monitor(s), '
                 u'%d x %d pixels)' % (maxMonitors, factorA, factorB))
        if self._capsCallback is not None:
            self._capsCallback(maxMonitors, factorA, factorB)
        if self._target is not None:
            self._schedule()

    def _onClosed(self, handler):
        if handler is self._handler:
            self._handler = None
            self._capsReceived = False
            self._cancelTimer()

    def isActive(self):
        """@return: {bool} the server accepts monitor layouts now"""
        return self.enabled and self._handler is not None and self._capsReceived

    def shutdown(self):
        self._cancelTimer()
        self._handler = None
        self._capsReceived = False

    # ---- layouts ----

    def _fitArea(self, width, height):
        """Stay within the largest area the server accepts (MaxMonitorAreaFactorA x B)."""
        area = self.maxAreaFactorA * self.maxAreaFactorB
        if area and width * height > area:
            scale = (float(area) / (width * height)) ** 0.5
            width, height = int(width * scale), int(height * scale)
        return normalizeSize(width, height)

    def requestSize(self, width, height, physicalWidth=0, physicalHeight=0):
        """
        @summary: Ask the server for a width x height desktop (sent when the channel is
        ready, at most every 500 ms; the last request wins)
        @param physicalWidth: {int} millimetres, 0 when unknown
        """
        width, height = self._fitArea(*normalizeSize(width, height))
        scale = validScaleFactors(self.desktopScaleFactor, self.deviceScaleFactor)
        target = MonitorLayout(
            width, height, physicalWidth=physicalWidth if scale else 0,
            physicalHeight=physicalHeight if scale else 0, orientation=self.orientation,
            desktopScaleFactor=self.desktopScaleFactor if scale else 100,
            deviceScaleFactor=self.deviceScaleFactor if scale else 100)
        self._target = target
        if self.isActive():
            self._schedule()
        return target.width, target.height

    def _schedule(self):
        if self._timer is not None:
            return  # sent when the timer fires (FreeRDP: wait for the running timer)
        wait = self._lastSentTime + RESIZE_MIN_DELAY - self._clock()
        if wait > 0:
            from twisted.internet import reactor
            self._timer = reactor.callLater(wait, self._onTimer)
        else:
            self._sendTarget()

    def _onTimer(self):
        self._timer = None
        if self.isActive():
            self._sendTarget()

    def _cancelTimer(self):
        if self._timer is not None:
            try:
                if self._timer.active():
                    self._timer.cancel()
            except Exception:
                pass
            self._timer = None

    def _sendTarget(self):
        target, self._target = self._target, None
        if target is None or self._handler is None:
            return
        if target.key() == self._lastSent:
            return  # unchanged (FreeRDP xf_disp_settings_changed)
        self._lastSent = target.key()
        self._lastSentTime = self._clock()
        log.info(u'display control: asking for %dx%d' % (target.width, target.height))
        self._handler.channel.write(buildMonitorLayoutPdu([target], self.maxMonitors or None))

    def noteDesktopSize(self, width, height):
        """
        @summary: The server's current resolution: a later request for this size
        is not sent again
        """
        if self._lastSent is not None and (self._lastSent[3], self._lastSent[4]) != (width, height):
            # the server chose another size (limits): allow asking again
            self._lastSent = None
