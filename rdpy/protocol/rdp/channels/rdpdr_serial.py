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
RDPDR serial port redirection (MS-RDPESP Serial and Parallel Port Virtual
Channel Extension, serial part).
A local serial port (a /dev/tty* or COMx device, or any pyserial URL such as
socket://host:port or rfc2217://host:port) is shown to the server as COMn.
Windows applications open it and send the IOCTL_SERIAL_* requests of the
Windows serial driver (Serial.sys), which are emulated here on top of pyserial:
line settings, flow control, modem lines, Windows read / write timeouts
(COMMTIMEOUTS), wait-on-event masks (WaitCommEvent), purge, break, XON / XOFF,
status and statistics.
Ported from FreeRDP channels/serial/client/serial_main.c and
winpr/libwinpr/comm (comm_io.c, comm_ioctl.c, comm_serial_sys.c). As in
FreeRDP, blocking requests (read, write, wait on mask) are served on their own
threads, so a pending read never blocks the configuration requests.
Requires pyserial (pip install "rdpymanyqt[serial]").
Protocol reference: [MS-RDPESP], [MS-RDPEFS]
"""
from __future__ import absolute_import, division, print_function
from errno import EACCES, EAGAIN, EBUSY, EINTR, EINVAL, ENODEV, ENOENT, ENOSYS, ENOTTY, ENXIO, EPERM, EWOULDBLOCK
from os import name as os_name, read as os_read, write as os_write
from struct import pack, unpack_from
from threading import RLock, Thread
from select import select
from time import sleep
from .rdpdr import RdpdrDevice, IrpWorker, sanitizeDosName, RDPDR_DTYP_SERIAL, IRP_MJ_CREATE, IRP_MJ_CLOSE, \
    IRP_MJ_READ, IRP_MJ_WRITE, IRP_MJ_DEVICE_CONTROL, STATUS_SUCCESS, STATUS_UNSUCCESSFUL, STATUS_NOT_SUPPORTED, \
    STATUS_INVALID_PARAMETER, STATUS_ACCESS_DENIED, STATUS_NO_SUCH_DEVICE, STATUS_INVALID_HANDLE, STATUS_TIMEOUT, \
    STATUS_NOT_IMPLEMENTED, STATUS_BUFFER_TOO_SMALL, STATUS_CANCELLED, STATUS_IO_DEVICE_ERROR
from rdpy.core.compat import monotonic
from rdpy.core import log

STATUS_SHARING_VIOLATION = 0xC0000043
# IOCTL_SERIAL_* (ntddser.h)
IOCTL_SERIAL_SET_BAUD_RATE = 0x001B0004
IOCTL_SERIAL_SET_QUEUE_SIZE = 0x001B0008
IOCTL_SERIAL_SET_LINE_CONTROL = 0x001B000C
IOCTL_SERIAL_SET_BREAK_ON = 0x001B0010
IOCTL_SERIAL_SET_BREAK_OFF = 0x001B0014
IOCTL_SERIAL_IMMEDIATE_CHAR = 0x001B0018
IOCTL_SERIAL_SET_TIMEOUTS = 0x001B001C
IOCTL_SERIAL_GET_TIMEOUTS = 0x001B0020
IOCTL_SERIAL_SET_DTR = 0x001B0024
IOCTL_SERIAL_CLR_DTR = 0x001B0028
IOCTL_SERIAL_RESET_DEVICE = 0x001B002C
IOCTL_SERIAL_SET_RTS = 0x001B0030
IOCTL_SERIAL_CLR_RTS = 0x001B0034
IOCTL_SERIAL_SET_XOFF = 0x001B0038
IOCTL_SERIAL_SET_XON = 0x001B003C
IOCTL_SERIAL_GET_WAIT_MASK = 0x001B0040
IOCTL_SERIAL_SET_WAIT_MASK = 0x001B0044
IOCTL_SERIAL_WAIT_ON_MASK = 0x001B0048
IOCTL_SERIAL_PURGE = 0x001B004C
IOCTL_SERIAL_GET_BAUD_RATE = 0x001B0050
IOCTL_SERIAL_GET_LINE_CONTROL = 0x001B0054
IOCTL_SERIAL_GET_CHARS = 0x001B0058
IOCTL_SERIAL_SET_CHARS = 0x001B005C
IOCTL_SERIAL_GET_HANDFLOW = 0x001B0060
IOCTL_SERIAL_SET_HANDFLOW = 0x001B0064
IOCTL_SERIAL_GET_MODEMSTATUS = 0x001B0068
IOCTL_SERIAL_GET_COMMSTATUS = 0x001B006C
IOCTL_SERIAL_XOFF_COUNTER = 0x001B0070
IOCTL_SERIAL_GET_PROPERTIES = 0x001B0074
IOCTL_SERIAL_GET_DTRRTS = 0x001B0078
IOCTL_SERIAL_LSRMST_INSERT = 0x001B007C
IOCTL_SERIAL_CONFIG_SIZE = 0x001B0080
IOCTL_SERIAL_GET_STATS = 0x001B008C
IOCTL_SERIAL_CLEAR_STATS = 0x001B0090
IOCTL_SERIAL_GET_MODEM_CONTROL = 0x001B0094
IOCTL_SERIAL_SET_MODEM_CONTROL = 0x001B0098
IOCTL_SERIAL_SET_FIFO_CONTROL = 0x001B009C
IOCTL_USBPRINT_GET_1284_ID = 0x00220034
IOCTL_NAMES = dict((v, k) for k, v in list(globals().items()) if k.startswith('IOCTL_'))
MAXULONG = 0xFFFFFFFF
# SERIAL_EV_* (wait mask)
SERIAL_EV_RXCHAR = 0x0001
SERIAL_EV_RXFLAG = 0x0002
SERIAL_EV_TXEMPTY = 0x0004
SERIAL_EV_CTS = 0x0008
SERIAL_EV_DSR = 0x0010
SERIAL_EV_RLSD = 0x0020
SERIAL_EV_BREAK = 0x0040
SERIAL_EV_ERR = 0x0080
SERIAL_EV_RING = 0x0100
SERIAL_EV_PERR = 0x0200
SERIAL_EV_RX80FULL = 0x0400
SERIAL_EV_EVENT1 = 0x0800
SERIAL_EV_EVENT2 = 0x1000
SERIAL_EV_ALL = 0x1FFF

# SERIAL_HANDFLOW.ControlHandShake
SERIAL_DTR_MASK = 0x03
SERIAL_DTR_CONTROL = 0x01
SERIAL_DTR_HANDSHAKE = 0x02
SERIAL_CTS_HANDSHAKE = 0x08
SERIAL_DSR_HANDSHAKE = 0x10
SERIAL_DCD_HANDSHAKE = 0x20
SERIAL_DSR_SENSITIVITY = 0x40
SERIAL_ERROR_ABORT = 0x80000000
SERIAL_CONTROL_INVALID = 0x7FFFFF84
# SERIAL_HANDFLOW.FlowReplace
SERIAL_AUTO_TRANSMIT = 0x01
SERIAL_AUTO_RECEIVE = 0x02
SERIAL_ERROR_CHAR = 0x04
SERIAL_NULL_STRIPPING = 0x08
SERIAL_BREAK_CHAR = 0x10
SERIAL_RTS_MASK = 0xC0
SERIAL_RTS_CONTROL = 0x40
SERIAL_RTS_HANDSHAKE = 0x80
SERIAL_TRANSMIT_TOGGLE = 0xC0
SERIAL_XOFF_CONTINUE = 0x80000000
SERIAL_FLOW_INVALID = 0x7FFFFF20
# modem status register
SERIAL_MSR_DCTS = 0x01
SERIAL_MSR_DDSR = 0x02
SERIAL_MSR_TERI = 0x04
SERIAL_MSR_DDCD = 0x08
SERIAL_MSR_CTS = 0x10
SERIAL_MSR_DSR = 0x20
SERIAL_MSR_RI = 0x40
SERIAL_MSR_DCD = 0x80
# modem control register
SERIAL_MCR_DTR = 0x01
SERIAL_MCR_RTS = 0x02
SERIAL_MCR_OUT2 = 0x08
# IOCTL_SERIAL_GET_DTRRTS
SERIAL_DTR_STATE = 0x01
SERIAL_RTS_STATE = 0x02
# SERIAL_STATUS.HoldReasons
SERIAL_TX_WAITING_FOR_CTS = 0x01
SERIAL_TX_WAITING_FOR_DSR = 0x02
SERIAL_TX_WAITING_FOR_DCD = 0x04
# IOCTL_SERIAL_PURGE
SERIAL_PURGE_TXABORT = 0x01
SERIAL_PURGE_RXABORT = 0x02
SERIAL_PURGE_TXCLEAR = 0x04
SERIAL_PURGE_RXCLEAR = 0x08
# SERIAL_LINE_CONTROL
STOP_BIT_1, STOP_BITS_1_5, STOP_BITS_2 = 0, 1, 2
NO_PARITY, ODD_PARITY, EVEN_PARITY, MARK_PARITY, SPACE_PARITY = 0, 1, 2, 3, 4
# COMMPROP (GetCommProperties)
SP_SERIALCOMM = 0x00000001
PST_RS232 = 0x00000001
BAUD_ALL = 0x0007FFFF | 0x10000000  # every BAUD_* bit and BAUD_USER
BAUD_USER = 0x10000000
PCF_ALL = 0x0001 | 0x0002 | 0x0004 | 0x0008 | 0x0010 | 0x0020 | 0x0040 | 0x0080 | 0x0100
SP_ALL = 0x0001 | 0x0002 | 0x0004 | 0x0008 | 0x0010 | 0x0020 | 0x0040
DATABITS_5_TO_8 = 0x0001 | 0x0002 | 0x0004 | 0x0008
STOPBITS_PARITY_ALL = 0x0001 | 0x0002 | 0x0004 | 0x0100 | 0x0200 | 0x0400 | 0x0800 | 0x1000
DEFAULT_QUEUE_SIZE = 4096

# server serial drivers (FreeRDP /serial:<name>,<path>[,<driver>[,permissive]])
SERIAL_DRIVERS = ('Serial', 'SerCx', 'SerCx2')

# largest read answered at once (a smaller read is valid serial semantics)
MAX_READ = 1024 * 1024
# how often a pending request checks for cancellation / line changes
_SLICE = 0.05


class IoctlError(Exception):
    """@summary: an IOCTL failed with this NTSTATUS"""

    def __init__(self, status, message=''):
        Exception.__init__(self, message or (u'status 0x%08x' % status))
        self.status = status


def _pyserialOpen(path):
    """
    @summary: Open a serial port with pyserial (device path or pyserial URL)
    @raise OSError / serial.SerialException
    """
    try:
        from serial import serial_for_url
    except:
        raise OSError(ENOSYS, u'pyserial is not installed (pip install "rdpymanyqt[serial]")')
    # reads never block (timeout 0): PortIO waits with select() or polls; writes on a
    # file descriptor bypass pyserial, others (Windows, URLs) block on the writer thread
    kwargs = dict(baudrate=9600, timeout=0, write_timeout=None, do_not_open=True)
    if os_name == 'posix' and '://' not in path:
        kwargs['exclusive'] = True  # like Windows: one owner at a time
    port = serial_for_url(path, **kwargs)
    port.open()
    return port


class PortIO(object):
    """
    @summary: Non blocking I/O on a pyserial port: select() on POSIX devices,
    polling elsewhere (Windows ports, pyserial URLs)
    """

    def __init__(self, port):
        self.port = port
        fd = getattr(port, 'fd', None)
        self.fd = fd if (os_name == 'posix' and isinstance(fd, int) and fd >= 0) else None

    def waitReadable(self, timeout):
        if self.fd is not None:
            try:
                r, _, _ = select([self.fd], [], [], max(0.0, timeout))
                return bool(r)
            except (OSError, ValueError):
                return False
        end = monotonic() + max(0.0, timeout)
        while True:
            if self.inWaiting() > 0:
                return True
            left = end - monotonic()
            if left <= 0:
                return False
            sleep(min(left, 0.01))

    def waitWritable(self, timeout):
        if self.fd is not None:
            try:
                _, w, _ = select([], [self.fd], [], max(0.0, timeout))
                return bool(w)
            except (OSError, ValueError):
                return False
        sleep(min(max(0.0, timeout), 0.01))
        return True

    def read(self, size):
        """@return: {bytes} what is available now, up to size bytes"""
        if self.fd is not None:
            try:
                return os_read(self.fd, size)
            except OSError as e:
                if e.errno in (EAGAIN, EWOULDBLOCK, EINTR):
                    return b''
                raise
        return bytes(self.port.read(size))

    def write(self, data):
        """@return: {int} bytes accepted now"""
        if self.fd is not None:
            try:
                return os_write(self.fd, data)
            except OSError as e:
                if e.errno in (EAGAIN, EWOULDBLOCK, EINTR):
                    return 0
                raise
        return self.port.write(data) or 0

    def inWaiting(self):
        try:
            return int(self.port.in_waiting)
        except Exception:
            return 0

    def outWaiting(self):
        try:
            return int(getattr(self.port, 'out_waiting', 0) or 0)
        except Exception:
            return 0

    def lines(self):
        """@return: {int} SERIAL_MSR_CTS | DSR | RI | DCD of the input lines"""
        value = 0
        for attr, bit in (('cts', SERIAL_MSR_CTS), ('dsr', SERIAL_MSR_DSR), ('ri', SERIAL_MSR_RI),
                          ('cd', SERIAL_MSR_DCD)):
            try:
                if getattr(self.port, attr):
                    value |= bit
            except Exception:  # ptys and some URLs have no modem lines
                pass
        return value


class _Pending(object):
    """@summary: the IRP of a blocking request and how to cancel it"""

    def __init__(self, irp):
        self.irp = irp
        self.cancelStatus = None  # set to STATUS_CANCELLED / STATUS_SUCCESS to stop it

    def cancel(self, status=STATUS_CANCELLED):
        if self.cancelStatus is None:
            self.cancelStatus = status


class SerialPortDevice(RdpdrDevice):
    """
    @summary: A redirected serial port (FreeRDP SERIAL_DEVICE + winpr Serial.sys emulation)
    """

    def __init__(self, name, path, driver=None, permissive=False, opener=None):
        """
        @param name: {str} port name on the server: COM1, COM2...
        @param path: {str} local port: /dev/ttyS0, /dev/ttyUSB0, COM3,
                     socket://host:port, rfc2217://host:port, loop://...
        @param driver: {str|None} serial driver the server uses: Serial (default), SerCx, SerCx2
        @param permissive: {bool} report every failed IOCTL as a success (FreeRDP permissive)
        @param opener: {callable(path)|None} returns an open pyserial-like port (tests)
        """
        name = sanitizeDosName(name, 'COM1')  # "COM1:" -> "COM1", at most 7 characters
        RdpdrDevice.__init__(self, RDPDR_DTYP_SERIAL, name,
                             bytes(bytearray(c if c < 0x80 else 0x5F for c in
                                             bytearray(name.encode('ascii', 'replace')))) + b'\x00')
        if driver and driver not in SERIAL_DRIVERS:
            matches = [d for d in SERIAL_DRIVERS if d.lower() == driver.lower()]
            if not matches:
                raise ValueError(u'serial: unknown driver \'%s\' (valid: %s)' % (driver, u', '.join(SERIAL_DRIVERS)))
            driver = matches[0]
        self.path = path
        self.driver = driver or 'Serial'
        self.permissive = bool(permissive)
        self._opener = opener or _pyserialOpen
        self._lock = RLock()
        self._port = None
        self._io = None
        self._fileId = 0
        self._resetState()
        # one lane per kind of request: configuration, reads, writes
        self._control = IrpWorker(u'rdpdr-serial-%s' % name, self._processControl, self._closePort)
        self._reader = IrpWorker(u'rdpdr-serial-%s-rx' % name, self._processRead, maxIrps=64)
        self._writer = IrpWorker(u'rdpdr-serial-%s-tx' % name, self._processWrite, maxIrps=64)
        self._waitThread = None

    def _resetState(self):
        # Serial.sys state returned by the GET requests
        self._baud = 9600
        self._lineControl = (STOP_BIT_1, NO_PARITY, 8)
        self._chars = (0, 0, 0, 0, 0x11, 0x13)  # Eof, Error, Break, Event, Xon, Xoff
        self._handflow = (SERIAL_DTR_CONTROL, SERIAL_RTS_CONTROL, DEFAULT_QUEUE_SIZE // 2,
                          DEFAULT_QUEUE_SIZE // 4)
        self._timeouts = (0, 0, 0, 0, 0)
        self._queueSize = (DEFAULT_QUEUE_SIZE, DEFAULT_QUEUE_SIZE)
        self._waitMask = 0
        self._history = 0
        self._rxSeen = 0
        self._txPending = False
        self._lastLines = 0
        self._msrLines = 0
        self._stats = [0, 0, 0, 0, 0, 0]  # received, transmitted, frame, overrun, buffer overrun, parity
        self._dtr = True
        self._rts = True
        self._breakOn = False
        self._readers = set()
        self._writers = set()
        self._wait = None

    # -------------------------------------------------------------------
    # RdpdrDevice interface
    # -------------------------------------------------------------------

    def irpRequest(self, irp):
        # reads and writes have their own lane, so a pending read never delays the
        # configuration requests (FreeRDP: one thread per IRP); WAIT_ON_MASK goes through
        # the control lane, after the SET_WAIT_MASK sent before it, and then waits on its
        # own thread
        major = irp.majorFunction
        if major == IRP_MJ_READ:
            self._reader.submit(irp)
        elif major == IRP_MJ_WRITE:
            self._writer.submit(irp)
        else:
            self._control.submit(irp)

    def init(self):
        # the server forgot its handles: close the port (completions are dropped by the layer)
        self._cancelAll(STATUS_CANCELLED)
        self._control.reset()

    def free(self):
        self._cancelAll(STATUS_CANCELLED)
        self._reader.stop()
        self._writer.stop()
        self._control.stop()

    # -------------------------------------------------------------------
    # helpers
    # -------------------------------------------------------------------

    def _cancelAll(self, status):
        with self._lock:
            pending = list(self._readers) + list(self._writers)
            if self._wait is not None:
                pending.append(self._wait)
        for p in pending:
            p.cancel(status)

    def _closePort(self):
        """@summary: close the port (control lane: channel reset or device freed)"""
        self._cancelAll(STATUS_CANCELLED)
        self._drain()
        with self._lock:
            port, self._port, self._io = self._port, None, None
            self._fileId = 0
        if port is not None:
            try:
                port.close()
            except Exception as e:
                log.debug(u'serial: close %s: %s' % (self.path, e))
            log.info(u'serial: %s (%s) closed' % (self.name, self.path))

    def _drain(self, timeout=2.0):
        """@summary: wait for the blocking requests to notice their cancellation"""
        end = monotonic() + timeout
        while monotonic() < end:
            with self._lock:
                if not self._readers and not self._writers and self._wait is None:
                    return
            sleep(0.01)
        log.warning(u'serial: %s: pending requests did not stop' % self.name)

    def _onBytesRead(self, count):
        with self._lock:
            self._stats[0] = (self._stats[0] + count) & 0xFFFFFFFF
            if count > self._rxSeen:
                self._history |= (SERIAL_EV_RXCHAR | SERIAL_EV_RXFLAG) & self._waitMask
            self._rxSeen = max(0, self._rxSeen - count)

    def _onBytesWritten(self, count):
        with self._lock:
            self._stats[1] = (self._stats[1] + count) & 0xFFFFFFFF
            if count:
                self._txPending = True

    def _pollEvents(self):
        """@summary: latch the events that happened since the last poll into the history"""
        io = self._io
        if io is None:
            return 0
        lines = io.lines()
        outQueue = io.outWaiting()
        with self._lock:
            # read under the lock: a read in progress updates _rxSeen atomically
            inQueue = io.inWaiting()
            events = 0
            if inQueue > self._rxSeen:
                events |= SERIAL_EV_RXCHAR | SERIAL_EV_RXFLAG
                if inQueue > 0.8 * self._queueSize[0] >= self._rxSeen:
                    events |= SERIAL_EV_RX80FULL
            self._rxSeen = inQueue
            changed = lines ^ self._lastLines
            if changed & SERIAL_MSR_CTS:
                events |= SERIAL_EV_CTS
            if changed & SERIAL_MSR_DSR:
                events |= SERIAL_EV_DSR
            if changed & SERIAL_MSR_DCD:
                events |= SERIAL_EV_RLSD
            if changed & SERIAL_MSR_RI:
                events |= SERIAL_EV_RING
            self._lastLines = lines
            if self._txPending and outQueue == 0:
                self._txPending = False
                events |= SERIAL_EV_TXEMPTY
            self._history |= events & self._waitMask
            return self._history

    # -------------------------------------------------------------------
    # control lane: create, close, IOCTLs
    # -------------------------------------------------------------------

    def _processControl(self, irp):
        irp.ioStatus = STATUS_SUCCESS
        major = irp.majorFunction
        try:
            if major == IRP_MJ_CREATE:
                self._irpCreate(irp)
            elif major == IRP_MJ_CLOSE:
                self._irpClose(irp)
            elif major == IRP_MJ_DEVICE_CONTROL:
                if self._irpDeviceControl(irp):
                    return  # WAIT_ON_MASK pending, completed by its thread
            else:
                irp.ioStatus = STATUS_NOT_SUPPORTED
        except ValueError as e:
            log.warning(u'serial: malformed IRP 0x%x: %s' % (major, e))
            irp.output = bytearray(5 if major in (IRP_MJ_CREATE, IRP_MJ_CLOSE) else 4)
            irp.ioStatus = STATUS_INVALID_PARAMETER
        irp.complete()

    def _irpCreate(self, irp):
        s = irp.input
        s.u32()  # DesiredAccess
        s.u64()  # AllocationSize
        s.u32()  # FileAttributes
        s.u32()  # SharedAccess
        s.u32()  # CreateDisposition
        s.u32()  # CreateOptions
        s.skip(s.u32())  # Path, empty for a port (MS-RDPESP 2.2.2.2)
        fileId = 0
        if self._port is not None:
            log.warning(u'serial: %s is already open, create refused' % self.name)
            irp.ioStatus = STATUS_SHARING_VIOLATION
        else:
            try:
                port = self._opener(self.path)
            except Exception as e:
                code = getattr(e, 'errno', None)
                irp.ioStatus = (STATUS_NO_SUCH_DEVICE if code in (ENOENT, ENODEV, ENXIO)
                                else STATUS_ACCESS_DENIED if code in (EACCES, EBUSY, EPERM)
                                or 'lock' in str(e).lower() or 'busy' in str(e).lower()
                                else STATUS_UNSUCCESSFUL)
                log.error(u'serial: cannot open %s for %s: %s' % (self.path, self.name, e))
            else:
                with self._lock:
                    self._resetState()
                    self._port = port
                    self._io = PortIO(port)
                    fileId = self._fileId = self.devman.nextId() if self.devman is not None else 1
                    try:
                        self._baud = int(port.baudrate)
                    except (TypeError, ValueError):
                        pass
                self._lastLines = self._io.lines()
                self._msrLines = self._lastLines
                log.info(u'serial: %s opened (%s, driver %s)' % (self.name, self.path, self.driver))
        irp.output = bytearray(pack('<IB', fileId, 0))  # FileId, Information

    def _irpClose(self, irp):
        irp.input.skip(min(32, irp.input.remaining()))  # Padding
        irp.output = bytearray(5)
        if self._port is None:
            irp.ioStatus = STATUS_INVALID_HANDLE
            return
        self._closePort()

    def _irpDeviceControl(self, irp):
        s = irp.input
        outputLength = s.u32()
        inputLength = s.u32()
        code = s.u32()
        s.skip(20)  # Padding
        data = s.read(inputLength)
        if code == IOCTL_SERIAL_WAIT_ON_MASK:
            return self._startWait(irp, outputLength)
        status, out = STATUS_SUCCESS, b''
        try:
            if self._port is None:
                raise IoctlError(STATUS_INVALID_HANDLE, u'port not open')
            out = self._ioctl(code, data, outputLength) or b''
            if len(out) > outputLength:
                raise IoctlError(STATUS_BUFFER_TOO_SMALL)
        except IoctlError as e:
            status, out = e.status, b''
            log.debug(u'serial: %s %s failed: %s' % (self.name, IOCTL_NAMES.get(code, hex(code)), e))
        except Exception as e:  # pyserial / OS error
            status, out = STATUS_IO_DEVICE_ERROR, b''
            log.warning(u'serial: %s %s failed: %s' % (self.name, IOCTL_NAMES.get(code, hex(code)), e))
        if status != STATUS_SUCCESS and self.permissive:
            log.debug(u'serial: [permissive] %s failed with 0x%08x, ignored' %
                      (IOCTL_NAMES.get(code, hex(code)), status))
            status = STATUS_SUCCESS
        irp.ioStatus = status
        # OutputBufferLength + OutputBuffer, without the padding byte (FreeRDP:
        # Windows 2008 R2 gets lost with it)
        irp.output = bytearray(pack('<I', len(out)) + out)
        return False

    # -------------------------------------------------------------------
    # IOCTLs (winpr comm_serial_sys.c, Serial.sys behaviour)
    # -------------------------------------------------------------------

    @staticmethod
    def _need(data, size):
        if len(data) < size:
            raise IoctlError(STATUS_BUFFER_TOO_SMALL, u'input buffer too small')

    @staticmethod
    def _room(outputLength, size):
        if outputLength < size:
            raise IoctlError(STATUS_BUFFER_TOO_SMALL, u'output buffer too small')

    def _ioctl(self, code, data, outputLength):
        port = self._port
        if code == IOCTL_SERIAL_SET_BAUD_RATE:
            self._need(data, 4)
            baud = unpack_from('<I', data)[0]
            if baud == 0:
                raise IoctlError(STATUS_INVALID_PARAMETER, u'baud rate 0')
            try:
                port.baudrate = baud
            except Exception as e:  # ValueError, SerialException, termios.error: the port refuses it
                raise IoctlError(STATUS_INVALID_PARAMETER, str(e))
            self._baud = baud
        elif code == IOCTL_SERIAL_GET_BAUD_RATE:
            self._room(outputLength, 4)
            return pack('<I', self._baud)
        elif code == IOCTL_SERIAL_SET_LINE_CONTROL:
            self._need(data, 3)
            stopBits, parity, wordLength = unpack_from('<BBB', data)
            self._setLineControl(stopBits, parity, wordLength)
        elif code == IOCTL_SERIAL_GET_LINE_CONTROL:
            self._room(outputLength, 3)
            return pack('<BBB', *self._lineControl)
        elif code == IOCTL_SERIAL_SET_CHARS:
            self._need(data, 6)
            chars = unpack_from('<6B', data)
            if chars[4] == chars[5] and self._handflow[1] & (SERIAL_AUTO_TRANSMIT | SERIAL_AUTO_RECEIVE):
                raise IoctlError(STATUS_INVALID_PARAMETER, u'XonChar == XoffChar')
            self._chars = chars
        elif code == IOCTL_SERIAL_GET_CHARS:
            self._room(outputLength, 6)
            return pack('<6B', *self._chars)
        elif code == IOCTL_SERIAL_SET_HANDFLOW:
            self._need(data, 16)
            self._setHandflow(*unpack_from('<IIii', data))
        elif code == IOCTL_SERIAL_GET_HANDFLOW:
            self._room(outputLength, 16)
            return pack('<IIii', *self._handflow)
        elif code == IOCTL_SERIAL_SET_TIMEOUTS:
            self._need(data, 20)
            timeouts = unpack_from('<5I', data)
            if timeouts[0] == MAXULONG and timeouts[2] == MAXULONG:
                raise IoctlError(STATUS_INVALID_PARAMETER,
                                 u'ReadIntervalTimeout and ReadTotalTimeoutConstant both MAXULONG')
            self._timeouts = timeouts
        elif code == IOCTL_SERIAL_GET_TIMEOUTS:
            self._room(outputLength, 20)
            return pack('<5I', *self._timeouts)
        elif code in (IOCTL_SERIAL_SET_DTR, IOCTL_SERIAL_CLR_DTR):
            if (self._handflow[0] & SERIAL_DTR_MASK) == SERIAL_DTR_HANDSHAKE:
                raise IoctlError(STATUS_INVALID_PARAMETER, u'DTR handshake enabled')
            self._dtr = code == IOCTL_SERIAL_SET_DTR
            self._setLine('dtr', self._dtr)
        elif code in (IOCTL_SERIAL_SET_RTS, IOCTL_SERIAL_CLR_RTS):
            if (self._handflow[1] & SERIAL_RTS_MASK) in (SERIAL_RTS_HANDSHAKE, SERIAL_TRANSMIT_TOGGLE):
                raise IoctlError(STATUS_INVALID_PARAMETER, u'RTS handshake enabled')
            self._rts = code == IOCTL_SERIAL_SET_RTS
            self._setLine('rts', self._rts)
        elif code == IOCTL_SERIAL_GET_DTRRTS:
            self._room(outputLength, 4)
            return pack('<I', (SERIAL_DTR_STATE if self._dtr else 0) | (SERIAL_RTS_STATE if self._rts else 0))
        elif code == IOCTL_SERIAL_GET_MODEMSTATUS:
            self._room(outputLength, 4)
            lines = self._io.lines()
            changed = lines ^ self._msrLines
            self._msrLines = lines
            msr = lines
            if changed & SERIAL_MSR_CTS:
                msr |= SERIAL_MSR_DCTS
            if changed & SERIAL_MSR_DSR:
                msr |= SERIAL_MSR_DDSR
            if changed & SERIAL_MSR_DCD:
                msr |= SERIAL_MSR_DDCD
            if changed & SERIAL_MSR_RI and not lines & SERIAL_MSR_RI:
                msr |= SERIAL_MSR_TERI
            return pack('<I', msr)
        elif code == IOCTL_SERIAL_GET_MODEM_CONTROL:
            self._room(outputLength, 4)
            return pack('<I', (SERIAL_MCR_DTR if self._dtr else 0) | (SERIAL_MCR_RTS if self._rts else 0) |
                               SERIAL_MCR_OUT2)
        elif code == IOCTL_SERIAL_SET_MODEM_CONTROL:
            self._need(data, 4)
            mcr = unpack_from('<I', data)[0]
            self._dtr, self._rts = bool(mcr & SERIAL_MCR_DTR), bool(mcr & SERIAL_MCR_RTS)
            self._setLine('dtr', self._dtr)
            self._setLine('rts', self._rts)
        elif code == IOCTL_SERIAL_SET_WAIT_MASK:
            self._need(data, 4)
            mask = unpack_from('<I', data)[0]
            if mask & ~SERIAL_EV_ALL:
                raise IoctlError(STATUS_INVALID_PARAMETER, u'invalid wait mask 0x%x' % mask)
            with self._lock:
                wait = self._wait
            if wait is not None:
                # a pending WAIT_ON_MASK completes with an empty mask (Serial.sys)
                wait.cancel(STATUS_SUCCESS)
                self._drainWait()
            with self._lock:
                self._waitMask = mask
                self._history = 0
                self._rxSeen = self._io.inWaiting()
                self._lastLines = self._io.lines()
        elif code == IOCTL_SERIAL_GET_WAIT_MASK:
            self._room(outputLength, 4)
            return pack('<I', self._waitMask)
        elif code == IOCTL_SERIAL_SET_QUEUE_SIZE:
            self._need(data, 8)
            self._queueSize = unpack_from('<II', data)
        elif code == IOCTL_SERIAL_PURGE:
            self._need(data, 4)
            self._purge(unpack_from('<I', data)[0])
        elif code == IOCTL_SERIAL_GET_COMMSTATUS:
            self._room(outputLength, 20)
            inQueue, outQueue = self._io.inWaiting(), self._io.outWaiting()
            hold = 0
            if outQueue:
                lines = self._io.lines()
                if self._handflow[0] & SERIAL_CTS_HANDSHAKE and not lines & SERIAL_MSR_CTS:
                    hold |= SERIAL_TX_WAITING_FOR_CTS
                if self._handflow[0] & SERIAL_DSR_HANDSHAKE and not lines & SERIAL_MSR_DSR:
                    hold |= SERIAL_TX_WAITING_FOR_DSR
            self._pollEvents()
            # Errors, HoldReasons, AmountInInQueue, AmountInOutQueue, EofReceived,
            # WaitForImmediate, padding
            return pack('<IIIIBBH', 0, hold, inQueue, outQueue, 0, 0, 0)
        elif code == IOCTL_SERIAL_GET_PROPERTIES:
            self._room(outputLength, 64)
            return pack('<HHIIIIIIIIIHHIIII4x', 64, 2, SP_SERIALCOMM, 0,
                               DEFAULT_QUEUE_SIZE, DEFAULT_QUEUE_SIZE, BAUD_USER, PST_RS232, PCF_ALL,
                               SP_ALL, BAUD_ALL, DATABITS_5_TO_8, STOPBITS_PARITY_ALL,
                               self._queueSize[1], self._queueSize[0], 0, 0)
        elif code in (IOCTL_SERIAL_SET_BREAK_ON, IOCTL_SERIAL_SET_BREAK_OFF):
            self._breakOn = code == IOCTL_SERIAL_SET_BREAK_ON
            self._setLine('break_condition', self._breakOn)
        elif code in (IOCTL_SERIAL_SET_XOFF, IOCTL_SERIAL_SET_XON):
            # act as if XOFF / XON was received: suspend / resume transmission
            control = getattr(port, 'set_output_flow_control', None)
            if control is None:
                raise IoctlError(STATUS_NOT_IMPLEMENTED)
            try:
                control(code == IOCTL_SERIAL_SET_XON)
            except NotImplementedError:
                raise IoctlError(STATUS_NOT_IMPLEMENTED)
        elif code == IOCTL_SERIAL_IMMEDIATE_CHAR:
            self._need(data, 1)
            if not self._writeAll(data[:1], monotonic() + 1.0):
                raise IoctlError(STATUS_TIMEOUT)
        elif code == IOCTL_SERIAL_CONFIG_SIZE:
            self._room(outputLength, 4)
            return pack('<I', 0)
        elif code == IOCTL_SERIAL_GET_STATS:
            self._room(outputLength, 24)
            return pack('<6I', *self._stats)
        elif code == IOCTL_SERIAL_CLEAR_STATS:
            self._stats = [0, 0, 0, 0, 0, 0]
        elif code in (IOCTL_SERIAL_RESET_DEVICE, IOCTL_SERIAL_SET_FIFO_CONTROL):
            pass  # nothing to do on a host port
        else:
            # LSRMST_INSERT, XOFF_COUNTER, USBPRINT_GET_1284_ID... (FreeRDP: ERROR_CALL_NOT_IMPLEMENTED)
            raise IoctlError(STATUS_NOT_IMPLEMENTED, u'unsupported IOCTL 0x%08x' % code)
        return b''

    def _setLine(self, attr, value):
        """@summary: drive an output line (DTR, RTS, break); ports without modem
        lines (ptys, some USB adapters) ignore it like the Windows driver does"""
        try:
            setattr(self._port, attr, value)
        except EnvironmentError as e:
            if getattr(e, 'errno', None) not in (ENOTTY, EINVAL, ENOSYS):
                raise
            log.debug(u'serial: %s has no %s line: %s' % (self.path, attr, e))

    def _setLineControl(self, stopBits, parity, wordLength):
        if wordLength not in (5, 6, 7, 8) or stopBits not in (0, 1, 2) or parity > 4:
            raise IoctlError(STATUS_INVALID_PARAMETER, u'invalid line control')
        if (wordLength == 5 and stopBits == STOP_BITS_2) or (wordLength > 5 and stopBits == STOP_BITS_1_5):
            raise IoctlError(STATUS_INVALID_PARAMETER, u'invalid data bits / stop bits combination')
        settings = {
            'bytesize': wordLength,
            'parity': 'NOEMS'[parity],  # serial.PARITY_*
            'stopbits': (1, 1.5, 2)[stopBits],  # serial.STOPBITS_*
        }
        self._apply(settings)
        self._lineControl = (stopBits, parity, wordLength)

    def _apply(self, settings):
        """@summary: apply pyserial settings, restored when the port refuses one"""
        port = self._port
        previous = port.get_settings()
        try:
            port.apply_settings(settings)
        except Exception as e:  # ValueError, SerialException, termios.error: the port refuses it
            try:
                port.apply_settings(previous)
            except Exception:
                pass
            raise IoctlError(STATUS_INVALID_PARAMETER, u'%s refused by %s: %s' % (settings, self.path, e))

    def _setHandflow(self, control, replace, xonLimit, xoffLimit):
        if control & SERIAL_CONTROL_INVALID or replace & SERIAL_FLOW_INVALID:
            raise IoctlError(STATUS_INVALID_PARAMETER, u'invalid handflow bits')
        if (control & SERIAL_DTR_MASK) == SERIAL_DTR_MASK:
            raise IoctlError(STATUS_INVALID_PARAMETER, u'invalid DTR control')
        if xonLimit < 0 or xoffLimit < 0:
            raise IoctlError(STATUS_INVALID_PARAMETER, u'invalid XON / XOFF limit')
        unsupported = control & (SERIAL_DCD_HANDSHAKE | SERIAL_DSR_SENSITIVITY | SERIAL_ERROR_ABORT)
        unsupported |= replace & (SERIAL_ERROR_CHAR | SERIAL_NULL_STRIPPING | SERIAL_BREAK_CHAR |
                                  SERIAL_XOFF_CONTINUE)
        if unsupported:
            log.debug(u'serial: %s: handflow features 0x%08x not emulated' % (self.name, unsupported))
        rtsMode = replace & SERIAL_RTS_MASK
        settings = {
            'xonxoff': bool(replace & (SERIAL_AUTO_TRANSMIT | SERIAL_AUTO_RECEIVE)),
            'rtscts': bool(control & SERIAL_CTS_HANDSHAKE) or rtsMode == SERIAL_RTS_HANDSHAKE,
            'dsrdtr': bool(control & SERIAL_DSR_HANDSHAKE) or (control & SERIAL_DTR_MASK) == SERIAL_DTR_HANDSHAKE,
        }
        self._apply(settings)
        dtrMode = control & SERIAL_DTR_MASK
        if dtrMode != SERIAL_DTR_HANDSHAKE:
            self._dtr = dtrMode == SERIAL_DTR_CONTROL
            self._setLine('dtr', self._dtr)
        if rtsMode in (0, SERIAL_RTS_CONTROL):
            self._rts = rtsMode == SERIAL_RTS_CONTROL
            self._setLine('rts', self._rts)
        self._handflow = (control, replace, xonLimit, xoffLimit)

    def _purge(self, mask):
        if mask & ~(SERIAL_PURGE_TXABORT | SERIAL_PURGE_RXABORT | SERIAL_PURGE_TXCLEAR | SERIAL_PURGE_RXCLEAR):
            raise IoctlError(STATUS_INVALID_PARAMETER, u'invalid purge mask 0x%x' % mask)
        with self._lock:
            if mask & SERIAL_PURGE_TXABORT:
                for p in self._writers:
                    p.cancel(STATUS_CANCELLED)
                if self._writers and self._io.fd is None and hasattr(self._port, 'cancel_write'):
                    self._port.cancel_write()  # a blocking pyserial write (Windows)
            if mask & SERIAL_PURGE_RXABORT:
                for p in self._readers:
                    p.cancel(STATUS_CANCELLED)
        if mask & SERIAL_PURGE_TXCLEAR:
            self._port.reset_output_buffer()
            self._txPending = False
        if mask & SERIAL_PURGE_RXCLEAR:
            self._port.reset_input_buffer()
            with self._lock:
                self._rxSeen = 0

    # -------------------------------------------------------------------
    # read lane (Windows COMMTIMEOUTS semantics)
    # -------------------------------------------------------------------

    def _register(self, table, irp):
        pending = _Pending(irp)
        with self._lock:
            if self._port is None:
                return None
            table.add(pending)
        return pending

    def _processRead(self, irp):
        s = irp.input
        try:
            length = s.u32()
            s.u64()  # Offset, ignored (MS-RDPESP 3.2.5.1.4)
            s.skip(min(20, s.remaining()))
        except ValueError as e:
            log.warning(u'serial: malformed read request: %s' % e)
            irp.output, irp.ioStatus = bytearray(4), STATUS_INVALID_PARAMETER
            irp.complete()
            return
        pending = self._register(self._readers, irp)
        if pending is None:
            irp.output, irp.ioStatus = bytearray(4), STATUS_INVALID_HANDLE
            irp.complete()
            return
        try:
            data, status = self._read(pending, min(length, MAX_READ))
        except Exception as e:
            log.warning(u'serial: %s read failed: %s' % (self.name, e))
            data, status = b'', STATUS_IO_DEVICE_ERROR
        finally:
            with self._lock:
                self._readers.discard(pending)
        if irp.isCancelled():
            irp.discard()
            return
        irp.ioStatus = status
        irp.output = bytearray(pack('<I', len(data)) + data)
        irp.complete()

    def _read(self, pending, length):
        io = self._io
        if io is None:
            return b'', STATUS_CANCELLED
        if length == 0:
            return b'', STATUS_SUCCESS
        interval, multiplier, constant = self._timeouts[:3]
        data = bytearray()

        def take():
            with self._lock:  # atomic with the event poller (_rxSeen)
                chunk = io.read(length - len(data))
                if chunk:
                    self._onBytesRead(len(chunk))
            data.extend(chunk)
            return len(chunk)

        if interval == MAXULONG and multiplier == 0 and constant == 0:
            take()  # return immediately with what is there
            return bytes(data), STATUS_SUCCESS
        now = monotonic()
        firstByteOnly = interval == MAXULONG and multiplier == MAXULONG and 0 < constant < MAXULONG
        if firstByteOnly:
            deadline = now + constant / 1000.0
        elif multiplier or constant:
            deadline = now + (length * multiplier + constant) / 1000.0
        else:
            deadline = None  # no total timeout
        useInterval = 0 < interval < MAXULONG
        lastByte = None
        while len(data) < length:
            if pending.cancelStatus is not None or pending.irp.isCancelled():
                return bytes(data), STATUS_CANCELLED if pending.cancelStatus is None else pending.cancelStatus
            now = monotonic()
            limits = []
            if deadline is not None:
                limits.append(deadline - now)
            if useInterval and lastByte is not None:
                limits.append(lastByte + interval / 1000.0 - now)
            if limits and min(limits) <= 0:
                return bytes(data), STATUS_TIMEOUT
            wait = min(limits + [_SLICE * 4])
            if io.waitReadable(wait):
                if take():
                    lastByte = monotonic()
                    if firstByteOnly:
                        take()
                        break
                elif io.fd is not None:
                    sleep(_SLICE)  # readable without data: line hang-up, do not spin
        return bytes(data), STATUS_SUCCESS

    # -------------------------------------------------------------------
    # write lane
    # -------------------------------------------------------------------

    def _processWrite(self, irp):
        s = irp.input
        try:
            length = s.u32()
            s.u64()  # Offset, ignored (MS-RDPESP 3.2.5.1.5)
            s.skip(20)  # Padding
            data = s.read(length)
        except ValueError as e:
            log.warning(u'serial: malformed write request: %s' % e)
            irp.output, irp.ioStatus = bytearray(5), STATUS_INVALID_PARAMETER
            irp.complete()
            return
        pending = self._register(self._writers, irp)
        if pending is None:
            irp.output, irp.ioStatus = bytearray(5), STATUS_INVALID_HANDLE
            irp.complete()
            return
        written, status = 0, STATUS_SUCCESS
        try:
            multiplier, constant = self._timeouts[3:]
            deadline = (monotonic() + (len(data) * multiplier + constant) / 1000.0
                        if multiplier or constant else None)
            written, status = self._write(pending, data, deadline)
        except Exception as e:
            log.warning(u'serial: %s write failed: %s' % (self.name, e))
            status = STATUS_IO_DEVICE_ERROR
        finally:
            with self._lock:
                self._writers.discard(pending)
        if irp.isCancelled():
            irp.discard()
            return
        irp.ioStatus = status
        irp.output = bytearray(pack('<IB', written, 0))  # Length, Padding
        irp.complete()

    def _write(self, pending, data, deadline):
        io = self._io
        if io is None:
            return 0, STATUS_CANCELLED
        view, written = memoryview(data), 0
        while written < len(data):
            if pending is not None and (pending.cancelStatus is not None or pending.irp.isCancelled()):
                return written, STATUS_CANCELLED if pending.cancelStatus is None else pending.cancelStatus
            n = io.write(view[written:written + 65536].tobytes())
            if n:
                written += n
                self._onBytesWritten(n)
                continue
            left = None if deadline is None else deadline - monotonic()
            if left is not None and left <= 0:
                return written, STATUS_TIMEOUT
            io.waitWritable(_SLICE * 4 if left is None else min(left, _SLICE * 4))
        return written, STATUS_SUCCESS

    def _writeAll(self, data, deadline):
        written, status = self._write(None, data, deadline)
        return status == STATUS_SUCCESS

    # -------------------------------------------------------------------
    # IOCTL_SERIAL_WAIT_ON_MASK (WaitCommEvent), on its own thread
    # -------------------------------------------------------------------

    def _startWait(self, irp, outputLength):
        """
        @return: {bool} True when the request is pending (completed by the wait thread)
        """
        with self._lock:
            current = self._wait
            if current is not None and current.irp.completionId == irp.completionId:
                # mstsc sends a pending WAIT_ON_MASK again (FreeRDP "IRP recall"): keep the first
                log.debug(u'serial: WAIT_ON_MASK recall (CompletionId %d), dropped' % irp.completionId)
                irp.discard()
                return True
            if self._port is None:
                status = STATUS_INVALID_HANDLE
            elif outputLength < 4:
                status = STATUS_BUFFER_TOO_SMALL
            elif current is not None or not self._waitMask:
                status = STATUS_INVALID_PARAMETER  # Serial.sys: no mask, or a wait already pending
            else:
                status = None
                pending = self._wait = _Pending(irp)
        if status is not None:
            irp.ioStatus = STATUS_SUCCESS if self.permissive else status
            irp.output = bytearray(pack('<I', 0))
            return False
        thread = Thread(target=self._waitOnMask, args=(pending,),
                                  name=u'rdpdr-serial-%s-wait' % self.name)
        thread.daemon = True
        self._waitThread = thread
        thread.start()
        return True

    def _drainWait(self, timeout=2.0):
        end = monotonic() + timeout
        while monotonic() < end:
            with self._lock:
                if self._wait is None:
                    return
            sleep(0.005)

    def _waitOnMask(self, pending):
        irp = pending.irp
        mask, status = 0, STATUS_SUCCESS
        try:
            while True:
                if pending.cancelStatus is not None or irp.isCancelled():
                    status = STATUS_CANCELLED if pending.cancelStatus is None else pending.cancelStatus
                    break
                history = self._pollEvents()
                with self._lock:
                    hit = history & self._waitMask
                    if hit:
                        self._history &= ~hit
                        mask = hit
                        break
                io = self._io
                if io is None:
                    status = STATUS_CANCELLED
                    break
                if self._waitMask & (SERIAL_EV_RXCHAR | SERIAL_EV_RXFLAG | SERIAL_EV_RX80FULL):
                    io.waitReadable(_SLICE)
                else:
                    sleep(_SLICE)
        except Exception as e:
            log.warning(u'serial: %s wait on mask failed: %s' % (self.name, e))
            status = STATUS_IO_DEVICE_ERROR
        finally:
            with self._lock:
                if self._wait is pending:
                    self._wait = None
        if irp.isCancelled():
            irp.discard()
            return
        irp.ioStatus = status
        irp.output = bytearray(pack('<II', 4, mask) if status == STATUS_SUCCESS else pack('<I', 0))
        irp.complete()


def parseSerialSpec(spec):
    """
    @summary: Parse a serial port description (FreeRDP /serial:<name>,<path>[,<driver>[,permissive]]):
        "COM1=/dev/ttyS0"  "COM1,/dev/ttyUSB0,SerCx2,permissive"  "COM2=socket://host:7000"
    @return: {tuple} (name, path, driver, permissive)
    @raise ValueError: invalid description
    """
    spec = (spec or '').strip()
    if '=' in spec and (spec.index('=') < spec.index(',') if ',' in spec else True):
        name, _, rest = spec.partition('=')
        parts = [name] + rest.split(',')
    else:
        parts = spec.split(',')
    parts = [p.strip() for p in parts]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(u'serial port: expected NAME=PATH[,DRIVER][,permissive], got \'%s\'' % spec)
    name, path, options = parts[0], parts[1], parts[2:]
    driver, permissive = None, False
    for option in options:
        if not option:
            continue
        if option.lower() == 'permissive':
            permissive = True
        elif option.lower() in [d.lower() for d in SERIAL_DRIVERS]:
            driver = [d for d in SERIAL_DRIVERS if d.lower() == option.lower()][0]
        else:
            raise ValueError(u'serial port: unknown option \'%s\' (driver Serial, SerCx, SerCx2 or permissive)'
                             % option)
    return name, path, driver, permissive
