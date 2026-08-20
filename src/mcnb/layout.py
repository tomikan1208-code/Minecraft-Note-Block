"""音符ブロックを Minecraft 空間に配置する（直線コリドー方式）。

トロッコもレールも使わない。X 軸を時間軸に取った**まっすぐな廊下**を作り、
プレイヤーは datapack から 1 tick ごとに ``/tp`` される。

    X 軸 = 時間。tick t のノートは平面 x = X0 + t*SPACING に置く。
    プレイヤーは tick t に x = X0 + t*SPACING へテレポートし、常に +X を向く。

平面内の位置で音量と定位が決まる:

    プレイヤーからの距離 d  → 音量 (gain ≈ 1 − d/48)
    左右方向のずれ dz      → ステレオ定位（+X を向いていると +Z が右）
    真上/真下             → 定位は中央のまま距離だけ稼げる

音符ブロックは「直下が楽器ブロック・真上が空気」でないと鳴らないので、
縦方向は 3 ブロック周期になる::

    y+2  楽器ブロック  ┐ 上のスロット
    y+1  空気         ┘ ← このスロットの「真上の空気」でもある
    y    音符ブロック
    y-1  楽器ブロック
    y-2  空気         ← 下のスロットの「真上の空気」

発火用のレッドストーンは平面の 1 ブロック手前 (x-1) のレーンに置く。
SPACING=2 にしてあるのでこのレーンは常に空いている。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import cos, hypot, pi, sin

from .instruments import INSTRUMENTS
from .song import NoteEvent, Song

#: 音符ブロックが聞こえる最大距離（ブロック）
MAX_HEARING = 48.0
#: プレイヤーにめり込まない最小距離
MIN_DISTANCE = 2.0
#: 一番小さい音でも聞こえる範囲に留める
MAX_DISTANCE = 45.0
#: 縦方向のスロット周期（楽器 / 音符 / 空気）
Y_PERIOD = 3
#: 時間軸方向の 1 tick あたりのブロック数。
#:
#: 発火用のレッドストーンブロックは平面の 1 手前 (x-1) に置く。これは x-2 とも
#: 隣接するので、SPACING=2 だと前 tick の音符ブロックに直接触れてしまい二重発火する。
#: SPACING=3 にすると x-2 が必ず空になり、干渉しない。
SPACING = 3
#: プレイヤーの耳のおおよその高さ（足元からの相対）
EAR_HEIGHT = 1.62


@dataclass(frozen=True)
class Placement:
    """配置が決まった1音。"""

    tick: int
    x: int
    y: int
    z: int
    instrument: str
    key: int
    #: 実際に得られる距離と定位（狙った値とはズレる）
    distance: float
    panning: float
    velocity: float

    @property
    def gain(self) -> float:
        """距離から予測される音量。v1 の実測で較正する。"""
        return max(0.0, 1.0 - self.distance / MAX_HEARING)

    @property
    def instrument_block(self) -> tuple[int, int, int]:
        return (self.x, self.y - 1, self.z)

    @property
    def trigger_pos(self) -> tuple[int, int, int]:
        return (self.x - 1, self.y, self.z)


@dataclass
class Layout:
    song: Song
    placements: list[Placement] = field(default_factory=list)
    origin: tuple[int, int, int] = (0, 0, 0)
    spacing: int = SPACING
    dropped_polyphony: int = 0
    dropped_unplaceable: int = 0

    @property
    def player_y(self) -> int:
        return self.origin[1]

    def player_pos(self, tick: int) -> tuple[int, int, int]:
        x0, y0, z0 = self.origin
        return (x0 + tick * self.spacing, y0, z0)

    @property
    def length_blocks(self) -> int:
        return self.song.length_ticks * self.spacing

    def bounds(self) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """構造全体の (最小, 最大)。空気や発火レーンも含む。"""
        if not self.placements:
            x0, y0, z0 = self.origin
            return (x0, y0, z0), (x0, y0, z0)
        xs = [p.x for p in self.placements]
        ys = [p.y for p in self.placements]
        zs = [p.z for p in self.placements]
        return (
            (min(xs) - 1, min(ys) - 1, min(zs)),
            (max(xs), max(ys) + 1, max(zs)),
        )

    def placements_by_tick(self) -> dict[int, list[Placement]]:
        out: dict[int, list[Placement]] = {}
        for p in self.placements:
            out.setdefault(p.tick, []).append(p)
        return out

    @property
    def block_count(self) -> int:
        """音符ブロック + 楽器ブロック。"""
        return len(self.placements) * 2


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def _ideal_offset(velocity: float, panning: float) -> tuple[float, float]:
    """音量と定位から、平面内での理想的な (dy, dz) を出す。

    音量 → 距離、定位 → 角度。定位が中央なら真上に置く（距離だけ稼ぐ）。
    """
    distance = MAX_HEARING * (1.0 - velocity)
    distance = max(MIN_DISTANCE, min(MAX_DISTANCE, distance))
    angle = panning * (pi / 2)
    return distance * cos(angle), distance * sin(angle)


def _snap(dy: float, dz: float) -> tuple[int, int]:
    """格子に落とす。dy は Y_PERIOD の倍数、dz は整数。"""
    return int(round(dy / Y_PERIOD)) * Y_PERIOD, int(round(dz))


def _spiral(dy: int, dz: int, rings: int = 6):
    """(dy, dz) の近くから外へ向かって候補を出す。"""
    yield dy, dz
    for r in range(1, rings + 1):
        for ddz in range(-r, r + 1):
            for ddy in (-r, r):
                yield dy + ddy * Y_PERIOD, dz + ddz
        for ddy in range(-r + 1, r):
            for ddz in (-r, r):
                yield dy + ddy * Y_PERIOD, dz + ddz


def _ear_distance(dy: int, dz: int) -> float:
    """耳の高さを基準にした実距離。足元ではなく耳から測る。"""
    return hypot(dy - EAR_HEIGHT, dz)


def _placement_error(dy: int, dz: int, target_d: float, target_pan: float) -> float:
    d = _ear_distance(dy, dz)
    if d < MIN_DISTANCE or d > MAX_DISTANCE:
        return float("inf")
    pan = dz / d if d else 0.0
    # 音量のズレはブロック単位、定位のズレは -1..1。1ブロック ≒ 定位 0.04 くらいの
    # 重みにすると、密なところで定位より音量が優先される。
    return abs(d - target_d) + abs(pan - target_pan) * 25.0


def build_layout(
    song: Song,
    origin: tuple[int, int, int] = (0, 100, 0),
    spacing: int = SPACING,
    max_polyphony: int = 200,
) -> Layout:
    """Song を直線コリドーに配置する。

    ``max_polyphony`` を超える tick では、音量の小さいノートから捨てる。
    バニラなら 247、RSLS 導入済みなら 4095 まで上げられるが、
    実際には音が濁るので既定値は控えめにしてある。
    """
    layout = Layout(song=song, origin=origin, spacing=spacing)
    x0, y0, z0 = origin

    by_tick = song.events_by_tick()

    for tick in sorted(by_tick):
        notes: list[NoteEvent] = sorted(by_tick[tick], key=lambda e: -e.velocity)
        if len(notes) > max_polyphony:
            layout.dropped_polyphony += len(notes) - max_polyphony
            notes = notes[:max_polyphony]

        x = x0 + tick * spacing
        used: set[tuple[int, int]] = set()

        for note in notes:
            inst = INSTRUMENTS.get(note.instrument)
            if inst is None:
                layout.dropped_unplaceable += 1
                continue
            try:
                key = note.key
            except ValueError:
                layout.dropped_unplaceable += 1
                continue

            target_d = max(MIN_DISTANCE, min(MAX_DISTANCE, MAX_HEARING * (1.0 - note.velocity)))
            ideal_dy, ideal_dz = _ideal_offset(note.velocity, note.panning)
            sdy, sdz = _snap(ideal_dy, ideal_dz)

            best: tuple[int, int] | None = None
            best_err = float("inf")
            for cy, cz in _spiral(sdy, sdz):
                if (cy, cz) in used:
                    continue
                err = _placement_error(cy, cz, target_d, note.panning)
                if err < best_err:
                    best, best_err = (cy, cz), err
                    if err < 0.75:  # 十分近ければ打ち切る
                        break

            # 真上が埋まっていたら真下も試す（対称なので定位は同じ）
            if best is None:
                for cy, cz in _spiral(-sdy, sdz):
                    if (cy, cz) in used:
                        continue
                    err = _placement_error(cy, cz, target_d, note.panning)
                    if err < best_err:
                        best, best_err = (cy, cz), err
                        if err < 0.75:
                            break

            if best is None:
                layout.dropped_unplaceable += 1
                continue

            dy, dz = best
            used.add((dy, dz))
            actual_d = _ear_distance(dy, dz)
            layout.placements.append(
                Placement(
                    tick=tick,
                    x=x,
                    y=y0 + dy,
                    z=z0 + dz,
                    instrument=note.instrument,
                    key=key,
                    distance=actual_d,
                    panning=dz / actual_d if actual_d else 0.0,
                    velocity=note.velocity,
                )
            )

    return layout


def summarize(layout: Layout) -> str:
    (x1, y1, z1), (x2, y2, z2) = layout.bounds()
    lines = [
        f"配置        : {len(layout.placements)} 音 / ブロック {layout.block_count}",
        f"長さ        : {layout.length_blocks} ブロック (X {x1} … {x2})",
        f"高さ        : Y {y1} … {y2}",
        f"幅          : Z {z1} … {z2}",
        f"プレイヤー  : Y={layout.player_y} を +X 方向へ {layout.spacing} ブロック/tick",
    ]
    if layout.dropped_polyphony:
        lines.append(f"同時発音超過で破棄: {layout.dropped_polyphony}")
    if layout.dropped_unplaceable:
        lines.append(f"配置できず破棄    : {layout.dropped_unplaceable}")

    errors = [abs(p.gain - p.velocity) for p in layout.placements]
    if errors:
        lines.append(f"音量の平均誤差    : {sum(errors) / len(errors):.3f}")
    return "\n".join(lines)
