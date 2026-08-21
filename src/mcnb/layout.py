"""音符ブロックを Minecraft 空間に配置する（直線コリドー方式）。

トロッコもレールも使わない。X 軸を時間軸に取った**まっすぐな廊下**を作り、
プレイヤーは datapack から 1 tick ごとに ``/tp`` される。

    X 軸 = 時間。tick t のノートは平面 x = X0 + t*SPACING に置く。
    プレイヤーは tick t に x = X0 + t*SPACING へテレポートし、常に +X を向く。

音符ブロックは**通り道の左右の壁**に並べる。真上には置かない::

    ♪♪♪♪♪♪♪♪♪♪♪  |  通り道  |  ♪♪♪♪♪♪♪♪♪♪♪
      左の壁      （プレイヤー）    右の壁

    プレイヤーからの距離 d  → 音量 (gain ≈ 1 − d/48)
    左右どちら側か          → ステレオ定位（+X を向いていると +Z が右）
    壁の高さ（数段）        → 同じ距離のスロットを増やすため

真上に積むほうが定位を中央に保てるが、
(1) 参考にしている作品が左右に広げている
(2) 左右のほうが音符ブロックが見える
ので壁にしている。そのぶん定位はほぼ左右に振り切る。

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
from math import hypot, sqrt

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
#: 壁の高さ。音符ブロックを置ける段（プレイヤーの足元からの相対）。
#: 低く抑えて見やすくする。段を増やすほど同じ距離のスロットが増える
WALL_ROWS = (1, 4, 7, 10, 13)
#: 中央寄りの定位をどちら側に振るかの閾値。これ以下なら左右を交互に使う
CENTER_PAN = 0.15
#: 時間軸方向の 1 tick あたりのブロック数。
#:
#: 発火用のレッドストーンブロックは平面の 1 手前 (x-1) に置く。これは x-2 とも
#: 隣接するので、SPACING=2 だと前 tick の音符ブロックに直接触れてしまい二重発火する。
#: SPACING=3 にすると x-2 が必ず空になり、干渉しない。
SPACING = 3
#: プレイヤーの耳のおおよその高さ（足元からの相対）
EAR_HEIGHT = 1.62
#: バニラのデフォルトのフラット地形で、プレイヤーが立つ高さ。
#: 岩盤(-64) + 土(-63,-62) + 草(-61) なので足元は -60。
#: ここを原点にすると構造が地面の上に建ち、空中に浮かない。
FLAT_SURFACE_Y = -60


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


def target_distance(velocity: float) -> float:
    """音量から、置きたい距離を出す。"""
    return max(MIN_DISTANCE, min(MAX_DISTANCE, MAX_HEARING * (1.0 - velocity)))


def _wall_dz(row: int, distance: float) -> float:
    """壁の ``row`` 段目で、耳から ``distance`` になる横方向のずれ。"""
    vertical = row - EAR_HEIGHT
    remaining = distance * distance - vertical * vertical
    return sqrt(remaining) if remaining > 0 else 0.0


#: 狙った距離からどれだけ横にずらして探すか。
#: 壁は平面より格段にスロットが少ないので、広めに探さないと置けない音が出る
SEARCH_RINGS = 24


def _candidates(distance: float, side: int, rings: int = SEARCH_RINGS):
    """狙った距離に近い順に (dy, dz) を出す。左右は ``side`` で固定する。

    段を下から順に見て、それぞれで狙った距離になる横位置を出す。
    そこが埋まっていたら少しずつ横へずらす。
    """
    for offset in range(rings + 1):
        for row in WALL_ROWS:
            dz = _wall_dz(row, distance)
            if dz <= 0:
                continue
            base = int(round(dz))
            for delta in ((0,) if offset == 0 else (offset, -offset)):
                z = base + delta
                if 1 <= z <= MAX_DISTANCE:
                    yield row, side * z


def _ear_distance(dy: int, dz: int) -> float:
    """耳の高さを基準にした実距離。足元ではなく耳から測る。"""
    return hypot(dy - EAR_HEIGHT, dz)


def _placement_error(dy: int, dz: int, target_d: float, target_pan: float, min_dy: int = 0) -> float:
    # 楽器ブロックが dy-1 に来るので、min_dy より下は地面に埋まる
    if dy < min_dy:
        return float("inf")
    d = _ear_distance(dy, dz)
    if d < MIN_DISTANCE or d > MAX_DISTANCE:
        return float("inf")
    pan = dz / d if d else 0.0
    # 音量のズレはブロック単位、定位のズレは -1..1。1ブロック ≒ 定位 0.04 くらいの
    # 重みにすると、密なところで定位より音量が優先される。
    return abs(d - target_d) + abs(pan - target_pan) * 25.0


def build_layout(
    song: Song,
    origin: tuple[int, int, int] = (0, FLAT_SURFACE_Y, 0),
    spacing: int = SPACING,
    max_polyphony: int = 200,
    min_dy: int = min(WALL_ROWS),
) -> Layout:
    """Song を直線コリドーに配置する。

    ``max_polyphony`` を超える tick では、音量の小さいノートから捨てる。
    バニラなら 247、RSLS 導入済みなら 4095 まで上げられるが、
    実際には音が濁るので既定値は控えめにしてある。

    音符ブロックは通り道の左右の壁に並べる（真上には積まない）。
    ``min_dy`` より下には置かない。フラット地形の地面に構造を埋めないため。
    """
    layout = Layout(song=song, origin=origin, spacing=spacing)
    x0, y0, z0 = origin

    by_tick = song.events_by_tick()
    # 中央寄りの音を振り分けるカウンタ。tick をまたいで持ち回すことで、
    # 主旋律のように連続する音が片側に寄らないようにする
    alternate = [0]

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

            target_d = target_distance(note.velocity)
            # 定位が中央寄りの音は左右を交互に振って、壁の使い方を偏らせない
            if abs(note.panning) <= CENTER_PAN:
                side = 1 if (alternate[0] % 2 == 0) else -1
                alternate[0] += 1
            else:
                side = 1 if note.panning > 0 else -1

            best: tuple[int, int] | None = None
            best_err = float("inf")
            for cy, cz in _candidates(target_d, side):
                if (cy, cz) in used:
                    continue
                err = _placement_error(cy, cz, target_d, note.panning, min_dy)
                if err < best_err:
                    best, best_err = (cy, cz), err
                    if err < 0.75:  # 十分近ければ打ち切る
                        break

            # その側が埋まっていたら反対側も試す
            if best is None or best_err == float("inf"):
                for cy, cz in _candidates(target_d, -side):
                    if (cy, cz) in used:
                        continue
                    err = _placement_error(cy, cz, target_d, note.panning, min_dy)
                    if err < best_err:
                        best, best_err = (cy, cz), err
                        if err < 0.75:
                            break

            if best is None or best_err == float("inf"):
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
