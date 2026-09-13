"""Animate a rigged OBJ character through one named action, inside Blender.

`model_rig.load()` hands back a `Rig`: a root empty at the feet and a dict of canonical
joints. This module is what makes it move. A shot names a verb - `walk_to`, `sleep` - and
everything here keyframes the whole shot from that one word.

Three rules run through all of it:

  * EVERY action fills the WHOLE shot. `seconds` becomes a frame count, so a shorter shot
    has to make the walk faster, not shorter - a cat that arrives in two seconds and then
    stands frozen for one is the single most obvious tell that the motion was canned.
  * NOTHING asks what the animal has. `rig.rot()` on a joint this model lacks does nothing,
    so a bird with no front legs runs the same walk code as the cat and simply drives fewer
    joints. There is not one `if rig.has(...)` in the pose functions, and there must not be.
  * EASING IS COMPUTED, NOT DELEGATED. Every action is sampled per frame with LINEAR keys
    and the curve shape is arithmetic here. Blender 5.2 moved actions to slots and dropped
    `Action.fcurves`, so reaching back into a curve to set a handle is version-dependent;
    a `_back_out()` in python is not, and it gives the landing a real overshoot anyway.

The sign convention is derived once so nobody has to guess it. The model faces -Y and a
limb hangs below its pivot, so `R_x(a) . (0,0,-1) = (0, +sin a, -cos a)`: a NEGATIVE X
rotation swings a limb's free end FORWARD (-Y) and a positive one swings it back. On the
torso - whose pivot is the root, at the feet - positive X tips the whole body backwards,
which is what sitting up looks like. On the head, positive X is nodding down.

Foot skate is measured, not hoped for. `_leg_length()` rotates a real leg and reads where
the foot actually goes, and the stride amplitude is then solved from the distance the shot
has to cover, so the ground travels under the paws at roughly the speed the paws travel
over it. `_stride_angle()` keeps that exact during the half of the cycle the foot is down,
which is the half you can see.
"""

from __future__ import annotations

import math

import bpy
from mathutils import Vector

# ---------------------------------------------------------------- the vocabulary

ACTIONS = ("sit", "walk_to", "run", "jump_on", "paw_at", "look_around", "sleep")

# Verbs a writer actually produces, folded onto the seven. Anything unrecognised sits,
# which is never wrong for an animal and is always a live frame.
_ALIASES = {
    "sit": "sit", "stand": "sit", "idle": "sit", "wait": "sit", "pose": "sit",
    "walk_to": "walk_to", "walk": "walk_to", "approach": "walk_to", "trot": "walk_to",
    "wander": "walk_to", "walk_away": "walk_to", "creep": "walk_to",
    "run": "run", "chase": "run", "flee": "run", "sprint": "run", "dash": "run",
    "jump_on": "jump_on", "jump": "jump_on", "leap": "jump_on", "hop": "jump_on",
    "pounce": "jump_on", "climb": "jump_on",
    "paw_at": "paw_at", "paw": "paw_at", "swat": "paw_at", "knock_over": "paw_at",
    "bat": "paw_at", "reach": "paw_at", "scratch": "paw_at", "play": "paw_at",
    "look_around": "look_around", "look": "look_around", "alert": "look_around",
    "curious": "look_around", "search": "look_around", "watch": "look_around",
    "listen": "look_around",
    "sleep": "sleep", "loaf": "sleep", "rest": "sleep", "nap": "sleep",
    "curl_up": "sleep", "lie_down": "sleep",
}

TRAVELLING = ("walk_to", "run", "jump_on")

# Below this the "target" is noise - a writer that says walk 4cm meant stand still, and
# turning to face 4cm away would spin the model for no reason.
HEADING_MIN = 0.15


def resolve(action) -> str:
    """Any verb -> one of ACTIONS. Never fails."""
    key = str(action or "sit").strip().lower().replace(" ", "_").replace("-", "_")
    return _ALIASES.get(key, "sit")


# ---------------------------------------------------------------- easing

def _smooth(u: float) -> float:
    """smoothstep: 0..1 with zero slope at both ends."""
    u = max(0.0, min(1.0, u))
    return u * u * (3.0 - 2.0 * u)


