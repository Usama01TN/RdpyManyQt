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
from os import environ
import sys


def compileArgs():
    """Optimisation flags: MSVC already optimises with /Ox, GCC / Clang (Linux,
    macOS, iOS, Android, MinGW) get -O3."""
    compiler = environ.get('CC', '')
    if sys.platform == 'win32' and 'gcc' not in compiler and 'clang' not in compiler:
        return []
    return ["-O3"]


ext_modules = []
if environ.get('NO_CYTHON'):
    print("NO_CYTHON is set: installing the pure Python decoders (slow)", file=sys.stderr)
else:
    try:
        from Cython.Build import cythonize
    except ImportError as e:
        sys.exit("Cannot build the Cython decoders (%s).\n"
                 "Install the build dependency with 'python -m pip install Cython' "
                 "or use 'python -m pip install .', or set NO_CYTHON=1 to install the slow "
                 "pure Python version." % e)

    ext_modules = cythonize(
        [
            Extension("rdpy.core.rle", ["rdpy/core/rle.pyx"]),
            Extension("rdpy.protocol.rdp.rlgr1_decode", ["rdpy/protocol/rdp/rlgr1_decode.pyx"]),
            Extension("rdpy.protocol.rdp.zgfx", ["rdpy/protocol/rdp/zgfx.pyx"]),
            Extension("rdpy.protocol.rdp.avc", ["rdpy/protocol/rdp/avc.pyx"]),
            Extension(
                "rdpy.protocol.rdp.rfx_progressive",
                ["rdpy/protocol/rdp/rfx_progressive.pyx"],
                extra_compile_args=compileArgs(),
            ),
        ],
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": False,
        },
    )

setup(ext_modules=ext_modules)
