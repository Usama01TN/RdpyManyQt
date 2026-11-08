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
from ManyQt.QtWidgets import QApplication
import os, sys, getopt, socket
import getpass
import traceback

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
_app = QApplication(sys.argv)

import qreactor

qreactor.install()

from rdpy.ui.qt import RDPClientQt, QRemoteDesktop, _get_qt_invoker
from rdpy.protocol.rdp import rdp
from rdpy.core.error import RDPSecurityNegoFail
import rdpy.core.log as log

# set_level() also clears the fast debug flag checked in the hot paths
log.set_level(log.Level.INFO)


def log_runtime_info():
    """
    @summary: Log what the decoding / playback speed depends on, so a slow
    setup (pure Python fallbacks, no H.264, no audio backend) is visible
    """
    try:
        import ManyQt
        binding = getattr(ManyQt, 'USED_API', None) or "?"
    except Exception:
        binding = "?"
    try:
        from ManyQt import QtMultimedia
        qtAudio = ("Qt6 QAudioSink" if hasattr(QtMultimedia.QAudioFormat, 'setSampleFormat') else "Qt5 QAudioOutput")
    except Exception as e:
        qtAudio = "unavailable (%s)" % e
    from rdpy.protocol.rdp import avc
    if avc.is_available():
        import av
        h264 = "PyAV %s" % getattr(av, '__version__', '?')
    else:
        h264 = "unavailable: pip install av"
    compiled, python = [], []
    import importlib
    for name in ("rdpy.core.rle", "rdpy.protocol.rdp.zgfx", "rdpy.protocol.rdp.avc",
                 "rdpy.protocol.rdp.rfx_progressive", "rdpy.protocol.rdp.rlgr1_decode"):
        try:
            mod = importlib.import_module(name)
            (python if (getattr(mod, '__file__', '') or '').endswith('.py') else compiled).append(
                name.rsplit('.', 1)[1])
        except Exception:
            python.append(name.rsplit('.', 1)[1])
    import rdpy
    log.info("RdpyManyQt %s (%s)" % (getattr(rdpy, '__version__', '?'), os.path.dirname(rdpy.__file__)))
    log.info("Qt binding: %s, audio: %s, H.264: %s" % (binding, qtAudio, h264))
    if python:
        log.warning("not compiled (slow pure Python decoding): %s - build them with "
                    "'pip install .' (needs a C compiler / MSVC Build Tools)" % ", ".join(python))
    else:
        log.info("compiled decoders: %s" % ", ".join(compiled))