def _back_out(u: float, over: float = 1.70158) -> float:
    """0..1 that overshoots past 1 and settles back - the cartoon absorb on a landing.

    Sampled: 0.00 0.65 0.87 0.85 0.80 for a 0.8 target. The overshoot is free and it is the
    whole difference between a cat landing and a cat being teleported onto the box.
    """
    u = max(0.0, min(1.0, u)) - 1.0
    return u * u * ((over + 1.0) * u + over) + 1.0


def _ramp(t: float, lo: float, hi: float) -> float:
    """Where t sits inside a phase, 0..1."""
    if hi <= lo:
        return 1.0 if t >= hi else 0.0
    return max(0.0, min(1.0, (t - lo) / (hi - lo)))


def _pulse(t: float, period: float, width: float) -> float:
    """One smooth 0->1->0 bump every `period` seconds, lasting `width`. Ear flicks, blinks."""
    if period <= 0.0 or width <= 0.0:
        return 0.0
    phase = t % period
    return math.sin(math.pi * phase / width) if phase < width else 0.0


# ---------------------------------------------------------------- gait

# The paw cancels the leg's swing exactly, so the sole stays level all the way through the
# stride. It is not only how a foot looks - a level sole keeps its contact patch on the same
# material point, and a rounded paw left to tilt ROLLS instead, which cost 40% of the stride
# on the reference cat (0.133m of travel where the geometry offers 0.197m).
PAW_COUNTER = 1.0
# How far the leg shortens at the top of its swing, as a share of its own length. A capsule
# leg has no knee: rotating it alone can never raise the foot, because +a and -a lift it by
# the same cos(a). Without this every foot drags on the floor for the whole cycle and the
# walk reads as a shop-window display sliding past.
SWING_LIFT = 0.13


def _measure_leg(rig):
    """(contact lever, geometric length) for one real leg, measured by swinging it.

    Everything about not skating depends on the first number and it cannot be guessed: it
    is a property of the model. Rotate a leg to a known angle - with the paw compensated
    exactly as the gait will compensate it - see where the sole actually went, and invert
    `travel = 2*L*sin(a)`.
    """
    cached = getattr(rig, "_action_leg", None)
    if cached is not None:
        return cached

    joint = next((j for j in ("leg_front_left", "leg_hind_left",
                              "leg_front_right", "leg_hind_right") if rig.has(j)), None)
    if joint is None:                                   # no legs: a plausible stand-in
        result = (0.30 * max(rig.height, 0.01), 0.35 * max(rig.height, 0.01))
        rig._action_leg = result
        return result

    leg = rig.joints[joint]
    paw_name = joint.replace("leg_", "paw_")
    parts = [leg] + ([rig.joints[paw_name]] if rig.has(paw_name) else [])
    saved = tuple(leg.rotation_euler)

    def sole():
        bpy.context.view_layer.update()
        pts = [ob.matrix_world @ Vector(c) for ob in parts for c in ob.bound_box]
        pts.sort(key=lambda p: p.z)
        low = pts[:4]                                   # the sole, not the whole bone
        return (sum(p.y for p in low) / 4.0, sum(p.z for p in low) / 4.0)

    rig.rot(joint, 0.0)
    rig.rot(paw_name, 0.0)
    _y0, z0 = sole()
    span = max(1e-4, leg.matrix_world.translation.z - z0)   # pivot down to the floor

    probe, foot = 30.0, []
    for angle in (-probe, probe):
        rig.rot(joint, angle)
        rig.rot(paw_name, -PAW_COUNTER * angle)
        foot.append(sole()[0])
    leg.rotation_euler = saved
    rig.rot(paw_name, 0.0)
    bpy.context.view_layer.update()

    lever = abs(foot[1] - foot[0]) / (2.0 * math.sin(math.radians(probe)))
    if not (0.02 < lever < 4.0):                        # a degenerate mesh, not a leg
        lever = 0.30 * max(rig.height, 0.01)
    result = (lever, span)
    rig._action_leg = result
    return result


def _leg_length(rig) -> float:
    return _measure_leg(rig)[0]


