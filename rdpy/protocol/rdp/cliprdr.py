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
CLIPRDR - Clipboard Virtual Channel Extension (MS-RDPECLIP).
Handles the "cliprdr" static virtual channel for bidirectional
clipboard sharing between the RDP client and server: text
(CF_UNICODETEXT / CF_TEXT) and files in both directions (FileGroupDescriptorW
+ File Contents, see cliprdr_file.py).

Protocol reference: [MS-RDPECLIP]
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpeclip/
"""
from __future__ import absolute_import, division, print_function

import os
import struct
from rdpy.core.layer import LayerAutomata
import rdpy.core.log as log
from rdpy.protocol.rdp import cliprdr_file
from rdpy.protocol.rdp.cliprdr_file import FILECONTENTS_SIZE, FILECONTENTS_RANGE

# Virtual Channel PDU flags
CHANNEL_FLAG_FIRST = 0x00000001
CHANNEL_FLAG_LAST = 0x00000002
CHANNEL_FLAG_SHOW_PROTOCOL = 0x00000010
# CLIPRDR PDU types (MS-RDPECLIP 2.2.1)
CB_MONITOR_READY = 0x0001
CB_FORMAT_LIST = 0x0002
CB_FORMAT_LIST_RESPONSE = 0x0003
CB_FORMAT_DATA_REQUEST = 0x0004
CB_FORMAT_DATA_RESPONSE = 0x0005
CB_TEMP_DIRECTORY = 0x0006
CB_CLIP_CAPS = 0x0007
CB_FILECONTENTS_REQUEST = 0x0008
CB_FILECONTENTS_RESPONSE = 0x0009
CB_LOCK_CLIPDATA = 0x000A
CB_UNLOCK_CLIPDATA = 0x000B
# CLIPRDR message flags
CB_RESPONSE_OK = 0x0001
CB_RESPONSE_FAIL = 0x0002
CB_ASCII_NAMES = 0x0004
# Clipboard format IDs (Windows standard)
CF_TEXT = 1
CF_UNICODETEXT = 13
# General capability set flags (MS-RDPECLIP 2.2.2.1.1.1)
CB_USE_LONG_FORMAT_NAMES = 0x00000002
CB_STREAM_FILECLIP_ENABLED = 0x00000004
CB_FILECLIP_NO_FILE_PATHS = 0x00000008
CB_CAN_LOCK_CLIPDATA = 0x00000010
CB_HUGE_FILE_SUPPORT_ENABLED = 0x00000020
# Capability set type
CB_CAPSTYPE_GENERAL = 0x0001
# Format id we use for "FileGroupDescriptorW" (registered formats are >= 0xC000)
CF_FILE_GROUP_DESCRIPTOR = 0xC0FE
# Largest download accepted when files are copied on the server (MB)
_MAX_DOWNLOAD_MB = int(os.environ.get('RDPY_CLIPBOARD_MAX_MB', '4096'))

_MSG_NAMES = {
    CB_MONITOR_READY: "MONITOR_READY",
    CB_FORMAT_LIST: "FORMAT_LIST",
    CB_FORMAT_LIST_RESPONSE: "FORMAT_LIST_RESPONSE",
    CB_FORMAT_DATA_REQUEST: "FORMAT_DATA_REQUEST",
    CB_FORMAT_DATA_RESPONSE: "FORMAT_DATA_RESPONSE",
    CB_TEMP_DIRECTORY: "TEMP_DIRECTORY",
    CB_CLIP_CAPS: "CLIP_CAPS",
    CB_FILECONTENTS_REQUEST: "FILECONTENTS_REQUEST",
    CB_FILECONTENTS_RESPONSE: "FILECONTENTS_RESPONSE",
    CB_LOCK_CLIPDATA: "LOCK_CLIPDATA",
    CB_UNLOCK_CLIPDATA: "UNLOCK_CLIPDATA",
}


class CliprdrLayer(LayerAutomata):
    """
    CLIPRDR static virtual channel layer.

    Implements the MS-RDPECLIP protocol for clipboard redirection.
    Only CF_UNICODETEXT is negotiated so all clipboard data is text.
    """

    def __init__(self):
        LayerAutomata.__init__(self, None)
        # VChannel reassembly buffer
        self._vchanBuf = b''
        # Server capability flags
        self._serverGeneralFlags = 0
        # Whether the server uses long format names
        self._useLongFormatNames = False
        # Callback: called with (text: str) when the server's clipboard is received
        self._onRemoteClipboardChanged = None
        # Callback: called with no args to request current local clipboard text
        self._getLocalClipboardText = None
        # Tracks whether we are suppressing the next local clipboard change
        # to avoid echo loops (server→client paste triggers local clipboard change)
        self._suppressNextLocalChange = False
        # --- file transfer ---
        self._getLocalFiles = None        # callback() -> [local paths]
        self._onRemoteFiles = None        # callback([local paths]) once downloaded
        self._localFilePaths = []         # paths offered in our last Format List
        self._localFileList = None        # LocalFileList built when the server asks
        self._lockedLists = {}            # clipDataId -> LocalFileList (server locks)
        self._pendingFormat = None        # format of our outstanding Format Data Request
        self._download = None             # RemoteDownload in progress
        self._nextStreamId = 1
        self._nextClipDataId = 1
        self._transferListener = None     # see setTransferListener
        self._uploads = {}                # id(LocalFileList) -> _UploadProgress

    def setRemoteClipboardCallback(self, callback):
        """Set callback(text: str) invoked when server clipboard data arrives."""
        self._onRemoteClipboardChanged = callback

    def setLocalClipboardGetter(self, callback):
        """Set callback() -> str that returns current local clipboard text."""
        self._getLocalClipboardText = callback

    def setLocalFilesGetter(self, callback):
        """Set callback() -> [str] returning the local files on the clipboard."""
        self._getLocalFiles = callback

    def setRemoteFilesCallback(self, callback):
        """Set callback([str]) invoked with the local copies of files copied on
        the server, once they are downloaded (put them on the local clipboard)."""
        self._onRemoteFiles = callback

    def setFileTransferEnabled(self, enabled):
        """Share files through the clipboard (default True); text only when False."""
        self._filesAllowed = bool(enabled)

    def setTransferListener(self, listener):
        """
        @summary: object notified of clipboard file transfers (all optional):
        onFileTransferStarted(direction, items, totalBytes),
        onFileTransferProgress(direction, doneBytes, totalBytes),
        onFileTransferFinished(direction, paths, bytes, status).
        direction: 'upload' (local files pasted on the server) or 'download'
        (server files received); status: 'completed', 'partial' or 'cancelled'.
        """
        self._transferListener = listener

    def _notify(self, name, *args):
        callback = getattr(self._transferListener, name, None)
        if callback is not None:
            try:
                callback(*args)
            except Exception as e:
                log.warning("CLIPRDR: transfer listener %s failed: %s" % (name, e))

    def _fileTransferEnabled(self):
        return bool(getattr(self, '_filesAllowed', True) and self._useLongFormatNames and
                    self._serverGeneralFlags & CB_STREAM_FILECLIP_ENABLED)

    def connect(self):
        log.debug("CliprdrLayer.connect()")

    def recv(self, s):
        """Receive data on the cliprdr static virtual channel with VChannel reassembly."""
        data = s.read()
        log.debug("CLIPRDR DEBUG: recv() called, data length=%d" % len(data))
        if len(data) < 8:
            log.warning("CLIPRDR: recv data too short (%d bytes)" % len(data))
            return
        totalLen = struct.unpack_from('<I', data, 0)[0]
        flags = struct.unpack_from('<I', data, 4)[0]
        payload = data[8:]
        log.debug("CLIPRDR DEBUG: recv totalLen=%d flags=0x%08x payloadLen=%d" % (totalLen, flags, len(payload)))

        if flags & CHANNEL_FLAG_FIRST:
            self._vchanBuf = payload
        else:
            self._vchanBuf += payload

        if flags & CHANNEL_FLAG_LAST:
            log.debug("CLIPRDR DEBUG: reassembled %d bytes, processing" % len(self._vchanBuf))
            if len(self._vchanBuf) >= 1:
                self._processData(bytes(self._vchanBuf))
            self._vchanBuf = b''

    def _processData(self, data):
        """Dispatch a reassembled CLIPRDR PDU."""
        if len(data) < 8:
            return

        # CLIPRDR_HEADER: msgType(2) + msgFlags(2) + dataLen(4)
        msgType = struct.unpack_from('<H', data, 0)[0]
        msgFlags = struct.unpack_from('<H', data, 2)[0]
        dataLen = struct.unpack_from('<I', data, 4)[0]
        body = data[8:8 + dataLen]

        log.debug("CLIPRDR: recv %s(0x%04x) flags=0x%04x dataLen=%d" %
                  (_MSG_NAMES.get(msgType, "?"), msgType, msgFlags, dataLen))

        if msgType == CB_CLIP_CAPS:
            self._processClipCaps(body)
        elif msgType == CB_MONITOR_READY:
            self._processMonitorReady(body)
        elif msgType == CB_FORMAT_LIST:
            self._processFormatList(body, msgFlags)
        elif msgType == CB_FORMAT_LIST_RESPONSE:
            self._processFormatListResponse(msgFlags)
        elif msgType == CB_FORMAT_DATA_REQUEST:
            self._processFormatDataRequest(body)
        elif msgType == CB_FORMAT_DATA_RESPONSE:
            self._processFormatDataResponse(body, msgFlags)
        elif msgType == CB_FILECONTENTS_REQUEST:
            self._processFileContentsRequest(body)
        elif msgType == CB_FILECONTENTS_RESPONSE:
            self._processFileContentsResponse(body, msgFlags)
        elif msgType == CB_LOCK_CLIPDATA:
            self._processLockClipData(body)
        elif msgType == CB_UNLOCK_CLIPDATA:
            self._processUnlockClipData(body)
        else:
            log.debug("CLIPRDR: unhandled msgType=0x%04x" % msgType)

    # -------------------------------------------------------------------
    # Clipboard Capabilities (MS-RDPECLIP 2.2.2.1)
    # -------------------------------------------------------------------

    def _processClipCaps(self, body):
        """Parse Clipboard Capabilities PDU from server."""
        if len(body) < 4:
            return
        cCapabilitySets = struct.unpack_from('<H', body, 0)[0]
        # pad1(2)
        offset = 4
        for _ in range(cCapabilitySets):
            if offset + 4 > len(body):
                break
            capType = struct.unpack_from('<H', body, offset)[0]
            capLen = struct.unpack_from('<H', body, offset + 2)[0]
            if capType == CB_CAPSTYPE_GENERAL and capLen >= 12:
                # version(4) + generalFlags(4)
                self._serverGeneralFlags = struct.unpack_from('<I', body, offset + 8)[0]
                self._useLongFormatNames = bool(
                    self._serverGeneralFlags & CB_USE_LONG_FORMAT_NAMES)
                log.debug("CLIPRDR: server generalFlags=0x%08x longNames=%s" %
                          (self._serverGeneralFlags, self._useLongFormatNames))
            offset += capLen

    def _sendClipCaps(self):
        """Send Clipboard Capabilities PDU to server."""
        # General capability set: capabilitySetType(2) + lengthCapability(2) + version(4) + generalFlags(4)
        generalFlags = CB_USE_LONG_FORMAT_NAMES
        if getattr(self, '_filesAllowed', True):
            generalFlags |= (CB_STREAM_FILECLIP_ENABLED | CB_FILECLIP_NO_FILE_PATHS |
                             CB_CAN_LOCK_CLIPDATA | CB_HUGE_FILE_SUPPORT_ENABLED)
        capSet = struct.pack('<HH', CB_CAPSTYPE_GENERAL, 12)
        capSet += struct.pack('<I', 2)  # version 2
        capSet += struct.pack('<I', generalFlags)

        # cCapabilitySets(2) + pad1(2) + capabilitySet
        body = struct.pack('<HH', 1, 0) + capSet
        self._sendPDU(CB_CLIP_CAPS, 0, body)
        log.debug("CLIPRDR: sent Clip Caps")

    # -------------------------------------------------------------------
    # Monitor Ready (MS-RDPECLIP 2.2.2.2)
    # -------------------------------------------------------------------

    def _processMonitorReady(self, body):
        """Handle Monitor Ready PDU: server is ready for clipboard exchange."""
        log.debug("CLIPRDR: server Monitor Ready")
        # Respond with our capabilities and an initial format list
        self._sendClipCaps()
        self._sendFormatList()

    # -------------------------------------------------------------------
    # Format List (MS-RDPECLIP 2.2.3.1)
    # -------------------------------------------------------------------

    def _sendFormatList(self):
        """Send Format List PDU: text, plus files when local files are copied."""
        if self._localFileList is not None and self._localFileList not in self._lockedLists.values():
            self._endUpload(self._localFileList)  # replaced before the server finished
        self._localFilePaths = []
        self._localFileList = None
        if self._fileTransferEnabled() and self._getLocalFiles is not None:
            try:
                self._localFilePaths = [p for p in (self._getLocalFiles() or []) if os.path.exists(p)]
            except Exception as e:
                log.warning("CLIPRDR: cannot read the local file list: %s" % e)
        formats = [(CF_UNICODETEXT, "")]
        if self._localFilePaths:
            formats.append((CF_FILE_GROUP_DESCRIPTOR, cliprdr_file.FORMAT_NAME_FILE_GROUP_DESCRIPTOR))
        body = b''
        for fmtId, name in formats:
            if self._useLongFormatNames:
                # Long Format Name: formatId(4) + wszFormatName (null-terminated UTF-16LE)
                body += struct.pack('<I', fmtId) + (name + '\0').encode('utf-16-le')
            else:
                # Short Format Name: formatId(4) + formatName[32]
                body += struct.pack('<I', fmtId) + name.encode('utf-16-le')[:30].ljust(32, b'\x00')
        self._sendPDU(CB_FORMAT_LIST, 0, body)
        log.debug("CLIPRDR: sent Format List %s" % formats)
        if self._localFilePaths:
            log.info("clipboard: %d local item(s) ready to paste on the server" % len(self._localFilePaths))

    def _processFormatList(self, body, msgFlags):
        """Handle Format List PDU from server (server clipboard changed)."""
        # Parse formats offered by the server
        formats = self._parseFormatList(body, msgFlags)
        log.debug("CLIPRDR: server Format List: %s" % formats)

        # Always respond with OK
        self._sendPDU(CB_FORMAT_LIST_RESPONSE, CB_RESPONSE_OK, b'')

        # A new server clipboard replaces a download still running
        self._abortDownload()

        # Files copied on the server: download them, then put them on the
        # local clipboard
        if self._fileTransferEnabled() and self._onRemoteFiles is not None:
            for fmtId, fmtName in formats:
                if fmtName == cliprdr_file.FORMAT_NAME_FILE_GROUP_DESCRIPTOR:
                    self._requestRemoteFiles(fmtId)
                    return

        # Request text data if available
        requestFormatId = None
        for fmtId, fmtName in formats:
            if fmtId == CF_UNICODETEXT:
                requestFormatId = CF_UNICODETEXT
                break
            elif fmtId == CF_TEXT:
                requestFormatId = CF_TEXT
                break

        if requestFormatId is not None:
            self._sendFormatDataRequest(requestFormatId)

    def _parseFormatList(self, body, msgFlags):
        """Parse format list entries. Returns list of (formatId, formatName)."""
        formats = []
        if self._useLongFormatNames and not (msgFlags & CB_ASCII_NAMES):
            # Long Format Names (MS-RDPECLIP 2.2.3.1.1.1)
            offset = 0
            while offset + 4 <= len(body):
                fmtId = struct.unpack_from('<I', body, offset)[0]
                offset += 4
                # Read null-terminated UTF-16LE string
                nameEnd = offset
                while nameEnd + 1 < len(body):
                    if body[nameEnd] == 0 and body[nameEnd + 1] == 0:
                        break
                    nameEnd += 2
                name = body[offset:nameEnd].decode('utf-16-le', errors='replace')
                offset = nameEnd + 2  # skip null terminator
                formats.append((fmtId, name))
        else:
            # Short Format Names (MS-RDPECLIP 2.2.3.1.1.2)
            offset = 0
            while offset + 36 <= len(body):
                fmtId = struct.unpack_from('<I', body, offset)[0]
                nameBytes = body[offset + 4:offset + 36]
                if msgFlags & CB_ASCII_NAMES:
                    name = nameBytes.split(b'\x00')[0].decode('ascii', errors='replace')
                else:
                    name = nameBytes.decode('utf-16-le', errors='replace').split('\x00')[0]
                formats.append((fmtId, name))
                offset += 36
        return formats

    def _processFormatListResponse(self, msgFlags):
        """Handle Format List Response (acknowledgement from server)."""
        if msgFlags & CB_RESPONSE_OK:
            log.debug("CLIPRDR: Format List Response OK")
        else:
            log.warning("CLIPRDR: Format List Response FAIL")

    # -------------------------------------------------------------------
    # Format Data Request / Response (MS-RDPECLIP 2.2.5)
    # -------------------------------------------------------------------

    def _sendFormatDataRequest(self, formatId):
        """Send Format Data Request to server."""
        body = struct.pack('<I', formatId)
        self._pendingFormat = formatId
        self._sendPDU(CB_FORMAT_DATA_REQUEST, 0, body)
        log.debug("CLIPRDR: sent Format Data Request formatId=%d" % formatId)

    def _processFormatDataRequest(self, body):
        """Handle Format Data Request from server (server wants our clipboard)."""
        if len(body) < 4:
            self._sendPDU(CB_FORMAT_DATA_RESPONSE, CB_RESPONSE_FAIL, b'')
            return

        requestedFormat = struct.unpack_from('<I', body, 0)[0]
        log.debug("CLIPRDR: server requests format %d" % requestedFormat)

        if requestedFormat == CF_FILE_GROUP_DESCRIPTOR:
            fileList = self._currentLocalFileList()
            if fileList is None or not fileList.entries:
                self._sendPDU(CB_FORMAT_DATA_RESPONSE, CB_RESPONSE_FAIL, b'')
                return
            self._sendPDU(CB_FORMAT_DATA_RESPONSE, CB_RESPONSE_OK, fileList.descriptors())
            log.info("clipboard: sending %d file(s) / folder(s) to the server" % len(fileList.entries))
            self._uploadFor(fileList)
            return

        text = ''
        if self._getLocalClipboardText is not None:
            text = self._getLocalClipboardText() or ''

        # Cap clipboard text to a reasonable maximum (1 MB) to avoid
        # excessive memory use and network traffic.
        _MAX_CLIP_BYTES = 1024 * 1024

        if requestedFormat == CF_UNICODETEXT:
            encoded = (text + '\0').encode('utf-16-le')
            if len(encoded) > _MAX_CLIP_BYTES:
                log.warning("CLIPRDR: clipboard text too large (%d bytes), truncating" % len(encoded))
                # Truncate to an even byte boundary (UTF-16LE)
                encoded = encoded[:_MAX_CLIP_BYTES & ~1]
            self._sendPDU(CB_FORMAT_DATA_RESPONSE, CB_RESPONSE_OK, encoded)
        elif requestedFormat == CF_TEXT:
            encoded = (text + '\0').encode('ascii', errors='replace')
            if len(encoded) > _MAX_CLIP_BYTES:
                log.warning("CLIPRDR: clipboard text too large (%d bytes), truncating" % len(encoded))
                encoded = encoded[:_MAX_CLIP_BYTES]
            self._sendPDU(CB_FORMAT_DATA_RESPONSE, CB_RESPONSE_OK, encoded)
        else:
            self._sendPDU(CB_FORMAT_DATA_RESPONSE, CB_RESPONSE_FAIL, b'')

    def _processFormatDataResponse(self, body, msgFlags):
        """Handle Format Data Response from server (clipboard data received)."""
        requested = self._pendingFormat
        self._pendingFormat = None
        if not (msgFlags & CB_RESPONSE_OK):
            log.warning("CLIPRDR: Format Data Response FAIL")
            if self._download is not None:
                self._abortDownload()
            return
        if self._download is not None and requested == self._download.formatId:
            self._startDownload(body)
            return

        # Try to decode as UTF-16LE (CF_UNICODETEXT), strip null terminator
        try:
            text = body.decode('utf-16-le')
        except (UnicodeDecodeError, ValueError):
            # Fallback: try as plain text
            try:
                text = body.decode('utf-8', errors='replace')
            except Exception:
                text = body.decode('latin-1')

        # Strip null terminators
        text = text.rstrip('\x00')

        if text and self._onRemoteClipboardChanged is not None:
            log.debug("CLIPRDR: received text (%d chars)" % len(text))
            self._suppressNextLocalChange = True
            self._onRemoteClipboardChanged(text)

    # -------------------------------------------------------------------
    # Files copied locally -> server (MS-RDPECLIP 3.1.5.4.5 - 3.1.5.4.7)
    # -------------------------------------------------------------------

    def _currentLocalFileList(self):
        if self._localFileList is None and self._localFilePaths:
            self._localFileList = cliprdr_file.LocalFileList(self._localFilePaths)
        return self._localFileList

    def _processLockClipData(self, body):
        if len(body) >= 4:
            clipDataId = struct.unpack_from('<I', body, 0)[0]
            self._lockedLists[clipDataId] = self._currentLocalFileList()

    def _processUnlockClipData(self, body):
        if len(body) >= 4:
            fileList = self._lockedLists.pop(struct.unpack_from('<I', body, 0)[0], None)
            if fileList is not None and fileList not in self._lockedLists.values():
                self._endUpload(fileList)

    def _uploadFor(self, fileList):
        """Progress of the paste of fileList on the server (created on first use)."""
        progress = self._uploads.get(id(fileList))
        if progress is None:
            progress = _UploadProgress(self, fileList)
            self._uploads[id(fileList)] = progress
            progress.start()
        return progress

    def _endUpload(self, fileList):
        """The server released the files: report an unfinished paste."""
        progress = self._uploads.pop(id(fileList), None)
        if progress is not None:
            progress.end()

    def _processFileContentsRequest(self, body):
        """Serve the size or a byte range of one of our files."""
        if len(body) < 24:
            return
        streamId, index, flags, posLow, posHigh, cbRequested = struct.unpack_from('<IIIIII', body, 0)
        fileList = None
        if len(body) >= 28:
            fileList = self._lockedLists.get(struct.unpack_from('<I', body, 24)[0])
        if fileList is None:
            fileList = self._currentLocalFileList()
        try:
            if fileList is None or index >= len(fileList.entries):
                raise IndexError("no file %d" % index)
            if flags & FILECONTENTS_SIZE:
                data = struct.pack('<Q', fileList.size(index))
            elif flags & FILECONTENTS_RANGE:
                position = (posHigh << 32) | posLow
                data = fileList.read(index, position, min(cbRequested, 8 * 1024 * 1024))
                self._uploadFor(fileList).served(index, position + len(data))
            else:
                raise ValueError("bad flags 0x%x" % flags)
        except (IndexError, ValueError, OSError) as e:
            log.warning("clipboard: cannot send file %d: %s" % (index, e))
            self._sendPDU(CB_FILECONTENTS_RESPONSE, CB_RESPONSE_FAIL, struct.pack('<I', streamId))
            return
        self._sendPDU(CB_FILECONTENTS_RESPONSE, CB_RESPONSE_OK, struct.pack('<I', streamId) + data)

    # -------------------------------------------------------------------
    # Files copied on the server -> local temporary folder
    # -------------------------------------------------------------------

    def _requestRemoteFiles(self, formatId):
        clipDataId = None
        if self._serverGeneralFlags & CB_CAN_LOCK_CLIPDATA:
            # keep the server's clipboard data alive during the download
            clipDataId = self._nextClipDataId
            self._nextClipDataId = (self._nextClipDataId + 1) & 0xFFFFFFFF or 1
            self._sendPDU(CB_LOCK_CLIPDATA, 0, struct.pack('<I', clipDataId))
        self._download = _PendingDownload(formatId, clipDataId)
        self._sendFormatDataRequest(formatId)

    def _startDownload(self, descriptorData):
        pending = self._download
        entries = cliprdr_file.parseDescriptors(descriptorData)
        if not entries:
            self._abortDownload()
            return
        total = sum(e.size or 0 for e in entries if not e.isDir)
        if total > _MAX_DOWNLOAD_MB * 1024 * 1024:
            log.warning("clipboard: %d MB copied on the server, above the %d MB limit "
                        "(set RDPY_CLIPBOARD_MAX_MB to change it), not downloaded" %
                        (total // (1024 * 1024), _MAX_DOWNLOAD_MB))
            self._abortDownload()
            return
        try:
            download = cliprdr_file.RemoteDownload(entries, cliprdr_file.newDownloadFolder(),
                                                   pending.clipDataId)
            download.prepare()
        except OSError as e:
            log.warning("clipboard: cannot prepare the download: %s" % e)
            self._abortDownload()
            return
        download.formatId = pending.formatId
        self._download = download
        log.info("clipboard: receiving %d item(s), %.1f MB, from the server" %
                 (len(entries), total / 1048576.0))
        self._notify('onFileTransferStarted', 'download', len(entries), total)
        self._requestNextChunk()

    def _requestNextChunk(self):
        download = self._download
        try:
            request = download.nextRequest(self._nextStreamId)
        except OSError as e:
            log.warning("clipboard: download failed: %s" % e)
            self._abortDownload()
            return
        self._nextStreamId = (self._nextStreamId + 1) & 0xFFFFFFFF or 1
        if request is None:
            self._finishDownload()
            return
        streamId, index, flags, position, length = request
        body = struct.pack('<IIIIII', streamId, index, flags, position & 0xFFFFFFFF,
                           position >> 32, length)
        if download.clipDataId is not None:
            body += struct.pack('<I', download.clipDataId)
        self._sendPDU(CB_FILECONTENTS_REQUEST, 0, body)

    def _processFileContentsResponse(self, body, msgFlags):
        download = self._download
        if not isinstance(download, cliprdr_file.RemoteDownload) or len(body) < 4:
            return
        if struct.unpack_from('<I', body, 0)[0] != download.streamId:
            return  # answer to an aborted request
        try:
            download.onResponse(bool(msgFlags & CB_RESPONSE_OK), body[4:])
        except OSError as e:
            log.warning("clipboard: cannot write the downloaded file: %s" % e)
            self._abortDownload()
            return
        self._notify('onFileTransferProgress', 'download', download.received, download.total)
        self._requestNextChunk()

    def _finishDownload(self):
        download = self._download
        self._download = None
        self._unlock(download)
        elapsed = max(0.001, cliprdr_file.monotonic() - download.started)
        log.info("clipboard: %d item(s) received (%.1f MB in %.1fs%s), paste them with Ctrl+V" %
                 (len(download.entries), download.received / 1048576.0, elapsed,
                  ", %d failed" % download.failed if download.failed else ""))
        if download.topLevel and self._onRemoteFiles is not None:
            self._suppressNextLocalChange = True
            self._onRemoteFiles(download.topLevel)
        self._notify('onFileTransferFinished', 'download', list(download.topLevel), download.received,
                     'partial' if download.failed else 'completed')

    def _abortDownload(self):
        download = self._download
        if download is None:
            return
        self._download = None
        if isinstance(download, cliprdr_file.RemoteDownload):
            download.abort()
            log.info("clipboard: file download cancelled")
            self._notify('onFileTransferFinished', 'download', [], download.received, 'cancelled')
        self._unlock(download)

    def _unlock(self, download):
        if download is not None and download.clipDataId is not None:
            self._sendPDU(CB_UNLOCK_CLIPDATA, 0, struct.pack('<I', download.clipDataId))

    # -------------------------------------------------------------------
    # Public API for local clipboard changes
    # -------------------------------------------------------------------

    def onLocalClipboardChanged(self):
        """Called when the local clipboard content changes.
        Sends a Format List to the server to advertise new content."""
        log.debug("CLIPRDR DEBUG: onLocalClipboardChanged() suppress=%s transport=%s" %
                  (self._suppressNextLocalChange,
                   type(self._transport).__name__ if self._transport else "None"))
        if self._suppressNextLocalChange:
            self._suppressNextLocalChange = False
            log.debug("CLIPRDR DEBUG: suppressed (echo prevention)")
            return
        if self._transport is not None:
            self._sendFormatList()
            log.debug("CLIPRDR: local clipboard changed, sent Format List")
        else:
            log.warning("CLIPRDR DEBUG: local clipboard changed but transport is None!")

    # -------------------------------------------------------------------
    # Send helpers
    # -------------------------------------------------------------------

    def _sendPDU(self, msgType, msgFlags, body):
        """Send a CLIPRDR PDU wrapped in VChannel header."""
        log.debug("CLIPRDR DEBUG: _sendPDU type=%s(0x%04x) flags=0x%04x bodyLen=%d" %
                  (_MSG_NAMES.get(msgType, "?"), msgType, msgFlags, len(body)))
        # CLIPRDR_HEADER: msgType(2) + msgFlags(2) + dataLen(4) + body
        cliprdrPdu = struct.pack('<HHI', msgType, msgFlags, len(body)) + body
        self._send(cliprdrPdu)

    # VChannel chunk size negotiated in Virtual Channel Capability Set
    _VC_CHUNK_SIZE = 1600

    def _send(self, data):
        """Send data via the static virtual channel, chunking if needed.

        RDP virtual channel data must be sent in chunks no larger than
        VCChunkSize (1600 bytes).  Each chunk carries a VChannel header
        with totalLength and FIRST/LAST flags.
        """
        log.debug("CLIPRDR DEBUG: _send() transport=%s dataLen=%d" %
                  (type(self._transport).__name__ if self._transport else "None", len(data)))
        if self._transport is None:
            log.warning("CLIPRDR DEBUG: _send() SKIPPED - transport is None!")
            return

        from rdpy.core.type import String
        totalLen = len(data)
        offset = 0
        while offset < totalLen:
            chunk = data[offset:offset + self._VC_CHUNK_SIZE]
            flags = CHANNEL_FLAG_SHOW_PROTOCOL
            if offset == 0:
                flags |= CHANNEL_FLAG_FIRST
            if offset + len(chunk) >= totalLen:
                flags |= CHANNEL_FLAG_LAST
            header = struct.pack('<II', totalLen, flags)
            self._transport.send(String(header + chunk))
            offset += len(chunk)


class _PendingDownload(object):
    """Files announced by the server, descriptor not received yet."""

    def __init__(self, formatId, clipDataId):
        self.formatId = formatId
        self.clipDataId = clipDataId


class _UploadProgress(object):
    """Bytes of a local file list the server read (paste on the server)."""

    def __init__(self, layer, fileList):
        self.layer = layer
        self.fileList = fileList
        self.sizes = dict((i, e.size) for i, e in enumerate(fileList.entries) if not e.isDir)
        self.total = sum(self.sizes.values())
        self.done = dict((i, 0) for i in self.sizes)
        self.finished = False

    def paths(self):
        return [e.path for e in self.fileList.entries if '\\' not in e.name]

    def doneBytes(self):
        return sum(min(self.done[i], self.sizes[i]) for i in self.sizes)

    def start(self):
        self.layer._notify('onFileTransferStarted', 'upload', len(self.fileList.entries), self.total)
        self._checkFinished()

    def served(self, index, end):
        if self.finished or index not in self.done:
            return
        if end > self.done[index]:
            self.done[index] = end
        self.layer._notify('onFileTransferProgress', 'upload', self.doneBytes(), self.total)
        self._checkFinished()

    def _checkFinished(self):
        if not self.finished and self.doneBytes() >= self.total:
            self.finished = True
            log.info("clipboard: %d item(s) sent to the server (%.1f MB)" %
                     (len(self.fileList.entries), self.total / 1048576.0))
            self.layer._notify('onFileTransferFinished', 'upload', self.paths(), self.total, 'completed')

    def end(self):
        if not self.finished:
            self.finished = True
            done = self.doneBytes()
            self.layer._notify('onFileTransferFinished', 'upload', self.paths(), done,
                               'partial' if done else 'cancelled')
