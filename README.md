# LED Matrix

Push a static image to a 256-LED NeoPixel matrix from a PC over a direct
USB serial connection, either from a GUI (color pickers, brightness slider,
live preview) or a one-shot CLI script. This is a static, on-demand
project: there's no live detection loop and no requirement that the PC and
ESP32 stay continuously connected.

## How it works

```
 image file --> image_loader.py --> renderer.py --> matrix_link.py --> USB serial --> ESP32 --> NeoPixel matrix
                                          |                                        <-- OK/ERR line --
                                          v
                                  gui.py's live preview
```

The pipeline is split into small, reusable pieces:

- **`image_loader.py`** loads an image (a hex-literal dump or a regular
  PNG/JPG/etc.) into a 16x16 grayscale array, plus a color array when the
  source actually has one.
- **`renderer.py`** turns that plus your chosen settings (two-tone vs. full
  color, threshold, colors, brightness) into the exact RGB array the panel
  will show. This one function feeds *both* the wire payload and the
  on-screen preview, so the preview can never drift from what the panel
  actually displays.
- **`matrix_link.py`** owns the serial connection and speaks the wire
  protocol (framing, checksums, acks).
- **`gui.py`** is the interactive control panel: pick colors, drag
  brightness/threshold sliders with a live preview, and push updates to
  the matrix in real time or with an explicit Send button.
- **`send_image.py`** is a scriptable one-shot CLI over the same pipeline,
  for automation or quick sends without opening the GUI.
- **`led_matrix_esp/led_matrix_esp.ino`** (runs on the ESP32) is a dumb RGB
  framebuffer: it just renders whatever RGB888 frame it's handed. All
  color/brightness decisions happen on the PC now, so nothing on the panel
  requires a reflash to change.

### Image format

Either a GUI "Open Image..." or a CLI path argument accepts:

- **Any regular image** (`.png`, `.jpg`, `.jpeg`, `.bmp`, `.gif`, `.webp`) —
  loaded via Pillow, resized to the matrix automatically.
- **A hex-literal dump** (`.h`, `.hpp`, `.c`, `.inc`, `.txt`) — either
  image2cpp-style (`..._width`/`..._height` declarations plus a packed
  pixel array at 1, 4, or 8 bits per pixel, auto-detected from the byte
  count) at any resolution, or a flat 256-byte grayscale dump with no
  metadata, matching the matrix 1:1. The parser just scans for `0xNN`
  tokens, so it doesn't care about the surrounding syntax.

See [`images/README.md`](images/README.md) for hex-format detail, and drop
your own files in `images/`.

### Rendering modes

- **Two-tone** (default) — each pixel is thresholded and rendered as one
  of two solid colors (`color_on`/`color_off`, default white/black).
  Good for markers, logos, and text, where intermediate gray from
  resizing/anti-aliasing just looks murky. Check "Smooth ramp" (GUI) or
  pass `--smooth` (CLI) to ramp between the two colors instead of hard
  thresholding -- this degrades to plain grayscale when the colors are
  black/white.
- **Full color** — shows the source image's actual colors. Only available
  for raster images; hex dumps carry no color data.

Brightness is applied last, as a uniform scale on the final RGB output --
never to the grayscale input -- so it can't interact with the threshold
decision.

### Invert, border, and background

- **Invert colors** — flips every channel (`255-x`) of the final composed
  image (mode output and border both), right before brightness is applied.
  CLI: `--invert`.
- **Border** — a 1px frame around the outer edge of the panel, toggled on
  its own (GUI: "Border" button; CLI: `--border`). It always matches the
  Primary Color (`--color-on`) unless overridden with `--border-color
  R,G,B` on the CLI -- the GUI has no separate border-color picker on
  purpose, since "border matches the primary squares" is the point.
- **Background** (GUI: "Image" box; CLI: `--background R,G,B`) — the color
  used for *every* blank/off pixel, not just transparent ones: it's both
  the two-tone off color and what transparent PNG areas are composited
  onto. In the GUI it's a toggle -- on uses the color swatch next to it,
  off turns those LEDs off (black). Changing it (or the toggle) reloads
  the current image, since transparency compositing needs the original
  alpha channel, which is gone once an image is cached as flattened RGB --
  the GUI caches the decoded source per file so this recomposites in
  memory rather than re-reading the file from disk every time. The CLI's
  `--color-off` and `--background` remain independent flags (no on/off
  toggle to unify there), so the two front ends aren't in lockstep here --
  intentional, not drift.

