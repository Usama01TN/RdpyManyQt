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
USB redirection: Remote Desktop Protocol USB Devices Virtual Channel Extension
(MS-RDPEUSB, FreeRDP /usb).
A local USB device is handed to the server, where its Windows driver runs:
the server sends USB Request Blocks (control, bulk, interrupt and isochronous
transfers, configuration and interface selection...) and the client performs
them on the device (through libusb, see urbdrc_libusb.py).
The server opens the "URBDRC" dynamic channel (control channel): capability
exchange, then the client asks for one more channel per device (ADD_VIRTUAL_CHANNEL);
on each device channel the client describes the device (ADD_DEVICE) and the
server's requests arrive.
Ported from FreeRDP channels/urbdrc (client/urbdrc_main.c, client/data_transfer.c,
common/msusb.c and the libusb back end client/libusb/libusb_udevice.c).
Protocol reference: [MS-RDPEUSB]
"""
from __future__ import absolute_import, division, print_function
from struct import calcsize, pack as struct_pack, unpack_from
from threading import Lock, Thread
from time import time
from rdpy.core.compat import queue
from rdpy.core import log

CHANNEL_NAME = u'URBDRC'
# Interface ids (2.2.1, InterfaceId: 2 bits of stream id + 30 bits of interface)
RIM_CAPABILITY_VERSION_01 = 0x00000001
CAPABILITIES_NEGOTIATOR = 0x00000000
CLIENT_DEVICE_SINK = 0x00000001
SERVER_CHANNEL_NOTIFICATION = 0x00000002
CLIENT_CHANNEL_NOTIFICATION = 0x00000003
BASE_USBDEVICE_NUM = 0x00000005
STREAM_ID_NONE = 0x0
STREAM_ID_PROXY = 0x1
STREAM_ID_STUB = 0x2
INTERFACE_ID_MASK = 0x3FFFFFFF
# Function ids
RIMCALL_RELEASE = 0x00000001
RIMCALL_QUERYINTERFACE = 0x00000002
RIM_EXCHANGE_CAPABILITY_REQUEST = 0x00000100
CHANNEL_CREATED = 0x00000100
ADD_VIRTUAL_CHANNEL = 0x00000100
ADD_DEVICE = 0x00000101
CANCEL_REQUEST = 0x00000100
REGISTER_REQUEST_CALLBACK = 0x00000101
IO_CONTROL = 0x00000102
INTERNAL_IO_CONTROL = 0x00000103
QUERY_DEVICE_TEXT = 0x00000104
TRANSFER_IN_REQUEST = 0x00000105
TRANSFER_OUT_REQUEST = 0x00000106
RETRACT_DEVICE = 0x00000107
IOCONTROL_COMPLETION = 0x00000100
URB_COMPLETION = 0x00000101
URB_COMPLETION_NO_DATA = 0x00000102
# IO controls
IOCTL_INTERNAL_USB_SUBMIT_URB = 0x00220003
IOCTL_INTERNAL_USB_RESET_PORT = 0x00220007
IOCTL_INTERNAL_USB_GET_PORT_STATUS = 0x00220013
IOCTL_INTERNAL_USB_CYCLE_PORT = 0x0022001F
IOCTL_INTERNAL_USB_SUBMIT_IDLE_NOTIFICATION = 0x00220027
IOCTL_TSUSBGD_IOCTL_USBDI_QUERY_BUS_TIME = 0x00224000
# Device text types (2.2.6.5)
DeviceTextDescription = 0
DeviceTextLocationInformation = 1
# URB functions (2.2.9.1.1)
TS_URB_SELECT_CONFIGURATION = 0x0000
TS_URB_SELECT_INTERFACE = 0x0001
TS_URB_PIPE_REQUEST = 0x0002
TS_URB_TAKE_FRAME_LENGTH_CONTROL = 0x0003
TS_URB_RELEASE_FRAME_LENGTH_CONTROL = 0x0004
TS_URB_GET_FRAME_LENGTH = 0x0005
TS_URB_SET_FRAME_LENGTH = 0x0006
TS_URB_GET_CURRENT_FRAME_NUMBER = 0x0007
TS_URB_CONTROL_TRANSFER = 0x0008
TS_URB_BULK_OR_INTERRUPT_TRANSFER = 0x0009
TS_URB_ISOCH_TRANSFER = 0x000A
TS_URB_GET_DESCRIPTOR_FROM_DEVICE = 0x000B
TS_URB_SET_DESCRIPTOR_TO_DEVICE = 0x000C
TS_URB_SET_FEATURE_TO_DEVICE = 0x000D
TS_URB_SET_FEATURE_TO_INTERFACE = 0x000E
TS_URB_SET_FEATURE_TO_ENDPOINT = 0x000F
TS_URB_CLEAR_FEATURE_TO_DEVICE = 0x0010
TS_URB_CLEAR_FEATURE_TO_INTERFACE = 0x0011
TS_URB_CLEAR_FEATURE_TO_ENDPOINT = 0x0012
TS_URB_GET_STATUS_FROM_DEVICE = 0x0013
TS_URB_GET_STATUS_FROM_INTERFACE = 0x0014
TS_URB_GET_STATUS_FROM_ENDPOINT = 0x0015
TS_URB_VENDOR_DEVICE = 0x0017
TS_URB_VENDOR_INTERFACE = 0x0018
TS_URB_VENDOR_ENDPOINT = 0x0019
TS_URB_CLASS_DEVICE = 0x001A
TS_URB_CLASS_INTERFACE = 0x001B
TS_URB_CLASS_ENDPOINT = 0x001C
TS_URB_SYNC_RESET_PIPE_AND_CLEAR_STALL = 0x001E
TS_URB_CLASS_OTHER = 0x001F
TS_URB_VENDOR_OTHER = 0x0020
TS_URB_GET_STATUS_FROM_OTHER = 0x0021
TS_URB_CLEAR_FEATURE_TO_OTHER = 0x0022
TS_URB_SET_FEATURE_TO_OTHER = 0x0023
TS_URB_GET_DESCRIPTOR_FROM_ENDPOINT = 0x0024
TS_URB_SET_DESCRIPTOR_TO_ENDPOINT = 0x0025
TS_URB_CONTROL_GET_CONFIGURATION_REQUEST = 0x0026
TS_URB_CONTROL_GET_INTERFACE_REQUEST = 0x0027
TS_URB_GET_DESCRIPTOR_FROM_INTERFACE = 0x0028
TS_URB_SET_DESCRIPTOR_TO_INTERFACE = 0x0029
TS_URB_GET_OS_FEATURE_DESCRIPTOR_REQUEST = 0x002A
TS_URB_SYNC_RESET_PIPE = 0x0030
TS_URB_SYNC_CLEAR_STALL = 0x0031
TS_URB_CONTROL_TRANSFER_EX = 0x0032
URB_NAMES = dict((v, k[len('TS_URB_'):]) for k, v in list(globals().items()) if k.startswith('TS_URB_'))
# USBD status (2.2.10.1.1)
USBD_STATUS_SUCCESS = 0x00000000
USBD_STATUS_PENDING = 0x40000000
USBD_STATUS_CANCELED = 0xC0010000
USBD_STATUS_STALL_PID = 0xC0000004
USBD_STATUS_DEV_NOT_RESPONDING = 0xC0000005
USBD_STATUS_DATA_OVERRUN = 0xC0000008
USBD_STATUS_BUFFER_OVERRUN = 0xC000000C
USBD_STATUS_NOT_ACCESSED = 0xC000000F
USBD_STATUS_BABBLE_DETECTED = 0xC0000012
USBD_STATUS_NOT_SUPPORTED = 0xC0000E00
USBD_STATUS_TIMEOUT = 0xC0006000
USBD_STATUS_DEVICE_GONE = 0xC0007000
USBD_STATUS_NO_MEMORY = 0x80000100
USBD_STATUS_INVALID_URB_FUNCTION = 0x80000200
USBD_STATUS_INVALID_PARAMETER = 0x80000300
USBD_STATUS_REQUEST_FAILED = 0x80000500
USBD_STATUS_INVALID_PIPE_HANDLE = 0x80000600
USBD_TRANSFER_DIRECTION_IN = 0x00000001
# HRESULT
S_OK = 0x00000000
HRESULT_GEN_FAILURE = 0x8007001F  # HRESULT_FROM_WIN32(ERROR_GEN_FAILURE)
E_INVALIDARG = 0x80070057
# USB requests and request types
REQUEST_GET_STATUS = 0x00
REQUEST_CLEAR_FEATURE = 0x01
REQUEST_SET_FEATURE = 0x03
REQUEST_GET_DESCRIPTOR = 0x06
REQUEST_SET_DESCRIPTOR = 0x07
REQUEST_GET_CONFIGURATION = 0x08
REQUEST_GET_INTERFACE = 0x0A
TYPE_CLASS = 0x01 << 5
TYPE_VENDOR = 0x02 << 5
RECIPIENT_DEVICE, RECIPIENT_INTERFACE, RECIPIENT_ENDPOINT, RECIPIENT_OTHER = 0, 1, 2, 3
# endpoint transfer types
TRANSFER_CONTROL, TRANSFER_ISOCHRONOUS, TRANSFER_BULK, TRANSFER_INTERRUPT = 0, 1, 2, 3
DEVICE_SPEEDS = ('unknown', 'low', 'full', 'high', 'super', 'super+')


class UsbError(Exception):
    """@summary: A USB operation failed: usbdStatus is the USBD_STATUS to report"""

    def __init__(self, message, usbdStatus=USBD_STATUS_REQUEST_FAILED):
        Exception.__init__(self, message)
        self.usbdStatus = usbdStatus


class _Reader(object):
    def __init__(self, data, pos=0, end=None):
        self.data, self.pos = data, pos
        self.end = len(data) if end is None else end

    def need(self, n):
        if n < 0 or self.pos + n > self.end:
            raise ValueError(u'truncated message (need %d bytes at %d of %d)' % (n, self.pos, self.end))

    def unpack(self, fmt):
        size = calcsize('<' + fmt)
        self.need(size)
        values = unpack_from('<' + fmt, self.data, self.pos)
        self.pos += size
        return values

    def u8(self):
        return self.unpack('B')[0]

    def u16(self):
        return self.unpack('H')[0]

    def u32(self):
        return self.unpack('I')[0]

    def read(self, n):
        self.need(n)
        value = bytes(self.data[self.pos:self.pos + n])
        self.pos += n
        return value

    def remaining(self):
        return self.end - self.pos


def header(interfaceId, messageId, functionId):
    """@return: {bytes} SHARED_MSG_HEADER (FreeRDP create_shared_message_header_with_functionid)"""
    return struct_pack('<III', interfaceId, messageId, functionId)


# ---------------------------------------------------------------------------
# MS-RDPEUSB configuration structures (FreeRDP common/msusb.c)
# ---------------------------------------------------------------------------

class MsPipe(object):
    """TS_USBD_PIPE_INFORMATION"""

    def __init__(self, maximumPacketSize=0, maximumTransferSize=0xFFFFFFFF, pipeFlags=0):
        self.maximumPacketSize = maximumPacketSize
        self.maximumTransferSize = maximumTransferSize
        self.pipeFlags = pipeFlags
        self.pipeHandle = 0
        self.endpointAddress = 0
        self.interval = 0
        self.pipeType = 0

    def pack(self):
        return struct_pack('<HBBIIII', self.maximumPacketSize & 0xFFFF, self.endpointAddress, self.interval,
                           self.pipeType, self.pipeHandle, self.maximumTransferSize, self.pipeFlags)


class MsInterface(object):
    """TS_USBD_INTERFACE_INFORMATION"""

    def __init__(self, interfaceNumber=0, alternateSetting=0):
        self.length = 0
        self.numberOfPipesExpected = 0
        self.interfaceNumber = interfaceNumber
        self.alternateSetting = alternateSetting
        self.interfaceHandle = 0
        self.interfaceClass = self.interfaceSubClass = self.interfaceProtocol = 0
        self.pipes = []

    @classmethod
    def read(cls, r):
        length, expected, number, alternate = r.unpack('HHBB')
        r.unpack('H')  # padding
        numberOfPipes = r.u32()
        r.need(12 * numberOfPipes)
        self = cls(number, alternate)
        self.length, self.numberOfPipesExpected = length, expected
        for _ in range(numberOfPipes):
            maxPacket, _pad, maxTransfer, flags = r.unpack('HHII')
            self.pipes.append(MsPipe(maxPacket, maxTransfer, flags))
        return self

    def pack(self):
        return struct_pack('<HBBBBBBII', self.length, self.interfaceNumber, self.alternateSetting,
                           self.interfaceClass, self.interfaceSubClass, self.interfaceProtocol, 0,
                           self.interfaceHandle, len(self.pipes)) + b''.join(p.pack() for p in self.pipes)

    def size(self):
        return 16 + 20 * len(self.pipes)


class MsConfig(object):
    """MSUSB_CONFIG_DESCRIPTOR: the configuration selected by the server"""

    def __init__(self):
        self.totalLength = 0
        self.configurationValue = 0
        self.configurationHandle = 0
        self.interfaces = []
        self.complete = False

    @classmethod
    def read(cls, r, numInterfaces):
        """TS_URB_SELECT_CONFIGURATION interfaces + USB_CONFIGURATION_DESCRIPTOR (msusb_msconfig_read)"""
        self = cls()
        for _ in range(numInterfaces):
            self.interfaces.append(MsInterface.read(r))
        length, kind, self.totalLength, _num, self.configurationValue = r.unpack('BBHBB')
        if (length, kind) != (9, 2):
            raise ValueError(u'not a configuration descriptor (%d, %d)' % (length, kind))
        return self

    def pack(self):
        return struct_pack('<II', self.configurationHandle, len(self.interfaces)) + \
            b''.join(i.pack() for i in self.interfaces)

    def outSize(self):
        """MsOutSize: ConfigurationHandle + NumInterfaces + interfaces"""
        return 8 + sum(i.size() for i in self.interfaces)


# ---------------------------------------------------------------------------
# What the back end knows about a device
# ---------------------------------------------------------------------------

class UsbEndpointInfo(object):
    def __init__(self, address, attributes, maxPacketSize, interval):
        self.address, self.attributes, self.maxPacketSize, self.interval = address, attributes, maxPacketSize, interval

    def transferType(self):
        return self.attributes & 0x03

    def effectiveMaxPacketSize(self):
        """wMaxPacketSize with the high bandwidth multiplier (libusb_udev_complete_msconfig_setup)"""
        size = self.maxPacketSize & 0x07FF
        if self.transferType() in (TRANSFER_ISOCHRONOUS, TRANSFER_INTERRUPT):
            size *= 1 + ((self.maxPacketSize >> 11) & 3)
        return size


class UsbAltSetting(object):
    def __init__(self, number, alternate, cls, subClass, protocol, endpoints):
        self.number, self.alternate = number, alternate
        self.cls, self.subClass, self.protocol = cls, subClass, protocol
        self.endpoints = list(endpoints)


class UsbConfigInfo(object):
    """@summary: A configuration descriptor: interface number -> [UsbAltSetting]"""

    def __init__(self, value, altSettings):
        self.value = value
        self.interfaces = {}
        for setting in altSettings:
            self.interfaces.setdefault(setting.number, []).append(setting)

    def altSetting(self, number, alternate):
        for setting in self.interfaces.get(number, ()):
            if setting.alternate == alternate:
                return setting
        return None

    def numInterfaces(self):
        return len(self.interfaces)


class UsbDeviceBackend(object):
    """
    @summary: The local USB device (implemented by urbdrc_libusb.LibusbDevice; tests
    use a fake). Attributes: bus, address, port, vid, pid, bcdUSB, bcdDevice,
    deviceClass, deviceSubClass, deviceProtocol, speed (DEVICE_SPEEDS index),
    isComposite, path, name. Methods raise UsbError.
    """
    bus = address = port = vid = pid = bcdUSB = bcdDevice = 0
    deviceClass = deviceSubClass = deviceProtocol = 0
    speed = 0
    isComposite = False
    path = ''
    name = ''

    def activeConfig(self):
        """@return: {UsbConfigInfo}"""
        raise NotImplementedError

    def open(self):
        """@summary: Take the device: detach the local drivers"""
        raise NotImplementedError

    def close(self):
        """@summary: Give the device back to the local drivers"""
        raise NotImplementedError

    def setConfiguration(self, value):
        """@summary: Release the interfaces, set the configuration (0: unconfigured), claim them"""
        raise NotImplementedError

    def setAltSetting(self, interface, alternate):
        raise NotImplementedError

    def controlTransfer(self, requestType, request, value, index, dataOrLength, timeout):
        """
        @param dataOrLength: {bytes} OUT data, or {int} IN length
        @return: {bytes} IN data, or {int} bytes written
        """
        raise NotImplementedError

    def submitTransfer(self, kind, endpoint, dataOrLength, timeout, callback, isoPacketLengths=None):
        """
        @summary: Asynchronous bulk / interrupt / isochronous transfer
        @param callback: callable(usbdStatus, dataOrWritten, isoPackets) on the event thread;
        isoPackets: [(usbdStatus, data)] for isochronous transfers
        @return: handle with cancel()
        """
        raise NotImplementedError

    def clearHalt(self, endpoint):
        raise NotImplementedError

    def stringDescriptor(self, index, langId):
        """@return: {str|None}"""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# One redirected device
# ---------------------------------------------------------------------------

def _hexId(data):
    """sprintf "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x" of 16 bytes"""
    h = ''.join('%02x' % b for b in bytearray(data[:16].ljust(16, b'\0')))
    return '%s-%s-%s-%s-%s' % (h[0:8], h[8:12], h[12:16], h[16:20], h[20:32])


def _stringBlock(strings, multi):
    """cchString (4 bytes) + UTF-16LE strings each NUL terminated [+ NUL] (write_string_block)"""
    encoded = b''.join(s.encode('utf-16-le') + b'\0\0' for s in strings) + (b'\0\0' if multi else b'')
    return struct_pack('<I', len(encoded) // 2) + encoded


class RedirectedDevice(object):
    """
    @summary: A device given to the server (FreeRDP UDEVICE): its requests run in
    order on a worker thread, the bulk / interrupt / isochronous transfers complete
    on the back end's event thread.
    """

    def __init__(self, backend, usbDevice):
        self.backend = backend
        self.usbDevice = usbDevice  # interface id of the device on the channel
        self.channel = None  # DynamicChannel of the device
        self.requestedChannel = False
        self.completionId = 0  # REGISTER_REQUEST_CALLBACK
        self.msConfig = MsConfig()
        self.closed = False
        self._pending = {}  # RequestId -> (handle, endpoint)
        self._lock = Lock()
        self._queue = None
        self._thread = None
        self._clock = time

    def describe(self):
        return u'%04x:%04x%s (bus %d address %d)' % (self.backend.vid, self.backend.pid,
                                                    u' ' + self.backend.name if self.backend.name else u'',
                                                    self.backend.bus, self.backend.address)

    # ---- ADD_DEVICE (urdbrc_send_usb_device_add) ----

    def addDeviceMessage(self):
        b = self.backend
        hardwareIds = [u'USB\\VID_%04X&PID_%04X&REV_%04X' % (b.vid, b.pid, b.bcdDevice),
                       u'USB\\VID_%04X&PID_%04X' % (b.vid, b.pid)]
        if b.isComposite:
            compatibilityIds = [u'USB\\DevClass_00&SubClass_00&Prot_00', u'USB\\DevClass_00&SubClass_00',
                                u'USB\\DevClass_00', u'USB\\COMPOSITE']
        else:
            compatibilityIds = [u'USB\\Class_%02X&SubClass_%02X&Prot_%02X' % (b.deviceClass, b.deviceSubClass,
                                                                            b.deviceProtocol),
                                u'USB\\Class_%02X&SubClass_%02X' % (b.deviceClass, b.deviceSubClass),
                                u'USB\\Class_%02X' % b.deviceClass]
        instanceId = _hexId(('\\' + b.path).encode('ascii', 'replace'))
        containerId = '{%s}' % _hexId((u'%04X%04X%s' % (b.vid, b.pid, b.path[-8:])).encode('ascii', 'replace'))
        highSpeed = 0 if b.speed in (1, 2) else (1 if b.speed else (1 if b.bcdUSB >= 0x200 else 0))
        body = struct_pack('<II', 1, self.usbDevice)
        body += _stringBlock([instanceId], False)
        body += _stringBlock(hardwareIds, True)
        body += _stringBlock(compatibilityIds, True)
        body += _stringBlock([containerId], False)
        # USB_DEVICE_CAPABILITIES: CbSize, UsbBusInterfaceVersion, USBDI_Version,
        # Supported_USB_Version, HcdCapabilities, DeviceIsHighSpeed, NoAckIsochWriteJitterBufferSizeInMs
        body += struct_pack('<IIIIIII', 28, 2, 0x600, b.bcdUSB, 0, highSpeed, 0x50)
        return header((STREAM_ID_PROXY << 30) | CLIENT_DEVICE_SINK, 0, ADD_DEVICE) + body

    # ---- worker ----

    def start(self):
        if self._thread is not None:
            return
        self._queue = queue.Queue()
        self._thread = Thread(target=self._loop, name=u'urbdrc-%04x-%04x' % (self.backend.vid,
                                                                                       self.backend.pid))
        self._thread.daemon = True
        self._thread.start()

    def _loop(self):
        while True:
            item = self._queue.get()
            if item is None:
                break
            try:
                self.process(*item)
            except Exception as e:  # never let one request stop the device
                log.warning(u'USB %s: request failed: %r' % (self.describe(), e))

    def submit(self, messageId, functionId, r):
        """@summary: A request for the device (network thread)"""
        if self.closed:
            return
        if functionId == CANCEL_REQUEST:
            self.cancel(r.u32())  # right away: the request to cancel may block the worker
            return
        if self._thread is None:
            self.process(messageId, functionId, r)
        else:
            self._queue.put((messageId, functionId, r))

    def stop(self):
        """@summary: The device channel closed / the session ended: cancel, give the device back"""
        if self.closed:
            return
        self.closed = True
        self.cancelAll()
        if self._queue is not None:
            self._queue.put(None)
        try:
            self.backend.close()
        except Exception as e:
            log.debug(u'USB %s: close: %r' % (self.describe(), e))
        log.info(u'USB %s returned to this computer' % self.describe())

    def _write(self, data):
        channel = self.channel
        if channel is not None and not self.closed:
            channel.write(data)

    def _completionInterface(self):
        return (STREAM_ID_PROXY << 30) | (self.completionId & INTERFACE_ID_MASK)

    # ---- requests (worker thread) ----

    def process(self, messageId, functionId, r):
        if functionId == REGISTER_REQUEST_CALLBACK:
            count = r.u32()
            for _ in range(count):
                self.completionId = r.u32()
        elif functionId == IO_CONTROL:
            self._ioControl(messageId, r)
        elif functionId == INTERNAL_IO_CONTROL:
            self._internalIoControl(messageId, r)
        elif functionId == QUERY_DEVICE_TEXT:
            self._queryDeviceText(messageId, r)
        elif functionId in (TRANSFER_IN_REQUEST, TRANSFER_OUT_REQUEST):
            self._transferRequest(messageId, r, functionId == TRANSFER_IN_REQUEST)
        elif functionId == RETRACT_DEVICE:
            reason = r.u32()
            log.warning(u'USB %s: the server stops the redirection (%s)' % (
                self.describe(), u'blocked by policy' if reason == 1 else u'reason %d' % reason))
            if self.channel is not None:
                self.channel.close()
            self.stop()
        else:
            log.debug(u'USB %s: function 0x%x ignored' % (self.describe(), functionId))

    def _ioCompletion(self, messageId, requestId, output, hresult=S_OK):
        """IOCONTROL_COMPLETION (2.2.7.1)"""
        body = struct_pack('<IIII', requestId, hresult, len(output), len(output)) + output
        self._write(header(self._completionInterface(), messageId, IOCONTROL_COMPLETION) + body)

    def _ioControl(self, messageId, r):
        code, inputSize = r.unpack('II')
        r.read(inputSize)
        outputSize, requestId = r.unpack('II')
        output = b''
        if code == IOCTL_INTERNAL_USB_GET_PORT_STATUS and outputSize >= 4:
            # enabled + connected (+ powered, + high speed) (usb_process_get_port_status)
            bcd = self.backend.bcdUSB
            output = struct_pack('<I', 0x303 if bcd == 0x100 else 0x103 if bcd == 0x110 else 0x503)
        elif code not in (IOCTL_INTERNAL_USB_SUBMIT_URB, IOCTL_INTERNAL_USB_RESET_PORT,
                          IOCTL_INTERNAL_USB_CYCLE_PORT, IOCTL_INTERNAL_USB_SUBMIT_IDLE_NOTIFICATION):
            log.debug(u'USB %s: io control 0x%08x not supported' % (self.describe(), code))
        self._ioCompletion(messageId, requestId, output)

    def _internalIoControl(self, messageId, r):
        code, inputSize = r.unpack('II')
        r.read(inputSize)
        outputSize, requestId = r.unpack('II')
        if code != IOCTL_TSUSBGD_IOCTL_USBDI_QUERY_BUS_TIME or outputSize < 4:
            self._ioCompletion(messageId, requestId, b'', E_INVALIDARG)
            return
        # the bus frame number (FreeRDP: a millisecond tick)
        self._ioCompletion(messageId, requestId, struct_pack('<I', int(self._clock() * 1000) & 0xFFFFFFFF))

    def _queryDeviceText(self, messageId, r):
        textType, localeId = r.unpack('II')
        hresult, text = S_OK, None
        if textType == DeviceTextDescription:
            try:
                text = self.backend.stringDescriptor(None, localeId & 0xFFFF)
            except Exception:
                text = None
            text = (text or u'Generic Usb String')[:126]
        elif textType == DeviceTextLocationInformation:
            text = u'Port_#%04d.Hub_#%04d' % (self.backend.address, self.backend.bus)
        else:
            hresult = E_INVALIDARG
        encoded = (text + '\0').encode('utf-16-le') if text is not None else b''
        # QUERY_DEVICE_TEXT_RSP: cchDeviceDescription, DeviceDescription, HResult
        self._write(header((STREAM_ID_STUB << 30) | self.usbDevice, messageId, len(encoded) // 2) +
                    encoded + struct_pack('<I', hresult))

    # ---- URBs (urbdrc_process_transfer_request) ----

    def _transferRequest(self, messageId, r, isIn):
        cbTsUrb = r.u32()
        urb = _Reader(r.data, r.pos, r.pos + cbTsUrb)
        r.need(cbTsUrb + 4)
        r.pos += cbTsUrb
        outputSize = r.u32()
        outData = b'' if isIn else r.read(min(outputSize, r.remaining()))
        size, function, requestField = urb.unpack('HHI')
        if size != cbTsUrb:
            raise ValueError(u'TS_URB size %d != %d' % (size, cbTsUrb))
        request = _Urb(self, messageId, function, requestField, isIn, outputSize, outData)
        handler = _URB_HANDLERS.get(function)
        if handler is None:
            log.debug(u'USB %s: URB %s not supported' % (self.describe(), URB_NAMES.get(function, hex(function))))
            request.complete(USBD_STATUS_INVALID_URB_FUNCTION)
            return
        try:
            handler(self, request, urb)
        except UsbError as e:
            log.debug(u'USB %s: %s failed: %s' % (self.describe(), URB_NAMES.get(function, hex(function)), e))
            request.complete(e.usbdStatus)

    # ---- device operations ----

    def control(self, request, requestType, bRequest, value, index, timeout=2000):
        """A control transfer for the URB (libusb_udev_control_transfer)."""
        if requestType & 0x80:
            data = self.backend.controlTransfer(requestType, bRequest, value, index, request.outputSize, timeout)
            request.complete(USBD_STATUS_SUCCESS, bytes(data))
        else:
            written = self.backend.controlTransfer(requestType, bRequest, value, index, request.outData, timeout)
            request.complete(USBD_STATUS_SUCCESS, written=written)

    def endpoint(self, address):
        """@return: {UsbEndpointInfo} of the selected configuration (func_get_ep_desc)"""
        config = self.backend.activeConfig()
        for msInterface in self.msConfig.interfaces:
            setting = config.altSetting(msInterface.interfaceNumber, msInterface.alternateSetting)
            if setting is None:
                continue
            for ep in setting.endpoints:
                if ep.address == address:
                    return ep
        raise UsbError(u'endpoint 0x%02x not in the selected configuration' % address,
                       USBD_STATUS_INVALID_PIPE_HANDLE)

    def completeConfig(self, msConfig):
        """
        @summary: Pipes, handles and classes of the selected configuration from the
        device's descriptors (libusb_udev_complete_msconfig_setup)
        """
        config = self.backend.activeConfig()
        if config.numInterfaces() != len(msConfig.interfaces):
            raise UsbError(u'the configuration has %d interfaces, the server selected %d' %
                           (config.numInterfaces(), len(msConfig.interfaces)), USBD_STATUS_INVALID_PARAMETER)
        for msInterface in msConfig.interfaces:
            self._completeInterface(config, msInterface)
        msConfig.configurationHandle = (msConfig.configurationValue | (self.backend.bus << 24) |
                                        ((self.backend.address << 16) & 0xFF0000))
        msConfig.complete = True

    def _completeInterface(self, config, msInterface):
        setting = config.altSetting(msInterface.interfaceNumber, msInterface.alternateSetting)
        if setting is None:
            raise UsbError(u'interface %d alternate setting %d not found' % (
                msInterface.interfaceNumber, msInterface.alternateSetting), USBD_STATUS_INVALID_PARAMETER)
        pipes = []
        for i, ep in enumerate(setting.endpoints):
            asked = msInterface.pipes[i] if i < len(msInterface.pipes) else MsPipe()
            pipe = MsPipe(ep.effectiveMaxPacketSize(), asked.maximumTransferSize, asked.pipeFlags)
            pipe.pipeHandle = ep.address | ((self.backend.address << 16) & 0xFF0000) | \
                ((self.backend.bus << 24) & 0xFF000000)
            pipe.endpointAddress = ep.address
            pipe.interval = ep.interval
            pipe.pipeType = ep.transferType()
            pipes.append(pipe)
        msInterface.pipes = pipes
        msInterface.interfaceHandle = (setting.number | (setting.alternate << 8) |
                                       ((self.backend.address << 16) & 0xFF0000) | (self.backend.bus << 24))
        msInterface.interfaceClass, msInterface.interfaceSubClass, msInterface.interfaceProtocol = \
            setting.cls, setting.subClass, setting.protocol

    # ---- asynchronous transfers ----

    def submitAsync(self, request, kind, endpoint, dataOrLength, timeout, done, isoPacketLengths=None):
        """
        @summary: Track then submit a transfer: the completion may come before
        submitTransfer() returns, and a cancel before the transfer exists
        """
        pending = _PendingTransfer()
        with self._lock:
            self._pending[request.requestId] = (pending, endpoint)
        try:
            pending.attach(self.backend.submitTransfer(kind, endpoint, dataOrLength, timeout, done,
                                                       isoPacketLengths=isoPacketLengths))
        except Exception:
            self.untrack(request.requestId)
            raise

    def untrack(self, requestId):
        with self._lock:
            self._pending.pop(requestId, None)

    def cancel(self, requestId):
        """CANCEL_REQUEST (libusb_udev_cancel_transfer_request)"""
        with self._lock:
            entry = self._pending.get(requestId & 0x7FFFFFFF)
        if entry is not None:
            try:
                entry[0].cancel()
            except Exception as e:
                log.debug(u'USB %s: cancel %d: %r' % (self.describe(), requestId, e))

    def cancelEndpoint(self, endpoint):
        with self._lock:
            handles = [h for h, ep in self._pending.values() if ep == endpoint]
        for handle in handles:
            try:
                handle.cancel()
            except Exception:
                pass

    def cancelAll(self):
        with self._lock:
            handles = [h for h, ep in self._pending.values()]
        for handle in handles:
            try:
                handle.cancel()
            except Exception:
                pass


class _PendingTransfer(object):
    """A submitted transfer that a CANCEL_REQUEST may cancel (even before it is attached)."""

    def __init__(self):
        self._handle = None
        self._cancelled = False
        self._lock = Lock()

    def attach(self, handle):
        with self._lock:
            self._handle = handle
            cancel = self._cancelled
        if cancel:
            handle.cancel()

    def cancel(self):
        with self._lock:
            self._cancelled = True
            handle = self._handle
        if handle is not None:
            handle.cancel()


class _Urb(object):
    """One URB of a TRANSFER_IN / TRANSFER_OUT request and its completion (urb_write_completion)."""

    def __init__(self, device, messageId, function, requestField, isIn, outputSize, outData):
        self.device = device
        self.messageId = messageId
        self.function = function
        self.noAck = bool(requestField & 0x80000000)
        self.requestId = requestField & 0x7FFFFFFF
        self.isIn = isIn
        self.outputSize = outputSize
        self.outData = outData

    def _send(self, functionId, result, hresult, outputSize, data=b''):
        if self.noAck:
            return
        body = struct_pack('<II', self.requestId, len(result)) + result + struct_pack('<II', hresult, outputSize)
        self.device._write(header(self.device._completionInterface(), self.messageId, functionId) + body + data)

    @staticmethod
    def resultHeader(size, status):
        """TS_URB_RESULT_HEADER: Size, Padding, UsbdStatus"""
        return struct_pack('<HHI', size, 0, status)

    def complete(self, status, data=b'', written=None):
        """URB_COMPLETION (IN data) or URB_COMPLETION_NO_DATA, with a TS_URB_RESULT_HEADER"""
        hresult = HRESULT_GEN_FAILURE if status & 0x80000000 else S_OK
        if self.isIn and data:
            self._send(URB_COMPLETION, self.resultHeader(8, status), hresult, len(data), data)
        else:
            self._send(URB_COMPLETION_NO_DATA, self.resultHeader(8, status), hresult,
                       len(data) if written is None else written)

    def completeResult(self, result):
        """A TS_URB result with its own content (select configuration / interface, frame number)"""
        self._send(URB_COMPLETION_NO_DATA, result, S_OK, 0)


# ---- URB handlers (FreeRDP data_transfer.c) ----

def _urbSelectConfiguration(device, request, urb):
    isValid = urb.u8()
    urb.read(3)
    numInterfaces = urb.u32()
    msConfig = None
    if isValid:
        msConfig = MsConfig.read(urb, numInterfaces)
        device.backend.setConfiguration(msConfig.configurationValue)
        for msInterface in msConfig.interfaces:
            try:
                device.backend.setAltSetting(msInterface.interfaceNumber, msInterface.alternateSetting)
            except UsbError as e:
                log.debug(u'USB %s: interface %d alt %d: %s' % (device.describe(), msInterface.interfaceNumber,
                                                              msInterface.alternateSetting, e))
        device.completeConfig(msConfig)
        device.msConfig = msConfig
    else:
        device.backend.setConfiguration(0)
        device.msConfig = MsConfig()
    if request.noAck:
        return
    body = msConfig.pack() if msConfig is not None else struct_pack('<II', 0, 0)
    request.completeResult(_Urb.resultHeader(8 + len(body), USBD_STATUS_SUCCESS) + body)


def _urbSelectInterface(device, request, urb):
    urb.u32()  # ConfigurationHandle
    msInterface = MsInterface.read(urb)
    if request.outputSize != 0:
        raise UsbError(u'select interface with output data', USBD_STATUS_INVALID_PARAMETER)
    config = device.msConfig
    index = next((i for i, x in enumerate(config.interfaces)
                  if x.interfaceNumber == msInterface.interfaceNumber), None)
    if index is None:
        raise UsbError(u'interface %d not in the selected configuration' % msInterface.interfaceNumber,
                       USBD_STATUS_INVALID_PARAMETER)
    device.backend.setAltSetting(msInterface.interfaceNumber, msInterface.alternateSetting)
    device._completeInterface(device.backend.activeConfig(), msInterface)
    config.interfaces[index] = msInterface
    if request.noAck:
        return
    body = msInterface.pack()
    request.completeResult(_Urb.resultHeader(8 + len(body), USBD_STATUS_SUCCESS) + body)


def _urbPipeRequest(device, request, urb):
    """ABORT_PIPE / SYNC_RESET_PIPE(_AND_CLEAR_STALL) / SYNC_CLEAR_STALL"""
    endpoint = urb.u32() & 0xFF
    device.cancelEndpoint(endpoint)
    if request.function != TS_URB_PIPE_REQUEST:
        try:
            device.backend.clearHalt(endpoint)
        except UsbError as e:
            log.debug(u'USB %s: clear halt 0x%02x: %s' % (device.describe(), endpoint, e))
    request.complete(USBD_STATUS_SUCCESS)


def _urbCurrentFrameNumber(device, request, urb):
    if request.noAck:
        return
    frame = int(device._clock() * 1000) & 0xFFFFFFFF
    request.completeResult(_Urb.resultHeader(12, USBD_STATUS_SUCCESS) + struct_pack('<I', frame))


def _urbControlTransfer(device, request, urb):
    _pipe, _flags = urb.unpack('II')
    timeout = urb.u32() if request.function == TS_URB_CONTROL_TRANSFER_EX else 2000
    requestType, bRequest, value, index, length = urb.unpack('BBHHH')
    if length != request.outputSize:
        raise UsbError(u'setup length %d != buffer %d' % (length, request.outputSize), USBD_STATUS_INVALID_PARAMETER)
    device.control(request, requestType, bRequest, value, index, timeout or 2000)


def _descriptorRequest(recipient):
    def handler(device, request, urb):
        index, kind, langId = urb.unpack('BBH')
        if request.isIn:
            device.control(request, 0x80 | recipient, REQUEST_GET_DESCRIPTOR, (kind << 8) | index, langId, 1000)
        else:
            device.control(request, recipient, REQUEST_SET_DESCRIPTOR, (kind << 8) | index, langId, 1000)
    return handler


def _featureRequest(recipient, bRequest):
    def handler(device, request, urb):
        selector, index = urb.unpack('HH')
        device.control(request, recipient | (0x80 if request.isIn else 0), bRequest, selector, index, 1000)
    return handler


def _statusRequest(recipient):
    def handler(device, request, urb):
        index = urb.u16()
        device.control(request, 0x80 | recipient, REQUEST_GET_STATUS, 0, index, 1000)
    return handler


def _vendorOrClassRequest(kind, recipient):
    def handler(device, request, urb):
        flags, _reserved, bRequest, value, index = urb.unpack('IBBHH')
        requestType = kind | recipient | (0x80 if flags & USBD_TRANSFER_DIRECTION_IN else 0)
        device.control(request, requestType, bRequest, value, index, 2000)
    return handler


def _urbGetConfiguration(device, request, urb):
    device.control(request, 0x80, REQUEST_GET_CONFIGURATION, 0, 0, 1000)


def _urbGetInterface(device, request, urb):
    interface = urb.u16()
    device.control(request, 0x80 | RECIPIENT_INTERFACE, REQUEST_GET_INTERFACE, 0, interface, 1000)


def _urbOsFeatureDescriptor(device, request, urb):
    """Microsoft OS descriptors (libusb_udev_os_feature_descriptor_request)"""
    recipient, interface, page = urb.unpack('BBB')
    featureIndex = urb.u16()
    recipient &= 0x1F
    try:
        # the vendor code is in the "MSFT100" string descriptor 0xEE
        msString = device.backend.controlTransfer(0x80 | recipient, REQUEST_GET_DESCRIPTOR, 0x03EE, 0, 0x12, 1000)
        if len(msString) < 17:
            raise UsbError(u'no Microsoft OS string descriptor', USBD_STATUS_STALL_PID)
        vendorCode = bytearray(msString)[16]
        data = device.backend.controlTransfer(0x80 | TYPE_VENDOR | recipient, vendorCode, (interface << 8) | page,
                                              featureIndex, request.outputSize, 1000)
        request.complete(USBD_STATUS_SUCCESS, bytes(data))
    except UsbError:
        request.complete(USBD_STATUS_STALL_PID)


def _urbBulkOrInterrupt(device, request, urb):
    pipeHandle, _flags = urb.unpack('II')
    endpoint = pipeHandle & 0xFF
    ep = device.endpoint(endpoint)
    kind = ep.transferType()
    if kind not in (TRANSFER_BULK, TRANSFER_INTERRUPT):
        raise UsbError(u'endpoint 0x%02x is not bulk / interrupt' % endpoint, USBD_STATUS_INVALID_PIPE_HANDLE)

    def done(status, result, isoPackets):
        device.untrack(request.requestId)
        if device.closed:
            return
        if request.isIn:
            request.complete(status, bytes(result or b''))
        else:
            request.complete(status, written=result or 0)

    # no timeout: like a Windows driver, the server cancels what it no longer waits for
    device.submitAsync(request, kind, endpoint, request.outputSize if request.isIn else request.outData, 0, done)


def _urbIsoch(device, request, urb):
    """TS_URB_ISOCH_TRANSFER (libusb_udev_isoch_transfer / func_iso_callback)"""
    pipeHandle, _flags, startFrame, numberOfPackets, errorCount = urb.unpack('IIIII')
    urb.read(12 * numberOfPackets)  # packet descriptors: the buffer is split evenly (FreeRDP)
    endpoint = pipeHandle & 0xFF
    if numberOfPackets == 0:
        raise UsbError(u'isochronous transfer without packets', USBD_STATUS_INVALID_PARAMETER)
    size = request.outputSize
    lengths = [size // numberOfPackets] * numberOfPackets

    def done(status, result, isoPackets):
        device.untrack(request.requestId)
        if device.closed or request.noAck:
            return
        packets, data, errors, offset = [], bytearray(), errorCount, 0
        for packetStatus, packetData in (isoPackets or []):
            packetData = packetData or b''
            if packetStatus != USBD_STATUS_SUCCESS:
                errors += 1
            elif request.isIn:
                data += packetData
            packets.append(struct_pack('<III', offset, len(packetData), packetStatus))
            if request.isIn and packetStatus == USBD_STATUS_SUCCESS:
                offset += len(packetData)
        if status != USBD_STATUS_SUCCESS:
            packets = []
        result = (_Urb.resultHeader(20 + 12 * len(packets), status) +
                  struct_pack('<III', startFrame, len(packets), errors) + b''.join(packets))
        if request.isIn and data:
            request._send(URB_COMPLETION, result, S_OK, len(data), bytes(data))
        else:
            request._send(URB_COMPLETION_NO_DATA, result, S_OK, 0 if request.isIn else size)

    device.submitAsync(request, TRANSFER_ISOCHRONOUS, endpoint, size if request.isIn else request.outData, 2000,
                       done, isoPacketLengths=lengths)


def _obsolete(device, request, urb):
    """Frame length control (obsolete since Windows 2000): nothing to do"""
    request.complete(USBD_STATUS_SUCCESS)


_URB_HANDLERS = {
    TS_URB_SELECT_CONFIGURATION: _urbSelectConfiguration,
    TS_URB_SELECT_INTERFACE: _urbSelectInterface,
    TS_URB_PIPE_REQUEST: _urbPipeRequest,
    TS_URB_TAKE_FRAME_LENGTH_CONTROL: _obsolete,
    TS_URB_RELEASE_FRAME_LENGTH_CONTROL: _obsolete,
    TS_URB_GET_FRAME_LENGTH: _obsolete,
    TS_URB_SET_FRAME_LENGTH: _obsolete,
    TS_URB_GET_CURRENT_FRAME_NUMBER: _urbCurrentFrameNumber,
    TS_URB_CONTROL_TRANSFER: _urbControlTransfer,
    TS_URB_CONTROL_TRANSFER_EX: _urbControlTransfer,
    TS_URB_BULK_OR_INTERRUPT_TRANSFER: _urbBulkOrInterrupt,
    TS_URB_ISOCH_TRANSFER: _urbIsoch,
    TS_URB_GET_DESCRIPTOR_FROM_DEVICE: _descriptorRequest(RECIPIENT_DEVICE),
    TS_URB_SET_DESCRIPTOR_TO_DEVICE: _descriptorRequest(RECIPIENT_DEVICE),
    TS_URB_GET_DESCRIPTOR_FROM_INTERFACE: _descriptorRequest(RECIPIENT_INTERFACE),
    TS_URB_SET_DESCRIPTOR_TO_INTERFACE: _descriptorRequest(RECIPIENT_INTERFACE),
    TS_URB_GET_DESCRIPTOR_FROM_ENDPOINT: _descriptorRequest(RECIPIENT_ENDPOINT),
    TS_URB_SET_DESCRIPTOR_TO_ENDPOINT: _descriptorRequest(RECIPIENT_ENDPOINT),
    TS_URB_SET_FEATURE_TO_DEVICE: _featureRequest(RECIPIENT_DEVICE, REQUEST_SET_FEATURE),
    TS_URB_SET_FEATURE_TO_INTERFACE: _featureRequest(RECIPIENT_INTERFACE, REQUEST_SET_FEATURE),
    TS_URB_SET_FEATURE_TO_ENDPOINT: _featureRequest(RECIPIENT_ENDPOINT, REQUEST_SET_FEATURE),
    TS_URB_SET_FEATURE_TO_OTHER: _featureRequest(RECIPIENT_OTHER, REQUEST_SET_FEATURE),
    TS_URB_CLEAR_FEATURE_TO_DEVICE: _featureRequest(RECIPIENT_DEVICE, REQUEST_CLEAR_FEATURE),
    TS_URB_CLEAR_FEATURE_TO_INTERFACE: _featureRequest(RECIPIENT_INTERFACE, REQUEST_CLEAR_FEATURE),
    TS_URB_CLEAR_FEATURE_TO_ENDPOINT: _featureRequest(RECIPIENT_ENDPOINT, REQUEST_CLEAR_FEATURE),
    TS_URB_CLEAR_FEATURE_TO_OTHER: _featureRequest(RECIPIENT_OTHER, REQUEST_CLEAR_FEATURE),
    TS_URB_GET_STATUS_FROM_DEVICE: _statusRequest(RECIPIENT_DEVICE),
    TS_URB_GET_STATUS_FROM_INTERFACE: _statusRequest(RECIPIENT_INTERFACE),
    TS_URB_GET_STATUS_FROM_ENDPOINT: _statusRequest(RECIPIENT_ENDPOINT),
    TS_URB_GET_STATUS_FROM_OTHER: _statusRequest(RECIPIENT_OTHER),
    TS_URB_VENDOR_DEVICE: _vendorOrClassRequest(TYPE_VENDOR, RECIPIENT_DEVICE),
    TS_URB_VENDOR_INTERFACE: _vendorOrClassRequest(TYPE_VENDOR, RECIPIENT_INTERFACE),
    TS_URB_VENDOR_ENDPOINT: _vendorOrClassRequest(TYPE_VENDOR, RECIPIENT_ENDPOINT),
    TS_URB_VENDOR_OTHER: _vendorOrClassRequest(TYPE_VENDOR, RECIPIENT_OTHER),
    TS_URB_CLASS_DEVICE: _vendorOrClassRequest(TYPE_CLASS, RECIPIENT_DEVICE),
    TS_URB_CLASS_INTERFACE: _vendorOrClassRequest(TYPE_CLASS, RECIPIENT_INTERFACE),
    TS_URB_CLASS_ENDPOINT: _vendorOrClassRequest(TYPE_CLASS, RECIPIENT_ENDPOINT),
    TS_URB_CLASS_OTHER: _vendorOrClassRequest(TYPE_CLASS, RECIPIENT_OTHER),
    TS_URB_SYNC_RESET_PIPE_AND_CLEAR_STALL: _urbPipeRequest,
    TS_URB_SYNC_RESET_PIPE: _urbPipeRequest,
    TS_URB_SYNC_CLEAR_STALL: _urbPipeRequest,
    TS_URB_CONTROL_GET_CONFIGURATION_REQUEST: _urbGetConfiguration,
    TS_URB_CONTROL_GET_INTERFACE_REQUEST: _urbGetInterface,
    TS_URB_GET_OS_FEATURE_DESCRIPTOR_REQUEST: _urbOsFeatureDescriptor,
}


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

class _UrbdrcChannel(object):
    """Handler of one URBDRC dynamic channel (FreeRDP urbdrc_on_data_received)."""

    def __init__(self, owner, channel):
        self.owner = owner
        self.channel = channel
        self.isControl = False
        self.done = False  # control: devices announced / device: ADD_DEVICE sent
        self.device = None
        self._fallback = None

    def recv(self, data):
        if len(data) < 12:
            raise ValueError(u'URBDRC message too short')
        r = _Reader(data)
        interfaceId, messageId, functionId = r.unpack('III')
        if interfaceId == CAPABILITIES_NEGOTIATOR:  # (STREAM_ID_NONE << 30) | 0
            if functionId == RIM_EXCHANGE_CAPABILITY_REQUEST:
                version = min(r.u32(), RIM_CAPABILITY_VERSION_01)
                # RIM_EXCHANGE_CAPABILITY_RESPONSE: header + CapabilityValue + Result
                self.channel.write(struct_pack('<IIII', CAPABILITIES_NEGOTIATOR, messageId, version, 0))
        elif interfaceId == (STREAM_ID_PROXY << 30) | SERVER_CHANNEL_NOTIFICATION:
            if functionId == CHANNEL_CREATED:
                r.unpack('III')  # major, minor, capabilities: version 1.0 only
                self.channel.write(header((STREAM_ID_PROXY << 30) | CLIENT_CHANNEL_NOTIFICATION, messageId,
                                          CHANNEL_CREATED) + struct_pack('<III', 1, 0, 0))
                self._armFallback()
            elif functionId == RIMCALL_RELEASE:
                self.ready()
        else:
            self.owner._deviceRequest(self, interfaceId & INTERFACE_ID_MASK, messageId, functionId, r)

    def _armFallback(self):
        """FreeRDP waits for the server's RIMCALL_RELEASE; go on anyway if it does not come"""
        if self.done or self._fallback is not None:
            return
        try:
            from twisted.internet import reactor
            self._fallback = reactor.callLater(1.0, self.ready)
        except Exception:
            self._fallback = None

    def ready(self):
        """The channel is set up: announce the devices (control) or describe one (device)."""
        if self._fallback is not None:
            try:
                if self._fallback.active():
                    self._fallback.cancel()
            except Exception:
                pass
            self._fallback = None
        if self.done:
            return
        self.done = True
        self.owner._channelReady(self)

    def closed(self):
        if self._fallback is not None:
            try:
                if self._fallback.active():
                    self._fallback.cancel()
            except Exception:
                pass
        self.owner._channelClosed(self)


