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
Video Redirection Virtual Channel Extension ("TSMF" dynamic channel, MS-RDPEV,
multimedia redirection): Windows Media Player / DirectShow / Media Foundation
playback in the session sends the compressed audio and video samples, which
the client decodes and plays itself (video drawn where the player window is,
audio on the local speakers).
Ported from FreeRDP channels/tsmf/client (tsmf_main.c, tsmf_ifman.c,
tsmf_codec.c, tsmf_media.c) with its FFmpeg decoder (ffmpeg/tsmf_ffmpeg.c)
replaced by PyAV:
 - messages (SHARED_MSG_HEADER, interface / message / function ids), replies
   only where FreeRDP replies, one TSMF channel for control and one per stream
   (SET_CHANNEL_PARAMS), presentations shared by all the channels;
 - media types (major / sub / format GUIDs, WAVEFORMATEX, VIDEOINFOHEADER(2),
   MPEG1/2 video info, MFVIDEOFORMAT), FreeRDP's FFmpeg codec set (VC-1, WMV3,
   MPEG-2 video, H.264 / AVC1 with the avcC built from MPEG2VIDEOINFO, WMA v2 /
   Pro, MP3, MPEG audio layer 2, AAC with the HEAACWAVEINFO prefix skipped,
   AC-3);
 - per stream a playback thread (decode, video paced by the sample duration
   and drawn at the presentation geometry with its visible rectangles, audio
   played and its latency used for the ack time) and an ack thread sending
   PLAYBACK_ACK when the sample is due (half its duration ahead, at most
   66.7 ms, when nothing was decoded), then the end of stream notification
   once all acks are sent.
