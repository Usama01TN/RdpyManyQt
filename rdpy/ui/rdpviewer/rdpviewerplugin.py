# coding=utf-8
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
Qt Designer plugin for RdpViewerWidget.
Point Qt Designer at this folder and start it:
    set PYQTDESIGNERPATH=<site-packages>\\rdpy\\ui\\rdpviewer      (Windows)
    export PYQTDESIGNERPATH=<site-packages>/rdpy/ui/rdpviewer     (Linux / macOS)
The widget appears in the "RdpyManyQt" group of the widget box; its connection,
security and feature options are editable in the property editor. Designer
needs the Python plugin loader (pyqt5-tools / pyqt6-tools, or PySide's designer).
"""
from __future__ import absolute_import, division, print_function
from os.path import dirname, join
from sys import path

if dirname(__file__) not in path:
    path.append(dirname(__file__))

from ManyQt.QtDesigner import QPyDesignerCustomWidgetPlugin
from ManyQt.QtGui import QIcon

try:
    from .rdpviewer import RdpViewerWidget
except:
    from rdpviewer import RdpViewerWidget


class RdpViewerPlugin(QPyDesignerCustomWidgetPlugin):
    """
    RdpViewerPlugin class: exposes RdpViewerWidget to Qt Designer.
    """

    def __init__(self, *args, **kwargs):
        """
        :param args: any
        :param kwargs: any
        """
        super(RdpViewerPlugin, self).__init__(*args, **kwargs)
        self.__m_initialized = False  # type: bool

    def initialize(self, core):
        """
        :param core: QDesignerFormEditorInterface
        :return:
        """
        if not self.__m_initialized:
            self.__m_initialized = True  # type: bool

    def isInitialized(self):
        """
        :return: bool
        """
        return self.__m_initialized

    def createWidget(self, parent):
        """
        In Designer the widget never connects: it only shows its preview.
        :param parent: QWidget
        :return: RdpViewerWidget
        """
        return RdpViewerWidget(parent, host='rdp.example.com')

    def name(self):
        """
        :return: str | unicode | QString
        """
        return 'RdpViewerWidget'

    def group(self):
        """
        :return: str | unicode | QString
        """
        return 'RdpyManyQt'

    def icon(self):
        """
        :return: QIcon
        """
        return QIcon(join(dirname(__file__), 'images', 'logo-64.png'))

    def toolTip(self):
        """
        :return: str | unicode | QString
        """
        return 'Remote Desktop (RDP) viewer'

    def whatsThis(self):
        """
        :return: str | unicode | QString
        """
        return 'Embeddable RDP client: set host, credentials, security and features, then call ' \
               'connectToHost(). Signals: connected, disconnected, connectionFailed, stateChanged.'

    def isContainer(self):
        """
        :return: bool
        """
        return False

    def includeFile(self):
        """
        Module uic-generated code imports the widget from.
        :return: str | unicode | QString
        """
        return 'rdpy.ui.rdpviewer'

    def domXml(self):
        """
        :return: str | unicode | QString
        """
        return ('<widget class="RdpViewerWidget" name="rdpViewer">\n'
                ' <property name="toolTip"><string>Remote Desktop (RDP) viewer</string></property>\n'
                ' <property name="geometry"><rect><x>0</x><y>0</y><width>640</width><height>400</height></rect>'
                '</property>\n'
                '</widget>\n')
