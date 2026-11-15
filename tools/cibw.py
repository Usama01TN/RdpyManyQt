# coding=utf-8
#
# Copyright (c) 2026 UsamaTN <ininou.oussematn@gmail.com>
# This file is part of RdpyManyQt (a fork of rdpyqt / rdpy).
# SPDX-License-Identifier: GPL-3.0-or-later
#
"""
Runs cibuildwheel (same arguments) so that a network outage does not fail the build.
    python tools/cibw.py --output-dir wheelhouse --platform ios
cibuildwheel downloads virtualenv, the Python installers, the iOS / Android Pythons, NuGet...
and gives up after about 90 seconds (6 tries), so a GitHub outage of a few minutes
("HTTP Error 504: Gateway Time-out" on github.com/.../releases/download/...) fails the build.
This wrapper:
* retries every cibuildwheel download for up to RDPY_CIBW_DOWNLOAD_PATIENCE seconds
  (default 900), alternating with mirrors that serve the same files when the original is a
  GitHub release:
    - virtualenv.pyz: the same file committed in the get-virtualenv repository
      (raw.githubusercontent.com, a different service from release downloads);
    - python-build-standalone (Android build Python): Astral's official mirror;
  the SHA-256 that cibuildwheel expects is still checked, whichever server answered;
* restarts the whole run, up to RDPY_CIBW_ATTEMPTS times (default 3), when it fails with a
  network error anywhere else (pip, NuGet, Docker...).
Downloads go to cibuildwheel's cache (CIBW_CACHE_PATH), which the GitHub workflow keeps
between runs, so a run normally downloads nothing at all.
iOS / Android wheels are tested in the simulator / emulator ("python -m rdpy.selftest").
PyPI has no iOS / Android wheels of NumPy (required) or PyAV (optional, H.264), but the
mobile package index of pyproject.toml (PIP_EXTRA_INDEX_URL, https://pypi.flet.dev) has
them for some Python versions. Before building, this wrapper asks pip which ones (a dry-run
install of the dependencies for each tested Python version and platform tag), then:
* installs PyAV in the tests of the Python versions it exists for (CIBW_TEST_REQUIRES);
* skips the tests of the Python versions whose dependencies (NumPy...) have no wheel there
  yet, or when the index can not be reached (CIBW_TEST_SKIP): their wheels are still built;
* with RDPY_MOBILE_TESTS=auto (default), when a mobile test fails (simulator or emulator
  that does not start...), builds the wheels again without tests and reports it as a
  warning; "required" makes a failed mobile test fail the build, "off" skips them.
New Python versions are tested as soon as the index publishes their wheels.
If a future cibuildwheel no longer has the download function patched here, cibuildwheel runs
unchanged (only the restart on network errors remains).
"""
from __future__ import absolute_import, division, print_function
from subprocess import PIPE, Popen, run, STDOUT, SubprocessError, TimeoutExpired
from platform import machine as platform_module_machine
from re import compile, I, match as re_match, search
from os.path import abspath, dirname, exists, join
from os import curdir, environ, replace, unlink
from ssl import create_default_context
from http.client import HTTPException
from importlib import import_module
from time import monotonic, sleep
from tempfile import gettempdir
from hashlib import file_digest
from shutil import copyfileobj
from shlex import quote, split
from collections import deque
from fnmatch import fnmatch
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError

CHILD_FLAG = u'RDPY_CIBW_CHILD'
PATIENCE = float(environ.get(u'RDPY_CIBW_DOWNLOAD_PATIENCE', u'900'))
ATTEMPTS = max(1, int(environ.get(u'RDPY_CIBW_ATTEMPTS', u'3')))
RESTART_PAUSES = (60, 300)  # seconds before the 2nd, 3rd... run

