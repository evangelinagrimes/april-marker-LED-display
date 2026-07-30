"""
Generate ArUco Marker Images (CLI)
=====================================
Batch-generates ArUco marker PNGs sized to the LED matrix's native
resolution (see aruco_gen.py) and saves them under images/ as ordinary
static files -- open one with gui.py's "Open Image..." button (or its
"Generate ArUco Marker..." dialog, for one-off interactive use) or pass it
to send_image.py, the same as any other picture.

Run:
    python generate_aruco.py 3                    # DICT_4X4_50 id 3 -> images/
    python generate_aruco.py 0 --count 10          # ids 0-9
    python generate_aruco.py 5 --dictionary 5x5_100
    python generate_aruco.py --list-dictionaries
"""

import argparse
import os
import sys

import aruco_gen

IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Generate ArUco marker PNGs for the LED matrix.")
    parser.add_argument("id", type=int, nargs="?", help="starting marker ID")
    parser.add_argument(
        "--count", type=int, default=1,
        help="how many consecutive IDs to generate starting from `id` (default: 1)")
    parser.add_argument(
        "--dictionary", default=aruco_gen.DEFAULT_DICTIONARY,
        choices=sorted(aruco_gen.DICTIONARIES), metavar="NAME",
        help="ArUco dictionary (default: %(default)s); see --list-dictionaries for choices")
    parser.add_argument(
        "--border-bits", type=int, default=1,
        help="marker border width in modules (default: %(default)s)")
    parser.add_argument(
        "--out-dir", default=IMAGES_DIR,
        help="output directory (default: images/)")
    parser.add_argument(
        "--list-dictionaries", action="store_true",
        help="list dictionary names and their valid ID ranges, then exit")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.list_dictionaries:
        for name in sorted(aruco_gen.DICTIONARIES):
            size = aruco_gen.dictionary_size(name)
            print(f"{name}: {size} marker(s) (valid ids 0-{size - 1})")
        return 0

    if args.id is None:
        print("Error: an id is required unless --list-dictionaries is given")
        return 1

    for marker_id in range(args.id, args.id + args.count):
        try:
            gray = aruco_gen.generate_marker(args.dictionary, marker_id, border_bits=args.border_bits)
        except aruco_gen.ArucoGenError as e:
            print(f"Error: {e}")
            return 1
        path = aruco_gen.save_marker_png(gray, args.dictionary, marker_id, args.out_dir)
        print(f"Wrote {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
