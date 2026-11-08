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
What the client learns about the server while it connects - no extra
connection is made (same report as PyFreeRdpApp's getServerInfo /
getSessionInfo):

* the Windows version and build, NetBIOS/DNS names and the server clock,
  from the NTLM CHALLENGE of the NLA exchange (MS-NLMP 2.2.1.2),
* the logged-on user and session id, from Save Session Info (MS-RDPBCGR 2.2.10.1),
* the negotiated security protocol and RDP version,
* the TLS certificate: subject, issuer, validity, fingerprints, alternative names.
"""
from __future__ import absolute_import, division, print_function

import hashlib
import struct
from datetime import datetime, timedelta
from rdpy.core.compat import utc

# MS-RDPBCGR 2.2.1.1.1 selectedProtocol
PROTOCOL_SSL = 0x00000001
PROTOCOL_HYBRID = 0x00000002
PROTOCOL_RDSTLS = 0x00000004
PROTOCOL_HYBRID_EX = 0x00000008
PROTOCOL_RDSAAD = 0x00000010
_PROTOCOL_NAMES = ((PROTOCOL_RDSAAD, u'Azure AD (RDSAAD)'), (PROTOCOL_HYBRID_EX, u'NLA with early user auth'),
                   (PROTOCOL_HYBRID, u'NLA (CredSSP)'), (PROTOCOL_RDSTLS, u'RDSTLS'), (PROTOCOL_SSL, u'TLS'))

# MS-RDPBCGR 2.2.1.4.2 server core data version
RDP_VERSIONS = {
    0x00080001: u'4.0', 0x00080004: u'5.0 - 8.1', 0x00080005: u'10.0', 0x00080006: u'10.1',
    0x00080007: u'10.2', 0x00080008: u'10.3', 0x00080009: u'10.4', 0x0008000A: u'10.5',
    0x0008000B: u'10.6', 0x0008000C: u'10.7', 0x0008000D: u'10.8', 0x0008000E: u'10.9',
    0x0008000F: u'10.10', 0x00080010: u'10.11', 0x00080011: u'10.12',
}

# MS-RDPEGFX 2.2.3 capability versions
RDP_GFX_VERSIONS = {
    0x00080004: u'8.0', 0x00080105: u'8.1', 0x000A0002: u'10.0', 0x000A0100: u'10.1',
    0x000A0200: u'10.2', 0x000A0301: u'10.3', 0x000A0400: u'10.4', 0x000A0502: u'10.5',
    0x000A0600: u'10.6', 0x000A0601: u'10.6', 0x000A0701: u'10.7',
}
# MS-NLMP 2.2.2.1 AV_PAIR ids
_AV_NAMES = {1: u'netbiosComputerName', 2: u'netbiosDomainName', 3: u'dnsComputerName',
             4: u'dnsDomainName', 5: u'dnsTreeName'}
_AV_TIMESTAMP = 7
# Windows builds (newest first); client and server editions share builds from Windows 10 on
WINDOWS_BUILDS = (
    (26100, u'Windows Server 2025 / Windows 11 24H2'), (22631, u'Windows 11 23H2'),
    (22621, u'Windows 11 22H2'), (22000, u'Windows 11 21H2'), (20348, u'Windows Server 2022'),
    (19045, u'Windows 10 22H2'), (19044, u'Windows 10 21H2'), (19043, u'Windows 10 21H1'),
    (19042, u'Windows 10 20H2'), (19041, u'Windows 10 2004'), (18363, u'Windows 10 1909'),
    (18362, u'Windows 10 1903'), (17763, u'Windows Server 2019 / Windows 10 1809'),
    (17134, u'Windows 10 1803'), (16299, u'Windows 10 1709'), (15063, u'Windows 10 1703'),
    (14393, u'Windows Server 2016 / Windows 10 1607'), (10586, u'Windows 10 1511'),
    (10240, u'Windows 10 1507'), (9600, u'Windows Server 2012 R2 / Windows 8.1'),
    (9200, u'Windows Server 2012 / Windows 8'), (7601, u'Windows Server 2008 R2 SP1 / Windows 7 SP1'),
    (7600, u'Windows Server 2008 R2 / Windows 7'), (6002, u'Windows Server 2008 SP2 / Windows Vista SP2'),
)
# MS-RDPBCGR 2.2.10.1.1 infoType
INFOTYPE_LOGON = 0x00000000
INFOTYPE_LOGON_LONG = 0x00000001
INFOTYPE_LOGON_PLAINNOTIFY = 0x00000002
INFOTYPE_LOGON_EXTENDED_INFO = 0x00000003


def protocolNames(protocols):
    names = [name for bit, name in _PROTOCOL_NAMES if protocols & bit]
    return u' + '.join(names) if names else u'standard RDP security'


def windowsName(major, minor, build):
    if (major, minor) >= (6, 0):
        for number, name in WINDOWS_BUILDS:
            if build >= number:
                return name
    return u'Windows %d.%d' % (major, minor)


def filetimeToText(value):
    return (datetime(1601, 1, 1) + timedelta(microseconds=value // 10)).strftime(u'%Y-%m-%d %H:%M:%S UTC')


def parseChallenge(data):
    """
    @summary: Useful fields of an NTLM CHALLENGE_MESSAGE found in data
    @return: {dict} (empty when there is none)
    """
    start = data.find(b'NTLMSSP\x00\x02\x00\x00\x00')
    if start < 0:
        return {}
    msg = data[start:]
    info = {}
    if len(msg) >= 56 and struct.unpack_from('<I', msg, 20)[0] & 0x02000000:  # NEGOTIATE_VERSION
        major, minor, build = struct.unpack_from('<BBH', msg, 48)
        info.update(osVersion=u'%d.%d.%d' % (major, minor, build), osName=windowsName(major, minor, build))
    if len(msg) >= 48:
        length, _maxLength, offset = struct.unpack_from('<HHI', msg, 40)
        pairs = msg[offset:offset + length]
        position = 0
        while position + 4 <= len(pairs):
            avId, avLength = struct.unpack_from('<HH', pairs, position)
            value = pairs[position + 4:position + 4 + avLength]
            position += 4 + avLength
            if avId == 0:
                break
            if avId in _AV_NAMES:
                info[_AV_NAMES[avId]] = value.decode('utf-16-le', 'replace')
            elif avId == _AV_TIMESTAMP and avLength == 8:
                info[u'serverTime'] = filetimeToText(struct.unpack_from('<Q', value)[0])
    return info


def parseLogonInfo(infoType, data):
    """
    @summary: Save Session Info data (MS-RDPBCGR 2.2.10.1.1.1 / 2.2.10.1.1.2)
    @return: {dict} user, domain, sessionId when present
    """
    try:
        if infoType == INFOTYPE_LOGON and len(data) >= 576:
            cbDomain = struct.unpack_from('<I', data, 0)[0]
            domain = data[4:4 + min(cbDomain, 52)]
            cbUser = struct.unpack_from('<I', data, 56)[0]
            user = data[60:60 + min(cbUser, 512)]
            sessionId = struct.unpack_from('<I', data, 572)[0]
            return {u'domain': _utf16(domain), u'user': _utf16(user), u'sessionId': sessionId}
        if infoType == INFOTYPE_LOGON_LONG and len(data) >= 576:
            sessionId, cbDomain, cbUser = struct.unpack_from('<III', data, 6)
            domain = data[576:576 + cbDomain]
            user = data[576 + cbDomain:576 + cbDomain + cbUser]
            return {u'domain': _utf16(domain), u'user': _utf16(user), u'sessionId': sessionId}
    except struct.error:
        pass
    return {}


# MS-RDPBCGR 2.2.10.1.1.4 Logon Info Extended / 2.2.10.1.1.4.1.1 Logon Errors Info
LOGON_EX_AUTORECONNECTCOOKIE = 0x00000001
LOGON_EX_LOGONERRORS = 0x00000002
LOGON_ERROR_TYPES = {
    0xFFFFFFF9: u'disconnect refused', 0xFFFFFFFA: u'no permission to connect',
    0xFFFFFFFB: u'another user is logged on', 0xFFFFFFFC: u'reconnection options',
    0xFFFFFFFD: u'session terminated', 0xFFFFFFFE: u'logon continues',
}
LOGON_ERROR_DATA = {
    0x00000000: u'bad user name or password', 0x00000001: u'the password must be updated',
    0x00000002: u'logon failed', 0x00000003: u'warning',
}


def parseLogonErrors(data):
    """
    @summary: Logon errors of a Logon Info Extended (INFOTYPE_LOGON_EXTENDED_INFO)
    @return: {tuple|None} (errorType, errorData, message)
    """
    try:
        _length, present = struct.unpack_from('<HI', data, 0)
        offset = 6
        if present & LOGON_EX_AUTORECONNECTCOOKIE:
            size = struct.unpack_from('<I', data, offset)[0]
            offset += 4 + size
        if present & LOGON_EX_LOGONERRORS:
            _size, errorType, errorData = struct.unpack_from('<III', data, offset)
            message = u'%s: %s' % (LOGON_ERROR_TYPES.get(errorType, u'logon error 0x%08X' % errorType),
                                   LOGON_ERROR_DATA.get(errorData, u'0x%08X' % errorData))
            return errorType, errorData, message
    except struct.error:
        pass
    return None


def _utf16(raw):
    return raw.decode('utf-16-le', 'replace').split(u'\x00', 1)[0]


def humanDuration(seconds):
    for unit, size in ((u'day', 86400), (u'hour', 3600), (u'minute', 60)):
        if seconds >= size:
            count = int(seconds // size)
            return u'%d %s%s' % (count, unit, u's' if count != 1 else u'')
    return u'less than a minute'


def describeCertificate(cert):
    """
    @summary: Decode the server certificate
    @param cert: pyOpenSSL X509 (what the TLS transport returns)
    @return: {dict}
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        c = cert.to_cryptography() if hasattr(cert, 'to_cryptography') else cert
    except Exception as e:
        return {u'error': u'cannot decode the certificate: %s' % e}
    info = {u'subject': c.subject.rfc4514_string(), u'issuer': c.issuer.rfc4514_string()}
    info[u'selfSigned'] = c.subject == c.issuer
    try:
        cn = c.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
        info[u'commonName'] = cn[0].value if cn else None
    except Exception:
        pass
    notBefore = getattr(c, 'not_valid_before_utc', None) or c.not_valid_before.replace(tzinfo=utc)
    notAfter = getattr(c, 'not_valid_after_utc', None) or c.not_valid_after.replace(tzinfo=utc)
    info[u'notBefore'] = notBefore.strftime(u'%Y-%m-%d %H:%M:%S UTC')
    info[u'notAfter'] = notAfter.strftime(u'%Y-%m-%d %H:%M:%S UTC')
    remaining = notAfter - datetime.now(utc)
    info[u'expired'] = remaining.total_seconds() < 0
    info[u'daysLeft'] = remaining.days
    info[u'validity'] = (u'EXPIRED %s ago' if info[u'expired'] else u'expires in %s') % \
        humanDuration(abs(remaining.total_seconds()))
    der = c.public_bytes(serialization.Encoding.DER)
    info[u'sha256'] = u':'.join(u'%02X' % b for b in bytearray(hashlib.sha256(der).digest()))
    info[u'sha1'] = u':'.join(u'%02X' % b for b in bytearray(hashlib.sha1(der).digest()))
    try:
        info[u'signature'] = c.signature_hash_algorithm.name
    except Exception:
        info[u'signature'] = None
    key = c.public_key()
    size = getattr(key, 'key_size', None)
    info[u'publicKey'] = type(key).__name__.replace(u'_', u'').replace(u'PublicKey', u'') + \
        (u' %d bits' % size if size else u'')
    try:
        san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        info[u'subjectAltNames'] = [str(n.value) for n in san]
    except Exception:
        pass
    info[u'pem'] = c.public_bytes(serialization.Encoding.PEM).decode('ascii')
    return info


