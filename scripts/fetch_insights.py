#!/usr/bin/env python3
"""
泊Rutto フィード・リールのインサイト取得（GitHub Actions実行用）

環境変数:
  IG_ACCESS_TOKEN   : Instagram Login APIのアクセストークン（Actions Secret）
  EXPECTED_USERNAME : 想定するIGユーザー名。違えば何もしない。既定は tomarutto_kagawa
  DAYS              : さかのぼる日数。既定 60
  CHECK_ONLY        : "1" なら最新1件でインサイト権限を確かめるだけ（CSVは作らない）
  OUT               : CSVの出力先。既定 insights.csv

このリポジトリは公開なので、ログ（Actionsの実行画面）には数値もキャプションも出さない。
出すのは件数と成否だけ。データはCSVにだけ書き、ワークフロー側で保持1日のアーティファクトにする。
"""

import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://graph.instagram.com/v23.0"
JST = timezone(timedelta(hours=9))

TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
EXPECTED_USERNAME = os.environ.get("EXPECTED_USERNAME") or "tomarutto_kagawa"
DAYS = int(os.environ.get("DAYS") or 60)
CHECK_ONLY = os.environ.get("CHECK_ONLY", "") == "1"
OUT = os.environ.get("OUT") or "insights.csv"

COMMON = ["reach", "views", "likes", "comments", "saved", "shares", "total_interactions"]
METRICS = {
    "FEED": COMMON + ["profile_visits", "follows"],
    "REELS": COMMON + ["ig_reels_avg_watch_time", "ig_reels_video_view_total_time"],
}
ALL_METRICS = list(dict.fromkeys(METRICS["FEED"] + METRICS["REELS"]))


class ApiError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def api_get(path_or_url: str, params: dict | None = None) -> dict:
    if path_or_url.startswith("http"):
        url = path_or_url
    else:
        q = dict(params or {})
        q["access_token"] = TOKEN
        url = f"{API}{path_or_url}?" + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e).get("error", {})
        except Exception:
            err = {}
        raise ApiError(err.get("code"), err.get("message") or f"http {e.code}") from None


def fetch_insights(media_id: str, metrics: list[str]) -> dict:
    """まとめて取り、ダメなら1指標ずつ取る（投稿の種類で使えない指標があるため）。"""
    def parse(d):
        out = {}
        for item in d.get("data", []):
            vals = item.get("values") or [{}]
            out[item["name"]] = item.get("total_value", {}).get("value", vals[0].get("value"))
        return out

    try:
        return parse(api_get(f"/{media_id}/insights", {"metric": ",".join(metrics)}))
    except ApiError:
        pass
    out = {}
    for m in metrics:
        try:
            out.update(parse(api_get(f"/{media_id}/insights", {"metric": m})))
        except ApiError:
            pass
    return out


def main() -> int:
    if not TOKEN:
        print("::error::Secret IG_ACCESS_TOKEN が空です")
        return 1

    me = api_get("/me", {"fields": "user_id,username"})
    if me.get("username") != EXPECTED_USERNAME:
        print(f"::error::トークンのアカウントが @{EXPECTED_USERNAME} ではありません")
        return 1
    print(f"アカウント確認: @{me['username']}")

    if CHECK_ONLY:
        d = api_get("/me/media", {"fields": "id,media_product_type", "limit": 1})
        items = d.get("data", [])
        if not items:
            print("::error::投稿が1件も取れません")
            return 1
        try:
            api_get(f"/{items[0]['id']}/insights", {"metric": "reach"})
            print("インサイト権限: OK")
            return 0
        except ApiError as e:
            print(f"::error::インサイト権限: NG code={e.code} msg={e.message}")
            return 1

    since = datetime.now(JST) - timedelta(days=DAYS)
    fields = ("id,caption,media_type,media_product_type,timestamp,permalink,"
              "like_count,comments_count")
    page = api_get("/me/media", {"fields": fields, "limit": 50})
    media = []
    while True:
        stop = False
        for m in page.get("data", []):
            ts = datetime.strptime(m["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(JST)
            if ts < since:
                stop = True
                break
            m["_ts"] = ts
            media.append(m)
        nxt = page.get("paging", {}).get("next")
        if stop or not nxt:
            break
        page = api_get(nxt)

    rows = []
    for m in media:
        kind = m.get("media_product_type", "FEED")
        ins = fetch_insights(m["id"], METRICS.get(kind, COMMON))
        head = (m.get("caption") or "").strip().split("\n")[0][:60]
        rows.append({
            "id": m["id"],
            "date_jst": m["_ts"].strftime("%Y-%m-%d"),
            "time_jst": m["_ts"].strftime("%H:%M"),
            "weekday": "月火水木金土日"[m["_ts"].weekday()],
            "product_type": kind,
            "media_type": m.get("media_type", ""),
            "caption_head": head,
            "permalink": m.get("permalink", ""),
            "like_count": m.get("like_count", ""),
            "comments_count": m.get("comments_count", ""),
            **{k: ins.get(k, "") for k in ALL_METRICS},
        })

    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["id"])
        w.writeheader()
        w.writerows(rows)

    kinds = {}
    for r in rows:
        kinds[r["product_type"]] = kinds.get(r["product_type"], 0) + 1
    missing = sum(1 for r in rows if r["reach"] == "")
    print(f"取得: {len(rows)}件（直近{DAYS}日） 内訳 {kinds}  インサイト欠け {missing}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
