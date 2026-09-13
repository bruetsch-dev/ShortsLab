"""Web review UI for the SFX categorization trainer (see tools/sfx_trainer.py).

Standalone ThreadingHTTPServer (own port, like scrape_trainer_serve) that lists every
sound in soundeffects/, plays it in the browser, and lets the user confirm/correct the
role + reaction BY EAR. Every change autosaves to soundeffects/sfx_labels.json. Nothing
in the live pipeline changes until "Activate" is pressed.
"""

import json
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from tools import sfx_trainer as T
except Exception:  # allow `python tools/sfx_trainer_serve.py`
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from tools import sfx_trainer as T

_AUDIO_MIME = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
               ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac"}


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _page():
    rows, data = T.merged_view(with_duration=True)
    reactions = data.get("reactions") or [list(r) for r in T.REACTIONS]
    payload = {
        "rows": rows,
        "roles": [list(r) for r in T.ROLES],
        "reactions": reactions,
        "policies": T.POLICIES,
        "active": bool(data.get("active")),
    }
    boot = json.dumps(payload, ensure_ascii=False)
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>SFX Trainer</title>
<style>
:root{--bg:#090b0a;--raised:#14171400;--card:#181a18;--line:rgba(255,255,255,.09);
 --line2:rgba(255,255,255,.16);--text:#f1f3f1;--muted:#a0a6a0;--faint:#646a64;
 --accent:#39ff14;--accent-sub:rgba(57,255,20,.13);--accent-fg:#062b00;--danger:#ef665c;--warn:#e6b450;}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(circle at 70% 8%,rgba(57,255,20,.06),transparent 45%),var(--bg);
 color:var(--text);font-family:Inter,ui-sans-serif,system-ui,"Segoe UI",sans-serif;padding:0 0 120px;}
header{position:sticky;top:0;z-index:20;backdrop-filter:blur(12px);background:rgba(9,11,10,.86);
 border-bottom:1px solid var(--line);padding:14px 22px;}
.hrow{display:flex;align-items:center;gap:16px;flex-wrap:wrap;}
h1{font-size:17px;margin:0;letter-spacing:-.01em;}
h1 b{color:var(--accent);}
.count{color:var(--muted);font-size:13px;font-variant-numeric:tabular-nums;}
.bar{flex:1;min-width:160px;height:8px;border-radius:99px;background:var(--card);overflow:hidden;border:1px solid var(--line);}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#7dff5c);width:0;transition:width .3s;}
.btn{border:1px solid var(--line2);background:var(--card);color:var(--text);border-radius:10px;
 padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;}
.btn:hover{border-color:var(--accent);}
.btn.primary{background:var(--accent);color:var(--accent-fg);border-color:var(--accent);}
.btn.primary:disabled{opacity:.4;cursor:not-allowed;filter:grayscale(.4);}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px;}
.chip{border:1px solid var(--line);background:var(--card);color:var(--muted);border-radius:99px;
 padding:5px 12px;font-size:12px;cursor:pointer;font-weight:600;}
.chip.on{background:var(--accent-sub);border-color:var(--accent);color:var(--text);}
/* align-items:start -> each card is its natural height (no stretching a short Accent card
   up to the height of a tall Reaction card with its chip row = big empty gap) */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px;padding:20px 22px;align-items:start;}
.card{border:1px solid var(--line);background:var(--card);border-radius:14px;padding:13px 14px;
 display:flex;flex-direction:column;gap:9px;position:relative;}
.card.suggested{border-style:dashed;border-color:var(--warn);}
.card.confirmed{border-color:rgba(57,255,20,.4);}
.card.skip{opacity:.62;}
.ctop{display:flex;align-items:center;gap:10px;}
.play{flex:0 0 auto;width:42px;height:42px;border-radius:50%;border:1px solid var(--line2);
 background:var(--accent-sub);color:var(--accent);font-size:16px;cursor:pointer;display:flex;
 align-items:center;justify-content:center;}
