# RdpyManyQt changelog

RdpyManyQt 3.1.3 (2026) - by UsamaTN <ininou.oussematn@gmail.com>, forked from rdpyqt 2.1.6.

## Channels folder (3.7.0):

- The 29 virtual channel modules moved to `rdpy/protocol/rdp/channels/` (like FreeRDP's
  `channels/`): cliprdr, rdpsnd, rdpdr (+ drive, printer, serial, parallel, smart card), rail,
  drdynvc, remdesk, encomsp, rdp2tcp, audin, disp, urbdrc, ainput, sshagent, rdpecam, echo,
  location, rdpei, geometry, video, rdpewa, tsmf and their helpers. The core (rdp, sec, lic,
  tpkt, x224, t125, nla, pdu), the codecs and audio_out stay in `rdpy/protocol/rdp/`.
- The former names still work (`import rdpy.protocol.rdp.cliprdr`,
  `from rdpy.protocol.rdp import rdpsnd`): an import alias gives the same module objects (Python 2 and 3).

## FreeRDP channel tsmf (3.6.0):

- Multimedia redirection (MS-RDPEV, "TSMF"): Windows Media Player / DirectShow / Media Foundation
  playback decoded here with PyAV instead of FreeRDP's FFmpeg backend: FreeRDP's messages and
  replies, media type parsing (WAVEFORMATEX, VIDEOINFOHEADER(2), MPEG1/2 video info,
  MFVIDEOFORMAT), its codec set (VC-1, WMV3, MPEG-2, H.264 / AVC1 with the avcC from
  MPEG2VIDEOINFO, WMA v2 / Pro, MP3, MP2, AAC, AC-3), per-stream playback and ack threads,
  end-of-stream notification; video drawn at the player geometry within its visible rectangles,
  audio on the local output with the stream volume. `--multimedia`, RdpViewerWidget
  `multimediaRedirection`.

## rdpewa aligned with FreeRDP (3.5.1):

- makeCredential / getAssertion sent with the parameters FreeRDP sets on libfido2 (first algorithm,
  no extensions, rk / up / uv options) and answered with the fields FreeRDP returns.
- PIN fallback only on UV_BLOCKED / UV_INVALID; built-in UV through a pinUvAuthToken on CTAP 2.1
  keys (libfido2 permissions); touch-to-select with several keys (30 s); "Touch the security key"
  notification with Cancel; credential list as FreeRDP (keys with a PIN skipped).
- Kept on purpose: the server's Cancel stops the key operation (FreeRDP ignores it).

## FreeRDP channel rdpewa (3.5.0):

- WebAuthn redirection (MS-RDPEWA, "WebAuthN_Channel"): passkey / security key sign-ins in the
  session use the FIDO2 keys of this computer through python-fido2 (USB HID): API version 4,
  authenticator list, UVPAA, cancel; makeCredential / getAssertion relayed to the key on a worker
  thread with FreeRDP's PIN order (built-in UV first, PIN dialog otherwise); FreeRDP's response
  format and errors. `--webauthn`, RdpViewerWidget `webAuthn`, extra `webauthn` (pip).
- The GUI invoker created from a worker thread now runs its calls in the GUI thread.

## FreeRDP channels (3.4.0):

- video (MS-RDPEVOR, Video Optimized Remoting): videos playing in the session arrive as H.264,
  decoded with PyAV on a worker thread, shown at their time where the video window is, scaled to
  the presentation size; latest due frame shown, older dropped, frame rate feedback every second
  as FreeRDP; `--video` (enables geometry), RdpViewerWidget `videoRedirection`.
- geometry (MS-RDPEGT): video window positions and regions; `--geometry`.
- encomsp (MS-RDPEMSC): multiparty channel; with Remote Assistance it requests the control of
  the novice's desktop once (FreeRDP RemoteAssistanceRequestControl); `--encomsp`,
  `--no-request-control`, Shift+right-click > Request control / View only,
  RdpViewerWidget.requestRemoteControl().

## FreeRDP channels (3.3.0):

- rdpei (MS-RDPEI): multi-touch (64 contacts) and pen (pressure, rotation, tilt, eraser, barrel)
  from Qt touch and tablet events, FreeRDP encodings and contact state machine; `--multitouch`,
  RdpViewerWidget `multitouch`. Without the channel Qt keeps turning them into mouse events.
