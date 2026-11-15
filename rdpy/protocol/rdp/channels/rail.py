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
RemoteApp: Remote Programs Virtual Channel Extension (MS-RDPERP, FreeRDP /app).
Instead of a whole desktop, the server runs applications and the client shows
each of their windows as a local window. The protocol has two parts:
* the "rail" static virtual channel (RailLayer): handshake, client status,
  system parameters, the start of the program (Client Execute), then window
  commands from the client (activate, system commands, moves, notification
  icon events) and information from the server (exec result, min / max
  sizes, local move / size, cloaking...);
* Windowing Alternate Secondary Drawing Orders in the ordinary update stream
  (WindowOrderParser): creation, update and deletion of windows, their
  icons, notification area icons and the desktop state. The windows' pixels
  are drawn on the session desktop as usual; the client cuts each window out
  of it (or gets its own surface from the graphics pipeline in "HiDef" mode).
Ported from FreeRDP channels/rail/client (rail_main.c, rail_orders.c,
client_rails.c), channels/rail/rail_common.c and libfreerdp/core/window.c.
Protocol reference: [MS-RDPERP]
"""
from __future__ import absolute_import, division, print_function
from struct import calcsize, pack, unpack_from
from threading import Lock
from rdpy.core.layer import LayerAutomata
from rdpy.core import log

CHANNEL_NAME = u'rail'
RAIL_PDU_HEADER_LENGTH = 4
RAIL_PDU_MAX_LENGTH = 0xFFFF
# Orders (MS-RDPERP 2.2.2.1)
TS_RAIL_ORDER_EXEC = 0x0001
TS_RAIL_ORDER_ACTIVATE = 0x0002
TS_RAIL_ORDER_SYSPARAM = 0x0003
TS_RAIL_ORDER_SYSCOMMAND = 0x0004
TS_RAIL_ORDER_HANDSHAKE = 0x0005
TS_RAIL_ORDER_NOTIFY_EVENT = 0x0006
TS_RAIL_ORDER_WINDOWMOVE = 0x0008
TS_RAIL_ORDER_LOCALMOVESIZE = 0x0009
TS_RAIL_ORDER_MINMAXINFO = 0x000A
TS_RAIL_ORDER_CLIENTSTATUS = 0x000B
TS_RAIL_ORDER_SYSMENU = 0x000C
TS_RAIL_ORDER_LANGBARINFO = 0x000D
TS_RAIL_ORDER_GET_APPID_REQ = 0x000E
TS_RAIL_ORDER_GET_APPID_RESP = 0x000F
TS_RAIL_ORDER_TASKBARINFO = 0x0010
TS_RAIL_ORDER_LANGUAGEIMEINFO = 0x0011
TS_RAIL_ORDER_COMPARTMENTINFO = 0x0012
TS_RAIL_ORDER_HANDSHAKE_EX = 0x0013
TS_RAIL_ORDER_ZORDER_SYNC = 0x0014
TS_RAIL_ORDER_CLOAK = 0x0015
TS_RAIL_ORDER_POWER_DISPLAY_REQUEST = 0x0016
TS_RAIL_ORDER_SNAP_ARRANGE = 0x0017
TS_RAIL_ORDER_GET_APPID_RESP_EX = 0x0018
TS_RAIL_ORDER_TEXTSCALEINFO = 0x0019
TS_RAIL_ORDER_CARETBLINKINFO = 0x001A
TS_RAIL_ORDER_EXEC_RESULT = 0x0080
ORDER_NAMES = dict((v, k[len('TS_RAIL_ORDER_'):]) for k, v in list(globals().items())
                   if k.startswith('TS_RAIL_ORDER_'))
# Exec flags (2.2.2.3.1)
TS_RAIL_EXEC_FLAG_EXPAND_WORKINGDIRECTORY = 0x0001
TS_RAIL_EXEC_FLAG_TRANSLATE_FILES = 0x0002
TS_RAIL_EXEC_FLAG_FILE = 0x0004
TS_RAIL_EXEC_FLAG_EXPAND_ARGUMENTS = 0x0008
TS_RAIL_EXEC_FLAG_APP_USER_MODEL_ID = 0x0010
# Exec results (2.2.2.3.2)
RAIL_EXEC_S_OK = 0x0000
EXEC_RESULTS = {
    0x0000: u'started',
    0x0001: u'the RemoteApp hook (rdpinit) is not loaded on the server',
    0x0002: u'the server could not decode the request',
    0x0003: u'the program is not in the server\'s allow list (publish it as a RemoteApp, or allow '
            u'unlisted programs)',
    0x0005: u'the program or file was not found on the server',
    0x0006: u'the server failed to start the program',
    0x0007: u'the session is locked',
}
# Client status flags (2.2.2.2.2)
TS_RAIL_CLIENTSTATUS_ALLOWLOCALMOVESIZE = 0x00000001
TS_RAIL_CLIENTSTATUS_AUTORECONNECT = 0x00000002
TS_RAIL_CLIENTSTATUS_ZORDER_SYNC = 0x00000004
TS_RAIL_CLIENTSTATUS_WINDOW_RESIZE_MARGIN_SUPPORTED = 0x00000010
TS_RAIL_CLIENTSTATUS_HIGH_DPI_ICONS_SUPPORTED = 0x00000020
TS_RAIL_CLIENTSTATUS_APPBAR_REMOTING_SUPPORTED = 0x00000040
TS_RAIL_CLIENTSTATUS_POWER_DISPLAY_REQUEST_SUPPORTED = 0x00000080
TS_RAIL_CLIENTSTATUS_GET_APPID_RESPONSE_EX_SUPPORTED = 0x00000100
TS_RAIL_CLIENTSTATUS_BIDIRECTIONAL_CLOAK_SUPPORTED = 0x00000200
# Handshake ex flags (2.2.2.2.3)
TS_RAIL_ORDER_HANDSHAKEEX_FLAGS_HIDEF = 0x00000001
TS_RAIL_ORDER_HANDSHAKE_EX_FLAGS_EXTENDED_SPI_SUPPORTED = 0x00000002
TS_RAIL_ORDER_HANDSHAKE_EX_FLAGS_SNAP_ARRANGE_SUPPORTED = 0x00000004
# System parameters (2.2.2.4.1)
SPI_SET_DRAG_FULL_WINDOWS = 0x00000025
SPI_SET_KEYBOARD_CUES = 0x0000100B
SPI_SET_KEYBOARD_PREF = 0x00000045
SPI_SET_MOUSE_BUTTON_SWAP = 0x00000021
SPI_SET_WORK_AREA = 0x0000002F
SPI_DISPLAY_CHANGE = 0x0000F001
SPI_TASKBAR_POS = 0x0000F000
SPI_SET_HIGH_CONTRAST = 0x00000043
SPI_SETSCREENSAVEACTIVE = 0x00000011
SPI_SETSCREENSAVESECURE = 0x00000077
# System commands (2.2.2.6.1)
SC_SIZE = 0xF000
SC_MOVE = 0xF010
SC_MINIMIZE = 0xF020
SC_MAXIMIZE = 0xF030
SC_CLOSE = 0xF060
SC_KEYMENU = 0xF100
SC_RESTORE = 0xF120
SC_DEFAULT = 0xF160
# Notification icon messages (2.2.2.6.3)
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401
NIN_BALLOONUSERCLICK = 0x0405
# Local move / size types (2.2.2.7.3)
RAIL_WMSZ_MOVE = 0x0009
RAIL_WMSZ_KEYMOVE = 0x000A
RAIL_WMSZ_KEYSIZE = 0x000B
# Language bar status: hidden
TF_SFT_HIDDEN = 0x00000008
# FreeRDP ClientBuild: Windows 10 1909
CLIENT_BUILD = 18363

# ---------------------------------------------------------------------------
# Windowing Alternate Secondary Drawing Orders (MS-RDPERP 2.2.1.3)
# ---------------------------------------------------------------------------
ORDER_STANDARD = 0x01
ORDER_SECONDARY = 0x02
ORDER_TYPE_WINDOW = 0x0B  # TS_ALTSEC_WINDOW
WINDOW_ORDER_TYPE_WINDOW = 0x01000000
WINDOW_ORDER_TYPE_NOTIFY = 0x02000000
WINDOW_ORDER_TYPE_DESKTOP = 0x04000000
WINDOW_ORDER_STATE_NEW = 0x10000000
WINDOW_ORDER_STATE_DELETED = 0x20000000
WINDOW_ORDER_FIELD_OWNER = 0x00000002
WINDOW_ORDER_FIELD_STYLE = 0x00000008
WINDOW_ORDER_FIELD_SHOW = 0x00000010
WINDOW_ORDER_FIELD_TITLE = 0x00000004
WINDOW_ORDER_FIELD_CLIENT_AREA_OFFSET = 0x00004000
WINDOW_ORDER_FIELD_CLIENT_AREA_SIZE = 0x00010000
WINDOW_ORDER_FIELD_RESIZE_MARGIN_X = 0x00000080
WINDOW_ORDER_FIELD_RESIZE_MARGIN_Y = 0x08000000
WINDOW_ORDER_FIELD_RP_CONTENT = 0x00020000
WINDOW_ORDER_FIELD_ROOT_PARENT = 0x00040000
WINDOW_ORDER_FIELD_WND_OFFSET = 0x00000800
WINDOW_ORDER_FIELD_WND_CLIENT_DELTA = 0x00008000
WINDOW_ORDER_FIELD_WND_SIZE = 0x00000400
WINDOW_ORDER_FIELD_WND_RECTS = 0x00000100
WINDOW_ORDER_FIELD_VIS_OFFSET = 0x00001000
WINDOW_ORDER_FIELD_VISIBILITY = 0x00000200
WINDOW_ORDER_FIELD_OVERLAY_DESCRIPTION = 0x00400000
WINDOW_ORDER_FIELD_ICON_OVERLAY_NULL = 0x00200000
WINDOW_ORDER_FIELD_TASKBAR_BUTTON = 0x00800000
WINDOW_ORDER_FIELD_ENFORCE_SERVER_ZORDER = 0x00080000
WINDOW_ORDER_FIELD_APPBAR_STATE = 0x00000040
WINDOW_ORDER_FIELD_APPBAR_EDGE = 0x00000001
WINDOW_ORDER_ICON = 0x40000000
WINDOW_ORDER_CACHED_ICON = 0x80000000
WINDOW_ORDER_FIELD_ICON_BIG = 0x00002000
WINDOW_ORDER_FIELD_ICON_OVERLAY = 0x00100000
WINDOW_ORDER_FIELD_NOTIFY_VERSION = 0x00000008
WINDOW_ORDER_FIELD_NOTIFY_TIP = 0x00000001
WINDOW_ORDER_FIELD_NOTIFY_INFO_TIP = 0x00000002
WINDOW_ORDER_FIELD_NOTIFY_STATE = 0x00000004
WINDOW_ORDER_FIELD_DESKTOP_NONE = 0x00000001
WINDOW_ORDER_FIELD_DESKTOP_HOOKED = 0x00000002
WINDOW_ORDER_FIELD_DESKTOP_ARC_COMPLETED = 0x00000004
WINDOW_ORDER_FIELD_DESKTOP_ARC_BEGAN = 0x00000008
WINDOW_ORDER_FIELD_DESKTOP_ZORDER = 0x00000010
WINDOW_ORDER_FIELD_DESKTOP_ACTIVE_WND = 0x00000020
# show states
WINDOW_HIDE = 0x00
WINDOW_SHOW_MINIMIZED = 0x02
WINDOW_SHOW_MAXIMIZED = 0x03
WINDOW_SHOW = 0x05
# window styles
WS_BORDER = 0x00800000
WS_CAPTION = 0x00C00000
WS_CHILD = 0x40000000
WS_DISABLED = 0x08000000
WS_DLGFRAME = 0x00400000
WS_MAXIMIZE = 0x01000000
WS_MINIMIZE = 0x20000000
WS_POPUP = 0x80000000
WS_SYSMENU = 0x00080000
WS_THICKFRAME = 0x00040000
WS_VISIBLE = 0x10000000
WS_EX_APPWINDOW = 0x00040000
WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020


class _Reader(object):
    """Little endian reader with bounds checks (ValueError on truncated data)."""

    def __init__(self, data, pos=0, end=None):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    def need(self, n):
        if self.pos + n > self.end:
            raise ValueError(u'truncated (need %d bytes at %d of %d)' % (n, self.pos, self.end))

    def unpack(self, fmt):
        size = calcsize(fmt)
        self.need(size)
        values = unpack_from('<' + fmt, self.data, self.pos)
        self.pos += size
        return values

    def u8(self):
        return self.unpack('B')[0]

    def u16(self):
        return self.unpack('H')[0]

    def i16(self):
        return self.unpack('h')[0]

    def u32(self):
        return self.unpack('I')[0]

    def i32(self):
        return self.unpack('i')[0]

    def bytes(self, n):
        self.need(n)
        value = bytes(self.data[self.pos:self.pos + n])
        self.pos += n
        return value

    def unicodeString(self):
        """TS_UNICODE_STRING: cbString (2 bytes) + UTF-16LE (rail_read_unicode_string)"""
        cb = self.u16()
        raw = self.bytes(cb)
        if cb % 2:
            raise ValueError(u'odd unicode string length %d' % cb)
        return raw.decode('utf-16-le', 'replace').rstrip('\0')

    def rects(self, count):
        self.need(8 * count)
        out = [unpack_from('<HHHH', self.data, self.pos + 8 * i) for i in range(count)]
        self.pos += 8 * count
        return out


def _rect16(left, top, right, bottom):
    """TS_RECTANGLE_16 (unsigned, FreeRDP RECTANGLE_16)"""
    return pack('<HHHH', *[min(max(int(v), 0), 0xFFFF) for v in (left, top, right, bottom)])


def encodeUnicode(text):
    """@return: {bytes} UTF-16LE without terminator (utf8_string_to_rail_string)"""
    return (text or '').encode('utf-16-le')


def _readIconInfo(r):
    """TS_ICON_INFO (MS-RDPERP 2.2.1.2.3, FreeRDP update_read_icon_info)"""
    cacheEntry, cacheId, bpp, width, height = r.unpack('HBBHH')
    if not 1 <= bpp <= 32:
        raise ValueError(u'icon bpp %d' % bpp)
    cbColorTable = r.u16() if bpp in (1, 4, 8) else 0
    cbBitsMask, cbBitsColor = r.unpack('HH')
    bitsMask = r.bytes(cbBitsMask)
    colorTable = r.bytes(cbColorTable)
    bitsColor = r.bytes(cbBitsColor)
    return {'cacheEntry': cacheEntry, 'cacheId': cacheId, 'bpp': bpp, 'width': width, 'height': height,
            'bitsMask': bitsMask, 'colorTable': colorTable, 'bitsColor': bitsColor}


def decodeIcon(icon):
    """
    @summary: TS_ICON_INFO -> 32-bit BGRA pixels, top-down (FreeRDP
    freerdp_image_copy_from_icon_data): bottom-up DIB rows, AND mask for transparency
    @return: {tuple|None} (width, height, bytes) or None when the data does not fit
    """
    from numpy import arange, frombuffer, uint16, uint8, unpackbits, where, zeros
    w, h, bpp = icon['width'], icon['height'], icon['bpp']
    color, mask = icon['bitsColor'], icon['bitsMask']
    if w <= 0 or h <= 0 or not color:
        return None
    stride = len(color) // h
    if stride * 8 < w * bpp:
        return None
    rows = frombuffer(color, uint8, count=stride * h).reshape(h, stride)[::-1]
    out = zeros((h, w, 4), uint8)
    if bpp == 32:
        out[...] = rows[:, :w * 4].reshape(h, w, 4)
    elif bpp == 24:
        out[..., :3] = rows[:, :w * 3].reshape(h, w, 3)
        out[..., 3] = 0xFF
    elif bpp == 16:
        v = rows[:, :w * 2].copy().view('<u2').reshape(h, w).astype(uint16)
        r, g, b = (v >> 10) & 0x1F, (v >> 5) & 0x1F, v & 0x1F  # DIB 16 bpp: X1R5G5B5
        out[..., 2] = ((r << 3) | (r >> 2)).astype(uint8)
        out[..., 1] = ((g << 3) | (g >> 2)).astype(uint8)
        out[..., 0] = ((b << 3) | (b >> 2)).astype(uint8)
        out[..., 3] = 0xFF
    elif bpp in (1, 4, 8):
        table = frombuffer(icon['colorTable'] + b'\0' * 1024, uint8, count=1024).reshape(256, 4)
        bits = unpackbits(rows, axis=1)[:, :w * bpp].reshape(h, w, bpp)
        weights = (1 << arange(bpp - 1, -1, -1)).astype(uint16)
        index = (bits.astype(uint16) * weights).sum(axis=2)
        out[..., :3] = table[index][..., :3]
        out[..., 3] = 0xFF
    else:
        return None
    if mask:
        mstride = len(mask) // h
        if mstride * 8 >= w:
            m = unpackbits(frombuffer(mask, uint8, count=mstride * h).reshape(h, mstride)[::-1], axis=1)[:, :w]
            if bpp != 32 or not out[..., 3].any():
                out[..., 3] = where(m.astype(bool), 0, 0xFF)
    elif bpp == 32 and not out[..., 3].any():
        out[..., 3] = 0xFF
    return w, h, out.tobytes()


class WindowOrderParser(object):
    """
    @summary: Windowing Alternate Secondary Drawing Orders of an Orders Update
    (FreeRDP update_recv_order / update_recv_altsec_window_order). The orders go to
    listener.onRailEvent(kind, ...):
        ('window', windowId, fields, isNew)    fields: {name: value} of the present fields
        ('windowDelete', windowId)
        ('windowIcon', windowId, iconInfo, isBig)
        ('windowCachedIcon', windowId, cacheEntry, cacheId, isBig)
        ('notifyIcon', windowId, notifyIconId, fields, isNew)
        ('notifyIconDelete', windowId, notifyIconId)
        ('desktop', fields)                    fields: hooked, arcBegan, arcCompleted,
                                               activeWindowId, zOrder
        ('desktopNone',)
    Only window orders are expected: no other drawing order is announced.
    """

    def __init__(self, listener):
        self._listener = listener
        self._warned = False

    def _emit(self, *event):
        self._listener(*event)

    def parseOrders(self, data, offset, numberOrders):
        """
        @param data: {bytes} update data
        @param offset: {int} first order
        @param numberOrders: {int}
        @return: {int} orders parsed
        """
        done = 0
        for _ in range(numberOrders):
            if offset >= len(data):
                break
            controlFlags = data[offset]
            if (controlFlags & 0x03) != ORDER_SECONDARY or (controlFlags >> 2) != ORDER_TYPE_WINDOW:
                # a drawing order: its length is not known without decoding it
                if not self._warned:
                    self._warned = True
                    log.warning(u'RemoteApp: drawing order 0x%02x in the update stream not supported, '
                                u'the rest of the update is skipped' % controlFlags)
                break
            if offset + 7 > len(data):
                break
            orderSize, fieldFlags = unpack_from('<HI', data, offset + 1)
            if orderSize < 7 or offset + orderSize > len(data):
                log.warning(u'RemoteApp: window order of %d bytes truncated' % orderSize)
                break
            try:
                self._parseWindowOrder(_Reader(data, offset + 7, offset + orderSize), fieldFlags)
            except ValueError as e:
                log.warning(u'RemoteApp: bad window order 0x%08x: %s' % (fieldFlags, e))
            offset += orderSize
            done += 1
        return done

    def _parseWindowOrder(self, r, flags):
        if flags & WINDOW_ORDER_TYPE_WINDOW:
            windowId = r.u32()
            big = bool(flags & WINDOW_ORDER_FIELD_ICON_BIG)
            if flags & WINDOW_ORDER_ICON:
                self._emit('windowIcon', windowId, _readIconInfo(r), big)
            elif flags & WINDOW_ORDER_CACHED_ICON:
                cacheEntry, cacheId = r.unpack('HB')
                self._emit('windowCachedIcon', windowId, cacheEntry, cacheId, big)
            elif flags & WINDOW_ORDER_STATE_DELETED:
                self._emit('windowDelete', windowId)
            else:
                self._emit('window', windowId, self._readWindowState(r, flags),
                           bool(flags & WINDOW_ORDER_STATE_NEW))
        elif flags & WINDOW_ORDER_TYPE_NOTIFY:
            windowId, iconId = r.unpack('II')
            if flags & WINDOW_ORDER_STATE_DELETED:
                self._emit('notifyIconDelete', windowId, iconId)
            else:
                self._emit('notifyIcon', windowId, iconId, self._readNotifyState(r, flags),
                           bool(flags & WINDOW_ORDER_STATE_NEW))
        elif flags & WINDOW_ORDER_TYPE_DESKTOP:
            if flags & WINDOW_ORDER_FIELD_DESKTOP_NONE:
                self._emit('desktopNone')
                return
            fields = {'hooked': bool(flags & WINDOW_ORDER_FIELD_DESKTOP_HOOKED),
                      'arcBegan': bool(flags & WINDOW_ORDER_FIELD_DESKTOP_ARC_BEGAN),
                      'arcCompleted': bool(flags & WINDOW_ORDER_FIELD_DESKTOP_ARC_COMPLETED)}
            if flags & WINDOW_ORDER_FIELD_DESKTOP_ACTIVE_WND:
                fields['activeWindowId'] = r.u32()
            if flags & WINDOW_ORDER_FIELD_DESKTOP_ZORDER:
                count = r.u8()
                fields['zOrder'] = list(r.unpack('%dI' % count)) if count else []
            self._emit('desktop', fields)

    @staticmethod
    def _readWindowState(r, flags):
        """TS_WINDOW_INFO fields (FreeRDP update_read_window_state_order)"""
        f = {}
        if flags & WINDOW_ORDER_FIELD_OWNER:
            f['owner'] = r.u32()
        if flags & WINDOW_ORDER_FIELD_STYLE:
            f['style'], f['exStyle'] = r.unpack('II')
        if flags & WINDOW_ORDER_FIELD_SHOW:
            f['showState'] = r.u8()
        if flags & WINDOW_ORDER_FIELD_TITLE:
            f['title'] = r.unicodeString()
        if flags & WINDOW_ORDER_FIELD_CLIENT_AREA_OFFSET:
            f['clientOffset'] = r.unpack('ii')
        if flags & WINDOW_ORDER_FIELD_CLIENT_AREA_SIZE:
            f['clientSize'] = r.unpack('II')
        if flags & WINDOW_ORDER_FIELD_RESIZE_MARGIN_X:
            f['resizeMarginX'] = r.unpack('II')  # left, right
        if flags & WINDOW_ORDER_FIELD_RESIZE_MARGIN_Y:
            f['resizeMarginY'] = r.unpack('II')  # top, bottom
        if flags & WINDOW_ORDER_FIELD_RP_CONTENT:
            f['rpContent'] = r.u8()
        if flags & WINDOW_ORDER_FIELD_ROOT_PARENT:
            f['rootParent'] = r.u32()
        if flags & WINDOW_ORDER_FIELD_WND_OFFSET:
            f['windowOffset'] = r.unpack('ii')
        if flags & WINDOW_ORDER_FIELD_WND_CLIENT_DELTA:
            f['windowClientDelta'] = r.unpack('ii')
        if flags & WINDOW_ORDER_FIELD_WND_SIZE:
            f['windowSize'] = r.unpack('II')
        if flags & WINDOW_ORDER_FIELD_WND_RECTS:
            f['windowRects'] = r.rects(r.u16())
        if flags & WINDOW_ORDER_FIELD_VIS_OFFSET:
            f['visibleOffset'] = r.unpack('ii')
        if flags & WINDOW_ORDER_FIELD_VISIBILITY:
            f['visibilityRects'] = r.rects(r.u16())
        if flags & WINDOW_ORDER_FIELD_OVERLAY_DESCRIPTION:
            f['overlayDescription'] = r.unicodeString()
        if flags & WINDOW_ORDER_FIELD_ICON_OVERLAY_NULL:
            f['iconOverlayNull'] = True
        if flags & WINDOW_ORDER_FIELD_TASKBAR_BUTTON:
            f['taskbarButton'] = r.u8()
        if flags & WINDOW_ORDER_FIELD_ENFORCE_SERVER_ZORDER:
            f['enforceServerZOrder'] = r.u8()
        if flags & WINDOW_ORDER_FIELD_APPBAR_STATE:
            f['appBarState'] = r.u8()
        if flags & WINDOW_ORDER_FIELD_APPBAR_EDGE:
            f['appBarEdge'] = r.u8()
        return f

    @staticmethod
    def _readNotifyState(r, flags):
        """TS_NOTIFYICON fields (FreeRDP update_read_notification_icon_state_order)"""
        f = {}
        if flags & WINDOW_ORDER_FIELD_NOTIFY_VERSION:
            f['version'] = r.u32()
        if flags & WINDOW_ORDER_FIELD_NOTIFY_TIP:
            f['toolTip'] = r.unicodeString()
        if flags & WINDOW_ORDER_FIELD_NOTIFY_INFO_TIP:
            timeout, infoFlags = r.unpack('II')
            f['infoTip'] = {'timeout': timeout, 'flags': infoFlags, 'text': r.unicodeString(),
                            'title': r.unicodeString()}
        if flags & WINDOW_ORDER_FIELD_NOTIFY_STATE:
            f['state'] = r.u32()
        if flags & WINDOW_ORDER_ICON:
            f['icon'] = _readIconInfo(r)
        if flags & WINDOW_ORDER_CACHED_ICON:
            f['cachedIcon'] = r.unpack('HB')
        return f


class RemoteApp(object):
    """
    @summary: The program the session starts (FreeRDP /app: program, cmd, workdir).
    """

    def __init__(self, program, arguments='', workingDir='', flags=0):
        if not program:
            raise ValueError(u'RemoteApp: no program')
        self.program = program
        self.arguments = arguments or ''
        self.workingDir = workingDir or ''
        self.flags = flags
        for name, value, limit in (('program', self.program, 520), ('working directory', self.workingDir, 520),
                                   ('arguments', self.arguments, 16000)):
            if len(encodeUnicode(value)) > limit:
                raise ValueError(u'RemoteApp: %s longer than %d bytes' % (name, limit))


def parseAppSpec(spec):
    """
    @summary: FreeRDP /app syntax: "program:||notepad,cmd:file.txt,workdir:C:\\temp"
    or a bare program ("||notepad", "C:\\Windows\\notepad.exe")
    @return: {RemoteApp}
    @raise ValueError: no program
    """
    spec = (spec or '').strip()
    keys = ('program:', 'cmd:', 'workdir:', 'name:', 'icon:', 'file:', 'guid:', 'hidef:')
    if not any(spec.startswith(k) for k in keys):
        return RemoteApp(spec)
    values = {}
    current = None
    for part in spec.split(','):
        key = next((k for k in keys if part.startswith(k)), None)
        if key is not None:
            current = key[:-1]
            values[current] = part[len(key):]
        elif current is not None:
            values[current] += ',' + part  # a comma inside a value
    arguments = values.get('cmd', '')
    if values.get('file'):
        arguments = (arguments + ' ' + values['file']).strip()
    app = RemoteApp(values.get('program', ''), arguments, values.get('workdir', ''))
    if 'hidef' in values:
        app.hidef = values['hidef'].lower() in ('on', '1', 'true', 'yes')
    return app


class RailLayer(LayerAutomata):
    """
    @summary: The "rail" static virtual channel (FreeRDP railPlugin). Events go to
    the listener (RDPClientController.onRailEvent): ('handshake', build, flags),
    ('execResult', flags, result, rawResult, exeOrFile, message), ('minMaxInfo', windowId,
    values), ('localMoveSize', windowId, isStart, moveSizeType, x, y), ('cloak', windowId,
    cloaked), ('appId', windowId, appId), ('started',).
    Commands (activate, sysCommand...) may be called from any thread.
    """

    def __init__(self):
        LayerAutomata.__init__(self, None)
        self.app = None
        self.clientStatus = TS_RAIL_CLIENTSTATUS_WINDOW_RESIZE_MARGIN_SUPPORTED
        self.workArea = None  # (left, top, right, bottom), default: the whole desktop
        self.desktopSize = (0, 0)
        self.serverBuild = 0
        self.handshakeFlags = 0
        self.handshakeDone = False
        self.started = False
        self._listener = None
        self._buf = bytearray()
        self._total = 0
        self._startTimer = None
        self._lock = Lock()

    def setListener(self, listener):
        self._listener = listener

    def _event(self, *event):
        if self._listener is not None:
            try:
                self._listener(*event)
            except Exception as e:
                log.warning(u'RemoteApp: event %s not handled: %r' % (event[0], e))

    # ---- channel ----

    def connect(self):
        log.debug(u'RemoteApp: rail channel joined')

    def close(self):
        self._cancelStartTimer()

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
            buf, self._buf = bytes(self._buf), bytearray()
            offset = 0
            while offset + 4 <= len(buf):  # one PDU per chunk, but be tolerant
                orderType, orderLength = unpack_from('<HH', buf, offset)
                if orderLength < RAIL_PDU_HEADER_LENGTH or offset + orderLength > len(buf):
                    log.warning(u'RemoteApp: bad PDU length %d (type 0x%x)' % (orderLength, orderType))
                    break
                try:
                    self._recvPdu(orderType, _Reader(buf, offset + 4, offset + orderLength))
                except ValueError as e:
                    log.warning(u'RemoteApp: bad %s PDU: %s' % (ORDER_NAMES.get(orderType, hex(orderType)), e))
                offset += orderLength

    def _send(self, orderType, body=b''):
        pdu = pack('<HH', orderType, RAIL_PDU_HEADER_LENGTH + len(body)) + body
        from rdpy.protocol.rdp.channels.drdynvc import callInReactor
        callInReactor(self._sendPdu, pdu)

    def _sendPdu(self, pdu):
        if self._transport is None:
            return
        from rdpy.core.type import String
        chunk = 1600
        for offset in range(0, len(pdu), chunk):
            # FIRST / LAST, SHOW_PROTOCOL like FreeRDP (the channel has CHANNEL_OPTION_SHOW_PROTOCOL)
            flags = (0x01 if offset == 0 else 0) | (0x02 if offset + chunk >= len(pdu) else 0) | 0x10
            self._transport.send(String(pack('<II', len(pdu), flags) + pdu[offset:offset + chunk]))

    # ---- server PDUs ----

    def _recvPdu(self, orderType, r):
        if orderType == TS_RAIL_ORDER_HANDSHAKE:
            self._onHandshake(r.u32(), 0)
        elif orderType == TS_RAIL_ORDER_HANDSHAKE_EX:
            self._onHandshake(*r.unpack('II'))
        elif orderType == TS_RAIL_ORDER_EXEC_RESULT:
            flags, result, raw = r.unpack('HHI')
            r.u16()  # padding
            exe = r.unicodeString()
            message = EXEC_RESULTS.get(result, u'error %d' % result)
            if result == RAIL_EXEC_S_OK:
                log.info(u'RemoteApp: %s started' % (exe or self.app.program if self.app else exe))
            else:
                log.error(u'RemoteApp: %s not started: %s (0x%08x)' % (exe, message, raw))
            self._event('execResult', flags, result, raw, exe, message)
        elif orderType == TS_RAIL_ORDER_SYSPARAM:
            param = r.u32()
            log.debug(u'RemoteApp: server system parameter 0x%x' % param)
        elif orderType == TS_RAIL_ORDER_MINMAXINFO:
            values = r.unpack('Ihhhhhhhh')
            self._event('minMaxInfo', values[0], values[1:])
        elif orderType == TS_RAIL_ORDER_LOCALMOVESIZE:
            windowId, start, kind, x, y = r.unpack('IHHhh')
            self._event('localMoveSize', windowId, bool(start), kind, x, y)
        elif orderType == TS_RAIL_ORDER_CLOAK:
            windowId, cloak = r.unpack('IB')
            self._event('cloak', windowId, bool(cloak))
        elif orderType in (TS_RAIL_ORDER_GET_APPID_RESP, TS_RAIL_ORDER_GET_APPID_RESP_EX):
            windowId = r.u32()
            appId = r.bytes(520).decode('utf-16-le', 'replace').split('\0', 1)[0]
            self._event('appId', windowId, appId)
        elif orderType == TS_RAIL_ORDER_ZORDER_SYNC:
            log.debug(u'RemoteApp: z-order marker window 0x%x' % r.u32())
        else:
            log.debug(u'RemoteApp: %s PDU ignored' % ORDER_NAMES.get(orderType, hex(orderType)))

    def _onHandshake(self, build, flags):
        """FreeRDP rail_server_handshake: client handshake + client status"""
        self.serverBuild, self.handshakeFlags = build, flags
        log.info(u'RemoteApp: server build %d%s' % (build, u' (HiDef)' if flags & TS_RAIL_ORDER_HANDSHAKEEX_FLAGS_HIDEF
                                                    else u''))
        self._send(TS_RAIL_ORDER_HANDSHAKE, pack('<I', CLIENT_BUILD))
        self._send(TS_RAIL_ORDER_CLIENTSTATUS, pack('<I', self.clientStatus))
        self.handshakeDone = True
        self._event('handshake', build, flags)
        # FreeRDP starts the program when the server finished describing the existing
        # windows (desktop order ARC_COMPLETED); older servers / FreeRDP 2 start right
        # after the handshake: do that if ARC_COMPLETED does not come
        from twisted.internet import reactor
        self._cancelStartTimer()
        self._startTimer = reactor.callLater(2.0, self.start)

    def _cancelStartTimer(self):
        if self._startTimer is not None:
            try:
                if self._startTimer.active():
                    self._startTimer.cancel()
            except Exception:
                pass
            self._startTimer = None

    def onDesktopOrder(self, fields):
        """@summary: Desktop window order (network thread): start on ARC_COMPLETED"""
        if fields.get('arcCompleted') and self.handshakeDone:
            self.start()

    def start(self):
        """
        @summary: Client system parameters + Client Execute (FreeRDP
        client_rail_server_start_cmd), once per connection
        """
        self._cancelStartTimer()
        with self._lock:
            if self.started or self.app is None:
                return
            self.started = True
        width, height = self.desktopSize
        left, top, right, bottom = self.workArea or (0, 0, width, height)
        # high contrast off, primary button left, no keyboard preference, windows dragged
        # with their content (moves are done by the server), no keyboard cues, work area
        self.sendSysParam(SPI_SET_HIGH_CONTRAST, pack('<II', 0x7E, 2) + pack('<H', 0))
        self.sendSysParam(SPI_SET_MOUSE_BUTTON_SWAP, b'\0')
        self.sendSysParam(SPI_SET_KEYBOARD_PREF, b'\0')
        self.sendSysParam(SPI_SET_DRAG_FULL_WINDOWS, b'\1')
        self.sendSysParam(SPI_SET_KEYBOARD_CUES, b'\0')
        self.sendSysParam(SPI_SET_WORK_AREA, _rect16(left, top, right, bottom))
        app = self.app
        exe, workdir, args = encodeUnicode(app.program), encodeUnicode(app.workingDir), encodeUnicode(app.arguments)
        self._send(TS_RAIL_ORDER_EXEC, pack('<HHHH', app.flags, len(exe), len(workdir), len(args)) +
                   exe + workdir + args)
        log.info(u'RemoteApp: starting %s%s' % (app.program, (u' ' + app.arguments) if app.arguments else u''))
        self._event('started')

    # ---- client commands (any thread) ----

    def sendSysParam(self, param, body):
        self._send(TS_RAIL_ORDER_SYSPARAM, pack('<I', param) + body)

    def setWorkArea(self, left, top, right, bottom):
        """@summary: The local work area changed (desktop coordinates)"""
        self.workArea = (left, top, right, bottom)
        if self.started:
            self.sendSysParam(SPI_SET_WORK_AREA, _rect16(left, top, right, bottom))

    def activate(self, windowId, enabled=True):
        self._send(TS_RAIL_ORDER_ACTIVATE, pack('<IB', windowId, 1 if enabled else 0))

    def sysCommand(self, windowId, command):
        self._send(TS_RAIL_ORDER_SYSCOMMAND, pack('<IH', windowId, command))

    def sysMenu(self, windowId, x, y):
        self._send(TS_RAIL_ORDER_SYSMENU, pack('<Ihh', windowId, x, y))

    def notifyEvent(self, windowId, iconId, message):
        self._send(TS_RAIL_ORDER_NOTIFY_EVENT, pack('<III', windowId, iconId, message))

    def windowMove(self, windowId, left, top, right, bottom):
        self._send(TS_RAIL_ORDER_WINDOWMOVE, pack('<Ihhhh', windowId, left, top, right, bottom))

    def getAppId(self, windowId):
        self._send(TS_RAIL_ORDER_GET_APPID_REQ, pack('<I', windowId))
