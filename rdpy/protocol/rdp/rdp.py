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
Use to manage RDP stack in twisted.
"""
from __future__ import absolute_import, division, print_function
from weakref import WeakSet
from rdpy.core import layer
from rdpy.core.error import CallPureVirtualFuntion, InvalidValue
from .channels import ainput, sshagent, rdpecam, remdesk, echo, location, rdpei, rdp2tcp, geometry, video, encomsp, \
    rdpewa, tsmf
from .channels import drdynvc, rdpsnd, cliprdr, rdpdr, disp, rail, urbdrc
from . import tpkt, x224, sec, serverinfo
from .nla import cssp, ntlm
from .t125 import mcs, gcc
from rdpy.core import log
from . import pdu


class SecurityLevel(object):
    """
    @summary: RDP security level
    """
    RDP_LEVEL_RDP = 0
    RDP_LEVEL_SSL = 1
    RDP_LEVEL_NLA = 2


class RDPClientController(pdu.layer.PDUClientListener):
    """
    Manage RDP stack as client
    """
    # Every controller, so an application can close all its sessions on exit.
    _instances = WeakSet()

    def __init__(self):
        RDPClientController._instances.add(self)
        #list of observer
        self._clientObserver = []
        #dynamic virtual channel layer
        self._drdynvcLayer = drdynvc.DrdynvcLayer()
        self._drdynvcLayer.setGfxCallback(self._onGfxBitmap)
        self._drdynvcLayer.setGfxSurfaceCallback(self._onGfxSurface)
        self._drdynvcLayer.setGfxPresentCallback(self._onGfxPresent)
        self._drdynvcLayer.setCapsConfirmCallback(self._onGfxCapsConfirm)
        #display control (MS-RDPEDISP): the resolution follows the window
        self._disp = disp.DisplayControl()
        self._disp.setCapsCallback(self._onDisplayControlReady)
        self._drdynvcLayer.addListener(disp.CHANNEL_NAME, self._disp.channelFactory)
        #USB redirection (MS-RDPEUSB): enabled by addUsbDevice
        self._urbdrc = urbdrc.Urbdrc()
        self._urbdrc.enabled = False
        self._usbHotplug = None
        self._drdynvcLayer.addListener(urbdrc.CHANNEL_NAME, self._urbdrc.channelFactory)
        #advanced input (FreeRDP::Advanced::Input): opened by FreeRDP based servers
        self._ainput = ainput.AInput()
        self._drdynvcLayer.addListener(ainput.CHANNEL_NAME, self._ainput.channelFactory)
        #ssh agent forwarding (SSHAGENT), enabled by setSshAgent
        self._sshagent = sshagent.SshAgent()
        self._drdynvcLayer.addListener(sshagent.CHANNEL_NAME, self._sshagent.channelFactory)
        #camera redirection (MS-RDPECAM), enabled by setCameraRedirection
        self._rdpecam = rdpecam.Rdpecam()
        self._rdpecam.addTo(self._drdynvcLayer)
        #echo (MS-RDPEECO), location (MS-RDPEL), multi-touch and pen (MS-RDPEI)
        self._echo = echo.Echo()
        self._drdynvcLayer.addListener(echo.CHANNEL_NAME, self._echo.channelFactory)
        self._location = location.Location()
        self._drdynvcLayer.addListener(location.CHANNEL_NAME, self._location.channelFactory)
        self._rdpei = rdpei.Rdpei()
        self._drdynvcLayer.addListener(rdpei.CHANNEL_NAME, self._rdpei.channelFactory)
        #geometry tracking (MS-RDPEGT) and video optimized remoting (MS-RDPEVOR)
        self._geometry = geometry.Geometry()
        self._drdynvcLayer.addListener(geometry.CHANNEL_NAME, self._geometry.channelFactory)
        self._video = video.Video(self._geometry)
        self._video.setFrameCallback(self._onVideoFrame)
        self._drdynvcLayer.addListener(video.CONTROL_CHANNEL_NAME, self._video.controlFactory)
        self._drdynvcLayer.addListener(video.DATA_CHANNEL_NAME, self._video.dataFactory)
        #multimedia redirection (MS-RDPEV, TSMF): Windows Media Player samples decoded here
        self._tsmf = tsmf.Tsmf()
        self._tsmf.frameCallback = self._onTsmfFrame
        self._drdynvcLayer.addListener(tsmf.CHANNEL_NAME, self._tsmf.channelFactory)
        #WebAuthn redirection (MS-RDPEWA): security keys of this computer for passkeys in the session
        self._rdpewa = rdpewa.Rdpewa()
        self._drdynvcLayer.addListener(rdpewa.CHANNEL_NAME, self._rdpewa.channelFactory)
        #multiparty (MS-RDPEMSC, encomsp static channel): Remote Assistance control
        self._encomspLayer = encomsp.EncomspLayer()
        self._encomspLayer.setListener(self.onEncomspEvent)
        #rdp2tcp static channel (TCP forwarding with an external program), added by setRdp2tcp
        self._rdp2tcpLayer = rdp2tcp.Rdp2tcpLayer()
        #Remote Assistance (remdesk static channel), added by setRemoteAssistance
        self._remdeskLayer = remdesk.RemdeskLayer()
        self._remdeskLayer.setListener(self.onRemoteAssistanceEvent)
        self._remoteAssistance = None
        #audio output virtual channel layer
        self._rdpsndLayer = rdpsnd.RdpsndLayer()
        self._drdynvcLayer.setRdpsndLayer(self._rdpsndLayer)
        #clipboard virtual channel layer
        self._cliprdrLayer = cliprdr.CliprdrLayer()
        #device redirection (drives...) virtual channel layer
        self._rdpdrLayer = rdpdr.RdpdrLayer()
        #RemoteApp (MS-RDPERP): the rail channel is added by setRemoteApp
        self._railLayer = rail.RailLayer()
        self._railLayer.setListener(self.onRailEvent)
        self._railMode = False
        #PDU layer
        self._pduLayer = pdu.layer.Client(self)
        # AVC freeze recovery: when H.264 output stalls for too long, request
        # a full-screen refresh (IDR) from the server via REFRESH_RECT PDU.
        self._drdynvcLayer.setRequestRefreshCallback(self._pduLayer.requestFullRefresh)
        #secure layer
        self._secLayer = sec.Client(self._pduLayer)
        # multichannel service with drdynvc, rdpsnd, cliprdr, and rdpdr virtual channels
        channels = [
            (gcc.ChannelDef(b'rdpdr',
                            gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP | gcc.ChannelOptions.CHANNEL_OPTION_COMPRESS_RDP),
             self._rdpdrLayer),
            (gcc.ChannelDef(b'rdpsnd',
                            gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP),
             self._rdpsndLayer),
            (gcc.ChannelDef(b'cliprdr',
                            gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP | gcc.ChannelOptions.CHANNEL_OPTION_COMPRESS_RDP | gcc.ChannelOptions.CHANNEL_OPTION_SHOW_PROTOCOL),
             self._cliprdrLayer),
            (gcc.ChannelDef(b'drdynvc',
                            gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP),
             self._drdynvcLayer),
        ]
        # channels that options may remove and add back
        self._optionalChannels = {
            self._rdpsndLayer: channels[1], self._cliprdrLayer: channels[2],
            self._railLayer: (gcc.ChannelDef(b'rail',
                                             gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP | gcc.ChannelOptions.CHANNEL_OPTION_COMPRESS_RDP | gcc.ChannelOptions.CHANNEL_OPTION_SHOW_PROTOCOL),
                              self._railLayer),
            self._remdeskLayer: (gcc.ChannelDef(b'remdesk',
                                                gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP | gcc.ChannelOptions.CHANNEL_OPTION_COMPRESS_RDP | gcc.ChannelOptions.CHANNEL_OPTION_SHOW_PROTOCOL),
                                 self._remdeskLayer),
            self._rdp2tcpLayer: (gcc.ChannelDef(b'rdp2tcp',
                                                gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP | gcc.ChannelOptions.CHANNEL_OPTION_COMPRESS_RDP),
                                 self._rdp2tcpLayer),
            self._encomspLayer: (gcc.ChannelDef(b'encomsp',
                                                gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP | gcc.ChannelOptions.CHANNEL_OPTION_COMPRESS_RDP | gcc.ChannelOptions.CHANNEL_OPTION_SHOW_PROTOCOL),
                                 self._encomspLayer),
        }
        self._mcsLayer = mcs.Client(self._secLayer, channels)
        #transport pdu layer
        self._x224Layer = x224.Client(self._mcsLayer)
        #transport packet (protocol layer)
        self._tpktLayer = tpkt.TPKT(self._x224Layer)
        #fastpath stack
        self._pduLayer.initFastPath(self._secLayer)
        self._secLayer.initFastPath(self._tpktLayer)
        #is pdu layer is ready to send
        self._isReady = False
        # GFX input gating: suppress input events until CAPS_CONFIRM or timeout
        self._gfxPending = False
        self._gfxTimer = None
        #routing token for server redirection
        self._redirectRoutingToken = None
        # optional features (see setColorDepth, setGraphicsPipeline, ...)
        self._colorDepth = 32
        self._gfxEnabled = True
        self._clipboardMode = 'files'
        self._soundEnabled = True
        self._microphoneEnabled = False
        # what the server told us (session report)
        self._logonInfo = {}
        # logon state: login screen shown / user logged on (desktop)
        self._loggedOn = False
        self._loginScreenNotified = False
        # file transfers (clipboard and redirected drives) -> observers
        self._cliprdrLayer.setTransferListener(self)
        self._drdynvcLayer._desktopSizeCallback = self.onDesktopSize
        self._rdpdrLayer.setDriveTransferCallback(self.onDriveFileTransferred)
        self._rdpdrLayer.setPrintJobCallback(self.onPrintJob)
        self._lastError = None
        self._connectedAt = None

    # -------------------------------------------------------------------
    # Optional features (call before connecting)
    # -------------------------------------------------------------------

    def setColorDepth(self, bpp):
        """
        @summary: Session colour depth (MS-RDPBCGR 2.2.1.3.2 highColorDepth)
        @param bpp: {int} 15, 16, 24 or 32. The graphics pipeline always runs
        in 32 bits, so a lower depth uses the legacy bitmap updates (as FreeRDP).
        """
        if bpp not in (15, 16, 24, 32):
            raise ValueError(u'colour depth must be 15, 16, 24 or 32')
        core = self._mcsLayer._clientSettings.getBlock(gcc.MessageType.CS_CORE)
        core.highColorDepth.value = {15: gcc.HighColor.HIGH_COLOR_15BPP,
                                     16: gcc.HighColor.HIGH_COLOR_16BPP}.get(bpp, gcc.HighColor.HIGH_COLOR_24BPP)
        if bpp == 32:
            core.earlyCapabilityFlags.value |= gcc.CapabilityFlags.RNS_UD_CS_WANT_32BPP_SESSION
        else:
            core.earlyCapabilityFlags.value &= ~gcc.CapabilityFlags.RNS_UD_CS_WANT_32BPP_SESSION
            self.setGraphicsPipeline(False)
        self._colorDepth = bpp
        self._pduLayer._colorDepth = bpp

    def setGraphicsPipeline(self, enabled):
        """
        @summary: Use the graphics pipeline (RDPGFX, H.264...) or legacy bitmap updates
        """
        core = self._mcsLayer._clientSettings.getBlock(gcc.MessageType.CS_CORE)
        if enabled and self._colorDepth == 32:
            core.earlyCapabilityFlags.value |= gcc.CapabilityFlags.RNS_UD_CS_SUPPORT_DYNVC_GFX_PROTOCOL
            self._gfxEnabled = True
        else:
            core.earlyCapabilityFlags.value &= ~gcc.CapabilityFlags.RNS_UD_CS_SUPPORT_DYNVC_GFX_PROTOCOL
            self._gfxEnabled = False
        self._drdynvcLayer.setGraphicsEnabled(self._gfxEnabled)

    def _setChannel(self, layer, enabled):
        channels = self._mcsLayer._virtualChannels
        present = any(vc[1] is layer for vc in channels)
        if not enabled and present:
            self._mcsLayer._virtualChannels = [vc for vc in channels if vc[1] is not layer]
        elif enabled and not present and layer in self._optionalChannels:
            self._mcsLayer._virtualChannels = list(channels) + [self._optionalChannels[layer]]

    def setClipboardMode(self, mode):
        """
        @summary: Clipboard sharing
        @param mode: {str} 'off' (no clipboard channel), 'text' or 'files' (text and files)
        """
        if mode not in ('off', 'text', 'files'):
            raise ValueError(u'clipboard mode must be off, text or files')
        self._clipboardMode = mode
        self._setChannel(self._cliprdrLayer, mode != 'off')
        self._cliprdrLayer.setFileTransferEnabled(mode == 'files')

    def setSoundEnabled(self, enabled):
        """
        @summary: Play the remote sound locally (default True)
        """
        self._soundEnabled = bool(enabled)
        self._setChannel(self._rdpsndLayer, self._soundEnabled)
        self._drdynvcLayer.setSoundEnabled(self._soundEnabled)
        info = self._secLayer._info.flag
        if self._soundEnabled:
            info.value &= ~sec.InfoFlag.INFO_NOAUDIOPLAYBACK
        else:
            info.value |= sec.InfoFlag.INFO_NOAUDIOPLAYBACK

    def setMicrophoneEnabled(self, enabled):
        """
        @summary: Redirect the local microphone to the server (MS-RDPEAI)
        """
        self._microphoneEnabled = bool(enabled)
        self._drdynvcLayer.setAudioInputEnabled(self._microphoneEnabled)
        info = self._secLayer._info.flag
        if self._microphoneEnabled:
            info.value |= sec.InfoFlag.INFO_AUDIOCAPTURE
        else:
            info.value &= ~sec.InfoFlag.INFO_AUDIOCAPTURE

    def setWorkingDirectory(self, path):
        """
        @summary: Working folder of the alternate shell (setAlternateShell)
        """
        self._secLayer._info.workingDir.value = path

    # -------------------------------------------------------------------
    # FreeRDP channels: advanced input, ssh agent, camera, Remote Assistance
    # -------------------------------------------------------------------

    def setAdvancedInput(self, enabled):
        """
        @summary: Accept FreeRDP's advanced input channel (default True): while a server
        opens it, mouse and wheel events use it instead of the input PDUs
        """
        self._ainput.enabled = bool(enabled)

    def setSshAgent(self, enabled, path=None):
        """
        @summary: Forward the local ssh-agent to the session (FreeRDP /ssh-agent)
        @param path: {str|None} agent socket / pipe (None: SSH_AUTH_SOCK, or the Windows OpenSSH pipe)
        """
        self._sshagent.enabled = bool(enabled)
        self._sshagent.path = path

    def setCameraRedirection(self, enabled, source=None):
        """
        @summary: Redirect the cameras of this computer (MS-RDPECAM)
        @param source: {rdpecam.CameraSource|None} None: Qt Multimedia cameras
        """
        self._rdpecam.enabled = bool(enabled)
        if source is not None:
            self._rdpecam.source = source

    def setRemoteAssistance(self, invitation, expertName=None):
        """
        @summary: Connect as the expert of a Windows Remote Assistance invitation
        (FreeRDP /assistance): remdesk channel, and the Client Info of Remote Assistance
        mode (FreeRDP info.c: password "*", alternate shell "*" with a PassStub, the
        RA session id as working directory)
        @param invitation: {assistance.Invitation}
        """
        self._remoteAssistance = invitation
        self._remdeskLayer.setInvitation(invitation, expertName or invitation.username or None)
        self._setChannel(self._remdeskLayer, True)
        info = self._secLayer._info
        info.password.value = u'*'
        info.alternateShell.value = u'*' if invitation.passStub else (invitation.password or u'')
        info.workingDir.value = invitation.sessionId or u''
        # FreeRDP /assistance enables the encomsp channel too (control requests)
        self.setEncomsp(True, getattr(self, '_requestControl', True))
        if invitation.username:
            self.setUsername(invitation.username)

    def setEcho(self, enabled):
        """@summary: Answer the server's echo channel (MS-RDPEECO, FreeRDP /echo)"""
        self._echo.enabled = bool(enabled)

    def setLocation(self, latitude=None, longitude=None, altitude=0, enabled=True):
        """
        @summary: Give a position to the session (MS-RDPEL). Without coordinates the channel
        is accepted and the position comes later (setLocation again or a position source).
        """
        self._location.enabled = bool(enabled)
        if latitude is not None and longitude is not None:
            self._location.setPosition(latitude, longitude, altitude)

    def setMultitouch(self, enabled):
        """@summary: Multi-touch and pen input (MS-RDPEI, FreeRDP /multitouch)"""
        self._rdpei.enabled = bool(enabled)

    def sendTouch(self, points):
        """
        @summary: Touch points of one event: [(id, 'begin'|'update'|'end'|'cancel', x, y)]
        @return: {bool} sent through MS-RDPEI (False: not available, use mouse events)
        """
        if not self._isReady:
            return False
        return self._rdpei.touch(points)

    def sendPen(self, state, x, y, **kwargs):
        """@summary: Pen event (rdpei.Rdpei.pen); @return: {bool} sent through MS-RDPEI"""
        if not self._isReady:
            return False
        return self._rdpei.pen(state, x, y, **kwargs)

    def setRdp2tcp(self, command):
        """
        @summary: TCP forwarding with rdp2tcp (FreeRDP /rdp2tcp:<command>): the local
        program is started when the channel joins and relayed to the server
        """
        self._rdp2tcpLayer.setCommand(command)
        self._setChannel(self._rdp2tcpLayer, bool(command))

    def setVideo(self, enabled):
        """
        @summary: Video optimized remoting (MS-RDPEVOR, FreeRDP /video): videos playing in the
        session come as H.264 and are shown where the geometry channel places them; enables
        geometry tracking too, which it needs
        """
        self._video.enabled = bool(enabled)
        if enabled:
            self._geometry.enabled = True

    def setGeometryTracking(self, enabled):
        """@summary: Geometry tracking (MS-RDPEGT, FreeRDP /geometry)"""
        self._geometry.enabled = bool(enabled)

    def setMultimediaRedirection(self, enabled, audioFactory=None):
        """
        @summary: Multimedia redirection (MS-RDPEV, TSMF, FreeRDP /multimedia): Windows Media
        Player audio / video samples decoded and played here
        @param audioFactory: {callable(rate, channels) -> player} (tsmf.AudioOutPlayer by default)
        """
        self._tsmf.enabled = bool(enabled)
        if audioFactory is not None:
            self._tsmf.audioFactory = audioFactory

    def _onTsmfFrame(self, x, y, width, height, bgra, visibleRects):
        """@summary: A TSMF video frame (playback thread)"""
        self._fanOut('onTsmfFrame', x, y, width, height, bgra, visibleRects)

    def setWebAuthn(self, enabled, pinCallback=None, notifyCallback=None):
        """
        @summary: WebAuthn redirection (MS-RDPEWA): passkey / security key sign-ins in the
        session use the FIDO2 keys plugged into this computer (python-fido2)
        @param pinCallback: {callable(prompt) -> str|None} asks the user for the key's PIN
        """
        self._rdpewa.enabled = bool(enabled)
        if pinCallback is not None:
            self._rdpewa.setPinCallback(pinCallback)
        if notifyCallback is not None:
            self._rdpewa.setNotifyCallback(notifyCallback)

    def setEncomsp(self, enabled, requestControl=True):
        """
        @summary: Multiparty channel (MS-RDPEMSC); with Remote Assistance it requests the control
        of the novice's desktop once (requestControl, FreeRDP RemoteAssistanceRequestControl)
        """
        self._encomspLayer.requestControl = bool(requestControl)
        self._setChannel(self._encomspLayer, bool(enabled))

    def setRemoteControl(self, control):
        """@summary: Remote Assistance: ask for control (True) or go back to view only (False)"""
        self._encomspLayer.setControl(bool(control))

    def onEncomspEvent(self, *event):
        """@summary: encomsp events (EncomspLayer listener)"""
        self._fanOut('onEncomspEvent', *event)

    def _onVideoFrame(self, x, y, width, height, bgra):
        """@summary: A video frame to show at x, y (MS-RDPEVOR), reactor thread"""
        self._fanOut('onVideoFrame', x, y, width, height, bgra)

    def onRemoteAssistanceEvent(self, *event):
        """@summary: remdesk events: ('version', minor), ('result', code, text), ('disconnect',)"""
        self._fanOut('onRemoteAssistanceEvent', *event)

    # -------------------------------------------------------------------
    # Display control (MS-RDPEDISP, FreeRDP /dynamic-resolution)
    # -------------------------------------------------------------------

    def setDynamicResolution(self, enabled):
        """
        @summary: Accept the display control channel (default True): requestDesktopSize()
        then changes the remote resolution without reconnecting. Call before connecting.
        """
        self._disp.enabled = bool(enabled)

    def setScaleFactors(self, desktopScaleFactor=100, deviceScaleFactor=100):
        """
        @summary: Scaling of the remote desktop (FreeRDP /scale-desktop, /scale-device):
        sent when connecting and in the display control layouts
        @param desktopScaleFactor: {int} 100 - 500 percent
        @param deviceScaleFactor: {int} 100, 140 or 180 percent
        @raise ValueError: values the server does not accept
        """
        desktopScaleFactor, deviceScaleFactor = int(desktopScaleFactor), int(deviceScaleFactor)
        if not disp.validScaleFactors(desktopScaleFactor, deviceScaleFactor):
            raise ValueError(u'desktop scale must be 100 - 500 %, device scale 100, 140 or 180 %')
        core = self._mcsLayer._clientSettings.getBlock(gcc.MessageType.CS_CORE)
        core.desktopScaleFactor.value = desktopScaleFactor
        core.deviceScaleFactor.value = deviceScaleFactor
        self._disp.desktopScaleFactor = desktopScaleFactor
        self._disp.deviceScaleFactor = deviceScaleFactor

    def isDynamicResolutionActive(self):
        """@return: {bool} the server opened the display control channel (any thread)"""
        return self._isReady and self._disp.isActive()

    def requestDesktopSize(self, width, height, physicalWidth=0, physicalHeight=0):
        """
        @summary: Ask the server to change the resolution (display control channel), network
        thread. The new size arrives through onDesktopResize.
        @param physicalWidth: {int} millimetres of the local screen area, 0 when unknown
        @return: {tuple|None} (width, height) requested (even, 200 - 8192), None when the
        channel is not open (reconnect with the new size instead)
        """
        if not self._disp.enabled or not self._isReady:
            return None
        size = self._disp.requestSize(width, height, physicalWidth, physicalHeight)
        return size if self._disp.isActive() else None

    def _onDisplayControlReady(self, maxMonitors, factorA, factorB):
        self._fanOut('onDynamicResolutionReady')

    # -------------------------------------------------------------------
    # RemoteApp (MS-RDPERP, FreeRDP /app)
    # -------------------------------------------------------------------

    def setRemoteApp(self, app, hidef=False):
        """
        @summary: Run one program instead of the desktop: its windows are shown as local
        windows (RemoteApp). Call before connecting.
        @param app: {rail.RemoteApp | str} program ("||alias" of a published RemoteApp, or a
        path the server allows), see rail.parseAppSpec
        @param hidef: {bool} Enhanced RemoteApp (window surfaces through the graphics pipeline)
        """
        if not isinstance(app, rail.RemoteApp):
            app = rail.parseAppSpec(app)
        hidef = bool(getattr(app, 'hidef', hidef))
        self._railMode = True
        self._railLayer.app = app
        self._setChannel(self._railLayer, True)
        info = self._secLayer._info
        info.flag.value |= sec.InfoFlag.INFO_RAIL
        if hidef:
            info.flag.value |= sec.InfoFlag.INFO_HIDEF_RAIL_SUPPORTED
        else:
            info.flag.value &= ~sec.InfoFlag.INFO_HIDEF_RAIL_SUPPORTED
        info.alternateShell.value = u''  # the program starts through the rail channel
        # no wallpaper; windows dragged with their content (the server moves them)
        perf = info.extendedInfo.performanceFlags
        perf.value = (perf.value | sec.PerfFlag.PERF_DISABLE_WALLPAPER) & ~sec.PerfFlag.PERF_DISABLE_FULLWINDOWDRAG
        self._pduLayer._railEnabled = True
        self._pduLayer._ordersHandler = rail.WindowOrderParser(self.onRailEvent)
        self._drdynvcLayer.setGfxWindowCallback(self._onGfxWindowSurface)

    def isRemoteApp(self):
        """@return: {bool} RemoteApp session (setRemoteApp)"""
        return self._railMode

    def getRailLayer(self):
        """@return: {rail.RailLayer} window commands: activate, sysCommand, sysMenu,
        notifyEvent, windowMove, setWorkArea (any thread)"""
        return self._railLayer

    def onRailEvent(self, kind, *args):
        """
        @summary: RemoteApp event (rail channel or window order, network thread; 'surface'
        from the graphics thread): to the observers' onRailEvent(kind, *args)
        """
        if kind == 'desktop':
            self._railLayer.onDesktopOrder(args[0])
        self._fanOut('onRailEvent', kind, *args)

    def _onGfxWindowSurface(self, windowId, x, y, width, height, bgra, surfaceWidth, surfaceHeight,
                            mappedWidth, mappedHeight):
        self._fanOut('onRailEvent', 'surface', windowId, x, y, width, height, bgra, surfaceWidth,
                     surfaceHeight, mappedWidth, mappedHeight)

    # -------------------------------------------------------------------
    # Session report (PyFreeRdpApp getSessionInfo)
    # -------------------------------------------------------------------

    def onSessionInfo(self, infoType, data):
        """
        @summary: Save Session Info from the server (MS-RDPBCGR 2.2.10.1). Windows
        sends a logon notification (INFOTYPE_LOGON, _LOGON_LONG or
        _LOGON_PLAINNOTIFY) once the user is logged on and the desktop starts,
        and logon errors in INFOTYPE_LOGON_EXTENDED_INFO - FreeRDP SaveSessionInfo.
        """
        if infoType == serverinfo.INFOTYPE_LOGON_EXTENDED_INFO:
            error = serverinfo.parseLogonErrors(data)
            if error is not None:
                log.warning(u'logon: %s' % error[2])
                for observer in self._clientObserver:
                    callback = getattr(observer, 'onLogonError', None)
                    if callback is not None:
                        callback(error[0], error[1], error[2])
            return
        if infoType in (serverinfo.INFOTYPE_LOGON, serverinfo.INFOTYPE_LOGON_LONG,
                        serverinfo.INFOTYPE_LOGON_PLAINNOTIFY) and not self._loggedOn:
            self._loggedOn = True
            log.info(u'logon: the user is logged on, the desktop is starting')
            for observer in self._clientObserver:
                callback = getattr(observer, 'onDesktopReady', None)
                if callback is not None:
                    callback()
        info = serverinfo.parseLogonInfo(infoType, data)
        if info:
            self._logonInfo = info
            log.info(u'logged on as %s\\%s (session %d)' % (info.get('domain', ''), info.get('user', ''),
                                                            info.get('sessionId', 0)))

    def onFirstPicture(self):
        """
        @summary: Called by the UI when the first picture of the connection is
        shown. Before the logon notification that picture is the Windows
        logon (or welcome) screen.
        """
        if self._loggedOn or self._loginScreenNotified:
            return
        self._loginScreenNotified = True
        log.info(u'logon: the login screen is displayed')
        for observer in self._clientObserver:
            callback = getattr(observer, 'onLoginScreen', None)
            if callback is not None:
                callback()

    def _fanOut(self, name, *args):
        for observer in self._clientObserver:
            callback = getattr(observer, name, None)
            if callback is not None:
                try:
                    callback(*args)
                except Exception as e:
                    log.warning(u'observer %s failed: %s' % (name, e))

    def onDesktopSize(self, width, height):
        """
        @summary: The remote desktop resolution (server Demand Active, RDPGFX ResetGraphics);
        any thread
        """
        if width > 0 and height > 0:
            drdynvc.callInReactor(self._disp.noteDesktopSize, int(width), int(height))
            self._fanOut('onDesktopResize', int(width), int(height))

    def getServerDesktopSize(self):
        """@return: {tuple|None} (width, height) the server activated the session with"""
        if getattr(self._pduLayer, '_serverDesktopSize', None):
            return self._pduLayer._serverDesktopSize
        cap = self._pduLayer._serverCapabilities.get(pdu.caps.CapsType.CAPSTYPE_BITMAP)
        if cap is None:
            return None
        try:
            return (cap.capability.desktopWidth.value, cap.capability.desktopHeight.value)
        except AttributeError:
            return None

    def onFileTransferStarted(self, direction, items, totalBytes):
        """@summary: clipboard file transfer started ('upload' local -> server, 'download')"""
        log.info(u'file transfer (%s) started: %d item(s), %d bytes' % (direction, items, totalBytes))
        self._fanOut('onFileTransferStarted', direction, items, totalBytes)

    def onFileTransferProgress(self, direction, doneBytes, totalBytes):
        """@summary: clipboard file transfer progress"""
        self._fanOut('onFileTransferProgress', direction, doneBytes, totalBytes)

    def onFileTransferFinished(self, direction, paths, totalBytes, status):
        """@summary: clipboard file transfer finished: status completed / partial / cancelled"""
        log.info(u'file transfer (%s) %s: %d bytes' % (direction, status, totalBytes))
        self._fanOut('onFileTransferFinished', direction, list(paths), totalBytes, status)

    def onDriveFileTransferred(self, direction, path, totalBytes):
        """@summary: the server finished reading ('upload') or writing ('download') a file
        of a redirected drive (called from the drive thread)"""
        log.info(u'drive file %s: %s (%d bytes)' % (direction, path, totalBytes))
        self._fanOut('onDriveFileTransferred', direction, path, totalBytes)

    def onPrintJob(self, printerName, title, totalBytes, destination, error):
        """@summary: a document printed on the server was handed to a local printer
        (error is None) or could not be printed (called from the printer thread)"""
        self._fanOut('onPrintJob', printerName, title, totalBytes, destination, error)

    def isLoggedOn(self):
        """@return: {bool} the server notified that the user is logged on (desktop)"""
        return self._loggedOn

    def onErrorInfo(self, code, message):
        """@summary: Set Error Info from the server (why it ends the session)"""
        self._lastError = (code, message)

    def getLastError(self):
        """@return: {tuple|None} (code, message) of the last Set Error Info PDU"""
        return self._lastError

    def getSessionInfo(self):
        """
        @summary: Status, connection, remote computer, logon, security, features
        and certificate - all from data received while connecting
        @return: {dict} sections of {name: value}
        """
        from time import time
        core = self._mcsLayer._clientSettings.getBlock(gcc.MessageType.CS_CORE)
        report = {}
        report['status'] = {
            'connected': self._isReady,
            'lastError': self._lastError[1] if self._lastError else None,
            'duration': serverinfo.humanDuration(time() - self._connectedAt)
            if self._connectedAt else None,
        }
        report['connection'] = {
            'desktop': '%dx%d' % (core.desktopWidth.value, core.desktopHeight.value),
            'colorDepth': '%d bits' % self._colorDepth,
            'user': self._secLayer._info.userName.value or None,
            'domain': self._secLayer._info.domain.value or None,
            'clientName': self._rdpdrLayer._computerName,
        }
        transport = getattr(self._tpktLayer, 'transport', None)
        challenge = getattr(transport, 'ntlmChallenge', None)
        if challenge:
            report['remote'] = serverinfo.parseChallenge(challenge)
        if self._logonInfo:
            report['logon'] = dict(self._logonInfo)
        security = {'protocol': serverinfo.protocolNames(self._x224Layer._selectedProtocol)}
        try:
            serverCore = self._mcsLayer._serverSettings.getBlock(gcc.MessageType.SC_CORE)
            version = serverCore.rdpVersion.value
            security['rdpVersion'] = serverinfo.RDP_VERSIONS.get(version, hex(version))
        except Exception:
            pass
        report['security'] = security
        gfx = self._drdynvcLayer
        clip = self._cliprdrLayer
        features = {
            'graphics': ('RDPGFX %s' % (serverinfo.RDP_GFX_VERSIONS.get(gfx._gfxVersion, hex(gfx._gfxVersion))
                                        if gfx._gfxVersion else 'not negotiated'))
            if self._gfxEnabled else 'legacy bitmap updates',
            'h264': 'yes (%d decoder(s))' % len(gfx._avcDecoders) if gfx._avcDecoders else 'no',
            'sound': self._soundEnabled,
            'microphone': ('on (%d channel(s) open)' % len(gfx._audinChannels)) if self._microphoneEnabled else False,
            'clipboard': {'off': False, 'text': 'text', 'files': 'text + files'}[self._clipboardMode],
            'dynamicResolution': ('active' if self._disp.isActive() else 'not offered by the server')
            if self._disp.enabled else False,
            'usb': (', '.join(self._urbdrc.describe()) or ('devices plugged in' if self._usbHotplug else None))
            if self._urbdrc.enabled else None,
            'advancedInput': self._ainput.describe(),
            'touch': (self._rdpei.describe() or 'not offered by the server') if self._rdpei.enabled else None,
            'video': (self._video.describe() or 'no video played') if self._video.enabled else None,
            'webAuthn': (self._rdpewa.describe() or 'no sign-in yet') if self._rdpewa.enabled else None,
            'multimedia': (self._tsmf.describe() or 'nothing played') if self._tsmf.enabled else None,
            'geometry': (self._geometry.describe() or 'no mapping') if self._geometry.enabled else None,
            'multiparty': (self._encomspLayer.describe() or 'no participant')
            if any(vc[1] is self._encomspLayer for vc in self._mcsLayer._virtualChannels) else None,
            'location': (self._location.describe() or 'no position sent') if self._location.enabled else None,
            'echo': (self._echo.describe() or 'not used by the server') if self._echo.enabled else None,
            'rdp2tcp': (self._rdp2tcpLayer.describe() or 'not started') if self._rdp2tcpLayer.command else None,
            'sshAgent': (
                    self._sshagent.describe() or 'not requested by the server') if self._sshagent.enabled else None,
            'camera': (self._rdpecam.describe() or 'not requested by the server') if self._rdpecam.enabled else None,
            'remoteAssistance': ('protocol 1.%d, %s' % (self._remdeskLayer.version, remdesk.RESULTS.get(
                self._remdeskLayer.lastResult, 'waiting for the novice') if self._remdeskLayer.lastResult is not None
            else 'waiting for the novice') if self._remdeskLayer.version else 'connecting')
            if self._remoteAssistance is not None else None,
            'remoteApp': ('%s (%s)' % (self._railLayer.app.program,
                                       'started' if self._railLayer.started else 'starting'))
            if self._railMode else False,
            'serverFileTransfer': clip._fileTransferEnabled() if self._clipboardMode == 'files' else None,
        }
        for kind, items in self._rdpdrLayer.describeDevices().items():
            features[kind] = ', '.join(items) or None
        report['features'] = features
        cert = serverinfo.findPeerCertificate(transport)
        if cert is not None:
            report['certificate'] = serverinfo.describeCertificate(cert)
        return report

    def getSessionReport(self):
        """@return: {str} getSessionInfo() as text"""
        return serverinfo.formatReport(self.getSessionInfo())

    def setClipboardCallbacks(self, onRemoteChanged, getLocalText, getLocalFiles=None,
                              onRemoteFiles=None):
        """
        @summary: Wire up clipboard callbacks between UI and CLIPRDR layer
        @param onRemoteChanged: callback(text: str) when server clipboard changes
        @param getLocalText: callback() -> str returning current local clipboard text
        @param getLocalFiles: callback() -> [str] local files copied to the clipboard
        @param onRemoteFiles: callback([str]) local copies of files copied on the server
        """
        self._cliprdrLayer.setRemoteClipboardCallback(onRemoteChanged)
        self._cliprdrLayer.setLocalClipboardGetter(getLocalText)
        self._cliprdrLayer.setLocalFilesGetter(getLocalFiles)
        self._cliprdrLayer.setRemoteFilesCallback(onRemoteFiles)

    def onLocalClipboardChanged(self):
        """
        @summary: Notify the server that the local clipboard has changed
        """
        self._cliprdrLayer.onLocalClipboardChanged()

    def addDrive(self, name, path):
        """
        @summary: Redirect a local directory to the server as a drive (RDPDR)
        must be called before the connection is established
        @param name: {str} drive name shown on the server (e.g. "home")
        @param path: {str} local directory to share
        @return: {int} RDPDR device id
        @raise ValueError: path is not a directory
        """
        return self._rdpdrLayer.addDrive(name, path)

    def addPrinter(self, printerName, driverName=None, isDefault=False):
        """
        @summary: Redirect a local printer (CUPS on Linux / macOS, the spooler on Windows):
        documents printed on the server are printed on it. Call before connecting.
        @param printerName: {str} local printer name, also shown on the server
        @param driverName: {str|None} Windows driver the server renders with (default
                           "MS Publisher Imagesetter", PostScript; "Microsoft Print to PDF")
        @param isDefault: {bool} default printer of the remote session
        @return: {int} RDPDR device id
        """
        return self._rdpdrLayer.addPrinter(printerName, driverName, isDefault)

    def addAllPrinters(self):
        """
        @summary: Redirect every local printer (the local default stays the default)
        @return: {list[int]} RDPDR device ids
        """
        return self._rdpdrLayer.addAllPrinters()

    def addPrintToFolder(self, folder, driverName=None, isDefault=False):
        """
        @summary: A redirected printer that saves the documents in a local folder
        (PDF files with the default driver)
        @return: {int} RDPDR device id
        """
        return self._rdpdrLayer.addPrintToFolder(folder, driverName, isDefault)

    def addSerialPort(self, name, path, driver=None, permissive=False):
        """
        @summary: Redirect a local serial port (needs pyserial). Call before connecting.
        @param name: {str} port name on the server (COM1...)
        @param path: {str} local port (/dev/ttyUSB0, COM3) or a pyserial URL
                     (socket://host:port, rfc2217://host:port)
        @param driver: {str|None} serial driver of the server: Serial, SerCx, SerCx2
        @param permissive: {bool} report unsupported requests as successful
        @return: {int} RDPDR device id
        """
        return self._rdpdrLayer.addSerialPort(name, path, driver, permissive)

    def addParallelPort(self, name, path):
        """
        @summary: Redirect a local parallel port. Call before connecting.
        @param name: {str} port name on the server (LPT1...)
        @param path: {str} local device (/dev/lp0, /dev/usb/lp0) or file
        @return: {int} RDPDR device id
        """
        return self._rdpdrLayer.addParallelPort(name, path)

    def addSmartcard(self, readerFilters=None):
        """
        @summary: Redirect the local smart card readers (needs pyscard and a PC/SC service).
        Call before connecting.
        @param readerFilters: {list[str]|None} only the readers whose name contains one of them
        @return: {int} RDPDR device id
        @raise OSError: pyscard is not installed
        """
        return self._rdpdrLayer.addSmartcard(readerFilters)

    def addUsbDevice(self, device):
        """
        @summary: Redirect local USB devices (MS-RDPEUSB, FreeRDP /usb; needs python-libusb1).
        Call before connecting.
        @param device: {str} "VVVV:PPPP", "id:VVVV:PPPP[#...]", "addr:BUS:ADDRESS[#...]" or
        "auto" (devices plugged in during the session), see urbdrc.parseUsbSpec;
        or a {urbdrc.UsbDeviceBackend}
        @return: {list[str]} the devices found
        @raise ValueError: bad description, no such device
        @raise OSError: python-libusb1 / libusb missing
        """
        if isinstance(device, urbdrc.UsbDeviceBackend):
            self._urbdrc.enabled = True
            return [self._urbdrc.addDevice(device).describe()]
        specs = urbdrc.parseUsbSpec(device)
        from .channels import urbdrc_libusb
        found = []
        if any(s[0] == 'auto' for s in specs):
            self._watchUsbHotplug(urbdrc_libusb)
            found.append('auto')
        devices = urbdrc_libusb.findDevices(specs)
        if not devices and not found:
            raise ValueError(u'no USB device matches \'%s\'' % device)
        known = set((d.backend.bus, d.backend.address) for d in self._urbdrc.devices)
        for backend in devices:
            if (backend.bus, backend.address) not in known:
                found.append(self._urbdrc.addDevice(backend).describe())
        self._urbdrc.enabled = True
        return found

    def _watchUsbHotplug(self, backendModule):
        """FreeRDP /usb:auto: devices plugged in while connected are redirected"""
        if self._usbHotplug is not None:
            return
        ctx = backendModule.LibusbContext.get()

        def arrived(backend):
            if not getattr(backend, 'isHub', False):
                drdynvc.callInReactor(self._urbdrc.addDevice, backend)

        def left(bus, address):
            def remove():
                for device in list(self._urbdrc.devices):
                    if (device.backend.bus, device.backend.address) == (bus, address):
                        log.info(u'USB %s unplugged' % device.describe())
                        self._urbdrc.removeDevice(device.backend)

            drdynvc.callInReactor(remove)

        ctx.watchHotplug(arrived, left)
        self._usbHotplug = ctx

    def _stopUsb(self):
        ctx, self._usbHotplug = self._usbHotplug, None
        if ctx is not None:
            ctx.stopHotplug()
        self._urbdrc.shutdown()

    def addRdpdrDevice(self, device):
        """
        @summary: Register a custom RDPDR device (rdpdr.RdpdrDevice subclass)
        @param device: {rdpdr.RdpdrDevice}
        @return: {int} RDPDR device id
        """
        return self._rdpdrLayer.addDevice(device)

    def getProtocol(self):
        """
        @return: return Protocol layer for twisted
        In case of RDP TPKT is the Raw layer
        """
        return cssp.CSSP(self._tpktLayer,
                         ntlm.NTLMv2(self._secLayer._info.domain.value, self._secLayer._info.userName.value,
                                     self._secLayer._info.password.value))

    def getColorDepth(self):
        """
        @return: color depth set by the server (15, 16, 24)
        """
        return self._pduLayer._serverCapabilities[
            pdu.caps.CapsType.CAPSTYPE_BITMAP].capability.preferredBitsPerPixel.value

    def getKeyEventUniCodeSupport(self):
        """
        @return: True if server support unicode input
        """
        return self._pduLayer._serverCapabilities[
            pdu.caps.CapsType.CAPSTYPE_INPUT].capability.inputFlags.value & pdu.caps.InputFlags.INPUT_FLAG_UNICODE

    def setPerformanceSession(self):
        """
        @summary: Set particular flag in RDP stack to avoid wall-paper, theme, menu animation etc...
        """
        self._secLayer._info.extendedInfo.performanceFlags.value = (
                sec.PerfFlag.PERF_DISABLE_WALLPAPER |
                sec.PerfFlag.PERF_DISABLE_MENUANIMATIONS |
                sec.PerfFlag.PERF_DISABLE_CURSOR_SHADOW |
                (0 if self._railMode else sec.PerfFlag.PERF_DISABLE_FULLWINDOWDRAG) |
                sec.PerfFlag.PERF_ENABLE_FONT_SMOOTHING |
                sec.PerfFlag.PERF_ENABLE_DESKTOP_COMPOSITION
        )

    def setScreen(self, width, height):
        """
        @summary: Set screen dim of session
        @param width: width in pixel of screen
        @param height: height in pixel of screen
        """
        #set screen definition in MCS layer
        self._mcsLayer._clientSettings.getBlock(gcc.MessageType.CS_CORE).desktopHeight.value = height
        self._mcsLayer._clientSettings.getBlock(gcc.MessageType.CS_CORE).desktopWidth.value = width

    def setUsername(self, username):
        """
        @summary: Set the username for session
        @param username: {string} username of session
        """
        #username in PDU info packet
        self._secLayer._info.userName.value = username
        self._secLayer._licenceManager._username = username

    def setPassword(self, password):
        """
        @summary: Set password for session
        @param password: {string} password of session
        """
        self.setAutologon()
        self._secLayer._info.password.value = password

    def setDomain(self, domain):
        """
        @summary: Set the windows domain of session
        @param domain: {string} domain of session
        """
        self._secLayer._info.domain.value = domain

    def setAutologon(self):
        """
        @summary: enable autologon
        """
        self._secLayer._info.flag |= sec.InfoFlag.INFO_AUTOLOGON

    def setAlternateShell(self, appName):
        """
        @summary: set application name of app which start at the begining of session
        @param appName: {string} application name
        """
        self._secLayer._info.alternateShell.value = appName

    def setKeyboardLayout(self, layout):
        """
        @summary: keyboard layout
        @param layout: name of gcc.KeyboardLayout attribute (e.g. US, FRENCH)
        """
        value = getattr(gcc.KeyboardLayout, layout.upper(), None)
        if value is None:
            log.warning(u'Unknown keyboard layout: %s, falling back to US' % layout)
            value = gcc.KeyboardLayout.US
        self._mcsLayer._clientSettings.CS_CORE.kbdLayout.value = value

    def setKeyboardType(self, keyboard_type):
        """
        @summary: keyboard type
        @param keyboard_type: name of gcc.KeyboardType attribute (e.g. IBM_101_102_KEYS)
        """
        value = getattr(gcc.KeyboardType, keyboard_type.upper(), None)
        if value is None:
            log.warning(u'Unknown keyboard type: %s, falling back to IBM_101_102_KEYS' % keyboard_type)
            value = gcc.KeyboardType.IBM_101_102_KEYS
        self._mcsLayer._clientSettings.CS_CORE.keyboardType.value = value

    def setHostname(self, hostname):
        """
        @summary: set hostname of machine
        """
        #        self._mcsLayer._clientSettings.CS_CORE.clientName.value = hostname[:15] + "\x00" * (15 - len(hostname))
        self._secLayer._licenceManager._hostname = hostname
        #name shown by the server for redirected devices ("<drive> on <hostname>")
        self._rdpdrLayer.setComputerName(hostname)

    def setRoutingToken(self, token):
        """
        @summary: Set routing token for server redirection reconnection
        @param token: {bytes} routing token from server redirection PDU
        """
        self._x224Layer.setRoutingToken(token)

    def setSecurityLevel(self, level):
        """
        @summary: Request basic security
        @param level: {SecurityLevel}
        """
        if level == SecurityLevel.RDP_LEVEL_RDP:
            self._x224Layer._requestedProtocol = x224.Protocols.PROTOCOL_RDP
        elif level == SecurityLevel.RDP_LEVEL_SSL:
            self._x224Layer._requestedProtocol = x224.Protocols.PROTOCOL_SSL
        elif level == SecurityLevel.RDP_LEVEL_NLA:
            self._x224Layer._requestedProtocol = x224.Protocols.PROTOCOL_SSL | x224.Protocols.PROTOCOL_HYBRID

    def setSecurity(self, settings, host, port, requestedProtocols):
        """
        @summary: FreeRDP-like security (rdpSettings NlaSecurity, TlsSecurity,
        RdpSecurity, IgnoreCertificate)
        @param settings: {security.SecuritySettings}
        @param host: {str} server name, checked against its certificate
        @param port: {int}
        @param requestedProtocols: {int} this attempt (SecuritySettings.attempts)
        """
        from . import security
        self._x224Layer._securitySettings = settings
        self._x224Layer._requestedProtocol = requestedProtocols
        self._x224Layer._certificateVerifier = security.CertificateVerifier(host, port, settings.certificate)

    def getCertificateVerifier(self):
        """@return: {security.CertificateVerifier | None} (rejection, certificate, trustCurrent())"""
        return self._x224Layer._certificateVerifier

    def getProtocolException(self):
        """@return: {Exception | None} the error that ended the connection on the client side"""
        return getattr(self._tpktLayer, 'protocolException', None)

    def setDecoderBrokenCallback(self, callback):
        """Set callback invoked when the H.264 decoder cannot recover.

        When the decoder hard-resets but the server still does not send an IDR
        within the reconnect timeout, this callback is fired.  The application
        layer should trigger a full RDP reconnect in response.
        """
        self._drdynvcLayer.setDecoderBrokenCallback(callback)

    def setAVC444Disabled(self, disabled):
        """
        @summary: Disable AVC444 / AVC444v2 support in RDPGFX
        @param disabled: {bool}
        """
        self._drdynvcLayer.setAVC444Disabled(disabled)

    def disableAVC444(self):
        """
        @summary: Disable AVC444 / AVC444v2 support in RDPGFX
        """
        self._drdynvcLayer.setAVC444Disabled(True)

    def addClientObserver(self, observer):
        """
        @summary: Add observer to RDP protocol
        @param observer: new observer to add
        """
        self._clientObserver.append(observer)

    def removeClientObserver(self, observer):
        """
        @summary: Remove observer to RDP protocol stack
        @param observer: observer to remove
        """
        for i in range(0, len(self._clientObserver)):
            if self._clientObserver[i] == observer:
                del self._clientObserver[i]
                return

    def onUpdate(self, rectangles):
        """
        @summary: Call when a bitmap data is received from update PDU
        @param rectangles: [pdu.BitmapData] struct
        """
        for observer in self._clientObserver:
            #for each rectangle in update PDU
            for rectangle in rectangles:
                observer.onUpdate(rectangle.destLeft.value, rectangle.destTop.value, rectangle.destRight.value,
                                  rectangle.destBottom.value, rectangle.width.value, rectangle.height.value,
                                  rectangle.bitsPerPixel.value,
                                  rectangle.flags.value & pdu.data.BitmapFlag.BITMAP_COMPRESSION,
                                  rectangle.bitmapDataStream.value)

    def _onGfxBitmap(self, destLeft, destTop, destRight, destBottom, width, height, bpp, isCompress, data):
        """Callback from DrdynvcLayer for RDPGFX bitmap delivery."""
        for observer in self._clientObserver:
            observer.onUpdate(destLeft, destTop, destRight, destBottom,
                              width, height, bpp, isCompress, data)

    def _onGfxPresent(self, items):
        """Callback from DrdynvcLayer: one presented RDPGFX frame.
        @param items: [(x, y, width, height, bgra)] bgra: (height, width*4) uint8 array
        """
        for observer in self._clientObserver:
            present = getattr(observer, 'onGfxPresent', None)
            if present is not None:
                present(items)
            else:
                for x, y, w, h, region in items:
                    observer.onUpdate(x, y, x + w - 1, y + h - 1, w, h, 32, 'gfx', region.tobytes())

    def _onGfxSurface(self, dest_x, dest_y, width, height, surf_buf, surf_w, src_left, src_top):
        """Fast-path callback: deliver AVC surface buffer region to observers."""
        for observer in self._clientObserver:
            observer.onGfxSurface(dest_x, dest_y, width, height,
                                  surf_buf, surf_w, src_left, src_top)

    def onPointerHide(self):
        """
        @summary: Call when server requests the pointer to be hidden
        """
        for observer in self._clientObserver:
            observer.onPointerHide()

    def onPointerDefault(self):
        """
        @summary: Call when server requests the default system pointer
        """
        for observer in self._clientObserver:
            observer.onPointerDefault()

    def onPointerCached(self, cacheIndex):
        """
        @summary: Call when server switches to a previously cached pointer
        @param cacheIndex: index of cached pointer
        """
        for observer in self._clientObserver:
            observer.onPointerCached(cacheIndex)

    def onPointerUpdate(self, xorBpp, cacheIndex, hotSpotX, hotSpotY, width, height, andMask, xorMask):
        """
        @summary: Call when server sends a new pointer shape
        @param xorBpp: bits per pixel of XOR mask
        @param cacheIndex: cache index to store this pointer
        @param hotSpotX: hotspot X coordinate
        @param hotSpotY: hotspot Y coordinate
        @param width: pointer width
        @param height: pointer height
        @param andMask: AND mask data
        @param xorMask: XOR mask data
        """
        for observer in self._clientObserver:
            observer.onPointerUpdate(xorBpp, cacheIndex, hotSpotX, hotSpotY, width, height, andMask, xorMask)

    def onReady(self):
        """
        @summary: Call when PDU layer is connected
        """
        self._isReady = True
        from time import time
        self._connectedAt = time()
        size = self.getServerDesktopSize()
        if size:
            self._railLayer.desktopSize = size
        from twisted.internet import reactor
        if self._gfxEnabled:
            # Start GFX input gating: suppress input until CAPS_CONFIRM or timeout
            self._gfxPending = True
            self._gfxTimer = reactor.callLater(5, self._onGfxTimeout)
        log.debug(u'RDPClientController: GFX input gating active (5s timeout)')
        #signal all listener
        for observer in self._clientObserver:
            observer.onReady()

    def _onGfxCapsConfirm(self):
        """Called by DrdynvcLayer when RDPGFX CAPS_CONFIRM is received."""
        log.debug(u'RDPClientController: GFX CAPS_CONFIRM received, enabling input')
        self._gfxPending = False
        if self._gfxTimer and self._gfxTimer.active():
            self._gfxTimer.cancel()
            self._gfxTimer = None

    def _onGfxTimeout(self):
        """Fallback: enable input after timeout even without CAPS_CONFIRM."""
        log.debug(u'RDPClientController: GFX timeout, enabling input')
        self._gfxPending = False
        self._gfxTimer = None

    def onSessionReady(self):
        """
        @summary: Call when Windows session is ready (connected)
        """
        self._isReady = True
        #signal all listener
        for observer in self._clientObserver:
            observer.onSessionReady()

    def onClose(self):
        """
        @summary: Event call when RDP stack is closed
        """
        self._isReady = False
        self._gfxPending = False
        self._pduLayer.stopSurfaceWorker()
        if self._gfxTimer and self._gfxTimer.active():
            self._gfxTimer.cancel()
            self._gfxTimer = None
        self._rdpsndLayer._stopAudio()
        self._disp.shutdown()
        self._railLayer.close()
        self._stopUsb()
        self._drdynvcLayer.shutdown()
        self._rdpdrLayer.shutdown()
        for observer in self._clientObserver:
            observer.onClose()

    def onRedirect(self, redirPDU):
        """
        @summary: Called when server sends a redirection PDU (e.g., GNOME Remote Desktop).
        Extracts routing token and closes connection to trigger redirect reconnection.
        @param redirPDU: ServerRedirectionPDU object
        """
        log.debug(u'Server redirection received, flags=0x%x' % redirPDU.redirFlags.value)
        lbInfo = redirPDU.getLoadBalanceInfo()
        if lbInfo:
            log.debug(u'Load balance info: %s' % repr(lbInfo))
            self._redirectRoutingToken = lbInfo
            self.close()
        else:
            log.warning(u'Server redirection PDU received but no load balance info found')

    def sendPointerEvent(self, x, y, button, isPressed):
        """
        @summary: send pointer events
        @param x: x position of pointer
        @param y: y position of pointer
        @param button: 1 or 2 or 3
        @param isPressed: true if button is pressed or false if it's released
        """
        if not self._isReady or self._gfxPending:
            return
        # FreeRDP freerdp_client_send_button_event: the advanced input channel first
        if self._ainput.sendPointer(x, y, button, isPressed):
            return
        try:
            if button == 4 or button == 5:
                event = pdu.data.PointerExEvent()
                if isPressed:
                    event.pointerFlags.value |= pdu.data.PointerExFlag.PTRXFLAGS_DOWN
                if button == 4:
                    event.pointerFlags.value |= pdu.data.PointerExFlag.PTRXFLAGS_BUTTON1
                elif button == 5:
                    event.pointerFlags.value |= pdu.data.PointerExFlag.PTRXFLAGS_BUTTON2
            else:
                event = pdu.data.PointerEvent()
                if isPressed:
                    event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_DOWN
                if button == 1:
                    event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_BUTTON1
                elif button == 2:
                    event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_BUTTON2
                elif button == 3:
                    event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_BUTTON3
                else:
                    event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_MOVE
            # position
            event.xPos.value = x
            event.yPos.value = y
            # send proper event
            self._pduLayer.sendInputEvents([event])

        except InvalidValue:
            log.debug(u'try send pointer event with incorrect position')

    def sendWheelEvent(self, x, y, scroll, isHorizontal=False):
        """
        @summary: Send a mouse wheel event
        @param x: x position of pointer
        @param y: y position of pointer
        @param scroll: signed scroll value (positive=up/right, negative=down/left)
        @param isHorizontal: horizontal wheel (default is vertical)
        """
        if not self._isReady or self._gfxPending:
            return
        # FreeRDP freerdp_client_send_wheel_event: the advanced input channel first
        if self._ainput.sendWheel(scroll, isHorizontal):
            return

        try:
            event = pdu.data.PointerEvent()
            if isHorizontal:
                event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_HWHEEL
            else:
                event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_WHEEL
            if scroll < 0:
                event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_WHEEL_NEGATIVE
            # Match grdp: uint8 cast of signed value gives 256-complement for negatives
            # e.g. scroll=-1 → ts=255 (0xFF), scroll=1 → ts=1
            ts = scroll & 0xFF
            event.pointerFlags.value |= (pdu.data.PointerFlag.WheelRotationMask & ts)
            #position
            event.xPos.value = x
            event.yPos.value = y
            #send proper event
            self._pduLayer.sendInputEvents([event])

        except InvalidValue:
            log.debug(u'try send wheel event with incorrect position')

    def sendKeyEventScancode(self, code, isPressed, extended=False):
        """
        @summary: Send a scan code to RDP stack
        @param code: scan code
        @param isPressed: True if key is pressed and false if it's released
        @param extended: {boolean} extended scancode like ctr or win button
        """
        if not self._isReady or self._gfxPending:
            return

        try:
            event = pdu.data.ScancodeKeyEvent()
            event.keyCode.value = code
            if not isPressed:
                event.keyboardFlags.value |= pdu.data.KeyboardFlag.KBDFLAGS_RELEASE

            if extended:
                event.keyboardFlags.value |= pdu.data.KeyboardFlag.KBDFLAGS_EXTENDED

            #send event
            self._pduLayer.sendInputEvents([event])

        except InvalidValue:
            log.debug(u'try send bad key event')

    def sendKeyEventUnicode(self, code, isPressed):
        """
        @summary: Send a scan code to RDP stack
        @param code: unicode
        @param isPressed: True if key is pressed and false if it's released
        """
        if not self._isReady or self._gfxPending:
            return

        try:
            event = pdu.data.UnicodeKeyEvent()
            event.unicode.value = code
            if not isPressed:
                event.keyboardFlags.value |= pdu.data.KeyboardFlag.KBDFLAGS_RELEASE

            #send event
            self._pduLayer.sendInputEvents([event])

        except InvalidValue:
            log.debug(u'try send bad key event')

    def sendRefreshOrder(self, left, top, right, bottom):
        """
        @summary: Force server to resend a particular zone
        @param left: left coordinate
        @param top: top coordinate
        @param right: right coordinate
        @param bottom: bottom coordinate
        """
        refreshPDU = pdu.data.RefreshRectPDU()
        rect = pdu.data.InclusiveRectangle()
        rect.left.value = left
        rect.top.value = top
        rect.right.value = right
        rect.bottom.value = bottom
        refreshPDU.areasToRefresh._array.append(rect)
        self._pduLayer.sendDataPDU(refreshPDU)

    def close(self):
        """
        @summary: Close protocol stack
        """
        from traceback import format_stack
        log.debug(u'RDPClientController.close() called from:\n%s' %
                  u''.join(format_stack()))
        if getattr(self, '_closing', False):
            return
        self._closing = True
        wasActive = self._isReady
        # FreeRDP freerdp_disconnect: leave the active state first so no input
        # or channel data is sent any more, release the local resources (audio
        # devices, decoder and drive threads), then send the Disconnect
        # Provider Ultimatum and close the transport.
        self._isReady = False
        try:
            self._rdpsndLayer._stopAudio()
            self._disp.shutdown()
            self._stopUsb()
            self._rdpecam.shutdown()
            self._rdp2tcpLayer.close()
            self._video.shutdown()
            self._tsmf.shutdown()
            self._drdynvcLayer.shutdown()
            self._rdpdrLayer.shutdown()
        except Exception as e:
            log.debug(u'RDPClientController.close(): %s' % e)
        if self._tpktLayer.transport is None:
            return  # not connected (yet)
        if not wasActive:
            # still connecting: no MCS domain to leave, just close the socket
            self._tpktLayer.close(graceful=False)
            return
        try:
            self._pduLayer.close()
        except Exception as e:
            # whatever happens, the socket must not stay open
            log.debug(u'RDPClientController.close(): disconnect PDU not sent: %s' % e)
            self._tpktLayer.abort()

    def isConnected(self):
        """@return: {bool} the TCP connection of this session is open"""
        tcp = layer.bottomTransport(getattr(self._tpktLayer, 'transport', None))
        return bool(tcp is not None and getattr(tcp, 'connected', False))

    @classmethod
    def openSessions(cls):
        """@return: {list} controllers whose connection is still open"""
        return [c for c in list(cls._instances) if c.isConnected()]

    def abort(self, reason):
        """
        @summary: Drop the connection immediately (timeout, fatal error)
        @param reason: {str} shown to the user as the cause
        """
        self._tpktLayer.protocolError = reason
        self._isReady = False
        self._tpktLayer.abort()

    def getProtocolError(self):
        """@return: {str|None} why the client ended the connection (error, timeout)"""
        return getattr(self._tpktLayer, 'protocolError', None)