"""
from __future__ import absolute_import, division, print_function
from struct import error, pack, pack_into, unpack_from
from threading import Event, Lock, Thread
from time import sleep, time
from rdpy.core.compat import queue
from rdpy.core import log

CHANNEL_NAME = u'TSMF'
GUID_SIZE = 16
TSMF_INTERFACE_DEFAULT = 0x00000000
TSMF_INTERFACE_CLIENT_NOTIFICATIONS = 0x00000001
TSMF_INTERFACE_CAPABILITIES = 0x00000002
STREAM_ID_STUB = 0x80000000
STREAM_ID_PROXY = 0x40000000
STREAM_ID_NONE = 0x00000000
RIMCALL_RELEASE = 0x00000001
RIMCALL_QUERYINTERFACE = 0x00000002
RIM_EXCHANGE_CAPABILITY_REQUEST = 0x00000100
PLAYBACK_ACK = 0x00000100
CLIENT_EVENT_NOTIFICATION = 0x00000101
EXCHANGE_CAPABILITIES_REQ = 0x00000100
SET_CHANNEL_PARAMS = 0x00000101
ADD_STREAM = 0x00000102
ON_SAMPLE = 0x00000103
SET_VIDEO_WINDOW = 0x00000104
ON_NEW_PRESENTATION = 0x00000105
SHUTDOWN_PRESENTATION_REQ = 0x00000106
SET_TOPOLOGY_REQ = 0x00000107
CHECK_FORMAT_SUPPORT_REQ = 0x00000108
ON_PLAYBACK_STARTED = 0x00000109
ON_PLAYBACK_PAUSED = 0x0000010a
ON_PLAYBACK_STOPPED = 0x0000010b
ON_PLAYBACK_RESTARTED = 0x0000010c
ON_PLAYBACK_RATE_CHANGED = 0x0000010d
ON_FLUSH = 0x0000010e
ON_STREAM_VOLUME = 0x0000010f
ON_CHANNEL_VOLUME = 0x00000110
ON_END_OF_STREAM = 0x00000111
SET_ALLOCATOR = 0x00000112
NOTIFY_PREROLL = 0x00000113
UPDATE_GEOMETRY_INFO = 0x00000114
REMOVE_STREAM = 0x00000115
SET_SOURCE_VIDEO_RECT = 0x00000116
MMREDIR_CAPABILITY_PLATFORM_MF = 0x00000001
MMREDIR_CAPABILITY_PLATFORM_DSHOW = 0x00000002
TSMM_CLIENT_EVENT_ENDOFSTREAM = 0x0064
TSMM_CLIENT_EVENT_STOP_COMPLETED = 0x00C8
TSMM_CLIENT_EVENT_START_COMPLETED = 0x00C9
TSMM_CLIENT_EVENT_MONITORCHANGED = 0x012C
TSMM_SAMPLE_EXT_HAS_NO_TIMESTAMPS = 0x00000080
MAJOR_TYPE_UNKNOWN, MAJOR_TYPE_VIDEO, MAJOR_TYPE_AUDIO = 0, 1, 2
(SUB_TYPE_UNKNOWN, SUB_TYPE_WVC1, SUB_TYPE_WMA2, SUB_TYPE_WMA9, SUB_TYPE_MP3, SUB_TYPE_MP2A, SUB_TYPE_MP2V,
 SUB_TYPE_WMV3, SUB_TYPE_AAC, SUB_TYPE_H264, SUB_TYPE_AVC1, SUB_TYPE_AC3, SUB_TYPE_WMV2, SUB_TYPE_WMV1,
 SUB_TYPE_MP1V, SUB_TYPE_MP1A, SUB_TYPE_YUY2, SUB_TYPE_MP43, SUB_TYPE_MP4S, SUB_TYPE_MP42, SUB_TYPE_OGG,
 SUB_TYPE_SPEEX, SUB_TYPE_THEORA, SUB_TYPE_FLAC, SUB_TYPE_VP8, SUB_TYPE_VP9, SUB_TYPE_H263, SUB_TYPE_M4S2,
 SUB_TYPE_WMA1) = range(29)
(FORMAT_TYPE_UNKNOWN, FORMAT_TYPE_MFVIDEOFORMAT, FORMAT_TYPE_WAVEFORMATEX, FORMAT_TYPE_MPEG2VIDEOINFO,
 FORMAT_TYPE_VIDEOINFO2, FORMAT_TYPE_MPEG1VIDEOINFO) = range(6)
_MS = b'\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71'  # ...-0000-0010-8000-00AA00389B71
_MPEG = b'\x46\xdb\xcf\x11\xb4\xd1\x00\x80\x5f\x6c\xbb\xea'
# FreeRDP tsmf_codec.c tables (first match wins, in this order)
MAJOR_TYPES = ((b'vids' + _MS, MAJOR_TYPE_VIDEO), (b'auds' + _MS, MAJOR_TYPE_AUDIO))
SUB_TYPES = (
    (b'WVC1' + _MS, SUB_TYPE_WVC1), (b'\x60\x01\x00\x00' + _MS, SUB_TYPE_WMA1),
    (b'\x61\x01\x00\x00' + _MS, SUB_TYPE_WMA2), (b'\x62\x01\x00\x00' + _MS, SUB_TYPE_WMA9),
    (b'\x55\x00\x00\x00' + _MS, SUB_TYPE_MP3), (b'\x2b\x80\x6d\xe0' + _MPEG, SUB_TYPE_MP2A),
    (b'\x26\x80\x6d\xe0' + _MPEG, SUB_TYPE_MP2V), (b'WMV1' + _MS, SUB_TYPE_WMV1), (b'WMV2' + _MS, SUB_TYPE_WMV2),
    (b'WMV3' + _MS, SUB_TYPE_WMV3), (b'\x10\x16\x00\x00' + _MS, SUB_TYPE_AAC), (b'H264' + _MS, SUB_TYPE_H264),
    (b'AVC1' + _MS, SUB_TYPE_AVC1), (b'MP43' + _MS, SUB_TYPE_MP43), (b'MP4V' + _MS, SUB_TYPE_MP4S),
    (b'MP42' + _MS, SUB_TYPE_MP42), (b'M4S2' + _MS, SUB_TYPE_M4S2),
    (b'\x81\xeb\x36\xe4\x4f\x52\xce\x11\x9f\x53\x00\x20\xaf\x0b\xa7\x70', SUB_TYPE_MP1V),
    (b'\x50\x00\x00\x00' + _MS, SUB_TYPE_MP1A), (b'\x2c\x80\x6d\xe0' + _MPEG, SUB_TYPE_AC3),
    (b'YUY2' + _MS, SUB_TYPE_YUY2), (b'\x01\x00\x00\x00' + _MS, SUB_TYPE_FLAC),
    (b'a4pmzvMI\xb4x\xf2\x9d%\xdc\x907', SUB_TYPE_OGG), (b'M4S2' + _MS, SUB_TYPE_H263),
    (b'VP80' + _MS, SUB_TYPE_VP8), (b'\x0b\xd1/\x8dAXkJ\x89\x05X\x8f\xec\x1a\xde\xd9', SUB_TYPE_OGG),
)
FORMAT_TYPES = (
    (b'-\xab\xd4\xae&s\xcbC\x94d\xc8y\xca\xb9\xc4=', FORMAT_TYPE_MFVIDEOFORMAT),
    (b'\x81\x9fX\x05V\xc3\xce\x11\xbf\x01\x00\xaa\x00UYZ', FORMAT_TYPE_WAVEFORMATEX),
    (b'\xe3\x80m\xe0F\xdb\xcf\x11\xb4\xd1\x00\x80_l\xbb\xea', FORMAT_TYPE_MPEG2VIDEOINFO),
    (b'\xa0v*\xf7\n\xeb\xd0\x11\xac\xe4\x00\x00\xc0\xcc\x16\xba', FORMAT_TYPE_VIDEOINFO2),
    (b'\x82\x9fX\x05V\xc3\xce\x11\xbf\x01\x00\xaa\x00UYZ', FORMAT_TYPE_MPEG1VIDEOINFO),
)

# FreeRDP's FFmpeg decoder (tsmf_ffmpeg_set_format): the sub types it decodes
FFMPEG_CODECS = {SUB_TYPE_WVC1: 'vc1', SUB_TYPE_WMA2: 'wmav2', SUB_TYPE_WMA9: 'wmapro', SUB_TYPE_MP3: 'mp3',
                 SUB_TYPE_MP2A: 'mp2', SUB_TYPE_MP2V: 'mpeg2video', SUB_TYPE_WMV3: 'wmv3', SUB_TYPE_AAC: 'aac',
                 SUB_TYPE_H264: 'h264', SUB_TYPE_AVC1: 'h264', SUB_TYPE_AC3: 'ac3'}

MAX_ACK_TIME = 666667  # 100 ns units (FreeRDP)
AUDIO_TOLERANCE = 10000000  # 100 ns units


def now100ns():
    """FreeRDP get_current_time: wall clock in 100 ns units."""
    return int(time() * 10000000)


def _lookup(table, guid):
    for g, value in table:
        if g == guid:
            return value
    return 0


class MediaType(object):
    """TS_AM_MEDIA_TYPE (FreeRDP tsmf_codec_parse_media_type)."""

    def __init__(self):
        self.majorType = self.subType = self.formatType = 0
        self.width = self.height = self.bitRate = 0
        self.fpsNum, self.fpsDen = 1, 1  # SamplesPerSecond
        self.channels = self.blockAlign = self.bitsPerSample = 0
        self.extraData = b''


def parseMediaType(data, offset=0):
    """@return: (MediaType, next offset); @raise ValueError: unknown / invalid"""
    m = MediaType()

    def need(n):
        if len(data) < offset + n:
            raise ValueError(u'truncated media type')

    need(16)
    m.majorType = _lookup(MAJOR_TYPES, bytes(data[offset:offset + 16]))
    if not m.majorType:
        raise ValueError(u'unknown major type')
    offset += 16
    need(16)
    m.subType = _lookup(SUB_TYPES, bytes(data[offset:offset + 16]))
    if not m.subType:
        raise ValueError(u'unknown sub type')
    offset += 16 + 12  # bFixedSizeSamples, bTemporalCompression, SampleSize
    need(16)
    m.formatType = _lookup(FORMAT_TYPES, bytes(data[offset:offset + 16]))
    if not m.formatType:
        raise ValueError(u'unknown format type')
    offset += 16
    need(4)
    cbFormat = unpack_from('<I', data, offset)[0]
    offset += 4
    need(cbFormat)
    _readFormat(m, bytes(data[offset:offset + cbFormat]), cbFormat)
    if m.fpsNum == 0:
        m.fpsNum = 1
    if m.fpsDen == 0:
        m.fpsDen = 1
    return m, offset + cbFormat


def _videoInfoHeader(m, f, pos, v2):
    """VIDEOINFOHEADER (48 bytes) / VIDEOINFOHEADER2 (72 bytes)."""
    size = 72 if v2 else 48
    if len(f) < pos + size:
        raise ValueError(u'short VIDEOINFOHEADER')
    m.width, m.height = unpack_from('<II', f, pos + 8)  # rcSource right / bottom
    m.bitRate = unpack_from('<I', f, pos + 32)[0]
    avgTimePerFrame = unpack_from('<Q', f, pos + 40)[0]
    m.fpsNum, m.fpsDen = 1000000, avgTimePerFrame // 10
    return size


def _bitmapInfoHeader(m, f, pos, bypass):
    if len(f) < pos + 40:
        raise ValueError(u'short BITMAPINFOHEADER')
    biSize, biWidth, biHeight = unpack_from('<IiI', f, pos)
    if m.width == 0:
        m.width = biWidth
    if m.height == 0:
        m.height = biHeight
    if biSize < 40 or len(f) < pos + biSize:
        raise ValueError(u'invalid BITMAPINFOHEADER')
    return biSize if bypass else 40


def _readFormat(m, f, cbFormat):
    """FreeRDP tsmf_read_format_type."""
    if m.formatType == FORMAT_TYPE_MFVIDEOFORMAT:
        if len(f) < 176:
            raise ValueError(u'short MFVIDEOFORMAT')
        m.width, m.height = unpack_from('<II', f, 8)
        m.fpsNum, m.fpsDen = unpack_from('<II', f, 48)
        m.bitRate = unpack_from('<I', f, 136)[0]
        # the codec data follows; FreeRDP's own check (ExtraDataSize < nsize while ExtraDataSize
        # is still 0) refuses every MFVIDEOFORMAT carrying it, which is a bug: it is kept here
        m.extraData = f[176:cbFormat]
    elif m.formatType == FORMAT_TYPE_WAVEFORMATEX:
        if len(f) < 18:
            raise ValueError(u'short WAVEFORMATEX')
        _tag, m.channels, rate, avgBytes, m.blockAlign, m.bitsPerSample, cbSize = unpack_from('<HHIIHHH', f, 0)
        m.fpsNum, m.fpsDen = rate, 1
        m.bitRate = avgBytes * 8
        if len(f) < 18 + cbSize:
            raise ValueError(u'truncated WAVEFORMATEX extra data')
        m.extraData = f[18:18 + cbSize]
    elif m.formatType in (FORMAT_TYPE_MPEG1VIDEOINFO, FORMAT_TYPE_MPEG2VIDEOINFO, FORMAT_TYPE_VIDEOINFO2):
        v2 = m.formatType != FORMAT_TYPE_MPEG1VIDEOINFO
        i = _videoInfoHeader(m, f, 0, v2)
        i += _bitmapInfoHeader(m, f, i, m.formatType != FORMAT_TYPE_VIDEOINFO2)
        if cbFormat > i:
            m.extraData = f[i:cbFormat]


def avc1ExtraData(extra):
    """
    @summary: FreeRDP tsmf_ffmpeg_init_stream for AVC1 + MPEG2VIDEOINFO: the MPEG2VIDEOINFO tail
    (dwProfile at 8, dwLevel at 12, SPS / PPS with 16-bit lengths from 20) as an avcC record
    """
    if len(extra) < 21:
        raise ValueError(u'AVC1 codec data too short')
    out = bytearray([1, bytearray(extra[8:9])[0], 0, bytearray(extra[12:13])[0], 0xFF, 0xE0 | 0x01])
    s = 20
    size = unpack_from('>H', extra, s)[0]
    if len(extra) < 12 + size + 2:
        raise ValueError(u'AVC1 SPS truncated')
    out += extra[s:s + size + 2]
    s += size + 2
    out.append(1)
    if len(extra) < s + 2:
        raise ValueError(u'AVC1 PPS missing')
    size = unpack_from('>H', extra, s)[0]
    if len(extra) < s + 2 + size:
        raise ValueError(u'AVC1 PPS truncated')
    out += extra[s:s + size + 2]
    return bytes(out)


def checkMediaType(data, offset=0):
    """FreeRDP tsmf_codec_check_media_type with the FFmpeg decoder: parsable and decodable."""
    try:
        m, _ = parseMediaType(data, offset)
    except (ValueError, error):
        return False
    name = FFMPEG_CODECS.get(m.subType)
    if name is None:
        return False
    try:
        from av import codecs_available
        return name in codecs_available
    except:
        return False


class Decoder(object):
    """PyAV in place of FreeRDP's tsmf_ffmpeg decoder."""

    def __init__(self, m):
        from av import CodecContext
        self.m = m
        name = FFMPEG_CODECS.get(m.subType)
        if name is None:
            raise ValueError(u'sub type %d not decodable' % m.subType)
        extra = m.extraData
        if m.subType == SUB_TYPE_AAC and extra:
            if len(extra) < 12:
                raise ValueError(u'AAC codec data too short')
            extra = extra[12:]  # HEAACWAVEINFO: the AudioSpecificConfig after 12 bytes
        if extra and m.subType == SUB_TYPE_AVC1 and m.formatType == FORMAT_TYPE_MPEG2VIDEOINFO:
            extra = avc1ExtraData(extra)
        self.ctx = CodecContext.create(name, 'r')
        if m.majorType == MAJOR_TYPE_VIDEO:
            self.ctx.width, self.ctx.height = m.width, m.height
        else:
            if m.fpsNum:
                self.ctx.sample_rate = m.fpsNum
            if m.channels:
                try:
                    self.ctx.layout = 'stereo' if m.channels == 2 else ('mono' if m.channels == 1 else m.channels)
                except Exception:
                    pass
            if m.blockAlign:
                try:
                    self.ctx.block_align = m.blockAlign
                except AttributeError:
                    pass
        if m.bitRate:
            self.ctx.bit_rate = m.bitRate
        if extra:
            self.ctx.extradata = bytes(extra)
        self._resampler = None

    def decode(self, data):
        """@return: decoded frames (PyAV frames)"""
        from av import Packet
        return self.ctx.decode(Packet(bytes(data)))

    def toPcm(self, frame):
        """s16 interleaved at the stream rate / channels."""
        from av import AudioResampler
        if self._resampler is None:
            self._resampler = AudioResampler(format='s16', layout=frame.layout.name, rate=frame.sample_rate)
        return b''.join(bytes(f.to_ndarray().tobytes()) for f in self._resampler.resample(frame))


