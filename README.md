# Minecraft Note Block — 音源自動編曲システム

音源ファイル（mp3 / wav / flac）を渡すと、それを **Minecraft Java Edition の音符ブロック演奏として
可能な限り原曲に近く再現する** データパックを自動生成する。

目指しているのは `Audio → MIDI → Note Block` ではなく、

```
Audio → 楽曲の理解 → Minecraft固有の再編曲 → Note Blocks
```

つまり **「音符に変換する」のではなく「音符ブロックという楽器で編曲しなおす」**。

対象: **Minecraft Java Edition 26.2**（2026-06-16 / 最新安定版）

---

## クイックスタート

```bash
uv sync
uv run mcnb setup                            # 音源抽出 + Fabric + 軽量化Mod
uv run mcnb build song.mp3 --world mcnb_song # 音源 → データパック → 専用ワールド
```

ランチャーで「mcnb (音ブロック)」プロファイルを起動 → ワールド `mcnb_song` を開く →

```
/function mcnb:panel   ← 操作盤（コマンドブロック+ボタン）が建ってそこへ飛ぶ
```

あとは足元の色で押す: **黄緑=設置 / 水色=演奏 / 赤=停止 / 黄=開始位置へ**。
コマンドでも同じ（`/function mcnb:build` `:play` `:stop` `:goto_start`）。

| コマンド | 何をするか |
|---|---|
| `mcnb setup` | 音源抽出 + Fabric + 軽量化Mod |
| `mcnb build <入力>` | 音源/MIDI/NBS → データパック。`--world NAME` で専用ワールドまで作る |
| `mcnb world --name NAME` | 演奏専用ワールドを新規生成（チートON/クリエイティブ/クラシックフラット） |
| `mcnb test` | テスト曲 1-10 を全部パイプラインに通す |
| **`mcnb verify <入力>`** | **headless サーバで実際に動かして検証する** |
| `mcnb info <nbs>` | NBS の中身を表示 |

既存のワールドは触らない。`mcnb world` が毎回まっさらな専用ワールドを作る。

---

## 演奏の仕組み（トロッコを使わない）

X 軸を時間軸に取った**まっすぐな廊下**を作る。トロッコもレールも使わない。

```
        ← 音量小 ────  プレイヤーの通り道  ──── 音量小 →
   ♪ ♪ ♪ ♪ ♪ ♪ ♪ ♪ ♪ ♪ |  ⇒ /tp で移動 ⇒  | ♪ ♪ ♪ ♪ ♪ ♪ ♪ ♪ ♪ ♪
   ▣ ▣ ▣ ▣ ▣ ▣ ▣ ▣ ▣ ▣ |                 | ▣ ▣ ▣ ▣ ▣ ▣ ▣ ▣ ▣ ▣
   左に定位                                        右に定位
```

- **tick t のノートは平面 `x = X0 + 3t` に置く**。プレイヤーは datapack から毎 tick `/tp` される
- 原点は **フラット地形の地表 `y = -60`**。音符ブロックは必ずその上に建つので、空中に浮かない
- **プレイヤーからの距離が音量**（`gain ≈ 1 − d/48`）、**左右のずれが定位**、真上は定位中央のまま距離だけ稼ぐ
- 音符ブロックは「直下が楽器ブロック・真上が空気」でないと鳴らないので、縦は 3 ブロック周期
- 発火は平面の 1 手前 (`x-1`) にレッドストーンブロックを `setblock` し、次の tick で `fill` して消す
- タイミングは `minecraft:tick` タグの関数 + マクロ。**リピーターを使わないので 20Hz が素で出る**

### なぜ `SPACING = 3` なのか

発火用のレッドストーンは `x-1` に置くが、これは `x-2` とも隣接する。
`SPACING=2` だと `x-2` が前 tick の音符ブロックになり**二重発火する**。
`SPACING=3` にすると `x-2` が必ず空になる。

---