class Urbdrc(object):
    """
    @summary: The USB redirection plugin (FreeRDP URBDRC_PLUGIN + udevman). Devices
    are added before connecting (addDevice), or while connected (hotplug).
    """

    def __init__(self):
        self.devices = []
        self.enabled = True
        self._control = None
        self._nextId = BASE_USBDEVICE_NUM
        self._lock = Lock()

    def addDevice(self, backend):
        """
        @param backend: {UsbDeviceBackend} opened when the server takes it
        @return: {RedirectedDevice}
        """
        with self._lock:
            device = RedirectedDevice(backend, self._nextId)
            self._nextId += 1
            self.devices.append(device)
        log.info(u'USB %s will be redirected' % device.describe())
        if self._control is not None and self._control.done:
            self._announce()  # hotplug: ask for a channel now
        return device

    def removeDevice(self, backend):
        """@summary: The device was unplugged: close its channel"""
        with self._lock:
            device = next((d for d in self.devices if d.backend is backend), None)
            if device is not None:
                self.devices.remove(device)
        if device is not None:
            if device.channel is not None:
                device.channel.close()
            device.stop()

    def channelFactory(self, channel):
        """@summary: DrdynvcLayer listener: every URBDRC channel"""
        if not self.enabled:
            return None
        handler = _UrbdrcChannel(self, channel)
        if self._control is None:
            handler.isControl = True
            self._control = handler
        return handler

    def _channelReady(self, handler):
        if handler.isControl:
            self._announce()
            return
        with self._lock:
            device = next((d for d in self.devices if d.requestedChannel and d.channel is None and not d.closed),
                          None)
            if device is not None:
                device.channel = handler.channel
                handler.device = device
        if device is None:
            log.debug(u'URBDRC: device channel without a device')
            return
        try:
            device.backend.open()
        except Exception as e:
            log.error(u'USB %s can not be redirected: %s' % (device.describe(), e))
            handler.channel.close()
            return
        device.start()
        handler.channel.write(device.addDeviceMessage())
        log.info(u'USB %s redirected to the server' % device.describe())

    def _announce(self):
        """ADD_VIRTUAL_CHANNEL for each device without a channel (urbdrc_announce_devices)"""
        control = self._control
        if control is None:
            return
        with self._lock:
            pending = [d for d in self.devices if not d.requestedChannel and not d.closed]
            for device in pending:
                device.requestedChannel = True
        for device in pending:
            control.channel.write(header((STREAM_ID_PROXY << 30) | CLIENT_DEVICE_SINK, device.usbDevice,
                                         ADD_VIRTUAL_CHANNEL))

    def _deviceRequest(self, handler, usbDevice, messageId, functionId, r):
        """A request for a device, whatever channel it came on (answers go to the device's channel)"""
        device = next((d for d in self.devices if d.usbDevice == usbDevice), None)
        if device is None or device.closed or device.channel is None:
            return  # unplugged / not announced yet: ignored (FreeRDP)
        device.submit(messageId, functionId, r)

    def _channelClosed(self, handler):
        if handler is self._control:
            self._control = None
        device = handler.device
        if device is not None:
            device.channel = None
            device.stop()
            with self._lock:
                if device in self.devices:
                    self.devices.remove(device)

    def shutdown(self):
        """@summary: The session ended: every device back to this computer"""
        with self._lock:
            devices, self.devices = list(self.devices), []
        for device in devices:
            device.stop()
        self._control = None

    def describe(self):
        return [d.describe() for d in self.devices]


