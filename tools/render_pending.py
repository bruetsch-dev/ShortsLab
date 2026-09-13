"""Render a project that the pipeline left sitting in its editor.

Both pipelines deliberately stop before the final encode and hand the project to the timeline /
frame editor. That is right for a person, but a test run has produced nothing watchable until
this step runs, so this performs exactly that last encode.

    python tools/render_pending.py longform <project-folder>
    python tools/render_pending.py clip     <project-slug>
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def log(message):
    print(message, flush=True)


def main(kind, name):
    if kind == "longform":
        import longform_video as lv
        out_dir = lv.OUT_ROOT / name
        if not out_dir.is_dir():
            raise SystemExit(f"No such longform project: {out_dir}")
        result = lv.rebuild_from_disk(out_dir, status_cb=log)
    else:
        import agent_core
        edits_path = agent_core.PROJECTS_DIR / name / "config" / "timeline_edits.json"
        try:
            edits = json.loads(edits_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            edits = {}
        result = agent_core.render_project_timeline(name, edits, status_cb=log)
    print("RENDER_RESULT|" + json.dumps(
        {k: str(v) for k, v in (result or {}).items()
         if isinstance(v, (str, int, float, bool))}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
