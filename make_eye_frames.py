# I draw the eye frames by editing idle.png in an AI image editor and saving the results as
# generated/<state>.png|jpg. This script pastes only the changed eye area back onto idle.png
# and talk.png, so I get both <state>.png and <state>_talk.png with the original alpha.

import os
import sys
from pathlib import Path

import numpy as np
import pygame

STATES = ["lowered", "half", "squint", "closed", "look_left", "look_right"]
CHANGE_THRESHOLD = 25
MIN_REGION = 500
GROW = 6
FEATHER = 4
SCLERA = np.array([252, 245, 235], np.float32)


def load_rgba(path):
    surf = pygame.image.load(path).convert_alpha()
    rgb = pygame.surfarray.array3d(surf).transpose(1, 0, 2).astype(np.float32)
    alpha = pygame.surfarray.array_alpha(surf).T.astype(np.float32)
    return rgb, alpha


def on_white(rgb, alpha):
    a = alpha[..., None] / 255
    return rgb * a + 255 * (1 - a)


def save_rgba(path, rgb, alpha):
    h, w = alpha.shape
    surf = pygame.Surface((w, h), pygame.SRCALPHA)
    pygame.surfarray.pixels3d(surf)[...] = rgb.transpose(1, 0, 2).round().clip(0, 255).astype(np.uint8)
    pygame.surfarray.pixels_alpha(surf)[...] = alpha.T.round().clip(0, 255).astype(np.uint8)
    pygame.image.save(surf, path)


def box_blur(a, r):
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r + 1, r)
        c = np.cumsum(np.pad(a, pad, mode="edge"), axis=axis)
        n = a.shape[axis]
        a = (np.take(c, np.arange(2 * r + 1, 2 * r + 1 + n), axis=axis) - np.take(c, np.arange(n), axis=axis)) / (2 * r + 1)
    return a


def grow(mask, r):
    return box_blur(mask.astype(np.float32), r) > 1e-3


def large_regions(mask, min_size):
    surf = pygame.Surface((mask.shape[1], mask.shape[0]), pygame.SRCALPHA)
    pygame.surfarray.pixels_alpha(surf)[...] = (mask.T * 255).astype(np.uint8)
    result = np.zeros_like(mask)
    for m in pygame.mask.from_surface(surf, 127).connected_components(min_size):
        drawn = m.to_surface(setcolor=(255, 255, 255, 255), unsetcolor=(0, 0, 0, 0))
        result |= pygame.surfarray.array_alpha(drawn).T > 0
    return result


def fill_holes(mask):
    surf = pygame.Surface((mask.shape[1], mask.shape[0]), pygame.SRCALPHA)
    pygame.surfarray.pixels_alpha(surf)[...] = (~mask.T * 255).astype(np.uint8)
    filled = mask.copy()
    for m in pygame.mask.from_surface(surf, 127).connected_components():
        x, y, w, h = m.get_bounding_rects()[0]
        if x > 0 and y > 0 and x + w < mask.shape[1] and y + h < mask.shape[0]:
            drawn = m.to_surface(setcolor=(255, 255, 255, 255), unsetcolor=(0, 0, 0, 0))
            filled |= pygame.surfarray.array_alpha(drawn).T > 0
    return filled


def find_brows(flat, rows, cols):
    # The generator redraws the eyebrows slightly differently, so I find the brows in the
    # original (the first short dark run in each column) to keep them untouched.
    dark = flat.mean(axis=2) < 110
    firsts = []
    for x in range(*cols):
        col = dark[rows[0]:rows[1], x]
        starts = np.where(col[1:] & ~col[:-1])[0] + 1
        if len(starts) == 0:
            continue
        end = starts[0]
        while end < len(col) and col[end]:
            end += 1
        firsts.append((x, rows[0] + starts[0], end - starts[0]))
    short = [(s, n) for _, s, n in firsts if n < 16]
    if not short:
        return {}
    band = np.median([s for s, _ in short])
    limit = np.median([n for _, n in short]) * 1.4
    return {x: (start, start + length if length <= limit else None) for x, start, length in firsts if abs(start - band) <= 8}


def brow_bottoms(brows, cols):
    xs = np.array(sorted(x for x, (_, bottom) in brows.items() if bottom is not None))
    return np.interp(np.arange(*cols), xs, [brows[x][1] for x in xs])


def brow_columns(brows, width):
    xs = sorted(brows)
    segments, current = [], [xs[0]]
    for x in xs[1:]:
        if x - current[-1] > 3:
            segments.append(current)
            current = []
        current.append(x)
    segments.append(current)
    mask = np.zeros(width, bool)
    for segment in segments:
        clean = [x for x in segment if brows[x][1] is not None]
        if len(clean) >= 30:
            mask[clean[0]:clean[-1] + 1] = True
    return mask


