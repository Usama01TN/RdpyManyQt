# coding=utf-8
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
Python 2 / 3 compatibility helpers: the code is written in a syntax both
interpreters accept, and the few standard library differences are hidden here.
"""
from __future__ import absolute_import, division, print_function

import binascii
import errno
import importlib
import os
import sys
import time
from datetime import timedelta, tzinfo

PY2 = sys.version_info[0] == 2

try:
    import queue  # noqa: F401  (Python 3)
except ImportError:  # Python 2
    import Queue as queue  # noqa: F401

# monotonic clock (time.monotonic is Python 3.3+)
monotonic = getattr(time, 'monotonic', None) or time.time

try:
    from datetime import timezone

    utc = timezone.utc
except ImportError:
    class _UTC(tzinfo):
        """UTC time zone (datetime.timezone.utc of Python 3)."""

        def utcoffset(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return 'UTC'

        def dst(self, dt):
            return timedelta(0)


    utc = _UTC()


def replace(src, dst):
    """os.replace (atomic rename that overwrites dst, Python 3.3+)."""
    if hasattr(os, 'replace'):
        os.replace(src, dst)
        return
    if os.name == 'nt' and os.path.exists(dst):
        os.remove(dst)
    os.rename(src, dst)


def makedirs(path):
    """os.makedirs(path, exist_ok=True)."""
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST or not os.path.isdir(path):
            raise


def isErrno(error, *codes):
    """
    @summary: True when an OSError / IOError has one of the errno codes (Python 2 has no
    FileNotFoundError, PermissionError, NotADirectoryError... subclasses)
    """
    return getattr(error, 'errno', None) in codes


def intFromBytes(data, byteorder):
    """int.from_bytes (Python 3.2+)."""
    data = bytearray(data)
    if byteorder == 'little':
        data.reverse()
    value = 0
    for b in data:
        value = (value << 8) | b
    return value


def intToBytes(value, length, byteorder):
    """int.to_bytes (Python 3.2+)."""
    out = bytearray(length)
    for i in range(length):
        out[length - 1 - i] = (value >> (8 * i)) & 0xFF
    if byteorder == 'little':
        out.reverse()
    return bytes(out)


def hexlify(data):
    """bytes.hex() (Python 3.5+)."""
    return binascii.hexlify(bytes(data)).decode('ascii')


class LazyModule(object):
    """
    @summary: Module imported on first attribute access. Breaks the circular
    imports Python 2 can not resolve (`from . import sec` while sec imports us).
    """

    def __init__(self, name):
        self.__dict__['_name'] = name
        self.__dict__['_module'] = None

    def __getattr__(self, attr):
        module = self.__dict__['_module']
        if module is None:
            module = importlib.import_module(self.__dict__['_name'])
            self.__dict__['_module'] = module
        return getattr(module, attr)


if PY2:
    class pybytes(str):
        """
        @summary: Python 2 str that indexes and iterates like Python 3 bytes
        (data[i] is an int), returned by Stream.read on Python 2 so the protocol
        parsers work unchanged.
        """

        def __getitem__(self, index):
            value = str.__getitem__(self, index)
            if isinstance(index, slice):
                return pybytes(value)
            return ord(value)

        def __getslice__(self, i, j):
            return pybytes(str.__getslice__(self, i, j))

        def __iter__(self):
            for ch in str.__iter__(self):
                yield ord(ch)
else:
    pybytes = bytes