# ---------------------------------------------------------------------------
# Device selection (FreeRDP /usb:id:VVVV:PPPP, /usb:addr:BUS:DEV, /usb:auto)
# ---------------------------------------------------------------------------

def _number(text, hexadecimal=False):
    text = text.strip().lower()
    if text.startswith('0x'):
        return int(text, 16)
    if hexadecimal or any(c in 'abcdef' for c in text):
        return int(text, 16)
    return int(text, 10)


def parseUsbSpec(spec):
    """
    @summary: A --usb argument
        "VVVV:PPPP", "id:VVVV:PPPP[#VVVV:PPPP...]"   vendor and product ids (hexadecimal)
        "addr:BUS:ADDRESS[#BUS:ADDRESS...]"          bus and device numbers (as lsusb prints them)
        "auto"                                       devices plugged in during the session
    @return: {list} [('id', vid, pid) | ('addr', bus, address) | ('auto',)]
    @raise ValueError: invalid description
    """
    spec = (spec or '').strip()
    if not spec:
        raise ValueError(u'USB: empty device description')
    if spec.lower() == 'auto':
        return [('auto',)]
    kind = 'id'
    for prefix in ('id:', 'dev:', 'addr:'):
        if spec.lower().startswith(prefix):
            kind = 'addr' if prefix == 'addr:' else 'id'
            spec = spec[len(prefix):]
            break
    if spec.lower().startswith('dev:'):  # FreeRDP "id,dev:VVVV:PPPP"
        spec = spec[4:]
    out = []
    for item in spec.split('#'):
        parts = item.split(':')
        if len(parts) != 2:
            raise ValueError(u'USB: expected %s, got \'%s\'' % ('BUS:ADDRESS' if kind == 'addr' else 'VVVV:PPPP', item))
        try:
            if kind == 'id':
                a, b = _number(parts[0], True), _number(parts[1], True)
                if not (0 <= a <= 0xFFFF and 0 <= b <= 0xFFFF):
                    raise ValueError()
            else:
                a, b = _number(parts[0]), _number(parts[1])
                if not (0 <= a <= 0xFF and 0 <= b <= 0xFF):
                    raise ValueError()
        except ValueError:
            raise ValueError(u'USB: invalid numbers in \'%s\'' % item)
        out.append((kind, a, b))
    return out


def matches(spec, backend):
    """@return: {bool} the device matches one parsed spec entry"""
    if spec[0] == 'id':
        return (backend.vid, backend.pid) == (spec[1], spec[2])
    if spec[0] == 'addr':
        return (backend.bus, backend.address) == (spec[1], spec[2])
    return spec[0] == 'auto'
