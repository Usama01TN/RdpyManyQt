# coding=utf-8
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
Self test of an installed RdpyManyQt: the compiled decoders are present (unless
--pure) and decode correctly, NumPy works, PyAV (optional) can decode H.264, and
the device redirection, display control, RemoteApp and USB modules load.
    python -m rdpy.selftest [--pure]
cibuildwheel runs it after each build (tools/check_wheel.py on desktops; directly
on the iOS simulator and the Android emulator, which cannot see the project folder).
It needs no display, no network and no Qt binding.
"""
from __future__ import absolute_import, division, print_function
from sys import argv as sys_argv, exit, platform, version
from importlib import import_module
from struct import pack

COMPILED = (u'rdpy.core.rle', u'rdpy.protocol.rdp.zgfx', u'rdpy.protocol.rdp.avc',
            u'rdpy.protocol.rdp.rfx_progressive', u'rdpy.protocol.rdp.rlgr1_decode')
DEVICE_MODULES = (u'rdpy.protocol.rdp.channels.rdpdr_printer', u'rdpy.protocol.rdp.channels.rdpdr_serial',
                  u'rdpy.protocol.rdp.channels.rdpdr_parallel', u'rdpy.protocol.rdp.channels.rdpdr_smartcard',
                  u'rdpy.protocol.rdp.channels.disp', u'rdpy.protocol.rdp.channels.rail', u'rdpy.protocol.rdp.channels.urbdrc',
                  u'rdpy.protocol.rdp.channels.urbdrc_libusb')


def _checkPyAV(errors):
    """PyAV is optional (H.264): absent is fine, installed but unusable is an error."""
    try:
        from importlib.util import find_spec
        installed = find_spec('av') is not None
    except:
        installed = False
    if not installed:
        print(u'%-36s %s' % (u'av (PyAV, optional)', u'not installed: H.264 off'))
        return
    try:
        from av import __version__, CodecContext
        CodecContext.create('h264', 'r')
        print(u'%-36s %s' % (u'av (PyAV, optional)', u'%s, H.264 decoder OK' % __version__))
    except Exception as e:  # a broken FFmpeg build must fail the check
        errors.append(u'PyAV is installed but cannot decode H.264: %r' % (e,))


def run(pure=False):
    """
    @param pure: {bool} the decoders are expected to be pure Python (pure wheel)
    @return: {list[str]} errors (empty: everything works)
    """
    errors = []
    from numpy import __version__ as np___version__, uint8, zeros
    print(u'%-36s %s' % (u'numpy', np___version__))
    for name in COMPILED:
        mod = import_module(name)
        path = getattr(mod, '__file__', '') or ''
        compiled = not path.endswith('.py')
        print(u'%-36s %s' % (name, 'compiled' if compiled else 'pure Python'))
        if compiled == pure:
            errors.append(u'%s: expected %s' % (name, 'pure Python' if pure else 'a compiled module'))
    # planar bitmap: rows 0, 10, 20 (decoder keeps the encoded order)
    from rdpy.core import rle
    plane = bytes([0x40, 0, 0, 0, 0]) + bytes([0x13, 20]) * 2
    out = rle.bitmap_decompress4(bytes([0x30]) + plane * 3, 4, 3)
    if [out[r * 16] for r in range(3)] != [0, 10, 20]:
        errors.append(u'rle.bitmap_decompress4 returned wrong pixels')
    # zgfx: uncompressed single segment
    from rdpy.protocol.rdp import zgfx
    data = zgfx.ZgfxDecompressor().decompress_segment(b'\x04hello')
    if bytes(data) != b'hello':
        errors.append(u'zgfx returned %r' % (data,))
    # progressive RFX: a region block with no tile decodes without touching the surface
    from rdpy.protocol.rdp import rfx_progressive
    surf = zeros(64 * 64 * 4, uint8)
    body = pack('<BHBBBHI', 64, 0, 0, 0, 0, 0, 0)  # tileSize numRects numQuant numProgQuant flags numTiles tileDataSize
    block = pack('<HI', 0xCCC4, 6 + len(body)) + body
    rects = rfx_progressive.RfxProgressiveDecoder().decode(block, surf, 64, 64)
    if surf.any():
        errors.append(u'rfx_progressive wrote pixels for an empty region (%r)' % (rects,))
    _checkPyAV(errors)
    # device / channel modules: importable without their optional packages
    for name in DEVICE_MODULES:
        try:
            import_module(name)
        except Exception as e:  # a broken module must fail the check
            errors.append(u'%s: %r' % (name, e))
    if not errors:
        from rdpy.protocol.rdp.channels import rdpdr_smartcard
        if len(rdpdr_smartcard.IOCTL_NAMES) != 48:
            errors.append(u'smart card IOCTL table incomplete')
    return errors


def main(argv=None):
    argv = sys_argv[1:] if argv is None else argv
    print(u'RdpyManyQt self test, Python %s on %s' % (version.split()[0], platform))
    errors = run('--pure' in argv)
    if errors:
        print(u'FAILED:\n  ' + u'\n  '.join(errors))
        return 1
    print(u'OK')
    return 0


if __name__ == '__main__':
    exit(main())
