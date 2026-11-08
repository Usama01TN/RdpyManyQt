# RdpyManyQt changelog

RdpyManyQt 3.1.3 (2026) - by UsamaTN <ininou.oussematn@gmail.com>, forked from rdpyqt 2.1.6.

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
- Surface bits: the 24-byte TS_COMPRESSED_BITMAP_HEADER_EX is not part of bitmapDataLength
  (it was subtracted, cutting every bitmap and misparsing the following commands: black
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

## Packaging:

- Wheels for CPython 3.11 - 3.15 (iOS / Android 3.13 - 3.15) and for 32-bit Windows; the
  Windows ARM64 wheels are now tested too.
- The Cython decoders use typed memoryviews instead of the NumPy C API: NumPy is no longer a
  build dependency, which fixes the iOS and Android builds (no NumPy exists for those targets
  at build time). Decoded output is byte-identical.
- Intel macOS installs cryptography 48 (the last release with an Intel wheel) instead of
  compiling cryptography 49+ from source; wheel tests never compile cryptography.
- `project.license` is an SPDX expression (setuptools >= 77) instead of the deprecated table.
- Every push to main / master publishes the release v<version>, force-moving its tag and
  replacing its assets, even when a platform build fails.
