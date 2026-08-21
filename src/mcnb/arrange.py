"""編曲 — 音符ブロックという楽器に合わせて音を作り直す。

「音符に変換する」のではなく「音符ブロックで編曲しなおす」ための層。
採譜（何の音が鳴っているか）と配置（どこに置くか）の間に入る。

各変換は ``Song -> Song`` で、掛ける順番を変えられるようにしてある。
効果は ``mcnb render --compare`` で数値として確認できる。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace

from .instruments import INSTRUMENTS
from .song import NoteEvent, Song

#: 鳴らし直しの下限。これより短い間隔で同じ音を重ねても濁るだけ
MIN_RETRIGGER = 2


@dataclass
class ArrangeConfig:
    """編曲の効き具合。全部切れば素通しになる。"""

    #: サステインの鳴らし直しを減衰時間に合わせる
    thin_sustains: bool = True
    #: 減衰時間の何割で鳴らし直すか。1.0 = 完全に消えてから
    retrigger_ratio: float = 0.55
    #: 音量がこれ以上跳ねたら新しいアタックとみなして鳴らし直す
    attack_jump: float = 0.25

    #: 同じ tick に重なった同音（オクターブ違いを除く）を1本にまとめる
    dedupe: bool = True
    #: まとめた音のエネルギーを残した音に足し戻す。
    #: これをやらないと、音を減らしたぶんだけ全体が痩せる（低域が特に落ちる）
    compensate: bool = True

    #: 倍音とみなせる音を落とす
    fold_harmonics: bool = True
    #: 基音の何割以下の音量なら倍音として落とすか
    harmonic_ratio: float = 0.55

    #: 1 tick に残す最大音数。0 なら制限しない
    max_per_tick: int = 0

    #: **同時に鳴っている音**の上限。0 なら制限しない。
    #:
    #: 音符ブロックの音は 0.5〜16 tick 残るので、「1 tick に始まる音」を絞っても
    #: 実際に重なっている数は減らない。雑音らしさ（spectral flatness）は
    #: 音数ではなく**重なりの数**で決まることが実測で分かったので、こちらを制御する。
    max_concurrent: int = 0

    stats: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# サステインの間引き
# --------------------------------------------------------------------------- #


def retrigger_interval(instrument: str, ratio: float) -> int:
    """その楽器を鳴らし直すべき間隔（tick）。

    実測した減衰時間（docs/02_measurements.md）を使う。
    hat は 0.5 tick で消えるので毎 tick 打ってよいが、
    chime は 16 tick 残るので毎 tick 打つと16重に積み上がる。
    """
    inst = INSTRUMENTS.get(instrument)
    if inst is None:
        return MIN_RETRIGGER
    return max(MIN_RETRIGGER, round(inst.decay_ticks * ratio))


def thin_sustains(song: Song, config: ArrangeConfig) -> Song:
    """同じ音高の鳴らし直しを、その楽器が減衰しきる間隔まで間引く。

    採譜がスペクトルのピークをフレームごとに出すので、持続音が
    「毎 tick 同じ音」になる。音符ブロックの音は 0.1〜0.8 秒残るため、
    そのまま置くと同じ音が何重にも重なって濁り、同時発音の枠も食い潰す。
    """
    groups: dict[tuple[str, int], list[NoteEvent]] = defaultdict(list)
    for e in song.events:
        groups[(e.instrument, e.midi)].append(e)

    kept: list[NoteEvent] = []
    removed = 0

    for (instrument, _midi), events in groups.items():
        events.sort(key=lambda e: e.tick)
        interval = retrigger_interval(instrument, config.retrigger_ratio)
        last_tick = -10**9
        last_velocity = 0.0

        pending: list[float] = []
        for e in events:
            gap = e.tick - last_tick
            # 音量が跳ねたら新しいアタック。間隔を無視して鳴らし直す
            attacked = e.velocity - last_velocity >= config.attack_jump
            if gap >= interval or attacked:
                # 直前に捨てた区間の音量を、これから鳴らす音に持たせる。
                # 持続音を間引いても全体が痩せないようにするため
                if config.compensate and pending:
                    e = replace(e, velocity=min(1.0, max(e.velocity, max(pending))))
                pending.clear()
                kept.append(e)
                last_tick = e.tick
                last_velocity = e.velocity
            else:
                removed += 1
                pending.append(e.velocity)

    config.stats["thin_sustains"] = removed
    kept.sort(key=lambda e: (e.tick, -e.velocity))
    return Song(name=song.name, events=kept, source=song.source)


# --------------------------------------------------------------------------- #
# 同音の重複除去
# --------------------------------------------------------------------------- #


def dedupe(song: Song, config: ArrangeConfig) -> Song:
    """同じ tick の同じ音高を1本にまとめる。

    楽器が違っても同じ音高が重なっていれば、聴感上は厚みが増すだけで
    音数を食う。一番音量の大きいものを残す。
    """
    groups: dict[tuple[int, int], list[NoteEvent]] = defaultdict(list)
    for e in song.events:
        groups[(e.tick, e.midi)].append(e)

    best: dict[tuple[int, int], NoteEvent] = {}
    removed = 0
    for key, events in groups.items():
        removed += len(events) - 1
        loudest = max(events, key=lambda e: e.velocity)
        if config.compensate and len(events) > 1:
            # 同じ音を N 本重ねた音量を1本に畳む。非干渉としてエネルギー和を取る
            energy = sum(e.velocity ** 2 for e in events) ** 0.5
            loudest = replace(loudest, velocity=min(1.0, energy))
        best[key] = loudest

    config.stats["dedupe"] = removed
    events = sorted(best.values(), key=lambda e: (e.tick, -e.velocity))
    return Song(name=song.name, events=events, source=song.source)


# --------------------------------------------------------------------------- #
# 倍音を落とす
# --------------------------------------------------------------------------- #

#: 基音に対する倍音の位置（半音）。2倍音=+12, 3倍音=+19, 4倍音=+24, 5倍音=+28, 6倍音=+31
HARMONIC_OFFSETS = (12, 19, 24, 28, 31, 36)


def fold_harmonics(song: Song, config: ArrangeConfig) -> Song:
    """基音より小さい倍音を落とす。

    CQT のピークを拾う採譜は、1つの音に対して倍音列も音符として出す。
    音符ブロックの音源自体が倍音を持っているので、それを重ねる意味は薄い。

    「下に基音があり、自分がそれより十分小さい」ものだけを落とす。
    和音の第5音などを消さないよう、音量の条件を必ず見る。
    """
    by_tick: dict[int, list[NoteEvent]] = defaultdict(list)
    for e in song.events:
        by_tick[e.tick].append(e)

    kept: list[NoteEvent] = []
    removed = 0

    for tick in sorted(by_tick):
        events = by_tick[tick]
        louder_at: dict[int, float] = {}
        for e in events:
            louder_at[e.midi] = max(louder_at.get(e.midi, 0.0), e.velocity)

        for e in events:
            is_harmonic = False
            for offset in HARMONIC_OFFSETS:
                fundamental = louder_at.get(e.midi - offset)
                if fundamental is not None and e.velocity <= fundamental * config.harmonic_ratio:
                    is_harmonic = True
                    break
            if is_harmonic:
                removed += 1
            else:
                kept.append(e)

    config.stats["fold_harmonics"] = removed
    kept.sort(key=lambda e: (e.tick, -e.velocity))
    return Song(name=song.name, events=kept, source=song.source)


# --------------------------------------------------------------------------- #
# 密度の上限
# --------------------------------------------------------------------------- #


def cap_density(song: Song, config: ArrangeConfig) -> Song:
    """1 tick に残す音数の上限。音量の大きいものから残す。

    人間の編曲は毎秒190音などという密度にはならない。
    ここは「何音まで意味があるか」を実験するための直接的なつまみ。
    """
    if config.max_per_tick <= 0:
        return song

    by_tick: dict[int, list[NoteEvent]] = defaultdict(list)
    for e in song.events:
        by_tick[e.tick].append(e)

    kept: list[NoteEvent] = []
    removed = 0
    for tick in sorted(by_tick):
        events = sorted(by_tick[tick], key=lambda e: -e.velocity)
        kept.extend(events[: config.max_per_tick])
        removed += max(0, len(events) - config.max_per_tick)

    config.stats["cap_density"] = removed
    kept.sort(key=lambda e: (e.tick, -e.velocity))
    return Song(name=song.name, events=kept, source=song.source)


# --------------------------------------------------------------------------- #
# 重なりの上限
# --------------------------------------------------------------------------- #


def cap_concurrent(song: Song, config: ArrangeConfig) -> Song:
    """同時に鳴っている音の数を上限で抑える。

    実測でわかったこと（docs/04_test_log.md）:

    * 音符ブロックの素の音は雑音らしさ 0.0003〜0.07 でまったく雑音ではない
    * 重ならない音階（Test10, 73音）を鳴らしても 0.0497 のまま
    * 重なる曲（Test4, たった20音）で 0.1676 に跳ね上がる
    * 3,787音を606音に減らしても 0.195 のまま変わらない

    つまり**「音符ブロックらしい音」を壊しているのは音数ではなく重なりの数**。
    ここが「聴覚パレイドリア」の直接の原因。

    どれを残すかは音量順。大きい音ほど曲の骨格を担っているとみなす。
    """
    if config.max_concurrent <= 0:
        return song

    events = sorted(song.events, key=lambda e: (e.tick, -e.velocity))
    #: 鳴り終わる tick を音量つきで持っておく
    ringing: list[tuple[int, float]] = []
    kept: list[NoteEvent] = []
    removed = 0

    for e in events:
        inst = INSTRUMENTS.get(e.instrument)
        # 実測の減衰時間（-40dB）だけ鳴り続けるものとして数える
        length = max(1, round(inst.decay_ticks)) if inst else 2
        ringing = [(end, v) for end, v in ringing if end > e.tick]

        if len(ringing) >= config.max_concurrent:
            # 上限に達している。いま鳴っている一番小さい音より大きければ差し替える
            quietest = min(ringing, key=lambda r: r[1])
            if e.velocity <= quietest[1]:
                removed += 1
                continue
            ringing.remove(quietest)

        ringing.append((e.tick + length, e.velocity))
        kept.append(e)

    config.stats["cap_concurrent"] = removed
    kept.sort(key=lambda e: (e.tick, -e.velocity))
    return Song(name=song.name, events=kept, source=song.source)


# --------------------------------------------------------------------------- #
# まとめて掛ける
# --------------------------------------------------------------------------- #

#: 掛ける順番。倍音を落としてから重複を消し、最後に密度を切る
PIPELINE = [
    ("fold_harmonics", fold_harmonics, "fold_harmonics"),
    ("dedupe", dedupe, "dedupe"),
    ("thin_sustains", thin_sustains, "thin_sustains"),
    ("cap_density", cap_density, None),
    ("cap_concurrent", cap_concurrent, None),
]


def arrange(song: Song, config: ArrangeConfig | None = None) -> tuple[Song, ArrangeConfig]:
    """編曲の変換を順に掛ける。``(結果, 統計を持った config)`` を返す。"""
    config = config or ArrangeConfig()
    config.stats.clear()
    config.stats["入力"] = len(song.events)

    for name, fn, flag in PIPELINE:
        if flag is not None and not getattr(config, flag):
            continue
        song = fn(song, config)
        config.stats[f"{name} 後"] = len(song.events)

    config.stats["出力"] = len(song.events)
    return song, config


def summarize(config: ArrangeConfig) -> str:
    stats = config.stats
    before, after = stats.get("入力", 0), stats.get("出力", 0)
    lines = [f"  入力 {before} 音 → 出力 {after} 音"]
    if before:
        lines[0] += f"  ({after / before * 100:.0f}%)"
    for name, _fn, _flag in PIPELINE:
        removed = stats.get(name)
        if removed:
            lines.append(f"    {name:16s} -{removed}")
    return "\n".join(lines)