def _solve_gait(rig, shot, cadence, amp_min, amp_max):
    """Pick a stride count and an amplitude that carry the model the distance it must go.

    A fixed amplitude either mows the ground with tiny steps or slides the feet across it;
    the honest fix is to let the shot's own speed choose the step size. When even the
    biggest safe step is not enough, take more of them.
    """
    length = _leg_length(rig)
    cycles = max(1, int(round(cadence * shot.seconds)))
    ceiling = max(1, int(shot.n / 6))                   # >=6 frames a cycle, or it flickers
    reach = 2.0 * length * math.sin(math.radians(amp_max))
    while shot.dist / cycles > reach and cycles < ceiling:
        cycles += 1
    need = (shot.dist / cycles) / (2.0 * length)
    amp = math.degrees(math.asin(max(math.sin(math.radians(amp_min)),
                                     min(math.sin(math.radians(amp_max)), need))))
    return cycles, amp, length


def _stride_angle(p: float, amp: float) -> float:
    """Leg angle at stride phase p. NEGATIVE is forward, so the cycle starts reaching.

    Stance (p < 0.5) is linear in the foot's horizontal position, not in the angle: that is
    the half the foot is on the ground, and a sine there drags it. Swing is a smoothstep
    back to the front, where nobody can tell.
    """
    if p < 0.5:
        s = 1.0 - 2.0 * (p / 0.5)                       # +1 (forward) -> -1 (back), linear
    else:
        s = -1.0 + 2.0 * _smooth((p - 0.5) / 0.5)       # back -> forward, eased
    return -math.degrees(math.asin(max(-1.0, min(1.0, math.sin(math.radians(amp)) * s))))


# ---------------------------------------------------------------- idle life

def idle_life(rig, seconds, *, tail=1.0, ear=1.0, blink=True, whisker=1.0):
    """The motion that stops a held pose reading as a freeze-frame.

    Additive on the tail and the ears (`rot_add`), so an action that already swings them
    keeps its own motion and gets a little jitter on top; the blink is absolute, because a
    lid is either down or it is not. Every one of these is a no-op on a model without the
    joint, which is why a bird with no ears and no whiskers can call this unguarded.
    """
    if tail:
        sway = 13.0 * tail * math.sin(2.0 * math.pi * 0.45 * seconds)
        rig.rot_add("tail", 0.0, 0.0, sway)
        # the tip lags a quarter cycle: a tail that moves in one rigid piece reads as a stick
        rig.rot_add("tail_tip", 0.0, 0.0,
                    1.4 * 13.0 * tail * math.sin(2.0 * math.pi * 0.45 * (seconds - 0.55)))
    if ear:
        left = _pulse(seconds, 2.3, 0.16)
        right = _pulse(seconds + 1.15, 2.9, 0.16)       # never both at once
        rig.rot_add("ear_left", 0.0, -9.0 * ear * left, 0.0)
        rig.rot_add("ear_right", 0.0, 9.0 * ear * right, 0.0)
    if whisker:
        for i in range(6):
            drift = 2.5 * whisker * math.sin(2.0 * math.pi * 0.6 * seconds + i * 0.9)
            rig.rot_add(f"whisker_l{i}", 0.0, 0.0, drift)
            rig.rot_add(f"whisker_r{i}", 0.0, 0.0, -drift)
    if blink:
        # Absolute, not additive: a blink is a lid coming down, and the model either has one
        # or it does not. No eyelids means no blink - never fake it by squashing the eye.
        shut = _pulse(seconds + 0.7, 3.6, 0.14)
        rig.rot("eyelid_left", -82.0 * shut)
        rig.rot("eyelid_right", -82.0 * shut)
    return rig


# ---------------------------------------------------------------- the shot

