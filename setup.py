# coding=utf-8
"""
Setup zone.
The Cython decoders (RLE, ZGFX, H.264 color conversion, RemoteFX) are needed
for real-time video. They are built unless NO_CYTHON is set; a missing build
dependency is reported instead of silently installing the slow pure Python fallbacks.
The decoders use typed memoryviews only (no NumPy C API), so building them needs
Cython and a C compiler but not NumPy: the same sources cross-compile for iOS and
Android, where no NumPy built for the target exists at build time.
"""
from __future__ import absolute_import, division, print_function
from setuptools import setup, Extension
from sys import platform, exit, stderr
from os import environ


def compileArgs():
    """
    Optimisation flags: MSVC already optimizes with /Ox, GCC / Clang (Linux, macOS, iOS, Android, MinGW) get -O3.
    """
    compiler = environ.get('CC', '')
    return [] if platform == 'win32' and 'gcc' not in compiler and 'clang' not in compiler else [u'-O3']


ext_modules = []
if environ.get('NO_CYTHON'):
    print(u'NO_CYTHON is set: installing the pure Python decoders (slow)', file=stderr)
else:
    try:
        from Cython.Build import cythonize
    except ImportError as e:
        exit(u'Cannot build the Cython decoders (%s).\n'
             u'Install the build dependency with \'python -m pip install Cython\' '
             u'or use \'python -m pip install .\', or set NO_CYTHON=1 to install the slow pure Python version.' % e)

    ext_modules = cythonize(
        [
            Extension(u'rdpy.core.rle', [u'rdpy/core/rle.pyx']),
            Extension(u'rdpy.protocol.rdp.rlgr1_decode', [u'rdpy/protocol/rdp/rlgr1_decode.pyx']),
            Extension(u'rdpy.protocol.rdp.zgfx', [u'rdpy/protocol/rdp/zgfx.pyx']),
            Extension(u'rdpy.protocol.rdp.avc', [u'rdpy/protocol/rdp/avc.pyx']),
            Extension(
                u'rdpy.protocol.rdp.rfx_progressive', [u'rdpy/protocol/rdp/rfx_progressive.pyx'],
                extra_compile_args=compileArgs(),
            ),
        ],
        compiler_directives={u'language_level': u'3', u'boundscheck': False, u'wraparound': False},
    )
setup(ext_modules=ext_modules)
