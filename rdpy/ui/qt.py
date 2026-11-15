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
Qt specific code
QRemoteDesktop is a widget use for render in rdpy
"""
from __future__ import absolute_import, division, print_function
from ManyQt.QtCore import Qt, QObject, QTimer, pyqtSignal, QRectF, QMimeData, QUrl, QRect, pyqtSlot, QEventLoop, QEvent
from ManyQt.QtWidgets import QMessageBox, QMenu, QApplication, QWidget, QFileDialog
from ManyQt.QtGui import QCursor, QPixmap, QImage, QPainter, QColor, QRegion
from rdpy.protocol.rdp.rdp import RDPClientObserver
from rdpy.core.error import CallPureVirtualFuntion
from rdpy.core import log, rle
from numpy import any as np_any, ascontiguousarray, concatenate, empty, frombuffer, ndarray, pad, uint8, unpackbits, \
    where, zeros
from sys import stderr, stdout, platform
from twisted.internet import reactor
from traceback import format_stack
from collections import deque

# Scan code swap table for --swap-alt-meta: swaps Alt keys with Meta (Windows) keys.
_ALT_META_SWAP = {
    0x38: 0xE05B,  # Left Alt -> Left Windows
    0xE05B: 0x38,  # Left Windows -> Left Alt
    0xE038: 0xE05C,  # Right Alt -> Right Windows
    0xE05C: 0xE038,  # Right Windows -> Right Alt
}


class QAdaptor(object):
    """
    @summary:  Adaptor model with link between protocol
                And Qt widget
    """

    def sendMouseEvent(self, e, isPressed):
        """
        @summary: Interface to send mouse event to protocol stack
        @param e: QMouseEvent
        @param isPressed: event come from press or release action
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'sendMouseEvent', u'QAdaptor'))

    def sendKeyEvent(self, e, isPressed):
        """
        @summary: Interface to send key event to protocol stack
        @param e: QEvent
        @param isPressed: event come from press or release action
        """
        raise CallPureVirtualFuntion(u'%s:%s defined by interface %s' % (self.__class__, u'sendKeyEvent', u'QAdaptor'))

    def sendWheelEvent(self, e):
        """
        @summary: Interface to send wheel event to protocol stack
        @param e: QWheelEvent
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'sendWheelEvent', u'QAdaptor'))

    def sendMouseMoveCoords(self, x, y):
        """
        @summary: Interface to send a mouse-move-only event with raw coordinates
        @param x: x position
        @param y: y position
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'sendMouseMoveCoords', u'QAdaptor'))

    def closeEvent(self, e):
        """
        @summary: Call when you want to close connection
        @param: QCloseEvent
        """
        raise CallPureVirtualFuntion(u'%s:%s defined by interface %s' % (self.__class__, u'closeEvent', u'QAdaptor'))

    def onResizeRequest(self, width, height):
        """
        @summary: Interface to handle window resize and request RDP reconnection
        @param width: {int} new width
        @param height: {int} new height
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'onResizeRequest', u'QAdaptor'))


def qtImageFormatFromRFBPixelFormat(pixelFormat):
    """
    @summary: convert RFB pixel format to QImage format
    """
    if pixelFormat.BitsPerPixel.value == 32:
        return QImage.Format.Format_RGB32
    elif pixelFormat.BitsPerPixel.value == 16:
        return QImage.Format.Format_RGB16


def _flip_rows_inplace(buf, width, height, bytes_per_pixel):
    """Flip bottom-up bitmap data to top-down order in-place."""
    stride = width * bytes_per_pixel
    needed = height * stride
    if len(buf) < needed:
        return False
    arr = frombuffer(buf, dtype=uint8)[:needed].reshape(height, stride)
    arr[:] = arr[::-1]
    return True


_rleErrors = [0]


def _interleavedDecode(data, width, height, bytesPerPixel):
    """
    @summary: Interleaved RLE bitmap (15 / 16 / 24 bpp) with the FreeRDP-exact
    decoder (rdpy.core.interleaved, verified against FreeRDP's own decoder)
    @return: {bytes | None} top-down pixels, None for an invalid stream
    """
    from rdpy.core import interleaved
    try:
        return interleaved.decompress(data, width, height, bytesPerPixel)
    except interleaved.RleError as e:
        _rleErrors[0] += 1
        if _rleErrors[0] <= 5:
            log.warning(u'invalid RLE bitmap %dx%d (%d bytes): %s' % (width, height, len(data), e))
        return None


def RDPBitmapToQtImage(width, height, bitsPerPixel, isCompress, data):
    if width <= 0 or height <= 0:
        return None
    # Ensure data is bytes for QImage compatibility
    if hasattr(data, 'encode'):
        data = data.encode('latin-1')
    elif not isinstance(data, (bytes, bytearray)):
        data = bytes(data)
    # RDPGFX path: data is already top-down BGRA — no flip needed.
    # Keep a reference to the Python buffer on the QImage so it stays alive
    # until the QImage is garbage collected — avoids the expensive .copy().
    if isCompress == 'gfx' and bitsPerPixel == 32:
        needed = width * height * 4
        if len(data) < needed:
            log.warning(u'RDPGFX bitmap truncated: len=%d need=%d' % (len(data), needed))
            return None
        buf = bytes(data[:needed])
        image = QImage(buf, width, height, width * 4, QImage.Format.Format_RGB32)
        image._buffer_ref = buf
        return image

    # Compressed data is already in top-down order from the RLE decoder.
    # We attach the buffer as _buffer_ref on the QImage so Python keeps
    # the data alive — avoiding an expensive .copy().
    #
    # Uncompressed data is bottom-up (Windows BMP convention).  We flip
    # it in-place inside a bytearray then wrap with QImage, avoiding the
    # extra allocation that .mirrored() would cause.

    if bitsPerPixel == 15:
        bpp = 2
        fmt = QImage.Format.Format_RGB555
        needed = width * height * bpp
        if isCompress:
            buf = _interleavedDecode(data, width, height, bpp)
            if buf is None:
                return None
            if len(buf) < needed:
                return None
            image = QImage(buf, width, height, width * bpp, fmt)
            image._buffer_ref = buf
        else:
            if len(data) < needed:
                log.warning(u'Uncompressed 15bpp bitmap truncated: len=%d need=%d' % (len(data), needed))
                return None
            raw = bytearray(data[:needed])
            if not _flip_rows_inplace(raw, width, height, bpp):
                return None
            image = QImage(raw, width, height, width * bpp, fmt)
            image._buffer_ref = raw

    elif bitsPerPixel == 16:
        bpp = 2
        fmt = QImage.Format.Format_RGB16
        needed = width * height * bpp
        if isCompress:
            buf = _interleavedDecode(data, width, height, bpp)
            if buf is None:
                return None
            if len(buf) < needed:
                return None
            image = QImage(buf, width, height, width * bpp, fmt)
            image._buffer_ref = buf
        else:
            if len(data) < needed:
                log.warning(u'Uncompressed 16bpp bitmap truncated: len=%d need=%d' % (len(data), needed))
                return None
            raw = bytearray(data[:needed])
            if not _flip_rows_inplace(raw, width, height, bpp):
                return None
            image = QImage(raw, width, height, width * bpp, fmt)
            image._buffer_ref = raw

    elif bitsPerPixel == 24:
        bpp = 3
        fmt = QImage.Format.Format_BGR888
        needed = width * height * bpp
        if isCompress:
            buf = _interleavedDecode(data, width, height, bpp)
            if buf is None:
                return None
            if len(buf) < needed:
                return None
            image = QImage(buf, width, height, width * bpp, fmt)
            image._buffer_ref = buf
        else:
            if len(data) < needed:
                log.warning(u'Uncompressed 24bpp bitmap truncated: len=%d need=%d' % (len(data), needed))
                return None
            raw = bytearray(data[:needed])
            if not _flip_rows_inplace(raw, width, height, bpp):
                return None
            image = QImage(raw, width, height, width * bpp, fmt)
            image._buffer_ref = raw

    elif bitsPerPixel == 32:
        bpp = 4
        fmt = QImage.Format.Format_RGB32
        needed = width * height * bpp
        if isCompress:
            buf = rle.bitmap_decompress4(data, width, height)
            if len(buf) < needed:
                return None
            # Planar (RDP 6.0) bitmaps are encoded bottom-up and the decoder
            # keeps the encoded order: flip them like FreeRDP does for bitmap
            # updates (planar_decompress(..., vFlip=TRUE))
            buf = bytearray(buf[:needed])
            if not _flip_rows_inplace(buf, width, height, bpp):
                return None
            image = QImage(buf, width, height, width * bpp, fmt)
            image._buffer_ref = buf
        else:
            if len(data) < needed:
                log.warning(u'Uncompressed 32bpp bitmap truncated: len=%d need=%d' % (len(data), needed))
                return None
            # Flip rows and set alpha in one numpy pass — avoids the
            # intermediate  b'\xff' * N  allocation from the old slice-assign.
            arr = frombuffer(data[:needed], dtype=uint8).reshape(height, width, 4)[::-1].copy()
            arr[:, :, 3] = 0xff
            image = QImage(arr.data, width, height, width * bpp, fmt)
            image._buffer_ref = arr
    else:
        log.warning(u'skipping bitmap with unsupported bit depth: %d' % bitsPerPixel)
        return None
    return image


class _QtInvoker(QObject):
    """Posts callables from any thread to the Qt main thread.
    Used to marshal QAudioSink and other Qt-thread-only operations from
    the Twisted background thread.
    """
    _signal = pyqtSignal(object)

    def __init__(self):
        super(_QtInvoker, self).__init__()
        self._signal.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @pyqtSlot(object)
    def _run(self, fn):
        try:
            fn()
        except Exception as e:
            log.warning(u'_QtInvoker: exception in queued call: %s' % e)

    def invoke(self, fn):
        """Thread-safe: post fn() to be executed on the Qt main thread."""
        self._signal.emit(fn)


# Module-level singleton; created lazily (Qt must be running first).
_qt_invoker = None

# ---------------------------------------------------------------------------
# Clean exit for every way an application can end
# ---------------------------------------------------------------------------

_quitting = False
_guardInstalled = False


def isQuitting():
    """@return: {bool} the application is shutting down (no reconnect / error dialogs)"""
    return _quitting


def closeAllSessions(wait=2.5, app=None):
    """
    @summary: Disconnect every open RDP session cleanly (FreeRDP freerdp_disconnect:
    Disconnect Provider Ultimatum, then the socket) and wait until they are closed,
    at most `wait` seconds; whatever is still open is then aborted. Safe to call
    from QApplication.aboutToQuit: events are processed while waiting.
    @return: {int} number of sessions that had to be aborted
    """
    from time import sleep, time
    from rdpy.protocol.rdp.rdp import RDPClientController
    sessions = RDPClientController.openSessions()
    if not sessions:
        return 0
    log.info(u'closing %d RDP session(s)' % len(sessions))
    for controller in sessions:
        try:
            controller.close()
        except Exception as e:
            log.debug(u'closeAllSessions: %s' % e)
    app = app or QApplication.instance()
    deadline = time() + wait
    while time() < deadline and any(c.isConnected() for c in sessions):
        if app is not None:
            app.processEvents(
                QEventLoop.ProcessEventsFlag.AllEvents if hasattr(QEventLoop,
                                                                  'ProcessEventsFlag') else QEventLoop.AllEvents, 50)
        sleep(0.01)
    left = [c for c in sessions if c.isConnected()]
    for controller in left:
        controller.abort(u'the application is closing')
    if app is not None:
        app.processEvents()
    return len(left)


def requestQuit(app=None, exitCode=0):
    """
    @summary: Quit the application cleanly: no dialog, every session disconnected
    first, then the event loop ends (used for Ctrl+C, SIGTERM, logout...)
    """
    global _quitting
    if _quitting:
        return
    _quitting = True
    app = app or QApplication.instance()
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, QRemoteDesktop):
            widget.userClosed = True
    closeAllSessions(app=app)
    if reactor.running:
        try:
            reactor.stop()
        except Exception:
            pass
    if app is not None:
        app.exit(exitCode)


def forceExit(code=0):
    """
    @summary: End the process right after the event loop: sessions are already
    disconnected (aboutToQuit), so nothing may keep the console busy - no
    leftover thread, no late log line. Flushes the output, removes the
    clipboard download folders, then exits immediately.
    """
    from os import _exit
    global _quitting
    _quitting = True
    try:
        closeAllSessions(wait=1.0)
    except Exception:
        pass
    try:
        from rdpy.protocol.rdp.channels import cliprdr_file
        cliprdr_file._cleanup()
    except Exception:
        pass
    for stream in (stdout, stderr):
        try:
            stream.flush()
        except Exception:
            pass
    _exit(int(code or 0))


def installQuitGuard(app):
    """
    @summary: Only the QApplication.aboutToQuit part of installCleanExit (does not
    touch the signal handlers of the host application). Installed automatically
    by RdpViewerWidget.
    """
    if app is None or getattr(app, '_rdpyQuitGuard', False):
        return
    app._rdpyQuitGuard = True

    def onQuit():
        closeAllSessions(app=app)
        # stop Twisted too: its thread pool threads would keep the process
        # alive (and printing) after the window is gone
        try:
            if reactor.running:
                reactor.stop()
        except Exception:
            pass

    app.aboutToQuit.connect(onQuit)


def installCleanExit(app):
    """
    @summary: Make every exit path disconnect the RDP sessions properly:
    - QApplication.aboutToQuit (app.quit(), last window closed, logout / shutdown
      of the computer, a crash handler calling quit) closes the open sessions;
    - Ctrl+C, SIGTERM, SIGHUP and Ctrl+Break request a clean quit;
    - a light timer lets Python run its signal handlers while Qt waits for events.
    """
    global _guardInstalled
    if _guardInstalled or app is None:
        return
    _guardInstalled = True
    installQuitGuard(app)
    if hasattr(app, 'commitDataRequest'):
        # the desktop session (logout, shutdown) is ending
        app.commitDataRequest.connect(lambda manager: closeAllSessions(app=app))
    import signal

    def onSignal(signum, frame):
        log.info(u'signal %d: closing the RDP session(s)' % signum)
        QTimer.singleShot(0, lambda: requestQuit(app, 128 + signum))

    for name in ('SIGINT', 'SIGTERM', 'SIGHUP', 'SIGBREAK'):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), onSignal)
            except (ValueError, OSError):
                pass  # not the main thread / not supported
    timer = QTimer(app)
    timer.timeout.connect(lambda: None)
    timer.start(250)
    app._rdpyWakeTimer = timer


def _get_qt_invoker():
    global _qt_invoker
    if _qt_invoker is None:
        invoker = _QtInvoker()
        app = QApplication.instance()
        if app is not None and invoker.thread() is not app.thread():
            # created from a worker thread (camera, WebAuthn PIN...): its queued calls must
            # still run in the GUI thread
            invoker.moveToThread(app.thread())
        _qt_invoker = invoker
    return _qt_invoker


def askSecurityKeyPin(prompt, parent=None):
    """
    @summary: Ask the PIN of a FIDO2 security key (MS-RDPEWA) in a password dialog shown
    in the GUI thread; may be called from any thread
    @return: {str|None}
    """
    import threading
    from ManyQt.QtWidgets import QInputDialog, QLineEdit
    result = {}
    done = threading.Event()

    def ask():
        try:
            mode = QLineEdit.EchoMode.Password if hasattr(QLineEdit, 'EchoMode') else QLineEdit.Password
            text, ok = QInputDialog.getText(parent, u'Security key', prompt, mode)
            result['pin'] = text if ok and text else None
        finally:
            done.set()

    mainThread = getattr(threading, 'main_thread', None)
    if mainThread is not None and threading.current_thread() is mainThread():
        ask()
    else:
        _get_qt_invoker().invoke(ask)
        done.wait()
    return result.get('pin')


_keyNotification = {}


def showSecurityKeyNotification(show, message, timeoutMs, cancel=None, parent=None):
    """
    @summary: FreeRDP's WebAuthn user notification ("Touch the security key", 30 s): a small
    non-modal box with a Cancel button that stops the operation; any thread
    """

    def run():
        box = _keyNotification.pop('box', None)
        if box is not None:
            box.close()
            box.deleteLater()
        if not show:
            return
        box = QMessageBox(QMessageBox.Icon.Information, u'Security key', message,
                          QMessageBox.StandardButton.Cancel, parent)
        box.setModal(False)
        if cancel is not None:
            box.rejected.connect(cancel.set)
            box.buttonClicked.connect(lambda button: cancel.set())
        box.show()
        QTimer.singleShot(int(timeoutMs), box.close)
        _keyNotification['box'] = box

    _get_qt_invoker().invoke(run)


def _touchStateName(state):
    """Name of a Qt touch point state (Qt 5 TouchPointPressed..., Qt 6 QEventPoint.Pressed...)."""
    name = getattr(state, 'name', None)
    if not name:
        for candidate in ('Pressed', 'Released', 'Moved', 'Stationary'):
            for holder in (Qt, getattr(Qt, 'TouchPointState', None)):
                value = getattr(holder, 'TouchPoint' + candidate, None) if holder is not None else None
                if value is not None and int(value) == int(state):
                    return candidate
        return 'Moved'
    return name.replace('TouchPoint', '')


class RDPClientQt(RDPClientObserver, QAdaptor):
    """
    @summary: Adaptor for RDP client
    """

    def __init__(self, controller, width, height, swap_alt_meta=False, widget=None):
        """
        @param controller: {RDPClientController} RDP controller
        @param width: {int} width of widget
        @param height: {int} height of widget
        @param swap_alt_meta: {bool} swap Alt and Meta (Windows) keys
        @param widget: {QRemoteDesktop} existing widget to reuse (for resize reconnection)
        """
        RDPClientObserver.__init__(self, controller)
        if widget is not None:
            self._widget = widget
            self._widget.setAdaptor(self)
            # the session asks for width x height (e.g. after a resize reconnection);
            # the server may still decide another size (onReady / onDesktopResize)
            self._widget.setRemoteSize(width, height)
        else:
            self._widget = QRemoteDesktop(width, height, self)
        self._pointerCache = {}
        self._swap_alt_meta = swap_alt_meta
        self._resizeCallback = None
        self._dynamicResolution = True
        self._railMode = False
        #set widget screen to RDP stack
        controller.setScreen(width, height)
        controller.setPerformanceSession()
        # RemoteApp windows (MS-RDPERP): idle until the server opens the rail channel
        self._railManager = None
        if hasattr(controller, 'getRailLayer'):
            try:
                from rdpy.ui.rail_qt import RailManager
                self._railManager = RailManager(self, self._widget)
            except Exception as e:
                log.warning(u'RemoteApp windows unavailable: %r' % (e,))
        # Route QAudioSink operations to the Qt main thread so they are
        # never executed from the Twisted background thread.
        if hasattr(controller, '_rdpsndLayer') and controller._rdpsndLayer is not None:
            controller._rdpsndLayer.setQtInvoker(_get_qt_invoker().invoke)
        # Clipboard integration
        self._clipboard = QApplication.clipboard()
        self._clipboardConnected = True
        self._clipboard.dataChanged.connect(self._onLocalClipboardChanged)
        controller.setClipboardCallbacks(
            self._onRemoteClipboardText,
            self._getLocalClipboardText,
            self._getLocalClipboardFiles,
            self._onRemoteClipboardFiles,
        )

    def getWidget(self):
        """
        @return: widget use for render
        """
        return self._widget

    def disconnectClipboard(self):
        """Disconnect QClipboard signal to prevent stale sends after reconnection."""
        if self._clipboardConnected:
            try:
                self._clipboard.dataChanged.disconnect(self._onLocalClipboardChanged)
            except TypeError:
                pass
            self._clipboardConnected = False

    def _onLocalClipboardChanged(self):
        """Notify the RDP server that local clipboard content changed."""
        log.debug(u'CLIPRDR DEBUG [Qt]: _onLocalClipboardChanged() fired')
        reactor.callFromThread(self._controller.onLocalClipboardChanged)

    def _onRemoteClipboardText(self, text):
        """Update the local clipboard with text received from the RDP server."""
        log.debug(u'CLIPRDR DEBUG [Qt]: _onRemoteClipboardText() len=%d' % len(text))
        mimeData = QMimeData()
        mimeData.setText(text)
        self._clipboard.setMimeData(mimeData)

    def _getLocalClipboardFiles(self):
        """Return the local files / folders copied to the clipboard (Explorer copy)."""
        mimeData = self._clipboard.mimeData()
        if not mimeData or not mimeData.hasUrls():
            return []
        return [url.toLocalFile() for url in mimeData.urls() if url.isLocalFile()]

    def _onRemoteClipboardFiles(self, paths):
        """Put the files downloaded from the server on the local clipboard."""
        mimeData = QMimeData()
        mimeData.setUrls([QUrl.fromLocalFile(p) for p in paths])
        self._clipboard.setMimeData(mimeData)

    def _getLocalClipboardText(self):
        """Return current local clipboard text for sending to the RDP server."""
        mimeData = self._clipboard.mimeData()
        text = u''
        if mimeData and mimeData.hasText():
            text = mimeData.text()
        log.debug(u'CLIPRDR DEBUG [Qt]: _getLocalClipboardText() -> %d chars' % len(text))
        return text

    def sendMouseEvent(self, e, isPressed):
        """
        @summary: Convert Qt mouse event to RDP mouse event
        @param e: qMouseEvent
        @param isPressed: event come from press(true) or release(false) action
        """
        button = e.button()
        buttonNumber = 0
        if button == Qt.MouseButton.LeftButton:
            buttonNumber = 1
        elif button == Qt.MouseButton.RightButton:
            buttonNumber = 2
        elif button == Qt.MouseButton.MiddleButton:
            buttonNumber = 3
        x, y = self._widget.toRemote(e.pos().x(), e.pos().y())
        reactor.callFromThread(self._controller.sendPointerEvent, x, y, buttonNumber, isPressed)

    def sendPointerAt(self, x, y, button, isPressed):
        """
        @summary: Mouse button at remote desktop coordinates (RemoteApp windows)
        @param button: {int} 1 left, 2 right, 3 middle
        """
        reactor.callFromThread(self._controller.sendPointerEvent, int(x), int(y), button, isPressed)

    def sendWheelAt(self, x, y, angle):
        """
        @summary: Wheel at remote desktop coordinates (RemoteApp windows)
        @param angle: {QPoint} QWheelEvent.angleDelta()
        """
        if angle.y():
            reactor.callFromThread(self._controller.sendWheelEvent, int(x), int(y), angle.y(), False)
        if angle.x():
            reactor.callFromThread(self._controller.sendWheelEvent, int(x), int(y), angle.x(), True)

    def sendMouseMoveCoords(self, x, y):
        """
        @summary: Send a mouse-move-only event with raw coordinates (no button)
        @param x: x position
        @param y: y position
        """
        reactor.callFromThread(self._controller.sendPointerEvent, x, y, 0, False)

    # Mapping from macOS native key codes (kVK_xxx) to RDP scan codes.
    # Extended keys use the 0xE0xx form; the upper byte is stripped and
    # extended=True is passed to sendKeyEventScancode.
    _MACOS_KEYCODE_MAP = {
        0x00: 0x1E,  # kVK_ANSI_A
        0x01: 0x1F,  # kVK_ANSI_S
        0x02: 0x20,  # kVK_ANSI_D
        0x03: 0x21,  # kVK_ANSI_F
        0x04: 0x23,  # kVK_ANSI_H
        0x05: 0x22,  # kVK_ANSI_G
        0x06: 0x2C,  # kVK_ANSI_Z
        0x07: 0x2D,  # kVK_ANSI_X
        0x08: 0x2E,  # kVK_ANSI_C
        0x09: 0x2F,  # kVK_ANSI_V
        0x0B: 0x30,  # kVK_ANSI_B
        0x0C: 0x10,  # kVK_ANSI_Q
        0x0D: 0x11,  # kVK_ANSI_W
        0x0E: 0x12,  # kVK_ANSI_E
        0x0F: 0x13,  # kVK_ANSI_R
        0x10: 0x15,  # kVK_ANSI_Y
        0x11: 0x14,  # kVK_ANSI_T
        0x12: 0x02,  # kVK_ANSI_1
        0x13: 0x03,  # kVK_ANSI_2
        0x14: 0x04,  # kVK_ANSI_3
        0x15: 0x05,  # kVK_ANSI_4
        0x16: 0x07,  # kVK_ANSI_6
        0x17: 0x06,  # kVK_ANSI_5
        0x18: 0x0D,  # kVK_ANSI_Equal
        0x19: 0x0A,  # kVK_ANSI_9
        0x1A: 0x08,  # kVK_ANSI_7
        0x1B: 0x0C,  # kVK_ANSI_Minus
        0x1C: 0x09,  # kVK_ANSI_8
        0x1D: 0x0B,  # kVK_ANSI_0
        0x1E: 0x1B,  # kVK_ANSI_RightBracket
        0x1F: 0x18,  # kVK_ANSI_O
        0x20: 0x16,  # kVK_ANSI_U
        0x21: 0x1A,  # kVK_ANSI_LeftBracket
        0x22: 0x17,  # kVK_ANSI_I
        0x23: 0x19,  # kVK_ANSI_P
        0x24: 0x1C,  # kVK_Return
        0x25: 0x26,  # kVK_ANSI_L
        0x26: 0x24,  # kVK_ANSI_J
        0x27: 0x28,  # kVK_ANSI_Quote
        0x28: 0x25,  # kVK_ANSI_K
        0x29: 0x27,  # kVK_ANSI_Semicolon
        0x2A: 0x2B,  # kVK_ANSI_Backslash
        0x2B: 0x33,  # kVK_ANSI_Comma
        0x2C: 0x35,  # kVK_ANSI_Slash
        0x2D: 0x31,  # kVK_ANSI_N
        0x2E: 0x32,  # kVK_ANSI_M
        0x2F: 0x34,  # kVK_ANSI_Period
        0x30: 0x0F,  # kVK_Tab
        0x31: 0x39,  # kVK_Space
        0x32: 0x29,  # kVK_ANSI_Grave
        0x33: 0x0E,  # kVK_Delete (Backspace)
        0x35: 0x01,  # kVK_Escape
        0x37: 0xE05B,  # kVK_Command (Left Windows key, extended)
        0x38: 0x2A,  # kVK_Shift
        0x39: 0x3A,  # kVK_CapsLock
        0x3A: 0x38,  # kVK_Option (Left Alt)
        0x3B: 0x1D,  # kVK_Control (Left Ctrl)
        0x3C: 0x36,  # kVK_RightShift
        0x3D: 0xE038,  # kVK_RightOption (Right Alt, extended)
        0x3E: 0xE01D,  # kVK_RightControl (extended)
        0x40: 0x68,  # kVK_F17
        0x41: 0x53,  # kVK_ANSI_KeypadDecimal
        0x43: 0x37,  # kVK_ANSI_KeypadMultiply
        0x45: 0x4E,  # kVK_ANSI_KeypadPlus
        0x47: 0x45,  # kVK_ANSI_KeypadClear (NumLock)
        0x4B: 0xE035,  # kVK_ANSI_KeypadDivide (extended)
        0x4C: 0xE01C,  # kVK_ANSI_KeypadEnter (extended)
        0x4E: 0x4A,  # kVK_ANSI_KeypadMinus
        0x4F: 0x69,  # kVK_F18
        0x50: 0x6A,  # kVK_F19
        0x52: 0x52,  # kVK_ANSI_Keypad0
        0x53: 0x4F,  # kVK_ANSI_Keypad1
        0x54: 0x50,  # kVK_ANSI_Keypad2
        0x55: 0x51,  # kVK_ANSI_Keypad3
        0x56: 0x4B,  # kVK_ANSI_Keypad4
        0x57: 0x4C,  # kVK_ANSI_Keypad5
        0x58: 0x4D,  # kVK_ANSI_Keypad6
        0x59: 0x47,  # kVK_ANSI_Keypad7
        0x5B: 0x48,  # kVK_ANSI_Keypad8
        0x5C: 0x49,  # kVK_ANSI_Keypad9
        0x5D: 0x7D,  # kVK_JIS_Yen
        0x5E: 0x73,  # kVK_JIS_Underscore
        0x5F: 0x53,  # kVK_JIS_KeypadComma
        0x60: 0x3F,  # kVK_F5
        0x61: 0x40,  # kVK_F6
        0x62: 0x41,  # kVK_F7
        0x63: 0x3D,  # kVK_F3
        0x64: 0x42,  # kVK_F8
        0x65: 0x43,  # kVK_F9
        0x66: 0x3A,  # kVK_JIS_Eisu
        0x67: 0x57,  # kVK_F11
        0x68: 0x70,  # kVK_JIS_Kana
        0x69: 0x64,  # kVK_F13
        0x6A: 0x67,  # kVK_F16
        0x6B: 0x65,  # kVK_F14
        0x6D: 0x44,  # kVK_F10
        0x6F: 0x58,  # kVK_F12
        0x71: 0x66,  # kVK_F15
        0x72: 0xE052,  # kVK_Help (Insert, extended)
        0x73: 0xE047,  # kVK_Home (extended)
        0x74: 0xE049,  # kVK_PageUp (extended)
        0x75: 0xE053,  # kVK_ForwardDelete (Delete, extended)
        0x76: 0x3E,  # kVK_F4
        0x77: 0xE04F,  # kVK_End (extended)
        0x78: 0x3C,  # kVK_F2
        0x79: 0xE051,  # kVK_PageDown (extended)
        0x7A: 0x3B,  # kVK_F1
        0x7B: 0xE04B,  # kVK_LeftArrow (extended)
        0x7C: 0xE04D,  # kVK_RightArrow (extended)
        0x7D: 0xE050,  # kVK_DownArrow (extended)
        0x7E: 0xE048,  # kVK_UpArrow (extended)
    }

    def sendKeyEvent(self, e, isPressed):
        """
        @summary: Convert Qt key press event to RDP press event
        @param e: QKeyEvent
        @param isPressed: event come from press or release action
        """
        if platform == u'darwin':
            native_code = e.nativeVirtualKey()
            # macOS native key codes (kVK_xxx) differ entirely from RDP
            # scan codes, so use the explicit translation map.  Keys not in
            # the map have no direct RDP equivalent and are silently dropped.
            code = self._MACOS_KEYCODE_MAP.get(native_code)
            if code is None:
                return
        elif platform == u'linux':
            # X11/XKB native scan codes are 8 higher than RDP scan codes.
            code = e.nativeScanCode() - 8
        else:
            code = e.nativeScanCode()
        if self._swap_alt_meta:
            code = _ALT_META_SWAP.get(code, code)
        # Detect extended keys (e.g. arrows, Home, End): scan codes whose
        # upper byte is 0xE0 are sent with KBDFLAGS_EXTENDED set.
        extended = (code >> 8 == 0xE0)
        if extended:
            code &= 0xFF
        reactor.callFromThread(self._controller.sendKeyEventScancode, code, isPressed, extended)

    def onVideoFrame(self, x, y, width, height, bgra):
        """
        @summary: MS-RDPEVOR video frame (already scaled to the presentation size) drawn
        over the desktop like FreeRDP gdiVideoShowSurface (clipped to the desktop)
        """
        rw, rh = self._widget.remoteSize()
        w = min(width, rw - x)
        h = min(height, rh - y)
        if w <= 0 or h <= 0:
            return
        data = bgra[:h, :w].copy()
        image = QImage(data.data, w, h, w * 4, QImage.Format.Format_RGB32)
        image._buffer_ref = data
        self._widget.notifyBatch([(x, y, image, w, h)])

    def onTsmfFrame(self, x, y, width, height, bgra, visibleRects):
        """
        @summary: TSMF (MS-RDPEV) video frame: scaled to the player's video rectangle and drawn
        in its visible rectangles only (FreeRDP xf_tsmf); any thread
        """
        if width <= 0 or height <= 0:
            return
        h0, w0 = bgra.shape[:2]
        data = bgra.copy()
        image = QImage(data.data, w0, h0, w0 * 4, QImage.Format.Format_RGB32)
        image._buffer_ref = data
        if (w0, h0) != (width, height):
            image = image.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        rw, rh = self._widget.remoteSize()
        parts = []
        for rx, ry, rwid, rhei in (visibleRects or [(0, 0, width, height)]):
            cx, cy = max(0, rx), max(0, ry)
            cw = min(width, rx + rwid) - cx
            ch = min(height, ry + rhei) - cy
            cw = min(cw, rw - (x + cx))
            ch = min(ch, rh - (y + cy))
            if cw > 0 and ch > 0 and x + cx >= 0 and y + cy >= 0:
                parts.append((x + cx, y + cy, image.copy(cx, cy, cw, ch), cw, ch))
        if parts:
            self._widget.notifyBatch(parts)

    def sendTouchEvent(self, e):
        """
        @summary: Qt touch event -> MS-RDPEI touch frame (FreeRDP xf_input touch handling)
        @return: {bool} sent (False: no rdpei channel, Qt then makes mouse events)
        """
        rdpei = getattr(self._controller, '_rdpei', None)
        if rdpei is None or not rdpei.isActive():
            return False
        points = []
        cancel = e.type() == QEvent.Type.TouchCancel
        for p in (e.points() if hasattr(e, 'points') else e.touchPoints()):
            pos = p.position() if hasattr(p, 'position') else p.pos()
            x, y = self._widget.toRemote(pos.x(), pos.y())
            name = _touchStateName(p.state())
            state = 'cancel' if cancel else {'Pressed': 'begin', 'Released': 'end'}.get(name, 'update')
            points.append((int(p.id()), state, x, y))
        reactor.callFromThread(self._controller.sendTouch, points)
        return True

    def sendTabletEvent(self, e):
        """
        @summary: Qt tablet (pen) event -> MS-RDPEI pen frame (protocol 2.0 and later)
        @return: {bool} sent (False: Qt then makes mouse events)
        """
        rdpei = getattr(self._controller, '_rdpei', None)
        if rdpei is None or not rdpei.penSupported():
            return False
        T = QEvent.Type
        kind = e.type()
        buttons = int(getattr(e.buttons(), 'value', e.buttons()))
        if kind == T.TabletPress:
            state = 'begin'
        elif kind == T.TabletRelease:
            state = 'end'
        elif buttons:
            state = 'update'
        else:
            state = 'hover'
        pos = e.position() if hasattr(e, 'position') else e.posF()
        x, y = self._widget.toRemote(pos.x(), pos.y())
        pointer = getattr(e, 'pointerType', lambda: None)()
        eraser = 'Eraser' in str(pointer)
        barrel = bool(buttons & int(getattr(Qt.MouseButton.RightButton, 'value', Qt.MouseButton.RightButton)))
        reactor.callFromThread(self._controller.sendPen, state, x, y, pressure=e.pressure(),
                               rotation=int(e.rotation()) % 360, tiltX=e.xTilt(), tiltY=e.yTilt(),
                               eraser=eraser, barrel=barrel)
        return True

    def sendWheelEvent(self, e):
        """
        @summary: Convert Qt wheel event to RDP Wheel event
        @param e: QWheelEvent
        """
        # angleDelta() returns 1/8-degree units; one standard mouse wheel notch = 120
        angle = e.angleDelta()
        x, y = self._widget.toRemote(e.position().x(), e.position().y())
        # Vertical scroll
        delta_y = angle.y()
        if delta_y != 0:
            scroll = delta_y
            if scroll == 0:
                scroll = 1 if delta_y > 0 else -1
            reactor.callFromThread(self._controller.sendWheelEvent, x, y, scroll, False)

        # Horizontal scroll
        delta_x = angle.x()
        if delta_x != 0:
            scroll = delta_x
            if scroll == 0:
                scroll = 1 if delta_x > 0 else -1
            reactor.callFromThread(self._controller.sendWheelEvent, x, y, scroll, True)

    def closeEvent(self, e):
        """
        @summary: Convert Qt close widget event into close stack event
        @param e: QCloseEvent
        """
        log.debug(u'closeEvent: scheduling close() from Qt thread:\n%s' % u''.join(format_stack()))
        reactor.callFromThread(self._controller.close)

    def setResizeCallback(self, callback):
        """
        @param callback: callable(width, height) to invoke on resize
        """
        self._resizeCallback = callback

    def getSessionReport(self):
        """@return: {str} the session report (status, server, security, certificate...)"""
        return self._controller.getSessionReport()

    def getSessionInfo(self):
        """@return: {dict} the session report as data"""
        return self._controller.getSessionInfo()

    def sendFiles(self, paths):
        """
        @summary: Put local files on the remote clipboard (drag & drop): paste
        them in the session with Ctrl+V
        """
        mimeData = QMimeData()
        mimeData.setUrls([QUrl.fromLocalFile(p) for p in paths])
        self._clipboard.setMimeData(mimeData)  # dataChanged announces them to the server

    def setDynamicResolution(self, enabled):
        """
        @summary: Resizing the widget changes the remote resolution through the display
        control channel (MS-RDPEDISP) when the server offers it (default True)
        """
        self._dynamicResolution = bool(enabled)

    def _screenDpi(self):
        try:
            screen = self._widget.screen() if hasattr(self._widget, 'screen') else None
            screen = screen or QApplication.primaryScreen()
            return screen.physicalDotsPerInch() if screen is not None else None
        except Exception:
            return None

    def onResizeRequest(self, width, height):
        """
        @summary: Called when the user resizes the widget: the display control channel
        changes the remote resolution (FreeRDP /dynamic-resolution), else the resize
        callback reconnects with the new size
        """
        controller = self._controller
        if getattr(self, '_dynamicResolution', True) and not getattr(self, '_railMode', False) and \
                hasattr(controller, 'isDynamicResolutionActive') and controller.isDynamicResolutionActive():
            from rdpy.protocol.rdp.channels import disp
            physical = disp.physicalSize(width, height, self._screenDpi())
            reactor.callFromThread(controller.requestDesktopSize, width, height, physical[0], physical[1])
            return
        if self._resizeCallback:
            self._resizeCallback(width, height)

    def onDynamicResolutionReady(self):
        """
        @summary: The display control channel is open (any thread): the remote desktop
        takes the size of the widget if it changed while connecting
        """
        if getattr(self, '_dynamicResolution', True):
            self._widget._dispReadySignal.emit()

    def onUpdate(self, destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, isCompress, data):
        """
        @summary: Notify bitmap update
        @param destLeft: {int} xmin position
        @param destTop: {int} ymin position
        @param destRight: {int} xmax position because RDP can send bitmap with padding
        @param destBottom: {int} ymax position because RDP can send bitmap with padding
        @param width: {int} width of bitmap
        @param height: {int} height of bitmap
        @param bitsPerPixel: {int} number of bit per pixel
        @param isCompress: {bool} use RLE compression
        @param data: {str} bitmap data
        """
        try:
            image = RDPBitmapToQtImage(width, height, bitsPerPixel, isCompress, data)
        except Exception as e:
            # one bad rectangle must never drop the others of the update
            log.warning(u'bitmap %dx%d at %d,%d not drawn: %r' % (width, height, destLeft, destTop, e))
            return
        if image is None or image.isNull():
            return
        w = min(destRight - destLeft + 1, width)
        h = min(destBottom - destTop + 1, height)
        if w <= 0 or h <= 0:
            return
        self._widget.notifyImage(destLeft, destTop, image, w, h)
        self._firstPicture()

    def _firstPicture(self):
        """The first picture of the connection: maybe the logon screen."""
        if not getattr(self, '_sawPicture', False):
            self._sawPicture = True
            controller = getattr(self, '_controller', None)
            if controller is not None and hasattr(controller, 'onFirstPicture'):
                try:
                    controller.onFirstPicture()
                except Exception as e:  # never break painting for a notification
                    log.warning(u'login screen notification failed: %s' % e)

    def onLoginScreen(self):
        """
        @summary: The Windows logon screen is displayed (RDPClientObserver)
        """
        self._widget.loginScreenReached.emit()

    def onDesktopReady(self):
        """
        @summary: The user is logged on, the desktop starts (RDPClientObserver)
        """
        self._widget.desktopReached.emit()

    def onLogonError(self, errorType, errorData, message):
        """
        @summary: Logon error or warning from the server (RDPClientObserver)
        """
        self._widget.logonError.emit(message)

    def onFileTransferStarted(self, direction, items, totalBytes):
        """@summary: clipboard file transfer started (RDPClientObserver)"""
        self._widget.fileTransferStarted.emit(direction, int(items), int(totalBytes))

    def onFileTransferProgress(self, direction, doneBytes, totalBytes):
        """@summary: clipboard file transfer progress (RDPClientObserver)"""
        self._widget.fileTransferProgress.emit(direction, int(doneBytes), int(totalBytes))

    def onFileTransferFinished(self, direction, paths, totalBytes, status):
        """@summary: clipboard file transfer finished (RDPClientObserver)"""
        self._widget.fileTransferFinished.emit(direction, list(paths), int(totalBytes), status)

    def onDriveFileTransferred(self, direction, path, totalBytes):
        """@summary: drive file read / written by the server (RDPClientObserver, any thread)"""
        self._widget.driveFileTransferred.emit(direction, path, int(totalBytes))

    def onPrintJob(self, printerName, title, totalBytes, destination, error):
        """@summary: document printed through a redirected printer (RDPClientObserver, any thread)"""
        self._widget.printJob.emit(printerName, title, int(totalBytes), destination or '', error or '')

    def onGfxPresent(self, items):
        """
        @summary: One RDPGFX frame: every updated area, painted together
        @param items: [(x, y, width, height, bgra)] bgra is a (height, width*4)
                      uint8 array owned by us (copied from the surface)
        """
        batch = []
        fmt = QImage.Format.Format_RGB32
        for x, y, w, h, region in items:
            if w <= 0 or h <= 0:
                continue
            image = QImage(region.data, w, h, w * 4, fmt)
            image._buffer_ref = region
            batch.append((x, y, image, w, h))
        if batch:
            self._widget.notifyBatch(batch)
            self._firstPicture()

    def onGfxSurface(self, dest_x, dest_y, width, height, surf_buf, surf_w, src_left, src_top):
        """Fast-path AVC delivery: create QImage from surface buffer region with stride.

        Avoids tobytes() — wraps the persistent surface buffer directly using
        Qt's bytes-per-line stride parameter so non-contiguous partial updates
        work without any extra memory allocation.
        """
        if width <= 0 or height <= 0:
            return
        offset = (src_top * surf_w + src_left) * 4
        needed_bytes = offset + (height - 1) * surf_w * 4 + width * 4
        if offset < 0 or needed_bytes > len(surf_buf):
            log.warning(u'RDPGFX: surface region out of bounds')
            return
        # The decoder (on its own thread) keeps writing the next frames into
        # the surface, so snapshot the region now: the GUI thread paints it
        # later and must not see a half decoded frame.
        stride = surf_w * 4
        surf = surf_buf if isinstance(surf_buf, ndarray) else frombuffer(surf_buf, dtype=uint8)
        surf = surf.reshape(-1)
        rowBytes = width * 4
        if (src_top + height) * stride <= surf.shape[0]:
            rows = surf[src_top * stride:(src_top + height) * stride].reshape(height, stride)
            region = ascontiguousarray(rows[:, src_left * 4:src_left * 4 + rowBytes])
        else:  # last row shorter than the stride
            region = empty((height, rowBytes), dtype=uint8)
            for r in range(height):
                start = offset + r * stride
                region[r] = surf[start:start + rowBytes]
        image = QImage(region.data, width, height, width * 4,
                       QImage.Format.Format_RGB32)
        image._buffer_ref = region
        self._widget.notifyImage(dest_x, dest_y, image, width, height)

    def onReady(self):
        """
        @summary: Call when stack is ready
        @see: rdp.RDPClientObserver.onReady
        """
        size = self._controller.getServerDesktopSize() if hasattr(self._controller, 'getServerDesktopSize') else None
        if size:
            self.onDesktopResize(size[0], size[1])

    def onDesktopResize(self, width, height):
        """
        @summary: The remote resolution is known / changed (any thread)
        """
        self._widget.setRemoteSizeThreadSafe(width, height)

    def onSessionReady(self):
        """
        @summary: Windows session is ready
        @see: rdp.RDPClientObserver.onSessionReady
        """
        pass

    def getRailManager(self):
        """@return: {rail_qt.RailManager|None} the RemoteApp windows"""
        return self._railManager

    def onRailEvent(self, kind, *args):
        """
        @summary: RemoteApp channel / window order event (any thread): to the windows
        """
        self._railMode = True
        if self._railManager is not None:
            self._railManager.railEvent(kind, *args)

    def onClose(self):
        """
        @summary: Call when stack is close
        @see: rdp.RDPClientObserver.onClose
        """
        if self._railManager is not None and self._railManager.active:
            _get_qt_invoker().invoke(self._railManager.shutdown)

    def onPointerHide(self):
        """
        @summary: Called when the server hides the pointer
        @see: rdp.RDPClientObserver.onPointerHide
        """
        self._widget.setCursorThreadSafe(QCursor(Qt.CursorShape.BlankCursor))

    def onPointerDefault(self):
        """
        @summary: Called when the server restores the default pointer
        @see: rdp.RDPClientObserver.onPointerDefault
        """
        self._widget.setCursorThreadSafe(QCursor(Qt.CursorShape.ArrowCursor))

    def onPointerCached(self, cacheIndex):
        """
        @summary: Called when the server switches to a cached pointer
        @see: rdp.RDPClientObserver.onPointerCached
        """
        cursor = self._pointerCache.get(cacheIndex)
        if cursor is not None:
            self._widget.setCursorThreadSafe(cursor)
        else:
            log.debug(u'RDPClientQt.onPointerCached() cache miss for index %d, restoring default' % cacheIndex)
            self._widget.setCursorThreadSafe(QCursor(Qt.CursorShape.ArrowCursor))

    def onPointerUpdate(self, xorBpp, cacheIndex, hotSpotX, hotSpotY, width, height, andMask, xorMask):
        """
        @summary: Called when the server sends a new pointer shape
        @see: rdp.RDPClientObserver.onPointerUpdate
        """
        if width <= 0 or height <= 0:
            return
        hotSpotX = max(0, min(hotSpotX, width - 1))
        hotSpotY = max(0, min(hotSpotY, height - 1))

        # AND mask rows are padded to 2-byte (word) boundaries
        and_stride = ((width + 15) // 16) * 2

        if xorBpp == 32:
            xor_stride = width * 4
            # Flip bottom-up to top-down using numpy reshape + flip
            xor_arr = frombuffer(xorMask, dtype=uint8)
            needed = height * xor_stride
            if len(xor_arr) < needed:
                xor_arr = concatenate(
                    [xor_arr, zeros(needed - len(xor_arr), dtype=uint8)])
            xor_2d = xor_arr[:needed].reshape(height, xor_stride)[::-1]
            buf = xor_2d.reshape(-1).copy()
            # Reshape as (H, W, 4) for vectorized alpha processing
            pixels = buf.reshape(height, width, 4)
            has_alpha = np_any(pixels[:, :, 3] != 0)

            # Unpack AND mask bits into boolean array
            and_bits = self._unpack_and_mask(andMask, and_stride, width, height)

            if not has_alpha:
                pixels[:, :, 3] = 255
                # Transparent where AND=1 and pixel is black
                black_mask = (pixels[:, :, 0] == 0) & (pixels[:, :, 1] == 0) & (pixels[:, :, 2] == 0)
                pixels[:, :, 3] = where(and_bits & black_mask, 0, pixels[:, :, 3])
            else:
                # AND=1 and not fully transparent → force alpha 255
                not_zero = (pixels[:, :, 0] != 0) | (pixels[:, :, 1] != 0) | (pixels[:, :, 2] != 0) | (
                        pixels[:, :, 3] != 0)
                pixels[:, :, 3] = where(and_bits & not_zero, 255, pixels[:, :, 3]).astype(uint8)

            image = QImage(pixels.tobytes(), width, height, width * 4, QImage.Format.Format_ARGB32)

        elif xorBpp == 24:
            xor_stride = width * 3
            xor_arr = frombuffer(xorMask, dtype=uint8)
            needed = height * xor_stride
            if len(xor_arr) < needed:
                xor_arr = pad(xor_arr, (0, needed - len(xor_arr)))
            xor_2d = xor_arr[:needed].reshape(height, xor_stride)[::-1]
            bgr = xor_2d.reshape(height, width, 3).copy()
            # Build BGRA with alpha=255
            pixels = empty((height, width, 4), dtype=uint8)
            pixels[:, :, :3] = bgr
            pixels[:, :, 3] = 255

            and_bits = self._unpack_and_mask(andMask, and_stride, width, height)
            black_mask = (pixels[:, :, 0] == 0) & (pixels[:, :, 1] == 0) & (pixels[:, :, 2] == 0)
            pixels[:, :, 3] = where(and_bits & black_mask, 0, pixels[:, :, 3])

            image = QImage(pixels.tobytes(), width, height, width * 4, QImage.Format.Format_ARGB32)

        elif xorBpp == 1:
            xor_stride = ((width + 15) // 16) * 2
            and_bits = self._unpack_and_mask(andMask, and_stride, width, height)
            xor_bits = self._unpack_and_mask(xorMask, xor_stride, width, height)

            pixels = zeros((height, width, 4), dtype=uint8)
            # AND=0, XOR=0 => black opaque
            m00 = ~and_bits & ~xor_bits
            pixels[:, :, 3] = where(m00, 255, pixels[:, :, 3])
            # AND=0, XOR=1 => white opaque
            m01 = ~and_bits & xor_bits
            pixels[:, :, 0] = where(m01, 255, pixels[:, :, 0])
            pixels[:, :, 1] = where(m01, 255, pixels[:, :, 1])
            pixels[:, :, 2] = where(m01, 255, pixels[:, :, 2])
            pixels[:, :, 3] = where(m01, 255, pixels[:, :, 3])
            # AND=1, XOR=0 => transparent (already 0)
            # AND=1, XOR=1 => inverted (approximate as black opaque)
            m11 = and_bits & xor_bits
            pixels[:, :, 3] = where(m11, 255, pixels[:, :, 3])

            image = QImage(pixels.tobytes(), width, height, width * 4, QImage.Format.Format_ARGB32)

        else:
            log.debug(u'RDPClientQt.onPointerUpdate() unsupported bpp=%d' % xorBpp)
            return

        pixmap = QPixmap.fromImage(image)
        cursor = QCursor(pixmap, hotSpotX, hotSpotY)
        self._pointerCache[cacheIndex] = cursor
        self._widget.setCursorThreadSafe(cursor)

    @staticmethod
    def _unpack_and_mask(mask_data, stride, width, height):
        """Unpack a 1bpp mask (bottom-up, padded rows) into a (height, width) bool array."""
        mask_arr = frombuffer(mask_data, dtype=uint8)
        needed = height * stride
        if len(mask_arr) < needed:
            mask_arr = pad(mask_arr, (0, needed - len(mask_arr)))
        rows = mask_arr[:needed].reshape(height, stride)[::-1]
        # Unpack all bits in each row, then truncate to width
        bits = unpackbits(rows, axis=1)[:, :width]
        return bits.astype(bool)


class QRemoteDesktop(QWidget):
    """
    @summary: Qt display widget
    """
    # Lightweight wake-up signal (no data) for bitmap update batching.
    # The actual image data is passed via a thread-safe deque; this signal
    # just tells the Qt event loop to schedule a flush.
    _updateSignal = pyqtSignal()
    # Logon state of the session (emitted from any thread, delivered in the GUI thread)
    loginScreenReached = pyqtSignal()
    desktopReached = pyqtSignal()
    logonError = pyqtSignal('QString')
    # File transfers: direction 'upload' (local -> server) or 'download'
    fileTransferStarted = pyqtSignal('QString', int, object)  # direction, items, total bytes
    fileTransferProgress = pyqtSignal('QString', object, object)  # direction, done, total
    fileTransferFinished = pyqtSignal('QString', object, object, 'QString')  # direction, paths, bytes, status
    driveFileTransferred = pyqtSignal('QString', 'QString', object)  # direction, path, bytes
    # Redirected printers: printer, document title, bytes, destination, error ('' when printed)
    printJob = pyqtSignal('QString', 'QString', object, 'QString', 'QString')
    # Thread-safe cursor update: Twisted thread emits, Qt main thread applies
    _cursorSignal = pyqtSignal(object)
    # Thread-safe remote resolution change (smart sizing scales to it)
    _remoteSizeSignal = pyqtSignal(int, int)
    # The display control channel opened (any thread)
    _dispReadySignal = pyqtSignal()

    def __init__(self, width, height, adaptor, *args, **kwargs):
        """
        @param adaptor: {QAdaptor}
        @param width: {int} width of widget
        @param height: {int} height of widget
        """
        super(QRemoteDesktop, self).__init__(*args, **kwargs)
        #adaptor use to send
        self._adaptor = adaptor
        self._buffer = None
        #bind mouse event
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._updateSignal.connect(self._scheduleFlush)
        self._cursorSignal.connect(self._applyCursor)
        self._remoteSizeSignal.connect(self.setRemoteSize)
        self._dispReadySignal.connect(self._onDisplayControlReady)
        # RemoteApp windows painted from this frame buffer: callable(QRect) per painted area
        self._damageListeners = []
        self._remoteCursor = None  # cursor shape from the server (scaled when smart sizing)
        #resize debounce
        self._programmaticResize = False
        self._sessionReady = False  # suppress resize-reconnect until first frame
        self._resizeTimer = QTimer()
        self._resizeTimer.setSingleShot(True)
        self._resizeTimer.timeout.connect(self._handleResizeTimeout)
        self._pendingSize = None
        #mouse move throttle: coalesce rapid moves to ~60fps
        self._pendingMousePos = None
        self._mouseMoveTimer = QTimer()
        self._mouseMoveTimer.setSingleShot(True)
        self._mouseMoveTimer.setInterval(16)  # ~60 fps
        self._mouseMoveTimer.timeout.connect(self._flushMouseMove)
        # Bitmap update batching: accumulate updates from the Twisted thread
        # in a deque and paint them in one batch on the Qt thread.  A 0ms
        # single-shot timer ensures the flush runs *after* any already-queued
        # events (mouse clicks/moves), preventing bitmap floods from starving
        # input events.
        self._pendingUpdates = deque()
        self._flushScheduled = False
        self._updateFlushTimer = QTimer()
        self._updateFlushTimer.setSingleShot(True)
        self._updateFlushTimer.setInterval(0)
        self._updateFlushTimer.timeout.connect(self._flushUpdates)
        self.userClosed = False
        self.setAcceptDrops(True)
        # touch screens and pens go to MS-RDPEI when the server offers it
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        # Smart sizing (mstsc "smart sizing", FreeRDP /smart-sizing): the remote
        # desktop keeps its resolution and is scaled to cover the whole widget,
        # like mstsc and FreeRDP (no black bars); resizing the window does not
        # reconnect. keepAspectRatio letterboxes instead (black bars).
        self._smartSizing = False
        self._keepAspect = False
        #set correct size; buffer is created in resizeEvent
        self.resize(width, height)
        if self._buffer is None:
            # hidden widget (console --info / --screenshot): no resize event yet
            self._buffer = QImage(width, height, QImage.Format.Format_RGB32)
            self._buffer.fill(QColor(0, 0, 0))

    def notifyImage(self, x, y, qimage, width, height):
        """
        @summary: Function called from Twisted thread to queue a bitmap update.
                  Updates are accumulated in a deque and flushed in batch on
                  the Qt main thread, so that rapid bitmap floods (e.g. video
                  playback) do not starve mouse / keyboard events.
        @param x: x position of new image
        @param y: y position of new image
        @param qimage: new QImage
        @param width: width of the image region
        @param height: height of the image region
        """
        if qimage is None or qimage.isNull() or width <= 0 or height <= 0:
            return
        self.notifyBatch([(x, y, qimage, width, height)])

    def notifyBatch(self, batch):
        """
        @summary: Queue several updates that must appear together (one RDPGFX
                  frame). Thread safe: the batch is queued as a single entry,
                  so the GUI never paints half of a frame.
        @param batch: [(x, y, qimage, width, height)]
        """
        if not batch:
            return
        if not self._sessionReady:
            self._sessionReady = True
        self._pendingUpdates.append(batch)
        # Emit the wake signal only once per batch to minimise Qt event-queue
        # pressure.  The flag is reset in _flushUpdates after the deque is
        # drained, so any update appended after that point will trigger a new
        # signal.
        if not self._flushScheduled:
            self._flushScheduled = True
            self._updateSignal.emit()

    def _scheduleFlush(self):
        """Slot on Qt main thread – start a 0 ms timer so the flush runs
        after any already-queued events (mouse/keyboard) are processed."""
        if not self._updateFlushTimer.isActive():
            self._updateFlushTimer.start()

    def _flushUpdates(self):
        """Process all pending bitmap updates in one batch, then repaint
        only the union of all dirty regions."""
        # Reset the flag BEFORE draining: updates are produced on other
        # threads, and one appended while we drain must schedule a new flush
        # (resetting it afterwards could strand that update in the deque).
        self._flushScheduled = False
        updates = []
        while self._pendingUpdates:
            try:
                updates.extend(self._pendingUpdates.popleft())
            except IndexError:
                break

        if not updates or self._buffer is None or self._buffer.isNull():
            return

        # When painting fell behind (several frames queued), skip the updates
        # that later ones cover completely: only the newest picture matters,
        # which keeps the display in real time instead of replaying old frames.
        if len(updates) > 1:
            covered = QRegion()
            visible = []
            for x, y, qimage, w, h in reversed(updates):
                area = QRegion(QRect(x, y, w, h))
                # skip only what later updates cover COMPLETELY (QRegion.contains
                # means "overlaps": using it dropped partly hidden updates)
                if area.subtracted(covered).isEmpty():
                    continue
                visible.append((x, y, qimage, w, h))
                covered = covered.united(area)
            visible.reverse()
            updates = visible

        dirty = QRect()
        with QPainter(self._buffer) as qp:
            for x, y, qimage, w, h in updates:
                if qimage is None or qimage.isNull() or w <= 0 or h <= 0:
                    continue
                qp.drawImage(x, y, qimage, 0, 0, w, h)
                rect = QRect(x, y, w, h)
                if dirty.isEmpty():
                    dirty = rect
                else:
                    dirty = dirty.united(rect)
        if not dirty.isEmpty():
            self.update(self._toView(dirty))
            self._notifyDamage(dirty)

    def addDamageListener(self, listener):
        """@param listener: callable(QRect) called (GUI thread) for each area painted in the frame buffer"""
        self._damageListeners.append(listener)

    def removeDamageListener(self, listener):
        if listener in self._damageListeners:
            self._damageListeners.remove(listener)

    def _notifyDamage(self, rect):
        for listener in list(self._damageListeners):
            try:
                listener(rect)
            except Exception as e:
                log.warning(u'frame buffer listener failed: %r' % (e,))

    def frameBuffer(self):
        """@return: {QImage} the remote desktop (GUI thread)"""
        return self._buffer

    @pyqtSlot()
    def _onDisplayControlReady(self):
        """The display control channel opened: the remote desktop takes the widget size."""
        if self._smartSizing or self._adaptor is None or self._buffer is None or not self.isVisible():
            return
        size = (self.width(), self.height())
        if size != (self._buffer.width(), self._buffer.height()) and min(size) > 0:
            self._adaptor.onResizeRequest(size[0], size[1])

    # ---- smart sizing ----

    def isSmartSizing(self):
        """@return: {bool} the remote desktop is scaled to the widget"""
        return self._smartSizing

    def setSmartSizing(self, enabled, keepAspectRatio=None):
        """
        @summary: Scale the remote desktop to the widget instead of changing the
        remote resolution when the widget is resized (FreeRDP /smart-sizing)
        @param enabled: {bool}
        @param keepAspectRatio: {bool | None} letterbox with black bars instead of covering the
        whole widget (default False, as mstsc / FreeRDP; None: unchanged)
        """
        if keepAspectRatio is not None:
            self._keepAspect = bool(keepAspectRatio)
        enabled = bool(enabled)
        if enabled == self._smartSizing:
            self.update()
            return
        self._smartSizing = enabled
        self._resizeTimer.stop()
        self._pendingSize = None
        if not enabled and self._buffer is not None and self.isWindow():
            # back to 1:1: the window takes the remote resolution again
            self.resize(self._buffer.width(), self._buffer.height())
        self._refreshCursor()
        self.update()

    def isKeepAspectRatio(self):
        """@return: {bool}"""
        return self._keepAspect

    def setKeepAspectRatio(self, keep):
        """@param keep: {bool} letterbox with black bars (True) or cover the whole widget (False, default)"""
        self._keepAspect = bool(keep)
        self._refreshCursor()
        self.update()

    def remoteSize(self):
        """@return: {tuple} (width, height) of the remote desktop"""
        if self._buffer is None:
            return (self.width(), self.height())
        return (self._buffer.width(), self._buffer.height())

    def _viewTransform(self):
        """
        @return: (offsetX, offsetY, scaleX, scaleY) from remote to widget coordinates.
        The remote desktop is always centred in the widget: scaled with smart sizing,
        1:1 otherwise (black margins when the widget is larger, the middle part when
        it is smaller).
        """
        if self._buffer is None or self._buffer.isNull():
            return 0.0, 0.0, 1.0, 1.0
        bw, bh = float(self._buffer.width()), float(self._buffer.height())
        ww, wh = float(max(1, self.width())), float(max(1, self.height()))
        if not self._smartSizing:
            # whole pixels: 1:1 drawing stays pixel exact
            return float(int((ww - bw) // 2)), float(int((wh - bh) // 2)), 1.0, 1.0
        sx, sy = ww / bw, wh / bh
        if self._keepAspect:
            sx = sy = min(sx, sy)
        return (ww - bw * sx) / 2.0, (wh - bh * sy) / 2.0, sx, sy

    def _viewRect(self):
        """@return: {QRectF} where the remote desktop is drawn in the widget"""
        ox, oy, sx, sy = self._viewTransform()
        bw, bh = self.remoteSize()
        return QRectF(ox, oy, bw * sx, bh * sy)

    def _toView(self, rect):
        """@return: {QRect} widget area showing the remote rect (with a pixel of margin when scaled)"""
        ox, oy, sx, sy = self._viewTransform()
        if not self._smartSizing:
            return rect.translated(int(ox), int(oy))
        return QRectF(ox + rect.x() * sx, oy + rect.y() * sy,
                      rect.width() * sx, rect.height() * sy).toAlignedRect().adjusted(-1, -1, 1, 1)

    def toRemote(self, x, y):
        """
        @summary: Widget coordinates -> remote desktop coordinates (clamped)
        @return: {tuple} (x, y) ints
        """
        bw, bh = self.remoteSize()
        ox, oy, sx, sy = self._viewTransform()
        x = (x - ox) / sx
        y = (y - oy) / sy
        return int(min(max(0, x), bw - 1)), int(min(max(0, y), bh - 1))

    def setAdaptor(self, adaptor):
        """
        @summary: Update the adaptor (used when reconnecting with existing widget)
        @param adaptor: {QAdaptor}
        """
        self._adaptor = adaptor
        # Drain queued bitmap updates from previous session on reconnect
        self._pendingUpdates.clear()
        self._flushScheduled = False

    def resize(self, *args, **kwargs):
        """
        @summary: Programmatic resize (does not trigger RDP reconnection)
        @param width: {int} width of widget
        @param height: {int} height of widget
        """
        self._programmaticResize = True
        super(QRemoteDesktop, self).resize(*args, **kwargs)

    def resizeEvent(self, event):
        """
        @summary: Called by Qt whenever the widget size changes
        @param event: QResizeEvent
        """
        w = event.size().width()
        h = event.size().height()
        if self._buffer is None or self._buffer.isNull():
            # first size: the frame buffer is the requested remote resolution
            self._buffer = QImage(max(1, w), max(1, h), QImage.Format.Format_RGB32)
            self._buffer.fill(QColor(0, 0, 0))
        # The frame buffer keeps the remote resolution: the desktop stays centred
        # (scaled with smart sizing). It changes when the session reports a new
        # resolution (setRemoteSize), e.g. after the resize reconnection below.
        self._refreshCursor()
        self.update()
        if self._smartSizing:
            self._programmaticResize = False
            return
        if self._programmaticResize:
            self._programmaticResize = False
            return
        # Don't trigger reconnection until the session is fully established;
        # window manager adjustments during startup would otherwise cause a
        # disruptive disconnect/reconnect cycle.
        if not self._sessionReady:
            return
        if w > 0 and h > 0:
            self._pendingSize = (w, h)
            self._resizeTimer.start(500)

    def _handleResizeTimeout(self):
        """
        @summary: Called after resize debounce period to trigger RDP reconnection
        """
        if self._pendingSize and self._adaptor:
            w, h = self._pendingSize
            self._pendingSize = None
            self._adaptor.onResizeRequest(w, h)

    @pyqtSlot(object)
    def _applyCursor(self, cursor):
        """
        @summary: Slot that applies a cursor change on the Qt main thread
        """
        self._remoteCursor = cursor
        self.setCursor(self._scaledCursor(cursor))

    def _scaledCursor(self, cursor):
        """
        @summary: With smart sizing the pointer is scaled like the desktop (as FreeRDP
        does), hot spot included, so it keeps its size relative to the remote screen
        """
        if not self._smartSizing or cursor is None:
            return cursor
        ox, oy, sx, sy = self._viewTransform()
        if abs(sx - 1.0) < 0.01 and abs(sy - 1.0) < 0.01:
            return cursor
        pixmap = cursor.pixmap()
        if pixmap is None or pixmap.isNull():
            return cursor  # standard shape (arrow, hidden...)
        w = max(1, int(round(pixmap.width() * sx)))
        h = max(1, int(round(pixmap.height() * sy)))
        scaled = pixmap.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
        hot = cursor.hotSpot()
        return QCursor(scaled, int(hot.x() * sx), int(hot.y() * sy))

    def _refreshCursor(self):
        """Re-apply the remote pointer after the scale changed."""
        if self._remoteCursor is not None:
            self.setCursor(self._scaledCursor(self._remoteCursor))

    def setRemoteSizeThreadSafe(self, width, height):
        """@summary: setRemoteSize from any thread"""
        self._remoteSizeSignal.emit(int(width), int(height))

    @pyqtSlot(int, int)
    def setRemoteSize(self, width, height):
        """
        @summary: The remote desktop resolution: the frame buffer follows it and is
        shown centred in the widget (scaled to it with smart sizing).
        """
        if width <= 0 or height <= 0:
            return
        if self._buffer is not None and (self._buffer.width(), self._buffer.height()) == (width, height):
            return
        old = self._buffer
        self._buffer = QImage(width, height, QImage.Format.Format_RGB32)
        self._buffer.fill(QColor(0, 0, 0))
        if old is not None and not old.isNull():
            with QPainter(self._buffer) as qp:
                qp.drawImage(0, 0, old)
        self._refreshCursor()
        self.update()
        self._notifyDamage(self._buffer.rect())

    def setCursorThreadSafe(self, cursor):
        """
        @summary: Thread-safe cursor update — safe to call from Twisted thread
        """
        self._cursorSignal.emit(cursor)

    def paintEvent(self, e):
        """
        @summary: Call when Qt renderer engine estimate that is needed
        @param e: QEvent
        """
        #draw only the dirty rect to avoid blitting the full buffer every frame
        if self._buffer is None:
            return
        rect = e.rect()
        with QPainter(self) as qp:
            if not self._smartSizing:
                ox, oy, sx, sy = self._viewTransform()
                view = self._buffer.rect().translated(int(ox), int(oy))  # centred, 1:1
                inside = rect.intersected(view)
                if inside != rect:
                    qp.fillRect(rect, QColor(0, 0, 0))  # margins around the desktop
                if not inside.isEmpty():
                    qp.drawImage(inside, self._buffer, inside.translated(-int(ox), -int(oy)))
                return
            view = self._viewRect()
            # letterbox bars: black wherever the exposed area is not fully covered
            if not view.contains(QRectF(rect)):
                qp.fillRect(rect, QColor(0, 0, 0))
            # source part of the remote desktop behind the exposed area
            area = QRectF(rect).intersected(view)
            if area.isEmpty():
                return
            ox, oy, sx, sy = self._viewTransform()
            src = QRectF((area.x() - ox) / sx, (area.y() - oy) / sy, area.width() / sx, area.height() / sy)
            # whole source pixels plus a margin: smooth filtering then uses the same
            # neighbours as a full repaint (no seams between partial repaints)
            src = src.toAlignedRect().adjusted(-2, -2, 2, 2).intersected(self._buffer.rect())
            dst = QRectF(ox + src.x() * sx, oy + src.y() * sy, src.width() * sx, src.height() * sy)
            qp.setClipRect(rect)
            qp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            qp.drawImage(dst, self._buffer, QRectF(src))

    def mouseMoveEvent(self, event):
        """
        @summary: Call when mouse move
        @param event: QMouseEvent
        """
        if self._adaptor:
            pos = event.pos()
            self._pendingMousePos = self.toRemote(pos.x(), pos.y())
            if not self._mouseMoveTimer.isActive():
                self._mouseMoveTimer.start()

    def _flushMouseMove(self):
        """
        @summary: Timer callback that sends the latest pending mouse position.
        Coalesces rapid mouse-move events to avoid flooding the RDP connection.
        """
        if self._adaptor and self._pendingMousePos is not None:
            x, y = self._pendingMousePos
            self._pendingMousePos = None
            self._adaptor.sendMouseMoveCoords(x, y)

    def mousePressEvent(self, event):
        """
        @summary: Call when button mouse is pressed
        @param event: QMouseEvent
        """
        if (event.button() == Qt.MouseButton.RightButton and
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            # Shift + right-click: the client's own menu (a plain right-click
            # goes to the remote machine), like PyFreeRdpApp
            self._showMenu(event.globalPosition().toPoint() if hasattr(event, 'globalPosition')
                           else event.globalPos())
            return
        if self._adaptor:
            self._adaptor.sendMouseEvent(event, True)

    def _showMenu(self, pos):
        menu = QMenu(self)
        info = menu.addAction(u'Session info...')
        shot = menu.addAction(u'Screenshot...')
        send = menu.addAction(u'Send files to the remote clipboard...')
        menu.addSeparator()
        control = None
        encomspLayer = getattr(getattr(self._adaptor, '_controller', None), '_encomspLayer', None)
        if encomspLayer is not None and encomspLayer.participants:
            control = menu.addAction(u'View only' if encomspLayer.hasControl else u'Request control')
        smart = menu.addAction(u'Smart sizing')
        smart.setCheckable(True)
        smart.setChecked(self._smartSizing)
        aspect = menu.addAction(u'Keep aspect ratio (black bars)')
        aspect.setCheckable(True)
        aspect.setChecked(self._keepAspect)
        aspect.setEnabled(self._smartSizing)
        chosen = getattr(menu, 'exec')(pos) if hasattr(menu, 'exec') else menu.exec_(pos)
        if control is not None and chosen is control:
            reactor.callFromThread(self._adaptor._controller.setRemoteControl, not encomspLayer.hasControl)
        if chosen is smart:
            self.setSmartSizing(not self._smartSizing)
        elif chosen is aspect:
            self.setKeepAspectRatio(not self._keepAspect)
        if chosen is info:
            self.showSessionInfo()
        elif chosen is shot:
            path = QFileDialog.getSaveFileName(
                self, u'Save screenshot', u'screenshot.png', u'Images (*.png *.jpg *.bmp)')[0]
            if path:
                self.saveScreenshot(path)
        elif chosen is send:
            paths = QFileDialog.getOpenFileNames(self, u'Files to paste in the session')[0]
            if paths and self._adaptor is not None and hasattr(self._adaptor, 'sendFiles'):
                self._adaptor.sendFiles(paths)

    def showSessionInfo(self):
        """@summary: Dialog with the session report (Copy puts it on the clipboard)"""
        if self._adaptor is None or not hasattr(self._adaptor, 'getSessionReport'):
            return
        try:
            report = self._adaptor.getSessionReport()
        except Exception as e:
            report = u'Session information unavailable: %s' % e
        box = QMessageBox(self)
        box.setWindowTitle(u'Session information')
        box.setText(report.split('\n\n', 1)[0])
        box.setDetailedText(report)
        copy = box.addButton(u'Copy', QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        getattr(box, 'exec')() if hasattr(box, 'exec') else box.exec_()
        if box.clickedButton() is copy:
            QApplication.clipboard().setText(report)

    def saveScreenshot(self, path):
        """
        @summary: Save the remote screen as an image
        @return: {bool} True when saved
        """
        ok = self._buffer is not None and not self._buffer.isNull() and self._buffer.save(path)
        log.info(u'screenshot %s: %s' % (u'saved' if ok else u'FAILED', path))
        return bool(ok)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        """Files dropped on the window go on the remote clipboard (paste with Ctrl+V)."""
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths and self._adaptor is not None and hasattr(self._adaptor, 'sendFiles'):
            self._adaptor.sendFiles(paths)
            event.acceptProposedAction()
            log.info(u'clipboard: %d dropped item(s) ready, press Ctrl+V in the session' % len(paths))

    def mouseReleaseEvent(self, event):
        """
        @summary: Call when button mouse is released
        @param event: QMouseEvent
        """
        if self._adaptor:
            self._adaptor.sendMouseEvent(event, False)

    def keyPressEvent(self, event):
        """
        @summary: Call when button key is pressed
        @param event: QKeyEvent
        """
        if self._adaptor:
            self._adaptor.sendKeyEvent(event, True)

    def keyReleaseEvent(self, event):
        """
        @summary: Call when button key is released
        @param event: QKeyEvent
        """
        if self._adaptor:
            self._adaptor.sendKeyEvent(event, False)

    def wheelEvent(self, event):
        """
        @summary: Call on wheel event
        @param event:    QWheelEvent
        """
        if self._adaptor:
            self._adaptor.sendWheelEvent(event)

    def event(self, event):
        """
        @summary: Touch events go to MS-RDPEI when available; otherwise Qt turns
        them into mouse events (the default behaviour)
        """
        T = QEvent.Type
        if event.type() in (T.TouchBegin, T.TouchUpdate, T.TouchEnd, T.TouchCancel):
            if self._adaptor is not None and hasattr(self._adaptor, 'sendTouchEvent') and \
                    self._adaptor.sendTouchEvent(event):
                event.accept()
                return True
        return super(QRemoteDesktop, self).event(event)

    def tabletEvent(self, event):
        """
        @summary: Pen events go to MS-RDPEI (protocol 2.0) when available, otherwise
        they are ignored here and Qt makes mouse events of them
        """
        if self._adaptor is not None and hasattr(
                self._adaptor, 'sendTabletEvent') and self._adaptor.sendTabletEvent(event):
            event.accept()
        else:
            event.ignore()

    def closeEvent(self, event):
        """
        @summary: Call when widget is closed
        @param event: QCloseEvent
        """
        log.debug(u'QRemoteDesktop.closeEvent fired:\n%s' % u''.join(format_stack()))
        self.userClosed = True
        callback = getattr(self, 'closedWithoutSession', None)
        if self._adaptor is None and callback is not None:
            callback()  # still connecting: nothing to disconnect, just stop
        elif self._adaptor is not None:
            # safety net: the disconnect closes the socket within 2 s
            QTimer.singleShot(5000, QApplication.quit)
        if self._adaptor:
            self._adaptor.closeEvent(event)
