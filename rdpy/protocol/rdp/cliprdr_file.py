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
File copy / paste through the clipboard (MS-RDPECLIP 3.1.5.4).

Files are described by a FILEGROUPDESCRIPTORW (the "FileGroupDescriptorW"
clipboard format) and their bytes are fetched with File Contents Request /
Response PDUs, like FreeRDP (client/common/client_cliprdr_file.c,
winpr/libwinpr/clipboard/synthetic_file.c).

* LocalFileList: files copied on this computer, offered to the server.
* RemoteDownload: files copied on the server, downloaded into a temporary
  folder so they can be pasted locally.
"""
from __future__ import absolute_import, division, print_function
import os
import re
import shutil
import struct
import tempfile
import rdpy.core.log as log
from rdpy.core.compat import makedirs, monotonic
from rdpy.core.filetimes import EPOCH_AS_FILETIME, HUNDREDS_OF_NANOSECONDS

FORMAT_NAME_FILE_GROUP_DESCRIPTOR = "FileGroupDescriptorW"
# FILEDESCRIPTORW (MS-RDPECLIP 2.2.5.2.3.1)
FILE_DESCRIPTOR_SIZE = 592
FD_ATTRIBUTES = 0x00000004
FD_FILESIZE = 0x00000040
FD_WRITESTIME = 0x00000020
FD_SHOWPROGRESSUI = 0x00004000
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_READONLY = 0x00000001

# File Contents Request dwFlags (MS-RDPECLIP 2.2.5.3)
FILECONTENTS_SIZE = 0x00000001
FILECONTENTS_RANGE = 0x00000002

_MAX_NAME = 259  # cFileName is 260 WCHAR including the terminator
_FORBIDDEN = re.compile(r'[<>:"|?*\x00-\x1f]')


def toFiletime(seconds):
    return max(0, int(seconds * HUNDREDS_OF_NANOSECONDS) + EPOCH_AS_FILETIME)


def fromFiletime(ft):
    return (ft - EPOCH_AS_FILETIME) / float(HUNDREDS_OF_NANOSECONDS)


class FileEntry(object):
    __slots__ = ('name', 'path', 'isDir', 'size', 'mtime', 'readOnly')

    def __init__(self, name, path, isDir, size, mtime, readOnly=False):
        self.name = name  # relative Windows path ("dir\\file.txt")
        self.path = path  # local absolute path (local list) or None
        self.isDir = isDir
        self.size = size
        self.mtime = mtime  # POSIX seconds, or None
        self.readOnly = readOnly


def packDescriptors(entries):
    """
    @summary: Encode a FILEGROUPDESCRIPTORW: cItems + FILEDESCRIPTORW[cItems]
    @param entries: [FileEntry]
    @return: {bytes}
    """
    out = [struct.pack('<I', len(entries))]
    for e in entries:
        attr = FILE_ATTRIBUTE_DIRECTORY if e.isDir else FILE_ATTRIBUTE_NORMAL
        if e.readOnly and not e.isDir:
            attr = FILE_ATTRIBUTE_READONLY
        flags = FD_ATTRIBUTES | FD_FILESIZE | FD_SHOWPROGRESSUI
        write = 0
        if e.mtime is not None:
            flags |= FD_WRITESTIME
            write = toFiletime(e.mtime)
        size = 0 if e.isDir else e.size
        name = e.name.encode('utf-16-le')[:_MAX_NAME * 2]
        name = name + b'\x00' * (520 - len(name))
        out.append(struct.pack('<I16s8s8sIQQQII', flags, b'', b'', b'', attr, 0, 0, write,
                               size >> 32, size & 0xFFFFFFFF) + name)
    return b''.join(out)


def parseDescriptors(data):
    """
    @summary: Decode a FILEGROUPDESCRIPTORW
    @return: [FileEntry] (path is None)
    """
    if len(data) < 4:
        return []
    count = struct.unpack_from('<I', data, 0)[0]
    entries = []
    for i in range(count):
        off = 4 + i * FILE_DESCRIPTOR_SIZE
        if off + FILE_DESCRIPTOR_SIZE > len(data):
            break
        flags = struct.unpack_from('<I', data, off)[0]
        attr = struct.unpack_from('<I', data, off + 36)[0]
        write = struct.unpack_from('<Q', data, off + 56)[0]
        hi, lo = struct.unpack_from('<II', data, off + 64)
        name = data[off + 72:off + 592].decode('utf-16-le', 'replace').split('\x00', 1)[0]
        isDir = bool(flags & FD_ATTRIBUTES and attr & FILE_ATTRIBUTE_DIRECTORY)
        size = ((hi << 32) | lo) if flags & FD_FILESIZE else None
        mtime = fromFiletime(write) if (flags & FD_WRITESTIME and write) else None
        entries.append(FileEntry(name, None, isDir, size, mtime))
    return entries


class LocalFileList(object):
    """
    @summary: Snapshot of local files / folders copied to the clipboard,
    expanded recursively into the flat list the server expects (folders
    first, names relative with '\\' separators, FreeRDP synthetic_file.c).
    """

    def __init__(self, paths):
        self.entries = []
        for path in paths:
            path = os.path.abspath(path)
            base = os.path.basename(path.rstrip('\\/')) or path
            self._add(base, path)

    def _add(self, name, path):
        if len(name) > _MAX_NAME:
            log.warning("clipboard: path too long for Windows, skipped: %s" % name)
            return
        try:
            st = os.stat(path)
        except OSError as e:
            log.warning("clipboard: cannot read %s: %s" % (path, e))
            return
        if os.path.isdir(path):
            self.entries.append(FileEntry(name, path, True, 0, st.st_mtime))
            try:
                children = sorted(os.listdir(path))
            except OSError as e:
                log.warning("clipboard: cannot list %s: %s" % (path, e))
                return
            for child in children:
                self._add(name + '\\' + child, os.path.join(path, child))
        elif os.path.isfile(path):
            readOnly = not os.access(path, os.W_OK)
            self.entries.append(FileEntry(name, path, False, st.st_size, st.st_mtime, readOnly))

    def descriptors(self):
        return packDescriptors(self.entries)

    def size(self, index):
        e = self.entries[index]
        return 0 if e.isDir else os.path.getsize(e.path)

    def read(self, index, position, length):
        e = self.entries[index]
        if e.isDir:
            return b''
        with open(e.path, 'rb') as f:
            f.seek(position)
            return f.read(length)


def safeRelativePath(name):
    """
    @summary: Turn a name sent by the server into a relative local path that
    can not leave the download folder ('..', drive letters, absolute paths
    and characters invalid on Windows are removed).
    @return: {str} or None when nothing usable is left
    """
    parts = []
    for part in re.split(r'[\\/]+', name):
        part = _FORBIDDEN.sub('_', part).strip().rstrip('.')
        if part in ('', '.', '..'):
            continue
        parts.append(part)
    return os.path.join(*parts) if parts else None


class RemoteDownload(object):
    """
    @summary: Download of the files the server put on its clipboard.
    One File Contents request is in flight at a time; the caller sends the
    requests returned by nextRequest() and feeds the responses back.
    """

    CHUNK = 1024 * 1024

    def __init__(self, entries, root, clipDataId=None):
        self.entries = entries
        self.root = root
        self.clipDataId = clipDataId
        self.index = -1
        self.position = 0
        self.file = None
        self.streamId = 0
        self.received = 0
        self.failed = 0
        self.started = monotonic()
        self.total = sum(e.size or 0 for e in entries if not e.isDir)
        self.topLevel = []  # local paths of the top-level items (for the clipboard)
        self._sizeKnown = False
        self._localPaths = {}

    def _localPath(self, i):
        rel = safeRelativePath(self.entries[i].name)
        return None if rel is None else os.path.join(self.root, rel)

    def prepare(self):
        """Create the folders and empty files, collect the top-level items."""
        seen = set()
        for i, e in enumerate(self.entries):
            path = self._localPath(i)
            if path is None:
                log.warning("clipboard: invalid remote file name skipped: %r" % e.name)
                continue
            self._localPaths[i] = path
            top = os.path.join(self.root, os.path.relpath(path, self.root).split(os.sep)[0])
            if top not in seen:
                seen.add(top)
                self.topLevel.append(top)
            if e.isDir:
                makedirs(path)

    def nextRequest(self, newStreamId):
        """
        @return: (streamId, index, flags, position, length) for the next File
        Contents request, or None when every file is done
        """
        if self.file is None:
            if not self._openNext():
                return None
        e = self.entries[self.index]
        self.streamId = newStreamId
        if e.size is None and not self._sizeKnown:
            return (self.streamId, self.index, FILECONTENTS_SIZE, 0, 8)
        length = min(self.CHUNK, e.size - self.position)
        return (self.streamId, self.index, FILECONTENTS_RANGE, self.position, length)

    def _openNext(self):
        while True:
            self.index += 1
            if self.index >= len(self.entries):
                return False
            e = self.entries[self.index]
            path = self._localPaths.get(self.index)
            if e.isDir or path is None:
                continue
            makedirs(os.path.dirname(path))
            self.file = open(path, 'wb')
            self.position = 0
            self._sizeKnown = e.size is not None
            if self._sizeKnown and e.size == 0:
                self._closeFile()
                continue
            return True

    def _closeFile(self):
        e = self.entries[self.index]
        path = self._localPaths.get(self.index)
        if self.file is not None:
            self.file.close()
            self.file = None
        if path and e.mtime:
            try:
                os.utime(path, (e.mtime, e.mtime))
            except OSError:
                pass

    def onResponse(self, ok, data):
        """Process the File Contents Response of the current request."""
        e = self.entries[self.index]
        if not ok:
            log.warning("clipboard: server refused %s, skipped" % e.name)
            self.failed += 1
            self._closeFile()
            return
        if e.size is None and not self._sizeKnown:
            e.size = struct.unpack_from('<Q', data.ljust(8, b'\x00'))[0]
            self.total += e.size
            self._sizeKnown = True
            if e.size == 0:
                self._closeFile()
            return
        self.file.write(data)
        self.position += len(data)
        self.received += len(data)
        if self.position >= e.size or not data:
            if self.position < e.size:
                log.warning("clipboard: %s truncated (%d of %d bytes)" % (e.name, self.position, e.size))
                self.failed += 1
            self._closeFile()

    def abort(self):
        if self.file is not None:
            self.file.close()
            self.file = None


_downloadRoots = []


def newDownloadFolder():
    """Temporary folder for one paste; all of them are removed at exit."""
    root = tempfile.mkdtemp(prefix="rdpyqt-clipboard-")
    if not _downloadRoots:
        import atexit
        atexit.register(_cleanup)
    _downloadRoots.append(root)
    return root


def _cleanup():
    for root in _downloadRoots:
        shutil.rmtree(root, ignore_errors=True)