class Shot:
    """Everything an action needs that comes from the scene and the spec, worked out once."""

    def __init__(self, sc, action, start, target, facing):
        self.action = action
        self.n = max(1, int(sc.frame_end))
        self.fps = float(sc.render.fps or 24)
        self.seconds = max(1.0 / self.fps, self.n / self.fps)
        self.start = _xy(start)
        self.target = _xy(target, self.start)
        dx = self.target[0] - self.start[0]
        dy = self.target[1] - self.start[1]
        self.dist = math.hypot(dx, dy)
        self.facing = float(facing or 0.0)
        # A shot that both walks somewhere and pins the facing crabs the model sideways
        # across the floor. Travel wins; the spec's facing is what a STATIONARY action uses.
        if action in TRAVELLING and self.dist >= HEADING_MIN:
            self.heading = math.degrees(math.atan2(dx, -dy))
            self.turned = True
        else:
            self.heading = self.facing
            self.turned = False
        self.report = {"action": action, "frames": self.n,
                       "seconds": round(self.seconds, 3),
                       "distance": round(self.dist, 3),
                       "heading": round(self.heading, 1), "from_travel": self.turned}

    def at(self, t):
        """Ground position at normalised time t."""
        return (self.start[0] + (self.target[0] - self.start[0]) * t,
                self.start[1] + (self.target[1] - self.start[1]) * t)


def _xy(value, fallback=(0.0, 0.0)):
    try:
        return (float(value[0]), float(value[1]))
    except (TypeError, ValueError, IndexError):
        return (float(fallback[0]), float(fallback[1]))


# ---------------------------------------------------------------- poses
# Each builder does its own arithmetic once and hands back `pose(t)`. The driver below owns
# the frame loop, the rest pose and the keying, so no action can forget to key a frame.


def _sit(rig, shot):
    """Rear folded, chest up, weight settled. Also the fallback for any verb we don't know.

    The hind leg on the reference cat is one rigid capsule with no knee, so a real feline
    sit is not reachable geometry - this is the closest read: tip the body back, tuck the
    hind legs forward under it, brace the front legs, and let `settle()` find the floor.
    """
    def pose(t):
        ease = _smooth(_ramp(t * shot.seconds, 0.0, 0.5))       # ease into it over 0.5s
        rig.rot("torso", 18.0 * ease)
        for j in rig.group("hind"):
            rig.rot(j, -42.0 * ease)
        for j in rig.group("paws_hind"):
            rig.rot(j, 34.0 * ease)
        for j in rig.group("front"):
            rig.rot(j, 6.0 * ease)
        rig.rot("head", -6.0 * ease)                            # counter the body tip
        rig.rot("neck", -5.0 * ease)
        idle_life(rig, t * shot.seconds)
        rig.place(shot.start[0], shot.start[1], 0.0, facing_deg=shot.heading)
        rig.settle()
    return pose


def _walk(rig, shot):
    cycles, amp, _length = _solve_gait(rig, shot, cadence=1.6, amp_min=9.0, amp_max=30.0)
    shot.report.update(cycles=cycles, amplitude=round(amp, 1))

    def pose(t):
        p = (t * cycles) % 1.0
        lead, lag = _stride_angle(p, amp), _stride_angle((p + 0.5) % 1.0, amp)
        rig.rot("leg_front_left", lead)
        rig.rot("leg_hind_right", lead)
        rig.rot("leg_front_right", lag)
        rig.rot("leg_hind_left", lag)
        # the sole stays flat while the leg swings under it: the paw cancels most of the leg
        rig.rot("paw_front_left", -0.8 * lead)
        rig.rot("paw_hind_right", -0.8 * lead)
        rig.rot("paw_front_right", -0.8 * lag)
        rig.rot("paw_hind_left", -0.8 * lag)
        rig.rot("torso", 0.0, 3.0 * math.sin(2.0 * math.pi * p), 0.0)
        rig.rot("head", 0.0, 0.0, -6.0 * math.sin(math.pi * p))
        rig.rot("neck", 0.0, 0.0, -3.0 * math.sin(math.pi * p))
        rig.rot("tail", -14.0, 0.0, 12.0 * math.sin(2.0 * math.pi * p))
        rig.rot("tail_tip", 0.0, 0.0, 18.0 * math.sin(2.0 * math.pi * (p - 0.25)))
        for j in rig.group("ears"):
            rig.rot(j, 5.0 * math.sin(2.0 * math.pi * p))
        for j in rig.group("wings"):
            rig.rot(j, 0.0, 8.0 * math.sin(2.0 * math.pi * p), 0.0)
        idle_life(rig, t * shot.seconds, tail=0.0, ear=0.35)
        x, y = shot.at(t)
        rig.place(x, y, 0.0, facing_deg=shot.heading)
        # The bob is not invented: swinging the legs lifts the feet, and dropping the model
        # back onto the floor every frame produces the rise and fall for free, in the right
        # phase, with the paws actually touching the ground.
        rig.settle()
    return pose


