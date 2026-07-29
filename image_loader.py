"""
Image Loading for LED Matrix
=============================
Loads an image file -- either a hex-literal byte dump (image2cpp-style, or a
flat NUMPIXELS-byte grayscale dump) or a regular raster image (PNG/JPG/etc.)
-- and returns it as a LoadedImage: a MATRIX_HEIGHT x MATRIX_WIDTH grayscale
array (always present), plus an RGB array when the source actually carries
color (raster images only -- hex dumps are grayscale by construction).

Kept as pure, side-effect-free (besides the file read) functions so the GUI
can hold the resulting arrays in memory and re-render them on every settings
change without re-reading or re-decoding the source file.

--------------------------------------------------------------------------
Hex format
--------------------------------------------------------------------------
load_image() accepts either:

- image2cpp-style: `..._width`/`..._height` declarations plus a packed
  pixel array at 1, 4, or 8 bits per pixel (auto-detected from the byte
  count). The source image does not need to already be MATRIX_WIDTH x
  MATRIX_HEIGHT -- it's downsampled (area-averaged) or upsampled
  (nearest-neighbor) automatically. See images/README.md for details.
- A flat NUMPIXELS-byte grayscale dump, if no width/height metadata is
  found in the file.

Either way, this just scans for `0xNN` tokens, so it doesn't care whether
the surrounding syntax is a `.h` C header or a plain comma-separated `.txt`.
--------------------------------------------------------------------------
"""

import os
import re
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageOps

# Physical dimensions of the LED matrix. Their product must match NUMPIXELS
# in led_matrix_esp.ino -- that's also the exact pixel count every loaded
# image gets resized to before it can be sent.
MATRIX_WIDTH = 16
MATRIX_HEIGHT = 16
NUMPIXELS = MATRIX_WIDTH * MATRIX_HEIGHT

HEX_EXTS = {".h", ".hpp", ".c", ".inc", ".txt"}
RASTER_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}

# Matches a hex byte literal like 0xFF or 0x0a. Deliberately format-agnostic
# about what's around it, so it works equally on a full C header (variable
# declaration, PROGMEM attribute, braces, comments) or a bare comma/newline
# separated hex dump -- it just pulls out every `0xNN` token in the file.
HEX_TOKEN_RE = re.compile(r"0[xX][0-9A-Fa-f]{2}")

# Matches image2cpp-style `..._width = N;` / `..._height = N;` declarations
# (any prefix, e.g. pic1_width, image_height) so the source resolution can
# be recovered regardless of what the generator named its variables.
WIDTH_RE = re.compile(r"\w*width\w*\s*=\s*(\d+)", re.IGNORECASE)
HEIGHT_RE = re.compile(r"\w*height\w*\s*=\s*(\d+)", re.IGNORECASE)


class ImageLoadError(ValueError):
    """Raised for any problem loading/decoding an image file -- lets callers
    (CLI, GUI) catch one exception type regardless of source format."""


@dataclass
class LoadedImage:
    gray: np.ndarray            # (MATRIX_HEIGHT, MATRIX_WIDTH) uint8, always present
    rgb: "np.ndarray | None"    # (MATRIX_HEIGHT, MATRIX_WIDTH, 3) uint8, or None (hex sources carry no color)
    path: str
    source_size: tuple          # (width, height) of the original source, before resizing


def _parse_dimensions(text: str):
    """Return (width, height) from image2cpp-style declarations, or None if
    the file has no such metadata (i.e. it's assumed to already be a flat
    NUMPIXELS-byte dump matching the matrix 1:1)."""
    w_match = WIDTH_RE.search(text)
    h_match = HEIGHT_RE.search(text)
    if not w_match or not h_match:
        return None
    return int(w_match.group(1)), int(h_match.group(1))