- location (MS-RDPEL): the position given with `--location LAT,LON[,ALT]` (or Qt Positioning with
  `--location auto`), FOUR_BYTE_FLOAT / SIGNED_INTEGER encodings identical to FreeRDP's;
  RdpViewerWidget `location`.
- echo (MS-RDPEECO): `--echo`, RdpViewerWidget `echoChannel`.
- rdp2tcp static channel: TCP forwarding with the rdp2tcp tool, `--rdp2tcp COMMAND`,
  RdpViewerWidget `rdp2tcpCommand`.

## FreeRDP channels (3.2.0):

- ainput (FreeRDP::Advanced::Input): while a FreeRDP based server opens it, mouse and wheel
  events go through it (32-bit coordinates, relative motion, wheel x 0x10000), as FreeRDP's
  client; `--no-advanced-input`, RdpViewerWidget `advancedInput`.
- sshagent (SSHAGENT): the local ssh-agent (SSH_AUTH_SOCK, or the Windows OpenSSH pipe) is
  forwarded to the session; `--ssh-agent`, `sshAgent`.
- remdesk (Remote Assistance, expert side): `.msrcincident` invitations (RCTICKET and
  encrypted LHTICKET), expert authentication for protocol 1.1 / 1.2, Remote Assistance Client
  Info; `--assistance FILE --assistance-password PASS`, RdpViewerWidget.setRemoteAssistance().
- rdpecam (MS-RDPECAM camera redirection): Qt Multimedia cameras (Qt 5 / 6), H.264 (PyAV
  libx264 or another H.264 encoder) or MJPEG samples, one per Sample Request;
  `--camera`, `--camera-device NAME`, `--camera-test`, `cameraRedirection` / `cameraDevice`.

## Display:

- FreeRDP-style RDPGFX presentation: decoders write into surfaces, invalid areas are shown once
  per frame; one H.264 decoder per surface; AVC region rects in surface coordinates.
- Graphics decoded on a worker thread, in order; real queue depth in frame acknowledgements.
- Fixes: partly covered updates no longer skipped, edge-of-surface copies clipped, upside-down
  planar tiles, uninitialised window buffer; GIL released in the Cython decoders.
- Upstream rdpyqt b94c7b1 (zgfx, bitmap bounds, reconnect drain) applied.

- Legacy mode (colour depth below 32 bits): NSCodec / uncompressed surface bits decoded in
  arrival order on one worker thread (the Twisted thread pool decoded them out of order,
  leaving stale black blocks), frames acknowledged after their bitmaps.
