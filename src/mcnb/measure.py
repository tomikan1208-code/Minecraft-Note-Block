"""実機測定リグ — Minecraft に鳴らさせて録音し、音響モデルを実測で確定する。

サーバは音を鳴らさないので、音を出すのはクライアントだけ。だから:

    [Python] ──RCON──→ [専用サーバ(headless)] ──パケット──→ [クライアント]
                                                                  │ スピーカー
    [Python] ←──WASAPI ループバック録音──────────────────────────┘

**私（Claude）は音を聴かない。** 録音して数値にする。

    uv run mcnb measure                     # 全部
    uv run mcnb measure --only distance     # 距離減衰だけ

あなたがやること: サーバが立ったらクライアントを起動して ``localhost:25566`` に
接続し、あとは放置。位置合わせもモード設定もこちらから RCON でやる。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from . import audio as au
from .instruments import INSTRUMENTS
from .mcassets import default_minecraft_dir, resolve_version
from .server import Rcon, Server, ServerError, ensure_server_jar, find_java

__all__ = ["Rig", "Report", "Strike", "run", "analyze_distance", "MEASUREMENTS", "ServerError"]

LEVEL_NAME = "measure"
#: プレイヤーを立たせる位置（フラット地形の地表）
ORIGIN = (0, -60, 0)
#: 音符ブロックが聞こえる上限。この式を確かめるのが目的
MAX_HEARING = 48.0

#: setblock してから鳴らすまでの待ち（チャンク更新とブロック更新のため）
SETTLE = 0.35
#: 1音を録る窓。一番長い chime が 1.1 秒なので余裕を持たせる
WINDOW = 1.8
#: 測定と測定の間に空ける時間（前の音の残りを混ぜない）
GAP = 0.35


@dataclass
class Strike:
    """1回鳴らして録った結果。"""

    label: str
    peak: float
    rms: float
    balance: float
    frequency: float | None
    onset: float | None
    frames: int


@dataclass
class Report:
    minecraft_version: str
    samplerate: int
    device: str
    sections: dict = field(default_factory=dict)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# クライアント側の設定
# --------------------------------------------------------------------------- #

#: 測定中は音符ブロック（record カテゴリ）以外を黙らせる
QUIET_OPTIONS = {
    "soundCategory_master": "1.0",
    "soundCategory_record": "1.0",   # 音符ブロックはここ
    "soundCategory_music": "0.0",
    "soundCategory_weather": "0.0",
    "soundCategory_block": "0.0",
    "soundCategory_hostile": "0.0",
    "soundCategory_neutral": "0.0",
    "soundCategory_player": "0.0",
    "soundCategory_ambient": "0.0",
    "soundCategory_voice": "0.0",
    "soundCategory_ui": "0.0",
    # ターミナルから操作するので、フォーカスが外れても止まらないようにする
    "pauseOnLostFocus": "false",
}


def tune_client_options(game_dir: Path) -> Path | None:
    """``options.txt`` を測定向きに書き換える。最初の1回だけバックアップを取る。"""
    path = game_dir / "options.txt"
    if not path.is_file():
        return None
    backup = game_dir / "options.txt.mcnb-backup"
    if not backup.exists():
        shutil.copy2(path, backup)

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    seen = set()
    out = []
    for line in lines:
        key = line.split(":", 1)[0]
        if key in QUIET_OPTIONS:
            out.append(f"{key}:{QUIET_OPTIONS[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in QUIET_OPTIONS.items():
        if key not in seen:
            out.append(f"{key}:{value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return backup


def restore_client_options(game_dir: Path) -> bool:
    backup = game_dir / "options.txt.mcnb-backup"
    if not backup.is_file():
        return False
    shutil.copy2(backup, game_dir / "options.txt")
    return True


# --------------------------------------------------------------------------- #
# リグ
# --------------------------------------------------------------------------- #


class Rig:
    """サーバ・RCON・録音をまとめて、「1音鳴らして録る」まで面倒を見る。"""

    def __init__(self, root: Path, game_dir: Path, mc: str, memory: str = "2G"):
        launcher = default_minecraft_dir()
        self.root = root
        self.game_dir = game_dir
        self.mc = mc
        self.server = Server(root, ensure_server_jar(root, mc, launcher), find_java(), memory=memory)
        self.rcon: Rcon | None = None
        self.recorder: au.Recorder | None = None

    # -- ライフサイクル ---------------------------------------------------- #

    def __enter__(self) -> Rig:
        world = self.root / LEVEL_NAME
        if world.exists():
            shutil.rmtree(world)
        self.server.configure(
            accept_eula=True,
            level_name=LEVEL_NAME,
            extra={
                "level-type": "minecraft:flat",
                "generator-settings": json.dumps(
                    {
                        "layers": [
                            {"block": "minecraft:bedrock", "height": 1},
                            {"block": "minecraft:dirt", "height": 2},
                            {"block": "minecraft:grass_block", "height": 1},
                        ],
                        "biome": "minecraft:plains",
                    }
                ),
                "spawn-protection": "0",
                "online-mode": "false",
            },
        )
        self.server.start()
        self.rcon = Rcon()
        self.rcon.connect()
        return self

    def __exit__(self, *exc) -> None:
        if self.recorder:
            self.recorder.stop()
            self.recorder = None
        if self.rcon:
            self.rcon.close()
            self.rcon = None
        self.server.stop()

    # -- プレイヤー -------------------------------------------------------- #

    def players(self) -> list[str]:
        assert self.rcon
        response = self.rcon.command("list")
        m = re.search(r"online:\s*(.*)$", response.strip())
        names = (m.group(1) if m else "").strip()
        return [n.strip() for n in names.split(",") if n.strip()]

    def wait_for_player(self, timeout: float = 600.0) -> str:
        """クライアントが繋がるのを待つ。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            found = self.players()
            if found:
                return found[0]
            time.sleep(2.0)
        raise ServerError("クライアントが接続しませんでした。")

    def prepare(self) -> None:
        """測定できる状態に整える。"""
        assert self.rcon
        for cmd in [
            "gamerule max_command_sequence_length 2147483647",
            "gamerule max_block_modifications 2147483647",
            "gamerule send_command_feedback false",
            "gamerule advance_time false",
            "gamerule advance_weather false",
            "gamerule spawn_mobs false",
            "gamerule random_tick_speed 0",
            "time set noon",
            "weather clear",
            "difficulty peaceful",
            f"forceload add {ORIGIN[0] - 16} {ORIGIN[2] - 64} {ORIGIN[0] + 16} {ORIGIN[2] + 64}",
            "gamemode spectator @a",
            f"tp @a {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} -90 0",
            "stopsound @a",
        ]:
            self.rcon.command(cmd)
        time.sleep(1.0)

    def recenter(self) -> None:
        """毎測定の前に位置と向きを揃え直す。"""
        assert self.rcon
        self.rcon.command(f"tp @a {ORIGIN[0]} {ORIGIN[1]} {ORIGIN[2]} -90 0")

    # -- 1音鳴らす --------------------------------------------------------- #

    def strike(
        self,
        label: str,
        offset: tuple[int, int, int],
        instrument: str = "harp",
        key: int = 12,
        window: float = WINDOW,
    ) -> Strike:
        """``offset`` の位置に音符ブロックを置いて1回鳴らし、録音を測る。"""
        assert self.rcon and self.recorder
        x, y, z = (ORIGIN[0] + offset[0], ORIGIN[1] + offset[1], ORIGIN[2] + offset[2])
        block = INSTRUMENTS[instrument].block

        self.recenter()
        self.rcon.command(f"setblock {x} {y - 1} {z} minecraft:{block} replace")
        self.rcon.command(
            f"setblock {x} {y} {z} minecraft:note_block[note={key},powered=false] replace"
        )
        self.rcon.command(f"setblock {x} {y + 1} {z} minecraft:air replace")
        self.rcon.command(f"setblock {x + 1} {y} {z} minecraft:air replace")
        time.sleep(SETTLE)

        mark = self.recorder.mark()
        self.rcon.command(f"setblock {x + 1} {y} {z} minecraft:redstone_block replace")
        time.sleep(window)
        clip = self.recorder.since(mark)

        self.rcon.command(f"setblock {x + 1} {y} {z} minecraft:air replace")
        self.rcon.command(f"setblock {x} {y} {z} minecraft:air replace")
        self.rcon.command(f"setblock {x} {y - 1} {z} minecraft:air replace")
        time.sleep(GAP)

        rate = self.recorder.rate
        onset = au.onset_index(clip)
        return Strike(
            label=label,
            peak=au.peak(clip),
            rms=au.rms(clip),
            balance=au.channel_balance(clip),
            frequency=au.dominant_frequency(clip, rate),
            onset=(onset / rate) if onset is not None else None,
            frames=len(clip),
        )


