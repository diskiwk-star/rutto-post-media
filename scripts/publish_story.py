#!/usr/bin/env python3
"""
泊Rutto ストーリーズ自動投稿（GitHub Actions実行用）

環境変数:
  IG_ACCESS_TOKEN : Instagram Login APIのアクセストークン（Actions Secret）
  IG_USER_ID      : 泊RuttoのIGユーザーID（Actions Variable・任意）。空ならトークンから取得する
  EXPECTED_USERNAME : 投稿先として想定するIGユーザー名。トークンが別アカウントのものなら投稿しない
                    （いりこ等のトークンを貼り間違えたときの安全装置）。既定は tomarutto_kagawa
  SLOT            : "am"（10:00） / "pm"（19:00） / "manual"（PAGEで指定したページを即時投稿。ローテは進めない）
                    午前と午後のどちらに「施設ローテ(daily)」「頻出(frequent)」を出すかは、日ごとにランダムで入れ替える
                    （同じ時間帯の人に毎日同じ種類が届かないようにするため。日付で固定。kind_for 参照）
  PAGE            : manual用。ページ番号（例 "6"）またはページID
  TARGET_TIME     : 投稿目標時刻（JST "HH:MM"）の上書き。空なら am=10:00 / pm=19:00
  DRY_RUN         : "1" なら投稿せず、選ばれた素材とAPIリクエストを表示のみ
  QUEUE_DATE      : 日付の上書き（"YYYY-MM-DD"）。テスト用
  RAW_BASE        : 素材URLの先頭。既定は本リポジトリのmainのraw URL

動作:
  stories/manifest.json（素材一覧）と state/rotation.json（ローテの進み具合）から
  scripts/pick.py で今日の素材を選び、STORIESとして投稿する。
  stories/overrides.json に {"YYYY-MM-DD": {"frequent": "PBxxx"}} があれば、その日その種類
  （daily=施設 / frequent=頻出）は指定ページを出す（ローテは進めない）。午前・午後のどちらに出るかは
  日ごとのランダム。固定したいときは {"am": "daily"} のように午前に出す種類を書く。
  manifestの "paused": true の間は指定分だけ投稿する。
  成功したら state/rotation.json と state/history.json を更新（ワークフロー側でコミット）。
  history に同じ日付・時間帯(am/pm)があれば投稿済みとして何もしない（二重投稿防止）。

投稿時刻について（iriko-post-media と同じ考え方）:
  GitHub Actionsの定時実行は数時間遅れることがあるので、cronは早めに仕掛け、
  早く起動したら TARGET_TIME まで待つ。遅れて起動したら待たずに投稿する。
"""

import json
import os
import random
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
# 投稿時刻は固定（午前10:00・午後19:00）。manualは即時。
SLOT_TIME = {"am": "10:00", "pm": "19:00", "manual": "00:00"}
MAX_WAIT_SEC = 90 * 60  # 起動が目標の90分より前なら何もせず終わる（早朝・昼過ぎから複数本cronを仕掛けてある）

TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
USER_ID = os.environ.get("IG_USER_ID", "")
PAGE = os.environ.get("PAGE", "").strip()
EXPECTED_USERNAME = os.environ.get("EXPECTED_USERNAME") or "tomarutto_kagawa"
SLOT = os.environ.get("SLOT", "am")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
TARGET_TIME_OVERRIDE = os.environ.get("TARGET_TIME", "").strip()  # 空なら既定（am=10:00 / pm=19:00）
RAW_BASE = os.environ.get(
    "RAW_BASE", "https://raw.githubusercontent.com/diskiwk-star/rutto-post-media/main")

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "stories" / "manifest.json"
ROTATION = REPO_ROOT / "state" / "rotation.json"
HISTORY = REPO_ROOT / "state" / "history.json"
OVERRIDES = REPO_ROOT / "stories" / "overrides.json"
# クロさんがpushする「今すぐ出して」の指示。{"page": "6", "request": "一意な文字列"}。
# 投稿したら {} に戻す。request は履歴に残し、同じ指示を2回投稿しない。
POST_NOW = REPO_ROOT / "stories" / "post_now.json"


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


