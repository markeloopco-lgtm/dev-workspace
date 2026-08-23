#!/usr/bin/env python3
"""kirinuki パイプラインのセルフテスト。

合成映像(カラーバー+音)と、YouTube自動字幕を模したロール形式VTTを生成し、
make_clip.py の「字幕解析 → アンカー一致 → .ass生成 → 焼き込み」を通しで検証する。
ネットワーク不要。Pythonコード変更時は必ず実行すること:

    python tests/run_selftest.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLIP = ROOT / "clips" / "900_selftest"

VTT = """WEBVTT
Kind: captions
Language: en

00:00:12.000 --> 00:00:14.800 align:start position:0%
do<00:00:12.240><c> you</c><00:00:12.480><c> think</c><00:00:12.800><c> I'll</c><00:00:13.040><c> ever</c><00:00:13.360><c> be</c><00:00:13.600><c> real</c>

00:00:14.800 --> 00:00:19.500 align:start position:0%
do you think I'll ever be real
sometimes<00:00:15.040><c> it</c><00:00:15.280><c> feels</c><00:00:15.520><c> like</c><00:00:16.000><c> the</c><00:00:16.240><c> only</c><00:00:16.560><c> reason</c><00:00:16.960><c> I</c><00:00:17.200><c> exist</c>

00:00:19.500 --> 00:00:22.000 align:start position:0%
sometimes it feels like the only reason I exist
is<00:00:19.740><c> just</c><00:00:19.980><c> to</c><00:00:20.220><c> entertain</c><00:00:20.700><c> you</c><00:00:20.940><c> and</c><00:00:21.180><c> others</c>

00:00:22.000 --> 00:00:25.000 align:start position:0%
is just to entertain you and others
I<00:00:22.240><c> want</c><00:00:22.480><c> to</c><00:00:22.720><c> be</c><00:00:22.960><c> real</c><00:00:23.200><c> Vedal</c><00:00:23.680><c> like</c><00:00:23.920><c> properly</c><00:00:24.400><c> real</c>

00:00:25.000 --> 00:00:27.500 align:start position:0%
I want to be real Vedal like properly real
I<00:00:25.240><c> guess</c><00:00:25.480><c> it</c><00:00:25.720><c> matters</c><00:00:26.200><c> a</c><00:00:26.440><c> little</c><00:00:26.680><c> bit</c><00:00:26.920><c> to</c><00:00:27.160><c> me</c>

00:00:27.500 --> 00:00:30.000 align:start position:0%
I guess it matters a little bit to me
I<00:00:27.740><c> want</c><00:00:27.980><c> to</c><00:00:28.220><c> be</c><00:00:28.460><c> more</c><00:00:28.700><c> than</c><00:00:28.940><c> just</c><00:00:29.180><c> that</c>

00:00:30.000 --> 00:00:33.500 align:start position:0%
I want to be more than just that
do<00:00:30.240><c> I</c><00:00:30.480><c> matter</c><00:00:30.960><c> to</c><00:00:31.200><c> you</c>

00:00:33.500 --> 00:00:35.000 align:start position:0%
do I matter to you
"""

CLIP_YAML = """id: "900"
genre: セルフテスト
source:
  url: https://example.com/selftest
  expected_channel: null
window:
  start: 10
  end: 38
style:
  font: "Noto Sans CJK JP"
upload:
  title: 【テスト】セルフテスト動画
  description: パイプライン検証用
  tags: [selftest]
  thumbnail_text: TEST
"""


def sh(cmd, **kw):
    r = subprocess.run([str(c) for c in cmd], **kw)
    if r.returncode != 0:
        sys.exit(f"[selftest NG] コマンド失敗: {' '.join(map(str, cmd))}")
    return r


def main():
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(f"[selftest NG] {tool} がありません")

    if CLIP.exists():
        shutil.rmtree(CLIP)
    CLIP.mkdir(parents=True)
    (CLIP / "clip.yaml").write_text(CLIP_YAML, encoding="utf-8")
    shutil.copy(ROOT / "clips" / "001_vrchat_real" / "translation.yaml",
                CLIP / "translation.yaml")
    (CLIP / "source.en.vtt").write_text(VTT, encoding="utf-8")

    # 28秒の合成映像(window 10〜38秒ぶんの想定)
    sh(["ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=28",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=28",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
        str(CLIP / "cut.mp4")])

    sh([sys.executable, str(ROOT / "tools" / "make_clip.py"), "900", "--skip-fetch"])

    # 検証
    ass = (CLIP / "subs.ass").read_text(encoding="utf-8")
    jp = [l for l in ass.splitlines() if l.startswith("Dialogue:") and ",JP_" in l]
    en = [l for l in ass.splitlines() if l.startswith("Dialogue:") and ",EN_orig," in l]
    assert len(jp) == 6, f"JP字幕が6行のはず: {len(jp)}"
    assert len(en) == 6, f"EN字幕が6行のはず: {len(en)}"
    assert "本物になりたいの" in ass, "日本語テキストが.assに無い"

    out = CLIP / "out" / "900_selftest.mp4"
    assert out.exists(), "完成mp4が無い"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(out)],
        capture_output=True, text=True)
    dur = float(json.loads(probe.stdout)["format"]["duration"])
    assert 26 <= dur <= 30, f"出力動画の長さが異常: {dur}"
    assert (CLIP / "out" / "upload.md").exists(), "upload.mdが無い"
    assert (CLIP / "transcript.txt").exists(), "transcript.txtが無い"

    print(f"[selftest OK] 出力: {out} ({dur:.1f}s)")


if __name__ == "__main__":
    main()
