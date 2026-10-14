"""Synthetic MTG-like cards and fake sorter photos for tests.

These exercise the pipeline mechanics (card detection, warping, hashing,
search, decisions). They do NOT measure real-world accuracy: that needs real
scans from the sorter (see `python3 -m localsorter eval`).
"""
import io
import math
import random

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

W, H = 488, 680

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
RARITY_COLOURS = {"common": (25, 25, 25), "uncommon": (165, 180, 195), "rare": (205, 165, 60),
                  "mythic": (225, 95, 30)}
RARITY_LETTER = {"common": "C", "uncommon": "U", "rare": "R", "mythic": "M"}


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


def make_printing(seed, set_code, number, rarity="common", symbol_seed=None, total=None, lang="EN",
                  foil=False, frame="2015", art_seed=None, scale=3, artist="Jane Doe"):
    """A specific printing: same card + art as make_card(seed, art_seed), plus
    a set symbol and the collector info of its frame, drawn sharply at
    `scale` x the reference size (real sorter photos are ~3x Scryfall's
    "normal" image)."""
    base = make_card(seed, art_seed if art_seed is not None else seed)
    img = base.resize((W * scale, H * scale), Image.BICUBIC)
    d = ImageDraw.Draw(img)
    s = scale

    # Set symbol at the right end of the type line, in rarity colour
    rng = random.Random(symbol_seed if symbol_seed is not None else set_code)
    cx, cy, radius = 0.885 * W * s, 0.587 * H * s, 0.032 * W * s
    points = []
    count = rng.randint(5, 9)
    for k in range(count):
        angle = 2 * math.pi * k / count + rng.uniform(-0.3, 0.3)
        r = radius * rng.uniform(0.45, 1.0)
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    d.polygon(points, fill=RARITY_COLOURS[rarity], outline=(0, 0, 0), width=max(1, s))

    if frame in ("2015", "2003"):
        # Thick black bottom border carrying the info text
        d.rectangle((0, int(0.915 * H * s), W * s, H * s), fill=(12, 12, 12))
        font = ImageFont.truetype(FONT, int(0.019 * H * s))
        x = int(0.045 * W * s)
        y1, y2 = int(0.922 * H * s), int(0.953 * H * s)
        white = (235, 235, 235)

        if frame == "2015":
            line1 = ("%s/%s %s" % (number, total, RARITY_LETTER[rarity])) if total else \
                    "%s %s" % (number, RARITY_LETTER[rarity])
            line2 = "%s %s %s  %s" % (set_code, "\u2605" if foil else "\u2022", lang, artist)
            d.text((x, y1), line1, font=font, fill=white)
            d.text((x, y2), line2, font=font, fill=white)
            d.text((int(0.62 * W * s), y1), "\u2122 & \u00a9 2018 Wizards of the Coast", font=font, fill=white)
        else:
            d.text((x, y1), "Illus. " + artist, font=font, fill=white)
            right = "\u2122 & \u00a9 2009 Wizards  %s/%s" % (number, total or 249)
            width = d.textlength(right, font=font)
            d.text((W * s - x - width, y1), right, font=font, fill=white)

    return img


def reference(img, size=(W, H)):
    """A Scryfall-like reference image (JPEG bytes) of a printing."""
    return jpeg(img.resize(size, Image.LANCZOS), 85)


def jpeg(img, quality=80):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)

    return buf.getvalue()


def reference_bytes(card):
    """What Scryfall serves as the "small" image."""
    return jpeg(card.resize((146, 204), Image.LANCZOS), 75)


def photo(card, seed=0, angle=None, upside_down=False, landscape=False, brightness=None,
          card_width=None, canvas_size=(1300, 1000), blur=0.8):
    """Simulates the sorter's cropped camera frame of a card. The defaults
    give a small, quick image; card_width=1200 with canvas_size=(2150, 1850)
    matches the real camera crop."""
    rng = random.Random(seed)
    canvas = Image.new("RGB", canvas_size, (rng.randint(150, 200),) * 3)
    noise = Image.effect_noise(canvas_size, 12).convert("RGB")
    canvas = Image.blend(canvas, noise, 0.15)

    card_width = card_width or int(W * 1.25)
    shot = card.resize((card_width, int(card_width * H / W)), Image.BICUBIC)

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
    canvas = canvas.filter(ImageFilter.GaussianBlur(blur))

    return jpeg(canvas, 85)
