import ctypes
import json
import math
import random
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import pygame

BASE_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
EXPRESSIONS_DIR = BASE_DIR / "expressions"

DEFAULT_CONFIG = {
    "microphone": None,
    "threshold": 0.01,
    "shout_threshold": 0.2,
    "voice_band": [100, 3000],
    "min_voice_duration": 0.06,
    "mouth_close_delay": 0.15,
    "mouth_flap_db": 5,
    "mouth_max_open": 0.3,
    "blink_interval": [3.0, 7.0],
    "blink_duration": 0.26,
    "double_blink_chance": 0.15,
    "eye_interval": [2.0, 5.0],
    "talk_lift": [3, 12],
    "talk_lift_speed": 8.0,
    "breath_amplitude": 4,
    "breath_period": 3.5,
    "tilt_angle": 1.5,
    "tilt_period": 2.2,
    "shake_amount": 3,
    "sleep_after": 120,
    "expression_hotkey": "ctrl+alt",
    "background_color": [0, 255, 0],
    "window_size": [1126, 1126],
    "sprite_size": 1024,
}

# Only idle.png is required: for any missing sprite I fall back to the closest one I have.
SPRITE_FALLBACKS = {
    "talk": "idle",
    "blink": "idle",
    "blink_talk": "talk",
    "closed": "blink",
    "closed_talk": "blink_talk",
    "half": "closed",
    "half_talk": "closed_talk",
    "lowered": "half",
    "lowered_talk": "half_talk",
    "squint": "closed",
    "squint_talk": "closed_talk",
    "look_left": "idle",
    "look_left_talk": "talk",
    "look_right": "idle",
    "look_right_talk": "talk",
    "sleep": "blink",
}
BLOCK_SIZE = 512
MOTION_REFERENCE_SIZE = 400
SUBPIXEL_X_MAX_SIZE = 600
MOUTH_MIN_OPEN = 0.06
MOUTH_MIN_CLOSED = 0.08
SPEECH_AVERAGE_TIME = 0.4
FADE_TIMES = {"blink": 0.025, "mouth": 0.03, "look": 0.1, "expression": 0.2, "sleep": 0.5}
# Each number is where that frame ends, as a fraction of blink_duration.
BLINK_FRAMES = [
    (0.08, "lowered"),
    (0.16, "half"),
    (0.24, "squint"),
    (0.44, "closed"),
    (0.58, "squint"),
    (0.76, "half"),
    (1.0, "lowered"),
]
METER_FLOOR_DB = -60


def load_config():
    config = dict(DEFAULT_CONFIG)
    if not CONFIG_PATH.exists():
        return config, None, True
    try:
        config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        return config, f"config.json не прочитан, настройки не сохраняются: {e}", False
    return config, None, False


def save_config(config):
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def db(value):
    return 20 * math.log10(max(value, 1e-6))


def approach(value, target, speed, dt):
    return value + (target - value) * (1 - math.exp(-speed * dt))


def smoothstep(x):
    return x * x * (3 - 2 * x)