- 15 / 16 / 24 bpp bitmaps: new interleaved RLE decoder (rdpy/core/interleaved.py), a port
  of FreeRDP's RLEDECOMPRESS, identical to FreeRDP's own decoder on 6,000+ random and
  encoder-made streams and on real server tiles. The old decoders drew about 3 % of the
  bitmaps wrongly and raised on others ("bytearray index out of range", "bytes length not a
  multiple of item size"), which dropped the whole update: black blocks. A bad rectangle
  no longer drops the other rectangles of an update.
- Legacy mode black blocks: the client announced DstBlt / PatBlt / ScrBlt drawing orders but
  never drew ORDERS updates, so Windows' fills and screen copies were lost. No order is
  announced any more: the server sends bitmap updates, which are decoded.
- Surface bits: the 24-byte TS_COMPRESSED_BITMAP_HEADER_EX is not part of bitmapDataLength (it was subtracted, cutting
  every bitmap and misparsing the following commands: black
  64x64 blocks below 32 bits); uncompressed surface bits flipped and converted from
  15/16/24/32 bpp like FreeRDP gdi_surface_bits.

## Audio:

- Playback on a dedicated Qt audio thread (Qt5 and Qt6, any ManyQt binding), latency-bounded
  jitter buffer, format conversion, playback latency reported in Wave Confirm.
- Microphone redirection (MS-RDPEAI, `--microphone`).

## Clipboard and drives:

- File copy / paste in both directions; `--clipboard off|text|files`.
- Drive redirection (rdpdr) with the Copilot review fixes (path confinement, symlinks,
  resource limits, locks, rename/delete semantics).

## Connection:

- Security options following FreeRDP rdpSettings: `--sec`, `--sec-nla/tls/rdp`, `--security max|compat`,
  certificate check `--cert ignore|tofu|deny` with known_hosts.
- `--connect-timeout`, `--timeout`, TCP keep-alive; FreeRDP-style clean disconnect; protocol errors
  end the session cleanly; standard RDP security fixes (experimental).
- Clean exit for every path: window close, Ctrl+C / SIGTERM / SIGHUP, QApplication.quit(),
  desktop logout, viewer window close / deletion - sessions always disconnect properly.
- Logon events: login screen, desktop reached, logon errors (`--on-login-screen`, `--on-desktop`,
  `--on-logon-error`, Qt signals).

## Logging:

- No "AVC: native YUV->BGRA load error: [WinError 2]" warning on Windows: the optional
  run-time compiled colour helper is only tried when a C compiler exists (swscale is used
  otherwise); its temporary source file is always removed.
- Closing the window ends the process at once (sessions already disconnected); one
  "disconnected" line instead of a failure dump; the version and the rdpy folder in use are
  printed at start (rdpyqt log, RdpViewer demo).
- Library default log level WARNING (no protocol trace in applications embedding the client);
  rdpyqt uses INFO; `RDPY_LOG_LEVEL=DEBUG` turns the trace on.

## Tools:

- The remote desktop is always centred in the widget, also at 1:1 (smart sizing off): the frame
  buffer keeps the remote resolution (it no longer takes the widget size, which drew the desktop
  in the top left corner of a larger viewer); the mouse follows the offset.
- Smart sizing (mstsc smart sizing, FreeRDP /smart-sizing): the remote desktop is scaled to cover
  the whole window (no black bars, as mstsc / FreeRDP), resizing does not reconnect;
  `--keep-aspect` / Keep aspect ratio letterboxes instead; mouse
  mapped back to remote coordinates. `--smart-sizing`, `--stretch`, Shift+right-click menu,
  RdpViewerWidget `smartSizing` / `keepAspectRatio`. The pointer is scaled with its hot spot,
  server-side resolution changes are followed, scaled repaints are seam-free, embedded widgets
  and HiDPI displays are handled.
- Session report (`--info`, `--json`), screenshots, Shift+right-click menu, drag & drop,
  disconnect dialog with Reconnect, `--bpp`, `--gfx`, `--shell`.
- RdpViewerWidget (embeddable widget) and its Qt Designer plugin; logo and banner. Nested IntEnum
  enumerations registered with Qt; every feature as a property (colour depth, AVC444, drives,
  keyboard layout / type, security preset, audio latency, shell, timeouts...).
- File transfer events: clipboard upload / download started, progress, finished; shared-drive
  files read / written (callbacks, Qt signals, `--on-transfer-start`, `--on-transfer-done`).

## Device redirection (FreeRDP printer, serial, parallel and smartcard channels):

- Printers (MS-RDPEPC): `--printer NAME[,DRIVER][,default]`, `--printers` / `--printer '*'`
  (every local printer), `--printer file:FOLDER` (saves PDF files). CUPS (Linux, macOS, BSD)
  or the Windows spooler; "MS Publisher Imagesetter" (PostScript) by default, "Microsoft
  Print to PDF" on macOS 14+; the printer configuration cached by the server is kept and sent
  back; `onPrintJob` / `printJob` / `--on-print` for every document.
- Serial ports (MS-RDPESP): `--serial COM1=/dev/ttyUSB0[,Serial|SerCx|SerCx2][,permissive]`,
  also pyserial URLs. Serial.sys emulation on pyserial: line settings, flow control, modem
  lines, break, XON / XOFF, Windows read / write timeouts, WaitCommEvent masks, purge,
  status, properties and statistics; reads, writes and waits on their own threads, cancelled
  by close / purge / channel reset.
- Parallel ports (MS-RDPESP): `--parallel LPT1=/dev/usb/lp0`.
- Smart cards (MS-RDPESC): `--smartcard`, `--smartcard-filter`. Every SCARD_IOCTL with its NDR
  encoding, on PC/SC through pyscard, with winpr's pcsc-lite conversions (protocols, card
  states, error and control codes) and emulations (card cache, transmit count, reader name
  attributes, device type); per-context threads, status-change waits on their own PC/SC
  context, private contexts per card, abandoned BeginTransaction handling and the Windows
  5-second idle transaction reset.
- RdpViewerWidget properties `printers`, `serialPorts`, `parallelPorts`, `smartcardEnabled`,
  `smartcardFilter`; controller `addPrinter()`, `addAllPrinters()`, `addPrintToFolder()`,
  `addSerialPort()`, `addParallelPort()`, `addSmartcard()`; the session report lists them.