def find_page(manifest: dict, key: str) -> dict:
    """ページ番号（"6"）またはページID（"PBxxx"）で素材を探す。書き出しファイルが必須。"""
    pages = manifest.get("pages", [])
    hits = [p for p in pages if not p.get("removed_from_canva")
            and (p["id"] == key or str(p.get("page_no")) == key)]
    if not hits:
        sys.exit(f"ERROR: ページ {key} がmanifestにありません")
    page = hits[0]
    if not page.get("file"):
        sys.exit(f"ERROR: ページ {key} は書き出しファイルがありません")
    return page


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


def target_time_for() -> str:
    """この時間帯の投稿目標時刻 "HH:MM"。TARGET_TIME指定があればそれを使う。"""
    return TARGET_TIME_OVERRIDE or SLOT_TIME.get(SLOT, "00:00")


ORDER_ANCHOR = "2026-10-01"  # 入れ替え順の計算の起点（変えると過去の並びも変わるので触らない）


def am_kind(date: str, overrides: dict) -> str:
    """その日、午前に出す種類（"daily"=施設ローテ / "frequent"=頻出）。
    日付ごとのランダム。ただし同じ並びが3日続いたら4日目は入れ替える。
    overrides[date]["am"] があればそれを優先する（固定したい日用）。"""
    fixed = overrides.get(date, {}).get("am")
    if fixed in ("daily", "frequent"):
        return fixed
    d = datetime.strptime(ORDER_ANCHOR, "%Y-%m-%d").date()
    target = datetime.strptime(date, "%Y-%m-%d").date()
    hist = []
    while d <= target:
        k = random.Random(f"rutto-story-order/{d.isoformat()}").choice(["daily", "frequent"])
        if len(hist) >= 3 and hist[-1] == hist[-2] == hist[-3] == k:
            k = "frequent" if k == "daily" else "daily"
        hist.append(k)
        d += timedelta(days=1)
    return hist[-1]


def kind_for(date: str, overrides: dict) -> str:
    """この時間帯(SLOT)に出す種類。manualは使わない。"""
    am = am_kind(date, overrides)
    if SLOT == "am":
        return am
    return "frequent" if am == "daily" else "daily"


def wait_until_target(target_time: str):
    TARGET_TIME = target_time
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
    if SLOT != "manual" and any(h["date"] == today and h["slot"] == SLOT for h in history):
        print("この時間帯は今日すでに投稿済み。終了。")
        return
    overrides = load_json(OVERRIDES, {})
    kind = None if SLOT == "manual" else kind_for(today, overrides)
    if kind:
        print(f"今日の{ '午前' if SLOT == 'am' else '午後' }の枠: {'施設ローテ' if kind == 'daily' else '頻出'}")

    manifest = load_json(MANIFEST, {"pages": []})
    rotation = load_json(ROTATION, {})
    new_state = rotation  # 指定投稿ではローテを進めない
    request = None
    if SLOT == "manual" and not PAGE:
        # Run workflowのページ入力が無い＝post_now.json のpushで起動した
        now_req = load_json(POST_NOW, {})
        if not now_req.get("page"):
            print("post_now.json に指示がありません。終了。")
            return
        request = now_req.get("request") or now_req["page"]
        if any(h.get("request") == request for h in history):
            print(f"指示 {request} は投稿済み。終了。")
            return
    forced = (PAGE or (now_req["page"] if request else None)) if SLOT == "manual" \
        else overrides.get(today, {}).get(kind)
    if SLOT == "manual" and not forced:
        sys.exit("ERROR: 手動投稿にはページ（番号かID）の指定が必要")
    if forced:
        page = find_page(manifest, forced)
        print(f"指定投稿: {forced}")
    elif manifest.get("paused"):
        print("自動ローテは一時停止中（manifestのpaused）。指定投稿も無いので終了。")
        return
    else:
        page, new_state = pick(manifest, rotation, today, kind)
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

    target = target_time_for()
    print(f"投稿目標時刻: {target}")
    wait_until_target(target)

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
        "date": today, "slot": SLOT, **({"kind": kind} if kind else {}), "id": page["id"], "page_no": page.get("page_no"),
        "facility": page.get("facility"), "posted_at": datetime.now(JST).isoformat(),
        **({"request": request} if request else {}),
    })
    if request:
        save_json(POST_NOW, {})
    save_json(HISTORY, history[-400:])
    print("state/history を更新しました")


if __name__ == "__main__":
    main()
