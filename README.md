# PNGtuber

A lightweight PNGtuber I wrote in Python for my streams. My avatar opens its mouth when I talk,
blinks, glances around, breathes, bobs with my voice, shakes when I shout and falls asleep when
I go quiet for a while. It draws on a solid green background, so I just add it to OBS with a
chroma key.

## Features

- Voice detection in a configurable frequency band, so keyboard clicks and hum don't trigger it
- Mouth flaps per syllable instead of staying open the whole time I'm talking
- Random blinks (sometimes double), glances left and right, idle breathing
- Lift, head tilt and shake that follow how loud I am
- Sleep mode after a period of silence
- Up to 9 expressions, switchable with number keys or global hotkeys (Windows)
- Smooth crossfades between frames and subpixel movement
- Settings overlay with a live level meter; changes are saved to `config.json`

## Running from source

I use Python 3.10+ on Windows.

```bat
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python main.py
```

## Building the .exe

```bat
build.bat
```

It installs PyInstaller into `venv`, builds `dist\PNGtuber.exe` and copies the sprites,
`expressions\` and `config.json` next to it. The exe looks for all of them in its own folder.

## Controls

| Key | Action |
| --- | --- |
| `Tab` | Show or hide the settings overlay |
| `↑` / `↓` | Raise or lower the voice threshold by 1 dB |
| `Shift` + `↑` / `↓` | Raise or lower the shout threshold by 1 dB |
| `M` | Switch to the next microphone |
| `1`–`9` | Switch expression |
| `Ctrl`+`Alt`+`1`–`9` | Switch expression from any window (Windows only, configurable) |

The overlay text is in Russian for now.

## Sprites

All sprites are square PNGs with transparency, placed next to `main.py` (or the exe).
Only `idle.png` is required; any missing frame falls back to the closest one that exists.

| File | When it's shown |
| --- | --- |
| `idle.png` | Eyes open, mouth closed |
| `talk.png` | Eyes open, mouth open |
| `lowered.png`, `half.png`, `squint.png`, `closed.png` | Blink frames, from slightly lowered lids to fully closed |
| `blink.png` | Used for blink frames that are missing |
| `look_left.png`, `look_right.png` | Glancing to the side |
| `sleep.png` | Asleep (falls back to `blink.png`) |

Every eye frame also has a `_talk` version with the mouth open, e.g. `half_talk.png`.

### Expressions

The sprites in the root folder are the default expression. For more, I create folders inside
`expressions/`, each with its own set of sprites (at least `idle.png`):

```
expressions/
  angry/
    idle.png
    talk.png
  happy/
    idle.png
    talk.png
```

Folders are sorted by name and mapped to keys `2`–`9`; key `1` is always the default.

### Generating eye frames

`make_eye_frames.py` helps me make blink and glance frames from a single drawing. I edit
`idle.png` in an AI image editor, save the results as `generated/<state>.png` or `.jpg`
(`lowered`, `half`, `squint`, `closed`, `look_left`, `look_right`) and run:

```bat
venv\Scripts\python make_eye_frames.py
```

The script finds the area that changed, pastes only that part onto `idle.png` and `talk.png`
and writes both `<state>.png` and `<state>_talk.png`, keeping the original eyebrows and alpha.

## Configuration

`config.json` is created with defaults on the first run. The app updates the thresholds and the
microphone itself; everything else I edit by hand.

| Key | Default | Meaning |
| --- | --- | --- |
| `microphone` | `null` | Input device name or part of it; `null` uses the system default |
| `threshold` | `0.01` | Voice threshold (linear amplitude, shown in dB in the overlay) |
| `shout_threshold` | `0.2` | Above this level the avatar shakes |
| `voice_band` | `[100, 3000]` | Frequency band in Hz used to detect voice |
| `min_voice_duration` | `0.06` | Seconds of sound needed before talking starts |
| `mouth_close_delay` | `0.15` | Seconds of silence before talking stops |
| `mouth_flap_db` | `5` | How deep (dB) a dip between syllables must be to close the mouth |
| `mouth_max_open` | `0.3` | Longest time in seconds the mouth stays open on one syllable |
| `blink_interval` | `[3.0, 7.0]` | Random pause between blinks, seconds |
| `blink_duration` | `0.26` | Length of one blink, seconds |
| `double_blink_chance` | `0.15` | Chance that a blink is followed by a second one |
| `eye_interval` | `[2.0, 5.0]` | Random pause between glances, seconds |
| `talk_lift` | `[3, 12]` | How high the avatar lifts while talking, from quiet to loud |
| `talk_lift_speed` | `8.0` | How fast the lift follows the voice |
| `breath_amplitude` | `4` | Idle breathing bob height |
| `breath_period` | `3.5` | Seconds per breath |
| `tilt_angle` | `1.5` | Head sway angle while talking, degrees |
| `tilt_period` | `2.2` | Seconds per sway |
| `shake_amount` | `3` | Shake strength when shouting |
| `sleep_after` | `120` | Seconds of silence before falling asleep; `0` disables it |
| `expression_hotkey` | `"ctrl+alt"` | Modifiers for global hotkeys (`ctrl`, `alt`, `shift`, `win`); `""` disables them |
| `background_color` | `[0, 255, 0]` | Background RGB color for the chroma key |
| `window_size` | `[1126, 1126]` | Window size in pixels |
| `sprite_size` | `1024` | Size the sprites are scaled to |

Motion values (`talk_lift`, `breath_amplitude`, `shake_amount`) are in pixels for a 400 px
sprite and scale with `sprite_size`.

## OBS setup

1. Add a **Window Capture** source and pick the PNGtuber window.
2. Add a **Chroma Key** filter with the green key color.
