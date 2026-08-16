"""Install an image as the app logo.

    python tools/set_logo.py <path-to-image>
"""
import sys
import os
import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "app", "ui", "assets")
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _strip_white_background(img, thresh=238, spread=14):
    """Make the white background transparent for dark-UI use, WITHOUT punching holes in
    glassy/light interior details: only remove near-white pixels CONNECTED to the border
    (flood fill from the edges). Returns the image unchanged if it already has real alpha
    or its border isn't predominantly white."""
    arr = np.array(img)                                  # RGBA
    if arr[:, :, 3].min() < 250:                         # already has transparency -> leave it
        return img
    rgb = arr[:, :, :3].astype(np.int16)
    near_white = (rgb.min(axis=2) >= thresh) & ((rgb.max(axis=2) - rgb.min(axis=2)) <= spread)
    border = np.concatenate([near_white[0, :], near_white[-1, :],
                             near_white[:, 0], near_white[:, -1]])
    if border.mean() < 0.6:                              # border isn't mostly white -> skip
        return img
    try:
        from scipy import ndimage
        lbl, _ = ndimage.label(near_white)
        keep = set(lbl[0, :]) | set(lbl[-1, :]) | set(lbl[:, 0]) | set(lbl[:, -1])
        keep.discard(0)
        bg = np.isin(lbl, list(keep))
    except Exception:                                    # no scipy: fall back to global key
        bg = near_white
    arr[bg, 3] = 0
    return Image.fromarray(arr)


def install(src):
    if not os.path.exists(src):
        raise FileNotFoundError(src)
    os.makedirs(ASSETS, exist_ok=True)
    img = Image.open(src).convert("RGBA")
    img = _strip_white_background(img)
    w, h = img.size
    s = max(w, h)
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))     # transparent square
    canvas.paste(img, ((s - w) // 2, (s - h) // 2), img)
    png_path = os.path.join(ASSETS, "app_logo.png")
    ico_path = os.path.join(ASSETS, "app_logo.ico")
    canvas.resize((256, 256), Image.LANCZOS).save(png_path)
    canvas.save(ico_path, sizes=ICO_SIZES)
    print("wrote:", png_path)
    print("wrote:", ico_path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python tools/set_logo.py <path-to-image>")
        sys.exit(1)
    install(sys.argv[1])