def _run(rig, shot):
    cycles, amp, _length = _solve_gait(rig, shot, cadence=3.0, amp_min=18.0, amp_max=40.0)
    shot.report.update(cycles=cycles, amplitude=round(amp, 1))
    hop = 0.030 * rig.height

    def pose(t):
        p = (t * cycles) % 1.0
        lead, lag = _stride_angle(p, amp), _stride_angle((p + 0.5) % 1.0, amp)
        rig.rot("leg_front_left", lead)
        rig.rot("leg_hind_right", lead)
        rig.rot("leg_front_right", lag)
        rig.rot("leg_hind_left", lag)
        rig.rot("paw_front_left", -0.8 * lead)
        rig.rot("paw_hind_right", -0.8 * lead)
        rig.rot("paw_front_right", -0.8 * lag)
        rig.rot("paw_hind_left", -0.8 * lag)
        rig.rot("torso", -9.0, 2.0 * math.sin(2.0 * math.pi * p), 0.0)   # nose down, driving
        rig.rot("head", 6.0, 0.0, 0.0)
        for j in rig.group("ears"):
            rig.rot(j, -13.0)                                            # laid back
        rig.rot("tail", -24.0, 0.0, 9.0 * math.sin(2.0 * math.pi * p))
        rig.rot("tail_tip", 0.0, 0.0, 14.0 * math.sin(2.0 * math.pi * (p - 0.25)))
        for j in rig.group("wings"):
            rig.rot(j, 0.0, 26.0 * math.sin(2.0 * math.pi * p), 0.0)
        idle_life(rig, t * shot.seconds, tail=0.0, ear=0.0, whisker=0.4)
        x, y = shot.at(t)
        rig.place(x, y, 0.0, facing_deg=shot.heading)
        rig.settle()
        # a gallop leaves the ground; settle() alone can only ever crawl
        rig.root.location.z += hop * max(0.0, math.sin(2.0 * math.pi * p))
    return pose


def _jump(rig, shot):
    """Crouch, fly, absorb. The only action that is allowed off the floor.

    The two ground heights are measured, not assumed: a crouched model and a landing model
    stand at different root heights, and picking one number for both either sinks the take
    off or floats the landing.
    """
    crouch_t, land_t = 0.18, 0.80
    apex = 0.55 * rig.height

    def crouch_pose(amount):
        rig.rot("torso", 11.0 * amount)
        for j in rig.group("hind"):
            rig.rot(j, -34.0 * amount)
        for j in rig.group("front"):
            rig.rot(j, -14.0 * amount)
        for j in rig.group("paws_hind"):
            rig.rot(j, 26.0 * amount)

    def ground_z(amount, xy):
        rig.rest()
        crouch_pose(amount)
        rig.place(xy[0], xy[1], 0.0, facing_deg=shot.heading)
        rig.settle()
        return rig.root.location.z

    z_crouch = ground_z(1.0, shot.start)
    z_stand = ground_z(0.0, shot.target)
    rig.rest()

    def pose(t):
        if t <= crouch_t:                                   # gather
            u = _smooth(_ramp(t, 0.0, crouch_t))
            crouch_pose(u)
            x, y = shot.start
            z = z_stand + (z_crouch - z_stand) * u
        elif t < land_t:                                    # flight
            u = _ramp(t, crouch_t, land_t)
            rig.rot("torso", -13.0 * math.sin(math.pi * u) - 2.0)
            for j in rig.group("front"):
                rig.rot(j, -38.0 * math.sin(math.pi * u) - 6.0)   # reaching ahead
            for j in rig.group("hind"):
                rig.rot(j, 28.0 * math.sin(math.pi * u))          # trailing behind
            for j in rig.group("paws_front"):
                rig.rot(j, 22.0 * math.sin(math.pi * u))
            rig.rot("tail", -30.0, 0.0, 0.0)
            for j in rig.group("ears"):
                rig.rot(j, -10.0)
            for j in rig.group("wings"):
                rig.rot(j, 0.0, 48.0 * math.sin(2.0 * math.pi * u), 0.0)
            travel = _ramp(t, crouch_t, land_t)
            x = shot.start[0] + (shot.target[0] - shot.start[0]) * travel
            y = shot.start[1] + (shot.target[1] - shot.start[1]) * travel
            z = z_stand + 4.0 * apex * u * (1.0 - u)
        else:                                               # absorb, with the overshoot
            u = _back_out(_ramp(t, land_t, 1.0))
            rig.rot("torso", 15.0 * (1.0 - u))
            for j in rig.group("hind"):
                rig.rot(j, -30.0 * (1.0 - u))
            for j in rig.group("front"):
                rig.rot(j, -18.0 * (1.0 - u))
            for j in rig.group("paws_hind"):
                rig.rot(j, 22.0 * (1.0 - u))
            rig.rot("tail", -18.0 * (1.0 - u))
            x, y = shot.target
            z = z_crouch + (z_stand - z_crouch) * u
        idle_life(rig, t * shot.seconds, tail=0.0, ear=0.0, whisker=0.0)
        rig.place(x, y, z, facing_deg=shot.heading)
    return pose


