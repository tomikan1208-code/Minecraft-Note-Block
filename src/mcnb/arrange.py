"""編曲 — 音符ブロックという楽器に合わせて音を作り直す。

「音符に変換する」のではなく「音符ブロックで編曲しなおす」ための層。
採譜（何の音が鳴っているか）と配置（どこに置くか）の間に入る。

各変換は ``Song -> Song`` で、掛ける順番を変えられるようにしてある。
効果は ``mcnb render --compare`` で数値として確認できる。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import numpy as np

from .instruments import INSTRUMENTS
from .song import TICKS_PER_SECOND, NoteEvent, Song

if TYPE_CHECKING:
    from .musical import MusicalContext

#: 鳴らし直しの下限。これより短い間隔で同じ音を重ねても濁るだけ
MIN_RETRIGGER = 2

# --------------------------------------------------------------------------- #
# 音楽的な重み — どの音を残すか
# --------------------------------------------------------------------------- #

#: 主旋律に乗っている音。**小さくても必ず残す**。旋律が消えたら曲ではなくなる。
#:
#: 値は当てずっぽうではなく、他が覆せない大きさとして決めてある。
#: 主旋律でない音の重みは、最大でも
#: 音量 1.0 + オクターブ重ね 0.45 + 根音 0.50 + 低音 0.30 + 小節頭 0.25 = 2.50。
#: これを超える値にすれば「主旋律は必ず伴奏より先に残る」が保証される。
#: 実際、1.0 にしていたときは大きい低音（2.40）に負けて旋律が 16 音中 7 音消えた。
WEIGHT_MELODY = 2.60
#: 主旋律のオクターブ違い（重ねて厚みを出している声部）
WEIGHT_MELODY_OCTAVE = 0.45
#: コードの構成音。根音と第3音が和音の性格を決める。第5音は省いても和音は壊れない
WEIGHT_CHORD = {0: 0.50, 1: 0.40, 2: 0.12, 3: 0.22}
#: コード外の音。採譜の誤り（倍音・雑音）である可能性が高いので不利にする。
#: ただし経過音や刺繍音も含まれるので、落とし切らない程度に留める
WEIGHT_OFF_CHORD = -0.30
#: 小節頭。拍子が当てにならない曲では効かせない
WEIGHT_DOWNBEAT = 0.25
#: 低音。曲の土台なので、上の音より優先して残す
WEIGHT_BASS = 0.30
BASS_MIDI = 52
#: 主旋律とみなす音高のずれ（半音）
MELODY_TOLERANCE = 0.7


def musical_weight(event: NoteEvent, context: MusicalContext, offset: float = 0.0) -> float:
    """その音がどれだけ「曲の骨格」かを返す。

    これまでは音量だけで取捨を決めていた。大きい音ほど骨格だという当て推量で、
    **静かな主旋律より賑やかな伴奏を残してしまう**。原音の解析が入ったので、
    役割で決められるようになった。

    返すのは音量に足す下駄。負にもなる。
    """
    seconds = at_seconds(event, offset)
    weight = 0.0

    on_melody = False
    melody = context.melody_at(seconds)
    if melody is not None:
        gap = abs(event.midi - melody)
        if gap <= MELODY_TOLERANCE:
            weight += WEIGHT_MELODY
            on_melody = True
        elif abs(gap - 12) <= MELODY_TOLERANCE or abs(gap - 24) <= MELODY_TOLERANCE:
            weight += WEIGHT_MELODY_OCTAVE

    chord = context.chord_at(seconds)
    if chord is not None and chord.root >= 0:
        degree = chord.degree(event.midi)
        if degree is not None:
            weight += WEIGHT_CHORD.get(degree, 0.0)
        elif not on_melody:
            # コード外の減点は**採譜の誤り**を狙ったもの。
            # 旋律の経過音・刺繍音は和音に属さないのが当たり前なので、
            # そこに当てると旋律が動くたびに削られる。
            weight += WEIGHT_OFF_CHORD

    if context.is_downbeat(seconds):
        weight += WEIGHT_DOWNBEAT

    if event.midi <= BASS_MIDI:
        weight += WEIGHT_BASS

    return weight


# --------------------------------------------------------------------------- #
# 拍の格子に割り付ける
# --------------------------------------------------------------------------- #

#: 1 拍を何分割した格子に載せるか。4 なら 16 分音符
QUANTIZE_DIVISION = 4
#: 同じ枠に入った音のうち、これ以内の音程差なら 1 音にまとめる（半音）。
#: 採譜は 1 つの音を半音上下に揺れながら刻むことがある（実測で MIDI 76 の
#: 1 音が 76/77 を 0.05 秒ごとに行き来する 8 連打になっていた）。
#: 音程が違うので「同じ音の連打」としては引っかからず、素通りしていた。
QUANTIZE_MERGE_SEMITONES = 1.0


def _grid_ticks(context: MusicalContext, offset: float, division: int) -> list[int]:
    """拍を分割した格子を tick で返す。"""
    if not context.beats:
        return []
    grid: list[int] = []
    beats = context.beats
    for i, beat in enumerate(beats):
        span = (beats[i + 1] - beat) if i + 1 < len(beats) else (
            beat - beats[i - 1] if i else 0.5
        )
        for k in range(division):
            seconds = beat + span * k / division - offset
            tick = int(round(seconds * TICKS_PER_SECOND))
            if tick >= 0 and (not grid or tick != grid[-1]):
                grid.append(tick)
    return grid


def quantize(song: Song, config: ArrangeConfig) -> Song:
    """音符を拍の格子に載せ、1 枠に 1 音だけ残す。

    採譜は「1 つの音」を細かい連打として出すことがある。長く伸ばした音を
    刻んだり、音程を半音上下に揺らしたりする。人が聴くと**1 音のはずが
    ピロピロ鳴る**。同じ音程の連打なら thin_sustains が間引くが、
    音程が揺れると別の音として素通りする。

    拍が分かっているなら、「この枠には音符が 1 個」と決めてしまえばよい。
    枠の中で近すぎる音程（既定で半音以内）は、大事なほうだけ残す。
    和音は音程が離れているので残る。
    """
    if not config.quantize or config.context is None:
        return song

    grid = _grid_ticks(config.context, config.time_offset, config.division)
    if len(grid) < 2:
        return song

    array = np.asarray(grid)
    cells: dict[int, list[NoteEvent]] = defaultdict(list)
    for e in song.events:
        i = int(np.searchsorted(array, e.tick))
        near = min(
            (j for j in (i - 1, i) if 0 <= j < len(array)),
            key=lambda j: abs(int(array[j]) - e.tick),
            default=None,
        )
        cells[int(array[near]) if near is not None else e.tick].append(e)

    kept: list[NoteEvent] = []
    removed = 0
    for tick, events in cells.items():
        events.sort(key=lambda e: -importance(e, config.context, config.time_offset))
        chosen: list[NoteEvent] = []
        drums: set[str] = set()
        for e in events:
            if e.instrument in PERCUSSION:
                # 打楽器の midi は音程ではないので、半音の近さで比べても意味がない。
                # 同じ枠に同じ太鼓が 2 つ要ることもないので、楽器ごとに 1 つにする
                if e.instrument in drums:
                    removed += 1
                    continue
                drums.add(e.instrument)
            elif any(
                c.instrument not in PERCUSSION
                and abs(e.midi - c.midi) <= QUANTIZE_MERGE_SEMITONES
                for c in chosen
            ):
                removed += 1
                continue
            chosen.append(replace(e, tick=tick))
        kept.extend(chosen)

    config.stats["quantize"] = removed
    kept.sort(key=lambda e: (e.tick, -e.velocity))
    return Song(name=song.name, events=kept, source=song.source)


# --------------------------------------------------------------------------- #
# 役割ごとの音色と音量
# --------------------------------------------------------------------------- #

#: 主旋律に使う楽器。音域ごとに、伴奏と音色がぶつからないものを選ぶ。
#: (下限MIDI, 上限MIDI, 楽器) の並び。上から順に見て最初に入るものを使う。
#:
#: harp を避けているのは、hyperchoron が伴奏の大半を harp に割り当てるから。
#: 同じ音色で重ねると、いくら音量を上げても旋律は伴奏に溶ける。
MELODY_VOICES = (
    (54, 78, "bit"),        # エメラルドブロック。減衰 4.5 tick で粒が立つ
    (66, 90, "flute"),      # 粘土。中高域で通る
    (78, 102, "bell"),      # 金ブロック。高域はこれ
    (42, 66, "guitar"),     # 羊毛。低めの旋律
)
#: 低音に使う楽器
BASS_VOICE = "bass"
#: 打楽器は音程を持たないので触らない
PERCUSSION = {"snare", "hat", "basedrum"}

#: 主旋律と伴奏を、それぞれ別の音量の帯に割り当てる。
#:
#: 音符ブロックには音量そのものが無い。layout が音量を**プレイヤーからの距離**に
#: 変換するので、距離の割り振りが唯一の強弱手段になる。
#: だから「何倍にする」ではなく「どの帯に置く」で決める。
#:
#: 倍率でやると、元の音量差が大きいときに逆転できない。実際 1.4 倍では
#: 音量 0.30 の旋律は 0.42 にしかならず、0.85 の伴奏に負けたままだった。
#: 帯を分ければ**重ならないことが構造的に保証される**。
#: 帯の中では元の強弱の順番を保つので、旋律の中の抑揚は残る。
#:
#: 端は layout の距離の上下限（2〜45 ブロック）に当たらないように取る。
#: 当たると帯の中の音が全部同じ距離に潰れ、抑揚が消える。
#:   音量 0.958 で最短 2 ブロック、0.063 で最長 45 ブロック。
#: 主旋律は 2.9〜10.6 ブロック、伴奏は 14.4〜28.8 ブロックに収まる。
MELODY_BAND = (0.78, 0.94)
ACCOMPANIMENT_BAND = (0.40, 0.70)


def melody_voice(midi: int) -> str | None:
    """その音高の主旋律に使う楽器。置けなければ None。"""
    for lo, hi, name in MELODY_VOICES:
        if lo <= midi <= hi:
            return name
    return None


def voice_by_role(song: Song, config: ArrangeConfig) -> Song:
    """主旋律と低音に、役割に合った楽器を割り当てる。

    採譜は原音の音色をなぞって楽器を選ぶので、**旋律と伴奏が同じ音色になる**。
    音符ブロックは1つの音色につき2オクターブしかなく、同じ音色が重なると
    どれが旋律か分からなくなる。ここで音色を分ける。

    音域外なら元のままにする。無理に移すと音程が変わってしまう。
    """
    if not config.voice_roles or config.context is None:
        return song

    changed = 0
    events: list[NoteEvent] = []
    for e in song.events:
        if e.instrument in PERCUSSION:
            events.append(e)
            continue
        want: str | None = None
        if is_melody(e, config.context, config.time_offset):
            want = melody_voice(e.midi)
        elif e.midi <= BASS_MIDI:
            want = BASS_VOICE
        if want and want != e.instrument:
            inst = INSTRUMENTS.get(want)
            if inst and inst.base_midi <= e.midi <= inst.base_midi + 24:
                events.append(replace(e, instrument=want))
                changed += 1
                continue
        events.append(e)

    config.stats["voice_by_role"] = changed
    return Song(name=song.name, events=events, source=song.source)


def emphasize_melody(song: Song, config: ArrangeConfig) -> Song:
    """主旋律を前に、伴奏を後ろに下げる。

    layout は音量を**プレイヤーからの距離**に変換する。音量を上げた音は
    近くに置かれ、実際に大きく聞こえる。音符ブロックには音量そのものが無いので、
    強弱をつける手段はこれしかない。
    """
    if not config.emphasize_melody or config.context is None:
        return song

    lifted = 0
    events: list[NoteEvent] = []
    for e in song.events:
        if e.instrument in PERCUSSION:
            events.append(e)
            continue
        if is_melody(e, config.context, config.time_offset):
            lo, hi = MELODY_BAND
            lifted += 1
        else:
            lo, hi = ACCOMPANIMENT_BAND
        level = lo + max(0.0, min(1.0, e.velocity)) * (hi - lo)
        events.append(replace(e, velocity=level))

    config.stats["emphasize_melody"] = lifted
    return Song(name=song.name, events=events, source=song.source)


def at_seconds(event: NoteEvent, offset: float = 0.0) -> float:
    """その音符が原音のどの時刻にあたるか。"""
    return event.tick / TICKS_PER_SECOND + offset


def is_melody(event: NoteEvent, context: MusicalContext | None, offset: float = 0.0) -> bool:
    """主旋律そのものか（オクターブ違いは含めない）。"""
    if context is None:
        return False
    melody = context.melody_at(at_seconds(event, offset))
    return melody is not None and abs(event.midi - melody) <= MELODY_TOLERANCE


def importance(event: NoteEvent, context: MusicalContext | None, offset: float = 0.0) -> float:
    """取捨に使う値。解析が無ければ音量そのまま（これまでの振る舞い）。"""
    if context is None:
        return event.velocity
    return event.velocity + musical_weight(event, context, offset)


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

    #: 音符を拍の格子に載せ、1 枠に 1 音だけ残す
    quantize: bool = True
    #: 1 拍を何分割するか。4 なら 16 分音符
    division: int = QUANTIZE_DIVISION

    #: 主旋律と低音に、役割に合った楽器を割り当てる
    voice_roles: bool = True
    #: 主旋律を前に、伴奏を後ろに下げる（音量＝距離）
    emphasize_melody: bool = True

    #: 原音の解析結果（mcnb.musical.MusicalContext）。
    #: あれば、どの音を残すかを音量ではなく**音楽的な役割**で決める
    context: MusicalContext | None = None
    #: 採譜と原音の時間のずれ（秒）。採譜の時刻にこれを足すと原音の時刻になる。
    #: 曲ごとに違う（実測で -93ms 〜 +372ms）。ずれたまま突き合わせると、
    #: 別の時刻の音を主旋律とみなして大きくしてしまう
    time_offset: float = 0.0

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

    **主旋律は落とさない。** 旋律は低音の 1 オクターブ上にいることが多く、
    音量も伴奏より小さいことがあるので、この判定にそのまま掛けると
    「倍音」として消える。実際、合成した素材では主旋律が丸ごと消えていた。
    倍音か旋律かはこの層だけでは区別できないので、原音の解析に聞く。
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
            if is_melody(e, config.context, config.time_offset):
                kept.append(e)
                continue
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
    """1 tick に残す音数の上限。**大事な音**から残す。

    人間の編曲は毎秒190音などという密度にはならない。
    ここは「何音まで意味があるか」を実験するための直接的なつまみ。

    解析結果を渡してあれば、主旋律・コードの構成音・低音を優先する。
    無ければ音量順（これまでの振る舞い）。
    """
    if config.max_per_tick <= 0:
        return song

    by_tick: dict[int, list[NoteEvent]] = defaultdict(list)
    for e in song.events:
        by_tick[e.tick].append(e)

    kept: list[NoteEvent] = []
    removed = 0
    for tick in sorted(by_tick):
        events = sorted(by_tick[tick], key=lambda e: -importance(e, config.context, config.time_offset))
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

    どれを残すかは**音楽的な役割**で決める。解析結果が無ければ音量順に戻る。

    音量順だけだと、静かな主旋律より賑やかな伴奏が残る。ここで旋律が
    削れると、いくら音数を合わせても曲として成り立たない。
    """
    if config.max_concurrent <= 0:
        return song

    events = sorted(song.events, key=lambda e: (e.tick, -importance(e, config.context, config.time_offset)))
    #: 鳴り終わる tick を、その音の大事さつきで持っておく
    ringing: list[tuple[int, float]] = []
    kept: list[NoteEvent] = []
    removed = 0

    for e in events:
        inst = INSTRUMENTS.get(e.instrument)
        # 実測の減衰時間（-40dB）だけ鳴り続けるものとして数える
        length = max(1, round(inst.decay_ticks)) if inst else 2
        weight = importance(e, config.context, config.time_offset)
        ringing = [(end, w) for end, w in ringing if end > e.tick]

        if len(ringing) >= config.max_concurrent:
            # 上限に達している。いま鳴っている一番どうでもいい音より大事なら差し替える
            least = min(ringing, key=lambda r: r[1])
            if weight <= least[1]:
                removed += 1
                continue
            ringing.remove(least)

        ringing.append((e.tick + length, weight))
        kept.append(e)

    config.stats["cap_concurrent"] = removed
    kept.sort(key=lambda e: (e.tick, -e.velocity))
    return Song(name=song.name, events=kept, source=song.source)


# --------------------------------------------------------------------------- #
# まとめて掛ける
# --------------------------------------------------------------------------- #

#: 掛ける順番。倍音を落としてから重複を消し、最後に密度を切る
PIPELINE = [
    # 格子への割り付けが最初。ここで tick が動くので、あとの処理は
    # 動いたあとの位置で数えないと辻褄が合わない
    ("quantize", quantize, "quantize"),
    # 音色は先に決める。cap_concurrent が楽器ごとの減衰時間で重なりを数えるので、
    # あとから楽器を変えると数え直しになる
    ("voice_by_role", voice_by_role, "voice_roles"),
    ("fold_harmonics", fold_harmonics, "fold_harmonics"),
    ("dedupe", dedupe, "dedupe"),
    ("thin_sustains", thin_sustains, "thin_sustains"),
    ("cap_density", cap_density, None),
    ("cap_concurrent", cap_concurrent, None),
    # 強弱は**最後**。dedupe がまとめた音のぶんを足し戻して音量を上げるので、
    # 先に帯へ収めても後から押し出されてしまう。
    # 取捨のほうは音量ではなく importance() で決めているので、順番の影響を受けない
    ("emphasize_melody", emphasize_melody, "emphasize_melody"),
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
