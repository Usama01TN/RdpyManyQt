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
Connection security, modeled on FreeRDP's rdpSettings:
* NlaSecurity / TlsSecurity / RdpSecurity: the security protocols the client
  offers (MS-RDPBCGR 2.2.1.1.1 requestedProtocols). Like FreeRDP's nego.c the
  client starts with the strongest enabled one and falls back to the next
  enabled one when the server refuses it.
* IgnoreCertificate and the certificate policy (FreeRDP /cert:ignore|tofu|deny):
  the server's TLS certificate is checked during the TLS handshake, before
  any credential is sent.

    ignore  accept any certificate (not recommended)
    tofu    trust on first use: a certificate signed by a trusted authority
            for this host name is accepted; otherwise the first certificate
            seen for host:port is remembered (known_hosts) and a different
            one later is refused
    deny    strict: only a valid chain to a trusted authority, for this host
            name, is accepted (self-signed certificates are refused)
"""
from __future__ import absolute_import, division, print_function

import fnmatch
import hashlib
import ipaddress
import os
import ssl as _pyssl
import sys
import threading

import rdpy.core.log as log
from rdpy.core.compat import replace
# MS-RDPBCGR requestedProtocols
PROTOCOL_RDP = 0x00000000
PROTOCOL_SSL = 0x00000001
PROTOCOL_HYBRID = 0x00000002
CERT_IGNORE = 'ignore'
CERT_TOFU = 'tofu'
CERT_DENY = 'deny'
CERT_MODES = (CERT_IGNORE, CERT_TOFU, CERT_DENY)


class SecuritySettings(object):
    """
    @summary: FreeRDP-like security flags (rdpSettings NlaSecurity,
    TlsSecurity, RdpSecurity, IgnoreCertificate)
    """

    def __init__(self, nla=True, tls=True, rdp=True, certificate=CERT_TOFU):
        self.nla = bool(nla)
        self.tls = bool(tls)
        self.rdp = bool(rdp)
        self.certificate = certificate

    @classmethod
    def maximum(cls):
        """NLA + TLS only, legacy RDP security off, strict certificate validation."""
        return cls(nla=True, tls=True, rdp=False, certificate=CERT_DENY)

    @classmethod
    def compatible(cls):
        """Every protocol, falling back to RDP security, self-signed certificates accepted."""
        return cls(nla=True, tls=True, rdp=True, certificate=CERT_IGNORE)

    @classmethod
    def only(cls, protocol, certificate=CERT_TOFU):
        """FreeRDP /sec:<nla|tls|rdp>: offer one protocol only."""
        return cls(nla=protocol == 'nla', tls=protocol == 'tls', rdp=protocol == 'rdp',
                   certificate=certificate)

    @property
    def ignoreCertificate(self):
        return self.certificate == CERT_IGNORE

    def attempts(self, havePassword):
        """
        @summary: requestedProtocols to try, strongest first (FreeRDP nego.c:
        NLA, then TLS, then RDP, each only when enabled)
        @param havePassword: {bool} NLA needs credentials
        @return: {list} of (name, requestedProtocols)
        """
        out = []
        if self.nla and havePassword:
            out.append(('NLA', PROTOCOL_HYBRID | (PROTOCOL_SSL if self.tls else 0)))
        if self.tls:
            out.append(('TLS', PROTOCOL_SSL))
        if self.rdp:
            out.append(('RDP', PROTOCOL_RDP))
        return out

    def allows(self, selectedProtocol):
        """@return: {bool} the protocol the server selected is enabled"""
        if selectedProtocol & PROTOCOL_HYBRID:
            return self.nla
        if selectedProtocol & PROTOCOL_SSL:
            return self.tls
        return self.rdp

    def describe(self):
        names = [n for n, on in (('NLA', self.nla), ('TLS', self.tls), ('RDP', self.rdp)) if on]
        return '%s, certificate: %s' % ('+'.join(names) or 'none', self.certificate)


# ---------------------------------------------------------------------------
# Known hosts (trust on first use)
# ---------------------------------------------------------------------------

def knownHostsPath():
    """~/.config/rdpymanyqt/known_hosts (%APPDATA%\\rdpymanyqt\\known_hosts on Windows)"""
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
    else:
        base = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'rdpymanyqt', 'known_hosts')


class KnownHosts(object):
    """
    @summary: host port sha256-fingerprint lines (like FreeRDP's known_hosts2)
    """
    _lock = threading.Lock()

    def __init__(self, path=None):
        self.path = path or knownHostsPath()

    def _load(self):
        entries = {}
        try:
            with open(self.path, 'r') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3 and not line.startswith('#'):
                        entries[(parts[0].lower(), parts[1])] = parts[2]
        except (IOError, OSError):
            pass
        return entries

    def get(self, host, port):
        return self._load().get((host.lower(), str(port)))

    def set(self, host, port, fingerprint):
        with self._lock:
            entries = self._load()
            entries[(host.lower(), str(port))] = fingerprint
            folder = os.path.dirname(self.path)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            tmp = self.path + '.tmp'
            with open(tmp, 'w') as f:
                f.write('# host port sha256 - certificates trusted by rdpymanyqt\n')
                for (h, p), fp in sorted(entries.items()):
                    f.write('%s %s %s\n' % (h, p, fp))
            replace(tmp, self.path)


# ---------------------------------------------------------------------------
# Certificate verification
# ---------------------------------------------------------------------------

def fingerprint(x509):
    """SHA-256 fingerprint AA:BB:... of a pyOpenSSL X509"""
    from OpenSSL import crypto
    der = crypto.dump_certificate(crypto.FILETYPE_ASN1, x509)
    return ':'.join('%02X' % b for b in bytearray(hashlib.sha256(der).digest()))


def certificateNames(x509):
    """(dns names, ip addresses, common name) of a pyOpenSSL X509"""
    dns, ips = [], []
    try:
        c = x509.to_cryptography()
        from cryptography import x509 as cx
        try:
            san = c.extensions.get_extension_for_class(cx.SubjectAlternativeName).value
            dns = [n.lower() for n in san.get_values_for_type(cx.DNSName)]
            ips = [str(a) for a in san.get_values_for_type(cx.IPAddress)]
        except cx.ExtensionNotFound:
            pass
    except Exception:
        pass
    cn = x509.get_subject().CN
    return dns, ips, (cn or '').lower()


def hostnameMatches(x509, host):
    """RFC 6125: SAN names (or the CN when there is no SAN) must match host."""
    dns, ips, cn = certificateNames(x509)
    try:
        ip = str(ipaddress.ip_address(host))
        return ip in ips
    except ValueError:
        pass
    host = host.lower().rstrip('.')
    names = dns or ([cn] if cn else [])
    for name in names:
        if name == host or (name.startswith('*.') and '.' in host and
                            fnmatch.fnmatchcase(host, name) and host.count('.') == name.count('.')):
            return True
    # Windows RDP certificates are issued to the computer name: accept the
    # short name when connecting with the fully qualified one and vice versa
    short = host.split('.')[0]
    return any(n.split('.')[0] == short and ('.' not in n or '.' not in host) for n in names)


def _systemCertificates():
    """Trusted root certificates of the system (works on Windows too)."""
    try:
        ctx = _pyssl.create_default_context()
        ctx.load_default_certs()
        return ctx.get_ca_certs(binary_form=True)
    except Exception:
        return []


class CertificateVerifier(object):
    """
    @summary: Checks the server certificate during the TLS handshake
    (pyOpenSSL verify callback) according to SecuritySettings.certificate.
    When a certificate is refused, `rejection` explains why and `certificate`
    holds the server certificate so the user can decide to trust it.
    """

    def __init__(self, host, port, mode=CERT_TOFU, knownHosts=None):
        self.host = host
        self.port = port
        self.mode = mode
        self.knownHosts = knownHosts or KnownHosts()
        self.rejection = None
        self.certificate = None
        self._chainOk = True
        self._chainErrors = []

    def configure(self, context):
        """Install the verification on a pyOpenSSL SSL.Context."""
        from OpenSSL import SSL, crypto
        if self.mode == CERT_IGNORE:
            context.set_verify(SSL.VERIFY_NONE, lambda *a: True)
            return
        store = context.get_cert_store()
        try:
            context.set_default_verify_paths()
        except Exception:
            pass
        for der in _systemCertificates():
            try:
                store.add_cert(crypto.load_certificate(crypto.FILETYPE_ASN1, der))
            except Exception:
                pass  # duplicates
        context.set_verify(SSL.VERIFY_PEER, self._verify)

    def _verify(self, conn, x509, errno, depth, ok):
        if not ok:
            self._chainOk = False
            self._chainErrors.append('depth %d: error %d' % (depth, errno))
        if depth > 0:
            return True  # decide on the server certificate itself
        self.certificate = x509
        accepted = self.decide(x509, self._chainOk)
        return accepted

    def decide(self, x509, chainOk):
        """
        @summary: Accept or refuse the server certificate
        @return: {bool}
        """
        fp = fingerprint(x509)
        nameOk = hostnameMatches(x509, self.host)
        if chainOk and nameOk:
            log.debug("certificate of %s verified by a trusted authority" % self.host)
            return True
        why = []
        if not chainOk:
            why.append('self-signed' if x509.get_subject() == x509.get_issuer()
                       else 'not issued by a trusted authority')
        if not nameOk:
            why.append('issued to "%s", not to "%s"' % (x509.get_subject().CN, self.host))
        problem = ' and '.join(w for w in why if w)
        if self.mode == CERT_DENY:
            self.rejection = "server certificate refused (%s); use --cert tofu or --cert ignore to " \
                             "connect anyway" % problem
            log.error(self.rejection + "\n  SHA-256 %s" % fp)
            return False
        known = self.knownHosts.get(self.host, self.port)
        if known is None:
            self.knownHosts.set(self.host, self.port, fp)
            log.warning("certificate of %s:%s is %s: trusted on first use and remembered in %s\n"
                        "  SHA-256 %s" % (self.host, self.port, problem, self.knownHosts.path, fp))
            return True
        if known == fp:
            return True
        self.rejection = ("the certificate of %s:%s CHANGED since the last connection - possible "
                          "man-in-the-middle attack. New SHA-256 %s, trusted %s" %
                          (self.host, self.port, fp, known))
        log.error(self.rejection)
        return False

    def trustCurrent(self):
        """Remember the refused certificate (user decision) for the next connection."""
        if self.certificate is not None:
            self.knownHosts.set(self.host, self.port, fingerprint(self.certificate))
