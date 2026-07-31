"""
Send Image to LED Matrix (CLI)
================================
Pushes a static image to an ESP32-driven NeoPixel LED matrix over a direct
USB serial connection. For interactive use (live color/brightness
adjustment with a preview), see gui.py instead -- this is the scriptable,
one-shot command-line entry point, built on the same image_loader.py /
renderer.py / matrix_link.py that the GUI uses.

Run:
    python send_image.py                                  # sends IMAGE_PATH with default rendering
    python send_image.py images/attempt1.txt              # send a specific file
    python send_image.py logo.png --full-color --brightness 40
    python send_image.py --port COM5 --color-on 255,0,0 --threshold 90
    python send_image.py --list-ports

Default rendering (two-tone, threshold 128, white/black, brightness 100%)
reproduces what the original grayscale-only firmware always did.

--------------------------------------------------------------------------
Requires the RGB888 firmware (v2)
--------------------------------------------------------------------------
This talks the RGB wire protocol in matrix_link.py -- MAGIC + TYPE + 768
RGB bytes + checksum -- which the ESP32 must be running led_matrix_esp.ino
v2 or later to understand. Older grayscale-only firmware will not respond
correctly; reflash if send_once() reports no response and a ping doesn't
return the "OK: led-matrix RGB888 16x16 v2" banner.
--------------------------------------------------------------------------
"""

import argparse
import os
import sys

import image_loader
import matrix_link
import renderer

# Default image to send if no path is given on the command line.
IMAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images", "attempt1.txt")

# Direct USB serial connection to the ESP32. Find the port in Windows'
# Device Manager under "Ports (COM & LPT)" once the board is plugged in
# (shows as something like "Silicon Labs CP210x" or "USB-SERIAL CH340").
# Only one program can hold the port open at a time -- close the Arduino
# Serial Monitor before running this. Override with --port instead of
# editing this if you'd rather not touch the file.
SERIAL_PORT = "COM3"


def _color_type(s: str) -> tuple:
    parts = s.split(",")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f"expected R,G,B (e.g. 255,0,0), got {s!r}")
    try:
        r, g, b = (int(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected three integers R,G,B, got {s!r}")
    if not all(0 <= v <= 255 for v in (r, g, b)):
        raise argparse.ArgumentTypeError(f"color channel values must be 0-255, got {s!r}")
    return (r, g, b)


def _int_range_type(lo: int, hi: int):
    def _parse(s: str) -> int:
        v = int(s)
        if not (lo <= v <= hi):
            raise argparse.ArgumentTypeError(f"must be between {lo} and {hi}, got {v}")
        return v
    return _parse


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Push a static image to the ESP32 LED matrix over USB serial.")
    parser.add_argument(
        "image", nargs="?", default=IMAGE_PATH,
        help=f"path to the image file to send (default: {IMAGE_PATH})")
    parser.add_argument(
        "--port", default=SERIAL_PORT,
        help=f"serial port the ESP32 is on (default: {SERIAL_PORT})")
    parser.add_argument(
        "--baud", type=int, default=matrix_link.BAUD_RATE,
        help="baud rate; must match Serial.begin() in led_matrix_esp.ino (default: %(default)s)")
    parser.add_argument(
        "--list-ports", action="store_true",
        help="list available serial ports and exit")
    parser.add_argument(
        "--full-color", action="store_true",
        help="use the source image's actual colors instead of two-tone thresholding "
             "(raster images only -- hex dumps carry no color data)")
    parser.add_argument(
        "--threshold", type=_int_range_type(0, 255), default=renderer.DEFAULTS.threshold,
        metavar="0-255", help="two-tone brightness cutoff (default: %(default)s)")
    parser.add_argument(
        "--smooth", action="store_true",
        help="ramp between --color-off and --color-on instead of hard-thresholding "
             "(two-tone mode only)")
    parser.add_argument(
        "--color-on", type=_color_type, default=renderer.DEFAULTS.color_on,
        metavar="R,G,B", help="two-tone 'on' color (default: %(default)s)")
    parser.add_argument(
        "--color-off", type=_color_type, default=renderer.DEFAULTS.color_off,
        metavar="R,G,B", help="two-tone 'off' color (default: %(default)s)")
    parser.add_argument(
        "--brightness", type=_int_range_type(0, 100), default=renderer.DEFAULTS.brightness,
        metavar="0-100", help="output brightness percent, applied last (default: %(default)s)")
    parser.add_argument(
        "--invert", action="store_true",
        help="swap --color-on and --color-off (and whichever one the border falls back to)")
    parser.add_argument(
        "--border", action="store_true",
        help="draw a 1px border around the panel edge in --border-color")
    parser.add_argument(
        "--border-color", type=_color_type, default=renderer.DEFAULTS.border_color,
        metavar="R,G,B", help="border color, only used with --border "
                               "(default: matches --color-on)")
    parser.add_argument(
        "--background", type=_color_type, default=(0, 0, 0),
        metavar="R,G,B", help="color transparent pixels are composited onto "
                               "(raster images with an alpha channel only, default: %(default)s)")
    return parser.parse_args(argv)


def _print_ports():
    ports = matrix_link.list_ports()
    if not ports:
        print("No serial ports found at all -- is the ESP32 plugged in?")
        return
    for device, description in ports:
        print(f"{device}  ({description})")


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.list_ports:
        _print_ports()
        return 0

    try:
        img = image_loader.load_image(args.image, background=args.background)
    except image_loader.ImageLoadError as e:
        print(f"Error: could not load image from {args.image}: {e}")
        return 1

    settings = renderer.DEFAULTS.replace(
        mode=renderer.FULL_COLOR if args.full_color else renderer.TWO_TONE,
        threshold=args.threshold,
        color_on=args.color_on,
        color_off=args.color_off,
        smooth=args.smooth,
        brightness=args.brightness,
        invert=args.invert,
        border=args.border,
        border_color=args.border_color,
    )

    try:
        rgb = renderer.render_rgb(img, settings)
    except ValueError as e:
        print(f"Error: {e}")
        return 1

    rgb = renderer.apply_brightness(rgb, args.brightness)
    payload = renderer.to_payload(rgb)

    try:
        ack = matrix_link.send_once(args.port, payload, baud=args.baud)
        print(f"Sent image frame ({len(payload)} bytes) from {args.image} to {args.port}")
        if ack:
            print(f"[ESP32] {ack}")
        else:
            print("No response from ESP32 within a few seconds -- "
                  "check its power/connection if the matrix didn't update.")
    except matrix_link.MatrixLinkError as e:
        print(f"Error: failed to send image over serial ({args.port}): {e}")
        _print_ports()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