# --------------------------------------------------------------------------- #
# 各測定
# --------------------------------------------------------------------------- #


def measure_distance(rig: Rig, distances: list[int] | None = None) -> dict:
    """距離と音量の関係。``gain ≈ 1 − d/48`` が正しいかを確かめる。

    真上に置く。配置アルゴリズムが定位中央のときに使う向きと同じなので、
    ここで得た式がそのまま使える。
    """
    distances = distances or [2, 3, 4, 6, 8, 10, 12, 15, 18, 21, 24, 27, 30, 33, 36, 39, 42, 45, 47, 48, 50]
    points = []
    for d in distances:
        s = rig.strike(f"d={d}", (0, d, 0))
        points.append({"distance": d, **asdict(s)})
        print(f"    d={d:3d}  peak={s.peak:.5f}  rms={s.rms:.6f}"
              + ("  （無音）" if s.peak < 1e-4 else ""))
    return {"points": points}


def measure_instruments(rig: Rig) -> dict:
    """ブロック → 楽器の対応が本当に合っているか。

    抽出済みの音源と録音のスペクトルを比べて、同じ音かどうかを見る。
    ``heavy_core`` が本当にスネアになるか、銅4種が別音色かをここで確定させる。
    """
    results = []
    for name in INSTRUMENTS:
        s = rig.strike(name, (0, 4, 0), instrument=name, key=12)
        results.append({"instrument": name, "block": INSTRUMENTS[name].block, **asdict(s)})
        print(f"    {name:20s} peak={s.peak:.5f}  freq={s.frequency or 0:7.1f}Hz"
              + ("  ← 鳴っていない" if s.peak < 1e-4 else ""))
    return {"results": results}


