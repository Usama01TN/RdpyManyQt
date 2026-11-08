# coding=utf-8
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Port of FreeRDP's interleaved RLE decoder (libfreerdp/codec/interleaved.c and
# libfreerdp/codec/include/bitmap.h, Apache License 2.0, Copyright the FreeRDP
# contributors), MS-RDPBCGR 2.2.9.1.1.3.1.2.4 "RLE Compressed Bitmap Stream".
#
"""
Interleaved RLE bitmap decoder for 8, 15, 16 and 24 bits per pixel, the legacy
(non graphics pipeline) bitmap updates Windows sends below 32 bits per pixel.
It follows FreeRDP's RLEDECOMPRESS order by order (same run lengths, same
foreground pel insertion, same first-line rules) so every bitmap FreeRDP draws
is drawn identically. The result is returned top-down (FreeRDP copies the
decoded bottom-up buffer with FREERDP_FLIP_VERTICAL).
"""
from __future__ import absolute_import, division, print_function

import array

REGULAR_BG_RUN = 0x00
MEGA_MEGA_BG_RUN = 0xF0
REGULAR_FG_RUN = 0x01
MEGA_MEGA_FG_RUN = 0xF1
LITE_SET_FG_FG_RUN = 0x0C
MEGA_MEGA_SET_FG_RUN = 0xF6
LITE_DITHERED_RUN = 0x0E
MEGA_MEGA_DITHERED_RUN = 0xF8
REGULAR_COLOR_RUN = 0x03
MEGA_MEGA_COLOR_RUN = 0xF3
REGULAR_FGBG_IMAGE = 0x02
MEGA_MEGA_FGBG_IMAGE = 0xF2
LITE_SET_FG_FGBG_IMAGE = 0x0D
MEGA_MEGA_SET_FGBG_IMAGE = 0xF7
REGULAR_COLOR_IMAGE = 0x04
MEGA_MEGA_COLOR_IMAGE = 0xF4
SPECIAL_FGBG_1 = 0xF9
SPECIAL_FGBG_2 = 0xFA
SPECIAL_WHITE = 0xFD
SPECIAL_BLACK = 0xFE

MASK_SPECIAL_FGBG_1 = 0x03
MASK_SPECIAL_FGBG_2 = 0x05

_MEGA = (MEGA_MEGA_BG_RUN, MEGA_MEGA_FG_RUN, MEGA_MEGA_SET_FG_RUN, MEGA_MEGA_DITHERED_RUN,
         MEGA_MEGA_COLOR_RUN, MEGA_MEGA_FGBG_IMAGE, MEGA_MEGA_SET_FGBG_IMAGE, MEGA_MEGA_COLOR_IMAGE)


class RleError(Exception):
    """The stream is invalid (FreeRDP's decoder returns FALSE)."""


def _codeId(header):
    """FreeRDP ExtractCodeId."""
    if (header & 0xC0) != 0xC0:
        return header >> 5          # regular orders
    if (header & 0xF0) == 0xF0:
        return header               # mega-mega and special orders
    return header >> 4              # lite orders


def _runLength(code, src, s, end):
    """
    FreeRDP ExtractRunLength.
    @return: (runLength, advance); advance 0 = invalid
    """
    header = src[s]
    if code == REGULAR_FGBG_IMAGE:
        run = header & 0x1F
        if run == 0:
            if s + 2 > end:
                return 0, 0
            return src[s + 1] + 1, 2
        return run * 8, 1
    if code == LITE_SET_FG_FGBG_IMAGE:
        run = header & 0x0F
        if run == 0:
            if s + 2 > end:
                return 0, 0
            return src[s + 1] + 1, 2
        return run * 8, 1
    if code in (REGULAR_BG_RUN, REGULAR_FG_RUN, REGULAR_COLOR_RUN, REGULAR_COLOR_IMAGE):
        run = header & 0x1F
        if run == 0:
            if s + 2 > end:
                return 0, 0
            return src[s + 1] + 32, 2
        return run, 1
    if code in (LITE_SET_FG_FG_RUN, LITE_DITHERED_RUN):
        run = header & 0x0F
        if run == 0:
            if s + 2 > end:
                return 0, 0
            return src[s + 1] + 16, 2
        return run, 1
    if code in _MEGA:
        if s + 3 > end:
            return 0, 0
        return src[s + 1] | (src[s + 2] << 8), 3
    return 0, 0


