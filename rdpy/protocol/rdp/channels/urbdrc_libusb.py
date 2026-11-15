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
USB devices for the USB redirection (urbdrc.py) through libusb, with the
python-libusb1 binding (pip install "rdpymanyqt[usb]").
Ported from FreeRDP channels/urbdrc/client/libusb (libusb_udevice.c,
libusb_udevman.c): the local drivers are detached from the device while it is
redirected and attached again afterwards, transfers are asynchronous and
complete on an event thread, "auto" follows the devices plugged in (hotplug).
Access rights: on Linux the user needs write access to /dev/bus/usb/BBB/DDD
(a udev rule, or the plugdev group); on Windows the device needs the WinUSB
driver (Zadig); on macOS the kernel may keep some classes (mass storage, HID).
"""
from __future__ import absolute_import, division, print_function
from threading import Thread, Lock, current_thread
from rdpy.core import log
from . import urbdrc
from .urbdrc import UsbError

_usb1 = None


def _module():
    """@return: the usb1 module @raise OSError: python-libusb1 / libusb missing"""
    global _usb1
    if _usb1 is None:
        try:
            import usb1
        except:
            raise OSError('USB redirection needs python-libusb1: pip install "rdpymanyqt[usb]"')
        _usb1 = usb1
    return _usb1


def _status(error):
    """libusb error -> USBD status (FreeRDP func_set_usbd_status)"""
    usb1 = _module()
    for cls, status in ((getattr(usb1, 'USBErrorIO', None), urbdrc.USBD_STATUS_STALL_PID),
                        (getattr(usb1, 'USBErrorInvalidParam', None), urbdrc.USBD_STATUS_INVALID_PARAMETER),
                        (getattr(usb1, 'USBErrorAccess', None), urbdrc.USBD_STATUS_NOT_ACCESSED),
                        (getattr(usb1, 'USBErrorNoDevice', None), urbdrc.USBD_STATUS_DEVICE_GONE),
                        (getattr(usb1, 'USBErrorTimeout', None), urbdrc.USBD_STATUS_TIMEOUT),
                        (getattr(usb1, 'USBErrorNoMem', None), urbdrc.USBD_STATUS_NO_MEMORY),
                        (getattr(usb1, 'USBErrorNotSupported', None), urbdrc.USBD_STATUS_NOT_SUPPORTED)):
        if cls is not None and isinstance(error, cls):
            return status
    return urbdrc.USBD_STATUS_STALL_PID  # pipe, busy, overflow, interrupted, other


def _transferStatus(status):
    """libusb transfer status -> USBD status (FreeRDP func_bulk_transfer_cb)"""
    usb1 = _module()
    return {
        usb1.TRANSFER_COMPLETED: urbdrc.USBD_STATUS_SUCCESS,
        usb1.TRANSFER_ERROR: urbdrc.USBD_STATUS_CANCELED,
        usb1.TRANSFER_TIMED_OUT: urbdrc.USBD_STATUS_TIMEOUT,
        usb1.TRANSFER_CANCELLED: urbdrc.USBD_STATUS_SUCCESS,  # completed with what arrived
        usb1.TRANSFER_STALL: urbdrc.USBD_STATUS_STALL_PID,
        usb1.TRANSFER_NO_DEVICE: urbdrc.USBD_STATUS_DEVICE_GONE,
        usb1.TRANSFER_OVERFLOW: urbdrc.USBD_STATUS_BABBLE_DETECTED,
    }.get(status, urbdrc.USBD_STATUS_CANCELED)


class LibusbContext(object):
    """@summary: A libusb context and its event thread (one per process)"""
    _instance = None
    _instanceLock = Lock()

    @classmethod
    def get(cls):
        with cls._instanceLock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        usb1 = _module()
        self.context = usb1.USBContext()
        self.context.open()
        self._users = 0
        self._thread = None
        self._running = False
        self._lock = Lock()
        self._hotplug = None

    def devices(self):
        """@return: {list} LibusbDevice of every local device (hubs included)"""
        out = []
        for device in self.context.getDeviceIterator(skip_on_error=True):
            try:
                out.append(LibusbDevice(self, device))
            except Exception as e:
                log.debug(u'USB: device skipped: %r' % (e,))
        return out

    def acquire(self):
        """@summary: A device is in use: handle the transfers' events"""
        with self._lock:
            self._users += 1
            if self._thread is None:
                self._running = True
                self._thread = Thread(target=self._loop, name=u'libusb-events')
                self._thread.daemon = True
                self._thread.start()

    def release(self):
        with self._lock:
            self._users = max(0, self._users - 1)
            stop = self._users == 0 and self._hotplug is None
            if stop:
                self._running = False
                thread, self._thread = self._thread, None
        if stop and thread is not None and thread is not current_thread():
            thread.join(2)

    def _loop(self):
        while self._running:
            try:
                self.context.handleEventsTimeout(tv=0.2)
            except Exception as e:  # interrupted calls and the like
                log.debug(u'USB: event loop: %r' % (e,))

    def watchHotplug(self, arrived, left):
        """
        @summary: Devices plugged in / out from now on (FreeRDP /usb:auto)
        @param arrived: callable(LibusbDevice) on the event thread
        @param left: callable(bus, address) on the event thread
        @raise OSError: hotplug not supported here (e.g. Windows)
        """
        usb1 = _module()
        if not self.context.hasCapability(usb1.CAP_HAS_HOTPLUG):
            raise OSError(u'this libusb has no hotplug support')

        def callback(context, device, event):
            try:
                if event == usb1.HOTPLUG_EVENT_DEVICE_ARRIVED:
                    arrived(LibusbDevice(self, device))
                elif event == usb1.HOTPLUG_EVENT_DEVICE_LEFT:
                    left(device.getBusNumber(), device.getDeviceAddress())
            except Exception as e:
                log.warning(u'USB: hotplug event not handled: %r' % (e,))
            return False  # stay registered

        with self._lock:
            self._hotplug = self.context.hotplugRegisterCallback(
                callback, events=usb1.HOTPLUG_EVENT_DEVICE_ARRIVED | usb1.HOTPLUG_EVENT_DEVICE_LEFT,
                flags=usb1.HOTPLUG_NO_FLAGS)
        self.acquire()

    def stopHotplug(self):
        with self._lock:
            handle, self._hotplug = self._hotplug, None
        if handle is not None:
            try:
                self.context.hotplugDeregisterCallback(handle)
            except Exception:
                pass
            self.release()


