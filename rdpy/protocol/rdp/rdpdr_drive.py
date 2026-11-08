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
RDPDR drive redirection device (MS-RDPEFS 2.2.3 File System Virtual Channel).
Exposes a local directory to the server as a redirected drive.

Ported from FreeRDP channels/drive/client (drive_main.c, drive_file.c).
Like FreeRDP's asynchronous mode, each drive processes its IRPs on its own
worker thread, so large file transfers do not block the Qt / Twisted
main loop. Completions are sent back from the reactor thread.

Protocol reference: [MS-RDPEFS], [MS-FSCC]
"""
from __future__ import absolute_import, division, print_function

import errno
import os
from rdpy.core.compat import queue, replace as _replace, isErrno
import re
import shutil
import stat
import struct
import threading
import zlib

import rdpy.core.log as log
from rdpy.core.filetimes import EPOCH_AS_FILETIME, HUNDREDS_OF_NANOSECONDS
from . import rdpdr
from .rdpdr import (
    RdpdrDevice, decodeUnicodeString, RDPDR_DTYP_FILESYSTEM,
    IRP_MJ_CREATE, IRP_MJ_CLOSE, IRP_MJ_READ, IRP_MJ_WRITE, IRP_MJ_QUERY_INFORMATION,
    IRP_MJ_SET_INFORMATION, IRP_MJ_QUERY_VOLUME_INFORMATION, IRP_MJ_DIRECTORY_CONTROL,
    IRP_MJ_DEVICE_CONTROL, IRP_MJ_LOCK_CONTROL, IRP_MN_QUERY_DIRECTORY,
    STATUS_SUCCESS, STATUS_UNSUCCESSFUL, STATUS_NOT_SUPPORTED, STATUS_ACCESS_DENIED,
    STATUS_NO_SUCH_FILE, STATUS_DEVICE_BUSY, STATUS_OBJECT_NAME_COLLISION,
    STATUS_INVALID_HANDLE, STATUS_NO_MORE_FILES, STATUS_NOT_A_DIRECTORY,
    STATUS_OBJECT_PATH_NOT_FOUND, STATUS_DIRECTORY_NOT_EMPTY, STATUS_DISK_FULL,
    STATUS_OBJECT_NAME_INVALID, STATUS_INVALID_PARAMETER, STATUS_FILE_LOCK_CONFLICT,
    STATUS_LOCK_NOT_GRANTED, STATUS_RANGE_NOT_LOCKED, STATUS_INSUFFICIENT_RESOURCES,
    STATUS_INVALID_LOCK_RANGE,
)

_IS_WINDOWS = os.name == 'nt'
_O_BINARY = getattr(os, 'O_BINARY', 0)

# Resource limits: a server must not be able to exhaust the client memory
_MAX_READ_LENGTH = 16 * 1024 * 1024  # bytes returned by one IRP_MJ_READ
_MAX_QUEUED_IRPS = 256  # IRPs waiting for the worker thread
_MAX_QUEUED_BYTES = 64 * 1024 * 1024  # request bytes waiting for the worker thread

# DR_WRITE_REQ.Offset value meaning "append" (client minor version >= 0x000D)
_APPEND_OFFSET = 0xFFFFFFFFFFFFFFFF
_MAX_FILE_OFFSET = 0x7FFFFFFFFFFFFFFF

# DR_DRIVE_LOCK_REQ.Operation (MS-RDPEFS 2.2.3.3.12)
RDP_LOWIO_OP_SHAREDLOCK = 0x00000002
RDP_LOWIO_OP_EXCLUSIVELOCK = 0x00000003
RDP_LOWIO_OP_UNLOCK = 0x00000004
RDP_LOWIO_OP_UNLOCK_MULTIPLE = 0x00000005

# Access mask ([MS-SMB2] 2.2.13.1)
FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_EXECUTE = 0x00000020
MAXIMUM_ALLOWED = 0x02000000
GENERIC_ALL = 0x10000000
GENERIC_EXECUTE = 0x20000000
GENERIC_WRITE = 0x40000000
GENERIC_READ = 0x80000000

_NEED_WRITE = FILE_WRITE_DATA | FILE_APPEND_DATA | GENERIC_WRITE | GENERIC_ALL
_NEED_READ = FILE_READ_DATA | FILE_EXECUTE | GENERIC_READ | GENERIC_EXECUTE | GENERIC_ALL | MAXIMUM_ALLOWED

# CreateDisposition
FILE_SUPERSEDE = 0x00000000
FILE_OPEN = 0x00000001
FILE_CREATE = 0x00000002
FILE_OPEN_IF = 0x00000003
FILE_OVERWRITE = 0x00000004
FILE_OVERWRITE_IF = 0x00000005

# CreateOptions
FILE_DIRECTORY_FILE = 0x00000001
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_DELETE_ON_CLOSE = 0x00001000

# Create response Information
FILE_SUPERSEDED = 0x00000000
FILE_OPENED = 0x00000001
FILE_OVERWRITTEN = 0x00000003

# File attributes ([MS-FSCC] 2.6)
FILE_ATTRIBUTE_READONLY = 0x00000001
FILE_ATTRIBUTE_HIDDEN = 0x00000002
FILE_ATTRIBUTE_SYSTEM = 0x00000004
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_ARCHIVE = 0x00000020
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

# File information classes ([MS-FSCC] 2.4)
FileDirectoryInformation = 1
FileFullDirectoryInformation = 2
FileBothDirectoryInformation = 3
FileBasicInformation = 4
FileStandardInformation = 5
FileRenameInformation = 10
FileNamesInformation = 12
FileDispositionInformation = 13
FileAllocationInformation = 19
FileEndOfFileInformation = 20
FileAttributeTagInformation = 35

# File system information classes ([MS-FSCC] 2.5)
FileFsVolumeInformation = 1
FileFsSizeInformation = 3
FileFsDeviceInformation = 4
FileFsAttributeInformation = 5
FileFsFullSizeInformation = 7

FILE_DEVICE_DISK = 0x00000007
FILE_CASE_SENSITIVE_SEARCH = 0x00000001
FILE_CASE_PRESERVED_NAMES = 0x00000002
FILE_UNICODE_ON_DISK = 0x00000004
MAX_PATH = 260

# Values reported for the volume (FreeRDP reports "FAT32" with the same flags)
VOLUME_LABEL = "RDPYQT"
FILE_SYSTEM_NAME = "FAT32"
_BYTES_PER_SECTOR = 512
_SECTORS_PER_CLUSTER = 8

# Characters forbidden in the DEVICE_ANNOUNCE name (MS-RDPEFS 2.2.1.3)
_FORBIDDEN_NAME_CHARS = ':<>"/\\| '


def defaultDriveName(path, pathmod=os.path):
    """
    @summary: Name used when none is given: the directory name, or the
    drive letter for a Windows drive root ("C:\\" -> "C")
    @param path: {str} absolute path
    @return: {str}
    """
    drive, rest = pathmod.splitdrive(path)
    name = pathmod.basename(rest.rstrip('\\/'))
    if not name and drive:
        name = drive.rstrip(':\\/').split('\\')[-1]
    return name or "drive"


class _IrpError(Exception):
    """Abort IRP processing and answer with an NTSTATUS."""

    def __init__(self, status):
        Exception.__init__(self, "NTSTATUS 0x%08x" % status)
        self.status = status


def mapOSError(e):
    """
    @summary: Map a Python OSError to an NTSTATUS (FreeRDP drive_map_windows_err)
    @param e: {OSError}
    @return: {int} NTSTATUS
    """
    code = getattr(e, 'errno', None)
    if code in (errno.EACCES, errno.EPERM, errno.EROFS, errno.EISDIR):
        return STATUS_ACCESS_DENIED
    if code == errno.ENOENT:
        return STATUS_NO_SUCH_FILE
    if code == errno.EEXIST:
        return STATUS_OBJECT_NAME_COLLISION
    if code == errno.ENOTDIR:
        return STATUS_OBJECT_PATH_NOT_FOUND
    if code == errno.ENOTEMPTY:
        return STATUS_DIRECTORY_NOT_EMPTY
    if code in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1)):
        return STATUS_DISK_FULL
    if code == errno.EBUSY:
        return STATUS_DEVICE_BUSY
    if code == errno.ENAMETOOLONG:
        return STATUS_OBJECT_NAME_INVALID
    if code == errno.EBADF:
        return STATUS_INVALID_HANDLE
    return STATUS_UNSUCCESSFUL


def toFiletime(seconds):
    """
    @summary: POSIX timestamp (float seconds) to Windows FILETIME
    """
    value = int(seconds * HUNDREDS_OF_NANOSECONDS) + EPOCH_AS_FILETIME
    return max(0, min(value, 0x7FFFFFFFFFFFFFFF))


def fromFiletime(filetime):
    """
    @summary: Windows FILETIME to POSIX timestamp (float seconds)
    """
    return (filetime - EPOCH_AS_FILETIME) / float(HUNDREDS_OF_NANOSECONDS)


def _creationTime(st):
    birth = getattr(st, 'st_birthtime', None)
    if birth is not None:
        return birth
    # st_ctime is the creation time on Windows but the change time on POSIX
    return st.st_ctime if _IS_WINDOWS else st.st_mtime


def fileAttributes(name, st):
    """
    @summary: Windows file attributes for a stat result (winpr FileAttributesFromStat)
    """
    winAttr = getattr(st, 'st_file_attributes', None)
    if winAttr is not None:
        attr = winAttr & (FILE_ATTRIBUTE_READONLY | FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM |
                          FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_ARCHIVE)
    else:
        attr = 0
        if stat.S_ISDIR(st.st_mode):
            attr |= FILE_ATTRIBUTE_DIRECTORY
        if not (st.st_mode & stat.S_IWUSR):
            attr |= FILE_ATTRIBUTE_READONLY
        if name.startswith('.') and name not in ('.', '..'):
            attr |= FILE_ATTRIBUTE_HIDDEN
    return attr or FILE_ATTRIBUTE_NORMAL


def _wildcardToRegex(pattern):
    """
    @summary: Compile a Windows search pattern (* ? and the DOS wildcards < > ")
    into a case-insensitive regular expression
    """
    if pattern in ('*', '*.*', '<.*'):
        return None  # match everything
    out = []
    for c in pattern:
        if c in ('*', '<'):
            out.append('.*')
        elif c == '?':
            out.append('.')
        elif c == '>':
            out.append('.?')
        elif c == '"':
            out.append(r'\.?')
        else:
            out.append(re.escape(c))
    return re.compile('^' + ''.join(out) + '$', re.IGNORECASE | re.DOTALL)


class _FindEntry(object):
    __slots__ = ('name', 'st')

    def __init__(self, name, st):
        self.name = name
        self.st = st


def _isLink(st):
    """
    @summary: True for a symbolic link, or a reparse point (junction) on Windows
    @param st: {os.stat_result} result of os.lstat
    """
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, 'st_file_attributes', 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _errorBody(majorFunction):
    """
    @summary: Minimal response body sent with an error status, so the reply
    always has the layout the server expects for the request
    @param majorFunction: {int} IRP_MJ_*
    @return: {bytearray}
    """
    if majorFunction in (IRP_MJ_CREATE, IRP_MJ_WRITE, IRP_MJ_DIRECTORY_CONTROL):
        return bytearray(5)  # FileId/Length (4) + Information/Padding (1)
    return bytearray(4)  # Length / Padding


class DriveFile(object):
    """
    @summary: An open file or directory on a redirected drive (FreeRDP DRIVE_FILE)
    """

    def __init__(self, fileId, fullpath, isDir, fd, openFlags, readable=False, writable=False):
        self.id = fileId
        self.fullpath = fullpath
        self.isDir = isDir
        # OS file descriptor, None for directories and attribute-only opens
        self.fd = fd
        # flags used to reopen the file (rename on Windows, move across devices)
        self.openFlags = openFlags
        self.readable = readable
        self.writable = writable
        self.deletePending = False
        self.findEntries = None
        self.findIndex = 0
        # identity of the file in the byte-range lock table, see DriveDevice._lockKey
        self.lockKey = None
        # bytes the server read from / wrote to this file (transfer events)
        self.bytesRead = 0
        self.bytesWritten = 0

    def stat(self):
        if self.fd is not None:
            return os.fstat(self.fd)
        return os.stat(self.fullpath)

    def closeHandle(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def reopen(self):
        """
        @summary: Reopen the file at its (new) path with its access mode. On
        failure the handle is left closed (reads and writes are then denied).
        """
        self.closeHandle()
        if self.isDir:
            return
        try:
            self.fd = os.open(self.fullpath, self.openFlags)
        except OSError as e:
            log.warning("RDPDR drive: cannot reopen '%s': %s" % (self.fullpath, e))
            self.fd = None
            self.readable = self.writable = False


class DriveDevice(RdpdrDevice):
    """
    @summary: Redirect a local directory as a drive on the server
    """

    def __init__(self, name, path, asyncIo=True):
        """
        @param name: {str} name of the drive shown on the server
        @param path: {str} local directory to share
        @param asyncIo: {bool} process IRPs on a worker thread (default) or inline
        """
        if not path or not str(path).strip():
            # abspath('') is the working directory: never share it by accident
            raise ValueError("drive path is empty")
        basePath = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(basePath):
            raise ValueError("drive path is not a directory: %s" % path)
        if not name:
            name = defaultDriveName(basePath)

        # Filter 2.2.1.3 Device Announce Header forbidden symbols
        safeName = ''.join('_' if c in _FORBIDDEN_NAME_CHARS else c for c in name)
        RdpdrDevice.__init__(self, RDPDR_DTYP_FILESYSTEM, safeName,
                             safeName.encode('utf-8') + b'\x00')
        self.basePath = basePath
        # symlinks and junctions are resolved against the real share root
        self._realBase = os.path.normcase(os.path.realpath(basePath))
        self._files = {}
        # byte-range locks: {lockKey: [(fileId, offset, length, exclusive)]}
        self._locks = {}
        self._asyncIo = asyncIo
        self._queue = None
        self._thread = None
        self._pendingLock = threading.Lock()
        self._pendingCount = 0
        self._pendingBytes = 0
        if asyncIo:
            self._queue = queue.Queue()
            self._thread = threading.Thread(target=self._threadFunc,
                                            name="rdpdr-drive-%s" % safeName)
            self._thread.daemon = True
            self._thread.start()

    # -------------------------------------------------------------------
    # RdpdrDevice interface
    # -------------------------------------------------------------------

    def setTransferCallback(self, callback):
        """
        @summary: callback(direction, path, bytes) when the server closes a file it
        read ('upload': local file sent to the server) or wrote ('download': file
        received from the server). Called from the drive worker thread.
        """
        self._transferCallback = callback

    def _reportTransfer(self, f):
        callback = getattr(self, '_transferCallback', None)
        if callback is None or f.isDir:
            return
        for direction, count in (('upload', f.bytesRead), ('download', f.bytesWritten)):
            if count:
                try:
                    callback(direction, f.fullpath, count)
                except Exception as e:
                    log.warning("RDPDR drive: transfer callback failed: %s" % e)

    def irpRequest(self, irp):
        if not self._asyncIo:
            self._processIrp(irp)
            return
        size = irp.input.remaining()
        with self._pendingLock:
            accepted = (self._pendingCount < _MAX_QUEUED_IRPS and
                        self._pendingBytes + size <= _MAX_QUEUED_BYTES)
            if accepted:
                self._pendingCount += 1
                self._pendingBytes += size
        if not accepted:
            log.warning("RDPDR drive: too many pending requests, rejecting IRP 0x%x" %
                        irp.majorFunction)
            irp.output = _errorBody(irp.majorFunction)
            irp.ioStatus = STATUS_INSUFFICIENT_RESOURCES
            irp.complete()
            return
        self._queue.put(('irp', irp, size))

    def init(self):
        # The server forgot every handle when it restarts the channel
        if self._asyncIo:
            self._queue.put(('reset', None, 0))
        else:
            self._closeAll()

    def free(self):
        if self._asyncIo:
            self._queue.put(('quit', None, 0))
        else:
            self._closeAll()

    # -------------------------------------------------------------------
    # Worker thread (FreeRDP drive_thread_func)
    # -------------------------------------------------------------------

    def _threadFunc(self):
        while True:
            kind, irp, size = self._queue.get()
            if kind == 'quit':
                self._closeAll()
                break
            if kind == 'reset':
                self._closeAll()
                continue
            with self._pendingLock:
                self._pendingCount -= 1
                self._pendingBytes -= size
            if irp.isCancelled():
                # queued before a disconnect or a channel reset: the server
                # forgot this request, do not modify the file system for it
                irp.discard()
                continue
            try:
                self._processIrp(irp)
            except Exception as e:  # never let the worker die
                log.error("RDPDR drive: unexpected error: %s" % e)

    def _closeAll(self):
        """
        @summary: Close every handle (connection closed or channel reset).
        Pending delete-on-close requests are honoured, as a real close would.
        """
        files = list(self._files.values())
        self._files.clear()
        self._locks.clear()
        for f in files:
            f.closeHandle()
        # files before directories, deepest paths first
        pending = [f for f in files if f.deletePending]
        pending.sort(key=lambda f: (f.isDir, -len(f.fullpath)))
        for f in pending:
            try:
                self._deleteFile(f)
            except (OSError, _IrpError) as e:
                log.warning("RDPDR drive: delete-on-close of '%s' failed: %s" % (f.fullpath, e))

    # -------------------------------------------------------------------
    # IRP dispatch (FreeRDP drive_process_irp)
    # -------------------------------------------------------------------

    def _processIrp(self, irp):
        irp.ioStatus = STATUS_SUCCESS
        handlers = {
            IRP_MJ_CREATE: self._irpCreate,
            IRP_MJ_CLOSE: self._irpClose,
            IRP_MJ_READ: self._irpRead,
            IRP_MJ_WRITE: self._irpWrite,
            IRP_MJ_QUERY_INFORMATION: self._irpQueryInformation,
            IRP_MJ_SET_INFORMATION: self._irpSetInformation,
            IRP_MJ_QUERY_VOLUME_INFORMATION: self._irpQueryVolumeInformation,
            IRP_MJ_LOCK_CONTROL: self._irpLockControl,
            IRP_MJ_DIRECTORY_CONTROL: self._irpDirectoryControl,
            IRP_MJ_DEVICE_CONTROL: self._irpDeviceControl,
        }
        handler = handlers.get(irp.majorFunction)
        try:
            if handler is None:
                irp.ioStatus = STATUS_NOT_SUPPORTED
            else:
                handler(irp)
        except ValueError as e:
            # Malformed request: FreeRDP drops it, which leaves the server
            # waiting forever. Answer it with an error instead.
            log.warning("RDPDR drive: malformed IRP 0x%x: %s" % (irp.majorFunction, e))
            irp.output = _errorBody(irp.majorFunction)
            irp.ioStatus = STATUS_INVALID_PARAMETER
        except _IrpError as e:
            irp.output = _errorBody(irp.majorFunction)
            irp.ioStatus = e.status
        except OSError as e:
            irp.output = _errorBody(irp.majorFunction)
            irp.ioStatus = mapOSError(e)
        except Exception as e:
            # Any other failure must still answer the server, otherwise the
            # request stays pending forever on the remote side.
            log.error("RDPDR drive: IRP 0x%x failed: %r" % (irp.majorFunction, e))
            irp.output = _errorBody(irp.majorFunction)
            irp.ioStatus = STATUS_UNSUCCESSFUL
        irp.complete()

    def _getFile(self, fileId):
        return self._files.get(fileId)

    def _resolvePath(self, rawPath):
        """
        @summary: Combine the drive base path with a server path
        (FreeRDP drive_file_combine_fullpath). Rejects '..' components and,
        on Windows clients, components that would escape the base directory
        (drive letters, alternate data streams).
        @param rawPath: {str} path relative to the drive root, '\\' separated
        @return: {str} local absolute path
        """
        parts = []
        for part in re.split(r'[\\/]', rawPath):
            if part in ('', '.'):
                continue
            if part == '..':
                log.warning("RDPDR drive: received invalid file path '%s' from server, aborting!" % rawPath)
                raise _IrpError(STATUS_ACCESS_DENIED)
            if _IS_WINDOWS and (':' in part or part.rstrip('. ') != part):
                raise _IrpError(STATUS_OBJECT_NAME_INVALID)
            if '\x00' in part:
                raise _IrpError(STATUS_OBJECT_NAME_INVALID)
            parts.append(part)
        fullpath = os.path.join(self.basePath, *parts) if parts else self.basePath
        if not self._isInside(fullpath):
            # a symlink / junction inside the share points outside of it
            log.warning("RDPDR drive: path '%s' resolves outside of the shared directory, denied" % rawPath)
            raise _IrpError(STATUS_ACCESS_DENIED)
        return fullpath

    def _isInside(self, path):
        """
        @summary: True when path, with every symlink / junction resolved, is
        the share root or below it
        """
        real = os.path.normcase(os.path.realpath(path))
        base = self._realBase
        if real == base:
            return True
        prefix = base if base.endswith(os.sep) else base + os.sep
        return real.startswith(prefix)

    def _isRoot(self, path):
        return os.path.normcase(os.path.realpath(path)) == self._realBase

    def _deleteFile(self, f):
        """
        @summary: Delete the file or (empty) directory of a closed handle.
        The share root itself is never removed.
        """
        if self._isRoot(f.fullpath):
            raise _IrpError(STATUS_ACCESS_DENIED)
        if f.isDir:
            os.rmdir(f.fullpath)
        else:
            os.remove(f.fullpath)

    # -------------------------------------------------------------------
    # IRP_MJ_CREATE (MS-RDPEFS 2.2.1.4.1 / 2.2.1.5.1)
    # -------------------------------------------------------------------

    def _irpCreate(self, irp):
        s = irp.input
        desiredAccess = s.u32()
        s.u64()  # AllocationSize: a hint only, it does not change the end of file
        fileAttributes = s.u32()
        s.u32()  # SharedAccess, not enforced on POSIX (like FreeRDP)
        createDisposition = s.u32()
        createOptions = s.u32()
        pathLength = s.u32()
        path = decodeUnicodeString(s.read(pathLength))

        fileId = 0
        information = 0
        try:
            fullpath = self._resolvePath(path)
            fileId = self.devman.nextId()
            f = self._openFile(fileId, fullpath, desiredAccess, createDisposition,
                               createOptions, fileAttributes)
            self._files[fileId] = f
            if createDisposition == FILE_OPEN_IF:
                information = FILE_OPENED
            elif createDisposition == FILE_OVERWRITE_IF:
                information = FILE_OVERWRITTEN
            else:
                information = FILE_SUPERSEDED
            log.debug("RDPDR drive: create '%s' -> fileId=%d dir=%s" % (path, fileId, f.isDir))
        except _IrpError as e:
            irp.ioStatus = e.status
            fileId = 0
        except OSError as e:
            irp.ioStatus = mapOSError(e)
            fileId = 0
            log.debug("RDPDR drive: create '%s' failed: %s" % (path, e))

        irp.output += struct.pack('<IB', fileId, information)

    def _openFile(self, fileId, fullpath, desiredAccess, createDisposition, createOptions,
                  fileAttributes):
        """
        @summary: FreeRDP drive_file_init
        """
        try:
            st = os.stat(fullpath)
            exists = True
        except OSError as e:
            if isErrno(e, errno.ENOTDIR):
                raise _IrpError(STATUS_OBJECT_PATH_NOT_FOUND)
            if not isErrno(e, errno.ENOENT):
                raise
            exists = False

        if exists:
            if stat.S_ISDIR(st.st_mode):
                if createDisposition == FILE_CREATE:
                    raise _IrpError(STATUS_OBJECT_NAME_COLLISION)
                if createOptions & FILE_NON_DIRECTORY_FILE:
                    raise _IrpError(STATUS_ACCESS_DENIED)
                if createOptions & FILE_DELETE_ON_CLOSE and self._isRoot(fullpath):
                    raise _IrpError(STATUS_ACCESS_DENIED)
                f = DriveFile(fileId, fullpath, True, None, 0)
                f.deletePending = bool(createOptions & FILE_DELETE_ON_CLOSE)
                return f
            if createOptions & FILE_DIRECTORY_FILE:
                raise _IrpError(STATUS_NOT_A_DIRECTORY)
            if not stat.S_ISREG(st.st_mode):
                # FIFOs, sockets and devices could block the worker forever
                raise _IrpError(STATUS_ACCESS_DENIED)
        else:
            parent = os.path.dirname(fullpath)
            if not os.path.isdir(parent):
                raise _IrpError(STATUS_OBJECT_PATH_NOT_FOUND)
            if createOptions & FILE_DIRECTORY_FILE:
                # only create the directory if the disposition allows it
                if createDisposition in (FILE_OPEN_IF, FILE_CREATE):
                    os.mkdir(fullpath)
                    f = DriveFile(fileId, fullpath, True, None, 0)
                    f.deletePending = bool(createOptions & FILE_DELETE_ON_CLOSE)
                    return f
                raise _IrpError(STATUS_NO_SUCH_FILE)
            if createDisposition in (FILE_OPEN, FILE_OVERWRITE):
                raise _IrpError(STATUS_NO_SUCH_FILE)

        flags = _O_BINARY
        if createDisposition in (FILE_SUPERSEDE, FILE_OVERWRITE_IF):  # CREATE_ALWAYS
            flags |= os.O_CREAT | os.O_TRUNC
        elif createDisposition == FILE_CREATE:  # CREATE_NEW
            flags |= os.O_CREAT | os.O_EXCL
        elif createDisposition == FILE_OPEN_IF:  # OPEN_ALWAYS
            flags |= os.O_CREAT
        elif createDisposition == FILE_OVERWRITE:  # TRUNCATE_EXISTING
            flags |= os.O_TRUNC

        mustWrite = bool(desiredAccess & _NEED_WRITE) or bool(flags & os.O_TRUNC)
        wantRead = bool(desiredAccess & _NEED_READ)
        mode = 0o444 if (fileAttributes & FILE_ATTRIBUTE_READONLY) else 0o666

        readable = writable = False
        if mustWrite:
            writable = True
            try:
                fd = os.open(fullpath, flags | os.O_RDWR, mode)
                readable = True
            except OSError as e:
                if wantRead or not isErrno(e, errno.EACCES, errno.EPERM):
                    raise
                # write-only access (e.g. a -w- file) does not need read permission
                fd = os.open(fullpath, flags | os.O_WRONLY, mode)
        elif desiredAccess & MAXIMUM_ALLOWED:
            try:
                fd = os.open(fullpath, flags | os.O_RDWR, mode)
                readable = writable = True
            except OSError as e:
                if not isErrno(e, errno.EACCES, errno.EPERM):
                    raise
                fd = os.open(fullpath, flags | os.O_RDONLY, mode)
                readable = True
        else:
            try:
                fd = os.open(fullpath, flags | os.O_RDONLY, mode)
                readable = True
            except OSError as e:
                # Opening only for attributes / delete does not need read
                # permission on the content.
                if exists and not wantRead and isErrno(e, errno.EACCES, errno.EPERM):
                    fd = None
                else:
                    raise
        if readable and writable:
            accessFlags = os.O_RDWR
        elif writable:
            accessFlags = os.O_WRONLY
        else:
            accessFlags = os.O_RDONLY
        openFlags = (flags & ~(os.O_CREAT | os.O_EXCL | os.O_TRUNC)) | accessFlags
        f = DriveFile(fileId, fullpath, False, fd, openFlags, readable, writable)
        f.deletePending = bool(createOptions & FILE_DELETE_ON_CLOSE)
        return f

    # -------------------------------------------------------------------
    # IRP_MJ_CLOSE
    # -------------------------------------------------------------------

    def _irpClose(self, irp):
        f = self._files.pop(irp.fileId, None)
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
        else:
            self._releaseLocks(f)
            f.closeHandle()
            self._reportTransfer(f)
            if f.deletePending:
                try:
                    self._deleteFile(f)
                except _IrpError as e:
                    irp.ioStatus = e.status
                except OSError as e:
                    irp.ioStatus = mapOSError(e)
        irp.output += b'\x00' * 4  # Padding

    # -------------------------------------------------------------------
    # IRP_MJ_READ / IRP_MJ_WRITE
    # -------------------------------------------------------------------

    def _irpRead(self, irp):
        length = irp.input.u32()
        offset = irp.input.u64()
        f = self._getFile(irp.fileId)
        data = b''
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
        elif f.fd is None or not f.readable:
            irp.ioStatus = STATUS_ACCESS_DENIED if not f.isDir else STATUS_UNSUCCESSFUL
        elif offset > _MAX_FILE_OFFSET:
            irp.ioStatus = STATUS_INVALID_PARAMETER
        elif length > 0:
            try:
                # never allocate more than the file can return, nor more than
                # _MAX_READ_LENGTH (a short read is a valid answer)
                available = max(0, os.fstat(f.fd).st_size - offset)
                length = min(length, available, _MAX_READ_LENGTH)
                conflict = self._ioConflict(f, offset, length, False)
                if conflict:
                    raise _IrpError(conflict)
                if length > 0:
                    os.lseek(f.fd, offset, os.SEEK_SET)
                chunks = []
                remaining = length
                while remaining > 0:
                    chunk = os.read(f.fd, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b''.join(chunks)
            except _IrpError as e:
                irp.ioStatus = e.status
                data = b''
            except OSError as e:
                irp.ioStatus = mapOSError(e)
                data = b''
        if data and f is not None:
            f.bytesRead += len(data)
        irp.output += struct.pack('<I', len(data)) + data

    def _irpWrite(self, irp):
        s = irp.input
        length = s.u32()
        offset = s.u64()
        s.skip(20)  # Padding
        data = s.read(length)
        f = self._getFile(irp.fileId)
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
            length = 0
        elif f.fd is None or not f.writable:
            irp.ioStatus = STATUS_ACCESS_DENIED
            length = 0
        elif offset > _MAX_FILE_OFFSET and offset != _APPEND_OFFSET:
            irp.ioStatus = STATUS_INVALID_PARAMETER
            length = 0
        else:
            try:
                if offset == _APPEND_OFFSET:
                    # MS-RDPEFS 2.2.1.4.4: write at the end of the file
                    offset = os.lseek(f.fd, 0, os.SEEK_END)
                conflict = self._ioConflict(f, offset, length, True)
                if conflict:
                    raise _IrpError(conflict)
                os.lseek(f.fd, offset, os.SEEK_SET)
                view = memoryview(data)
                while view:
                    written = os.write(f.fd, view)
                    view = view[written:]
                f.bytesWritten += length
            except _IrpError as e:
                irp.ioStatus = e.status
                length = 0
            except OSError as e:
                irp.ioStatus = mapOSError(e)
                length = 0
        irp.output += struct.pack('<IB', length, 0)  # Length + Padding

    # -------------------------------------------------------------------
    # IRP_MJ_QUERY_INFORMATION
    # -------------------------------------------------------------------

    def _irpQueryInformation(self, irp):
        infoClass = irp.input.u32()
        f = self._getFile(irp.fileId)
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
            irp.output += struct.pack('<I', 0)
            return
        try:
            st = f.stat()
        except OSError as e:
            irp.ioStatus = mapOSError(e)
            irp.output += struct.pack('<I', 0)
            return

        attr = fileAttributes(os.path.basename(f.fullpath), st)
        isDir = stat.S_ISDIR(st.st_mode)
        size = 0 if isDir else st.st_size
        if infoClass == FileBasicInformation:
            irp.output += struct.pack('<I', 36) + struct.pack(
                '<QQQQI',
                toFiletime(_creationTime(st)),  # CreationTime
                toFiletime(st.st_atime),  # LastAccessTime
                toFiletime(st.st_mtime),  # LastWriteTime
                toFiletime(st.st_mtime),  # ChangeTime
                attr)  # FileAttributes (Reserved(4) MUST NOT be added)
        elif infoClass == FileStandardInformation:
            irp.output += struct.pack('<I', 22) + struct.pack(
                '<QQIBB',
                size,  # AllocationSize
                size,  # EndOfFile
                st.st_nlink,  # NumberOfLinks
                1 if f.deletePending else 0,  # DeletePending
                1 if isDir else 0)  # Directory (Reserved(2) MUST NOT be added)
        elif infoClass == FileAttributeTagInformation:
            irp.output += struct.pack('<III', 8, attr, 0)  # FileAttributes + ReparseTag
        else:
            log.debug("RDPDR drive: unhandled FsInformationClass %d" % infoClass)
            irp.ioStatus = STATUS_NOT_SUPPORTED
            irp.output += struct.pack('<I', 0)

    # -------------------------------------------------------------------
    # IRP_MJ_SET_INFORMATION
    # -------------------------------------------------------------------

    def _irpSetInformation(self, irp):
        s = irp.input
        infoClass = s.u32()
        length = s.u32()
        s.skip(24)  # Padding
        body = rdpdr.StreamReader(s.read(length))
        f = self._getFile(irp.fileId)
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
        else:
            try:
                if infoClass == FileBasicInformation:
                    self._setBasicInformation(f, length, body)
                elif infoClass in (FileEndOfFileInformation, FileAllocationInformation):
                    self._setSize(f, infoClass, length, body)
                elif infoClass == FileDispositionInformation:
                    self._setDispositionInformation(f, length, body)
                elif infoClass == FileRenameInformation:
                    self._setRenameInformation(f, length, body)
                else:
                    log.debug("RDPDR drive: unhandled set FsInformationClass %d" % infoClass)
                    irp.ioStatus = STATUS_NOT_SUPPORTED
            except _IrpError as e:
                irp.ioStatus = e.status
            except OSError as e:
                irp.ioStatus = mapOSError(e)
        irp.output += struct.pack('<I', length)

    def _setBasicInformation(self, f, length, s):
        if length != 36:
            raise _IrpError(STATUS_INVALID_PARAMETER)
        s.u64()  # CreationTime: cannot be set portably
        lastAccess = s.u64()
        lastWrite = s.u64()
        change = s.u64()
        attributes = s.u32()

        def isSet(value):
            # 0: do not change, -1/-2: stop/resume automatic updates
            return 0 < value < 0xFFFFFFFFFFFFFFFE

        # ChangeTime (st_ctime on POSIX) cannot be set: it is updated by the
        # OS itself and MUST NOT be applied to LastWriteTime
        if isSet(lastAccess) or isSet(lastWrite):
            st = os.stat(f.fullpath)
            atime = fromFiletime(lastAccess) if isSet(lastAccess) else st.st_atime
            mtime = fromFiletime(lastWrite) if isSet(lastWrite) else st.st_mtime
            os.utime(f.fullpath, (atime, mtime))
        if attributes:
            st = os.stat(f.fullpath)
            if attributes & FILE_ATTRIBUTE_READONLY:
                mode = st.st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            else:
                mode = st.st_mode | stat.S_IWUSR
            if mode != st.st_mode:
                os.chmod(f.fullpath, stat.S_IMODE(mode))

    def _setSize(self, f, infoClass, length, s):
        if length != 8:
            raise _IrpError(STATUS_INVALID_PARAMETER)
        size = s.i64()
        if size < 0:
            raise _IrpError(STATUS_INVALID_PARAMETER)
        if f.isDir:
            raise _IrpError(STATUS_ACCESS_DENIED)
        current = f.stat().st_size
        if infoClass == FileAllocationInformation and size >= current:
            return  # growing the allocation does not change the end of file
        # changing the size needs a handle opened for writing: never truncate
        # through the pathname on behalf of a read-only / attribute handle
        if f.fd is None or not f.writable:
            raise _IrpError(STATUS_ACCESS_DENIED)
        os.ftruncate(f.fd, size)

    def _setDispositionInformation(self, f, length, s):
        if f.isDir and os.listdir(f.fullpath):
            raise _IrpError(STATUS_DIRECTORY_NOT_EMPTY)
        deletePending = s.u8() if length else 1
        if deletePending:
            st = os.stat(f.fullpath)
            if fileAttributes(os.path.basename(f.fullpath), st) & FILE_ATTRIBUTE_READONLY:
                raise _IrpError(STATUS_ACCESS_DENIED)
            if self._isRoot(f.fullpath):
                raise _IrpError(STATUS_ACCESS_DENIED)
        f.deletePending = bool(deletePending)

    def _setRenameInformation(self, f, length, s):
        if length < 6:
            raise _IrpError(STATUS_INVALID_PARAMETER)
        replaceIfExists = s.u8()
        s.u8()  # RootDirectory
        fileNameLength = s.u32()
        if length != 6 + fileNameLength:
            raise _IrpError(STATUS_INVALID_PARAMETER)
        target = self._resolvePath(decodeUnicodeString(s.read(fileNameLength)))
        if self._isRoot(f.fullpath):
            raise _IrpError(STATUS_ACCESS_DENIED)

        if os.path.lexists(target):
            sameFile = False
            try:
                sameFile = os.path.samefile(f.fullpath, target)  # case-only rename
            except OSError:
                pass
            if not sameFile:
                if not replaceIfExists:
                    raise _IrpError(STATUS_OBJECT_NAME_COLLISION)
                if os.path.isdir(target):
                    raise _IrpError(STATUS_ACCESS_DENIED)

        source = f.fullpath
        reopen = _IS_WINDOWS and f.fd is not None
        if reopen:
            # Windows cannot rename an open file
            f.closeHandle()
        try:
            crossDevice = False
            try:
                _replace(source, target)
            except OSError as e:
                if e.errno != errno.EXDEV:
                    raise
                # MOVEFILE_COPY_ALLOWED: copy + delete, the open descriptors
                # still point to the deleted original and must be reopened
                shutil.move(source, target)
                crossDevice = True
            self._relocateHandles(source, target, crossDevice)
        finally:
            if reopen:
                f.reopen()

    def _relocateHandles(self, source, target, crossDevice):
        """
        @summary: Update every open handle on the renamed file, or below the
        renamed directory, so path based operations use the new location
        """
        src = os.path.normcase(source)
        prefix = src if src.endswith(os.sep) else src + os.sep
        for other in list(self._files.values()):
            path = os.path.normcase(other.fullpath)
            if path == src:
                other.fullpath = target
            elif path.startswith(prefix):
                other.fullpath = os.path.join(target, other.fullpath[len(prefix):])
            else:
                continue
            if crossDevice and other.fd is not None:
                oldKey = other.lockKey
                other.reopen()
                if oldKey is not None:
                    other.lockKey = None
                    self._migrateLocks(oldKey, other)

    # -------------------------------------------------------------------
    # IRP_MJ_QUERY_VOLUME_INFORMATION
    # -------------------------------------------------------------------

    def _irpQueryVolumeInformation(self, irp):
        infoClass = irp.input.u32()
        clusterSize = _BYTES_PER_SECTOR * _SECTORS_PER_CLUSTER
        try:
            usage = shutil.disk_usage(self.basePath)
            totalUnits = usage.total // clusterSize
            freeUnits = usage.free // clusterSize
        except OSError:
            totalUnits = freeUnits = 0

        out = irp.output
        if infoClass == FileFsVolumeInformation:
            label = (VOLUME_LABEL + '\0').encode('utf-16-le')
            try:
                created = toFiletime(_creationTime(os.stat(self.basePath)))
            except OSError:
                created = 0
            serial = zlib.crc32(self.basePath.encode('utf-8', 'replace')) & 0xFFFFFFFF
            out += struct.pack('<I', 17 + len(label))
            out += struct.pack('<QIIB', created, serial, len(label), 0)  # Reserved(1) MUST NOT be added
            out += label
        elif infoClass == FileFsSizeInformation:
            out += struct.pack('<I', 24)
            out += struct.pack('<QQII', totalUnits, freeUnits, _SECTORS_PER_CLUSTER, _BYTES_PER_SECTOR)
        elif infoClass == FileFsAttributeInformation:
            fsName = (FILE_SYSTEM_NAME + '\0').encode('utf-16-le')
            out += struct.pack('<I', 12 + len(fsName))
            out += struct.pack('<III', FILE_CASE_SENSITIVE_SEARCH | FILE_CASE_PRESERVED_NAMES |
                               FILE_UNICODE_ON_DISK, MAX_PATH, len(fsName))
            out += fsName
        elif infoClass == FileFsFullSizeInformation:
            out += struct.pack('<I', 32)
            out += struct.pack('<QQQII', totalUnits, freeUnits, freeUnits, _SECTORS_PER_CLUSTER,
                               _BYTES_PER_SECTOR)
        elif infoClass == FileFsDeviceInformation:
            out += struct.pack('<III', 8, FILE_DEVICE_DISK, 0)
        else:
            log.debug("RDPDR drive: unhandled FsInformationClass %d" % infoClass)
            irp.ioStatus = STATUS_UNSUCCESSFUL
            out += struct.pack('<I', 0)

    # -------------------------------------------------------------------
    # IRP_MJ_LOCK_CONTROL / IRP_MJ_DEVICE_CONTROL
    # -------------------------------------------------------------------

    def _irpLockControl(self, irp):
        """
        @summary: Byte-range locks (DR_DRIVE_LOCK_REQ, MS-RDPEFS 2.2.3.3.12).
        Locks are enforced between the handles of this session with Windows
        semantics: overlapping exclusive locks conflict, shared locks only
        conflict with exclusive ones, reads and writes of other handles in a
        locked range fail with STATUS_FILE_LOCK_CONFLICT. A conflicting lock
        request fails at once with STATUS_LOCK_NOT_GRANTED (waiting for the
        range to be released is not supported).
        """
        s = irp.input
        operation = s.u32()
        s.u32()  # F + Padding
        numLocks = s.u32()
        s.skip(20)  # Padding2
        if numLocks * 16 > s.remaining():
            raise ValueError("lock request announces %d locks for %d bytes" % (numLocks, s.remaining()))
        ranges = []
        for _ in range(numLocks):
            length = s.u64()
            offset = s.u64()
            ranges.append((offset, length))

        try:
            f = self._getFile(irp.fileId)
            if f is None:
                raise _IrpError(STATUS_UNSUCCESSFUL)
            if f.isDir or f.fd is None:
                raise _IrpError(STATUS_INVALID_PARAMETER if f.isDir else STATUS_ACCESS_DENIED)
            if operation in (RDP_LOWIO_OP_SHAREDLOCK, RDP_LOWIO_OP_EXCLUSIVELOCK):
                self._lockRanges(f, ranges, operation == RDP_LOWIO_OP_EXCLUSIVELOCK)
            elif operation in (RDP_LOWIO_OP_UNLOCK, RDP_LOWIO_OP_UNLOCK_MULTIPLE):
                self._unlockRanges(f, ranges)
            else:
                raise _IrpError(STATUS_INVALID_PARAMETER)
        except _IrpError as e:
            irp.ioStatus = e.status
        except OSError as e:
            irp.ioStatus = mapOSError(e)
        irp.output += struct.pack('<I', 0)  # Padding

    def _lockKey(self, f):
        """
        @summary: Identity of the file shared by all its handles
        """
        if f.lockKey is None:
            st = f.stat()
            if st.st_ino:
                f.lockKey = (st.st_dev, st.st_ino)
            else:
                f.lockKey = os.path.normcase(os.path.realpath(f.fullpath))
        return f.lockKey

    @staticmethod
    def _overlaps(offset1, length1, offset2, length2):
        # zero length ranges never conflict
        if length1 == 0 or length2 == 0:
            return False
        return offset1 < offset2 + length2 and offset2 < offset1 + length1

    def _lockRanges(self, f, ranges, exclusive):
        for offset, length in ranges:
            if length and offset + length - 1 > 0xFFFFFFFFFFFFFFFF:
                raise _IrpError(STATUS_INVALID_LOCK_RANGE)
        key = self._lockKey(f)
        held = self._locks.get(key, [])
        granted = []
        for offset, length in ranges:
            for owner, o, l, excl in held + granted:
                if (exclusive or excl) and self._overlaps(offset, length, o, l):
                    raise _IrpError(STATUS_LOCK_NOT_GRANTED)
            granted.append((f.id, offset, length, exclusive))
        if granted:
            self._locks[key] = held + granted

    def _unlockRanges(self, f, ranges):
        if f.lockKey is None or f.lockKey not in self._locks:
            if ranges:
                raise _IrpError(STATUS_RANGE_NOT_LOCKED)
            return
        held = self._locks[f.lockKey]
        status = STATUS_SUCCESS
        for offset, length in ranges:
            for i, (owner, o, l, excl) in enumerate(held):
                if owner == f.id and o == offset and l == length:
                    del held[i]
                    break
            else:
                status = STATUS_RANGE_NOT_LOCKED
        if not held:
            del self._locks[f.lockKey]
        if status != STATUS_SUCCESS:
            raise _IrpError(status)

    def _releaseLocks(self, f):
        if f.lockKey is None or f.lockKey not in self._locks:
            return
        held = [entry for entry in self._locks[f.lockKey] if entry[0] != f.id]
        if held:
            self._locks[f.lockKey] = held
        else:
            del self._locks[f.lockKey]

    def _migrateLocks(self, oldKey, f):
        """
        @summary: Move the locks of a file whose identity changed (moved across devices)
        """
        held = self._locks.pop(oldKey, None)
        if not held:
            return
        try:
            newKey = self._lockKey(f)
        except OSError:
            return
        self._locks.setdefault(newKey, []).extend(held)

    def _ioConflict(self, f, offset, length, write):
        """
        @summary: NTSTATUS when a read / write of this handle hits a range
        locked by another handle (or a shared lock, for a write), else 0
        """
        if not self._locks or length == 0:
            return 0
        held = self._locks.get(self._lockKey(f))
        if not held:
            return 0
        for owner, o, l, excl in held:
            if not self._overlaps(offset, length, o, l):
                continue
            if write and (not excl or owner != f.id):
                return STATUS_FILE_LOCK_CONFLICT
            if not write and excl and owner != f.id:
                return STATUS_FILE_LOCK_CONFLICT
        return 0

    def _irpDeviceControl(self, irp):
        irp.output += struct.pack('<I', 0)  # OutputBufferLength

    # -------------------------------------------------------------------
    # IRP_MJ_DIRECTORY_CONTROL
    # -------------------------------------------------------------------

    def _irpDirectoryControl(self, irp):
        if irp.minorFunction == IRP_MN_QUERY_DIRECTORY:
            self._irpQueryDirectory(irp)
        else:
            # IRP_MN_NOTIFY_CHANGE_DIRECTORY is not implemented (as in FreeRDP)
            irp.ioStatus = STATUS_NOT_SUPPORTED
            irp.output += struct.pack('<I', 0)

    def _irpQueryDirectory(self, irp):
        s = irp.input
        infoClass = s.u32()
        initialQuery = s.u8()
        pathLength = s.u32()
        s.skip(23)  # Padding
        path = decodeUnicodeString(s.read(pathLength))

        f = self._getFile(irp.fileId)
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
            irp.output += struct.pack('<I', 0)
            return

        try:
            if initialQuery:
                f.findEntries = self._findFirst(path)
                f.findIndex = 0
            if not f.findEntries or f.findIndex >= len(f.findEntries):
                if initialQuery or f.findEntries is None:
                    raise _IrpError(STATUS_NO_SUCH_FILE)
                raise _IrpError(STATUS_NO_MORE_FILES)
            entry = f.findEntries[f.findIndex]
            f.findIndex += 1
            irp.output += self._encodeDirEntry(infoClass, entry)
        except _IrpError as e:
            irp.ioStatus = e.status
            irp.output += struct.pack('<IB', 0, 0)  # Length + Padding
        except OSError as e:
            irp.ioStatus = mapOSError(e)
            irp.output += struct.pack('<IB', 0, 0)

    def _findFirst(self, path):
        """
        @summary: Snapshot the entries matching a search path like '\\dir\\*'
        (FindFirstFileW + FindNextFileW)
        """
        sep = max(path.rfind('\\'), path.rfind('/'))
        dirPart, pattern = path[:sep + 1], path[sep + 1:]
        directory = self._resolvePath(dirPart)
        if not pattern:
            pattern = '*'

        if not re.search(r'[*?<>"]', pattern):
            # no wildcard: a single, exact entry
            fullpath = self._resolvePath(path)
            st = os.lstat(fullpath)
            if stat.S_ISLNK(st.st_mode):
                st = os.stat(fullpath)
            return [_FindEntry(os.path.basename(fullpath), st)]

        regex = _wildcardToRegex(pattern)
        dirStat = os.stat(directory)
        if not stat.S_ISDIR(dirStat.st_mode):
            raise _IrpError(STATUS_NOT_A_DIRECTORY)

        entries = []
        # '.' and '..' are reported like FindFirstFile does on a real file system;
        # '..' uses the directory itself so nothing outside the share is touched
        for dot in ('.', '..'):
            if regex is None or regex.match(dot):
                entries.append(_FindEntry(dot, dirStat))
        for name in sorted(os.listdir(directory)):
            if regex is not None and not regex.match(name):
                continue
            full = os.path.join(directory, name)
            try:
                lst = os.lstat(full)
            except OSError:
                continue
            st = lst
            if _isLink(lst):
                if not self._isInside(full):
                    continue  # symlink / junction leading outside of the share: hidden
                try:
                    st = os.stat(full)
                except OSError:
                    pass  # dangling symlink
            entries.append(_FindEntry(name, st))
        return entries

    def _encodeDirEntry(self, infoClass, entry):
        st = entry.st
        nameW = entry.name.encode('utf-16-le', 'surrogatepass')
        isDir = stat.S_ISDIR(st.st_mode)
        size = 0 if isDir else st.st_size
        attr = fileAttributes(entry.name, st)
        times = struct.pack('<QQQQ',
                            toFiletime(_creationTime(st)),  # CreationTime
                            toFiletime(st.st_atime),  # LastAccessTime
                            toFiletime(st.st_mtime),  # LastWriteTime
                            toFiletime(st.st_mtime))  # ChangeTime
        sizes = struct.pack('<QQI', size, size, attr)  # EndOfFile, AllocationSize, FileAttributes
        header = struct.pack('<II', 0, 0)  # NextEntryOffset, FileIndex

        if infoClass == FileDirectoryInformation:
            body = header + times + sizes + struct.pack('<I', len(nameW)) + nameW
        elif infoClass == FileFullDirectoryInformation:
            body = header + times + sizes + struct.pack('<II', len(nameW), 0) + nameW  # + EaSize
        elif infoClass == FileBothDirectoryInformation:
            body = (header + times + sizes + struct.pack('<II', len(nameW), 0) +
                    struct.pack('<B', 0) +  # ShortNameLength (Reserved(1) MUST NOT be added)
                    b'\x00' * 24 + nameW)  # ShortName
        elif infoClass == FileNamesInformation:
            body = header + struct.pack('<I', len(nameW)) + nameW
        else:
            log.debug("RDPDR drive: unhandled directory FsInformationClass %d" % infoClass)
            raise _IrpError(STATUS_NOT_SUPPORTED)
        return struct.pack('<I', len(body)) + body
