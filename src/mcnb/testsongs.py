"""テスト用の小さな MIDI を生成する。

いきなりフルサイズの曲を通さない。何が壊れているのか切り分けられるよう、
確かめたい性質を1つずつ持った短い曲を用意する。

    uv run python -m mcnb.testsongs --out tests/inputs

| # | テスト | 確かめること |
|---|---|---|
| 1 | 単音       | ピッチ・楽器選択・tick 整合 |
| 2 | 2音の和音  | 同時発音、strum の要否 |
| 3 | 3音の和音  | voicing 選択、濁りの発生点 |
| 4 | メロ+伴奏  | 主旋律と伴奏の音量差（距離配置） |
| 5 | ドラム     | 打楽器のマッピング |
| 6 | 強弱       | 距離による velocity 制御の精度 |
| 7 | 短い残響   | 減衰と再発音 |
| 8 | 複数楽器   | 音色の重ね |
| 9 | 高速フレーズ | 20Hz 量子化の限界 |
| 10| 音域       | 6オクターブの楽器切り替え |
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

TICKS_PER_BEAT = 480
DRUM_CHANNEL = 9


@dataclass(frozen=True)
class Hit:
    """拍単位で指定する発音。"""

    beat: float
    duration: float
    notes: tuple[int, ...]
    velocity: int = 96
    channel: int = 0


def write_midi(path: Path, hits: list[Hit], bpm: float = 120, programs: dict[int, int] | None = None) -> None:
    """絶対時刻のイベント列から MIDI を書く。"""
    import mido
    from mido import Message, MetaMessage, MidiFile, MidiTrack

    mf = MidiFile(ticks_per_beat=TICKS_PER_BEAT)
    track = MidiTrack()
    mf.tracks.append(track)

    events: list[tuple[int, int, Message]] = []  # (絶対tick, 優先度, msg)
    for channel, program in (programs or {0: 0}).items():
        events.append((0, 0, Message("program_change", channel=channel, program=program)))

    for hit in hits:
        on = int(round(hit.beat * TICKS_PER_BEAT))
        off = int(round((hit.beat + hit.duration) * TICKS_PER_BEAT))
        for note in hit.notes:
            # note_off を先に処理して、同音の連打が消えないようにする
            events.append((off, 0, Message("note_off", channel=hit.channel, note=note, velocity=0)))
            events.append((on, 1, Message("note_on", channel=hit.channel, note=note, velocity=hit.velocity)))

    events.sort(key=lambda e: (e[0], e[1]))

    track.append(MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    prev = 0
    for absolute, _, msg in events:
        msg.time = absolute - prev
        prev = absolute
        track.append(msg)

    path.parent.mkdir(parents=True, exist_ok=True)
    mf.save(str(path))


# --------------------------------------------------------------------------- #
# テスト曲
# --------------------------------------------------------------------------- #

C4 = 60
MAJOR = [0, 2, 4, 5, 7, 9, 11, 12]


def t01_single() -> tuple[list[Hit], dict]:
    return [Hit(i * 0.5, 0.4, (C4,)) for i in range(8)], {}


def t02_dyad() -> tuple[list[Hit], dict]:
    return [Hit(i * 0.5, 0.4, (C4, C4 + 7)) for i in range(8)], {}


def t03_triad() -> tuple[list[Hit], dict]:
    chords = [(0, 4, 7), (2, 5, 9), (4, 7, 11), (5, 9, 12)]
    return [Hit(i * 1.0, 0.9, tuple(C4 + n for n in c)) for i, c in enumerate(chords * 2)], {}


def t04_melody_accomp() -> tuple[list[Hit], dict]:
    hits = [Hit(i * 0.5, 0.45, (C4 + n,), 110) for i, n in enumerate(MAJOR)]
    hits += [Hit(i * 1.0, 0.9, (C4 - 12, C4 - 8, C4 - 5), 55) for i in range(4)]
    return hits, {}


def t05_drums() -> tuple[list[Hit], dict]:
    hits: list[Hit] = []
    for bar in range(4):
        b = bar * 2
        hits.append(Hit(b + 0.0, 0.1, (36,), 110, DRUM_CHANNEL))  # kick
        hits.append(Hit(b + 1.0, 0.1, (38,), 100, DRUM_CHANNEL))  # snare
        for i in range(8):
            hits.append(Hit(b + i * 0.25, 0.1, (42,), 70, DRUM_CHANNEL))  # hat
    return hits, {}


def t06_dynamics() -> tuple[list[Hit], dict]:
    """クレッシェンド → デクレッシェンド。距離による音量制御の精度を見る。"""
    hits = []
    steps = 16
    for i in range(steps):
        v = 20 + int(107 * (i / (steps - 1)))
        hits.append(Hit(i * 0.25, 0.2, (C4,), v))
    for i in range(steps):
        v = 127 - int(107 * (i / (steps - 1)))
        hits.append(Hit(4 + i * 0.25, 0.2, (C4,), v))
    return hits, {}


def t07_decay() -> tuple[list[Hit], dict]:
    """長い音。1発では持たないので再発音が要る。"""
    return [Hit(i * 2.0, 1.9, (C4, C4 + 4, C4 + 7), 100) for i in range(4)], {}


def t08_multi_instrument() -> tuple[list[Hit], dict]:
    hits = []
    for i, n in enumerate(MAJOR):
        hits.append(Hit(i * 0.5, 0.4, (C4 + n,), 100, 0))
        hits.append(Hit(i * 0.5, 0.4, (C4 + n - 24,), 90, 1))
        hits.append(Hit(i * 0.5, 0.4, (C4 + n + 12,), 70, 2))
    return hits, {0: 0, 1: 33, 2: 11}  # piano / bass / vibraphone


def t09_fast() -> tuple[list[Hit], dict]:
    """32分音符 @ 180BPM = 41ms。20Hz(50ms) では表現できない領域。"""
    scale = [C4 + n for n in MAJOR] + [C4 + 12 + n for n in MAJOR]
    return [Hit(i * 0.125, 0.1, (scale[i % len(scale)],), 100) for i in range(64)], {}


def t10_range() -> tuple[list[Hit], dict]:
    """F♯1 から F♯7 まで。楽器の自動切り替えを見る。"""
    return [Hit(i * 0.25, 0.2, (30 + i,), 100) for i in range(73)], {}


TESTS = {
    "01_single": (t01_single, 120),
    "02_dyad": (t02_dyad, 120),
    "03_triad": (t03_triad, 120),
    "04_melody_accomp": (t04_melody_accomp, 120),
    "05_drums": (t05_drums, 120),
    "06_dynamics": (t06_dynamics, 120),
    "07_decay": (t07_decay, 120),
    "08_multi_instrument": (t08_multi_instrument, 120),
    "09_fast": (t09_fast, 180),
    "10_range": (t10_range, 120),
}


def generate_all(out_dir: Path) -> list[Path]:
    written = []
    for name, (fn, bpm) in TESTS.items():
        hits, programs = fn()
        path = out_dir / f"{name}.mid"
        write_midi(path, hits, bpm=bpm, programs=programs or {0: 0})
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="テスト用 MIDI を生成する")
    ap.add_argument("--out", type=Path, default=Path("tests/inputs"))
    ap.add_argument("--only", nargs="*", default=None, help="生成するテスト名")
    args = ap.parse_args(argv)

    names = args.only or list(TESTS)
    for name in names:
        if name not in TESTS:
            print(f"未知のテスト: {name}（{', '.join(TESTS)}）", file=sys.stderr)
            return 1
        fn, bpm = TESTS[name]
        hits, programs = fn()
        path = args.out / f"{name}.mid"
        write_midi(path, hits, bpm=bpm, programs=programs or {0: 0})
        beats = max(h.beat + h.duration for h in hits)
        print(f"  {path}  {len(hits)} 発音 / {beats * 60 / bpm:.1f} 秒 / {bpm:g} BPM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