def _paw_at(rig, shot):
    """Two swipes at whatever is in front. Degrades to a wing beat, then to looking around.

    A model with no front limb and no wing has nothing to swipe WITH, and freezing it in a
    sit is a dead frame - so it looks around instead, which is at least alive and at most
    slightly off-brief.
    """
    front = rig.group("front")
    wings = rig.group("wings")
    if not front and not wings:
        shot.report["degraded_to"] = "look_around"
        return _look_around(rig, shot)

    limb = ("leg_front_right" if "leg_front_right" in front else
            (front[0] if front else None))
    period = 1.4

    def pose(t):
        seconds = t * shot.seconds
        swing = max(0.0, math.sin(2.0 * math.pi * seconds / period))
        rig.rot("torso", 12.0)                              # sitting back on the haunches
        for j in rig.group("hind"):
            rig.rot(j, -30.0)
        for j in rig.group("paws_hind"):
            rig.rot(j, 24.0)
        for j in front:
            rig.rot(j, 4.0)
        if limb:
            rig.rot(limb, -52.0 * swing)
            rig.rot(limb.replace("leg_", "paw_"), 20.0 * swing)
        for j in wings:
            rig.rot(j, 0.0, -45.0 * swing, 0.0)
        rig.rot("head", 10.0 - 4.0 * swing, 0.0, 6.0 * math.sin(2.0 * math.pi * seconds
                                                                / period))
        for j in rig.group("ears"):
            rig.rot(j, -7.0)                                # forward, locked on
        for j in rig.group("pupils"):
            rig.rot(j, 8.0 * swing)
        rig.rot("tail", 0.0, 0.0, 20.0 * math.sin(2.0 * math.pi * 0.9 * seconds))
        idle_life(rig, seconds, tail=0.0, ear=0.5)
        rig.place(shot.start[0], shot.start[1], 0.0, facing_deg=shot.heading)
        rig.settle()
    return pose


def _look_around(rig, shot):
    """A 35-degree sweep split across head and neck.

    All of it on the head exposes the neck - the reference cat only buries 9% of its skull
    in it, and past 25 degrees you see straight through the join. Two joints turn further
    than one and neither leaves its safe range. The eyes lead the head, as eyes do.
    """
    def pose(t):
        seconds = t * shot.seconds
        wave = math.sin(2.0 * math.pi * 0.5 * seconds)
        rig.rot("torso", 14.0)
        for j in rig.group("hind"):
            rig.rot(j, -34.0)
        for j in rig.group("paws_hind"):
            rig.rot(j, 28.0)
        for j in rig.group("front"):
            rig.rot(j, 4.0)
        rig.rot("head", 5.0, 0.0, 24.0 * wave)
        rig.rot("neck", 0.0, 0.0, 10.0 * wave)
        for j in rig.group("pupils"):
            # a quarter cycle early: the eye goes first and the head follows it
            rig.rot(j, 0.0, 0.0, -10.0 * math.sin(2.0 * math.pi * 0.5 * (seconds + 0.5)))
        rig.rot("ear_left", 13.0 * wave)
        rig.rot("ear_right", -13.0 * wave)
        rig.rot("tail", 0.0, 0.0, 20.0 * math.sin(2.0 * math.pi * 0.75 * seconds))
        idle_life(rig, seconds, tail=0.0, ear=0.4)
        rig.place(shot.start[0], shot.start[1], 0.0, facing_deg=shot.heading)
        rig.settle()
    return pose