class RDPServerController(pdu.layer.PDUServerListener):
    """
    @summary: Controller use in server side mode
    """

    def __init__(self, colorDepth, privateKeyFileName=None, certificateFileName=None):
        """
        @param privateKeyFileName: file contain server private key
        @param certficiateFileName: file that contain public key
        @param colorDepth: 15, 16, 24
        """
        self._isReady = False
        #list of observer
        self._serverObserver = []
        #build RDP protocol stack
        self._pduLayer = pdu.layer.Server(self)
        #secure layer
        self._secLayer = sec.Server(self._pduLayer)
        #multichannel service
        self._mcsLayer = mcs.Server(self._secLayer)
        #transport pdu layer
        self._x224Layer = x224.Server(self._mcsLayer, privateKeyFileName, certificateFileName, False)
        #transport packet (protocol layer)
        self._tpktLayer = tpkt.TPKT(self._x224Layer)
        #fastpath stack
        self._pduLayer.initFastPath(self._secLayer)
        self._secLayer.initFastPath(self._tpktLayer)
        #set color depth of session
        self.setColorDepth(colorDepth)

    def close(self):
        """
        @summary: Close protocol stack
        """
        self._pduLayer.close()

    def getProtocol(self):
        """
        @return: the twisted protocol layer
        in RDP case is TPKT layer
        """
        return self._tpktLayer

    def getHostname(self):
        """
        @return: name of client (information done by RDP)
        """
        return self._mcsLayer._clientSettings.CS_CORE.clientName.value.decode(u'utf-8').strip(u'\x00')

    def getUsername(self):
        """
        @summary: Must be call after on ready event else always empty string
        @return: username send by client may be an empty string
        """
        return self._secLayer._info.userName.value

    def getPassword(self):
        """
        @summary: Must be call after on ready event else always empty string
        @return: password send by client may be an empty string
        """
        return self._secLayer._info.password.value

    def getDomain(self):
        """
        @summary: Must be call after on ready event else always empty string
        @return: domain send by client may be an empty string
        """
        return self._secLayer._info.domain.value

    def getCredentials(self):
        """
        @summary: Must be call after on ready event else always empty string
        @return: tuple(domain, username, password)
        """
        return (self.getDomain(), self.getUsername(), self.getPassword())

    def getColorDepth(self):
        """
        @return: color depth define by server
        """
        return self._colorDepth

    def getScreen(self):
        """
        @return: tuple(width, height) of client asked screen
        """
        bitmapCap = self._pduLayer._clientCapabilities[pdu.caps.CapsType.CAPSTYPE_BITMAP].capability
        return bitmapCap.desktopWidth.value, bitmapCap.desktopHeight.value

    def addServerObserver(self, observer):
        """
        @summary: Add observer to RDP protocol
        @param observer: new observer to add
        """
        self._serverObserver.append(observer)

    def setColorDepth(self, colorDepth):
        """
        @summary:  Set color depth of session
                    if PDU stack is already connected send a deactive-reactive sequence
                    and an onReady message is re send when client is ready
        @param colorDepth: {integer} depth of session (15, 16, 24)
        """
        self._colorDepth = colorDepth
        self._pduLayer._serverCapabilities[
            pdu.caps.CapsType.CAPSTYPE_BITMAP].capability.preferredBitsPerPixel.value = colorDepth
        if self._isReady:
            #restart connection sequence
            self._isReady = False
            self._pduLayer.sendPDU(pdu.data.DeactiveAllPDU())

    def setKeyEventUnicodeSupport(self):
        """
        @summary: Enable key event in unicode format
        """
        self._pduLayer._serverCapabilities[
            pdu.caps.CapsType.CAPSTYPE_INPUT].capability.inputFlags.value |= pdu.caps.InputFlags.INPUT_FLAG_UNICODE

    def onReady(self):
        """
        @summary: RDP stack is now ready
        """
        self._isReady = True
        for observer in self._serverObserver:
            observer.onReady()

    def onClose(self):
        """
        @summary: Event call when RDP stack is closed
        """
        self._isReady = False
        for observer in self._serverObserver:
            observer.onClose()

    def onSlowPathInput(self, slowPathInputEvents):
        """
        @summary: Event call when slow path input are available
        @param slowPathInputEvents: [data.SlowPathInputEvent]
        """
        for observer in self._serverObserver:
            for event in slowPathInputEvents:
                #scan code
                if event.messageType.value == pdu.data.InputMessageType.INPUT_EVENT_SCANCODE:
                    observer.onKeyEventScancode(event.slowPathInputData.keyCode.value, not (
                            event.slowPathInputData.keyboardFlags.value & pdu.data.KeyboardFlag.KBDFLAGS_RELEASE),
                                                bool(
                                                    event.slowPathInputData.keyboardFlags.value & pdu.data.KeyboardFlag.KBDFLAGS_EXTENDED))
                #unicode
                elif event.messageType.value == pdu.data.InputMessageType.INPUT_EVENT_UNICODE:
                    observer.onKeyEventUnicode(event.slowPathInputData.unicode.value, not (
                            event.slowPathInputData.keyboardFlags.value & pdu.data.KeyboardFlag.KBDFLAGS_RELEASE))
                #mouse events
                elif event.messageType.value == pdu.data.InputMessageType.INPUT_EVENT_MOUSE:
                    isPressed = event.slowPathInputData.pointerFlags.value & pdu.data.PointerFlag.PTRFLAGS_DOWN
                    button = 0
                    if event.slowPathInputData.pointerFlags.value & pdu.data.PointerFlag.PTRFLAGS_BUTTON1:
                        button = 1
                    elif event.slowPathInputData.pointerFlags.value & pdu.data.PointerFlag.PTRFLAGS_BUTTON2:
                        button = 2
                    elif event.slowPathInputData.pointerFlags.value & pdu.data.PointerFlag.PTRFLAGS_BUTTON3:
                        button = 3
                    observer.onPointerEvent(event.slowPathInputData.xPos.value, event.slowPathInputData.yPos.value,
                                            button, isPressed)
                elif event.messageType.value == pdu.data.InputMessageType.INPUT_EVENT_MOUSEX:
                    isPressed = event.slowPathInputData.pointerFlags.value & pdu.data.PointerExFlag.PTRXFLAGS_DOWN
                    button = 0
                    if event.slowPathInputData.pointerFlags.value & pdu.data.PointerExFlag.PTRXFLAGS_BUTTON1:
                        button = 4
                    elif event.slowPathInputData.pointerFlags.value & pdu.data.PointerExFlag.PTRXFLAGS_BUTTON2:
                        button = 5
                    observer.onPointerEvent(event.slowPathInputData.xPos.value, event.slowPathInputData.yPos.value,
                                            button, isPressed)

    def sendUpdate(self, destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, isCompress, data):
        """
        @summary: send bitmap update
        @param destLeft: xmin position
        @param destTop: ymin position
        @param destRight: xmax position because RDP can send bitmap with padding
        @param destBottom: ymax position because RDP can send bitmap with padding
        @param width: width of bitmap
        @param height: height of bitmap
        @param bitsPerPixel: number of bit per pixel
        @param isCompress: use RLE compression
        @param data: bitmap data
        """
        if not self._isReady:
            return
        bitmapData = pdu.data.BitmapData(destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, data)
        if isCompress:
            bitmapData.flags.value = pdu.data.BitmapFlag.BITMAP_COMPRESSION

        self._pduLayer.sendBitmapUpdatePDU([bitmapData])


