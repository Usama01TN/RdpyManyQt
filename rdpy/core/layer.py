# coding=utf-8
#
# Copyright (c) 2014-2015 Sylvain Peyrefitte
# Modified (c) 2026 UsamaTN <ininou.oussematn@gmail.com> - RdpyManyQt, see CHANGELOG.md
#
# This file is part of rdpy.
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
Join RDPY design with twisted design
RDPY use Layer Protocol design (like twisted)
"""
from __future__ import absolute_import, division, print_function
from rdpy.core.error import CallPureVirtualFuntion
from collections import deque
from rdpy.core import log


class IStreamListener(object):
    """
    @summary: Interface use to inform stream receiver capability
    """

    def recv(self, s):
        """
        @summary: Signal that data is available
        @param s: Stream
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s" % (self.__class__, "recv", "IStreamListener"))


class IStreamSender(object):
    """
    @summary: Interface use to inform stream sender capability 
    """

    def send(self, data):
        """
        @summary: Send Stream on layer
        @param data: Type or tuple element handle by transport layer
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s" % (self.__class__, "send", "IStreamSender"))


class Layer(object):
    """
    @summary:  A simple double linked list with presentation and transport layer
                and a subset of event (connect and close)
    """

    def __init__(self, presentation=None):
        """
        @param presentation: presentation layer
        """
        #presentation layer higher layer in model
        self._presentation = presentation
        #transport layer under layer in model
        self._transport = None
        #auto set transport layer of own presentation layer
        if self._presentation is not None:
            self._presentation._transport = self

    def connect(self):
        """
        @summary:  Call when transport layer is connected
                    default is send connect event to presentation layer
        """
        if self._presentation is not None:
            log.debug("Layer.connect()")
            self._presentation.connect()

    def close(self):
        """
        @summary:  Close layer event
                    default is sent to transport layer
        """
        if self._transport is not None:
            self._transport.close()


class LayerAutomata(Layer, IStreamListener):
    """
    @summary:  Layer with automata callback
                we can set next recv function used for Stream packet
                Usefull for event driven engine as twisted
    """

    def __init__(self, presentation=None):
        """
        @param presentation: presentation Layer
        """
        #call parent constructor
        Layer.__init__(self, presentation)

    def setNextState(self, callback=None):
        """
        @summary: Set the next callback in automata
        @param callback: a callable object
        """
        if callback is None:
            callback = lambda x: self.__class__.recv(self, x)

        self.recv = callback


#twisted layer concept
from twisted.internet import protocol
from twisted.internet.abstract import FileDescriptor
#first that handle stream     
from .type import Stream
import socket
import sys
import traceback

# TCP settings, FreeRDP defaults (libfreerdp/core/settings.c, tcp.c):
# keep-alive after 5 s idle, every 2 s, 3 probes; unacknowledged data
# aborts the connection after TcpAckTimeout (rdpyqt --timeout).
TCP_SETTINGS = {
    'keepAlive': True,
    'keepAliveDelay': 5,
    'keepAliveInterval': 2,
    'keepAliveRetries': 3,
    'ackTimeoutMs': 9000,
}


def _transportChain(transport):
    """Transports from the top (CSSP / TLS) down to the TCP socket."""
    chain = []
    while transport is not None and len(chain) < 8:
        chain.append(transport)
        transport = getattr(transport, 'transport', None)
    return chain


def bottomTransport(transport):
    """The TCP transport (the one owning the socket), or None."""
    for t in reversed(_transportChain(transport)):
        if hasattr(t, 'getHandle') and hasattr(t, 'abortConnection'):
            return t
    return None


def applyTcpSettings(transport):
    """No Nagle, keep-alive and ack timeout on the socket (FreeRDP freerdp_tcp_set_keep_alive_mode)."""
    tcp = bottomTransport(transport)
    if tcp is None:
        return
    try:
        tcp.setTcpNoDelay(True)
    except Exception:
        pass
    try:
        sock = tcp.getHandle()
        cfg = TCP_SETTINGS
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1 if cfg['keepAlive'] else 0)
        if not cfg['keepAlive']:
            return
        if sys.platform == 'win32' and hasattr(socket, 'SIO_KEEPALIVE_VALS'):
            sock.ioctl(socket.SIO_KEEPALIVE_VALS,
                       (1, cfg['keepAliveDelay'] * 1000, cfg['keepAliveInterval'] * 1000))
        else:
            for name, value in (('TCP_KEEPIDLE', cfg['keepAliveDelay']),
                                ('TCP_KEEPINTVL', cfg['keepAliveInterval']),
                                ('TCP_KEEPCNT', cfg['keepAliveRetries']),
                                ('TCP_USER_TIMEOUT', cfg['ackTimeoutMs'])):
                if hasattr(socket, name):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), int(value))
    except Exception as e:
        log.debug("TCP keep-alive settings not applied: %s" % e)


class RawLayerClientFactory(protocol.ClientFactory):
    """
    @summary: Abstract class for Raw layer client factory
    """

    def buildProtocol(self, addr):
        """
        @summary: Function call from twisted
        @param addr: destination address
        """
        rawLayer = self.buildRawLayer(addr)
        rawLayer.setFactory(self)
        return rawLayer

    def buildRawLayer(self, addr):
        """
        @summary: Override this function to build raw layer
        @param addr: destination address
        """
        raise CallPureVirtualFuntion(
            "%s:%s defined by interface %s" % (self.__class__, "buildRawLayer", "RawLayerClientFactory"))

    def connectionLost(self, rawlayer, reason):
        """
        @summary: Override this method to handle connection lost
        @param rawlayer: rawLayer that cause connectionLost event
        @param reason: twisted reason
        """
        raise CallPureVirtualFuntion(
            "%s:%s defined by interface %s" % (self.__class__, "connectionLost", "RawLayerClientFactory"))


class RawLayerServerFactory(protocol.ServerFactory):
    """
    @summary: Abstract class for Raw layer server factory
    """

    def buildProtocol(self, addr):
        """
        @summary: Function call from twisted
        @param addr: destination address
        """
        rawLayer = self.buildRawLayer(addr)
        rawLayer.setFactory(self)
        return rawLayer

    def buildRawLayer(self, addr):
        """
        @summary: Override this function to build raw layer
        @param addr: destination address
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s" % (self.__class__, "recv", "IStreamListener"))

    def connectionLost(self, rawlayer, reason):
        """
        @summary: Override this method to handle connection lost
        @param rawlayer: rawLayer that cause connectionLost event
        @param reason: twisted reason
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s" % (self.__class__, "recv", "IStreamListener"))


class RawLayer(protocol.Protocol, LayerAutomata, IStreamSender):
    """
    @summary:  Wait event from twisted engine
                And format correct size packet
                And send correct packet to next automata callback
    """

    def __init__(self, presentation=None):
        """
        @param presentation: presentation layer in layer list
        """
        #call parent automata
        LayerAutomata.__init__(self, presentation)
        # deque of incoming data chunks — O(1) append and popleft, no compaction needed
        self._chunks = deque()
        self._chunk_offset = 0  # offset into self._chunks[0]
        self._total_available = 0  # total bytes across all chunks
        #len of next packet pass to next state function
        self._expectedLen = 0
        self._factory = None

    def setFactory(self, factory):
        """
        @summary: Call by RawLayer Factory
        @param param: RawLayerClientFactory or RawLayerFactory
        """
        self._factory = factory

    def _consume(self, n):
        """
        Consume exactly n bytes from the chunk deque and return them as bytes.
        Uses O(1) popleft — no buffer compaction required.
        """
        result = bytearray(n)
        written = 0
        while written < n:
            chunk = self._chunks[0]
            available = len(chunk) - self._chunk_offset
            take = min(available, n - written)
            result[written:written + take] = chunk[self._chunk_offset:self._chunk_offset + take]
            written += take
            self._chunk_offset += take
            if self._chunk_offset >= len(chunk):
                self._chunks.popleft()
                self._chunk_offset = 0
        self._total_available -= n
        return bytes(result)

    def dataReceived(self, data):
        """
        @summary:  Inherit from twisted.protocol class
                    main event of received data
        @param data: string data receive from twisted
        """
        if getattr(self, 'protocolError', None) is not None:
            return  # already failing: ignore what is still buffered
        self._chunks.append(bytes(data))
        self._total_available += len(data)
        try:
            while self._expectedLen > 0 and self._total_available >= self._expectedLen:
                expectedData = Stream(self._consume(self._expectedLen))
                self.recv(expectedData)
        except Exception as e:
            # A PDU the client can not process: end the session at once
            # (FreeRDP: the error is recorded, the transport disconnected)
            # instead of leaving a half-working connection behind.
            self.protocolError = "%s: %s" % (type(e).__name__, e)
            self.protocolException = e
            log.error("protocol error, disconnecting: %s\n%s" % (self.protocolError, traceback.format_exc()))
            self.abort()

    def connectionMade(self):
        """
        @summary: inherit from twisted protocol
        """
        # Disable Nagle's algorithm: input events, frame acknowledgements and
        # audio confirmations are small packets that must leave at once,
        # otherwise the server sees the client as slow and throttles video.
        # Keep-alive and ack timeout detect a dead server like FreeRDP does.
        self.protocolError = None
        applyTcpSettings(self.transport)
        #join two scheme
        log.debug("RawLayer.connect()")
        self.connect()

    def connectionLost(self, reason):
        """
        @summary: Call from twisted engine when protocol is closed
        @param reason: str represent reason of close connection
        """
        self._factory.connectionLost(self, reason)

    def getDescriptor(self):
        """
        @return: the twited file descriptor
        """
        return self.transport

    def close(self, graceful=True):
        """
        @summary: Close the connection.
        graceful (after the Disconnect Provider Ultimatum): the server
        answers the ultimatum by closing its side, so wait briefly for that
        (no data is left unread, no reset), then close the socket, and abort
        it if it is still open after 2 s - closing never hangs.
        Otherwise (still connecting) the socket is closed at once.
        """
        tcp = bottomTransport(self.transport)
        if tcp is None:
            FileDescriptor.loseConnection(self.getDescriptor())
            return
        from twisted.internet import reactor

        def lose():
            if getattr(tcp, 'connected', False):
                FileDescriptor.loseConnection(tcp)

        def force():
            if getattr(tcp, 'connected', False):
                log.debug("RawLayer.close(): server did not close, aborting the socket")
                tcp.abortConnection()
        if graceful:
            reactor.callLater(0.5, lose)
        else:
            lose()
        reactor.callLater(2.0, force)

    def abort(self):
        """Drop the connection immediately (errors, timeouts)."""
        tcp = bottomTransport(self.transport)
        if tcp is not None:
            tcp.abortConnection()
        elif self.transport is not None:
            self.transport.loseConnection()

    def expect(self, expectedLen, callback=None):
        """
        @summary:  Set next automata callback, 
                    But this callback will be only called when
                    data have expectedLen
        @param expectedLen: in bytes length use to call next state
        @param callback: callback call when expected length bytes is received
        """
        self._expectedLen = expectedLen
        #default callback is recv from LayerAutomata
        self.setNextState(callback)

    def send(self, message):
        """
        @summary:  Send Stream on TCP layer
                    write rdpy Stream message to str
                    And send it to transport layer
        @param message: (tuple | Type)
        """
        s = Stream()
        s.writeType(message)
        self.transport.write(s.getvalue())
