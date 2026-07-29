"""
Rendering Pipeline for LED Matrix
==================================
Turns a LoadedImage (see image_loader.py) plus a RenderSettings into the
exact RGB pixel array the ESP32 will display, the 768-byte wire payload for
that array, and a scaled-up preview image for on-screen display.

This is deliberately the single place where "what does the panel show"
gets decided: render_rgb() is the one function that feeds both
to_payload() (what actually gets sent) and to_preview_image() (what the
GUI shows on screen), so the preview cannot drift from the panel -- there
is no second code path that could compute something different.

Rendering modes:

- Two-tone (the default): each pixel is thresholded against
  RenderSettings.threshold and rendered as one of two solid colors
  (color_on/color_off). Good for markers, logos, and text, where
  intermediate gray from resizing/anti-aliasing just looks murky and true
  black is otherwise indistinguishable from the matrix being off.
  With `smooth=True`, pixels are ramped between color_off and color_on
  instead of hard-thresholded -- this degrades to plain grayscale when
  color_off=black and color_on=white, so smooth two-tone is a strict
  superset of the old firmware's non-binarized behavior.
- Full color: uses the source image's actual RGB values directly (only
  available when the source is a raster image -- see LoadedImage.rgb).

Brightness is applied last, as a uniform scale on the final RGB output --
not to the grayscale input -- so it can never interact with the threshold
decision (scaling the input first could push an already-dim pixel below
the threshold and flip it off, which would be a surprising side effect of
"brightness").
--------------------------------------------------------------------------
"""

from dataclasses import dataclass, replace

import numpy as np
from PIL import Image

from image_loader import LoadedImage, MATRIX_WIDTH, MATRIX_HEIGHT

TWO_TONE = "two_tone"
FULL_COLOR = "full_color"

# This panel is physically mounted (or wired) mirrored left-right relative
# to how the image looks in the preview. To make the panel look upright we
# have to send mirror(logical) -- but the preview should keep showing
# logical, because that's what the panel will physically look like once its
# own hardware mirroring cancels ours back out. So this is applied only in
# to_payload(), never to render_rgb()/to_preview_image(), the same way
# MATRIX_SERPENTINE (firmware's row-wiring remap) never touches the
# preview: both are corrections for how the physical panel differs from the
# logical image, not changes to the logical image itself.
PANEL_MIRROR_X = True


@dataclass(frozen=True)
class RenderSettings:
    mode: str = TWO_TONE
    threshold: int = 128
    color_on: tuple = (255, 255, 255)
    color_off: tuple = (0, 0, 0)
    smooth: bool = False       # ramp between color_off/color_on instead of hard threshold
    brightness: int = 100      # percent, 0-100
    invert: bool = False       # flip every channel (255-x) on the final composed image
    border: bool = False       # draw a 1px frame around the panel edge in border_color
    border_color: "tuple | None" = None  # None -> match color_on (the primary/on color)

    def replace(self, **changes) -> "RenderSettings":
        return replace(self, **changes)


DEFAULTS = RenderSettings()


def render_rgb(img: LoadedImage, settings: RenderSettings) -> np.ndarray:
    """Return the (MATRIX_HEIGHT, MATRIX_WIDTH, 3) uint8 array the panel
    will display for this image under these settings."""
    if settings.mode == FULL_COLOR:
        if img.rgb is None:
            raise ValueError(
                "full_color mode requires a source image with real color data "
                "(a raster image, not a hex dump) -- use two_tone mode instead"
            )
        rgb = img.rgb.astype(np.float32)
    else:
        on = np.array(settings.color_on, dtype=np.float32)
        off = np.array(settings.color_off, dtype=np.float32)
        gray = img.gray.astype(np.float32)
        if settings.smooth:
            t = (gray / 255.0)[..., None]
            rgb = off + (on - off) * t
        else:
            mask = (gray >= settings.threshold)[..., None]
            rgb = np.where(mask, on, off)

    if settings.border:
        # rgb is always a freshly-allocated array by this point (astype()
        # and the two arithmetic/np.where branches above all allocate) --
        # no aliasing of img.rgb or the on/off arrays, so writing into it
        # directly is safe without an extra copy.
        border_rgb = settings.border_color if settings.border_color is not None else settings.color_on
        border_color = np.array(border_rgb, dtype=np.float32)
        rgb[0, :, :] = border_color
        rgb[-1, :, :] = border_color
        rgb[:, 0, :] = border_color
        rgb[:, -1, :] = border_color

    if settings.invert:
        # Applied to the whole composed image (border included) rather than
        # just the mode-derived content, so what you see in the preview --
        # which is rendered from this exact same array -- is always exactly
        # what gets sent, border and all.
        rgb = 255.0 - rgb

    rgb = rgb * (settings.brightness / 100.0)
    return np.clip(rgb + 0.5, 0, 255).astype(np.uint8)


def to_payload(rgb: np.ndarray) -> bytes:
    """Flatten an (H, W, 3) RGB array to the 768-byte row-major R,G,B wire
    payload. Explicit ascontiguousarray so a future transpose/slice upstream
    can't silently reorder the bytes on the wire."""
    if PANEL_MIRROR_X:
        rgb = rgb[:, ::-1, :]
    return np.ascontiguousarray(rgb, dtype=np.uint8).tobytes()


def blank_payload() -> bytes:
    """768-byte all-zero payload -- every LED off. Used to blank the panel
    (e.g. on GUI close) without needing a loaded image to render from."""
    return bytes(MATRIX_WIDTH * MATRIX_HEIGHT * 3)


def to_preview_image(rgb: np.ndarray, scale: int = 24) -> Image.Image:
    """Scale an (H, W, 3) RGB array up for on-screen display. NEAREST is
    mandatory here -- any smoothing would make the preview lie about where
    pixel boundaries actually fall on the physical matrix."""
    img = Image.fromarray(rgb, "RGB")
    return img.resize((MATRIX_WIDTH * scale, MATRIX_HEIGHT * scale), Image.NEAREST)