def _detect_bit_depth(token_count: int, width: int, height: int) -> int:
    """Infer pixels-per-byte packing from the byte count alone.

    image2cpp (and similar tools) pack multiple low-bit-depth pixels per
    byte, row-aligned (each row starts on a byte boundary, so a row whose
    width isn't a multiple of the packing factor gets padded). Rather than
    trust a possibly-wrong size comment in the source (a `/8` size
    declaration is arithmetically incorrect for non-multiple-of-8 widths --
    integer truncation), just check which depth's *actual* row-padded size
    matches the real token count.
    """
    candidates = {
        8: width * height,                # 1 byte/pixel, direct grayscale
        4: ((width + 1) // 2) * height,    # 2 px/byte (4-bit grayscale)
        1: ((width + 7) // 8) * height,    # 8 px/byte (1-bit monochrome)
    }
    for depth, expected in candidates.items():
        if token_count == expected:
            return depth
    raise ImageLoadError(
        f"{token_count} hex byte(s) doesn't match any known packing for a "
        f"{width}x{height} image (tried 8/4/1 bits per pixel: "
        f"{candidates[8]}/{candidates[4]}/{candidates[1]} byte(s) expected)"
    )


def _unpack_bitmap(raw: bytes, width: int, height: int, bit_depth: int) -> np.ndarray:
    """Expand a row-padded, MSB-first packed bitmap into an 8-bit HxW
    grayscale array (0-255)."""
    pixels_per_byte = 8 // bit_depth
    row_bytes = -(-width // pixels_per_byte)  # ceil division
    max_val = (1 << bit_depth) - 1
    scale = 255 // max_val  # e.g. 1-bit -> 255, 4-bit -> 17, 8-bit -> 1

    img = np.zeros((height, width), dtype=np.uint8)
    for y in range(height):
        row_start = y * row_bytes
        for x in range(width):
            byte_val = raw[row_start + x // pixels_per_byte]
            shift = bit_depth * (pixels_per_byte - 1 - (x % pixels_per_byte))
            img[y, x] = ((byte_val >> shift) & max_val) * scale
    return img


def _resize_to_matrix(img: np.ndarray) -> np.ndarray:
    """Resize an HxW (or HxWx3) array to MATRIX_WIDTH x MATRIX_HEIGHT.

    Area-average when shrinking (the common case -- a photo down to 16x16),
    nearest-neighbor when enlarging (keeps small pixel-art crisp instead of
    blurring it). Compares total pixel *area*, not width/height as a tuple --
    a lexicographic tuple compare like `(w, h) > (MW, MH)` picks the wrong
    interpolation whenever one axis grows and the other shrinks (e.g. a
    20x8 source: `20 > 16` is True even though the height is upscaling).
    """
    h, w = img.shape[:2]
    interpolation = cv2.INTER_AREA if (w * h) > NUMPIXELS else cv2.INTER_NEAREST
    resized = cv2.resize(img, (MATRIX_WIDTH, MATRIX_HEIGHT), interpolation=interpolation)
    return resized.astype(np.uint8)


def _load_hex(path: str) -> LoadedImage:
    """Load an image2cpp-style hex dump or flat NUMPIXELS-byte grayscale
    dump. See module docstring for the two accepted shapes."""
    try:
        with open(path, "r") as f:
            text = f.read()
    except OSError as e:
        raise ImageLoadError(str(e)) from e

    raw = bytes(int(tok, 16) for tok in HEX_TOKEN_RE.findall(text))

    dims = _parse_dimensions(text)
    if dims is None:
        if len(raw) != NUMPIXELS:
            raise ImageLoadError(
                f"found {len(raw)} hex byte(s) and no width/height metadata, "
                f"expected exactly {NUMPIXELS} (one grayscale byte per LED)"
            )
        gray = np.frombuffer(raw, dtype=np.uint8).reshape(MATRIX_HEIGHT, MATRIX_WIDTH)
        return LoadedImage(gray=gray, rgb=None, path=path, source_size=(MATRIX_WIDTH, MATRIX_HEIGHT))

    width, height = dims
    bit_depth = _detect_bit_depth(len(raw), width, height)
    img = _unpack_bitmap(raw, width, height, bit_depth)
    gray = _resize_to_matrix(img)
    return LoadedImage(gray=gray, rgb=None, path=path, source_size=(width, height))


def decode_raster(path: str):
    """Decode a raster file into an RGBA PIL.Image (exif-transposed; an
    opaque alpha channel is added if the source didn't have one) plus its
    original (pre-resize) size -- the I/O half of _load_raster(), with no
    background compositing or matrix resizing done yet.

    Split out from _load_raster()/composite_raster() so a caller that needs
    to recomposite the same source against different background colors
    (the GUI's background-color picker, see gui.py's _decode_and_composite)
    can decode once and reuse it, instead of re-reading and re-decoding the
    file from disk on every color change.

    Pillow, not cv2.imread, on purpose: cv2.imread silently returns None for
    paths containing non-ASCII characters on Windows, which would surface as
    a baffling "image is None" for anyone with an accented character
    anywhere in the path. Pillow has no such issue.
    """
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            source_size = im.size  # (width, height), before any resizing
            im = im.convert("RGBA")
    except (OSError, ValueError) as e:
        raise ImageLoadError(str(e)) from e
    return im, source_size


def composite_raster(im: Image.Image, background: tuple, path: str, source_size: tuple) -> LoadedImage:
    """Flatten an already-decoded RGBA image (see decode_raster) onto
    `background` and resize to the matrix.

    Safe to call unconditionally even for sources that never had an alpha
    channel: alpha_composite() over fully-opaque pixels is a no-op, so
    `background` only visibly matters where the source actually had
    transparency -- convert("L")/convert("RGB") on a transparent PNG
    without this would otherwise treat the transparent area as white, which
    would light up the whole panel around the subject.
    """
    bg = Image.new("RGBA", im.size, tuple(background) + (255,))
    composited = Image.alpha_composite(bg, im)
    rgb_full = np.asarray(composited.convert("RGB"), dtype=np.uint8)
    gray_full = np.asarray(composited.convert("L"), dtype=np.uint8)
    gray = _resize_to_matrix(gray_full)
    rgb = _resize_to_matrix(rgb_full)
    return LoadedImage(gray=gray, rgb=rgb, path=path, source_size=source_size)


def _load_raster(path: str, background: tuple = (0, 0, 0)) -> LoadedImage:
    """Load a regular raster image (PNG/JPG/etc.) via Pillow -- decode_raster()
    then composite_raster() in one shot, for callers that don't need to
    reuse the decoded source across multiple background colors."""
    im, source_size = decode_raster(path)
    return composite_raster(im, background, path, source_size)


def load_image(path: str, background: tuple = (0, 0, 0)) -> LoadedImage:
    """Load path, dispatching on file extension to the hex-dump loader or
    the raster-image loader. Raises ImageLoadError on any problem.

    `background` is the color transparent pixels are flattened onto -- only
    meaningful for raster sources with an alpha channel; hex dumps carry no
    transparency and ignore it.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext in RASTER_EXTS:
        return _load_raster(path, background=background)
    if ext in HEX_EXTS:
        return _load_hex(path)
    raise ImageLoadError(
        f"unrecognized file extension {ext!r} -- expected one of "
        f"{sorted(HEX_EXTS | RASTER_EXTS)}"
    )
