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
Multiparty Virtual Channel Extension ("encomsp" static channel, MS-RDPEMSC):
the participants of a shared session. With Windows Remote Assistance the
expert is a participant that may view; this channel requests the control of
the novice's desktop (view + interact), as FreeRDP does when
RemoteAssistanceRequestControl is set.
Ported from FreeRDP channels/encomsp/client/encomsp_main.c and
client/common/client.c (client_encomsp_participant_created,
freerdp_client_encomsp_set_control).
"""
from __future__ import absolute_import, division, print_function
from struct import error, pack, unpack_from
from rdpy.core.layer import LayerAutomata
from rdpy.core import log

CHANNEL_NAME = u'encomsp'
ENCOMSP_ORDER_HEADER_SIZE = 4
ENCOMSP_FILTER_ENABLED = 0x0001
ENCOMSP_MAY_VIEW = 0x0001
ENCOMSP_MAY_INTERACT = 0x0002
ENCOMSP_IS_PARTICIPANT = 0x0004
ENCOMSP_REQUEST_VIEW = 0x0001
ENCOMSP_REQUEST_INTERACT = 0x0002
ENCOMSP_ALLOW_CONTROL_REQUESTS = 0x0008
ODTYPE_FILTER_STATE_UPDATED = 0x0001
ODTYPE_APP_REMOVED = 0x0002
ODTYPE_APP_CREATED = 0x0003
ODTYPE_WND_REMOVED = 0x0004
ODTYPE_WND_CREATED = 0x0005
ODTYPE_WND_SHOW = 0x0006
ODTYPE_PARTICIPANT_REMOVED = 0x0007
ODTYPE_PARTICIPANT_CREATED = 0x0008
ODTYPE_PARTICIPANT_CTRL_CHANGED = 0x0009
ODTYPE_GRAPHICS_STREAM_PAUSED = 0x000A
ODTYPE_GRAPHICS_STREAM_RESUMED = 0x000B


def readUnicodeString(data, offset):
    """@summary: ENCOMSP_UNICODE_STRING: cchString (<= 1024) + UTF-16; @return: (text, next offset)"""
    cch = unpack_from('<H', data, offset)[0]
    if cch > 1024 or len(data) < offset + 2 + 2 * cch:
        raise ValueError(u'invalid ENCOMSP_UNICODE_STRING')
    return data[offset + 2:offset + 2 + 2 * cch].decode('utf-16-le', 'replace'), offset + 2 + 2 * cch


def controlLevelPdu(participantId, control):
    """@summary: ODTYPE_PARTICIPANT_CTRL_CHANGED (FreeRDP encomsp_send_change_participant_control_level_pdu)"""
    flags = ENCOMSP_REQUEST_VIEW | (ENCOMSP_REQUEST_INTERACT if control else 0)
    return pack('<HHHI', ODTYPE_PARTICIPANT_CTRL_CHANGED, ENCOMSP_ORDER_HEADER_SIZE + 6, flags, participantId)


class EncomspLayer(LayerAutomata):
    """
    @summary: The encomsp static channel. requestControl (default True, FreeRDP
    RemoteAssistanceRequestControl with /assistance) asks for control once, when this
    participant may view but not interact; setControl() asks or gives it back later.
    Events to the listener: ('participant', id, flags, name, isSelf), ('participantRemoved', id),
    ('control', id, flags), ('filter', flags), ('paused',), ('resumed',)
    """

    def __init__(self):
        LayerAutomata.__init__(self, None)
        self.requestControl = True
        self.participantId = 0
        self.participants = {}  # id -> (flags, name)
        self.hasControl = False
        self._listener = None
        self._buf = bytearray()

    def setListener(self, listener):
        self._listener = listener

    def _event(self, *event):
        if self._listener is not None:
            try:
                self._listener(*event)
            except Exception as e:
                log.warning(u'encomsp: event %s not handled: %r' % (event[0], e))

    def connect(self):
        log.debug(u'encomsp channel joined')

    def close(self):
        pass

    def recv(self, s):
        data = s.read()
        if len(data) < 8:
            return
        total, flags = unpack_from('<II', data, 0)
        if flags & 0x60:
            return
        if flags & 0x01:
            self._buf = bytearray()
        self._buf += data[8:]
        if flags & 0x02:
            pdu, self._buf = bytes(self._buf), bytearray()
            try:
                self.processReceive(pdu)
            except (ValueError, error) as e:
                log.warning(u'encomsp: bad PDU: %s' % e)

    def processReceive(self, data):
        """FreeRDP encomsp_process_receive: one or more orders."""
        offset = 0
        while offset < len(data):
            if len(data) - offset < ENCOMSP_ORDER_HEADER_SIZE:
                raise ValueError(u'short order header')
            orderType, length = unpack_from('<HH', data, offset)
            if length < ENCOMSP_ORDER_HEADER_SIZE or offset + length > len(data):
                raise ValueError(u'invalid order length %d' % length)
            self._order(orderType, data[offset + 4:offset + length])
            offset += length

    def _order(self, orderType, body):
        if orderType == ODTYPE_PARTICIPANT_CREATED:
            participantId, groupId, flags = unpack_from('<IIH', body, 0)
            name, _ = readUnicodeString(body, 10)
            self.participants[participantId] = (flags, name)
            isSelf = bool(flags & ENCOMSP_IS_PARTICIPANT)
            if isSelf:
                self.participantId = participantId
                self.hasControl = bool(flags & ENCOMSP_MAY_INTERACT)
            log.info(u'encomsp: participant %s%s (%s)' % (name or participantId, ' (this client)' if isSelf else '',
                                                          _flagText(flags)))
            self._event('participant', participantId, flags, name, isSelf)
            # FreeRDP client_encomsp_participant_created
            if self.requestControl and flags & ENCOMSP_MAY_VIEW and not flags & ENCOMSP_MAY_INTERACT:
                self.setControl(True)
                self.requestControl = False  # only once per connection, like FreeRDP
        elif orderType == ODTYPE_PARTICIPANT_REMOVED:
            participantId = unpack_from('<I', body, 0)[0]
            self.participants.pop(participantId, None)
            self._event('participantRemoved', participantId)
        elif orderType == ODTYPE_PARTICIPANT_CTRL_CHANGED:
            flags, participantId = unpack_from('<HI', body, 0)
            if participantId == self.participantId:
                self.hasControl = bool(flags & ENCOMSP_REQUEST_INTERACT)
                log.info(u'encomsp: control %s' % ('granted' if self.hasControl else 'view only'))
            self._event('control', participantId, flags)
        elif orderType == ODTYPE_FILTER_STATE_UPDATED:
            self._event('filter', bytearray(body[:1])[0] if body else 0)
        elif orderType == ODTYPE_GRAPHICS_STREAM_PAUSED:
            self._event('paused')
        elif orderType == ODTYPE_GRAPHICS_STREAM_RESUMED:
            self._event('resumed')
        elif orderType in (ODTYPE_APP_CREATED, ODTYPE_APP_REMOVED, ODTYPE_WND_CREATED, ODTYPE_WND_REMOVED,
                           ODTYPE_WND_SHOW):
            pass
        else:
            raise ValueError(u'unknown order type %d' % orderType)

    def setControl(self, control):
        """@summary: Request control (view + interact) or view only (FreeRDP freerdp_client_encomsp_set_control)"""
        from rdpy.protocol.rdp.channels.drdynvc import callInReactor
        callInReactor(self._send, controlLevelPdu(self.participantId, control))
        log.info(u'encomsp: %s' % ('control requested' if control else 'view only requested'))

    def _send(self, pdu):
        if self._transport is None:
            return
        from rdpy.core.type import String
        self._transport.send(String(pack('<II', len(pdu), 0x03) + pdu))

    def describe(self):
        if not self.participants:
            return None
        return 'participant %d, %s' % (self.participantId, 'control' if self.hasControl else 'view only')


def _flagText(flags):
    names = [n for f, n in ((ENCOMSP_MAY_VIEW, 'may view'), (ENCOMSP_MAY_INTERACT, 'may interact')) if flags & f]
    return ', '.join(names) or 'no access'
