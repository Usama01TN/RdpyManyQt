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
Low latency PCM playback for the RDPSND channel.

Three parts:

* _PcmRing: thread safe jitter buffer with a latency target. It starts
  playing once enough audio is buffered, drops the oldest audio when the
  buffer grows past the target (so sound never drifts behind the video)
  and fades in / out around gaps to avoid clicks.
* PcmConverter: converts the server PCM (8/16-bit, mono/stereo, any rate)
  to the format the output device accepts (sample type, channels, rate).
* AudioOutput: owns a dedicated Qt thread running the QAudioSink (Qt6) or
  QAudioOutput (Qt5). The device pulls audio from that thread, so a busy
  GUI / network thread can not starve the sound card. Works with every
  binding ManyQt supports (PyQt5, PyQt6, PySide2, PySide6).
"""
from __future__ import absolute_import, division, print_function
import collections
import threading
import numpy as np
from rdpy.core.compat import monotonic
import rdpy.core.log as log

# Fade length (frames) used around underruns / dropped audio: ~3 ms at 44.1 kHz
_FADE_FRAMES = 128
# Latency target bounds (seconds of audio waiting in the jitter buffer)
_TARGET_MIN = 0.06
_TARGET_MAX = 0.35
# Device buffer requested from Qt (seconds)
_DEVICE_BUFFER_QT6 = 0.06
_DEVICE_BUFFER_QT5 = 0.10

# Output sample kinds: numpy dtype and bytes per sample
_KINDS = {
    'u8': (np.uint8, 1),
    's16': (np.dtype('<i2'), 2),
    's32': (np.dtype('<i4'), 4),
    'f32': (np.dtype('<f4'), 4),
}


class PcmFormat(object):
    """Raw PCM layout: rate (Hz), channels and sample kind (see _KINDS)."""

    __slots__ = ('rate', 'channels', 'kind')

    def __init__(self, rate, channels, kind):
        self.rate = int(rate)
        self.channels = int(channels)
        self.kind = kind

    @property
    def sampleBytes(self):
        return _KINDS[self.kind][1]

    @property
    def frameBytes(self):
        return self.sampleBytes * self.channels

    @property
    def bytesPerSecond(self):
        return self.frameBytes * self.rate

    def __eq__(self, other):
        return (isinstance(other, PcmFormat) and self.rate == other.rate and
                self.channels == other.channels and self.kind == other.kind)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return "PcmFormat(%d Hz, %d ch, %s)" % (self.rate, self.channels, self.kind)


def _toFloat(data, fmt):
    """bytes in *fmt* -> float32 array (frames, channels) in [-1, 1]."""
    dtype = _KINDS[fmt.kind][0]
    a = np.frombuffer(data, dtype=dtype, count=len(data) // fmt.sampleBytes)
    if fmt.kind == 'u8':
        f = (a.astype(np.float32) - 128.0) * (1.0 / 128.0)
    elif fmt.kind == 's16':
        f = a.astype(np.float32) * (1.0 / 32768.0)
    elif fmt.kind == 's32':
        f = a.astype(np.float32) * (1.0 / 2147483648.0)
    else:
        f = a.astype(np.float32)
    frames = len(f) // fmt.channels
    return f[:frames * fmt.channels].reshape(frames, fmt.channels)


def _fromFloat(f, kind):
    """float32 array -> bytes of the given sample kind."""
    np.clip(f, -1.0, 1.0, out=f)
    if kind == 'u8':
        return (f * 127.0 + 128.0).astype(np.uint8).tobytes()
    if kind == 's16':
        return (f * 32767.0).astype('<i2').tobytes()
    if kind == 's32':
        return (f * 2147483647.0).astype('<i4').tobytes()
    return f.astype('<f4').tobytes()


class PcmConverter(object):
    """
    Stateful PCM converter (sample type, channel count, sample rate).
    The resampler is linear and keeps its phase between calls, so chunk
    boundaries do not click. Identity conversions return the input as is.
    """

    def __init__(self, src, dst):
        """
        @param src: {PcmFormat} server format
        @param dst: {PcmFormat} device format
        """
        self.src = src
        self.dst = dst
        self.identity = (src == dst)
        self._ratio = float(src.rate) / float(dst.rate)
        self._prev = np.zeros((1, dst.channels), dtype=np.float32)
        self._t = 1.0  # read position in [prev, chunk...] coordinates
        self._partial = b''  # trailing bytes of an incomplete input frame

    def convert(self, data):
        if self.identity:
            return bytes(data)
        if self._partial:
            data = self._partial + bytes(data)
            self._partial = b''
        cut = len(data) - len(data) % self.src.frameBytes
        if cut != len(data):
            self._partial = bytes(data[cut:])
            data = data[:cut]
        if not data:
            return b''
        f = _toFloat(data, self.src)
        # channels
        if f.shape[1] != self.dst.channels:
            if self.dst.channels == 1:
                f = f.mean(axis=1, keepdims=True)
            elif f.shape[1] == 1:
                f = np.repeat(f, self.dst.channels, axis=1)
            else:
                out = np.zeros((f.shape[0], self.dst.channels), dtype=np.float32)
                n = min(f.shape[1], self.dst.channels)
                out[:, :n] = f[:, :n]
                f = out
        # rate
        if self.src.rate != self.dst.rate:
            f = self._resample(np.ascontiguousarray(f, dtype=np.float32))
        return _fromFloat(np.ascontiguousarray(f, dtype=np.float32), self.dst.kind)

    def _resample(self, x):
        n = x.shape[0]
        buf = np.concatenate((self._prev, x), axis=0)  # buf[0] = last frame of previous chunk
        if self._t > n:
            self._t -= n
            self._prev = x[-1:].copy()
            return np.zeros((0, x.shape[1]), dtype=np.float32)
        count = int(np.floor((n - self._t) / self._ratio)) + 1
        t = self._t + np.arange(count, dtype=np.float64) * self._ratio
        i0 = np.floor(t).astype(np.int64)
        frac = (t - i0).astype(np.float32)[:, None]
        i1 = np.minimum(i0 + 1, n)
        y = buf[i0] * (1.0 - frac) + buf[i1] * frac
        self._t = t[-1] + self._ratio - n
        self._prev = x[-1:].copy()
        return y


class _PcmRing(object):
    """
    Thread safe jitter buffer between the network thread (write) and the
    audio thread (read).

    The latency target follows the duration of the packets the server
    sends: playback starts (and restarts after an underrun) once `target`
    seconds are buffered, and audio older than `target + packet + margin`
    is dropped, so the delay between picture and sound stays bounded.
    """

    def __init__(self, fmt, clock=monotonic):
        self.fmt = fmt
        self._clock = clock
        self._lock = threading.Lock()
        self._buf = bytearray()
        self._pos = 0
        self._primed = False
        self._fadeInNext = False
        self._lastWrite = 0.0
        self._packets = collections.deque(maxlen=24)
        self._readChunk = 0.0  # largest chunk the device pulls at once (seconds)
        self.target = _TARGET_MIN
        self.dropped = 0  # bytes dropped to keep latency (stats)
        self.underruns = 0

    # --- helpers (lock held) ---
    def _available(self):
        return len(self._buf) - self._pos

    def _align(self, n):
        return n - n % self.fmt.frameBytes

    def _packetSeconds(self):
        return max(self._packets) if self._packets else 0.0

    def _updateTarget(self):
        # Enough to absorb the server packet interval and arrival jitter, and
        # to serve the device's largest pull in one go (Qt5 / PulseAudio pull
        # ~90 ms at a time, WASAPI ~10 ms).
        p = self._packetSeconds()
        need = max(1.3 * p + 0.04, self._readChunk + 0.5 * p + 0.02)
        self.target = min(_TARGET_MAX, max(_TARGET_MIN, need))

    def _maxSeconds(self):
        return self.target + max(self._packetSeconds(), 0.05) + 0.08

    # --- network side ---
    def write(self, data):
        if not data:
            return
        bps = float(self.fmt.bytesPerSecond)
        with self._lock:
            self._packets.append(len(data) / bps)
            self._updateTarget()
            self._lastWrite = self._clock()
            if self._pos > (1 << 20) or (self._pos and self._pos >= len(self._buf)):
                del self._buf[:self._pos]
                self._pos = 0
            self._buf.extend(data)
            limit = self._align(int(self._maxSeconds() * bps))
            avail = self._available()
            if avail > limit:
                # Too far behind the server: skip the oldest audio so the
                # sound catches up with the picture again.
                drop = self._align(avail - int(self.target * bps))
                self._pos += drop
                self.dropped += drop
                self._fadeInNext = True

    def levelSeconds(self):
        with self._lock:
            return self._available() / float(self.fmt.bytesPerSecond)

    def clear(self):
        with self._lock:
            self._buf = bytearray()
            self._pos = 0
            self._primed = False

    # --- audio side ---
    def read(self, maxSize):
        """Return exactly `maxSize` bytes rounded down to whole frames."""
        n = self._align(maxSize)
        if n <= 0:
            return b''
        with self._lock:
            chunk = n / float(self.fmt.bytesPerSecond)
            if chunk > self._readChunk:
                self._readChunk = min(chunk, 0.25)
                self._updateTarget()
            avail = self._available()
            if not self._primed:
                idle = self._clock() - self._lastWrite
                stalled = avail > 0 and idle > max(0.05, 2.0 * self._packetSeconds() + 0.05)
                if avail >= int(self.target * self.fmt.bytesPerSecond) or stalled:
                    # enough buffered, or the server stopped sending: play the tail
                    self._primed = True
                    self._fadeInNext = True
                else:
                    return b'\x00' * n
            take = self._align(min(n, avail))
            chunk = bytearray(self._buf[self._pos:self._pos + take])
            self._pos += take
            if self._fadeInNext and take:
                _fade(chunk, self.fmt, True)
                self._fadeInNext = False
            if take < n:
                # underrun: fade the tail out, pad with silence, re-prime
                if take:
                    _fade(chunk, self.fmt, False)
                self._primed = False
                self.underruns += 1
                chunk.extend(b'\x00' * (n - take))
            return bytes(chunk)


def _fade(chunk, fmt, fadeIn):
    """In place fade of the first (fade in) or last (fade out) frames."""
    frames = min(_FADE_FRAMES, len(chunk) // fmt.frameBytes)
    if frames < 2 or fmt.kind == 'u8':
        return
    nbytes = frames * fmt.frameBytes
    start = 0 if fadeIn else len(chunk) - nbytes
    dtype = _KINDS[fmt.kind][0]
    seg = np.frombuffer(chunk, dtype=dtype, count=nbytes // fmt.sampleBytes,
                        offset=start).reshape(frames, fmt.channels)
    gain = np.linspace(0.0, 1.0, frames, dtype=np.float32)
    if not fadeIn:
        gain = gain[::-1]
    out = (seg.astype(np.float32) * gain[:, None]).astype(dtype)
    chunk[start:start + nbytes] = out.tobytes()


# ---------------------------------------------------------------------------
# Qt side
# ---------------------------------------------------------------------------

def _enum(owner, scope, name):
    """Qt6 scoped enum (owner.scope.name) or Qt5 flat enum (owner.name)."""
    s = getattr(owner, scope, None)
    if s is not None and hasattr(s, name):
        return getattr(s, name)
    return getattr(owner, name)


def _signalClass():
    from ManyQt import QtCore
    return getattr(QtCore, 'pyqtSignal', None) or getattr(QtCore, 'Signal')


def _slotDecorator():
    from ManyQt import QtCore
    return getattr(QtCore, 'pyqtSlot', None) or getattr(QtCore, 'Slot')


class _QtAudioApi(object):
    """Hides the differences between the Qt5 and Qt6 audio classes."""

    def __init__(self):
        from ManyQt import QtMultimedia as M
        self.M = M
        self.qt6 = hasattr(M.QAudioFormat, 'setSampleFormat')
        self.name = "Qt6 QAudioSink" if self.qt6 else "Qt5 QAudioOutput"
        self.inputName = "Qt6 QAudioSource" if self.qt6 else "Qt5 QAudioInput"

    def defaultDevice(self):
        M = self.M
        if self.qt6:
            dev = M.QMediaDevices.defaultAudioOutput()
        else:
            dev = M.QAudioDeviceInfo.defaultOutputDevice()
        return None if dev.isNull() else dev

    def defaultInputDevice(self):
        M = self.M
        if self.qt6:
            dev = M.QMediaDevices.defaultAudioInput()
        else:
            dev = M.QAudioDeviceInfo.defaultInputDevice()
        return None if dev.isNull() else dev

    def createSource(self, dev, qf):
        M = self.M
        if self.qt6:
            return M.QAudioSource(dev, qf)
        return M.QAudioInput(dev, qf)

    def deviceName(self, dev):
        try:
            return dev.description() if self.qt6 else dev.deviceName()
        except Exception:
            return "?"

    def makeFormat(self, pcm):
        M = self.M
        qf = M.QAudioFormat()
        qf.setSampleRate(pcm.rate)
        qf.setChannelCount(pcm.channels)
        if self.qt6:
            name = {'u8': 'UInt8', 's16': 'Int16', 's32': 'Int32', 'f32': 'Float'}[pcm.kind]
            qf.setSampleFormat(_enum(M.QAudioFormat, 'SampleFormat', name))
        else:
            qf.setCodec("audio/pcm")
            qf.setByteOrder(_enum(M.QAudioFormat, 'Endian', 'LittleEndian'))
            qf.setSampleSize(pcm.sampleBytes * 8)
            stype = {'u8': 'UnSignedInt', 's16': 'SignedInt', 's32': 'SignedInt', 'f32': 'Float'}[pcm.kind]
            qf.setSampleType(_enum(M.QAudioFormat, 'SampleType', stype))
        return qf

    def toPcm(self, qf):
        """QAudioFormat -> PcmFormat, or None when the layout is not usable."""
        M = self.M
        try:
            rate, channels = qf.sampleRate(), qf.channelCount()
            if self.qt6:
                sf = qf.sampleFormat()
                kind = None
                for name, k in (('UInt8', 'u8'), ('Int16', 's16'), ('Int32', 's32'), ('Float', 'f32')):
                    if sf == _enum(M.QAudioFormat, 'SampleFormat', name):
                        kind = k
            else:
                size, stype = qf.sampleSize(), qf.sampleType()
                kind = None
                if stype == _enum(M.QAudioFormat, 'SampleType', 'Float') and size == 32:
                    kind = 'f32'
                elif size == 8:
                    kind = 'u8'
                elif size == 16:
                    kind = 's16'
                elif size == 32:
                    kind = 's32'
            if kind is None or rate <= 0 or channels <= 0:
                return None
            return PcmFormat(rate, min(channels, 2), kind)
        except Exception:
            return None

    def chooseFormat(self, dev, src):
        """Best device format for the server format *src* (PcmFormat)."""
        wanted = PcmFormat(src.rate, src.channels if src.channels in (1, 2) else 2, 's16')
        candidates = [wanted, PcmFormat(src.rate, 2, 's16')]
        pref = None
        try:
            pref = self.toPcm(dev.preferredFormat())
        except Exception:
            pass
        if pref is not None:
            candidates.append(PcmFormat(pref.rate, 2, 's16'))
            candidates.append(pref)
        if not self.qt6:
            try:
                near = self.toPcm(dev.nearestFormat(self.makeFormat(wanted)))
                if near is not None:
                    candidates.append(near)
            except Exception:
                pass
        for c in candidates:
            try:
                if dev.isFormatSupported(self.makeFormat(c)):
                    return c
            except Exception:
                pass
        # Nothing reported as supported: try the preferred one anyway
        return pref or wanted

    def createSink(self, dev, qf):
        M = self.M
        if self.qt6:
            return M.QAudioSink(dev, qf)
        return M.QAudioOutput(dev, qf)


_pullDeviceClass = None


def _pullDevice():
    """QIODevice subclass that the audio sink pulls from (pull mode)."""
    global _pullDeviceClass
    if _pullDeviceClass is None:
        from ManyQt.QtCore import QIODevice

        class _PullDevice(QIODevice):
            def __init__(self, ring):
                QIODevice.__init__(self)
                self._ring = ring

            def readData(self, maxSize):
                try:
                    return self._ring.read(int(maxSize))
                except Exception as e:  # never raise into Qt
                    log.warning("audio: read error %s" % e)
                    return b'\x00' * int(maxSize)

            def writeData(self, data):
                return -1

            def bytesAvailable(self):
                # A live stream: always report one device buffer worth of data
                # (silence is generated on underrun) so the sink keeps pulling.
                return 65536 + QIODevice.bytesAvailable(self)

            def isSequential(self):
                return True

        _pullDeviceClass = _PullDevice
    return _pullDeviceClass


class AudioOutput(object):
    """
    Plays PCM through Qt from a dedicated audio thread.

    start() / stop() are thread safe. When the device is ready, the
    `onReady(token, ring, converter, deviceLatency)` callback is called (from
    the audio thread) with the ring buffer to write converted audio into.
    """

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def instance(cls):
        """Shared instance, or None when Qt multimedia is not available."""
        with cls._lock:
            if cls._instance is None:
                try:
                    cls._instance = cls()
                except Exception as e:
                    log.warning("audio: playback disabled (%s)" % e)
                    cls._instance = False
            return cls._instance or None

    def __init__(self):
        """Must be created on the GUI thread (see instance())."""
        from ManyQt import QtCore
        self._api = _QtAudioApi()
        try:
            self._api.defaultDevice()  # initialise the Qt multimedia backend here
        except Exception:
            pass
        Signal = _signalClass()
        Slot = _slotDecorator()

        api = self._api

        class _Worker(QtCore.QObject):
            startRequested = Signal(object)
            stopRequested = Signal(object)
            captureRequested = Signal(object)
            stopAllRequested = Signal()

            def __init__(self):
                QtCore.QObject.__init__(self)
                self.sinks = {}

            @Slot(object)
            def _start(self, req):
                token, src, onReady, dev = req
                try:
                    if dev is None:
                        log.warning("audio: no audio output device available")
                        return
                    out = api.chooseFormat(dev, src)
                    qf = api.makeFormat(out)
                    sink = api.createSink(dev, qf)
                    bufSecs = _DEVICE_BUFFER_QT6 if api.qt6 else _DEVICE_BUFFER_QT5
                    sink.setBufferSize(max(4096, int(out.bytesPerSecond * bufSecs)))
                    ring = _PcmRing(out)
                    pull = _pullDevice()(ring)
                    pull.open(_enum(QtCore.QIODevice, 'OpenModeFlag', 'ReadOnly'))
                    sink.start(pull)
                    try:
                        actual = sink.bufferSize() / float(out.bytesPerSecond)
                    except Exception:
                        actual = bufSecs
                    self.sinks[token] = (sink, pull)
                    log.info("audio: playing %s on '%s' via %s (server %s, device buffer %d ms)" %
                             (out, api.deviceName(dev), api.name, src, int(actual * 1000)))
                    onReady(token, ring, PcmConverter(src, out), actual)
                except Exception as e:
                    log.warning("audio: cannot open the output device: %r" % (e,))

            @Slot(object)
            def _stop(self, token):
                entry = self.sinks.pop(token, None)
                if entry is None:
                    return
                sink, pull = entry
                try:
                    sink.stop()
                except Exception:
                    pass
                try:
                    pull.close()
                except Exception:
                    pass

            @Slot(object)
            def _startCapture(self, req):
                token, dst, onData, dev = req
                try:
                    if dev is None:
                        log.warning("microphone: no audio input device available")
                        return
                    fmt = api.chooseFormat(dev, dst)
                    source = api.createSource(dev, api.makeFormat(fmt))
                    source.setBufferSize(max(2048, int(fmt.bytesPerSecond * 0.04)))
                    converter = PcmConverter(fmt, dst)
                    io = source.start()  # push mode: read what the device captured

                    def readCaptured():
                        chunk = io.readAll()
                        raw = chunk.data() if hasattr(chunk, 'data') else chunk
                        raw = bytes(raw)
                        if raw:
                            out = converter.convert(raw)
                            if out:
                                onData(token, out)

                    io.readyRead.connect(readCaptured)
                    self.sinks[token] = (source, io)
                    log.info("microphone: capturing %s from '%s' via %s" %
                             (fmt, api.deviceName(dev), api.inputName))
                except Exception as e:
                    log.warning("microphone: cannot open the input device: %r" % (e,))

            @Slot()
            def stopAll(self):
                for token in list(self.sinks):
                    self._stop(token)

        self._thread = QtCore.QThread()
        try:
            self._thread.setObjectName("rdpsnd-audio")
        except Exception:
            pass
        self._worker = _Worker()
        self._worker.moveToThread(self._thread)
        queued = _enum(QtCore.Qt, 'ConnectionType', 'QueuedConnection')
        self._worker.startRequested.connect(self._worker._start, queued)
        self._worker.stopRequested.connect(self._worker._stop, queued)
        self._worker.captureRequested.connect(self._worker._startCapture, queued)
        self._worker.stopAllRequested.connect(
            self._worker.stopAll, _enum(QtCore.Qt, 'ConnectionType', 'BlockingQueuedConnection'))
        self._thread.start()
        try:
            self._thread.setPriority(_enum(QtCore.QThread, 'Priority', 'TimeCriticalPriority'))
        except Exception:
            pass
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)
        self._token = 0
        self._alive = True

    @property
    def backendName(self):
        return self._api.name

    def nextToken(self):
        """@return: {int} a new stream id for start()"""
        with self._lock:
            self._token += 1
            return self._token

    def start(self, src, onReady, token=None):
        """
        @param src: {PcmFormat} format of the PCM that will be written
        @param onReady: callable(token, ring, converter, deviceLatencySeconds)
        @param token: {int} stream id from nextToken() (allocated when None)
        @return: {int} token identifying this stream (pass it to stop())
        """
        if token is None:
            token = self.nextToken()
        if self._alive:
            # Device lookup on the calling (GUI) thread: Qt6 QMediaDevices
            # must not be first used from a secondary thread (it crashes).
            try:
                dev = self._api.defaultDevice()
            except Exception as e:
                log.warning("audio: cannot query the output device: %s" % e)
                dev = None
            self._worker.startRequested.emit((token, src, onReady, dev))
        return token

    def stop(self, token):
        if self._alive and token:
            self._worker.stopRequested.emit(token)

    def startCapture(self, dst, onData):
        """
        @summary: Capture the default microphone (thread safe)
        @param dst: {PcmFormat} format onData receives (converted when needed)
        @param onData: callable(token, bytes) called from the audio thread
        @return: {int} token (pass it to stop())
        """
        token = self.nextToken()
        if self._alive:
            try:
                dev = self._api.defaultInputDevice()
            except Exception as e:
                log.warning("microphone: cannot query the input device: %s" % e)
                dev = None
            self._worker.captureRequested.emit((token, dst, onData, dev))
        return token

    def shutdown(self):
        """Stop every sink and the audio thread (application exit)."""
        if not self._alive:
            return
        self._alive = False
        try:
            self._worker.stopAllRequested.emit()
        except Exception:
            pass
        self._thread.quit()
        self._thread.wait(2000)


class AudioInput(object):
    """Microphone capture on the shared audio thread (see AudioOutput)."""

    @staticmethod
    def instance():
        output = AudioOutput.instance()
        return None if output is None else AudioInput(output)

    def __init__(self, output):
        self._output = output

    def start(self, dst, onData):
        return self._output.startCapture(dst, onData)

    def stop(self, token):
        self._output.stop(token)
