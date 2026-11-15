# coding=utf-8
"""
RDP virtual channels (static and dynamic), the client side of FreeRDP's
channels/ folder:
 static channels:  cliprdr (clipboard), rdpsnd (audio output), rdpdr (device
                   redirection: drives, printers, serial / parallel ports, smart
                   cards), rail (RemoteApp), drdynvc (dynamic channels and the
                   graphics pipeline), remdesk + encomsp (Remote Assistance),
                   rdp2tcp (TCP forwarding)
 dynamic channels: audin (microphone), disp (display control), urbdrc (USB),
                   ainput (advanced input), sshagent, rdpecam (cameras), echo,
                   location, rdpei (touch and pen), geometry + video (video
                   optimized remoting), rdpewa (WebAuthn), tsmf (multimedia)
Helpers: assistance (Remote Assistance invitations), cliprdr_file (clipboard
files), rdpdr_* (redirected devices), urbdrc_libusb (USB through libusb).
The modules were in rdpy.protocol.rdp before 3.7.0; those names still import
them (rdpy.protocol.rdp installs an alias).
"""
from __future__ import absolute_import, division, print_function

# Every module of this package (the aliases of rdpy.protocol.rdp use this list).
MODULES = (
    'ainput', 'assistance', 'audin', 'cliprdr', 'cliprdr_file', 'disp', 'drdynvc', 'echo', 'encomsp', 'geometry',
    'location', 'rail', 'rdp2tcp', 'rdpdr', 'rdpdr_drive', 'rdpdr_parallel', 'rdpdr_printer', 'rdpdr_serial',
    'rdpdr_smartcard', 'rdpecam', 'rdpei', 'rdpewa', 'rdpsnd', 'remdesk', 'sshagent', 'tsmf', 'urbdrc',
    'urbdrc_libusb', 'video',
)
