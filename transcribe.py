"""zh-en-scribe：中英混说录音的本地转写与核对工具。

Whisper 和 SenseVoice 分别识别每一段，分歧处只在两者之间选择（英文取 Whisper，中文取 SenseVoice），
并标出需要人工回听的段。输出 {名}.transcript.md、{名}.compare.md，识别结果缓存在 {名}.asr.json。

用法：
    uv run transcribe.py interview.m4a
    uv run transcribe.py a.m4a b.m4a --vocab my_vocab.txt --corrections my_corrections.tsv
    uv run transcribe.py a.m4a --context interview_guide/     # 从项目文档提取英文术语
"""
import argparse
import gc
import json
import re
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SR = 16000
WHISPER_REPO = "mlx-community/whisper-large-v3-turbo"

# Whisper 在静音处的常见幻觉句
HALLUCINATIONS = ["请不吝点赞", "订阅", "转发", "打赏支持", "明镜与点点", "字幕由", "感谢观看", "Amara.org"]
# Whisper 幻觉时常输出日文、韩文、俄文、越南文等
FOREIGN = re.compile(r"[぀-ヿ가-힯Ѐ-ӿͰ-ϿÀ-ɏḀ-ỿ]")
FILLERS = set("嗯呃啊哦哎诶唉嘛吧呢")
LATIN = re.compile(r"[A-Za-z]")
STOPWORDS = set("""the and for with that this from are was were you your our not but have has had can
will would should could into about what when where which who how why all any some more most other than
then them they their there these those its also just like very only over such use used using via per
each both may might must one two new etc http https www com md png pdf""".split())


def ts(t):
    return f"{int(t // 60):02d}:{int(t % 60):02d}"


def read_lines(path):
    return [l.strip() for l in Path(path).read_text().splitlines() if l.strip() and not l.startswith("#")]


def load_vocab(vocab_file, context_paths):
    """术语表 + 从项目文档自动提取的英文术语（优先含大写的专有名词，其次高频词）。"""
    words = read_lines(vocab_file)
    counts = {}
    for p in context_paths:
        for f in [p] if p.is_file() else [f for f in p.rglob("*") if f.suffix in (".md", ".txt")]:
            for tok in re.findall(r"[A-Za-z][A-Za-z.+\-]*[A-Za-z]", f.read_text(errors="ignore")):
                if tok.lower() not in STOPWORDS and len(tok) > 2:
                    counts[tok] = counts.get(tok, 0) + 1
    auto = sorted((t for t in counts if counts[t] >= 2 and t not in words),
                  key=lambda t: (t == t.lower(), -counts[t]))
    return words + auto[:max(0, 60 - len(words))]  # Whisper 提示词有长度上限


def load_corrections(path):
    pairs = [tuple(l.split("\t", 1)) for l in read_lines(path) if "\t" in l]
    return sorted(((w, r.strip()) for w, r in pairs), key=lambda p: -len(p[0]))  # 长的先替换


def clean(text, corrections):
    if any(h in text for h in HALLUCINATIONS):
        return ""
    text = re.sub(r"[\U00010000-\U0010FFFF]", "", text)  # SenseVoice 的情绪 emoji
    text = re.sub(r"(.{1,4}?)\1{4,}", r"\1", text)        # "我呃我呃我呃…"
    for wrong, right in corrections:
        text = text.replace(wrong, right)
    return text.strip()


# ---------- 识别 ----------