class Microphone:
    def __init__(self, config):
        self.config = config
        self.stream = None
        self.name = ""
        self.error = None
        self.samplerate = 48000
        self.level = 0.0
        self.last_voice_time = -math.inf
        self.voiced_samples = 0
        self.mouth_is_open = False
        self.mouth_timer = 0.0
        self.speech_db = 0.0

    @staticmethod
    def default_hostapi():
        try:
            return sd.query_devices(kind="input")["hostapi"]
        except sd.PortAudioError:
            return None

    def devices(self):
        hostapi = self.default_hostapi()
        return [d for d in sd.query_devices() if d["max_input_channels"] > 0 and d["hostapi"] == hostapi]

    def find_device(self, name):
        if not name:
            return None
        hostapi = self.default_hostapi()
        inputs = sorted(
            (d for d in sd.query_devices() if d["max_input_channels"] > 0),
            key=lambda d: d["hostapi"] != hostapi,
        )
        for d in inputs:
            if d["name"] == name:
                return d["index"]
        for d in inputs:
            if name.casefold() in d["name"].casefold():
                return d["index"]
        raise ValueError(f"микрофон «{name}» не найден")

    def open(self, name):
        self.close()
        try:
            stream = sd.InputStream(device=self.find_device(name), blocksize=BLOCK_SIZE, callback=self.callback)
            self.samplerate = stream.samplerate
            self.name = sd.query_devices(stream.device)["name"]
            stream.start()
            self.stream = stream
            self.error = None
        except Exception as e:
            self.name = name or ""
            self.error = str(e)

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None
        self.level = 0.0

    def next_device(self):
        names = [d["name"] for d in self.devices()]
        if not names:
            return None
        index = (names.index(self.name) + 1) % len(names) if self.name in names else 0
        self.open(names[index])
        return names[index]

    def is_talking(self, now):
        return now - self.last_voice_time < self.config["mouth_close_delay"]

    def mouth_open(self, now):
        return self.mouth_is_open and self.is_talking(now)

    def update_mouth(self, level_db, block, starting):
        # I flap the mouth per syllable: it closes when the level dips mouth_flap_db below
        # the running speech average (or stays open too long) and reopens when it climbs back.
        self.mouth_timer += block
        if level_db is None:
            if self.mouth_is_open and self.mouth_timer >= MOUTH_MIN_OPEN:
                self.mouth_is_open, self.mouth_timer = False, 0.0
            return
        if starting:
            self.speech_db = level_db
        else:
            self.speech_db += (level_db - self.speech_db) * (1 - math.exp(-block / SPEECH_AVERAGE_TIME))
        depth = self.config["mouth_flap_db"]
        if self.mouth_is_open:
            dipped = level_db < self.speech_db - depth
            held_too_long = self.mouth_timer >= self.config["mouth_max_open"]
            if self.mouth_timer >= MOUTH_MIN_OPEN and (dipped or held_too_long):
                self.mouth_is_open, self.mouth_timer = False, 0.0
        elif self.mouth_timer >= MOUTH_MIN_CLOSED and level_db > self.speech_db - depth / 2:
            self.mouth_is_open, self.mouth_timer = True, 0.0

    def callback(self, indata, frames, time_info, status):
        # I only measure energy inside voice_band, so keyboard clicks and low hum don't open the mouth.
        spectrum = np.abs(np.fft.rfft(indata, axis=0)) ** 2
        freqs = np.fft.rfftfreq(frames, 1 / self.samplerate)
        low, high = self.config["voice_band"]
        band = (freqs >= low) & (freqs <= high)
        self.level = level = float(np.sqrt(2 * spectrum[band].sum(axis=0)).max() / frames)

        now = time.monotonic()
        block = frames / self.samplerate
        if level <= self.config["threshold"]:
            self.voiced_samples = 0
            self.update_mouth(None, block, False)
            return
        self.voiced_samples += frames
        # A short blip isn't speech, so I wait for min_voice_duration of sound before I start talking.
        talking = self.is_talking(now)
        if talking or self.voiced_samples >= self.config["min_voice_duration"] * self.samplerate:
            self.last_voice_time = now
            self.update_mouth(db(level), block, not talking)
        else:
            self.update_mouth(None, block, False)


def load_expression(folder, size):
    sprites = {}
    for name in ["idle", *SPRITE_FALLBACKS]:
        path = folder / f"{name}.png"
        if path.exists():
            img = pygame.image.load(path).convert_alpha().premul_alpha()
            sprites[name] = pygame.transform.smoothscale(img, (size, size))
    if "idle" not in sprites:
        return None
    for name, fallback in SPRITE_FALLBACKS.items():
        sprites.setdefault(name, sprites[fallback])
    return sprites


def changing_area(sprites):
    # I find the box where the sprites actually differ (eyes, mouth) and crossfade only that,
    # which is much cheaper than fading the whole sprite every frame.
    base =pygame.surfarray.array3d(sprites["idle"]).astype(np.int16)
    base_alpha = pygame.surfarray.array_alpha(sprites["idle"]).astype(np.int16)
    changed = np.zeros(base_alpha.shape, bool)
    for sprite in {id(s): s for s in sprites.values()}.values():
        changed |= np.abs(pygame.surfarray.array3d(sprite) - base).max(axis=2) > 8
        changed |= np.abs(pygame.surfarray.array_alpha(sprite) - base_alpha) > 8
    if not changed.any():
        return None
    xs, ys = np.where(changed)
    rect = pygame.Rect(xs.min(), ys.min(), xs.max() - xs.min() + 1, ys.max() - ys.min() + 1)
    return rect.inflate(8, 8).clip(sprites["idle"].get_rect())