def remove_grey_shading(rgb, region):
    # The generator likes to tint the eye whites bluish grey; I pull them back to the sclera colour.
    lum = rgb.mean(axis=2)
    bluish = (rgb[..., 2] - rgb[..., 0] > 3) & (lum > 120) & region
    weight = np.clip((lum - 120) / 50, 0, 1)[..., None] * bluish[..., None]
    return rgb * (1 - weight) + SCLERA * weight


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    generated = folder / "generated"
    pygame.init()
    pygame.display.set_mode((1, 1))

    idle_rgb, idle_alpha = load_rgba(folder / "idle.png")
    talk_rgb, talk_alpha = load_rgba(folder / "talk.png") if (folder / "talk.png").exists() else (idle_rgb, idle_alpha)
    idle_flat = on_white(idle_rgb, idle_alpha)
    talk_flat = on_white(talk_rgb, talk_alpha)

    frames = {}
    for state in STATES:
        source = next((generated / f"{state}{ext}" for ext in (".png", ".jpg", ".jpeg", ".webp") if (generated / f"{state}{ext}").exists()), None)
        if source is None:
            print(f"нет generated/{state}.*, пропускаю")
            continue
        surf = pygame.image.load(source).convert()
        surf = pygame.transform.smoothscale(surf, (idle_rgb.shape[1], idle_rgb.shape[0]))
        gen_rgb = pygame.surfarray.array3d(surf).transpose(1, 0, 2).astype(np.float32)
        changed = large_regions(np.abs(gen_rgb - idle_flat).max(axis=2) > CHANGE_THRESHOLD, MIN_REGION)
        frames[state] = (gen_rgb, changed)
    if not frames:
        return

    ys, xs = np.where(np.any([changed for _, changed in frames.values()], axis=0))
    rows, cols = (max(ys.min() - 50, 0), ys.min() + 25), (max(xs.min() - 20, 0), xs.max() + 20)
    idle_brows, talk_brows = find_brows(idle_flat, rows, cols), find_brows(talk_flat, rows, cols)
    shape = idle_alpha.shape
    yy = np.arange(shape[0])[:, None]
    zone = np.zeros(shape, np.float32)
    columns = np.zeros(shape[1], bool)
    bottoms = {}
    if idle_brows and talk_brows:
        columns = brow_columns(idle_brows, shape[1]) | brow_columns(talk_brows, shape[1])
        bottoms = {"": brow_bottoms(idle_brows, cols), "_talk": brow_bottoms(talk_brows, cols)}
        top = min(t for brows in (idle_brows, talk_brows) for t, _ in brows.values()) - 10
        zone_bottom = np.maximum(bottoms[""], bottoms["_talk"]) + 12
        zone[:, cols[0]:cols[1]] = np.clip((zone_bottom - yy) / 3, 0, 1) * (yy >= top)
    zone = zone[..., None]

    for state, (gen_rgb, changed) in frames.items():
        region = fill_holes(grow(changed, GROW))
        gen_rgb = remove_grey_shading(gen_rgb, region)
        weight = box_blur(region.astype(np.float32), FEATHER)[..., None]
        gen_lum = gen_rgb.mean(axis=2)
        skin = np.median(gen_rgb[region & (gen_rgb[..., 0] > gen_rgb[..., 2] + 30) & (gen_lum > 180)], axis=0)

        for suffix, rgb, alpha in (("", idle_rgb, idle_alpha), ("_talk", talk_rgb, talk_alpha)):
            # Near the brows I keep the original above the brow line; below it I take whichever
            # pixel is lighter and paint leftover dark strokes with skin tone.
            below_brow = np.zeros(shape, bool)
            if suffix in bottoms:
                below_brow[:, cols[0]:cols[1]] = yy >= np.round(bottoms[suffix])
            base_lum = rgb.mean(axis=2)
            lighter = np.clip((gen_lum - base_lum) / 20 + 0.5, 0, 1)[..., None]
            under = rgb * (1 - lighter) + gen_rgb * lighter
            darker = (np.clip((skin.mean() - 8 - under.mean(axis=2)) / 50, 0, 1) * columns)[..., None]
            under = under * (1 - darker) + skin * darker
            near_brows = np.where(below_brow[..., None], under, rgb)
            out = rgb * (1 - weight) + gen_rgb * weight
            if (changed & (zone[..., 0] > 0.5)).any():
                out = out * (1 - zone) + near_brows * zone
            save_rgba(folder / f"{state}{suffix}.png", out, alpha)
        ys, xs = np.where(changed)
        print(f"{state}: заменена область x {xs.min()}..{xs.max()}, y {ys.min()}..{ys.max()} -> {state}.png, {state}_talk.png")


if __name__ == "__main__":
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
