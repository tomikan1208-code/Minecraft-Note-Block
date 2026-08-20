"""演奏データの中間表現と、NBS からの読み込み。

hyperchoron に音楽的な変換（音源分離・採譜・楽器割り当て・strum など）をやらせ、
その結果を ``.nbs`` で受け取る。NBS v4 以降は**ノートごとに velocity と panning**
を持つので、音量と定位の情報を失わずに受け渡せる。

ここから先（Minecraft 内での配置とタイミング）は自前でやる。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .instruments import INSTRUMENTS, Instrument, instruments_covering, nbs_instrument_name

#: Minecraft の game tick
TICKS_PER_SECOND = 20

#: NBS の key 0 は A0 = MIDI 21
NBS_KEY_TO_MIDI = 21


@dataclass(frozen=True)
class NoteEvent:
    """1つの発音。まだ Minecraft のどこに置くかは決まっていない。"""

    tick: int
    instrument: str
    midi: int
    velocity: float  # 0..1
    panning: float = 0.0  # -1(左) .. +1(右)

    @property
    def key(self) -> int:
        """音符ブロックの note ステート (0-24)。"""
        return INSTRUMENTS[self.instrument].key_for(self.midi)


@dataclass
class Song:
    name: str
    events: list[NoteEvent] = field(default_factory=list)
    source: str = ""

    @property
    def length_ticks(self) -> int:
        return max((e.tick for e in self.events), default=0) + 1

    @property
    def length_seconds(self) -> float:
        return self.length_ticks / TICKS_PER_SECOND

    def events_by_tick(self) -> dict[int, list[NoteEvent]]:
        out: dict[int, list[NoteEvent]] = {}
        for e in self.events:
            out.setdefault(e.tick, []).append(e)
        return out

    @property
    def max_polyphony(self) -> int:
        by_tick = self.events_by_tick()
        return max((len(v) for v in by_tick.values()), default=0)

    def instrument_histogram(self) -> dict[str, int]:
        h: dict[str, int] = {}
        for e in self.events:
            h[e.instrument] = h.get(e.instrument, 0) + 1
        return dict(sorted(h.items(), key=lambda kv: -kv[1]))


def _retarget(instrument: str, midi: int) -> tuple[str, int] | None:
    """音高が今の楽器の範囲外なら、出せる楽器に載せ替える。

    候補が複数ある場合は「元の楽器と音域の中心が近いもの」を選ぶ。
    v0 では単純な最近傍でよい（音色の最適化は編曲最適化器の仕事）。
    """
    inst = INSTRUMENTS.get(instrument)
    if inst is not None and inst.pitched and inst.covers(midi):
        return instrument, midi

    candidates = instruments_covering(midi)
    if candidates:
        if inst is not None:
            origin = (inst.lo_midi + inst.hi_midi) / 2
            candidates.sort(key=lambda c: abs((c.lo_midi + c.hi_midi) / 2 - origin))
        return candidates[0].name, midi

    # 全楽器の範囲外 → オクターブ単位で折り返す
    for shift in (12, -12, 24, -24, 36, -36):
        moved = midi + shift
        candidates = instruments_covering(moved)
        if candidates:
            return candidates[0].name, moved
    return None


def load_nbs(path: Path | str, name: str | None = None) -> Song:
    """``.nbs`` を読み込んで Song にする。

    * レイヤーの volume / stereo をノートに畳み込む
    * NBS の tempo が 20 tps でない場合は tick を張り替える
    * 音域外のノートは出せる楽器に載せ替える
    """
    import pynbs

    path = Path(path)
    nbs = pynbs.read(str(path))

    # NBS の tempo は 1秒あたりの tick 数
    tps = nbs.header.tempo or TICKS_PER_SECOND
    scale = TICKS_PER_SECOND / tps

    layers = list(nbs.layers)
    events: list[NoteEvent] = []
    dropped = 0

    for note in nbs.notes:
        layer = layers[note.layer] if note.layer < len(layers) else None

        velocity = note.velocity / 100.0
        panning = note.panning / 100.0
        if layer is not None:
            velocity *= layer.volume / 100.0
            # NBS のレイヤー panning は 0=左, 100=中央, 200=右 で保存され、
            # pynbs は -100..100 に正規化して返す
            panning = max(-1.0, min(1.0, panning + layer.panning / 100.0))
        if velocity <= 0:
            continue

        instrument = nbs_instrument_name(note.instrument)
        inst = INSTRUMENTS[instrument]

        if inst.pitched:
            midi = note.key + NBS_KEY_TO_MIDI
            retargeted = _retarget(instrument, midi)
            if retargeted is None:
                dropped += 1
                continue
            instrument, midi = retargeted
        else:
            # 打楽器は音程を持たない。note ステートは音色バリエーションとして使う
            midi = inst.base_midi + max(0, min(24, note.key + NBS_KEY_TO_MIDI - inst.base_midi))

        events.append(
            NoteEvent(
                tick=round(note.tick * scale),
                instrument=instrument,
                midi=midi,
                velocity=max(0.0, min(1.0, velocity)),
                panning=max(-1.0, min(1.0, panning)),
            )
        )

    events.sort(key=lambda e: (e.tick, -e.velocity))
    song = Song(name=name or path.stem, events=events, source=str(path))
    if dropped:
        print(f"  音域外で捨てたノート: {dropped}")
    return song


def summarize(song: Song) -> str:
    lines = [
        f"曲          : {song.name}",
        f"長さ        : {song.length_ticks} tick ({song.length_seconds:.1f} 秒)",
        f"ノート数    : {len(song.events)}",
        f"最大同時発音: {song.max_polyphony}",
        "楽器の内訳  :",
    ]
    for inst, count in song.instrument_histogram().items():
        lines.append(f"    {inst:20s} {count:6d}")
    return "\n".join(lines)


def _instrument_or_die(name: str) -> Instrument:
    if name not in INSTRUMENTS:
        raise KeyError(f"未知の楽器: {name}")
    return INSTRUMENTS[name]
