# coding=utf-8
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
RdpyManyQt build helper (local builds and releases).
    python tools/build.py version          print the version from pyproject.toml
    python tools/build.py wheel            for this machine (compiled decoders)
    python tools/build.py pure             pure-Python wheel (any platform, slow decoders)
    python tools/build.py sdist            source archive
    python tools/build.py cibw [PLATFORM]  cibuildwheel for this machine, or for
                                           android / ios (needs the matching SDK)
    python tools/build.py check [--pure]   check the installed wheel
    python tools/build.py release          tag v<version> on HEAD and force-push it: the
                                           GitHub workflow builds every platform and
                                           publishes (or replaces) the release "v<version>"
                                           (every push to main / master does that too)
Wheels go to ./dist (./wheelhouse for cibuildwheel).
"""
from __future__ import absolute_import, division, print_function
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def version():
    with open(os.path.join(ROOT, 'pyproject.toml')) as f:
        match = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
    if not match:
        sys.exit("version not found in pyproject.toml")
    return match.group(1)


def run(cmd, env=None):
    print('$ ' + ' '.join(cmd))
    subprocess.check_call(cmd, cwd=ROOT, env=dict(os.environ, **(env or {})))


def main(argv):
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(__doc__)
        return
    action, rest = argv[0], argv[1:]
    py = sys.executable
    if action == 'version':
        print(version())
    elif action == 'wheel':
        run([py, '-m', 'pip', 'wheel', '--no-deps', '-w', 'dist', '.'])
    elif action == 'pure':
        run([py, '-m', 'pip', 'wheel', '--no-deps', '-w', 'dist', '.'], {'NO_CYTHON': '1'})
    elif action == 'sdist':
        run([py, '-m', 'build', '--sdist', '--outdir', 'dist'])
    elif action == 'cibw':
        # tools/cibw.py: cibuildwheel that survives download outages (see its docstring)
        cmd = [py, os.path.join('tools', 'cibw.py'), '--output-dir', 'wheelhouse']
        if rest:
            cmd += ['--platform', rest[0]]
        run(cmd)
    elif action == 'check':
        run([py, os.path.join('tools', 'check_wheel.py')] + rest)
    elif action == 'release':
        tag = 'v' + version()
        if subprocess.call(['git', 'diff', '--quiet', 'HEAD'], cwd=ROOT) != 0:
            sys.exit("commit your changes before releasing")
        # the workflow may already have created the tag on an earlier commit: move it
        run(['git', 'tag', '--force', '-a', tag, '-m', 'RdpyManyQt ' + tag])
        run(['git', 'push', '--force', 'origin', 'refs/tags/' + tag])
        print("pushed %s: the GitHub workflow now builds and publishes the release" % tag)
    else:
        sys.exit("unknown action %r\n%s" % (action, __doc__))


if __name__ == '__main__':
    main(sys.argv[1:])