## いまどこ

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | 既存技術の調査 | ✅ [docs/01_research.md](docs/01_research.md) |
| 1 | 基盤・音源抽出・実測 | ✅ [docs/02_measurements.md](docs/02_measurements.md) |
| **v0** | **音源 → データパック の貫通** | ✅ テスト1-10 が headless サーバで実機検証済み<br>実機で音が鳴ることも確認（[記録](docs/04_test_log.md)） |
| v1 | 実機測定リグ（RCON + 録音） | — |
| v2 | レンダラ改造（距離減衰・同時発音制限） | — |
| v3 | 目的関数 + テストランナー | — |
| v4 | **編曲最適化器**（本体） | — |
| v5 | 採譜補正 | — |
| v6 | ワールド生成 | ✅ `mcnb world`（チートON/クリエイティブ/ボイド） |

計画の詳細は [docs/03_plan.md](docs/03_plan.md)。

## 実機検証（`mcnb verify`）

「理論上動く」で止めないための仕組み。Mojang 公式のサーバ jar（SHA1検証つき）を
headless で起動し、RCON でデータパックを実際に動かして確かめる。

1. データパックが読み込めるか
2. `mcnb:build` が音符ブロックを本当に置けたか（ブロックステートまで一致するか）
3. `/tick freeze` + `/tick step` で 1 tick ずつ進めて、
   発火用レッドストーンが**正しい tick に正しい場所へ現れて消えるか**
4. サーバログにエラーが出ていないか

**これで実際に見つかったバグ（どれも無言で失敗していた）:**

| 症状 | 原因 |
|---|---|
| データパックが読み込まれない | `pack.mcmeta` に `min_format` / `max_format` が必須（`pack_format` だけだと弾かれる） |
| `setup` が丸ごと失敗 | **26.x でゲームルール名が全面的に変わっていた**（下表） |
| 音符ブロックが1個も置かれない | `/forceload add` は**同じ tick ではチャンクを読み込まない**。直後の `setblock` が "That position is not loaded" で失敗 |
| コマンドの応答がズレる | Minecraft の RCON は長い応答を 4096 バイトで分割する。1パケットしか読まないと次のコマンドが前の応答を受け取る |

### 26.x で変わったゲームルール名

| 旧 | 新 |
|---|---|
| `maxCommandChainLength` | `max_command_sequence_length` |
| `doDaylightCycle` | `advance_time` |
| `doWeatherCycle` | `advance_weather` |
| `doMobSpawning` | `spawn_mobs` |
| `commandModificationBlockLimit` | `max_block_modifications` |
| `commandBlockOutput` | `command_block_output` |
| `sendCommandFeedback` | `send_command_feedback` |

camelCase → snake_case になっただけでなく一部は改名されている。旧名は
「Incorrect argument」で**無言で失敗する**ので、実機で回すまで気づけなかった。

---

### v0 でわかっている問題

- **音そのものは自動検証できない。** サーバは音を鳴らさないので、聴感は人が聞くしかない
  （記録は [docs/04_test_log.md](docs/04_test_log.md)）
- hyperchoron の既定は 40Hz なので `-r 20` を明示しないと**倍速になる**（対処済み）
- hyperchoron の velocity 変換が対数寄りで、MIDI の 55 と 110（-6 dB）が NBS の 10 と 100（-20 dB）になる。**強弱が誇張される**
- 長い音符が再発音されない（Test 7）。1発で減衰しきる
- `snare` に `heavy_core` を使っている（砂は落下するため）。**実機で音が出るか未確認**
- 3分の曲だと廊下が 10,800 ブロックになる。チャンク読み込みが追いつくかは未検証

---

## 環境

`uv run mcnb setup` が全部やる。中身:

1. **音源抽出** — `.minecraft` から 26.2 の音符ブロック音源20種を取り出す（Mojang アセットは再配布不可のため実行時に抽出）
2. **Fabric Loader** — 公式 meta API の profile JSON を置くだけ。**インストーラ jar のダウンロード・実行はしない**
3. **軽量化 Mod** — Modrinth から SHA1 検証つきでダウンロード

