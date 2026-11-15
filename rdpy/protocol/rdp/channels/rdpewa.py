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
WebAuthn Virtual Channel Extension ("WebAuthN_Channel", MS-RDPEWA): sign-ins
with passkeys / security keys in the session (browsers, Windows Hello for
Business) use the FIDO2 security key plugged into this computer.
Ported from FreeRDP channels/rdpewa/client (rdpewa_main.c, rdpewa_cbor.c,
rdpewa_fido.c) and the libfido2 calls it makes: the server sends CBOR requests
{command, flags, request, timeout, transactionId, rpId}; WebAuthn requests carry
a CTAP2 command whose parameters are read and sent to the key the way FreeRDP
sets them on libfido2 (makeCredential: client data hash, rp id / name, user id /
name / display name, the first algorithm (ES256 by default), the exclude list,
rk / uv; getAssertion: rp id, client data hash, allow list, up / uv; no
extension), on a worker thread, one at a time. With several keys the user
touches the one to use (30 s). User verification: built-in UV when asked and
available, otherwise the PIN when the key has one; the PIN is asked again when
UV fails (UV_BLOCKED / UV_INVALID). The key's answer is rebuilt with the fields
FreeRDP returns (makeCredential: fmt, authData, attStmt; getAssertion:
credential, authData, signature, user id). Answers: HRESULT (UINT32 LE) then
CBOR; WebAuthn answers carry {deviceInfo, status, response = CTAP status byte +
CTAP CBOR}, and E_FAIL with status 0x01 on failure, like FreeRDP. A "Touch the
security key" notification is shown during the operation (FreeRDP's 30 s user
notification). One deliberate difference: the server's Cancel really stops the
operation (FreeRDP answers it but lets the key time out).
Security keys are reached with python-fido2 (USB HID, `pip install fido2`). On
Windows, raw access to FIDO keys needs administrator rights (a Windows rule);
on Linux the user needs access to the hidraw device (udev rules of libfido2).
"""
from __future__ import absolute_import, division, print_function
from threading import Event, Thread
from struct import error, pack
from time import time
from rdpy.core import log

CHANNEL_NAME = u'WebAuthN_Channel'
CTAPCBOR_RPC_COMMAND_WEB_AUTHN = 5
CTAPCBOR_RPC_COMMAND_IUVPAA = 6
CTAPCBOR_RPC_COMMAND_CANCEL_CUR_OP = 7
CTAPCBOR_RPC_COMMAND_API_VERSION = 8
CTAPCBOR_RPC_COMMAND_GET_CREDENTIALS = 9
CTAPCBOR_RPC_COMMAND_GET_AUTHENTICATOR_LIST = 12
CTAPCBOR_CMD_MAKE_CREDENTIAL = 0x01
CTAPCBOR_CMD_GET_ASSERTION = 0x02
S_OK = 0x00000000
E_FAIL = 0x80004005
E_NOTIMPL = 0x80004001
API_VERSION = 4  # FreeRDP: consistent with Windows 11 / Server 2025
# CTAP2 parameter keys
MC_CLIENT_DATA_HASH, MC_RP, MC_OPTIONS, MC_PIN_AUTH, MC_PIN_PROTOCOL = 1, 2, 7, 8, 9
GA_RP_ID, GA_CLIENT_DATA_HASH, GA_OPTIONS, GA_PIN_AUTH, GA_PIN_PROTOCOL = 1, 2, 5, 6, 7
UV_ERRORS = (0x3C, 0x3F)  # FreeRDP rdpewa_fido_uv_failed: FIDO_ERR_UV_BLOCKED, FIDO_ERR_UV_INVALID
COSE_ES256 = -7
SELECT_TIMEOUT = 30  # seconds to touch one of several keys (FreeRDP)
NOTIFY_TIMEOUT_MS = 30000
TOUCH_MESSAGE = u'Touch the security key'


# ---------------------------------------------------------------------------
# Minimal CBOR (RFC 8949): maps keep their order, like libcbor's definite maps
# ---------------------------------------------------------------------------

def _head(major, value):
    if value < 24:
        return pack('B', (major << 5) | value)
    for info, fmt, limit in ((24, '>B', 0xFF), (25, '>H', 0xFFFF), (26, '>I', 0xFFFFFFFF), (27, '>Q', 1 << 64)):
        if value <= limit:
            return pack('B', (major << 5) | info) + pack(fmt, value)
    raise ValueError(u'integer too large for CBOR')


def cborEncode(item):
    if isinstance(item, bool):
        return b'\xf5' if item else b'\xf4'
    if item is None:
        return b'\xf6'
    if isinstance(item, int):
        return _head(0, item) if item >= 0 else _head(1, -1 - item)
    if isinstance(item, (bytes, bytearray)):
        return _head(2, len(item)) + bytes(item)
    if isinstance(item, type(u'')):
        data = item.encode('utf-8')
        return _head(3, len(data)) + data
    if isinstance(item, (list, tuple)):
        return _head(4, len(item)) + b''.join(cborEncode(i) for i in item)
    if isinstance(item, dict):
        return _head(5, len(item)) + b''.join(cborEncode(k) + cborEncode(v) for k, v in item.items())
    raise TypeError(u'cannot CBOR-encode %r' % type(item))


def cborDecode(data, offset=0):
    """@return: (item, next offset)"""
    data = bytes(data)
    first = bytearray(data[offset:offset + 1])[0]
    major, info = first >> 5, first & 0x1F
    offset += 1
    if info < 24:
        value = info
    elif info in (24, 25, 26, 27):
        size = {24: 1, 25: 2, 26: 4, 27: 8}[info]
        value = int(''.join('%02x' % b for b in bytearray(data[offset:offset + size])), 16)
        offset += size
    elif major == 7 and info in (20, 21, 22, 23):
        value = info
    else:
        raise ValueError(u'unsupported CBOR item 0x%02x' % first)
    if major == 0:
        return value, offset
    if major == 1:
        return -1 - value, offset
    if major in (2, 3):
        raw = data[offset:offset + value]
        if len(raw) != value:
            raise ValueError(u'truncated CBOR string')
        return (raw if major == 2 else raw.decode('utf-8')), offset + value
    if major == 4:
        out = []
        for _ in range(value):
            item, offset = cborDecode(data, offset)
            out.append(item)
        return out, offset
    if major == 5:
        out = {}
        for _ in range(value):
            key, offset = cborDecode(data, offset)
            out[key], offset = cborDecode(data, offset)
        return out, offset
    if major == 6:
        return cborDecode(data, offset)  # tags: the tagged item
    if major == 7:
        return {20: False, 21: True, 22: None, 23: None}[value], offset
    raise ValueError(u'unsupported CBOR major type %d' % major)


# ---------------------------------------------------------------------------
# Requests and responses (FreeRDP rdpewa_cbor.c)
# ---------------------------------------------------------------------------

class Request(object):
    """RDPEWA_REQUEST."""

    def __init__(self):
        self.command = 0
        self.flags = 0
        self.request = b''
        self.timeout = 0
        self.transactionId = None
        self.rpId = u''


def decodeRequest(data):
    """@summary: FreeRDP rdpewa_cbor_decode_request; @raise ValueError: invalid request"""
    root, _ = cborDecode(data)
    if not isinstance(root, dict):
        raise ValueError(u'request is not a CBOR map')
    req = Request()
    for key, value in root.items():
        if key == u'rpId' and isinstance(value, type(u'')):
            req.rpId = value[:255]
        elif key == u'flags':
            if not isinstance(value, int) or value < 0:
                raise ValueError(u'flags')
            req.flags = value
        elif key == u'command':
            if not isinstance(value, int) or value < 0:
                raise ValueError(u'command')
            req.command = value
        elif key == u'request':
            if not isinstance(value, bytes):
                raise ValueError(u'request')
            req.request = value
        elif key == u'timeout':
            if not isinstance(value, int) or value < 0:
                raise ValueError(u'timeout')
            req.timeout = value
        elif key == u'transactionId':
            if not isinstance(value, bytes) or len(value) != 16:
                raise ValueError(u'transactionId')
            req.transactionId = value
    if req.command == 0:
        raise ValueError(u'no command')
    return req


def hresultResponse(hresult):
    return pack('<I', hresult & 0xFFFFFFFF)


def simpleResponse(hresult, value):
    return pack('<II', hresult & 0xFFFFFFFF, value)


class DeviceInfo(object):
    """RDPEWA_DEVICE_INFO with FreeRDP's defaults."""

    def __init__(self):
        self.maxMsgSize = 1200
        self.maxSerializedLargeBlobArray = 1024
        self.providerType = u'Hid'
        self.providerName = u'RdpyManyQtFidoProvider'
        self.devicePath = u''
        self.manufacturer = u''
        self.product = u''
        self.aaGuid = b'\x00' * 16
        self.uvStatus = 0
        self.uvRetries = 3
        self.transports = 0

    def toCbor(self):
        # the casing is the spec's (FreeRDP keeps it too)
        return {u'maxMsgSize': self.maxMsgSize, u'maxSerializedLargeBlobArray': self.maxSerializedLargeBlobArray,
                u'providerType': self.providerType, u'providerName': self.providerName,
                u'devicePath': self.devicePath, u'Manufacturer': self.manufacturer, u'Product': self.product,
                u'aaGuid': self.aaGuid, u'uvStatus': self.uvStatus, u'uvRetries': self.uvRetries,
                u'transports': self.transports}


