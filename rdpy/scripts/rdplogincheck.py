# coding=utf-8
"""
Headless RDP check (no UI, no Qt): does the RDP server let this user reach the desktop?
    python scripts/rdplogincheck.py -u alice -p secret 192.168.1.20
    python scripts/rdplogincheck.py -u alice -d CORP --security tls --cert ignore host:3389
    RDP_PASSWORD=secret python scripts/rdplogincheck.py -u alice --json host
What it does:
  1. connects (NLA -> TLS -> RDP fallback, like the GUI client) and sends the credentials;
  2. watches the session: first picture = Windows login (or lock / welcome) screen,
     server "logon" notification (Save Session Info) = the user is logged on, the desktop starts;
  3. if the desktop is not reached by itself (the session stays on the login screen) it presses
     ENTER (scan code 0x1C) after --enter-delay seconds, then every --enter-interval seconds,
     at most --max-enters times, until the server confirms the logon;
  4. optionally saves PNG screenshots of the login screen and of the desktop (--screenshots DIR,
     --shot-login FILE, --shot-desktop FILE; needs NumPy and Pillow, both rdpy dependencies);
  5. optionally (--bpp N) asks the server for a 15, 16, 24 or 32 bits per pixel session;
  6. optionally (--info) prints rdpy's own session report (controller.getSessionInfo() /
     serverinfo): Windows name (Windows 7, 10, 11, Server 2019...), version and build, computer
     name, domain, RDP version, security, certificate, logon user and session id, features, color
     depth. The Windows name comes from NLA; without NLA only a coarse hint is possible.
     (--probe-multi-session also opens a 2nd session to find out whether several sessions of the
     same account can run at once, which also tells a Windows Server from a client Windows);
  7. prints a report and exits.
Exit code: 0 desktop reached | 1 logon error (bad password, account locked...) |
           2 desktop not confirmed before --timeout | 3 connection failed / lost | 4 bad arguments
"""
from __future__ import absolute_import, division, print_function
from os.path import abspath, dirname, isdir, join
from argparse import ArgumentParser
from sys import exit, stderr, path
from os import environ, makedirs
from time import strftime, time
from socket import gethostname
from getpass import getpass
from json import dumps
from copy import copy
from re import sub

try:
    from importlib.util import find_spec
except:
    from pkgutil import find_loader


    def find_spec(name):
        return find_loader(name)

path.insert(0, dirname(dirname(abspath(__file__))))  # run from a checkout without installing rdpy

from rdpy.protocol.rdp import rdp, security, serverinfo  # noqa: E402
from rdpy.core.error import RDPSecurityNegoFail  # noqa: E402
from rdpy.protocol.rdp.pdu import caps  # noqa: E402
from twisted.internet import reactor  # noqa: E402
from rdpy.core import log  # noqa: E402

SCANCODE_ENTER = 0x1C
EXIT_DESKTOP, EXIT_LOGON_ERROR, EXIT_NOT_CONFIRMED, EXIT_CONNECTION, EXIT_ARGS = 0, 1, 2, 3, 4


def constantName(owner, prefix, value):
    """@return: {str|None} name of the rdpy constant of owner that holds value (without its prefix)."""
    for name, number in vars(owner).items():
        if name.startswith(prefix) and number == value:
            return name[len(prefix):].replace(u'_', u' ').lower()
    return None


def serverOsType(controller):
    """
    @return: {str|None} the operating system family announced in the General Capability Set of the
    server (names of rdpy's caps.MajorType / caps.MinorType), None when the server does not say.
    """
    cap = controller._pduLayer._serverCapabilities.get(caps.CapsType.CAPSTYPE_GENERAL)
    if cap is None:
        return None
    major, minor = cap.capability.osMajorType.value, cap.capability.osMinorType.value
    if not major and not minor:
        return None
    return u'%s (%s)' % (constantName(caps.MajorType, u'OSMAJORTYPE_', major) or major,
                         constantName(caps.MinorType, u'OSMINORTYPE_', minor) or minor)


