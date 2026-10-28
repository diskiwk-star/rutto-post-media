#!/usr/bin/env python3
"""
書き出したMP4の音のズレを調べる（ローカル用。ffmpeg と numpy が必要）。

  python3 scripts/audio_check.py compare 新.mp4 基準.mp4
      2つの動画の音を重ね合わせ、「新が基準より何秒ずれているか」と「どれだけ同じ音か」を出す。
      基準＝音が正しい状態で書き出した（投稿した）ファイル。stories/media/ のファイルがそのまま使える。
      ずれ ±0.05秒以内かつ類似度0.9以上なら「同じ」。

  python3 scripts/audio_check.py inspect ファイル.mp4
      基準が無いときの簡易チェック。音が入っているか、頭が無音か、曲の途中から始まっていないか、
      動画の長さと音の長さが合っているかを見る。

どちらも終了コードは、問題なし=0 / 要確認=1。
"""

import subprocess
import sys

import numpy as np

SR = 8000  # 解析用のサンプリング周波数


def load(path: str):
    """動画から音声（モノラル8kHz）と動画・音声の長さを取り出す"""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(SR),
         "-f", "f32le", "-"], capture_output=True, check=True).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
         "-of", "csv=p=0", path], capture_output=True, text=True, check=True).stdout
    dur = {}
    for line in probe.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and parts[0] in ("video", "audio"):
            try:
                dur[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return audio, dur


def envelope(a, hop=80):
    """10ミリ秒ごとの音の大きさ。ずれの検出はこちらで行う（細かい波形より安定）"""
    n = len(a) // hop
    return np.sqrt((a[: n * hop].reshape(n, hop) ** 2).mean(axis=1) + 1e-12)


def gcc_phat(x, y):
    """波形そのものを位相だけで重ね合わせる（GCC-PHAT）。同じ曲が時間だけずれているなら、
    ずれの位置に鋭い山ができる。戻り値: (lag秒 正＝xがyより遅れている, 信頼度＝山の高さ/ふつうの高さ)"""
    n = len(x) + len(y)
    size = 1 << (n - 1).bit_length()
    X, Y = np.fft.rfft(x, size), np.fft.rfft(y, size)
    R = X * np.conj(Y)
    R /= np.abs(R) + 1e-9
    cc = np.fft.irfft(R, size)
    cc = np.concatenate([cc[-(len(y) - 1):], cc[:len(x)]])  # lag = -(len(y)-1) .. len(x)-1
    mag = np.abs(cc)
    i = int(np.argmax(mag))
    lag = i - (len(y) - 1)
    conf = float(mag[i] / (np.median(mag) + 1e-9))
    return lag / SR, conf


def compare(new_path: str, ref_path: str) -> int:
    new, nd = load(new_path)
    ref, rd = load(ref_path)
    if len(new) == 0 or len(ref) == 0:
        print("音が入っていません（片方が無音）。要確認")
        return 1
    lag_s, conf = gcc_phat(new, ref)
    print(f"長さ（動画）新 {nd.get('video', 0):.2f}秒 / 基準 {rd.get('video', 0):.2f}秒")
    print(f"長さ（音）  新 {len(new) / SR:.2f}秒 / 基準 {len(ref) / SR:.2f}秒")
    same_track = conf >= 60
    if same_track:
        print(f"ずれ: 新の音は基準より {lag_s:+.3f}秒（＋は遅れ、−は早まり）　一致の確かさ: {conf:.0f}")
    else:
        print(f"同じ曲の一致が見つかりません（確かさ {conf:.0f}）。別の曲、または曲の別の部分の可能性")
    same_len = abs(len(new) - len(ref)) / SR <= 0.3
    if not same_len:
        print("・長さが違います（ページの長さが変わっている）")
    ok = same_track and abs(lag_s) <= 0.05 and same_len
    print("判定: " + ("同じ（ずれなし）" if ok else "ずれている／別の音の可能性。要確認"))
    return 0 if ok else 1


def inspect(path: str) -> int:
    a, dur = load(path)
    problems = []
    if len(a) == 0:
        print("音が入っていません。要確認")
        return 1
    secs = len(a) / SR
    env = envelope(a)
    peak = float(env.max())
    head = env[:30]   # 最初の0.3秒
    tail = env[-30:]  # 最後の0.3秒
    lead_silence = 0.0
    for v in env:
        if v > peak * 0.05:
            break
        lead_silence += 0.01
    print(f"動画 {dur.get('video', 0):.2f}秒 / 音 {secs:.2f}秒 / 先頭の無音 {lead_silence:.2f}秒")
    if abs(dur.get("video", secs) - secs) > 0.3:
        problems.append("動画と音の長さが合っていません")
    if lead_silence > 0.5:
        problems.append(f"音の始まりが{lead_silence:.1f}秒遅れています")
    if head.mean() > peak * 0.5:
        problems.append("冒頭からいきなり大きな音です（曲の途中から始まっている可能性）")
    if tail.mean() > peak * 0.5:
        problems.append("終わりが途切れています（曲が途中で切れている可能性）")
    for pb in problems:
        print("・" + pb)
    print("判定: " + ("問題なし" if not problems else "要確認"))
    return 0 if not problems else 1


def fix(new_path: str, ref_path: str, out_path: str) -> int:
    _, nd = load(new_path)
    _, rd = load(ref_path)
    nv, rv = nd.get("video", 0), rd.get("video", 0)
    if abs(nv - rv) > 0.3:
        print(f"映像の長さが違うため直せません（新 {nv:.2f}秒 / 基準 {rv:.2f}秒）。ページの長さが変わっています")
        return 2
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", new_path, "-i", ref_path,
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
         "-t", f"{nv:.3f}", "-movflags", "+faststart", out_path], check=True)
    print(f"作成: {out_path}（映像＝新しい書き出し、音＝基準）")
    print("--- 作ったファイルの確認 ---")
    return compare(out_path, ref_path)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "compare":
        sys.exit(compare(sys.argv[2], sys.argv[3]))
    if len(sys.argv) == 5 and sys.argv[1] == "fix":
        sys.exit(fix(sys.argv[2], sys.argv[3], sys.argv[4]))
    if len(sys.argv) == 3 and sys.argv[1] == "inspect":
        sys.exit(inspect(sys.argv[2]))
    sys.exit(__doc__)
