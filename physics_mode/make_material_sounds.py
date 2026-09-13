"""Synthesize the material sounds a physics sweep needs.

The shipped sound library is meme editing SFX - the closest thing to an impact in it is a
Wii boom. Dropping one of those on a brick tower sounds like a video edit, not like a
collision, so the mode generates its own.

Everything here is MODAL: a handful of decaying sinusoids per event, the way a struck
solid actually rings. The earlier version built these from filtered white noise, which is
what a hiss is - it read as tape noise sitting under the video rather than as material.
No noise generator is used for anything you hear in the foreground.

    python -m physics_mode.make_material_sounds
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 48000
OUT = Path(__file__).resolve().parent.parent / "assets" / "physics_sound"


def _write(path: Path, sig: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    peak = float(np.max(np.abs(sig))) or 1.0
    sig = np.clip(sig / peak * 0.92, -1.0, 1.0)
    # 3ms fades at both ends: a waveform that starts or stops on a non-zero sample clicks,
    # and a click is the one thing that survives every later gain stage.
    n = int(0.003 * SR)
    sig[:n] *= np.linspace(0, 1, n)
    sig[-n:] *= np.linspace(1, 0, n)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())
    return path


def _modes(freqs, decays, gains, seconds, detune=0.0, rng=None) -> np.ndarray:
    """Sum of exponentially decaying sinusoids - one struck solid."""
    t = np.arange(int(seconds * SR)) / SR
    out = np.zeros_like(t)
    for f, d, g in zip(freqs, decays, gains):
        if detune and rng is not None:
            f *= 1.0 + rng.uniform(-detune, detune)
        out += g * np.sin(2 * np.pi * f * t) * np.exp(-t / d)
    return out


def _thump(f0: float, decay: float, seconds: float) -> np.ndarray:
    """The body of a heavy hit: a sine that slides down in pitch as it dies.

    A fixed-pitch sine reads as a test tone. Real mass falling on mass drops in pitch as
    the contact spreads, and that downward slide is most of what makes it feel heavy.
    """
    t = np.arange(int(seconds * SR)) / SR
    f = f0 * np.exp(-t * 2.2)
    phase = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(phase) * np.exp(-t / decay)


def _lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """One-pole lowpass, vectorised via lfilter when scipy is around, else a plain loop."""
    a = float(np.exp(-2.0 * np.pi * cutoff / SR))
    try:
        from scipy.signal import lfilter
        return lfilter([1 - a], [1, -a], x)
    except ImportError:
        y = np.empty_like(x)
        acc = 0.0
        for i, v in enumerate(x):
            acc = a * acc + (1 - a) * v
            y[i] = acc
        return y


def _soften(x: np.ndarray, cutoff: float = 2600.0) -> np.ndarray:
    """Roll off the top so nothing sounds brittle.

    Everything above roughly 3kHz is what makes a synthesized hit feel sharp and tiring.
    The material still reads as wood and stone from its low modes; losing the sparkle is
    the entire point. Applied twice for 12dB/oct - a single pole leaves too much bite.
    """
    return _lowpass(_lowpass(x, cutoff), cutoff)


def wood_knock(f0: float, seconds: float = 0.35, rng=None) -> np.ndarray:
    """One wooden block struck once. Inharmonic partials - wood is not a string.

    Softened deliberately: the high partials are pulled down and the onset is eased over
    a few milliseconds. The sharp version was accurate and unpleasant - a click at the
    front of every hit is exactly the "tickling", nervy quality to avoid.
    """
    ratios = (1.0, 2.71, 5.15, 8.9)
    decays = (seconds * 0.85, seconds * 0.45, seconds * 0.22, seconds * 0.12)
    gains = (1.0, 0.28, 0.09, 0.03)
    sig = _modes([f0 * r for r in ratios], decays, gains, seconds,
                 detune=0.02, rng=rng)
    # ease the onset instead of spiking it: no transient click, so it lands as a soft
    # knock rather than a snap
    n = int(0.006 * SR)
    sig[:n] *= np.linspace(0, 1, n) ** 0.6
    return sig


def impact_heavy(seconds: float = 1.6, seed: int = 11) -> np.ndarray:
    """Steel ball into a stacked brick tower: body thump plus the tower cracking open."""
    rng = np.random.default_rng(seed)
    # Longer, rounder body. A short punchy thump reads as aggressive; a low tone that
    # blooms and decays over most of a second reads as weight, and is calm to sit through.
    sig = _thump(72.0, 0.75, seconds) * 1.0
    sig += _thump(126.0, 0.34, seconds) * 0.35
    # fewer, lower, softer pieces breaking loose - a cluster of bright cracks was the
    # nervy part of the old version
    for _ in range(7):
        start = int(rng.uniform(0.01, 0.30) * SR)
        k = wood_knock(rng.uniform(170, 460), rng.uniform(0.30, 0.55), rng)
        end = min(len(sig), start + len(k))
        sig[start:end] += k[:end - start] * rng.uniform(0.10, 0.24)
    return _soften(sig)


def debris_wood(seconds: float = 2.8, blocks: int = 34, seed: int = 5) -> np.ndarray:
    """Blocks tumbling and coming to rest: many wooden knocks, thinning out.

    Density falls off with t**2 so the tail sounds like pieces settling rather than a
    rattle that simply stops.
    """
    rng = np.random.default_rng(seed)
    sig = np.zeros(int(seconds * SR))
    for _ in range(blocks):
        t = (rng.random() ** 2) * seconds * 0.9
        start = int(t * SR)
        # narrower and lower pitch band with longer decays: pieces coming to rest, not a
        # rattle. 90 bright knocks over 2.6s was a wooden machine gun.
        k = wood_knock(rng.uniform(150, 520), rng.uniform(0.22, 0.5), rng)
        end = min(len(sig), start + len(k))
        if end <= start:
            continue
        sig[start:end] += k[:end - start] * rng.uniform(0.15, 0.55) * (1.0 - t / seconds)
    return _soften(sig)


def swing_air(seconds: float = 0.7, seed: int = 3) -> np.ndarray:
    """The ball travelling: a soft low swell, deliberately dull.

    Broadband whoosh presets are mostly high-frequency hiss. This is capped low enough
    that it reads as air being moved rather than as noise on the recording.
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    x = rng.normal(0, 1, n)
    # two one-pole lowpasses in series: 24dB/oct at 260Hz, nothing bright survives
    for _ in range(2):
        a = np.exp(-2.0 * np.pi * 260.0 / SR)
        acc = 0.0
        for i, v in enumerate(x):
            acc = a * acc + (1 - a) * v
            x[i] = acc
    t = np.linspace(0, 1, n)
    return x * (t ** 2) * np.exp(-((t - 0.85) ** 2) / 0.06)


def main():
    files = {
        "impact_heavy.wav": impact_heavy(),
        "debris_wood.wav": debris_wood(),
        "swing_air.wav": swing_air(),
    }
    for name, sig in files.items():
        p = _write(OUT / name, sig)
        print(f"{p}  {len(sig) / SR:.2f}s")


if __name__ == "__main__":
    main()
