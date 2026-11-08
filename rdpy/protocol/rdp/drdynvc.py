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
@summary: Dynamic Virtual Channel Extension (MS-RDPEDYC) with RDPGFX support.
Handles DRDYNVC protocol over the "drdynvc" static virtual channel, and
implements the Graphics Pipeline Extension (MS-RDPEGFX) for GNOME Remote
Desktop and other modern RDP servers.
"""
from __future__ import absolute_import, division, print_function
import collections
import os
from rdpy.core.compat import queue, monotonic
import struct
import threading
import time
import numpy as np
from rdpy.core.layer import LayerAutomata
import rdpy.core.log as log
from rdpy.protocol.rdp.rfx_progressive import RfxProgressiveDecoder
from rdpy.protocol.rdp.rfx import RfxDecoder
from rdpy.protocol.rdp.zgfx import ZgfxDecompressor
from rdpy.protocol.rdp.nscodec import decode_nscodec
from rdpy.protocol.rdp import avc as avc_module
from rdpy.protocol.rdp import audin

RDPGFX_CHANNEL_NAME = "Microsoft::Windows::RDS::Graphics"
# RDPGFX command IDs (MS-RDPEGFX 2.2, FreeRDP rdpgfx.h)
RDPGFX_CMDID_WIRETOSURFACE_1 = 0x0001
RDPGFX_CMDID_WIRETOSURFACE_2 = 0x0002
RDPGFX_CMDID_DELETEENCODINGCONTEXT = 0x0003
RDPGFX_CMDID_SOLIDFILL = 0x0004
RDPGFX_CMDID_SURFACETOSURFACE = 0x0005
RDPGFX_CMDID_SURFACETOCACHE = 0x0006
RDPGFX_CMDID_CACHETOSURFACE = 0x0007
RDPGFX_CMDID_EVICTCACHEENTRY = 0x0008
RDPGFX_CMDID_CREATESURFACE = 0x0009
RDPGFX_CMDID_DELETESURFACE = 0x000A
RDPGFX_CMDID_STARTFRAME = 0x000B
RDPGFX_CMDID_ENDFRAME = 0x000C
RDPGFX_CMDID_FRAMEACKNOWLEDGE = 0x000D
RDPGFX_CMDID_RESETGRAPHICS = 0x000E
RDPGFX_CMDID_MAPSURFACETOOUTPUT = 0x000F
RDPGFX_CMDID_CACHEIMPORTOFFER = 0x0010
RDPGFX_CMDID_CACHEIMPORTREPLY = 0x0011
RDPGFX_CMDID_CAPSADVERTISE = 0x0012
RDPGFX_CMDID_CAPSCONFIRM = 0x0013
RDPGFX_CMDID_MAPSURFACETOWINDOW = 0x0015
RDPGFX_CMDID_MAPSURFACETOSCALEDOUTPUT = 0x0017
_RDPGFX_CMDID_NAMES = {
    0x0001: "WIRETOSURFACE_1", 0x0002: "WIRETOSURFACE_2",
    0x0003: "DELETEENCODINGCTX", 0x0004: "SOLIDFILL",
    0x0005: "SURFACETOSURFACE", 0x0006: "SURFACETOCACHE",
    0x0007: "CACHETOSURFACE", 0x0008: "EVICTCACHEENTRY",
    0x0009: "CREATESURFACE", 0x000A: "DELETESURFACE",
    0x000B: "STARTFRAME", 0x000C: "ENDFRAME",
    0x000D: "FRAMEACKNOWLEDGE", 0x000E: "RESETGRAPHICS",
    0x000F: "MAPSURFACETOOUTPUT", 0x0010: "CACHEIMPORTOFFER",
    0x0011: "CACHEIMPORTREPLY", 0x0012: "CAPSADVERTISE",
    0x0013: "CAPSCONFIRM", 0x0015: "MAPSURFACETOWINDOW",
    0x0017: "MAPSURFACETOSCALEDOUTPUT",
}
# RDPGFX codec IDs (MS-RDPEGFX, FreeRDP rdpgfx.h)
RDPGFX_CODECID_UNCOMPRESSED = 0x0000
RDPGFX_CODECID_CAVIDEO = 0x0003
RDPGFX_CODECID_PLANAR = 0x0004  # grdp-style Planar (thin-client / v8.0 path)
RDPGFX_CODECID_CLEARCODEC = 0x0008
RDPGFX_CODECID_CAPROGRESSIVE = 0x0009
RDPGFX_CODECID_PLANAR_V2 = 0x000A  # MS-RDPEGFX 2.2.2.1 Planar (v10 path)
RDPGFX_CODECID_AVC420 = 0x000B
RDPGFX_CODECID_ALPHA = 0x000C
RDPGFX_CODECID_AVC444 = 0x000E
RDPGFX_CODECID_AVC444v2 = 0x000F
# Pixel formats
GFX_PIXEL_FORMAT_XRGB_8888 = 0x20
GFX_PIXEL_FORMAT_ARGB_8888 = 0x21
# RDPGFX capability versions (MS-RDPEGFX 2.2.3)
RDPGFX_CAPVERSION_8 = 0x00080004
RDPGFX_CAPVERSION_81 = 0x00080105
RDPGFX_CAPVERSION_10 = 0x000A0002
RDPGFX_CAPVERSION_101 = 0x000A0100
RDPGFX_CAPVERSION_102 = 0x000A0200
RDPGFX_CAPVERSION_103 = 0x000A0301
RDPGFX_CAPVERSION_104 = 0x000A0400
RDPGFX_CAPVERSION_105 = 0x000A0502
RDPGFX_CAPVERSION_106 = 0x000A0600
RDPGFX_CAPVERSION_106_ERR = 0x000A0601
RDPGFX_CAPVERSION_107 = 0x000A0701
# RDPGFX capability flags
RDPGFX_CAPS_FLAG_THINCLIENT = 0x00000001
RDPGFX_CAPS_FLAG_SMALL_CACHE = 0x00000002
RDPGFX_CAPS_FLAG_AVC420_ENABLED = 0x00000010  # v8.1: explicitly enable AVC420
# ClearCodec log2 floor table for palette bit calculation (MS-RDPEGFX 2.2.4.1.1.2)
_CLEAR_LOG2_FLOOR = bytes(
    [0] * 2 + [1] * 2 + [2] * 4 + [3] * 8 + [4] * 16 + [5] * 32 + [6] * 64 + [7] * 128
)
RDPGFX_CAPS_FLAG_AVC_DISABLED = 0x00000020  # v10+: disable AVC
RDPGFX_CAPS_FLAG_AVC_THINCLIENT = 0x00000040  # v10.3+: AVC thin client
# Frame drop: skip heavy codec decode when frames arrive faster than decode.
# If time since last END_FRAME exceeds this interval (seconds), skip heavy
# CaVideo/Progressive decode but still process EndFrame for ACKs.
# VChannel flags
CHANNEL_FLAG_FIRST = 0x00000001
CHANNEL_FLAG_LAST = 0x00000002
CHANNEL_FLAG_SHOW_PROTOCOL = 0x00000010


class DrdynvcCmd(object):
    CREATE = 0x01
    DATA_FIRST = 0x02
    DATA = 0x03
    CLOSE = 0x04
    CAPABILITY = 0x05
    DATA_FIRST_COMPRESSED = 0x06
    DATA_COMPRESSED = 0x07
    SOFT_SYNC_REQUEST = 0x08
    SOFT_SYNC_RESPONSE = 0x09


# Decode the graphics pipeline (ZGFX, H.264, RemoteFX...) on a worker thread
# so the GUI, the network and the audio stay responsive while video frames are
# decoded. Set RDPY_GFX_THREAD=0 to decode on the network thread instead.
_GFX_THREAD = os.environ.get('RDPY_GFX_THREAD', '1') != '0'


class DrdynvcLayer(LayerAutomata):
    """
    @summary: Dynamic Virtual Channel Extension layer with RDPGFX support.
    Handles DRDYNVC protocol over the "drdynvc" static virtual channel,
    including full RDPGFX Graphics Pipeline for rendering.
    """

    def __init__(self):
        LayerAutomata.__init__(self, None)
        self._version = 2
        self._dynamicChannels = {}  # channelId -> channelName
        self._channelCbId = {}  # channelId -> cbId (for sending data)
        # VChannel reassembly buffer
        self._vchanBuf = bytearray()
        # DVC DATA_FIRST reassembly: channelId -> (totalLen, buffer)
        self._dvcReassembly = {}
        # RDPGFX state
        self._gfxChannelId = None  # DVC channelId for Graphics pipeline
        self._gfxConfirmed = False  # True after CAPS_CONFIRM received
        self._surfaces = {}  # surfaceId -> (width, height, pixelFormat)
        self._surfaceData = {}  # surfaceId -> bytearray (BGRA pixel buffer)
        self._surfaceOutputMap = {}  # surfaceId -> (outputOriginX, outputOriginY)
        self._currentFrameId = 0
        self._totalFramesDecoded = 0
        self._lastFrameTime = 0.0  # monotonic time of last END_FRAME (for stats)
        self._resetWidth = 0
        self._resetHeight = 0
        # RFX Progressive decoder
        self._rfxDecoder = RfxProgressiveDecoder()
        # Non-progressive RFX decoder (for CaVideo codec 0x0003)
        self._rfxTileDecoder = RfxDecoder()
        # ZGFX decompressor (stateful, persistent history across segments)
        self._zgfx = ZgfxDecompressor()
        # H.264/AVC decoders: one per surface, like FreeRDP (each surface has
        # its own reference frames). _avcDecoder is the one in use.
        self._avcDecoders = {}  # surfaceId -> AvcDecoder
        self._avcDecoder = None
        # Presentation (FreeRDP gdi/gfx.c): decoders only write into the
        # surfaces and mark rectangles invalid; the invalid areas are copied
        # to the screen once per frame (END_FRAME), or right away for
        # commands sent outside of a frame.
        self._invalidRects = {}  # surfaceId -> [(x0, y0, x1, y1)]
        self._inGfxFrame = False
        self._gfxPresentCallback = None
        # AVC freeze recovery: track the last successful decode time so we
        # can request a full-screen refresh (IDR) after a prolonged freeze.
        self._avcLastSuccessTime = 0.0  # monotonic; 0 = no success yet this session
        self._avcLastRefreshTime = 0.0  # monotonic; time of last full freeze refresh request
        self._avcKeyframeRequestTime = 0.0  # monotonic; time of last keyframe-wait IDR request
        self._avcHardResetTime = 0.0  # monotonic; time of FIRST AVC hard reset (0 = none)
        self._avcDecoderBrokenNotified = False  # True once reconnect callback has been fired
        self._avcSoftResetCount = 0  # soft resets attempted since last successful decode
        self._lastLC2RecvTime = 0.0  # monotonic; time of most recent LC=2 frame arrival
        self._auxDecoderBrokenTime = 0.0  # monotonic; when LC=2 aux decoder first went stuck (0=ok)
        self._decoderBrokenCallback = None
        self._avc444Disabled = False
        # Graphics pipeline scheduling. Every RDPGFX message is decoded in
        # arrival order (H.264 needs it): on the worker thread, or on the
        # network thread through _gfxPduQueue when the worker is disabled.
        self._gfxThreadEnabled = _GFX_THREAD
        self._gfxQueue = None  # queue.Queue of segments for the worker
        self._gfxThread = None
        self._gfxBacklog = 0  # segments waiting for the worker
        self._gfxBacklogLock = threading.Lock()
        self._gfxStopped = False
        self._gfxPduQueue = collections.deque()  # inline mode: PDUs waiting
        self._gfxDraining = False
        # Bitmap cache: cacheSlot -> (width, height, bytearray BGRA data)
        self._gfxCache = {}
        # ClearCodec VBAR and glyph caches (persistent across decode calls, matching grdp)
        self._ccVBarStorage = [None] * 32768  # VBAR cache: list of (pixels_bytes, count)
        self._ccShortVBarStorage = [None] * 16384  # Short VBAR cache
        self._ccGlyphStorage = {}  # Glyph cache: glyphIndex -> BGRA bytes
        self._ccVBarCursor = 0
        self._ccShortVBarCursor = 0
        # Negotiated GFX version (set by CAPS_CONFIRM)
        self._gfxVersion = 0
        # Callbacks
        self._gfxCallback = None  # bitmap delivery callback
        self._gfxSurfaceCallback = None  # fast path: deliver surface buf region directly
        self._capsConfirmCallback = None  # called when CAPS_CONFIRM received
        self._requestRefreshCallback = None  # called to request IDR from server
        # RDPSND DVC routing
        self._rdpsndLayer = None  # set via setRdpsndLayer() for DVC audio routing
        self._rdpsndDvcChannelIds = set()  # DVC channelIds for rdpsnd audio
        self._rdpsndDvcPrimaryId = None  # primary channel for sending responses
        # Optional channels
        self._gfxEnabled = True  # graphics pipeline (RDPGFX)
        self._soundEnabled = True  # audio output over AUDIO_PLAYBACK_DVC
        self._audinEnabled = False  # microphone (AUDIO_INPUT, MS-RDPEAI)
        self._audinChannels = {}  # channelId -> audin.AudinChannel

    def setGfxCallback(self, callback):
        """Set callback for RDPGFX bitmap delivery: callback(x, y, w, h, bpp, data)"""
        self._gfxCallback = callback

    def setGfxPresentCallback(self, callback):
        """Set the frame presentation callback: callback(items) where items is
        a list of (x, y, width, height, bgra) with bgra a (height, width*4)
        uint8 numpy array owned by the receiver (a copy of the surface)."""
        self._gfxPresentCallback = callback

    def setGfxSurfaceCallback(self, callback):
        """Set fast-path callback for AVC surface delivery.

        callback(dest_x, dest_y, width, height, surf_buf, surf_w, src_left, src_top)
        where surf_buf is the surface bytearray and (src_left, src_top) is the
        offset of the decoded region within it (row stride = surf_w * 4).
        When set, _renderAvc420/444 skip tobytes() and deliver the surface
        buffer directly.  Falls back to _gfxCallback with tobytes() if None.
        """
        self._gfxSurfaceCallback = callback

    def setCapsConfirmCallback(self, callback):
        """Set callback for RDPGFX CAPS_CONFIRM notification."""
        self._capsConfirmCallback = callback

    def setRdpsndLayer(self, rdpsndLayer):
        """Set the RDPSND layer for DVC audio routing."""
        self._rdpsndLayer = rdpsndLayer

    def setRequestRefreshCallback(self, callback):
        """Set callback to request a full-screen refresh (IDR) from the server.

        The callback takes no arguments.  It is called when AVC frames have
        produced no output for longer than _AVC_FREEZE_THRESHOLD seconds,
        indicating the decoder is stuck waiting for an IDR key frame.
        """
        self._requestRefreshCallback = callback

    def setDecoderBrokenCallback(self, callback):
        """Set callback invoked when the H.264 decoder is permanently broken.

        Called when the decoder has been hard-reset but the server still has
        not sent an IDR within _AVC_RECONNECT_TIMEOUT seconds.  The callback
        should trigger a full RDP reconnect to restore video playback.
        """
        self._decoderBrokenCallback = callback

    def setGraphicsEnabled(self, enabled):
        """Accept the graphics pipeline channel (default True)."""
        self._gfxEnabled = bool(enabled)

    def setSoundEnabled(self, enabled):
        """Accept the dynamic audio output channels (default True)."""
        self._soundEnabled = bool(enabled)

    def setAudioInputEnabled(self, enabled):
        """Accept the microphone channel AUDIO_INPUT (default False)."""
        self._audinEnabled = bool(enabled)

    def setAVC444Disabled(self, disabled):
        """Set whether AVC444 / AVC444v2 support is disabled."""
        self._avc444Disabled = bool(disabled)

    def connect(self):
        log.debug("DrdynvcLayer.connect()")

    def recv(self, s):
        """Receive data on the drdynvc static virtual channel with VChannel reassembly."""
        data = s.read()
        if len(data) < 9:
            return
        totalLen, flags = struct.unpack_from('<II', data, 0)
        payload = data[8:]

        if flags & CHANNEL_FLAG_FIRST:
            self._vchanBuf = bytearray(payload)
        else:
            self._vchanBuf.extend(payload)

        if flags & CHANNEL_FLAG_LAST:
            buf = self._vchanBuf
            self._vchanBuf = bytearray()
            if len(buf) >= 1:
                self._processData(buf)

    def _processData(self, data):
        header = data[0]
        cmd = (header >> 4) & 0x0f
        sp = (header >> 2) & 0x03
        cbId = header & 0x03

        if cmd == DrdynvcCmd.CAPABILITY:
            self._processCapabilities(data)
        elif cmd == DrdynvcCmd.CREATE:
            self._processCreateRequest(data, cbId)
        elif cmd == DrdynvcCmd.DATA_FIRST:
            self._processDataFirst(data, cbId, sp)
        elif cmd == DrdynvcCmd.DATA:
            self._processDataPdu(data, cbId)
        elif cmd == DrdynvcCmd.CLOSE:
            self._processClose(data, cbId)
        elif cmd == DrdynvcCmd.SOFT_SYNC_REQUEST:
            self._processSoftSyncRequest(data)
        else:
            log.debug("DrdynvcLayer: unknown cmd=%d" % cmd)

    def _readChannelId(self, data, offset, cbId):
        if cbId == 0:
            return data[offset], offset + 1
        elif cbId == 1:
            return struct.unpack_from('<H', data, offset)[0], offset + 2
        elif cbId == 2:
            return struct.unpack_from('<I', data, offset)[0], offset + 4
        return 0, offset

    def _readLength(self, data, offset, sp):
        """Read variable-length field based on Sp bits (for DATA_FIRST totalLength)."""
        if sp == 0:
            return data[offset], offset + 1
        elif sp == 1:
            return struct.unpack_from('<H', data, offset)[0], offset + 2
        elif sp == 2:
            return struct.unpack_from('<I', data, offset)[0], offset + 4
        return 0, offset

    def _processCapabilities(self, data):
        if len(data) < 4:
            log.warning("DrdynvcLayer: capabilities PDU too short")
            return
        version = struct.unpack_from('<H', data, 2)[0]
        log.debug("DrdynvcLayer: server DRDYNVC version=%d" % version)

        # Clear all DVC state from previous connection (reconnection case)
        if self._dynamicChannels:
            log.debug("DrdynvcLayer: clearing stale DVC state (%d channels)" % len(self._dynamicChannels))
        self._dynamicChannels.clear()
        self._channelCbId.clear()
        self._dvcReassembly.clear()
        self._gfxChannelId = None
        self._gfxConfirmed = False
        self._rdpsndDvcChannelIds.clear()
        self._rdpsndDvcPrimaryId = None
        # Reset ZGFX decompressor (history is connection-scoped)
        self._zgfx = ZgfxDecompressor()

        # Accept up to version 3 (Soft-Sync)
        self._version = min(version, 3)

        response = struct.pack('<BBH', 0x50, 0x00, self._version)
        self._send(response)
        log.debug("DrdynvcLayer: sent capabilities response version=%d" % self._version)

    def _processSoftSyncRequest(self, data):
        """Handle DVC Soft-Sync Request (version 3). Parse and respond."""
        # data[0] = header (already parsed), data[1] = pad
        if len(data) < 10:
            log.warning("DrdynvcLayer: Soft-Sync Request too short (%d bytes)" % len(data))
            return
        offset = 1  # skip header byte
        pad = data[offset];
        offset += 1
        length = struct.unpack_from('<I', data, offset)[0];
        offset += 4
        flags = struct.unpack_from('<H', data, offset)[0];
        offset += 2
        numTunnels = struct.unpack_from('<H', data, offset)[0];
        offset += 2
        log.debug("DrdynvcLayer: Soft-Sync Request flags=0x%04x numTunnels=%d" % (flags, numTunnels))

        tunnelTypes = []
        for i in range(numTunnels):
            if offset + 4 > len(data):
                break
            tt = struct.unpack_from('<I', data, offset)[0];
            offset += 4
            tunnelTypes.append(tt)
            log.debug("DrdynvcLayer: Soft-Sync tunnel[%d] type=0x%08x" % (i, tt))

        SOFT_SYNC_CHANNEL_LIST_PRESENT = 0x0002
        if flags & SOFT_SYNC_CHANNEL_LIST_PRESENT:
            if offset + 2 <= len(data):
                numChannels = struct.unpack_from('<H', data, offset)[0];
                offset += 2
                for i in range(numChannels):
                    if offset + 8 > len(data):
                        break
                    dvcChId = struct.unpack_from('<I', data, offset)[0];
                    offset += 4
                    chTunnelType = struct.unpack_from('<I', data, offset)[0];
                    offset += 4
                    log.debug("DrdynvcLayer: Soft-Sync channel[%d] dvcId=%d tunnelType=0x%08x" %
                              (i, dvcChId, chTunnelType))

        # Send Soft-Sync Response
        SOFT_SYNC_TCP_FLUSHED = 0x0001
        resp = bytearray()
        resp.append(0x90)  # header: cmd=SOFT_SYNC_RESPONSE(0x09) << 4
        resp.append(0x00)  # pad
        respPayload = struct.pack('<H', SOFT_SYNC_TCP_FLUSHED)  # flags
        respPayload += struct.pack('<H', numTunnels)  # numberOfTunnels
        for tt in tunnelTypes:
            respPayload += struct.pack('<I', tt)
        resp += struct.pack('<I', len(respPayload))  # length
        resp += respPayload
        self._send(bytes(resp))
        log.debug("DrdynvcLayer: sent Soft-Sync Response")

    def _processCreateRequest(self, data, cbId):
        channelId, offset = self._readChannelId(data, 1, cbId)
        channelName = data[offset:].split(b'\x00')[0].decode('utf-8', errors='replace')
        log.debug("DrdynvcLayer: CREATE channelId=%d name=%s" % (channelId, channelName))

        # Only accept channels we actually implement; reject others so the
        # server doesn't expect functionality we cannot provide (matching grdp).
        _SUPPORTED_CHANNELS = {
            # AUDIO_PLAYBACK_LOSSY_DVC (AAC/Opus) is not supported; reject it so
            # gnome-remote-desktop falls back to lossless AUDIO_PLAYBACK_DVC (PCM).
            # CoreInput and MouseCursor must be accepted (grdp accepts them);
            # rejecting them may cause Windows Server to disconnect mid-session.
            "Microsoft::Windows::RDS::CoreInput",
            "Microsoft::Windows::RDS::MouseCursor",
        }
        if self._gfxEnabled:
            _SUPPORTED_CHANNELS.add(RDPGFX_CHANNEL_NAME)
        if self._soundEnabled:
            _SUPPORTED_CHANNELS.update(("rdpsnd", "AUDIO_PLAYBACK_DVC"))
        if self._audinEnabled:
            _SUPPORTED_CHANNELS.add(audin.CHANNEL_NAME)
        accepted = channelName in _SUPPORTED_CHANNELS

        self._dynamicChannels[channelId] = channelName
        self._channelCbId[channelId] = cbId

        header = (DrdynvcCmd.CREATE << 4) | cbId
        response = bytearray([header])
        if cbId == 0:
            response += struct.pack('<B', channelId)
        elif cbId == 1:
            response += struct.pack('<H', channelId)
        elif cbId == 2:
            response += struct.pack('<I', channelId)

        if accepted:
            response += struct.pack('<I', 0)  # CHANNEL_RC_OK
            self._send(bytes(response))
            log.debug("DrdynvcLayer: sent create response (OK) for channelId=%d" % channelId)
        else:
            response += struct.pack('<i', -1)  # reject
            self._send(bytes(response))
            log.debug("DrdynvcLayer: rejected channel %s (id=%d) — no handler" % (channelName, channelId))
            return

        if channelName == audin.CHANNEL_NAME:
            from twisted.internet import reactor
            self._audinChannels[channelId] = audin.AudinChannel(
                lambda data, _ch=channelId, _cb=cbId: self._sendDvcData(_ch, _cb, data),
                callFromThread=reactor.callFromThread)
            log.debug("DrdynvcLayer: microphone channel opened (id=%d)" % channelId)
        elif channelName == RDPGFX_CHANNEL_NAME:
            self._gfxChannelId = channelId
            if not self._gfxConfirmed:
                self._sendRdpgfxCapsAdvertise(channelId, cbId)
            else:
                log.debug("RDPGFX: channel re-created without close, skipping duplicate CAPS_ADVERTISE")
        elif channelName in ("rdpsnd", "AUDIO_PLAYBACK_DVC"):
            self._rdpsndDvcChannelIds.add(channelId)
            log.debug("DrdynvcLayer: RDPSND DVC channel mapped to channelId=%d (%s)" % (channelId, channelName))
            if self._rdpsndLayer is not None and self._rdpsndDvcPrimaryId is None:
                # Use the first audio channel as the primary for sending responses
                self._rdpsndDvcPrimaryId = channelId

                def dvcSendRdpsnd(data, _chId=channelId, _cbId=cbId):
                    log.debug("DrdynvcLayer: dvcSendRdpsnd channelId=%d cbId=%d dataLen=%d" %
                              (_chId, _cbId, len(data)))
                    header = (DrdynvcCmd.DATA << 4) | _cbId
                    pdu = bytearray([header])
                    if _cbId == 0:
                        pdu += struct.pack('<B', _chId)
                    elif _cbId == 1:
                        pdu += struct.pack('<H', _chId)
                    elif _cbId == 2:
                        pdu += struct.pack('<I', _chId)
                    pdu += data
                    self._send(bytes(pdu))

                self._rdpsndLayer._dvcSendCallback = dvcSendRdpsnd

    def _processDataFirst(self, data, cbId, sp):
        """Handle DVC DATA_FIRST: first fragment of a large DVC message."""
        channelId, offset = self._readChannelId(data, 1, cbId)
        totalLen, offset = self._readLength(data, offset, sp)
        fragment = data[offset:]
        channelName = self._dynamicChannels.get(channelId, "unknown")
        if log._is_debug:
            log.debug("DrdynvcLayer: DATA_FIRST channelId=%d (%s) totalLen=%d fragLen=%d" %
                      (channelId, channelName, totalLen, len(fragment)))
        if len(fragment) >= totalLen:
            # Complete message in first fragment — dispatch immediately
            # (matching grdp behaviour; avoids losing the message if a
            # subsequent DATA_FIRST overwrites the reassembly entry and
            # desynchronising the ZGFX history buffer).
            self._dispatchDvcData(channelId, bytes(fragment[:totalLen]))
        else:
            self._dvcReassembly[channelId] = (totalLen, bytearray(fragment))

    def _processDataPdu(self, data, cbId):
        """Handle DVC DATA: either a complete message or continuation of DATA_FIRST."""
        channelId, offset = self._readChannelId(data, 1, cbId)
        fragment = data[offset:]

        # Check if this is a continuation of a DATA_FIRST sequence
        if channelId in self._dvcReassembly:
            totalLen, buf = self._dvcReassembly[channelId]
            buf += fragment
            if len(buf) >= totalLen:
                del self._dvcReassembly[channelId]
                self._dispatchDvcData(channelId, bytes(buf[:totalLen]))
            else:
                self._dvcReassembly[channelId] = (totalLen, buf)
            return

        # Complete single-segment message
        self._dispatchDvcData(channelId, fragment)

    def _dispatchDvcData(self, channelId, payload):
        """Dispatch reassembled DVC channel data to the appropriate handler."""
        channelName = self._dynamicChannels.get(channelId, "unknown")
        if channelId in self._audinChannels:
            self._audinChannels[channelId].recv(bytes(payload))
        elif channelId == self._gfxChannelId and len(payload) >= 8:
            self._processRdpgfxStream(payload)
        elif channelId in self._rdpsndDvcChannelIds and self._rdpsndLayer is not None:
            self._rdpsndLayer._processData(payload)
        else:
            if log._is_debug:
                log.debug("DrdynvcLayer: data on channelId=%d (%s) len=%d" %
                          (channelId, channelName, len(payload)))

    def _processClose(self, data, cbId):
        channelId, offset = self._readChannelId(data, 1, cbId)
        channelName = self._dynamicChannels.pop(channelId, "unknown")
        self._channelCbId.pop(channelId, None)
        self._dvcReassembly.pop(channelId, None)
        channel = self._audinChannels.pop(channelId, None)
        if channel is not None:
            channel.close()
        if channelId == self._gfxChannelId:
            self._gfxChannelId = None
            self._gfxConfirmed = False
        if channelId in self._rdpsndDvcChannelIds:
            self._rdpsndDvcChannelIds.discard(channelId)
            if self._rdpsndDvcPrimaryId == channelId:
                self._rdpsndDvcPrimaryId = None
                if self._rdpsndLayer is not None:
                    self._rdpsndLayer._dvcSendCallback = None
        log.debug("DrdynvcLayer: CLOSE channelId=%d (%s)" % (channelId, channelName))

    # ---------------------------------------------------------------
    # RDP_SEGMENTED_DATA unwrapping (MS-RDPEGFX 2.2.5)
    # ---------------------------------------------------------------

    ZGFX_SEGMENTED_SINGLE = 0xE0
    ZGFX_SEGMENTED_MULTIPART = 0xE1
    ZGFX_PACKET_COMPRESSED = 0x20

    def _unwrapSegmentedData(self, data):
        """Unwrap RDP_SEGMENTED_DATA envelope to get raw RDPGFX PDU stream.
        Returns decompressed/raw data, or None on error."""
        if len(data) < 2:
            return None
        descriptor = data[0]
        if descriptor == self.ZGFX_SEGMENTED_SINGLE:
            # Single segment: decompress using ZGFX
            try:
                return self._zgfx.decompress_segment(data[1:])
            except Exception as e:
                log.warning("RDPGFX: ZGFX decompress error (single): %s" % e)
                return None
        elif descriptor == self.ZGFX_SEGMENTED_MULTIPART:
            if len(data) < 7:
                return None
            segmentCount, = struct.unpack_from('<H', data, 1)
            uncompressedSize, = struct.unpack_from('<I', data, 3)
            offset = 7
            result = bytearray()
            for i in range(segmentCount):
                if offset + 4 > len(data):
                    break
                segSize = struct.unpack_from('<I', data, offset)[0]
                offset += 4
                if offset + segSize > len(data):
                    break
                segData = data[offset:offset + segSize]
                offset += segSize
                try:
                    result.extend(self._zgfx.decompress_segment(segData))
                except Exception as e:
                    log.warning("RDPGFX: ZGFX decompress error (multi seg %d): %s" % (i, e))
                    return None
            return bytes(result)
        else:
            log.warning("RDPGFX: unknown segmented data descriptor 0x%02x" % descriptor)
            return None

    # ---------------------------------------------------------------
    # RDPGFX (MS-RDPEGFX) handling
    # ---------------------------------------------------------------

    def _processRdpgfxStream(self, data):
        """Queue one RDP_SEGMENTED_DATA message of the graphics channel."""
        if self._gfxStopped:
            return
        if self._gfxThreadEnabled:
            self._startGfxWorker()
            with self._gfxBacklogLock:
                self._gfxBacklog += 1
            self._gfxQueue.put(bytes(data))
        else:
            self._processRdpgfxStreamSync(data)

    # ---- worker thread ----

    def _startGfxWorker(self):
        if self._gfxThread is not None:
            return
        self._gfxQueue = queue.Queue()
        self._gfxThread = threading.Thread(target=self._gfxWorkerLoop, name="rdpgfx-decode")
        self._gfxThread.daemon = True
        self._gfxThread.start()
        log.debug("RDPGFX: decoding on a worker thread")

    def _gfxWorkerLoop(self):
        q = self._gfxQueue
        while True:
            data = q.get()
            if data is None or self._gfxStopped:
                break
            with self._gfxBacklogLock:
                self._gfxBacklog -= 1
            try:
                for cmdId, flags, payload in self._parseRdpgfxSegment(data):
                    if self._gfxStopped:
                        break
                    self._processRdpgfxPduSafe(cmdId, flags, payload)
            except Exception as e:  # never let the decoder thread die
                log.error("RDPGFX: worker error: %r" % (e,))

    def _onGfxThread(self):
        t = self._gfxThread
        return t is not None and t.ident == threading.get_ident()

    def _reactorCall(self, fn, *args):
        """Run fn(*args) on the network (Twisted) thread."""
        if fn is None:
            return
        if self._onGfxThread():
            from twisted.internet import reactor
            reactor.callFromThread(fn, *args)
        else:
            fn(*args)

    def shutdown(self):
        """Stop decoding (connection closed). Queued frames are discarded."""
        for channel in self._audinChannels.values():
            channel.close()
        self._audinChannels.clear()
        self._gfxStopped = True
        self._gfxPduQueue.clear()
        if self._gfxQueue is not None:
            self._gfxQueue.put(None)

    def _gfxQueueDepth(self):
        """Frames received but not decoded yet (FRAME_ACKNOWLEDGE.queueDepth)."""
        if self._gfxThreadEnabled:
            return max(0, self._gfxBacklog)
        return sum(1 for pdu in self._gfxPduQueue if pdu[0] == RDPGFX_CMDID_ENDFRAME)

    # ---- parsing ----

    def _parseRdpgfxSegment(self, data):
        """Decompress a segment and split it into (cmdId, flags, payload) PDUs."""
        raw = self._unwrapSegmentedData(data)
        if raw is None:
            return []
        pdus = []
        offset = 0
        while offset + 8 <= len(raw):
            cmdId, flags, pduLen = struct.unpack_from('<HHI', raw, offset)
            if pduLen < 8 or offset + pduLen > len(raw):
                break
            payload = bytes(raw[offset + 8:offset + pduLen])
            pdus.append((cmdId, flags, payload))
            offset += pduLen
        return pdus

    def _processRdpgfxPduSafe(self, cmdId, flags, payload):
        try:
            self._processRdpgfxPdu(cmdId, flags, payload)
        except Exception as e:
            log.warning("RDPGFX: error in %s: %r" %
                        (_RDPGFX_CMDID_NAMES.get(cmdId, "0x%04x" % cmdId), e))
        # drawing outside of START_FRAME/END_FRAME is shown at once
        # (FreeRDP gdi_interFrameUpdate)
        if not self._inGfxFrame and self._invalidRects:
            try:
                self._updateSurfaces()
            except Exception as e:
                log.warning("RDPGFX: surface update error: %r" % (e,))

    # ---- inline mode (RDPY_GFX_THREAD=0) ----

    def _processRdpgfxStreamSync(self, data):
        """Decode on the network thread, in strict arrival order.

        PDUs go through a FIFO: the queue yields to the reactor between
        frames (so audio packets are still handled) and data arriving
        meanwhile is appended behind the frames not decoded yet, never
        decoded before them.
        """
        self._gfxPduQueue.extend(self._parseRdpgfxSegment(data))
        if not self._gfxDraining:
            self._drainGfxPduQueue()

    def _drainGfxPduQueue(self):
        self._gfxDraining = True
        q = self._gfxPduQueue
        while q:
            cmdId, flags, payload = q.popleft()
            self._processRdpgfxPduSafe(cmdId, flags, payload)
            if cmdId == RDPGFX_CMDID_ENDFRAME and q:
                from twisted.internet import reactor
                reactor.callLater(0, self._drainGfxPduQueue)
                return
        self._gfxDraining = False

    def _processRdpgfxPdu(self, cmdId, flags, payload):
        """Dispatch a single RDPGFX PDU."""
        name = _RDPGFX_CMDID_NAMES.get(cmdId, "UNKNOWN(0x%04x)" % cmdId)

        if cmdId == RDPGFX_CMDID_CAPSCONFIRM:
            self._onCapsConfirm(payload)
        elif cmdId == RDPGFX_CMDID_RESETGRAPHICS:
            self._onResetGraphics(payload)
        elif cmdId == RDPGFX_CMDID_CREATESURFACE:
            self._onCreateSurface(payload)
        elif cmdId == RDPGFX_CMDID_DELETESURFACE:
            self._onDeleteSurface(payload)
        elif cmdId == RDPGFX_CMDID_MAPSURFACETOOUTPUT:
            self._onMapSurfaceToOutput(payload)
        elif cmdId == RDPGFX_CMDID_STARTFRAME:
            self._onStartFrame(payload)
        elif cmdId == RDPGFX_CMDID_ENDFRAME:
            self._onEndFrame(payload)
        elif cmdId == RDPGFX_CMDID_WIRETOSURFACE_1:
            self._onWireToSurface1(payload)
        elif cmdId == RDPGFX_CMDID_WIRETOSURFACE_2:
            self._onWireToSurface2(payload)
        elif cmdId == RDPGFX_CMDID_SOLIDFILL:
            self._onSolidFill(payload)
        elif cmdId == RDPGFX_CMDID_SURFACETOSURFACE:
            self._onSurfaceToSurface(payload)
        elif cmdId == RDPGFX_CMDID_SURFACETOCACHE:
            self._onSurfaceToCache(payload)
        elif cmdId == RDPGFX_CMDID_CACHETOSURFACE:
            self._onCacheToSurface(payload)
        elif cmdId == RDPGFX_CMDID_EVICTCACHEENTRY:
            self._onEvictCacheEntry(payload)
        elif cmdId == RDPGFX_CMDID_DELETEENCODINGCONTEXT:
            self._onDeleteEncodingContext(payload)
        elif cmdId == RDPGFX_CMDID_CACHEIMPORTREPLY:
            log.debug("RDPGFX: CACHEIMPORTREPLY len=%d" % len(payload))
        elif cmdId == RDPGFX_CMDID_MAPSURFACETOWINDOW:
            log.debug("RDPGFX: MAPSURFACETOWINDOW len=%d" % len(payload))
        elif cmdId == RDPGFX_CMDID_MAPSURFACETOSCALEDOUTPUT:
            self._onMapSurfaceToScaledOutput(payload)
        else:
            log.debug("RDPGFX: %s len=%d" % (name, len(payload)))

    def _onCapsConfirm(self, payload):
        if len(payload) >= 12:
            version = struct.unpack_from('<I', payload, 0)[0]
            dataLen = struct.unpack_from('<I', payload, 4)[0]
            flags = struct.unpack_from('<I', payload, 8)[0] if dataLen >= 4 else 0
            log.debug("RDPGFX: CAPS_CONFIRM version=0x%08x flags=0x%08x" % (version, flags))
            self._gfxVersion = version
        else:
            log.debug("RDPGFX: CAPS_CONFIRM (short payload)")
        self._gfxConfirmed = True
        # Reset frame counter on CAPS_CONFIRM (matching FreeRDP behaviour).
        # NOTE: do NOT reset on RESET_GRAPHICS — the server's frame IDs
        # continue incrementing across resets, so totalFramesDecoded must too.
        self._totalFramesDecoded = 0
        if self._capsConfirmCallback:
            self._reactorCall(self._capsConfirmCallback)

    def _onResetGraphics(self, payload):
        """RESET_GRAPHICS: server tells client the desktop dimensions."""
        if len(payload) < 12:
            return
        width = struct.unpack_from('<I', payload, 0)[0]
        height = struct.unpack_from('<I', payload, 4)[0]
        monitorCount = struct.unpack_from('<I', payload, 8)[0]
        self._resetWidth = width
        self._resetHeight = height
        callback = getattr(self, '_desktopSizeCallback', None)
        if callback is not None:
            try:
                callback(width, height)  # the remote resolution (smart sizing scales to it)
            except Exception as e:
                log.debug("desktop size callback failed: %s" % e)
        # FreeRDP gdi_ResetGraphics: surfaces, mappings and the bitmap cache
        # stay valid; surfaces are cleared and their pending updates dropped.
        for surfBuf in self._surfaceData.values():
            surfBuf.fill(0xFF)
        self._invalidRects.clear()
        # Reset RFX Progressive decoder state
        self._rfxDecoder.reset()
        # Reset ClearCodec caches (matches grdp's onResetGraphics)
        self._ccVBarStorage = [None] * 32768
        self._ccShortVBarStorage = [None] * 16384
        self._ccGlyphStorage.clear()
        self._ccVBarCursor = 0
        self._ccShortVBarCursor = 0
        # Reset H.264 decoder state (matches grdp's onResetGraphics)
        self._closeAvcDecoders()
        # Reset AVC freeze-recovery timing (fresh start after graphics reset)
        self._avcLastSuccessTime = 0.0
        self._avcLastRefreshTime = 0.0
        self._avcHardResetTime = 0.0
        self._avcDecoderBrokenNotified = False
        self._avcSoftResetCount = 0
        self._lastLC2RecvTime = 0.0
        self._auxDecoderBrokenTime = 0.0
        # Do NOT reset _totalFramesDecoded here — the server's frame IDs
        # continue across RESET_GRAPHICS, so our cumulative count must too.
        # FreeRDP only resets on CAPS_CONFIRM, not RESET_GRAPHICS.
        log.debug("RDPGFX: RESET_GRAPHICS %dx%d monitors=%d (totalFramesDecoded=%d)" %
                  (width, height, monitorCount, self._totalFramesDecoded))

    def _onCreateSurface(self, payload):
        """CREATE_SURFACE: surfaceId(2) + width(2) + height(2) + pixelFormat(1)"""
        if len(payload) < 7:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        width = struct.unpack_from('<H', payload, 2)[0]
        height = struct.unpack_from('<H', payload, 4)[0]
        pixelFormat = payload[6]
        self._surfaces[surfaceId] = (width, height, pixelFormat)
        self._surfaceData[surfaceId] = np.zeros(height * width * 4, dtype=np.uint8)
        self._invalidRects.pop(surfaceId, None)
        self._surfaceOutputMap.pop(surfaceId, None)
        self._closeAvcDecoders(surfaceId)
        log.debug("RDPGFX: CREATE_SURFACE id=%d %dx%d fmt=0x%02x" %
                  (surfaceId, width, height, pixelFormat))

    def _onDeleteSurface(self, payload):
        if len(payload) < 2:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        self._surfaces.pop(surfaceId, None)
        self._surfaceData.pop(surfaceId, None)
        self._surfaceOutputMap.pop(surfaceId, None)
        self._invalidRects.pop(surfaceId, None)
        self._closeAvcDecoders(surfaceId)
        log.debug("RDPGFX: DELETE_SURFACE id=%d" % surfaceId)

    def _onMapSurfaceToOutput(self, payload):
        """MAP_SURFACE_TO_OUTPUT: surfaceId(2) + reserved(2) + outputOriginX(4) + outputOriginY(4)"""
        if len(payload) < 12:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        outputOriginX = struct.unpack_from('<I', payload, 4)[0]
        outputOriginY = struct.unpack_from('<I', payload, 8)[0]
        self._surfaceOutputMap[surfaceId] = (outputOriginX, outputOriginY)
        self._invalidRects.pop(surfaceId, None)
        log.debug("RDPGFX: MAP_SURFACE_TO_OUTPUT id=%d -> (%d,%d)" %
                  (surfaceId, outputOriginX, outputOriginY))

    def _onStartFrame(self, payload):
        """START_FRAME: timestamp(4) + frameId(4)"""
        if len(payload) < 8:
            return
        timestamp = struct.unpack_from('<I', payload, 0)[0]
        frameId = struct.unpack_from('<I', payload, 4)[0]
        self._currentFrameId = frameId
        self._inGfxFrame = True
        log.debug("RDPGFX: START_FRAME id=%d ts=%d" % (frameId, timestamp))

    def _onEndFrame(self, payload):
        """END_FRAME: frameId(4). Send FRAME_ACKNOWLEDGE."""
        if len(payload) < 4:
            return
        frameId = struct.unpack_from('<I', payload, 0)[0]
        self._totalFramesDecoded += 1
        self._lastFrameTime = monotonic()
        log.debug("RDPGFX: END_FRAME id=%d (total=%d)" %
                  (frameId, self._totalFramesDecoded))
        # present the whole frame at once (FreeRDP gdi_EndFrame)
        self._updateSurfaces()
        self._inGfxFrame = False
        self._sendFrameAcknowledge(frameId)

    def _onWireToSurface1(self, payload):
        """WIRE_TO_SURFACE_1: surfaceId(2) + codecId(2) + pixelFormat(1) + destRect(8) + bitmapData"""
        if len(payload) < 17:
            return
        surfaceId, codecId = struct.unpack_from('<HH', payload, 0)
        pixelFormat = payload[4]
        left, top, right, bottom = struct.unpack_from('<HHHH', payload, 5)
        bitmapDataLen, = struct.unpack_from('<I', payload, 13)
        bitmapData = payload[17:17 + bitmapDataLen]

        width = right - left
        height = bottom - top

        if log._is_debug:
            log.debug("RDPGFX: WTS1 surfId=%d codecId=0x%04X %dx%d bmpLen=%d" %
                      (surfaceId, codecId, width, height, len(bitmapData)))

        # CaVideo is heavy (RFX tile decode)
        if codecId == RDPGFX_CODECID_CAVIDEO:
            self._renderCaVideo(surfaceId, left, top, width, height, bitmapData)
        elif codecId == RDPGFX_CODECID_UNCOMPRESSED:
            self._renderUncompressed(surfaceId, left, top, width, height,
                                     pixelFormat, bitmapData)
        elif codecId in (RDPGFX_CODECID_PLANAR, RDPGFX_CODECID_PLANAR_V2):
            self._renderPlanar(surfaceId, left, top, width, height, bitmapData)
        elif codecId == RDPGFX_CODECID_CAPROGRESSIVE:
            surfInfo = self._surfaces.get(surfaceId)
            surfBuf = self._surfaceData.get(surfaceId)
            if surfInfo is None or surfBuf is None:
                log.warning("RDPGFX: WTS1 unknown surface %d" % surfaceId)
                return
            w, h, fmt = surfInfo
            try:
                rects = self._rfxDecoder.decode(bitmapData, surfBuf, w, h)
                self._deliverSurfaceBitmap(surfaceId, rects)
            except Exception as e:
                log.warning("RDPGFX: Progressive decode error (WTS1): %s" % str(e))
        elif codecId == RDPGFX_CODECID_CLEARCODEC:
            self._renderClearCodec(surfaceId, left, top, width, height,
                                   pixelFormat, bitmapData)
        elif codecId == RDPGFX_CODECID_ALPHA:
            log.debug("RDPGFX: Alpha codec not yet supported, %d bytes" % len(bitmapData))
        elif codecId == RDPGFX_CODECID_AVC420:
            self._renderAvc420(surfaceId, left, top, width, height, bitmapData)
        elif codecId in (RDPGFX_CODECID_AVC444, RDPGFX_CODECID_AVC444v2):
            self._renderAvc444(surfaceId, left, top, width, height, bitmapData)
        else:
            log.debug("RDPGFX: unsupported codec 0x%04x, %d bytes" % (codecId, len(bitmapData)))

    def _onWireToSurface2(self, payload):
        """WIRE_TO_SURFACE_2: surfaceId(2) + codecId(2) + codecContextId(4) + pixelFormat(1) + bitmapDataLen(4) + bitmapData"""
        if len(payload) < 13:
            return
        surfaceId, codecId = struct.unpack_from('<HH', payload, 0)
        codecContextId, = struct.unpack_from('<I', payload, 4)
        pixelFormat = payload[8]
        bitmapDataLen, = struct.unpack_from('<I', payload, 9)
        bitmapData = payload[13:13 + bitmapDataLen]

        surfInfo = self._surfaces.get(surfaceId)
        w, h = (surfInfo[0], surfInfo[1]) if surfInfo else (0, 0)
        if log._is_debug:
            log.debug("RDPGFX: WTS2 surfId=%d codecId=0x%04X %dx%d bmpLen=%d" %
                      (surfaceId, codecId, w, h, len(bitmapData)))

        if codecId == RDPGFX_CODECID_CAPROGRESSIVE:
            surfBuf = self._surfaceData.get(surfaceId)
            if surfInfo is None or surfBuf is None:
                log.warning("RDPGFX: WIRE_TO_SURFACE_2 unknown surface %d" % surfaceId)
                return
            w, h, fmt = surfInfo
            try:
                rects = self._rfxDecoder.decode(bitmapData, surfBuf, w, h)
                self._deliverSurfaceBitmap(surfaceId, rects)
            except Exception as e:
                log.warning("RDPGFX: Progressive decode error: %s" % str(e))
        elif codecId == RDPGFX_CODECID_CAVIDEO:
            surfBuf = self._surfaceData.get(surfaceId)
            if surfInfo is None or surfBuf is None:
                log.warning("RDPGFX: WTS2 unknown surface %d" % surfaceId)
                return
            w, h, _ = surfInfo
            try:
                rects = self._rfxTileDecoder.decode(bitmapData, 0, 0, surfBuf, w, h)
                self._deliverSurfaceBitmap(surfaceId, rects)
            except Exception as e:
                log.warning("RDPGFX: CaVideo RFX decode error: %s" % str(e))
        elif codecId == RDPGFX_CODECID_AVC420:
            if surfInfo is None:
                log.warning("RDPGFX: WTS2 unknown surface %d" % surfaceId)
                return
            w, h, _ = surfInfo
            self._renderAvc420(surfaceId, 0, 0, w, h, bitmapData)
        elif codecId in (RDPGFX_CODECID_AVC444, RDPGFX_CODECID_AVC444v2):
            if surfInfo is None:
                log.warning("RDPGFX: WTS2 unknown surface %d" % surfaceId)
                return
            w, h, _ = surfInfo
            self._renderAvc444(surfaceId, 0, 0, w, h, bitmapData)
        elif codecId == RDPGFX_CODECID_CLEARCODEC:
            if surfInfo is None:
                log.warning("RDPGFX: WTS2 unknown surface %d" % surfaceId)
                return
            w, h, _ = surfInfo
            self._renderClearCodec(surfaceId, 0, 0, w, h, pixelFormat, bitmapData)
        else:
            log.debug("RDPGFX: WTS2 unsupported codec 0x%04x" % codecId)

    def _onSolidFill(self, payload):
        """SOLID_FILL: surfaceId(2) + fillPixel(4) + fillRectCount(2) + fillRects"""
        if len(payload) < 8:
            return
        surfaceId, = struct.unpack_from('<H', payload, 0)
        fillPixelB = payload[2]
        fillPixelG = payload[3]
        fillPixelR = payload[4]
        fillPixelA = payload[5]
        rectCount, = struct.unpack_from('<H', payload, 6)
        log.debug("RDPGFX: SOLID_FILL surf=%d color=(%d,%d,%d,%d) rects=%d" %
                  (surfaceId, fillPixelR, fillPixelG, fillPixelB, fillPixelA, rectCount))

        surfInfo = self._surfaces.get(surfaceId)
        surfBuf = self._surfaceData.get(surfaceId)
        if surfInfo is None or surfBuf is None:
            return
        sw, sh = surfInfo[0], surfInfo[1]
        surf = surfBuf.reshape(sh, sw, 4)
        # alpha is ignored (MS-RDPEGFX 3.3.5.4, FreeRDP gdi_SolidFill)
        color = np.array([fillPixelB, fillPixelG, fillPixelR, 0xFF], dtype=np.uint8)
        offset = 8
        for i in range(rectCount):
            if offset + 8 > len(payload):
                break
            left, top, right, bottom = struct.unpack_from('<HHHH', payload, offset)
            offset += 8
            # intersect with the surface (FreeRDP intersect_rect)
            right, bottom = min(right, sw), min(bottom, sh)
            if left >= right or top >= bottom:
                continue
            surf[top:bottom, left:right] = color
            self._invalidate(surfaceId, left, top, right - left, bottom - top)

    def _onSurfaceToSurface(self, payload):
        """SURFACE_TO_SURFACE: srcSurfaceId(2) + destSurfaceId(2) + rcSrc(8) + destPtsCount(2) + destPts"""
        if len(payload) < 14:
            return
        srcId, dstId = struct.unpack_from('<HH', payload, 0)
        left, top, right, bottom = struct.unpack_from('<HHHH', payload, 4)
        destPtsCount, = struct.unpack_from('<H', payload, 12)

        srcInfo = self._surfaces.get(srcId)
        dstInfo = self._surfaces.get(dstId)
        srcBuf = self._surfaceData.get(srcId)
        dstBuf = self._surfaceData.get(dstId)
        if srcInfo is None or dstInfo is None or srcBuf is None or dstBuf is None:
            return
        srcW, srcH = srcInfo[0], srcInfo[1]
        dstW, dstH = dstInfo[0], dstInfo[1]
        # the source rect must lie inside the source surface (FreeRDP is_rect_valid)
        if left > right or top > bottom or right > srcW or bottom > srcH:
            log.debug("RDPGFX: SURFACE_TO_SURFACE source rect outside surface %d" % srcId)
            return
        w = right - left
        h = bottom - top
        if w <= 0 or h <= 0:
            return
        # copy first: source and destination may overlap (scrolling)
        block = srcBuf.reshape(srcH, srcW * 4)[top:bottom, left * 4:right * 4].copy()
        dst = dstBuf.reshape(dstH, dstW * 4)
        offset = 14
        for _ in range(destPtsCount):
            if offset + 4 > len(payload):
                break
            destX, destY = struct.unpack_from('<HH', payload, offset)
            offset += 4
            cw, ch = min(w, dstW - destX), min(h, dstH - destY)
            if cw <= 0 or ch <= 0:
                continue
            dst[destY:destY + ch, destX * 4:(destX + cw) * 4] = block[:ch, :cw * 4]
            self._invalidate(dstId, destX, destY, cw, ch)

    def _onMapSurfaceToScaledOutput(self, payload):
        """MAPSURFACETOSCALEDOUTPUT: surfaceId(2) + reserved(2) + outputOriginX(4) + outputOriginY(4) + targetWidth(4) + targetHeight(4)"""
        if len(payload) < 20:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        outputOriginX = struct.unpack_from('<I', payload, 4)[0]
        outputOriginY = struct.unpack_from('<I', payload, 8)[0]
        self._surfaceOutputMap[surfaceId] = (outputOriginX, outputOriginY)
        self._invalidRects.pop(surfaceId, None)

    def _renderCaVideo(self, surfaceId, left, top, width, height, data):
        """Decode CaVideo (0x0003) RFX tile data onto surface (WTS1)."""
        surfInfo = self._surfaces.get(surfaceId)
        surfBuf = self._surfaceData.get(surfaceId)
        if surfInfo is None or surfBuf is None:
            log.warning("RDPGFX: CaVideo WTS1 unknown surface %d" % surfaceId)
            return
        sw, sh, _ = surfInfo
        try:
            rects = self._rfxTileDecoder.decode(data, left, top, surfBuf, sw, sh)
        except Exception as e:
            log.warning("RDPGFX: CaVideo RFX decode error: %s" % e)
            return
        self._deliverSurfaceBitmap(surfaceId, rects)

    def _getAvcDecoder(self, surfaceId=None):
        """Return the H.264/AVC decoder of a surface (created on first use)."""
        decoder = self._avcDecoders.get(surfaceId)
        if decoder is None:
            if not avc_module.is_available():
                log.warning("RDPGFX: AVC codec received but PyAV not available")
                return None
            try:
                decoder = avc_module.AvcDecoder()
                decoder.on_hard_reset = self._onAvcHardReset
                log.debug("RDPGFX: AVC decoder for surface %s initialized (hardware=%s)" %
                          (surfaceId, decoder.is_hardware))
            except Exception as e:
                log.warning("RDPGFX: failed to initialize AVC decoder: %s" % e)
                return None
            self._avcDecoders[surfaceId] = decoder
        self._avcDecoder = decoder
        return decoder

    def _closeAvcDecoders(self, surfaceId=None):
        """Close the decoder of one surface, or all of them (surfaceId None)."""
        ids = list(self._avcDecoders) if surfaceId is None else [surfaceId]
        for sid in ids:
            decoder = self._avcDecoders.pop(sid, None)
            if decoder is None:
                continue
            try:
                decoder.close()
            except Exception:
                pass
            if decoder is self._avcDecoder:
                self._avcDecoder = None
        if surfaceId is None:
            self._avcDecoder = None

    @staticmethod
    def _avc420Regions(data):
        """Region rects (left, top, right, bottom) of an AVC420 metablock
        (MS-RDPEGFX 2.2.4.4.1), in surface coordinates."""
        if len(data) < 4:
            return []
        count = struct.unpack_from('<I', data, 0)[0]
        if count == 0 or len(data) < 4 + count * 10:
            return []
        raw = struct.unpack_from('<%dH' % (count * 4), data, 4)
        return [raw[i:i + 4] for i in range(0, count * 4, 4)]

    @classmethod
    def _avc444Regions(cls, data):
        """Region rects of both streams of an AVC444(v2) bitmap stream."""
        if len(data) < 4:
            return []
        info = struct.unpack_from('<I', data, 0)[0]
        lc = (info >> 30) & 0x3
        size1 = info & 0x3FFFFFFF
        stream1 = data[4:4 + size1]
        regions = cls._avc420Regions(stream1)
        if lc == 0 and len(data) > 4 + size1:
            regions = regions + cls._avc420Regions(data[4 + size1:])
        return regions

    def _copyAvcRegions(self, surfaceId, frame, regions):
        """Copy the updated regions of a decoded H.264 frame into the surface.

        FreeRDP avc420_decompress: the frame is in surface coordinates and
        only the region rects of the metablock carry new content; the rest
        of the frame must not touch the surface.
        """
        surfInfo = self._surfaces.get(surfaceId)
        surfBuf = self._surfaceData.get(surfaceId)
        if surfInfo is None or surfBuf is None or frame is None:
            return
        sw, sh = surfInfo[0], surfInfo[1]
        if frame.ndim == 3:
            fh, fw = frame.shape[0], frame.shape[1]
            src = frame.reshape(fh, fw * 4)
        else:
            fh, fw = sh, sw
            src = frame.reshape(fh, fw * 4)
        dst = surfBuf.reshape(sh, sw * 4)
        maxW, maxH = min(sw, fw), min(sh, fh)
        for left, top, right, bottom in regions:
            x0, y0 = max(0, left), max(0, top)
            x1, y1 = min(maxW, right), min(maxH, bottom)
            if x1 <= x0 or y1 <= y0:
                continue
            dst[y0:y1, x0 * 4:x1 * 4] = src[y0:y1, x0 * 4:x1 * 4]
            dst[y0:y1, x0 * 4 + 3:x1 * 4:4] = 0xFF
            self._invalidate(surfaceId, x0, y0, x1 - x0, y1 - y0)

    # Soft-reset limit before escalating to a full reconnect.
    # Mirrors grdp's softResetLimit=5.
    _AVC_SOFT_RESET_LIMIT = 5

    def _onAvcHardReset(self):
        """Called by AvcDecoder immediately after a hard reset.

        Idempotent: only the *first* hard reset starts the reconnect countdown.
        Subsequent resets (e.g. aux decoder resetting shortly after the main
        decoder reset) do NOT restart the clock — mirrors grdp's
        startAuxDecoderBrokenTimer() which is a no-op when already running.

        After _AVC_SOFT_RESET_LIMIT resets without a successful decode,
        triggers an immediate reconnect — mirrors grdp's maybeNotifyDecoderBroken
        escalating after softResetLimit soft resets.
        """
        self._avcSoftResetCount += 1
        log.debug("RDPGFX: AVC soft reset #%d/%d — requesting immediate server refresh"
                  % (self._avcSoftResetCount, self._AVC_SOFT_RESET_LIMIT))
        self._avcLastRefreshTime = 0.0  # clear cooldown so request goes through
        if self._avcHardResetTime == 0.0:
            # Start the reconnect countdown from the FIRST hard reset only.
            self._avcHardResetTime = monotonic()

        # After reaching the soft-reset limit, reconnect immediately rather
        # than waiting for _AVC_RECONNECT_TIMEOUT.  Mirrors grdp escalating
        # from soft-reset to onDecoderBroken after softResetLimit attempts.
        if self._avcSoftResetCount >= self._AVC_SOFT_RESET_LIMIT and not self._avcDecoderBrokenNotified:
            self._avcDecoderBrokenNotified = True
            log.warning("RDPGFX: AVC soft reset limit (%d) reached — triggering reconnect"
                        % self._AVC_SOFT_RESET_LIMIT)
            if self._decoderBrokenCallback is not None:
                self._reactorCall(self._decoderBrokenCallback)
            return

        if self._requestRefreshCallback is not None:
            self._reactorCall(self._requestRefreshCallback)
            self._avcLastRefreshTime = monotonic()

    # Seconds of no AVC output before requesting an IDR from the server.
    # 7 s gives the HW decoder (VideoToolbox) enough headroom to process an
    # IDR/SPS before we declare a stall — mirrors grdp's avcHWReadyFreezeThreshold.
    _AVC_FREEZE_THRESHOLD = 7.0
    # Minimum seconds between consecutive IDR refresh requests (cooldown).
    # Kept low (5 s) so stall recovery is not blocked for too long after a
    # previous request.  The old value of 15 s allowed null_count to accumulate
    # from 30 to 104 (log confirmed) before a second request could go out.
    _AVC_REFRESH_COOLDOWN = 5.0
    # Also trigger recovery when this many consecutive null frames accumulate.
    # NOTE: _AVC_FREEZE_MIN_SECS was intentionally removed: it caused the count
    # trigger to miss when LC=2 (aux decoder) successes kept resetting
    # _avcLastSuccessTime, masking the main decoder's null run.  The startup
    # guard (_avcLastSuccessTime == 0.0 → return) already prevents false
    # positives during initialisation.
    _AVC_NULL_FRAME_THRESHOLD = 30
    # (Unused constant kept for reference; fast keyframe-request path was removed
    # because repeated SupressOutputDataPDU calls disconnect some servers.)
    _AVC_KEYFRAME_REQUEST_INTERVAL = 2.0
    # Seconds after a hard reset before giving up and triggering a full reconnect.
    # Gives the server time to respond to the post-reset ForceRefresh.
    # When the server is actively sending LC=2 frames (server_active), this is
    # halved: an active server that ignores IDR requests is definitively stuck.
    _AVC_RECONNECT_TIMEOUT = 10.0

    def _renderAvc420(self, surfaceId, left, top, width, height, data):
        """Decode AVC420 (H.264 YUV420) and copy its region rects into the surface."""
        surfInfo = self._surfaces.get(surfaceId)
        if surfInfo is None:
            log.warning("RDPGFX: AVC420 unknown surface %d" % surfaceId)
            return
        decoder = self._getAvcDecoder(surfaceId)
        if decoder is None:
            return
        try:
            # decoded frame in surface coordinates (FreeRDP: h264 context
            # sized to the surface), destRect only bounds the update
            bgra_arr = decoder.decode_avc420_arr(data, surfInfo[0], surfInfo[1])
            if bgra_arr is None:
                self._onAvcNoOutput()
                return
            self._avcLastSuccessTime = monotonic()
            self._avcHardResetTime = 0.0  # decoder recovered; cancel reconnect countdown
            self._avcSoftResetCount = 0  # reset soft-reset counter on success
            self._copyAvcRegions(surfaceId, bgra_arr, self._avc420Regions(data))
        except Exception as e:
            log.warning("RDPGFX: AVC420 decode error: %s" % e)
            self._onAvcNoOutput()

    def _renderAvc444(self, surfaceId, left, top, width, height, data):
        """Decode and render AVC444/AVC444v2 (H.264 YUV444) bitmap data."""
        # Record LC=2 arrival unconditionally (even when aux decoder can't decode it).
        # Used to distinguish an active-LC=2-only server from a truly idle/AVC420 server.
        # Mirrors grdp's lastLC2RecvTime tracking in decodeAVC444LC2.
        if len(data) >= 4:
            _lc = (struct.unpack_from('<I', data, 0)[0] >> 30) & 0x03
            if _lc == 2:
                self._lastLC2RecvTime = monotonic()
        surfInfo = self._surfaces.get(surfaceId)
        if surfInfo is None:
            log.warning("RDPGFX: AVC444 unknown surface %d" % surfaceId)
            return
        decoder = self._getAvcDecoder(surfaceId)
        if decoder is None:
            return
        try:
            bgra_arr = decoder.decode_avc444_arr(data, surfInfo[0], surfInfo[1])
            if bgra_arr is None:
                # Real decode failure — H.264 reference chain may be broken.
                self._onAvcNoOutput()
                return
            if isinstance(bgra_arr, bytes):
                # b"" sentinel: LC=2 frame not yet decodable (aux decoder not
                # primed, no cached luma, or Y cache stale — main decoder likely
                # stalling).  Do NOT update _avcLastSuccessTime: the luma decoder
                # may be the source of a freeze, and LC=2 frames arriving
                # independently should not mask it.
                #
                # When the Y cache is stale the main decoder is stalling; request
                # a keyframe at _AVC_KEYFRAME_REQUEST_INTERVAL rate so the server
                # delivers a fresh IDR.  Mirrors grdp's decodeAVC444LC2 staleness
                # path which calls onKeyframeRequest with its own rate-limiter.
                decoder = self._avcDecoder
                if (decoder is not None and decoder._avc444_y is not None
                        and decoder._avc444_y_updated_at > 0.0):
                    now = monotonic()
                    age = now - decoder._avc444_y_updated_at
                    if (age > decoder._AVC444_Y_STALENESS
                            and now - self._avcLastRefreshTime >= self._AVC_KEYFRAME_REQUEST_INTERVAL):
                        log.debug("RDPGFX: AVC444 LC=2 Y cache stale (%.0fms), requesting keyframe"
                                  % (age * 1000))
                        self._avcLastRefreshTime = now
                        if self._requestRefreshCallback is not None:
                            self._reactorCall(self._requestRefreshCallback)
                #
                # However, if the main decoder is working (recent _avcLastSuccessTime)
                # but LC=2 keeps returning the sentinel, the aux decoder is stuck in
                # IDR-wait — the server will never send a fresh stream2 IDR without a
                # reconnect.  Mirror grdp's auxDecoderBrokenTimer: arm a one-shot
                # _AVC_RECONNECT_TIMEOUT timer; if LC=2 is still broken when it fires,
                # trigger a full reconnect.
                if not self._avcDecoderBrokenNotified and self._avcLastSuccessTime > 0.0:
                    now = monotonic()
                    if now - self._avcLastSuccessTime < self._AVC_RECONNECT_TIMEOUT:
                        # Main decoder recently active — aux is the broken one.
                        if self._auxDecoderBrokenTime == 0.0:
                            self._auxDecoderBrokenTime = now
                        elif now - self._auxDecoderBrokenTime >= self._AVC_RECONNECT_TIMEOUT:
                            self._avcDecoderBrokenNotified = True
                            log.warning(
                                "RDPGFX: AVC444 aux decoder stuck %.0fs"
                                " (server sending LC=2 only) — triggering reconnect"
                                % (now - self._auxDecoderBrokenTime))
                            if self._decoderBrokenCallback is not None:
                                self._reactorCall(self._decoderBrokenCallback)
                    else:
                        # Main decoder also stalled; _onAvcNoOutput handles reconnect.
                        self._auxDecoderBrokenTime = 0.0
                return
            self._avcLastSuccessTime = monotonic()
            self._avcHardResetTime = 0.0  # decoder recovered; cancel reconnect countdown
            self._avcSoftResetCount = 0  # reset soft-reset counter on success
            self._auxDecoderBrokenTime = 0.0  # aux decoder working; cancel broken timer
            self._copyAvcRegions(surfaceId, bgra_arr, self._avc444Regions(data))
        except Exception as e:
            log.warning("RDPGFX: AVC444 decode error: %s" % e)
            self._onAvcNoOutput()

    def _onAvcNoOutput(self):
        """Called when an AVC frame produces no output.

        Two cases:

        1. **Keyframe-wait state** (needs_keyframe=True): the decoder is waiting
           for an IDR after a decode error or stall.  Non-IDR frames are silently
           dropped by AvcDecoder._decode_h264(); we simply return here.

        2. **VideoToolbox null-frame stall** (needs_keyframe=False): the decoder
           is silently returning null for P-frames.  This happens on macOS when
           VideoToolbox is temporarily overloaded or buffering.  We:
             a. call decoder.flush() to reset null-frame counters,
             b. call decoder.request_keyframe() to enter IDR-wait state so
                P-frames are dropped (mirrors grdp's needsKeyFrame pattern),
             c. send ONE ForceRefresh IDR request per _AVC_REFRESH_COOLDOWN.
           We do NOT hard-reset the decoder because:
             - Hard reset destroys VideoToolbox's reference-frame chain.
             - VideoToolbox null stalls are often transient and self-resolve
               without intervention (confirmed in logs: frames 10–15 stalled
               then resumed on their own).
             - If an IDR request causes the server to send an IDR, the
               decoder — still holding its full codec context — processes it
               naturally without needing recreation.
           When request_keyframe() is set, _keyframe_wait_count grows until
           _KEYFRAME_WAIT_LIMIT (900 frames ≈ 30 s) triggers _hard_reset(),
           which destroys/recreates the decoder and fires on_hard_reset for
           another ForceRefresh — a full recovery fallback.
        """
        # While waiting for IDR (after InvalidDataError or stall detection),
        # frames are silently dropped — this is not a new freeze state.
        # However, the server may not send an IDR unless we keep asking.
        # Mirrors grdp's pattern: re-send ForceRefresh every
        # _AVC_KEYFRAME_REQUEST_INTERVAL seconds until an IDR arrives.
        if self._avcDecoder and self._avcDecoder.needs_keyframe:
            now = monotonic()
            # If a hard reset has fired but the server still hasn't sent an IDR
            # after the reconnect timeout, trigger a full reconnect.
            # This handles the "server ignores ForceRefresh" deadlock.
            #
            # Use a shorter timeout when the server is actively sending LC=2
            # frames: if it is sending data but not responding to IDR requests,
            # it is definitively stuck and a full reconnect is the only fix.
            # "Active" = LC=2 received within the last 2 s.
            # Quieter servers (static screen or no LC=2) get the full timeout.
            if self._avcHardResetTime > 0.0 and not self._avcDecoderBrokenNotified:
                lc2_age = (now - self._lastLC2RecvTime
                           if self._lastLC2RecvTime > 0.0 else float('inf'))
                server_active = lc2_age < 2.0
                reconnect_timeout = (self._AVC_RECONNECT_TIMEOUT / 2
                                     if server_active
                                     else self._AVC_RECONNECT_TIMEOUT)
                if now - self._avcHardResetTime >= reconnect_timeout:
                    self._avcDecoderBrokenNotified = True
                    log.warning(
                        "RDPGFX: AVC still frozen %.0fs after hard reset"
                        " — triggering reconnect (lastLC2=%.1fs ago%s)"
                        % (reconnect_timeout,
                           lc2_age if lc2_age != float('inf') else -1.0,
                           ", server active" if server_active else ""))
                    if self._decoderBrokenCallback is not None:
                        self._reactorCall(self._decoderBrokenCallback)
                    return
            if now - self._avcLastRefreshTime >= self._AVC_KEYFRAME_REQUEST_INTERVAL:
                self._avcLastRefreshTime = now
                log.debug("RDPGFX: AVC: still waiting for IDR, re-requesting refresh")
                if self._requestRefreshCallback is not None:
                    self._reactorCall(self._requestRefreshCallback)
            return

        if self._avcLastSuccessTime == 0.0:
            return  # No successful decode yet; normal during startup

        now = monotonic()
        frozen_for = now - self._avcLastSuccessTime

        # Don't trigger stall recovery when the server isn't actively sending
        # video packets — a static screen produces no frames but the decoder is
        # healthy.  Mirrors grdp's LastReceiveTime() guard in maybeTriggerInputStall.
        if self._avcDecoder is not None:
            recv_time = self._avcDecoder.last_receive_time
            if recv_time == 0.0 or now - recv_time >= self._AVC_FREEZE_THRESHOLD:
                return

        null_count = self._avcDecoder.null_count if self._avcDecoder else 0
        count_triggered = null_count >= self._AVC_NULL_FRAME_THRESHOLD

        if not count_triggered:
            return

        if now - self._avcLastRefreshTime < self._AVC_REFRESH_COOLDOWN:
            return  # Cooldown: avoid spamming the server

        trigger_reason = 'count(%d)' % null_count
        log.debug("RDPGFX: AVC stall [%s], soft-resetting decoder and requesting refresh"
                  % trigger_reason)
        self._avcLastRefreshTime = now

        if self._avcDecoder is not None:
            # Soft-reset: destroy and recreate the decoder context immediately.
            # Mirrors grdp's maybeNotifyDecoderBroken() soft reset path: rather
            # than waiting 900 frames (~30 s) for the internal hard-reset threshold,
            # we reset as soon as a stall is detected.  This unsticks VideoToolbox
            # and gives the server a fresh context to send an IDR into.
            # soft_reset() internally calls _hard_reset() which fires on_hard_reset
            # → _onAvcHardReset() → increments _avcSoftResetCount, sets
            # _avcHardResetTime (idempotent), and issues a ForceRefresh.
            self._avcDecoder.soft_reset()

    def _renderUncompressed(self, surfaceId, left, top, width, height, pixelFormat, data):
        """Render uncompressed XRGB_8888/ARGB_8888 bitmap data."""
        if width <= 0 or height <= 0:
            return

        bpp = 32
        stride = width * 4
        expected = stride * height
        if len(data) < expected:
            log.warning("RDPGFX: uncompressed data too short: %d < %d" % (len(data), expected))
            return

        # RDPGFX XRGB_8888: pixels are [B, G, R, X] (same as RDP BGRX)
        # Force alpha to 0xFF for Qt Format_RGB32 compatibility
        raw = bytearray(data[:expected])
        raw[3::4] = b'\xff' * (expected // 4)

        self._blitToSurface(surfaceId, left, top, width, height, raw)

    # ---------------------------------------------------------------
    # Planar codec (0x0004) — ported from grdp decodePlanar
    # ---------------------------------------------------------------

    def _renderPlanar(self, surfaceId, left, top, width, height, data):
        """Decode Planar bitmap (codec 0x0004) per MS-RDPEGDI 2.2.2.5."""
        if width <= 0 or height <= 0:
            return
        out = self._decodePlanar(data, width, height)
        self._blitToSurface(surfaceId, left, top, width, height, out)

    def _decodePlanar(self, data, w, h):
        """Decode one Planar-codec bitmap; returns BGRA bytearray (w*h*4)."""
        plane_size = w * h
        out = bytearray(plane_size * 4)
        if len(data) < 1:
            return out

        header = data[0]
        cll = header & 0x07
        cs = bool(header & 0x08)
        rle = bool(header & 0x10)
        no_alpha = bool(header & 0x20)
        offset = 1

        if cs:
            plane1_size = ((w + 1) // 2) * ((h + 1) // 2)
            plane2_size = plane1_size
        else:
            plane1_size = plane_size
            plane2_size = plane_size

        if not rle:
            if not no_alpha:
                plane0, offset = self._readRawPlane(data, offset, plane_size)
            else:
                plane0 = None
            plane1, offset = self._readRawPlane(data, offset, plane1_size)
            plane2, offset = self._readRawPlane(data, offset, plane2_size)
            plane3, offset = self._readRawPlane(data, offset, plane_size)
        else:
            if not no_alpha:
                plane0, offset = self._decodePlaneRLE(data, offset, plane_size)
            else:
                plane0 = None
            plane1, offset = self._decodePlaneRLE(data, offset, plane1_size)
            plane2, offset = self._decodePlaneRLE(data, offset, plane2_size)
            plane3, offset = self._decodePlaneRLE(data, offset, plane_size)

        if cll == 0:
            out_arr = np.frombuffer(out, dtype=np.uint8).reshape(plane_size, 4)
            out_arr[:, 0] = np.frombuffer(plane3, dtype=np.uint8)[:plane_size]
            out_arr[:, 1] = np.frombuffer(plane2, dtype=np.uint8)[:plane_size]
            out_arr[:, 2] = np.frombuffer(plane1, dtype=np.uint8)[:plane_size]
            if not no_alpha and plane0 is not None:
                out_arr[:, 3] = np.frombuffer(plane0, dtype=np.uint8)[:plane_size]
            else:
                out_arr[:, 3] = 0xFF
        else:
            shift = cll
            p3_np = np.frombuffer(plane3, dtype=np.uint8)
            p1_np = np.frombuffer(plane1, dtype=np.int8)
            p2_np = np.frombuffer(plane2, dtype=np.int8)

            if cs:
                half_w = (w + 1) // 2
                py = np.arange(h, dtype=np.int32) // 2
                px = np.arange(w, dtype=np.int32) // 2
                ci = (py[:, np.newaxis] * half_w + px[np.newaxis, :]).ravel()
                np.clip(ci, 0, len(p1_np) - 1, out=ci)
                co_raw = p1_np[ci].astype(np.int32)
                cg_raw = p2_np[ci].astype(np.int32)
                y_val = p3_np[:plane_size].astype(np.int32)
            else:
                y_val = p3_np[:plane_size].astype(np.int32)
                co_raw = p1_np[:plane_size].astype(np.int32)
                cg_raw = p2_np[:plane_size].astype(np.int32)

            co = co_raw << shift
            cg = cg_raw << shift
            t = y_val - (cg >> 1)
            g = np.clip(t + cg, 0, 255).astype(np.uint8)
            b = t - (co >> 1)
            r = np.clip(b + co, 0, 255).astype(np.uint8)
            b = np.clip(b, 0, 255).astype(np.uint8)

            out_arr = np.frombuffer(out, dtype=np.uint8).reshape(plane_size, 4)
            out_arr[:, 0] = b
            out_arr[:, 1] = g
            out_arr[:, 2] = r
            if not no_alpha and plane0 is not None:
                out_arr[:, 3] = np.frombuffer(plane0, dtype=np.uint8)[:plane_size]
            else:
                out_arr[:, 3] = 0xFF

        return out

    def _readRawPlane(self, data, offset, size):
        plane = bytearray(size)
        end = min(offset + size, len(data))
        if offset < end:
            plane[:end - offset] = data[offset:end]
        return plane, offset + size

    def _decodePlaneRLE(self, data, offset, plane_size):
        """Decode an RDP 6.0 RLE plane per MS-RDPEGDI 2.2.2.5.1.2."""
        out = bytearray(plane_size)
        pos = 0
        dlen = len(data)
        while pos < plane_size and offset < dlen:
            ctrl = data[offset]
            offset += 1
            n_run_length = ctrl & 0x0F
            c_raw_bytes = ctrl >> 4
            if n_run_length == 15:
                while offset < dlen:
                    b = data[offset]
                    offset += 1
                    n_run_length += b
                    if b != 0xFF:
                        break
            if n_run_length > 0:
                run_fill = min(n_run_length, plane_size - pos)
                if pos > 0:
                    prev_val = out[pos - 1]
                    out[pos:pos + run_fill] = bytearray([prev_val]) * run_fill
                pos += run_fill

            if c_raw_bytes == 15:
                while offset < dlen:
                    b = data[offset]
                    offset += 1
                    c_raw_bytes += b
                    if b != 0xFF:
                        break
            actual = min(c_raw_bytes, plane_size - pos, dlen - offset)
            if actual > 0:
                out[pos:pos + actual] = data[offset:offset + actual]
                pos += actual
                offset += actual
        return out, offset

    # ---------------------------------------------------------------
    # ClearCodec (0x0008) — ported from grdp decodeClearCodec
    # ---------------------------------------------------------------

    def _renderClearCodec(self, surfaceId, left, top, width, height, pixelFormat, bitmapData):
        """Decode ClearCodec bitmap per MS-RDPEGFX 2.2.4."""
        if width <= 0 or height <= 0:
            return
        out = self._decodeClearCodec(bitmapData, width, height)
        if out is None:
            return
        self._blitToSurface(surfaceId, left, top, width, height, out)

    def _resetClearCodecVBar(self):
        self._ccVBarStorage = [None] * 32768
        self._ccShortVBarStorage = [None] * 16384
        self._ccVBarCursor = 0
        self._ccShortVBarCursor = 0

    def _decodeClearCodec(self, data, width, height):
        if len(data) < 2:
            return bytearray(width * height * 4)

        flags = data[0]
        off = 2

        if flags & 0x04:  # CLEARCODEC_FLAG_CACHE_RESET
            self._resetClearCodecVBar()

        out = bytearray(width * height * 4)
        glyph_index = 0
        has_glyph_index = False

        if flags & (0x01 | 0x02):  # CLEARCODEC_FLAG_GLYPH_INDEX | CLEARCODEC_FLAG_GLYPH_HIT
            if off + 2 <= len(data):
                glyph_index = struct.unpack_from('<H', data, off)[0]
                has_glyph_index = True
                off += 2

        if flags & 0x02:  # CLEARCODEC_FLAG_GLYPH_HIT
            if has_glyph_index:
                cached = self._ccGlyphStorage.get(glyph_index)
                if cached is not None and len(cached) == width * height * 4:
                    out[:] = cached
            return out

        if off + 12 > len(data):
            return out

        residual_len = struct.unpack_from('<I', data, off)[0]
        bands_len = struct.unpack_from('<I', data, off + 4)[0]
        subcodec_len = struct.unpack_from('<I', data, off + 8)[0]
        off += 12

        if residual_len > 0 and off + residual_len <= len(data):
            self._decodeClearCodecResidual(data[off:off + residual_len], out, width, height)
            off += residual_len
        if bands_len > 0 and off + bands_len <= len(data):
            self._decodeClearCodecBands(data[off:off + bands_len], out, width, height)
            off += bands_len
        if subcodec_len > 0 and off + subcodec_len <= len(data):
            self._decodeClearCodecSubcodec(data[off:off + subcodec_len], out, width, height)
            off += subcodec_len

        if (flags & 0x01) and has_glyph_index:  # CLEARCODEC_FLAG_GLYPH_INDEX
            self._ccGlyphStorage[glyph_index] = bytes(out)

        return out

    def _decodeClearCodecResidual(self, data, out, width, height):
        total_pixels = width * height
        pixel_index = 0
        off = 0
        dlen = len(data)
        out_view = memoryview(out)
        while off + 4 <= dlen and pixel_index < total_pixels:
            b = data[off]
            g = data[off + 1]
            r = data[off + 2]
            run_len = data[off + 3]
            off += 4
            if run_len >= 0xFF:
                if off + 2 > dlen:
                    break
                run_len = struct.unpack_from('<H', data, off)[0]
                off += 2
                if run_len >= 0xFFFF:
                    if off + 4 > dlen:
                        break
                    run_len = struct.unpack_from('<I', data, off)[0]
                    off += 4
            end = min(pixel_index + run_len, total_pixels)
            fill_len = end - pixel_index
            if fill_len > 0:
                out_view[pixel_index * 4:end * 4] = bytearray([b, g, r, 0xFF]) * fill_len
            pixel_index = end

    def _decodeClearCodecBands(self, data, out, surfW, surfH):
        off = 0
        dlen = len(data)
        out_u32 = np.frombuffer(out, dtype=np.uint32)

        while off + 11 <= dlen:
            x_start = struct.unpack_from('<H', data, off)[0]
            x_end = struct.unpack_from('<H', data, off + 2)[0]
            y_start = struct.unpack_from('<H', data, off + 4)[0]
            y_end = struct.unpack_from('<H', data, off + 6)[0]
            blue_bg = data[off + 8]
            green_bg = data[off + 9]
            red_bg = data[off + 10]
            off += 11

            if x_end < x_start or y_end < y_start:
                continue

            band_h = y_end - y_start + 1
            vbar_count = x_end - x_start + 1
            color_bkg = blue_bg | (green_bg << 8) | (red_bg << 16) | (0xFF << 24)

            for i in range(vbar_count):
                if off + 2 > dlen:
                    return
                vbar_header = struct.unpack_from('<H', data, off)[0]
                off += 2

                cur_vbar = None
                vbar_short_entry = None
                vbar_update = False
                vbar_y_on = 0
                vbar_short_pixel_count = 0

                top2 = vbar_header & 0xC000
                if top2 == 0x4000:  # SHORT_VBAR_CACHE_HIT
                    vbar_index = vbar_header & 0x3FFF
                    if vbar_index < len(self._ccShortVBarStorage):
                        vbar_short_entry = self._ccShortVBarStorage[vbar_index]
                    if off >= dlen:
                        return
                    vbar_y_on = data[off]
                    off += 1
                    if vbar_short_entry is not None:
                        vbar_short_pixel_count = len(vbar_short_entry)
                    vbar_update = True
                elif top2 == 0x0000:  # SHORT_VBAR_CACHE_MISS
                    vbar_y_on = vbar_header & 0xFF
                    vbar_y_off = (vbar_header >> 8) & 0x3F
                    if vbar_y_off >= vbar_y_on:
                        vbar_short_pixel_count = vbar_y_off - vbar_y_on
                    if off + vbar_short_pixel_count * 3 > dlen:
                        return
                    short_pixels = np.empty(vbar_short_pixel_count, dtype=np.uint32)
                    for p in range(vbar_short_pixel_count):
                        b = data[off]
                        g = data[off + 1]
                        r = data[off + 2]
                        off += 3
                        short_pixels[p] = b | (g << 8) | (r << 16) | (0xFF << 24)
                    vbar_short_entry = short_pixels
                    if self._ccShortVBarCursor < len(self._ccShortVBarStorage):
                        self._ccShortVBarStorage[self._ccShortVBarCursor] = vbar_short_entry
                    self._ccShortVBarCursor = (self._ccShortVBarCursor + 1) % len(self._ccShortVBarStorage)
                    vbar_update = True
                elif (vbar_header & 0x8000) == 0x8000:  # VBAR_CACHE_HIT
                    vbar_index = vbar_header & 0x7FFF
                    if vbar_index < len(self._ccVBarStorage):
                        cur_vbar = self._ccVBarStorage[vbar_index]
                    if cur_vbar is None or len(cur_vbar) == 0:
                        cur_vbar = np.full(band_h, color_bkg, dtype=np.uint32)
                else:
                    return

                if vbar_update:
                    vbar_pixels = np.full(band_h, color_bkg, dtype=np.uint32)
                    if vbar_short_entry is not None and vbar_short_pixel_count > 0:
                        copy_count = min(vbar_short_pixel_count, max(0, band_h - vbar_y_on))
                        if copy_count > 0 and vbar_y_on < band_h:
                            vbar_pixels[vbar_y_on:vbar_y_on + copy_count] = vbar_short_entry[:copy_count]
                    cur_vbar = vbar_pixels
                    if self._ccVBarCursor < len(self._ccVBarStorage):
                        self._ccVBarStorage[self._ccVBarCursor] = cur_vbar
                    self._ccVBarCursor = (self._ccVBarCursor + 1) % len(self._ccVBarStorage)

                # Render column x = x_start + i
                dst_x = x_start + i
                if 0 <= dst_x < surfW and cur_vbar is not None:
                    max_y = min(len(cur_vbar), surfH - y_start)
                    for y in range(max(0, -y_start), max_y):
                        dst_y = y_start + y
                        dst_idx = dst_y * surfW + dst_x
                        if dst_idx < len(out_u32):
                            out_u32[dst_idx] = cur_vbar[y]

    def _decodeClearCodecSubcodec(self, data, out, surfW, surfH):
        off = 0
        dlen = len(data)
        out_u32 = np.frombuffer(out, dtype=np.uint32)

        while off + 13 <= dlen:
            x_start = struct.unpack_from('<H', data, off)[0]
            y_start = struct.unpack_from('<H', data, off + 2)[0]
            width = struct.unpack_from('<H', data, off + 4)[0]
            height = struct.unpack_from('<H', data, off + 6)[0]
            bmp_len = struct.unpack_from('<I', data, off + 8)[0]
            subcodec_id = data[off + 12]
            off += 13

            if off + bmp_len > dlen:
                break
            bmp_data = data[off:off + bmp_len]
            off += bmp_len

            if subcodec_id == 0:  # Uncompressed BGR24
                src_off = 0
                for y in range(height):
                    dst_y = y_start + y
                    for x in range(width):
                        if src_off + 3 > len(bmp_data):
                            break
                        b = bmp_data[src_off]
                        g = bmp_data[src_off + 1]
                        r = bmp_data[src_off + 2]
                        src_off += 3
                        dst_x = x_start + x
                        if 0 <= dst_x < surfW and 0 <= dst_y < surfH:
                            dst_idx = dst_y * surfW + dst_x
                            if dst_idx < len(out_u32):
                                out_u32[dst_idx] = b | (g << 8) | (r << 16) | (0xFF << 24)

            elif subcodec_id == 1:  # CLEARCODEC_SUBCODEC_NSCODEC
                nsc_bgra = decode_nscodec(bmp_data, width, height)
                if nsc_bgra is not None:
                    nsc_u32 = np.frombuffer(nsc_bgra, dtype=np.uint32).reshape(height, width)
                    dst_y_end = min(y_start + height, surfH)
                    dst_x_end = min(x_start + width, surfW)
                    if dst_y_end > y_start and dst_x_end > x_start:
                        out_2d = out_u32.reshape(surfH, surfW)
                        h_copy = dst_y_end - y_start
                        w_copy = dst_x_end - x_start
                        out_2d[y_start:dst_y_end, x_start:dst_x_end] = nsc_u32[:h_copy, :w_copy]

            elif subcodec_id == 2:  # CLEARCODEC_SUBCODEC_RLEX
                if len(bmp_data) < 1:
                    continue
                palette_count = bmp_data[0]
                if palette_count < 1 or palette_count > 127 or 1 + palette_count * 3 > len(bmp_data):
                    continue
                palette = np.empty(palette_count, dtype=np.uint32)
                p_off = 1
                for p in range(palette_count):
                    b = bmp_data[p_off]
                    g = bmp_data[p_off + 1]
                    r = bmp_data[p_off + 2]
                    p_off += 3
                    palette[p] = b | (g << 8) | (r << 16) | (0xFF << 24)

                num_bits = _CLEAR_LOG2_FLOOR[palette_count - 1] + 1
                depth_mask = (1 << (8 - num_bits)) - 1
                stop_mask = (1 << num_bits) - 1

                pixel_count = width * height
                pixel_index = 0
                x = 0
                y = 0
                bmp_dlen = len(bmp_data)

                while p_off < bmp_dlen and pixel_index < pixel_count:
                    if p_off + 2 > bmp_dlen:
                        break
                    tmp = bmp_data[p_off]
                    run_length_factor = bmp_data[p_off + 1]
                    p_off += 2

                    suite_depth = (tmp >> num_bits) & depth_mask
                    stop_index = tmp & stop_mask
                    start_index = stop_index - suite_depth

                    if run_length_factor >= 0xFF:
                        if p_off + 2 > bmp_dlen:
                            break
                        run_length_factor = struct.unpack_from('<H', bmp_data, p_off)[0]
                        p_off += 2
                        if run_length_factor >= 0xFFFF:
                            if p_off + 4 > bmp_dlen:
                                break
                            run_length_factor = struct.unpack_from('<I', bmp_data, p_off)[0]
                            p_off += 4

                    if start_index < 0 or start_index >= palette_count or stop_index >= palette_count:
                        break

                    color = palette[start_index]
                    for r_run in range(run_length_factor):
                        if pixel_index >= pixel_count:
                            break
                        dst_x = x_start + x
                        dst_y = y_start + y
                        if 0 <= dst_x < surfW and 0 <= dst_y < surfH:
                            dst_idx = dst_y * surfW + dst_x
                            if dst_idx < len(out_u32):
                                out_u32[dst_idx] = color
                        x += 1
                        if x >= width:
                            x = 0
                            y += 1
                        pixel_index += 1

                    for s in range(suite_depth + 1):
                        if pixel_index >= pixel_count:
                            break
                        idx = start_index + s
                        if idx < palette_count:
                            ccolor = palette[idx]
                            dst_x = x_start + x
                            dst_y = y_start + y
                            if 0 <= dst_x < surfW and 0 <= dst_y < surfH:
                                dst_idx = dst_y * surfW + dst_x
                                if dst_idx < len(out_u32):
                                    out_u32[dst_idx] = ccolor
                        x += 1
                        if x >= width:
                            x = 0
                            y += 1
                        pixel_index += 1

    def _blitToSurface(self, surfaceId, left, top, width, height, data):
        """Write pixel data (top-down BGRA) into the surface buffer."""
        surfInfo = self._surfaces.get(surfaceId)
        surfBuf = self._surfaceData.get(surfaceId)
        if surfInfo is None or surfBuf is None:
            return
        surfW = surfInfo[0]
        surfH = surfInfo[1]

        # Calculate clipped intersection
        x0 = max(0, left)
        y0 = max(0, top)
        x1 = min(surfW, left + width)
        y1 = min(surfH, top + height)
        if x1 <= x0 or y1 <= y0:
            return

        src_x = x0 - left
        src_y = y0 - top
        clip_w = x1 - x0
        clip_h = y1 - y0

        try:
            src = (data.reshape(height, width * 4) if isinstance(data, np.ndarray)
                   else np.frombuffer(data, dtype=np.uint8, count=width * height * 4)
                   .reshape(height, width * 4))
            dst = surfBuf.reshape(surfH, surfW * 4)
            dst[y0:y1, x0 * 4:x1 * 4] = src[src_y:src_y + clip_h, src_x * 4:(src_x + clip_w) * 4]
            self._invalidate(surfaceId, x0, y0, clip_w, clip_h)
            return
        except (ValueError, IndexError):
            stride = surfW * 4
            src_stride = width * 4
            src_bytes = bytes(data) if not isinstance(data, (bytes, bytearray, memoryview)) else data
            for row in range(clip_h):
                src_off = (src_y + row) * src_stride + src_x * 4
                dst_off = (y0 + row) * stride + x0 * 4
                row = src_bytes[src_off:src_off + clip_w * 4]
                if len(row) < clip_w * 4:
                    break  # short data: keep what was complete
                surfBuf[dst_off:dst_off + clip_w * 4] = np.frombuffer(row, dtype=np.uint8)
        self._invalidate(surfaceId, x0, y0, clip_w, clip_h)

    def _onSurfaceToCache(self, payload):
        """SURFACETOCACHE: surfaceId(2) + cacheKey(8) + cacheSlot(2) + rectLeft(2) +
        rectTop(2) + rectRight(2) + rectBottom(2)"""
        if len(payload) < 20:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        cacheSlot, rectLeft, rectTop, rectRight, rectBottom = struct.unpack_from('<HHHHH', payload, 10)

        w = rectRight - rectLeft
        h = rectBottom - rectTop
        if w <= 0 or h <= 0:
            return

        surfInfo = self._surfaces.get(surfaceId)
        surfBuf = self._surfaceData.get(surfaceId)
        if surfInfo is None or surfBuf is None:
            log.debug("RDPGFX: SURFACETOCACHE unknown surface %d" % surfaceId)
            return

        surfW = surfInfo[0]
        surfH = surfInfo[1]
        # the rect must lie inside the surface (FreeRDP is_rect_valid)
        if rectRight > surfW or rectBottom > surfH:
            log.debug("RDPGFX: SURFACETOCACHE rect outside surface %d" % surfaceId)
            return
        region = surfBuf.reshape(surfH, surfW * 4)[rectTop:rectBottom, rectLeft * 4:rectRight * 4].copy()

        self._gfxCache[cacheSlot] = (w, h, region)
        log.debug("RDPGFX: SURFACETOCACHE surf=%d slot=%d rect=(%d,%d,%d,%d)" %
                  (surfaceId, cacheSlot, rectLeft, rectTop, rectRight, rectBottom))

    def _onCacheToSurface(self, payload):
        """CACHETOSURFACE: cacheSlot(2) + surfaceId(2) + numDestPoints(2) + destPoints"""
        if len(payload) < 6:
            return
        cacheSlot, surfaceId, numDest = struct.unpack_from('<HHH', payload, 0)

        cached = self._gfxCache.get(cacheSlot)
        if cached is None:
            log.debug("RDPGFX: CACHETOSURFACE missing cache slot %d" % cacheSlot)
            return

        cw, ch, cdata = cached
        surfInfo = self._surfaces.get(surfaceId)
        surfBuf = self._surfaceData.get(surfaceId)
        if surfInfo is None or surfBuf is None:
            return

        off = 6
        dest_coords = []
        for _ in range(numDest):
            if off + 4 > len(payload):
                break
            dx, dy = struct.unpack_from('<HH', payload, off)
            off += 4
            dest_coords.append((dx, dy))
            self._blitToSurface(surfaceId, dx, dy, cw, ch, cdata)

        first_coords = ", ".join("(%d,%d)" % (dx, dy) for dx, dy in dest_coords[:5])
        log.debug("RDPGFX: CACHETOSURFACE slot=%d surf=%d points=%d %dx%d first=[%s]%s" %
                  (cacheSlot, surfaceId, numDest, cw, ch, first_coords,
                   " ..." if numDest > 5 else ""))

    def _onEvictCacheEntry(self, payload):
        """EVICTCACHEENTRY: cacheSlot(2)"""
        if len(payload) < 2:
            return
        cacheSlot = struct.unpack_from('<H', payload, 0)[0]
        self._gfxCache.pop(cacheSlot, None)
        log.debug("RDPGFX: EVICTCACHEENTRY slot=%d" % cacheSlot)

    def _onDeleteEncodingContext(self, payload):
        """DELETEENCODINGCONTEXT: surfaceId(2) + codecContextId(4)"""
        if len(payload) < 6:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        codecContextId = struct.unpack_from('<I', payload, 2)[0]
        # Note: Tile coefficient state belongs to the surface and must NOT
        # be discarded per encoding context deletion (FreeRDP / grdp).
        log.debug("RDPGFX: DELETEENCODINGCTX surf=%d ctx=%d" % (surfaceId, codecContextId))

    def _deliverSurfaceBitmap(self, surfaceId, rects):
        """Mark the tiles a codec decoded into the surface as invalid."""
        for (rx, ry, rw, rh) in rects or ():
            if rw > 0 and rh > 0:
                self._invalidate(surfaceId, rx, ry, rw, rh)

    # ---------------------------------------------------------------
    # Presentation (FreeRDP gdi_surface_invalidate_rect / gdi_OutputUpdate)
    # ---------------------------------------------------------------

    _MAX_INVALID_RECTS = 256

    def _invalidate(self, surfaceId, x, y, w, h):
        """Add a rectangle (clipped to the surface) to the invalid region."""
        info = self._surfaces.get(surfaceId)
        if info is None:
            return
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(info[0], x + w), min(info[1], y + h)
        if x1 <= x0 or y1 <= y0:
            return
        rects = self._invalidRects.get(surfaceId)
        if rects is None:
            self._invalidRects[surfaceId] = [(x0, y0, x1, y1)]
            return
        for a, b, c, d in rects:
            if a <= x0 and b <= y0 and c >= x1 and d >= y1:
                return  # already covered
        rects[:] = [r for r in rects
                    if not (x0 <= r[0] and y0 <= r[1] and x1 >= r[2] and y1 >= r[3])]
        rects.append((x0, y0, x1, y1))
        if len(rects) > self._MAX_INVALID_RECTS:
            rects[:] = [self._extents(rects)]

    @staticmethod
    def _extents(rects):
        return (min(r[0] for r in rects), min(r[1] for r in rects),
                max(r[2] for r in rects), max(r[3] for r in rects))

    @classmethod
    def _coalesce(cls, rects):
        """Few large copies are cheaper than many small ones: use the bounding
        box when it is not much larger than the rects themselves."""
        if len(rects) <= 1:
            return rects
        box = cls._extents(rects)
        boxArea = (box[2] - box[0]) * (box[3] - box[1])
        area = sum((r[2] - r[0]) * (r[3] - r[1]) for r in rects)
        if len(rects) > 32 or boxArea <= area * 1.5:
            return [box]
        return rects

    def _updateSurfaces(self):
        """Copy the invalid areas of every mapped surface to the screen as
        one batch (one frame), then clear them (FreeRDP gdi_UpdateSurfaces)."""
        if not self._invalidRects:
            return
        items = []
        for surfaceId in sorted(self._invalidRects):
            origin = self._surfaceOutputMap.get(surfaceId)
            if origin is None:
                continue  # kept until mapped (the mapping clears it)
            rects = self._invalidRects.pop(surfaceId)
            info = self._surfaces.get(surfaceId)
            surfBuf = self._surfaceData.get(surfaceId)
            if info is None or surfBuf is None or not rects:
                continue
            sw, sh = info[0], info[1]
            surf = surfBuf.reshape(sh, sw * 4)
            ox, oy = origin
            for x0, y0, x1, y1 in self._coalesce(rects):
                # clip to the desktop (FreeRDP gdi_OutputUpdate)
                if self._resetWidth > 0:
                    x1 = min(x1, self._resetWidth - ox)
                if self._resetHeight > 0:
                    y1 = min(y1, self._resetHeight - oy)
                if x1 <= x0 or y1 <= y0:
                    continue
                # a copy: the decoder keeps writing into the surface
                region = surf[y0:y1, x0 * 4:x1 * 4].copy()
                items.append((ox + x0, oy + y0, x1 - x0, y1 - y0, region))
        if not items:
            return
        if self._gfxPresentCallback is not None:
            self._gfxPresentCallback(items)
        elif self._gfxCallback is not None:
            for x, y, w, h, region in items:
                self._gfxCallback(x, y, x + w - 1, y + h - 1, w, h, 32, 'gfx', region.tobytes())

    def _extractRegionFallback(self, surfBuf, surfW, rx, ry, rw, rh):
        """Row-by-row region extraction fallback."""
        region = bytearray(rw * rh * 4)
        row_bytes = rw * 4
        stride = surfW * 4
        for row in range(rh):
            src_off = (ry + row) * stride + rx * 4
            dst_off = row * row_bytes
            if src_off + row_bytes <= len(surfBuf):
                row_data = surfBuf[src_off:src_off + row_bytes]
                region[dst_off:dst_off + row_bytes] = row_data.tobytes() if isinstance(row_data,
                                                                                       np.ndarray) else row_data
        return bytes(region)

    def _deliverBitmap(self, x, y, width, height, bpp, data):
        """Deliver decoded bitmap to the observer via callback.
        Data is top-down BGRA scanlines from RDPGFX codecs.
        Pass isCompress='gfx' sentinel so the UI layer knows the data
        is already top-down and does not need the bottom-up flip.
        """
        if self._gfxCallback is None:
            return
        self._gfxCallback(x, y, x + width - 1, y + height - 1,
                          width, height, bpp, 'gfx', data)

    def _sendFrameAcknowledge(self, frameId):
        """Send RDPGFX FRAME_ACKNOWLEDGE PDU."""
        if self._gfxChannelId is None:
            return
        # Flow control (MS-RDPEGFX 2.2.2.8): report how many frames are still
        # waiting to be decoded. 0 (QUEUE_DEPTH_UNAVAILABLE) while the client
        # keeps up, so the server streams at full rate; a growing value makes
        # it lower the frame rate instead of letting the delay build up.
        queueDepth = self._gfxQueueDepth()
        ackPayload = struct.pack('<III', queueDepth, frameId, self._totalFramesDecoded)
        pduLen = 8 + len(ackPayload)
        gfxPdu = struct.pack('<HHI', RDPGFX_CMDID_FRAMEACKNOWLEDGE, 0, pduLen)
        gfxPdu += ackPayload

        cbId = self._channelCbId.get(self._gfxChannelId, 0)
        self._sendDvcData(self._gfxChannelId, cbId, gfxPdu)
        log.debug("RDPGFX: sent FRAME_ACKNOWLEDGE frameId=%d total=%d queueDepth=%d" %
                  (frameId, self._totalFramesDecoded, queueDepth))

    # ---------------------------------------------------------------
    # RDPGFX CAPS_ADVERTISE
    # ---------------------------------------------------------------

    def _sendRdpgfxCapsAdvertise(self, channelId, cbId):
        """Send RDPGFX CAPS_ADVERTISE PDU (MS-RDPEGFX 2.2.3)

        Following FreeRDP's approach: advertise many cap versions so the
        server can pick the best one it supports.

        When PyAV is available (FreeRDP-compatible):
          - v8.0 : baseline (THINCLIENT only, no SMALL_CACHE per spec)
          - v8.1 : AVC420 via explicit AVC420_ENABLED flag
          - v10 through v10.7 : AVC enabled by default (no AVC_DISABLED)
        When PyAV is unavailable:
          - v8.0 only with AVC_DISABLED flag → server uses Planar codec."""
        capsSets = []
        if (avc_module.is_available() and not self._avc444Disabled and
                not getattr(avc_module, 'fast_conversion_available', lambda: True)()):
            # AVC444 doubles the decoding work and its chroma merge has no
            # native fast path here: stay with AVC420 so video keeps up.
            log.info("RDPGFX: no native YUV conversion available, AVC444 disabled")
            self._avc444Disabled = True
        if avc_module.is_available():
            # ---- v8.0 baseline (spec says don't combine THINCLIENT + SMALL_CACHE) ----
            capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_8, 4,
                                        RDPGFX_CAPS_FLAG_THINCLIENT))

            # ---- v8.1 with AVC420 explicitly enabled ----
            capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_81, 4,
                                        RDPGFX_CAPS_FLAG_SMALL_CACHE |
                                        RDPGFX_CAPS_FLAG_AVC420_ENABLED))

            if not self._avc444Disabled:
                # ---- v10+ : AVC enabled by default (no AVC_DISABLED flag) ----
                caps10Flags = RDPGFX_CAPS_FLAG_SMALL_CACHE

                capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_10, 4,
                                            caps10Flags))

                # v10.1 has capsDataLength=0x10 (FreeRDP sends 16 bytes with flags=0)
                capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_101, 0x10, 0)
                                + b'\x00' * 12)

                capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_102, 4,
                                            caps10Flags))

                capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_103, 4,
                                            caps10Flags & ~RDPGFX_CAPS_FLAG_SMALL_CACHE))

                capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_104, 4,
                                            caps10Flags))

                capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_105, 4,
                                            caps10Flags))

                capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_106, 4,
                                            caps10Flags))

                capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_106_ERR, 4,
                                            caps10Flags))

                capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_107, 4,
                                            caps10Flags))
        else:
            # Non-AVC: v8.0 only + THINCLIENT, matching grdp's thin-client path.
            capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_8, 4,
                                        RDPGFX_CAPS_FLAG_THINCLIENT |
                                        RDPGFX_CAPS_FLAG_SMALL_CACHE |
                                        RDPGFX_CAPS_FLAG_AVC_DISABLED))

        capsPayload = struct.pack('<H', len(capsSets))
        for cs in capsSets:
            capsPayload += cs

        pduLen = 8 + len(capsPayload)
        gfxPdu = struct.pack('<HHI', RDPGFX_CMDID_CAPSADVERTISE, 0, pduLen)
        gfxPdu += capsPayload

        self._sendDvcData(channelId, cbId, gfxPdu)
        if avc_module.is_available():
            if self._avc444Disabled:
                log.debug("RDPGFX: sent CAPS_ADVERTISE (v8.1..v8.0, AVC444 disabled)")
            else:
                log.debug("RDPGFX: sent CAPS_ADVERTISE (v10.7..v8.0, AVC enabled)")
        else:
            log.debug("RDPGFX: sent CAPS_ADVERTISE (v8.0 only, THINCLIENT, AVC disabled)")

    # ---------------------------------------------------------------
    # DVC transport
    # ---------------------------------------------------------------

    # Largest DVC PDU sent in one static channel chunk (CHANNEL_CHUNK_LENGTH)
    _DVC_CHUNK = 1600

    @staticmethod
    def _channelIdBytes(cbId, channelId):
        return struct.pack(('<B', '<H', '<I', '<I')[cbId], channelId)

    def _sendDvcData(self, channelId, cbId, data):
        """Send a message on a dynamic channel; messages larger than one chunk
        are split into DATA_FIRST + DATA PDUs (MS-RDPEDYC 2.2.3, FreeRDP
        drdynvc_write_data)."""
        cid = self._channelIdBytes(cbId, channelId)
        single = 1 + len(cid) + len(data)
        if single <= self._DVC_CHUNK:
            self._send(bytes(bytearray([(DrdynvcCmd.DATA << 4) | cbId])) + cid + bytes(data))
            return
        total = len(data)
        sp, lenBytes = (0, struct.pack('<B', total)) if total <= 0xFF else \
            (1, struct.pack('<H', total)) if total <= 0xFFFF else (2, struct.pack('<I', total))
        head = bytes(bytearray([(DrdynvcCmd.DATA_FIRST << 4) | (sp << 2) | cbId])) + cid + lenBytes
        first = self._DVC_CHUNK - len(head)
        self._send(head + bytes(data[:first]))
        step = self._DVC_CHUNK - 1 - len(cid)
        for off in range(first, total, step):
            self._send(bytes(bytearray([(DrdynvcCmd.DATA << 4) | cbId])) + cid + bytes(data[off:off + step]))

    def _send(self, data):
        """Send data as a Virtual Channel PDU on the drdynvc static channel."""
        if self._onGfxThread():
            # Twisted is not thread safe: send from the network thread
            from twisted.internet import reactor
            reactor.callFromThread(self._send, data)
            return
        if self._transport is not None:
            from rdpy.core.type import String
            flags = CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST
            header = struct.pack('<II', len(data), flags)
            self._transport.send(String(header + data))