def webauthnResponse(hresult, ctapStatus, ctapData, deviceInfo):
    """@summary: FreeRDP rdpewa_cbor_encode_webauthn_response"""
    body = {u'deviceInfo': deviceInfo.toCbor(), u'status': 0,
            u'response': pack('B', ctapStatus) + (ctapData or b'')}
    return hresultResponse(hresult) + cborEncode(body)


def failure(deviceInfo=None):
    return webauthnResponse(E_FAIL, 0x01, b'', deviceInfo or DeviceInfo())


# ---------------------------------------------------------------------------
# Security keys
# ---------------------------------------------------------------------------

class PinRequired(Exception):
    """The operation needs the PIN of the key."""


class HidBackend(object):
    """
    @summary: FIDO2 keys over USB HID with python-fido2 (FreeRDP uses libfido2).
    devices() -> [device]; info(device) -> (DeviceInfo, hasPin, hasUv);
    transact(device, cmd, args, event) -> raw CTAP response (status byte + CBOR);
    pinAuth(device, pin, permissions, rpId, clientDataHash) -> (pinUvAuthParam, protocol)
    """

    def devices(self):
        try:
            from fido2.hid import CtapHidDevice
        except:
            log.warning(u'webauthn: python-fido2 is not installed (pip install fido2)')
            return []
        try:
            return list(CtapHidDevice.list_devices())
        except Exception as e:
            log.warning(u'webauthn: cannot list the security keys: %r' % (e,))
            return []

    def info(self, device):
        from fido2.ctap2 import Ctap2
        info = DeviceInfo()
        d = getattr(device, 'descriptor', None)
        info.devicePath = u'%s' % (getattr(d, 'path', '') or '')
        info.product = u'%s' % (getattr(d, 'product_name', '') or '')
        info.manufacturer = u'%s' % (getattr(d, 'manufacturer', '') or '')
        ci = Ctap2(device).info
        options = dict(ci.options or {})
        info.maxMsgSize = int(ci.max_msg_size or 1200)
        info.maxSerializedLargeBlobArray = int(getattr(ci, 'max_large_blob', None) or 1024)
        info.aaGuid = bytes(ci.aaguid)
        info.uvStatus = 1 if options.get('uv') else 0
        for t in (getattr(ci, 'transports', None) or []):
            info.transports |= {'usb': 1, 'nfc': 2, 'ble': 4}.get(str(t), 0)
        return info, options.get('clientPin') is True, options.get('uv') is True

    def transact(self, device, cmd, args, event=None):
        from fido2.hid import CTAPHID
        return device.call(CTAPHID.CBOR, pack('B', cmd) + cborEncode(args), event)

    def pinAuth(self, device, pin, permissions, rpId, clientDataHash, event=None):
        """
        pinUvAuthParam over the client data hash (libfido2 fido_dev_get_uv_token): with the PIN,
        or, when pin is None, with the built-in user verification (CTAP 2.1 keys)
        """
        from fido2.ctap2 import Ctap2, ClientPin
        clientPin = ClientPin(Ctap2(device))
        perms = ClientPin.PERMISSION.MAKE_CREDENTIAL if permissions == CTAPCBOR_CMD_MAKE_CREDENTIAL \
            else ClientPin.PERMISSION.GET_ASSERTION
        if pin is None:
            token = clientPin.get_uv_token(perms, rpId, event=event)
        else:
            token = clientPin.get_pin_token(pin, perms, rpId)
        return clientPin.protocol.authenticate(token, clientDataHash), clientPin.protocol.VERSION

    def supportsUvToken(self, device):
        """CTAP 2.1 keys: built-in UV through a pinUvAuthToken (libfido2 fido_dev_supports_permissions)."""
        from fido2.ctap2 import Ctap2
        options = dict(Ctap2(device).info.options or {})
        return bool(options.get('pinUvAuthToken'))

    def select(self, devices, timeout, cancel=None):
        """
        Touch-to-select (FreeRDP rdpewa_fido_select_device with libfido2 touch requests): every key
        blinks, the first one touched is used.
        @return: {int} index, -1 on timeout
        """
        from fido2.ctap2 import Ctap2
        chosen = []
        stop = Event()

        def wait(index, device):
            try:
                ctap = Ctap2(device)
                if 'FIDO_2_1' in (ctap.info.versions or []) or 'FIDO_2_1_PRE' in (ctap.info.versions or []):
                    ctap.selection(event=stop)
                else:
                    # CTAP 2.0: a makeCredential with an empty pinAuth waits for a touch (libfido2)
                    try:
                        ctap.make_credential(b'\x00' * 32, {'id': '.dummy'}, {'id': b'dummy'},
                                             [{'type': 'public-key', 'alg': COSE_ES256}],
                                             pin_uv_param=b'', pin_uv_protocol=1, event=stop)
                    except Exception as e:
                        if 'KEEPALIVE_CANCEL' in str(e) or stop.is_set():
                            return
                if not stop.is_set():
                    chosen.append(index)
                    stop.set()
            except Exception:
                if not stop.is_set():
                    chosen.append(index)  # an error after the touch still means "this key"
                    stop.set()

        threads = [Thread(target=wait, args=(i, d)) for i, d in enumerate(devices)]
        for t in threads:
            t.daemon = True
            t.start()
        end = time() + timeout
        while not stop.is_set() and time() < end and not (cancel is not None and cancel.is_set()):
            stop.wait(0.2)
        stop.set()
        return chosen[0] if chosen else -1

    def listCredentials(self, device, rpId):
        """Resident credentials of a key without a PIN (FreeRDP fido_credman_get_dev_rk with no PIN):
        CTAP credential management needs a pinUvAuthParam, so keys refuse it - as with libfido2."""
        return []