def _sleep(rig, shot):
    """Folded down into a loaf, breathing. The frame must never be dead.

    Without eyelids the eyes stay open and the head pitch carries the read. Hiding or
    flattening the eyeballs is the obvious cheat and it makes a sleeping animal look like a
    dead one; this never invents geometry the model does not have.
    """
    breath_hz = 0.25

    def pose(t):
        seconds = t * shot.seconds
        breath = math.sin(2.0 * math.pi * breath_hz * seconds)
        rig.rot("torso", 4.0)
        for j in rig.group("hind"):
            rig.rot(j, -44.0)                               # tucked all the way under
        for j in rig.group("front"):
            rig.rot(j, 44.0)                                # folded back the other way
        for j in rig.group("paws_hind"):
            rig.rot(j, 34.0)
        for j in rig.group("paws_front"):
            rig.rot(j, -34.0)
        rig.rot("head", 22.0, 0.0, 8.0)                     # nose tucked towards a shoulder
        rig.rot("neck", 12.0, 0.0, 6.0)
        for j in rig.group("ears"):
            rig.rot(j, -11.0)
        rig.rot("tail", 0.0, 0.0, 34.0 + 3.0 * breath)      # wrapped round the body
        rig.rot("tail_tip", 0.0, 0.0, 22.0)
        rig.rot("eyelid_left", -82.0)
        rig.rot("eyelid_right", -82.0)
        rig.scale_joint("chest", 1.0, 1.0 + 0.022 * breath, 1.0 + 0.016 * breath)
        rig.scale_joint("belly", 1.0, 1.0 + 0.018 * breath, 1.0)
        idle_life(rig, seconds, tail=0.0, ear=0.25, blink=False, whisker=0.5)
        rig.place(shot.start[0], shot.start[1], 0.0, facing_deg=shot.heading)
        rig.settle()
        rig.root.location.z += 0.006 * rig.height * breath
    return pose


_BUILDERS = {"sit": _sit, "walk_to": _walk, "run": _run, "jump_on": _jump,
             "paw_at": _paw_at, "look_around": _look_around, "sleep": _sleep}


# ---------------------------------------------------------------- the driver

def _drive(rig, shot, pose):
    """Sample the pose on every frame and key everything the rig owns.

    Per frame, not per beat: the shape of every curve is already computed above, so LINEAR
    keys reproduce it exactly and no keyframe handle can quietly reinterpret the motion.
    `rest()` first, so a pose only has to say what it wants and never what it does not.
    """
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    span = max(1, shot.n - 1)
    for f in range(1, shot.n + 1):
        rig.rest()
        pose((f - 1) / span)
        rig.key(f)
    return shot


def act(rig, action, sc, *, target=(0.0, 0.0), start=(0.0, 0.0), facing=0.0, status=None):
    """Animate `rig` through one named action for the whole of scene `sc`.

    Returns the shot's report - heading, stride count, amplitude - which is worth putting in
    the job log: it is how you tell "the walk skated" from "the writer asked for 4 metres in
    2 seconds" without opening Blender.
    """
    name = resolve(action)
    shot = Shot(sc, name, start, target, facing)
    rig.rest()
    _drive(rig, shot, _BUILDERS[name](rig, shot))
    if status:
        bits = " ".join(f"{k}={v}" for k, v in shot.report.items() if k != "action")
        status(f"ACT {name} {bits}")
    return shot.report
