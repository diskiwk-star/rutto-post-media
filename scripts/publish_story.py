#!/usr/bin/env python3
"""
泊Rutto ストーリーズ自動投稿（GitHub Actions実行用）

環境変数:
  IG_ACCESS_TOKEN : Instagram Login APIのアクセストークン（Actions Secret）
  IG_USER_ID      : 泊RuttoのIGユーザーID（Actions Variable・任意）。空ならトークンから取得する
  EXPECTED_USERNAME : 投稿先として想定するIGユーザー名。トークンが別アカウントのものなら投稿しない
                    （いりこ等のトークンを貼り間違えたときの安全装置）。既定は tomarutto_kagawa
  SLOT            : "daily"（11:30・施設ローテ） or "frequent"（19:00・頻出ページ）
  TARGET_TIME     : 投稿目標時刻（JST "HH:MM"）。既定は daily=11:30 / frequent=19:00
  DRY_RUN         : "1" なら投稿せず、選ばれた素材とAPIリクエストを表示のみ
  QUEUE_DATE      : 日付の上書き（"YYYY-MM-DD"）。テスト用
  RAW_BASE        : 素材URLの先頭。既定は本リポジトリのmainのraw URL

動作:
  stories/manifest.json（素材一覧）と state/rotation.json（ローテの進み具合）から
  scripts/pick.py で今日の素材を選び、STORIESとして投稿する。
  成功したら state/rotation.json と state/history.json を更新（ワークフロー側でコミット）。
  history に同じ日付・枠があれば投稿済みとして何もしない（二重投稿防止）。

投稿時刻について（iriko-post-media と同じ考え方）:
  GitHub Actionsの定時実行は数時間遅れることがあるので、cronは早めに仕掛け、
  早く起動したら TARGET_TIME まで待つ。遅れて起動したら待たずに投稿する。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pick import pick  # noqa: E402

API = "https://graph.instagram.com/v23.0"
JST = timezone(timedelta(hours=9))
DEFAULT_TIME = {"daily": "11:30", "frequent": "19:00"}
MAX_WAIT_SEC = 90 * 60

TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
USER_ID = os.environ.get("IG_USER_ID", "")
EXPECTED_USERNAME = os.environ.get("EXPECTED_USERNAME") or "tomarutto_kagawa"
SLOT = os.environ.get("SLOT", "daily")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
TARGET_TIME = os.environ.get("TARGET_TIME") or DEFAULT_TIME.get(SLOT, "11:30")
RAW_BASE = os.environ.get(
    "RAW_BASE", "https://raw.githubusercontent.com/diskiwk-star/rutto-post-media/main")

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "stories" / "manifest.json"
ROTATION = REPO_ROOT / "state" / "rotation.json"
HISTORY = REPO_ROOT / "state" / "history.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def api_post(path: str, params: dict) -> dict:
    params = {**params, "access_token": TOKEN}
    if DRY_RUN:
        safe = {k: v for k, v in params.items() if k != "access_token"}
        print(f"  [DRY RUN] POST {path} {json.dumps(safe, ensure_ascii=False)}")
        return {"id": "DRYRUN_ID"}
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}{path}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"API error {e.code} on {path}: {body}") from e


def api_get(path: str, params: dict) -> dict:
    params = {**params, "access_token": TOKEN}
    if DRY_RUN:
        return {"status_code": "FINISHED"}
    url = f"{API}{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())


def wait_until_ready(creation_id: str, timeout_sec: int = 600):
    """動画コンテナの処理完了を待つ"""
    start = time.time()
    while time.time() - start < timeout_sec:
        code = api_get(f"/{creation_id}", {"fields": "status_code"}).get("status_code")
        print(f"  container {creation_id}: {code}")
        if code == "FINISHED":
            return
        if code == "ERROR":
            raise RuntimeError(f"container {creation_id} processing ERROR")
        time.sleep(15)
    raise RuntimeError(f"container {creation_id} not ready after {timeout_sec}s")


def publish(creation_id: str) -> dict:
    """公開する。コンテナ準備中のエラーは少し待って再試行（iriko-post-mediaで実績あり）"""
    last_err = None
    for attempt in range(6):
        try:
            return api_post(f"/{USER_ID}/media_publish", {"creation_id": creation_id})
        except RuntimeError as e:
            if not any(t in str(e) for t in ("2207027", "2207006", '"code":9007')):
                raise
            last_err = e
            wait = 10 * (attempt + 1)
            print(f"  まだ公開準備中。{wait}秒待って再試行（{attempt + 1}/6）")
            time.sleep(wait)
    raise RuntimeError(f"公開の再試行が上限に達しました: {last_err}")


def post_story(url: str):
    is_video = url.lower().endswith((".mp4", ".mov"))
    params = {"media_type": "STORIES", ("video_url" if is_video else "image_url"): url}
    c = api_post(f"/{USER_ID}/media", params)
    if is_video:
        wait_until_ready(c["id"])
    print(f"  published: {publish(c['id'])}")


def check_account():
    """トークンの持ち主を確認し、USER_IDを確定する。想定と違うアカウントなら中止。"""
    global USER_ID
    url = f"{API}/me?" + urllib.parse.urlencode(
        {"fields": "user_id,username", "access_token": TOKEN})
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            me = json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: トークンでアカウント情報を取れません（失効・貼り間違い？）: "
                 f"{e.code} {e.read().decode(errors='replace')}")
    username = me.get("username")
    print(f"トークンのアカウント: @{username} (user_id={me.get('user_id')})")
    if username != EXPECTED_USERNAME:
        sys.exit(f"ERROR: 想定は @{EXPECTED_USERNAME} だが、トークンは @{username} のもの。投稿を中止。")
    if not USER_ID:
        USER_ID = str(me["user_id"])
    elif USER_ID != str(me.get("user_id")):
        sys.exit(f"ERROR: IG_USER_ID({USER_ID}) とトークンのuser_idが一致しません。")


def wait_until_target():
    now = datetime.now(JST)
    hh, mm = (int(x) for x in TARGET_TIME.split(":"))
    delta = (now.replace(hour=hh, minute=mm, second=0, microsecond=0) - now).total_seconds()
    if delta <= 0:
        print(f"目標 {TARGET_TIME} を{int(-delta // 60)}分過ぎて起動。待たずに投稿します。")
        return
    if delta > MAX_WAIT_SEC:
        print(f"目標 {TARGET_TIME} まで{int(delta // 60)}分あり、待機上限を超えるため中止。")
        sys.exit(0)
    print(f"目標 {TARGET_TIME} まで{int(delta // 60)}分{int(delta % 60)}秒待機します。")
    if not DRY_RUN:
        time.sleep(delta)


def main():
    today = os.environ.get("QUEUE_DATE") or datetime.now(JST).strftime("%Y-%m-%d")
    print(f"date(JST)={today} slot={SLOT} dry_run={DRY_RUN}")
    if DRY_RUN and TOKEN:
        check_account()  # ドライランでもトークンの持ち主は確認する（素材が無くても確認できるよう先頭で）

    history = load_json(HISTORY, [])
    if any(h["date"] == today and h["slot"] == SLOT for h in history):
        print("この枠は今日すでに投稿済み。終了。")
        return

    manifest = load_json(MANIFEST, {"pages": []})
    page, new_state = pick(manifest, load_json(ROTATION, {}), today, SLOT)
    if page is None:
        print("候補の素材がありません（manifestを確認）。終了。")
        return
    url = f"{RAW_BASE}/{page['file']}"
    print(f"選ばれた素材: p.{page.get('page_no', '?')} {page.get('facility') or '-'} "
          f"「{page.get('title') or ''}」 {page['file']}")

    if not DRY_RUN:
        if not TOKEN:
            sys.exit("ERROR: IG_ACCESS_TOKEN が未設定")
        check_account()

    wait_until_target()

    # 待機中に別の実行（予備のcron）が投稿を終えていないか、コミット済みの履歴で再確認
    if not DRY_RUN:
        os.system("git pull --quiet --rebase origin main >/dev/null 2>&1")
        history = load_json(HISTORY, [])
        if any(h["date"] == today and h["slot"] == SLOT for h in history):
            print("待機中に別の実行が投稿済み。二重投稿を回避。")
            return

    post_story(url)

    if DRY_RUN:
        print("[DRY RUN] state/history は更新しません")
        return
    save_json(ROTATION, new_state)
    history.append({
        "date": today, "slot": SLOT, "id": page["id"], "page_no": page.get("page_no"),
        "facility": page.get("facility"), "posted_at": datetime.now(JST).isoformat(),
    })
    save_json(HISTORY, history[-400:])
    print("state/history を更新しました")


if __name__ == "__main__":
    main()