def describeWindows(info, osType=None, multiSession=None):
    """
    @summary: Which Windows runs on the server (Windows 7, 10, 11, Server 2019...).
    The exact name comes from the build sent in the NTLM challenge of NLA: rdpy already turns it into
    a name (serverinfo.windowsName -> getSessionInfo()['remote']['osName']). Client and server share
    a build from Windows 10 on ("Windows Server 2019 / Windows 10 1809"): when the multiple-session
    probe found several sessions at once, only the server edition is kept. Without NLA the server only
    tells its RDP version, which gives a coarse hint, said as such.
    @return: {str|None}
    """
    remote, version = info.get(u'remote', {}), info.get(u'security', {}).get(u'rdpVersion', u'')
    if remote.get(u'osName'):
        names = remote[u'osName'].split(u' / ')
        if multiSession == u'yes':
            names = [n for n in names if u'Server' in n] or names
        return u' / '.join(names)
    if osType and not osType.startswith(u'windows'):
        return u'not Windows (%s)' % osType
    hints = {u'5.0 - 8.1': u'Windows 2000 ... 8.1 / Server 2003 ... 2012 R2'}
    hint = hints.get(version) or (
        u'Windows 10 / 11 or Windows Server 2016 or newer' if version.startswith(u'10.') else None)
    return u'unknown, the exact name needs NLA (RDP %s server: %s)' % (version, hint) if hint else None


def mergeInfo(old, new):
    """
    Sections of rdpy's getSessionInfo(): the transport is gone by the end of the session (certificate,
    NTLM challenge), so keep what was seen earlier; newer non-empty values win.
    """
    for section, values in new.items():
        target = old.setdefault(section, {})
        for key, value in values.items():
            if value not in (None, u'', [], {}):
                target[key] = value
    return old


def bitmapToRgb(width, height, bitsPerPixel, isCompress, data):
    """
    @summary: One RDP bitmap rectangle -> RGB pixels (same decoding rules as the Qt client)
    @return: {numpy.ndarray|None} (height, width, 3) uint8, top-down; None when the data is unusable
    """
    from numpy import frombuffer, stack, uint8, uint16
    if width <= 0 or height <= 0:
        return None
    if hasattr(data, u'encode'):
        data = data.encode(u'latin-1')
    data = bytes(data)
    if isCompress == u'gfx' and bitsPerPixel == 32:  # RDPGFX: already top-down BGRA
        need = width * height * 4
        if len(data) < need:
            return None
        return frombuffer(data[:need], dtype=uint8).reshape(height, width, 4)[:, :, 2::-1]
    bpp = {15: 2, 16: 2, 24: 3, 32: 4}.get(bitsPerPixel)
    if bpp is None:
        return None
    need = width * height * bpp
    if isCompress:
        if bitsPerPixel == 32:  # planar (RDP 6.0), kept bottom-up by the decoder
            from rdpy.core import rle
            raw = rle.bitmap_decompress4(data, width, height)
            flip = True
        else:  # Interleaved RLE, decoded top-down.
            from rdpy.core import interleaved
            try:
                raw = interleaved.decompress(data, width, height, bpp)
            except interleaved.RleError:
                return None
            flip = False
    else:  # uncompressed: bottom-up (BMP convention)
        raw, flip = data, True
    if len(raw) < need:
        return None
    rows = frombuffer(raw[:need], dtype=uint8).reshape(height, width * bpp)
    if flip:
        rows = rows[::-1]
    if bpp == 2:
        v = frombuffer(rows.tobytes(), dtype=u'<u2').reshape(height, width).astype(uint16)
        if bitsPerPixel == 16:  # RGB565
            r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
            r, g, b = (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)
        else:  # RGB555
            r, g, b = (v >> 10) & 31, (v >> 5) & 31, v & 31
            r, g, b = (r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2)
        return stack((r, g, b), axis=-1).astype(uint8)
    return rows.reshape(height, width, bpp)[:, :, 2::-1]  # BGR / BGRA -> RGB


