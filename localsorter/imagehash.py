"""Card image normalisation and perceptual hashing (numpy + Pillow only).

The sorter uploads a fixed crop of the camera frame: the card plus some
background, at an unknown orientation. We find the card outline (OpenCV, if
installed, which it is on the sorter image), warp it to a portrait rectangle,
and compute two DCT perceptual hashes:

* full: 16x16 = 256 bits over the whole card (frame, layout, text box)
* art:   8x8  =  64 bits over the illustration box

Scryfall's reference images get the exact same treatment, so the Hamming
distance between hashes measures visual difference.
"""
import io

import numpy as np
from PIL import Image, ImageOps

try:
    import cv2
except ImportError:  # pragma: no cover - exercised on machines without OpenCV
    cv2 = None

# Normalised card size: 63x88 mm ratio
CARD_W, CARD_H = 252, 352
CARD_RATIO = CARD_W / CARD_H

# Illustration box of a modern-frame MTG card (fractions of width/height).
# Old frames are close enough for a coarse 8x8 hash
ART_BOX = (0.08, 0.11, 0.92, 0.55)

FULL_BITS = 256
ART_BITS = 64
FULL_BYTES = FULL_BITS // 8
ART_BYTES = ART_BITS // 8

_POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint16)


def _dct_matrix(n):
    k = np.arange(n)
    m = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n))
    m[0] *= 1 / np.sqrt(2)

    return m * np.sqrt(2 / n)


_DCT = {n: _dct_matrix(n) for n in (32, 64)}


def phash(image, hash_size, sample=None):
    """Classic pHash: grayscale, downscale, 2D DCT, keep the low frequencies,
    threshold on their median. Returns a flat bool array of hash_size**2."""
    sample = sample or hash_size * 4
    gray = image.convert("L").resize((sample, sample), Image.LANCZOS)
    pixels = np.asarray(gray, dtype=np.float64)
    dct = _DCT[sample]
    freq = dct @ pixels @ dct.T
    low = freq[:hash_size, :hash_size].flatten()
    # Skip the DC term when computing the median: it only encodes brightness
    median = np.median(low[1:])

    return low > median


def pack(bits):
    return np.packbits(bits.astype(np.uint8)).tobytes()


def card_hashes(card_image):
    """(full, art) packed hashes of an already normalised portrait card."""
    if card_image.size != (CARD_W, CARD_H):
        card_image = card_image.resize((CARD_W, CARD_H), Image.LANCZOS)

    w, h = card_image.size
    art = card_image.crop((int(ART_BOX[0] * w), int(ART_BOX[1] * h),
                           int(ART_BOX[2] * w), int(ART_BOX[3] * h)))

    return pack(phash(card_image, 16, 64)), pack(phash(art, 8, 32))


def load_image(data, min_side=None):
    """Decodes an image. With min_side, JPEGs are decoded at a reduced scale
    (libjpeg DCT scaling, much faster than decode + resize) while keeping the
    longest side at least min_side pixels."""
    image = Image.open(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)

    if min_side and image.format == "JPEG":
        scale = max(1, max(image.size) // min_side)
        image.draft("RGB", (image.width // scale, image.height // scale))

    # Camera frames carry no EXIF orientation; skip the extra copy then
    if image.getexif().get(0x0112, 1) != 1:
        image = ImageOps.exif_transpose(image)

    return image.convert("RGB")


def reference_hashes(data):
    """Hashes of a Scryfall card image (already tightly cropped, portrait)."""
    image = load_image(data)

    # Battle and split cards are printed sideways; Scryfall serves some landscape
    if image.width > image.height:
        image = image.rotate(90, expand=True)

    return card_hashes(image)


def _order_corners(pts):
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()

    return np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                     pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def find_card(image):
    """Locates the card in a camera crop and returns it warped to portrait
    CARD_W x CARD_H. Falls back to a centred crop at card ratio when no
    convincing outline is found (or OpenCV is unavailable)."""
    if cv2 is not None:
        warped = _find_card_cv(image)

        if warped is not None:
            return warped

    return _center_crop(image)


def _find_card_cv(image):
    rgb = np.asarray(image)
    scale = 800.0 / max(rgb.shape[:2])
    small = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    scale = min(scale, 1.0)

    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(gray, 30, 100)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=2)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    area_total = small.shape[0] * small.shape[1]
    best = None

    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        area = cv2.contourArea(contour)

        if area < 0.15 * area_total:
            break

        rect = cv2.minAreaRect(contour)
        (rw, rh) = rect[1]

        if not rw or not rh:
            continue

        ratio = min(rw, rh) / max(rw, rh)

        # Card ratio is 0.716; allow for perspective and sleeve slop
        if abs(ratio - CARD_RATIO) > 0.08:
            continue

        best = cv2.boxPoints(rect)
        break

    if best is None:
        return None

    corners = _order_corners(best / scale)
    width = np.linalg.norm(corners[1] - corners[0])
    height = np.linalg.norm(corners[3] - corners[0])

    if width > height:
        # Landscape on camera: rotate corner order so the long side is vertical
        corners = np.roll(corners, -1, axis=0)

    target = np.array([[0, 0], [CARD_W - 1, 0], [CARD_W - 1, CARD_H - 1], [0, CARD_H - 1]], dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(corners, target)
    warped = cv2.warpPerspective(rgb, matrix, (CARD_W, CARD_H), flags=cv2.INTER_AREA)

    return Image.fromarray(warped)


def _center_crop(image):
    if image.width > image.height:
        image = image.rotate(90, expand=True)

    w, h = image.size

    if w / h > CARD_RATIO:
        new_w = int(h * CARD_RATIO)
        left = (w - new_w) // 2
        image = image.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / CARD_RATIO)
        top = (h - new_h) // 2
        image = image.crop((0, top, w, top + new_h))

    return image.resize((CARD_W, CARD_H), Image.LANCZOS)


def query_hashes(data):
    """Hashes of a sorter scan for both upright and upside-down orientation.

    Returns a list of (full, art) pairs. The warp leaves the card portrait but
    can't know which end is the top, so we try 0 and 180 degrees."""
    # The card ends up 252x352: ~1000 px of camera frame is plenty
    card = find_card(load_image(data, min_side=1000))

    return [card_hashes(card), card_hashes(card.rotate(180))]


def hamming(query, matrix):
    """Hamming distance between one packed hash and every row of a packed
    uint8 matrix (N x bytes)."""
    q = np.frombuffer(query, dtype=np.uint8)

    return _POPCOUNT[np.bitwise_xor(matrix, q)].sum(axis=1)
