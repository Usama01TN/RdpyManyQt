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
RemoteApp windows in Qt (FreeRDP client/X11/xf_rail.c).
The server draws the windows of the remote program on the session desktop
(the hidden QRemoteDesktop frame buffer). Each remote window becomes a
frameless local top-level window at the same place on the local screens,
showing its part of the frame buffer; its title bar and borders are drawn by
the server, so moving, resizing, maximizing and closing are done by the server
from the mouse input (windows are dragged with their content). In Enhanced
RemoteApp (HiDef) a window can also get its own graphics pipeline surface.
Desktop coordinates: the session desktop covers the local screens, its (0, 0)
being the top left corner of the virtual screen. On Wayland, where windows can
not be placed, the input of a window is translated with its own position.
"""
from __future__ import absolute_import, division, print_function
from ManyQt.QtCore import Qt, QObject, QTimer, QRect, QRectF, QEvent, pyqtSignal, pyqtSlot
from ManyQt.QtGui import QImage, QPainter, QColor, QRegion, QIcon, QPixmap, QCursor
from ManyQt.QtWidgets import QApplication, QWidget, QMessageBox
from rdpy.protocol.rdp.channels import rail
from rdpy.core import log

try:
    from ManyQt.QtWidgets import QSystemTrayIcon
except:  # pragma: no cover - every binding has it.
    QSystemTrayIcon = None


def _globalPos(event):
    return event.globalPosition().toPoint() if hasattr(event, 'globalPosition') else event.globalPos()


def _localPos(event):
    return event.position().toPoint() if hasattr(event, 'position') else event.pos()


def localDesktopGeometry():
    """
    @summary: The session desktop of a RemoteApp session: the local virtual screen
    (FreeRDP /app uses the monitors' area)
    @return: {tuple} (QRect virtual screen, QRect work area of the primary screen)
    """
    screen = QApplication.primaryScreen()
    if screen is None:
        return QRect(0, 0, 1280, 800), QRect(0, 0, 1280, 800)
    return screen.virtualGeometry(), screen.availableGeometry()


class RailWindow(QWidget):
    """
    One remote window (FreeRDP xfAppWindow).
    """

    def __init__(self, manager, windowId, *args, **kwargs):
        super(RailWindow, self).__init__(*args, **kwargs)
        self.windowId = windowId
        self._m = manager
        self.owner = 0
        self.style = 0
        self.exStyle = 0
        self.showState = None
        self.offset = (0, 0)
        self.wndSize = (0, 0)
        self.visibleOffset = None
        self.visibility = None
        self.margins = (0, 0, 0, 0)  # left, top, right, bottom resize margins (not shown)
        self.surface = None  # QImage of a window mapped surface (HiDef)
        self.mapped = (0, 0)
        self.cloaked = False
        self.serverMinimized = False
        self._kind = None
        self._applying = 0
        self._bigIcon = False
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(False)

    # ---- state from the window orders ----

    def _windowKind(self):
        """
        normal (taskbar entry), tool (owned / popup / tool window) or popup (no focus)
        """
        if self.exStyle & rail.WS_EX_APPWINDOW:
            return 'normal'
        if self.style & rail.WS_POPUP and not self.style & rail.WS_CAPTION:
            return 'popup'
        if self.owner or self.exStyle & rail.WS_EX_TOOLWINDOW:
            return 'tool'
        return 'normal'

    def _applyFlags(self):
        kind = self._windowKind()
        topmost = bool(self.exStyle & rail.WS_EX_TOPMOST)
        if (kind, topmost) == self._kind:
            return False
        self._kind = (kind, topmost)
        flags = Qt.WindowType.FramelessWindowHint
        if kind == 'normal':
            flags |= Qt.WindowType.Window
        else:
            flags |= Qt.WindowType.Tool
        if kind == 'popup' or self.exStyle & rail.WS_EX_NOACTIVATE:
            flags |= Qt.WindowType.WindowDoesNotAcceptFocus
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        if topmost:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        visible = self.isVisible()
        self.setWindowFlags(flags)  # hides the window
        return visible

    def shownRect(self):
        """
        @return: {QRect} the part of the window shown, in desktop coordinates.
        """
        ml, mt, mr, mb = self.margins
        w, h = self.wndSize
        return QRect(self.offset[0] + ml, self.offset[1] + mt, max(1, w - ml - mr), max(1, h - mt - mb))

    def apply(self, fields, isNew):
        self._applying += 1
        try:
            self._apply(fields, isNew)
        finally:
            self._applying -= 1

    def _apply(self, fields, isNew):
        reshow = False
        if 'owner' in fields:
            self.owner = fields['owner']
        if 'style' in fields:
            self.style, self.exStyle = fields['style'], fields['exStyle']
        if isNew or 'owner' in fields or 'style' in fields:
            reshow = self._applyFlags()
        if 'title' in fields:
            self.setWindowTitle(fields['title'] or self._m.appName)
        elif isNew:
            self.setWindowTitle(self._m.appName)
        geometry = False
        if 'windowOffset' in fields:
            self.offset = fields['windowOffset']
            geometry = True
        if 'windowSize' in fields:
            self.wndSize = fields['windowSize']
            geometry = True
        if 'resizeMarginX' in fields or 'resizeMarginY' in fields:
            mx = fields.get('resizeMarginX', (self.margins[0], self.margins[2]))
            my = fields.get('resizeMarginY', (self.margins[1], self.margins[3]))
            self.margins = (mx[0], my[0], mx[1], my[1])
            geometry = True
        if geometry:
            self._m.placeWindow(self)
        if 'visibleOffset' in fields:
            self.visibleOffset = fields['visibleOffset']
        if 'visibilityRects' in fields:
            self.visibility = fields['visibilityRects']
        if geometry or 'visibleOffset' in fields or 'visibilityRects' in fields:
            self._applyMask()
        if 'showState' in fields:
            self.showState = fields['showState']
        if 'showState' in fields or reshow or isNew:
            self._applyShowState()
        if geometry:
            self.update()

    def _applyMask(self):
        """Window shape from the visibility rectangles (FreeRDP xf_SetWindowVisibilityRects)."""
        if not self.visibility:
            self.clearMask()
            return
        vx, vy = self.visibleOffset if self.visibleOffset is not None else self.offset
        shown = self.shownRect()
        dx, dy = vx - shown.x(), vy - shown.y()
        region = QRegion()
        for left, top, right, bottom in self.visibility:
            if right > left and bottom > top:
                region = region.united(QRegion(QRect(left + dx, top + dy, right - left, bottom - top)))
        if region.isEmpty():
            self.clearMask()
        else:
            self.setMask(region)

    def _visibleByServer(self):
        if self.cloaked:
            return False
        if self.showState is not None:
            return self.showState != rail.WINDOW_HIDE
        return bool(self.style & rail.WS_VISIBLE)

    def _applyShowState(self):
        if not self._visibleByServer():
            self.hide()
            return
        if self.showState == rail.WINDOW_SHOW_MINIMIZED:
            self.serverMinimized = True
            if not self.isMinimized():
                self.showMinimized()
            return
        self.serverMinimized = False
        if self.isMinimized():
            self.showNormal()
        elif not self.isVisible():
            self.show()
        self._m.placeWindow(self)  # the window manager may have moved a window it showed

    def setCloaked(self, cloaked):
        self.cloaked = cloaked
        self._applying += 1
        try:
            self._applyShowState()
        finally:
            self._applying -= 1

    # ---- painting ----

    def paintEvent(self, event):
        rect = event.rect()
        with QPainter(self) as qp:
            if self.surface is not None:
                ml, mt = self.margins[0], self.margins[1]
                mw, mh = self.mapped
                qp.drawImage(QRectF(-ml, -mt, mw or self.surface.width(), mh or self.surface.height()),
                             self.surface, QRectF(self.surface.rect()))
                return
            fb = self._m.frameBuffer()
            shown = self.shownRect()
            if fb is None or fb.isNull():
                qp.fillRect(rect, QColor(0, 0, 0))
                return
            src = rect.translated(shown.x(), shown.y())
            inside = src.intersected(fb.rect())
            if inside != src:
                qp.fillRect(rect, QColor(0, 0, 0))
            if not inside.isEmpty():
                qp.drawImage(inside.translated(-shown.x(), -shown.y()), fb, inside)

    def updateSurface(self, x, y, width, height, bgra, surfaceWidth, surfaceHeight, mappedWidth, mappedHeight):
        """HiDef: a part of the window's own surface (GUI thread), shown scaled to the mapped size."""
        if self.surface is None or (self.surface.width(), self.surface.height()) != (surfaceWidth, surfaceHeight):
            self.surface = QImage(max(1, surfaceWidth), max(1, surfaceHeight), QImage.Format.Format_RGB32)
            self.surface.fill(QColor(0, 0, 0))
        self.mapped = (mappedWidth, mappedHeight)
        # XRGB surfaces: the alpha byte is not meaningful
        image = QImage(bgra.data, width, height, width * 4, QImage.Format.Format_RGB32)
        with QPainter(self.surface) as qp:
            qp.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            qp.drawImage(x, y, image)
        sx = float(mappedWidth or surfaceWidth) / max(1, surfaceWidth)
        sy = float(mappedHeight or surfaceHeight) / max(1, surfaceHeight)
        self.update(QRectF((x * sx) - self.margins[0], (y * sy) - self.margins[1], width * sx, height * sy)
                    .toAlignedRect().adjusted(-1, -1, 1, 1))

    # ---- input ----

    def desktopPos(self, event):
        if self._m.globalMapping:
            p = _globalPos(event) - self._m.origin
            return p.x(), p.y()
        p = _localPos(event)
        shown = self.shownRect()
        return shown.x() + p.x(), shown.y() + p.y()

    def mousePressEvent(self, event):
        x, y = self.desktopPos(event)
        self._m.sendButton(x, y, event.button(), True)

    def mouseReleaseEvent(self, event):
        x, y = self.desktopPos(event)
        self._m.sendButton(x, y, event.button(), False)

    def mouseDoubleClickEvent(self, event):
        # Windows makes the double click from two clicks
        self.mousePressEvent(event)

    def mouseMoveEvent(self, event):
        self._m.queueMove(*self.desktopPos(event))

    def wheelEvent(self, event):
        x, y = self.desktopPos(event)
        self._m.sendWheel(x, y, event.angleDelta())

    def keyPressEvent(self, event):
        self._m.sendKey(event, True)

    def keyReleaseEvent(self, event):
        self._m.sendKey(event, False)

    def focusNextPrevChild(self, forward):
        return False  # Tab goes to the remote program

    # ---- window manager actions ----

    def changeEvent(self, event):
        QWidget.changeEvent(self, event)
        if self._applying or self._m.closing:
            return
        kind = event.type()
        if kind == QEvent.Type.ActivationChange and self.isActiveWindow():
            self._m.rail.activate(self.windowId, True)
        elif kind == QEvent.Type.WindowStateChange:
            if self.isMinimized() and not self.serverMinimized:
                self.serverMinimized = True
                self._m.rail.sysCommand(self.windowId, rail.SC_MINIMIZE)
            elif not self.isMinimized() and self.serverMinimized:
                self.serverMinimized = False
                self._m.rail.sysCommand(self.windowId, rail.SC_RESTORE)

    def closeEvent(self, event):
        if self._m.closing:
            event.accept()
            return
        # the taskbar / window manager asked to close it: the program decides
        event.ignore()
        self._m.rail.sysCommand(self.windowId, rail.SC_CLOSE)


class RailTrayIcon(object):
    """
    A notification area icon of the remote program (QSystemTrayIcon).
    """

    def __init__(self, manager, windowId, iconId):
        self._m = manager
        self.windowId, self.iconId = windowId, iconId
        self.version = 0
        self.tray = QSystemTrayIcon()  # type: QSystemTrayIcon
        self.tray.activated.connect(self._onActivated)
        if hasattr(self.tray, 'messageClicked'):
            self.tray.messageClicked.connect(
                lambda: self._m.rail.notifyEvent(self.windowId, self.iconId, rail.NIN_BALLOONUSERCLICK))

    def apply(self, fields):
        if 'version' in fields:
            self.version = fields['version']
        if 'toolTip' in fields:
            self.tray.setToolTip(fields['toolTip'])
        image = None
        if 'icon' in fields:
            image = self._m.iconImage(fields['icon'])
        elif 'cachedIcon' in fields:
            image = self._m.cachedIcon(fields['cachedIcon'][1], fields['cachedIcon'][0])
        if image is not None:
            self.tray.setIcon(QIcon(QPixmap.fromImage(image)))
        if not self.tray.isVisible() and not self.tray.icon().isNull():
            self.tray.show()
        tip = fields.get('infoTip')
        if tip and (tip['text'] or tip['title']):
            self.tray.showMessage(tip['title'], tip['text'], QSystemTrayIcon.MessageIcon.Information,
                                  max(1000, min(tip['timeout'] or 10000, 30000)))

    def _onActivated(self, reason):
        m, w, i = self._m, self.windowId, self.iconId
        # the server shows menus at the pointer: tell it where the pointer is
        p = QCursor.pos() - m.origin
        m.client.sendMouseMoveCoords(max(0, p.x()), max(0, p.y()))
        Reason = QSystemTrayIcon.ActivationReason
        if reason == Reason.Trigger:
            m.rail.notifyEvent(w, i, rail.WM_LBUTTONDOWN)
            m.rail.notifyEvent(w, i, rail.WM_LBUTTONUP)
            if self.version >= 3:
                m.rail.notifyEvent(w, i, rail.NIN_SELECT)
        elif reason == Reason.DoubleClick:
            m.rail.notifyEvent(w, i, rail.WM_LBUTTONDBLCLK)
        elif reason == Reason.Context:
            m.rail.notifyEvent(w, i, rail.WM_RBUTTONDOWN)
            m.rail.notifyEvent(w, i, rail.WM_RBUTTONUP)
            if self.version >= 3:
                m.rail.notifyEvent(w, i, rail.WM_CONTEXTMENU)

    def close(self):
        self.tray.hide()
        self.tray.deleteLater()


class RailManager(QObject):
    """
    @summary: The windows of a RemoteApp session (FreeRDP xf_rail_*). Events come
    from any thread through railEvent(); everything else runs in the GUI thread.
    """
    _event = pyqtSignal(object)
    # The program could not start / the session ended: (message).
    failed = pyqtSignal('QString')

    def __init__(self, client, desktop, *args, **kwargs):
        """
        @param client: {RDPClientQt} input and the controller
        @param desktop: {QRemoteDesktop} the (hidden) frame buffer of the session desktop
        """
        super(RailManager, self).__init__(*args, **kwargs)
        self.client = client
        self.desktop = desktop
        self.rail = client._controller.getRailLayer()
        self.appName = u'RemoteApp'
        self.windows = {}
        self.trayIcons = {}
        self.icons = {}  # (cacheId, cacheEntry) -> QImage
        self.closing = False
        self.active = False
        virtual, work = localDesktopGeometry()
        self.origin = virtual.topLeft()
        platform = (QApplication.platformName() or '').lower() if hasattr(QApplication, 'platformName') else ''
        # Wayland does not let clients place their windows
        self.globalMapping = 'wayland' not in platform
        self._pendingMove = None
        self._moveTimer = QTimer(self)
        self._moveTimer.setSingleShot(True)
        self._moveTimer.setInterval(16)
        self._moveTimer.timeout.connect(self._flushMove)
        self._event.connect(self._onEvent)
        self._cursor = None
        self._work = work.translated(-self.origin.x(), -self.origin.y())
        if getattr(self.rail, 'app', None) is not None:
            self.rail.setWorkArea(self._work.left(), self._work.top(), self._work.right() + 1, self._work.bottom() + 1)

    def _activate(self):
        """
        First RemoteApp event: follow the frame buffer and the pointer shape.
        """
        self.active = True
        app = getattr(self.rail, 'app', None)
        if app is not None:
            self.appName = app.program.lstrip('|').replace('/', '\\').rsplit('\\', 1)[-1] or u'RemoteApp'
        self.desktop.addDamageListener(self._onDamage)
        self.desktop._cursorSignal.connect(self._setCursor)
        self._cursor = self.desktop._remoteCursor
        self.rail.setWorkArea(self._work.left(), self._work.top(), self._work.right() + 1, self._work.bottom() + 1)

    @staticmethod
    def desktopSize():
        """@return: {tuple} (width, height) the RemoteApp session asks for"""
        virtual, _ = localDesktopGeometry()
        return virtual.width(), virtual.height()

    def railEvent(self, kind, *args):
        """@summary: RDPClientController.onRailEvent fan-out (any thread)"""
        self._event.emit((kind,) + tuple(args))

    def frameBuffer(self):
        return self.desktop.frameBuffer()

    # ---- events (GUI thread) ----

    @pyqtSlot(object)
    def _onEvent(self, event):
        kind, args = event[0], event[1:]
        handler = getattr(self, '_on_' + kind, None)
        if handler is None or self.closing:
            return
        if not self.active:
            self._activate()
        try:
            handler(*args)
        except Exception as e:
            log.warning(u'RemoteApp: %s event failed: %r' % (kind, e))

    def _on_handshake(self, build, flags):
        log.debug(u'RemoteApp: rail channel ready')

    def _on_started(self):
        log.debug(u'RemoteApp: program requested')

    def _on_execResult(self, flags, result, raw, exe, message):
        if result != rail.RAIL_EXEC_S_OK:
            self.failed.emit(u'%s could not be started: %s' % (exe or self.appName, message))

    def _on_window(self, windowId, fields, isNew):
        window = self.windows.get(windowId)
        if window is None:
            if not isNew:
                log.debug(u'RemoteApp: update of unknown window 0x%x, created' % windowId)
            window = RailWindow(self, windowId)
            if self._cursor is not None:
                window.setCursor(self._cursor)
            self.windows[windowId] = window
            isNew = True
        window.apply(fields, isNew)

    def _on_windowDelete(self, windowId):
        window = self.windows.pop(windowId, None)
        if window is not None:
            closing, self.closing = self.closing, True
            try:
                window.close()
            finally:
                self.closing = closing
            window.deleteLater()

    def _on_windowIcon(self, windowId, iconInfo, isBig):
        image = self.iconImage(iconInfo)
        self._setWindowIcon(windowId, image, isBig)

    def _on_windowCachedIcon(self, windowId, cacheEntry, cacheId, isBig):
        self._setWindowIcon(windowId, self.cachedIcon(cacheId, cacheEntry), isBig)

    def _setWindowIcon(self, windowId, image, isBig):
        window = self.windows.get(windowId)
        if window is None or image is None:
            return
        if isBig or not window._bigIcon:  # the big icon wins
            window._bigIcon = window._bigIcon or isBig
            window.setWindowIcon(QIcon(QPixmap.fromImage(image)))

    def _on_notifyIcon(self, windowId, iconId, fields, isNew):
        if QSystemTrayIcon is None or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = self.trayIcons.get((windowId, iconId))
        if tray is None:
            tray = self.trayIcons[(windowId, iconId)] = RailTrayIcon(self, windowId, iconId)
        tray.apply(fields)

    def _on_notifyIconDelete(self, windowId, iconId):
        tray = self.trayIcons.pop((windowId, iconId), None)
        if tray is not None:
            tray.close()

    def _on_desktop(self, fields):
        if fields.get('arcBegan') and fields.get('hooked') and self.windows:
            # the server describes every window again (reconnection): start afresh
            for windowId in list(self.windows):
                self._on_windowDelete(windowId)

    def _on_desktopNone(self):
        # the server stopped RemoteApp: show the whole desktop (FreeRDP disables remote app mode)
        log.warning(u'RemoteApp: the server left RemoteApp mode, showing the desktop')
        for windowId in list(self.windows):
            self._on_windowDelete(windowId)
        self.desktop.show()

    def _on_cloak(self, windowId, cloaked):
        window = self.windows.get(windowId)
        if window is not None:
            window.setCloaked(cloaked)

    def _on_minMaxInfo(self, windowId, values):
        log.debug(u'RemoteApp: window 0x%x min/max %r' % (windowId, values))

    def _on_localMoveSize(self, windowId, isStart, kind, x, y):
        log.debug(u'RemoteApp: local move/size not requested (window 0x%x)' % windowId)

    def _on_surface(self, windowId, x, y, width, height, bgra, surfaceWidth, surfaceHeight, mappedWidth,
                    mappedHeight):
        window = self.windows.get(windowId)
        if window is not None:
            window.updateSurface(x, y, width, height, bgra, surfaceWidth, surfaceHeight, mappedWidth, mappedHeight)

    def _on_appId(self, windowId, appId):
        log.debug(u'RemoteApp: window 0x%x is %s' % (windowId, appId))

    # ---- icons ----

    def iconImage(self, iconInfo):
        decoded = rail.decodeIcon(iconInfo)
        if decoded is None:
            return None
        w, h, pixels = decoded
        image = QImage(pixels, w, h, w * 4, QImage.Format.Format_ARGB32).copy()
        if iconInfo.get('cacheEntry', 0xFFFF) != 0xFFFF:
            self.icons[(iconInfo['cacheId'], iconInfo['cacheEntry'])] = image
        return image

    def cachedIcon(self, cacheId, cacheEntry):
        return self.icons.get((cacheId, cacheEntry))

    # ---- geometry and painting ----

    def placeWindow(self, window):
        shown = window.shownRect()
        if self.globalMapping:
            window.setGeometry(shown.translated(self.origin.x(), self.origin.y()))
        else:
            window.resize(shown.width(), shown.height())

    def _onDamage(self, rect):
        for window in self.windows.values():
            if window.surface is not None or not window.isVisible():
                continue
            area = rect.intersected(window.shownRect())
            if not area.isEmpty():
                shown = window.shownRect()
                window.update(area.translated(-shown.x(), -shown.y()))

    @pyqtSlot(object)
    def _setCursor(self, cursor):
        self._cursor = cursor
        for window in self.windows.values():
            window.setCursor(cursor)

    # ---- input ----

    def sendButton(self, x, y, button, pressed):
        self._flushMove()
        number = {Qt.MouseButton.LeftButton: 1, Qt.MouseButton.RightButton: 2,
                  Qt.MouseButton.MiddleButton: 3}.get(button)
        if number is not None:
            self.client.sendPointerAt(x, y, number, pressed)

    def queueMove(self, x, y):
        self._pendingMove = (x, y)
        if not self._moveTimer.isActive():
            self._moveTimer.start()

    def _flushMove(self):
        if self._pendingMove is not None:
            x, y = self._pendingMove
            self._pendingMove = None
            self.client.sendMouseMoveCoords(x, y)

    def sendWheel(self, x, y, angleDelta):
        self._flushMove()
        self.client.sendWheelAt(x, y, angleDelta)

    def sendKey(self, event, pressed):
        self.client.sendKeyEvent(event, pressed)

    # ---- end ----

    def shutdown(self):
        """@summary: The session ended: close every window (GUI thread)"""
        self.closing = True
        for windowId in list(self.windows):
            window = self.windows.pop(windowId)
            window.close()
            window.deleteLater()
        for tray in self.trayIcons.values():
            tray.close()
        self.trayIcons.clear()
        if self.active:
            self.desktop.removeDamageListener(self._onDamage)
            try:
                self.desktop._cursorSignal.disconnect(self._setCursor)
            except (TypeError, RuntimeError):
                pass


def showRemoteAppError(parent, message):
    """
    @summary: Tell the user why the program did not start.
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(u'RemoteApp')
    box.setText(message)
    getattr(box, 'exec')() if hasattr(box, 'exec') else box.exec_()
