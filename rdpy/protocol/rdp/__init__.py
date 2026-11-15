# coding=utf-8
"""
rdp initialization.
The virtual channels live in rdpy.protocol.rdp.channels. Their former names
(rdpy.protocol.rdp.cliprdr, ``from rdpy.protocol.rdp import rdpsnd``...) still
work: they import the very same module objects through the alias below.
"""
from __future__ import absolute_import, division, print_function
from importlib import import_module
from sys import meta_path, modules

_OLD = __name__ + '.'
_NEW = __name__ + '.channels.'
# kept here (not imported from .channels) so that the alias costs no import
_MOVED = frozenset((
    'ainput', 'assistance', 'audin', 'cliprdr', 'cliprdr_file', 'disp', 'drdynvc', 'echo', 'encomsp', 'geometry',
    'location', 'rail', 'rdp2tcp', 'rdpdr', 'rdpdr_drive', 'rdpdr_parallel', 'rdpdr_printer', 'rdpdr_serial',
    'rdpdr_smartcard', 'rdpecam', 'rdpei', 'rdpewa', 'rdpsnd', 'remdesk', 'sshagent', 'tsmf', 'urbdrc',
    'urbdrc_libusb', 'video',
))


class _ChannelAlias(object):
    """
    Import hook: rdpy.protocol.rdp.<channel> -> rdpy.protocol.rdp.channels.<channel>, the same
    module object (Python 3 find_spec / create_module, Python 2 find_module / load_module).
    """

    @staticmethod
    def _target(fullname):
        if fullname.startswith(_OLD) and fullname[len(_OLD):] in _MOVED:
            return _NEW + fullname[len(_OLD):]
        return None

    # Python 3.4+
    def find_spec(self, fullname, path=None, target=None):
        if self._target(fullname) is None:
            return None
        from importlib.util import spec_from_loader
        return spec_from_loader(fullname, self)

    def create_module(self, spec):
        return import_module(self._target(spec.name))

    def exec_module(self, module):
        pass

    # Python 2
    def find_module(self, fullname, path=None):
        return self if self._target(fullname) is not None else None

    def load_module(self, fullname):
        module = import_module(self._target(fullname))
        modules[fullname] = module
        return module


if not any(isinstance(f, _ChannelAlias) for f in meta_path):
    meta_path.append(_ChannelAlias())