class Sample(object):
    __slots__ = ('sampleId', 'startTime', 'endTime', 'duration', 'extensions', 'data', 'channel', 'streamId',
                 'ackTime', 'invalidTimestamps', 'dataSize')


class Stream(object):
    """FreeRDP TSMF_STREAM: sample queue, playback thread, ack thread."""

    def __init__(self, presentation, streamId):
        self.presentation = presentation
        self.streamId = streamId
        self.mediaType = None
        self.decoder = None
        self.samples = queue.Queue()
        self.acks = []
        self._ackLock = Lock()
        self.stopEvent = Event()
        self.eos = False
        self.eosMessageId = 0
        self.eosChannel = None
        self.lastStartTime = self.lastEndTime = 0
        self.nextStartTime = 0
        self.width = self.height = 0
        self.audio = None
        self.paused = False
        self.played = 0
        self._threads = []

    @property
    def majorType(self):
        return self.mediaType.majorType if self.mediaType else 0

    def setFormat(self, data, offset):
        """FreeRDP tsmf_stream_set_format."""
        self.mediaType, _ = parseMediaType(data, offset)
        self.width, self.height = self.mediaType.width, self.mediaType.height
        self.decoder = Decoder(self.mediaType)

    def start(self):
        for target, name in ((self._playbackLoop, 'tsmf-playback'), (self._ackLoop, 'tsmf-ack')):
            t = Thread(target=target, name=name)
            t.daemon = True
            t.start()
            self._threads.append(t)

    def push(self, channel, messageId, start, end, duration, extensions, data):
        s = Sample()
        s.sampleId, s.startTime, s.endTime, s.duration, s.extensions = messageId, start, end, duration, extensions
        s.data, s.dataSize, s.channel, s.streamId = data, len(data), channel, channel.streamId
        s.ackTime = 0
        s.invalidTimestamps = bool(extensions & TSMM_SAMPLE_EXT_HAS_NO_TIMESTAMPS)
        self.samples.put(s)

    def flush(self):
        """FreeRDP tsmf_stream_flush: drop queued samples (their acks are still sent)."""
        while True:
            try:
                s = self.samples.get_nowait()
            except queue.Empty:
                break
            if s is not None:
                s.ackTime = 0
                self._queueAck(s)
        self.lastStartTime = self.lastEndTime = 0
        self.nextStartTime = 0
        if self.audio is not None:
            self.audio.flush()

    def end(self, messageId, channel):
        """FreeRDP tsmf_stream_end: EOS notification once every ack is sent."""
        self.eosMessageId = messageId
        self.eosChannel = channel
        self.eos = True

    def stop(self):
        self.stopEvent.set()
        self.samples.put(None)
        if self.audio is not None:
            self.audio.close()
            self.audio = None

    # ---- playback (FreeRDP tsmf_stream_playback_func / tsmf_sample_playback) ----

    def _playbackLoop(self):
        while not self.stopEvent.is_set():
            if self.paused:
                sleep(0.01)
                continue
            try:
                sample = self.samples.get(timeout=1.0)
            except queue.Empty:
                continue
            if sample is None:
                break
            if not sample.invalidTimestamps:
                self.lastStartTime = max(self.lastStartTime, sample.startTime)
                self.lastEndTime = max(self.lastEndTime, sample.endTime)
            try:
                self._playback(sample)
            except Exception as e:
                log.debug(u'tsmf: sample %d not played: %r' % (sample.sampleId, e))
                sample.ackTime = 0
                self._queueAck(sample)

    def _playback(self, sample):
        frames = self.decoder.decode(sample.data) if self.decoder is not None else []
        sample.data = None
        if not frames:
            # nothing decoded yet (FreeRDP: no decoded data): ack ahead of time
            sample.ackTime = now100ns() + min(sample.duration // 2, MAX_ACK_TIME)
            self._queueAck(sample)
            return
        if self.majorType == MAJOR_TYPE_VIDEO:
            for frame in frames:
                self._playVideo(sample, frame)
        else:
            latency = 0
            for frame in frames:
                latency = self._playAudio(frame)
            sample.ackTime = now100ns() + latency
            if not sample.invalidTimestamps:
                self.lastStartTime = sample.startTime + latency
                self.lastEndTime = sample.endTime + latency
                self.presentation.audioStartTime = sample.startTime + latency
                self.presentation.audioEndTime = sample.endTime + latency
        self.played += 1
        self._queueAck(sample)

    def _playVideo(self, sample, frame):
        """FreeRDP tsmf_sample_playback_video: paced by the sample duration, drawn at the geometry."""
        t = now100ns()
        p = self.presentation
        if self.nextStartTime > t and (sample.startTime >= p.audioStartTime or
                                       (sample.startTime < self.lastStartTime and not sample.invalidTimestamps)):
            sleep((self.nextStartTime - t) / 1e7)
        self.width, self.height = frame.width, frame.height
        self.nextStartTime = now100ns() + sample.duration - 50000
        if p.frameCallback is not None and p.rect is not None:
            image = frame.to_ndarray(format='bgra')
            x, y, w, h = p.rect
            p.frameCallback(x, y, w, h, image, list(p.visibleRects))

    def _playAudio(self, frame):
        """@return: latency in 100 ns units (FreeRDP audio->GetLatency)"""
        pcm = self.decoder.toPcm(frame)
        if self.audio is None:
            self.audio = self.presentation.audioFactory(frame.sample_rate, len(frame.layout.channels))
        if self.audio is None:
            return 0
        self.audio.setVolume(self.presentation.volume, self.presentation.muted)
        self.audio.play(pcm)
        return int(self.audio.latency() * 1e7)

    # ---- acks (FreeRDP tsmf_stream_ack_func / tsmf_stream_process_ack) ----

    def _queueAck(self, sample):
        with self._ackLock:
            self.acks.append(sample)

    def _processAck(self, force):
        """@return: True when no ack is left"""
        with self._ackLock:
            if not self.acks:
                return True
            sample = self.acks[0]
            if not force and sample.ackTime > now100ns():
                return False
            self.acks.pop(0)
        sample.channel.playbackAck(sample.sampleId, sample.duration, sample.dataSize, sample.streamId)
        return False

    def _ackLoop(self):
        while True:
            if self.stopEvent.is_set():
                while not self._processAck(True):
                    pass
                break
            if self.eos and self.samples.empty():
                while not self._processAck(True):
                    pass
                if self.eosChannel is not None:
                    self.eosChannel.sendEndOfStream(self.eosMessageId)
                self.eos = False
            if self._processAck(False):
                sleep(0.005)


class Presentation(object):
    """FreeRDP TSMF_PRESENTATION."""

    def __init__(self, presentationId, plugin):
        self.presentationId = presentationId
        self.streams = {}
        self.rect = None  # (x, y, width, height) of the video on the desktop
        self.visibleRects = []  # [(x, y, width, height)] relative to rect
        self.audioStartTime = self.audioEndTime = 0
        self.volume, self.muted = 0xFFFF, False
        self.frameCallback = plugin.frameCallback
        self.audioFactory = plugin.audioFactory

    def setGeometry(self, x, y, width, height, rects):
        self.rect = (x, y, width, height)
        self.visibleRects = rects

    def pause(self, paused):
        for s in list(self.streams.values()):
            s.paused = paused
            if s.audio is not None:
                (s.audio.pause if paused else s.audio.resume)()

    def stop(self):
        for s in list(self.streams.values()):
            s.flush()

    def free(self):
        for s in list(self.streams.values()):
            s.stop()
        self.streams.clear()


class Tsmf(object):
    """
    @summary: The TSMF plugin. frameCallback(x, y, width, height, bgra, visibleRects) draws a video
    frame (any thread); audioFactory(rate, channels) -> player with play(pcm s16), latency() seconds,
    setVolume(volume, muted), pause(), resume(), flush(), close() (None: no audio).
    """

    def __init__(self):
        self.enabled = False  # FreeRDP builds it off by default (/multimedia, /tsmf)
        self.presentations = {}
        self.frameCallback = None
        self.audioFactory = lambda rate, channels: None
        self.lock = Lock()

    def channelFactory(self, channel):
        if not self.enabled:
            return None
        return TsmfChannel(self, channel)

    def find(self, presentationId):
        with self.lock:
            return self.presentations.get(bytes(presentationId))

    def describe(self):
        with self.lock:
            streams = sum(len(p.streams) for p in self.presentations.values())
        return ('%d stream(s) playing' % streams) if streams else None

    def shutdown(self):
        with self.lock:
            presentations, self.presentations = list(self.presentations.values()), {}
        for p in presentations:
            p.free()


class TsmfChannel(object):
    """One TSMF channel (FreeRDP TSMF_CHANNEL_CALLBACK + tsmf_on_data_received)."""

    def __init__(self, plugin, channel):
        self.plugin = plugin
        self.channel = channel
        self.presentationId = b'\x00' * 16
        self.streamId = 0

    # ---- client -> server ----

    def playbackAck(self, messageId, duration, dataSize, streamId=None):
        """FreeRDP tsmf_playback_ack."""
        self.channel.write(pack('<IIIIQQ', TSMF_INTERFACE_CLIENT_NOTIFICATIONS | STREAM_ID_PROXY, messageId,
                                       PLAYBACK_ACK, self.streamId if streamId is None else streamId,
                                       duration, dataSize))

    def sendEndOfStream(self, messageId):
        """FreeRDP tsmf_send_eos_response."""
        if self.streamId:
            self.channel.write(pack('<IIIIII', TSMF_INTERFACE_CLIENT_NOTIFICATIONS | STREAM_ID_PROXY,
                                           messageId, CLIENT_EVENT_NOTIFICATION, self.streamId,
                                           TSMM_CLIENT_EVENT_ENDOFSTREAM, 0))

    # ---- server -> client ----

    def recv(self, data):
        data = bytes(data)
        if len(data) < 12:
            return
        interfaceId, messageId, functionId = unpack_from('<III', data, 0)
        body = data[12:]
        self._out = b''
        self._pending = False
        self._outInterface = interfaceId
        processed = False
        try:
            if interfaceId == TSMF_INTERFACE_CAPABILITIES | STREAM_ID_NONE:
                if functionId == RIM_EXCHANGE_CAPABILITY_REQUEST:
                    self._rimExchangeCapability(body)
                    processed = True
            elif interfaceId == TSMF_INTERFACE_DEFAULT | STREAM_ID_PROXY:
                handler = self._HANDLERS.get(functionId)
                if handler is not None:
                    handler(self, body, messageId)
                    processed = True
        except (ValueError, error, IndexError) as e:
            log.warning(u'tsmf: function 0x%x: %s' % (functionId, e))
        if not processed:
            if functionId == RIMCALL_RELEASE:
                self._pending = True  # no reply (MS-RDPEXPS IFACE_RELEASE)
            # QueryInterface and unknown requests: empty reply (FreeRDP)
            processed = True
        if processed and not self._pending:
            self.channel.write(pack('<II', self._outInterface, messageId) + self._out)

    def _presentation(self, body):
        return self.plugin.find(body[:GUID_SIZE])

    def _rimExchangeCapability(self, body):
        self._out = pack('<II', 1, 0)  # CapabilityValue, Result

    def _exchangeCapabilities(self, body, mid):
        """FreeRDP: the host capabilities echoed, the platform one answered MF | DShow."""
        out = bytearray(body)
        num = unpack_from('<I', out, 0)[0]
        pos = 4
        for _ in range(num):
            capType, capLen = unpack_from('<II', out, pos)
            pos += 8
            if len(out) < pos + capLen:
                raise ValueError(u'truncated capability')
            if capType == 2 and capLen >= 4:
                pack_into('<I', out, pos, MMREDIR_CAPABILITY_PLATFORM_MF | MMREDIR_CAPABILITY_PLATFORM_DSHOW)
            pos += capLen
        self._out = bytes(out[:pos]) + pack('<I', 0)
        self._outInterface = TSMF_INTERFACE_DEFAULT | STREAM_ID_STUB

    def _setChannelParams(self, body, mid):
        self.presentationId = body[:GUID_SIZE]
        self.streamId = unpack_from('<I', body, GUID_SIZE)[0]
        self._pending = True

    def _checkFormatSupport(self, body, mid):
        cookie, _rollover, _num = unpack_from('<III', body, 0)
        supported = 1 if checkMediaType(body, 12) else 0
        self._out = pack('<III', supported, cookie, 0)
        self._outInterface = TSMF_INTERFACE_DEFAULT | STREAM_ID_STUB

    def _newPresentation(self, body, mid):
        pid = bytes(body[:GUID_SIZE])
        if len(pid) < GUID_SIZE:
            raise ValueError(u'short presentation id')
        with self.plugin.lock:
            exists = pid in self.plugin.presentations
            if not exists:
                self.plugin.presentations[pid] = Presentation(pid, self.plugin)
                log.info(u'tsmf: new presentation')
        # FreeRDP: a known presentation gets an empty reply, a new one none
        self._pending = not exists

    def _addStream(self, body, mid):
        p = self._presentation(body)
        self._pending = True
        if p is None:
            return
        streamId = unpack_from('<I', body, GUID_SIZE)[0]
        stream = Stream(p, streamId)
        stream.setFormat(body, GUID_SIZE + 8)
        p.streams[streamId] = stream
        stream.start()
        m = stream.mediaType
        log.info(u'tsmf: stream %d: %s %s' % (streamId, 'video' if m.majorType == MAJOR_TYPE_VIDEO else 'audio',
                                             FFMPEG_CODECS.get(m.subType)))

    def _setTopology(self, body, mid):
        self._out = pack('<II', 1, 0)  # TopologyReady, Result
        self._outInterface = TSMF_INTERFACE_DEFAULT | STREAM_ID_STUB

    def _removeStream(self, body, mid):
        p = self._presentation(body)
        self._pending = True
        if p is not None:
            stream = p.streams.pop(unpack_from('<I', body, GUID_SIZE)[0], None)
            if stream is not None:
                stream.stop()

    def _setSourceVideoRect(self, body, mid):
        self._pending = True

    def _shutdownPresentation(self, body, mid):
        with self.plugin.lock:
            p = self.plugin.presentations.pop(bytes(body[:GUID_SIZE]), None)
        if p is None:
            raise ValueError(u'presentation not found')
        p.free()
        self._out = pack('<I', 0)
        self._outInterface = TSMF_INTERFACE_DEFAULT | STREAM_ID_STUB

    def _streamVolume(self, body, mid):
        p = self._presentation(body)
        if p is None:
            raise ValueError(u'presentation not found')
        p.volume, muted = unpack_from('<II', body, GUID_SIZE)
        p.muted = bool(muted)
        self._pending = True

    def _channelVolume(self, body, mid):
        self._pending = True

    def _setVideoWindow(self, body, mid):
        self._pending = True

    def _updateGeometry(self, body, mid):
        """FreeRDP tsmf_ifman_update_geometry_info."""
        p = self._presentation(body)
        if p is None:
            raise ValueError(u'presentation not found')
        numGeometryInfo = unpack_from('<I', body, GUID_SIZE)[0]
        pos = GUID_SIZE + 4
        width, height, left, top = unpack_from('<IIII', body, pos + 12)
        pos += numGeometryInfo
        cbVisibleRect = unpack_from('<I', body, pos)[0]
        pos += 4
        rects = []
        for i in range(cbVisibleRect // 16):
            t, l, b, r = unpack_from('<IIII', body, pos + 16 * i)
            rects.append((l, t, r - l, b - t))
        p.setGeometry(left, top, width, height, rects)
        self._pending = True

    def _setAllocator(self, body, mid):
        self._pending = True

    def _notifyPreroll(self, body, mid):
        self._playbackPaused(body, mid)
        self._pending = True

    def _onSample(self, body, mid):
        streamId = unpack_from('<I', body, GUID_SIZE)[0]
        start, end, throttle = unpack_from('<QQQ', body, GUID_SIZE + 8)
        _flags, extensions, cbData = unpack_from('<III', body, GUID_SIZE + 32)
        data = body[GUID_SIZE + 44:GUID_SIZE + 44 + cbData]
        p = self.plugin.find(self.presentationId)
        if p is None:
            raise ValueError(u'presentation not found')
        stream = p.streams.get(streamId)
        if stream is None:
            raise ValueError(u'stream %d not found' % streamId)
        stream.push(self, mid, start, end, throttle, extensions, data)
        self._pending = True

    def _onFlush(self, body, mid):
        p = self.plugin.find(self.presentationId)
        if p is None:
            raise ValueError(u'presentation not found')
        stream = p.streams.get(unpack_from('<I', body, GUID_SIZE)[0])
        if stream is not None:
            stream.flush()
        self._pending = True

    def _endOfStream(self, body, mid):
        p = self._presentation(body)
        if p is not None:
            stream = p.streams.get(unpack_from('<I', body, GUID_SIZE)[0])
            if stream is not None:
                stream.end(mid, self)
        self._pending = True
        self._outInterface = TSMF_INTERFACE_CLIENT_NOTIFICATIONS | STREAM_ID_PROXY

    def _playbackStarted(self, body, mid):
        p = self._presentation(body)
        if p is not None:
            p.pause(False)
        self._out = pack('<IIII', CLIENT_EVENT_NOTIFICATION, 0, TSMM_CLIENT_EVENT_START_COMPLETED, 0)
        self._outInterface = TSMF_INTERFACE_CLIENT_NOTIFICATIONS | STREAM_ID_PROXY

    def _playbackPaused(self, body, mid):
        self._pending = True
        p = self._presentation(body)
        if p is not None:
            p.pause(True)

    def _playbackRestarted(self, body, mid):
        self._pending = True
        p = self._presentation(body)
        if p is not None:
            p.pause(False)

    def _playbackStopped(self, body, mid):
        p = self._presentation(body)
        if p is not None:
            p.stop()
        self._out = pack('<IIII', CLIENT_EVENT_NOTIFICATION, 0, TSMM_CLIENT_EVENT_STOP_COMPLETED, 0)
        self._outInterface = TSMF_INTERFACE_CLIENT_NOTIFICATIONS | STREAM_ID_PROXY

    def _rateChanged(self, body, mid):
        self._out = pack('<IIII', CLIENT_EVENT_NOTIFICATION, 0, TSMM_CLIENT_EVENT_MONITORCHANGED, 0)
        self._outInterface = TSMF_INTERFACE_CLIENT_NOTIFICATIONS | STREAM_ID_PROXY

    _HANDLERS = {
        SET_CHANNEL_PARAMS: _setChannelParams, EXCHANGE_CAPABILITIES_REQ: _exchangeCapabilities,
        CHECK_FORMAT_SUPPORT_REQ: _checkFormatSupport, ON_NEW_PRESENTATION: _newPresentation,
        ADD_STREAM: _addStream, SET_TOPOLOGY_REQ: _setTopology, REMOVE_STREAM: _removeStream,
        SET_SOURCE_VIDEO_RECT: _setSourceVideoRect, SHUTDOWN_PRESENTATION_REQ: _shutdownPresentation,
        ON_STREAM_VOLUME: _streamVolume, ON_CHANNEL_VOLUME: _channelVolume, SET_VIDEO_WINDOW: _setVideoWindow,
        UPDATE_GEOMETRY_INFO: _updateGeometry, SET_ALLOCATOR: _setAllocator, NOTIFY_PREROLL: _notifyPreroll,
        ON_SAMPLE: _onSample, ON_FLUSH: _onFlush, ON_END_OF_STREAM: _endOfStream,
        ON_PLAYBACK_STARTED: _playbackStarted, ON_PLAYBACK_PAUSED: _playbackPaused,
        ON_PLAYBACK_RESTARTED: _playbackRestarted, ON_PLAYBACK_STOPPED: _playbackStopped,
        ON_PLAYBACK_RATE_CHANGED: _rateChanged,
    }

    def closed(self):
        """FreeRDP tsmf_on_close: the stream of this channel goes away."""
        if self.streamId:
            p = self.plugin.find(self.presentationId)
            if p is not None:
                stream = p.streams.pop(self.streamId, None)
                if stream is not None:
                    stream.stop()


class AudioOutPlayer(object):
    """
    @summary: TSMF audio through rdpy's Qt audio output (the one rdpsnd uses): s16 PCM written
    to its ring buffer; latency = buffered audio + device latency (FreeRDP GetLatency)
    """

    def __init__(self, rate, channels, invoke):
        from rdpy.protocol.rdp import audio_out
        self._ring = None
        self._converter = None
        self._deviceLatency = 0.0
        self._pending = []
        self._lock = Lock()
        self._volume, self._muted = 0xFFFF, False
        self._paused = False
        self._token = None
        src = audio_out.PcmFormat(rate, channels, 's16')
        self.bytesPerSecond = rate * channels * 2

        def open_():
            output = audio_out.AudioOutput.instance()
            if output:
                self._token = output.start(src, self._ready)

        invoke(open_)

    def _ready(self, token, ring, converter, deviceLatency):
        with self._lock:
            self._ring, self._converter, self._deviceLatency = ring, converter, deviceLatency
            pending, self._pending = self._pending, []
        for pcm in pending:
            self.play(pcm)

    def setVolume(self, volume, muted):
        self._volume, self._muted = volume, muted

    def play(self, pcm):
        if self._muted or self._volume < 0xFFFF:
            from numpy import frombuffer, int32
            a = frombuffer(pcm, '<i2').astype(int32)
            pcm = (a * (0 if self._muted else self._volume) // 0xFFFF).astype('<i2').tobytes()
        with self._lock:
            ring, converter = self._ring, self._converter
            if ring is None:
                self._pending.append(pcm)
                return
        ring.write(converter.convert(pcm))

    def latency(self):
        with self._lock:
            ring = self._ring
            if ring is None:
                return sum(len(p) for p in self._pending) / float(max(1, self.bytesPerSecond))
        return ring.levelSeconds() + self._deviceLatency

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def flush(self):
        with self._lock:
            self._pending = []
            if self._ring is not None:
                self._ring.clear()

    def close(self):
        from rdpy.protocol.rdp import audio_out
        output = audio_out.AudioOutput.instance()
        if output and self._token:
            output.stop(self._token)