def load_expressions(size):
    default = load_expression(BASE_DIR, size)
    if default is None:
        raise FileNotFoundError(f"Не найден {BASE_DIR / 'idle.png'}")
    expressions = [("обычная", default, changing_area(default))]
    if EXPRESSIONS_DIR.is_dir():
        for folder in sorted(p for p in EXPRESSIONS_DIR.iterdir() if p.is_dir()):
            sprites = load_expression(folder, size)
            if sprites is not None:
                expressions.append((folder.name, sprites, changing_area(sprites)))
    return expressions[:9]


def blit_subpixel(target, sprite, x, y):
    # pygame only blits at whole pixels, so I spread the sprite over the four neighbouring
    # positions to keep slow motion smooth instead of jumping pixel by pixel.
    ix, iy = math.floor(x), math.floor(y)
    fx, fy = x - ix, y - iy
    offsets = [(0, 0), (1, 0), (0, 1), (1, 1)]
    weights = [(1 - fx) * (1 - fy), fx * (1 - fy), (1 - fx) * fy, fx * fy]
    levels = [round(w * 255) for w in weights]
    levels[weights.index(max(weights))] += 255 - sum(levels)

    if levels[0] == 255:
        target.blit(sprite, (ix, iy), special_flags=pygame.BLEND_PREMULTIPLIED)
        return

    w, h = sprite.get_size()
    acc = pygame.Surface((w + 1, h + 1), pygame.SRCALPHA)
    for offset, level in zip(offsets, levels):
        if level == 0:
            continue
        part = sprite.copy()
        part.fill((level, level, level, level), special_flags=pygame.BLEND_RGBA_MULT)
        acc.blit(part, offset, special_flags=pygame.BLEND_RGBA_ADD)
    target.blit(acc, (ix, iy), special_flags=pygame.BLEND_PREMULTIPLIED)


def crossfade(a, b, t, rect=None):
    level = round(t * 255)
    rect = rect or a.get_rect()
    mixed = a.subsurface(rect).copy()
    mixed.fill((255 - level,) * 4, special_flags=pygame.BLEND_RGBA_MULT)
    part = b.subsurface(rect).copy()
    part.fill((level,) * 4, special_flags=pygame.BLEND_RGBA_MULT)
    mixed.blit(part, (0, 0), special_flags=pygame.BLEND_RGBA_ADD)
    out = b.copy()
    out.fill((0, 0, 0, 0), rect)
    out.blit(mixed, rect.topleft, special_flags=pygame.BLEND_RGBA_ADD)
    return out


