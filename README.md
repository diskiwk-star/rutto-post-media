# rutto-post-media

泊RuttoのInstagramストーリーズを毎日自動投稿するための配信用リポジトリ（公開）。
GitHub ActionsがInstagram Graph API（Instagram Login API）で投稿し、画像・動画のURLには
このリポジトリのraw URLを使う。仕組みは `iriko-post-media` と同じ。

## 投稿枠

| 枠 | 時刻(JST) | 選び方 | ワークフロー |
|---|---|---|---|
| daily | 11:30 | 施設ローテーション。施設の順番はランダムで、全施設を1周したら並べ直す。施設内の素材もランダムで、出し切るまで重複なし | `story-daily.yml` |
| frequent | 19:00 | ページ名に「頻出」を含むページからランダム。出し切るまで重複なし | `story-frequent.yml` |

- 頻出ページは11:30枠には出さない（同じ日に2回出るのを防ぐ）
- 同じ日・同じ枠の再実行は同じ素材を選ぶ（乱数の種＝日付＋枠）。投稿済みかどうかは `state/history.json` で判定

## 指定して出したいとき

- **今すぐ1枚出す**: Actions →「story-manual」→ Run workflow → ページ番号（例 `6`）を入れて実行。ローテは進めない
- **日付と枠を指定して出す**: `stories/overrides.json` に `{"2026-10-16": {"frequent": "ページID"}}` の形で書く。その日その枠は指定ページを出し、ローテは進めない
- **自動ローテの一時停止**: `stories/manifest.json` の `"paused": true`。停止中も上の2つは動く。全ページの書き出しがそろったら `false` にする（sync_manifest.py は設定を引き継ぐ）

## 素材の元: Canva「ストーリーズ投稿用」

- design_id: `DAHX4vXPj-4`（1080×1920）
- **ページ名（ページタイトル）で振り分ける**
  - 施設名を入れる → 11:30枠のその施設の素材（例: `ZERO BASE`、`三豊鶴`、`COTON`）
  - `頻出` を入れる → 19:00枠
  - どちらも無いページ・空白ページは投稿しない
  - 積凪は `MOKURASU` / `KITAKEN` まで書く（「積凪」だけだと判定しない）
- 施設名の表記ゆれの許容範囲は `scripts/sync_manifest.py` の `FACILITIES`

## 素材を追加・削除したとき（クロさんの作業）

1. Canvaコネクタの `read-design`（トランザクションを開いて `design_content`）で全ページのID・ページ名・動画/静止画/空白を取得し、`canva_pages.json` を作る（読むだけなので最後に `cancel` で閉じる）
2. 新しいページ・中身が変わったページを書き出して `stories/media/{ページID}.mp4|.png` に保存
   - 基本は全ページ `export-design` の `mp4`（`quality: vertical_1080p`）。音源を付ける運用なので静止画ページもMP4にする
   - 音楽なしの静止画ページだけ `png` でもよい
   - **ユーザーがテキスト・音源を入れ終わってから書き出す**（途中で書き出すとやり直しになる）
   - 書き出しURLはすぐ失効するので受け取ったらすぐ `curl`
3. `python3 scripts/sync_manifest.py canva_pages.json` → 未分類・書き出し漏れの一覧を確認
4. `DRY_RUN=1 SLOT=daily TARGET_TIME=00:00 python3 scripts/publish_story.py` で選ばれ方を確認
5. commit & push（`canva_pages.json` はコミットしない）

## 設定（GitHub）

- Secret `IG_ACCESS_TOKEN`: 泊RuttoのIGアカウントのアクセストークン（約60日で失効。値はユーザーが自分で貼る）
- Variable `IG_USER_ID`: 任意。空ならトークンから自動取得する
- 投稿前にトークンの持ち主が `@tomarutto_kagawa` か確認し、違えば中止する（いりこ等のトークンの貼り間違い対策）
- トークンの確認だけしたいときは、Actionsから手動実行（dry_run=ON）。ログに「トークンのアカウント: @tomarutto_kagawa」と出ればOK

## 制約

- APIで投稿したストーリーズには、リンク・場所・メンション・音楽スタンプを付けられない。必要なら画像・動画に焼き込む
- 音楽はCanva側でページに追加してからMP4で書き出す（Canvaコネクタからは音楽を追加できない）
- 動画ストーリーズは60秒まで
