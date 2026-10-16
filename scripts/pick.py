#!/usr/bin/env python3
"""
泊Rutto ストーリーズ - 今日投稿する素材を選ぶ（選曲ロジックだけを持つ。APIは呼ばない）

枠:
  daily    (11:30) : 施設ローテーション。施設の順番はランダムで、全施設を1周したら
                     並べ直す。施設の中の素材もランダムで、全部出し切るまで同じものは出ない。
  frequent (19:00) : Canvaのページ名に「頻出」を含むページだけからランダム。
                     全部出し切るまで同じものは出ない。

素材の追加・削除への強さ:
  「残り」をリストで持たず「この周で使った物」を集合で持つ。
  残り = 現在の素材一覧 − 使った物 なので、追加された素材は次の抽選から自然に候補に入り、
  削除された素材は自然に候補から消える。

同じ日に同じ枠を再実行しても同じ結果になるよう、乱数の種は「日付＋枠」で固定する。
"""

import random

SLOTS = ("daily", "frequent")


def _rng(date: str, slot: str, salt: str = "") -> random.Random:
    return random.Random(f"rutto-story/{date}/{slot}/{salt}")


def _draw(pool, used, last, rng):
    """pool から used に無いものを1つ引く。出し切っていたら周をリセットする。
    リセット直後は直前に出した last を避ける（周の切れ目で同じ物が続かないように）。
    戻り値: (選んだ物, 新しいused)"""
    pool = sorted(pool)
    remaining = [p for p in pool if p not in used]
    if not remaining:
        used = set()
        remaining = [p for p in pool if p != last] or pool
    choice = rng.choice(remaining)
    return choice, set(used) | {choice}


def active_pages(manifest: dict):
    return [p for p in manifest.get("pages", []) if p.get("active", True)]


def pick(manifest: dict, state: dict, date: str, slot: str):
    """素材を1つ選び、(page, 新しいstate) を返す。候補が無ければ (None, state)。
    state は書き換えずにコピーを返す。"""
    if slot not in SLOTS:
        raise ValueError(f"unknown slot: {slot}")
    state = {
        "daily": dict(state.get("daily", {})),
        "frequent": dict(state.get("frequent", {})),
    }
    pages = active_pages(manifest)

    if slot == "frequent":
        pool = {p["id"]: p for p in pages if p.get("frequent")}
        if not pool:
            return None, state
        st = state["frequent"]
        rng = _rng(date, slot)
        pid, used = _draw(pool.keys(), set(st.get("used", [])), st.get("last"), rng)
        st.update(used=sorted(used), last=pid)
        return pool[pid], state

    # daily: 頻出ページは19:00枠で毎日のように出るので、11:30枠からは外す
    by_fac = {}
    for p in pages:
        if p.get("frequent") or not p.get("facility"):
            continue
        by_fac.setdefault(p["facility"], {})[p["id"]] = p
    if not by_fac:
        return None, state

    st = state["daily"]
    fac, fac_used = _draw(by_fac.keys(), set(st.get("facilities_used", [])),
                          st.get("last_facility"), _rng(date, slot, "facility"))
    per_fac = {k: list(v) for k, v in st.get("pages_used", {}).items()}
    last_page = st.get("last_page_by_facility", {}).get(fac)
    pid, pages_used = _draw(by_fac[fac].keys(), set(per_fac.get(fac, [])),
                            last_page, _rng(date, slot, fac))
    per_fac[fac] = sorted(pages_used)
    last_by_fac = dict(st.get("last_page_by_facility", {}))
    last_by_fac[fac] = pid
    st.update(facilities_used=sorted(fac_used), last_facility=fac,
              pages_used=per_fac, last_page_by_facility=last_by_fac)
    return by_fac[fac][pid], state