.play.playing{background:var(--accent);color:var(--accent-fg);}
.name{flex:1;min-width:0;}
.name .fn{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.name .du{font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums;}
.tag{position:absolute;top:11px;right:12px;font-size:10px;font-weight:800;text-transform:uppercase;
 letter-spacing:.05em;color:var(--warn);}
.card.confirmed .tag{color:var(--accent);}
.roles{display:flex;flex-wrap:wrap;gap:5px;}
.role{border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:8px;
 padding:5px 9px;font-size:11.5px;font-weight:600;cursor:pointer;}
.role.on{background:var(--accent-sub);border-color:var(--accent);color:var(--text);}
.rowline{display:flex;gap:8px;align-items:center;}
label.lbl{font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.04em;min-width:62px;}
.hintlbl{color:var(--faint);font-weight:600;text-transform:none;letter-spacing:0;}
select,input.note{background:#0f110f;border:1px solid var(--line2);color:var(--text);border-radius:8px;
 padding:6px 8px;font-size:12.5px;flex:1;min-width:0;font-family:inherit;}
.reactwrap{display:flex;flex-direction:column;gap:5px;}
.reactwrap .lbl{min-width:0;}
.rchips{display:flex;flex-wrap:wrap;gap:4px;}
.rchip{border:1px solid var(--line);background:transparent;color:var(--muted);border-radius:7px;
 padding:4px 8px;font-size:11px;font-weight:600;cursor:pointer;}
.rchip:hover{border-color:var(--accent);color:var(--text);}
.rchip.on{background:var(--accent-sub);border-color:var(--accent);color:var(--text);}
.reactline[hidden]{display:none;}
.note{width:100%;}
footer{position:fixed;bottom:0;left:0;right:0;background:rgba(9,11,10,.94);backdrop-filter:blur(12px);
 border-top:1px solid var(--line);padding:12px 22px;display:flex;gap:14px;align-items:center;z-index:20;}
.savedot{font-size:12px;color:var(--faint);}
.savedot.ok{color:var(--accent);}
</style></head><body>
<header>
 <div class="hrow">
  <h1><b>SFX</b> Trainer &mdash; teach the master which sound is what</h1>
  <span class="count" id="count"></span>
  <div class="bar"><i id="progbar"></i></div>
  <button class="btn" id="addReaction" title="Add a new reaction category">+ reaction</button>
 </div>
 <div class="chips" id="filters"></div>
</header>
<div class="grid" id="grid"></div>
<footer>
 <span class="savedot" id="savedot">All changes autosave</span>
 <span style="flex:1"></span>
 <span class="count" id="footcount"></span>
 <button class="btn" id="confirmShown">✓ Confirm all shown</button>
 <button class="btn primary" id="activate">Activate labels for the pipeline</button>
</footer>
<script>
const BOOT = __BOOT__;
let ROWS = BOOT.rows, ROLES = BOOT.roles, REACTIONS = BOOT.reactions, POLICIES = BOOT.policies;
let filter = "all";
let audio = null, playingFile = null;
const byFile = {};
ROWS.forEach(r => byFile[r.file] = r);
// a reaction-role row must carry a concrete reaction slug (seed may leave it null)
ROWS.forEach(r => { if(r.role==='reaction' && !r.reaction && REACTIONS.length) r.reaction = REACTIONS[0][0]; });

function esc(s){const d=document.createElement('div');d.textContent=(s==null?'':s);return d.innerHTML;}
function reactionLabel(slug){const r=REACTIONS.find(x=>x[0]===slug);return r?r[1]:slug;}

function play(file, btn){
  if(playingFile===file && audio){ audio.pause(); audio=null; playingFile=null; render(); return; }
  if(audio){ audio.pause(); }
  // cache-buster: always fetch the CURRENT file (library sounds get re-trimmed/replaced)
  audio = new Audio('/media/'+encodeURIComponent(file)+'?v='+Date.now());
  playingFile = file;
  audio.play().catch(()=>{});
  audio.onended = ()=>{ playingFile=null; render(); };
  render();
}

function save(file){
  const r = byFile[file];
  fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({file:file, roles:r.roles, reactions:r.reactions, policy:r.policy, note:r.note})})
    .then(x=>x.json()).then(()=>{ flash(); }).catch(()=>{});
}
let ft; function flash(){ const d=document.getElementById('savedot'); d.textContent='Saved ✓'; d.classList.add('ok');
  clearTimeout(ft); ft=setTimeout(()=>{ d.textContent='All changes autosave'; d.classList.remove('ok'); },1200); }

// a sound can hold MULTIPLE roles; "skip" is exclusive (clears the rest and vice-versa)
function toggleRole(file, role){
  const r=byFile[file];
  if(role==='skip'){ r.roles = r.roles.includes('skip') ? [] : ['skip']; }
  else {
    r.roles = r.roles.filter(x=>x!=='skip');
    r.roles = r.roles.includes(role) ? r.roles.filter(x=>x!==role) : r.roles.concat([role]);
  }
  if(!r.roles.includes('reaction')) { /* keep reactions stored, just hidden */ }
  else if(!r.reactions.length && REACTIONS.length) r.reactions = [REACTIONS[0][0]];
  r.labeled = r.roles.length>0; r.suggested = !r.labeled;
  save(file); render();
}
function toggleReaction(file, slug){
  const r=byFile[file];
  r.reactions = r.reactions.includes(slug) ? r.reactions.filter(x=>x!==slug) : r.reactions.concat([slug]);
  if(!r.roles.includes('reaction')) r.roles = r.roles.concat(['reaction']);
  r.labeled = r.roles.length>0; r.suggested=false;
  save(file); render();
}
function setPolicy(file, val){ const r=byFile[file]; r.policy=val; if(r.roles.length){r.labeled=true;r.suggested=false;} save(file); }
function setNote(file, val){ const r=byFile[file]; r.note=val; save(file); }
function confirmRow(file){ const r=byFile[file]; r.labeled=r.roles.length>0; r.suggested=!r.labeled; save(file); render(); }

