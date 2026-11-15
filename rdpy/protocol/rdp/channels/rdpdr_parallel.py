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
RDPDR parallel port redirection (MS-RDPESP Serial and Parallel Port Virtual
Channel Extension, parallel part).
A local parallel port device (/dev/lp0, /dev/usb/lp0, /dev/parport0 on
Linux, LPT1 on Windows), or any file or FIFO, is shown to the server as LPTn:
what Windows applications write to the port is written to the device.
Ported from FreeRDP channels/parallel/client/parallel_main.c.
Protocol reference: [MS-RDPESP], [MS-RDPEFS]
"""
from __future__ import absolute_import, division, print_function
from errno import EACCES, EAGAIN, EINTR, EISDIR, ENXIO, EPERM, EROFS, EWOULDBLOCK
from select import select
from struct import pack
import os
from .rdpdr import RdpdrDevice, IrpWorker, sanitizeDosName, RDPDR_DTYP_PARALLEL, IRP_MJ_CREATE, IRP_MJ_CLOSE, \
    IRP_MJ_READ, IRP_MJ_WRITE, IRP_MJ_DEVICE_CONTROL, STATUS_SUCCESS, STATUS_UNSUCCESSFUL, STATUS_NOT_SUPPORTED, \
    STATUS_INVALID_PARAMETER, STATUS_ACCESS_DENIED, STATUS_INVALID_HANDLE, STATUS_CANCELLED
from rdpy.core import log

STATUS_SHARING_VIOLATION = 0xC0000043
MAX_READ = 1024 * 1024
_O_BINARY = getattr(os, 'O_BINARY', 0)
_O_NONBLOCK = getattr(os, 'O_NONBLOCK', 0)


class ParallelPortDevice(RdpdrDevice):
    """
    @summary: A redirected parallel port (FreeRDP PARALLEL_DEVICE)
    """

    def __init__(self, name, path):
        """
        @param name: {str} port name on the server: LPT1, LPT2...
        @param path: {str} local device or file
        """
        name = sanitizeDosName(name, 'LPT1')  # "LPT1:" -> "LPT1", at most 7 characters
        RdpdrDevice.__init__(self, RDPDR_DTYP_PARALLEL, name,
                             bytes(bytearray(c if c < 0x80 else 0x5F for c in
                                             bytearray(name.encode('ascii', 'replace')))) + b'\x00')
        self.path = path
        self._fd = None
        self._fileId = 0
        self._worker = IrpWorker(u'rdpdr-parallel-%s' % name, self._processIrp, self._close)

    # -------------------------------------------------------------------
    # RdpdrDevice interface
    # -------------------------------------------------------------------

    def irpRequest(self, irp):
        self._worker.submit(irp)

    def init(self):
        self._worker.reset()

    def free(self):
        self._worker.stop()

    # -------------------------------------------------------------------
    # IRPs (FreeRDP parallel_process_irp), on the port thread
    # -------------------------------------------------------------------

    def _close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            log.info(u'parallel: %s (%s) closed' % (self.name, self.path))
        self._fd = None
        self._fileId = 0

    def _processIrp(self, irp):
        irp.ioStatus = STATUS_SUCCESS
        major = irp.majorFunction
        try:
            if major == IRP_MJ_CREATE:
                self._irpCreate(irp)
            elif major == IRP_MJ_CLOSE:
                irp.output = bytearray(5)  # Padding
                if self._fd is None:
                    irp.ioStatus = STATUS_INVALID_HANDLE
                self._close()
            elif major == IRP_MJ_READ:
                self._irpRead(irp)
            elif major == IRP_MJ_WRITE:
                self._irpWrite(irp)
            elif major == IRP_MJ_DEVICE_CONTROL:
                irp.output = bytearray(pack('<I', 0))  # OutputBufferLength
            else:
                irp.ioStatus = STATUS_NOT_SUPPORTED
        except ValueError as e:
            log.warning(u'parallel: malformed IRP 0x%x: %s' % (major, e))
            irp.output = bytearray(4 if major in (IRP_MJ_READ, IRP_MJ_DEVICE_CONTROL) else 5)
            irp.ioStatus = STATUS_INVALID_PARAMETER
        if irp.isCancelled():
            irp.discard()
            return
        irp.complete()

    def _irpCreate(self, irp):
        s = irp.input
        s.skip(28)  # DesiredAccess, AllocationSize, FileAttributes, SharedAccess, CreateDisposition, CreateOptions
        s.skip(s.u32())  # Path
        fileId = 0
        error = ''
        if self._fd is not None:
            irp.ioStatus = STATUS_SHARING_VIOLATION
        else:
            fd = None
            for flags in (os.O_RDWR, os.O_WRONLY | getattr(os, 'O_APPEND', 0)):
                try:
                    fd = os.open(self.path, flags | _O_BINARY | _O_NONBLOCK)
                    break
                except OSError as e:
                    error = e
                    if e.errno not in (EACCES, EPERM, EROFS, EISDIR, ENXIO):
                        break
            if fd is None:
                log.error(u'parallel: cannot open %s for %s: %s' % (self.path, self.name, error))
                irp.ioStatus = STATUS_ACCESS_DENIED
            else:
                self._fd = fd
                fileId = self._fileId = self.devman.nextId() if self.devman is not None else 1
                log.info(u'parallel: %s opened (%s)' % (self.name, self.path))
        irp.output = bytearray(pack('<IB', fileId, 0))  # FileId, Information

    def _irpRead(self, irp):
        s = irp.input
        length = s.u32()
        s.u64()  # Offset, ignored
        data = b''
        if self._fd is None:
            irp.ioStatus = STATUS_INVALID_HANDLE
        else:
            try:
                if os.name == 'posix':
                    r, _, _ = select([self._fd], [], [], 0)
                    if r:
                        data = os.read(self._fd, min(length, MAX_READ))
                # the port is not readable without blocking elsewhere: nothing read
            except OSError as e:
                if e.errno not in (EAGAIN, EWOULDBLOCK, EINTR):
                    log.warning(u'parallel: %s read failed: %s' % (self.name, e))
                    irp.ioStatus = STATUS_UNSUCCESSFUL
        irp.output = bytearray(pack('<I', len(data)) + data)

    def _irpWrite(self, irp):
        s = irp.input
        length = s.u32()
        s.u64()  # Offset, ignored
        s.skip(20)  # Padding
        data = memoryview(s.read(length))
        written = 0
        if self._fd is None:
            irp.ioStatus = STATUS_INVALID_HANDLE
        else:
            try:
                while written < length:
                    if irp.isCancelled():
                        irp.ioStatus = STATUS_CANCELLED
                        break
                    try:
                        n = os.write(self._fd, data[written:])
                    except OSError as e:
                        if e.errno not in (EAGAIN, EWOULDBLOCK, EINTR):
                            raise
                        n = 0
                    written += n
                    if n == 0 and os.name == 'posix':
                        select.select([], [self._fd], [], 0.2)  # printer busy / out of paper
            except OSError as e:
                log.warning(u'parallel: %s write failed: %s' % (self.name, e))
                irp.ioStatus = STATUS_UNSUCCESSFUL
                written = 0  # FreeRDP reports 0 bytes on a failed write
        irp.output = bytearray(pack('<IB', written, 0))  # Length, Padding


def parseParallelSpec(spec):
    """
    @summary: Parse a parallel port description (FreeRDP /parallel:<name>,<path>):
        "LPT1=/dev/usb/lp0" or "LPT1,/dev/lp0"
    @return: {tuple} (name, path)
    @raise ValueError: invalid description
    """
    spec = (spec or '').strip()
    sep = '=' if '=' in spec else ','
    name, _, path = spec.partition(sep)
    name, path = name.strip(), path.strip()
    if not name or not path:
        raise ValueError(u'parallel port: expected NAME=PATH, got \'%s\'' % spec)
    return name, path