def measure_pitch(rig: Rig, instrument: str = "harp") -> dict:
    """25段階のピッチが本当に ``2^((n-12)/12)`` になっているか。"""
    points = []
    for key in range(25):
        s = rig.strike(f"{instrument}[{key}]", (0, 4, 0), instrument=instrument, key=key)
        points.append({"key": key, **asdict(s)})
        print(f"    key={key:2d}  freq={s.frequency or 0:7.1f}Hz  peak={s.peak:.5f}")
    return {"instrument": instrument, "points": points}


def measure_panning(rig: Rig, offsets: list[int] | None = None) -> dict:
    """左右のずれが定位にどう効くか。プレイヤーは +X を向いている。"""
    offsets = offsets or [-24, -16, -12, -8, -4, -2, 0, 2, 4, 8, 12, 16, 24]
    points = []
    for dz in offsets:
        # 真横に置くと距離が変わるので、高さを一定にして横だけ動かす
        s = rig.strike(f"z={dz:+d}", (0, 4, dz))
        points.append({"dz": dz, **asdict(s)})
        print(f"    dz={dz:+4d}  balance={s.balance:+.3f}  peak={s.peak:.5f}")
    return {"points": points}


MEASUREMENTS = {
    "distance": ("距離と音量", measure_distance),
    "instruments": ("ブロック → 楽器の対応", measure_instruments),
    "pitch": ("25段階のピッチ", measure_pitch),
    "panning": ("左右の定位", measure_panning),
}


