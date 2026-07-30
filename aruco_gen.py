"""
ArUco Marker Generation
=========================
Generates ArUco marker bitmaps via cv2.aruco -- a module this project
already carries opencv-python for -- rendered directly at the LED matrix's
native resolution (image_loader.MATRIX_WIDTH x MATRIX_HEIGHT) instead of at
some arbitrary size that then has to be downsampled through the normal
image pipeline. That matters because a marker's modules (the individual
black/white cells, including its solid border) need to land on pixel
boundaries as cleanly as possible for the panel to reproduce it legibly;
letting cv2 render straight to the target size does that better than
area-averaging a larger source image down.

Markers are always generated as a matrix_width x matrix_height array, so a
non-square matrix would distort the marker -- true today's 16x16 hardware,
noted here rather than handled since there's nothing square-specific to
special-case yet.
--------------------------------------------------------------------------
"""

import os

import cv2
import numpy as np
from PIL import Image

from image_loader import MATRIX_WIDTH, MATRIX_HEIGHT

# The common predefined dictionaries, named the way OpenCV names them minus
# the DICT_ prefix and lowercased. 4x4/5x5 are listed first in most UIs
# because their modules (bits + 2*border) resolve to the most LED pixels
# each on a 16x16 matrix -- 6x6/7x7 are still offered since a bigger matrix,
# or just close-range use, can make them workable too.
DICTIONARIES = {
    "4x4_50": cv2.aruco.DICT_4X4_50,
    "4x4_100": cv2.aruco.DICT_4X4_100,
    "4x4_250": cv2.aruco.DICT_4X4_250,
    "4x4_1000": cv2.aruco.DICT_4X4_1000,
    "5x5_50": cv2.aruco.DICT_5X5_50,
    "5x5_100": cv2.aruco.DICT_5X5_100,
    "5x5_250": cv2.aruco.DICT_5X5_250,
    "5x5_1000": cv2.aruco.DICT_5X5_1000,
    "6x6_50": cv2.aruco.DICT_6X6_50,
    "6x6_100": cv2.aruco.DICT_6X6_100,
    "6x6_250": cv2.aruco.DICT_6X6_250,
    "6x6_1000": cv2.aruco.DICT_6X6_1000,
    "7x7_50": cv2.aruco.DICT_7X7_50,
    "7x7_100": cv2.aruco.DICT_7X7_100,
    "7x7_250": cv2.aruco.DICT_7X7_250,
    "7x7_1000": cv2.aruco.DICT_7X7_1000,
    "original": cv2.aruco.DICT_ARUCO_ORIGINAL,
}

DEFAULT_DICTIONARY = "4x4_50"


class ArucoGenError(ValueError):
    """Raised for an unknown dictionary name or an out-of-range marker ID --
    lets callers (CLI, GUI dialog) catch one exception type regardless of
    which validation failed."""


def dictionary_size(name: str) -> int:
    """Return how many distinct marker IDs `name` supports (valid IDs are
    0..size-1)."""
    if name not in DICTIONARIES:
        raise ArucoGenError(f"unknown dictionary {name!r} -- expected one of {sorted(DICTIONARIES)}")
    dictionary = cv2.aruco.getPredefinedDictionary(DICTIONARIES[name])
    return dictionary.bytesList.shape[0]


def generate_marker(name: str, marker_id: int, border_bits: int = 1) -> np.ndarray:
    """Return a (MATRIX_HEIGHT, MATRIX_WIDTH) uint8 grayscale array (0 or
    255) for the given dictionary/ID, rendered directly at the matrix's
    native resolution -- see module docstring."""
    size = dictionary_size(name)  # also validates `name`
    if not (0 <= marker_id < size):
        raise ArucoGenError(f"marker id {marker_id} out of range for {name!r} (valid: 0-{size - 1})")
    if border_bits < 0:
        raise ArucoGenError(f"border bits must be >= 0, got {border_bits}")

    dictionary = cv2.aruco.getPredefinedDictionary(DICTIONARIES[name])
    return cv2.aruco.generateImageMarker(dictionary, marker_id, MATRIX_WIDTH, borderBits=border_bits)


def save_marker_png(gray: np.ndarray, name: str, marker_id: int, directory: str) -> str:
    """Save a generated marker as a static PNG under `directory`, so it
    exists as an ordinary file from then on instead of only in memory --
    openable later via gui.py's "Open Image..." or send_image.py, exactly
    like any other picture, without regenerating it. Named so the
    dictionary/ID are recoverable from the filename alone; returns the path
    written."""
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"aruco_{name}_id{marker_id}.png")
    Image.fromarray(gray, "L").save(path)
    return path
