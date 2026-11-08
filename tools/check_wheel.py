# coding=utf-8
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
Checks an installed RdpyManyQt wheel (run by cibuildwheel after each build, or by hand):
the compiled decoders are present (unless --pure) and decode correctly.
    python tools/check_wheel.py [--pure]
"""
from __future__ import absolute_import, division, print_function
import importlib
import struct
import sys

COMPILED = ("rdpy.core.rle", "rdpy.protocol.rdp.zgfx", "rdpy.protocol.rdp.avc",
            "rdpy.protocol.rdp.rfx_progressive", "rdpy.protocol.rdp.rlgr1_decode")


def main():
    pure = '--pure' in sys.argv
    errors = []
    for name in COMPILED:
        mod = importlib.import_module(name)
        path = getattr(mod, '__file__', '') or ''
        compiled = not path.endswith('.py')
        print("%-36s %s" % (name, 'compiled' if compiled else 'pure Python'))
        if compiled == pure:
            errors.append("%s: expected %s" % (name, 'pure Python' if pure else 'a compiled module'))
    # planar bitmap: rows 0, 10, 20 (decoder keeps the encoded order)
    from rdpy.core import rle
    plane = bytes([0x40, 0, 0, 0, 0]) + bytes([0x13, 20]) * 2
    out = rle.bitmap_decompress4(bytes([0x30]) + plane * 3, 4, 3)
    if [out[r * 16] for r in range(3)] != [0, 10, 20]:
        errors.append("rle.bitmap_decompress4 returned wrong pixels")
    # zgfx: uncompressed single segment
    from rdpy.protocol.rdp import zgfx
    data = zgfx.ZgfxDecompressor().decompress_segment(b'\x04hello')
    if bytes(data) != b'hello':
        errors.append("zgfx returned %r" % (data,))
    # progressive RFX: a region block with no tile decodes without touching the surface
    from rdpy.protocol.rdp import rfx_progressive
    import numpy as np
    surf = np.zeros(64 * 64 * 4, np.uint8)
    body = struct.pack('<BHBBBHI', 64, 0, 0, 0, 0, 0, 0)  # tileSize numRects numQuant numProgQuant flags numTiles tileDataSize
    block = struct.pack('<HI', 0xCCC4, 6 + len(body)) + body
    rects = rfx_progressive.RfxProgressiveDecoder().decode(block, surf, 64, 64)
    if surf.any():
        errors.append("rfx_progressive wrote pixels for an empty region (%r)" % (rects,))
    if errors:
        print("FAILED:\n  " + "\n  ".join(errors))
        sys.exit(1)
    print("OK")


if __name__ == '__main__':
    main()
