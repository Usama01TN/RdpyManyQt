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
AUDIO_INPUT - Audio Input Redirection Virtual Channel (MS-RDPEAI): the local
microphone is sent to the server, which uses it as its own microphone.

Ported from FreeRDP channels/audin/client/audin_main.c. The server opens the
"AUDIO_INPUT" dynamic channel (when the client sets INFO_AUDIOCAPTURE),
offers formats, then opens the device with one of them; the client answers
and streams Incoming Data + Data PDUs of FramesPerPacket frames.

Only PCM formats are accepted (no audio encoder is involved); the capture
itself runs in audio_out.AudioInput on the Qt audio thread.
"""
from __future__ import absolute_import, division, print_function
import struct
import rdpy.core.log as log
from rdpy.protocol.rdp import audio_out
from rdpy.protocol.rdp.rdpsnd import AudioFormat, WAVE_FORMAT_PCM

CHANNEL_NAME = "AUDIO_INPUT"
MSG_SNDIN_VERSION = 0x01
MSG_SNDIN_FORMATS = 0x02
MSG_SNDIN_OPEN = 0x03
MSG_SNDIN_OPEN_REPLY = 0x04
MSG_SNDIN_DATA_INCOMING = 0x05
MSG_SNDIN_DATA = 0x06
MSG_SNDIN_FORMATCHANGE = 0x07
SNDIN_VERSION = 0x02


class AudinChannel(object):
    """
    @summary: Client side of one AUDIO_INPUT dynamic channel
    """

    def __init__(self, send, capture=None, callFromThread=None):
        """
        @param send: callable(bytes) sending a message on the dynamic channel
        @param capture: object with start(PcmFormat, onData) -> token and stop(token)
                        (audio_out.AudioInput by default)
        @param callFromThread: callable(fn, *args) running fn on the network thread
        """
        self._send = send
        self._capture = capture
        self._callFromThread = callFromThread
        self._formats = []  # formats accepted (index = format number of the server)
        self._format = None  # AudioFormat in use
        self._framesPerPacket = 0
        self._token = 0
        self._pending = bytearray()
        self._converter = None

    # --- server messages ---

    def recv(self, data):
        if not data:
            return
        msgId = data[0]
        body = data[1:]
        try:
            if msgId == MSG_SNDIN_VERSION:
                self._onVersion(body)
            elif msgId == MSG_SNDIN_FORMATS:
                self._onFormats(body)
            elif msgId == MSG_SNDIN_OPEN:
                self._onOpen(body)
            elif msgId == MSG_SNDIN_FORMATCHANGE:
                self._onFormatChange(body)
            else:
                log.debug("AUDIN: unknown message 0x%02x" % msgId)
        except struct.error as e:
            log.warning("AUDIN: malformed message 0x%02x: %s" % (msgId, e))

    def _onVersion(self, body):
        serverVersion = struct.unpack_from('<I', body, 0)[0]
        if serverVersion > SNDIN_VERSION:
            log.warning("AUDIN: unsupported server version %d" % serverVersion)
            return  # FreeRDP does not answer either
        self._send(struct.pack('<BI', MSG_SNDIN_VERSION, SNDIN_VERSION))

    def _onFormats(self, body):
        count = struct.unpack_from('<I', body, 0)[0]
        if count < 1 or count > 1000:
            raise struct.error("bad format count %d" % count)
        self.close()
        offset = 8  # NumFormats + cbSizeFormatsPacket
        self._formats = []
        for _ in range(count):
            fmt, offset = AudioFormat.unpack(body, offset)
            if fmt is None:
                break
            if (fmt.tag == WAVE_FORMAT_PCM and fmt.bits_per_sample in (8, 16) and
                    fmt.channels in (1, 2) and 8000 <= fmt.samples_per_sec <= 96000):
                self._formats.append(fmt)
        packed = b''.join(f.pack() for f in self._formats)
        self._send(struct.pack('<B', MSG_SNDIN_DATA_INCOMING))
        self._send(struct.pack('<BII', MSG_SNDIN_FORMATS, len(self._formats), 9 + len(packed)) + packed)
        log.debug("AUDIN: %d of %d formats accepted" % (len(self._formats), count))
        if not self._formats:
            log.warning("AUDIN: the server offered no PCM format, microphone unavailable")

    def _onOpen(self, body):
        framesPerPacket, initialFormat = struct.unpack_from('<II', body, 0)
        if initialFormat >= len(self._formats):
            raise struct.error("bad format index %d" % initialFormat)
        self._framesPerPacket = min(max(framesPerPacket, 1), 1 << 20)
        self._openDevice(self._formats[initialFormat])
        self._send(struct.pack('<BI', MSG_SNDIN_FORMATCHANGE, initialFormat))
        self._send(struct.pack('<BI', MSG_SNDIN_OPEN_REPLY, 0))

    def _onFormatChange(self, body):
        newFormat = struct.unpack_from('<I', body, 0)[0]
        if newFormat >= len(self._formats):
            raise struct.error("bad format index %d" % newFormat)
        fmt = self._formats[newFormat]
        if self._format is None or fmt.pack() != self._format.pack():
            self._openDevice(fmt)
        self._send(struct.pack('<BI', MSG_SNDIN_FORMATCHANGE, newFormat))

    # --- capture ---

    def _openDevice(self, fmt):
        self.close()
        self._format = fmt
        self._pending = bytearray()
        pcm = audio_out.PcmFormat(fmt.samples_per_sec, fmt.channels, 'u8' if fmt.bits_per_sample == 8 else 's16')
        capture = self._capture or audio_out.AudioInput.instance()
        if capture is None:
            log.warning("AUDIN: no audio input available")
            return
        self._capture = capture
        self._token = capture.start(pcm, self._onCaptured)
        log.info("microphone: sending %d Hz, %d channel(s), %d-bit to the server" %
                 (fmt.samples_per_sec, fmt.channels, fmt.bits_per_sample))

    def _onCaptured(self, token, data):
        """Called from the audio thread with PCM already in the server format."""
        if self._callFromThread is not None:
            self._callFromThread(self._sendAudio, token, data)
        else:
            self._sendAudio(token, data)

    def _sendAudio(self, token, data):
        if token != self._token or self._format is None:
            return
        self._pending.extend(data)
        frameBytes = max(1, self._format.channels * self._format.bits_per_sample // 8)
        packet = max(1, self._framesPerPacket) * frameBytes
        while len(self._pending) >= packet:
            chunk = bytes(self._pending[:packet])
            del self._pending[:packet]
            self._send(struct.pack('<B', MSG_SNDIN_DATA_INCOMING))
            self._send(struct.pack('<B', MSG_SNDIN_DATA) + chunk)

    def close(self):
        """Stop the capture (channel closed, new formats, session end)."""
        if self._token and self._capture is not None:
            self._capture.stop(self._token)
        self._token = 0
        self._format = None
        self._pending = bytearray()
