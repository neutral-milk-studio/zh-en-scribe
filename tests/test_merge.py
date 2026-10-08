"""对比与合并逻辑的测试。不需要下载模型：uv run pytest"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import transcribe as T  # noqa: E402

needs_dict = pytest.mark.skipif(not Path("/usr/share/dict/words").exists(), reason="需要系统英文词典")


@pytest.fixture(scope="module")
def checker():
    return T.EnglishChecker([], [])


def resolve(sv, wh, checker):
    final, flag, note, *_ = T.resolve({"sensevoice": sv, "whisper": wh}, 0.8, checker, None)
    return final, flag, note


def test_英文分歧取whisper():
    assert T.rule_choice("prot", "prompt") == "W"


def test_中文分歧取sensevoice():
    assert T.rule_choice("跑偏", "泡片") == "S"


@needs_dict
def test_合并_英文用whisper_中文用sensevoice(checker):
    final, flag, _ = resolve("给了他一些prot，它就跑偏了。", "给了他一些prompt它就泡片了", checker)
    assert final == "给了他一些prompt，它就跑偏了。"
    assert not flag


def test_只差标点语气词大小写不算分歧():
    _, ops = T.find_diffs("嗯，好的。OK", "好的ok")
    assert ops == []


def test_whisper输出外文乱码时直接用sensevoice(checker):
    final, flag, _ = resolve("嗯，对的对的。", "はい。動画です", checker)
    assert final == "嗯，对的对的。"
    assert not flag


def test_两模型差异过大时整段标出且不合并(checker):
    final, flag, note = resolve("我觉得没有什么大问题。", "好的那我们开始吧", checker)
    assert final == "我觉得没有什么大问题。"
    assert flag and "差异过大" in note


@needs_dict
def test_查不到的英文词要标出(checker):
    final, flag, note = resolve("Harrier has never heard of Google.", "Herbudo has never heard of Google.", checker)
    assert flag
    assert "herbudo" in note


@needs_dict
def test_两个词连写不算存疑(checker):
    assert checker.unknown("非常的makesense") == []
    assert checker.unknown("herbudo") == ["herbudo"]


def test_过滤whisper常见幻觉句():
    assert T.clean("请不吝点赞 订阅 转发 打赏支持明镜与点点栏目", []) == ""


def test_压缩重复():
    assert T.clean("我呃我呃我呃我呃我呃我呃", []) == "我呃"


def test_纠错表长的先替换():
    corrections = sorted([("XVT", "ChatGPT"), ("XXVT", "ChatGPT")], key=lambda p: -len(p[0]))
    assert T.clean("我一般用XXVT", corrections) == "我一般用ChatGPT"


def test_只有标点的段不输出(tmp_path, checker):
    data = {"path": str(tmp_path / "a.m4a"), "total": 10.0,
            "segs": [(0, 1, {"sensevoice": ".", "whisper": ""}), (2, 4, {"sensevoice": "好的。", "whisper": "好的"})]}
    T.write_outputs(data, tmp_path, 0.8, checker)
    segs = json.loads((tmp_path / "a.segments.json").read_text())["segments"]
    assert [s["text"] for s in segs] == ["好的。"]
