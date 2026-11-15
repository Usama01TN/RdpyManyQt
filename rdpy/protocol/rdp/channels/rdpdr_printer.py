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
RDPDR printer redirection (MS-RDPEPC Print Virtual Channel Extension).
A local printer is announced to the server, which installs it with the
printer driver named in the announcement. Windows renders each document with
that driver and streams the result (PostScript for "MS Publisher
Imagesetter", PDF for "Microsoft Print to PDF", the printer language for a
native driver) through IRP_MJ_CREATE / WRITE / CLOSE: the client spools it
and hands it to a backend:
* CupsBackend: CUPS on Linux / macOS / BSD (lp, lpstat); CUPS converts
  PostScript / PDF for the real printer;
* WindowsBackend: the Windows spooler (winspool through ctypes), raw data;
* FileBackend: saves every job in a folder ("print to file", PDF by default).
Ported from FreeRDP channels/printer/client (printer_main.c, printer_cups.c,
printer_win.c), including the printer configuration cache the server sends
(PAKID_PRN_CACHE_DATA), restored in the next announce.
Protocol reference: [MS-RDPEPC], [MS-RDPEFS]
"""
from __future__ import absolute_import, division, print_function
from os.path import abspath, exists, expanduser, isdir, join
from subprocess import PIPE, Popen, TimeoutExpired
from os import environ, fdopen, makedirs, remove
from base64 import b64decode, b64encode
from re import finditer, M, search, sub
from shutil import move, which
from platform import release
from tempfile import mkstemp
from json import dump, load
from threading import Lock
from hashlib import sha256
from time import strftime
from sys import platform
from struct import pack
from .rdpdr import RdpdrDevice, IrpWorker, decodeUnicodeString, RDPDR_DTYP_PRINT, RDPDR_CTYP_PRN, \
    PAKID_PRN_CACHE_DATA, PAKID_PRN_USING_XPS, IRP_MJ_CREATE, IRP_MJ_CLOSE, IRP_MJ_WRITE, IRP_MJ_DEVICE_CONTROL, \
    STATUS_SUCCESS, STATUS_UNSUCCESSFUL, STATUS_NOT_SUPPORTED, STATUS_PRINT_QUEUE_FULL, STATUS_INVALID_PARAMETER
from rdpy.core.compat import replace
from rdpy.core import log

# DR_PRN_DEVICE_ANNOUNCE.Flags (MS-RDPEPC 2.2.2.1)
RDPDR_PRINTER_ANNOUNCE_FLAG_ASCII = 0x00000001
RDPDR_PRINTER_ANNOUNCE_FLAG_DEFAULTPRINTER = 0x00000002
RDPDR_PRINTER_ANNOUNCE_FLAG_NETWORKPRINTER = 0x00000004
RDPDR_PRINTER_ANNOUNCE_FLAG_TSPRINTER = 0x00000008
RDPDR_PRINTER_ANNOUNCE_FLAG_XPSFORMAT = 0x00000010
# SERVER_PRINTER_CACHE_EVENT.EventId (MS-RDPEPC 2.2.2.3)
RDPDR_ADD_PRINTER_EVENT = 0x00000001
RDPDR_UPDATE_PRINTER_EVENT = 0x00000002
RDPDR_DELETE_PRINTER_EVENT = 0x00000003
RDPDR_RENAME_PRINTER_EVENT = 0x00000004
# Printer drivers present on every Windows server: the server renders the
# documents with the driver named in the announce
DRIVER_POSTSCRIPT = u'MS Publisher Imagesetter'  # PostScript, every Windows version
DRIVER_PDF = u'Microsoft Print to PDF'  # PDF, Windows 10 / Server 2016 and later
# Largest print job spooled by the client (a server can not fill the disk)
MAX_JOB_SIZE = 2 * 1024 * 1024 * 1024
_APP = u'RdpyManyQt'


def defaultDriverName():
    """
    @summary: Driver announced when none is configured (FreeRDP printer_cups.c):
    PostScript, or PDF on macOS 14 (Sonoma) and later, whose CUPS no longer
    converts PostScript
    @return: {str}
    """
    if platform == 'darwin':
        try:
            if int(release().split('.')[0]) >= 23:
                return DRIVER_PDF
        except ValueError:
            pass
    return DRIVER_POSTSCRIPT


def _configBase():
    if platform == 'win32':
        base = environ.get('APPDATA') or expanduser('~')
    else:
        base = environ.get('XDG_CONFIG_HOME') or join(expanduser('~'), '.config')
    return join(base, 'rdpymanyqt')


def _utf16z(text):
    """UTF-16LE with the null terminator, the form used on the wire"""
    return (text + '\0').encode('utf-16-le')


def jobTitle(jobId):
    """
    @summary: Name of a print job (FreeRDP printer_cups_get_printjob_name)
    """
    return u'%s Print %s - Job %d' % (_APP, strftime(u'%Y-%m-%d %H-%M-%S'), jobId)


def sniffExtension(head):
    """
    @summary: File extension of a print job from its first bytes
    @param head: {bytes}
    @return: {str}
    """
    if head.startswith(b'%PDF'):
        return '.pdf'
    if head.startswith(b'%!') or head.startswith(b'\x04%!') or head.startswith(b'\xc5\xd0\xd3\xc6'):
        return '.ps'
    if head.startswith(b'PK\x03\x04'):
        return '.xps'
    if head.startswith(b'\x1bE') or head.startswith(b'\x1b%-12345X'):
        return '.pcl'
    return '.prn'


class PrinterInfo(object):
    """
    @summary: A local printer of a backend
    """

    def __init__(self, name, driver=None, isDefault=False):
        """
        @param name: {str} printer (queue) name
        @param driver: {str|None} Windows driver announced to the server (None: default)
        @param isDefault: {bool} announced as the default printer
        """
        self.name = name
        self.driver = driver
        self.isDefault = bool(isDefault)

    def __repr__(self):
        return u'PrinterInfo(%r, %r, %r)' % (self.name, self.driver, self.isDefault)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class PrinterBackend(object):
    """
    @summary: Where print jobs go (FreeRDP rdpPrinterDriver)
    """
    name = 'none'

    def available(self):
        """@return: {bool} the backend can print on this machine"""
        return False

    def enumPrinters(self):
        """@return: {list[PrinterInfo]} local printers"""
        return []

    def defaultDriver(self, printerName):
        """@return: {str} driver announced for this printer when none is configured"""
        return defaultDriverName()

    def submit(self, printerName, title, path):
        """
        @summary: Print the spooled job file (called from the printer thread)
        @return: {str} where the job went (shown in the log / transfer hooks)
        @raise OSError: the job could not be printed
        """
        raise OSError(u'no printer backend')


def _run(args, inputData=None, timeout=60):
    env = dict(environ, LC_ALL='C', LANG='C')
    proc = Popen(args, stdin=PIPE if inputData is not None else None,
                 stdout=PIPE, stderr=PIPE, env=env)
    try:
        out, err = proc.communicate(inputData, timeout=timeout)
    except TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise OSError(u'%s timed out' % args[0])
    return proc.returncode, out.decode('utf-8', 'replace'), err.decode('utf-8', 'replace')


class CupsBackend(PrinterBackend):
    """
    @summary: CUPS through its command line tools (Linux, macOS, BSD): no library
    to build. Jobs are sent with automatic format detection, so CUPS converts
    PostScript / PDF for printers that do not understand them (FreeRDP
    CUPS_FORMAT_AUTO).
    """
    name = 'cups'

    def __init__(self, lp='lp', lpstat='lpstat'):
        self._lp = lp
        self._lpstat = lpstat

    def available(self):
        return which(self._lp) is not None and which(self._lpstat) is not None

    def enumPrinters(self):
        if not self.available():
            return []
        try:
            code, out, _ = _run([self._lpstat, '-e'], timeout=15)
            names = [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []
            if not names:
                # older CUPS: "printer NAME is idle. ..."
                code, out, _ = _run([self._lpstat, '-p'], timeout=15)
                names = [m.group(1) for m in finditer(r'^printer (\S+)', out, M)]
            code, out, _ = _run([self._lpstat, '-d'], timeout=15)
            match = search(r'destination:\s*(\S+)', out)
            default = match.group(1) if match else None
        except OSError as e:
            log.warning(u'printer: cannot list the CUPS printers: %s' % e)
            return []
        # instances ("queue/instance") share the queue of their printer
        printers, seen = [], set()
        for name in names:
            queueName = name.split('/')[0]
            if queueName not in seen:
                seen.add(queueName)
                printers.append(PrinterInfo(queueName, None, queueName == default))
        if printers and not any(p.isDefault for p in printers):
            printers[0].isDefault = True  # FreeRDP: first printer when none is default
        return printers

    def submit(self, printerName, title, path):
        code, out, err = _run([self._lp, '-d', printerName, '-t', title, '--', path], timeout=120)
        if code != 0:
            raise OSError(u'lp: %s' % (err.strip() or out.strip() or u'exit code %d' % code))
        match = search(r'request id is (\S+)', out)
        return u'%s (CUPS job %s)' % (printerName, match.group(1) if match else '?')


class WindowsBackend(PrinterBackend):
    """
    @summary: The Windows spooler through winspool.drv (ctypes, no pywin32).
    The server renders with the printer's own driver (the driver name of the
    local printer is announced), so the data is written raw (FreeRDP printer_win.c).
    """
    name = 'win'
    PRINTER_ENUM_LOCAL = 0x00000002
    PRINTER_ENUM_CONNECTIONS = 0x00000004

    def __init__(self):
        self._winspool = None
        if platform == 'win32':
            try:
                import ctypes
                self._winspool = ctypes.WinDLL('winspool.drv', use_last_error=True)
            except OSError as e:
                log.warning(u'printer: winspool.drv not available: %s' % e)

    def available(self):
        return self._winspool is not None

    @staticmethod
    def _printerInfo2():
        import ctypes
        from ctypes import wintypes

        class PRINTER_INFO_2W(ctypes.Structure):
            _fields_ = [(n, wintypes.LPWSTR) for n in (
                'pServerName', 'pPrinterName', 'pShareName', 'pPortName', 'pDriverName',
                'pComment', 'pLocation')] + [
                           ('pDevMode', ctypes.c_void_p)] + [(n, wintypes.LPWSTR) for n in (
                'pSepFile', 'pPrintProcessor', 'pDatatype', 'pParameters')] + [
                           ('pSecurityDescriptor', ctypes.c_void_p)] + [(n, wintypes.DWORD) for n in (
                'Attributes', 'Priority', 'DefaultPriority', 'StartTime', 'UntilTime', 'Status',
                'cJobs', 'AveragePPM')]

        return PRINTER_INFO_2W

    def _defaultPrinter(self):
        import ctypes
        from ctypes import wintypes
        size = wintypes.DWORD(0)
        self._winspool.GetDefaultPrinterW(None, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_unicode_buffer(size.value)
        if not self._winspool.GetDefaultPrinterW(buf, ctypes.byref(size)):
            return None
        return buf.value

    def enumPrinters(self):
        if not self.available():
            return []
        import ctypes
        from ctypes import wintypes
        info = self._printerInfo2()
        flags = self.PRINTER_ENUM_LOCAL | self.PRINTER_ENUM_CONNECTIONS
        needed, returned = wintypes.DWORD(0), wintypes.DWORD(0)
        self._winspool.EnumPrintersW(flags, None, 2, None, 0, ctypes.byref(needed), ctypes.byref(returned))
        if not needed.value:
            return []
        buf = (ctypes.c_byte * needed.value)()
        if not self._winspool.EnumPrintersW(flags, None, 2, buf, needed, ctypes.byref(needed),
                                            ctypes.byref(returned)):
            log.warning(u'printer: EnumPrinters failed (%d)' % ctypes.get_last_error())
            return []
        array = ctypes.cast(buf, ctypes.POINTER(info))
        default = self._defaultPrinter()
        printers = []
        for i in range(returned.value):
            name = array[i].pPrinterName
            if name:
                printers.append(PrinterInfo(name, array[i].pDriverName, name == default))
        if printers and not any(p.isDefault for p in printers):
            printers[0].isDefault = True
        return printers

    def defaultDriver(self, printerName):
        for printer in self.enumPrinters():
            if printer.name == printerName and printer.driver:
                return printer.driver
        return defaultDriverName()

    def submit(self, printerName, title, path):
        import ctypes
        from ctypes import wintypes

        class DOC_INFO_1W(ctypes.Structure):
            _fields_ = [('pDocName', wintypes.LPWSTR), ('pOutputFile', wintypes.LPWSTR),
                        ('pDatatype', wintypes.LPWSTR)]

        spool = self._winspool
        handle = wintypes.HANDLE()
        if not spool.OpenPrinterW(printerName, ctypes.byref(handle), None):
            raise OSError(u'OpenPrinter(%s) failed (%d)' % (printerName, ctypes.get_last_error()))
        try:
            doc = DOC_INFO_1W(title, None, None)  # default data type of the driver (FreeRDP)
            if not spool.StartDocPrinterW(handle, 1, ctypes.byref(doc)):
                raise OSError(u'StartDocPrinter failed (%d)' % ctypes.get_last_error())
            try:
                spool.StartPagePrinter(handle)
                with open(path, 'rb') as f:
                    while True:
                        chunk = f.read(256 * 1024)
                        if not chunk:
                            break
                        written = wintypes.DWORD(0)
                        if not spool.WritePrinter(handle, chunk, len(chunk), ctypes.byref(written)):
                            raise OSError(u'WritePrinter failed (%d)' % ctypes.get_last_error())
                spool.EndPagePrinter(handle)
            finally:
                spool.EndDocPrinter(handle)
        finally:
            spool.ClosePrinter(handle)
        return printerName


class FileBackend(PrinterBackend):
    """
    @summary: "Print to folder": every job is saved as a file (.pdf with the
    default "Microsoft Print to PDF" driver, .ps with "MS Publisher Imagesetter")
    """
    name = 'file'

    def __init__(self, directory):
        self.directory = abspath(expanduser(directory))

    def available(self):
        return True

    def enumPrinters(self):
        return [PrinterInfo(u'Print to folder', DRIVER_PDF, False)]

    def defaultDriver(self, printerName):
        return DRIVER_PDF

    def submit(self, printerName, title, path):
        if not isdir(self.directory):
            makedirs(self.directory)
        with open(path, 'rb') as f:
            ext = sniffExtension(f.read(16))
        base = sub(r'[\\/:*?"<>|\x00-\x1f]+', '_', title).strip(' .') or 'print'
        target = join(self.directory, base + ext)
        n = 1
        while exists(target):
            n += 1
            target = join(self.directory, u'%s (%d)%s' % (base, n, ext))
        move(path, target)
        return target


def defaultBackend():
    """
    @summary: The printing system of this machine (FreeRDP: cups, else win)
    @return: {PrinterBackend}
    """
    if platform == 'win32':
        backend = WindowsBackend()
        if backend.available():
            return backend
    return CupsBackend()


# ---------------------------------------------------------------------------
# Printer configuration cache (PAKID_PRN_CACHE_DATA, FreeRDP printer_main.c)
# ---------------------------------------------------------------------------

class PrinterConfigStore(object):
    """
    @summary: Configuration the server stores for a printer (paper, trays...)
    in the CachedPrinterConfigData of the next announce. One JSON file per
    printer, named after the SHA-256 of its name.
    """

    def __init__(self, directory=None):
        self.directory = directory or join(_configBase(), 'printers')
        self._lock = Lock()

    def _path(self, rawName):
        # the key is the decoded name: the server may or may not count the terminator
        name = decodeUnicodeString(rawName) if isinstance(rawName, bytes) else rawName
        return join(self.directory, sha256(name.encode('utf-8')).hexdigest() + '.json')

    def _load(self, rawName):
        try:
            with open(self._path(rawName), 'r') as f:
                return load(f)
        except (IOError, OSError, ValueError):
            return None

    def _save(self, rawName, entry):
        path = self._path(rawName)
        if not isdir(self.directory):
            makedirs(self.directory)
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            dump(entry, f)
        replace(tmp, path)

    def cachedConfig(self, rawName):
        """@return: {bytes} CachedPrinterConfigData for the printer (b'' when none)"""
        with self._lock:
            entry = self._load(rawName) or {}
        try:
            return b64decode(entry.get('CachedPrinterConfigData', ''))
        except (TypeError, ValueError):
            return b''

    def add(self, rawName, portDosName, pnpName, driverName, configData):
        with self._lock:
            self._save(rawName, {
                'PortDosName': b64encode(portDosName).decode('ascii'),
                'PnPName': b64encode(pnpName).decode('ascii'),
                'DriverName': b64encode(driverName).decode('ascii'),
                'CachedPrinterConfigData': b64encode(configData).decode('ascii')})

    def update(self, rawName, configData):
        with self._lock:
            entry = self._load(rawName)
            if entry is None:
                log.warning(u'printer: configuration update for an unknown printer ignored')
                return
            entry['CachedPrinterConfigData'] = b64encode(configData).decode('ascii')
            self._save(rawName, entry)

    def delete(self, rawName):
        with self._lock:
            try:
                remove(self._path(rawName))
            except OSError:
                pass

    def rename(self, oldRawName, newRawName):
        with self._lock:
            entry = self._load(oldRawName)
            if entry is not None:
                self._save(newRawName, entry)
                try:
                    remove(self._path(oldRawName))
                except OSError:
                    pass


def processCacheData(store, s):
    """
    @summary: Handle a PAKID_PRN_CACHE_DATA PDU (FreeRDP printer_custom_component)
    @param store: {PrinterConfigStore}
    @param s: {StreamReader} positioned after the RDPDR header
    """
    eventId = s.u32()
    if eventId == RDPDR_ADD_PRINTER_EVENT:
        portDosName = s.read(8)
        pnpLen, driverLen, nameLen, cacheLen = s.u32(), s.u32(), s.u32(), s.u32()
        pnpName, driverName = s.read(pnpLen), s.read(driverLen)
        printerName, configData = s.read(nameLen), s.read(cacheLen)
        log.info(u'printer: server cached the configuration of \'%s\' (%d bytes)' %
                 (decodeUnicodeString(printerName), len(configData)))
        store.add(printerName, portDosName, pnpName, driverName, configData)
    elif eventId == RDPDR_UPDATE_PRINTER_EVENT:
        nameLen, cacheLen = s.u32(), s.u32()
        printerName, configData = s.read(nameLen), s.read(cacheLen)
        log.debug(u'printer: configuration of \'%s\' updated' % decodeUnicodeString(printerName))
        store.update(printerName, configData)
    elif eventId == RDPDR_DELETE_PRINTER_EVENT:
        printerName = s.read(s.u32())
        store.delete(printerName)
    elif eventId == RDPDR_RENAME_PRINTER_EVENT:
        oldLen, newLen = s.u32(), s.u32()
        oldName, newName = s.read(oldLen), s.read(newLen)
        store.rename(oldName, newName)
    else:
        log.warning(u'printer: unknown cache data event 0x%08x' % eventId)


# ---------------------------------------------------------------------------
# Device
# ---------------------------------------------------------------------------

class PrintJob(object):
    """
    @summary: A document being received: spooled to a temporary file
    """

    def __init__(self, jobId, title):
        self.id = jobId
        self.title = title
        self.size = 0
        fd, self.path = mkstemp(prefix='rdpyqt-print-', suffix='.prn')
        self._file = fdopen(fd, 'wb')

    def write(self, data):
        if self.size + len(data) > MAX_JOB_SIZE:
            raise OSError(u'print job larger than %d bytes' % MAX_JOB_SIZE)
        self._file.write(data)
        self.size += len(data)

    def finish(self):
        self._file.close()
        return self.path

    def discard(self):
        try:
            self._file.close()
        finally:
            try:
                remove(self.path)
            except OSError:
                pass


class PrinterDevice(RdpdrDevice):
    """
    @summary: A redirected printer (FreeRDP PRINTER_DEVICE)
    """

    def __init__(self, portName, printerName, backend, driverName=None, isDefault=False,
                 configStore=None):
        """
        @param portName: {str} DOS name of the port, PRN1, PRN2...
        @param printerName: {str} local printer (CUPS queue / Windows printer) and the
                            name shown on the server
        @param backend: {PrinterBackend}
        @param driverName: {str|None} Windows driver the server renders with
                           (default: the backend's, "MS Publisher Imagesetter")
        @param isDefault: {bool} the server makes it the session's default printer
        @param configStore: {PrinterConfigStore|None} printer configuration cache
        """
        RdpdrDevice.__init__(self, RDPDR_DTYP_PRINT, portName)
        self.printerName = printerName
        self.backend = backend
        self.driverName = driverName or backend.defaultDriver(printerName)
        self.isDefault = bool(isDefault)
        self.configStore = configStore if configStore is not None else PrinterConfigStore()
        self._jobs = {}
        self._callback = None
        self._worker = IrpWorker(u'rdpdr-printer-%s' % portName, self._processIrp, self._closeAll)

    # FreeRDP replaces '\\' (network printers) in the announced name
    @property
    def announcedName(self):
        return self.printerName.replace('\\', '_')

    @property
    def deviceData(self):
        """
        @summary: DR_PRN_DEVICE_ANNOUNCE.DeviceData (FreeRDP printer_load_from_config),
        built at each announce so a configuration cached by the server is sent back
        """
        flags = RDPDR_PRINTER_ANNOUNCE_FLAG_DEFAULTPRINTER if self.isDefault else 0
        pnpName = b''
        driverName = _utf16z(self.driverName)
        printName = _utf16z(self.announcedName)
        cached = self.configStore.cachedConfig(self.announcedName) if self.configStore else b''
        return pack('<IIIIII', flags, 0, len(pnpName), len(driverName), len(printName),
                    len(cached)) + pnpName + driverName + printName + cached

    @deviceData.setter
    def deviceData(self, value):
        pass  # computed (RdpdrDevice.__init__ assigns it)

    def setJobCallback(self, callback):
        """
        @summary: callback(printerName, title, bytes, destination, error) once a
        job was handed to the backend (error is None on success). Printer thread.
        """
        self._callback = callback

    # -------------------------------------------------------------------
    # RdpdrDevice interface
    # -------------------------------------------------------------------

    def irpRequest(self, irp):
        self._worker.submit(irp)

    def customComponentRequest(self, component, packetId, s):
        if component != RDPDR_CTYP_PRN:
            return False
        try:
            if packetId == PAKID_PRN_CACHE_DATA:
                processCacheData(self.configStore, s)
            elif packetId == PAKID_PRN_USING_XPS:
                printerId, flags = s.u32(), s.u32()
                log.debug(u'printer: server uses XPS for printer %d (flags 0x%x), ignored' %
                          (printerId, flags))
            else:
                log.warning(u'printer: unknown printer component packet 0x%04x' % packetId)
        except (ValueError, OSError) as e:
            log.warning(u'printer: cannot process printer cache data: %s' % e)
        return True

    def init(self):
        self._worker.reset()

    def free(self):
        self._worker.stop()

    # -------------------------------------------------------------------
    # IRPs (FreeRDP printer_process_irp), on the printer thread
    # -------------------------------------------------------------------

    def _closeAll(self):
        """@summary: the server forgot its jobs (channel reset or disconnect): drop them"""
        for job in list(self._jobs.values()):
            log.warning(u'printer: job \'%s\' abandoned by the server, not printed' % job.title)
            job.discard()
        self._jobs.clear()

    def _processIrp(self, irp):
        irp.ioStatus = STATUS_SUCCESS
        try:
            if irp.majorFunction == IRP_MJ_CREATE:
                self._irpCreate(irp)
            elif irp.majorFunction == IRP_MJ_WRITE:
                self._irpWrite(irp)
            elif irp.majorFunction == IRP_MJ_CLOSE:
                self._irpClose(irp)
            elif irp.majorFunction == IRP_MJ_DEVICE_CONTROL:
                irp.output = bytearray(pack('<I', 0))  # OutputBufferLength
            else:
                irp.ioStatus = STATUS_NOT_SUPPORTED
        except ValueError as e:
            log.warning(u'printer: malformed IRP 0x%x: %s' % (irp.majorFunction, e))
            irp.output = bytearray(5 if irp.majorFunction == IRP_MJ_WRITE else 4)
            irp.ioStatus = STATUS_INVALID_PARAMETER
        irp.complete()

    def _irpCreate(self, irp):
        jobId = self.devman.nextId() if self.devman is not None else len(self._jobs) + 1
        try:
            job = PrintJob(jobId, jobTitle(jobId))
        except (IOError, OSError) as e:
            log.error(u'printer: cannot spool a print job: %s' % e)
            irp.output = bytearray(pack('<I', 0))
            irp.ioStatus = STATUS_PRINT_QUEUE_FULL
            return
        self._jobs[jobId] = job
        log.info(u'printer: \'%s\' job %d started' % (self.printerName, jobId))
        irp.output = bytearray(pack('<I', jobId))  # FileId

    def _irpWrite(self, irp):
        s = irp.input
        length = s.u32()
        s.u64()  # Offset, reserved (MS-RDPEPC 2.2.2.9)
        s.skip(20)  # Padding
        data = s.read(length)
        job = self._jobs.get(irp.fileId)
        if job is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
            length = 0
        else:
            try:
                job.write(data)
            except (IOError, OSError) as e:
                log.error(u'printer: job %d: %s' % (job.id, e))
                irp.ioStatus = STATUS_UNSUCCESSFUL
                length = 0
        irp.output = bytearray(pack('<IB', length, 0))  # Length + Padding

    def _irpClose(self, irp):
        irp.output = bytearray(4)  # Padding
        job = self._jobs.pop(irp.fileId, None)
        if job is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
            return
        path = job.finish()
        if job.size == 0:
            log.info(u'printer: \'%s\' job %d is empty, not printed' % (self.printerName, job.id))
            job.discard()
            return
        error, destination = None, None
        try:
            destination = self.backend.submit(self.printerName, job.title, path)
            log.info(u'printer: \'%s\' job %d (%d bytes) printed: %s' %
                     (self.printerName, job.id, job.size, destination))
        except (IOError, OSError) as e:
            error = str(e)
            log.error(u'printer: \'%s\' job %d not printed: %s' % (self.printerName, job.id, e))
        finally:
            if exists(path):
                remove(path)
        if self._callback is not None:
            try:
                self._callback(self.printerName, job.title, job.size, destination, error)
            except Exception as e:
                log.warning(u'printer: job callback failed: %s' % e)


def parsePrinterSpec(spec):
    """
    @summary: Parse a printer description (FreeRDP /printer:<name>[,<driver>[,default]]):
        "*"                          every local printer
        "NAME[,DRIVER][,default]"    one local printer
        "file:FOLDER[,DRIVER]"       save the jobs in FOLDER (PDF by default)
    @param spec: {str}
    @return: {dict} kind ('all' | 'printer' | 'file'), name, driver, isDefault, folder
    @raise ValueError: empty description
    """
    spec = (spec or '').strip()
    if not spec:
        raise ValueError(u'empty printer description')
    if spec == '*':
        return {'kind': 'all', 'name': None, 'driver': None, 'isDefault': False, 'folder': None}
    if spec.lower().startswith('file:'):
        rest = spec[5:]
        folder, _, driver = rest.partition(',')
        if not folder.strip():
            raise ValueError(u'printer: file: needs a folder')
        return {'kind': 'file', 'name': None, 'driver': driver.strip() or None, 'isDefault': False,
                'folder': abspath(expanduser(folder.strip()))}
    parts = [p.strip() for p in spec.split(',')]
    isDefault = len(parts) > 1 and parts[-1].lower() == 'default'
    if isDefault:
        parts = parts[:-1]
    return {'kind': 'printer', 'name': parts[0], 'driver': (parts[1] if len(parts) > 1 and parts[1] else None),
            'isDefault': isDefault, 'folder': None}
