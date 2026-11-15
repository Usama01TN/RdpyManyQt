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
RDPDR smart card redirection (MS-RDPESC Smart Card Virtual Channel Extension).
The server forwards the WinSCard calls of the remote session (smart card
logon, certificates, PIV / PKCS#11 middleware...) as IOCTLs whose parameters
are NDR (MS-RPCE type serialization version 1) encoded. They are decoded,
executed on the local smart card readers through PC/SC (pcsc-lite on Linux /
BSD, the PCSC framework on macOS, WinSCard on Windows, through pyscard) and
the results encoded back.
Ported from FreeRDP channels/smartcard/client/smartcard_main.c,
libfreerdp/utils/smartcard_{pack,call,operations}.c and the pcsc-lite
adaptations of winpr/libwinpr/smartcard/smartcard_pcsc.c (protocol, card
state, error and control code conversions, emulated SCardReadCache /
SCardWriteCache / SCardGetTransmitCount / SCardGetDeviceTypeId and reader
name attributes, idle transaction watchdog).
As in FreeRDP, the calls of a context are run in order on a thread of the
context, and the calls that may wait for another application (Connect,
Reconnect, BeginTransaction) on a second one. SCardGetStatusChange runs on
its own thread with its own PC/SC context, so a long wait for a card never
delays the other calls of the context, and connected cards use private PC/SC
contexts. (Pcsc-lite serializes the calls of a context.)
Requires pyscard (pip install "rdpymanyqt[smartcard]") and a running PC/SC
service (pcscd on Linux).
Protocol reference: [MS-RDPESC], [MS-RPCE] 2.2.6, [MS-RDPEFS]
"""
from __future__ import absolute_import, division, print_function
from threading import Event, Lock, RLock, Thread
from struct import pack, unpack, unpack_from
from collections import OrderedDict
from os import environ, urandom
from sys import platform
from time import sleep
from .rdpdr import RdpdrDevice, IrpWorker, StreamReader, RDPDR_DTYP_SMARTCARD, IRP_MJ_CREATE, IRP_MJ_CLOSE, \
    IRP_MJ_DEVICE_CONTROL, STATUS_SUCCESS, STATUS_UNSUCCESSFUL, STATUS_NOT_SUPPORTED, STATUS_BUFFER_TOO_SMALL
from rdpy.core.compat import monotonic
from rdpy.core import log


def SCARD_CTL_CODE(code):
    return 0x00090000 | (code << 2)


SCARD_IOCTL_ESTABLISHCONTEXT = SCARD_CTL_CODE(5)
SCARD_IOCTL_RELEASECONTEXT = SCARD_CTL_CODE(6)
SCARD_IOCTL_ISVALIDCONTEXT = SCARD_CTL_CODE(7)
SCARD_IOCTL_LISTREADERGROUPSA = SCARD_CTL_CODE(8)
SCARD_IOCTL_LISTREADERGROUPSW = SCARD_CTL_CODE(9)
SCARD_IOCTL_LISTREADERSA = SCARD_CTL_CODE(10)
SCARD_IOCTL_LISTREADERSW = SCARD_CTL_CODE(11)
SCARD_IOCTL_INTRODUCEREADERGROUPA = SCARD_CTL_CODE(20)
SCARD_IOCTL_INTRODUCEREADERGROUPW = SCARD_CTL_CODE(21)
SCARD_IOCTL_FORGETREADERGROUPA = SCARD_CTL_CODE(22)
SCARD_IOCTL_FORGETREADERGROUPW = SCARD_CTL_CODE(23)
SCARD_IOCTL_INTRODUCEREADERA = SCARD_CTL_CODE(24)
SCARD_IOCTL_INTRODUCEREADERW = SCARD_CTL_CODE(25)
SCARD_IOCTL_FORGETREADERA = SCARD_CTL_CODE(26)
SCARD_IOCTL_FORGETREADERW = SCARD_CTL_CODE(27)
SCARD_IOCTL_ADDREADERTOGROUPA = SCARD_CTL_CODE(28)
SCARD_IOCTL_ADDREADERTOGROUPW = SCARD_CTL_CODE(29)
SCARD_IOCTL_REMOVEREADERFROMGROUPA = SCARD_CTL_CODE(30)
SCARD_IOCTL_REMOVEREADERFROMGROUPW = SCARD_CTL_CODE(31)
SCARD_IOCTL_LOCATECARDSA = SCARD_CTL_CODE(38)
SCARD_IOCTL_LOCATECARDSW = SCARD_CTL_CODE(39)
SCARD_IOCTL_GETSTATUSCHANGEA = SCARD_CTL_CODE(40)
SCARD_IOCTL_GETSTATUSCHANGEW = SCARD_CTL_CODE(41)
SCARD_IOCTL_CANCEL = SCARD_CTL_CODE(42)
SCARD_IOCTL_CONNECTA = SCARD_CTL_CODE(43)
SCARD_IOCTL_CONNECTW = SCARD_CTL_CODE(44)
SCARD_IOCTL_RECONNECT = SCARD_CTL_CODE(45)
SCARD_IOCTL_DISCONNECT = SCARD_CTL_CODE(46)
SCARD_IOCTL_BEGINTRANSACTION = SCARD_CTL_CODE(47)
SCARD_IOCTL_ENDTRANSACTION = SCARD_CTL_CODE(48)
SCARD_IOCTL_STATE = SCARD_CTL_CODE(49)
SCARD_IOCTL_STATUSA = SCARD_CTL_CODE(50)
SCARD_IOCTL_STATUSW = SCARD_CTL_CODE(51)
SCARD_IOCTL_TRANSMIT = SCARD_CTL_CODE(52)
SCARD_IOCTL_CONTROL = SCARD_CTL_CODE(53)
SCARD_IOCTL_GETATTRIB = SCARD_CTL_CODE(54)
SCARD_IOCTL_SETATTRIB = SCARD_CTL_CODE(55)
SCARD_IOCTL_ACCESSSTARTEDEVENT = SCARD_CTL_CODE(56)
SCARD_IOCTL_RELEASETARTEDEVENT = SCARD_CTL_CODE(57)
SCARD_IOCTL_LOCATECARDSBYATRA = SCARD_CTL_CODE(58)
SCARD_IOCTL_LOCATECARDSBYATRW = SCARD_CTL_CODE(59)
SCARD_IOCTL_READCACHEA = SCARD_CTL_CODE(60)
SCARD_IOCTL_READCACHEW = SCARD_CTL_CODE(61)
SCARD_IOCTL_WRITECACHEA = SCARD_CTL_CODE(62)
SCARD_IOCTL_WRITECACHEW = SCARD_CTL_CODE(63)
SCARD_IOCTL_GETTRANSMITCOUNT = SCARD_CTL_CODE(64)
SCARD_IOCTL_GETREADERICON = SCARD_CTL_CODE(65)
SCARD_IOCTL_GETDEVICETYPEID = SCARD_CTL_CODE(66)
IOCTL_NAMES = dict((v, k[12:]) for k, v in list(globals().items()) if k.startswith('SCARD_IOCTL_'))
# return codes (WinSCard values)
SCARD_S_SUCCESS = 0x00000000
SCARD_F_INTERNAL_ERROR = 0x80100001
SCARD_E_CANCELLED = 0x80100002
SCARD_E_INVALID_HANDLE = 0x80100003
SCARD_E_INVALID_PARAMETER = 0x80100004
SCARD_E_NO_MEMORY = 0x80100006
SCARD_E_INSUFFICIENT_BUFFER = 0x80100008
SCARD_E_UNKNOWN_READER = 0x80100009
SCARD_E_TIMEOUT = 0x8010000A
SCARD_E_INVALID_VALUE = 0x80100011
SCARD_F_UNKNOWN_ERROR = 0x80100014
SCARD_E_NO_SERVICE = 0x8010001D
SCARD_E_UNEXPECTED = 0x8010001F  # also pcsc-lite's (wrong) SCARD_E_UNSUPPORTED_FEATURE
SCARD_E_UNSUPPORTED_FEATURE = 0x80100022
SCARD_E_NO_READERS_AVAILABLE = 0x8010002E
SCARD_W_CACHE_ITEM_NOT_FOUND = 0x80100070
SCARD_W_CACHE_ITEM_STALE = 0x80100071
SCARD_W_CACHE_ITEM_TOO_BIG = 0x80100072
SCARD_AUTOALLOCATE = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
# protocols: WinSCard / pcsc-lite
SCARD_PROTOCOL_UNDEFINED = 0x00000000
SCARD_PROTOCOL_T0 = 0x00000001
SCARD_PROTOCOL_T1 = 0x00000002
SCARD_PROTOCOL_Tx = 0x00000003
SCARD_PROTOCOL_RAW = 0x00010000
SCARD_PROTOCOL_DEFAULT = 0x80000000
PCSC_SCARD_PROTOCOL_RAW = 0x00000004
PCSC_SCARD_PROTOCOL_T15 = 0x00000008
SCARD_SHARE_DIRECT = 3
SCARD_LEAVE_CARD = 0
SCARD_RESET_CARD = 1
# SCardStatus card state: WinSCard enumeration / pcsc-lite bit field
SCARD_UNKNOWN, SCARD_ABSENT, SCARD_PRESENT, SCARD_SWALLOWED = 0, 1, 2, 3
SCARD_POWERED, SCARD_NEGOTIABLE, SCARD_SPECIFIC = 4, 5, 6
PCSC_SCARD_UNKNOWN, PCSC_SCARD_ABSENT, PCSC_SCARD_PRESENT = 0x0001, 0x0002, 0x0004
PCSC_SCARD_SWALLOWED, PCSC_SCARD_POWERED = 0x0008, 0x0010
PCSC_SCARD_NEGOTIABLE, PCSC_SCARD_SPECIFIC = 0x0020, 0x0040
SCARD_STATE_EMPTY = 0x00000010
SCARD_STATE_ATRMATCH = 0x00000040
# reader attributes
SCARD_ATTR_VENDOR_NAME = 0x00010100
SCARD_ATTR_CHANNEL_ID = 0x00020110
SCARD_ATTR_CURRENT_PROTOCOL_TYPE = 0x00080201
SCARD_ATTR_DEVICE_FRIENDLY_NAME_A = 0x7FFF0003
SCARD_ATTR_DEVICE_SYSTEM_NAME_A = 0x7FFF0004
SCARD_ATTR_DEVICE_FRIENDLY_NAME_W = 0x7FFF0005
SCARD_ATTR_DEVICE_SYSTEM_NAME_W = 0x7FFF0006
SCARD_READER_TYPE_USB = 0x20
FILE_DEVICE_SMARTCARD = 0x00000031
SMARTCARD_PNP_NOTIFICATION = u'\\\\?PnP?\\Notification'
SCARD_DEFAULT_GROUP = u'SCard$DefaultReaders'
# limits of [MS-RDPESC] / FreeRDP
MAX_READERS = 11
MAX_BUFFER = 66560
MAX_STRING = 65536
STATUS_CHANGE_STEP_MS = 1000
LOCATE_TIMEOUT_MS = 500
MAX_CACHE_ITEMS = 1024
MAX_WAITS = 64
# idle transaction timeout (Windows resets a card whose transaction is idle 5 s)
TRANSACTION_TIMEOUT_MS = int(environ.get('RDPY_SCARD_TRANSACTION_TIMEOUT_MS', '5000') or 0)
_COMMON_TYPE_HEADER = pack('<BBHI', 1, 0x10, 8, 0xCCCCCCCC)
_SYNC_IOCTLS = (SCARD_IOCTL_CANCEL, SCARD_IOCTL_ACCESSSTARTEDEVENT, SCARD_IOCTL_ISVALIDCONTEXT)
_CONTROL_IOCTLS = (SCARD_IOCTL_ESTABLISHCONTEXT, SCARD_IOCTL_RELEASECONTEXT)
# may wait for the transaction of another application: second thread of the context
_TX_IOCTLS = (SCARD_IOCTL_BEGINTRANSACTION, SCARD_IOCTL_CONNECTA, SCARD_IOCTL_CONNECTW,
              SCARD_IOCTL_RECONNECT)
_WAIT_IOCTLS = (SCARD_IOCTL_GETSTATUSCHANGEA, SCARD_IOCTL_GETSTATUSCHANGEW,
                SCARD_IOCTL_LOCATECARDSBYATRA, SCARD_IOCTL_LOCATECARDSBYATRW)
_UNSUPPORTED_IOCTLS = (
    SCARD_IOCTL_INTRODUCEREADERGROUPA, SCARD_IOCTL_INTRODUCEREADERGROUPW,
    SCARD_IOCTL_FORGETREADERGROUPA, SCARD_IOCTL_FORGETREADERGROUPW,
    SCARD_IOCTL_INTRODUCEREADERA, SCARD_IOCTL_INTRODUCEREADERW,
    SCARD_IOCTL_FORGETREADERA, SCARD_IOCTL_FORGETREADERW,
    SCARD_IOCTL_ADDREADERTOGROUPA, SCARD_IOCTL_ADDREADERTOGROUPW,
    SCARD_IOCTL_REMOVEREADERFROMGROUPA, SCARD_IOCTL_REMOVEREADERFROMGROUPW)


def u32(value):
    return value & 0xFFFFFFFF


def isPnpNotification(name):
    return name.lower() == SMARTCARD_PNP_NOTIFICATION.lower()


# ---------------------------------------------------------------------------
# NDR (MS-RPCE 2.2.6 type serialization version 1)
# ---------------------------------------------------------------------------

class NdrReader(object):
    """
    @summary: Decoder of the NDR encoded call structures (FreeRDP smartcard_pack.c
    unpack functions). Raises ValueError on malformed data. Referent ids are
    only checked for null / not null.
    """

    def __init__(self, data):
        self.s = StreamReader(data)

    def u32(self):
        return self.s.u32()

    def i32(self):
        return unpack('<i', self.s.read(4))[0]

    def read(self, n):
        return self.s.read(n)

    def pointer(self):
        return self.s.u32() != 0

    def _pad(self, size):
        pad = (4 - size % 4) % 4
        self.s.skip(min(pad, self.s.remaining()))

    def context(self):
        """@return: {list} [cbContext, present] of a REDIR_SCARDCONTEXT"""
        cb = self.u32()
        if cb > 16:
            raise ValueError(u'REDIR_SCARDCONTEXT cbContext %d > 16' % cb)
        present = self.pointer()
        if bool(cb) != present:
            raise ValueError(u'REDIR_SCARDCONTEXT cbContext / pointer inconsistency')
        return [cb, present]

    handle = context

    def ref(self, header):
        """@return: {bytes|None} the deferred bytes of a context or handle"""
        cb = header[0]
        if not cb:
            return None
        length = self.u32()
        if length != cb:
            raise ValueError(u'handle length %d, cb %d mismatch' % (length, cb))
        value = self.read(cb)
        self._pad(cb)
        return value

    def array(self, elementSize=1, count=None, maxCount=MAX_BUFFER):
        """@summary: conformant array (count, elements, padding to 4)"""
        n = self.u32()
        if count is not None and n != count:
            raise ValueError(u'array count %d, expected %d' % (n, count))
        if n > maxCount:
            raise ValueError(u'array of %d elements too large' % n)
        data = self.read(n * elementSize)
        self._pad(len(data))
        return data

    def string(self, wide):
        """@summary: conformant varying string (max count, offset, actual count)"""
        maxCount, offset, actual = self.u32(), self.u32(), self.u32()
        if maxCount != offset + actual or actual > MAX_STRING:
            raise ValueError(u'invalid NDR string header')
        raw = self.read(actual * (2 if wide else 1))
        self._pad(len(raw))
        return decodeString(raw, wide)

    def readerStates(self, count, wide):
        """@return: {list[dict]} SCARD_READERSTATE[A|W] array with their names"""
        n = self.u32()
        if n != count:
            raise ValueError(u'reader states count %d, expected %d' % (n, count))
        states = []
        for _ in range(count):
            present = self.pointer()
            current, event, cbAtr = self.u32(), self.u32(), self.u32()
            atr = self.read(36)
            states.append({'present': present, 'name': None, 'current': current, 'event': event,
                           'atr': atr[:min(cbAtr, 36)]})
        for state in states:
            if state['present']:
                state['name'] = self.string(wide)
        return states


class NdrWriter(object):
    """
    @summary: Encoder of the NDR return structures (FreeRDP smartcard_pack.c pack
    functions): referent ids are 0x00020000 + 4 * index
    """

    def __init__(self):
        self.buf = bytearray()
        self.index = 0

    def u32(self, value):
        self.buf += pack('<I', u32(value))

    def raw(self, data):
        self.buf += data

    def pointer(self, present):
        if present:
            self.u32(0x00020000 + self.index * 4)
            self.index += 1
        else:
            self.u32(0)

    def array(self, data, count=None):
        """@summary: conformant array; nothing at all for an empty one (null pointer)"""
        if not data:
            return
        self.u32(len(data) if count is None else count)
        self.buf += data
        self.buf += b'\x00' * ((4 - len(data) % 4) % 4)

    def context(self, value):
        if value:
            self.u32(len(value))
            self.pointer(True)
        else:
            self.buf += b'\x00' * 8

    handle = context

    def ref(self, value):
        if value:
            self.u32(len(value))
            self.buf += value


def decodeString(raw, wide):
    text = raw.decode('utf-16-le', 'replace') if wide else raw.decode('utf-8', 'replace')
    return text.split('\x00', 1)[0]


def decodeMultiString(raw, wide):
    text = raw.decode('utf-16-le', 'replace') if wide else raw.decode('utf-8', 'replace')
    return [name for name in text.split('\x00') if name]


def encodeMultiString(names, wide):
    text = ''.join(name + '\x00' for name in names) + '\x00'
    if len(names) == 0:
        text = '\x00\x00'
    return text.encode('utf-16-le') if wide else text.encode('utf-8')


def readerStateReturn(current, event, atr):
    atr = bytes(atr)[:36]
    return pack('<III', u32(current), u32(event), len(atr)) + atr.ljust(36, b'\x00')


# ---------------------------------------------------------------------------
# PC/SC backend (pyscard), WinSCard semantics
# ---------------------------------------------------------------------------

class PcscBackend(object):
    """
    @summary: The local PC/SC service through pyscard. Values exchanged with the
    device are WinSCard ones: on pcsc-lite (Linux, BSD) and the macOS PCSC
    framework, protocols, card states and error codes are converted like winpr
    smartcard_pcsc.c does.
    """

    def __init__(self, scardModule=None, native=None):
        if scardModule is None:
            try:
                from smartcard import scard as scardModule
            except:
                raise OSError(u'pyscard is not installed (pip install "rdpymanyqt[smartcard]")')
        self.scard = scardModule
        self.native = (platform == 'win32') if native is None else native
        self.pnpNotification = platform != 'darwin'

    # conversions (winpr PCSC_*) ---------------------------------------------

    def _rc(self, rc):
        rc = u32(rc)
        if not self.native and rc == SCARD_E_UNEXPECTED:
            rc = SCARD_E_UNSUPPORTED_FEATURE  # pcsc-lite value of SCARD_E_UNSUPPORTED_FEATURE
        return rc

    def protocolsToPcsc(self, protocols):
        if self.native:
            return protocols
        if protocols & SCARD_PROTOCOL_RAW:
            protocols = (protocols & ~SCARD_PROTOCOL_RAW) | PCSC_SCARD_PROTOCOL_RAW
        protocols &= ~SCARD_PROTOCOL_DEFAULT
        if protocols == SCARD_PROTOCOL_UNDEFINED:
            protocols = SCARD_PROTOCOL_Tx
        return u32(protocols)

    def protocolsFromPcsc(self, protocols):
        if self.native:
            return u32(protocols)
        if protocols & PCSC_SCARD_PROTOCOL_RAW:
            protocols = (protocols & ~PCSC_SCARD_PROTOCOL_RAW) | SCARD_PROTOCOL_RAW
        return u32(protocols & ~PCSC_SCARD_PROTOCOL_T15)

    def cardStateFromPcsc(self, state, rc):
        if self.native:
            return state
        if rc == SCARD_S_SUCCESS and state & (PCSC_SCARD_NEGOTIABLE | PCSC_SCARD_SPECIFIC):
            return SCARD_SPECIFIC  # pcsc-lite never reports SPECIFIC, expected by Windows applications
        for bit, value in ((PCSC_SCARD_POWERED, SCARD_POWERED), (PCSC_SCARD_NEGOTIABLE, SCARD_NEGOTIABLE),
                           (PCSC_SCARD_SPECIFIC, SCARD_SPECIFIC), (PCSC_SCARD_ABSENT, SCARD_ABSENT),
                           (PCSC_SCARD_PRESENT, SCARD_PRESENT), (PCSC_SCARD_SWALLOWED, SCARD_SWALLOWED)):
            if state & bit:
                return value
        return SCARD_UNKNOWN

    def controlCodeToPcsc(self, code):
        if self.native:
            return code
        if (code >> 16) == FILE_DEVICE_SMARTCARD:
            return 0x42000000 + ((code >> 2) & 0xFFF)  # pcsc-lite SCARD_CTL_CODE
        return code

    # calls ---------------------------------------------------------------

    def _call(self, name, *args):
        try:
            result = getattr(self.scard, name)(*args)
        except Exception as e:  # pyscard type / system errors
            log.warning(u'smartcard: %s failed: %r' % (name, e))
            return SCARD_F_INTERNAL_ERROR, None, None, None, None
        if isinstance(result, (tuple, list)):  # pyscard returns [hresult, values...]
            return (self._rc(result[0]),) + tuple(result[1:])
        return (self._rc(result),)

    def establish(self, scope):
        rc, context = self._call('SCardEstablishContext', scope)[:2]
        return rc, context

    def release(self, context):
        return self._call('SCardReleaseContext', context)[0]

    def isValid(self, context):
        return self._call('SCardIsValidContext', context)[0]

    def cancel(self, context):
        return self._call('SCardCancel', context)[0]

    def listReaders(self, context):
        rc, readers = self._call('SCardListReaders', context, [])[:2]
        return rc, list(readers or [])

    def listReaderGroups(self, context):
        rc, groups = self._call('SCardListReaderGroups', context)[:2]
        return rc, list(groups or [])

    def getStatusChange(self, context, timeout, states):
        """
        @param states: {list[tuple]} (reader, current state, atr)
        @return: {tuple} (rc, [(event state, atr)])
        """
        rc, result = self._call('SCardGetStatusChange', context, timeout,
                                [(name, u32(current), list(bytearray(atr))) for name, current, atr in states])[:2]
        out = []
        for i, (name, current, atr) in enumerate(states):
            if result and i < len(result):
                entry = result[i]
                out.append((u32(entry[1]), bytes(bytearray(entry[2] or []))))
            else:
                out.append((current, atr))
        return rc, out

    def connect(self, context, reader, share, protocols):
        rc, card, active = self._call('SCardConnect', context, reader, share, self.protocolsToPcsc(protocols))[:3]
        return rc, card, self.protocolsFromPcsc(active or 0)

    def reconnect(self, card, share, protocols, initialization):
        rc, active = self._call('SCardReconnect', card, share, self.protocolsToPcsc(protocols),
                                initialization)[:2]
        return rc, self.protocolsFromPcsc(active or 0)

    def disconnect(self, card, disposition):
        return self._call('SCardDisconnect', card, disposition)[0]

    def beginTransaction(self, card):
        return self._call('SCardBeginTransaction', card)[0]

    def endTransaction(self, card, disposition):
        return self._call('SCardEndTransaction', card, disposition)[0]

    def status(self, card):
        """@return: {tuple} (rc, reader, WinSCard state, WinSCard protocol, atr)"""
        result = self._call('SCardStatus', card)
        rc = result[0]
        if rc != SCARD_S_SUCCESS or len(result) < 5:
            return rc, None, SCARD_UNKNOWN, 0, b''
        reader, state, protocol, atr = result[1:5]
        return (rc, reader, self.cardStateFromPcsc(u32(state), rc), self.protocolsFromPcsc(u32(protocol)),
                bytes(bytearray(atr or [])))

    def transmit(self, card, protocol, data):
        rc, response = self._call('SCardTransmit', card, self.protocolsToPcsc(protocol), list(bytearray(data)))[:2]
        return rc, bytes(bytearray(response or []))

    def control(self, card, code, data):
        rc, response = self._call('SCardControl', card, self.controlCodeToPcsc(code), list(bytearray(data)))[:2]
        return rc, bytes(bytearray(response or []))

    def getAttrib(self, card, attrId):
        rc, value = self._call('SCardGetAttrib', card, attrId)[:2]
        return rc, bytes(bytearray(value or []))

    def setAttrib(self, card, attrId, value):
        return self._call('SCardSetAttrib', card, attrId, list(bytearray(value)))[0]



# ---------------------------------------------------------------------------
# device
# ---------------------------------------------------------------------------

class ScardOp(object):
    """@summary: a decoded smart card IOCTL"""

    def __init__(self, code, outputLength):
        self.code = code
        self.name = IOCTL_NAMES.get(code, u'0x%08x' % code)
        self.outputLength = outputLength
        self.wide = False
        self.context = None  # redirected context id (bytes)
        self.card = None  # redirected card handle id (bytes)
        self.args = {}
        self.cancelGeneration = 0


class _Context(object):
    def __init__(self, device, redirId, handle):
        self.id = redirId
        self.handle = handle  # PC/SC context of the context
        self.closed = False
        self.cancelGeneration = 0
        self.waitPool = []
        self.waitBusy = set()
        self.waits = 0
        self.cards = set()
        tag = redirId.hex()[:8] if hasattr(redirId, 'hex') else 'ctx'
        self.queue = IrpWorker(u'rdpdr-scard-%s' % tag, device._processQueued, maxIrps=128)
        self.txQueue = IrpWorker(u'rdpdr-scard-%s-tx' % tag, device._processQueued, maxIrps=128)


class _Card(object):
    def __init__(self, redirId, context, handle, privateContext, reader, protocol, share):
        self.id = redirId
        self.context = context
        self.handle = handle
        self.privateContext = privateContext
        self.reader = reader
        self.protocol = protocol
        self.share = share
        self.lock = Lock()
        self.activeCalls = 0
        self.lastActivity = monotonic()
        self.inTransaction = False
        self.pendingBegins = 0
        self.abandonedBegins = 0


class ScardError(Exception):
    """@summary: the call fails with this SCARD_* result"""

    def __init__(self, result):
        Exception.__init__(self, u'0x%08x' % result)
        self.result = result


class SmartcardDevice(RdpdrDevice):
    """
    @summary: The redirected smart card subsystem (FreeRDP SMARTCARD_DEVICE): every
    local reader is available on the server
    """

    def __init__(self, readerFilters=None, backend=None):
        """
        @param readerFilters: {list[str]|None} only the readers whose name contains
                              one of these strings are shown (FreeRDP /smartcard:<names>)
        @param backend: {PcscBackend|None} PC/SC access (default: pyscard)
        @raise OSError: pyscard is not installed
        """
        RdpdrDevice.__init__(self, RDPDR_DTYP_SMARTCARD, u'SCARD', b'')
        self.backend = backend if backend is not None else PcscBackend()
        self.filters = [f for f in (readerFilters or []) if f]
        self._lock = RLock()
        self._contexts = {}
        self._cards = {}
        self._cache = OrderedDict()
        self._transmitCounts = {}
        self._control = IrpWorker(u'rdpdr-scard', self._processQueued)
        self._watchdog = None
        self._stopped = Event()

    # -------------------------------------------------------------------
    # RdpdrDevice interface
    # -------------------------------------------------------------------

    def irpRequest(self, irp):
        if irp.majorFunction != IRP_MJ_DEVICE_CONTROL:
            log.warning(u'smartcard: unexpected IRP major function 0x%x' % irp.majorFunction)
            irp.ioStatus = STATUS_NOT_SUPPORTED
            if irp.majorFunction in (IRP_MJ_CREATE, IRP_MJ_CLOSE):
                irp.output = bytearray(5)
            irp.complete()
            return
        try:
            op = self.decode(irp.input)
        except (ValueError, ScardError) as e:
            log.warning(u'smartcard: cannot decode IOCTL request: %s' % e)
            irp.ioStatus = STATUS_UNSUCCESSFUL
            irp.output = bytearray(4)  # OutputBufferLength 0
            irp.complete()
            return
        irp.scardOp = op
        log.debug(u'smartcard: %s' % op.name)
        if op.code in _CONTROL_IOCTLS:
            self._control.submit(irp)
            return
        if op.code in _SYNC_IOCTLS:
            self._processQueued(irp)
            return
        with self._lock:
            context = self._contexts.get(op.context)
        if context is None or context.closed:
            self._processQueued(irp)  # answers SCARD_E_INVALID_HANDLE at once
        elif op.code in _WAIT_IOCTLS:
            op.cancelGeneration = context.cancelGeneration
            self._startWait(irp, context)
        elif op.code in _TX_IOCTLS:
            context.txQueue.submit(irp)
        else:
            context.queue.submit(irp)

    def init(self):
        # channel re-initialized: the server forgot its contexts
        self._closeAll()

    def free(self):
        self._stopped.set()
        self._closeAll()
        self._control.stop()

    # -------------------------------------------------------------------
    # decoding (FreeRDP smartcard_irp_device_control_decode_request)
    # -------------------------------------------------------------------

    def decode(self, s):
        """
        @param s: {StreamReader} positioned on the DR_CONTROL_REQ fields
        @return: {ScardOp}
        @raise ValueError: malformed request
        """
        outputLength, inputLength, code = s.u32(), s.u32(), s.u32()
        s.skip(20)  # Padding
        data = s.read(inputLength)
        op = ScardOp(code, outputLength)
        if code == SCARD_IOCTL_ACCESSSTARTEDEVENT:
            return op
        if code == SCARD_IOCTL_RELEASETARTEDEVENT or code not in IOCTL_NAMES:
            # [MS-RDPESC] 3.1.4: RELEASETARTEDEVENT is not used (FreeRDP: decode failure)
            raise ValueError(u'unsupported IOCTL %s' % op.name)
        r = NdrReader(data)
        version, endianness, length, filler = unpack('<BBHI', r.read(8))
        if version != 1 or endianness != 0x10 or length != 8 or filler != 0xCCCCCCCC:
            raise ValueError(u'invalid CommonTypeHeader')
        objectLength, filler = r.u32(), r.u32()
        if filler != 0 or objectLength > r.s.remaining():
            raise ValueError(u'invalid PrivateTypeHeader')
        self._decodeCall(op, r)
        return op

    def _decodeCall(self, op, r):
        code, args = op.code, op.args
        wide = op.wide = code in (
            SCARD_IOCTL_LISTREADERGROUPSW, SCARD_IOCTL_LISTREADERSW, SCARD_IOCTL_LOCATECARDSW,
            SCARD_IOCTL_GETSTATUSCHANGEW, SCARD_IOCTL_CONNECTW, SCARD_IOCTL_STATUSW,
            SCARD_IOCTL_LOCATECARDSBYATRW, SCARD_IOCTL_READCACHEW, SCARD_IOCTL_WRITECACHEW,
            SCARD_IOCTL_GETREADERICON, SCARD_IOCTL_GETDEVICETYPEID, SCARD_IOCTL_INTRODUCEREADERGROUPW,
            SCARD_IOCTL_FORGETREADERGROUPW, SCARD_IOCTL_INTRODUCEREADERW, SCARD_IOCTL_FORGETREADERW,
            SCARD_IOCTL_ADDREADERTOGROUPW, SCARD_IOCTL_REMOVEREADERFROMGROUPW)

        if code == SCARD_IOCTL_ESTABLISHCONTEXT:
            args['scope'] = r.u32()
        elif code in (SCARD_IOCTL_RELEASECONTEXT, SCARD_IOCTL_ISVALIDCONTEXT, SCARD_IOCTL_CANCEL):
            ctx = r.context()
            op.context = r.ref(ctx)
        elif code in (SCARD_IOCTL_LISTREADERGROUPSA, SCARD_IOCTL_LISTREADERGROUPSW):
            ctx = r.context()
            args['groupsIsNull'], args['cchGroups'] = r.i32(), r.u32()
            op.context = r.ref(ctx)
        elif code in (SCARD_IOCTL_LISTREADERSA, SCARD_IOCTL_LISTREADERSW):
            ctx = r.context()
            cBytes = r.u32()
            if cBytes > MAX_STRING or (wide and cBytes % 2):
                raise ValueError(u'ListReaders cBytes %d' % cBytes)
            groups = r.pointer()
            args['readersIsNull'], args['cchReaders'] = r.i32(), r.u32()
            op.context = r.ref(ctx)
            args['groups'] = decodeMultiString(r.array(1, cBytes), wide) if groups else []
        elif code in (SCARD_IOCTL_GETSTATUSCHANGEA, SCARD_IOCTL_GETSTATUSCHANGEW):
            ctx = r.context()
            args['timeout'], cReaders = r.u32(), r.u32()
            if cReaders > MAX_READERS:
                raise ValueError(u'GetStatusChange cReaders %d > %d' % (cReaders, MAX_READERS))
            states = r.pointer()
            op.context = r.ref(ctx)
            if not states:
                raise ScardError(SCARD_E_UNEXPECTED)
            args['states'] = r.readerStates(cReaders, wide)
        elif code in (SCARD_IOCTL_CONNECTA, SCARD_IOCTL_CONNECTW):
            r.pointer()  # szReader
            ctx = r.context()
            args['share'], args['protocols'] = r.u32(), r.u32()
            args['reader'] = r.string(wide)
            op.context = r.ref(ctx)
        elif code == SCARD_IOCTL_RECONNECT:
            ctx, card = r.context(), r.handle()
            args['share'], args['protocols'], args['initialization'] = r.u32(), r.u32(), r.u32()
            op.context, op.card = r.ref(ctx), r.ref(card)
        elif code in (SCARD_IOCTL_DISCONNECT, SCARD_IOCTL_BEGINTRANSACTION, SCARD_IOCTL_ENDTRANSACTION):
            ctx, card = r.context(), r.handle()
            args['disposition'] = r.u32()
            op.context, op.card = r.ref(ctx), r.ref(card)
        elif code == SCARD_IOCTL_STATE:
            ctx, card = r.context(), r.handle()
            args['atrIsNull'], args['cbAtrLen'] = r.i32(), r.u32()
            op.context, op.card = r.ref(ctx), r.ref(card)
        elif code in (SCARD_IOCTL_STATUSA, SCARD_IOCTL_STATUSW):
            ctx, card = r.context(), r.handle()
            args['namesIsNull'], args['cchReaderLen'], args['cbAtrLen'] = r.i32(), r.u32(), r.u32()
            op.context, op.card = r.ref(ctx), r.ref(card)
        elif code == SCARD_IOCTL_TRANSMIT:
            self._decodeTransmit(op, r)
        elif code == SCARD_IOCTL_CONTROL:
            ctx, card = r.context(), r.handle()
            args['controlCode'], cbIn = r.u32(), r.u32()
            if cbIn > MAX_BUFFER:
                raise ValueError(u'Control cbInBufferSize %d' % cbIn)
            inBuffer = r.pointer()
            args['outIsNull'], args['cbOut'] = r.i32(), r.u32()
            op.context, op.card = r.ref(ctx), r.ref(card)
            args['data'] = r.array(1, cbIn) if inBuffer else b''
        elif code == SCARD_IOCTL_GETATTRIB:
            ctx, card = r.context(), r.handle()
            args['attrId'], args['attrIsNull'], args['cbAttrLen'] = r.u32(), r.i32(), r.u32()
            op.context, op.card = r.ref(ctx), r.ref(card)
        elif code == SCARD_IOCTL_SETATTRIB:
            ctx, card = r.context(), r.handle()
            args['attrId'], cbAttrLen = r.u32(), r.u32()
            if cbAttrLen > MAX_STRING:
                raise ValueError(u'SetAttrib cbAttrLen %d' % cbAttrLen)
            present = r.pointer()
            op.context, op.card = r.ref(ctx), r.ref(card)
            args['value'] = r.array(1)[:cbAttrLen] if present else b''
        elif code in (SCARD_IOCTL_LOCATECARDSBYATRA, SCARD_IOCTL_LOCATECARDSBYATRW):
            ctx = r.context()
            cAtrs = r.u32()
            if cAtrs > 1000:
                raise ValueError(u'LocateCardsByATR cAtrs %d' % cAtrs)
            masks = r.pointer()
            cReaders = r.u32()
            if cReaders > 10:
                raise ValueError(u'LocateCardsByATR cReaders %d' % cReaders)
            states = r.pointer()
            op.context = r.ref(ctx)
            if masks != bool(cAtrs):
                raise ValueError(u'LocateCardsByATR masks / cAtrs inconsistency')
            raw = r.array(76, cAtrs) if masks else b''
            args['masks'] = []
            for i in range(cAtrs):
                cbAtr = unpack_from('<I', raw, i * 76)[0]
                if cbAtr > 36:
                    raise ValueError(u'LocateCards_ATRMask cbAtr %d > 36' % cbAtr)
                args['masks'].append((raw[i * 76 + 4:i * 76 + 4 + cbAtr], raw[i * 76 + 40:i * 76 + 40 + cbAtr]))
            args['states'] = r.readerStates(cReaders, wide) if states else []
            args['timeout'] = LOCATE_TIMEOUT_MS
        elif code in (SCARD_IOCTL_READCACHEA, SCARD_IOCTL_READCACHEW,
                      SCARD_IOCTL_WRITECACHEA, SCARD_IOCTL_WRITECACHEW):
            write = code in (SCARD_IOCTL_WRITECACHEA, SCARD_IOCTL_WRITECACHEW)
            name = r.pointer()
            ctx = r.context()
            cardId = r.pointer()
            args['freshness'] = r.u32()
            if write:
                cbData = r.u32()
                if cbData > MAX_STRING:
                    raise ValueError(u'WriteCache cbDataLen %d' % cbData)
                data = r.pointer()
            else:
                args['dataIsNull'], args['cbDataLen'] = r.i32(), r.u32()
            args['name'] = r.string(wide) if name else None
            op.context = r.ref(ctx)
            args['cardId'] = r.read(16) if cardId else None
            if write:
                args['data'] = r.array(1, cbData) if data else b''
        elif code == SCARD_IOCTL_GETTRANSMITCOUNT:
            ctx, card = r.context(), r.handle()
            op.context, op.card = r.ref(ctx), r.ref(card)
        elif code in (SCARD_IOCTL_LOCATECARDSA, SCARD_IOCTL_LOCATECARDSW) or code in _UNSUPPORTED_IOCTLS or \
                code in (SCARD_IOCTL_GETREADERICON, SCARD_IOCTL_GETDEVICETYPEID):
            ctx = r.context()
            if code in (SCARD_IOCTL_GETREADERICON, SCARD_IOCTL_GETDEVICETYPEID):
                r.pointer()
                op.context = r.ref(ctx)
                args['reader'] = r.string(True)
            else:
                # only the context is used: the call is not supported by PC/SC
                op.context = None if not ctx[0] else b'?'
                args['unsupported'] = True
        else:
            raise ValueError(u'unsupported IOCTL %s' % op.name)

    @staticmethod
    def _decodeTransmit(op, r):
        args = op.args
        ctx, card = r.context(), r.handle()
        args['sendProtocol'], cbExtra = r.u32(), r.u32()
        extra = r.pointer()
        cbSend = r.u32()
        if cbSend > MAX_BUFFER or cbExtra > 1024:
            raise ValueError(u'Transmit cbSendLength %d / cbExtraBytes %d' % (cbSend, cbExtra))
        send, recvPci = r.pointer(), r.pointer()
        args['recvIsNull'], args['cbRecvLength'] = r.i32(), r.u32()
        op.context, op.card = r.ref(ctx), r.ref(card)
        if cbExtra and not extra:
            raise ValueError(u'Transmit cbExtraBytes without pbExtraBytes')
        if extra:
            r.u32()  # Length
            r.read(cbExtra)
            r._pad(cbExtra)
        args['data'] = r.array(1, cbSend) if send else b''
        args['recvPci'] = None
        if recvPci:
            protocol, cbExtra = r.u32(), r.u32()
            if r.pointer():
                if r.u32() != cbExtra or cbExtra > 1024:
                    raise ValueError(u'Transmit pioRecvPci extra bytes')
                r.read(cbExtra)
                r._pad(cbExtra)
            args['recvPci'] = protocol

    # -------------------------------------------------------------------
    # dispatch and replies
    # -------------------------------------------------------------------

    def _processQueued(self, irp):
        op = irp.scardOp
        try:
            result, payload = self._execute(op)
        except ScardError as e:
            result, payload = e.result, self._emptyPayload(op)
        except Exception as e:
            log.error(u'smartcard: %s failed: %r' % (op.name, e))
            result, payload = SCARD_F_INTERNAL_ERROR, self._emptyPayload(op)
        if irp.isCancelled():
            irp.discard()
            return
        self._reply(irp, op, result, payload)

    @staticmethod
    def _reply(irp, op, result, payload):
        """@summary: DR_CONTROL_RSP (FreeRDP smartcard_irp_device_control_call)"""
        result = u32(result)
        body = pack('<I', result) + payload
        if op.code not in (SCARD_IOCTL_ACCESSSTARTEDEVENT, SCARD_IOCTL_RELEASETARTEDEVENT):
            body += b'\x00' * ((8 - (16 + len(body)) % 8) % 8)  # [MS-RPCE] 8 byte alignment
        outputLength = 16 + len(body)
        if outputLength > op.outputLength:
            # [MS-RDPESC] 3.2.5.2: the server retries with a larger buffer
            log.debug(u'smartcard: %s reply of %d bytes larger than %d' % (op.name, outputLength, op.outputLength))
            irp.ioStatus = STATUS_BUFFER_TOO_SMALL
            irp.output = bytearray(pack('<I', 0) + _COMMON_TYPE_HEADER +
                                   pack('<III', 0, 0, STATUS_BUFFER_TOO_SMALL))
        else:
            irp.ioStatus = result if (result & 0xC0000000) == 0xC0000000 else STATUS_SUCCESS
            irp.output = bytearray(pack('<I', outputLength) + _COMMON_TYPE_HEADER +
                                   pack('<II', len(body), 0) + body)
        irp.complete()

    def _emptyPayload(self, op):
        """@summary: the return structure of a failed call (zero lengths, null pointers)"""
        code = op.code
        w = NdrWriter()
        if code == SCARD_IOCTL_ESTABLISHCONTEXT:
            w.context(None)
        elif code in (SCARD_IOCTL_LISTREADERGROUPSA, SCARD_IOCTL_LISTREADERGROUPSW, SCARD_IOCTL_LISTREADERSA,
                      SCARD_IOCTL_LISTREADERSW, SCARD_IOCTL_GETSTATUSCHANGEA, SCARD_IOCTL_GETSTATUSCHANGEW,
                      SCARD_IOCTL_LOCATECARDSA, SCARD_IOCTL_LOCATECARDSW, SCARD_IOCTL_LOCATECARDSBYATRA,
                      SCARD_IOCTL_LOCATECARDSBYATRW, SCARD_IOCTL_CONTROL, SCARD_IOCTL_GETATTRIB,
                      SCARD_IOCTL_READCACHEA, SCARD_IOCTL_READCACHEW, SCARD_IOCTL_GETREADERICON):
            w.u32(0)
            w.pointer(False)
        elif code in (SCARD_IOCTL_CONNECTA, SCARD_IOCTL_CONNECTW):
            return self._connectReturn(op.context, None, 0)
        elif code in (SCARD_IOCTL_RECONNECT, SCARD_IOCTL_GETTRANSMITCOUNT, SCARD_IOCTL_GETDEVICETYPEID):
            w.u32(0)
        elif code == SCARD_IOCTL_STATE:
            w.u32(0), w.u32(0), w.u32(0)
            w.pointer(False)
        elif code in (SCARD_IOCTL_STATUSA, SCARD_IOCTL_STATUSW):
            return self._statusReturn(None, 0, 0, b'', op.wide)
        elif code == SCARD_IOCTL_TRANSMIT:
            w.pointer(False)
            w.u32(0)
            w.pointer(False)
        return bytes(w.buf)

    # -------------------------------------------------------------------
    # resources
    # -------------------------------------------------------------------

    def _newId(self, table):
        while True:
            value = urandom(8)
            if value != b'\x00' * 8 and value not in table:
                return value

    def _context(self, op):
        with self._lock:
            context = self._contexts.get(op.context)
        if context is None or context.closed:
            raise ScardError(SCARD_E_INVALID_HANDLE)
        return context

    def _card(self, op):
        context = self._context(op)
        with self._lock:
            card = self._cards.get(op.card)
        if card is None or card.context is not context:
            raise ScardError(SCARD_E_INVALID_HANDLE)
        return context, card

    def _readerAllowed(self, name):
        return not self.filters or isPnpNotification(name) or any(f in name for f in self.filters)

    def _closeContext(self, context):
        """@summary: forget a context: cancel its waits, then free it on its own thread"""
        with self._lock:
            if context.closed:
                return
            context.closed = True
            self._contexts.pop(context.id, None)
            busy = list(context.waitBusy)
        context.cancelGeneration += 1
        for handle in [context.handle] + busy:
            self.backend.cancel(handle)
        # the queued calls answer SCARD_E_INVALID_HANDLE, then the context is freed
        context.queue.stop()
        context.txQueue.stop()
        thread = Thread(target=self._freeContext, args=(context,), name=u'rdpdr-scard-free')
        thread.daemon = True
        thread.start()

    def _freeContext(self, context):
        for worker in (context.queue, context.txQueue):
            worker.join(30)
        with self._lock:
            cards = [self._cards.pop(cid) for cid in list(context.cards) if cid in self._cards]
            context.cards.clear()
            pool, context.waitPool = context.waitPool, []
        for card in cards:
            self._releaseCard(card, SCARD_LEAVE_CARD)
        for handle in pool:
            self.backend.release(handle)
        self.backend.release(context.handle)

    def _releaseCard(self, card, disposition):
        rc = self.backend.disconnect(card.handle, disposition)
        if card.privateContext is not None:
            self.backend.release(card.privateContext)
        return rc

    def _closeAll(self):
        with self._lock:
            contexts = list(self._contexts.values())
        for context in contexts:
            self._closeContext(context)
        with self._lock:
            self._cache.clear()

    # -------------------------------------------------------------------
    # calls (FreeRDP smartcard_call.c)
    # -------------------------------------------------------------------

    def _execute(self, op):
        code = op.code
        if code == SCARD_IOCTL_ACCESSSTARTEDEVENT:
            return SCARD_S_SUCCESS, b''
        if code == SCARD_IOCTL_ESTABLISHCONTEXT:
            return self._establishContext(op)
        if code == SCARD_IOCTL_RELEASECONTEXT:
            self._closeContext(self._context(op))
            return SCARD_S_SUCCESS, b''
        if code == SCARD_IOCTL_ISVALIDCONTEXT:
            return self.backend.isValid(self._context(op).handle), b''
        if code == SCARD_IOCTL_CANCEL:
            context = self._context(op)
            context.cancelGeneration += 1
            with self._lock:
                busy = list(context.waitBusy)
            rc = self.backend.cancel(context.handle)
            for handle in busy:
                self.backend.cancel(handle)
            return rc, b''
        if op.args.get('unsupported'):
            if not op.context:
                raise ScardError(SCARD_E_INVALID_HANDLE)
            raise ScardError(SCARD_E_UNSUPPORTED_FEATURE)
        if code in (SCARD_IOCTL_LISTREADERGROUPSA, SCARD_IOCTL_LISTREADERGROUPSW):
            context = self._context(op)
            rc, groups = self.backend.listReaderGroups(context.handle)
            if rc != SCARD_S_SUCCESS:
                rc, groups = SCARD_S_SUCCESS, [SCARD_DEFAULT_GROUP]
            return rc, self._multiStringReturn(groups or [SCARD_DEFAULT_GROUP], op.wide)
        if code in (SCARD_IOCTL_LISTREADERSA, SCARD_IOCTL_LISTREADERSW):
            context = self._context(op)
            rc, readers = self.backend.listReaders(context.handle)
            if rc != SCARD_S_SUCCESS:
                raise ScardError(rc)
            readers = [r for r in readers if self._readerAllowed(r)]
            if not readers:
                raise ScardError(SCARD_E_NO_READERS_AVAILABLE)
            return SCARD_S_SUCCESS, self._multiStringReturn(readers, op.wide)
        if code in (SCARD_IOCTL_CONNECTA, SCARD_IOCTL_CONNECTW):
            return self._connect(op)
        if code == SCARD_IOCTL_GETDEVICETYPEID:
            self._context(op)
            return SCARD_S_SUCCESS, pack('<I', SCARD_READER_TYPE_USB)
        if code == SCARD_IOCTL_GETREADERICON:
            self._context(op)
            raise ScardError(SCARD_E_UNSUPPORTED_FEATURE)
        if code in (SCARD_IOCTL_READCACHEA, SCARD_IOCTL_READCACHEW):
            self._context(op)
            return self._readCache(op)
        if code in (SCARD_IOCTL_WRITECACHEA, SCARD_IOCTL_WRITECACHEW):
            self._context(op)
            return self._writeCache(op), b''
        # calls on a connected card
        context, card = self._card(op)
        self._activityBegin(card, code)
        result = SCARD_F_INTERNAL_ERROR
        try:
            result, payload = self._cardCall(op, context, card)
            return result, payload
        finally:
            if self._activityEnd(card, code, result):
                log.warning(u'smartcard: transaction granted after the server ended it, released')
                self.backend.endTransaction(card.handle, SCARD_LEAVE_CARD)
                raise ScardError(SCARD_E_CANCELLED)

    def _cardCall(self, op, context, card):
        code, args = op.code, op.args
        if code == SCARD_IOCTL_RECONNECT:
            protocols = args['protocols']
            if protocols == SCARD_PROTOCOL_UNDEFINED and args['share'] != SCARD_SHARE_DIRECT:
                protocols = SCARD_PROTOCOL_Tx
            rc, active = self.backend.reconnect(card.handle, args['share'], protocols, args['initialization'])
            if rc == SCARD_S_SUCCESS:
                card.protocol = active
            return rc, pack('<I', active if rc == SCARD_S_SUCCESS else 0)
        if code == SCARD_IOCTL_DISCONNECT:
            with self._lock:
                self._cards.pop(card.id, None)
                context.cards.discard(card.id)
            return self._releaseCard(card, args['disposition']), b''
        if code == SCARD_IOCTL_BEGINTRANSACTION:
            rc = self.backend.beginTransaction(card.handle)
            if rc == SCARD_S_SUCCESS:
                self._startWatchdog()
            return rc, b''
        if code == SCARD_IOCTL_ENDTRANSACTION:
            return self.backend.endTransaction(card.handle, args['disposition']), b''
        if code in (SCARD_IOCTL_STATE, SCARD_IOCTL_STATUSA, SCARD_IOCTL_STATUSW):
            rc, reader, state, protocol, atr = self.backend.status(card.handle)
            if rc != SCARD_S_SUCCESS:
                raise ScardError(rc)
            if code == SCARD_IOCTL_STATE:
                w = NdrWriter()
                w.u32(state), w.u32(protocol), w.u32(len(atr))
                w.pointer(bool(atr))
                w.array(atr)
                return rc, bytes(w.buf)
            names = None if args['namesIsNull'] else [reader or card.reader]
            return rc, self._statusReturn(names, state, protocol, atr, op.wide)
        if code == SCARD_IOCTL_TRANSMIT:
            return self._transmit(op, card)
        if code == SCARD_IOCTL_CONTROL:
            rc, out = self.backend.control(card.handle, args['controlCode'], args['data'])
            if rc == SCARD_S_SUCCESS and len(out) > min(args['cbOut'], MAX_BUFFER):
                rc, out = SCARD_E_INSUFFICIENT_BUFFER, b''
            if rc != SCARD_S_SUCCESS:
                raise ScardError(rc)
            w = NdrWriter()
            w.u32(len(out))
            w.pointer(bool(out))
            w.array(out)
            return rc, bytes(w.buf)
        if code == SCARD_IOCTL_GETATTRIB:
            return self._getAttrib(op, card)
        if code == SCARD_IOCTL_SETATTRIB:
            return self.backend.setAttrib(card.handle, args['attrId'], args['value']), b''
        if code == SCARD_IOCTL_GETTRANSMITCOUNT:
            with self._lock:
                count = self._transmitCounts.get(card.reader, 0)
            return SCARD_S_SUCCESS, pack('<I', count)
        raise ScardError(SCARD_E_UNSUPPORTED_FEATURE)

    def _establishContext(self, op):
        rc, handle = self.backend.establish(op.args['scope'])
        if rc != SCARD_S_SUCCESS:
            log.warning(u'smartcard: SCardEstablishContext failed: 0x%08x%s' % (
                rc, u' (is the PC/SC service, pcscd, running?)' if rc == SCARD_E_NO_SERVICE else u''))
            raise ScardError(rc)
        with self._lock:
            redirId = self._newId(self._contexts)
            self._contexts[redirId] = _Context(self, redirId, handle)
        w = NdrWriter()
        w.context(redirId)
        w.ref(redirId)
        return rc, bytes(w.buf)

    @staticmethod
    def _multiStringReturn(names, wide):
        msz = encodeMultiString(names, wide)
        w = NdrWriter()
        w.u32(len(msz))
        w.pointer(True)
        w.array(msz)
        return bytes(w.buf)

    @staticmethod
    def _statusReturn(names, state, protocol, atr, wide):
        msz = encodeMultiString(names, wide) if names else b''
        w = NdrWriter()
        w.u32(len(msz))
        w.pointer(bool(msz))
        w.u32(state), w.u32(protocol)
        w.raw(bytes(atr)[:32].ljust(32, b'\x00'))
        w.u32(min(len(atr), 32))
        w.array(msz)
        return bytes(w.buf)

    @staticmethod
    def _connectReturn(contextId, cardId, protocol):
        w = NdrWriter()
        w.context(contextId)
        w.handle(cardId or b'\x00' * 8)
        w.u32(protocol)
        w.ref(contextId)
        w.ref(cardId or b'\x00' * 8)
        return bytes(w.buf)

    def _connect(self, op):
        context = self._context(op)
        args = op.args
        reader = args['reader']
        if not self._readerAllowed(reader) or isPnpNotification(reader):
            raise ScardError(SCARD_E_UNKNOWN_READER)
        protocols = args['protocols']
        if protocols == SCARD_PROTOCOL_UNDEFINED and args['share'] != SCARD_SHARE_DIRECT:
            protocols = SCARD_PROTOCOL_Tx
        # a private PC/SC context per card (winpr): pcsc-lite serializes the calls of a context
        rc, private = self.backend.establish(0)
        if rc != SCARD_S_SUCCESS:
            raise ScardError(rc)
        rc, handle, active = self.backend.connect(private, reader, args['share'], protocols)
        if rc != SCARD_S_SUCCESS:
            self.backend.release(private)
            return rc, self._connectReturn(op.context, None, 0)
        with self._lock:
            if context.closed:
                closed = True
            else:
                closed = False
                cardId = self._newId(self._cards)
                card = _Card(cardId, context, handle, private, reader, active, args['share'])
                self._cards[cardId] = card
                context.cards.add(cardId)
        if closed:
            self.backend.disconnect(handle, SCARD_LEAVE_CARD)
            self.backend.release(private)
            raise ScardError(SCARD_E_INVALID_HANDLE)
        log.info(u'smartcard: connected to \'%s\' (protocol 0x%x)' % (reader, active))
        return rc, self._connectReturn(op.context, cardId, active)

    def _transmit(self, op, card):
        args = op.args
        if args['recvIsNull']:
            raise ScardError(SCARD_E_INVALID_PARAMETER)  # pcsc-lite: no receive buffer
        protocol = args['sendProtocol'] or card.protocol
        rc, response = self.backend.transmit(card.handle, protocol, args['data'])
        if rc == SCARD_S_SUCCESS and len(response) > min(args['cbRecvLength'], MAX_BUFFER):
            rc = SCARD_E_INSUFFICIENT_BUFFER
        if rc != SCARD_S_SUCCESS:
            raise ScardError(rc)
        with self._lock:
            self._transmitCounts[card.reader] = (self._transmitCounts.get(card.reader, 0) + 1) & 0xFFFFFFFF
        w = NdrWriter()
        w.pointer(args['recvPci'] is not None)  # pioRecvPci
        w.u32(len(response))
        w.pointer(bool(response))
        if args['recvPci'] is not None:
            w.u32(protocol)  # SCardIO_Request: dwProtocol, cbExtraBytes, pbExtraBytes
            w.u32(0)
            w.pointer(False)
        w.array(response)
        return rc, bytes(w.buf)

    def _getAttrib(self, op, card):
        args = op.args
        attrId = args['attrId']
        if attrId in (SCARD_ATTR_DEVICE_FRIENDLY_NAME_A, SCARD_ATTR_DEVICE_SYSTEM_NAME_A):
            rc, value = SCARD_S_SUCCESS, card.reader.encode('utf-8') + b'\x00'
        elif attrId in (SCARD_ATTR_DEVICE_FRIENDLY_NAME_W, SCARD_ATTR_DEVICE_SYSTEM_NAME_W):
            rc, value = SCARD_S_SUCCESS, (card.reader + '\x00').encode('utf-16-le')
        else:
            rc, value = self.backend.getAttrib(card.handle, attrId)
            if rc == SCARD_S_SUCCESS and attrId == SCARD_ATTR_VENDOR_NAME and b'\x00' in value:
                value = value[:value.index(b'\x00') + 1]  # pcsc-lite adds a terminator (winpr)
            elif rc != SCARD_S_SUCCESS and attrId == SCARD_ATTR_CURRENT_PROTOCOL_TYPE:
                status = self.backend.status(card.handle)
                rc, value = status[0], pack('<I', status[3])
            elif rc != SCARD_S_SUCCESS and attrId == SCARD_ATTR_CHANNEL_ID:
                rc, value = SCARD_S_SUCCESS, pack('<I', SCARD_READER_TYPE_USB << 16)
        if rc != SCARD_S_SUCCESS:
            raise ScardError(rc)
        if not args['attrIsNull'] and args['cbAttrLen'] != SCARD_AUTOALLOCATE and len(value) > args['cbAttrLen']:
            raise ScardError(SCARD_E_INSUFFICIENT_BUFFER)
        value = value[:65536]
        w = NdrWriter()
        w.u32(len(value))
        w.pointer(bool(value))
        # FreeRDP: a null pbAttr gets the length and a zero filled buffer
        w.array(b'\x00' * len(value) if args['attrIsNull'] else value)
        return rc, bytes(w.buf)

    # SCardReadCache / SCardWriteCache, emulated (winpr smartcard_pcsc.c)

    @staticmethod
    def _cacheKey(op):
        if op.args['cardId'] is None or op.args['name'] is None:
            raise ScardError(SCARD_E_INVALID_PARAMETER)
        return op.args['cardId'], op.args['name']

    def _readCache(self, op):
        key = self._cacheKey(op)
        with self._lock:
            item = self._cache.get(key)
        if item is None:
            raise ScardError(SCARD_W_CACHE_ITEM_NOT_FOUND)
        freshness, data = item
        if freshness != op.args['freshness']:
            raise ScardError(SCARD_W_CACHE_ITEM_STALE)
        if not op.args['dataIsNull'] and op.args['cbDataLen'] != SCARD_AUTOALLOCATE \
                and len(data) > op.args['cbDataLen']:
            raise ScardError(SCARD_E_INSUFFICIENT_BUFFER)
        w = NdrWriter()
        w.u32(len(data))
        w.pointer(bool(data))
        w.array(b'\x00' * len(data) if op.args['dataIsNull'] else data)
        return SCARD_S_SUCCESS, bytes(w.buf)

    def _writeCache(self, op):
        key = self._cacheKey(op)
        with self._lock:
            self._cache.pop(key, None)
            self._cache[key] = (op.args['freshness'], op.args['data'])
            while len(self._cache) > MAX_CACHE_ITEMS:
                self._cache.popitem(last=False)
        return SCARD_S_SUCCESS

    # -------------------------------------------------------------------
    # SCardGetStatusChange / SCardLocateCardsByATR, on their own thread
    # -------------------------------------------------------------------

    def _startWait(self, irp, context):
        with self._lock:
            busy = context.waits >= MAX_WAITS
            if not busy:
                context.waits += 1
        if busy:
            log.warning(u'smartcard: too many pending status change requests')
            self._reply(irp, irp.scardOp, SCARD_E_NO_MEMORY, self._emptyPayload(irp.scardOp))
            return
        thread = Thread(target=self._waitThread, args=(irp, context), name=u'rdpdr-scard-wait')
        thread.daemon = True
        thread.start()

    def _waitThread(self, irp, context):
        op = irp.scardOp
        try:
            try:
                result, payload = self._statusChange(op, context, irp)
            except ScardError as e:
                result, payload = e.result, self._emptyPayload(op)
            except Exception as e:
                log.error(u'smartcard: %s failed: %r' % (op.name, e))
                result, payload = SCARD_F_INTERNAL_ERROR, self._emptyPayload(op)
            if irp.isCancelled():
                irp.discard()
            else:
                self._reply(irp, op, result, payload)
        finally:
            with self._lock:
                context.waits -= 1

    def _acquireWaitContext(self, context):
        with self._lock:
            if context.closed:
                raise ScardError(SCARD_E_INVALID_HANDLE)
            handle = context.waitPool.pop() if context.waitPool else None
        if handle is None:
            rc, handle = self.backend.establish(0)
            if rc != SCARD_S_SUCCESS:
                raise ScardError(rc)
        with self._lock:
            context.waitBusy.add(handle)
        return handle

    def _releaseWaitContext(self, context, handle):
        with self._lock:
            context.waitBusy.discard(handle)
            keep = not context.closed and len(context.waitPool) < 4
            if keep:
                context.waitPool.append(handle)
        if not keep:
            self.backend.release(handle)

    def _statusChange(self, op, context, irp):
        args = op.args
        states = args['states']
        if any(s['name'] is None for s in states):
            raise ScardError(SCARD_E_INVALID_PARAMETER)
        timeout = args['timeout']
        locate = op.code in (SCARD_IOCTL_LOCATECARDSBYATRA, SCARD_IOCTL_LOCATECARDSBYATRW)
        # the macOS PCSC framework has no "\\?PnP?\Notification" reader (winpr)
        query = [s for s in states if self.backend.pnpNotification or not isPnpNotification(s['name'])]
        results = dict((id(s), (s['current'] & ~0x2, s['atr'])) for s in states)  # unchanged
        rc = SCARD_S_SUCCESS
        if states:
            handle = self._acquireWaitContext(context)
            try:
                rc = self._waitLoop(handle, context, op, irp, query, results, timeout)
            finally:
                self._releaseWaitContext(context, handle)
        payload = b''
        returned = []
        for s in states:
            event, atr = results[id(s)]
            if rc == SCARD_S_SUCCESS and event & SCARD_STATE_EMPTY:
                with self._lock:
                    self._transmitCounts.pop(s['name'], None)  # card removed (winpr)
            if locate:
                for maskAtr, mask in args['masks']:
                    if len(atr) == len(maskAtr) and all(
                            (a & m) == (b & m) for a, b, m in zip(bytearray(atr), bytearray(maskAtr),
                                                                  bytearray(mask))):
                        event |= SCARD_STATE_ATRMATCH
            returned.append(readerStateReturn(s['current'], event, atr))
        if rc not in (SCARD_S_SUCCESS, SCARD_E_TIMEOUT):
            returned = []
        w = NdrWriter()
        w.u32(len(returned))
        w.pointer(bool(returned))
        w.array(b''.join(returned), len(returned))
        payload = bytes(w.buf)
        return rc, payload

    def _waitLoop(self, handle, context, op, irp, query, results, timeout):
        """@summary: wait in steps of at most 1 s to notice a cancellation (FreeRDP)"""
        start = monotonic()
        while True:
            if context.cancelGeneration != op.cancelGeneration or context.closed or self._stopped.is_set():
                return SCARD_E_CANCELLED
            if irp.isCancelled():
                return SCARD_E_CANCELLED
            if timeout == INFINITE:
                step = STATUS_CHANGE_STEP_MS
            else:
                waited = int((monotonic() - start) * 1000)
                step = max(1, min(timeout - waited, STATUS_CHANGE_STEP_MS))  # pcsc-lite: 0 = infinite
            if query:
                rc, out = self.backend.getStatusChange(handle, step, [(s['name'], s['current'], s['atr'])
                                                                      for s in query])
                for s, value in zip(query, out):
                    results[id(s)] = value
            else:
                sleep(step / 1000.0)
                rc = SCARD_E_TIMEOUT
            if rc != SCARD_E_TIMEOUT:
                return rc
            if timeout != INFINITE and (monotonic() - start) * 1000 >= timeout:
                return rc

    # -------------------------------------------------------------------
    # transactions (FreeRDP smartcard_handle_activity_* and watchdog)
    # -------------------------------------------------------------------

    @staticmethod
    def _activityBegin(card, code):
        with card.lock:
            card.activeCalls += 1
            card.lastActivity = monotonic()
            if code == SCARD_IOCTL_BEGINTRANSACTION:
                card.pendingBegins += 1
            elif code == SCARD_IOCTL_ENDTRANSACTION:
                # the server gave up waiting: release the transaction when granted
                if card.inTransaction:
                    card.inTransaction = False
                elif card.pendingBegins > card.abandonedBegins:
                    card.abandonedBegins += 1

    @staticmethod
    def _activityEnd(card, code, result):
        """@return: {bool} a transaction the server gave up on was granted"""
        abandoned = False
        with card.lock:
            card.activeCalls = max(0, card.activeCalls - 1)
            card.lastActivity = monotonic()
            if code == SCARD_IOCTL_BEGINTRANSACTION:
                card.pendingBegins = max(0, card.pendingBegins - 1)
                if card.abandonedBegins:
                    card.abandonedBegins -= 1
                    abandoned = result == SCARD_S_SUCCESS
                elif result == SCARD_S_SUCCESS:
                    card.inTransaction = True
            elif code == SCARD_IOCTL_DISCONNECT and result == SCARD_S_SUCCESS:
                card.inTransaction = False
        return abandoned

    def _startWatchdog(self):
        if TRANSACTION_TIMEOUT_MS <= 0:
            return
        with self._lock:
            if self._watchdog is not None:
                return
            self._watchdog = Thread(target=self._watchdogLoop, name=u'rdpdr-scard-watchdog')
            self._watchdog.daemon = True
            self._watchdog.start()

    def _watchdogLoop(self):
        while not self._stopped.wait(0.25):
            now = monotonic()
            with self._lock:
                cards = list(self._cards.values())
            for card in cards:
                with card.lock:
                    expired = (card.inTransaction and not card.activeCalls and
                               (now - card.lastActivity) * 1000 >= TRANSACTION_TIMEOUT_MS)
                    if expired:
                        card.inTransaction = False
                if expired:
                    log.warning(u'smartcard: transaction on \'%s\' idle for more than %d ms, ending it and '
                                u'resetting the card like Windows does' % (card.reader, TRANSACTION_TIMEOUT_MS))
                    self.backend.endTransaction(card.handle, SCARD_RESET_CARD)


def parseSmartcardSpec(spec):
    """
    @summary: Reader name filters (FreeRDP /smartcard:<str>[,<str>...]): only the
    readers whose name contains one of them are redirected; empty: every reader
    @return: {list[str]}
    """
    return [part.strip() for part in (spec or '').split(',') if part.strip() and part.strip() != '*']