# (GitHub release URL, mirror serving the same file)
MIRRORS = (
    (compile(r'https://github\.com/pypa/get-virtualenv/releases/download/([^/]+)/virtualenv\.pyz'),
     r'https://raw.githubusercontent.com/pypa/get-virtualenv/\1/public/virtualenv.pyz'),
    (compile(r'https://github\.com/astral-sh/python-build-standalone/releases/download/(.+)'),
     r'https://releases.astral.sh/github/python-build-standalone/releases/download/\1'),
)
# a failed run whose output ends with one of these is restarted
NETWORK_ERROR = compile(u'|'.join((
    r'HTTP Error (?:408|429|5\d\d)', r'Gateway Time-?out', r'Bad Gateway', r'Service Unavailable',
    r'Too Many Requests', r'toomanyrequests', r'urlopen error', r'Max retries exceeded',
    r'Could not fetch URL', r'Connection (?:reset|refused|aborted|timed out)',
    r'RemoteDisconnected', r'IncompleteRead', r'Read ?Timeout', r'Connect ?Timeout',
    r'timed out', r'TLS handshake timeout', r'i/o timeout',
    r'Temporary failure in name resolution', r'Name or service not known',
    r'nodename nor servname', r'getaddrinfo failed', r'Could not resolve host',
    r'unexpected EOF', r'error pulling image', r'failed to resolve reference',
    r'Unable to load the service index',
)), I)


def log(message, level=None):
    """Prints a message, as a GitHub Actions annotation when running there."""
    if level and environ.get(u'GITHUB_ACTIONS') == u'true':
        print(u'::%s::%s' % (level, message), flush=True)
    else:
        print((u'%s: ' % level if level else u'') + message, flush=True)


# ----------------------------------------------------------------------------------------
# patient downloads (run inside the cibuildwheel process)
# ----------------------------------------------------------------------------------------

def candidates(url):
    """The URL followed by its mirrors."""
    return [url] + [m.sub(mirror, url) for m, mirror in MIRRORS if m.fullmatch(url)]


def user_agent():
    # some CDNs (releases.astral.sh) refuse urllib's default "Python-urllib/3.x"
    try:
        from cibuildwheel import __version__
        version = __version__
    except:
        version = u'unknown'
    return u'cibuildwheel/%s (RdpyManyQt tools/cibw.py)' % version


def fetch(url, dest, sha256, ca_file):
    """One download attempt; raises ValueError when the file is not the expected one."""
    context = create_default_context(cafile=ca_file)
    part = dest.with_name(dest.name + u'.part')
    request = Request(url, headers={u'User-Agent': user_agent()})
    try:
        with urlopen(request, context=context, timeout=120) as response, \
                open(part, u'wb') as out:
            copyfileobj(response, out, 1 << 20)
        if sha256:
            with open(part, u'rb') as f:
                digest = file_digest(f, u'sha256').hexdigest()
            if digest != sha256:
                raise ValueError(u'SHA256 mismatch: expected %s, got %s' % (sha256, digest))
        replace(part, dest)
    finally:
        if exists(part):
            unlink(part)


