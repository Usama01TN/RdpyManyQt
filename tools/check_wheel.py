# coding=utf-8
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
Checks an installed RdpyManyQt wheel (run by cibuildwheel after each build, or by hand):
the compiled decoders are present (unless --pure) and decode correctly, NumPy and PyAV
(when installed) work. The checks are in the package (rdpy/selftest.py), so the iOS
simulator and the Android emulator run them as "python -m rdpy.selftest".
    python tools/check_wheel.py [--pure]
"""
from __future__ import absolute_import, division, print_function
from os.path import abspath, dirname
from sys import argv, exit, path
from os import curdir

if __name__ == '__main__':
    # tools/ is sys.path[0]: keep it out (tools/build.py would shadow the "build" package)
    HERE = dirname(abspath(__file__))
    path[:] = [p for p in path if abspath(p or curdir) != HERE]
    from rdpy import selftest

    exit(selftest.main(argv[1:]))