class ClientFactory(layer.RawLayerClientFactory):
    """
    @summary: Factory of Client RDP protocol
    @param reason: twisted reason
    """
    _redirectRoutingToken = None

    def connectionLost(self, csspLayer, reason):
        #retrieve controller
        tpktLayer = csspLayer._layer
        x224Layer = tpktLayer._presentation
        mcsLayer = x224Layer._presentation
        secLayer = mcsLayer._channels[mcs.Channel.MCS_GLOBAL_CHANNEL]
        pduLayer = secLayer._presentation
        controller = pduLayer._listener
        #save redirect routing token before closing
        if controller._redirectRoutingToken:
            self._redirectRoutingToken = controller._redirectRoutingToken
        controller.onClose()

    def buildRawLayer(self, addr):
        """
        @summary: Function call from twisted and build rdp protocol stack
        @param addr: destination address
        """
        controller = RDPClientController()
        self.buildObserver(controller, addr)
        return controller.getProtocol()

    def buildObserver(self, controller, addr):
        """
        @summary: Build observer use for connection
        @param controller: RDPClientController
        @param addr: destination address
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'buildObserver', u'ClientFactory'))


class ServerFactory(layer.RawLayerServerFactory):
    """
    @summary: Factory of Server RDP protocol
    """

    def __init__(self, colorDepth, privateKeyFileName=None, certificateFileName=None):
        """
        @param colorDepth: color depth of session
        @param privateKeyFileName: file contain server private key (if none -> back to standard RDP security)
        @param certficiateFileName: file that contain public key (if none -> back to standard RDP security)
        """
        self._colorDepth = colorDepth
        self._privateKeyFileName = privateKeyFileName
        self._certificateFileName = certificateFileName

    def connectionLost(self, tpktLayer, reason):
        """
        @param reason: twisted reason
        """
        #retrieve controller
        x224Layer = tpktLayer._presentation
        mcsLayer = x224Layer._presentation
        secLayer = mcsLayer._channels[mcs.Channel.MCS_GLOBAL_CHANNEL]
        pduLayer = secLayer._presentation
        controller = pduLayer._listener
        controller.onClose()

    def buildRawLayer(self, addr):
        """
        @summary: Function call from twisted and build rdp protocol stack
        @param addr: destination address
        """
        controller = RDPServerController(self._colorDepth, self._privateKeyFileName, self._certificateFileName)
        self.buildObserver(controller, addr)
        return controller.getProtocol()

    def buildObserver(self, controller, addr):
        """
        @summary: Build observer use for connection
        @param controller: RDP stack controller
        @param addr: destination address
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'buildObserver', u'ServerFactory'))


