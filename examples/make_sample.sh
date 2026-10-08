#!/bin/sh
# 用 macOS 自带的语音合成生成一段虚构的中英混说示例录音，用来试跑工具。
# 用法：sh examples/make_sample.sh   →  生成 examples/sample.m4a
set -e
cd "$(dirname "$0")"
# 优先用标准普通话语音 Tingting，没有的话用任意一个 zh_CN 语音
VOICE=$(LC_ALL=C say -v '?' | LC_ALL=C awk '$0 ~ /zh_CN/ {print $1}' | grep -x Tingting || LC_ALL=C say -v '?' | LC_ALL=C awk '$0 ~ /zh_CN/ {print $1; exit}')
[ -n "$VOICE" ] || { echo "没有找到中文语音：系统设置 → 辅助功能 → 朗读内容 → 系统语音 → 管理语音，添加一个中文语音"; exit 1; }
say -v "$VOICE" -o sample.aiff \
  "好，那我们开始吧。你平时怎么用这个 app？ [[slnc 800]] \
   我一般是下班以后打开，主要看一下 dashboard 上这周花了多少钱。 [[slnc 800]] \
   但是它的 notification 太多了，我基本都关掉了。 [[slnc 800]] \
   那你觉得哪个 feature 最有用？ [[slnc 800]] \
   应该是 budget 那个吧，设个上限，超了它会提醒你，这个挺 make sense 的。"
ffmpeg -v error -y -i sample.aiff -c:a aac sample.m4a && rm sample.aiff
echo "✓ 已生成 examples/sample.m4a（语音：${VOICE}）"
