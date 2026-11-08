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
If a future cibuildwheel no longer has the download function patched here, cibuildwheel runs
unchanged (only the restart on network errors remains).
"""
from __future__ import absolute_import, division, print_function
import collections
import hashlib
import http.client
import importlib
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

CHILD_FLAG = "RDPY_CIBW_CHILD"
PATIENCE = float(os.environ.get("RDPY_CIBW_DOWNLOAD_PATIENCE", "900"))
ATTEMPTS = max(1, int(os.environ.get("RDPY_CIBW_ATTEMPTS", "3")))
RESTART_PAUSES = (60, 300)  # seconds before the 2nd, 3rd... run

# (GitHub release URL, mirror serving the same file)
MIRRORS = (
    (re.compile(r"https://github\.com/pypa/get-virtualenv/releases/download/([^/]+)/virtualenv\.pyz"),
     r"https://raw.githubusercontent.com/pypa/get-virtualenv/\1/public/virtualenv.pyz"),
    (re.compile(r"https://github\.com/astral-sh/python-build-standalone/releases/download/(.+)"),
     r"https://releases.astral.sh/github/python-build-standalone/releases/download/\1"),
)

# a failed run whose output ends with one of these is restarted
NETWORK_ERROR = re.compile("|".join((
    r"HTTP Error (?:408|429|5\d\d)", r"Gateway Time-?out", r"Bad Gateway", r"Service Unavailable",
    r"Too Many Requests", r"toomanyrequests", r"urlopen error", r"Max retries exceeded",
    r"Could not fetch URL", r"Connection (?:reset|refused|aborted|timed out)",
    r"RemoteDisconnected", r"IncompleteRead", r"Read ?Timeout", r"Connect ?Timeout",
    r"timed out", r"TLS handshake timeout", r"i/o timeout",
    r"Temporary failure in name resolution", r"Name or service not known",
    r"nodename nor servname", r"getaddrinfo failed", r"Could not resolve host",
    r"unexpected EOF", r"error pulling image", r"failed to resolve reference",
    r"Unable to load the service index",
)), re.I)


def log(message, level=None):
    """Prints a message, as a GitHub Actions annotation when running there."""
    if level and os.environ.get("GITHUB_ACTIONS") == "true":
        print("::%s::%s" % (level, message), flush=True)
    else:
        print(("%s: " % level if level else "") + message, flush=True)


# ----------------------------------------------------------------------------------------
# patient downloads (run inside the cibuildwheel process)
# ----------------------------------------------------------------------------------------

def candidates(url):
    """The URL followed by its mirrors."""
    return [url] + [m.sub(mirror, url) for m, mirror in MIRRORS if m.fullmatch(url)]


def user_agent():
    # some CDNs (releases.astral.sh) refuse urllib's default "Python-urllib/3.x"
    try:
        import cibuildwheel
        version = cibuildwheel.__version__
    except (ImportError, AttributeError):
        version = "unknown"
    return "cibuildwheel/%s (RdpyManyQt tools/cibw.py)" % version


def fetch(url, dest, sha256, ca_file):
    """One download attempt; raises ValueError when the file is not the expected one."""
    context = ssl.create_default_context(cafile=ca_file)
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    try:
        with urllib.request.urlopen(request, context=context, timeout=120) as response, \
                open(part, "wb") as out:
            shutil.copyfileobj(response, out, 1 << 20)
        if sha256:
            with open(part, "rb") as f:
                digest = hashlib.file_digest(f, "sha256").hexdigest()
            if digest != sha256:
                raise ValueError("SHA256 mismatch: expected %s, got %s" % (sha256, digest))
        os.replace(part, dest)
    finally:
        if os.path.exists(part):
            os.unlink(part)


def make_patient_download(cibw_file):
    """A replacement for cibuildwheel.util.file.download(url, dest, *, sha256=None)."""
    ca_bundle = getattr(cibw_file, "ca_bundle_path", lambda: None)
    fatal = getattr(cibw_file, "FatalError", RuntimeError)

    def download(url, dest, *, sha256=None):
        print("+ Download %s to %s" % (url, dest), flush=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        urls = candidates(url)
        deadline = time.monotonic() + PATIENCE
        pause, last_error = 5, None
        while True:
            client_errors = 0
            for candidate in list(urls):
                try:
                    fetch(candidate, dest, sha256, ca_bundle())
                    if candidate != url:
                        print("  downloaded from mirror %s" % candidate, flush=True)
                    return
                except ValueError as e:  # wrong file
                    if candidate == url:
                        raise fatal("%s for %s" % (e, url))
                    print("  mirror %s: %s (not used)" % (candidate, e), flush=True)
                    urls.remove(candidate)
                    continue
                except urllib.error.HTTPError as e:
                    last_error = e
                    if 400 <= e.code < 500 and e.code not in (408, 429):
                        client_errors += 1  # will not fix itself (bad URL)
                except (OSError, http.client.HTTPException) as e:  # network, timeout, TLS...
                    last_error = e
                print("  %s: %s" % (candidate, last_error), flush=True)
            if client_errors == len(urls) or time.monotonic() + pause > deadline:
                raise last_error
            print("  retrying in %d s (gives up %d s from now)"
                  % (pause, deadline - time.monotonic()), flush=True)
            time.sleep(pause)
            pause = min(pause * 2, 120)

    return download


def install_patient_downloads():
    """Replaces cibuildwheel's download function in every cibuildwheel module.
    Returns False when this cibuildwheel has no such function (left unchanged)."""
    import cibuildwheel.util.file as cibw_file
    original = getattr(cibw_file, "download", None)
    if not callable(original):
        log("cibuildwheel.util.file.download not found: downloads are not made patient",
            "warning")
        return False
    patient = make_patient_download(cibw_file)
    cibw_file.download = patient
    importlib.import_module("cibuildwheel.__main__")  # imports every platform module
    for name, module in list(sys.modules.items()):
        if name.startswith("cibuildwheel") and getattr(module, "download", None) is original:
            module.download = patient
    return True


def run_cibuildwheel(args):
    install_patient_downloads()
    import cibuildwheel.__main__ as cibw_main
    sys.argv = ["cibuildwheel"] + list(args)
    cibw_main.main()


# ----------------------------------------------------------------------------------------
# restart on network errors (parent process)
# ----------------------------------------------------------------------------------------

def network_error(output_tail):
    """The network error that ended a failed run, or None."""
    match = NETWORK_ERROR.search(output_tail)
    return match.group(0) if match else None


def run_once(args):
    """Runs cibuildwheel in a child process, echoing its output; returns (exit code, tail)."""
    env = dict(os.environ, **{CHILD_FLAG: "1", "PYTHONUNBUFFERED": "1"})
    process = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + list(args),
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    tail, out = collections.deque(maxlen=64), sys.stdout.buffer
    while True:
        chunk = process.stdout.read1(65536)
        if not chunk:
            break
        out.write(chunk)
        out.flush()
        tail.append(chunk)
    code = process.wait()
    text = b"".join(tail).decode("utf-8", "replace")
    return code, "\n".join(text.splitlines()[-300:])


def main(args):
    if os.environ.get(CHILD_FLAG) == "1":
        return run_cibuildwheel(args)
    for attempt in range(1, ATTEMPTS + 1):
        code, tail = run_once(args)
        if code == 0:
            return 0
        reason = network_error(tail)
        if not reason:
            return code
        if attempt == ATTEMPTS:
            log("cibuildwheel failed with a network error (%s) %d times: giving up"
                % (reason, ATTEMPTS), "error")
            return code
        pause = RESTART_PAUSES[min(attempt - 1, len(RESTART_PAUSES) - 1)]
        log("cibuildwheel failed with a network error (%s): restarting in %d s (attempt %d of %d)"
            % (reason, pause, attempt + 1, ATTEMPTS), "warning")
        time.sleep(pause)
    return 1


if __name__ == '__main__':
    # tools/ is sys.path[0] when run as a script: tools/build.py would shadow the "build"
    # package that cibuildwheel imports
    HERE = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.curdir) != HERE]
    sys.exit(main(sys.argv[1:]))
