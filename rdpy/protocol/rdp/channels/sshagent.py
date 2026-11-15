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
SSH agent forwarding ("SSHAGENT" dynamic channel): programs in the remote
session use the ssh-agent of the client computer (keys never leave it).
A FreeRDP extension used by FreeRDP based servers (xrdp's chansrv supports it
too): the server relays the agent protocol of the session to this channel and
the client relays it to the local agent - the Unix socket SSH_AUTH_SOCK, or on
Windows the OpenSSH agent pipe \\\\.\\pipe\\openssh-ssh-agent (or the pipe named
by SSH_AUTH_SOCK).
Ported from FreeRDP channels/sshagent/client/sshagent_main.c. The agent protocol
is request / response (draft-miller-ssh-agent: uint32 length + message): the
relay writes each complete request and reads the complete answer on one worker
thread, which works the same with Unix sockets and Windows pipes.
"""
from __future__ import absolute_import, division, print_function
from socket import socket, AF_UNIX, SOCK_STREAM
from struct import unpack_from, unpack
from threading import Thread
from sys import platform
from os import environ
from rdpy.core.compat import queue
from rdpy.core import log

CHANNEL_NAME = u'SSHAGENT'
WINDOWS_AGENT_PIPE = r'\\.\pipe\openssh-ssh-agent'
MAX_MESSAGE = 256 * 1024  # ssh-agent messages are small; refuse absurd lengths


def agentPath():
    """@return: {str|None} where the local agent listens"""
    path = environ.get('SSH_AUTH_SOCK')
    if path:
        return path
    if platform == 'win32':
        return WINDOWS_AGENT_PIPE
    return None


class _AgentConnection(object):
    """
    One connection to the local agent (Unix socket or Windows pipe).
    """

    def __init__(self, path):
        self.path = path
        if path.startswith('\\\\.\\pipe\\') or path.startswith('//./pipe/'):
            self._pipe = open(path, 'r+b', buffering=0)
            self._sock = None
        else:
            self._pipe = None
            self._sock = socket(AF_UNIX, SOCK_STREAM)
            self._sock.connect(path)

    def write(self, data):
        if self._sock is not None:
            self._sock.sendall(data)
        else:
            self._pipe.write(data)
            self._pipe.flush()

    def readExact(self, n):
        out = bytearray()
        while len(out) < n:
            chunk = self._sock.recv(n - len(out)) if self._sock is not None else self._pipe.read(n - len(out))
            if not chunk:
                raise EOFError(u'the ssh agent closed the connection')
            out += chunk
        return bytes(out)

    def close(self):
        for obj in (self._sock, self._pipe):
            if obj is not None:
                try:
                    obj.close()
                except (OSError, IOError):
                    pass


class SshAgent(object):
    """
    @summary: The sshagent plugin (DrdynvcLayer listener). One channel at a time,
    like FreeRDP; refused when no local agent is reachable.
    """

    def __init__(self):
        self.enabled = False  # FreeRDP /ssh-agent
        self.path = None  # None: SSH_AUTH_SOCK, or the Windows OpenSSH pipe
        self._handler = None

    def channelFactory(self, channel):
        if not self.enabled:
            return None
        if self._handler is not None:
            log.warning(u'ssh agent: a second channel was refused (one at a time)')
            return None
        path = self.path or agentPath()
        if not path:
            log.warning(u'ssh agent: SSH_AUTH_SOCK is not set: forwarding refused')
            return None
        try:
            agent = _AgentConnection(path)
        except (OSError, IOError) as e:
            log.warning(u'ssh agent: cannot reach %s: %s' % (path, e))
            return None
        self._handler = _SshAgentChannel(self, channel, agent)
        log.info(u'ssh agent: forwarding %s to the remote session' % path)
        return self._handler

    def _closed(self, handler):
        if handler is self._handler:
            self._handler = None

    def describe(self):
        return 'forwarded' if self._handler is not None else None


class _SshAgentChannel(object):

    def __init__(self, plugin, channel, agent):
        self.plugin = plugin
        self.channel = channel
        self.agent = agent
        self._buffer = bytearray()
        self._queue = queue.Queue()
        self._thread = Thread(target=self._run, name=u'sshagent-relay')
        self._thread.daemon = True
        self._thread.start()

    def recv(self, data):
        """Bytes from the remote session: complete requests go to the agent, in order."""
        self._buffer += bytes(data)
        while len(self._buffer) >= 4:
            length = unpack_from('>I', self._buffer, 0)[0]
            if length > MAX_MESSAGE:
                log.warning(u'ssh agent: %d-byte message refused' % length)
                self.channel.close()
                return
            if len(self._buffer) < 4 + length:
                break
            message = bytes(self._buffer[:4 + length])
            del self._buffer[:4 + length]
            self._queue.put(message)

    def _run(self):
        while True:
            message = self._queue.get()
            if message is None:
                break
            try:
                self.agent.write(message)
                header = self.agent.readExact(4)
                length = unpack('>I', header)[0]
                if length > MAX_MESSAGE:
                    raise ValueError(u'%d-byte answer' % length)
                self.channel.write(header + self.agent.readExact(length))
            except (OSError, IOError, EOFError, ValueError) as e:
                log.warning(u'ssh agent: %s' % e)
                self.channel.close()
                break
        self.agent.close()

    def closed(self):
        self._queue.put(None)
        self.plugin._closed(self)