class Avatar:
    def __init__(self, config, expressions):
        self.config = config
        self.expressions = expressions
        self.expression = 0
        now = time.monotonic()
        self.last_activity_time = now
        self.next_blink_time = self.schedule_blink(now)
        self.blink_start = -math.inf
        self.look = None
        self.next_look_time = now + random.uniform(*config["eye_interval"])
        self.lift = 0.0
        self.talk_amount = 0.0
        self.shake_amount = 0.0
        self.sleep_amount = 0.0
        self.breath_phase = 0.0
        self.tilt_phase = 0.0
        self.sprite = expressions[0][1]["idle"]
        self.frame_key = (None, False)
        self.fade_from = self.sprite
        self.fade_start = -math.inf
        self.fade_time = 0.0
        self.fade_rect = None
        self.offset = (0.0, 0.0)
        self.angle = 0.0

    def schedule_blink(self, now):
        return now + random.uniform(*self.config["blink_interval"])

    def start_blink(self, now):
        duration = self.config["blink_duration"]
        self.blink_start = now
        if random.random() < self.config["double_blink_chance"]:
            self.next_blink_time = now + duration + 0.08
        else:
            self.next_blink_time = self.schedule_blink(now + duration)

    def eye_phase(self, now):
        t = (now - self.blink_start) / self.config["blink_duration"]
        if t < 0:
            return None
        return next((frame for end, frame in BLINK_FRAMES if t < end), None)

    def update_look(self, now, is_talking):
        if now < self.next_look_time:
            return
        if self.look is None:
            glance = random.random() < (0.1 if is_talking else 0.35)
            look = random.choice(["look_left", "look_right"]) if glance else None
        else:
            look = None if random.random() < 0.8 else self.look
        interval = self.config["eye_interval"] if look is None else (0.8, 2.2)
        self.next_look_time = now + random.uniform(*interval)
        if look != self.look and random.random() < 0.2 and self.eye_phase(now) is None:
            self.start_blink(now)
        self.look = look

    def start_fade(self, now, kind):
        self.fade_from = self.sprite
        self.fade_start = now
        self.fade_time = FADE_TIMES[kind]
        self.fade_rect = None if kind == "expression" else self.expressions[self.expression][2]

    def motion_scale(self):
        return self.config["sprite_size"] / MOTION_REFERENCE_SIZE

    def overhang(self):
        # I sink the sprite below the bottom edge by the most it can move,
        # so bobbing and tilting never reveal a gap under it.
        c = self.config
        tilt = c["sprite_size"] / 2 * math.sin(math.radians(abs(c["tilt_angle"])))
        return (max(c["talk_lift"]) + c["breath_amplitude"] * 1.5 + c["shake_amount"] + 2) * self.motion_scale() + tilt

    def set_expression(self, index):
        if index < len(self.expressions) and index != self.expression:
            self.expression = index
            self.last_activity_time = time.monotonic()
            self.start_fade(self.last_activity_time, "expression")

    def update(self, now, dt, is_talking, loudness, is_shouting, mouth_open=None):
        if mouth_open is None:
            mouth_open = is_talking
        c = self.config
        if is_talking:
            self.last_activity_time = now
        is_sleeping = 0 < c["sleep_after"] < now - self.last_activity_time

        if now >= self.next_blink_time:
            self.start_blink(now)
        self.update_look(now, is_talking)
        eyes = "sleep" if is_sleeping else self.eye_phase(now) or self.look
        key = (eyes, mouth_open and not is_sleeping)
        if key != self.frame_key:
            old_eyes = self.frame_key[0]
            if "sleep" in (eyes, old_eyes):
                kind = "sleep"
            elif eyes == old_eyes:
                kind = "mouth"
            else:
                blink_frames = {frame for _, frame in BLINK_FRAMES}
                kind = "blink" if blink_frames & {eyes, old_eyes} else "look"
            self.start_fade(now, kind)
            self.frame_key = key

        sprites = self.expressions[self.expression][1]
        if eyes == "sleep":
            target = sprites["sleep"]
        elif eyes:
            target = sprites[eyes + ("_talk" if key[1] else "")]
        else:
            target = sprites["talk" if key[1] else "idle"]
        progress = (now - self.fade_start) / self.fade_time if self.fade_time else 1
        self.sprite = target if progress >= 1 else crossfade(self.fade_from, target, smoothstep(progress), self.fade_rect)

        scale = self.motion_scale()
        lift_min, lift_max = (v * scale for v in c["talk_lift"])
        target_lift = lift_min + (lift_max - lift_min) * loudness if is_talking else 0
        self.lift = approach(self.lift, target_lift, c["talk_lift_speed"], dt)
        self.talk_amount = approach(self.talk_amount, is_talking, 4, dt)
        self.shake_amount = approach(self.shake_amount, is_shouting, 12 if is_shouting else 5, dt)
        self.sleep_amount = approach(self.sleep_amount, is_sleeping, 1, dt)

        breath_period = c["breath_period"] * (1 + 0.6 * self.sleep_amount)
        self.breath_phase = (self.breath_phase + 2 * math.pi * dt / breath_period) % (2 * math.pi)
        self.tilt_phase = (self.tilt_phase + 2 * math.pi * dt / c["tilt_period"]) % (2 * math.pi)

        breath = c["breath_amplitude"] * scale * (1 + 0.5 * self.sleep_amount) * math.sin(self.breath_phase)
        shake = c["shake_amount"] * scale * self.shake_amount
        jitter_x = 0.6 * math.sin(now * 37) + 0.4 * math.sin(now * 23 + 1.7)
        jitter_y = 0.6 * math.sin(now * 31 + 0.5) + 0.4 * math.sin(now * 19 + 2.9)
        self.offset = (shake * jitter_x, breath - self.lift + shake * jitter_y)
        self.angle = c["tilt_angle"] * self.talk_amount * math.sin(self.tilt_phase)

    def draw(self, target, pivot_x, pivot_y):
        w, h = self.sprite.get_size()
        x = pivot_x + self.offset[0]
        y = pivot_y + self.offset[1]
        # Blending a big sprite four times is slow, so I snap it to whole pixels on x
        # and keep subpixel precision only for the vertical bob.
        if w > SUBPIXEL_X_MAX_SIZE:
            x = round(x)
        if abs(self.angle) < 0.01:
            blit_subpixel(target, self.sprite, x - w / 2, y - h)
            return
        rotated = pygame.transform.rotozoom(self.sprite, self.angle, 1)
        rad = math.radians(self.angle)
        center_x = x - h / 2 * math.sin(rad)
        center_y = y - h / 2 * math.cos(rad)
        rw, rh = rotated.get_size()
        if w > SUBPIXEL_X_MAX_SIZE:
            target.blit(rotated, (round(center_x) - rw // 2, round(center_y) - rh // 2), special_flags=pygame.BLEND_PREMULTIPLIED)
        else:
            blit_subpixel(target, rotated, center_x - rw // 2, center_y - rh // 2)


def start_global_hotkeys(modifiers, count, on_hotkey):
    # Global hotkeys let me switch expressions while OBS or a game has focus.
    if sys.platform != "win32" or not modifiers or count == 0:
        return []
    import ctypes.wintypes

    mod_bits = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
    mods = 0x4000  # MOD_NOREPEAT
    for part in modifiers.lower().split("+"):
        if part.strip() not in mod_bits:
            raise ValueError(f"expression_hotkey: неизвестная клавиша «{part.strip()}», можно ctrl, alt, shift, win")
        mods |= mod_bits[part.strip()]

    failed = []
    ready = threading.Event()

    def run():
        # RegisterHotKey(None, ...) posts to the calling thread's queue,
        # so I register and pump messages on the same thread.
        user32 = ctypes.windll.user32
        for i in range(count):
            if not user32.RegisterHotKey(None, i + 1, mods, ord("1") + i):
                failed.append(i + 1)
        ready.set()
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:  # WM_HOTKEY
                on_hotkey(msg.wParam - 1)

    threading.Thread(target=run, daemon=True).start()
    ready.wait(1)
    return failed


def fit_text(font, text, width):
    if font.size(text)[0] <= width:
        return text
    while text and font.size(text + "…")[0] > width:
        text = text[:-1]
    return text + "…"


def draw_overlay(target, font, config, mic, level, expression_name, notes):
    width = target.get_width() - 16
    line_height = font.get_linesize()
    hotkey = config["expression_hotkey"].title()
    lines = [
        ("↑/↓ порог голоса: %.0f дБ" % db(config["threshold"]), (235, 235, 235)),
        ("Shift+↑/↓ порог крика: %.0f дБ" % db(config["shout_threshold"]), (235, 235, 235)),
        (f"Эмоция: {expression_name}  (1–9, {hotkey}+1–9)", (235, 235, 235)),
        ("M — другой микрофон   Tab — скрыть", (170, 170, 170)),
        *((note, (255, 140, 120)) for note in notes),
    ]
    panel = pygame.Surface((width, 40 + line_height * (len(lines) + 1)), pygame.SRCALPHA)
    panel.fill((0, 0, 0, 190))

    mic_text = f"Микрофон: {mic.name}" if mic.error is None else f"Микрофон недоступен: {mic.error}"
    mic_color = (235, 235, 235) if mic.error is None else (255, 140, 120)
    panel.blit(font.render(fit_text(font, mic_text, width - 20), True, mic_color), (10, 8))

    bar = pygame.Rect(10, 12 + line_height, width - 20, 12)

    def x_of(value):
        return bar.x + bar.w * min(max(1 - db(value) / METER_FLOOR_DB, 0), 1)

    threshold, shout = config["threshold"], config["shout_threshold"]
    fill = (230, 80, 60) if level > shout else (90, 170, 255) if level > threshold else (150, 150, 150)
    pygame.draw.rect(panel, (60, 60, 60), bar)
    pygame.draw.rect(panel, fill, (bar.x, bar.y, x_of(level) - bar.x, bar.h))
    pygame.draw.line(panel, (255, 255, 255), (x_of(threshold), bar.y - 3), (x_of(threshold), bar.bottom + 2), 2)
    pygame.draw.line(panel, (255, 90, 70), (x_of(shout), bar.y - 3), (x_of(shout), bar.bottom + 2), 2)

    y = bar.bottom + 8
    for text, color in lines:
        panel.blit(font.render(fit_text(font, text, width - 20), True, color), (10, y))
        y += line_height
    target.blit(panel, (8, 8))


def adjust_thresholds(config, key, step_db):
    # I keep the shout threshold at least 3 dB above the voice threshold.
    config[key] = min(max(config[key] * 10 ** (step_db / 20), 0.001), 1.0)
    gap = 10 ** (3 / 20)
    if key == "threshold":
        config["shout_threshold"] = min(max(config["shout_threshold"], config["threshold"] * gap), 1.0)
        config["threshold"] = min(config["threshold"], config["shout_threshold"] / gap)
    else:
        config["threshold"] = min(config["threshold"], config["shout_threshold"] / gap)
        config["shout_threshold"] = max(config["shout_threshold"], config["threshold"] * gap)


def main():
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config, config_error, first_run = load_config()
    if first_run:
        save_config(config)

    def save():
        if config_error is None:
            save_config(config)

    pygame.init()
    screen = pygame.display.set_mode(config["window_size"])
    pygame.display.set_caption("PNGtuber")
    font = pygame.font.SysFont("segoeui", 15)
    clock = pygame.time.Clock()

    expressions = load_expressions(config["sprite_size"])
    avatar = Avatar(config, expressions)
    pivot_x = screen.get_width() / 2
    pivot_y = screen.get_height() + avatar.overhang()

    notes = []
    if config_error:
        notes.append(config_error)

    expression_event = pygame.event.custom_type()
    try:
        failed_hotkeys = start_global_hotkeys(
            config["expression_hotkey"],
            len(expressions),
            lambda index: pygame.event.post(pygame.event.Event(expression_event, index=index)),
        )
    except ValueError as e:
        failed_hotkeys = []
        notes.append(str(e))

    mic = Microphone(config)
    mic.open(config["microphone"])

    print("PNGtuber запущен. Tab — панель настроек, стрелки — чувствительность, M — микрофон, 1–9 — эмоции.")
    print("Микрофоны:", *(f"  {d['name']}" for d in mic.devices()), sep="\n")
    print("Эмоции:", ", ".join(name for name, *_ in expressions))

    if failed_hotkeys:
        notes.append("Заняты другой программой: " + ", ".join(f"{config['expression_hotkey'].title()}+{n}" for n in failed_hotkeys))
    for note in notes:
        print(note)

    show_overlay = first_run or bool(notes)
    level = 0.0
    last_frame_time = time.monotonic()
    running = True

    try:
        while running:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type == expression_event:
                    avatar.set_expression(event.index)
                elif event.type == pygame.KEYDOWN:
                    shift = event.mod & pygame.KMOD_SHIFT
                    if event.key == pygame.K_TAB:
                        show_overlay = not show_overlay
                    elif event.key in (pygame.K_UP, pygame.K_DOWN):
                        step = 1 if event.key == pygame.K_UP else -1
                        adjust_thresholds(config, "shout_threshold" if shift else "threshold", step)
                        save()
                    elif event.key == pygame.K_m:
                        name = mic.next_device()
                        if name is not None:
                            config["microphone"] = name
                            save()
                    elif pygame.K_1 <= event.key <= pygame.K_9:
                        avatar.set_expression(event.key - pygame.K_1)

            now = time.monotonic()
            dt = now - last_frame_time
            last_frame_time = now

            # I let the meter jump up fast and fall slowly so it's easier to read.
            level = approach(level, mic.level, 30 if mic.level > level else 6, dt)
            is_talking = mic.is_talking(now)
            threshold_db, shout_db = db(config["threshold"]), db(config["shout_threshold"])
            loudness = min(max((db(level) - threshold_db) / (shout_db - threshold_db), 0), 1)
            is_shouting = is_talking and level > config["shout_threshold"]
            avatar.update(now, dt, is_talking, loudness, is_shouting, mic.mouth_open(now))

            screen.fill(config["background_color"])
            avatar.draw(screen, pivot_x, pivot_y)
            if show_overlay:
                draw_overlay(screen, font, config, mic, level, expressions[avatar.expression][0], notes)
            pygame.display.flip()

            clock.tick(60)
    finally:
        mic.close()
        pygame.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, f"{type(e).__name__}: {e}", "PNGtuber", 0x10)
        raise
