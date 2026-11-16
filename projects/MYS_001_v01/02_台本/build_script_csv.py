"""台本の原稿（script_partN.txt）を、指示書の列のCSVに変換し、字数と推定尺を出す。

原稿の書き方:
  ## S001 章の名前          … 場面の始まり
  @画面 説明                … 以後の台詞の「画面と動作」
  @音 説明                  … 以後の台詞の「音楽と効果音」
  @出典 SRC01 伝承          … 以後の台詞の出典IDと事実区分（"@出典 - 演出" で解除）
  ず：台詞 / め：台詞        … ずんだもん / 四国めたん
  （無言 6）                … 台詞のない間（秒）

推定尺は仮。読み上げ速度 CHARS_PER_SEC と台詞間の間 GAP_SEC で見積もる。
本番の尺は仮音声（VOICEVOX）で測る（指示書: 声の速度の一括変更で尺を合わせない）。

使い方: python build_script_csv.py   → 台本.csv と 尺の見積もり.txt を同じフォルダに書く
"""
import csv
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHARS_PER_SEC = 5.0   # ずんだもん・めたんの標準速度の仮の目安（仮音声で較正する）
GAP_SEC = 0.6         # 台詞と台詞の間
SPEAKERS = {"ず": "ずんだもん", "め": "四国めたん"}


def tc(sec: float) -> str:
    s = int(round(sec))
    return f"{s // 60:02d}:{s % 60:02d}"


def spoken_len(text: str) -> int:
    return len(re.sub(r"[「」『』（）()、。！？…ー・\s]", "", text))


def main() -> None:
    rows, scenes = [], []
    t = 0.0
    total_chars = 0
    for part in sorted(HERE.glob("script_part*.txt")):
        scene = screen = sound = ""
        src, kind = "", "演出"
        for raw in part.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#!"):
                continue
            if line.startswith("## "):
                scene = line[3:].split()[0]
                scenes.append((scene, line[3:], t))
                continue
            if line.startswith("@画面"):
                screen = line[3:].strip()
                continue
            if line.startswith("@音"):
                sound = line[2:].strip()
                continue
            if line.startswith("@出典"):
                parts = line[3:].split()
                src = "" if parts[0] == "-" else parts[0]
                kind = parts[1] if len(parts) > 1 else "演出"
                continue
            m = re.match(r"（無言\s*([\d.]+)）", line)
            if m:
                dur = float(m.group(1))
                rows.append([scene, tc(t), tc(t + dur), "（無言）", "", screen, sound, src, kind])
                t += dur
                continue
            m = re.match(r"(ず|め)：(.+)", line)
            if not m:
                raise SystemExit(f"{part.name}: 読めない行: {raw}")
            text = m.group(2).strip()
            n = spoken_len(text)
            total_chars += len(text)
            dur = n / CHARS_PER_SEC + GAP_SEC
            rows.append([scene, tc(t), tc(t + dur), SPEAKERS[m.group(1)], text, screen, sound, src, kind])
            t += dur
    with open(HERE / "台本.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["シーンID", "開始", "終了", "話者", "台詞", "画面と動作", "音楽と効果音", "出典ID", "事実区分"])
        w.writerows(rows)
    lines = [f"読み上げ本文の字数: {total_chars}（目標 16,000〜19,000）",
             f"推定尺: {tc(t)}（目標 59:30〜60:30。仮の速度 {CHARS_PER_SEC}字/秒・間 {GAP_SEC}秒）",
             "", "場面ごとの推定開始時刻:"]
    lines += [f"  {tc(st)}  {title}" for _, title, st in scenes]
    (HERE / "尺の見積もり.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:2]))


if __name__ == "__main__":
    main()