def make_patient_download(cibw_file):
    """A replacement for cibuildwheel.util.file.download(url, dest, *, sha256=None)."""
    ca_bundle = getattr(cibw_file, u'ca_bundle_path', lambda: None)
    fatal = getattr(cibw_file, u'FatalError', RuntimeError)

    def download(url, dest, *, sha256=None):
        print(u'+ Download %s to %s' % (url, dest), flush=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        urls = candidates(url)
        deadline = monotonic() + PATIENCE
        pause, last_error = 5, None
        while True:
            client_errors = 0
            for candidate in list(urls):
                try:
                    fetch(candidate, dest, sha256, ca_bundle())
                    if candidate != url:
                        print(u'  downloaded from mirror %s' % candidate, flush=True)
                    return
                except ValueError as e:  # wrong file
                    if candidate == url:
                        raise fatal(u'%s for %s' % (e, url))
                    print(u'  mirror %s: %s (not used)' % (candidate, e), flush=True)
                    urls.remove(candidate)
                    continue
                except HTTPError as e:
                    last_error = e
                    if 400 <= e.code < 500 and e.code not in (408, 429):
                        client_errors += 1  # will not fix itself (bad URL)
                except (OSError, HTTPException) as e:  # network, timeout, TLS...
                    last_error = e
                print(u'  %s: %s' % (candidate, last_error), flush=True)
            if client_errors == len(urls) or monotonic() + pause > deadline:
                raise last_error
            print(u'  retrying in %d s (gives up %d s from now)'
                  % (pause, deadline - monotonic()), flush=True)
            sleep(pause)
            pause = min(pause * 2, 120)

    return download


def install_patient_downloads():
    """Replaces cibuildwheel's download function in every cibuildwheel module.
    Returns False when this cibuildwheel has no such function (left unchanged)."""
    from cibuildwheel.util import file as cibw_file
    original = getattr(cibw_file, u'download', None)
    if not callable(original):
        log(u'cibuildwheel.util.file.download not found: downloads are not made patient',
            u'warning')
        return False
    patient = make_patient_download(cibw_file)
    cibw_file.download = patient
    import_module(u'cibuildwheel.__main__')  # imports every platform module
    for name, module in list(sys.modules.items()):
        if name.startswith(u'cibuildwheel') and getattr(module, u'download', None) is original:
            module.download = patient
    return True


def run_cibuildwheel(args):
    install_patient_downloads()
    from cibuildwheel.__main__ import main as cibw_main
    sys.argv = [u'cibuildwheel'] + list(args)
    cibw_main()


# ----------------------------------------------------------------------------------------
# iOS / Android tests: dependencies from the mobile package index (parent process)
# ----------------------------------------------------------------------------------------

MOBILE_PLATFORMS = (u'ios', u'android')
MOBILE_TESTS = environ.get(u'RDPY_MOBILE_TESTS', u'auto').strip().lower()  # auto|required|off
DEFAULT_MOBILE_INDEX = u'https://pypi.flet.dev'
OPTIONAL_MOBILE_TEST_REQUIRES = (u'av',)  # PyAV: H.264 decoding, tested where it exists
# pip could not find a wheel (as opposed to an index that can not be reached)
NOT_AVAILABLE = compile(
    r'Could not find a version that satisfies|No matching distribution|ResolutionImpossible|conflicting dependencies', I
)
TEST_STEPS = (u'Testing wheel...', u'Setting up test harness...', u'Installing test requirements...',
              u'Running test suite...')
BUILD_STEPS = (u'Building wheel...', u'Repairing wheel...', u'Creating cross build environment...',
               u'Setting up build environment...')


def target_platform(args):
    """The --platform given to cibuildwheel (or CIBW_PLATFORM)."""
    for i, arg in enumerate(args):
        if arg == u'--platform' and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(u'--platform='):
            return arg.split(u'=', 1)[1]
    return environ.get(u'CIBW_PLATFORM', u'auto')


def load_pyproject():
    try:
        from tomllib import load
        with open(u'pyproject.toml', u'rb') as f:
            return load(f)
    except:
        return {}


def cibw_option(config, platform, name, per_platform=True):
    """An option as cibuildwheel sees it: environment first, then pyproject.toml."""
    key = u'CIBW_' + name.upper().replace(u'-', u'_')
    for env in ((key + u'_' + platform.upper(),) if per_platform else ()) + (key,):
        if env in environ:
            return environ[env]
    tool = config.get(u'tool', {}).get(u'cibuildwheel', {})
    if per_platform and name in tool.get(platform, {}):
        return tool[platform][name]
    return tool.get(name)


def as_list(value):
    if not value:
        return []
    return split(value) if isinstance(value, str) else list(value)


def mobile_index(config, platform):
    """PIP_EXTRA_INDEX_URL of the mobile tests (test-environment on iOS; on Android the
    test requirements are installed with the build environment)."""
    value = cibw_option(config, platform, u'test-environment' if platform == u'ios' else u'environment')
    if isinstance(value, dict):
        return value.get(u'PIP_EXTRA_INDEX_URL') or DEFAULT_MOBILE_INDEX
    match = search(r'PIP_EXTRA_INDEX_URL=(\S+)', value or u'')
    return match.group(1).strip(u'"\'') if match else DEFAULT_MOBILE_INDEX


def native_test_arch(platform):
    """Architecture whose wheels cibuildwheel can test on this machine."""
    machine = platform_module_machine().lower()
    if platform == u'ios':
        return {u'arm64': u'arm64', u'x86_64': u'x86_64'}.get(machine)
    return {u'x86_64': u'x86_64', u'amd64': u'x86_64', u'arm64': u'arm64_v8a', u'aarch64': u'arm64_v8a'}.get(machine)


def tested_identifier(platform, identifier):
    """(python version, architecture, pip platform tags) of a wheel tested here, or None."""
    if platform == u'ios':
        match = re_match(r'cp(\d)(\d+)-ios_(.+)_(iphoneos|iphonesimulator)$', identifier)
        if not match or match.group(4) != u'iphonesimulator' or match.group(3) != native_test_arch(platform):
            return None  # devices and foreign simulators are not tested by cibuildwheel
        target = environ.get(u'IPHONEOS_DEPLOYMENT_TARGET', u'13.0').replace(u'.', u'_')
        tags = [u'ios_%s_%s_%s' % (target, match.group(3), match.group(4))]
    else:
        match = re_match(r'cp(\d)(\d+)-android_(.+)$', identifier)
        if not match or match.group(3) != native_test_arch(platform):
            return None  # only the build machine's architecture runs in the emulator
        api = int(environ.get(u'ANDROID_API_LEVEL', u'24'))
        tags = [u'android_%d_%s' % (level, match.group(3)) for level in range(api, 20, -1)]
    return u'%s.%s' % (match.group(1), match.group(2)), match.group(3), tags


def mobile_requirements(config, platform, arch, version):
    """The dependencies of the project for this platform (markers evaluated for it)."""
    try:
        from packaging.requirements import Requirement
    except:
        from pip._vendor.packaging.requirements import Requirement
    environment = {u'sys_platform': platform, u'platform_machine': arch, u'os_name': u'posix',
                   u'platform_system': {u'ios': u'iOS', u'android': u'Android'}[platform],
                   u'python_version': version, u'python_full_version': version + u'.0',
                   u'implementation_name': u'cpython', u'platform_python_implementation': u'CPython'}
    requirements = []
    for text in config.get(u'project', {}).get(u'dependencies', []):
        requirement = Requirement(text)
        if requirement.marker is None or requirement.marker.evaluate(environment):
            requirement.marker = None
            requirements.append(str(requirement))
    return requirements


def pip_can_install(version, tags, requirements, index):
    """'ok', 'missing' (no wheel) or 'unknown' (index not reachable...), and pip's message."""
    command = [sys.executable, u'-m', u'pip', u'install', u'--dry-run', u'--quiet', u'--ignore-installed',
               u'--disable-pip-version-check', u'--only-binary=:all:', u'--python-version', version,
               u'--implementation', u'cp', u'--abi', u'cp' + version.replace(u'.', u''),
               u'--target', join(gettempdir(), u'rdpy-cibw-probe'),
               u'--extra-index-url', index, u'--retries', u'3', u'--timeout', u'30']
    for tag in tags:
        command += [u'--platform', tag]
    try:
        result = run(command + list(requirements), stdout=PIPE, stderr=STDOUT, timeout=600)
    except (OSError, TimeoutExpired) as e:
        return u'unknown', str(e)
    output = result.stdout.decode(u'utf-8', u'replace')
    if result.returncode == 0:
        return u'ok', u''
    lines = [line for line in output.splitlines() if u'ERROR' in line] or output.splitlines()[-1:]
    message = lines[0].strip() if lines else u'pip failed'
    return (u'missing' if NOT_AVAILABLE.search(output) else u'unknown'), message


def build_identifiers(args):
    command = [sys.executable, u'-m', u'cibuildwheel'] + list(args) + [u'--print-build-identifiers']
    try:
        output = run(command, stdout=PIPE, stderr=PIPE, timeout=300, check=True).stdout.decode()
    except (OSError, SubprocessError) as e:
        log(u'cannot list the wheels to build (%s): mobile test dependencies not checked' % e,
            u'warning')
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def prepare_mobile_tests(args):
    """
    Environment for cibuildwheel deciding, per Python version, whether the iOS / Android
    wheels can be tested (their dependencies have wheels on the mobile package index) and
    whether PyAV is installed for the test. Empty for the other platforms.
    """
    platform = target_platform(args)
    if platform not in MOBILE_PLATFORMS:
        return {}
    config = load_pyproject()
    if not cibw_option(config, platform, u'test-command'):
        return {}
    skip = as_list(cibw_option(config, platform, u'test-skip', per_platform=False))
    if MOBILE_TESTS == u'off':
        log(u'RDPY_MOBILE_TESTS=off: %s wheels are built without tests' % platform, u'notice')
        return {u'CIBW_TEST_SKIP': u'*'}
    index = mobile_index(config, platform)
    tested = []
    for identifier in build_identifiers(args):
        info = tested_identifier(platform, identifier)
        if info and not any(fnmatch(identifier, pattern) for pattern in skip):
            tested.append((identifier,) + info)
    if not tested:
        return {}
    print(u'Checking which %s test dependencies %s has (NumPy required, %s optional)...'
          % (platform, index, u', '.join(OPTIONAL_MOBILE_TEST_REQUIRES)), flush=True)
    new_skip, optional = [], {}
    for identifier, version, arch, tags in tested:
        requirements = mobile_requirements(config, platform, arch, version)
        state, message = pip_can_install(version, tags, requirements, index)
        if state != u'ok':
            new_skip.append(identifier)
            log(u'%s: not tested, %s (%s); the wheel is still built'
                % (identifier, u'no wheel on %s for a dependency' % index if state == u'missing'
            else u'%s not reachable' % index, message), u'warning')
            continue
        found = [name for name in OPTIONAL_MOBILE_TEST_REQUIRES
                 if pip_can_install(version, tags, requirements + [name], index)[0] == u'ok']
        for name in found:
            optional.setdefault(name, set()).add(version)
        print(u'  %s: tested with NumPy%s from %s' % (
            identifier, u''.join(u' + ' + name for name in found), index), flush=True)
    env = {}
    if new_skip:
        env[u'CIBW_TEST_SKIP'] = u' '.join(skip + new_skip)
    if optional:
        requires = as_list(cibw_option(config, platform, u'test-requires'))
        for name, versions in sorted(optional.items()):
            marker = u' or '.join('python_version == "%s"' % v for v in sorted(versions))
            requires.append(u'%s; %s' % (name, marker))
        env[u'CIBW_TEST_REQUIRES_' + platform.upper()] = u' '.join(quote(r) for r in requires)
    return env


class StepTracker(object):
    """Follows cibuildwheel's steps in its whole output (test logs can be very long)."""
    HEADER = compile(r'Building (\S+) wheel')

    def __init__(self):
        self.lines = 0
        self.lastTest = self.lastBuild = -1
        self.current = self.tested = None
        self._partial = b''

    def feed(self, chunk):
        lines = (self._partial + chunk).split(b'\n')
        self._partial = lines.pop()
        for line in lines:
            self._line(line.decode(u'utf-8', u'replace'))

    def _line(self, line):
        self.lines += 1
        match = self.HEADER.search(line)
        if match:  # the next wheel starts
            self.current, self.lastBuild = match.group(1), self.lines
        elif any(step in line for step in TEST_STEPS):
            self.lastTest, self.tested = self.lines, self.current
        elif any(step in line for step in BUILD_STEPS):
            self.lastBuild = self.lines

    def failed_in_tests(self):
        """The identifier whose test step was the last step (the build succeeded), or None."""
        if self._partial:
            self._line(self._partial.decode(u'utf-8', u'replace'))
            self._partial = b''
        if self.lastTest < 0 or self.lastTest < self.lastBuild:
            return None
        return self.tested or u'a wheel'


def failed_in_tests(output):
    tracker = StepTracker()
    tracker.feed(output.encode(u'utf-8'))
    return tracker.failed_in_tests()


# ----------------------------------------------------------------------------------------
# restart on network errors (parent process)
# ----------------------------------------------------------------------------------------

def network_error(output_tail):
    """The network error that ended a failed run, or None."""
    match = NETWORK_ERROR.search(output_tail)
    return match.group(0) if match else None


def run_once(args, extra_env=None, tracker=None):
    """Runs cibuildwheel in a child process, echoing its output; returns (exit code, tail)."""
    env = dict(environ, **{CHILD_FLAG: u'1', u'PYTHONUNBUFFERED': u'1'})
    env.update(extra_env or {})
    process = Popen([sys.executable, abspath(__file__)] + list(args), stdout=PIPE, stderr=STDOUT, env=env)
    tail, out = deque(maxlen=64), sys.stdout.buffer
    while True:
        chunk = process.stdout.read1(65536)
        if not chunk:
            break
        out.write(chunk)
        out.flush()
        tail.append(chunk)
        if tracker is not None:
            tracker.feed(chunk)
    code = process.wait()
    text = b''.join(tail).decode(u'utf-8', u'replace')
    return code, u'\n'.join(text.splitlines()[-300:])


def main(args):
    if environ.get(CHILD_FLAG) == u'1':
        return run_cibuildwheel(args)
    extra_env = prepare_mobile_tests(args)
    for name, value in sorted(extra_env.items()):
        print(u'%s=%s' % (name, value), flush=True)
    mobile = target_platform(args) in MOBILE_PLATFORMS
    attempt, without_tests = 0, False
    while True:
        attempt += 1
        tracker = StepTracker()
        code, tail = run_once(args, extra_env, tracker)
        if code == 0:
            return 0
        reason = network_error(tail)
        if reason and attempt < ATTEMPTS:
            pause = RESTART_PAUSES[min(attempt - 1, len(RESTART_PAUSES) - 1)]
            log(u'cibuildwheel failed with a network error (%s): restarting in %d s (attempt %d of %d)'
                % (reason, pause, attempt + 1, ATTEMPTS), u'warning')
            sleep(pause)
            continue
        failed = tracker.failed_in_tests() if mobile and MOBILE_TESTS == u'auto' else None
        if failed and not without_tests:
            # the simulator / emulator is part of the test: do not lose the wheels for it
            log(u'the %s test of %s failed: building the %s wheels again without tests '
                u'(RDPY_MOBILE_TESTS=required makes this an error)' % (target_platform(
                args), failed, target_platform(args)), u'warning')
            extra_env = dict(extra_env, CIBW_TEST_SKIP=u'*')
            attempt, without_tests = 0, True
            continue
        if reason:
            log(u'cibuildwheel failed with a network error (%s) %d times: giving up'
                % (reason, ATTEMPTS), u'error')
        return code


if __name__ == '__main__':
    # tools/ is sys.path[0] when run as a script: tools/build.py would shadow the "build"
    # package that cibuildwheel imports
    HERE = dirname(abspath(__file__))
    sys.path[:] = [p for p in sys.path if abspath(p or curdir) != HERE]
    sys.exit(main(sys.argv[1:]))
