"""核对网页：逐段听录音、对照文字稿判断对错，导出结果。

先运行 transcribe.py，再：
    uv run review.py 访谈.m4a              # 核对模式：显示 ⚠️，可改文字，导出校对后的文字稿
    uv run review.py 访谈.m4a --blind      # 盲审模式：不显示 ⚠️，用于评估工具准确率
    uv run review.py 访谈.m4a --score review_results.json   # 盲审结束后计算漏标率
网页和音频片段生成在 .segments.json 旁边的 {名}.review/ 目录，只在本地打开，不上传。
"""
import argparse
import html
import json
import subprocess
import sys
import webbrowser
from pathlib import Path


def ts(t):
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def find_segments(audio, seg_file):
    f = seg_file or audio.with_suffix(".segments.json")
    if not f.exists():
        sys.exit(f"找不到 {f}，请先运行：uv run transcribe.py {audio}（或用 --segments 指定）")
    return f


def cut_clips(audio, segs, out):
    (out / "clips").mkdir(parents=True, exist_ok=True)
    for i, g in enumerate(segs, 1):
        clip = out / "clips" / f"{i:03d}.m4a"
        if not clip.exists():
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(max(0, g["start"] - 0.3)),
                            "-to", str(g["end"] + 0.3), "-i", str(audio), "-c:a", "aac", "-b:a", "96k",
                            str(clip)], check=True)


def card(i, g, blind):
    e = html.escape
    flag = g["flag"] and not blind
    badge = f'<span class="badge">⚠️ {e(g["note"] or "两模型分歧较大")}</span>' if flag else ""
    models = "" if blind else (
        f'<details><summary>两个模型的原始结果</summary><p>SenseVoice：{e(g["sensevoice"])}</p>'
        f'<p>Whisper：{e(g["whisper"])}</p></details>')
    box = ('<textarea rows="2" placeholder="（可选）正确的原话 / 备注"></textarea>' if blind else
           f'<textarea rows="2" data-orig="{e(g["text"])}">{e(g["text"])}</textarea>')
    return f"""<section class="card{' flagged' if flag else ''}" data-i="{i}" data-t="{ts(g['start'])}">
  <div class="meta"><span class="num">{i:02d}</span><span>{ts(g['start'])} · {round(g['end'] - g['start'])} 秒</span>{badge}</div>
  <audio controls preload="none" src="clips/{i:03d}.m4a"></audio>
  {'<p class="text">' + e(g['text']) + '</p>' if blind else ''}{box}
  <div class="choices">
    <label><input type="radio" name="r{i}" value="ok"> ✅ 正确</label>
    <label><input type="radio" name="r{i}" value="minor"> ⚠️ 小错</label>
    <label><input type="radio" name="r{i}" value="major"> ❌ 实质错误</label>
  </div>{models}
</section>"""


def build_page(title, segs, blind, store_key):
    n_flag = sum(g["flag"] for g in segs)
    if blind:
        intro = ("逐段播放，对照文字判断转写是否正确。<b>页面不显示哪些段被工具标记</b>，以免影响判断。")
        tools = ""
    else:
        intro = (f"工具标出了 <b>{n_flag}</b> 段可能有错（⚠️）。可以直接修改文字框里的内容，"
                 "完成后导出校对后的文字稿。")
        tools = '<label class="filter"><input type="checkbox" id="only"> 只看 ⚠️</label>'
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · 核对</title>
<style>
:root {{ --bg:#fafaf8; --card:#fff; --ink:#1d1d1f; --muted:#6b6b70; --line:#e4e4e0; --accent:#2f6fed;
  --done:#eef7f1; --warn:#fff6e0; --warn-ink:#8a5a00; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16171a; --card:#1f2024; --ink:#ececee; --muted:#9a9aa2;
  --line:#33343a; --accent:#7aa2ff; --done:#1e2e24; --warn:#3a3020; --warn-ink:#f0c060; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; }}
main {{ max-width:760px; margin:0 auto; padding:24px 16px 120px; }}
h1 {{ font-size:22px; margin:0 0 8px }}
.intro {{ color:var(--muted); font-size:14px; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 16px; }}
.intro b {{ color:var(--ink) }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px 16px; margin:14px 0; }}
.card.flagged {{ border-left:4px solid var(--warn-ink) }}
.card.done {{ background:var(--done) }}
.meta {{ display:flex; flex-wrap:wrap; gap:6px 10px; align-items:center; color:var(--muted); font-size:13px; margin-bottom:6px }}
.num {{ font-weight:600; color:var(--ink) }}
.badge {{ background:var(--warn); color:var(--warn-ink); border-radius:6px; padding:1px 8px }}
audio {{ width:100%; height:36px }}
.text {{ font-size:17px; margin:10px 0 }}
textarea {{ width:100%; margin:8px 0; font:inherit; font-size:16px; padding:6px 8px; border:1px solid var(--line); border-radius:8px; background:transparent; color:inherit; }}
.choices {{ display:flex; flex-wrap:wrap; gap:8px 18px; font-size:15px }}
.choices label, .filter {{ cursor:pointer }}
details {{ margin-top:8px; font-size:13px; color:var(--muted) }}
details p {{ margin:4px 0 }}
.bar {{ position:fixed; left:0; right:0; bottom:0; background:var(--card); border-top:1px solid var(--line); padding:10px 16px;
  display:flex; flex-wrap:wrap; justify-content:center; gap:10px 16px; align-items:center; font-size:14px; }}
button {{ font:inherit; padding:7px 14px; border-radius:8px; border:0; background:var(--accent); color:#fff; cursor:pointer }}
select {{ font:inherit }}
.hide {{ display:none }}
</style></head><body><main>
<h1>{html.escape(title)}</h1>
<div class="intro">{intro}<br>
  ✅ <b>正确</b>：与原话一致（标点、嗯/啊等语气词、他/她/它的差别不算错）<br>
  ⚠️ <b>小错</b>：有错但不影响理解　❌ <b>实质错误</b>：意思变了、关键词错了、漏了或多了内容<br>
  进度自动保存在本浏览器里。</div>
{"".join(card(i, g, blind) for i, g in enumerate(segs, 1))}
</main>
<div class="bar"><span id="prog"></span>{tools}
  <label>倍速 <select id="rate"><option>1</option><option>1.25</option><option>1.5</option><option>2</option></select></label>
  <button id="exp">导出结果</button>{'' if blind else '<button id="txt">导出校对后的文字稿</button>'}</div>
<script>
const KEY = {json.dumps(store_key)};
const cards = [...document.querySelectorAll(".card")];
let saved = {{}};
try {{ saved = JSON.parse(localStorage.getItem(KEY) || "{{}}"); }} catch (e) {{}}
function collect() {{
  const res = {{}};
  for (const c of cards) {{
    const r = c.querySelector("input:checked");
    res[c.dataset.i] = {{ label: r ? r.value : null, text: c.querySelector("textarea").value }};
    c.classList.toggle("done", !!r);
  }}
  return res;
}}
function update() {{
  const res = collect();
  document.getElementById("prog").textContent = Object.values(res).filter(x => x.label).length + " / " + cards.length + " 已判断";
  try {{ localStorage.setItem(KEY, JSON.stringify(res)); }} catch (e) {{}}
}}
for (const c of cards) {{
  const s = saved[c.dataset.i];
  if (s) {{
    if (s.label) c.querySelector(`input[value="${{s.label}}"]`).checked = true;
    if (s.text != null) c.querySelector("textarea").value = s.text;
  }}
  c.addEventListener("input", update);
  const a = c.querySelector("audio");
  a.addEventListener("play", () => {{ a.playbackRate = +document.getElementById("rate").value; }});
  a.addEventListener("ended", () => {{
    let n = c.nextElementSibling;
    while (n && n.classList.contains("hide")) n = n.nextElementSibling;
    if (n && n.classList.contains("card")) n.scrollIntoView({{ behavior: "smooth", block: "center" }});
  }});
}}
update();
function download(name, text, type) {{
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], {{ type }})); a.download = name; a.click();
}}
document.getElementById("exp").onclick = () => download("review_results.json", JSON.stringify(collect(), null, 1), "application/json");
const txt = document.getElementById("txt");
if (txt) txt.onclick = () => download("transcript.reviewed.md",
  cards.map(c => `[${{c.dataset.t}}] ${{c.querySelector("textarea").value}}`).join("\\n") + "\\n", "text/markdown");
