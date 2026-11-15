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
Video Optimized Remoting Virtual Channel Extension (MS-RDPEVOR): a video
playing in the session (browser, media player) is sent as an H.264 stream on
"Microsoft::Windows::RDS::Video::Data::v08.01" instead of being encoded as
desktop updates; "...::Video::Control::v08.01" starts and stops
presentations. The client decodes it and shows each frame at its time, scaled
to the presentation size, where the geometry channel (MS-RDPEGT) says the video window is.
Ported from FreeRDP channels/video/client/video_main.c and
libfreerdp/gdi/video.c: presentation request / response, samples reassembled
from their packets, publish time = first sample time + the durations, the
latest due frame shown and older ones dropped, and once a second a frame rate
override (24 fps after drops, then -2 / +2, unlimited when nothing is dropped).
"""
from __future__ import absolute_import, division, print_function
from struct import error, pack, unpack_from
from threading import Lock, Thread
from rdpy.core.compat import monotonic, queue
from rdpy.core import log

CONTROL_CHANNEL_NAME = u'Microsoft::Windows::RDS::Video::Control::v08.01'
DATA_CHANNEL_NAME = u'Microsoft::Windows::RDS::Video::Data::v08.01'
TSMM_PACKET_TYPE_PRESENTATION_REQUEST = 1
TSMM_PACKET_TYPE_PRESENTATION_RESPONSE = 2
TSMM_PACKET_TYPE_CLIENT_NOTIFICATION = 3
TSMM_PACKET_TYPE_VIDEO_DATA = 4
TSMM_START_PRESENTATION = 1
TSMM_STOP_PRESENTATION = 2
TSMM_VIDEO_DATA_FLAG_HAS_TIMESTAMPS = 0x01
TSMM_VIDEO_DATA_FLAG_KEYFRAME = 0x02
TSMM_VIDEO_DATA_FLAG_NEW_FRAMERATE = 0x04
TSMM_CLIENT_NOTIFICATION_TYPE_NETWORK_ERROR = 1
TSMM_CLIENT_NOTIFICATION_TYPE_FRAMERATE_OVERRIDE = 2
MFVideoFormat_H264 = b'H264\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71'
VIDEO_UNLIMITED_RATE = 31
VIDEO_MAX_SAMPLE_SIZE = 32 * 1024 * 1024
TIMER_INTERVAL = 0.010


def presentationResponse(presentationId):
    """TSMM_PRESENTATION_RESPONSE: cbSize 12, type, PresentationId, 3 reserved bytes."""
    return pack('<IIB3x', 12, TSMM_PACKET_TYPE_PRESENTATION_RESPONSE, presentationId)


def frameRateOverride(presentationId, rate):
    """TSMM_CLIENT_NOTIFICATION with TSMM_CLIENT_NOTIFICATION_FRAMERATE_OVERRIDE (FreeRDP layout)."""
    flags, desired = (0x01, 0) if rate == VIDEO_UNLIMITED_RATE else (0x02, rate)
    return pack('<IIBB2xIII8x', 32, TSMM_PACKET_TYPE_CLIENT_NOTIFICATION, presentationId,
                TSMM_CLIENT_NOTIFICATION_TYPE_FRAMERATE_OVERRIDE, 16, flags, desired)


class _Presentation(object):
    def __init__(self, presentationId, x, y, sourceWidth, sourceHeight, scaledWidth, scaledHeight, geometry):
        self.presentationId = presentationId
        self.x, self.y = x, y
        self.sourceWidth, self.sourceHeight = sourceWidth, sourceHeight
        self.scaledWidth, self.scaledHeight = scaledWidth, scaledHeight
        self.geometry = geometry
        self.sample = bytearray()
        self.lastPublishTime = 0.0
        self.decoder = None
        from av import CodecContext
        self.decoder = CodecContext.create('h264', 'r')


class Video(object):
    """
    @summary: The video plugin (control + data listeners). Decoded frames are given to
    the frame callback (x, y, width, height, bgra (h, w, 4) uint8 at the presentation's
    scaled size), from the reactor thread, at their publish time.
    """

    def __init__(self, geometry):
        self.enabled = False  # FreeRDP /video (needs /geometry, enabled with it)
        self.geometry = geometry
        self.geometry.addListener(self._onGeometry)
        self._frameCallback = None
        self._control = None
        self._presentation = None
        self._frames = []  # [(publishTime, presentationId, x, y, w, h, bgra)]
        self._lock = Lock()
        self._work = None
        self._worker = None
        self._timer = None
        self.published = 0
        self.dropped = 0
        self.lastSentRate = VIDEO_UNLIMITED_RATE
        self._nextFeedback = 0.0
        self.totalPublished = 0

    def setFrameCallback(self, callback):
        self._frameCallback = callback

    # ---- listeners ----

    def controlFactory(self, channel):
        if not self.enabled:
            return None
        self._control = channel
        return _ControlChannel(self, channel)

    def dataFactory(self, channel):
        if not self.enabled:
            return None
        return _DataChannel(self, channel)

    # ---- control (FreeRDP video_PresentationRequest) ----

    def presentationRequest(self, data):
        if len(data) < 8 + 60:
            raise ValueError(u'short presentation request')
        pid, version, command, frameRate = unpack_from('<BBBB', data, 8)
        srcW, srcH, scW, scH = unpack_from('<IIII', data, 16)
        tsOffset, mappingId = unpack_from('<QQ', data, 32)
        subtype = bytes(data[48:64])
        if command == TSMM_START_PRESENTATION:
            if 0 in (srcW, srcH, scW, scH):
                raise ValueError(u'presentation with an empty size')
            if subtype != MFVideoFormat_H264:
                log.warning(u'video: only H.264 presentations are supported')
                return
            with self._lock:
                current = self._presentation
            if current is not None:
                if current.presentationId == pid:
                    return  # already started
                self._stopPresentation()
            geometry = self.geometry.get(mappingId)
            if geometry is None:
                log.warning(u'video: geometry 0x%x unknown, presentation refused' % mappingId)
                return
            if min(geometry.topLevelLeft, geometry.left, geometry.topLevelTop, geometry.top) < 0:
                log.warning(u'video: negative geometry, presentation refused')
                return
            x, y = geometry.origin
            try:
                presentation = _Presentation(pid, x, y, srcW, srcH, scW, scH, geometry)
            except Exception as e:
                log.warning(u'video: no H.264 decoder: %r' % (e,))
                return
            with self._lock:
                self._presentation = presentation
                self.published = self.dropped = 0
                self.lastSentRate = VIDEO_UNLIMITED_RATE
            self._startWorker()
            log.info(u'video: presentation %d, %dx%d shown %dx%d at %d,%d' % (pid, srcW, srcH, scW, scH, x, y))
            self._writeControl(presentationResponse(pid))
        elif command == TSMM_STOP_PRESENTATION:
            with self._lock:
                current = self._presentation
            if current is not None:
                log.info(u'video: presentation %d stopped' % current.presentationId)
                self._stopPresentation()

    def _stopPresentation(self):
        with self._lock:
            self._presentation = None
            self._frames = []
            self.published = self.dropped = 0

    def _writeControl(self, data):
        if self._control is not None:
            self._control.write(data)

    def _onGeometry(self, event, geometry, isNew=False):
        """FreeRDP video_onMappedGeometryUpdate / Clear."""
        with self._lock:
            presentation = self._presentation
        if presentation is None or presentation.geometry is not geometry:
            return
        if event == 'update' and min(geometry.topLevelLeft, geometry.left, geometry.topLevelTop, geometry.top) >= 0:
            presentation.x, presentation.y = geometry.origin
        elif event == 'clear':
            presentation.geometry = None

    # ---- data (FreeRDP video_VideoData) ----

    def videoData(self, data):
        if len(data) < 8 + 32:
            raise ValueError(u'short video data')
        cbSize, packetType = unpack_from('<II', data, 0)
        if packetType != TSMM_PACKET_TYPE_VIDEO_DATA:
            raise ValueError(u'not a video data packet')
        pid, version, flags = unpack_from('<BBB', data, 8)
        hnsTimestamp, hnsDuration = unpack_from('<QQ', data, 12)
        index, count, sampleNumber, cbSample = unpack_from('<HHII', data, 28)
        if len(data) < 40 + cbSample:
            raise ValueError(u'truncated sample')
        with self._lock:
            presentation = self._presentation
        if presentation is None or presentation.presentationId != pid:
            return
        if len(presentation.sample) + cbSample > VIDEO_MAX_SAMPLE_SIZE:
            presentation.sample = bytearray()
            raise ValueError(u'sample too large')
        presentation.sample += data[40:40 + cbSample]
        if index != count:
            return
        sample, presentation.sample = bytes(presentation.sample), bytearray()
        if presentation.geometry is None:
            return
        now = monotonic()
        if sampleNumber == 1:
            presentation.lastPublishTime = now
        presentation.lastPublishTime += hnsDuration * 1e-7
        self._work.put((presentation, sample, presentation.lastPublishTime))

    # ---- decoding (worker) and presentation (reactor timer) ----

    def _startWorker(self):
        if self._worker is None or not self._worker.is_alive():
            self._work = queue.Queue()
            self._worker = Thread(target=self._decodeLoop, args=(self._work,), name=u'video-decode')
            self._worker.daemon = True
            self._worker.start()
        if self._timer is None:
            from rdpy.protocol.rdp.channels.drdynvc import callInReactor
            callInReactor(self._scheduleTimer)

    def _decodeLoop(self, work):
        from av import Packet
        from numpy import ascontiguousarray
        while True:
            item = work.get()
            if item is None:
                break
            presentation, sample, publishTime = item
            try:
                frames = presentation.decoder.decode(Packet(sample))
            except Exception as e:
                log.debug(u'video: undecodable sample: %r' % (e,))
                continue
            for frame in frames:
                image = frame.reformat(width=presentation.scaledWidth, height=presentation.scaledHeight,
                                       format='bgra').to_ndarray()
                with self._lock:
                    if self._presentation is presentation:
                        self._frames.append((publishTime, presentation.presentationId, ascontiguousarray(image)))

    def _scheduleTimer(self):
        from twisted.internet import reactor
        self._timer = reactor.callLater(TIMER_INTERVAL, self._tick)

    def _tick(self):
        self._timer = None
        self.timer(monotonic())
        with self._lock:
            active = self._presentation is not None
        if active and self.enabled:
            self._scheduleTimer()

    def timer(self, now):
        """FreeRDP video_timer: show the latest due frame, drop older ones, send feedback."""
        frame = None
        with self._lock:
            presentation = self._presentation
            while self._frames and self._frames[0][0] <= now:
                if frame is not None:
                    self.dropped += 1
                frame = self._frames.pop(0)
        if frame is not None and presentation is not None and presentation.presentationId == frame[1]:
            self.published += 1
            self.totalPublished += 1
            if self._frameCallback is not None:
                image = frame[2]
                self._frameCallback(presentation.x, presentation.y, image.shape[1], image.shape[0], image)
        if self._nextFeedback < now:
            if self.published and presentation is not None:
                if self.dropped:
                    rate = 24 if self.lastSentRate == VIDEO_UNLIMITED_RATE else max(2, self.lastSentRate - 2)
                else:
                    rate = VIDEO_UNLIMITED_RATE if self.lastSentRate == VIDEO_UNLIMITED_RATE else \
                        min(VIDEO_UNLIMITED_RATE, self.lastSentRate + 2)
                if rate != self.lastSentRate:
                    self._writeControl(frameRateOverride(presentation.presentationId, rate))
                    self.lastSentRate = rate
            self.dropped = self.published = 0
            self._nextFeedback = now + 1.0

    def shutdown(self):
        self._stopPresentation()
        if self._work is not None:
            self._work.put(None)
            self._work = None
        if self._timer is not None and self._timer.active():
            self._timer.cancel()
        self._timer = None

    def describe(self):
        with self._lock:
            p = self._presentation
        if p is not None:
            return 'playing %dx%d at %d,%d' % (p.scaledWidth, p.scaledHeight, p.x, p.y)
        return ('%d frame(s) shown' % self.totalPublished) if self.totalPublished else None


class _ControlChannel(object):
    def __init__(self, plugin, channel):
        self.plugin = plugin
        self.channel = channel

    def recv(self, data):
        data = bytes(data)
        try:
            if len(data) < 8:
                raise ValueError(u'short control packet')
            cbSize, packetType = unpack_from('<II', data, 0)
            if cbSize < 8 or len(data) < cbSize:
                raise ValueError(u'invalid cbSize')
            if packetType == TSMM_PACKET_TYPE_PRESENTATION_REQUEST:
                self.plugin.presentationRequest(data)
        except (ValueError, error) as e:
            log.warning(u'video: bad control packet: %s' % e)

    def closed(self):
        if self.plugin._control is self.channel:
            self.plugin._control = None
            self.plugin._stopPresentation()


class _DataChannel(object):
    def __init__(self, plugin, channel):
        self.plugin = plugin
        self.channel = channel

    def recv(self, data):
        try:
            self.plugin.videoData(bytes(data))
        except (ValueError, error) as e:
            log.warning(u'video: bad data packet: %s' % e)

    def closed(self):
        """
        :return:
        """
