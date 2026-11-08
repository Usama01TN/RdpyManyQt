# coding=utf-8
"""
Setup zone.
The Cython decoders (RLE, ZGFX, H.264 color conversion, RemoteFX) are needed
for real-time video. They are built unless NO_CYTHON is set; a missing build
dependency is reported instead of silently installing the slow pure Python fallbacks.
"""
from __future__ import absolute_import, division, print_function
from setuptools import setup, Extension
from os import environ
import sys
import os

def numpyInclude():
    """
    NumPy's C headers. When cross-compiling (iOS / Android wheels) the numpy
    installed in the build environment is built for the target and can not be
    imported on the build machine: find its headers without importing it.
    """
    try:
        import numpy
        return numpy.get_include()
    except ImportError:
        from importlib.util import find_spec
        spec = find_spec('numpy')
        if spec is None or not spec.submodule_search_locations:
            raise
        root = list(spec.submodule_search_locations)[0]
        for sub in (('_core', 'include'), ('core', 'include')):
            path = os.path.join(root, *sub)
            if os.path.isdir(os.path.join(path, 'numpy')):
                return path
        raise ImportError("numpy headers not found in %s" % root)


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

        numpy_include = numpyInclude()
    except ImportError as e:
        sys.exit("Cannot build the Cython decoders (%s).\n"
                 "Install the build dependencies with 'python -m pip install Cython numpy' "
                 "or use 'python -m pip install .', or set NO_CYTHON=1 to install the slow "
                 "pure Python version." % e)

    ext_modules = cythonize(
        [
            Extension("rdpy.core.rle", ["rdpy/core/rle.pyx"]),
            Extension(
                "rdpy.protocol.rdp.rlgr1_decode",
                ["rdpy/protocol/rdp/rlgr1_decode.pyx"],
                include_dirs=[numpy_include],
            ),
            Extension(
                "rdpy.protocol.rdp.zgfx",
                ["rdpy/protocol/rdp/zgfx.pyx"],
            ),
            Extension(
                "rdpy.protocol.rdp.avc",
                ["rdpy/protocol/rdp/avc.pyx"],
                include_dirs=[numpy_include],
            ),
            Extension(
                "rdpy.protocol.rdp.rfx_progressive",
                ["rdpy/protocol/rdp/rfx_progressive.pyx"],
                include_dirs=[numpy_include],
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
