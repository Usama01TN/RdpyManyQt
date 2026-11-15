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
rdp2tcp static virtual channel: TCP forwarding through the RDP connection with
the rdp2tcp tool (https://github.com/V-E-O/rdp2tcp). The client starts the
local rdp2tcp program (FreeRDP /rdp2tcp:<command>) and relays the channel to its
standard input / output; rdp2tcp.exe runs in the session.
Ported from FreeRDP channels/rdp2tcp/client/rdp2tcp_main.c: data from the
server goes to the program's standard input, each channel message preceded by
its total length (UINT32, little endian) when its first chunk arrives; what the
program writes on its standard output is sent on the channel as it comes.
"""
from __future__ import absolute_import, division, print_function
from struct import pack, unpack_from
from subprocess import PIPE, Popen
from threading import Lock, Thread
from sys import platform
from shlex import split
from os import read
from rdpy.core.layer import LayerAutomata
from rdpy.core import log

CHANNEL_NAME = u'rdp2tcp'
CHANNEL_FLAG_FIRST = 0x01
CHANNEL_FLAG_LAST = 0x02
CHANNEL_FLAG_SUSPEND = 0x20
CHANNEL_FLAG_RESUME = 0x40
READ_SIZE = 16 * 1024


class Rdp2tcpLayer(LayerAutomata):
    """@summary: The rdp2tcp static channel and its external program"""

    def __init__(self):
        LayerAutomata.__init__(self, None)
        self.command = None
        self.process = None
        self._reader = None
        self._lock = Lock()

    def setCommand(self, command):
        """@param command: {str} the local rdp2tcp program with its arguments"""
        self.command = command

    def connect(self):
        """Channel joined: start the program (FreeRDP init_external_addin)."""
        if not self.command or self.process is not None:
            return
        args = self.command if platform == 'win32' else split(self.command)
        try:
            self.process = Popen(args, stdin=PIPE, stdout=PIPE, bufsize=0)
        except (OSError, ValueError) as e:
            log.warning(u'rdp2tcp: cannot start %r: %s' % (self.command, e))
            self.process = None
            return
        log.info(u'rdp2tcp: %s started' % self.command)
        self._reader = Thread(target=self._copyThread, args=(self.process,), name=u'rdp2tcp-copy')
        self._reader.daemon = True
        self._reader.start()

    def recv(self, s):
        """Server data -> the program's standard input (FreeRDP dataReceived)."""
        data = s.read()
        if len(data) < 8:
            return
        total, flags = unpack_from('<II', data, 0)
        if flags & (CHANNEL_FLAG_SUSPEND | CHANNEL_FLAG_RESUME):
            return
        process = self.process
        if process is None or process.stdin is None:
            return
        try:
            if flags & CHANNEL_FLAG_FIRST:
                process.stdin.write(pack('<I', total))
            process.stdin.write(data[8:])
            process.stdin.flush()
        except (OSError, IOError, ValueError) as e:
            log.warning(u'rdp2tcp: the program does not read its input: %s' % e)

    def _copyThread(self, process):
        """The program's standard output -> the channel (FreeRDP copyThread)."""
        fd = process.stdout.fileno()
        while True:
            try:
                chunk = read(fd, READ_SIZE)
            except OSError:
                break
            if not chunk:
                break
            from rdpy.protocol.rdp.channels.drdynvc import callInReactor
            callInReactor(self._send, chunk)
        log.info(u'rdp2tcp: the program ended')

    def _send(self, data):
        if self._transport is None:
            return
        from rdpy.core.type import String
        chunk = 1600
        for offset in range(0, len(data), chunk):
            flags = (CHANNEL_FLAG_FIRST if offset == 0 else 0) | \
                    (CHANNEL_FLAG_LAST if offset + chunk >= len(data) else 0)
            self._transport.send(String(pack('<II', len(data), flags) + data[offset:offset + chunk]))

    def close(self):
        """Disconnection: stop the program (FreeRDP channel_terminated)."""
        process, self.process = self.process, None
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, IOError):
                pass
        try:
            process.terminate()
        except OSError:
            pass

    def describe(self):
        return ('Running: %s' % self.command) if self.process is not None and self.process.poll() is None else None