function counts(){ let lab=ROWS.filter(r=>r.labeled).length; return {lab, total:ROWS.length}; }

function render(){
  const c = counts();
  document.getElementById('count').textContent = c.lab+' / '+c.total+' labeled';
  document.getElementById('footcount').textContent = (c.total-c.lab)+' still on a suggestion';
  document.getElementById('progbar').style.width = (c.total? (100*c.lab/c.total):0)+'%';
  document.getElementById('activate').disabled = (c.lab < c.total);
  // filter chips (a multi-role sound is counted under each of its roles)
  const fEl = document.getElementById('filters');
  const roleCounts = {}; ROWS.forEach(r=>r.roles.forEach(x=>roleCounts[x]=(roleCounts[x]||0)+1));
  const chips = [['all','All ('+c.total+')'],['unlabeled','Needs ear ('+(c.total-c.lab)+')']]
    .concat(ROLES.map(r=>[r[0], r[1]+' ('+(roleCounts[r[0]]||0)+')']));
  fEl.innerHTML = chips.map(ch=>'<span class="chip '+(filter===ch[0]?'on':'')+'" data-f="'+ch[0]+'">'+esc(ch[1])+'</span>').join('');
  fEl.querySelectorAll('.chip').forEach(el=>el.onclick=()=>{ filter=el.dataset.f; render(); });
  // cards
  const g = document.getElementById('grid');
  const rows = ROWS.filter(r=> filter==='all' ? true : filter==='unlabeled' ? !r.labeled : r.roles.includes(filter));
  g.innerHTML = rows.map(r=>cardHTML(r)).join('');
  rows.forEach(r=>wire(r.file));
}

function cardHTML(r){
  const cls = ['card', r.labeled?'confirmed':'suggested', r.roles.includes('skip')?'skip':''].join(' ');
  const tag = r.labeled ? (r.roles.length>1?'✓ '+r.roles.length+' roles':'✓ set') : 'suggested';
  const roleBtns = ROLES.map(role=>'<button class="role '+(r.roles.includes(role[0])?'on':'')+'" data-role="'+role[0]+'" title="'+esc(role[2])+'">'+esc(role[1])+'</button>').join('');
  const reactChips = REACTIONS.map(rc=>'<button class="rchip '+(r.reactions.includes(rc[0])?'on':'')+'" data-react="'+rc[0]+'" title="'+esc(rc[2]||'')+'">'+esc(rc[1])+'</button>').join('');
  const polOpts = POLICIES.map(p=>'<option value="'+p+'"'+(r.policy===p?' selected':'')+'>'+esc(p)+'</option>').join('');
  const du = r.duration? r.duration.toFixed(2)+'s' : '';
  return '<div class="'+cls+'" data-file="'+esc(r.file)+'">'
    + '<span class="tag">'+tag+'</span>'
    + '<div class="ctop"><button class="play '+(playingFile===r.file?'playing':'')+'" data-play>'+(playingFile===r.file?'❚❚':'▶')+'</button>'
    + '<div class="name"><div class="fn" title="'+esc(r.file)+'">'+esc(r.stem)+'</div><div class="du">'+du+' &middot; '+esc(r.file.split('.').pop())+'</div></div></div>'
    + '<div class="roles">'+roleBtns+'</div>'
    + '<div class="reactwrap reactline"'+(r.roles.includes('reaction')?'':' hidden')+'><label class="lbl">Reactions <span class="hintlbl">(pick all that fit)</span></label><div class="rchips">'+reactChips+'</div></div>'
    + '<div class="rowline"><label class="lbl">Policy</label><select data-pol>'+polOpts+'</select>'
    + (r.labeled?'':'<button class="btn" data-confirm title="Keep this suggestion as-is">✓ ok</button>')+'</div>'
    + '<input class="note" data-note placeholder="note (optional)" value="'+esc(r.note||'')+'">'
    + '</div>';
}

