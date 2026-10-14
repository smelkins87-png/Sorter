"""Synthetic MTG-like cards and fake sorter photos for tests.

These exercise the pipeline mechanics (card detection, warping, hashing,
search, decisions). They do NOT measure real-world accuracy: that needs real
scans from the sorter (see `python3 -m localsorter eval`).
"""
import io
import random

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

W, H = 488, 680


def make_card(seed, art_seed=None):
    """A card image. Cards sharing art_seed share the illustration (reprints)."""
    rng = random.Random(seed)
    art_rng = random.Random(art_seed if art_seed is not None else seed)

    frame = tuple(rng.randint(60, 230) for _ in range(3))
    img = Image.new("RGB", (W, H), (15, 15, 15))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((18, 18, W - 18, H - 18), radius=18, fill=frame)

    # Name bar and type bar with "text"
    for top in (34, 382):
        d.rectangle((34, top, W - 34, top + 34), fill=(235, 230, 215))
        x = 44
        while x < W - 120:
            w = rng.randint(12, 45)
            d.rectangle((x, top + 10, x + w, top + 24), fill=(20, 20, 20))
            x += w + rng.randint(5, 12)

    # Illustration
    art = (40, 76, W - 40, 372)
    d.rectangle(art, fill=tuple(art_rng.randint(0, 255) for _ in range(3)))
    for _ in range(14):
        x0 = art_rng.randint(art[0], art[2] - 20)
        y0 = art_rng.randint(art[1], art[3] - 20)
        x1 = min(art[2], x0 + art_rng.randint(20, 220))
        y1 = min(art[3], y0 + art_rng.randint(20, 180))
        colour = tuple(art_rng.randint(0, 255) for _ in range(3))

        if art_rng.random() < 0.5:
            d.ellipse((x0, y0, x1, y1), fill=colour)
        else:
            d.polygon([(x0, y1), ((x0 + x1) // 2, y0), (x1, y1)], fill=colour)

    # Rules text box
    d.rectangle((40, 424, W - 40, H - 60), fill=(240, 235, 220))
    y = 436
    while y < H - 80:
        x = 52
        while x < W - 80:
            w = rng.randint(10, 50)
            d.rectangle((x, y, x + w, y + 9), fill=(30, 30, 30))
            x += w + rng.randint(5, 10)
        y += 20

    return img


def jpeg(img, quality=80):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)

    return buf.getvalue()


def reference_bytes(card):
    """What Scryfall serves as the "small" image."""
    return jpeg(card.resize((146, 204), Image.LANCZOS), 75)


def photo(card, seed=0, angle=None, upside_down=False, landscape=False, brightness=None):
    """Simulates the sorter's cropped camera frame of a card."""
    rng = random.Random(seed)
    canvas = Image.new("RGB", (1300, 1000), (rng.randint(150, 200),) * 3)
    noise = Image.effect_noise((1300, 1000), 12).convert("RGB")
    canvas = Image.blend(canvas, noise, 0.15)

    shot = card.resize((int(W * 1.25), int(H * 1.25)), Image.BICUBIC)

    if upside_down:
        shot = shot.rotate(180)

    if landscape:
        shot = shot.rotate(90, expand=True)

    angle = rng.uniform(-3, 3) if angle is None else angle
    shot = shot.convert("RGBA").rotate(angle, expand=True, resample=Image.BICUBIC)

    x = (canvas.width - shot.width) // 2 + rng.randint(-40, 40)
    y = (canvas.height - shot.height) // 2 + rng.randint(-40, 40)
    canvas.paste(shot, (x, y), shot)

    factor = rng.uniform(0.8, 1.15) if brightness is None else brightness
    canvas = ImageEnhance.Brightness(canvas).enhance(factor)
    canvas = canvas.filter(ImageFilter.GaussianBlur(0.8))

    return jpeg(canvas, 85)
