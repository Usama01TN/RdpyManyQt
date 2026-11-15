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
Remote Assistance virtual channel ("remdesk" static channel, MS-RA): the expert
side of Windows Remote Assistance. The novice's computer announces its
protocol version; the client answers with the invitation ticket and the expert
blob (expert name + PassStub encrypted with the invitation password), which the
novice verifies before sharing the session.
Ported from FreeRDP channels/remdesk/client/remdesk_main.c and
channels/remdesk/common/remdesk_common.c. Invitations are read by
rdpy.protocol.rdp.channels.assistance.
"""
from __future__ import absolute_import, division, print_function
from struct import error, pack, unpack_from
from threading import Lock
from rdpy.protocol.rdp.channels import assistance
from rdpy.core.layer import LayerAutomata
from rdpy.core import log

CHANNEL_NAME = u'remdesk'
CHANNEL_CTL_NAME = u'RC_CTL'
REMDESK_CTL_REMOTE_CONTROL_DESKTOP = 1
REMDESK_CTL_RESULT = 2
REMDESK_CTL_AUTHENTICATE = 3
REMDESK_CTL_SERVER_ANNOUNCE = 4
REMDESK_CTL_DISCONNECT = 5
REMDESK_CTL_VERSIONINFO = 6
REMDESK_CTL_ISCONNECTED = 7
REMDESK_CTL_VERIFY_PASSWORD = 8
REMDESK_CTL_EXPERT_ON_VISTA = 9
REMDESK_CTL_RANOVICE_NAME = 10
REMDESK_CTL_RAEXPERT_NAME = 11
REMDESK_CTL_TOKEN = 12
RESULTS = {
    0: u'no error', 1: u'no information', 3: u'local not error', 4: u'ended by the novice',
    5: u'ended by the server', 8: u'connection timed out', 21: u'licensing failed',
    25: u'help session not found', 26: u'invalid password', 27: u'help session expired',
    35: u'the novice has not answered yet', 36: u'the novice accepted', 37: u'the novice is already helped',
    38: u'the novice is considering the request', 40: u'the novice never answered', 41: u'the novice refused',
    42: u'access denied', 43: u'user not found', 45: u'self help not supported', 47: u'incompatible version',
    48: u'session not connected', 50: u'system shut down', 51: u'stopped by the user', 53: u'parameters mismatch',
    61: u'passwords do not match',
}


def channelHeader(name, dataLength):
    """
    @summary: REMDESK_CHANNEL_HEADER: ChannelNameLen (bytes, with the NUL), DataLen,
    ChannelName UTF-16 with its NUL (FreeRDP remdesk_write_channel_header)
    """
    nameW = (name + u'\x00').encode('utf-16-le')
    return pack('<II', len(nameW), dataLength) + nameW


def ctlPdu(msgType, body=b''):
    """@summary: A RC_CTL PDU: channel header (DataLen = 4 + body) + msgType + body"""
    return channelHeader(CHANNEL_CTL_NAME, 4 + len(body)) + pack('<I', msgType) + body


def utf16(text):
    """UTF-16LE without terminator (FreeRDP ConvertUtf8ToWCharAlloc length)."""
    return (text or u'').encode('utf-16-le')


class RemdeskLayer(LayerAutomata):
    """
    @summary: The remdesk static channel. Configure with setInvitation() (or
    setTicket()) before connecting; events go to the listener:
    ('version', minor), ('result', code, text), ('disconnect',).
    """

    def __init__(self):
        LayerAutomata.__init__(self, None)
        self.ticket = None        # RemoteAssistanceRCTicket
        self.passStub = None
        self.password = None      # RemoteAssistancePassword (invitation password)
        self.expertName = u'Expert'
        self.version = 0
        self.lastResult = None
        self._listener = None
        self._buf = bytearray()
        self._total = 0
        self._expertBlob = None
        self._encryptedPassStub = None
        self._lock = Lock()

    def setListener(self, listener):
        self._listener = listener

    def setInvitation(self, invitation, expertName=None):
        """@param invitation: {assistance.Invitation}"""
        self.setTicket(invitation.ticket, invitation.passStub, invitation.password, expertName)

    def setTicket(self, ticket, passStub, password, expertName=None):
        self.ticket = ticket
        self.passStub = passStub
        self.password = password
        if expertName:
            self.expertName = expertName
        self._expertBlob = None
        self._encryptedPassStub = None

    def _event(self, *event):
        if self._listener is not None:
            try:
                self._listener(*event)
            except Exception as e:
                log.warning(u'remote assistance: event %s not handled: %r' % (event[0], e))

    # ---- expert blob (FreeRDP remdesk_generate_expert_blob) ----

    def _generateExpertBlob(self):
        if self._expertBlob is None:
            if self.password is None or self.passStub is None:
                raise ValueError(u'remote assistance needs the invitation password and PassStub')
            self._encryptedPassStub = assistance.encryptPassStub(self.password, self.passStub)
            self._expertBlob = assistance.expertBlob(self.expertName or u'Expert', self._encryptedPassStub)
        return self._expertBlob

    # ---- channel ----

    def connect(self):
        log.debug(u'remote assistance: remdesk channel joined')

    def close(self):
        pass

    def recv(self, s):
        data = s.read()
        if len(data) < 8:
            return
        total, flags = unpack_from('<II', data, 0)
        if flags & 0x60:  # CHANNEL_FLAG_SUSPEND / RESUME
            return
        if flags & 0x01:
            self._buf = bytearray()
            self._total = total
        self._buf += data[8:]
        if flags & 0x02:
            pdu, self._buf = bytes(self._buf), bytearray()
            try:
                self._processReceive(pdu)
            except (ValueError, error) as e:
                log.warning(u'remote assistance: bad PDU: %s' % e)

    def _processReceive(self, pdu):
        """FreeRDP remdesk_process_receive."""
        if len(pdu) < 8:
            raise ValueError(u'short header')
        nameLen, dataLen = unpack_from('<II', pdu, 0)
        if nameLen > 64 or nameLen % 2:
            raise ValueError(u'invalid channel name length %d' % nameLen)
        name = pdu[8:8 + nameLen].decode('utf-16-le', 'replace').split(u'\x00')[0]
        body = pdu[8 + nameLen:]
        if name == CHANNEL_CTL_NAME:
            self._recvCtl(body, dataLen)
        else:
            log.debug(u'remote assistance: data on sub-channel %r ignored' % name)

    def _recvCtl(self, body, dataLen):
        if dataLen < 4 or len(body) < 4:
            raise ValueError(u'short RC_CTL PDU')
        msgType = unpack_from('<I', body, 0)[0]
        if msgType == REMDESK_CTL_VERSIONINFO:
            if dataLen < 12 or len(body) < 12:
                raise ValueError(u'short version info')
            major, minor = unpack_from('<II', body, 4)
            if major != 1 or minor == 0 or minor > 2:
                log.warning(u'remote assistance: unsupported version %d.%d' % (major, minor))
                return
            self.version = minor
            log.info(u'remote assistance: novice protocol version 1.%d' % minor)
            self._event('version', minor)
            if minor == 1:
                self.sendVersionInfo()
                self.sendAuthenticate()
                self.sendRemoteControlDesktop()
            else:
                self.sendExpertOnVista()
                self.sendVerifyPassword()
        elif msgType == REMDESK_CTL_RESULT:
            if dataLen < 8 or len(body) < 8:
                raise ValueError(u'short result')
            result = unpack_from('<I', body, 4)[0]
            self.lastResult = result
            text = RESULTS.get(result, u'result %d' % result)
            (log.info if result in (0, 36) else log.warning)(u'remote assistance: %s' % text)
            self._event('result', result, text)
        elif msgType == REMDESK_CTL_DISCONNECT:
            log.info(u'remote assistance: the novice ended the assistance')
            self._event('disconnect')
        elif msgType in (REMDESK_CTL_AUTHENTICATE, REMDESK_CTL_SERVER_ANNOUNCE, REMDESK_CTL_ISCONNECTED,
                         REMDESK_CTL_VERIFY_PASSWORD, REMDESK_CTL_EXPERT_ON_VISTA, REMDESK_CTL_RANOVICE_NAME,
                         REMDESK_CTL_RAEXPERT_NAME, REMDESK_CTL_TOKEN, REMDESK_CTL_REMOTE_CONTROL_DESKTOP):
            pass
        else:
            raise ValueError(u'unknown RC_CTL message %d' % msgType)

    # ---- client PDUs (FreeRDP remdesk_send_ctl_*) ----

    def sendVersionInfo(self):
        self._send(ctlPdu(REMDESK_CTL_VERSIONINFO, pack('<II', 1, 2)))

    def sendAuthenticate(self):
        blob = self._generateExpertBlob()
        self._send(ctlPdu(REMDESK_CTL_AUTHENTICATE, utf16(self.ticket) + utf16(blob)))

    def sendRemoteControlDesktop(self):
        self._send(ctlPdu(REMDESK_CTL_REMOTE_CONTROL_DESKTOP, utf16(self.ticket)))

    def sendVerifyPassword(self):
        self._send(ctlPdu(REMDESK_CTL_VERIFY_PASSWORD, utf16(self._generateExpertBlob())))

    def sendExpertOnVista(self):
        self._generateExpertBlob()
        self._send(ctlPdu(REMDESK_CTL_EXPERT_ON_VISTA, self._encryptedPassStub))

    def _send(self, pdu):
        from rdpy.protocol.rdp.channels.drdynvc import callInReactor
        callInReactor(self._sendPdu, pdu)

    def _sendPdu(self, pdu):
        if self._transport is None:
            return
        from rdpy.core.type import String
        chunk = 1600
        for offset in range(0, max(1, len(pdu)), chunk):
            flags = (0x01 if offset == 0 else 0) | (0x02 if offset + chunk >= len(pdu) else 0)
            self._transport.send(String(pack('<II', len(pdu), flags) + pdu[offset:offset + chunk]))