class RDPClientQtRecorder(RDPClientQt):
    """
    @summary: Widget with record session
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
        log.debug("RDPClientRecorder.onReady()")
        RDPClientQt.onReady(self)

    def onClose(self):
        """
        @summary: Call when stack is close
        """
        log.debug("RDPClientRecorder.onClose()")
        RDPClientQt.onClose(self)

    def closeEvent(self, e):
        """
        @summary: Convert Qt close widget event into close stack event
        @param e: QCloseEvent
        """
        log.debug("RDPClientRecorder.onEvent()")
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
            raise ValueError("no usable security protocol: %s%s" % (
                self._securitySettings.describe(),
                " (NLA needs a user name and a password)" if self._securitySettings.nla else ""))
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
        controller.setUsername(self._username)
        controller.setPassword(self._password)
        controller.setDomain(self._domain)
        controller.setKeyboardType(self._keyboardType)
        controller.setKeyboardLayout(self._keyboardLayout)
        controller.setHostname(socket.gethostname())
        name, requested = self._attempts[self._attempt]
        log.info("security: trying %s (%s)" % (name, self._securitySettings.describe()))
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
                log.warning("drive '%s' (%s) is not available, not redirected: %s" % (name, path, e))

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
                log.warning("connection timed out after %gs" % seconds)
                controller.abort("connection timed out: no answer from the server within %g s "
                                 "(--connect-timeout)" % seconds)

        reactor.callLater(seconds, check)

    # ---- console mode: --info / --screenshot (PyFreeRdpApp) ----

    def _consoleMode(self):
        return bool(self._options['info'] or self._options['screenshot'])

    def _watchConsoleSession(self):
        """Wait for the first picture, then the --wait delay, then report and exit."""
        from twisted.internet import reactor
        import time
        start = time.time()

        def check():
            if self._consoleDone:
                return
            if self._w is not None and self._w._sessionReady:
                reactor.callLater(self._options['wait'], self._finishConsole)
            elif time.time() - start > self._options['timeout']:
                log.warning("no picture from the server after %ds" % self._options['timeout'])
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
                import json
                print(json.dumps(self._client.getSessionInfo(), indent=2, default=str))
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
        from twisted.internet import error as neterror
        value = getattr(reason, 'value', reason)
        if isinstance(value, neterror.TimeoutError):
            return "no answer from the server within %g s (--connect-timeout)" % self._options['connectTimeout']
        return str(value) if value else "the connection was closed"

    def _askReconnect(self, connector, title, reason):
        """Dialog with the cause, Reconnect / Close (PyFreeRdpApp DisconnectDialog)."""
        from ManyQt import QtCore, QtWidgets
        from twisted.internet import reactor

        def ask():
            box = QtWidgets.QMessageBox(self._w)
            box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            box.setWindowTitle("rdpyqt")
            box.setText(title)
            box.setInformativeText(reason)
            again = box.addButton("Reconnect", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
            verifier = None
            if getattr(self, '_client', None) is not None:
                verifier = self._client._controller.getCertificateVerifier()
            trust = None
            if verifier is not None and verifier.rejection and verifier.certificate is not None:
                from rdpy.protocol.rdp import security
                box.setDetailedText("SHA-256 %s\nSubject: %s\nIssuer: %s" % (
                    security.fingerprint(verifier.certificate),
                    verifier.certificate.get_subject().CN, verifier.certificate.get_issuer().CN))
                trust = box.addButton("Trust this certificate", QtWidgets.QMessageBox.ButtonRole.YesRole)
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

        # outside of the Twisted callback: the dialog runs its own event loop
        QtCore.QTimer.singleShot(0, ask)

    def _onDecoderBroken(self):
        """Called from the Twisted thread when the H.264 decoder cannot recover.

        Triggers an automatic reconnect so the user does not have to manually
        close and reopen the window after a video freeze.
        """
        log.warning("H.264 decoder broken — scheduling auto-reconnect")
        self._decoderBroken = True
        from twisted.internet import reactor
        reactor.callLater(0, self._client._controller.close)

    def _onResize(self, width, height):
        """
        @summary: Called when user resizes the window, triggers reconnection
        """
        self._width = width
        self._height = height
        self._resizing = True
        # Close must be called from the Twisted thread, not the Qt timer thread
        from twisted.internet import reactor
        log.debug("_onResize: scheduling close() from Qt/timer thread:\n%s" % "".join(traceback.format_stack()))
        reactor.callFromThread(self._client._controller.close)

    def clientConnectionLost(self, connector, reason):
        """
        @summary: Connection lost event
        @param connector: twisted connector use for rdp connection (use reconnect to restart connection)
        @param reason: str use to advertise reason of lost connection
        """
        if self._redirectRoutingToken:
            log.info("Reconnecting with routing token for server redirection")
            connector.connect()
            return
        if self._decoderBroken:
            log.info("Reconnecting after H.264 decoder failure")
            self._decoderBroken = False
            connector.connect()
            return
        if self._resizing:
            log.info("Reconnecting with new screen size %dx%d" % (self._width, self._height))
            self._resizing = False
            connector.connect()
            return
        error = self._client._controller.getProtocolException() if self._client else None
        if (isinstance(error, RDPSecurityNegoFail) or reason.type == RDPSecurityNegoFail) and \
                self._attempt + 1 < len(self._attempts) and not self._client._controller._isReady:
            failed = self._attempts[self._attempt][0]
            self._attempt += 1
            log.info("security: %s refused (%s), falling back to %s" %
                     (failed, error or reason.value, self._attempts[self._attempt][0]))
            connector.connect()
            return
        from rdpy.ui.qt import isQuitting
        if (self._w is not None and self._w.userClosed) or isQuitting():
            log.info("disconnected")
        else:
            log.info("connection lost: %s" % self._endReason(reason))
        from rdpy.ui.qt import isQuitting
        if not self._consoleMode() and self._w is not None and not self._w.userClosed and not isQuitting():
            self._askReconnect(connector, "The remote session ended.", self._endReason(reason))
            return
        if self._consoleMode() and not self._consoleDone:
            print("connection lost: %s" % self._endReason(reason), file=sys.stderr)
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
        log.info("connection failed: %s" % self._endReason(reason))
        from rdpy.ui.qt import isQuitting
        if not self._consoleMode() and self._w is not None and not self._w.userClosed and not isQuitting():
            self._askReconnect(connector, "Could not connect to the server.", self._endReason(reason))
            return
        if self._consoleMode():
            print("connection failed: %s" % self._endReason(reason), file=sys.stderr)
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
    \t--size WIDTHxHEIGHT: desktop size (same as -w / -h)
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
}


def parse_drive(arg):
    """
    @summary: Parse a --drive argument: "NAME=PATH" or "PATH" (name = directory name)
    @param arg: {str}
    @return: {tuple} (name, absolute path)
    @raise ValueError: invalid argument or path is not a directory
    """
    import os
    name, sep, path = arg.partition("=")
    if not sep:
        name, path = "", arg
    if not path.strip():
        # os.path.abspath("") is the working directory: never share it implicitly
        raise ValueError("--drive: empty path in '%s'" % arg)
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise ValueError("--drive: not a directory: %s" % path)
    if not name:
        from rdpy.protocol.rdp.rdpdr_drive import defaultDriveName
        name = defaultDriveName(path)
    return name, path


def main():
    username = ""
    password = ""
    domain = ""
    width = 1280
    height = 1024
    keyboardType = "IBM_101_102_KEYS"
    keyboardLayout = "US"
    swap_alt_meta = False
    disable_avc444 = False
    drives = []

    argv = []
    for a in sys.argv[1:]:
        if a.startswith("-kt"):
            argv.append("--kt" + a[3:])
        elif a.startswith("-kl"):
            argv.append("--kl" + a[3:])
        elif a == "-disable-avc444":
            argv.append("--disable-avc444")
        elif a == "-swap-alt-meta":
            argv.append("--swap-alt-meta")
        elif a == "-drive" or a.startswith("-drive="):
            argv.append("-" + a)
        else:
            argv.append(a)
    options = dict(DEFAULT_OPTIONS)
    secOnly, secPreset, certMode, secFlags = None, None, None, {}
    hooks = {}

    try:
        opts, args = getopt.getopt(argv, "fou:p:d:w:h:r:", [
            "kt=", "kl=", "swap-alt-meta", "disable-avc444", "drive=", "share=", "size=", "bpp=", "gfx=",
            "clipboard=", "no-clipboard", "no-sound", "microphone", "audio-latency=", "shell=", "workdir=",
            "info", "json", "screenshot=", "wait=", "help", "connect-timeout=", "timeout=",
            "sec=", "sec-nla=", "sec-tls=", "sec-rdp=", "security=", "cert=", "cert-ignore",
            "on-login-screen=", "on-desktop=", "on-logon-error=", "on-transfer-start=", "on-transfer-done=",
            "smart-sizing", "stretch", "keep-aspect"])
    except getopt.GetoptError:
        help()
        sys.exit(1)
    for opt, arg in opts:
        if opt == "-u":
            username = arg
        elif opt == "-p":
            password = arg
        elif opt == "-d":
            domain = arg
        elif opt == "-w":
            width = int(arg)
        elif opt == "-h":
            height = int(arg)
        elif opt == "--kt":
            keyboardType = arg
        elif opt == "--kl":
            keyboardLayout = arg
        elif opt == "--swap-alt-meta":
            swap_alt_meta = True
        elif opt == "--disable-avc444":
            disable_avc444 = True
        elif opt == "--drive":
            try:
                drives.append(parse_drive(arg))
            except ValueError as e:
                print(e)
                sys.exit(1)
        elif opt == "--share":
            # PyFreeRdpApp syntax: DIR[=NAME]
            path, sep, name = arg.rpartition("=") if "=" in arg else (arg, "", "")
            try:
                drives.append(parse_drive("%s=%s" % (name, path) if sep else path))
            except ValueError as e:
                print(e)
                sys.exit(1)
        elif opt == "--size":
            try:
                width, height = (int(v) for v in arg.lower().split("x"))
            except ValueError:
                print("--size: expected WIDTHxHEIGHT, e.g. 1920x1080")
                sys.exit(1)
        elif opt == "--bpp":
            if arg not in ("15", "16", "24", "32"):
                print("--bpp: 15, 16, 24 or 32")
                sys.exit(1)
            options['bpp'] = int(arg)
        elif opt == "--gfx":
            if arg not in ("on", "off"):
                print("--gfx: on or off")
                sys.exit(1)
            options['gfx'] = arg
        elif opt == "--clipboard":
            if arg not in ("off", "text", "files"):
                print("--clipboard: off, text or files")
                sys.exit(1)
            options['clipboard'] = arg
        elif opt == "--no-clipboard":
            options['clipboard'] = 'off'
        elif opt == "--no-sound":
            options['sound'] = False
        elif opt == "--microphone":
            options['microphone'] = True
        elif opt == "--audio-latency":
            from rdpy.protocol.rdp import audio_out
            audio_out._TARGET_MIN = max(0.02, int(arg) / 1000.0)
        elif opt == "--shell":
            options['shell'] = arg
        elif opt == "--workdir":
            options['workdir'] = arg
        elif opt == "--info":
            options['info'] = True
        elif opt == "--json":
            options['json'] = True
        elif opt == "--screenshot":
            options['screenshot'] = arg
        elif opt == "--wait":
            options['wait'] = max(0.0, float(arg))
        elif opt == "--sec":
            if arg not in ("nla", "tls", "rdp"):
                print("--sec: nla, tls or rdp")
                sys.exit(1)
            secOnly = arg
        elif opt in ("--sec-nla", "--sec-tls", "--sec-rdp"):
            if arg not in ("on", "off"):
                print("%s: on or off" % opt)
                sys.exit(1)
            secFlags[opt[6:]] = arg == "on"
        elif opt == "--security":
            if arg not in ("max", "compat"):
                print("--security: max or compat")
                sys.exit(1)
            secPreset = arg
        elif opt == "--cert":
            if arg not in ("ignore", "tofu", "deny"):
                print("--cert: ignore, tofu or deny")
                sys.exit(1)
            certMode = arg
        elif opt == "--smart-sizing":
            options['smartSizing'] = True
        elif opt == "--stretch":
            options['smartSizing'] = True
            options['keepAspect'] = False
        elif opt == "--keep-aspect":
            options['smartSizing'] = True
            options['keepAspect'] = True
        elif opt == "--cert-ignore":
            certMode = "ignore"
        elif opt in ("--on-login-screen", "--on-desktop", "--on-logon-error", "--on-transfer-start",
                     "--on-transfer-done"):
            hooks[opt[5:]] = arg
        elif opt in ("--connect-timeout", "--timeout"):
            try:
                ms = int(arg)
                if ms <= 0:
                    raise ValueError()
            except ValueError:
                print("%s: a positive number of milliseconds" % opt)
                sys.exit(1)
            if opt == "--connect-timeout":
                options['connectTimeout'] = ms / 1000.0
            else:
                from rdpy.core import layer as _layer
                _layer.TCP_SETTINGS['ackTimeoutMs'] = ms
        elif opt == "--help":
            help()
            sys.exit(0)

    if not args:
        help()
        sys.exit(1)

    # security (FreeRDP rdpSettings NlaSecurity / TlsSecurity / RdpSecurity / IgnoreCertificate)
    from rdpy.protocol.rdp import security
    if secPreset == "max":
        settings = security.SecuritySettings.maximum()
    elif secPreset == "compat":
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
        username = input("User: ")
    if not password:
        password = getpass.getpass()

    if ':' in args[0]:
        ip, port = args[0].split(':')
    else:
        ip, port = args[0], "3389"

    # QApplication is already created at module load time (so qreactor could
    # bind to it).  Reuse that instance here.
    app = _app
    # Closing the window must disconnect first (FreeRDP freerdp_disconnect):
    # the application quits once the connection is closed, not when the last
    # window closes (that would reset the socket under the server).
    app.setQuitOnLastWindowClosed(False)

    from twisted.internet import reactor

    log_runtime_info()
    log.info("keyboard type set to %s" % keyboardType)
    log.info("keyboard layout set to %s" % keyboardLayout)
    for name, path in drives:
        log.info("redirecting drive '%s' -> %s" % (name, path))

    options['host'], options['port'] = ip, int(port)
    try:
        factory = RDPClientQtFactory(app, width, height, username, password, domain, keyboardType, keyboardLayout,
                                     "nego",
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
    if not factory._consoleMode():
        initial_widget.show()
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
        import subprocess
        env = dict(os.environ, RDP_HOST=ip, RDP_PORT=str(port), RDP_USER=username,
                   RDP_LOGON_MESSAGE=message, RDP_EVENT=event)
        env.update(dict((k, str(v)) for k, v in extra.items()))
        try:
            subprocess.Popen(command, shell=True, env=env)
        except OSError as e:
            log.warning("--on-%s: cannot run %r: %s" % (event, command, e))

    initial_widget.loginScreenReached.connect(lambda: runHook('login-screen'))
    initial_widget.desktopReached.connect(lambda: runHook('desktop'))
    initial_widget.logonError.connect(lambda message: runHook('logon-error', message))
    initial_widget.fileTransferStarted.connect(lambda direction, items, total: runHook(
        'transfer-start', RDP_TRANSFER_KIND='clipboard', RDP_TRANSFER_DIRECTION=direction,
        RDP_TRANSFER_ITEMS=items, RDP_TRANSFER_BYTES=total))
    initial_widget.fileTransferFinished.connect(lambda direction, paths, total, status: runHook(
        'transfer-done', RDP_TRANSFER_KIND='clipboard', RDP_TRANSFER_DIRECTION=direction,
        RDP_TRANSFER_PATHS=os.pathsep.join(paths), RDP_TRANSFER_BYTES=total, RDP_TRANSFER_STATUS=status))
    initial_widget.driveFileTransferred.connect(lambda direction, path, total: runHook(
        'transfer-done', RDP_TRANSFER_KIND='drive', RDP_TRANSFER_DIRECTION=direction,
        RDP_TRANSFER_PATHS=path, RDP_TRANSFER_BYTES=total, RDP_TRANSFER_STATUS='completed'))
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
