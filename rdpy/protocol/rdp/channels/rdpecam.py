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
Video Capture Virtual Channel Extension (MS-RDPECAM): the cameras of the client
computer appear as cameras in the remote session (Teams, Zoom, the Camera app).
The server opens "RDCamera_Device_Enumerator"; the client selects the protocol
version and announces each camera with the name of a dynamic channel the
server then opens for it. On that channel the server activates the device,
lists its streams and media types, starts a stream and requests samples one at
a time; every Sample Request gets one encoded frame (or a Sample Error Response).
Ported from FreeRDP channels/rdpecam/client (camera_device_enum_main.c,
camera_device_main.c, encoding.c): one stream per device, H.264 output
(constrained low latency x264, FreeRDP's webcam bitrates), MJPEG when no H.264
encoder is available. Frames come from Qt Multimedia (QCamera, Qt 5 and 6) and
are encoded with PyAV (FFmpeg) on a worker thread, only when the server asked
for a sample, so the encoder never references a frame that was not sent.
"""
from __future__ import absolute_import, division, print_function
from struct import error, pack as struct_pack, unpack_from
import threading
from rdpy.core.compat import monotonic, queue
from rdpy.core import log

ENUMERATOR_CHANNEL = u'RDCamera_Device_Enumerator'
PROTOCOL_VERSION = 2
CAM_MSG_ID_SuccessResponse = 0x01
CAM_MSG_ID_ErrorResponse = 0x02
CAM_MSG_ID_SelectVersionRequest = 0x03
CAM_MSG_ID_SelectVersionResponse = 0x04
CAM_MSG_ID_DeviceAddedNotification = 0x05
CAM_MSG_ID_DeviceRemovedNotification = 0x06
CAM_MSG_ID_ActivateDeviceRequest = 0x07
CAM_MSG_ID_DeactivateDeviceRequest = 0x08
CAM_MSG_ID_StreamListRequest = 0x09
CAM_MSG_ID_StreamListResponse = 0x0A
CAM_MSG_ID_MediaTypeListRequest = 0x0B
CAM_MSG_ID_MediaTypeListResponse = 0x0C
CAM_MSG_ID_CurrentMediaTypeRequest = 0x0D
CAM_MSG_ID_CurrentMediaTypeResponse = 0x0E
CAM_MSG_ID_StartStreamsRequest = 0x0F
CAM_MSG_ID_StopStreamsRequest = 0x10
CAM_MSG_ID_SampleRequest = 0x11
CAM_MSG_ID_SampleResponse = 0x12
CAM_MSG_ID_SampleErrorResponse = 0x13
CAM_MSG_ID_PropertyListRequest = 0x14
CAM_MSG_ID_PropertyListResponse = 0x15
CAM_MSG_ID_PropertyValueRequest = 0x16
CAM_MSG_ID_PropertyValueResponse = 0x17
CAM_MSG_ID_SetPropertyValueRequest = 0x18
CAM_ERROR_CODE_UnexpectedError = 0x01
CAM_ERROR_CODE_InvalidMessage = 0x02
CAM_ERROR_CODE_NotInitialized = 0x03
CAM_ERROR_CODE_InvalidRequest = 0x04
CAM_ERROR_CODE_InvalidStreamNumber = 0x05
CAM_ERROR_CODE_InvalidMediaType = 0x06
CAM_ERROR_CODE_OutOfMemory = 0x07
CAM_ERROR_CODE_ItemNotFound = 0x08
CAM_ERROR_CODE_SetNotFound = 0x09
CAM_ERROR_CODE_OperationNotSupported = 0x0A
CAM_MEDIA_FORMAT_H264 = 0x01
CAM_MEDIA_FORMAT_MJPG = 0x02
CAM_MEDIA_FORMAT_YUY2 = 0x03
CAM_MEDIA_FORMAT_NV12 = 0x04
CAM_MEDIA_FORMAT_I420 = 0x05
CAM_MEDIA_FORMAT_RGB24 = 0x06
CAM_MEDIA_FORMAT_RGB32 = 0x07
FORMAT_NAMES = {1: 'H264', 2: 'MJPG', 3: 'YUY2', 4: 'NV12', 5: 'I420', 6: 'RGB24', 7: 'RGB32'}
CAM_MEDIA_TYPE_DESCRIPTION_FLAG_DecodingRequired = 0x01
CAM_MEDIA_TYPE_DESCRIPTION_FLAG_BottomUpImage = 0x02
CAM_STREAM_FRAME_SOURCE_TYPE_Color = 0x0001
CAM_STREAM_CATEGORY_Capture = 0x01
ECAM_DEVICE_MAX_STREAMS = 1
ECAM_MAX_MEDIA_TYPE_DESCRIPTORS = 256
MEDIA_TYPE_SIZE = 26
# FreeRDP encoding.c h264_get_max_bitrate (webcam streaming, kbps)
_BITRATES = ((1080, 2700), (720, 1250), (480, 700), (360, 400), (240, 170), (180, 140), (0, 100))


def h264Bitrate(height):
    for minHeight, kbps in _BITRATES:
        if height >= minHeight:
            return kbps * 1000
    return 100000


class MediaType(object):
    """CAM_MEDIA_TYPE_DESCRIPTION (26 bytes)."""
    __slots__ = ('format', 'width', 'height', 'fpsNum', 'fpsDen', 'parNum', 'parDen', 'flags')

    def __init__(self, fmt, width, height, fpsNum, fpsDen=1, parNum=1, parDen=1, flags=None):
        self.format = int(fmt)
        self.width = int(width)
        self.height = int(height)
        self.fpsNum = int(fpsNum)
        self.fpsDen = int(fpsDen)
        self.parNum = int(parNum)
        self.parDen = int(parDen)
        self.flags = mediaTypeFlags(fmt) if flags is None else int(flags)

    def pack(self):
        return struct_pack('<BIIIIIIB', self.format, self.width, self.height, self.fpsNum, self.fpsDen,
                           self.parNum, self.parDen, self.flags)

    @classmethod
    def unpack(cls, data, offset=0):
        """FreeRDP ecam_dev_read_media_type (no zero rate / aspect, known format and flags)."""
        fmt, w, h, fn, fd, pn, pd, flags = unpack_from('<BIIIIIIB', data, offset)
        if fmt not in FORMAT_NAMES or 0 in (fn, fd, pn, pd) or flags & ~0x03:
            raise ValueError(u'invalid media type')
        return cls(fmt, w, h, fn, fd, pn, pd, flags)

    def key(self):
        return (self.format, self.width, self.height, self.fpsNum, self.fpsDen, self.parNum, self.parDen, self.flags)

    def __eq__(self, other):
        return isinstance(other, MediaType) and self.key() == other.key()

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return u'%s %dx%d@%g' % (FORMAT_NAMES.get(self.format, self.format), self.width, self.height,
                                 float(self.fpsNum) / max(1, self.fpsDen))


def mediaTypeFlags(fmt):
    """FreeRDP ecam_media_type_flags: compressed formats need decoding."""
    return CAM_MEDIA_TYPE_DESCRIPTION_FLAG_DecodingRequired if fmt in (CAM_MEDIA_FORMAT_H264,
                                                                       CAM_MEDIA_FORMAT_MJPG) else 0


def header(version, msgId):
    return struct_pack('<BB', version, msgId)


# ---------------------------------------------------------------------------
# Encoding (FreeRDP encoding.c with PyAV instead of the FreeRDP video context)
# ---------------------------------------------------------------------------

H264_ENCODERS = ('libx264', 'h264_mf', 'h264_videotoolbox', 'libopenh264', 'h264_nvenc', 'h264_qsv', 'h264_amf')


def availableOutputFormat():
    """
    @return: {int} CAM_MEDIA_FORMAT_H264 when an H.264 encoder exists, else MJPG (None: no PyAV)
    """
    try:
        from av import codecs_available
    except:
        return None
    for name in H264_ENCODERS:
        if name in codecs_available:
            return CAM_MEDIA_FORMAT_H264
    return CAM_MEDIA_FORMAT_MJPG if 'mjpeg' in codecs_available else None


class SampleEncoder(object):
    """
    @summary: Encodes BGRA frames (numpy (h, w, 4) uint8) to one sample each:
    an H.264 access unit (Annex B, SPS/PPS before every key frame) or a JPEG
    """

    def __init__(self, mediaType):
        from av import CodecContext, codecs_available
        self.mediaType = mediaType
        self.frames = 0
        fps = max(1, int(round(float(mediaType.fpsNum) / max(1, mediaType.fpsDen))))
        if mediaType.format == CAM_MEDIA_FORMAT_H264:
            last = None
            for name in H264_ENCODERS:
                if name not in codecs_available:
                    continue
                try:
                    ctx = CodecContext.create(name, 'w')
                    ctx.width, ctx.height = mediaType.width, mediaType.height
                    ctx.pix_fmt = 'yuv420p' if name != 'h264_mf' else 'nv12'
                    ctx.time_base = _fraction(mediaType.fpsDen, mediaType.fpsNum)
                    ctx.framerate = _fraction(mediaType.fpsNum, mediaType.fpsDen)
                    ctx.bit_rate = h264Bitrate(mediaType.height)
                    ctx.gop_size = fps * 2
                    ctx.max_b_frames = 0
                    if name == 'libx264':
                        ctx.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'profile': 'baseline',
                                       'repeat-headers': '1'}
                    ctx.open()
                    self.ctx = ctx
                    self.codec = name
                    break
                except Exception as e:  # encoder present but not usable here
                    last = e
            else:
                raise RuntimeError(u'no usable H.264 encoder (%s)' % last)
        elif mediaType.format == CAM_MEDIA_FORMAT_MJPG:
            ctx = CodecContext.create('mjpeg', 'w')
            ctx.width, ctx.height = mediaType.width, mediaType.height
            ctx.pix_fmt = 'yuvj420p'
            ctx.time_base = _fraction(mediaType.fpsDen, mediaType.fpsNum)
            ctx.options = {'qscale': '4'}
            ctx.open()
            self.ctx = ctx
            self.codec = 'mjpeg'
        else:
            raise ValueError(u'output format %s not supported' % FORMAT_NAMES.get(mediaType.format))

    def encode(self, bgra):
        """@return: {bytes|None} the sample of this frame"""
        from av import VideoFrame
        from numpy import ascontiguousarray
        frame = VideoFrame.from_ndarray(ascontiguousarray(bgra), format='bgra')
        if (frame.width, frame.height) != (self.mediaType.width, self.mediaType.height):
            frame = frame.reformat(width=self.mediaType.width, height=self.mediaType.height)
        frame = frame.reformat(format=self.ctx.pix_fmt)
        frame.pts = self.frames
        self.frames += 1
        data = b''.join(bytes(p) for p in self.ctx.encode(frame))
        return data or None

    def close(self):
        self.ctx = None


def _fraction(num, den):
    from fractions import Fraction
    return Fraction(int(num), max(1, int(den)))


# ---------------------------------------------------------------------------
# Camera sources (FreeRDP ICamHal)
# ---------------------------------------------------------------------------

class CameraSource(object):
    """
    @summary: Where frames come from. devices() -> [(deviceId, name)];
    modes(deviceId) -> [(width, height, fps)]; start(deviceId, width, height, fps,
    onFrame(bgra)) -> bool; stop(deviceId). onFrame may be called from any thread.
    """

    def devices(self):
        return []

    def modes(self, deviceId):
        return []

    def start(self, deviceId, width, height, fps, onFrame):
        return False

    def stop(self, deviceId):
        pass


class TestPatternSource(CameraSource):
    """A synthetic camera (moving colour bars), for tests and `--camera test`."""

    def __init__(self, name=u'RdpyManyQt test camera', modes=((640, 480, 30), (320, 240, 30))):
        self.name = name
        self._modes = list(modes)
        self._timers = {}

    def devices(self):
        return [(u'test', self.name)]

    def modes(self, deviceId):
        return list(self._modes)

    def frame(self, width, height, index):
        from numpy import arange, int32, uint8, zeros
        x = arange(width, dtype=int32)[None, :]
        y = arange(height, dtype=int32)[:, None]
        img = zeros((height, width, 4), uint8)
        img[..., 0] = ((x + index * 4) * 255 // max(1, width)) % 256
        img[..., 1] = (y * 255 // max(1, height)) % 256
        img[..., 2] = ((x // 40 + y // 40 + index // 10) % 2) * 255
        img[..., 3] = 255
        return img

    def start(self, deviceId, width, height, fps, onFrame):
        stop = threading.Event()
        self._timers[deviceId] = stop

        def run():
            index = 0
            while not stop.wait(1.0 / max(1, fps)):
                onFrame(self.frame(width, height, index))
                index += 1

        t = threading.Thread(target=run, name=u'camera-test-pattern')
        t.daemon = True
        t.start()
        return True

    def stop(self, deviceId):
        stop = self._timers.pop(deviceId, None)
        if stop is not None:
            stop.set()


class QtCameraSource(CameraSource):
    """
    @summary: Cameras through Qt Multimedia: QMediaDevices / QCamera / QVideoSink
    (Qt 6) or QCameraInfo / QCamera / QAbstractVideoSurface (Qt 5). Qt objects
    are created and driven in the GUI thread.
    """

    def __init__(self, nameFilter=None):
        self.nameFilter = (nameFilter or u'').lower()
        self._running = {}

    def _qt(self):
        from ManyQt import QtMultimedia
        return QtMultimedia, hasattr(QtMultimedia, 'QMediaDevices')

    def _cameraInfos(self):
        M, qt6 = self._qt()
        if qt6:
            infos = [(bytes(d.id()).hex() if hasattr(bytes(d.id()), 'hex') else str(d.id()), d.description(), d)
                     for d in M.QMediaDevices.videoInputs()]
        else:
            infos = [(c.deviceName(), c.description(), c) for c in M.QCameraInfo.availableCameras()]
        return [i for i in infos if not self.nameFilter or self.nameFilter in (i[1] or u'').lower()]

    def devices(self):
        try:
            return [(cid, name) for cid, name, _ in self._cameraInfos()]
        except Exception as e:
            log.warning(u'camera: cannot list the cameras: %r' % (e,))
            return []

    def modes(self, deviceId):
        M, qt6 = self._qt()
        for cid, name, info in self._cameraInfos():
            if cid != deviceId:
                continue
            seen = {}
            if qt6:
                for f in info.videoFormats():
                    size = f.resolution()
                    key = (size.width() - size.width() % 2, size.height() - size.height() % 2)
                    seen[key] = max(seen.get(key, 0), int(f.maxFrameRate() or 30))
            else:
                camera = M.QCamera(info)
                camera.load()
                for size in camera.supportedViewfinderResolutions():
                    seen[(size.width() - size.width() % 2, size.height() - size.height() % 2)] = 30
                camera.unload()
            if not seen:
                seen[(640, 480)] = 30
            modes = sorted(((w, h, min(fps, 60)) for (w, h), fps in seen.items() if w > 0 and h > 0),
                           key=lambda m: -m[0] * m[1])
            return modes[:16]
        return []

    def start(self, deviceId, width, height, fps, onFrame):
        from rdpy.ui.qt6 import _get_qt_invoker
        result = {}
        done = threading.Event()

        def startInGui():
            try:
                result['ok'] = self._startGui(deviceId, width, height, fps, onFrame)
            except Exception as e:
                log.warning(u'camera: cannot start %s: %r' % (deviceId, e))
                result['ok'] = False
            done.set()

        mainThread = getattr(threading, 'main_thread', None)
        if mainThread is not None and threading.current_thread() is mainThread():
            startInGui()
        else:
            _get_qt_invoker().invoke(startInGui)
            done.wait(5.0)
        return result.get('ok', False)

    def _startGui(self, deviceId, width, height, fps, onFrame):
        from numpy import frombuffer, uint8
        from ManyQt import QtGui
        M, qt6 = self._qt()
        info = [i for i in self._cameraInfos() if i[0] == deviceId]
        if not info:
            return False
        info = info[0][2]
        fmt = QtGui.QImage.Format.Format_RGB32

        def deliver(image):
            if image.isNull():
                return
            image = image.convertToFormat(fmt)
            if (image.width(), image.height()) != (width, height):
                from ManyQt.QtCore import Qt
                image = image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                                     Qt.TransformationMode.FastTransformation)
            ptr = image.constBits()
            if hasattr(ptr, 'setsize'):
                ptr.setsize(image.sizeInBytes() if hasattr(image, 'sizeInBytes') else image.byteCount())
            arr = frombuffer(bytes(ptr), uint8).reshape(image.height(), image.bytesPerLine())
            onFrame(arr[:, :width * 4].reshape(height, width, 4).copy())

        if qt6:
            camera = M.QCamera(info)
            for f in info.videoFormats():
                r = f.resolution()
                if (r.width() - r.width() % 2, r.height() - r.height() % 2) == (width, height):
                    camera.setCameraFormat(f)
                    break
            session = M.QMediaCaptureSession()
            sink = M.QVideoSink()
            session.setCamera(camera)
            session.setVideoSink(sink)
            sink.videoFrameChanged.connect(lambda frame: deliver(frame.toImage()))
            camera.start()
            self._running[deviceId] = (camera, session, sink)
        else:
            surface = _Qt5Surface(M, deliver)
            camera = M.QCamera(info)
            camera.setViewfinder(surface)
            settings = M.QCameraViewfinderSettings()
            from ManyQt.QtCore import QSize
            settings.setResolution(QSize(width, height))
            camera.setViewfinderSettings(settings)
            camera.start()
            self._running[deviceId] = (camera, surface)
        log.info(u'camera: %s capturing %dx%d' % (deviceId, width, height))
        return True

    def stop(self, deviceId):
        objects = self._running.pop(deviceId, None)
        if objects is None:
            return
        from rdpy.ui.qt6 import _get_qt_invoker
        _get_qt_invoker().invoke(lambda: objects[0].stop())


def _Qt5Surface(M, deliver):
    """A QAbstractVideoSurface giving each frame as a QImage (Qt 5.15 QVideoFrame.image())."""

    class Surface(M.QAbstractVideoSurface):
        def supportedPixelFormats(self, handleType=None):
            F = M.QVideoFrame
            return [F.Format_RGB32, F.Format_ARGB32, F.Format_YUYV, F.Format_NV12, F.Format_YUV420P, F.Format_Jpeg]

        def present(self, frame):
            try:
                deliver(frame.image())
            except Exception as e:
                log.debug(u'camera: frame dropped: %r' % (e,))
            return True

    return Surface()


# ---------------------------------------------------------------------------
# The plugin (FreeRDP CameraPlugin / CameraDevice)
# ---------------------------------------------------------------------------

class Rdpecam(object):
    """
    @summary: Camera redirection plugin. addTo(drdynvcLayer) registers the
    enumerator listener; each announced camera gets its own channel listener.
    """

    def __init__(self):
        self.enabled = False
        self.source = None  # CameraSource (QtCameraSource by default)
        self.outputFormat = None  # None: H.264 if possible, else MJPG
        self.version = PROTOCOL_VERSION
        self._drdynvc = None
        self._devices = {}  # channel name -> _Device
        self._enumerator = None

    def addTo(self, drdynvcLayer):
        self._drdynvc = drdynvcLayer
        drdynvcLayer.addListener(ENUMERATOR_CHANNEL, self.enumeratorFactory)

    def enumeratorFactory(self, channel):
        if not self.enabled:
            return None
        if self._enumerator is not None:
            log.warning(u'camera: enumerator channel already open')
            return None
        self._enumerator = _Enumerator(self, channel)
        return self._enumerator

    def _source(self):
        if self.source is None:
            self.source = QtCameraSource()
        return self.source

    def _addDevices(self, channel):
        source = self._source()
        devices = source.devices()
        if not devices:
            log.info(u'camera: no camera to redirect')
        for index, (deviceId, name) in enumerate(devices):
            channelName = u'RDCamera_Device_%d' % index
            device = self._devices.get(channelName)
            if device is None:
                device = _Device(self, channelName, deviceId, name)
                self._devices[channelName] = device
                if self._drdynvc is not None:
                    self._drdynvc.addListener(channelName, device.channelFactory)
            nameW = (name + u'\x00').encode('utf-16-le')
            channel.write(header(self.version, CAM_MSG_ID_DeviceAddedNotification) + nameW +
                          channelName.encode('ascii') + b'\x00')
            log.info(u'camera: %s redirected (%s)' % (name, channelName))

    def _enumeratorClosed(self, handler):
        if handler is self._enumerator:
            self._enumerator = None

    def shutdown(self):
        for device in list(self._devices.values()):
            device.stopStreams()

    def describe(self):
        names = [d.name for d in self._devices.values()]
        return ', '.join(names) if names else None


class _Enumerator(object):
    """RDCamera_Device_Enumerator (FreeRDP camera_device_enum_main.c)."""

    def __init__(self, plugin, channel):
        self.plugin = plugin
        self.channel = channel

    def opened(self):
        # ecam_on_open: Select Version Request with the client version
        self.channel.write(header(PROTOCOL_VERSION, CAM_MSG_ID_SelectVersionRequest))

    def recv(self, data):
        data = bytes(data)
        if len(data) < 2:
            return
        version, msgId = unpack_from('<BB', data, 0)
        if msgId == CAM_MSG_ID_SelectVersionResponse:
            if version > PROTOCOL_VERSION:
                log.warning(u'camera: server protocol %d is newer than %d' % (version, PROTOCOL_VERSION))
                return
            self.plugin.version = version
            self.plugin._addDevices(self.channel)
        else:
            self.channel.write(header(self.plugin.version, CAM_MSG_ID_ErrorResponse) +
                               struct_pack('<I', CAM_ERROR_CODE_OperationNotSupported))

    def closed(self):
        self.plugin._enumeratorClosed(self)


class _Device(object):
    """One redirected camera (FreeRDP CameraDevice, one stream)."""

    def __init__(self, plugin, channelName, deviceId, name):
        self.plugin = plugin
        self.channelName = channelName
        self.deviceId = deviceId
        self.name = name
        self.channel = None
        self.currentMediaType = None
        self.streaming = False
        self._offered = []
        self._lock = threading.Lock()
        self._requested = 0
        self._pendingFrame = None
        self._encoder = None
        self._work = None
        self._worker = None
        self.samplesSent = 0

    def channelFactory(self, channel):
        if not self.plugin.enabled:
            return None
        self.channel = channel
        return _DeviceChannel(self, channel)

    @property
    def version(self):
        return self.plugin.version

    def _send(self, msgId, body=b''):
        if self.channel is not None:
            self.channel.write(header(self.version, msgId) + body)

    def _error(self, code):
        self._send(CAM_MSG_ID_ErrorResponse, struct_pack('<I', code))

    # ---- requests (FreeRDP ecam_dev_process_*) ----

    def recv(self, data):
        data = bytes(data)
        if len(data) < 2:
            return
        msgId = bytearray(data)[1]
        body = data[2:]
        handler = {
            CAM_MSG_ID_ActivateDeviceRequest: self._activate,
            CAM_MSG_ID_DeactivateDeviceRequest: self._deactivate,
            CAM_MSG_ID_StreamListRequest: self._streamList,
            CAM_MSG_ID_MediaTypeListRequest: self._mediaTypeList,
            CAM_MSG_ID_CurrentMediaTypeRequest: self._currentMediaType,
            CAM_MSG_ID_PropertyListRequest: self._propertyList,
            CAM_MSG_ID_StartStreamsRequest: self._startStreams,
            CAM_MSG_ID_StopStreamsRequest: self._stopStreams,
            CAM_MSG_ID_SampleRequest: self._sampleRequest,
        }.get(msgId)
        if handler is None:
            self._error(CAM_ERROR_CODE_OperationNotSupported)
            return
        handler(body)

    def _activate(self, body):
        self._send(CAM_MSG_ID_SuccessResponse)

    def _deactivate(self, body):
        self.stopStreams()
        self._send(CAM_MSG_ID_SuccessResponse)

    def _streamList(self, body):
        self._send(CAM_MSG_ID_StreamListResponse,
                   struct_pack('<HBBB', CAM_STREAM_FRAME_SOURCE_TYPE_Color, CAM_STREAM_CATEGORY_Capture, 1, 0))

    def _streamIndex(self, body):
        if len(body) < 1:
            self._error(CAM_ERROR_CODE_InvalidMessage)
            return None
        index = bytearray(body)[0]
        if index >= ECAM_DEVICE_MAX_STREAMS:
            self._error(CAM_ERROR_CODE_InvalidStreamNumber)
            return None
        return index

    def offeredMediaTypes(self):
        """The media types sent in the Media Type List Response (output format)."""
        fmt = self.plugin.outputFormat or availableOutputFormat()
        if fmt is None:
            return []
        modes = self.plugin._source().modes(self.deviceId)
        return [MediaType(fmt, w - w % 2, h - h % 2, fps, 1) for (w, h, fps) in modes][:ECAM_MAX_MEDIA_TYPE_DESCRIPTORS]

    def _mediaTypeList(self, body):
        if self._streamIndex(body) is None:
            return
        self._offered = self.offeredMediaTypes()
        if not self._offered:
            self._error(CAM_ERROR_CODE_ItemNotFound)
            return
        if self.currentMediaType is None:
            self.currentMediaType = self._offered[0]
        self._send(CAM_MSG_ID_MediaTypeListResponse, b''.join(m.pack() for m in self._offered))

    def _currentMediaType(self, body):
        if self._streamIndex(body) is None:
            return
        if self.currentMediaType is None:
            self._error(CAM_ERROR_CODE_NotInitialized)
            return
        self._send(CAM_MSG_ID_CurrentMediaTypeResponse, self.currentMediaType.pack())

    def _propertyList(self, body):
        self._send(CAM_MSG_ID_PropertyListResponse)  # no property (as FreeRDP)

    def _startStreams(self, body):
        if len(body) < 1 + MEDIA_TYPE_SIZE:
            self._error(CAM_ERROR_CODE_InvalidMessage)
            return
        if self._streamIndex(body) is None:
            return
        try:
            mediaType = MediaType.unpack(body, 1)
        except (ValueError, error):
            self._error(CAM_ERROR_CODE_InvalidMessage)
            return
        if not self._offered:
            self._offered = self.offeredMediaTypes()
        if mediaType not in self._offered:
            self._error(CAM_ERROR_CODE_InvalidMessage)
            return
        if self.streaming:
            log.warning(u'camera: stream already started')
            self._error(CAM_ERROR_CODE_UnexpectedError)
            return
        try:
            self._encoder = SampleEncoder(mediaType)
        except Exception as e:
            log.warning(u'camera: no encoder for %r: %r' % (mediaType, e))
            self._error(CAM_ERROR_CODE_UnexpectedError)
            return
        self.currentMediaType = mediaType
        with self._lock:
            self._requested = 0
            self._pendingFrame = None
        self._work = queue.Queue()
        self._worker = threading.Thread(
            target=self._encodeLoop, args=(self._work, self._encoder), name=u'camera-encoder')
        self._worker.daemon = True
        self._worker.start()
        self.streaming = True
        fps = max(1, int(round(float(mediaType.fpsNum) / mediaType.fpsDen)))
        if not self.plugin._source().start(self.deviceId, mediaType.width, mediaType.height, fps, self._onFrame):
            self.stopStreams()
            self._error(CAM_ERROR_CODE_UnexpectedError)
            return
        log.info(u'camera: %s streaming %r (%s)' % (self.name, mediaType, self._encoder.codec))
        self._send(CAM_MSG_ID_SuccessResponse)

    def _stopStreams(self, body):
        self.stopStreams()
        self._send(CAM_MSG_ID_SuccessResponse)

    def stopStreams(self):
        if self.streaming:
            self.streaming = False
            self.plugin._source().stop(self.deviceId)
            log.info(u'camera: %s stopped' % self.name)
        if self._work is not None:
            self._work.put(None)
            self._work = None
        with self._lock:
            self._requested = 0
            self._pendingFrame = None

    def _sampleRequest(self, body):
        index = self._streamIndex(body)
        if index is None:
            return
        if not self.streaming:
            self._send(CAM_MSG_ID_SampleErrorResponse, struct_pack('<BI', index, CAM_ERROR_CODE_NotInitialized))
            return
        with self._lock:
            self._requested += 1
            frame, self._pendingFrame = self._pendingFrame, None
            if frame is not None:
                self._requested -= 1
        if frame is not None:
            self._work.put(frame)

    # ---- frames (any thread) ----

    def _onFrame(self, bgra):
        """A captured frame: encoded only if the server is waiting for a sample, else kept as
        the latest frame (older ones are dropped before encoding, so H.264 stays valid)."""
        if not self.streaming:
            return
        with self._lock:
            if self._requested > 0:
                self._requested -= 1
                work = self._work
            else:
                self._pendingFrame = bgra
                return
        if work is not None:
            work.put(bgra)

    def _encodeLoop(self, work, encoder):
        while True:
            frame = work.get()
            if frame is None:
                break
            try:
                sample = encoder.encode(frame)
            except Exception as e:
                log.warning(u'camera: encoding failed: %r' % (e,))
                sample = None
            if sample is None:
                with self._lock:
                    self._requested += 1  # nothing produced: the request is still open
                continue
            if self.streaming:
                self._send(CAM_MSG_ID_SampleResponse, struct_pack('<B', 0) + sample)
                self.samplesSent += 1
        encoder.close()

    def closed(self):
        self.stopStreams()
        self.channel = None


class _DeviceChannel(object):
    def __init__(self, device, channel):
        self.device = device
        self.channel = channel

    def recv(self, data):
        self.device.recv(data)

    def closed(self):
        if self.device.channel is self.channel:
            self.device.closed()