### Connection status dot

The Send button next to it is always clickable -- it's not gated on being
connected -- and instead reports "Not connected."/"No image loaded." in the
status bar if you click it without either. The dot is the at-a-glance
version of that: green while connected, gray otherwise.

### Blanking on exit

Closing the GUI window sends one all-off frame before disconnecting, so the
panel goes dark instead of continuing to show whatever was last pushed.

## Hardware

- ESP32 DevKit V1 (or similar), connected to the PC by USB
- WS2812/NeoPixel matrix, 256 pixels (16x16), data line on GPIO 5 (`LED_PIN`)
- A stable 5V supply for the ESP32 + LED matrix

## Setup

### ESP32 firmware

1. Open `led_matrix_esp/led_matrix_esp.ino` in the Arduino IDE.
2. Install the `Adafruit NeoPixel` library (Library Manager).
3. Flash it, then open the Serial Monitor at 115200 baud to confirm it
   prints `Ready. Waiting for frames over serial.` — then close the Serial
   Monitor again (only one program can hold the port open at a time, and
   the GUI/CLI needs it next).

This firmware speaks an RGB888 protocol (see `matrix_link.py`'s module
docstring for the exact framing) and identifies itself as
`OK: led-matrix RGB888 16x16 v2` in response to a ping. If you have an
older grayscale-only build flashed, reflash before using the GUI/CLI --
the two don't interoperate.

### Python side

```
pip install -r requirements.txt
```

Find the ESP32's COM port in Windows' Device Manager, under
"Ports (COM & LPT)", once it's plugged in (shows as something like
"Silicon Labs CP210x" or "USB-SERIAL CH340").

**GUI** (recommended for interactive use):

```
python gui.py
```

Pick the port from the dropdown and hit Connect, then Open Image, adjust
colors/threshold/brightness, and either drag with "Live update" checked
(pushes to the matrix as you go) or use the Send button. The preview
canvas always shows exactly what the render settings will produce.

**CLI** (for scripting or a quick one-off send):

```
python send_image.py                                    # sends the default image
python send_image.py images/attempt1.txt                # or a specific file
python send_image.py logo.png --full-color --brightness 40
python send_image.py --port COM5 --color-on 255,0,0 --threshold 90
python send_image.py logo.png --invert --border --border-color 0,255,0
python send_image.py logo.png --background 0,255,0        # composite transparent pixels onto green
python send_image.py --list-ports
```

Set `SERIAL_PORT` at the top of `send_image.py` if you'd rather not pass
`--port` every time. If the port can't be opened, both the GUI and CLI
list the serial ports they actually found to help you pick the right one.

## Tuning knobs

`MATRIX_WIDTH`/`MATRIX_HEIGHT` (top of `image_loader.py`, and again in
`led_matrix_esp.ino`) describe the physical matrix; their product must
match `NUMPIXELS` in the firmware. `matrix_link.BAUD_RATE` must match
`Serial.begin()` there too. `matrix_link.ACK_TIMEOUT_S` controls how long
a send waits for the ESP32's ack before giving up (not fatal -- the frame
likely still displayed); `BOOT_SETTLE_S` is how long the CLI waits after
opening the port before writing, since opening it resets most ESP32
boards (the GUI instead pings and retries after this delay, rather than
blocking on it unconditionally). Both the CLI and GUI pin DTR/RTS low
*before* opening the port so a subsequent close doesn't trigger a second
reset that would blank whatever the panel was just showing.

`renderer.PANEL_MIRROR_X` (top of `renderer.py`, default `True` for this
panel) flips the payload left-right in `to_payload()` only -- the preview
is unaffected, same as `MATRIX_SERPENTINE` below -- for panels physically
mounted mirrored relative to how the image looks on screen. Flip it back to
`False` if you're driving an unmirrored panel.

`MATRIX_SERPENTINE` (top of `led_matrix_esp.ino`) accounts for how the
panel is physically wired. Most pre-made 16x16 WS2812 panels wire
alternate rows in reverse (serpentine/zigzag) rather than every row
running left-to-right (raster) -- if a pushed image comes out completely
scrambled despite a valid checksum, flip this constant and reflash. This
lives in firmware (not as a PC-side setting) because it's a property of
the panel's wiring, not a rendering style choice -- the GUI's preview
always shows the logical (un-remapped) image, so "preview looks right but
the panel is scrambled" means this constant, not a bug in the Python side.