def load_audio(path):
    cmd = ["ffmpeg", "-v", "error", "-i", str(path), "-af", "loudnorm", "-ar", str(SR), "-ac", "1", "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, capture_output=True, check=True).stdout, dtype=np.float32).copy()


def local_model(repo):
    """已缓存就用本地路径，断网也能跑（否则 FunASR 会联网检查，断网时报 "model is not registered"）。"""
    cached = Path.home() / ".cache/modelscope/models" / repo.replace("/", "--") / "snapshots/master"
    return str(cached) if (cached / "configuration.json").exists() else repo


def vad_chunks(audio, max_len_s):
    """VAD 去掉静音，再把语音段合并成不超过 max_len_s 秒的块。"""
    from funasr import AutoModel
    vad = AutoModel(model=local_model("iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"),
                    disable_update=True, disable_pbar=True, log_level="ERROR")
    chunks = []
    for s, e in vad.generate(input=audio, fs=SR)[0]["value"]:
        s, e = s / 1000, e / 1000
        if chunks and e - chunks[-1][0] <= max_len_s and s - chunks[-1][1] < 1.5:
            chunks[-1][1] = e
        else:
            chunks.append([s, e])
    return chunks


class Whisper:
    def __init__(self, vocab):
        import mlx_whisper
        self.mlx_whisper = mlx_whisper
        self.prompt = "以下是一段中文访谈，夹杂英文" + (f"，会提到{'、'.join(vocab)}。" if vocab else "。")

    def __call__(self, clip):
        r = self.mlx_whisper.transcribe(clip, path_or_hf_repo=WHISPER_REPO, language="zh",
                                        initial_prompt=self.prompt, condition_on_previous_text=False,
                                        no_speech_threshold=0.6, compression_ratio_threshold=2.2)
        return "".join(s["text"] for s in r["segments"])


class SenseVoice:
    def __init__(self):
        from funasr import AutoModel
        from funasr.utils.postprocess_utils import rich_transcription_postprocess
        self.post = rich_transcription_postprocess
        self.model = AutoModel(model=local_model("iic/SenseVoiceSmall"),
                               disable_update=True, disable_pbar=True, log_level="ERROR")

    def __call__(self, clip):
        return self.post(self.model.generate(input=clip, language="auto", use_itn=True)[0]["text"])


def asr_file(path, engines, max_len_s, corrections):
    print(f"→ {path.name}", file=sys.stderr)
    audio = load_audio(path)
    chunks = vad_chunks(audio, max_len_s)
    segs = []
    for i, (s, e) in enumerate(chunks, 1):
        print(f"  识别 {i}/{len(chunks)}  [{ts(s)}]", file=sys.stderr, flush=True)
        clip = audio[int(s * SR):int(e * SR)]
        results = {n: clean(engine(clip), corrections) for n, engine in engines.items()}
        if any(results.values()):
            segs.append((s, e, results))
    return {"path": str(path), "total": len(audio) / SR, "segs": segs}


# ---------- 对比与合并 ----------

def norm(text):
    """对比时忽略标点、空格、语气词和大小写。"""
    return "".join(c for c in re.sub(r"[^\w]", "", text).lower() if c not in FILLERS)


def find_diffs(sv, wh):
    """以 SenseVoice（带标点）为底稿，返回一致度和实质分歧 [(i1, i2, sv片段, wh片段)]。"""
    ops = [(i1, i2, sv[i1:i2], wh[j1:j2])
           for op, i1, i2, j1, j2 in SequenceMatcher(None, sv, wh, autojunk=False).get_opcodes()
           if op != "equal" and norm(sv[i1:i2]) != norm(wh[j1:j2])]
    return SequenceMatcher(None, norm(sv), norm(wh), autojunk=False).ratio(), ops


class EnglishChecker:
    """查不到的英文词：系统词典 + 术语表 + 纠错表都没有。没有系统词典时不检查。"""

    def __init__(self, vocab, corrections):
        d = Path("/usr/share/dict/words")
        self.known = {w.lower() for w in d.read_text().split()} if d.exists() else set()
        self.enabled = bool(self.known)
        for text in vocab + [r for _, r in corrections]:
            self.known |= {w.lower() for w in re.findall(r"[A-Za-z]+", text)}

    def _base(self, w):  # 粗略处理复数和时态
        return len(w) <= 2 or any(x in self.known for x in (
            w, w.rstrip("s"), w[:-2] if w.endswith(("ed", "es")) else w, w[:-3] if w.endswith("ing") else w))

    def unknown(self, text):
        if not self.enabled:
            return []
        # 识别结果常把两个词连写（"linebecause"），能拆成两个 ≥4 字母的已知词也算
        return sorted({w for w in re.findall(r"[a-z]+", text.lower()) if not self._base(w) and not any(
            w[:i] in self.known and w[i:] in self.known for i in range(4, len(w) - 3))})


def rule_choice(a, b):
    return "W" if LATIN.search(a + b) else "S"


def merge(sv, ops, choices):
    out, pos = [], 0
    for (i1, i2, a, b), c in zip(ops, choices):
        out += [sv[pos:i1], b if c == "W" else f"{a}[?]" if c == "?" else a]
        pos = i2
    return "".join(out + [sv[pos:]])


def resolve(r, threshold, checker, chooser):
    """返回 (合并结果, 是否需回听, 说明, 分歧, 选择, 一致度)。"""
    sv, wh = r.get("sensevoice", ""), r.get("whisper", "")
    if not wh or FOREIGN.search(wh):
        return sv, False, "Whisper 无结果或输出乱码，采用 SenseVoice", [], [], None
    if not sv:
        return wh, True, "SenseVoice 无结果，采用 Whisper", [], [], None
    ratio, ops = find_diffs(sv, wh)
    if not ops:
        return sv, False, "", [], [], ratio
    if ratio < 0.5:
        return sv, True, "两模型差异过大，未自动合并", ops, [], ratio
    choices = chooser(sv, wh, ops) if chooser else [rule_choice(a, b) for *_, a, b in ops]
    final = merge(sv, ops, choices)
    frags = [norm(x) for *_, a, b in ops for x in (a, b) if LATIN.search(x)]
    bad = [w for w in checker.unknown(final) if any(f in w or w in f for f in frags)]
    flag = ratio < threshold or bool(bad) or "?" in choices
    return final, flag, f"英文词存疑：{', '.join(bad)}" if bad else "", ops, choices, ratio


class OllamaChooser:
    """实验选项：让本地 LLM 对每处分歧做选择题（S / W / ?），不允许改写。实测不如规则，默认不用。"""
    PROMPT = """同一段中英混说录音，两个语音识别结果：
S（SenseVoice，中文通常更准）：{sv}
W（Whisper，英文通常更准）：{wh}
术语表：{vocab}
请逐处判断哪个更可能是原话，只能回答 "S"、"W" 或 "?"（都不对）：
{items}
只输出 JSON，例如 {{"1": "S", "2": "W"}}"""

    def __init__(self, model, vocab):
        self.model, self.vocab = model, "、".join(vocab) or "无"

    def __call__(self, sv, wh, ops):
        import urllib.request
        items = "\n".join(f"{k}. S「{a}」 vs W「{b}」" for k, (*_, a, b) in enumerate(ops, 1))
        body = {"model": self.model, "stream": False, "format": "json", "think": False,
                "options": {"temperature": 0},
                "messages": [{"role": "user", "content": self.PROMPT.format(
                    sv=sv, wh=wh, vocab=self.vocab, items=items)}]}
        req = urllib.request.Request("http://localhost:11434/api/chat", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                out = json.loads(json.loads(resp.read())["message"]["content"])
        except Exception as ex:
            print(f"  LLM 调用失败，改用规则：{ex}", file=sys.stderr)
            out = {}
        return [c if (c := str(out.get(str(k), "")).upper()) in ("S", "W", "?") else rule_choice(a, b)
                for k, (*_, a, b) in enumerate(ops, 1)]


# ---------- 输出 ----------

def write_outputs(data, out_dir, threshold, checker, chooser=None):
    path, total, segs = Path(data["path"]), data["total"], data["segs"]
    transcript, compare = [], []
    flagged_secs, n_flagged, n_auto = 0.0, 0, 0
    label = {"S": "SenseVoice", "W": "Whisper", "?": "都不对"}
    for s, e, r in segs:
        final, flag, note, ops, choices, ratio = resolve(r, threshold, checker, chooser)
        if not final:
            continue
        n_auto += bool(ops) and not flag
        n_flagged += flag
        flagged_secs += (e - s) * flag
        mark = "⚠️ " if flag else ""
        transcript.append(f"{mark}[{ts(s)}] {final}")
        compare += [f"{mark}**[{ts(s)}–{ts(e)}]**" + (f" 一致度 {ratio:.0%}" if ratio is not None else ""),
                    f"- **合并**: {final}",
                    f"- sensevoice: {r.get('sensevoice', '')}",
                    f"- whisper: {r.get('whisper', '')}"]
        if choices:
            compare.append("- 分歧：" + "；".join(f"「{a or '∅'}」↔「{b or '∅'}」→ {label[c]}"
                                                for (*_, a, b), c in zip(ops, choices)))
        if note:
            compare.append(f"- 说明：{note}")
        compare.append("")

    summary = (f"需回听：{n_flagged}/{len(segs)} 段标 ⚠️，共 {ts(flagged_secs)}，占录音 {flagged_secs / total:.0%}；"
               f"另有 {n_auto} 段的小分歧已自动合并")
    via = f"LLM（{chooser.model}）选择" if chooser else "规则选择（英文取 Whisper，中文取 SenseVoice）"
    header = [f"# 转写：{path.name}", "", f"- 时长：{ts(total)}",
              f"- 方法：Whisper + SenseVoice，分歧处由{via}，不改写", f"- {summary}",
              "- ⚠️ = 两模型分歧较大 / 英文词存疑 / 无法判断（[?]），引用前请对照录音", "- 未区分说话人", "", "---", ""]
    out_dir.mkdir(parents=True, exist_ok=True)
    for suffix, body in (("transcript", transcript), ("compare", compare)):
        (out_dir / f"{path.stem}.{suffix}.md").write_text("\n".join(header + body) + "\n")
    print(f"  {summary}\n  ✓ {out_dir / path.stem}.transcript.md / .compare.md", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio", nargs="+", type=Path)
    ap.add_argument("--out-dir", type=Path, help="输出目录（默认与音频同目录）")
    ap.add_argument("--vocab", type=Path, default=HERE / "vocab.txt", help="术语表，一行一个词")
    ap.add_argument("--corrections", type=Path, default=HERE / "corrections.tsv", help="纠错表：错误写法<TAB>正确写法")
    ap.add_argument("--context", type=Path, nargs="*", default=[], help="项目文档（如访谈提纲），从中提取英文术语")
    ap.add_argument("--threshold", type=float, default=0.8, help="一致度低于此值标 ⚠️（默认 0.8）")
    ap.add_argument("--max-len", type=float, default=20, help="每段最长秒数（默认 20）")
    ap.add_argument("--llm", metavar="MODEL", nargs="?", const="qwen3:4b", help="实验：用本地 Ollama 模型做选择")
    ap.add_argument("--redo-asr", action="store_true", help="忽略缓存，重新识别")
    args = ap.parse_args()

    vocab = load_vocab(args.vocab, args.context)
    corrections = load_corrections(args.corrections)
    jobs = []
    for path in args.audio:
        out_dir = args.out_dir or path.parent
        cache = out_dir / f"{path.stem}.asr.json"
        cached = cache.exists() and cache.stat().st_mtime > path.stat().st_mtime and not args.redo_asr
        jobs.append((path, out_dir, cache, cached))

    if not all(cached for *_, cached in jobs):
        print(f"术语表 {len(vocab)} 个：{', '.join(vocab) or '（空）'}", file=sys.stderr)
        engines = {"whisper": Whisper(vocab), "sensevoice": SenseVoice()}
        for path, out_dir, cache, cached in jobs:
            if not cached:
                out_dir.mkdir(parents=True, exist_ok=True)
                data = asr_file(path, engines, args.max_len, corrections)
                cache.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        del engines  # 释放识别模型的内存再加载 LLM
        gc.collect()

    checker = EnglishChecker(vocab, corrections)
    chooser = OllamaChooser(args.llm, vocab) if args.llm else None
    for path, out_dir, cache, cached in jobs:
        print(f"→ 合并 {path.name}" + ("（使用缓存）" if cached else ""), file=sys.stderr)
        write_outputs(json.loads(cache.read_text()), out_dir, args.threshold, checker, chooser)


if __name__ == "__main__":
    main()