- Extras: `rdpymanyqt[serial]` (pyserial), `rdpymanyqt[smartcard]` (pyscard).

## Display control, RemoteApp and USB redirection (FreeRDP disp, rail and urbdrc channels):

- Dynamic channel plugins: `DrdynvcLayer.addListener(name, factory)` (FreeRDP CreateListener),
  several openings of a name, client-side close (DYNVC_CLOSE), writes from any thread.
- Display control (MS-RDPEDISP): resizing the window changes the remote resolution without
  reconnecting (monitor layouts at most every 500 ms, even width, 200 - 8192, server area
  limit); the session takes the window size when the channel opens; `--no-dynamic-resolution`,
  `--scale-desktop`, `--scale-device`; RdpViewerWidget `dynamicResolution`. The desktop size of
  a Demand Active PDU is confirmed and reported (deactivation-reactivation resizes).
- RemoteApp (MS-RDPERP): `--app` (FreeRDP `/app` syntax), `--app-cmd`, `--app-workdir`,
  `--app-hidef`; the rail channel (handshake, client status, system parameters, execute,
  exec result, min / max info, cloaking, application ids), the window list capability and
  the Windowing Alternate Secondary Drawing Orders (windows, icons and cached icons,
  notification icons, desktop state); RDPGFX MapSurfaceToWindow / MapSurfaceToScaledWindow.
  Qt: frameless local windows cut out of the session desktop (shape from the visibility
  rectangles, resize margins hidden), input mapped to the desktop, activation, minimize /
  restore / close through system commands, window and task bar icons, notification area icons (QSystemTrayIcon, clicks
  and balloons), the remote pointer; the exec error is shown and the
  session ends with the program. RdpViewerWidget `remoteApp*` properties, `remoteAppFailed`.
- USB redirection (MS-RDPEUSB): `--usb VVVV:PPPP | addr:BUS:ADDRESS | auto`, `--usb-list`;
  control and device channels, ADD_DEVICE description (hardware / compatibility ids,
  composite devices), every TS_URB function of FreeRDP (configuration and interface selection
  with pipe handles, control / vendor / class / descriptor / feature / status requests,
  Microsoft OS descriptors, bulk, interrupt and isochronous transfers, pipe abort / reset,
  frame numbers), IO controls, device text, cancellation and no-ack requests, on libusb (python-libusb1) with
  asynchronous transfers, kernel driver detach / re-attach and hotplug.
  RdpViewerWidget `usbDevices`; extra `rdpymanyqt[usb]`.

## Packaging:

- Wheels for CPython 3.11 - 3.15 (iOS / Android 3.13 - 3.15) and for 32-bit Windows; the
  Windows ARM64 wheels are now tested too.
- The Cython decoders use typed memoryviews instead of the NumPy C API: NumPy is no longer a
  build dependency, which fixes the iOS and Android builds (PyPI has no iOS / Android NumPy;
  Flet's index pypi.flet.dev has it for CPython 3.12 - 3.14, not 3.15, so the build does not
  depend on it). Decoded output is byte-identical. To install on iOS / Android:
  `pip install --extra-index-url https://pypi.flet.dev rdpymanyqt`.
- iOS / Android wheels are tested in the simulator / emulator (`python -m rdpy.selftest`, the
  wheel checks now in the package). `tools/cibw.py` asks pip what Flet's index has for each
  Python version: NumPy and PyAV (H.264) are installed where they exist (3.13, 3.14) and the
  other versions' tests are skipped (3.15, wheels still built), automatically following the
  index; a simulator / emulator failure rebuilds the wheels without tests with a warning
  (`RDPY_MOBILE_TESTS=auto|required|off`). The Android job enables KVM for the emulator.
- Intel macOS installs cryptography 48 (the last release with an Intel wheel) instead of
  compiling cryptography 49+ from source; wheel tests never compile cryptography.
- `project.license` is an SPDX expression (setuptools >= 77) instead of the deprecated table.
- Every push to main / master publishes the release v<version>, force-moving its tag and
  replacing its assets, even when a platform build fails.
- Builds survive download outages ("HTTP Error 504: Gateway Time-out" from GitHub release
  downloads): cibuildwheel's downloads are cached between runs, retried for 15 minutes with
  mirrors (`tools/cibw.py`), a run that fails with a network error is restarted, and the
  release job retries its GitHub calls.
