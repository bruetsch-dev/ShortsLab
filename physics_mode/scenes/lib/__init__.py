"""Hand-maintained, parametric physics scenes.

Each module in here is a complete Blender script (run with `blender -b -P <file> -- <json>`)
AND an importable module: the `bpy` import is guarded, so

    from physics_mode.scenes.lib import tower_collapse
    tower_collapse.PARAMS

works in the app without Blender, which is how the model that chooses a scene reads the
parameters it is allowed to fill in.
"""