def _ordered(args):
    """CTAP parameters in key order, as libfido2 builds them."""
    return dict((k, args[k]) for k in sorted(args))


def _user(user):
    out = {}
    if not isinstance(user.get(u'id'), bytes):
        raise ValueError(u'makeCredential without a user id')
    out[u'id'] = user[u'id']
    for key in (u'name', u'displayName'):
        if isinstance(user.get(key), type(u'')):
            out[key] = user[key]
    return out


def makeCredentialArgs(params):
    """
    @summary: The makeCredential parameters FreeRDP sets on libfido2 (fido_cred_set_*) and their
    libfido2 encoding; @return: (args, rpId, clientDataHash, uv)
    """
    cdh = params.get(MC_CLIENT_DATA_HASH)
    rp = params.get(MC_RP) or {}
    if not isinstance(cdh, bytes) or not isinstance(rp.get(u'id'), type(u'')):
        raise ValueError(u'makeCredential without client data hash or rp id')
    rpOut = {u'id': rp[u'id']}
    if isinstance(rp.get(u'name'), type(u'')):
        rpOut[u'name'] = rp[u'name']
    alg = COSE_ES256
    keyParams = params.get(4) or []
    if keyParams and isinstance(keyParams[0], dict) and isinstance(keyParams[0].get(u'alg'), int):
        alg = keyParams[0][u'alg']  # FreeRDP: the first algorithm of the list
    args = {MC_CLIENT_DATA_HASH: cdh, MC_RP: rpOut, 3: _user(params.get(3) or {}),
            4: [{u'alg': alg, u'type': u'public-key'}]}
    exclude = [{u'id': c[u'id'], u'type': u'public-key'} for c in (params.get(5) or [])
               if isinstance(c, dict) and isinstance(c.get(u'id'), bytes)]
    if exclude:
        args[5] = exclude
    options = params.get(MC_OPTIONS) or {}
    uv = bool(options.get(u'uv'))
    opts = {}
    if u'rk' in options:
        opts[u'rk'] = bool(options[u'rk'])
    if u'uv' in options:
        opts[u'uv'] = uv
    if opts:
        args[MC_OPTIONS] = opts
    return args, rp[u'id'], cdh, uv