class _Transfer(object):
    """A submitted libusb transfer (cancel() for CANCEL_REQUEST)."""

    def __init__(self, transfer):
        self._transfer = transfer

    def cancel(self):
        try:
            self._transfer.cancel()
        except Exception:
            pass  # already completed


class LibusbDevice(urbdrc.UsbDeviceBackend):
    """@summary: One local device (FreeRDP UDEVICE on libusb)"""

    def __init__(self, ctx, device):
        self._ctx = ctx
        self._device = device
        self._handle = None
        self._claimed = []
        self._config = None
        self.bus = device.getBusNumber()
        self.address = device.getDeviceAddress()
        try:
            self.port = device.getPortNumber()
        except Exception:
            self.port = 0
        self.path = u'%d-%d' % (self.bus, self.port or self.address)
        self.vid, self.pid = device.getVendorID(), device.getProductID()
        self.bcdUSB, self.bcdDevice = device.getbcdUSB(), device.getbcdDevice()
        self.deviceClass = device.getDeviceClass()
        self.deviceSubClass = device.getDeviceSubClass()
        self.deviceProtocol = device.getDeviceProtocol()
        self.isHub = self.deviceClass == 0x09
        # libusb speeds have the urbdrc.DEVICE_SPEEDS numbering (unknown, low, full, high, super, super+)
        self.speed = min(max(int(device.getDeviceSpeed()), 0), len(urbdrc.DEVICE_SPEEDS) - 1)
        configs = list(device.iterConfigurations())
        first = configs[0] if configs else None
        interfaces = list(first.iterInterfaces()) if first is not None else []
        settings = [s for i in interfaces[:1] for s in list(i.iterSettings())[:1]]
        # composite device (udev_init): several interfaces, class per interface / IAD
        self.isComposite = bool((len(configs) == 1 and len(interfaces) > 1 and self.deviceClass == 0) or
                                (self.deviceClass, self.deviceSubClass, self.deviceProtocol) == (0xEF, 0x02, 0x01))
        if settings:  # FreeRDP announces the class of the first interface
            self.deviceClass, self.deviceSubClass, self.deviceProtocol = \
                settings[0].getClass(), settings[0].getSubClass(), settings[0].getProtocol()
        self.name = ''
        try:
            self.name = device.getProduct() or ''
        except Exception:
            pass  # no access to the strings before opening

    def __repr__(self):
        return u'<LibusbDevice %04x:%04x bus %d address %d>' % (self.vid, self.pid, self.bus, self.address)

    # ---- descriptors ----

    def activeConfig(self):
        if self._config is None:
            self._config = self._readConfig()
        return self._config

    def _readConfig(self):
        value = None
        if self._handle is not None:
            try:
                value = self._handle.getConfiguration()
            except Exception:
                value = None
        configs = list(self._device.iterConfigurations())
        chosen = next((c for c in configs if c.getConfigurationValue() == value), configs[0] if configs else None)
        if chosen is None:
            return urbdrc.UsbConfigInfo(0, [])
        settings = []
        for interface in chosen.iterInterfaces():
            for s in interface.iterSettings():
                endpoints = [urbdrc.UsbEndpointInfo(e.getAddress(), e.getAttributes(), e.getMaxPacketSize(),
                                                    e.getInterval()) for e in s.iterEndpoints()]
                settings.append(urbdrc.UsbAltSetting(s.getNumber(), s.getAlternateSetting(), s.getClass(),
                                                     s.getSubClass(), s.getProtocol(), endpoints))
        return urbdrc.UsbConfigInfo(chosen.getConfigurationValue(), settings)

    # ---- ownership ----

    def open(self):
        if self._handle is not None:
            return
        usb1 = _module()
        try:
            self._handle = self._device.open()
        except usb1.USBError as e:
            hint = ''
            if isinstance(e, getattr(usb1, 'USBErrorAccess', ())):
                hint = (u' (no access: add a udev rule for %04x:%04x or use the plugdev group on Linux, '
                        u'install the WinUSB driver with Zadig on Windows)' % (self.vid, self.pid))
            raise OSError(u'cannot open USB device %04x:%04x: %s%s' % (self.vid, self.pid, e, hint))
        try:
            self._handle.setAutoDetachKernelDriver(True)
        except Exception:
            pass  # not on Windows / macOS
        if not self.name:
            try:
                self.name = self._handle.getProduct() or ''
            except Exception:
                pass
        self._config = None
        self._detachKernelDrivers()
        self._claimAll()
        self._ctx.acquire()

    def _interfaceNumbers(self):
        return sorted(self.activeConfig().interfaces)

    def _detachKernelDrivers(self):
        """libusb_udev_detach_kernel_driver: the local drivers let go of the device"""
        for number in self._interfaceNumbers():
            try:
                if self._handle.kernelDriverActive(number):
                    self._handle.detachKernelDriver(number)
            except Exception:
                pass

    def _claimAll(self):
        for number in self._interfaceNumbers():
            if number in self._claimed:
                continue
            try:
                self._handle.claimInterface(number)
                self._claimed.append(number)
            except Exception as e:
                log.debug(u'USB %04x:%04x: interface %d not claimed: %r' % (self.vid, self.pid, number, e))

    def _releaseAll(self):
        for number in list(self._claimed):
            try:
                self._handle.releaseInterface(number)
            except Exception:
                pass
        self._claimed = []

    def close(self):
        if self._handle is None:
            return
        handle = self._handle
        numbers = self._interfaceNumbers()
        self._releaseAll()
        for number in numbers:  # libusb_udev_attach_kernel_driver
            try:
                handle.attachKernelDriver(number)
            except Exception:
                pass
        self._handle = None
        try:
            handle.close()  # cancels the transfers still submitted
        finally:
            self._ctx.release()

    # ---- requests ----

    def setConfiguration(self, value):
        """libusb_udev_select_configuration"""
        self._releaseAll()
        self._config = None
        self._detachKernelDrivers()  # a driver may have bound since
        try:
            self._handle.setConfiguration(value if value else -1)
        except _module().USBError as e:
            self._claimAll()
            raise UsbError(u'set configuration %d: %s' % (value, e), _status(e))
        self._config = None
        self._claimAll()

    def setAltSetting(self, interface, alternate):
        try:
            self._handle.setInterfaceAltSetting(interface, alternate)
        except _module().USBError as e:
            raise UsbError(u'interface %d alternate setting %d: %s' % (interface, alternate, e), _status(e))

    def controlTransfer(self, requestType, request, value, index, dataOrLength, timeout):
        try:
            if requestType & 0x80:
                return self._handle.controlRead(requestType, request, value, index, int(dataOrLength), timeout)
            return self._handle.controlWrite(requestType, request, value, index, bytes(dataOrLength), timeout)
        except _module().USBError as e:
            raise UsbError(u'control transfer 0x%02x/0x%02x: %s' % (requestType, request, e), _status(e))

    def submitTransfer(self, kind, endpoint, dataOrLength, timeout, callback, isoPacketLengths=None):
        usb1 = _module()
        isIn = bool(endpoint & 0x80)
        packets = len(isoPacketLengths or ())
        transfer = self._handle.getTransfer(iso_packets=packets)
        buffer_or_len = int(dataOrLength) if isIn else bytearray(dataOrLength)

        def done(t):
            status = _transferStatus(t.getStatus())
            iso = None
            if kind == urbdrc.TRANSFER_ISOCHRONOUS:
                iso = []
                setups = t.getISOSetupList()
                buffers = t.getISOBufferList() if isIn else [b''] * len(setups)
                for setup, data in zip(setups, buffers):
                    packetStatus = _transferStatus(setup['status'])
                    iso.append((packetStatus, bytes(data[:setup['actual_length']]) if isIn else b''))
                result = None if isIn else sum(s['actual_length'] for s in setups)
            elif isIn:
                result = bytes(t.getBuffer()[:t.getActualLength()])
            else:
                result = t.getActualLength()
            try:
                callback(status, result, iso)
            except Exception as e:
                log.warning(u'USB %04x:%04x: completion failed: %r' % (self.vid, self.pid, e))

        if kind == urbdrc.TRANSFER_BULK:
            transfer.setBulk(endpoint, buffer_or_len, callback=done, timeout=timeout)
        elif kind == urbdrc.TRANSFER_INTERRUPT:
            transfer.setInterrupt(endpoint, buffer_or_len, callback=done, timeout=timeout)
        elif kind == urbdrc.TRANSFER_ISOCHRONOUS:
            transfer.setIsochronous(endpoint, buffer_or_len, callback=done, timeout=timeout,
                                    iso_transfer_length_list=list(isoPacketLengths))
        else:
            raise UsbError(u'transfer type %d' % kind, urbdrc.USBD_STATUS_INVALID_PIPE_HANDLE)
        try:
            transfer.submit()
        except usb1.USBError as e:
            raise UsbError(u'submit to endpoint 0x%02x: %s' % (endpoint, e), _status(e))
        return _Transfer(transfer)

    def clearHalt(self, endpoint):
        try:
            self._handle.clearHalt(endpoint)
        except _module().USBError as e:
            raise UsbError(u'clear halt 0x%02x: %s' % (endpoint, e), _status(e))

    def stringDescriptor(self, index, langId):
        if index is None:
            index = self._device.getProductDescriptor()
        if not index or self._handle is None:
            return None
        try:
            return self._handle.getStringDescriptor(index, langId or 0x0409, errors='replace')
        except Exception:
            return self._handle.getStringDescriptor(index, 0x0409, errors='replace')


def listDevices():
    """
    @summary: Local USB devices (rdpyqt --usb-list)
    @return: {list} LibusbDevice, hubs excluded
    """
    return [d for d in LibusbContext.get().devices() if not d.isHub]


def findDevices(specs):
    """
    @param specs: urbdrc.parseUsbSpec() entries (without 'auto')
    @return: {list} LibusbDevice matching one of them
    """
    return [d for d in listDevices() if any(urbdrc.matches(s, d) for s in specs if s[0] != 'auto')]