# --------------------------------------------------------------------------- #
# 実行
# --------------------------------------------------------------------------- #


def run(
    only: list[str] | None = None,
    out_dir: Path = Path("out/measure"),
    game_dir: Path | None = None,
    memory: str = "2G",
    wait: float = 600.0,
) -> Report:
    game_dir = game_dir or Path(__file__).resolve().parents[2] / ".minecraft"
    launcher = default_minecraft_dir()
    mc = resolve_version(launcher)
    root = game_dir / "measure"

    names = only or list(MEASUREMENTS)
    for n in names:
        if n not in MEASUREMENTS:
            raise ValueError(f"未知の測定: {n}（{', '.join(MEASUREMENTS)}）")

    tune_client_options(game_dir)
    print(f"■ クライアント設定を測定向きに調整: {game_dir / 'options.txt'}")
    print("  （音符ブロック以外を消音 / フォーカスが外れても止めない）")

    with Rig(root, game_dir, mc, memory=memory) as rig:
        print(f"\n■ サーバ起動: localhost:25566 ({mc})")
        print("\n" + "=" * 68)
        print("  Minecraft を起動して localhost:25566 に接続してください")
        print("  ランチャーのプロファイル: mcnb (音ブロック)")
        print("  接続したら放置で大丈夫です（位置合わせはこちらでやります）")
        print("=" * 68 + "\n")

        player = rig.wait_for_player(timeout=wait)
        print(f"■ 接続しました: {player}")
        rig.prepare()

        rig.recorder = au.Recorder()
        rig.recorder.start()
        print(f"■ 録音: {rig.recorder.device.name} / {rig.recorder.rate} Hz / {rig.recorder.channels} ch")

        report = Report(
            minecraft_version=mc,
            samplerate=rig.recorder.rate,
            device=rig.recorder.device.name,
        )
        for n in names:
            title, fn = MEASUREMENTS[n]
            print(f"\n■ {title}")
            report.sections[n] = fn(rig)

    out = Path(out_dir)
    report.save(out / "measurements.json")
    print(f"\n■ 保存: {out / 'measurements.json'}")
    return report


def analyze_distance(section: dict) -> str:
    """距離減衰の測定結果から、どのモデルが合うかを判定する。"""
    points = [p for p in section["points"] if p["peak"] > 1e-4]
    if len(points) < 4:
        return "有効な測定点が足りません（音が録れていない可能性）。"

    d = np.array([p["distance"] for p in points], dtype=float)
    a = np.array([p["peak"] for p in points], dtype=float)
    a = a / a.max()

    linear = np.clip(1.0 - d / MAX_HEARING, 0, None)
    linear = linear / linear.max()
    inverse = 1.0 / np.maximum(d, 1.0)
    inverse = inverse / inverse.max()

    def err(model: np.ndarray) -> float:
        return float(np.sqrt(np.mean((a - model) ** 2)))

    e_lin, e_inv = err(linear), err(inverse)
    silent = [p["distance"] for p in section["points"] if p["peak"] <= 1e-4]
    audible_limit = min(silent) if silent else None

    lines = [
        f"有効点 {len(points)} / 線形モデル(1−d/48) の誤差 {e_lin:.4f} / 距離の逆数モデルの誤差 {e_inv:.4f}",
        f"→ {'線形' if e_lin < e_inv else '逆数'}のほうが近い",
    ]
    if audible_limit:
        lines.append(f"聞こえなくなる距離: {audible_limit} ブロック以上")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 図にする（数値だけだと形が見えないので）