function wire(file){
  const card = document.querySelector('.card[data-file="'+CSS.escape(file)+'"]');
  if(!card) return;
  card.querySelector('[data-play]').onclick = ()=>play(file);
  card.querySelectorAll('.role').forEach(b=> b.onclick=()=>toggleRole(file, b.dataset.role));
  card.querySelectorAll('.rchip').forEach(b=> b.onclick=()=>toggleReaction(file, b.dataset.react));
  const ps = card.querySelector('[data-pol]'); if(ps) ps.onchange=()=>setPolicy(file, ps.value);
  const nt = card.querySelector('[data-note]'); if(nt) nt.onchange=()=>setNote(file, nt.value);
  const cf = card.querySelector('[data-confirm]'); if(cf) cf.onclick=()=>confirmRow(file);
}

document.getElementById('addReaction').onclick = ()=>{
  const label = prompt('New reaction name (e.g. "cringe / disgust"):'); if(!label) return;
  const slug = label.toLowerCase().replace(/[^a-z0-9]+/g,'_').replace(/^_|_$/g,'').slice(0,24) || ('r'+Date.now());
  if(REACTIONS.find(r=>r[0]===slug)){ alert('That reaction already exists.'); return; }
  REACTIONS.push([slug, label, '']);
  fetch('/reactions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reactions:REACTIONS})})
    .then(()=>render());
};
document.getElementById('confirmShown').onclick = ()=>{
  const shown = ROWS.filter(r=> filter==='all' ? true : filter==='unlabeled' ? !r.labeled : r.role===filter);
  const todo = shown.filter(r=>!r.labeled);
  if(!todo.length){ return; }
  if(!confirm('Confirm '+todo.length+' shown suggestion(s) as-is? You can still edit any of them afterwards.')) return;
  todo.forEach(r=>{ r.labeled=r.roles.length>0; r.suggested=!r.labeled; });
  fetch('/save-all',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({labels: todo.map(r=>({file:r.file, roles:r.roles, reactions:r.reactions, policy:r.policy, note:r.note||''}))})})
    .then(x=>x.json()).then(()=>{ flash(); render(); });
};
document.getElementById('activate').onclick = ()=>{
  fetch('/activate',{method:'POST'}).then(x=>x.json()).then(d=>{
    if(d.ok){ document.getElementById('savedot').textContent='ACTIVATED — pipeline now uses your labels ✓';
      document.getElementById('savedot').classList.add('ok'); }
    else alert(d.error||'Could not activate.');
  });
};
render();
</script></body></html>""".replace("__BOOT__", boot)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/media/"):
            return self._serve_media(parsed.path)
        html = _page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _serve_media(self, path):
        name = urllib.parse.unquote(path[len("/media/"):])
        fp = (T.SFX_DIR / name).resolve()
        # containment: only serve files directly inside soundeffects/
        if fp.parent != T.SFX_DIR.resolve() or not fp.exists() or fp.suffix.lower() not in T.AUDIO_EXTS:
            self.send_error(404)
            return
        size = fp.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            try:
                a, b = rng[6:].split("-")
                start = int(a) if a else 0
                end = int(b) if b else size - 1
            except Exception:
                start, end = 0, size - 1
        end = min(end, size - 1)
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", _AUDIO_MIME.get(fp.suffix.lower(),
                         mimetypes.guess_type(str(fp))[0] or "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        with fp.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except Exception:
                    break
                remaining -= len(chunk)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def _json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        if route == "/save":
            body = self._read_json()
            f = body.get("file")
            if not f:
                return self._json({"ok": False, "error": "no file"})
            data = T.load_labels()
            data["labels"][f] = {"roles": body.get("roles") or [],
                                 "reactions": body.get("reactions") or [],
                                 "policy": body.get("policy", "core"),
                                 "note": body.get("note", "")}
            # any manual save invalidates a stale "active" flag (labels changed since activation)
            data["active"] = False
            T.save_labels(data)
            return self._json({"ok": True})
        if route == "/save-all":
            body = self._read_json()
            data = T.load_labels()
            n = 0
            for rec in body.get("labels") or []:
                f = rec.get("file")
                if not f:
                    continue
                data["labels"][f] = {"roles": rec.get("roles") or [],
                                     "reactions": rec.get("reactions") or [],
                                     "policy": rec.get("policy", "core"),
                                     "note": rec.get("note", "")}
                n += 1
            data["active"] = False
            T.save_labels(data)
            return self._json({"ok": True, "count": n})
        if route == "/reactions":
            body = self._read_json()
            data = T.load_labels()
            data["reactions"] = body.get("reactions") or data.get("reactions")
            T.save_labels(data)
            return self._json({"ok": True})
        if route == "/activate":
            try:
                T.apply_labels()
                return self._json({"ok": True})
            except SystemExit as exc:
                return self._json({"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "error": str(exc)})
        self.send_error(404)


def serve(port=7871):
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"[sfx-trainer] review UI at {url}")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    server.serve_forever()


if __name__ == "__main__":
    serve()
