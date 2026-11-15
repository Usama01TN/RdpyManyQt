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
Windows Remote Assistance invitations (.msrcincident files) and expert
authentication, ported from FreeRDP libfreerdp/common/assistance.c.
An invitation holds the novice's computer addresses and either a plain
connection string 1 (RCTICKET, Windows XP style) or a connection string 2
encrypted with the invitation password (LHTICKET, Windows Vista and later).
The expert proves the password to the novice through the remdesk channel with
the PassStub encrypted by the password (RC4 keyed with MD5 of the UTF-16 password).
"""
from __future__ import absolute_import, division, print_function
from binascii import Error, hexlify, unhexlify
from re import compile, escape
from hashlib import md5, sha1
from struct import pack
from io import open

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:
    from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4
except:  # older cryptography
    ARC4 = algorithms.ARC4


class AssistanceError(ValueError):
    """The invitation can not be used (format, password...)."""


def _utf16(text):
    return (text or u'').encode('utf-16-le')


def encryptPassStub(password, passStub):
    """
    @summary: FreeRDP freerdp_assistance_encrypt_pass_stub: RC4(MD5(password UTF-16),
    UINT32 length of the UTF-16 PassStub + PassStub UTF-16)
    @return: {bytes}
    """
    stub = _utf16(passStub)
    key = md5(_utf16(password)).digest()
    plain = pack('<I', len(stub))
    encryptor = Cipher(ARC4(key), mode=None).encryptor()
    return encryptor.update(plain + stub) + encryptor.finalize()


def binToHex(data):
    """@summary: FreeRDP freerdp_assistance_bin_to_hex_string (upper case, no separator)"""
    return hexlify(bytes(data)).decode('ascii').upper()


def expertBlob(name, encryptedPassStub):
    """
    @summary: FreeRDP freerdp_assistance_construct_expert_blob:
    "<len('NAME=' + name)>;NAME=<name><len('PASS=' + pass)>;PASS=<pass>" with pass the
    upper case hex of the encrypted PassStub
    """
    password = binToHex(encryptedPassStub)
    return u'%d;NAME=%s%d;PASS=%s' % (len(name) + 5, name, len(password) + 5, password)


def _deriveKeySha1(digest, keyLength):
    """FreeRDP freerdp_assistance_crypt_derive_key_sha1 (CryptDeriveKey with SHA-1)."""
    pad1 = bytearray([0x36] * 64)
    pad2 = bytearray([0x5C] * 64)
    for i, b in enumerate(bytearray(digest)):
        pad1[i] ^= b
        pad2[i] ^= b
    return (sha1(bytes(pad1)).digest() + sha1(bytes(pad2)).digest())[:keyLength]


def _attr(tag, key):
    """FreeRDP freerdp_assistance_parse_attr: KEY="value" (None when absent)."""
    i = tag.find(key + '="')
    if i < 0:
        return None
    start = i + len(key) + 2
    end = tag.find('"', start)
    if end < 0:
        raise AssistanceError('unterminated attribute %s' % key)
    return tag[start:end]


def _elements(text, key):
    """All <KEY ...>...</KEY> or <KEY .../> elements (FreeRDP element search)."""
    out = []
    pos = 0
    while True:
        m = compile(r'<%s([>/ \t])' % escape(key)).search(text, pos)
        if not m:
            return out
        start = text.find('>', m.start())
        if start < 0:
            return out
        if text[start - 1] == '/':
            out.append(text[m.start():start + 1])
            pos = start + 1
        else:
            end = text.rfind('</%s>' % key, start)
            if end < 0:
                raise AssistanceError('missing </%s>' % key)
            out.append(text[m.start():end + len(key) + 3])
            pos = end + len(key) + 3


class Invitation(object):
    """
    @summary: A parsed Remote Assistance invitation (FreeRDP rdpAssistanceFile)
    """

    def __init__(self):
        self.type = 0  # 1 RCTICKET, 2 LHTICKET (encrypted)
        self.username = None
        self.rcTicket = None  # connection string 1
        self.rcTicketEncrypted = False
        self.lhTicket = None
        self.passStub = None
        self.dtStart = 0
        self.dtLength = 0
        self.lowSpeed = False
        self.connectionString2 = None
        self.sessionId = None  # RASessionId
        self.specificParams = None  # RASpecificParams (KH)
        self.specificParams2 = None  # KH2
        self.addresses = []  # [(host, port)]
        self.password = None
        self.encryptedPassStub = None

    # ---- what the connection needs (FreeRDP populate_settings_from_assistance_file) ----

    @property
    def ticket(self):
        """@return: {str} RCTicket sent to the novice: connection string 1, else 2"""
        return self.rcTicket if self.rcTicket else self.connectionString2

    def target(self):
        """@return: {tuple} (host, port) of the first address"""
        if not self.addresses:
            raise AssistanceError('the invitation has no address')
        return self.addresses[0]


def parse(text, password):
    """
    @summary: FreeRDP freerdp_assistance_parse_file_buffer
    @param text: {str} the .msrcincident content (or a bare connection string 2 <E>...</E>)
    @param password: {str} the invitation password
    @return: {Invitation}
    @raise AssistanceError:
    """
    if not isinstance(text, type(u'')):
        text = bytes(text).decode('utf-8', 'replace')
    inv = Invitation()
    inv.password = password
    uploads = _elements(text, 'UPLOADINFO')
    if uploads:
        _parseUploadInfo(inv, uploads[0])
    else:
        e = _elements(text, 'E')
        if not e:
            raise AssistanceError('neither UPLOADINFO nor E element: not an invitation')
        inv.connectionString2 = e[0]
        _parseConnectionString2(inv)
    return inv


def parseFile(path, password):
    """@summary: Parse an .msrcincident file (FreeRDP freerdp_assistance_parse_file)"""
    with open(path, 'rb') as f:
        data = f.read()
    for encoding in ('utf-8', 'utf-16'):
        try:
            text = data.decode(encoding)
            if '<' in text:
                return parse(text.replace(u'\ufeff', u''), password)
        except UnicodeDecodeError:
            continue
    raise AssistanceError('unreadable invitation file')


def _parseUploadInfo(inv, upload):
    head = upload[:upload.find('>') + 1]
    if _attr(head, 'TYPE') != 'Escalated':
        raise AssistanceError('UPLOADINFO TYPE is not "Escalated"')
    data = _elements(upload, 'UPLOADDATA')
    if not data:
        raise AssistanceError('missing UPLOADDATA')
    d = data[0]
    inv.username = _attr(d, 'USERNAME')
    inv.lhTicket = _attr(d, 'LHTICKET')
    inv.rcTicket = _attr(d, 'RCTICKET')
    inv.rcTicketEncrypted = _attr(d, 'RCTICKETENCRYPTED') == '1'
    stub = _attr(d, 'PassStub')
    inv.passStub = stub.replace('&amp;', '&') if stub is not None else None
    inv.dtStart = int(_attr(d, 'DtStart') or 0, 0) if (_attr(d, 'DtStart') or '').strip() else 0
    inv.dtLength = int(_attr(d, 'DtLength') or 0, 0) if (_attr(d, 'DtLength') or '').strip() else 0
    inv.lowSpeed = _attr(d, 'L') == '1'
    inv.type = 2 if inv.lhTicket else 1
    if inv.type == 2:
        _decrypt2(inv)
    else:
        _parseConnectionString1(inv)
    inv.encryptedPassStub = encryptPassStub(inv.password, inv.passStub)


def _parseConnectionString1(inv):
    """
    <ProtocolVersion>,<protocolType>,<machineAddressList>,<assistantAccountPwd>,
    <RASessionID>,<RASessionName>,<RASessionPwd>,<protocolSpecificParms>
    """
    if not inv.rcTicket:
        raise AssistanceError('missing RCTICKET')
    tokens = inv.rcTicket.split(',')
    if len(tokens) != 8 or tokens[0] != '65538' or tokens[1] != '1' or \
            tokens[3] != '*' or tokens[5] != '*' or tokens[6] != '*':
        raise AssistanceError('invalid RCTICKET')
    inv.sessionId = tokens[4]
    inv.specificParams = tokens[7]
    for item in tokens[2].split(';'):
        if not item:
            continue
        host, sep, port = item.partition(':')  # first ':' like FreeRDP
        if not sep:
            raise AssistanceError('address without port: %s' % item)
        inv.addresses.append((host, _port(port)))


def _port(text):
    try:
        port = int(text, 0)
    except ValueError:
        raise AssistanceError('invalid port %r' % text)
    if not 0 < port <= 0xFFFF:
        raise AssistanceError('invalid port %r' % text)
    return port


def _decrypt2(inv):
    """FreeRDP freerdp_assistance_decrypt2: AES-128-CBC, key derived from SHA1(password UTF-16)."""
    if inv.password is None:
        raise AssistanceError('the invitation is encrypted: a password is needed')
    try:
        encrypted = unhexlify(inv.lhTicket.strip())
    except (Error, TypeError):
        raise AssistanceError('invalid LHTICKET')
    key = _deriveKeySha1(sha1(_utf16(inv.password)).digest(), 16)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b'\x00' * 16)).decryptor()
    plain = decryptor.update(encrypted) + decryptor.finalize()
    pad = bytearray(plain)[-1] if plain else 0
    if not 1 <= pad <= 16 or plain[-pad:] != bytes(bytearray([pad] * pad)):
        raise AssistanceError('wrong invitation password')
    text = plain[:-pad].decode('utf-16-le', 'replace').split(u'\x00')[0]
    inv.connectionString2 = text
    _parseConnectionString2(inv)


def _parseConnectionString2(inv):
    """<E><A KH="..." ID="..."/><C><T ID="1" SID="0"><L P="port" N="address"/>...</T></C></E>"""
    e = _elements(inv.connectionString2 or '', 'E')
    if not e:
        raise AssistanceError('connection string 2 without E element')
    a = _elements(e[0], 'A')
    if not a:
        raise AssistanceError('connection string 2 without A element')
    for c in _elements(e[0], 'C'):
        for t in _elements(c, 'T'):
            for l in _elements(t, 'L'):
                port = int(_attr(l, 'P') or 0)
                for key in ('N', 'U'):
                    host = _attr(l, key)
                    if host:
                        inv.addresses.append((host, port))
    head = a[0]
    inv.specificParams = _attr(head, 'KH')
    inv.specificParams2 = _attr(head, 'KH2')
    inv.sessionId = _attr(head, 'ID')
