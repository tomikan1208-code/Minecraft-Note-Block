# Minecraft Note Block — 音源自動編曲システム

音源ファイル（mp3 / wav / flac）を渡すと、それを **Minecraft Java Edition の音符ブロック演奏として
可能な限り原曲に近く再現する** ワールド／スケマティックを自動生成する。

目指しているのは `Audio → MIDI → Note Block` ではなく、

```
Audio → 楽曲の理解 → Minecraft固有の再編曲 → Note Blocks
```

つまり **「音符に変換する」のではなく「音符ブロックという楽器で編曲しなおす」**。

対象: **Minecraft Java Edition 26.2**（2026-06-16 / 最新安定版）

---

## いまどこ

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | 既存技術の調査 | ✅ [docs/01_research.md](docs/01_research.md) |
| 1 | 基盤構築・音源抽出・ベースライン | 🚧 進行中 |
| 2 | Minecraft音響モデルの実測 | — |
| 3 | 評価系（目的関数） | — |
| 4 | **編曲最適化器**（本体） | — |
| 5 | 音声入力パイプライン | — |
| 6 | 出力と実機検証 | — |
| 7 | 統合 | — |

### Phase 1 でわかったこと

26.2 の `sounds.json` を解決した結果:

- **音符ブロック楽器は 20 種** — 従来の16種 + **26.1 で追加された trumpet 4種**
  （`trumpet` / `trumpet_exposed` / `trumpet_weathered` / `trumpet_oxidized`、銅の酸化段階ごとに別サンプル）
- `block.note_block.harp` が鳴らすのは `note/harp.ogg` **ではなく** `note/harp2.ogg`、
  `bass` は `note/bassattack.ogg`。`harp.ogg` / `bass.ogg` は使われていない旧ファイル
  （**hyperchoron のオフラインレンダラはここを取り違えている**）
- Mob ヘッド音6種のうち、**決定論的なのは `imitate.creeper` だけ**（`random/fuse` を pitch×0.5）。
  他の5種はサンプルが3〜5個あり再生ごとにランダムに選ばれるため、厳密な再現には使えない

---

## セットアップ

必要なもの:

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- Minecraft Java Edition 26.2 を **一度ランチャーで起動済み**であること（アセットのダウンロードのため）

```bash
uv sync
```

### 音符ブロック音源の抽出

Minecraft のアセットは再配布できないので、リポジトリには入っていない。
自分の `.minecraft` から取り出す:

```bash
uv run python -m mcnb.mcassets --out assets/mc
```

インストール済みバージョンの確認:

```bash
uv run python -m mcnb.mcassets --list-versions
```

`--version 26.2` でバージョンを固定できる。抽出結果は `assets/mc/manifest.json` に記録される
（イベント名・実サンプル名・SHA1・ランダム variant・pitch/volume 倍率）。

---

## 再生環境の2プロファイル

音の同時発音数について、出力を2系統用意する。

| プロファイル | 同時発音上限 | 必要なもの | 用途 |
|---|---|---|---|
| `vanilla` | 247（うちムード音8を除く） | なし | 配布用。誰でもそのまま鳴らせる |
| `enhanced` | 最大 4095 | [Raise Sound Limit Simplified](https://modrinth.com/mod/rsls)（MIT / クライアント側 / Fabric・NeoForge / 26.2対応） | 本命。レイヤリング・オクターブ重ね・疑似残響を積極的に使う |

`enhanced` ではサウンドチャンネルの上限が実質的に外れるため、
**制約は「濁って聞こえるかどうか」＝目的関数の複雑さペナルティに移る**。
これは本プロジェクトの中心戦略（1音を複数ブロックで作る）と直接噛み合う。

> 注意: サウンド上限を上げても、hyperchoron の構造が持つ **87音/tick** は
> 物理的な配置上の制約なので自動では外れない。`--max-distance` を広げるか、
> 独自の配置構造が必要になる（Phase 4 で扱う）。

---

## ライセンス

本リポジトリは **GPL-3.0-or-later**。

依存する [`litemapy`](https://github.com/SmylerMC/litemapy)（.litematic 出力に必要）が GPL-3.0 のため、
配布物全体が GPL-3.0 になる。

主要な依存とライセンス:

| 依存 | ライセンス |
|---|---|
| [hyperchoron](https://github.com/thomas-xin/hyperchoron) | MIT AND (Apache-2.0 OR BSD-2-Clause) |
| [pynbs](https://github.com/OpenNBS/pynbs) / mido / demucs / audio-separator | MIT |
| [mcschematic](https://github.com/Sloimayyy/mcschematic) / Basic Pitch | Apache-2.0 |
| librosa | ISC |
| **litemapy** | **GPL-3.0** |

### リポジトリに入れないもの

- **Minecraft のアセット**（`*.ogg`）— Mojang EULA。実行時に `.minecraft` から抽出する
- **音源分離・採譜モデルの重み** — UVR コミュニティ由来のものはライセンスが不明瞭

`.gitignore` で除外済み。

---

## 参考

- 目指す方向性: [【Minecraft】音ブロで「真っ黒ナイト・オブ・ナイツ」](https://www.youtube.com/watch?v=qeJ7NMr0cLk)
- 調査報告: [docs/01_research.md](docs/01_research.md)