class ScreenBuffer(object):
    """
    @summary: The remote screen rebuilt from the bitmap updates (what a window would show), so
    that it can be saved as a PNG without any UI. The mouse pointer is not drawn.
    """

    def __init__(self, width, height):
        from numpy import zeros, uint8
        self.rgb = zeros((max(1, height), max(1, width), 3), dtype=uint8)
        self.rectangles = 0

    def resize(self, width, height):
        """Match the desktop size the server decided (existing pixels are kept)."""
        from numpy import zeros, uint8
        if width <= 0 or height <= 0 or self.rgb.shape[:2] == (height, width):
            return
        new = zeros((height, width, 3), dtype=uint8)
        h, w = min(height, self.rgb.shape[0]), min(width, self.rgb.shape[1])
        new[:h, :w] = self.rgb[:h, :w]
        self.rgb = new

    def _blit(self, left, top, pixels):
        h, w = pixels.shape[:2]
        if left < 0 or top < 0:
            return
        if top + h > self.rgb.shape[0] or left + w > self.rgb.shape[1]:  # server drew outside the announced size
            self.resize(max(self.rgb.shape[1], left + w), max(self.rgb.shape[0], top + h))
        self.rgb[top:top + h, left:left + w] = pixels
        self.rectangles += 1

    def paint(self, left, top, right, bottom, width, height, bitsPerPixel, isCompress, data):
        pixels = bitmapToRgb(width, height, bitsPerPixel, isCompress, data)
        if pixels is None:
            return
        w, h = min(right - left + 1, width), min(bottom - top + 1, height)  # the rectangle may carry padding
        if w > 0 and h > 0:
            self._blit(left, top, pixels[:h, :w])

    def paintSurface(self, x, y, width, height, surface, surfaceWidth, srcLeft, srcTop):
        """RDPGFX / AVC: a region of the persistent BGRA surface buffer."""
        from numpy import ndarray, frombuffer, uint8
        buf = (surface if isinstance(surface, ndarray) else frombuffer(surface, dtype=uint8)).reshape(-1)
        stride = surfaceWidth * 4
        rows = min(height, (len(buf) - srcTop * stride) // stride if stride else 0)
        if width <= 0 or rows <= 0:
            return
        block = buf[srcTop * stride:(srcTop + rows) * stride].reshape(rows, stride)
        self._blit(x, y, block[:, srcLeft * 4:(srcLeft + width) * 4].reshape(rows, width, 4)[:, :, 2::-1].copy())

    def save(self, pth):
        from PIL import Image
        folder = dirname(abspath(pth))
        if not isdir(folder):
            makedirs(folder)
        Image.fromarray(self.rgb, u'RGB').save(pth)


class LoginCheck(rdp.RDPClientObserver):
    """
    @summary: RDP observer that drives the "login screen -> press ENTER -> desktop" check
    """

    def __init__(self, controller, options):
        rdp.RDPClientObserver.__init__(self, controller)
        self.options = options
        self.started = time()
        self.events = []  # [(seconds since start, text)]
        self.enters = 0  # ENTER presses sent
        self.pictures = 0  # bitmap updates received
        self.stage = u'connecting'  # connecting | connected | login_screen | desktop
        self.result = None  # None while running, else the exit code
        self.reason = u''
        self.session = {}  # security protocol / desktop size, when the server told
        self._firstPicture = False
        self.info = {}  # getSessionInfo() snapshots, merged
        self.bppSeen = set()  # color depths of the bitmaps the server really sent
        self.probing = False  # a 2nd session is being tested: do not stop the reactor on a lost link
        self.serverClosed = None  # why the server closed this session (seen during the probe)
        self.multiSession = None  # {supported, detail}
        self.shots = {u'login': None, u'desktop': None}  # kind -> saved file
        self._shotDone = set()
        self.screen = ScreenBuffer(options.width, options.height) if options.shot_login or options.shot_desktop \
            else None
        self._enterCall = None
        self._timeoutCall = reactor.callLater(options.timeout, self._onTimeout)

    # ------------------------------------------------------------------ helpers
    def note(self, text):
        self.events.append((round(time() - self.started, 2), text))
        log.info(text)

    def finish(self, code, reason):
        """Record the result, close the session and let the factory stop the reactor."""
        if self.result is not None:
            return
        self.result, self.reason = code, reason
        self.note(u'result: %s' % reason)
        self.snapshot()
        if self.stage == u'desktop':  # last chance: the session is about to be closed
            self._takeShot(u'desktop')
        elif self.stage == u'login_screen':
            self._takeShot(u'login')
        for call in (self._enterCall, self._timeoutCall):
            if call is not None and call.active():
                call.cancel()
        try:
            if self._controller.isConnected():
                self._controller.close()
        except Exception as e:  # the session is going away anyway
            log.debug(u'close failed: %r' % (e,))
        if not self.options.secondary:
            reactor.callLater(2, self.stopReactor)  # safety net if the connection never reports "lost"
        if self.options.on_finish is not None:
            self.options.on_finish(self)

    @staticmethod
    def stopReactor():
        if reactor.running:
            reactor.stop()

    def _onTimeout(self):
        if self.stage == u'desktop':
            return
        where = {u'connecting': u'the RDP session never became ready',
                 u'connected': u'the server showed no picture',
                 u'login_screen': u'stuck on the login screen (ENTER sent %d time(s))' % self.enters}[self.stage]
        self.finish(EXIT_NOT_CONFIRMED, u'desktop not confirmed in %ds: %s' % (self.options.timeout, where))

    # ------------------------------------------------------------------ server information
    def snapshot(self):
        """
        Remember what the server told us so far: rdpy's own session report (controller.getSessionInfo(),
        built by serverinfo), completed with what it does not report: the Windows name, the OS family of
        the server, the depth of the bitmaps really received, the multiple-session result.
        """
        if not self.options.info:
            return
        try:
            controller = self._controller
            extra = {u'connection': {}, u'remote': {u'osType': serverOsType(controller)}}
            size = controller.getServerDesktopSize()
            if size and size[0] > 0 and size[1] > 0:  # (0, 0) = the server did not announce one
                extra[u'connection'][u'serverDesktop'] = u'%dx%d' % size
            extra[u'connection'][u'colorDepthReceived'] = u', '.join(u'%d bits' % b for b in sorted(self.bppSeen))
            report = controller.getSessionInfo()
            if not controller.isConnected():
                report.pop(u'status', None)  # Live state: after the close it would only say 'not connected'
            mergeInfo(self.info, report)
            mergeInfo(self.info, extra)
            self.info.setdefault(
                u'features', {}).setdefault(u'multipleSessions', u'not tested (use --probe-multi-session)')
            remote = self.info.setdefault(u'remote', {})
            windows = describeWindows(self.info, remote.get(u'osType'), (self.multiSession or {}).get(u'supported'))
            # the Windows name first: it is what people look for
            self.info[u'remote'] = dict([(u'windows', windows)] if windows else [],
                                        **{k: v for k, v in remote.items() if k != u'windows'})
        except Exception as e:  # information is a bonus, it never changes the verdict
            log.debug(u'server information unavailable: %r' % (e,))

    def sessionId(self):
        return self.info.get(u'logon', {}).get(u'sessionId')

    def _desktopReason(self):
        return u'desktop reached%s' % (u' after %d ENTER press(es)' % self.enters if self.enters else
                                       u' (automatic logon, no ENTER needed)')

    def _desktopDone(self):
        """The desktop had its time to be drawn: leave, or hand over to the multi-session probe."""
        if self.options.on_desktop is not None:
            self.options.on_desktop(self)
        elif self.options.probe_multi_session:
            MultiSessionProbe(self, self.options).start()
        else:
            self.finish(EXIT_DESKTOP, self._desktopReason())

    # ------------------------------------------------------------------ screenshots
    def _takeShot(self, kind):
        """Save the screen as shot_<kind> once (nothing happens when that screenshot was not asked for)."""
        pth = getattr(self.options, u'shot_' + kind)
        if pth is None or self.screen is None or kind in self._shotDone:
            return
        self._shotDone.add(kind)
        try:
            self.screen.save(pth)
            self.shots[kind] = pth
            self.note(u'%s screenshot saved: %s' % (kind, pth))
        except Exception as e:  # a screenshot problem never changes the verdict
            self.note(u'%s screenshot failed: %s' % (kind, e))

    # ------------------------------------------------------------------ ENTER logic
    def _scheduleEnter(self, delay):
        if self.result is None and self.stage != u'desktop' and self.enters < self.options.max_enters \
                and not self.options.no_enter:
            self._enterCall = reactor.callLater(delay, self._pressEnter)

    def _pressEnter(self):
        """
        Still not on the desktop: press ENTER (key down, then key up 60 ms later).
        """
        if self.result is not None or self.stage == u'desktop':
            return
        controller = self._controller
        if not getattr(controller, u'_isReady', False) or getattr(controller, u'_gfxPending', False):
            self._scheduleEnter(0.5)  # input not accepted yet (session activating)
            return
        self._takeShot(u'login')  # the login screen as it is before the first key press
        self.enters += 1
        self.note(u'no desktop yet -> sending ENTER (%d/%d)' % (self.enters, self.options.max_enters))
        controller.sendKeyEventScancode(SCANCODE_ENTER, True)
        reactor.callLater(0.06, controller.sendKeyEventScancode, SCANCODE_ENTER, False)
        self._scheduleEnter(self.options.enter_interval)

    # ------------------------------------------------------------------ RDPClientObserver
    def onReady(self):
        self.stage = u'connected'
        self.note(u'RDP connection established (credentials sent)')
        size = self._controller.getServerDesktopSize() if self.screen is not None else None
        if size:
            self.screen.resize(*size)
        self.snapshot()

    def onDesktopResize(self, width, height):
        if self.screen is not None:
            self.screen.resize(width, height)

    def onSessionReady(self):
        self.note(u'session ready')

    def _onPicture(self):
        self.pictures += 1
        if not self._firstPicture:
            self._firstPicture = True
            # before the logon notification, the first picture is the Windows login screen
            self._controller.onFirstPicture()

    def onUpdate(self, destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, isCompress, data):
        self.bppSeen.add(bitsPerPixel)
        if self.screen is not None:  # Pixels are only decoded when a screenshot was asked for
            try:
                self.screen.paint(destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel,
                                  isCompress, data)
            except Exception as e:
                log.warning(u'Bitmap %dx%d at %d,%d not drawn: %r' % (width, height, destLeft, destTop, e))
        self._onPicture()

    def onGfxSurface(self, dest_x, dest_y, width, height, surf_buf, surf_w, src_left, src_top):
        self.bppSeen.add(32)
        if self.screen is not None:
            try:
                self.screen.paintSurface(dest_x, dest_y, width, height, surf_buf, surf_w, src_left, src_top)
            except Exception as e:
                log.warning(u'surface region not drawn: %r' % (e,))
        self._onPicture()

    def onLoginScreen(self):
        if self.stage == u'desktop':
            return
        self.stage = u'login_screen'
        self.note(u'Login screen displayed')
        self._scheduleEnter(self.options.enter_delay)  # give autologon a chance first
        if self.options.shot_login:  # a moment later: the login screen needs time to finish painting
            reactor.callLater(self.options.shot_delay, self._takeShot, u'login')

    def onDesktopReady(self):
        if self.stage == u'login_screen':
            self._takeShot(u'login')  # before the desktop starts to replace it
        self.stage = u'desktop'
        self.note(u'Server confirmed the logon: the desktop is starting')
        try:  # a few facts for the report (never needed for the verdict)
            info = self._controller.getSessionInfo() or {}
            self.session = {u'security': info.get(u'security', {}).get(u'protocol'),
                            u'desktop': info.get(u'connection', {}).get(u'desktop')}
        except Exception as e:
            log.debug(u'session info unavailable: %r' % (e,))
        if self._enterCall is not None and self._enterCall.active():
            self._enterCall.cancel()
        # let the first desktop frames arrive, then leave
        wait = self.options.hold
        if self.options.shot_desktop:  # the desktop needs a few seconds to be drawn
            reactor.callLater(self.options.shot_desktop_delay, self._takeShot, u'desktop')
            wait = max(wait, self.options.shot_desktop_delay + 0.3)
        reactor.callLater(wait, self._desktopDone)

    def onLogonError(self, errorType, errorData, message):
        self.note(u'Logon error: %s' % message)
        self.finish(EXIT_LOGON_ERROR, u'Logon refused: %s' % message)

    def onClose(self):
        self.note(u'connection closed')


class MultiSessionProbe(object):
    """
    @summary: Does the server allow several sessions of the same account at once? The only way to know
    is to try: a 2nd session is opened with the same credentials while the 1st stays connected.
      - both stay connected with different session ids  -> yes
      - the 1st is disconnected / both share one session -> no (the 2nd took the session over)
      - the 2nd is refused                                -> no
    WARNING: on a single-session server this disconnects the 1st session - and with it a person who
    is working in that account, as any connection with the same account does.
    """

    def __init__(self, first, options):
        self.first, self.options = first, options
        self.second = None
        self.done = False
        self._timeout = None

    def start(self):
        o = copy(self.options)
        o.secondary, o.probe_multi_session = True, False
        o.shot_login = o.shot_desktop = None
        o.info, o.hold = True, 0
        o.on_desktop, o.on_finish = self._onSecondDesktop, self._onSecondFinish
        self.first.probing = True
        self.first.snapshot()
        self.first.note(u'Probe: opening a second session with the same account')
        self.factory = LoginCheckFactory(o)
        reactor.connectTCP(o.host, o.port, self.factory, timeout=15)
        self._timeout = reactor.callLater(o.timeout, self._conclude, u'unknown',
                                          u'The second session did not reach the desktop in %ds' % o.timeout)

    def _onSecondDesktop(self, second):
        self.second = second
        self.first.note(u'Probe: second session is on the desktop, watching the first one for %gs' %
                        self.options.probe_settle)
        reactor.callLater(self.options.probe_settle, self._evaluate)

    def _onSecondFinish(self, second):
        if self.done or second.stage == u'desktop':
            return
        if second.result == EXIT_LOGON_ERROR:
            self._conclude(u'no', u'the server refused a second session: %s' % second.reason)
        else:
            self._conclude(u'unknown', u'the second session failed: %s' % second.reason)

    def _evaluate(self):
        if self.done:
            return
        first, second = self.first, self.second
        first.snapshot()
        second.snapshot()
        id1, id2 = first.sessionId(), second.sessionId()
        ids = u'session ids %s and %s' % (id1, id2)
        error = first._controller.getLastError()
        alive = first.serverClosed is None and first._controller.isConnected()
        if alive and id1 is not None and id2 is not None and id1 != id2:
            self._conclude(u'yes', u'both sessions are active at the same time (%s)' % ids)
        elif alive and id1 is not None and id1 == id2:
            self._conclude(u'no', u'Both connections joined the same session (%s)' % ids)
        elif alive:
            self._conclude(u'probably yes', u'Both connections stayed up (the server sent no session id)')
        elif id1 is not None and id1 == id2:
            self._conclude(u'no', u'One session per account: the second connection took session %s over and '
                                  u'disconnected the first%s' % (id1, u' (%s)' % error[1] if error else u''))
        else:
            self._conclude(u'no', u'The first session was disconnected when the second one started (%s)%s' %
                           (ids, u': %s' % error[1] if error else u''))

    def _conclude(self, supported, detail):
        if self.done:
            return
        self.done = True
        if self._timeout is not None and self._timeout.active():
            self._timeout.cancel()
        first = self.first
        first.multiSession = {u'supported': supported, u'detail': detail}
        first.info.setdefault(u'features', {})[u'multipleSessions'] = u'%s - %s' % (supported, detail)
        first.snapshot()  # the Windows name may be narrowed by this result
        first.note(u'probe result: %s - %s' % (supported, detail))
        if self.second is not None and self.second.result is None:
            self.second.finish(EXIT_DESKTOP, u'probe finished')
        first.probing = False
        # let the second session close first, then close the first one (this stops the reactor)
        reactor.callLater(1.0, first.finish, EXIT_DESKTOP, first._desktopReason())


class LoginCheckFactory(rdp.ClientFactory):
    """
    @summary: Builds the controller + observer, falls back NLA -> TLS -> RDP like the GUI client
    """

    def __init__(self, options):
        self.options = options
        self.settings = options.settings
        self.attempts = self.settings.attempts(bool(options.username and options.password))
        self.attempt = 0
        self.observer = None
        self.exitCode = EXIT_CONNECTION
        self.lastError = u''

    def buildObserver(self, controller, addr):
        o = self.options
        if self.observer is None:
            self.observer = LoginCheck(controller, o)
        else:  # reconnection with the next security protocol: same report, new controller
            self.observer._controller = controller
            controller.addClientObserver(self.observer)
        controller.setScreen(o.width, o.height)
        controller.setColorDepth(o.bpp)  # 15 / 16 / 24 / 32 bits per pixel
        controller.setGraphicsPipeline(False)  # legacy bitmap updates: no GFX input gating, plain pixels
        controller.setPerformanceSession()
        controller.setSoundEnabled(False)
        controller.setMicrophoneEnabled(False)
        controller.setClipboardMode('off')
        controller.setUsername(o.username)
        controller.setPassword(o.password)  # also enables autologon
        controller.setDomain(o.domain)
        controller.setKeyboardType('IBM_101_102_KEYS')
        controller.setKeyboardLayout(o.layout)
        controller.setHostname(gethostname())
        name, requested = self.attempts[self.attempt]
        log.info(u'security: trying %s (%s)' % (name, self.settings.describe()))
        controller.setSecurity(self.settings, o.host, o.port, requested)
        return self.observer

    def _reason(self, reason):
        controller = self.observer._controller if self.observer else None
        if controller is not None:
            verifier = controller.getCertificateVerifier()
            if verifier is not None and verifier.rejection:
                return verifier.rejection
            error = controller.getProtocolError()
            if error:
                return error
            last = controller.getLastError()
            if last:
                return last[1]
        return str(getattr(reason, u'value', reason))

    def _lost(self, connector, reason, what):
        obs = self.observer
        controller = obs._controller if obs else None
        if obs is not None:
            obs.snapshot()
        if obs is not None and obs.probing and obs.result is None:  # the probe decides when to stop
            obs.serverClosed = self._reason(reason)
            obs.note(u'The server closed this session: %s' % obs.serverClosed)
            return
        error = controller.getProtocolException() if controller is not None else None
        ready = bool(controller is not None and obs.stage != u'connecting')
        if (isinstance(error, RDPSecurityNegoFail) or reason.type == RDPSecurityNegoFail) and \
                self.attempt + 1 < len(self.attempts) and not ready:
            failed = self.attempts[self.attempt][0]
            self.attempt += 1
            log.info(u'security: %s refused (%s), falling back to %s' %
                     (failed, error or reason.value, self.attempts[self.attempt][0]))
            connector.connect()
            return
        if obs is not None and obs.result is None and obs.stage == u'desktop':
            obs.finish(EXIT_DESKTOP, u'desktop reached (the server closed the session afterwards)')
        if obs is not None and obs.result is None:
            self.lastError = self._reason(reason)
            obs.finish(EXIT_CONNECTION, u'%s: %s' % (what, self.lastError))
        self.exitCode = obs.result if obs is not None and obs.result is not None else EXIT_CONNECTION
        if not self.options.secondary:
            LoginCheck.stopReactor()

    def clientConnectionLost(self, connector, reason):
        self._lost(connector, reason, u'Connection lost before the desktop was reached')

    def clientConnectionFailed(self, connector, reason):
        if self.observer is None:
            self.exitCode = EXIT_CONNECTION
            self.lastError = str(getattr(reason, u'value', reason))
            print(u'connection failed: %s' % self.lastError, file=stderr)
            LoginCheck.stopReactor()
            return
        self._lost(connector, reason, u'connection failed')


def parseArguments(argv=None):
    p = ArgumentParser(description=u'Headless RDP check: log in and make sure the desktop is reached '
                                   u'(presses ENTER when the session stays on the login screen).')
    p.add_argument(u'target', help=u'host or host:port (default port 3389)')
    p.add_argument(u'-u', u'--username', default=u'')
    p.add_argument(u'-p', u'--password', default=None,
                   help=u'Password (default: $RDP_PASSWORD, otherwise asked without echo)')
    p.add_argument(u'-d', u'--domain', default=u'')
    p.add_argument(u'--security', choices=(u'nego', u'nla', u'tls', u'rdp'), default=u'nego',
                   help=u'nego: NLA, then TLS, then RDP (default)')
    p.add_argument(u'--cert', choices=security.CERT_MODES, default=security.CERT_TOFU,
                   help=u'Server certificate: tofu = trust on first use (default), ignore, deny')
    p.add_argument(u'--layout', default=u'US', help=u'Keyboard layout name (US, FRENCH...)')
    p.add_argument(u'--size', default=u'1024x768', help=u'Session size WxH')
    p.add_argument(u'--timeout', type=int, default=60, help=u'Give up after this many seconds (60)')
    p.add_argument(u'--enter-delay', type=float, default=5.0,
                   help=u'Seconds on the login screen before the first ENTER (5)')
    p.add_argument(u'--enter-interval', type=float, default=5.0, help=u'Seconds between ENTER presses (5)')
    p.add_argument(u'--max-enters', type=int, default=5, help=u'Maximum ENTER presses (5)')
    p.add_argument(u'--no-enter', action=u'store_true', help=u'Only observe, never press ENTER')
    p.add_argument(u'--hold', type=float, default=2.0, help=u'Seconds to stay after the desktop starts (2)')
    p.add_argument(u'--bpp', type=int, choices=(15, 16, 24, 32), default=16,
                   help=u'Colour depth of the session: 15, 16 (default), 24 or 32 bits per pixel')
    p.add_argument(u'--info', action=u'store_true',
                   help=u'Report what the server revealed (rdpy session report): Windows name and version, computer '
                        u'name, domain, RDP version, security, certificate, logon user, session id, colour depth')
    p.add_argument(u'--probe-multi-session', action=u'store_true',
                   help=u'Open a 2nd session with the same account to learn whether several sessions can run at '
                        u'once (implies --info). On a single-session server this DISCONNECTS the 1st session '
                        u'of that account')
    p.add_argument(u'--probe-settle', type=float, default=4.0,
                   help=u'Seconds to watch the 1st session once the 2nd is on the desktop (4)')
    p.add_argument(u'--screenshots', metavar=u'DIR', default=None,
                   help=u'Save <host>_<time>_login.png and <host>_<time>_desktop.png in DIR')
    p.add_argument(u'--shot-login', metavar=u'FILE', default=None,
                   help=u'PNG of the login screen (when the session shows one)')
    p.add_argument(u'--shot-desktop', metavar=u'FILE', default=None, help=u'PNG of the desktop')
    p.add_argument(u'--shot-delay', type=float, default=1.0,
                   help=u'Seconds after the login screen appears before the screenshot (1)')
    p.add_argument(u'--shot-desktop-delay', type=float, default=3.0,
                   help=u'Seconds after the logon before the desktop screenshot (3)')
    p.add_argument(u'--json', action=u'store_true', help=u'Print the report as JSON')
    p.add_argument(u'-v', u'--verbose', action=u'store_true', help=u'Protocol log')
    o = p.parse_args(argv)
    host, _, port = o.target.rpartition(u':') if o.target.count(u':') == 1 else (o.target, u'', u'')
    o.host, o.port = (host, int(port)) if port.isdigit() else (o.target, 3389)
    try:
        o.width, o.height = [int(v) for v in o.size.lower().split(u'x')]
    except ValueError:
        p.error(u'--size must look like 1024x768')
    o.secondary, o.on_desktop, o.on_finish = False, None, None  # internal: used by the 2nd probe session
    o.info = o.info or o.probe_multi_session
    if o.screenshots:
        stem = join(o.screenshots, u'%s_%s' % (sub(u'[^A-Za-z0-9.-]+', u'_', o.host), strftime(u'%Y%m%d-%H%M%S')))
        o.shot_login = o.shot_login or stem + u'_login.png'
        o.shot_desktop = o.shot_desktop or stem + u'_desktop.png'
    if o.shot_login or o.shot_desktop:
        missing = [name for name in (u'numpy', u'PIL') if find_spec(name) is None]
        if missing:
            p.error(u'Screenshots need numpy and Pillow (pip install numpy Pillow); missing: %s' % u', '.join(missing))
    if o.password is None:
        o.password = environ.get(u'RDP_PASSWORD')
    if o.password is None and o.username:
        o.password = getpass(u'password for %s: ' % o.username)
    o.password = o.password or u''
    kinds = {u'nego': security.SecuritySettings(certificate=o.cert),
             u'nla': security.SecuritySettings.only(u'nla', o.cert),
             u'tls': security.SecuritySettings.only(u'tls', o.cert),
             u'rdp': security.SecuritySettings.only(u'rdp', o.cert)}
    o.settings = kinds[o.security]
    if not o.settings.attempts(bool(o.username and o.password)):
        p.error(u'No usable security protocol (NLA needs a user name and a password)')
    return o


def report(options, factory):
    obs = factory.observer
    code = obs.result if obs is not None and obs.result is not None else factory.exitCode
    data = {
        u'target': u'%s:%d' % (options.host, options.port),
        u'user': options.username,
        u'desktop_reached': code == EXIT_DESKTOP,
        u'exit_code': code,
        u'reason': obs.reason if obs is not None and obs.reason else factory.lastError,
        u'stage': obs.stage if obs is not None else u'connecting',
        u'session': obs.session if obs is not None else {},
        u'screenshots': obs.shots if obs is not None else {u'login': None, u'desktop': None},
        u'info': None,
        u'enter_presses': obs.enters if obs is not None else 0,
        u'pictures': obs.pictures if obs is not None else 0,
        u'events': obs.events if obs is not None else [],
    }
    if obs is not None and options.info:
        data[u'info'] = {section: dict(values) for section, values in obs.info.items()}
        data[u'info'].get(u'certificate', {}).pop(u'pem', None)  # long, and not needed here
        data[u'windows'] = data[u'info'].get(u'remote', {}).get(u'windows')
    if options.json:
        print(dumps(data, indent=2))
    else:
        print(u'%s  %s' % (u'PASS' if data[u'desktop_reached'] else u'FAIL', data[u'target']))
        print(u'  reason : %s' % data[u'reason'])
        print(u'  stage  : %s, ENTER presses: %d, bitmap updates: %d' %
              (data[u'stage'], data[u'enter_presses'], data[u'pictures']))
        for kind in (u'login', u'desktop'):
            if data[u'screenshots'][kind]:
                print(u'  %-8s screenshot: %s' % (kind, data[u'screenshots'][kind]))
        if data[u'info'] is not None:
            print(u'  Server information')
            for line in serverinfo.formatReport(data[u'info']).splitlines():  # rdpy's own report format
                print((u'  ' + line).rstrip())
        if data[u'session']:
            print(u'  session: %s' % u', '.join(u'%s=%s' % kv for kv in sorted(data[u'session'].items())))
        for at, text in data[u'events']:
            print(u'  %6.2fs  %s' % (at, text))
    return code


def main(argv=None):
    options = parseArguments(argv)
    log.set_level(log.Level.DEBUG if options.verbose else log.Level.ERROR)
    try:
        factory = LoginCheckFactory(options)
    except ValueError as e:
        print(e, file=stderr)
        return EXIT_ARGS
    reactor.connectTCP(options.host, options.port, factory, timeout=15)
    reactor.run()
    return report(options, factory)


if __name__ == u'__main__':
    exit(main())