def rleDecompress(data, width, height, bytesPerPixel):
    """
    @summary: FreeRDP RLEDECOMPRESS (RleDecompress8to8 / 16to16 / 24to24)
    @param data: {bytes} RLE stream (without the TS_CD_HEADER)
    @param bytesPerPixel: {int} 1 (8 bpp), 2 (15 / 16 bpp) or 3 (24 bpp)
    @return: {array.array} pixel values ('I'), bottom-up as decoded
    @raise RleError: invalid stream (FreeRDP draws nothing in that case)
    """
    src = bytearray(data)
    end = len(src)
    rowDelta = width
    total = width * height
    out = array.array('I', [0]) * total
    white = (1 << (8 * bytesPerPixel)) - 1
    size = bytesPerPixel
    d = 0
    s = 0
    fgPel = white
    insertFgPel = False
    firstLine = True

    while s < end:
        if firstLine and d >= rowDelta:
            firstLine = False
            insertFgPel = False

        code = _codeId(src[s])

        # background runs
        if code == REGULAR_BG_RUN or code == MEGA_MEGA_BG_RUN:
            run, advance = _runLength(code, src, s, end)
            if advance == 0:
                raise RleError("background run")
            s += advance
            if firstLine:
                if insertFgPel:
                    if d + 1 > total:
                        raise RleError("overflow")
                    out[d] = fgPel
                    d += 1
                    run -= 1
                if run < 0 or d + run > total:
                    raise RleError("overflow")
                d += run                     # black pixels (buffer is zeroed)
            else:
                if insertFgPel:
                    if d + 1 > total:
                        raise RleError("overflow")
                    out[d] = out[d - rowDelta] ^ fgPel
                    d += 1
                    run -= 1
                if run < 0 or d + run > total:
                    raise RleError("overflow")
                while run > 0:
                    n = run if run < rowDelta else rowDelta   # never copies a pixel not yet written
                    out[d:d + n] = out[d - rowDelta:d - rowDelta + n]
                    d += n
                    run -= n
            insertFgPel = True
            continue

        insertFgPel = False

        if code in (REGULAR_FG_RUN, MEGA_MEGA_FG_RUN, LITE_SET_FG_FG_RUN, MEGA_MEGA_SET_FG_RUN):
            run, advance = _runLength(code, src, s, end)
            if advance == 0:
                raise RleError("foreground run")
            s += advance
            if code == LITE_SET_FG_FG_RUN or code == MEGA_MEGA_SET_FG_RUN:
                if s + size > end:
                    raise RleError("truncated")
                fgPel = _readPixel(src, s, size)
                s += size
            if d + run > total:
                raise RleError("overflow")
            if firstLine:
                out[d:d + run] = array.array('I', [fgPel]) * run
                d += run
            else:
                for _ in range(run):
                    out[d] = out[d - rowDelta] ^ fgPel
                    d += 1

        elif code == LITE_DITHERED_RUN or code == MEGA_MEGA_DITHERED_RUN:
            run, advance = _runLength(code, src, s, end)
            if advance == 0:
                raise RleError("dithered run")
            s += advance
            if s + 2 * size > end:
                raise RleError("truncated")
            pixelA = _readPixel(src, s, size)
            pixelB = _readPixel(src, s + size, size)
            s += 2 * size
            if d + 2 * run > total:
                raise RleError("overflow")
            out[d:d + 2 * run] = array.array('I', [pixelA, pixelB]) * run
            d += 2 * run

        elif code == REGULAR_COLOR_RUN or code == MEGA_MEGA_COLOR_RUN:
            run, advance = _runLength(code, src, s, end)
            if advance == 0:
                raise RleError("colour run")
            s += advance
            if s + size > end:
                raise RleError("truncated")
            pixelA = _readPixel(src, s, size)
            s += size
            if d + run > total:
                raise RleError("overflow")
            out[d:d + run] = array.array('I', [pixelA]) * run
            d += run

        elif code in (REGULAR_FGBG_IMAGE, MEGA_MEGA_FGBG_IMAGE, LITE_SET_FG_FGBG_IMAGE, MEGA_MEGA_SET_FGBG_IMAGE):
            run, advance = _runLength(code, src, s, end)
            if advance == 0:
                raise RleError("fg/bg image")
            s += advance
            if code == LITE_SET_FG_FGBG_IMAGE or code == MEGA_MEGA_SET_FGBG_IMAGE:
                if s + size > end:
                    raise RleError("truncated")
                fgPel = _readPixel(src, s, size)
                s += size
            if s + run // 8 > end:
                raise RleError("truncated")
            while run > 8:
                bitmask = src[s]
                s += 1
                d = _fgbg(out, d, total, rowDelta, bitmask, fgPel, 8, firstLine)
                run -= 8
            if run > 0:
                if s + 1 > end:
                    raise RleError("truncated")
                bitmask = src[s]
                s += 1
                d = _fgbg(out, d, total, rowDelta, bitmask, fgPel, run, firstLine)

        elif code == REGULAR_COLOR_IMAGE or code == MEGA_MEGA_COLOR_IMAGE:
            run, advance = _runLength(code, src, s, end)
            if advance == 0:
                raise RleError("colour image")
            s += advance
            if d + run > total or s + run * size > end:
                raise RleError("overflow")
            if size == 1:
                out[d:d + run] = array.array('I', src[s:s + run])
            elif size == 2:
                out[d:d + run] = array.array('I', [src[i] | (src[i + 1] << 8) for i in range(s, s + 2 * run, 2)])
            else:
                out[d:d + run] = array.array('I', [src[i] | (src[i + 1] << 8) | (src[i + 2] << 16)
                                                  for i in range(s, s + 3 * run, 3)])
            s += run * size
            d += run

        elif code == SPECIAL_FGBG_1 or code == SPECIAL_FGBG_2:
            if s + 1 > end:
                raise RleError("truncated")
            s += 1
            mask = MASK_SPECIAL_FGBG_1 if code == SPECIAL_FGBG_1 else MASK_SPECIAL_FGBG_2
            d = _fgbg(out, d, total, rowDelta, mask, fgPel, 8, firstLine)

        elif code == SPECIAL_WHITE or code == SPECIAL_BLACK:
            if s + 1 > end:
                raise RleError("truncated")
            s += 1
            if d + 1 > total:
                raise RleError("overflow")
            out[d] = white if code == SPECIAL_WHITE else 0
            d += 1

        else:
            raise RleError("invalid code 0x%02x" % code)

    return out


