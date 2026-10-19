"""Render a QR code to the terminal without any Python QR dependency.

The module matrix comes from ``libqrencode`` through ctypes. There is NO CLI path, so
the shared library — not the ``qrencode`` binary — is what has to be present, and on a
Homebrew install it lands in a prefix the dynamic loader does not search by default, so
it is resolved by explicit path as well as by name.

Windows needs both halves of that treatment and gets neither from the Unix defaults: the
library is ``libqrencode.dll``, a name this module never asked for, and a bare-name load does
not reach it. Observed on a Windows install: a directory holding the DLL *and* all three of
its dependency DLLs, present on ``PATH``, still fails a bare-name ``ctypes.CDLL`` load, while
the same file by absolute path loads. That observation is the whole basis for the Windows
branch below — the explicit path works regardless of which part of the search order is
actually being skipped, so no claim is made here about ``LoadLibraryEx`` versus Python's
default DLL search mode.

Drawing rules that keep it scannable:

* Modules are drawn as glyph *pairs*, so a 2-module-wide cell fits a normal
  terminal. ``dark_terminal=True`` (the default) inverts the polarity: scanners
  expect dark modules on a light field, and a dark terminal has the opposite
  background, so the glyph that paints MORE pixels must represent a LIGHT
  module. Half blocks carry one row on top and one below, which is what makes
  two QR rows share a single terminal line.
* A quiet zone of 2 modules on every side (the spec asks for 4; 2 keeps the
  code narrow enough to fit a phone-narrow terminal and still scans).
"""

from __future__ import annotations

import ctypes
import os
import shutil
from typing import List, Optional, Sequence

_LIB_NAMES = (
    "libqrencode.so.4", "libqrencode.so", "libqrencode.dylib", "libqrencode.4.dylib",
    "libqrencode.dll", "libqrencode-4.dll", "qrencode.dll",   # Windows (mingw64 / MSVC)
)

# Resolved once, at import, so the Windows branch can be tested by patching THIS name. Tests must
# not reach for ``os.name``: that is the shared ``os`` module, and on Python < 3.13 pathlib picks
# WindowsPath/PosixPath from it at CALL time, so setting it process-wide makes ``pathlib.Path``
# raise NotImplementedError — which kills pytest's own failure reporter instead of failing a test.
_IS_WINDOWS = os.name == "nt"

# Directories a package manager installs into but that the dynamic loader does NOT search for a
# plain process. macOS is the case that bites: Homebrew's prefix (/opt/homebrew on arm64) is not on
# dyld's fallback path, so a bare-name load fails on a box where `brew install qrencode` succeeded
# and the library is sitting right there. The .so is the real file on Debian-family systems while
# `libqrencode.so` is usually the -dev symlink, so the versioned name is tried first.
_LIB_DIRS = (
    "/opt/homebrew/lib",                 # Homebrew (Apple Silicon)
    "/usr/local/lib",                    # Homebrew (Intel), other local prefixes
    "/opt/local/lib",                    # MacPorts
    "/usr/lib",
    "/usr/lib64",
    "/usr/lib/x86_64-linux-gnu",
    "/usr/lib/aarch64-linux-gnu",
    "/usr/lib/arm-linux-gnueabihf",
    "/lib",
)
_ECL = {"L": 0, "M": 1, "Q": 2, "H": 3}
# QR_MODE_8 == 2. (4 is QR_MODE_STRUCTURE, which makes libqrencode return NULL.)
_MODE_8BIT = 2

_FULL = "\u2588"    # full block
_TOP = "\u2580"     # upper half block
_BOTTOM = "\u2584"  # lower half block
_EMPTY = " "


class QRError(RuntimeError):
    """No renderer could produce a matrix."""


def _windows_lib_dirs() -> List[str]:
    """Windows directories worth probing by explicit path, most specific first.

    A bare-name load does not reach a DLL that is on ``PATH`` (see the module docstring for the
    observation), so the explicit-path fallback below is what finds it. The install's own ``lib``
    directory comes first (that is where the installer puts the DLLs), then every ``PATH`` entry,
    so a Windows box that already has the library somewhere sensible works without also setting
    an environment variable.
    """
    dirs: List[str] = []
    home = (os.environ.get("HERMES_HOME") or "").strip()
    if not home:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            home = os.path.join(local, "hermes")
    if home:
        parts = os.path.normpath(home).split(os.sep)
        if "profiles" in parts:            # HERMES_HOME may carry /profiles/<name>
            home = os.sep.join(parts[:parts.index("profiles")])
        dirs.append(os.path.join(home, "lib"))
    dirs += [p for p in (os.environ.get("PATH") or "").split(os.pathsep) if p]
    return dirs


def _candidate_paths() -> List[str]:
    """Explicit paths to try, in order: known lib dirs, then whatever the env advertises."""
    dirs = list(_LIB_DIRS)
    if _IS_WINDOWS:
        dirs += _windows_lib_dirs()
    for var in ("DYLD_FALLBACK_LIBRARY_PATH", "LD_LIBRARY_PATH"):
        dirs += [p for p in (os.environ.get(var) or "").split(os.pathsep) if p]
    return [os.path.join(d, n) for d in dirs for n in _LIB_NAMES]


def _find_library_by_convention() -> Optional[str]:
    """What the platform's own lookup finds (dyld/ldconfig conventions). May be unavailable."""
    try:
        from ctypes.util import find_library

        return find_library("qrencode")
    except Exception:
        return None


