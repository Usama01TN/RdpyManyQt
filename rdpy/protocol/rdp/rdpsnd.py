# coding=utf-8
#
# Copyright (c) 2026 Hajime Nakagami
# Modified (c) 2026 UsamaTN <ininou.oussematn@gmail.com> - RdpyManyQt, see CHANGELOG.md
#
# This file is part of rdpy.
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
@summary: RDPSND - Audio Output Virtual Channel Extension (MS-RDPEA).
Handles the "rdpsnd" static virtual channel for server-to-client audio
redirection. Negotiates audio formats with the server and plays back
PCM audio through Qt (QAudioSink with Qt6, QAudioOutput with Qt5) from a
dedicated audio thread, see audio_out.py.

Protocol reference: [MS-RDPEA]
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpea/
"""
from __future__ import absolute_import, division, print_function

import struct
import time
from rdpy.core.layer import LayerAutomata
import rdpy.core.log as log
from rdpy.protocol.rdp import audio_out

# Virtual Channel PDU flags (same as drdynvc.py)
CHANNEL_FLAG_FIRST = 0x00000001
CHANNEL_FLAG_LAST = 0x00000002
# RDPSND PDU types (MS-RDPEA 2.2)
SNDC_CLOSE = 0x01
SNDC_WAVE = 0x02
SNDC_SETVOLUME = 0x03
SNDC_SETPITCH = 0x04
SNDC_WAVECONFIRM = 0x05
SNDC_TRAINING = 0x06
SNDC_FORMATS = 0x07
SNDC_CRYPTKEY = 0x08
SNDC_WAVEENCRYPT = 0x09
SNDC_UDPWAVE = 0x0A
SNDC_UDPWAVELAST = 0x0B
SNDC_QUALITYMODE = 0x0C
SNDC_WAVE2 = 0x0D
# RDPSND version
# gnome-remote-desktop (grd-rdp-dvc-audio-playback.c) requires clientVersion >= 8
# (CHANNEL_VERSION_WIN_8). FreeRDP defines WIN_7=6, WIN_8=8, WIN_MAX=8.
RDPSND_VERSION_MAJOR = 0x08
RDPSND_VERSION_MINOR = 0x00
# Audio format tags (subset of WAVE format tags)
WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_ADPCM = 0x0002
WAVE_FORMAT_ALAW = 0x0006
WAVE_FORMAT_MULAW = 0x0007
WAVE_FORMAT_OPUS = 0x704F
# RDPSND flags
TSSNDCAPS_ALIVE = 0x00000001
TSSNDCAPS_VOLUME = 0x00000002
TSSNDCAPS_PITCH = 0x00000004
# Quality modes
HIGH_QUALITY = 0x0001
MEDIUM_QUALITY = 0x0002
DYNAMIC_QUALITY = 0x0000


class AudioFormat(object):
    """
    Represents a WAVE audio format (WAVEFORMATEX).
    """

    def __init__(self, tag=0, channels=0, samples_per_sec=0,
                 avg_bytes_per_sec=0, block_align=0, bits_per_sample=0,
                 extra_data=b''):
        self.tag = tag
        self.channels = channels
        self.samples_per_sec = samples_per_sec
        self.avg_bytes_per_sec = avg_bytes_per_sec
        self.block_align = block_align
        self.bits_per_sample = bits_per_sample
        self.extra_data = extra_data

    def pack(self):
        """Pack into WAVEFORMATEX binary + cbSize prefix."""
        hdr = struct.pack('<HHIIH',
                          self.tag,
                          self.channels,
                          self.samples_per_sec,
                          self.avg_bytes_per_sec,
                          self.block_align)
        hdr += struct.pack('<H', self.bits_per_sample)
        hdr += struct.pack('<H', len(self.extra_data))
        hdr += self.extra_data
        return hdr

    @staticmethod
    def unpack(data, offset):
        """Unpack WAVEFORMATEX from data at offset. Returns (AudioFormat, new_offset)."""
        if len(data) - offset < 18:
            return None, offset
        tag, channels, sps, abps, ba = struct.unpack_from('<HHIIH', data, offset)
        bps = struct.unpack_from('<H', data, offset + 14)[0]
        cb_size = struct.unpack_from('<H', data, offset + 16)[0]
        extra = data[offset + 18:offset + 18 + cb_size]
        fmt = AudioFormat(tag, channels, sps, abps, ba, bps, extra)
        return fmt, offset + 18 + cb_size

    def is_pcm(self):
        return self.tag == WAVE_FORMAT_PCM

    def __repr__(self):
        names = {WAVE_FORMAT_PCM: "PCM", WAVE_FORMAT_ADPCM: "ADPCM",
                 WAVE_FORMAT_ALAW: "A-Law", WAVE_FORMAT_MULAW: "μ-Law"}
        name = names.get(self.tag, "0x%04x" % self.tag)
        return "AudioFormat(%s %dHz %dch %dbit)" % (
            name, self.samples_per_sec, self.channels, self.bits_per_sample)


class RdpsndLayer(LayerAutomata):
    """
    RDPSND static virtual channel layer.

    Handles the MS-RDPEA protocol: format negotiation, training, and
    wave data reception. PCM audio is played back via QAudioSink.
    """

    def __init__(self):
        LayerAutomata.__init__(self, None)
        # VChannel reassembly
        self._vchanBuf = b''
        # Server and client formats
        self._serverFormats = []
        self._clientFormatIndices = []  # indices into _serverFormats that we support
        self._activeFormatIndex = -1
        # Training
        self._trainingTimestamp = 0
        # Wave state
        self._waveTimestamp = 0
        self._pendingWaveData = b''
        self._expectingWaveData = False
        # Audio playback (see audio_out.AudioOutput)
        self._audioInitialized = False
        self._audioSrcFmt = None  # audio_out.PcmFormat of the negotiated server format
        self._audioToken = 0  # stream id of the active output (0 = none requested)
        self._audioRing = None  # audio_out._PcmRing, set once the device is open
        self._audioConverter = None  # audio_out.PcmConverter server -> device format
        self._audioDeviceLatency = 0.0  # seconds buffered inside the device
        self._audioPendingBuf = bytearray()  # PCM received while the device opens
        self._audioDisabled = False
        # Debug WAV dump (set to a path to write raw PCM to a WAV file)
        self._wavDumpFile = None
        self._wavDumpFmt = None
        self._wavDumpPath = None
        self._audioGain = 1.0
        self._lastPlayTime = 0.0
        # Auto-enable WAV dump from environment variable
        import os
        wav_path = os.environ.get('RDPSND_WAV_DUMP')
        if wav_path:
            self._wavDumpPath = wav_path
        # DVC send callback (set when audio arrives via DVC)
        self._dvcSendCallback = None
        # Qt main-thread invoker: set by RDPClientQt via setQtInvoker().
        # Default calls fn() directly (safe when qreactor is used, or in tests).
        self._qt_invoke = lambda fn: fn()

    def setQtInvoker(self, invoke_fn):
        """Set the function used to post callables to the Qt main thread.

        Must be called before any audio data arrives.  When the Twisted
        reactor runs on a background thread (not via qreactor), pass a
        thread-safe dispatcher here so that QAudioSink operations are
        always executed on the Qt main thread.

        @param invoke_fn: callable(fn) — schedules fn() on the Qt main thread.
        """
        self._qt_invoke = invoke_fn

    def connect(self):
        log.debug("RdpsndLayer.connect()")

    def recv(self, s):
        """Receive data on the rdpsnd static virtual channel with VChannel reassembly."""
        data = s.read()
        if len(data) < 8:
            log.warning("RDPSND: recv data too short (%d bytes), skipping" % len(data))
            return
        totalLen = struct.unpack_from('<I', data, 0)[0]
        flags = struct.unpack_from('<I', data, 4)[0]
        payload = data[8:]

        if flags & CHANNEL_FLAG_FIRST:
            self._vchanBuf = payload
        else:
            self._vchanBuf += payload

        if flags & CHANNEL_FLAG_LAST:
            if len(self._vchanBuf) >= 1:
                self._processStaticData(bytes(self._vchanBuf))
            self._vchanBuf = b''

    def _processStaticData(self, data):
        """Process data from the static rdpsnd virtual channel.

        When a DVC audio channel (AUDIO_PLAYBACK_DVC) is active, the server
        sends audio on both the DVC channel and the static rdpsnd channel for
        backward compatibility.  Playing both causes audio to play twice, which
        sounds like the video is repeating.  When the DVC channel is active
        (indicated by _dvcSendCallback being set), skip audio playback messages
        on the static channel so only the DVC path produces sound.

        Capability-negotiation messages (FORMATS, TRAINING, QUALITYMODE) are
        always processed regardless of DVC state.
        """
        if self._dvcSendCallback is not None:
            if self._expectingWaveData:
                # Skip the Wave body continuation when DVC is active
                self._expectingWaveData = False
                return
            if len(data) >= 1 and data[0] in (SNDC_WAVE, SNDC_WAVE2, SNDC_CLOSE):
                log.debug("RDPSND: static channel audio/close suppressed (DVC active)")
                return
        self._processData(data)

    def _processData(self, data):
        """Dispatch a reassembled RDPSND PDU."""
        if self._expectingWaveData:
            self._processWaveBody(data)
            return

        if len(data) < 4:
            return

        msgType = data[0]
        # data[1] is bPad
        bodySize = struct.unpack_from('<H', data, 2)[0]
        body = data[4:4 + bodySize]

        _names = {SNDC_FORMATS: "FORMATS", SNDC_TRAINING: "TRAINING",
                  SNDC_WAVE: "WAVE", SNDC_WAVE2: "WAVE2", SNDC_CLOSE: "CLOSE",
                  SNDC_SETVOLUME: "SETVOLUME", SNDC_QUALITYMODE: "QUALITYMODE"}
        if log._is_debug:
            log.debug("RDPSND: recv msgType=%s(0x%02x) bodySize=%d" %
                      (_names.get(msgType, "?"), msgType, bodySize))

        if msgType == SNDC_FORMATS:
            self._processServerFormats(body)
        elif msgType == SNDC_TRAINING:
            self._processTraining(body)
        elif msgType == SNDC_WAVE:
            self._processWaveInfo(body)
        elif msgType == SNDC_WAVE2:
            self._processWave2(body)
        elif msgType == SNDC_CLOSE:
            self._processClose()
        elif msgType == SNDC_SETVOLUME:
            pass
        elif msgType == SNDC_QUALITYMODE:
            pass
        else:
            log.debug("RDPSND: unknown msgType=0x%02x len=%d" % (msgType, len(data)))

    # ---------------------------------------------------------------
    # Server Audio Formats and Version (MS-RDPEA 2.2.2.1)
    # ---------------------------------------------------------------

    def _processServerFormats(self, body):
        """Parse Server Audio Formats PDU and respond with client formats."""
        if len(body) < 20:
            log.warning("RDPSND: Server Formats PDU too short")
            return

        dwFlags, dwVolume, dwPitch, wDGramPort = struct.unpack_from('<IIIH', body, 0)
        wNumberOfFormats = struct.unpack_from('<H', body, 14)[0]
        # body[16] = cLastBlockConfirmed (1 byte)
        # body[17] = wVersion (2 bytes)
        wVersion = struct.unpack_from('<H', body, 17)[0]
        # body[19] = bPad (1 byte)

        log.debug("RDPSND: Server Formats version=%d numFormats=%d flags=0x%x" %
                  (wVersion, wNumberOfFormats, dwFlags))

        offset = 20
        self._serverFormats = []
        for i in range(wNumberOfFormats):
            fmt, offset = AudioFormat.unpack(body, offset)
            if fmt is None:
                break
            self._serverFormats.append(fmt)
            log.debug("RDPSND:   server format[%d] = %s" % (i, fmt))

        # Select PCM formats we can play
        self._clientFormatIndices = []
        for i, fmt in enumerate(self._serverFormats):
            if fmt.is_pcm() and fmt.bits_per_sample in (8, 16) and fmt.channels in (1, 2):
                self._clientFormatIndices.append(i)

        if not self._clientFormatIndices:
            log.warning("RDPSND: no supported PCM format found among server formats")
            # Still respond with empty format list so the channel doesn't hang
            self._clientFormatIndices = []

        self._sendClientFormats(wVersion)

    def _sendClientFormats(self, serverVersion):
        """Send Client Audio Formats and Version PDU (MS-RDPEA 2.2.2.2)."""
        version = min(serverVersion, RDPSND_VERSION_MAJOR)

        # Build the list of formats we support
        formatData = b''
        for idx in self._clientFormatIndices:
            formatData += self._serverFormats[idx].pack()

        # Header: dwFlags(4) + dwVolume(4) + dwPitch(4) + wDGramPort(2)
        #        + wNumberOfFormats(2) + cLastBlockConfirmed(1) + wVersion(2) + bPad(1)
        hdr = struct.pack('<IIIH', TSSNDCAPS_ALIVE, 0, 0, 0)
        hdr += struct.pack('<H', len(self._clientFormatIndices))
        hdr += struct.pack('<B', 0)  # cLastBlockConfirmed
        hdr += struct.pack('<H', version)
        hdr += struct.pack('<B', 0)  # bPad
        body = hdr + formatData

        pdu = struct.pack('<BBH', SNDC_FORMATS, 0, len(body)) + body
        self._send(pdu)
        log.debug("RDPSND: sent Client Formats version=%d numFormats=%d" %
                  (version, len(self._clientFormatIndices)))

        # FreeRDP sends Quality Mode PDU right after Client Formats
        # when server version >= 6 (CHANNEL_VERSION_WIN_7).
        if version >= RDPSND_VERSION_MAJOR:
            self._sendQualityMode()

    # ---------------------------------------------------------------
    # Training (MS-RDPEA 2.2.2.3)
    # ---------------------------------------------------------------

    def _processTraining(self, body):
        """Process Training PDU and send Training Confirm."""
        if len(body) < 4:
            return
        wTimeStamp, wPackSize = struct.unpack_from('<HH', body, 0)
        log.debug("RDPSND: Training timestamp=%d packSize=%d" % (wTimeStamp, wPackSize))
        self._sendTrainingConfirm(wTimeStamp, wPackSize)

    def _sendTrainingConfirm(self, timestamp, packSize):
        """Send Training Confirm PDU (MS-RDPEA 2.2.2.4)."""
        body = struct.pack('<HH', timestamp, packSize)
        pdu = struct.pack('<BBH', SNDC_TRAINING, 0, len(body)) + body
        self._send(pdu)
        log.debug("RDPSND: sent Training Confirm")

    # ---------------------------------------------------------------
    # Wave Info / Wave Data (MS-RDPEA 2.2.2.5 / 2.2.2.6)
    # ---------------------------------------------------------------

    def _processWaveInfo(self, body):
        """Process WaveInfo PDU (first part of audio data)."""
        if len(body) < 12:
            log.warning("RDPSND: WaveInfo body too short (%d)" % len(body))
            return

        wTimeStamp = struct.unpack_from('<H', body, 0)[0]
        wFormatNo = struct.unpack_from('<H', body, 2)[0]
        # body[4] = cBlockNo (1 byte)
        cBlockNo = body[4]
        # body[5:8] = bPad (3 bytes)
        # body[8:12] = first 4 bytes of audio data
        initialData = body[8:12]

        self._waveTimestamp = wTimeStamp
        self._waveBlockNo = cBlockNo
        self._pendingWaveData = initialData

        # wFormatNo is an index into the CLIENT's format list
        if wFormatNo < len(self._clientFormatIndices):
            serverIdx = self._clientFormatIndices[wFormatNo]
            if self._activeFormatIndex != serverIdx:
                self._activeFormatIndex = serverIdx
                self._setupAudio(self._serverFormats[serverIdx])
        else:
            log.warning("RDPSND: WaveInfo format index %d out of range (client has %d formats)" %
                        (wFormatNo, len(self._clientFormatIndices)))

        # Next PDU body will be the Wave data body
        self._expectingWaveData = True
        log.debug("RDPSND: WaveInfo ts=%d fmt=%d block=%d initialBytes=%d" %
                  (wTimeStamp, wFormatNo, cBlockNo, len(initialData)))

    def _processWaveBody(self, data):
        """Process Wave PDU body (continuation of WaveInfo)."""
        self._expectingWaveData = False
        # Wave body: first 4 bytes are padding (duplicate of WaveInfo header),
        # then the remaining audio data
        audioData = self._pendingWaveData + data[4:]
        self._pendingWaveData = b''

        log.debug("RDPSND: Wave data %d bytes" % len(audioData))
        self._playAudio(audioData)
        self._sendWaveConfirm(self._confirmTimestamp(self._waveTimestamp), self._waveBlockNo)

    # ---------------------------------------------------------------
    # Wave2 (MS-RDPEA 2.2.2.7) - SNDC_WAVE2 (version >= 6)
    # ---------------------------------------------------------------

    def _processWave2(self, body):
        """Process Wave2 PDU (single PDU with all audio data)."""
        if len(body) < 12:
            log.warning("RDPSND: Wave2 body too short")
            return

        wTimeStamp = struct.unpack_from('<H', body, 0)[0]
        wFormatNo = struct.unpack_from('<H', body, 2)[0]
        cBlockNo = body[4]
        # body[5:8] = bPad (3 bytes)
        # body[8:12] = dwAudioTimeStamp (4 bytes, not used for playback)
        audioData = body[12:]

        # wFormatNo is an index into the CLIENT's format list
        if wFormatNo < len(self._clientFormatIndices):
            serverIdx = self._clientFormatIndices[wFormatNo]
            if self._activeFormatIndex != serverIdx:
                self._activeFormatIndex = serverIdx
                self._setupAudio(self._serverFormats[serverIdx])
        else:
            log.warning("RDPSND: Wave2 format index %d out of range (client has %d formats)" %
                        (wFormatNo, len(self._clientFormatIndices)))

        log.debug("RDPSND: Wave2 ts=%d fmt=%d block=%d dataLen=%d" %
                  (wTimeStamp, wFormatNo, cBlockNo, len(audioData)))
        self._playAudio(audioData)
        self._sendWaveConfirm(self._confirmTimestamp(wTimeStamp), cBlockNo)

    # ---------------------------------------------------------------
    # Wave Confirm (MS-RDPEA 2.2.2.8)
    # ---------------------------------------------------------------

    def _confirmTimestamp(self, wTimeStamp):
        """
        @summary: Wave Confirm timestamp: the server timestamp plus the time
        until this wave is heard (jitter buffer + device buffer), like
        FreeRDP. The server uses it to measure the audio latency and keep
        the video in sync with the sound.
        """
        latency = 0.0
        ring = self._audioRing
        if ring is not None:
            latency = ring.levelSeconds() + self._audioDeviceLatency
        return (wTimeStamp + int(min(latency, 2.0) * 1000)) & 0xFFFF

    def _sendWaveConfirm(self, timestamp, blockNo):
        """Send Wave Confirm PDU."""
        body = struct.pack('<HBB', timestamp, blockNo, 0)  # wTimeStamp, cConfBlockNo, bPad
        pdu = struct.pack('<BBH', SNDC_WAVECONFIRM, 0, len(body)) + body
        self._send(pdu)
        log.debug("RDPSND: sent WaveConfirm ts=%d block=%d" % (timestamp, blockNo))

    # ---------------------------------------------------------------
    # Close (MS-RDPEA 2.2.2.9)
    # ---------------------------------------------------------------

    def _processClose(self):
        """Handle Close PDU from server."""
        log.debug("RDPSND: server closed audio channel")
        self._stopAudio()
        # Reset format index so the next Wave2 always re-initialises the sink,
        # even if the server uses the same format number for the new stream.
        self._activeFormatIndex = -1

    # ---------------------------------------------------------------
    # Quality Mode (MS-RDPEA 2.2.2.13)
    # ---------------------------------------------------------------

    def _sendQualityMode(self):
        """Send Quality Mode PDU (MS-RDPEA 2.2.2.13).
        FreeRDP sends this after Client Formats when version >= 6.
        DYNAMIC_QUALITY lets the server choose the best mode."""
        body = struct.pack('<HH', DYNAMIC_QUALITY, 0)  # wQualityMode + Reserved
        pdu = struct.pack('<BBH', SNDC_QUALITYMODE, 0, len(body)) + body
        self._send(pdu)
        log.debug("RDPSND: sent Quality Mode (DYNAMIC)")

    # ---------------------------------------------------------------
    # Audio playback (audio_out.AudioOutput on its own thread)
    # ---------------------------------------------------------------

    def _setupAudio(self, fmt):
        """Configure audio for the given AudioFormat (called on format change).

        The output device is opened lazily, on the Qt side, when the first
        wave arrives; audio received meanwhile is kept in _audioPendingBuf.
        """
        self._stopAudio()
        if not fmt.is_pcm() or fmt.bits_per_sample not in (8, 16) or fmt.channels not in (1, 2):
            log.warning("RDPSND: unsupported format, audio playback disabled: %s" % fmt)
            return
        self._audioSrcFmt = audio_out.PcmFormat(
            fmt.samples_per_sec, fmt.channels, 'u8' if fmt.bits_per_sample == 8 else 's16')
        self._audioInitialized = True
        self._wavDumpFmt = fmt
        if self._wavDumpPath:
            self.enableWavDump(self._wavDumpPath)
            self._wavDumpPath = None
        log.debug("RDPSND: audio format %s" % self._audioSrcFmt)

    def _requestOutput(self):
        """Ask the audio thread to open the output device (Qt thread)."""
        src = self._audioSrcFmt

        def _open():
            output = audio_out.AudioOutput.instance()
            if output is None:
                self._audioDisabled = True
                return
            if not self._audioInitialized or self._audioSrcFmt is not src:
                return  # stopped or format changed meanwhile
            token = output.nextToken()
            self._audioToken = token  # before start(): the device may be ready at once
            output.start(src, self._onOutputReady, token)

        self._audioToken = -1  # requested, token not known yet
        self._qt_invoke(_open)

    def _onOutputReady(self, token, ring, converter, deviceLatency):
        """Called from the audio thread once the device is playing."""
        if token != self._audioToken or not self._audioInitialized:
            output = audio_out.AudioOutput.instance()
            if output is not None:
                output.stop(token)
            return
        self._audioConverter = converter
        self._audioDeviceLatency = deviceLatency
        self._audioRing = ring  # published last: the network thread polls it

    def _playAudio(self, data):
        """Queue PCM received from the server for playback (network thread)."""
        if not self._audioInitialized or self._audioDisabled:
            return
        if self._wavDumpFile is not None:
            try:
                self._wavDumpFile.writeframesraw(data)
            except Exception:
                pass
        ring = self._audioRing
        if ring is None:
            self._audioPendingBuf.extend(data)
            # keep at most ~1 s while the device opens
            limit = self._audioSrcFmt.bytesPerSecond
            if len(self._audioPendingBuf) > limit:
                cut = len(self._audioPendingBuf) - limit
                cut -= cut % self._audioSrcFmt.frameBytes
                del self._audioPendingBuf[:cut]
            if self._audioToken == 0:
                self._requestOutput()
            return
        converter = self._audioConverter
        if self._audioPendingBuf:
            pending = bytes(self._audioPendingBuf)
            self._audioPendingBuf = bytearray()
            ring.write(converter.convert(pending))
        ring.write(converter.convert(data))

    def _stopAudio(self):
        """Stop playback and release the output device."""
        self._closeWavDump()
        token = self._audioToken
        self._audioInitialized = False
        self._audioToken = 0
        self._audioRing = None
        self._audioConverter = None
        self._audioPendingBuf = bytearray()
        if token > 0:
            def _stop():
                output = audio_out.AudioOutput.instance()
                if output is not None:
                    output.stop(token)
            self._qt_invoke(_stop)

    def enableWavDump(self, path):
        """Start dumping raw PCM audio to *path* (WAV format).

        Call before connecting, or at any time — the file is opened when
        the first audio format is negotiated.  The dump captures the
        exact bytes received from the server, BEFORE any crossfade or
        buffering.  Play the resulting file locally to verify whether
        scratchiness originates from the server or from client playback.
        """
        import wave
        if self._wavDumpFmt is None:
            # Format not yet known; store path and open later
            self._wavDumpPath = path
            return
        self._closeWavDump()
        fmt = self._wavDumpFmt
        wf = wave.open(path, 'wb')
        wf.setnchannels(fmt.channels)
        wf.setsampwidth(fmt.bits_per_sample // 8)
        wf.setframerate(fmt.samples_per_sec)
        self._wavDumpFile = wf
        log.info("RDPSND: WAV dump started: %s" % path)

    def _closeWavDump(self):
        if self._wavDumpFile is not None:
            try:
                self._wavDumpFile.close()
                log.info("RDPSND: WAV dump closed")
            except Exception:
                pass
            self._wavDumpFile = None

    # ---------------------------------------------------------------
    # Send helper
    # ---------------------------------------------------------------

    def _send(self, data):
        """Send data back to server via static VChannel or DVC."""
        if self._dvcSendCallback is not None:
            # DVC path: send raw rdpsnd PDU (no VChannel header)
            log.debug("RDPSND: _send via DVC callback, len=%d" % len(data))
            self._dvcSendCallback(data)
        elif self._transport is not None:
            # Static VChannel path: wrap with VChannel header
            log.debug("RDPSND: _send via static VChannel, len=%d" % len(data))
            from rdpy.core.type import String
            flags = CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST
            header = struct.pack('<II', len(data), flags)
            self._transport.send(String(header + data))
        else:
            log.warning("RDPSND: _send failed: no transport or DVC callback")