def _readPixel(src, s, size):
    """SRCREADPIXEL: little endian pixel of 1, 2 or 3 bytes."""
    if size == 2:
        return src[s] | (src[s + 1] << 8)
    if size == 3:
        return src[s] | (src[s + 1] << 8) | (src[s + 2] << 16)
    return src[s]


def _fgbg(out, d, total, rowDelta, bitmask, fgPel, cBits, firstLine):
    """WRITEFGBGIMAGE / WRITEFIRSTLINEFGBGIMAGE."""
    if cBits > 8 or d + cBits > total:
        raise RleError("overflow")
    mask = 0x01
    for _ in range(cBits):
        if firstLine:
            out[d] = fgPel if bitmask & mask else 0
        else:
            above = out[d - rowDelta]
            out[d] = above ^ fgPel if bitmask & mask else above
        d += 1
        mask = (mask << 1) & 0xFF
    return d


def decompress(data, width, height, bytesPerPixel):
    """
    @summary: Decode an interleaved RLE bitmap
    @return: {bytes} top-down rows of width * bytesPerPixel bytes (little endian
    pixels: BGR for 24 bpp, RGB565 / RGB555 for 16 / 15 bpp)
    @raise RleError: invalid stream
    """
    import numpy as np
    pixels = np.frombuffer(rleDecompress(data, width, height, bytesPerPixel), dtype=np.uint32)
    if bytesPerPixel == 1:
        rows = pixels.astype('<u1').reshape(height, width)
    elif bytesPerPixel == 2:
        rows = pixels.astype('<u2').reshape(height, width)
    else:
        rows = pixels.astype('<u4').view(np.uint8).reshape(height, width, 4)[:, :, :3]
    # bottom-up -> top-down (FREERDP_FLIP_VERTICAL)
    return np.ascontiguousarray(rows[::-1]).tobytes()
