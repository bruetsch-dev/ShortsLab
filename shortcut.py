"""shortcut - the Action Edit command line.

    shortcut build --short-dir ./shorts/01-downhill-skateboard --out ./out/01.mp4
    shortcut batch --root ./shorts --out-dir ./out [--jobs 4] [--force]
    shortcut probe --short-dir ./shorts/01-downhill-skateboard

The editing itself lives in action_editor; this file is only the argument surface, so the same
pipeline can be driven from the app's Enhance-video tab without going through a shell.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import action_editor
from action_editor import ActionEditError


def _log(message):
    print(message, flush=True)


def cmd_probe(args) -> int:
    short_dir = Path(args.short_dir)
    cfg = action_editor.load_config(args.root or short_dir.parent.parent, short_dir)
    probes = action_editor.probe_short(short_dir)
    ffmpeg, _ = action_editor._tools()
    out = {"short": short_dir.name, "title": cfg["short"].get("title") or short_dir.name,
           "clips": []}
    for i, p in enumerate(probes):
        cuts = action_editor.detect_boundaries(
            p.path, ffmpeg, float(cfg["edit"]["scene_threshold"]))
        out["clips"].append({
            "file": p.path.name, "duration_s": round(p.duration, 3), "fps": round(p.fps, 3),
            "resolution": f"{p.width}x{p.height}", "audio": p.has_audio,
            "internal_cuts": cuts, "shots": len(cuts) + 1,
        })
    out["shots_total"] = sum(c["shots"] for c in out["clips"])
    print(json.dumps(out, indent=2))
    return 0


def cmd_build(args) -> int:
    short_dir = Path(args.short_dir)
    root = Path(args.root) if args.root else short_dir.parent.parent
    out = Path(args.out) if args.out else Path("out") / f"{short_dir.name}.mp4"
    report = action_editor.build_short(short_dir, out, root=root, status_cb=_log,
                                       use_lut=not args.no_lut)
    _log(f"report: {report['report']}")
    return 0


def cmd_batch(args) -> int:
    if not args.root:
        raise ActionEditError("batch needs --root: the folder holding the Short directories.")
    root = Path(args.root)
    summary = action_editor.build_batch(
        root, Path(args.out_dir), jobs=args.jobs, force=args.force,
        use_lut=not args.no_lut, status_cb=_log)
    _log(f"{len(summary['built'])} built, {len(summary['skipped'])} skipped, "
         f"{len(summary['failed'])} failed, of {summary['total']}.")
    # A malformed Short must not take the healthy ones down with it, but the batch as a whole
    # still has to report failure so a caller notices.
    return 1 if summary["failed"] else 0


def build_parser() -> argparse.ArgumentParser:
    # --root and --no-lut live on a shared parent rather than on the top-level parser: declaring
    # them in both places lets the subparser's default silently overwrite a value the user gave
    # before the subcommand, so "shortcut --root X batch" would lose X.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", help="Project root holding config.toml and assets/.")
    common.add_argument("--no-lut", action="store_true",
                        help="Ignore the series LUT and grade with the filter chain instead.")

    parser = argparse.ArgumentParser(prog="shortcut", description=__doc__.splitlines()[0])
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("build", parents=[common], help="Build one Short.")
    p.add_argument("--short-dir", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_build)

    p = subs.add_parser("batch", parents=[common], help="Build every Short under a root.")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--force", action="store_true", help="Rebuild even if the output is current.")
    p.set_defaults(func=cmd_batch)

    p = subs.add_parser("probe", parents=[common],
                        help="Report what a Short contains, without building it.")
    p.add_argument("--short-dir", required=True)
    p.set_defaults(func=cmd_probe)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ActionEditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
