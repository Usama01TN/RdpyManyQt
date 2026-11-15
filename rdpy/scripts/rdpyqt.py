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
example of use rdpy as rdp client.
"""
from __future__ import absolute_import, division, print_function
from rdpy.ui.qt import RDPClientQt, QRemoteDesktop, _get_qt_invoker
from rdpy.core.error import RDPSecurityNegoFail
from rdpy.ui.qreactor import install
from rdpy.protocol.rdp import rdp
from rdpy.core import log
from ManyQt.QtWidgets import QApplication
from getopt import getopt, GetoptError
from traceback import format_stack
from os import environ, pathsep
from socket import gethostname
from os.path import dirname
from getpass import getpass
import sys

# IMPORTANT: order matters here.
#
# 1) Create the QApplication *before* installing qreactor so that qreactor
#    binds to our GUI-capable QApplication instead of constructing its own
#    headless QCoreApplication (which would prevent the GUI from running and
#    cause the program to appear "blocked").
#
# 2) Install qreactor *before* importing any rdpy module: rdpy.ui.qt does
#    ``from twisted.internet import reactor`` at module load time, which would
#    otherwise install the default selector reactor and make qreactor.install()
#    fail with ReactorAlreadyInstalledError.
_app = QApplication(sys.argv)  # type: QApplication
install()
# set_level() also clears the fast debug flag checked in the hot paths
log.set_level(log.Level.INFO)


def log_runtime_info():
    """
    @summary: Log what the decoding / playback speed depends on, so a slow
    setup (pure Python fallbacks, no H.264, no audio backend) is visible
    """
    try:
        import ManyQt
        binding = getattr(ManyQt, 'USED_API', None) or u'?'
    except Exception:
        binding = u'?'
    try:
        from ManyQt import QtMultimedia
        qtAudio = (u'Qt6 QAudioSink' if hasattr(QtMultimedia.QAudioFormat, 'setSampleFormat') else u'Qt5 QAudioOutput')
    except Exception as e:
        qtAudio = u'unavailable (%s)' % e
    from rdpy.protocol.rdp import avc
    if avc.is_available():
        import av
        h264 = u'PyAV %s' % getattr(av, '__version__', '?')
    else:
        h264 = u'unavailable: pip install av'
    compiled, python = [], []
    from importlib import import_module
    for name in (u'rdpy.core.rle', u'rdpy.protocol.rdp.zgfx', u'rdpy.protocol.rdp.avc',
                 u'rdpy.protocol.rdp.rfx_progressive', u'rdpy.protocol.rdp.rlgr1_decode'):
        try:
            mod = import_module(name)
            (python if (getattr(mod, '__file__', '') or '').endswith('.py') else compiled).append(
                name.rsplit('.', 1)[1])
        except Exception:
            python.append(name.rsplit('.', 1)[1])
    import rdpy
    log.info(u'RdpyManyQt %s (%s)' % (getattr(rdpy, '__version__', '?'), dirname(rdpy.__file__)))
    log.info(u'Qt binding: %s, audio: %s, H.264: %s' % (binding, qtAudio, h264))
    if python:
        log.warning(u'not compiled (slow pure Python decoding): %s - build them with '
                    u'\'pip install .\' (needs a C compiler / MSVC Build Tools)' % u', '.join(python))
    else:
        log.info(u'compiled decoders: %s' % u', '.join(compiled))


class RDPClientQtRecorder(RDPClientQt):
    """
    @summary: Widget with record session.
    """

    def __init__(self, controller, width, height):
        """
        @param controller: {RDPClientController} RDP controller
        @param width: {int} width of widget
        @param height: {int} height of widget
        """
        RDPClientQt.__init__(self, controller, width, height)
        self._screensize = width, height

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
        log.debug(
            'RDPClientRecorder.onUpdate() {}, {}, {}, {}, {}, {}, {}, {}, {}'.format(destLeft, destTop, destRight,
                                                                                     destBottom, width, height,
                                                                                     bitsPerPixel, isCompress, data))
        RDPClientQt.onUpdate(self, destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, isCompress,
                             data)

    def onReady(self):
        """
        @summary: Call when stack is ready
        """
        log.debug(u'RDPClientRecorder.onReady()')
        RDPClientQt.onReady(self)

    def onClose(self):
        """
        @summary: Call when stack is close
        """
        log.debug(u'RDPClientRecorder.onClose()')
        RDPClientQt.onClose(self)

    def closeEvent(self, e):
        """
        @summary: Convert Qt close widget event into close stack event
        @param e: QCloseEvent
        """
        log.debug(u'RDPClientRecorder.onEvent()')
        RDPClientQt.closeEvent(self, e)


def _stopReactor():
    """Stop Twisted once (several paths may end the session at the same time)."""
    from twisted.internet import reactor
    if reactor.running:
        reactor.stop()


class RDPClientQtFactory(rdp.ClientFactory):
    """
    @summary: Factory create a RDP GUI client
    """

    def __init__(self, app, width, height, username, password, domain, keyboardType, keyboardLayout, security,
                 swap_alt_meta=False, disable_avc444=False, drives=None, options=None):
        """
        @param app: {QApplication} Qt application instance
        @param width: {integer} width of client
        @param heigth: {integer} heigth of client
        @param username: {str} username present to the server
        @param password: {str} password present to the server
        @param domain: {str} microsoft domain
        @param keyboardType: {str} name of gcc.KeyboardType attribute (e.g. IBM_101_102_KEYS)
        @param keyboardLayout: {str} name of gcc.KeyboardLayout attribute (e.g. US, FRENCH)
        @param security: {str} (ssl | rdp | nego)
        @param swap_alt_meta: {bool} swap Alt and Meta (Windows) keys
        @param disable_avc444: {bool} disable AVC444/AVC444v2 and use AVC420 only
        @param drives: {list} [(name, path)] local directories redirected as drives (rdpdr)
        @param options: {dict} optional features (see DEFAULT_OPTIONS)
        """
        self._options = dict(DEFAULT_OPTIONS)
        self._options.update(options or {})
        self.exitCode = 0
        self._consoleDone = False
        self._app = app
        self._width = width
        self._height = height
        self._username = username
        self._password = password
        self._domain = domain
        self._keyboardType = keyboardType
        self._keyboardLayout = keyboardLayout
        # FreeRDP nego: strongest enabled protocol first, then fall back
        self._securitySettings = self._options['security']
        self._attempts = self._securitySettings.attempts(bool(username and password))
        self._attempt = 0
        if not self._attempts:
            raise ValueError(u'no usable security protocol: %s%s' % (
                self._securitySettings.describe(),
                u' (NLA needs a user name and a password)' if self._securitySettings.nla else u''))
        self._swap_alt_meta = swap_alt_meta
        self._disable_avc444 = disable_avc444
        self._drives = drives or []
        self._w = None
        self._resizing = False
        self._decoderBroken = False

    def buildObserver(self, controller, addr):
        """
        @summary:  Build RFB observer
                    We use a RDPClientQt as RDP observer
        @param controller: build factory and needed by observer
        @param addr: destination address
        @return: RDPClientQt
        """
        # Disconnect old clipboard signal before creating new observer
        if hasattr(self, '_client') and self._client is not None:
            self._client.disconnectClipboard()
        if self._w is not None:
            self._client = RDPClientQt(controller, self._width, self._height, self._swap_alt_meta, widget=self._w)
        else:
            self._client = RDPClientQt(controller, self._width, self._height, self._swap_alt_meta)
            self._w = self._client.getWidget()
            self._w.setWindowTitle('rdpyqt')
            if not self._consoleMode():
                self._w.show()
        self._client.setResizeCallback(self._onResize)
        self._client.setDynamicResolution(self._options['dynamicResolution'])
        if self._options['app'] is not None and self._client.getRailManager() is not None:
            self._client.getRailManager().failed.connect(self._onRemoteAppFailed)
        controller.setUsername(self._username)
        controller.setPassword(self._password)
        controller.setDomain(self._domain)
        controller.setKeyboardType(self._keyboardType)
        controller.setKeyboardLayout(self._keyboardLayout)
        controller.setHostname(gethostname())
        name, requested = self._attempts[self._attempt]
        log.info(u'security: trying %s (%s)' % (name, self._securitySettings.describe()))
        controller.setSecurity(self._securitySettings, self._options['host'], self._options['port'], requested)
        controller.setAVC444Disabled(self._disable_avc444)
        self._applyOptions(controller)

        # Device redirection: a new controller is built for every
        # (re)connection, so drives are registered each time
        for name, path in self._drives:
            try:
                controller.addDrive(name, path)
            except (ValueError, OSError) as e:
                # e.g. a removable disk unplugged since the first connection:
                # skip this drive instead of aborting the (re)connection
                log.warning(u'drive \'%s\' (%s) is not available, not redirected: %s' % (name, path, e))
        self._addDevices(controller)

        # Wire up H.264 decoder-broken callback for auto-reconnect
        controller.setDecoderBrokenCallback(self._onDecoderBroken)

        if self._consoleMode():
            self._watchConsoleSession()
        self._watchConnection(controller)

        #set routing token for server redirection reconnection
        if self._redirectRoutingToken:
            controller.setRoutingToken(self._redirectRoutingToken)
            self._redirectRoutingToken = None

        return self._client

    def _addDevices(self, controller):
        """Printers, serial / parallel ports and smart cards chosen on the command line."""
        o = self._options
        for spec in o['printers']:
            try:
                if spec['kind'] == 'all':
                    controller.addAllPrinters()
                elif spec['kind'] == 'file':
                    controller.addPrintToFolder(spec['folder'], spec['driver'], spec['isDefault'])
                else:
                    controller.addPrinter(spec['name'], spec['driver'], spec['isDefault'])
            except (ValueError, OSError) as e:
                log.warning(u'printer %s not redirected: %s' % (spec['name'] or spec['folder'] or '*', e))
        for name, path, driver, permissive in o['serialPorts']:
            try:
                controller.addSerialPort(name, path, driver, permissive)
            except (ValueError, OSError) as e:
                log.warning(u'serial port %s (%s) not redirected: %s' % (name, path, e))
        for name, path in o['parallelPorts']:
            try:
                controller.addParallelPort(name, path)
            except (ValueError, OSError) as e:
                log.warning(u'parallel port %s (%s) not redirected: %s' % (name, path, e))
        if o['smartcard'] is not None:
            try:
                controller.addSmartcard(o['smartcard'])
            except (ValueError, OSError) as e:
                log.warning(u'smart cards not redirected: %s' % e)
        for spec in o['usb']:
            try:
                for name in controller.addUsbDevice(spec):
                    log.info(u'USB: %s' % (u'devices plugged in during the session are redirected' if name == 'auto'
                                           else u'redirecting %s' % name))
            except (ValueError, OSError) as e:
                log.warning(u'USB %s not redirected: %s' % (spec, e))

    def _applyOptions(self, controller):
        """Optional features chosen on the command line."""
        o = self._options
        controller.setColorDepth(o['bpp'])
        if o['gfx'] == 'off':
            controller.setGraphicsPipeline(False)
        controller.setClipboardMode(o['clipboard'])
        controller.setSoundEnabled(o['sound'])
        controller.setMicrophoneEnabled(o['microphone'])
        if o['shell']:
            controller.setAlternateShell(o['shell'])
        if o['workdir']:
            controller.setWorkingDirectory(o['workdir'])
        controller.setDynamicResolution(o['dynamicResolution'])
        if (o['scaleDesktop'], o['scaleDevice']) != (100, 100):
            controller.setScaleFactors(o['scaleDesktop'], o['scaleDevice'])
        if o['app'] is not None:
            controller.setRemoteApp(o['app'], o['appHidef'])
        # FreeRDP channels: advanced input, ssh agent, camera, Remote Assistance
        controller.setAdvancedInput(o['advancedInput'])
        if o['sshAgent']:
            controller.setSshAgent(True)
        if o['camera'] is not None:
            from rdpy.protocol.rdp.channels import rdpecam
            source = rdpecam.TestPatternSource() if o['camera'] == 'test' else rdpecam.QtCameraSource(o['camera'])
            controller.setCameraRedirection(True, source)
        if o['assistance'] is not None:
            controller.setRemoteAssistance(o['assistance'], o['assistanceExpert'])
        if o['video']:
            controller.setVideo(True)
        if o['geometry']:
            controller.setGeometryTracking(True)
        controller._requestControl = o['requestControl']
        if o['encomsp']:
            controller.setEncomsp(True, o['requestControl'])
        if o['multimedia']:
            from rdpy.protocol.rdp.channels import tsmf
            from rdpy.ui.qt import _get_qt_invoker
            invoker = _get_qt_invoker()
            controller.setMultimediaRedirection(
                True, lambda rate, channels: tsmf.AudioOutPlayer(rate, channels, invoker.invoke)
                if self._options['sound'] else None)
        if o['webauthn']:
            from rdpy.ui.qt import askSecurityKeyPin, showSecurityKeyNotification
            controller.setWebAuthn(True, askSecurityKeyPin, showSecurityKeyNotification)
        controller.setEcho(o['echo'])
        controller.setMultitouch(o['multitouch'])
        if o['rdp2tcp']:
            controller.setRdp2tcp(o['rdp2tcp'])
        if o['location'] is not None:
            if o['location'] == 'auto':
                controller.setLocation(enabled=True)
                from rdpy.protocol.rdp.channels import location
                self._positionSource = location.QtPositionSource(controller._location)
                self._positionSource.start()
            else:
                controller.setLocation(*o['location'])

    # ---- connection timeout ----

    def _watchConnection(self, controller):
        """
        Abort when the RDP connection sequence (security, licensing,
        capabilities) has not completed --connect-timeout after the TCP
        connection (FreeRDP TcpConnectTimeout bounds the whole connect too).
        """
        from twisted.internet import reactor
        seconds = self._options['connectTimeout']

        def check():
            if not controller._isReady and not getattr(controller, '_closing', False):
                log.warning(u'connection timed out after %gs' % seconds)
                controller.abort(u'connection timed out: no answer from the server within %g s '
                                 u'(--connect-timeout)' % seconds)

        reactor.callLater(seconds, check)

    # ---- console mode: --info / --screenshot (PyFreeRdpApp) ----

    def _consoleMode(self):
        return bool(self._options['info'] or self._options['screenshot'])

    def _watchConsoleSession(self):
        """Wait for the first picture, then the --wait delay, then report and exit."""
        from twisted.internet import reactor
        from time import time
        start = time()

        def check():
            if self._consoleDone:
                return
            if self._w is not None and self._w._sessionReady:
                reactor.callLater(self._options['wait'], self._finishConsole)
            elif time() - start > self._options['timeout']:
                log.warning(u'no picture from the server after %ds' % self._options['timeout'])
                self._finishConsole()
            else:
                reactor.callLater(0.5, check)

        reactor.callLater(0.5, check)

    def _finishConsole(self):
        if self._consoleDone:
            return
        self._consoleDone = True
        o = self._options
        if o['screenshot'] and not self._w.saveScreenshot(o['screenshot']):
            self.exitCode = 3
        if o['info']:
            if o['json']:
                from json import dumps
                print(dumps(self._client.getSessionInfo(), indent=2, default=str))
            else:
                print(self._client.getSessionReport())
        self._w.userClosed = True
        self._client._controller.close()

    # ---- disconnections ----

    def _endReason(self, reason):
        """The server's reason (Set Error Info) or the network error."""
        error = None
        if getattr(self, '_client', None) is not None:
            verifier = self._client._controller.getCertificateVerifier()
            if verifier is not None and verifier.rejection:
                return verifier.rejection
            protocolError = self._client._controller.getProtocolError()
            if protocolError:
                return protocolError
            error = self._client._controller.getLastError()
        if error:
            return error[1]
        from twisted.internet.error import TimeoutError
        value = getattr(reason, 'value', reason)
        if isinstance(value, TimeoutError):
            return u'no answer from the server within %g s (--connect-timeout)' % self._options['connectTimeout']
        return str(value) if value else u'the connection was closed'

    def _askReconnect(self, connector, title, reason):
        """Dialog with the cause, Reconnect / Close (PyFreeRdpApp DisconnectDialog)."""
        from ManyQt import QtCore, QtWidgets
        from twisted.internet import reactor

        def ask():
            box = QtWidgets.QMessageBox(self._w)
            box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            box.setWindowTitle(u'rdpyqt')
            box.setText(title)
            box.setInformativeText(reason)
            again = box.addButton(u'Reconnect', QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            verifier = None
            if getattr(self, '_client', None) is not None:
                verifier = self._client._controller.getCertificateVerifier()
            trust = None
            if verifier is not None and verifier.rejection and verifier.certificate is not None:
                from rdpy.protocol.rdp import security
                box.setDetailedText(u'SHA-256 %s\nSubject: %s\nIssuer: %s' % (
                    security.fingerprint(verifier.certificate),
                    verifier.certificate.get_subject().CN, verifier.certificate.get_issuer().CN))
                trust = box.addButton(u'Trust this certificate', QtWidgets.QMessageBox.ButtonRole.YesRole)
            box.addButton(QtWidgets.QMessageBox.StandardButton.Close)
            getattr(box, 'exec')() if hasattr(box, 'exec') else box.exec_()
            if trust is not None and box.clickedButton() is trust:
                verifier.trustCurrent()
                if self._options['security'].certificate == 'deny':
                    self._options['security'].certificate = 'tofu'  # the user accepted it
                reactor.callLater(0, connector.connect)
            elif box.clickedButton() is again:
                reactor.callLater(0, connector.connect)
            else:
                _stopReactor()
                self._app.quit()

        # outside the Twisted callback: the dialog runs its own event loop
        QtCore.QTimer.singleShot(0, ask)

    def _onDecoderBroken(self):
        """Called from the Twisted thread when the H.264 decoder cannot recover.

        Triggers an automatic reconnect so the user does not have to manually
        close and reopen the window after a video freeze.
        """
        log.warning(u'H.264 decoder broken — scheduling auto-reconnect')
        self._decoderBroken = True
        from twisted.internet import reactor
        reactor.callLater(0, self._client._controller.close)

    def _onRemoteAppFailed(self, message):
        """The RemoteApp program did not start: say why and end the session."""
        log.error(message)
        self._remoteAppFailed = message
        if not self._consoleMode():
            from rdpy.ui.rail_qt import showRemoteAppError
            showRemoteAppError(None, message)
        if self._w is not None:
            self._w.userClosed = True
        from twisted.internet import reactor
        reactor.callLater(0, self._client._controller.close)

    def _remoteAppEnded(self):
        """RemoteApp: the program exited and the server logged off / disconnected the session."""
        if self._options['app'] is None or getattr(self, '_client', None) is None:
            return False
        error = self._client._controller.getLastError()
        return getattr(self, '_remoteAppFailed', None) is not None or \
            (error is not None and error[0] in (0x1, 0x2, 0xB, 0xC))

    def _onResize(self, width, height):
        """
        @summary: Called when user resizes the window, triggers reconnection
        """
        self._width = width
        self._height = height
        self._resizing = True
        # Close must be called from the Twisted thread, not the Qt timer thread
        from twisted.internet import reactor
        log.debug(u'_onResize: scheduling close() from Qt/timer thread:\n%s' % u''.join(format_stack()))
        reactor.callFromThread(self._client._controller.close)

    def clientConnectionLost(self, connector, reason):
        """
        @summary: Connection lost event
        @param connector: twisted connector use for rdp connection (use reconnect to restart connection)
        @param reason: str use to advertise reason of lost connection
        """
        if self._redirectRoutingToken:
            log.info(u'Reconnecting with routing token for server redirection')
            connector.connect()
            return
        if self._decoderBroken:
            log.info(u'Reconnecting after H.264 decoder failure')
            self._decoderBroken = False
            connector.connect()
            return
        if self._resizing:
            log.info(u'Reconnecting with new screen size %dx%d' % (self._width, self._height))
            self._resizing = False
            connector.connect()
            return
        error = self._client._controller.getProtocolException() if self._client else None
        if (isinstance(error, RDPSecurityNegoFail) or reason.type == RDPSecurityNegoFail) and \
                self._attempt + 1 < len(self._attempts) and not self._client._controller._isReady:
            failed = self._attempts[self._attempt][0]
            self._attempt += 1
            log.info(u'security: %s refused (%s), falling back to %s' %
                     (failed, error or reason.value, self._attempts[self._attempt][0]))
            connector.connect()
            return
        from rdpy.ui.qt import isQuitting
        if self._remoteAppEnded():
            log.info(u'the RemoteApp session ended: %s' % self._endReason(reason))
            if self._w is not None:
                self._w.userClosed = True
        if (self._w is not None and self._w.userClosed) or isQuitting():
            log.info(u'disconnected')
        else:
            log.info(u'connection lost: %s' % self._endReason(reason))
        from rdpy.ui.qt import isQuitting
        if not self._consoleMode() and self._w is not None and not self._w.userClosed and not isQuitting():
            self._askReconnect(connector, u'The remote session ended.', self._endReason(reason))
            return
        if self._consoleMode() and not self._consoleDone:
            print(u'connection lost: %s' % self._endReason(reason), file=sys.stderr)
            self.exitCode = 2
        _stopReactor()
        # quit() is documented thread-safe in Qt; posts a quit event to the main loop
        self._app.quit()

    def clientConnectionFailed(self, connector, reason):
        """
        @summary: Connection failed event
        @param connector: twisted connector use for rdp connection (use reconnect to restart connection)
        @param reason: str use to advertise reason of lost connection
        """
        log.info(u'connection failed: %s' % self._endReason(reason))
        from rdpy.ui.qt import isQuitting
        if not self._consoleMode() and self._w is not None and not self._w.userClosed and not isQuitting():
            self._askReconnect(connector, u'Could not connect to the server.', self._endReason(reason))
            return
        if self._consoleMode():
            print(u'Connection failed: %s' % self._endReason(reason), file=sys.stderr)
            self.exitCode = 2
        _stopReactor()
        self._app.quit()


def help():
    print("""
    Usage: rdpy-rdpclient [options] ip[:port]"
    \t-u: user name
    \t-p: password
    \t-d: domain
    \t-w: width of screen [default : 1280]
    \t-h: height of screen [default : 1024]
    \t-kt: keyboard type (e.g. IBM_101_102_KEYS) [default : IBM_101_102_KEYS]
    \t-kl: keyboard layout (e.g. US, FRENCH) [default : US]
    \t--swap-alt-meta: swap Alt and Meta (Windows/Super/Command) keys [default : False]
    \t--disable-avc444: disable AVC444/AVC444v2 and use AVC420 only [default : False]
    \t--drive [NAME=]PATH: redirect a local directory as a drive, can be repeated
    \t--share DIR[=NAME]: same as --drive (PyFreeRdpApp syntax)
    \tDevice redirection (FreeRDP /printer, /serial, /parallel, /smartcard):
    \t--printer NAME[,DRIVER][,default]: redirect a local printer (CUPS queue, Windows
    \t    printer); DRIVER is the Windows driver the server renders with [default :
    \t    MS Publisher Imagesetter (PostScript), Microsoft Print to PDF on macOS 14+];
    \t    can be repeated
    \t--printer '*', --printers: redirect every local printer
    \t--printer file:FOLDER[,DRIVER]: a printer that saves the documents in FOLDER
    \t    (PDF with the default Microsoft Print to PDF driver)
    \t--serial NAME=PATH[,DRIVER][,permissive]: redirect a serial port, e.g.
    \t    COM1=/dev/ttyUSB0, COM2=COM5, COM3=socket://host:7000 (pyserial URL);
    \t    DRIVER Serial|SerCx|SerCx2; needs pyserial; can be repeated
    \t--parallel NAME=PATH: redirect a parallel port, e.g. LPT1=/dev/usb/lp0
    \t--smartcard: redirect the local smart card readers (needs pyscard and a PC/SC
    \t    service: pcscd on Linux)
    \t--smartcard-filter TEXT[,TEXT]: only the readers whose name contains TEXT
    \t--usb VVVV:PPPP | id:VVVV:PPPP[#VVVV:PPPP] | addr:BUS:ADDRESS[#BUS:ADDRESS] | auto:
    \t    redirect local USB devices (FreeRDP /usb): by vendor:product id (hexadecimal), by
    \t    bus and device number (as lsusb prints them), or auto = devices plugged in during
    \t    the session; can be repeated; needs python-libusb1 (pip install "rdpymanyqt[usb]")
    \t    and access to the device (Linux: udev rule / plugdev, Windows: WinUSB driver)
    \t--usb-list: list the local USB devices and exit
    \tFreeRDP channels:
    \t--no-advanced-input: do not use FreeRDP's advanced input channel (FreeRDP servers open
    \t    it; mouse and wheel events then use it instead of the input PDUs)
    \t--ssh-agent: forward the local ssh-agent (SSH_AUTH_SOCK, or the Windows OpenSSH agent)
    \t--camera: redirect the cameras (MS-RDPECAM, H.264 or MJPEG)
    \t--camera-device NAME: redirect only the cameras whose name contains NAME
    \t--camera-test: redirect a synthetic test camera (moving colour bars)
    \t--assistance FILE: connect as the expert of a Remote Assistance invitation
    \t    (.msrcincident); the address comes from the invitation when no host is given
    \t--assistance-password PASS: the invitation password (asked when missing)
    \t--multitouch: touch screens and pens through MS-RDPEI (FreeRDP /multitouch)
    \t--location LAT,LON[,ALT] | auto: give this position to the session (MS-RDPEL);
    \t    auto: Qt Positioning
    \t--echo: answer the server's echo channel (MS-RDPEECO, round trip measures)
    \t--rdp2tcp COMMAND: TCP forwarding with the local rdp2tcp program (FreeRDP /rdp2tcp)
    \t--video: video optimized remoting (MS-RDPEVOR, FreeRDP /video): videos in the session
    \t    come as H.264 and are shown where they play (enables --geometry)
    \t--geometry: geometry tracking channel (MS-RDPEGT, FreeRDP /geometry)
    \t--encomsp: multiparty channel (MS-RDPEMSC); on with --assistance, where it requests the
    \t    control of the novice's desktop once (Shift + right-click > Request control later)
    \t--no-request-control: with --assistance, stay in view only mode
    \t--multimedia: multimedia redirection (TSMF, MS-RDPEV, FreeRDP /multimedia): Windows Media
    \t    Player audio / video decoded here (PyAV) and played where the player is
    \t--webauthn: passkey / security key sign-ins in the session use the FIDO2 keys of this
    \t    computer (MS-RDPEWA, needs `pip install fido2`; the PIN is asked in a dialog)
    \tRemoteApp (FreeRDP /app):
    \t--app PROGRAM: run PROGRAM alone, its windows shown as local windows instead of the
    \t    remote desktop: "||alias" of a published RemoteApp, or a path the server allows;
    \t    FreeRDP syntax accepted: program:||notepad,cmd:file.txt,workdir:C:\\temp
    \t--app-cmd ARGUMENTS: command line arguments of --app
    \t--app-workdir DIR: working folder of --app
    \t--app-hidef on|off: Enhanced RemoteApp, window surfaces through the graphics
    \t    pipeline [default : off]
    \t--size WIDTHxHEIGHT: desktop size (same as -w / -h)
    \t--dynamic-resolution, --no-dynamic-resolution: resizing the window changes the remote
    \t    resolution through the display control channel (FreeRDP /dynamic-resolution);
    \t    without it, or when the server does not offer it, the session reconnects with
    \t    the new size [default : on]
    \t--scale-desktop PERCENT: remote desktop scale factor, 100 - 500 [default : 100]
    \t--scale-device 100|140|180: remote device scale factor [default : 100]
    \t--bpp 15|16|24|32: colour depth [default : 32]; below 32 the legacy bitmap
    \t    updates are used (the graphics pipeline is 32-bit only)
    \t--gfx on|off: graphics pipeline (H.264, RemoteFX) or legacy updates [default : on]
    \t--clipboard off|text|files: clipboard sharing [default : files = text + files]
    \t--no-clipboard: same as --clipboard off
    \t--no-sound: do not play the remote sound
    \t--microphone: redirect the local microphone to the server
    \t--audio-latency MS: smallest audio buffer [default : 60]
    \t--shell COMMAND: start COMMAND instead of the desktop (server policy must allow it)
    \t--workdir DIR: working folder of --shell
    \t--info: console only, print the session report (server, logon, security,
    \t    certificate...) once connected, then exit
    \t--json: with --info, print the report as JSON
    \t--screenshot FILE: console only, save the remote screen to FILE and exit
    \tSecurity (FreeRDP rdpSettings NlaSecurity / TlsSecurity / RdpSecurity / IgnoreCertificate):
    \t--sec nla|tls|rdp: offer only this protocol (FreeRDP /sec:)
    \t--sec-nla on|off, --sec-tls on|off, --sec-rdp on|off: enable / disable one
    \t    protocol [default : all on; the strongest is tried first, then the next]
    \t--security max: NLA + TLS only, no legacy RDP security, strict certificate check
    \t--security compat: everything allowed, self-signed certificates accepted
    \t--cert ignore|tofu|deny: server certificate check [default : tofu]
    \t    ignore: accept any; tofu: trusted authority, else trust on first use and
    \t    refuse a changed certificate; deny: trusted authority and host name only
    \t--cert-ignore: same as --cert ignore
    \tLogon events (run COMMAND without waiting; RDP_HOST, RDP_PORT, RDP_USER and
    \tRDP_LOGON_MESSAGE are set in its environment):
    \t--on-login-screen COMMAND: when the Windows logon screen is displayed
    \t--on-desktop COMMAND: when the user is logged on and the desktop starts
    \t--on-logon-error COMMAND: when the server reports a logon error / warning
    \t--on-transfer-start COMMAND: a clipboard file transfer starts (RDP_TRANSFER_DIRECTION
    \t    upload|download, RDP_TRANSFER_ITEMS, RDP_TRANSFER_BYTES)
    \t--on-transfer-done COMMAND: a clipboard transfer ended or the server finished
    \t    reading / writing a file of a shared drive (RDP_TRANSFER_KIND clipboard|drive,
    \t    RDP_TRANSFER_PATHS, RDP_TRANSFER_STATUS completed|partial|cancelled)
    \t--on-print COMMAND: a document was printed through a redirected printer
    \t    (RDP_PRINT_PRINTER, RDP_PRINT_TITLE, RDP_PRINT_BYTES, RDP_PRINT_DESTINATION,
    \t    RDP_PRINT_STATUS printed|failed, RDP_PRINT_ERROR)
    \t--smart-sizing: scale the remote desktop to cover the whole window (mstsc smart sizing,
    \t    FreeRDP /smart-sizing): resizing the window keeps the remote resolution;
    \t    Shift + right-click > Smart sizing switches it at any time
    \t--stretch: same as --smart-sizing
    \t--keep-aspect: with smart sizing, keep the aspect ratio with black bars
    \t--connect-timeout MS: give up when the connection is not established in
    \t    MS milliseconds (TCP connect and RDP connection sequence) [default : 15000]
    \t--timeout MS: drop a connection whose sent data is not acknowledged in MS
    \t    milliseconds (FreeRDP /timeout, TCP keep-alive detects dead servers) [default : 9000]
    \t--wait SECONDS: with --info / --screenshot, time to wait after the first
    \t    picture [default : 3]
    \tExit codes: 0 ok, 1 bad arguments, 2 connection failed, 3 screenshot failed
    """)


DEFAULT_OPTIONS = {
    'bpp': 32, 'gfx': 'on', 'clipboard': 'files', 'sound': True, 'microphone': False,
    'shell': '', 'workdir': '', 'info': False, 'json': False, 'screenshot': '',
    'smartSizing': False, 'keepAspect': False,
    'wait': 3.0, 'timeout': 60, 'connectTimeout': 15.0, 'security': None, 'host': '', 'port': 3389,
    # device redirection: printer descriptions (rdpdr_printer.parsePrinterSpec),
    # (name, path, driver, permissive) serial ports, (name, path) parallel ports,
    # smart card reader filters (None: no smart card redirection)
    'printers': [], 'serialPorts': [], 'parallelPorts': [], 'smartcard': None,
    # display control (MS-RDPEDISP), RemoteApp (rail.RemoteApp, MS-RDPERP), USB (urbdrc.parseUsbSpec)
    'dynamicResolution': True, 'scaleDesktop': 100, 'scaleDevice': 100,
    'app': None, 'appHidef': False, 'usb': [],
    # FreeRDP channels: ainput (on by default), sshagent, rdpecam (None: off, '': all cameras,
    # a name filter, or 'test'), remdesk (assistance.Invitation)
    'advancedInput': True, 'sshAgent': False, 'camera': None,
    'assistance': None, 'assistanceExpert': None,
    # echo (MS-RDPEECO), multi-touch / pen (MS-RDPEI), location (None, 'auto' or
    # (latitude, longitude, altitude)), rdp2tcp program
    'echo': False, 'multitouch': False, 'location': None, 'rdp2tcp': '',
    # video optimized remoting (+ geometry), geometry alone, multiparty (Remote Assistance control)
    'video': False, 'geometry': False, 'encomsp': False, 'requestControl': True,
    # WebAuthn redirection (FIDO2 security keys, MS-RDPEWA)
    'webauthn': False,
    # multimedia redirection (TSMF, MS-RDPEV)
    'multimedia': False,
}


def list_usb_devices():
    """--usb-list: the local USB devices (bus, address, ids, name), for --usb"""
    try:
        from rdpy.protocol.rdp.channels import urbdrc_libusb
        devices = urbdrc_libusb.listDevices()
    except OSError as e:
        print(e)
        return 1
    if not devices:
        print(u'no USB device found (or no access to them)')
        return 0
    print(u'%-8s %-8s %-10s %-6s %s' % (u'bus', u'address', u'id', u'class', u'name'))
    for d in devices:
        print(u'%-8d %-8d %04x:%04x  %02x     %s' % (d.bus, d.address, d.vid, d.pid, d.deviceClass, d.name or ''))
    print('redirect with --usb VVVV:PPPP or --usb addr:BUS:ADDRESS')
    return 0


def parse_drive(arg):
    """
    @summary: Parse a --drive argument: "NAME=PATH" or "PATH" (name = directory name)
    @param arg: {str}
    @return: {tuple} (name, absolute path)
    @raise ValueError: invalid argument or path is not a directory
    """
    from os.path import abspath, expanduser, isdir
    name, sep, path = arg.partition(u'=')
    if not sep:
        name, path = u'', arg
    if not path.strip():
        # os.path.abspath("") is the working directory: never share it implicitly
        raise ValueError(u'--drive: empty path in \'%s\'' % arg)
    path = abspath(expanduser(path))
    if not isdir(path):
        raise ValueError(u'--drive: not a directory: %s' % path)
    if not name:
        from rdpy.protocol.rdp.channels.rdpdr_drive import defaultDriveName
        name = defaultDriveName(path)
    return name, path


def main():
    username = u''
    password = u''
    domain = u''
    width = 1280
    height = 1024
    keyboardType = u'IBM_101_102_KEYS'
    keyboardLayout = u'US'
    swap_alt_meta = False
    disable_avc444 = False
    drives = []

    argv = []
    for a in sys.argv[1:]:
        if a.startswith(u'-kt'):
            argv.append(u'--kt' + a[3:])
        elif a.startswith(u'-kl'):
            argv.append(u'--kl' + a[3:])
        elif a == u'-disable-avc444':
            argv.append(u'--disable-avc444')
        elif a == u'-swap-alt-meta':
            argv.append(u'--swap-alt-meta')
        elif a == u'-drive' or a.startswith(u'-drive='):
            argv.append(u'-' + a)
        else:
            argv.append(a)
    options = dict(DEFAULT_OPTIONS)
    for key in ('printers', 'serialPorts', 'parallelPorts', 'usb'):
        options[key] = []
    appSpec, appCmd, appWorkdir = None, None, None
    assistanceFile, assistancePassword = None, None
    smartcardFilters = []
    secOnly, secPreset, certMode, secFlags = None, None, None, {}
    hooks = {}

    try:
        opts, args = getopt(argv, u'fou:p:d:w:h:r:', [
            u'kt=', u'kl=', u'swap-alt-meta', u'disable-avc444', u'drive=', u'share=', u'size=', u'bpp=', u'gfx=',
            u'clipboard=', u'no-clipboard', u'no-sound', u'microphone', u'audio-latency=', u'shell=', u'workdir=',
            u'info', u'json', u'screenshot=', u'wait=', u'help', u'connect-timeout=', u'timeout=',
            u'sec=', u'sec-nla=', u'sec-tls=', u'sec-rdp=', u'security=', u'cert=', u'cert-ignore',
            u'on-login-screen=', u'on-desktop=', u'on-logon-error=', u'on-transfer-start=', u'on-transfer-done=',
            u'smart-sizing', u'stretch', u'keep-aspect',
            u'printer=', u'printers', u'serial=', u'parallel=', u'smartcard', u'smartcard-filter=', u'on-print=',
            u'dynamic-resolution', u'no-dynamic-resolution', u'scale-desktop=', u'scale-device=',
            u'app=', u'app-cmd=', u'app-workdir=', u'app-hidef=', u'usb=', u'usb-list',
            u'no-advanced-input', u'ssh-agent', u'camera', u'camera-device=', u'camera-test',
            u'assistance=', u'assistance-password=', u'echo', u'multitouch', u'location=', u'rdp2tcp=',
            u'video', u'geometry', u'encomsp', u'no-request-control', u'webauthn', u'multimedia'])
    except GetoptError:
        help()
        sys.exit(1)
    for opt, arg in opts:
        if opt == u'-u':
            username = arg
        elif opt == u'-p':
            password = arg
        elif opt == u'-d':
            domain = arg
        elif opt == u'-w':
            width = int(arg)
        elif opt == u'-h':
            height = int(arg)
        elif opt == u'--kt':
            keyboardType = arg
        elif opt == u'--kl':
            keyboardLayout = arg
        elif opt == u'--swap-alt-meta':
            swap_alt_meta = True
        elif opt == u'--disable-avc444':
            disable_avc444 = True
        elif opt == u'--drive':
            try:
                drives.append(parse_drive(arg))
            except ValueError as e:
                print(e)
                sys.exit(1)
        elif opt == u'--share':
            # PyFreeRdpApp syntax: DIR[=NAME]
            path, sep, name = arg.rpartition(u'=') if u'=' in arg else (arg, u'', u'')
            try:
                drives.append(parse_drive(u'%s=%s' % (name, path) if sep else path))
            except ValueError as e:
                print(e)
                sys.exit(1)
        elif opt == u'--size':
            try:
                width, height = (int(v) for v in arg.lower().split(u'x'))
            except ValueError:
                print(u'--size: expected WIDTHxHEIGHT, e.g. 1920x1080')
                sys.exit(1)
        elif opt == u'--bpp':
            if arg not in (u'15', u'16', u'24', u'32'):
                print(u'--bpp: 15, 16, 24 or 32')
                sys.exit(1)
            options['bpp'] = int(arg)
        elif opt == u'--gfx':
            if arg not in (u'on', u'off'):
                print(u'--gfx: on or off')
                sys.exit(1)
            options['gfx'] = arg
        elif opt == u'--clipboard':
            if arg not in (u'off', u'text', u'files'):
                print(u'--clipboard: off, text or files')
                sys.exit(1)
            options['clipboard'] = arg
        elif opt == u'--no-clipboard':
            options['clipboard'] = 'off'
        elif opt == u'--no-sound':
            options['sound'] = False
        elif opt == u'--microphone':
            options['microphone'] = True
        elif opt == u'--audio-latency':
            from rdpy.protocol.rdp import audio_out
            audio_out._TARGET_MIN = max(0.02, int(arg) / 1000.0)
        elif opt == u'--shell':
            options['shell'] = arg
        elif opt == u'--workdir':
            options['workdir'] = arg
        elif opt == u'--info':
            options['info'] = True
        elif opt == u'--json':
            options['json'] = True
        elif opt == u'--screenshot':
            options['screenshot'] = arg
        elif opt == u'--wait':
            options['wait'] = max(0.0, float(arg))
        elif opt == u'--sec':
            if arg not in (u'nla', u'tls', u'rdp'):
                print(u'--sec: nla, tls or rdp')
                sys.exit(1)
            secOnly = arg
        elif opt in (u'--sec-nla', u'--sec-tls', u'--sec-rdp'):
            if arg not in (u'on', u'off'):
                print(u'%s: on or off' % opt)
                sys.exit(1)
            secFlags[opt[6:]] = arg == u'on'
        elif opt == u'--security':
            if arg not in (u'max', u'compat'):
                print(u'--security: max or compat')
                sys.exit(1)
            secPreset = arg
        elif opt == u'--cert':
            if arg not in (u'ignore', u'tofu', u'deny'):
                print(u'--cert: ignore, tofu or deny')
                sys.exit(1)
            certMode = arg
        elif opt == u'--smart-sizing':
            options['smartSizing'] = True
        elif opt == u'--stretch':
            options['smartSizing'] = True
            options['keepAspect'] = False
        elif opt == u'--keep-aspect':
            options['smartSizing'] = True
            options['keepAspect'] = True
        elif opt == u'--cert-ignore':
            certMode = u'ignore'
        elif opt in (u'--printer', u'--printers'):
            from rdpy.protocol.rdp.channels.rdpdr_printer import parsePrinterSpec
            try:
                options['printers'].append(parsePrinterSpec(arg if opt == u'--printer' else u'*'))
            except ValueError as e:
                print(u'--printer: %s' % e)
                sys.exit(1)
        elif opt in (u'--serial', u'--parallel'):
            from rdpy.protocol.rdp.channels.rdpdr_serial import parseSerialSpec
            from rdpy.protocol.rdp.channels.rdpdr_parallel import parseParallelSpec
            try:
                if opt == u'--serial':
                    options['serialPorts'].append(parseSerialSpec(arg))
                else:
                    options['parallelPorts'].append(parseParallelSpec(arg))
            except ValueError as e:
                print(u'%s: %s' % (opt, e))
                sys.exit(1)
        elif opt in (u'--dynamic-resolution', u'--no-dynamic-resolution'):
            options['dynamicResolution'] = opt == u'--dynamic-resolution'
        elif opt in (u'--scale-desktop', u'--scale-device'):
            try:
                value = int(arg.rstrip('%'))
            except ValueError:
                value = 0
            if (opt == u'--scale-desktop' and not 100 <= value <= 500) or \
                    (opt == u'--scale-device' and value not in (100, 140, 180)):
                print(u'--scale-desktop: 100 - 500, --scale-device: 100, 140 or 180')
                sys.exit(1)
            options['scaleDesktop' if opt == u'--scale-desktop' else 'scaleDevice'] = value
        elif opt == u'--app':
            appSpec = arg
        elif opt == u'--app-cmd':
            appCmd = arg
        elif opt == u'--app-workdir':
            appWorkdir = arg
        elif opt == u'--app-hidef':
            if arg not in (u'on', u'off'):
                print(u'--app-hidef: on or off')
                sys.exit(1)
            options['appHidef'] = arg == u'on'
        elif opt == u'--usb':
            from rdpy.protocol.rdp.channels.urbdrc import parseUsbSpec
            try:
                parseUsbSpec(arg)
            except ValueError as e:
                print(u'--usb: %s' % e)
                sys.exit(1)
            options['usb'].append(arg)
        elif opt == u'--usb-list':
            sys.exit(list_usb_devices())
        elif opt == u'--no-advanced-input':
            options['advancedInput'] = False
        elif opt == u'--ssh-agent':
            options['sshAgent'] = True
        elif opt == u'--camera':
            options['camera'] = options['camera'] or ''
        elif opt == u'--camera-device':
            options['camera'] = arg
        elif opt == u'--camera-test':
            options['camera'] = 'test'
        elif opt == u'--multimedia':
            options['multimedia'] = True
        elif opt == u'--webauthn':
            options['webauthn'] = True
        elif opt == u'--video':
            options['video'] = True
        elif opt == u'--geometry':
            options['geometry'] = True
        elif opt == u'--encomsp':
            options['encomsp'] = True
        elif opt == u'--no-request-control':
            options['requestControl'] = False
        elif opt == u'--echo':
            options['echo'] = True
        elif opt == u'--multitouch':
            options['multitouch'] = True
        elif opt == u'--rdp2tcp':
            options['rdp2tcp'] = arg
        elif opt == u'--location':
            if arg == 'auto':
                options['location'] = 'auto'
            else:
                try:
                    values = [float(v) for v in arg.split(',')]
                    if len(values) not in (2, 3) or not (-90 <= values[0] <= 90 and -180 <= values[1] <= 180):
                        raise ValueError()
                except ValueError:
                    print(u'--location: LATITUDE,LONGITUDE[,ALTITUDE] in degrees / metres, or auto')
                    sys.exit(1)
                options['location'] = tuple(values)
        elif opt == u'--assistance':
            assistanceFile = arg
        elif opt == u'--assistance-password':
            assistancePassword = arg
        elif opt == u'--smartcard':
            options['smartcard'] = smartcardFilters
        elif opt == u'--smartcard-filter':
            from rdpy.protocol.rdp.channels.rdpdr_smartcard import parseSmartcardSpec
            smartcardFilters.extend(parseSmartcardSpec(arg))
            options['smartcard'] = smartcardFilters
        elif opt in (u'--on-login-screen', u'--on-desktop', u'--on-logon-error', u'--on-transfer-start',
                     u'--on-transfer-done', u'--on-print'):
            hooks[opt[5:]] = arg
        elif opt in (u'--connect-timeout', u'--timeout'):
            try:
                ms = int(arg)
                if ms <= 0:
                    raise ValueError()
            except ValueError:
                print(u'%s: a positive number of milliseconds' % opt)
                sys.exit(1)
            if opt == u'--connect-timeout':
                options['connectTimeout'] = ms / 1000.0
            else:
                from rdpy.core import layer as _layer
                _layer.TCP_SETTINGS['ackTimeoutMs'] = ms
        elif opt == u'--help':
            help()
            sys.exit(0)

    if assistanceFile is not None:
        # FreeRDP /assistance: the invitation gives the novice's address, the ticket and the
        # PassStub; Remote Assistance runs without NLA (FreeRDP cmdline.c)
        from rdpy.protocol.rdp.channels import assistance
        if assistancePassword is None:
            assistancePassword = getpass(u'Invitation password: ')
        try:
            invitation = assistance.parseFile(assistanceFile, assistancePassword)
            target = invitation.target()
        except (assistance.AssistanceError, IOError, OSError) as e:
            print(u'--assistance: %s' % e)
            sys.exit(1)
        options['assistance'] = invitation
        options['assistanceExpert'] = username or None
        secFlags['nla'] = False
        if not args:
            host = target[0]
            args = ['[%s]:%d' % target if ':' in host else '%s:%d' % target]
        username = username or invitation.username or 'Expert'
        password = password or '*'

    if not args:
        help()
        sys.exit(1)

    if appSpec is not None or appCmd or appWorkdir:
        from rdpy.protocol.rdp.channels import rail
        try:
            app = rail.parseAppSpec(appSpec or '')
        except ValueError:
            print(u'--app: the program to start is missing')
            sys.exit(1)
        if appCmd is not None:
            app.arguments = appCmd
        if appWorkdir is not None:
            app.workingDir = appWorkdir
        if hasattr(app, 'hidef'):
            options['appHidef'] = app.hidef
        options['app'] = app

    # optional modules of the requested devices
    for wanted, module, package, what, dist in (
            (options['serialPorts'], 'serial', 'serial', 'serial port', 'pyserial'),
            (options['smartcard'] is not None, 'smartcard', 'smartcard', 'smart card', 'pyscard'),
            (options['usb'], 'usb1', 'usb', 'USB', 'python-libusb1')):
        if wanted:
            try:
                __import__(module)
            except:
                print('%s redirection needs %s: pip install "rdpymanyqt[%s]"' % (what, dist, package))
                sys.exit(1)

    # security (FreeRDP rdpSettings NlaSecurity / TlsSecurity / RdpSecurity / IgnoreCertificate)
    from rdpy.protocol.rdp import security
    if secPreset == u'max':
        settings = security.SecuritySettings.maximum()
    elif secPreset == u'compat':
        settings = security.SecuritySettings.compatible()
    elif secOnly:
        settings = security.SecuritySettings.only(secOnly)
    else:
        settings = security.SecuritySettings()
    for flag, on in secFlags.items():
        setattr(settings, flag, on)
    if certMode:
        settings.certificate = certMode
    options['security'] = settings

    if not username:
        username = input(u'User: ')
    if not password:
        password = getpass()

    if args[0].startswith('[') and ']' in args[0]:
        ip, _, rest = args[0][1:].partition(']')
        port = rest.lstrip(':') or u'3389'
    elif args[0].count(':') == 1:
        ip, port = args[0].split(':')
    else:
        ip, port = args[0], u'3389'

    # QApplication is already created at module load time (so qreactor could
    # bind to it).  Reuse that instance here.
    app = _app
    # Closing the window must disconnect first (FreeRDP freerdp_disconnect):
    # the application quits once the connection is closed, not when the last
    # window closes (that would reset the socket under the server).
    app.setQuitOnLastWindowClosed(False)

    from twisted.internet import reactor

    log_runtime_info()
    log.info(u'keyboard type set to %s' % keyboardType)
    log.info(u'keyboard layout set to %s' % keyboardLayout)
    for name, path in drives:
        log.info(u'redirecting drive \'%s\' -> %s' % (name, path))
    for spec in options['printers']:
        log.info(u'redirecting printer %s' % ({'all': 'all local printers',
                                               'file': 'to folder %s' % spec['folder']}.get(spec['kind'])
                                              or u'\'%s\'' % spec['name']))
    for name, path, driver, permissive in options['serialPorts']:
        log.info(u'redirecting serial port %s -> %s' % (name, path))
    for name, path in options['parallelPorts']:
        log.info(u'redirecting parallel port %s -> %s' % (name, path))
    if options['smartcard'] is not None:
        log.info(u'redirecting smart card readers%s' % (
            u' matching %s' % ', '.join(options['smartcard']) if options['smartcard'] else u''))
    if options['app'] is not None:
        # RemoteApp: the session desktop covers the local screens (FreeRDP /app)
        from rdpy.ui.rail_qt import RailManager
        width, height = RailManager.desktopSize()
        log.info(u'RemoteApp: starting %s (session desktop %dx%d)' % (options['app'].program, width, height))

    options['host'], options['port'] = ip, int(port)
    try:
        factory = RDPClientQtFactory(app, width, height, username, password, domain, keyboardType, keyboardLayout,
                                     u'nego',
                                     swap_alt_meta, disable_avc444, drives, options)
    except ValueError as e:
        print(e)
        sys.exit(1)

    # Pre-create the Qt invoker on the Qt main thread so the _QtInvoker QObject
    # lives on the Qt main thread.  If created lazily from the Twisted thread it
    # would be owned by that thread and QueuedConnection would not dispatch to Qt.
    _get_qt_invoker()

    # Pre-create the widget on the Qt main thread so buildObserver (which runs
    # on the Twisted thread) never has to create Qt objects directly.
    initial_widget = QRemoteDesktop(width, height, None)
    initial_widget.setWindowTitle('rdpyqt')
    try:
        from ManyQt.QtGui import QIcon
        from rdpy.ui.rdpviewer.rdpviewer import LOGO_PATH
        app.setWindowIcon(QIcon(LOGO_PATH))
    except Exception:
        pass
    if not factory._consoleMode() and options['app'] is None:
        initial_widget.show()  # RemoteApp: only the program's windows are shown
    factory._w = initial_widget

    connector = reactor.connectTCP(ip, int(port), factory, timeout=options['connectTimeout'])

    def closedWithoutSession():
        # window closed before the TCP connection exists: stop at once
        if getattr(connector, 'state', None) == 'connecting':
            connector.stopConnecting()
        if reactor.running:
            reactor.stop()
        app.quit()

    initial_widget.closedWithoutSession = closedWithoutSession
    if options['smartSizing']:
        initial_widget.setSmartSizing(True, options['keepAspect'])

    def runHook(event, message='', **extra):
        command = hooks.get(event)
        if not command:
            return
        from subprocess import Popen
        env = dict(environ, RDP_HOST=ip, RDP_PORT=str(port), RDP_USER=username,
                   RDP_LOGON_MESSAGE=message, RDP_EVENT=event)
        env.update(dict((k, str(v)) for k, v in extra.items()))
        try:
            Popen(command, shell=True, env=env)
        except OSError as e:
            log.warning(u'--on-%s: cannot run %r: %s' % (event, command, e))

    initial_widget.loginScreenReached.connect(lambda: runHook('login-screen'))
    initial_widget.desktopReached.connect(lambda: runHook('desktop'))
    initial_widget.logonError.connect(lambda message: runHook('logon-error', message))
    initial_widget.fileTransferStarted.connect(lambda direction, items, total: runHook(
        'transfer-start', RDP_TRANSFER_KIND='clipboard', RDP_TRANSFER_DIRECTION=direction,
        RDP_TRANSFER_ITEMS=items, RDP_TRANSFER_BYTES=total))
    initial_widget.fileTransferFinished.connect(lambda direction, paths, total, status: runHook(
        'transfer-done', RDP_TRANSFER_KIND='clipboard', RDP_TRANSFER_DIRECTION=direction,
        RDP_TRANSFER_PATHS=pathsep.join(paths), RDP_TRANSFER_BYTES=total, RDP_TRANSFER_STATUS=status))
    initial_widget.driveFileTransferred.connect(lambda direction, path, total: runHook(
        'transfer-done', RDP_TRANSFER_KIND='drive', RDP_TRANSFER_DIRECTION=direction,
        RDP_TRANSFER_PATHS=path, RDP_TRANSFER_BYTES=total, RDP_TRANSFER_STATUS='completed'))
    initial_widget.printJob.connect(lambda printer, title, total, destination, error: runHook(
        'print', RDP_PRINT_PRINTER=printer, RDP_PRINT_TITLE=title, RDP_PRINT_BYTES=total,
        RDP_PRINT_DESTINATION=destination, RDP_PRINT_STATUS='failed' if error else 'printed',
        RDP_PRINT_ERROR=error))
    # Drive the Twisted reactor from the Qt event loop.  reactor.runReturn()
    # returns immediately; the Qt main loop then services both Qt events and
    # Twisted I/O on the same (main) thread.
    reactor.runReturn()
    # after runReturn(): replaces Twisted's signal handlers with a clean disconnect
    from rdpy.ui.qt import installCleanExit
    installCleanExit(app)
    code = getattr(app, 'exec')()
    # the window is closed and the session disconnected: return to the console now
    from rdpy.ui.qt import forceExit
    forceExit(factory.exitCode or code or 0)


if __name__ == '__main__':
    sys.exit(main())