def _load_library() -> Optional[ctypes.CDLL]:
    """Load libqrencode — by name first, then by explicit path.

    By name is right when the library is on the loader's search path; the explicit paths are what
    make it work when it is NOT, which is the normal state on macOS with Homebrew.
    """
    for name in _LIB_NAMES:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    found = _find_library_by_convention()
    if found:
        try:
            return ctypes.CDLL(found)
        except OSError:
            pass
    for path in _candidate_paths():
        if not os.path.exists(path):
            continue
        try:
            return ctypes.CDLL(path)
        except OSError:
            continue
    return None


def _matrix_via_libqrencode(payload: str, ecl: str = "M") -> Optional[List[List[bool]]]:
    """Module matrix with True = dark, straight from libqrencode."""
    lib = _load_library()
    if lib is None:
        return None

    class _QRCode(ctypes.Structure):
        _fields_ = [
            ("version", ctypes.c_int),
            ("width", ctypes.c_int),
            ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    lib.QRcode_encodeString.restype = ctypes.POINTER(_QRCode)
    lib.QRcode_encodeString.argtypes = [
        ctypes.c_char_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ]
    lib.QRcode_free.argtypes = [ctypes.POINTER(_QRCode)]

    handle = lib.QRcode_encodeString(
        payload.encode("utf-8"), 0, _ECL.get(ecl.upper(), 1), _MODE_8BIT, 1)
    if not handle:
        return None
    try:
        width = handle.contents.width
        count = width * width
        raw = ctypes.cast(handle.contents.data,
                          ctypes.POINTER(ctypes.c_ubyte * count)).contents
        return [[bool(raw[row * width + col] & 1) for col in range(width)]
                for row in range(width)]
    finally:
        lib.QRcode_free(handle)


def _unicode_cols() -> int:
    try:
        return shutil.get_terminal_size(fallback=(0, 0)).columns or 0
    except Exception:
        return 0


def _cell(top: bool, bottom: bool) -> str:
    """Glyph for one terminal cell covering two module rows. True = paint pixels."""
    if top and bottom:
        return _FULL
    if top:
        return _TOP
    if bottom:
        return _BOTTOM
    return _EMPTY


def _at(matrix: Sequence[Sequence[bool]], row: int, col: int, size: int) -> bool:
    if row < 0 or col < 0 or row >= size or col >= size:
        return False  # quiet zone stays light
    return bool(matrix[row][col])


def render(payload: str, *, ecl: str = "M", ascii_fallback: bool = False,
           quiet: int = 2, dark_terminal: bool = True) -> str:
    """Return the QR as text ready for stdout (light module = painted glyph)."""
    matrix = _matrix_via_libqrencode(payload, ecl)
    if matrix is None:
        raise QRError(
            "no QR renderer available (install `qrencode`, or the libqrencode "
            "shared library); use --link to print the pairing link instead")

    size = len(matrix)
    full = size + quiet * 2
    per_cell = 2 if ascii_fallback else 1
    out: List[str] = []
    for row in range(0, full, 2):
        chars: List[str] = []
        for col in range(full):
            top = _at(matrix, row - quiet, col - quiet, size)
            bottom = _at(matrix, row + 1 - quiet, col - quiet, size)
            if dark_terminal:
                top, bottom = not top, not bottom
            chars.append(_cell(top, bottom) * per_cell)
        out.append("".join(chars))
    cols = _unicode_cols()
    if cols and full * per_cell > cols:
        out.append("")
        out.append(f"  ({full} modules wide — widen the terminal or shrink the "
                   f"font if this wraps)")
    return "\n".join(out)


def matrix_for_test(payload: str, ecl: str = "M") -> Optional[List[List[bool]]]:
    """Raw module matrix (True = dark). Used by tests and verification."""
    return _matrix_via_libqrencode(payload, ecl)


def matrix_from_art(art: str, *, quiet: int = 2, dark_terminal: bool = True,
                    per_cell: int = 1) -> List[List[bool]]:
    """Rebuild the module matrix from rendered art (verification helper).

    Inverts :func:`render` exactly — including ``ascii_fallback=True`` output,
    which spends two characters per module — so a test can prove the glyph
    mapping is faithful before handing the matrix to a real decoder.
    """
    lines = [line for line in art.splitlines()
             if line and not line.lstrip().startswith("(")]
    height = len(lines) * 2
    width = max((len(line) // per_cell for line in lines), default=0)
    grid = [[False] * width for _ in range(height)]
    for y, line in enumerate(lines):
        for cell, ch in enumerate(line[::per_cell]):
            if cell >= width:
                break
            top = ch in (_FULL, _TOP)
            bottom = ch in (_FULL, _BOTTOM)
            if dark_terminal:
                top, bottom = not top, not bottom
            grid[y * 2][cell] = top
            grid[y * 2 + 1][cell] = bottom
    size = width - quiet * 2
    return [row[quiet:quiet + size] for row in grid[quiet:quiet + size]]


def art_to_png(path: str, art: str, *, quiet: int = 2, dark_terminal: bool = True,
               scale: int = 8) -> None:
    """Write rendered art to a PNG (for decoders that want an image)."""
    try:
        from PIL import Image  # type: ignore
    except Exception as exc:  # pragma: no cover - only used by verification
        raise QRError(f"PIL not available for PNG export: {exc}") from exc
    grid = matrix_from_art(art, quiet=quiet, dark_terminal=dark_terminal)
    size = len(grid)
    img = Image.new("L", (size, size), 255)
    px = img.load()
    for y in range(size):
        for x in range(size):
            # Dark module -> black pixel (scanner polarity, independent of theme).
            px[x, y] = 0 if grid[y][x] else 255
    img = img.resize((size * scale, size * scale), Image.NEAREST)
    img.save(path)
