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
Echo Virtual Channel Extension ("ECHO" dynamic channel, MS-RDPEECO): the
server measures the round trip time by sending data the client sends back
unchanged. Ported from FreeRDP channels/echo/client/echo_main.c.
"""
from __future__ import absolute_import, division, print_function

CHANNEL_NAME = u'ECHO'


class Echo(object):
    """
    @summary: The echo plugin (DrdynvcLayer listener); FreeRDP /echo.
    """

    def __init__(self):
        self.enabled = False
        self.echoed = 0  # messages sent back

    def channelFactory(self, channel):
        return None if not self.enabled else _EchoChannel(self, channel)

    def describe(self):
        return ('%d message(s) echoed' % self.echoed) if self.echoed else None


class _EchoChannel(object):
    def __init__(self, plugin, channel):
        self.plugin = plugin
        self.channel = channel

    def recv(self, data):
        # ECHO has no message ids: everything goes back as it came
        self.channel.write(bytes(data))
        self.plugin.echoed += 1

    def closed(self):
        """
        :return:
        """