def findPeerCertificate(transport):
    """Walk the transport chain (CSSP -> TLS -> TCP) for the server certificate."""
    seen = 0
    while transport is not None and seen < 8:
        get = getattr(transport, 'getPeerCertificate', None)
        if get is not None:
            try:
                cert = get()
                if cert is not None:
                    return cert
            except Exception:
                pass
        transport = getattr(transport, 'transport', None)
        seen += 1
    return None


_SECTIONS = (
    (u'status', u'Status'),
    (u'connection', u'Connection'),
    (u'remote', u'Remote computer'),
    (u'logon', u'Logon'),
    (u'security', u'Connection security'),
    (u'features', u'Features'),
    (u'certificate', u'Certificate'),
)


def formatReport(info):
    """@return: {str} the report as text (console, dialog)"""
    lines = []
    for key, title in _SECTIONS:
        section = info.get(key)
        if not section:
            continue
        lines.append(title)
        for name, value in section.items():
            if value is None or value == u'' or name == u'pem':
                continue
            if isinstance(value, (list, tuple)):
                value = u', '.join(str(v) for v in value)
            elif isinstance(value, bool):
                value = u'yes' if value else u'no'
            lines.append(u'  %-22s %s' % (_label(name) + u':', value))
        lines.append(u'')
    return u'\n'.join(lines).rstrip() + u'\n'


def _label(name):
    out = []
    for ch in name:
        if ch.isupper() and out:
            out.append(u' ')
        out.append(ch.lower() if out else ch.upper())
    return u''.join(out)
