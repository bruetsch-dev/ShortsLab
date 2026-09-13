"""Review web UI for the Scrape Trainer.

Shows every scene with its exact script part + Visual Intent, and ALL found clips (auto-rejected
ones are NOT hidden) with a playable local video and full metadata. Your rating (label + primary /
backups + segment edit + comment + a better query) is persisted to the run's SQLite DB immediately.
Your rating is the ground truth.

    python -m tools.scrape_trainer serve <run_id> [--port 7870]
"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tools.scrape_trainer import db_connect, db_save_rating, ensure_preview, LABELS


def _scene_rows(con, run_id):
    scenes = list(con.execute("SELECT * FROM scenes WHERE run_id=? ORDER BY scene_id", (run_id,)))
    out = []
    for s in scenes:
        results = list(con.execute(
            "SELECT * FROM results WHERE run_id=? AND scene_id=? ORDER BY search_position",
            (run_id, s["scene_id"])))
        ratings = {r["source_id"]: r for r in con.execute(
            "SELECT * FROM ratings WHERE run_id=? AND scene_id=?", (run_id, s["scene_id"]))}
        out.append((s, results, ratings))
    return out


def _page(run_id):
    con = db_connect(run_id)
    run = con.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    scenes = _scene_rows(con, run_id)
    n_res = con.execute("SELECT COUNT(*) c FROM results WHERE run_id=?", (run_id,)).fetchone()["c"]
    n_rat = con.execute("SELECT COUNT(*) c FROM ratings WHERE run_id=?", (run_id,)).fetchone()["c"]
    con.close()
    label_btns = "".join(f'<button type=button class=lbl data-l="{l}">{l.replace("_"," ")}</button>' for l in LABELS)
    blocks = []
    for s, results, ratings in scenes:
        try:
            qmap = json.loads(s["queries_json"] or "{}")
        except Exception:
            qmap = {}
        qsum = " &middot; ".join(f'<code>{qt}</code>: {", ".join(qs)}' for qt, qs in qmap.items() if qs)
        cards = []
        for r in results:
            rid = r["source_id"]
            tech = json.loads(r["tech_scores_json"] or "{}")
            vis = json.loads(r["vision_scores_json"] or "{}")
            rej = json.loads(r["reject_reasons_json"] or "[]")
            fb = json.loads(r["found_by_queries_json"] or "[]")
            rat = ratings.get(rid)
            has_video = bool(r["proxy_path"] and Path(r["proxy_path"]).exists())
            media = (f'<video controls preload=none width=180 src="/media/{run_id}/{s["scene_id"]}/{urllib.parse.quote(rid)}"></video>'
                     if has_video else '<div class="novid">no local video</div>')
            rejtxt = (f'<div class=rej>auto-flags: {", ".join(rej)}</div>' if rej else '')
            qlist = ", ".join(sorted({e.get("query", "") for e in fb}))
            techtxt = " ".join(f"{k}={v}" for k, v in tech.items())
            vistxt = (f'<div class=vis>vision: overall={vis.get("overall_match")} act={vis.get("action_match")}</div>'
                      if vis else '')
            cur = rat["label"] if rat else ""
            prim = "checked" if rat and rat["is_primary"] else ""
            back = "checked" if rat and rat["is_backup"] else ""
            cards.append(f'''<div class="card{' rated' if rat else ''}" data-scene="{s["scene_id"]}" data-src="{rid}">
              {media}
              <div class=meta>
                <div class=pos>#{r["search_position"]} &middot; @{r["creator_id"]} &middot; {r["likes"]:,}♥ &middot; {r["duration"]:.0f}s &middot; {r["width"]}x{r["height"]}</div>
                <div class=cap>{(r["caption"] or "")[:180]}</div>
                <div class=q>found by: {qlist}</div>
                <div class=tech>{techtxt}</div>{vistxt}{rejtxt}
                <div class=controls>
                  <div class=labels>{label_btns}</div>
                  <label><input type=checkbox class=prim {prim}> primary</label>
                  <label><input type=checkbox class=back {back}> backup</label>
                  seg <input class=segs type=number step=0.1 value="{r["seg_start"] if r["seg_start"] is not None else ''}" style=width:56px>–<input class=sege type=number step=0.1 value="{r["seg_end"] if r["seg_end"] is not None else ''}" style=width:56px>
                  <input class=cmt placeholder=comment value="{(rat['comment'] if rat else '')}">
                  <input class=bq placeholder="better query" value="{(rat['better_query'] if rat else '')}">
                  <button type=button class=save>save</button>
                  <span class=cur>{cur}</span>
                </div>
              </div>
            </div>''')
        blocks.append(f'''<section class=scene>
          <h2>Scene {s["scene_id"]} &middot; {s["category"]}</h2>
          <div class=script><b>Script:</b> {s["script_text"]}</div>
          <div class=intent>Visual intent: <b>{s["subject"]}</b> / <b>{s["action"]}</b> / <b>{s["location"]}</b>
            &nbsp; JP: {s["jp_subject"]} / {s["jp_action"]} / {s["jp_location"]}</div>
          <div class=queries>{qsum}</div>
          <div class=grid>{"".join(cards) or "<i>no results</i>"}</div>
        </section>''')
    return f'''<!doctype html><meta charset=utf-8><title>Scrape Trainer &middot; {run_id}</title>
<style>body{{font-family:system-ui,Segoe UI,sans-serif;margin:16px;background:#faf7ef}}
.scene{{border:1px solid #ccc;border-radius:8px;padding:12px;margin:14px 0;background:#fff}}
.script{{font-size:15px;margin:4px 0}} .intent,.queries{{font-size:12px;color:#555;margin:2px 0}}
.grid{{display:flex;flex-wrap:wrap;gap:10px;margin-top:8px}}
.card{{border:1px solid #ddd;border-radius:6px;padding:6px;width:300px;background:#fcfbf7}}
.card.rated{{border-color:#3a7}} .novid{{width:180px;height:100px;background:#eee;display:flex;align-items:center;justify-content:center;color:#999;font-size:12px}}
.meta{{font-size:11px}} .cap{{margin:3px 0;color:#333}} .q{{color:#77c}} .tech{{color:#999}} .rej{{color:#c33}} .vis{{color:#a70}}
.labels{{display:flex;flex-wrap:wrap;gap:2px;margin:4px 0}} .lbl{{font-size:10px;padding:2px 4px;cursor:pointer;border:1px solid #bbb;border-radius:3px;background:#fff}}
.lbl.on{{background:#3a7;color:#fff;border-color:#3a7}} .controls input[type=text],.cmt,.bq{{width:120px}} .cur{{color:#3a7;font-weight:700}}
video{{background:#000;border-radius:4px}}</style>
<h1>Scrape Trainer &mdash; review</h1>
<p>Run <code>{run_id}</code> &middot; mode {run["mode"] if run else "?"} &middot; {len(scenes)} scenes &middot;
{n_res} results &middot; <b>{n_rat} rated</b>. Your rating is ground truth; auto-rejected clips are shown too.</p>
{"".join(blocks)}
<script>
document.querySelectorAll('.card').forEach(card=>{{
  card.querySelectorAll('.lbl').forEach(b=>b.addEventListener('click',()=>{{
    card.querySelectorAll('.lbl').forEach(x=>x.classList.remove('on')); b.classList.add('on');
  }}));
  const cur=card.querySelector('.cur').textContent.trim();
  if(cur) card.querySelectorAll('.lbl').forEach(b=>{{if(b.dataset.l===cur)b.classList.add('on');}});
  card.querySelector('.save').addEventListener('click',()=>{{
    const on=card.querySelector('.lbl.on');
    const body={{run_id:"{run_id}",scene_id:+card.dataset.scene,source_id:card.dataset.src,
      label:on?on.dataset.l:null,is_primary:card.querySelector('.prim').checked,
      is_backup:card.querySelector('.back').checked,
      seg_start:parseFloat(card.querySelector('.segs').value)||null,
      seg_end:parseFloat(card.querySelector('.sege').value)||null,
      comment:card.querySelector('.cmt').value,better_query:card.querySelector('.bq').value}};
    fetch('/rate',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}})
      .then(r=>r.json()).then(res=>{{if(res.ok){{card.classList.add('rated');
        card.querySelector('.cur').textContent=body.label||'';}}else alert(res.error||'save failed');}})
      .catch(()=>alert('save failed'));
  }});
}});
</script>'''


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/media/"):
            return self._serve_media(parsed.path)
        run_id = urllib.parse.parse_qs(parsed.query).get("run", [self.server.run_id])[0]
        html = _page(run_id).encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html))); self.end_headers(); self.wfile.write(html)

    def _serve_media(self, path):
        parts = [urllib.parse.unquote(p) for p in path.split("/") if p]
        # /media/<run_id>/<scene_id>/<source_id>
        if len(parts) < 4:
            self.send_error(404); return
        run_id, scene_id, source_id = parts[1], parts[2], parts[3]
        con = db_connect(run_id)
        row = con.execute("SELECT proxy_path FROM results WHERE run_id=? AND scene_id=? AND source_id=?",
                          (run_id, int(scene_id), source_id)).fetchone()
        con.close()
        fp = Path(row["proxy_path"]) if row and row["proxy_path"] else None
        if not fp or not fp.exists():
            self.send_error(404); return
        # serve a browser-playable H.264 preview (TikTok proxies are often HEVC, which
        # Chrome/Edge can't decode -> dead black player). Created once, then cached.
        prev = ensure_preview(fp)
        if prev:
            fp = Path(prev)
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
        self.send_header("Content-Type", "video/mp4")
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

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/rate":
            self.send_error(404); return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            data = {}
        try:
            con = db_connect(data["run_id"])
            db_save_rating(con, data["run_id"], int(data["scene_id"]), data["source_id"],
                           label=data.get("label"), is_primary=data.get("is_primary"),
                           is_backup=data.get("is_backup"), seg_start=data.get("seg_start"),
                           seg_end=data.get("seg_end"), comment=data.get("comment", ""),
                           better_query=data.get("better_query", ""))
            con.close()
            res = {"ok": True}
        except Exception as exc:                        # noqa: BLE001
            res = {"ok": False, "error": str(exc)}
        body = json.dumps(res).encode("utf-8")
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


def serve(run_id, port=7870):
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.run_id = run_id
    url = f"http://127.0.0.1:{port}/?run={run_id}"
    print(f"[trainer] review UI at {url}")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    server.serve_forever()