# --------------------------------------------------------------------------- #


def plot_report(report: Report, out_dir: Path) -> list[Path]:
    """測定結果をグラフにする。軸と単位を必ず入れる（読めない図は作らない）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # 軸ラベルが日本語なので、日本語が出るフォントを選ぶ。無ければ英語にフォールバック
    available = {f.name for f in font_manager.fontManager.ttflist}
    for candidate in ("Meiryo", "Yu Gothic", "MS Gothic", "Noto Sans CJK JP", "Hiragino Sans"):
        if candidate in available:
            plt.rcParams["font.family"] = candidate
            break
    plt.rcParams["axes.unicode_minus"] = False

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    section = report.sections.get("distance")
    if section:
        pts = [p for p in section["points"] if p["peak"] > 1e-6]
        d = np.array([p["distance"] for p in pts], dtype=float)
        a = np.array([p["peak"] for p in pts], dtype=float)
        if len(d) and a.max() > 0:
            a = a / a.max()
            fig, ax = plt.subplots(figsize=(7, 4.2))
            ax.plot(d, a, "o-", label="実測 (peak, 最大で正規化)")
            grid = np.linspace(1, max(d.max(), 48), 200)
            lin = np.clip(1 - grid / MAX_HEARING, 0, None)
            ax.plot(grid, lin / lin.max(), "--", label="線形 1 − d/48")
            inv = 1 / np.maximum(grid, 1)
            ax.plot(grid, inv / inv.max(), ":", label="距離の逆数 1/d")
            ax.set_xlabel("プレイヤーからの距離 (ブロック)")
            ax.set_ylabel("相対音量")
            ax.set_title("音符ブロックの距離減衰")
            ax.grid(alpha=0.3)
            ax.legend()
            path = out_dir / "distance.png"
            fig.tight_layout()
            fig.savefig(path, dpi=110)
            plt.close(fig)
            written.append(path)

    section = report.sections.get("pitch")
    if section:
        pts = [p for p in section["points"] if p.get("frequency")]
        k = np.array([p["key"] for p in pts], dtype=float)
        f = np.array([p["frequency"] for p in pts], dtype=float)
        if len(k):
            fig, ax = plt.subplots(figsize=(7, 4.2))
            ax.plot(k, f, "o-", label="実測")
            base = f[np.argmin(np.abs(k - 12))] if (k == 12).any() else f[0]
            ax.plot(k, base * 2 ** ((k - 12) / 12), "--", label="2^((n−12)/12)")
            ax.set_xlabel("note ステート (0-24)")
            ax.set_ylabel("周波数 (Hz)")
            ax.set_title(f"ピッチの実測 — {section.get('instrument', '')}")
            ax.set_yscale("log")
            ax.grid(alpha=0.3, which="both")
            ax.legend()
            path = out_dir / "pitch.png"
            fig.tight_layout()
            fig.savefig(path, dpi=110)
            plt.close(fig)
            written.append(path)

    section = report.sections.get("panning")
    if section:
        pts = section["points"]
        z = np.array([p["dz"] for p in pts], dtype=float)
        b = np.array([p["balance"] for p in pts], dtype=float)
        if len(z):
            fig, ax = plt.subplots(figsize=(7, 4.2))
            ax.plot(z, b, "o-")
            ax.axhline(0, color="k", lw=0.6)
            ax.axvline(0, color="k", lw=0.6)
            ax.set_xlabel("左右のずれ dz (ブロック、+Z が右)")
            ax.set_ylabel("定位 (−1 = 左, +1 = 右)")
            ax.set_title("左右配置とステレオ定位の対応")
            ax.set_ylim(-1.1, 1.1)
            ax.grid(alpha=0.3)
            path = out_dir / "panning.png"
            fig.tight_layout()
            fig.savefig(path, dpi=110)
            plt.close(fig)
            written.append(path)

    return written
