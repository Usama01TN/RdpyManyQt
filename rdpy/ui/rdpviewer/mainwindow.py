# coding=utf-8
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
RdpViewerWidget demo: a connection form and the viewer.
"""
from __future__ import absolute_import, division, print_function
from os.path import dirname
from sys import path

if dirname(__file__) not in path:
    path.append(dirname(__file__))

from ManyQt.QtCore import pyqtSlot
from ManyQt.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, \
    QComboBox, QCheckBox, QLabel, QMessageBox, QFileDialog

try:
    from .rdpviewer import RdpViewerWidget
except:
    from rdpviewer import RdpViewerWidget


class MainWindow(QMainWindow):
    """
    MainWindow class.
    """

    def __init__(self, *args, **kwargs):
        """
        :param args: any
        :param kwargs: any
        """
        super(MainWindow, self).__init__(*args, **kwargs)
        self.setWindowTitle(self.tr('RdpViewer demo'))
        self.resize(1100, 760)
        self.__m_viewer = RdpViewerWidget(host='', port=3389, clipboardMode=RdpViewerWidget.ClipboardMode.Files,
                                          certificateMode=RdpViewerWidget.CertificateMode.Tofu,
                                          colorDepth=RdpViewerWidget.ColorDepth.Bpp32,
                                          keyboardLayout=RdpViewerWidget.KeyboardLayout.US,
                                          connected=self.onConnected,
                                          fileTransferStarted=self.onTransferStarted,
                                          fileTransferProgress=self.onTransferProgress,
                                          fileTransferFinished=self.onTransferFinished,
                                          driveFileTransferred=self.onDriveTransferred,
                                          disconnected=self.onDisconnected,
                                          connectionFailed=self.onFailed,
                                          loginScreenReached=self.onLoginScreen,
                                          desktopReached=self.onDesktop,
                                          logonFailed=self.onLogonFailed)  # type: RdpViewerWidget
        self.__m_host = QLineEdit(placeholderText=self.tr('host[:port]'))  # type: QLineEdit
        self.__m_user = QLineEdit(placeholderText=self.tr('user'))  # type: QLineEdit
        self.__m_pass = QLineEdit(placeholderText=self.tr('password'))  # type: QLineEdit
        self.__m_pass.setEchoMode(QLineEdit.EchoMode.Password if hasattr(QLineEdit, 'EchoMode') else QLineEdit.Password)
        self.__m_security = QComboBox()  # type: QComboBox
        for preset in (RdpViewerWidget.SecurityPreset.Default, RdpViewerWidget.SecurityPreset.Maximum,
                       RdpViewerWidget.SecurityPreset.Compatible):
            self.__m_security.addItem(preset.name, int(preset))
        self.__m_depth = QComboBox()  # type: QComboBox
        for depth in reversed(list(RdpViewerWidget.ColorDepth)):
            self.__m_depth.addItem('{} bits'.format(int(depth)), int(depth))
        self.__m_share = QPushButton(self.tr('Share folder...'))  # type: QPushButton
        self.__m_share.clicked.connect(self.onShareFolder)
        self.__m_send = QPushButton(self.tr('Send files...'))  # type: QPushButton
        self.__m_send.clicked.connect(self.onSendFiles)
        self.__m_mic = QCheckBox(self.tr('microphone'))  # type: QCheckBox
        self.__m_smart = QCheckBox(self.tr('smart sizing'))  # type: QCheckBox
        self.__m_smart.toggled.connect(self.__m_viewer.setSmartSizing)
        self.__m_button = QPushButton(self.tr('Connect'))  # type: QPushButton
        self.__m_button.clicked.connect(self.onConnectClicked)
        self.__m_status = QLabel(self.tr('idle'))  # type: QLabel
        self.__m_viewer.stateChanged.connect(
            lambda state: self.__m_status.setText(RdpViewerWidget.State(state).name))
        bar = QHBoxLayout()  # type: QHBoxLayout
        for w in (self.__m_host, self.__m_user, self.__m_pass, self.__m_security, self.__m_depth, self.__m_mic,
                  self.__m_smart,
                  self.__m_share, self.__m_send, self.__m_button, self.__m_status):
            bar.addWidget(w)
        wdg = QWidget()  # type: QWidget
        layout = QVBoxLayout(wdg)  # type: QVBoxLayout
        layout.addLayout(bar)
        layout.addWidget(self.__m_viewer, 1)
        self.setCentralWidget(wdg)

    @pyqtSlot()
    def onConnectClicked(self):
        """
        :return: None
        """
        viewer = self.__m_viewer  # type: RdpViewerWidget
        if viewer.getState() != RdpViewerWidget.State.Idle:
            viewer.disconnectFromHost()
            return
        host, _, port = self.__m_host.text().strip().partition(':')
        viewer.setHost(host)
        viewer.setPort(int(port) if port.isdigit() else 3389)
        viewer.setUsername(self.__m_user.text())
        viewer.setPassword(self.__m_pass.text())
        viewer.setSecurityPreset(self.__m_security.currentData())
        viewer.setColorDepth(self.__m_depth.currentData())
        viewer.setMicrophoneEnabled(self.__m_mic.isChecked())
        self.__m_button.setText(self.tr('Disconnect'))
        viewer.connectToHost()

    @pyqtSlot()
    def onShareFolder(self):
        """
        :return:
        """
        folder = QFileDialog.getExistingDirectory(self, self.tr('Folder to share with the session'))
        if folder:
            self.__m_viewer.addDrive(folder)
            self.__m_viewer.update()

    @pyqtSlot()
    def onSendFiles(self):
        """
        :return:
        """
        paths = QFileDialog.getOpenFileNames(self, self.tr('Files to paste in the session'))[0]
        if paths and self.__m_viewer.sendFiles(paths):
            self.__m_status.setText(self.tr('press Ctrl+V in the session'))

    def onTransferStarted(self, direction, items, total):
        """
        :param direction: int (RdpViewerWidget.TransferDirection)
        :param items: int
        :param total: int
        :return:
        """
        print('{} started: {} item(s), {} bytes'.format(
            RdpViewerWidget.TransferDirection(direction).name, items, total))

    def onTransferProgress(self, direction, done, total):
        """
        :param direction: int
        :param done: int
        :param total: int
        :return:
        """
        self.__m_status.setText('{} {}%'.format(RdpViewerWidget.TransferDirection(direction).name.lower(),
                                                int(100.0 * done / float(total)) if total else 100))

    def onTransferFinished(self, direction, paths, total, status):
        """
        :param direction: int
        :param paths: list[str]
        :param total: int
        :param status: int (RdpViewerWidget.TransferStatus)
        :return:
        """
        text = '{} {}: {} bytes'.format(RdpViewerWidget.TransferDirection(direction).name,
                                        RdpViewerWidget.TransferStatus(status).name.lower(), total)
        self.__m_status.setText(text)
        print(text, paths)

    def onDriveTransferred(self, direction, path, total):
        """
        :param direction: int
        :param path: str | unicode | QString
        :param total: int
        :return:
        """
        print(self.tr('Drive {}: {} ({} bytes)').format(RdpViewerWidget.TransferDirection(direction).name, path, total))

    @pyqtSlot()
    def onConnected(self):
        """
        :return:
        """
        print(self.tr('Connected'))
        print(self.__m_viewer.getSessionReport())

    @pyqtSlot()
    def onLoginScreen(self):
        """
        :return:
        """
        print(self.tr('Login screen displayed'))

    @pyqtSlot()
    def onDesktop(self):
        """
        :return:
        """
        print(self.tr('Logged on: desktop reached'))

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def onLogonFailed(self, message):
        """
        :param message: str | unicode | QString
        :return:
        """
        print(self.tr('Logon: {}').format(message))

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def onDisconnected(self, reason):
        """
        :param reason: str | unicode | QString
        :return:
        """
        self.__m_button.setText(self.tr('Connect'))
        print(self.tr('Disconnected: {}').format(reason))

    @pyqtSlot('QString')
    @pyqtSlot(str)
    def onFailed(self, reason):
        """
        :param reason: str | unicode | QString
        :return:
        """
        self.__m_button.setText(self.tr('Connect'))
        QMessageBox.warning(self, self.tr('Connection failed'), reason)