def getAssertionArgs(params):
    """@summary: The getAssertion parameters FreeRDP sets (fido_assert_set_*, allow_cred)"""
    rpId = params.get(GA_RP_ID)
    cdh = params.get(GA_CLIENT_DATA_HASH)
    if not isinstance(cdh, bytes) or not isinstance(rpId, type(u'')):
        raise ValueError(u'getAssertion without client data hash or rp id')
    args = {GA_RP_ID: rpId, GA_CLIENT_DATA_HASH: cdh}
    allow = [{u'id': c[u'id'], u'type': u'public-key'} for c in (params.get(3) or [])
             if isinstance(c, dict) and isinstance(c.get(u'id'), bytes)]
    if allow:
        args[3] = allow
    options = params.get(GA_OPTIONS) or {}
    uv = bool(options.get(u'uv'))
    opts = {}
    if u'up' in options:
        opts[u'up'] = bool(options[u'up'])
    if u'uv' in options:
        opts[u'uv'] = uv
    if opts:
        args[GA_OPTIONS] = opts
    return args, rpId, cdh, uv


def makeCredentialAnswer(answer):
    """FreeRDP: {1: fmt, 2: authData (raw), 3: attStmt (raw)}."""
    if not isinstance(answer, dict) or not isinstance(answer.get(2), bytes):
        raise ValueError(u'makeCredential answer without authData')
    fmt = answer.get(1)
    return {1: fmt if isinstance(fmt, type(u'')) else u'none', 2: answer[2],
            3: answer.get(3) if isinstance(answer.get(3), dict) else {}}