class RDPClientObserver(object):
    """
    @summary: Class use to inform all RDP event handle by RDPY
    """

    def __init__(self, controller):
        """
        @param controller: RDP controller use to interact with protocol
        """
        self._controller = controller
        self._controller.addClientObserver(self)

    def onReady(self):
        """
        @summary: Stack is ready and connected
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'onReady', u'RDPClientObserver'))

    def onSessionReady(self):
        """
        @summary: Windows session is ready
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'onSessionReady', u'RDPClientObserver'))

    def onLoginScreen(self):
        """
        @summary: The first picture is shown before the user is logged on:
        the Windows logon (or welcome) screen. Optional.
        """

    def onDesktopReady(self):
        """
        @summary: The server notified that the user is logged on: the desktop
        is starting (Save Session Info logon notification). Optional.
        """

    def onFileTransferStarted(self, direction, items, totalBytes):
        """
        @summary: Clipboard file transfer started. Optional.
        @param direction: {str} 'upload' (local files pasted on the server) or 'download'
        @param items: {int} files and folders
        @param totalBytes: {int}
        """

    def onFileTransferProgress(self, direction, doneBytes, totalBytes):
        """
        @summary: Clipboard file transfer progress. Optional.
        """

    def onFileTransferFinished(self, direction, paths, totalBytes, status):
        """
        @summary: Clipboard file transfer finished. Optional.
        @param paths: {list} local paths (sent files, or the downloaded copies)
        @param status: {str} 'completed', 'partial' or 'cancelled'
        """

    def onDriveFileTransferred(self, direction, path, totalBytes):
        """
        @summary: The server closed a file of a redirected drive after reading it
        ('upload') or writing it ('download'). Optional; drive thread.
        """

    def onPrintJob(self, printerName, title, totalBytes, destination, error):
        """
        @summary: A document printed on the server went to a redirected printer.
        Optional; printer thread.
        @param destination: {str|None} where it went (CUPS job, file...)
        @param error: {str|None} why it could not be printed
        """

    def onLogonError(self, errorType, errorData, message):
        """
        @summary: The server reported a logon error / warning. Optional.
        @param errorType: {int} LOGON_MSG_* (MS-RDPBCGR 2.2.10.1.1.4.1.1)
        @param errorData: {int} LOGON_FAILED_* / LOGON_WARNING
        @param message: {str} readable text
        """

    def onClose(self):
        """
        @summary: Stack is closes
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'onClose', u'RDPClientObserver'))

    def onUpdate(self, destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, isCompress, data):
        """
        @summary: Notify bitmap update
        @param destLeft: xmin position
        @param destTop: ymin position
        @param destRight: xmax position because RDP can send bitmap with padding
        @param destBottom: ymax position because RDP can send bitmap with padding
        @param width: width of bitmap
        @param height: height of bitmap
        @param bitsPerPixel: number of bit per pixel
        @param isCompress: use RLE compression
        @param data: bitmap data
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'onUpdate', u'RDPClientObserver'))

    def onGfxSurface(self, dest_x, dest_y, width, height, surf_buf, surf_w, src_left, src_top):
        """Fast-path AVC delivery: a region of *surf_buf* (bytearray) has been
        updated.  The pixel at column *src_left*, row *src_top* is at byte
        offset ``(src_top * surf_w + src_left) * 4``; row stride is
        ``surf_w * 4`` bytes.  Override to create a QImage with stride instead
        of calling onUpdate with bytes — avoids the tobytes() copy.
        """
        # Default: no-op; subclasses override for zero-copy delivery

    def onPointerHide(self):
        """
        @summary: Called when the server hides the pointer
        """

    def onPointerDefault(self):
        """
        @summary: Called when the server requests the default system pointer
        """

    def onPointerCached(self, cacheIndex):
        """
        @summary: Called when the server switches to a cached pointer
        @param cacheIndex: index of cached pointer
        """

    def onPointerUpdate(self, xorBpp, cacheIndex, hotSpotX, hotSpotY, width, height, andMask, xorMask):
        """
        @summary: Called when the server sends a new pointer shape
        @param xorBpp: bits per pixel of XOR mask
        @param cacheIndex: cache index to store this pointer
        @param hotSpotX: hotspot X coordinate
        @param hotSpotY: hotspot Y coordinate
        @param width: pointer width
        @param height: pointer height
        @param andMask: AND mask data
        @param xorMask: XOR mask data
        """


