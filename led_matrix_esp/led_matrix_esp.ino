#include <Adafruit_NeoPixel.h>

#define LED_PIN 5
#define MATRIX_WIDTH 16
#define MATRIX_HEIGHT 16
#define NUMPIXELS (MATRIX_WIDTH * MATRIX_HEIGHT)
#define FRAME_BYTES (NUMPIXELS * 3)  // RGB888 payload: 3 bytes/pixel, row-major
#define FRAME_TIMEOUT_MS 2000        // abort a partially-received frame after this long

// Wire protocol markers -- must match MAGIC/TYPE_PING/TYPE_RGB in
// matrix_link.py. Frame shape: MAGIC(2) + TYPE(1) + payload (FRAME_BYTES
// for TYPE_RGB, empty for TYPE_PING) + 1 checksum byte
// ((TYPE + sum(payload)) mod 256).
//
// A 2-byte magic (rather than the older protocol's single sync byte)
// matters because an RGB payload can contain any byte value at all -- a
// one-byte marker would be a real resync hazard, since a single dropped
// byte could make the scanner latch onto the first matching byte inside
// pixel data and render garbage. 0xA5 0x5A needs two specific adjacent
// values to false-trigger; the checksum is the backstop either way.
#define MAGIC_0 0xA5
#define MAGIC_1 0x5A
#define TYPE_PING 0x00
#define TYPE_RGB 0x01

// Printed for both a successful RGB frame ack and a ping reply, so the PC
// can tell this firmware apart from the older grayscale-only protocol
// instead of the two silently talking past each other.
#define FIRMWARE_ID "OK: led-matrix RGB888 16x16 v2"

// Most pre-made 16x16 WS2812 panels wire alternate rows in reverse
// (serpentine/zigzag) to shorten the trace run between rows, rather than
// every row running left-to-right (raster). If the image comes out
// looking scrambled/shredded despite a valid checksum, flip this and
// reflash -- it's the classic tell. This is a physical property of the
// panel's wiring, not a rendering style choice, which is why it lives
// here rather than being one of the (now PC-side) rendering settings.
#define MATRIX_SERPENTINE true

// All color rendering -- thresholding, custom on/off colors, brightness --
// now happens on the PC (see renderer.py). This firmware is a dumb RGB
// framebuffer: it just displays whatever RGB888 frame it's handed.
Adafruit_NeoPixel pixels(NUMPIXELS, LED_PIN, NEO_GRB + NEO_KHZ800);

uint8_t frameBuffer[FRAME_BYTES];

void setColor(uint8_t r, uint8_t g, uint8_t b) {
  for (int i = 0; i < NUMPIXELS; i++) {
    pixels.setPixelColor(i, pixels.Color(r, g, b));
  }
  pixels.show();
}

// Renders a raw RGB888 frame (FRAME_BYTES bytes, row-major R,G,B per pixel,
// top-left to bottom-right) pushed from the PC -- see matrix_link.py.
// Remaps row-major source coordinates onto the matrix's actual physical
// wiring order (see MATRIX_SERPENTINE above) before writing.
void displayRgbFrame(const uint8_t* rgb) {
  for (int y = 0; y < MATRIX_HEIGHT; y++) {
    for (int x = 0; x < MATRIX_WIDTH; x++) {
      int physX = (MATRIX_SERPENTINE && (y % 2 == 1)) ? (MATRIX_WIDTH - 1 - x) : x;
      const uint8_t* p = &rgb[(y * MATRIX_WIDTH + x) * 3];
      pixels.setPixelColor(y * MATRIX_WIDTH + physX, pixels.Color(p[0], p[1], p[2]));
    }
  }
  pixels.show();
}

// Blocks until one byte is available, or returns -1 once more than
// timeoutMs has passed since `start`. Subtraction-based (rather than
// comparing against a precomputed deadline) so it stays correct across a
// millis() rollover (~49 days uptime), matching Arduino's usual idiom.
int readByteUntil(unsigned long start, unsigned long timeoutMs) {
  while (!Serial.available()) {
    if (millis() - start > timeoutMs) return -1;
  }
  return Serial.read();
}

// Reads `length` payload bytes into `out` plus 1 trailing checksum byte,
// verifying it against `typeByte` + the payload sum. Serial has no packet
// boundaries the way UDP did, so the checksum is what catches a corrupted
// or desynced frame instead of silently drawing garbage. Called with
// length=0 for a ping, which just validates its (empty-payload) checksum.
bool readFrame(uint8_t* out, size_t length, uint8_t typeByte) {
  unsigned long start = millis();
  uint8_t sum = typeByte;
  for (size_t i = 0; i < length; i++) {
    int b = readByteUntil(start, FRAME_TIMEOUT_MS);
    if (b < 0) return false;
    out[i] = (uint8_t)b;
    sum += out[i];
  }
  int checksum = readByteUntil(start, FRAME_TIMEOUT_MS);
  if (checksum < 0) return false;
  return sum == (uint8_t)checksum;
}

// Discards whatever's currently buffered -- used after a failed/unknown
// frame so a desync or stray trailing byte can't cascade into misreading
// whatever comes next.
void drainInput() {
  while (Serial.available()) Serial.read();
}

void setup() {
  // Must precede begin() -- called after, it's silently ignored. Default
  // is 256 bytes, which a 772-byte RGB frame would overrun; comfortably
  // covers a full frame arriving faster than loop() drains it (e.g. while
  // pixels.show()'s ~8ms blocks the very next read).
  Serial.setRxBufferSize(1024);
  Serial.begin(115200);
  delay(1000);

  pixels.begin();
  setColor(0, 0, 0);  // start with LEDs off

  Serial.println("ESP32 LED Matrix starting...");
  Serial.println("Ready. Waiting for frames over serial.");
}

void loop() {
  static uint8_t prevByte = 0;

  if (!Serial.available()) return;
  uint8_t b = Serial.read();
  if (!(prevByte == MAGIC_0 && b == MAGIC_1)) {
    prevByte = b;
    return;  // no magic sequence yet -- keep scanning
  }
  prevByte = 0;  // magic consumed; don't let it immediately re-match

  unsigned long start = millis();
  int type = readByteUntil(start, FRAME_TIMEOUT_MS);
  if (type < 0) {
    Serial.println("ERR: frame timeout or checksum mismatch");
    return;
  }

  if (type == TYPE_PING) {
    uint8_t unused;
    if (readFrame(&unused, 0, (uint8_t)type)) {
      Serial.println(FIRMWARE_ID);
    } else {
      Serial.println("ERR: frame timeout or checksum mismatch");
    }
    return;
  }

  if (type != TYPE_RGB) {
    drainInput();
    Serial.println("ERR: frame timeout or checksum mismatch");
    return;
  }

  if (readFrame(frameBuffer, FRAME_BYTES, (uint8_t)type)) {
    displayRgbFrame(frameBuffer);
    Serial.println("OK: displayed RGB frame");
  } else {
    drainInput();
    Serial.println("ERR: frame timeout or checksum mismatch");
  }
}
