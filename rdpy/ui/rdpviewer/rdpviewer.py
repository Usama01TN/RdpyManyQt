# -*- coding: utf-8 -*-
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
RdpViewerWidget: an embeddable Remote Desktop viewer.
Drop it in any Qt application (or in Qt Designer through rdpviewerplugin.py),
set the connection options as properties and call connectToHost(). Every
RdpyManyQt feature is an option: colour depth, graphics pipeline / AVC444,
clipboard (text / files), sound, microphone, drive sharing, keyboard layout and
type, security (NLA / TLS / RDP, certificate check, presets), timeouts, shell.
The widget reports the session with signals: connection, logon (login screen,
desktop), file transfers (clipboard and drives) and disconnection.
Enumerations (State, ColorDepth, ClipboardMode, CertificateMode, SecurityPreset,
KeyboardLayout, KeyboardType, TransferDirection, TransferStatus) are nested
IntEnums; setters also accept their int value or their name.
Works with every binding ManyQt supports (PyQt5, PyQt6, PySide2, PySide6). The
Twisted reactor must be the Qt reactor (qreactor): it is installed on first use
when no other reactor was imported before.
"""
from __future__ import absolute_import, division, print_function
from ManyQt.QtCore import Qt, QRectF, QSize, QEvent, QTimer, Q_ENUMS, qVersion, pyqtSignal, pyqtSlot, pyqtProperty
from ManyQt.QtGui import QPainter, QColor, QFont, QPixmap, QLinearGradient
from ManyQt.QtWidgets import QWidget, QVBoxLayout
from os.path import dirname, join, isdir, abspath, expanduser, basename
from socket import gethostname
from enum import IntEnum

LOGO_PATH = join(dirname(__file__), 'images', 'logo-256.png')  # type: str


def ensureQtReactor():
    """
    Install the Qt reactor for Twisted when no reactor is installed yet.
    :return: bool (True when the running reactor drives Qt events)
    """
    from sys import modules
    if 'twisted.internet.reactor' not in modules:
        try:
            from rdpy.ui.qreactor import install
            install()
        except:
            return False
    from twisted.internet import reactor
    return reactor.__class__.__name__.lower().startswith('qt')


def toEnum(enumClass, value):
    """
    Accept an enum member, its int value or its name (case-insensitive).
    :param enumClass: IntEnum class
    :param value: IntEnum | int | str
    :return: IntEnum
    """
    if isinstance(value, enumClass):
        return value
    if hasattr(value, 'encode'):
        key = value.strip().replace('-', '_').replace(' ', '_').lower()
        for member in enumClass:
            if member.name.lower() == key or str(int(member)) == key:
                return member
        raise ValueError(u'{} is not a {}'.format(value, enumClass.__name__))
    return enumClass(int(value))


class RdpViewerWidget(QWidget):
    """
    RdpViewerWidget class: Remote Desktop session in a widget.
    """

    class State(IntEnum):
        """
        State enumeration class.
        """
        Idle = 0  # type: int
        Connecting = 1  # type: int
        Connected = 2  # type: int
        LoginScreen = 3  # type: int
        Desktop = 4  # type: int

    class ColorDepth(IntEnum):
        """
        ColorDepth enumeration class (bits per pixel).
        """
        Bpp15 = 15  # type: int
        Bpp16 = 16  # type: int
        Bpp24 = 24  # type: int
        Bpp32 = 32  # type: int

    class ClipboardMode(IntEnum):
        """
        ClipboardMode enumeration class.
        """
        Off = 0  # type: int
        Text = 1  # type: int
        Files = 2  # type: int

    class CertificateMode(IntEnum):
        """
        CertificateMode enumeration class (FreeRDP /cert:).
        """
        Ignore = 0  # type: int
        Tofu = 1  # type: int
        Deny = 2  # type: int

    class SecurityPreset(IntEnum):
        """
        SecurityPreset enumeration class.
        """
        Custom = 0  # type: int
        Default = 1  # type: int
        Maximum = 2  # type: int
        Compatible = 3  # type: int

    class KeyboardLayout(IntEnum):
        """
        KeyboardLayout enumeration class (Windows layout identifiers).
        """
        ARABIC = 0x00000401  # type: int
        BULGARIAN = 0x00000402  # type: int
        CHINESE_US_KEYBOARD = 0x00000404  # type: int
        CZECH = 0x00000405  # type: int
        DANISH = 0x00000406  # type: int
        GERMAN = 0x00000407  # type: int
        GREEK = 0x00000408  # type: int
        US = 0x00000409  # type: int
        SPANISH = 0x0000040A  # type: int
        FINNISH = 0x0000040B  # type: int
        FRENCH = 0x0000040C  # type: int
        HEBREW = 0x0000040D  # type: int
        HUNGARIAN = 0x0000040E  # type: int
        ICELANDIC = 0x0000040F  # type: int
        ITALIAN = 0x00000410  # type: int
        JAPANESE = 0x00000411  # type: int
        KOREAN = 0x00000412  # type: int
        DUTCH = 0x00000413  # type: int
        NORWEGIAN = 0x00000414  # type: int

    class KeyboardType(IntEnum):
        """
        KeyboardType enumeration class.
        """
        IBM_PC_XT_83_KEY = 1  # type: int
        OLIVETTI = 2  # type: int
        IBM_PC_AT_84_KEY = 3  # type: int
        IBM_101_102_KEYS = 4  # type: int
        NOKIA_1050 = 5  # type: int
        NOKIA_9140 = 6  # type: int
        JAPANESE = 7  # type: int

    class TransferDirection(IntEnum):
        """
        TransferDirection enumeration class.
        """
        Upload = 0  # type: int
        Download = 1  # type: int

    class TransferStatus(IntEnum):
        """
        TransferStatus enumeration class.
        """
        Completed = 0  # type: int
        Partial = 1  # type: int
        Cancelled = 2  # type: int

    # Enumeration aliases.
    Idle = State.Idle  # type: int
    Connecting = State.Connecting  # type: int
    Connected = State.Connected  # type: int
    LoginScreen = State.LoginScreen  # type: int
    Desktop = State.Desktop  # type: int
    Upload = TransferDirection.Upload  # type: int
    Download = TransferDirection.Download  # type: int
    # Session signals.
    connected = pyqtSignal()  # type: pyqtSignal
    disconnected = pyqtSignal('QString')  # type: pyqtSignal
    connectionFailed = pyqtSignal('QString')  # type: pyqtSignal
    stateChanged = pyqtSignal(int)  # type: pyqtSignal
    # Logon signals.
    loginScreenReached = pyqtSignal()  # type: pyqtSignal
    desktopReached = pyqtSignal()  # type: pyqtSignal
    logonFailed = pyqtSignal('QString')  # type: pyqtSignal
    # File transfer signals (TransferDirection, TransferStatus as int).
    fileTransferStarted = pyqtSignal(int, int, object)  # type: pyqtSignal
    fileTransferProgress = pyqtSignal(int, object, object)  # type: pyqtSignal
    fileTransferFinished = pyqtSignal(int, object, object, int)  # type: pyqtSignal
    driveFileTransferred = pyqtSignal(int, 'QString', object)  # type: pyqtSignal
    # Redirected printers: printer, document title, bytes, destination, error ('' when printed).
    printJob = pyqtSignal('QString', 'QString', object, 'QString', 'QString')  # type: pyqtSignal
    # RemoteApp: the program could not be started (the reason), the session is closed.
    remoteAppFailed = pyqtSignal('QString')  # type: pyqtSignal
    SIGNALS = ('connected', 'disconnected', 'connectionFailed', 'stateChanged', 'loginScreenReached',
               'desktopReached', 'logonFailed', 'fileTransferStarted', 'fileTransferProgress',
               'fileTransferFinished', 'driveFileTransferred', 'printJob', 'remoteAppFailed')  # type: tuple

    def __init__(self, *args, **kwargs):
        """
        :param host: (str | unicode | QString) server name or address.
        :param port: (int) RDP port.
        :param username: str | unicode | QString
        :param password: str | unicode | QString
        :param domain: str | unicode | QString
        :param clientName: (str | unicode | QString) computer name sent to the server, empty = this computer.
        :param desktopWidth: (int) remote desktop width, 0 = widget width.
        :param desktopHeight: (int) remote desktop height, 0 = widget height.
        :param colorDepth: (ColorDepth | int) 15, 16, 24 or 32 bits.
        :param graphicsPipeline: (bool) RDPGFX (H.264, RemoteFX) or legacy bitmap updates.
        :param avc444: (bool) allow H.264 AVC444 (full chroma).
        :param clipboardMode: (ClipboardMode | int | str) Off, Text or Files.
        :param soundEnabled: (bool) play the remote sound.
        :param microphoneEnabled: (bool) redirect the local microphone.
        :param audioLatency: (int) ms, smallest audio buffer.
        :param keyboardLayout: (KeyboardLayout | int | str) remote keyboard layout.
        :param keyboardType: (KeyboardType | int | str) keyboard type.
        :param swapAltMeta: (bool) swap Alt and Meta (Windows) keys.
        :param nlaSecurity: (bool) FreeRDP NlaSecurity.
        :param tlsSecurity: (bool) FreeRDP TlsSecurity.
        :param rdpSecurity: (bool) FreeRDP RdpSecurity (legacy, experimental).
        :param certificateMode: (CertificateMode | int | str) Ignore, Tofu or Deny (FreeRDP /cert:).
        :param connectTimeout: (int) ms to establish the connection (FreeRDP TcpConnectTimeout).
        :param ackTimeout: (int) ms before unacknowledged data drops the connection (FreeRDP TcpAckTimeout).
        :param shell: (str | unicode | QString) program started instead of the desktop.
        :param workingDirectory: (str | unicode | QString) working folder of the shell.
        :param autoConnect: (bool) connect when the widget is first shown.
        :param smartSizing: (bool) scale the remote desktop to the widget instead of resizing it.
        :param advancedInput: (bool) use FreeRDP's advanced input channel when a server opens it.
        :param sshAgent: (bool) forward the local ssh-agent (FreeRDP /ssh-agent).
        :param cameraRedirection: (bool) redirect the cameras (MS-RDPECAM).
        :param cameraDevice: (str | unicode | QString) only the cameras whose name contains this text ('test': a test camera).
        :param multitouch: (bool) touch and pen input through MS-RDPEI.
        :param location: (str | unicode | QString) 'LATITUDE,LONGITUDE[,ALTITUDE]' given to the session (MS-RDPEL), '' = none.
        :param echoChannel: (bool) answer the server's echo channel (MS-RDPEECO).
        :param rdp2tcpCommand: (str | unicode | QString) local rdp2tcp program for TCP forwarding, '' = none.
        :param videoRedirection: (bool) video optimized remoting (MS-RDPEVOR) with geometry tracking.
        :param webAuthn: (bool) passkey / security key sign-ins use the local FIDO2 keys (MS-RDPEWA).
        :param multimediaRedirection: (bool) Windows Media Player audio / video decoded here (TSMF).
        :param keepAspectRatio: (bool) with smartSizing, letterbox with black bars instead of
                                covering the whole widget (default False, as mstsc / FreeRDP).
        :param drives: (list[str | unicode | QString] | QStringList | list[tuple]) shared folders, 'NAME=PATH' or (name, path).
        :param printers: (list[str | unicode | QString] | QStringList) redirected printers: 'NAME[,DRIVER][,default]', '*' (all local
                         printers) or 'file:FOLDER[,DRIVER]' (FreeRDP /printer).
        :param serialPorts: (list[str | unicode | QString] | QStringList) 'COM1=/dev/ttyUSB0[,DRIVER][,permissive]' (FreeRDP /serial, pyserial).
        :param parallelPorts: (list[str | unicode | QString] | QStringList) 'LPT1=/dev/usb/lp0' (FreeRDP /parallel).
        :param smartcardEnabled: (bool) redirect the local smart card readers (pyscard, PC/SC).
        :param smartcardFilter: (str | unicode | QString) comma separated reader name parts, empty = every reader.
        :param usbDevices: (list[str | unicode | QString] | QStringList) USB devices: 'VVVV:PPPP', 'addr:BUS:ADDRESS' or 'auto'
                           (FreeRDP /usb, python-libusb1).
        :param dynamicResolution: (bool) resizing the widget changes the remote resolution
                                  (display control channel, FreeRDP /dynamic-resolution).
        :param remoteApp: (str | unicode | QString) RemoteApp program ('||alias' or a path): its windows are shown
                          as separate windows instead of the desktop (FreeRDP /app).
        :param remoteAppArguments: (str | unicode | QString) command line of remoteApp.
        :param remoteAppWorkingDirectory: (str | unicode | QString) working folder of remoteApp.
        :param securityPreset: (SecurityPreset | int | str) applied after the security options.
        :param connected: callable | None
        :param disconnected: callable | None
        :param connectionFailed: callable | None
        :param stateChanged: callable | None
        :param loginScreenReached: callable | None
        :param desktopReached: callable | None
        :param logonFailed: callable | None
        :param fileTransferStarted: callable | None
        :param fileTransferProgress: callable | None
        :param fileTransferFinished: callable | None
        :param driveFileTransferred: callable | None
        :param printJob: callable | None
        :param remoteAppFailed: callable | None
        :param args: any
        :param kwargs: any
        """
        host = kwargs.pop('host', u'')  # type: str
        port = kwargs.pop('port', 3389)  # type: int
        username = kwargs.pop('username', u'')  # type: str
        password = kwargs.pop('password', u'')  # type: str
        domain = kwargs.pop('domain', u'')  # type: str
        clientName = kwargs.pop('clientName', '')  # type: str
        desktopWidth = kwargs.pop('desktopWidth', 0)  # type: int
        desktopHeight = kwargs.pop('desktopHeight', 0)  # type: int
        colorDepth = kwargs.pop('colorDepth', RdpViewerWidget.ColorDepth.Bpp32)  # type: RdpViewerWidget.ColorDepth
        graphicsPipeline = kwargs.pop('graphicsPipeline', True)  # type: bool
        avc444 = kwargs.pop('avc444', True)  # type: bool
        clipboardMode = kwargs.pop(
            'clipboardMode', RdpViewerWidget.ClipboardMode.Files)  # type: RdpViewerWidget.ClipboardMode
        soundEnabled = kwargs.pop('soundEnabled', True)  # type: bool
        microphoneEnabled = kwargs.pop('microphoneEnabled', False)  # type: bool
        audioLatency = kwargs.pop('audioLatency', 60)  # type: int
        keyboardLayout = kwargs.pop(
            'keyboardLayout', RdpViewerWidget.KeyboardLayout.US)  # type: RdpViewerWidget.KeyboardLayout
        keyboardType = kwargs.pop(
            'keyboardType', RdpViewerWidget.KeyboardType.IBM_101_102_KEYS)  # type: RdpViewerWidget.KeyboardType
        swapAltMeta = kwargs.pop('swapAltMeta', False)  # type: bool
        nlaSecurity = kwargs.pop('nlaSecurity', True)  # type: bool
        tlsSecurity = kwargs.pop('tlsSecurity', True)  # type: bool
        rdpSecurity = kwargs.pop('rdpSecurity', True)  # type: bool
        certificateMode = kwargs.pop(
            'certificateMode', RdpViewerWidget.CertificateMode.Tofu)  # type: RdpViewerWidget.CertificateMode
        connectTimeout = kwargs.pop('connectTimeout', 15000)  # type: int
        ackTimeout = kwargs.pop('ackTimeout', 9000)  # type: int
        shell = kwargs.pop('shell', u'')  # type: str
        workingDirectory = kwargs.pop('workingDirectory', u'')  # type: str
        autoConnect = kwargs.pop('autoConnect', False)  # type: bool
        smartSizing = kwargs.pop('smartSizing', False)  # type: bool
        advancedInput = kwargs.pop('advancedInput', True)  # type: bool
        sshAgent = kwargs.pop('sshAgent', False)  # type: bool
        cameraRedirection = kwargs.pop('cameraRedirection', False)  # type: bool
        cameraDevice = kwargs.pop('cameraDevice', '')  # type: str
        multitouch = kwargs.pop('multitouch', False)  # type: bool
        location = kwargs.pop('location', '')  # type: str
        echoChannel = kwargs.pop('echoChannel', False)  # type: bool
        rdp2tcpCommand = kwargs.pop('rdp2tcpCommand', u'')  # type: str
        videoRedirection = kwargs.pop('videoRedirection', False)  # type: bool
        webAuthn = kwargs.pop('webAuthn', False)  # type: bool
        multimediaRedirection = kwargs.pop('multimediaRedirection', False)  # type: bool
        keepAspectRatio = kwargs.pop('keepAspectRatio', False)  # type: bool
        drives = kwargs.pop('drives', [])  # type: list[str]
        printers = kwargs.pop('printers', [])  # type: list[str]
        serialPorts = kwargs.pop('serialPorts', [])  # type: list[str]
        parallelPorts = kwargs.pop('parallelPorts', [])  # type: list[str]
        smartcardEnabled = kwargs.pop('smartcardEnabled', False)  # type: bool
        smartcardFilter = kwargs.pop('smartcardFilter', u'')  # type: str
        usbDevices = kwargs.pop('usbDevices', [])  # type: list[str]
        dynamicResolution = kwargs.pop('dynamicResolution', False)  # type: bool
        remoteApp = kwargs.pop('remoteApp', u'')  # type: str
        remoteAppArguments = kwargs.pop('remoteAppArguments', u'')  # type: str
        remoteAppWorkingDirectory = kwargs.pop('remoteAppWorkingDirectory', u'')  # type: str
        securityPreset = kwargs.pop('securityPreset', None)  # type: RdpViewerWidget.SecurityPreset | None
        callbacks = dict((name, kwargs.pop(name, None)) for name in RdpViewerWidget.SIGNALS)  # type: dict
        super(RdpViewerWidget, self).__init__(*args, **kwargs)
        # Options (through the setters, which validate and normalize).
        self.__m_host = ''  # type: str
        self.__m_port = 3389  # type: int
        self.__m_username = ''  # type: str
        self.__m_password = ''  # type: str
        self.__m_domain = ''  # type: str
        self.__m_clientName = ''  # type: str
        self.__m_desktopWidth = 0  # type: int
        self.__m_desktopHeight = 0  # type: int
        self.__m_colorDepth = RdpViewerWidget.ColorDepth.Bpp32  # type: RdpViewerWidget.ColorDepth
        self.__m_graphicsPipeline = True  # type: bool
        self.__m_avc444 = True  # type: bool
        self.__m_clipboardMode = RdpViewerWidget.ClipboardMode.Files  # type: RdpViewerWidget.ClipboardMode
        self.__m_soundEnabled = True  # type: bool
        self.__m_microphoneEnabled = False  # type: bool
        self.__m_audioLatency = 60  # type: int
        self.__m_keyboardLayout = RdpViewerWidget.KeyboardLayout.US  # type: RdpViewerWidget.KeyboardLayout
        self.__m_keyboardType = RdpViewerWidget.KeyboardType.IBM_101_102_KEYS  # type: RdpViewerWidget.KeyboardType
        self.__m_swapAltMeta = False  # type: bool
        self.__m_nlaSecurity = True  # type: bool
        self.__m_tlsSecurity = True  # type: bool
        self.__m_rdpSecurity = True  # type: bool
        self.__m_certificateMode = RdpViewerWidget.CertificateMode.Tofu  # type: RdpViewerWidget.CertificateMode
        self.__m_connectTimeout = 15000  # type: int
        self.__m_ackTimeout = 9000  # type: int
        self.__m_shell = ''  # type: str
        self.__m_workingDirectory = ''  # type: str
        self.__m_autoConnect = False  # type: bool
        self.__m_smartSizing = bool(smartSizing)  # type: bool
        self.__m_advancedInput = bool(advancedInput)  # type: bool
        self.__m_sshAgent = bool(sshAgent)  # type: bool
        self.__m_cameraRedirection = bool(cameraRedirection)  # type: bool
        self.__m_cameraDevice = cameraDevice  # type: str
        self.__m_invitation = None  # type: object | None
        self.__m_multitouch = bool(multitouch)  # type: bool
        self.__m_location = location  # type: str
        self.__m_echoChannel = bool(echoChannel)  # type: bool
        self.__m_rdp2tcpCommand = rdp2tcpCommand  # type: str
        self.__m_videoRedirection = bool(videoRedirection)  # type: bool
        self.__m_webAuthn = bool(webAuthn)  # type: bool
        self.__m_multimediaRedirection = bool(multimediaRedirection)  # type: bool
        self.__m_keepAspectRatio = bool(keepAspectRatio)  # type: bool
        self.__m_drives = []  # type: list[str]
        self.__m_printers = []  # type: list[str]
        self.__m_serialPorts = []  # type: list[str]
        self.__m_parallelPorts = []  # type: list[str]
        self.__m_smartcardEnabled = False  # type: bool
        self.__m_smartcardFilter = ''  # type: str
        self.__m_usbDevices = []  # type: list[str]
        self.__m_dynamicResolution = False  # type: bool
        self.__m_remoteApp = ''  # type: str
        self.__m_remoteAppArguments = ''  # type: str
        self.__m_remoteAppWorkingDirectory = ''  # type: str
        self.setHost(host)
        self.setPort(port)
        self.setUsername(username)
        self.setPassword(password)
        self.setDomain(domain)
        self.setClientName(clientName)
        self.setDesktopWidth(desktopWidth)
        self.setDesktopHeight(desktopHeight)
        self.setColorDepth(colorDepth)
        self.setGraphicsPipeline(graphicsPipeline)
        self.setAvc444(avc444)
        self.setClipboardMode(clipboardMode)
        self.setSoundEnabled(soundEnabled)
        self.setMicrophoneEnabled(microphoneEnabled)
        self.setAudioLatency(audioLatency)
        self.setKeyboardLayout(keyboardLayout)
        self.setKeyboardType(keyboardType)
        self.setSwapAltMeta(swapAltMeta)
        self.setNlaSecurity(nlaSecurity)
        self.setTlsSecurity(tlsSecurity)
        self.setRdpSecurity(rdpSecurity)
        self.setCertificateMode(certificateMode)
        self.setConnectTimeout(connectTimeout)
        self.setAckTimeout(ackTimeout)
        self.setShell(shell)
        self.setWorkingDirectory(workingDirectory)
        self.setAutoConnect(autoConnect)
        for drive in drives:
            self.addDrive(drive[1], drive[0]) if isinstance(drive, (tuple, list)) else self.addDrive(drive)
        self.setPrinters(printers)
        self.setSerialPorts(serialPorts)
        self.setParallelPorts(parallelPorts)
        self.setSmartcardEnabled(smartcardEnabled)
        self.setSmartcardFilter(smartcardFilter)
        self.setUsbDevices(usbDevices)
        self.setDynamicResolution(dynamicResolution)
        self.setRemoteApp(remoteApp)
        self.setRemoteAppArguments(remoteAppArguments)
        self.setRemoteAppWorkingDirectory(remoteAppWorkingDirectory)
        if securityPreset is not None:
            self.setSecurityPreset(securityPreset)
        for name, callback in callbacks.items():
            if callable(callback):
                getattr(self, name).connect(callback)
        # Session state.
        self.__m_state = RdpViewerWidget.State.Idle  # type: RdpViewerWidget.State
        self.__m_lastError = ''  # type: str
        self.__m_factory = None  # type: object | None
        self.__m_connector = None  # type: object | None
        self.__m_verifier = None  # type: object | None
        self.__m_desktop = None  # type: QWidget | None
        self.__m_shownOnce = False  # type: bool
        self.__m_userDisconnect = False  # type: bool
        self.__m_window = None  # type: QWidget | None
        self.__m_windowClosing = False  # type: bool
        self.__m_logo = QPixmap(LOGO_PATH)  # type: QPixmap
        self.__m_layout = QVBoxLayout(self)  # type: QVBoxLayout
        self.__m_layout.setContentsMargins(0, 0, 0, 0)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ---------- Helpers ----------

    def _setState(self, state):
        """
        :param state: RdpViewerWidget.State | int
        :return:
        """
        state = RdpViewerWidget.State(state)  # type: RdpViewerWidget.State
        if self.__m_state != state:
            self.__m_state = state  # type: RdpViewerWidget.State
            self.stateChanged.emit(int(state))
            self.update()

    def _securitySettings(self):
        """
        :return: security.SecuritySettings
        """
        from rdpy.protocol.rdp import security
        return security.SecuritySettings(nla=self.__m_nlaSecurity, tls=self.__m_tlsSecurity,
                                         rdp=self.__m_rdpSecurity, certificate=self.__m_certificateMode.name.lower())

    @staticmethod
    def _direction(direction):
        """
        :param direction: (str | unicode | QString) 'upload' or 'download'.
        :return: int (TransferDirection)
        """
        return int(RdpViewerWidget.TransferDirection.Upload if direction == 'upload'
                   else RdpViewerWidget.TransferDirection.Download)

    @staticmethod
    def _status(status):
        """
        :param status: (str | unicode | QString) 'completed', 'partial' or 'canceled'.
        :return: int (TransferStatus)
        """
        return int(toEnum(RdpViewerWidget.TransferStatus, status))

    # ---------- Public API ----------

    @pyqtSlot()
    def connectToHost(self):
        """
        Open the session with the current properties.
        :return: None
        """
        if self.__m_state != RdpViewerWidget.State.Idle or not self.__m_host:
            return
        if not ensureQtReactor():
            self.__m_lastError = u'The Twisted reactor is not the Qt reactor (install qreactor first)'  # type: str
            self.connectionFailed.emit(self.__m_lastError)
            return
        from ManyQt.QtWidgets import QApplication
        from twisted.internet import reactor
        from rdpy.ui.qt import RDPClientQt, installQuitGuard
        from rdpy.protocol.rdp import rdp, audio_out
        from rdpy.core import layer
        installQuitGuard(QApplication.instance())  # sessions closed when the application quits
        viewer = self  # type: RdpViewerWidget
        width = self.__m_desktopWidth or max(640, self.width())  # type: int
        height = self.__m_desktopHeight or max(480, self.height())  # type: int
        if self.__m_remoteApp:
            # RemoteApp: the session desktop covers the local screens.
            from rdpy.ui.rail_qt import RailManager
            width, height = RailManager.desktopSize()
        settings = self._securitySettings()
        attempts = settings.attempts(bool(self.__m_username and self.__m_password))  # type: list
        if not attempts:
            self.__m_lastError = u'No usable security protocol: {}'.format(settings.describe())  # type: str
            self.connectionFailed.emit(self.__m_lastError)
            return
        layer.TCP_SETTINGS['ackTimeoutMs'] = self.__m_ackTimeout
        audio_out._TARGET_MIN = self.__m_audioLatency / 1000.0

        class _Factory(rdp.ClientFactory):
            """
            _Factory class: one RDP connection for the viewer.
            """

            def __init__(self):
                """
                :return:
                """
                self.attempt = 0  # type: int
                self.client = None  # type: RDPClientQt | None
                self.cancelled = False  # type: bool

            def buildObserver(self, controller, addr):
                """
                :param controller: RDPClientController
                :param addr: any
                :return: RDPClientQt
                """
                controller.setUsername(viewer.getUsername())
                controller.setPassword(viewer.getPassword())
                controller.setDomain(viewer.getDomain())
                controller.setHostname(viewer.getClientName() or gethostname())
                controller.setKeyboardLayout(viewer.getKeyboardLayout().name)
                controller.setKeyboardType(viewer.getKeyboardType().name)
                controller.setScreen(width, height)
                controller.setSecurity(settings, viewer.getHost(), viewer.getPort(), attempts[self.attempt][1])
                controller.setAVC444Disabled(not viewer.isAvc444())
                controller.setColorDepth(int(viewer.getColorDepth()))
                if not viewer.isGraphicsPipeline():
                    controller.setGraphicsPipeline(False)
                controller.setClipboardMode(viewer.getClipboardMode().name.lower())
                controller.setSoundEnabled(viewer.isSoundEnabled())
                controller.setMicrophoneEnabled(viewer.isMicrophoneEnabled())
                viewer._applyChannels(controller)
                if viewer.getShell():
                    controller.setAlternateShell(viewer.getShell())
                if viewer.getWorkingDirectory():
                    controller.setWorkingDirectory(viewer.getWorkingDirectory())
                # The channel is always accepted: dynamicResolution can then change during the session.
                controller.setDynamicResolution(True)
                if viewer.getRemoteApp():
                    from rdpy.protocol.rdp.channels import rail
                    controller.setRemoteApp(rail.RemoteApp(viewer.getRemoteApp(), viewer.getRemoteAppArguments(),
                                                           viewer.getRemoteAppWorkingDirectory()))
                for name, path in viewer.getDriveList():
                    try:
                        controller.addDrive(name, path)
                    except (ValueError, OSError):
                        pass  # Folder gone since it was added: not shared.
                viewer._addDevices(controller)
                desktop = viewer._desktopWidget(width, height)
                self.client = RDPClientQt(controller, width, height, viewer.isSwapAltMeta(), widget=desktop)
                self.client.setDynamicResolution(viewer.isDynamicResolution())
                if viewer.getRemoteApp():
                    desktop.setVisible(False)  # only the program's windows are shown
                    manager = self.client.getRailManager()
                    if manager is not None:
                        manager.failed.connect(viewer._onRemoteAppFailed)
                original = self.client.onReady
                factory = self  # type: _Factory

                def onReady():
                    """
                    :return:
                    """
                    original()
                    if viewer._isCurrent(factory):
                        viewer._onConnected()

                self.client.onReady = onReady
                if self.cancelled:
                    # disconnectFromHost() came while the TCP connection was opening.
                    reactor.callLater(0, controller.close)

                def watchdog():
                    """
                    :return:
                    """
                    if not controller._isReady and not getattr(controller, '_closing', False):
                        controller.abort(u'Connection timed out ({} ms)'.format(viewer.getConnectTimeout()))

                reactor.callLater(viewer.getConnectTimeout() / 1000.0, watchdog)
                return self.client

            def _reason(self, reason):
                """
                :param reason: Failure
                :return: str
                """
                if self.client is not None:
                    controller = self.client._controller
                    verifier = controller.getCertificateVerifier()
                    if verifier is not None and verifier.rejection:
                        viewer._setVerifier(verifier)
                        return verifier.rejection
                    error = controller.getProtocolError() or (controller.getLastError() or (0, ''))[1]
                    if error:
                        return error
                return str(getattr(reason, 'value', reason))

            def clientConnectionLost(self, connector, reason):
                """
                :param connector: Connector
                :param reason: Failure
                :return:
                """
                from rdpy.core.error import RDPSecurityNegoFail
                error = self.client._controller.getProtocolException() if self.client else None
                if isinstance(error, RDPSecurityNegoFail) and self.attempt + 1 < len(attempts) and \
                        not self.client._controller._isReady:
                    self.attempt += 1
                    connector.connect()
                    return
                if viewer._isCurrent(self):
                    viewer._onEnded(self._reason(reason), viewer.getState() in (
                        RdpViewerWidget.State.Connected, RdpViewerWidget.State.LoginScreen,
                        RdpViewerWidget.State.Desktop))

            def clientConnectionFailed(self, connector, reason):
                """
                :param connector: Connector
                :param reason: Failure
                :return:
                """
                if viewer._isCurrent(self):
                    viewer._onEnded(self._reason(reason), False)

        self.__m_lastError = ''  # type: str
        self.__m_userDisconnect = False  # type: bool
        self.__m_verifier = None
        self.__m_factory = _Factory()
        factory = self.__m_factory

        def closeOnDestroy(*args):
            """
            The widget is deleted with the session still open.
            :param args: any
            :return:
            """
            if factory.client is not None and factory.client._controller.isConnected():
                factory.client._controller.close()

        self.destroyed.connect(closeOnDestroy)
        self._setState(RdpViewerWidget.State.Connecting)
        self.__m_connector = reactor.connectTCP(
            self.__m_host, self.__m_port, self.__m_factory, timeout=self.__m_connectTimeout / 1000.0)

    @pyqtSlot()
    def disconnectFromHost(self):
        """
        Close the session cleanly (FreeRDP freerdp_disconnect).
        :return: None
        """
        if self.__m_state == RdpViewerWidget.State.Idle:
            return
        self.__m_userDisconnect = True  # type: bool
        factory = self.__m_factory
        if factory is not None and factory.client is not None:
            factory.client._controller.close()
        elif self.__m_connector is not None and getattr(self.__m_connector, 'state', '') == 'connecting':
            self.__m_connector.stopConnecting()
        else:
            if factory is not None:
                factory.cancelled = True  # the session being built closes itself
            self._onEnded('closed by the user', True)

    def _controller(self):
        """
        :return: RDPClientController | None
        """
        factory = self.__m_factory
        return factory.client._controller if factory is not None and factory.client is not None else None

    def getSessionReport(self):
        """
        :return: str (server, logon, security, certificate... see rdpyqt --info)
        """
        controller = self._controller()
        return controller.getSessionReport() if controller is not None else ''

    def getSessionInfo(self):
        """
        :return: dict (getSessionReport as data)
        """
        controller = self._controller()
        return controller.getSessionInfo() if controller is not None else {}

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def saveScreenshot(self, path):
        """
        :param path: (str | unicode | QString) image file (.png, .jpg, .bmp).
        :return: bool
        """
        return bool(self.__m_desktop is not None and self.__m_desktop.saveScreenshot(path))

    def sendFiles(self, paths):
        """
        Put local files on the remote clipboard: paste them in the session (Ctrl+V).
        fileTransferStarted / Progress / Finished report the paste.
        :param paths: list[str | unicode | QString] | QStringList
        :return: bool
        """
        factory = self.__m_factory
        if factory is None or factory.client is None or self.__m_clipboardMode != RdpViewerWidget.ClipboardMode.Files:
            return False
        factory.client.sendFiles([p for p in paths])
        return True

    def _setVerifier(self, verifier):
        """
        :param verifier: security.CertificateVerifier
        :return:
        """
        self.__m_verifier = verifier

    def getCertificateRejection(self):
        """
        :return: str (why the server certificate was refused, empty if it was not)
        """
        return self.__m_verifier.rejection if self.__m_verifier is not None and self.__m_verifier.rejection else ''

    @pyqtSlot()
    def trustCertificate(self):
        """
        Trust the certificate refused by the last connection (stored in known_hosts),
        then connect again with connectToHost().
        :return: bool
        """
        if self.__m_verifier is None or self.__m_verifier.certificate is None:
            return False
        self.__m_verifier.trustCurrent()
        if self.__m_certificateMode == RdpViewerWidget.CertificateMode.Deny:
            self.__m_certificateMode = RdpViewerWidget.CertificateMode.Tofu  # type: RdpViewerWidget.CertificateMode
        self.__m_verifier = None
        return True

    def getLastError(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_lastError

    def getState(self):
        """
        :return: RdpViewerWidget.State
        """
        return self.__m_state

    def isConnected(self):
        """
        :return: bool
        """
        return self.__m_state in (RdpViewerWidget.State.Connected, RdpViewerWidget.State.LoginScreen,
                                  RdpViewerWidget.State.Desktop)

    def isLoggedOn(self):
        """
        :return: bool (the server notified the logon: the desktop is shown)
        """
        return self.__m_state == RdpViewerWidget.State.Desktop

    # ----- Drives -----

    def getDriveList(self):
        """
        :return: list[tuple] [(name, path)]
        """
        return self.__m_drives

    def getDrives(self):
        """
        :return: list[str | unicode | QString] | QStringList ['NAME=PATH']
        """
        return [u'{}={}'.format(name, path) for name, path in self.__m_drives]

    @pyqtSlot('QStringList')
    def setDrives(self, drives):
        """
        :param drives: list[str | unicode | QString] | QStringList 'NAME=PATH' or 'PATH'
        :return:
        """
        self.__m_drives = []  # type: list[str]
        for drive in drives:
            self.addDrive(drive)

    @pyqtSlot('QString', 'QString')
    @pyqtSlot(str, str)
    @pyqtSlot('QString')
    @pyqtSlot(str)
    def addDrive(self, path, name=''):
        """
        Share a local folder with the session (applies to the next connection).
        :param path: (str | unicode | QString) folder, or 'NAME=PATH'.
        :param name: (str | unicode | QString) name shown in the session, default: the folder name.
        :return: bool
        """
        if not name and '=' in path and not isdir(path):
            name, path = path.split('=', 1)
        path = abspath(expanduser(path.strip()))  # type: str
        if not path.strip() or not isdir(path):
            return False
        name = (name or basename(path.rstrip('\\\\/')) or 'drive').strip()  # type: str
        self.__m_drives = [d for d in self.__m_drives if d[0].lower() != name.lower()]  # type: list[str]
        self.__m_drives.append((name, path))
        return True

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def removeDrive(self, name):
        """
        :param name: str | unicode | QString
        :return:
        """
        self.__m_drives = [d for d in self.__m_drives if d[0].lower() != name.lower()]  # type: list[str]
        self.update()

    @pyqtSlot()
    def clearDrives(self):
        """
        :return:
        """
        self.__m_drives = []  # type: list[str]
        self.update()

    # ----- Printers, serial / parallel ports, smart cards -----

    def getPrinters(self):
        """
        :return: (list[str | unicode | QString] | QStringList) Printer descriptions ('NAME[,DRIVER][,default]', '*', 'file:FOLDER[,DRIVER]')
        """
        return self.__m_printers

    @pyqtSlot('QStringList')
    def setPrinters(self, printers):
        """
        :param printers: list[str | unicode | QString] | QStringList
        :return:
        """
        self.__m_printers = []  # type: list[str]
        for printer in printers:
            self.addPrinter(printer)

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def addPrinter(self, printer):
        """
        Redirect a local printer (applies to the next connection), FreeRDP /printer syntax.
        :param printer: (str | unicode | QString) 'NAME[,DRIVER][,default]', '*' for every local printer, or
                        'file:FOLDER[,DRIVER]' for a printer saving PDF files in FOLDER.
        :return: bool
        """
        from rdpy.protocol.rdp.channels.rdpdr_printer import parsePrinterSpec
        try:
            parsePrinterSpec(printer)
        except ValueError:
            return False
        if printer.strip() not in self.__m_printers:
            self.__m_printers.append(printer.strip())
        return True

    @pyqtSlot()
    def clearPrinters(self):
        """
        :return:
        """
        self.__m_printers = []  # type: list[str]
        self.update()

    def getSerialPorts(self):
        """
        :return: (list[str | unicode | QString] | QStringList) ['COM1=/dev/ttyUSB0[,DRIVER][,permissive]']
        """
        return self.__m_serialPorts

    @pyqtSlot('QStringList')
    def setSerialPorts(self, ports):
        """
        :param ports: list[str | unicode | QString] | QStringList
        :return:
        """
        self.__m_serialPorts = []  # type: list[str]
        for port in ports:
            self.addSerialPort(port)

    def addSerialPort(self, port, path='', driver='', permissive=False):
        """
        Redirect a serial port (applies to the next connection, needs pyserial).
        :param port: (str | unicode | QString) 'NAME=PATH[,DRIVER][,permissive]', or the name with path.
        :param path: (str | unicode | QString) local port (/dev/ttyUSB0, COM3) or pyserial URL (socket://host:port).
        :param driver: (str | unicode | QString) Serial, SerCx or SerCx2.
        :param permissive: (bool) report unsupported requests as successful.
        :return: bool
        """
        from rdpy.protocol.rdp.channels.rdpdr_serial import parseSerialSpec
        spec = str(port) if not path else ','.join(
            ['{}={}'.format(port, path)] + ([driver] if driver else []) + (['permissive'] if permissive else []))
        try:
            name = parseSerialSpec(spec)[0]
        except ValueError:
            return False
        self.__m_serialPorts = [
            p for p in self.__m_serialPorts if parseSerialSpec(p)[0].lower() != name.lower()]  # type: list[str]
        self.__m_serialPorts.append(spec)
        return True

    def getParallelPorts(self):
        """
        :return: list[str | unicode | QString] | QStringList ['LPT1=/dev/usb/lp0']
        """
        return self.__m_parallelPorts

    @pyqtSlot('QStringList')
    def setParallelPorts(self, ports):
        """
        :param ports: list[str | unicode | QString] | QStringList
        :return:
        """
        self.__m_parallelPorts = []  # type: list[str]
        for port in ports:
            self.addParallelPort(port)

    @pyqtSlot('QString', 'QString')
    @pyqtSlot(str, str)
    @pyqtSlot('QString')
    @pyqtSlot(str)
    def addParallelPort(self, port, path=''):
        """
        Redirect a parallel port (applies to the next connection).
        :param port: (str | unicode | QString) 'NAME=PATH', or the name with path.
        :param path: (str | unicode | QString) local device (/dev/lp0, /dev/usb/lp0) or file.
        :return: bool
        """
        from rdpy.protocol.rdp.channels.rdpdr_parallel import parseParallelSpec
        spec = '{}={}'.format(port, path) if path else str(port)  # type: str
        try:
            name = parseParallelSpec(spec)[0]  # type: str
        except ValueError:
            return False
        self.__m_parallelPorts = [
            p for p in self.__m_parallelPorts if parseParallelSpec(p)[0].lower() != name.lower()]  # type: list[str]
        self.__m_parallelPorts.append(spec)
        return True

    @pyqtSlot()
    def clearPorts(self):
        """
        :return:
        """
        self.__m_serialPorts = []  # type: list[str]
        self.__m_parallelPorts = []  # type: list[str]
        self.update()

    def isSmartcardEnabled(self):
        """
        :return: bool
        """
        return self.__m_smartcardEnabled

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setSmartcardEnabled(self, enabled):
        """
        Redirect the local smart card readers (applies to the next connection, needs pyscard).
        :param enabled: bool
        :return:
        """
        self.__m_smartcardEnabled = bool(enabled)  # type: bool
        self.update()

    def getSmartcardFilter(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_smartcardFilter

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setSmartcardFilter(self, readers=''):
        """
        :param readers: (str | unicode | QString) comma separated parts of reader names, empty = every reader.
        :return:
        """
        self.__m_smartcardFilter = readers  # type: str
        self.update()

    def _addDevices(self, controller):
        """
        Register the printers, ports and smart cards of the widget on a new controller.
        :param controller: RDPClientController
        :return:
        """
        from rdpy.protocol.rdp.channels.rdpdr_smartcard import parseSmartcardSpec
        from rdpy.protocol.rdp.channels.rdpdr_parallel import parseParallelSpec
        from rdpy.protocol.rdp.channels.rdpdr_printer import parsePrinterSpec
        from rdpy.protocol.rdp.channels.rdpdr_serial import parseSerialSpec
        for printer in self.__m_printers:
            try:
                spec = parsePrinterSpec(printer)  # type: dict
                if spec['kind'] == 'all':
                    controller.addAllPrinters()
                elif spec['kind'] == 'file':
                    controller.addPrintToFolder(spec['folder'], spec['driver'], spec['isDefault'])
                else:
                    controller.addPrinter(spec['name'], spec['driver'], spec['isDefault'])
            except (ValueError, OSError):
                pass  # printer gone since it was added: not redirected
        for port in self.__m_serialPorts:
            try:
                controller.addSerialPort(*parseSerialSpec(port))
            except (ValueError, OSError):
                pass
        for port in self.__m_parallelPorts:
            try:
                controller.addParallelPort(*parseParallelSpec(port))
            except (ValueError, OSError):
                pass
        if self.__m_smartcardEnabled:
            try:
                controller.addSmartcard(parseSmartcardSpec(self.__m_smartcardFilter))
            except (ValueError, OSError):
                pass  # pyscard missing: no smart card redirection
        for device in self.__m_usbDevices:
            try:
                controller.addUsbDevice(device)
            except (ValueError, OSError):
                pass  # Unplugged / python-libusb1 missing: not redirected.

    def getUsbDevices(self):
        """
        :return: list[str | unicode | QString] | QStringList
        """
        return self.__m_usbDevices

    @pyqtSlot('QStringList')
    def setUsbDevices(self, devices):
        """
        USB devices redirected at the next connection (FreeRDP /usb, needs python-libusb1).
        :param devices: (list[str | unicode | QString] | QStringList) 'VVVV:PPPP', 'id:VVVV:PPPP', 'addr:BUS:ADDRESS' or 'auto'.
        :return:
        :raise ValueError: invalid description.
        """
        from rdpy.protocol.rdp.channels.urbdrc import parseUsbSpec
        devices = [d for d in (devices or []) if d.strip()]  # type: list[str]
        for device in devices:
            parseUsbSpec(device)
        self.__m_usbDevices = devices  # type: list[str]

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def addUsbDevice(self, device):
        """
        :param device: (str | unicode | QString) 'VVVV:PPPP', 'addr:BUS:ADDRESS' or 'auto'.
        :return:
        """
        self.setUsbDevices(self.__m_usbDevices + [device])

    def isDynamicResolution(self):
        """
        :return: bool
        """
        return self.__m_dynamicResolution

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setDynamicResolution(self, enabled):
        """
        Resizing the widget changes the remote resolution without reconnecting when the server
        offers the display control channel (FreeRDP /dynamic-resolution); takes effect at once.
        :param enabled: bool
        :return:
        """
        self.__m_dynamicResolution = bool(enabled)  # type: bool
        factory = getattr(self, '_RdpViewerWidget__m_factory', None)  # None while constructing.
        if factory is not None and getattr(factory, 'client', None) is not None:
            factory.client.setDynamicResolution(self.__m_dynamicResolution)

    def getRemoteApp(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_remoteApp

    @pyqtSlot('QString')
    @pyqtSlot(str)
    @pyqtSlot()
    def setRemoteApp(self, program=''):
        """
        RemoteApp program of the next connection ('||alias' of a published RemoteApp or a path the
        server allows), empty = the desktop (FreeRDP /app).
        :param program: str | unicode | QString
        :return:
        """
        self.__m_remoteApp = program  # type: str
        self.update()

    def getRemoteAppArguments(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_remoteAppArguments

    def setRemoteAppArguments(self, arguments):
        """
        :param arguments: (str | unicode | QString) command line of the RemoteApp program.
        :return:
        """
        self.__m_remoteAppArguments = arguments  # type: str
        self.update()

    def getRemoteAppWorkingDirectory(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_remoteAppWorkingDirectory

    def setRemoteAppWorkingDirectory(self, directory):
        """
        :param directory: (str | unicode | QString) working folder of the RemoteApp program.
        :return:
        """
        self.__m_remoteAppWorkingDirectory = directory  # type: str
        self.update()

    def _onRemoteAppFailed(self, message):
        """
        The RemoteApp program did not start: report it and close the session.
        :param message: str | unicode | QString
        :return:
        """
        self.__m_lastError = message  # type: str
        self.remoteAppFailed.emit(self.__m_lastError)
        self.disconnectFromHost()

    # ----- Security preset -----

    def getSecurityPreset(self):
        """
        :return: RdpViewerWidget.SecurityPreset (Custom when the flags match no preset)
        """
        flags = (self.__m_nlaSecurity, self.__m_tlsSecurity, self.__m_rdpSecurity, self.__m_certificateMode)
        cert = RdpViewerWidget.CertificateMode
        presets = {(True, True, True, cert.Tofu): RdpViewerWidget.SecurityPreset.Default,
                   (True, True, False, cert.Deny): RdpViewerWidget.SecurityPreset.Maximum,
                   (True, True, True, cert.Ignore): RdpViewerWidget.SecurityPreset.Compatible}
        return presets.get(flags, RdpViewerWidget.SecurityPreset.Custom)

    @pyqtSlot(int)
    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setSecurityPreset(self, preset):
        """
        :param preset: (SecurityPreset | int | str) Default (NLA, TLS, RDP, tofu), Maximum (NLA + TLS,
                       strict certificate) or Compatible (all, certificate ignored); Custom changes nothing.
        :return:
        """
        preset = toEnum(RdpViewerWidget.SecurityPreset, preset)  # type: RdpViewerWidget.SecurityPreset
        cert = RdpViewerWidget.CertificateMode
        values = {RdpViewerWidget.SecurityPreset.Default: (True, True, True, cert.Tofu),
                  RdpViewerWidget.SecurityPreset.Maximum: (True, True, False, cert.Deny),
                  RdpViewerWidget.SecurityPreset.Compatible: (True, True, True, cert.Ignore)}.get(preset)
        if values is not None:
            self.__m_nlaSecurity, self.__m_tlsSecurity, self.__m_rdpSecurity, self.__m_certificateMode = values
            self.update()

    # ----- Session widget and its signals -----

    def _desktopWidget(self, width, height):
        """
        :param width: int
        :param height: int
        :return: QRemoteDesktop
        """
        from rdpy.ui.qt import QRemoteDesktop
        if self.__m_desktop is None:
            self.__m_desktop = QRemoteDesktop(width, height, None)
            self.__m_desktop.closedWithoutSession = lambda: None
            self.__m_desktop.setSmartSizing(self.__m_smartSizing, self.__m_keepAspectRatio)
            self.__m_desktop.loginScreenReached.connect(self._onLoginScreen)
            self.__m_desktop.desktopReached.connect(self._onDesktop)
            self.__m_desktop.logonError.connect(self.logonFailed)
            self.__m_desktop.fileTransferStarted.connect(self._onTransferStarted)
            self.__m_desktop.fileTransferProgress.connect(self._onTransferProgress)
            self.__m_desktop.fileTransferFinished.connect(self._onTransferFinished)
            self.__m_desktop.driveFileTransferred.connect(self._onDriveTransferred)
            self.__m_desktop.printJob.connect(self.printJob)
            self.__m_layout.addWidget(self.__m_desktop)
        return self.__m_desktop

    def _onTransferStarted(self, direction, items, total):
        """
        :param direction: str | unicode | QString
        :param items: int
        :param total: int
        :return:
        """
        self.fileTransferStarted.emit(self._direction(direction), int(items), int(total))

    def _onTransferProgress(self, direction, done, total):
        """
        :param direction: str | unicode | QString
        :param done: int
        :param total: int
        :return:
        """
        self.fileTransferProgress.emit(self._direction(direction), int(done), int(total))

    def _onTransferFinished(self, direction, paths, total, status):
        """
        :param direction: str | unicode | QString
        :param paths: list[str | unicode | QString]
        :param total: int
        :param status: str | unicode | QString
        :return:
        """
        self.fileTransferFinished.emit(self._direction(direction), list(paths), int(total), self._status(status))

    def _onDriveTransferred(self, direction, path, total):
        """
        :param direction: str | unicode | QString
        :param path: str | unicode | QString
        :param total: int
        :return:
        """
        self.driveFileTransferred.emit(self._direction(direction), path, int(total))

    def _isCurrent(self, factory):
        """
        :param factory: (object) a connection factory of this viewer.
        :return: bool (False for a connection that already ended for the widget)
        """
        return factory is self.__m_factory

    def _onConnected(self):
        """
        The connection sequence completed (emitted once per connection).
        :return:
        """
        if self.__m_state == RdpViewerWidget.State.Connecting:
            self._setState(RdpViewerWidget.State.Connected)
            self.connected.emit()

    @pyqtSlot()
    def _onLoginScreen(self):
        """
        :return:
        """
        if self.__m_state != RdpViewerWidget.State.Desktop:
            self._setState(RdpViewerWidget.State.LoginScreen)
            self.loginScreenReached.emit()

    @pyqtSlot()
    def _onDesktop(self):
        """
        :return:
        """
        self._setState(RdpViewerWidget.State.Desktop)
        self.desktopReached.emit()

    def _onEnded(self, reason, wasConnected):
        """
        :param reason: str | unicode | QString
        :param wasConnected: bool
        :return:
        """
        self.__m_lastError = reason  # type: str
        self.__m_factory = None
        self.__m_connector = None
        try:
            if self.__m_desktop is not None:
                self.__m_layout.removeWidget(self.__m_desktop)
                self.__m_desktop.userClosed = True
                self.__m_desktop.hide()
                self.__m_desktop.deleteLater()
            self.__m_desktop = None
            self._setState(RdpViewerWidget.State.Idle)
        except RuntimeError:
            return  # the widget itself was deleted: nobody to notify
        userDisconnect = self.__m_userDisconnect  # type: bool
        self.__m_userDisconnect = False  # type: bool
        if userDisconnect:
            # asked by the application or the user: not an error to report
            self.__m_lastError = ''  # type: str
            self.disconnected.emit('closed by the user')
        elif wasConnected:
            self.disconnected.emit(reason)
        else:
            self.connectionFailed.emit(reason)
        if self.__m_windowClosing and self.__m_window is not None:
            # the window close waited for the disconnection: finish it now
            self.__m_windowClosing = False  # type: bool
            QTimer.singleShot(0, self.__m_window.close)

    def getHost(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_host

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setHost(self, host):
        """
        :param host: (str | unicode | QString) server name or address.
        :return:
        """
        self.__m_host = host  # type: str
        self.update()

    def getPort(self):
        """
        :return: int
        """
        return self.__m_port

    @pyqtSlot(int)
    def setPort(self, port):
        """
        :param port: (int) RDP port.
        :return:
        """
        self.__m_port = max(1, min(65535, int(port)))  # type: int
        self.update()

    def getUsername(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_username

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setUsername(self, username):
        """
        :param username: str | unicode | QString
        :return:
        """
        self.__m_username = username  # type: str
        self.update()

    def getPassword(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_password

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setPassword(self, password):
        """
        :param password: str | unicode | QString
        :return:
        """
        self.__m_password = password  # type: str
        self.update()

    def getDomain(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_domain

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setDomain(self, domain):
        """
        :param domain: str | unicode | QString
        :return:
        """
        self.__m_domain = domain  # type: str
        self.update()

    def getClientName(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_clientName

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setClientName(self, clientName):
        """
        :param clientName: (str | unicode | QString) Computer name sent to the server, empty = this computer.
        :return:
        """
        self.__m_clientName = clientName  # type: str
        self.update()

    def getDesktopWidth(self):
        """
        :return: int
        """
        return self.__m_desktopWidth

    @pyqtSlot(int)
    def setDesktopWidth(self, desktopWidth):
        """
        :param desktopWidth: (int) remote desktop width, 0 = widget width.
        :return:
        """
        self.__m_desktopWidth = max(0, int(desktopWidth))  # type: int
        self.update()

    def getDesktopHeight(self):
        """
        :return: int
        """
        return self.__m_desktopHeight

    @pyqtSlot(int)
    def setDesktopHeight(self, desktopHeight):
        """
        :param desktopHeight: (int) remote desktop height, 0 = widget height.
        :return:
        """
        self.__m_desktopHeight = max(0, int(desktopHeight))  # type: int
        self.update()

    def getColorDepth(self):
        """
        :return: RdpViewerWidget.ColorDepth
        """
        return self.__m_colorDepth

    @pyqtSlot(int)
    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setColorDepth(self, colorDepth):
        """
        :param colorDepth: (ColorDepth | int) 15, 16, 24 or 32 bits.
        :return:
        """
        self.__m_colorDepth = toEnum(RdpViewerWidget.ColorDepth, colorDepth)  # type: RdpViewerWidget.ColorDepth
        self.update()

    def isGraphicsPipeline(self):
        """
        :return: bool
        """
        return self.__m_graphicsPipeline

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setGraphicsPipeline(self, graphicsPipeline):
        """
        :param graphicsPipeline: (bool) RDPGFX (H.264, RemoteFX) or legacy bitmap updates.
        :return:
        """
        self.__m_graphicsPipeline = bool(graphicsPipeline)  # type: bool
        self.update()

    def isAvc444(self):
        """
        :return: bool
        """
        return self.__m_avc444

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setAvc444(self, avc444):
        """
        :param avc444: (bool) allow H.264 AVC444 (full chroma).
        :return:
        """
        self.__m_avc444 = bool(avc444)  # type: bool
        self.update()

    def getClipboardMode(self):
        """
        :return: RdpViewerWidget.ClipboardMode
        """
        return self.__m_clipboardMode

    @pyqtSlot(int)
    @pyqtSlot('QString')
    @pyqtSlot(str)
    @pyqtSlot(ClipboardMode)
    def setClipboardMode(self, clipboardMode):
        """
        :param clipboardMode: (ClipboardMode | int | str) Off, Text or Files.
        :return:
        """
        self.__m_clipboardMode = toEnum(
            RdpViewerWidget.ClipboardMode, clipboardMode)  # type: RdpViewerWidget.ClipboardMode
        self.update()

    def isSoundEnabled(self):
        """
        :return: bool
        """
        return self.__m_soundEnabled

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setSoundEnabled(self, soundEnabled):
        """
        :param soundEnabled: (bool) play the remote sound.
        :return:
        """
        self.__m_soundEnabled = bool(soundEnabled)  # type: bool
        self.update()

    def isMicrophoneEnabled(self):
        """
        :return: bool
        """
        return self.__m_microphoneEnabled

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setMicrophoneEnabled(self, microphoneEnabled):
        """
        :param microphoneEnabled: (bool) redirect the local microphone.
        :return:
        """
        self.__m_microphoneEnabled = bool(microphoneEnabled)  # type: bool
        self.update()

    def getAudioLatency(self):
        """
        :return: int
        """
        return self.__m_audioLatency

    @pyqtSlot(int)
    def setAudioLatency(self, audioLatency):
        """
        :param audioLatency: (int) ms, smallest audio buffer.
        :return:
        """
        self.__m_audioLatency = max(20, int(audioLatency))  # type: int
        self.update()

    def getKeyboardLayout(self):
        """
        :return: RdpViewerWidget.KeyboardLayout
        """
        return self.__m_keyboardLayout

    @pyqtSlot(int)
    @pyqtSlot('QString')
    @pyqtSlot(str)
    @pyqtSlot(KeyboardLayout)
    def setKeyboardLayout(self, keyboardLayout):
        """
        :param keyboardLayout: (KeyboardLayout | int | str | QString) remote keyboard layout.
        :return:
        """
        self.__m_keyboardLayout = toEnum(
            RdpViewerWidget.KeyboardLayout, keyboardLayout)  # type: RdpViewerWidget.KeyboardLayout

    def getKeyboardType(self):
        """
        :return: RdpViewerWidget.KeyboardType
        """
        return self.__m_keyboardType

    @pyqtSlot(int)
    @pyqtSlot('QString')
    @pyqtSlot(str)
    @pyqtSlot(KeyboardType)
    def setKeyboardType(self, keyboardType):
        """
        :param keyboardType: (RdpViewerWidget.KeyboardType | int | str | QString) keyboard type.
        :return:
        """
        self.__m_keyboardType = toEnum(RdpViewerWidget.KeyboardType, keyboardType)  # type: RdpViewerWidget.KeyboardType
        self.update()

    def isSwapAltMeta(self):
        """
        :return: bool
        """
        return self.__m_swapAltMeta

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setSwapAltMeta(self, swapAltMeta):
        """
        :param swapAltMeta: (bool) swap Alt and Meta (Windows) keys.
        :return:
        """
        self.__m_swapAltMeta = bool(swapAltMeta)  # type: bool
        self.update()

    def isNlaSecurity(self):
        """
        :return: bool
        """
        return self.__m_nlaSecurity

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setNlaSecurity(self, nlaSecurity):
        """
        :param nlaSecurity: (bool) FreeRDP NlaSecurity.
        :return:
        """
        self.__m_nlaSecurity = bool(nlaSecurity)  # type: bool
        self.update()

    def isTlsSecurity(self):
        """
        :return: bool
        """
        return self.__m_tlsSecurity

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setTlsSecurity(self, tlsSecurity):
        """
        :param tlsSecurity: (bool) FreeRDP TlsSecurity.
        :return:
        """
        self.__m_tlsSecurity = bool(tlsSecurity)  # type: bool
        self.update()

    def isRdpSecurity(self):
        """
        :return: bool
        """
        return self.__m_rdpSecurity

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setRdpSecurity(self, rdpSecurity):
        """
        :param rdpSecurity: (bool) FreeRDP RdpSecurity (legacy, experimental).
        :return:
        """
        self.__m_rdpSecurity = bool(rdpSecurity)  # type: bool
        self.update()

    def getCertificateMode(self):
        """
        :return: RdpViewerWidget.CertificateMode
        """
        return self.__m_certificateMode

    @pyqtSlot(int)
    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setCertificateMode(self, certificateMode):
        """
        :param certificateMode: (CertificateMode | int | str) Ignore, Tofu or Deny (FreeRDP /cert:).
        :return:
        """
        self.__m_certificateMode = toEnum(
            RdpViewerWidget.CertificateMode, certificateMode)  # type: RdpViewerWidget.CertificateMode
        self.update()

    def getConnectTimeout(self):
        """
        :return: int
        """
        return self.__m_connectTimeout

    @pyqtSlot(int)
    def setConnectTimeout(self, connectTimeout):
        """
        :param connectTimeout: (int) ms to establish the connection (FreeRDP TcpConnectTimeout).
        :return:
        """
        self.__m_connectTimeout = max(1000, int(connectTimeout))  # type: int
        self.update()

    def getAckTimeout(self):
        """
        :return: int
        """
        return self.__m_ackTimeout

    @pyqtSlot(int)
    def setAckTimeout(self, ackTimeout):
        """
        :param ackTimeout: (int) ms before unacknowledged data drops the connection (FreeRDP TcpAckTimeout).
        :return:
        """
        self.__m_ackTimeout = max(1000, int(ackTimeout))  # type: int
        self.update()

    def getShell(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_shell

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setShell(self, shell):
        """
        :param shell: (str | unicode | QString) program started instead of the desktop.
        :return:
        """
        self.__m_shell = shell  # type: str
        self.update()

    def getWorkingDirectory(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_workingDirectory

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setWorkingDirectory(self, workingDirectory):
        """
        :param workingDirectory: (str | unicode | QString) working folder of the shell.
        :return:
        """
        self.__m_workingDirectory = workingDirectory  # type: str
        self.update()

    # ----- FreeRDP channels: advanced input, ssh agent, camera, Remote Assistance -----

    def _applyChannels(self, controller):
        """
        :param controller: RDPClientController
        :return:
        """
        controller.setAdvancedInput(self.__m_advancedInput)
        if self.__m_sshAgent:
            controller.setSshAgent(True)
        if self.__m_cameraRedirection:
            from rdpy.protocol.rdp.channels import rdpecam
            source = rdpecam.TestPatternSource() if self.__m_cameraDevice == 'test' \
                else rdpecam.QtCameraSource(self.__m_cameraDevice)
            controller.setCameraRedirection(True, source)
        if self.__m_invitation is not None:
            controller.setRemoteAssistance(self.__m_invitation, self.__m_username or None)
        controller.setMultitouch(self.__m_multitouch)
        if self.__m_videoRedirection:
            controller.setVideo(True)
        if self.__m_multimediaRedirection:
            from rdpy.protocol.rdp.channels import tsmf
            from rdpy.ui.qt import _get_qt_invoker
            invoker = _get_qt_invoker()
            sound = self.__m_soundEnabled
            controller.setMultimediaRedirection(
                True, lambda rate, channels: tsmf.AudioOutPlayer(rate, channels, invoker.invoke) if sound else None)
        if self.__m_webAuthn:
            from rdpy.ui.qt import askSecurityKeyPin, showSecurityKeyNotification
            controller.setWebAuthn(True, lambda prompt: askSecurityKeyPin(prompt, self),
                                   lambda show, message, timeout, cancel=None:
                                   showSecurityKeyNotification(show, message, timeout, cancel, self))
        controller.setEcho(self.__m_echoChannel)
        if self.__m_rdp2tcpCommand:
            controller.setRdp2tcp(self.__m_rdp2tcpCommand)
        position = self._parseLocation(self.__m_location)
        if position is not None:
            controller.setLocation(*position)

    @staticmethod
    def _parseLocation(text):
        """
        :param text: (str | unicode | QString) 'LATITUDE,LONGITUDE[,ALTITUDE]'.
        :return: tuple | None
        """
        try:
            values = tuple(float(v) for v in text.split(','))
        except ValueError:
            return None
        if len(values) not in (2, 3) or not (-90 <= values[0] <= 90 and -180 <= values[1] <= 180):
            return None
        return values

    def isMultitouch(self):
        """
        :return: bool
        """
        return self.__m_multitouch

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setMultitouch(self, enabled):
        """
        :param enabled: (bool) touch screens and pens through MS-RDPEI.
        :return:
        """
        self.__m_multitouch = bool(enabled)  # type: bool
        self.update()

    def getLocation(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_location

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setLocation(self, location):
        """
        :param location: (str | unicode | QString) 'LATITUDE,LONGITUDE[,ALTITUDE]', '' = no position.
        :return:
        """
        self.__m_location = location if self._parseLocation(location) is not None else ''  # type: str
        self.update()

    def isEchoChannel(self):
        """
        :return: bool
        """
        return self.__m_echoChannel

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setEchoChannel(self, enabled):
        """
        :param enabled: (bool) answer the server's echo channel.
        :return:
        """
        self.__m_echoChannel = bool(enabled)  # type: bool
        self.update()

    def isVideoRedirection(self):
        """
        :return: bool
        """
        return self.__m_videoRedirection

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setVideoRedirection(self, enabled):
        """
        :param enabled: (bool) videos in the session come as H.264 and are drawn where they play.
        :return:
        """
        self.__m_videoRedirection = bool(enabled)  # type: bool
        self.update()

    def isMultimediaRedirection(self):
        """
        :return: bool
        """
        return self.__m_multimediaRedirection

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setMultimediaRedirection(self, enabled):
        """
        :param enabled: (bool) Windows Media Player audio / video decoded and played here (TSMF).
        :return:
        """
        self.__m_multimediaRedirection = bool(enabled)  # type: bool
        self.update()

    def isWebAuthn(self):
        """
        :return: bool
        """
        return self.__m_webAuthn

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setWebAuthn(self, enabled):
        """
        :param enabled: (bool) passkey / security key sign-ins in the session use the local FIDO2 keys.
        :return:
        """
        self.__m_webAuthn = bool(enabled)  # type: bool
        self.update()

    @pyqtSlot(bool)
    def requestRemoteControl(self, control=True):
        """
        Remote Assistance: request the control of the novice's desktop (True) or view only (False).
        :param control: bool
        :return: bool (False: not connected)
        """
        controller = self._controller()
        if controller is None:
            return False
        from twisted.internet import reactor
        reactor.callFromThread(controller.setRemoteControl, bool(control))
        return True

    def getRdp2tcpCommand(self):
        """
        :return: str
        """
        return self.__m_rdp2tcpCommand

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setRdp2tcpCommand(self, command):
        """
        :param command: (str | unicode | QString) local rdp2tcp program, '' = no TCP forwarding.
        :return:
        """
        self.__m_rdp2tcpCommand = command  # type: str
        self.update()

    def isAdvancedInput(self):
        """
        :return: bool
        """
        return self.__m_advancedInput

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setAdvancedInput(self, enabled):
        """
        :param enabled: (bool) mouse and wheel through FreeRDP's advanced input channel when offered.
        :return:
        """
        self.__m_advancedInput = bool(enabled)  # type: bool
        self.update()

    def isSshAgent(self):
        """
        :return: bool
        """
        return self.__m_sshAgent

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setSshAgent(self, enabled):
        """
        :param enabled: (bool) forward the local ssh-agent to the session.
        :return:
        """
        self.__m_sshAgent = bool(enabled)  # type: bool
        self.update()

    def isCameraRedirection(self):
        """
        :return: bool
        """
        return self.__m_cameraRedirection

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setCameraRedirection(self, enabled):
        """
        :param enabled: (bool) redirect the cameras of this computer.
        :return:
        """
        self.__m_cameraRedirection = bool(enabled)  # type: bool
        self.update()

    def getCameraDevice(self):
        """
        :return: str | unicode | QString
        """
        return self.__m_cameraDevice

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def setCameraDevice(self, name):
        """
        :param name: (str | unicode | QString) Camera name filter, '' = all cameras, 'test' = test camera.
        :return:
        """
        self.__m_cameraDevice = name  # type: str
        self.update()

    def setRemoteAssistance(self, invitationFile, password):
        """
        Connect as the expert of a Windows Remote Assistance invitation (FreeRDP /assistance):
        host, port and the ticket come from the invitation, NLA is turned off.
        :param invitationFile: (str | unicode | QString) .msrcincident file.
        :param password: (str | unicode | QString) invitation password.
        :return: bool (False: unreadable invitation or wrong password, see getLastError())
        """
        from rdpy.protocol.rdp.channels import assistance
        try:
            invitation = assistance.parseFile(invitationFile, password)
            host, port = invitation.target()
        except (assistance.AssistanceError, IOError, OSError) as e:
            self.__m_lastError = u'remote assistance: {}'.format(e)  # type: str
            return False
        self.__m_invitation = invitation
        self.setHost(host)
        self.setPort(port)
        self.setNlaSecurity(False)
        if not self.__m_username:
            self.setUsername(invitation.username or 'Expert')
        if not self.__m_password:
            self.setPassword('*')
        return True

    def clearRemoteAssistance(self):
        """
        :return:
        """
        self.__m_invitation = None
        self.update()

    def isSmartSizing(self):
        """
        :return: bool
        """
        return self.__m_smartSizing

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setSmartSizing(self, enabled):
        """
        Scale the remote desktop to the widget (mstsc smart sizing, FreeRDP /smart-sizing):
        the remote resolution stays desktopWidth x desktopHeight whatever the widget size.
        :param enabled: bool
        :return:
        """
        self.__m_smartSizing = bool(enabled)  # type: bool
        if self.__m_desktop is not None:
            self.__m_desktop.setSmartSizing(self.__m_smartSizing, self.__m_keepAspectRatio)

    def isKeepAspectRatio(self):
        """
        :return: bool
        """
        return self.__m_keepAspectRatio

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setKeepAspectRatio(self, keep):
        """
        :param keep: (bool) letterbox with black bars (True) or cover the whole widget (False, default).
        :return:
        """
        self.__m_keepAspectRatio = bool(keep)  # type: bool
        if self.__m_desktop is not None:
            self.__m_desktop.setKeepAspectRatio(self.__m_keepAspectRatio)

    def isAutoConnect(self):
        """
        :return: bool
        """
        return self.__m_autoConnect

    @pyqtSlot(bool)
    @pyqtSlot(int)
    def setAutoConnect(self, autoConnect):
        """
        :param autoConnect: (bool) connect when the widget is first shown.
        :return:
        """
        self.__m_autoConnect = bool(autoConnect)  # type: bool
        self.update()

    # ---------- Qt overrides ----------

    def eventFilter(self, obj, e):
        """
        The window holding the viewer is closing: disconnect first (no reset of
        the server connection), then let the window close.
        :param obj: QObject
        :param e: QEvent
        :return: bool
        """
        if obj is self.__m_window and e.type() == (QEvent.Type.Close if hasattr(QEvent, 'Type') else QEvent.Close):
            if self.__m_state != RdpViewerWidget.State.Idle:
                e.ignore()
                if not self.__m_windowClosing:
                    self.__m_windowClosing = True  # type: bool
                    self.disconnectFromHost()
                    QTimer.singleShot(3000, self._forceWindowClose)
                return True
        return super(RdpViewerWidget, self).eventFilter(obj, e)

    @pyqtSlot()
    def _forceWindowClose(self):
        """
        The server did not let the connection go in time: drop it and close.
        :return:
        """
        if self.__m_windowClosing:
            controller = self._controller()
            if controller is not None:
                controller.abort(u'The window was closed')
            self._onEnded(u'Closed by the user', True)

    def showEvent(self, e):
        """
        :param e: QShowEvent
        :return:
        """
        super(RdpViewerWidget, self).showEvent(e)
        window = self.window()  # type: QWidget
        if window is not self and window is not self.__m_window:
            if self.__m_window is not None:
                self.__m_window.removeEventFilter(self)
            self.__m_window = window
            window.installEventFilter(self)
        if not self.__m_shownOnce:
            self.__m_shownOnce = True  # type: bool
            if self.__m_autoConnect and self.__m_host:
                self.connectToHost()

    def closeEvent(self, e):
        """
        :param e: QCloseEvent
        :return:
        """
        self.disconnectFromHost()
        super(RdpViewerWidget, self).closeEvent(e)

    def sizeHint(self):
        """
        :return: QSize
        """
        return QSize(800, 600)

    def minimumSizeHint(self):
        """
        :return: QSize
        """
        return QSize(160, 120)

    # ---------- Painting ----------

    def paintEvent(self, e):
        """
        Placeholder shown while no session is displayed (and in Qt Designer).
        :param e: QPaintEvent
        :return: None
        """
        if self.__m_desktop is not None and self.__m_desktop.isVisible():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing if hasattr(QPainter, 'RenderHint')
                              else QPainter.Antialiasing)
        rect = QRectF(self.rect())  # type: QRectF
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())  # type: QLinearGradient
        gradient.setColorAt(0.0, QColor('#14213D'))
        gradient.setColorAt(1.0, QColor('#0A0F1E'))
        painter.fillRect(rect, gradient)
        side = min(rect.width(), rect.height()) * 0.35  # type: float
        if not self.__m_logo.isNull() and side > 16:
            logoRect = QRectF(rect.center().x() - side / 2.0, rect.center().y() - side * 0.75, side, side)
            painter.drawPixmap(logoRect.toRect(), self.__m_logo)
        else:
            side = 0.0  # type: float
        font = QFont(self.font())  # type: QFont
        font.setPointSizeF(max(8.0, min(14.0, rect.width() / 40.0)))
        painter.setFont(font)
        painter.setPen(QColor('#C9D6EA'))
        target = '{}:{}'.format(self.__m_host, self.__m_port) if self.__m_host else 'no host set'  # type: str
        names = [n for n, on in (('NLA', self.__m_nlaSecurity), ('TLS', self.__m_tlsSecurity),
                                 ('RDP', self.__m_rdpSecurity)) if on]  # type: list
        lines = ['RdpViewer - {}'.format(target),
                 u'{} - {} bits - certificate: {}'.format('+'.join(names) or 'no security', int(self.__m_colorDepth),
                                                          self.__m_certificateMode.name.lower())]  # type: list[str]
        if self.__m_drives:
            lines.append('drives: {}'.format(', '.join(name for name, path in self.__m_drives)))
        devices = (['printers: {}'.format(', '.join(self.__m_printers))] if self.__m_printers else []) + \
                  (['ports: {}'.format(', '.join(p.split('=', 1)[0].split(',', 1)[0]
                                                 for p in self.__m_serialPorts + self.__m_parallelPorts))]
                   if self.__m_serialPorts or self.__m_parallelPorts else []) + \
                  (['smart cards'] if self.__m_smartcardEnabled else []) + \
                  (['USB: {}'.format(', '.join(self.__m_usbDevices))] if self.__m_usbDevices else [])  # type: list
        if self.__m_remoteApp:
            lines.append(u'RemoteApp: {} (its windows are shown separately)'.format(self.__m_remoteApp))
        if devices:
            lines.append(u' - '.join(devices))
        if self.__m_state == RdpViewerWidget.State.Connecting:
            lines.append(u'Connecting...')
        elif self.__m_lastError:
            lines.append(self.__m_lastError)
        textRect = QRectF(rect.left() + 8, rect.center().y() + side * 0.35, rect.width() - 16,
                          rect.bottom() - rect.center().y() - side * 0.35)  # type: QRectF
        painter.drawText(textRect, int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop |
                                       Qt.TextFlag.TextWordWrap), '\n'.join(lines))
        painter.end()

    # ---------- Properties ----------

    # One call per enumeration (PyQt6 / PySide6 register a single enum per call).
    Q_ENUMS('State' if qVersion()[0] == '4' else State)
    Q_ENUMS('ColorDepth' if qVersion()[0] == '4' else ColorDepth)
    Q_ENUMS('ClipboardMode' if qVersion()[0] == '4' else ClipboardMode)
    Q_ENUMS('CertificateMode' if qVersion()[0] == '4' else CertificateMode)
    Q_ENUMS('SecurityPreset' if qVersion()[0] == '4' else SecurityPreset)
    Q_ENUMS('KeyboardLayout' if qVersion()[0] == '4' else KeyboardLayout)
    Q_ENUMS('KeyboardType' if qVersion()[0] == '4' else KeyboardType)
    Q_ENUMS('TransferDirection' if qVersion()[0] == '4' else TransferDirection)
    Q_ENUMS('TransferStatus' if qVersion()[0] == '4' else TransferStatus)
    host = pyqtProperty('QString', fget=getHost, fset=setHost)  # type: pyqtProperty
    port = pyqtProperty(int, fget=getPort, fset=setPort)  # type: pyqtProperty
    username = pyqtProperty('QString', fget=getUsername, fset=setUsername)  # type: pyqtProperty
    password = pyqtProperty('QString', fget=getPassword, fset=setPassword)  # type: pyqtProperty
    domain = pyqtProperty('QString', fget=getDomain, fset=setDomain)  # type: pyqtProperty
    clientName = pyqtProperty('QString', fget=getClientName, fset=setClientName)  # type: pyqtProperty
    desktopWidth = pyqtProperty(int, fget=getDesktopWidth, fset=setDesktopWidth)  # type: pyqtProperty
    desktopHeight = pyqtProperty(int, fget=getDesktopHeight, fset=setDesktopHeight)  # type: pyqtProperty
    colorDepth = pyqtProperty(ColorDepth, fget=getColorDepth, fset=setColorDepth)  # type: pyqtProperty
    graphicsPipeline = pyqtProperty(bool, fget=isGraphicsPipeline, fset=setGraphicsPipeline)  # type: pyqtProperty
    avc444 = pyqtProperty(bool, fget=isAvc444, fset=setAvc444)  # type: pyqtProperty
    clipboardMode = pyqtProperty(ClipboardMode, fget=getClipboardMode, fset=setClipboardMode)  # type: pyqtProperty
    soundEnabled = pyqtProperty(bool, fget=isSoundEnabled, fset=setSoundEnabled)  # type: pyqtProperty
    microphoneEnabled = pyqtProperty(bool, fget=isMicrophoneEnabled, fset=setMicrophoneEnabled)  # type: pyqtProperty
    audioLatency = pyqtProperty(int, fget=getAudioLatency, fset=setAudioLatency)  # type: pyqtProperty
    keyboardLayout = pyqtProperty(KeyboardLayout, fget=getKeyboardLayout, fset=setKeyboardLayout)  # type: pyqtProperty
    keyboardType = pyqtProperty(KeyboardType, fget=getKeyboardType, fset=setKeyboardType)  # type: pyqtProperty
    swapAltMeta = pyqtProperty(bool, fget=isSwapAltMeta, fset=setSwapAltMeta)  # type: pyqtProperty
    nlaSecurity = pyqtProperty(bool, fget=isNlaSecurity, fset=setNlaSecurity)  # type: pyqtProperty
    tlsSecurity = pyqtProperty(bool, fget=isTlsSecurity, fset=setTlsSecurity)  # type: pyqtProperty
    rdpSecurity = pyqtProperty(bool, fget=isRdpSecurity, fset=setRdpSecurity)  # type: pyqtProperty
    certificateMode = pyqtProperty(
        CertificateMode, fget=getCertificateMode, fset=setCertificateMode)  # type: pyqtProperty
    connectTimeout = pyqtProperty(int, fget=getConnectTimeout, fset=setConnectTimeout)  # type: pyqtProperty
    ackTimeout = pyqtProperty(int, fget=getAckTimeout, fset=setAckTimeout)  # type: pyqtProperty
    shell = pyqtProperty('QString', fget=getShell, fset=setShell)  # type: pyqtProperty
    workingDirectory = pyqtProperty('QString', fget=getWorkingDirectory, fset=setWorkingDirectory)  # type: pyqtProperty
    autoConnect = pyqtProperty(bool, fget=isAutoConnect, fset=setAutoConnect)  # type: pyqtProperty
    smartSizing = pyqtProperty(bool, fget=isSmartSizing, fset=setSmartSizing)  # type: pyqtProperty
    advancedInput = pyqtProperty(bool, fget=isAdvancedInput, fset=setAdvancedInput)  # type: pyqtProperty
    sshAgent = pyqtProperty(bool, fget=isSshAgent, fset=setSshAgent)  # type: pyqtProperty
    cameraRedirection = pyqtProperty(bool, fget=isCameraRedirection, fset=setCameraRedirection)  # type: pyqtProperty
    cameraDevice = pyqtProperty('QString', fget=getCameraDevice, fset=setCameraDevice)  # type: pyqtProperty
    multitouch = pyqtProperty(bool, fget=isMultitouch, fset=setMultitouch)  # type: pyqtProperty
    location = pyqtProperty('QString', fget=getLocation, fset=setLocation)  # type: pyqtProperty
    echoChannel = pyqtProperty(bool, fget=isEchoChannel, fset=setEchoChannel)  # type: pyqtProperty
    rdp2tcpCommand = pyqtProperty('QString', fget=getRdp2tcpCommand, fset=setRdp2tcpCommand)  # type: pyqtProperty
    videoRedirection = pyqtProperty(bool, fget=isVideoRedirection, fset=setVideoRedirection)  # type: pyqtProperty
    webAuthn = pyqtProperty(bool, fget=isWebAuthn, fset=setWebAuthn)  # type: pyqtProperty
    multimediaRedirection = pyqtProperty(bool, fget=isMultimediaRedirection,
                                         fset=setMultimediaRedirection)  # type: pyqtProperty
    keepAspectRatio = pyqtProperty(bool, fget=isKeepAspectRatio, fset=setKeepAspectRatio)  # type: pyqtProperty
    drives = pyqtProperty('QStringList', fget=getDrives, fset=setDrives)  # type: pyqtProperty
    printers = pyqtProperty('QStringList', fget=getPrinters, fset=setPrinters)  # type: pyqtProperty
    serialPorts = pyqtProperty('QStringList', fget=getSerialPorts, fset=setSerialPorts)  # type: pyqtProperty
    parallelPorts = pyqtProperty('QStringList', fget=getParallelPorts, fset=setParallelPorts)  # type: pyqtProperty
    smartcardEnabled = pyqtProperty(bool, fget=isSmartcardEnabled, fset=setSmartcardEnabled)  # type: pyqtProperty
    smartcardFilter = pyqtProperty('QString', fget=getSmartcardFilter, fset=setSmartcardFilter)  # type: pyqtProperty
    usbDevices = pyqtProperty('QStringList', fget=getUsbDevices, fset=setUsbDevices)  # type: pyqtProperty
    dynamicResolution = pyqtProperty(bool, fget=isDynamicResolution, fset=setDynamicResolution)  # type: pyqtProperty
    remoteApp = pyqtProperty('QString', fget=getRemoteApp, fset=setRemoteApp)  # type: pyqtProperty
    remoteAppArguments = pyqtProperty('QString', fget=getRemoteAppArguments,
                                      fset=setRemoteAppArguments)  # type: pyqtProperty
    remoteAppWorkingDirectory = pyqtProperty('QString', fget=getRemoteAppWorkingDirectory,
                                             fset=setRemoteAppWorkingDirectory)  # type: pyqtProperty
    securityPreset = pyqtProperty(SecurityPreset, fget=getSecurityPreset, fset=setSecurityPreset)  # type: pyqtProperty