const only = document.getElementById("only");
if (only) only.onchange = () => cards.forEach(c => c.classList.toggle("hide", only.checked && !c.classList.contains("flagged")));
</script></body></html>"""


def score(segs, results_file):
    res = json.loads(Path(results_file).read_text())
    rows = {(True, l): 0 for l in ("ok", "minor", "major")} | {(False, l): 0 for l in ("ok", "minor", "major")}
    secs = {True: 0.0, False: 0.0}
    for i, g in enumerate(segs, 1):
        label = res.get(str(i), {}).get("label")
        if label:
            rows[(g["flag"], label)] += 1
            secs[g["flag"]] += g["end"] - g["start"]
    print(f"{'':10}{'✅ 正确':>8}{'⚠️ 小错':>8}{'❌ 实质':>8}{'时长':>8}")
    for f, name in ((True, "标了 ⚠️"), (False, "没标")):
        print(f"{name:10}{rows[(f, 'ok')]:>8}{rows[(f, 'minor')]:>8}{rows[(f, 'major')]:>8}{ts(secs[f]):>8}")
    for kind, labels in (("实质错误", ("major",)), ("所有错误（含小错）", ("minor", "major"))):
        hit = sum(rows[(True, l)] for l in labels)
        miss = sum(rows[(False, l)] for l in labels)
        print(f"\n{kind}：共 {hit + miss} 段，被标出 {hit} 段，漏标 {miss} 段"
              + (f"（漏标率 {miss / (hit + miss):.0%}）" if hit + miss else ""))
    n_flag = sum(rows[(True, l)] for l in ("ok", "minor", "major"))
    if n_flag:
        print(f"标记准确率：标了 ⚠️ 的 {n_flag} 段中，{n_flag - rows[(True, 'ok')]} 段确实有错"
              f"（{(n_flag - rows[(True, 'ok')]) / n_flag:.0%}）")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio", type=Path)
    ap.add_argument("--segments", type=Path, help="默认为与录音同名的 .segments.json")
    ap.add_argument("--blind", action="store_true", help="盲审模式：不显示 ⚠️")
    ap.add_argument("--score", metavar="RESULTS_JSON", help="用盲审导出的结果计算漏标率")
    ap.add_argument("--no-open", action="store_true", help="生成后不自动打开浏览器")
    args = ap.parse_args()

    seg_file = find_segments(args.audio, args.segments)
    segs = json.loads(seg_file.read_text())["segments"]
    if args.score:
        return score(segs, args.score)
    out = seg_file.parent / f"{args.audio.stem}.review"
    cut_clips(args.audio, segs, out)
    mode = "blind" if args.blind else "review"
    page = out / f"{mode}.html"
    page.write_text(build_page(args.audio.stem, segs, args.blind, f"zh-en-scribe:{args.audio.stem}:{mode}"))
    print(f"✓ {page}")
    if not args.no_open:
        webbrowser.open(page.resolve().as_uri())


if __name__ == "__main__":
    main()
