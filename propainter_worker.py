
"""One ProPainter process for a whole render, instead of one per clip.

Their inference script does everything inside `if __name__ == "__main__":` - argument parsing,
loading RAFT, the flow-completion net and the inpainting generator, then the fill. Run once per
clip, that means loading three models from disk and initialising CUDA every time; measured on the
eating-walk render, 1.4 minutes per clip of which the fill itself is seconds.

So the models are built ONCE and the constructors are then memoised: the script is re-run per job
through runpy exactly as before, and the second time it asks for `RAFT_bi(...)` it gets the
instance that is already on the card. Nothing in their file is edited.

Jobs arrive as JSON files in a directory and answers are written next to them. NOT over stdin:
stdin is the kernel lifeline this child watches to die with its parent, and a second reader on it
deadlocked the next import on Windows once already.
"""
import json, os, runpy, sys, threading, time, traceback

JOBS = sys.argv[1]
os.chdir(sys.argv[2])

# die with the parent, whatever kills it
try:
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x00100000, False, os.getppid())
    if h:
        threading.Thread(target=lambda: (k.WaitForSingleObject(h, 0xFFFFFFFF), os._exit(3)),
                         daemon=True).start()
except Exception:
    pass

import torch
free, total = torch.cuda.mem_get_info()
torch.cuda.set_per_process_memory_fraction(min(float(sys.argv[3]), free * 0.92 / total))

# memoise the three heavy constructors before their script imports them by name
import model.modules.flow_comp_raft as _raft_mod
import model.recurrent_flow_completion as _flow_mod
import model.propainter as _paint_mod

def _once(module, name):
    original = getattr(module, name)
    cache = {}
    def factory(*a, **kw):
        key = repr(a) + repr(sorted(kw.items()))
        if key not in cache:
            cache[key] = original(*a, **kw)
        return cache[key]
    factory.__name__ = name
    setattr(module, name, factory)

_once(_raft_mod, "RAFT_bi")
_once(_flow_mod, "RecurrentFlowCompleteNet")
_once(_paint_mod, "InpaintGenerator")

sys.stderr.write("worker ready\n"); sys.stderr.flush()
open(os.path.join(JOBS, "ready"), "w").close()

while True:
    pending = sorted(f for f in os.listdir(JOBS) if f.endswith(".job"))
    if not pending:
        time.sleep(0.2)
        continue
    name = pending[0]
    path = os.path.join(JOBS, name)
    try:
        argv = json.load(open(path, encoding="utf-8"))
    except Exception:
        try: os.remove(path)
        except OSError: pass
        continue
    try: os.remove(path)
    except OSError: pass
    out = os.path.join(JOBS, name[:-4] + ".done")
    try:
        sys.argv = ["inference_propainter.py"] + argv
        runpy.run_path("inference_propainter.py", run_name="__main__")
        payload = {"ok": True}
    except BaseException as exc:                      # noqa: BLE001 - report, never die
        payload = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                   "trace": traceback.format_exc()[-1500:]}
    try:
        torch.cuda.empty_cache()
    except Exception:
        pass
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
