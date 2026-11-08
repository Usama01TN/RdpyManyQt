# coding=utf-8
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
Entry point for the RdpViewerWidget demo.
"""
from __future__ import absolute_import, division, print_function
from os.path import dirname
from sys import path

if dirname(__file__) not in path:
    path.append(dirname(__file__))

if __name__ == '__main__':
    from ManyQt.QtWidgets import QApplication
    from sys import argv, exit

    a = QApplication(argv)  # type: QApplication
    # The Qt reactor must exist before Twisted is used anywhere.
    try:
        from .rdpviewer import ensureQtReactor
    except:
        from rdpviewer import ensureQtReactor
    ensureQtReactor()
    from twisted.internet import reactor

    try:
        from .mainwindow import MainWindow
    except:
        from mainwindow import MainWindow

    w = MainWindow()  # type: MainWindow
    w.show()
    reactor.runReturn()
    # Disconnect cleanly on quit, Ctrl+C, SIGTERM, logout
    from rdpy.ui.qt import installCleanExit

    installCleanExit(a)
    import rdpy

    print('RdpyManyQt {} ({})'.format(getattr(rdpy, '__version__', '?'), dirname(rdpy.__file__)))
    code = a.exec_() if hasattr(a, 'exec_') else getattr(a, 'exec')()  # type: int
    # sessions are disconnected by now: return to the console at once
    from rdpy.ui.qt import forceExit

    forceExit(code)