| Mod | 役割 |
|---|---|
| fabric-api | 前提 |
| **lithium** | サーバ側 tick の最適化。datapack で大量の setblock を打つので効く |
| sodium | 描画の軽量化 |
| ferrite-core | メモリ削減 |
| immediatelyfast | 描画まわりの軽量化 |
| **rsls** | [Raise Sound Limit Simplified](https://modrinth.com/mod/rsls)。同時発音を 4095 まで |

ゲームディレクトリは **`<repo>/.minecraft`**（本番の `.minecraft` を汚さない）。
ランチャーに「mcnb (音ブロック)」プロファイルが作られる。

### 同時発音の2プロファイル

| | 上限 | 必要なもの |
|---|---|---|
| `vanilla` | 247 | なし |
| **`enhanced`** | **4095** | RSLS（導入済み） |

`--max-polyphony` で指定する（既定 200）。上限が外れると、制約は
**「濁って聞こえるか」＝目的関数の複雑さペナルティ**に移る。

---

## Phase 1 でわかったこと

26.2 の `sounds.json` を解決した結果:

- **音符ブロック楽器は 20 種** — 従来の16種 + **26.1 で追加された trumpet 4種**
  （銅の酸化段階ごとに別サンプル）
- `block.note_block.harp` が鳴らすのは `note/harp.ogg` **ではなく** `note/harp2.ogg`、
  `bass` は `note/bassattack.ogg`（**hyperchoron のオフラインレンダラはここを取り違えている**）
- Mob ヘッド音6種のうち**決定論的なのは `imitate.creeper` だけ**。他はランダム選択で再現不能
- 有音程で最も長く残るのは chime の **0.79 秒（約16 tick）**、最短は hat の 0.026 秒

---

## テスト

```bash
uv run mcnb test --regen
```

| # | テスト | 確かめること |
|---|---|---|
| 1 | 単音 | ピッチ・楽器選択・tick 整合 |
| 2 | 2音の和音 | 同時発音、strum の要否 |
| 3 | 3音の和音 | voicing 選択、濁りの発生点 |
| 4 | メロ+伴奏 | 主旋律と伴奏の音量差（距離配置） |
| 5 | ドラム | 打楽器のマッピング |
| 6 | 強弱 | 距離による velocity 制御の精度 |
| 7 | 短い残響 | 減衰と再発音 |
| 8 | 複数楽器 | 音色の重ね |
| 9 | 高速フレーズ | 20Hz 量子化の限界 |
| 10 | 音域 | 6オクターブの楽器切り替え |

```bash
uv run mcnb verify tests/inputs/04_melody_accomp.mid   # 1つを実機検証
```

聴感の記録は [docs/04_test_log.md](docs/04_test_log.md)。**音の良し悪しは人が聞くしかない**ので、
数値で確かめられることと耳でしか確かめられないことを分けて書いている。

---

## ライセンス

**GPL-3.0-or-later**。依存する [`litemapy`](https://github.com/SmylerMC/litemapy) が GPL-3.0 のため。

| 依存 | ライセンス |
|---|---|
| [hyperchoron](https://github.com/thomas-xin/hyperchoron) | MIT AND (Apache-2.0 OR BSD-2-Clause) |
| pynbs / mido / demucs / audio-separator | MIT |
| mcschematic / Basic Pitch | Apache-2.0 |
| librosa | ISC |
| **litemapy** | **GPL-3.0** |

### リポジトリに入れないもの

- **Minecraft のアセット**（`*.ogg`）— Mojang EULA。実行時に抽出する
- **`.minecraft/`** — usercache / logs / telemetry / saves は個人情報を含む
- **モデルの重み** — UVR 由来はライセンスが不明瞭

---

## 参考

- 目指す方向性: [【Minecraft】音ブロで「真っ黒ナイト・オブ・ナイツ」](https://www.youtube.com/watch?v=qeJ7NMr0cLk)
