"""Рисует значок программы (цилиндр базы данных) и сохраняет packaging/app.ico (16–256 px)."""
from pathlib import Path

from PIL import Image, ImageDraw


def draw(size: int = 256) -> Image.Image:
    s = size
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=(31, 78, 121, 255))
    left, right = int(s * 0.22), int(s * 0.78)
    top, bottom, eh = int(s * 0.2), int(s * 0.8), int(s * 0.16)
    body = (233, 241, 250, 255)
    line = (31, 78, 121, 255)
    d.rectangle((left, top + eh // 2, right, bottom - eh // 2), fill=body)
    d.ellipse((left, bottom - eh, right, bottom), fill=body)
    for frac in (0.42, 0.62):
        y = int(top + (bottom - top) * frac)
        d.arc((left, y - eh // 2, right, y + eh // 2), 0, 180, fill=line, width=max(1, s // 64))
    d.ellipse((left, top, right, top + eh), fill=(255, 255, 255, 255), outline=line, width=max(1, s // 64))
    return img


def main() -> Path:
    out = Path(__file__).with_name("app.ico")
    draw(256).save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return out


if __name__ == "__main__":
    print(main())