class RDPServerObserver(object):
    """
    @summary: Class use to inform all RDP event handle by RDPY
    """

    def __init__(self, controller):
        """
        @param controller: RDP controller use to interact with protocol
        """
        self._controller = controller
        self._controller.addServerObserver(self)

    def onReady(self):
        """
        @summary: Stack is ready and connected
        May be called after an setColorDepth too
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'onReady', u'RDPServerObserver'))

    def onClose(self):
        """
        @summary: Stack is closes
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'onClose', u'RDPClientObserver'))

    def onKeyEventScancode(self, code, isPressed, isExtended):
        """
        @summary: Event call when a keyboard event is catch in scan code format
        @param code: {integer} scan code of key
        @param isPressed: {boolean} True if key is down
        @param isExtended: {boolean} True if a special key
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'onKeyEventScanCode', u'RDPServerObserver'))

    def onKeyEventUnicode(self, code, isPressed):
        """
        @summary: Event call when a keyboard event is catch in unicode format
        @param code: Unicode of key
        @param isPressed: True if key is down
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'onKeyEventUnicode', u'RDPServerObserver'))

    def onPointerEvent(self, x, y, button, isPressed):
        """
        @summary: Event call on mouse event
        @param x: x position
        @param y: y position
        @param button: 1, 2, 3, 4 or 5 button
        @param isPressed: True if mouse button is pressed
        """
        raise CallPureVirtualFuntion(
            u'%s:%s defined by interface %s' % (self.__class__, u'onPointerEvent', u'RDPServerObserver'))
