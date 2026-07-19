#!/usr/bin/env python3
"""一枚絵フォルダを一括処理する量産用ドライバ。

input/ のPNGをSee-throughでレイヤー分解し、出力PSDを正規化して output/ に集める。
See-through本体は別リポジトリのため、事前にセットアップして場所を
--see-through-dir か環境変数 SEE_THROUGH_DIR で指定する(docs/02参照)。

usage:
  python scripts/batch_decompose.py --input input/ --output output/
  python scripts/batch_decompose.py --input input/ --vram 8gb
  python scripts/batch_decompose.py --normalize-only path/to/psd_dir/   # 分解済みPSDの正規化だけ行う
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import normalize_psd

# VRAM帯ごとのSee-through推論スクリプト(本家README準拠)
VRAM_PROFILES = {
    "high": ["inference/scripts/inference_psd.py"],                    # 16GB+
    "12gb": ["inference/scripts/inference_psd.py", "--group_offload"],  # ~12GB
    "8gb":  ["inference/scripts/inference_psd_quantized.py"],           # ~8GB (NF4量子化)
}


def run_see_through(see_dir: Path, input_dir: Path, vram: str) -> Path:
    script, *extra = VRAM_PROFILES[vram]
    cmd = [sys.executable, script, "--srcp", str(input_dir.resolve()), "--save_to_psd", *extra]
    print(f"[see-through] {' '.join(cmd)}  (cwd={see_dir})")
    subprocess.run(cmd, cwd=see_dir, check=True)
    out_dir = see_dir / "workspace" / "layerdiff_output"
    if not out_dir.exists():
        raise FileNotFoundError(
            f"See-throughの出力ディレクトリが見つかりません: {out_dir}\n"
            "本体のバージョンにより出力先が変わっている可能性があります。"
            "see-throughリポジトリのworkspace/以下を確認してください。")
    return out_dir


def normalize_all(psd_dir: Path, output_dir: Path, config: Path, strict: bool) -> int:
    psds = sorted(psd_dir.rglob("*.psd"))
    if not psds:
        print(f"[error] PSDが見つかりません: {psd_dir}", file=sys.stderr)
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)
    failed = []
    for psd in psds:
        print(f"\n=== {psd.name} ===")
        args = argparse.Namespace(
            psd=str(psd), output=str(output_dir / f"{psd.stem}_normalized.psd"),
            config=str(config), strict=strict, emit_pngs=None, top_first=True,
        )
        if normalize_psd.cmd_normalize(args) != 0:
            failed.append(psd.name)
    print(f"\n完了: {len(psds) - len(failed)}/{len(psds)} 件")
    if failed:
        print(f"失敗: {', '.join(failed)}", file=sys.stderr)
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="input", help="一枚絵PNGを置いたフォルダ")
    parser.add_argument("--output", default="output", help="正規化PSDの出力先")
    parser.add_argument("--see-through-dir", default=os.environ.get("SEE_THROUGH_DIR"),
                        help="see-throughリポジトリの場所 (env: SEE_THROUGH_DIR)")
    parser.add_argument("--vram", choices=VRAM_PROFILES, default="high")
    parser.add_argument("--config", default=str(normalize_psd.DEFAULT_CONFIG))
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--normalize-only", metavar="PSD_DIR",
                        help="分解をスキップし、既存PSDフォルダの正規化のみ実行")
    args = parser.parse_args()

    if args.normalize_only:
        return normalize_all(Path(args.normalize_only), Path(args.output),
                             Path(args.config), args.strict)

    if not args.see_through_dir:
        print("[error] --see-through-dir か環境変数 SEE_THROUGH_DIR を設定してください。"
              "セットアップ手順は docs/02_see_through_setup.md 参照。", file=sys.stderr)
        return 1

    input_dir = Path(args.input)
    if not any(input_dir.glob("*.png")):
        print(f"[error] {input_dir}/ にPNGがありません。", file=sys.stderr)
        return 1

    psd_dir = run_see_through(Path(args.see_through_dir), input_dir, args.vram)
    return normalize_all(psd_dir, Path(args.output), Path(args.config), args.strict)


if __name__ == "__main__":
    sys.exit(main())
