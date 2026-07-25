#!/usr/bin/env python3
"""完成した動画をYouTubeへ自動投稿する。

make_finance_video.py が作った出力ディレクトリ（script.json / video.mp4 /
thumbnail.png）を渡すと、タイトル・概要欄・タグを引き継いでアップロードする。

準備（初回のみ・docs/06 Step5参照）:
  1. pip install google-api-python-client google-auth-oauthlib
  2. Google CloudでOAuthクライアントID（デスクトップアプリ）を作成し、
     configs/client_secret.json に保存
  3. 初回実行時にブラウザが開くのでGoogleアカウントで許可
     （トークンは configs/.youtube_token.json にキャッシュされる）

使い方:
  python scripts/upload_youtube.py output/videos/<ディレクトリ> [--privacy unlisted]

注意: API審査(audit)を通していないプロジェクトからのアップロードは、
指定に関わらず**非公開(private)に固定**される。公開はYouTube Studioから
手動で行うか、審査を申請する（docs/06参照）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "configs" / "finance_channel.yaml"
CLIENT_SECRET = REPO_ROOT / "configs" / "client_secret.json"
TOKEN_CACHE = REPO_ROOT / "configs" / ".youtube_token.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def get_service():
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise SystemExit(
            "依存ライブラリが未導入です:\n"
            "  pip install google-api-python-client google-auth-oauthlib"
        )
    creds = None
    if TOKEN_CACHE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_CACHE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                raise SystemExit(
                    f"OAuthクライアント設定がありません: {CLIENT_SECRET}\n"
                    "docs/06 Step5の手順で client_secret.json を作成してください。"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_CACHE.write_text(creds.to_json(), encoding="utf-8")
    return build("youtube", "v3", credentials=creds)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", help="output/videos/ 配下の動画ディレクトリ")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"])
    args = ap.parse_args()

    outdir = Path(args.dir)
    if not outdir.is_absolute() and not outdir.exists():
        outdir = REPO_ROOT / "output" / "videos" / args.dir
    video = outdir / "video.mp4"
    if not video.exists():
        raise SystemExit(f"video.mp4 がありません: {outdir}（先に render を実行）")
    script = json.loads((outdir / "script.json").read_text(encoding="utf-8"))
    with open(args.config, encoding="utf-8") as f:
        up = yaml.safe_load(f)["upload"]

    description = script.get("description", "").strip()
    footer = up.get("description_footer", "").strip()
    if footer:
        description = f"{description}\n\n{footer}" if description else footer

    body = {
        "snippet": {
            "title": script["title"][:100],
            "description": description[:4900],
            "tags": script.get("tags", []),
            "categoryId": str(up.get("category_id", "27")),
            "defaultLanguage": "ja",
            "defaultAudioLanguage": "ja",
        },
        "status": {
            "privacyStatus": args.privacy or up.get("privacy", "private"),
            "selfDeclaredMadeForKids": bool(up.get("made_for_kids", False)),
            "containsSyntheticMedia": bool(up.get("synthetic_media", True)),
        },
    }

    from googleapiclient.http import MediaFileUpload  # get_service後で確実にimport可能

    yt = get_service()
    print(f"[upload] アップロード中: {video.name} ({video.stat().st_size // 1_000_000}MB)")
    media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    res = None
    while res is None:
        status, res = req.next_chunk()
        if status:
            print(f"[upload] {int(status.progress() * 100)}%", flush=True)
    video_id = res["id"]
    print(f"[upload] OK: https://youtu.be/{video_id}")

    thumb = outdir / "thumbnail.png"
    if thumb.exists():
        try:
            yt.thumbnails().set(videoId=video_id, media_body=str(thumb)).execute()
            print("[upload] サムネイル設定OK")
        except Exception as e:  # 電話番号認証前のアカウントはカスタムサムネ不可
            print(f"[upload] サムネイル設定失敗（後からStudioで手動設定可）: {e}",
                  file=sys.stderr)
    print(f"[upload] 公開状態: {body['status']['privacyStatus']}"
          "（API審査未了の場合はprivate固定。YouTube Studioで確認を）")


if __name__ == "__main__":
    main()
