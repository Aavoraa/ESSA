"""Draw the ESSA AI app icon at every size it is used.

    python backend/tools/make_icons.py

Kept as a script rather than as three PNGs somebody once exported, because the
icon is used in five places — the PWA manifest at two sizes, the favicon-ish
icon.png, the Android launcher and the iOS app icon — and hand-editing them separately is how they
end up disagreeing.

The mark is "ESSA AI" inside a ring, on one line and as large as the ring
allows — about six capitals is the most a home-screen icon (about 48px on a real
phone) can carry and still be read at a glance, which is what an icon is for.
"""
import pathlib

from PIL import Image, ImageDraw, ImageFont

BG = (11, 61, 46)          # #0B3D2E — the app's own dark forest-green chrome
FG = (255, 255, 255)       # #FFFFFF — text on chrome, as in its "E" logo chip

HERE = pathlib.Path(__file__).resolve().parent
MOBILE = HERE.parent / "app" / "mobile"
ANDROID = HERE.parent.parent / "android" / "app" / "src" / "main" / "res"
IOS = HERE.parent.parent / "ios" / "EssaAI" / "Assets.xcassets" / "AppIcon.appiconset"

# Windows ships these; the fallbacks keep the script working elsewhere.
FONTS = ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/seguibl.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def _font(size):
    for path in FONTS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _centred(draw, text, font, cx, cy):
    """Draw text centred on (cx, cy) using its real ink box.

    `textbbox` rather than `textlength`: capital letters carry no descender, so
    the nominal line height would sit the glyphs visibly high in the ring.
    """
    box = draw.textbbox((0, 0), text, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    draw.text((cx - w / 2 - box[0], cy - h / 2 - box[1]), text, font=font, fill=FG)


def render(size):
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)

    # The ring, inset enough to survive a launcher's circular mask.
    pad = size * 0.09
    d.ellipse([pad, pad, size - pad, size - pad],
              outline=FG, width=max(2, round(size * 0.018)))

    _centred(d, "ESSA AI", _font(round(size * 0.145)), size / 2, size / 2)
    return img


TARGETS = [
    (MOBILE / "icon-192.png", 192),
    (MOBILE / "icon-512.png", 512),
    (MOBILE / "icon.png", 192),
    (ANDROID / "mipmap-xhdpi" / "ic_launcher.png", 192),
    (ANDROID / "mipmap-xxxhdpi" / "ic_launcher.png", 512),
    (IOS / "AppIcon-1024.png", 1024),
]


def main():
    for path, size in TARGETS:
        if not path.parent.is_dir():
            print(f"  skip {path} — no such directory")
            continue
        render(size).save(path, "PNG")
        print(f"  wrote {path.relative_to(HERE.parent.parent)}  ({size}px)")


if __name__ == "__main__":
    main()