def getAssertionAnswer(answer, params):
    """FreeRDP: {1: {id, type} if any, 2: authData, 3: signature, 4: {id} if a user id}."""
    if not isinstance(answer, dict) or not isinstance(answer.get(2), bytes) or not isinstance(answer.get(3), bytes):
        raise ValueError(u'getAssertion answer without authData or signature')
    out = {}
    cred = answer.get(1) if isinstance(answer.get(1), dict) else {}
    credId = cred.get(u'id')
    if not isinstance(credId, bytes):
        allow = params.get(3) or []
        credId = allow[0].get(u'id') if len(allow) == 1 and isinstance(allow[0], dict) else None
    if isinstance(credId, bytes) and credId:
        out[1] = {u'id': credId, u'type': u'public-key'}
    out[2] = answer[2]
    out[3] = answer[3]
    user = answer.get(4) if isinstance(answer.get(4), dict) else {}
    if isinstance(user.get(u'id'), bytes) and user[u'id']:
        out[4] = {u'id': user[u'id']}
    return out


class Rdpewa(object):
    """
    @summary: The WebAuthn plugin. setPinCallback(fn(prompt) -> str|None) asks the user
    for the security key PIN (any thread; the Qt client shows a dialog).
    """

    def __init__(self):
        self.enabled = False  # FreeRDP builds it on by default; rdpyqt: --webauthn
        self.backend = HidBackend()
        self._pinCallback = None
        self._notifyCallback = None
        self._worker = None
        self._cancel = None
        self.operations = 0

    def setPinCallback(self, callback):
        self._pinCallback = callback

    def setNotifyCallback(self, callback):
        """
        @param callback: callable(show, message, timeoutMs, cancel) - FreeRDP's user notification
        ("Touch the security key", 30 s); cancel is a threading.Event the UI may set
        """
        self._notifyCallback = callback

    def _notify(self, show, cancel=None):
        if self._notifyCallback is not None:
            try:
                self._notifyCallback(show, TOUCH_MESSAGE, NOTIFY_TIMEOUT_MS, cancel)
            except Exception as e:
                log.debug(u'webauthn: notification failed: %r' % (e,))

    def channelFactory(self, channel):
        if not self.enabled:
            return None
        return _RdpewaChannel(self, channel)

    # ---- commands (FreeRDP rdpewa_on_data_received) ----

    def handle(self, channel, data):
        try:
            req = decodeRequest(data)
        except (ValueError, IndexError, KeyError, error) as e:
            log.warning(u'webauthn: invalid request: %s' % e)
            return
        if req.command == CTAPCBOR_RPC_COMMAND_WEB_AUTHN:
            if self._worker is not None:
                self._worker.join()  # one operation at a time (FreeRDP)
            self._cancel = Event()
            self._worker = Thread(target=self._webauthnThread, args=(channel, req, self._cancel),
                                  name=u'webauthn')
            self._worker.daemon = True
            self._worker.start()
            return
        if req.command == CTAPCBOR_RPC_COMMAND_IUVPAA:
            response = simpleResponse(S_OK, 1 if self.backend.devices() else 0)
        elif req.command == CTAPCBOR_RPC_COMMAND_CANCEL_CUR_OP:
            if self._cancel is not None:
                self._cancel.set()
            response = hresultResponse(S_OK)
        elif req.command == CTAPCBOR_RPC_COMMAND_API_VERSION:
            response = simpleResponse(S_OK, API_VERSION)
        elif req.command == CTAPCBOR_RPC_COMMAND_GET_CREDENTIALS:
            response = self.getCredentials(req.rpId)
        elif req.command == CTAPCBOR_RPC_COMMAND_GET_AUTHENTICATOR_LIST:
            response = self.authenticatorList()
        else:
            response = hresultResponse(E_NOTIMPL)
        channel.write(response)

    def authenticatorList(self):
        """FreeRDP rdpewa_fido_get_authenticator_list (MS-RDPEWA 2.2.2.3)."""
        devices = self.backend.devices()
        if not devices:
            return hresultResponse(S_OK)
        entries = []
        for device in devices:
            try:
                name = self.backend.info(device)[0].product
            except Exception:
                name = u''
            entries.append({1: 1, 3: name or u'FIDO2 Key', 4: b'', 5: False})
        return hresultResponse(S_OK) + cborEncode(entries)

    def getCredentials(self, rpId):
        """FreeRDP rdpewa_fido_get_credentials: resident credentials of the keys without a PIN."""
        if not rpId:
            return hresultResponse(S_OK)
        devices = self.backend.devices()
        if not devices:
            return hresultResponse(S_OK)
        entries = []
        for device in devices:
            try:
                info, hasPin, hasUv = self.backend.info(device)
                if hasPin:
                    continue  # FreeRDP: no PIN prompt for a listing
                for cred in self.backend.listCredentials(device, rpId):
                    user = {u'id': cred['userId']}
                    if cred.get('userName'):
                        user[u'name'] = cred['userName']
                    entries.append({0: 4, 1: cred['id'], 2: {u'id': rpId}, 3: user})
            except Exception as e:
                log.debug(u'webauthn: credentials of a key not listed: %r' % (e,))
        return hresultResponse(S_OK) + cborEncode(entries)

    def _webauthnThread(self, channel, req, cancel):
        try:
            response = self.webauthn(req, cancel)
        except Exception as e:
            log.warning(u'webauthn: %r' % (e,))
            response = failure()
        if response is not None:
            channel.write(response)
        self.operations += 1

    def webauthn(self, req, cancel=None):
        """FreeRDP rdpewa_fido_webauthn / rdpewa_fido_make_credential / rdpewa_fido_get_assertion."""
        if len(req.request) < 1:
            return None
        cmd = bytearray(req.request[:1])[0]
        if cmd not in (CTAPCBOR_CMD_MAKE_CREDENTIAL, CTAPCBOR_CMD_GET_ASSERTION):
            return None
        try:
            params, _ = cborDecode(req.request, 1)
        except (ValueError, IndexError, KeyError, error):
            return failure()
        if not isinstance(params, dict):
            return failure()
        mc = cmd == CTAPCBOR_CMD_MAKE_CREDENTIAL
        try:
            args, rpId, cdh, uv = (makeCredentialArgs if mc else getAssertionArgs)(params)
        except ValueError as e:
            log.warning(u'webauthn: %s' % e)
            return failure()
        devices = self.backend.devices()
        if not devices:
            log.warning(u'webauthn: no security key plugged in')
            return failure()
        index = 0
        if len(devices) > 1:
            self._notify(True, cancel)
            index = self.backend.select(devices, SELECT_TIMEOUT, cancel)
            self._notify(False)
            if index < 0:
                log.warning(u'webauthn: no security key touched')
                return failure()
        device = devices[index]
        info, hasPin, hasUv = self.backend.info(device)
        useUv = uv and hasUv
        log.info(u'webauthn: %s for %s on %s' % ('registration' if mc else 'sign-in', rpId,
                                                 info.product or 'the security key'))
        pin = None
        if hasPin and not useUv:
            pin = self._askPin(rpId)
            if pin is None:
                return failure(info)
        raw = self._run(device, cmd, args, rpId, cdh, pin, useUv, cancel)
        if useUv and raw and bytearray(raw[:1])[0] in UV_ERRORS and hasPin:
            pin = self._askPin(rpId)  # FreeRDP: UV failed, the PIN then
            if pin is None:
                return failure(info)
            raw = self._run(device, cmd, args, rpId, cdh, pin, False, cancel)
        if not raw or bytearray(raw[:1])[0] != 0:
            status = bytearray(raw[:1])[0] if raw else None
            log.warning(u'webauthn: the security key refused (CTAP status %s)' % (
                '0x%02x' % status if status is not None else 'none'))
            return failure(info)
        try:
            answer, _ = cborDecode(raw, 1)
            ctap = makeCredentialAnswer(answer) if mc else getAssertionAnswer(answer, params)
        except (ValueError, IndexError, KeyError, error) as e:
            log.warning(u'webauthn: unexpected answer from the key: %s' % e)
            return failure(info)
        return webauthnResponse(S_OK, 0x00, cborEncode(ctap), info)

    def _askPin(self, rpId):
        pin = self._pinCallback(u'PIN of the security key for %s' % rpId) if self._pinCallback else None
        if not pin:
            log.warning(u'webauthn: the key needs its PIN, none given')
            return None
        return pin

    def _run(self, device, cmd, args, rpId, cdh, pin, useUv, cancel):
        """
        One libfido2 fido_dev_make_cred / fido_dev_get_assert: a pinUvAuthToken from the PIN, or
        from the built-in UV on CTAP 2.1 keys (else the "uv" option), then the command, with the
        "Touch the security key" notification while the key waits.
        """
        mc = cmd == CTAPCBOR_CMD_MAKE_CREDENTIAL
        args = dict(args)
        optionsKey = MC_OPTIONS if mc else GA_OPTIONS
        options = dict(args.get(optionsKey) or {})
        self._notify(True, cancel)
        try:
            token = None
            if pin is not None:
                token = self.backend.pinAuth(device, pin, cmd, rpId, cdh)
            elif useUv and self.backend.supportsUvToken(device):
                token = self.backend.pinAuth(device, None, cmd, rpId, cdh, cancel)
            if token is not None:
                options.pop(u'uv', None)  # libfido2: no uv option with a pinUvAuthParam
                args[MC_PIN_AUTH if mc else GA_PIN_AUTH] = token[0]
                args[MC_PIN_PROTOCOL if mc else GA_PIN_PROTOCOL] = token[1]
            if options:
                args[optionsKey] = options
            else:
                args.pop(optionsKey, None)
            return self.backend.transact(device, cmd, _ordered(args), cancel)
        except Exception as e:
            log.warning(u'webauthn: %r' % (e,))
            return None
        finally:
            self._notify(False)

    def describe(self):
        return ('%d operation(s)' % self.operations) if self.operations else None


class _RdpewaChannel(object):
    def __init__(self, plugin, channel):
        self.plugin = plugin
        self.channel = channel

    def recv(self, data):
        self.plugin.handle(self.channel, bytes(data))

    def closed(self):
        if self.plugin._cancel is not None:
            self.plugin._cancel.set()
