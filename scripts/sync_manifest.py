#!/usr/bin/env python3
"""
Canvaの「ストーリーズ投稿用」デザインの現状から stories/manifest.json を作り直す（ローカル用）。

使い方:
  python3 scripts/sync_manifest.py canva_pages.json

canva_pages.json はクロさんがCanvaコネクタ（read-design）の結果から作る一覧:
  [{"id": "PBxxxx", "page_no": 1, "title": "頻出 三豊鶴", "kind": "video"}, ...]
  kind は video / image / empty（空白ページ）

ページ名（Canvaのページタイトル）の付け方:
  - 施設名を入れる → その施設の素材として 11:30枠（施設ローテ）に入る
  - 「頻出」を入れる → 19:00枠（頻出ページ）に入る。11:30枠からは外れる
  - 両方入れてよい（例: 「頻出 三豊鶴」）
  - 「積凪」だけ（MOKURASU/KITAKENの区別なし）→「積凪（共通）」という1施設として扱う
  - どちらも無いページ・空白ページは投稿しない（未分類として一覧に出す）

素材ファイル: stories/media/{ページID}.mp4（動画・音楽付き）または .png（静止画）。
ファイルが無いページは投稿候補に入れない（書き出し漏れとして一覧に出す）。
Canvaから消えたページは active=false にして候補から外す（履歴との対応のため行は残す）。
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = REPO_ROOT / "stories" / "manifest.json"
MEDIA_DIR = REPO_ROOT / "stories" / "media"

# 正式名 → ページ名に書かれていたら一致とみなす語（大文字小文字は無視）
FACILITIES = {
    "Archipelago TSUSHIMA": ["archipelago", "tsushima", "津島"],
    "MINO stylish house": ["mino"],
    "TABI-YADO sorato": ["sorato", "そらと"],
    "SETOUCHI SHOWER": ["setouchi shower", "shower", "シャワー"],
    "積凪MOKURASU棟": ["mokurasu", "モクラス"],
    "高瀬茶の湯ゲストハウスJIJI": ["jiji", "茶の湯"],
    "COTON": ["coton", "コトン"],
    "ARIAKE palm tree house": ["ariake", "有明"],
    "ZERO BASE villa": ["zero base", "zerobase", "ゼロベース"],
    "積凪KITAKEN棟": ["kitaken"],
    "箱家": ["箱家", "hakoya"],
    "三豊鶴TOJI": ["三豊鶴", "toji"],
}
FREQUENT_WORD = "頻出"
SEKINAGI_COMMON = "積凪（共通）"


def parse_title(title: str):
    t = (title or "").lower()
    hits = [name for name, words in FACILITIES.items() if any(w.lower() in t for w in words)]
    # 「積凪」だけでMOKURASU/KITAKENの区別が無いページ（敷地の海・プール等）は共通枠として1施設扱い
    if not hits and "積凪" in (title or ""):
        hits = [SEKINAGI_COMMON]
    return hits, FREQUENT_WORD in (title or "")


def media_file(page_id: str):
    for ext in (".mp4", ".png", ".jpg"):
        if (MEDIA_DIR / f"{page_id}{ext}").exists():
            return f"stories/media/{page_id}{ext}"
    return None


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    canva = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    old_manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    old = {p["id"]: p for p in old_manifest.get("pages", [])}

    pages, unclassified, missing, ambiguous = [], [], [], []
    seen = set()
    for c in canva:
        seen.add(c["id"])
        facs, frequent = parse_title(c.get("title"))
        if len(facs) > 1:
            ambiguous.append((c, facs))
        facility = facs[0] if len(facs) == 1 else None
        f = media_file(c["id"])
        usable = c.get("kind") != "empty" and (facility or frequent)
        if c.get("kind") != "empty" and not (facility or frequent):
            unclassified.append(c)
        if usable and not f:
            missing.append(c)
        pages.append({
            "id": c["id"],
            "page_no": c["page_no"],
            "title": c.get("title") or "",
            "facility": facility,
            "frequent": frequent,
            "file": f,
            "active": bool(usable and f),
        })
    removed = [p for pid, p in old.items() if pid not in seen]
    for p in removed:
        pages.append({**p, "active": False, "removed_from_canva": True})

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    out = {"paused": True} if old_manifest.get("paused") else {}  # 一時停止の設定は引き継ぐ
    out["pages"] = pages
    MANIFEST.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    act = [p for p in pages if p["active"]]
    print(f"投稿候補: {len(act)}件（11:30枠 {sum(1 for p in act if p['facility'] and not p['frequent'])}件"
          f" / 19:00頻出 {sum(1 for p in act if p['frequent'])}件）")
    by_fac = {}
    for p in act:
        if p["facility"] and not p["frequent"]:
            by_fac[p["facility"]] = by_fac.get(p["facility"], 0) + 1
    for name in list(FACILITIES) + [SEKINAGI_COMMON]:
        print(f"  {name}: {by_fac.get(name, 0)}件")
    if unclassified:
        print("未分類（ページ名に施設名も「頻出」も無い）: p." +
              ", ".join(str(c["page_no"]) for c in unclassified))
    if ambiguous:
        for c, facs in ambiguous:
            print(f"施設名が複数一致して判定不可: p.{c['page_no']}「{c.get('title')}」→ {facs}")
    if missing:
        print("書き出しファイルが無い: p." + ", ".join(str(c["page_no"]) for c in missing))
    if removed:
        print("Canvaから消えたので候補から外した: " + ", ".join(p["id"] for p in removed))


if __name__ == "__main__":
    main()
