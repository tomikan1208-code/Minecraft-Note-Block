# 調査報告書 — 音源→Minecraft音符ブロック自動編曲システム

**作成日**: 2026-08-21
**対象**: Minecraft Java Edition 26.2（2026-06-16 リリース、最新安定版）
**フェーズ**: Phase 0（実装前調査）— コードは未記述

---

## 0. 結論サマリ

### 0.1 一番重要な発見

**`hyperchoron`（MIT, Thomas Xin）が、当初の想定よりはるかに広い範囲を既に実装している。**

このプロジェクトが「新規実装が必要」と考えていた領域のうち、以下は**既に存在する**:

| 想定していた自作範囲 | 実際の状況 |
|---|---|
| 音源分離パイプライン | `hyperchoron/pcm.py` に実装済（DeEcho-DeReverb → BS-RoFormer → HTDemucs-ft → DrumSep の4段カスケード） |
| 6オクターブ対応・楽器自動切替 | `mappings.py` の `material_map` に14楽器クラス × 6オクターブのブロック表として実装済 |
| 音量→距離マッピング | 実装済（センターラインからの距離で velocity を表現） |
| 和音の strum / sustain 再発音 | 実装済 |
| pitch bend → grace note | 実装済 |
| 20Hz（フルtick）再生構造 | ピストン+葉ブロック回路で実装済 |
| litematic / mcfunction / nbt 出力 | 実装済 |
| **Minecraftサウンドのオフラインレンダリング** | **`render_nbs()` に実装済。実際のMC音源18種（`templates/Notes.zip`）をリサンプリング+定パワーパンで合成しWAV出力** |

**`render_nbs()` の存在が決定的に重要**です。これにより「Minecraftを起動して録音する」ことなく、
編曲候補 → 音声 → 原曲との距離計算 のループを **CPU上で高速に回せる**。
要件10の最適化ループは、これがあって初めて現実的になります。

### 0.2 したがって、本プロジェクトの新規性は「編曲最適化」に集中すべき

既存ツールが全て共有している欠陥は同一です:

> **どれも「音符 → 音符ブロック」の写像を貪欲（greedy）に決めており、
> 「Minecraftで鳴らした結果の音」を評価してフィードバックするループを持たない。**

hyperchoron ですら、`render_nbs()` は **出力の確認用** であって、編曲決定には使われていません。
ここが本プロジェクトの実装すべき中核です。

### 0.3 再利用 vs 新規実装の切り分け（結論）

| 領域 | 方針 | 根拠 |
|---|---|---|
| 音源分離 | **再利用**（`audio-separator`） | SOTAモデル（BS-RoFormer等）を統一APIで利用可。自作の余地なし |
| BPM/ビート推定 | **再利用**（`beat_this` + librosa） | 2024年以降のTransformer系がDBN後処理不要で高精度 |
| 採譜(AMT) | **再利用+補正**（Basic Pitch / YourMT3+ / librosa CQT） | 単体では不十分。後段の補正が必須（→ 新規） |
| MIDI/NBS I/O | **再利用**（`pynbs`, `mido`） | 枯れている |
| MC構造生成 | **再利用**（hyperchoron の litematic 出力） | ピストン回路等の redstone 知見は自作困難 |
| MC音響レンダラ | **再利用+改造**（hyperchoron `render_nbs`） | 距離減衰・247音制限のモデル化を追加する必要あり（一部新規） |
| **編曲最適化器** | **新規実装** | **これが本プロジェクトの中核。既存に存在しない** |
| **知覚的距離の目的関数** | **新規実装** | 既存の音楽類似度指標の組み合わせ。MIR系ライブラリを部品として使う |
| **採譜結果の原音照合補正** | **新規実装** | 要件4。既存パイプラインは採譜結果を無検証で信用している |
| 26.1 トランペット対応 | **新規実装** | 既存ツールは全て未対応（後述） |

---

## A. 既存ツール調査

### A-1. hyperchoron（★最重要）

- URL: https://github.com/thomas-xin/hyperchoron
- ライセンス: **MIT AND (Apache-2.0 OR BSD-2-Clause)**（`pyproject.toml` 記載）
- 言語: Python >= 3.10（+ 任意のRust拡張 `fastmidi`）
- 規模: Python 約7,450行（`minecraft.py` 1,970 / `midi.py` 1,058 / `text.py` 1,051 / `util.py` 1,041 / `mappings.py` 693 / `tracker.py` 625 / `pcm.py` 603）
- PyPI: `hyperchoron` 1.1.17

**入力**: `.hpc` `.mid` `.csv` `.nbs` `.org` `.xm`(WIP) `.wav/.flac/.mp3/.aac/.ogg/.opus/.m4a`（実装あり）+ DawVert経由の各種DAWフォーマット
**出力**: `.mid` `.csv` `.nbs` `.hpc` `.mcfunction` `.litematic` `.nbt` `.org` `.wav` 他

**Minecraft向けアルゴリズム（README + ソース確認済）**:

1. **6オクターブ楽器マッピング** — `mappings.py:73` の `material_map` は 14 の「楽器クラス」それぞれに低音→高音の6段階ブロック列を割り当て、音が現在の音域を超えると自動でブロック（＝楽器）を切り替える。
   例: Plucked クラス = `["bamboo_planks", "black_wool", "black_wool+", "amethyst_block+", "gold_block", "gold_block+"]`（`+` はオクターブ上シフトの内部記法）
2. **音量→距離** — プレイヤーが通る「センターライン」からの距離で velocity を表現。`--max-distance` で制御。panning情報があれば左右方向にも反映。
3. **ポリフォニー予算** — 構造上、同時最大 **87音**（`--max-distance` 依存）。超過時は「最も静かな音」と「サステイン用の中間音」から順に破棄。
4. **サステイン** — MC音源はワンショットで減衰するため、長い音符を分割して再発音。音量に応じて再発音間隔を適応。
5. **strum** — 和音を数tickずらして配置し、同時発音による突然のラウドネス増を回避。
6. **pitch bend → grace note** — MIDIのピッチベンドを装飾音に変換。`--microtones` でコマンドブロックによる真の微分音も可能（ただしサバイバル非合法になる）。
7. **フルtick(20Hz)** — ピストン+葉ブロック回路で **5 gametick 遅延** を作り、レッドストーン部品が通常扱えない奇数tickオフセットを実現 → 20Hz全解像度。
8. **テンポ整合** — 拍子記号ではなく **音符タイムスタンプの最大公約数** で MC の tick に同期。三連符・五連符に対応。候補が見つからない場合は非同期再生にフォールバック。
9. **ドラム** — バニラの3種（basedrum/snare/hat）だけでは潰し合うため、一部の打楽器を他楽器やMobヘッドにマッピングして音色バリエーションを確保。

**`render_nbs()`（`pcm.py:264`）— オフラインMC音響レンダラ**:

- `templates/Notes.zip` に MC の音符ブロック音源18種（harp, harp2, bass, bassattack, bd, snare, hat, guitar, flute, bell, icechime, xylobone, iron_xylophone, cow_bell, didgeridoo, bit, banjo, pling）を同梱
- 音程は `soxr_hq` リサンプリング。±12半音を超える場合は phase vocoder（`audiotsm`）を併用してフォルマント破綻を緩和
- panning は定パワー則 `t = (pan/100+1)·π/4`, `L = cos(t)`, `R = sin(t)`
- 36kHz でステレオ合成 → SoundFile 書き出し

> **注意**: `Notes.zip` は Mojang のアセットそのものと推定される。本プロジェクトで再配布する場合はライセンス上の問題があるため、**ユーザーのMinecraftインストールから `.jar` 内の note block 音源を抽出する**方式に置き換えるべき。

**弱点**:

- 26.1 で追加された **トランペット（銅ブロック）に未対応**（`material_map` に copper 系が無い）
- 音声入力パス（`load_raw`）は librosa の `pyin` / `hybrid_cqt` ベースで、深層学習採譜モデルを使っていない。コード中にも `# TODO: Find a better model to break down the remaining instruments` とある
- `render_nbs()` は **距離減衰と247音制限をモデル化していない**（velocity をそのまま振幅に使う）
- 編曲は決定論的ヒューリスティック。評価→再探索のループが無い

### A-2. Note Block Studio / Open Note Block Studio

- URL: https://noteblock.studio/ , https://github.com/OpenNBS/NoteBlockStudio
- ライセンス: **MIT**（OpenNBS, 2024）
- 言語: GameMaker Language (GML), GameMaker 2022.0.3 LTS
- 位置づけ: **NBSフォーマットの事実上の標準を定義しているソフト**。GUI音楽エディタ

**機能**:

- MIDIインポート（ファイル・MIDIキーボード両方）。note length対応（コーラス効果で長さを表現）
- エクスポート: `.mp3`/音声、**データパック**（ZIP化可、リソースパック併用で音域外の音も出力可能）、`.schematic`（MCEdit形式＝旧式）、MIDI
- リソースパックの `sounds.json` を読んでカスタム音符ブロック音源・ピッチシフトに追従する機能あり

**NBSフォーマット（v5、リトルエンディアン符号付き整数）**:

- 構成: Header(必須) / Note Blocks(必須) / Layers(任意) / Custom Instruments(任意)
- 文字列 = 32bit長 + 文字バイト列
- v2: レイヤーごとのステレオ（panning byte: 0=右, 100=中央, 200=左）
- v4: **ノートごとに velocity / panning / pitch(finetune) を保持**。ループ制御、レイヤーロック
- v5: カスタム楽器 18 → **240** に拡張。パス中のスラッシュ許可（サブフォルダ対応）。構造は v4 と互換
- 実効音量 = `(layer_volume × note_volume) / 100`、velocity は 0–100、note panning は -100〜+100

**評価**: **フォーマットとして採用価値が高い**。中間表現の一部として NBS を使えば、Note Block Studio / MidiMC / NoteBlockLib / nbswave など既存エコシステム全部と接続できる。ただしGML製の本体アプリ自体は自動パイプラインには組み込めない（GUI前提）。

### A-3. MidiMC

- URL: https://www.midimc.com/ （直接fetchは403。検索結果から把握）
- ライセンス: **クローズドソース**。基本無料 + Pro 一括 $9.99
- 入力: MIDI, NBS
- 出力: **Litematica(.litematic) / WorldEdit(.schem) / データパック / NBS / MIDI / MP3・WAV**
- 機能: ブラウザ完結。マルチトラックピアノロール、マクロ、フェード、半音シフト、ループ再生、音声プレビュー。「AIが2オクターブ制約に合わせて音を賢くシフトする」と謳う。チューニング済みレッドストーンクロック、**ステレオカラム**、コマンドブロックトリガを生成
- **対象バージョン: Java 1.20.1**（＝26.2 から見て大幅に古い）

**評価**: **参考にはなるがコードは再利用不可**（クローズドソース）。出力フォーマットの網羅性と「ステレオカラム」という配置概念は設計の参考にする。

### A-4. NoteBlockLib / NoteBlockTool (RaphiMC)

- URL: https://github.com/RaphiMC/NoteBlockLib , https://github.com/RaphiMC/NoteBlockTool
- ライセンス: **LGPL-3.0**
- 言語: Java（Maven Central 配布）
- 読み込み: `.nbs` `.mid` `.txt` `.mcsp` `.mcsp2` / 書き出し: `.nbs` `.txt` `.mcsp2`
- 特徴: **NBS全バージョン(0–6)対応**、Tempo Changer対応、MIDIインポートで velocity/panning 対応、**Black MIDI対応**、移調・tick速度リサンプリング・楽器置換・ノート重複除去、`SongPlayer` によるリアルタイム再生

**評価**: Java製のためPythonパイプラインへの直結は不便だが、**NBS v0–v6の完全な仕様実装のリファレンス**として価値が高い。LGPL-3.0 なので動的リンク（別プロセス/CLI呼び出し）なら自作コードのライセンスに影響しない。

### A-5. beats-to-blocks

- URL: https://github.com/dustinlaa/beats-to-blocks
- ライセンス: **明記なし（＝再利用不可と扱うべき）**
- 出自: NJIT CS 485 (Machine Listening) の学生プロジェクト（Dr. Mark Cartwright指導、4名）
- パイプライン: Demucs/Spleeter → MT3/Basic Pitch → MIDI結合 → NBS変換 → WAV合成検証
- 実体: Jupyter Notebook 2本。Google Drive前提。**1分のWAVに1時間以上**
- 対象: Minecraft 1.20.2

**評価**: **アプローチは本プロジェクトとほぼ同じ方向性**。ただしPoC止まりで、「MIDIをNBSに置くだけ」であり編曲最適化は無い。**「素朴な MT3→NBS 変換ではどこまでしか行かないか」のベースライン**として参照する価値がある。ライセンス未記載のためコードのコピーは不可。

### A-6. その他

| ツール | ライセンス | 用途 | 評価 |
|---|---|---|---|
| `pynbs` (OpenNBS) | **MIT** | NBS読み書き（Python） | **採用**。デファクト |
| `nbswave` (Bentroen) | **MIT** | NBS → 音声レンダリング | 代替レンダラ候補。FFmpeg必須、hyperchoron より遅い見込み |
| `litemapy` | **GPL-3.0** | .litematic 読み書き（Python） | 採用可だが **GPL汚染に注意**（後述） |
| `mcschematic` | **Apache-2.0** | .schem (Sponge) 生成 | 採用候補。GPL回避したい場合の第一選択 |
| `amulet-core` | 要確認 | .mca ワールド直接編集 | ワールド直生成が必要になった場合の候補 |
| `nbtlib` | MIT系 | NBT読み書き | 採用 |
| `mido` | **MIT** | MIDI I/O | 採用 |
| `nbs2schematic` (euichan41) | 要確認 | NBS→litematic | 同時発音36音まで。hyperchoron(87音)に劣る |
| `nbs-converter` (jazziiRed) | 要確認 | NBS→schematic | pynbs + mcschematic の薄いラッパ。参考実装 |
| HarmonAI | — | AIエージェントが歩いて音符ブロックを叩く | 方向性が違う（AnthemScore=商用ソフト依存） |
| MrGarretto MIDI→Command | — | コマンド生成 | 古い |

---

## B. 既存研究調査

### B-1. 音源分離（Music Source Separation）

**2026年時点のSOTA**:

| モデル | MUSDB18-HQ 平均SDR | 特徴 |
|---|---|---|
| **BS-RoFormer** | **約 9.80 dB** | 帯域分割 + RoPE Transformer。SDX23優勝系統。**現行SOTA** |
| Mel-Band RoFormer | 約 9.7 dB | メル帯域分割版 |
| HTDemucs (ft) | 約 9.20 dB | 時間領域+周波数領域ハイブリッド + Transformer |
| SCNet | — | 複素U-Net |
| Demucs v3 | 約 7.8 dB | |
| Spleeter | 約 5.9 dB | 2019年。**現在では明確に劣る。採用しない** |
| Open-Unmix | 約 5.3 dB | 同上 |

**実務上の結論**: 単一モデルではなく **カスケード** が有効。hyperchoron が採用している構成が実際に良い:

```
原曲
 └─ UVR-DeEcho-DeReverb        → 残響除去（採譜精度が大幅に上がる）
     └─ BS-RoFormer (ep317)     → Vocals / Instrumental
         └─ HTDemucs-ft         → Vocals / Drums / Bass / Other
             └─ MDX23C-DrumSep  → Kick / Snare / Toms / HH / Ride / Crash
```

`audio-separator`（MIT, karaokenerds）がこれら全てを統一APIで扱える。**これを採用する。**
※ 個別チェックポイント（UVRコミュニティ製 `.ckpt`）のライセンスは **モデルごとに要確認**。コードはMITでも重みは別ライセンスのことがある。

**6-stem分離**（vocals/drums/bass/guitar/piano/other）は `htdemucs_6s` があるが精度が低く、hyperchoron でもコメントアウトされている。**Other stem をさらに分解する良いモデルは2026年時点でも未解決。**

### B-2. 自動採譜（Automatic Music Transcription）

| モデル | 種別 | ライセンス | 評価 |
|---|---|---|---|
| **Basic Pitch** (Spotify) | CNN、汎用ピッチ+オンセット | Apache-2.0 | 軽量・高速・安定。ピッチベンド出力可。**単音〜中程度のポリフォニーで実用的** |
| **MT3** (Google) | Transformer、マルチ楽器トークン化 | Apache-2.0 | マルチ楽器。ただし **楽器リーク**（同じ音が複数楽器に分散）が問題 |
| **MR-MT3** (arXiv 2403.10024) | MT3 + メモリ保持機構 | 要確認 | 楽器リークを緩和。onset F1 改善 |
| **YourMT3+** (arXiv 2407.04822) | MT3 + PerceiverTF SCA + MoE | **要精査（NC条項の可能性）** | マルチAMTベンチマークでMT3/PerceiverTFを有意に上回る。パラメータ増は2.5%未満 |
| hFT-Transformer | 階層周波数-時間Transformer | — | ピアノ特化で高精度 |

**重要な知見**: 2025年のAMTチャレンジでも「MT3を超える精度」が課題設定になっている ＝ **AMTは依然として未解決問題**。採譜結果をそのまま信じる設計は誤り（要件4の指摘は正しい）。

**本プロジェクトの方針**: stem ごとに最適なモデルを使い分ける。

- Bass stem → 単音前提の pYIN / Basic Pitch（hyperchoronと同様）
- Drums stem → 分離済み6要素の onset 検出のみ（ピッチ不要）
- Vocals stem → Basic Pitch（メロディ抽出。★除去するが **主旋律の根拠として保持**）
- Other stem → CQT + Basic Pitch/YourMT3+ の併用 + **原音照合による補正**

### B-3. ビート・テンポ推定

| 手法 | 年 | 評価 |
|---|---|---|
| `madmom` (RNN + DBN) | 2016 | 長年の標準。まだ実用的だがメンテが停滞、新しい Python/numpy で問題が出やすい |
| **Beat This!** | 2024 | Transformer。**DBN後処理不要** ＝ 拍子変化・大きなテンポ変動に強い。現行推奨 |
| BEAST | 2024 | ストリーミングTransformer（オンライン用途） |
| BeatNet | 2021 | CRNN + パーティクルフィルタ |
| `librosa.beat.beat_track` | — | 軽量。hyperchoron が使用。**精度は上記に劣る** |

**方針**: `beat_this` を第一候補、`librosa` をフォールバックに。
Minecraftは 20 tick/s 固定なので、**BPMの推定誤差は曲全体でのズレとして蓄積する**。
拍位置の系列（beat grid）を直接使って tick 量子化する方が、BPM単一値より安全。

### B-4. 音楽・音響類似度の評価指標

| 指標 | 何を測るか | ライブラリ |
|---|---|---|
| Mel spectrogram L1/L2 距離 | スペクトル包絡の一致 | `librosa` |
| MFCC 距離（DTW可） | 音色の一致 | `librosa`, `scipy` |
| Chroma cosine 類似度 | **和声（ピッチクラス）の一致** | `librosa` |
| Onset F1 / 距離 | **リズムの一致** | `mir_eval` |
| Beat / tempo alignment | テンポ整合 | `mir_eval` |
| Spectral centroid / contrast / flatness | 音色の明るさ・厚み | `librosa` |
| Loudness (LUFS / RMS envelope) 相関 | **強弱・ダイナミクスの一致** | `pyloudnorm`, `librosa` |
| **FAD (Fréchet Audio Distance)** | 埋め込み分布の距離（VGGish/CLAP/PANNs） | `frechet-audio-distance`, `fadtk` |
| CLAP score | テキスト-音声類似 | LAION-CLAP |
| SDR / SI-SDR | 波形一致 | `museval`, `torchmetrics` |

**重要な設計上の注意**（要件9の指摘は正しい）:

- **SDR/波形一致は使ってはいけない**。MC音源は原曲と全く違う音色なので波形は根本的に一致しない
- **FADは単一ペアの評価には不適**（分布間距離のため、統計的に意味を持つには多数サンプルが必要）
- 単一指標最適化は破綻する。例: chroma だけ最適化すると、リズムが崩れても和音が合っていれば高得点になる

**採用する目的関数の設計**（詳細は E-4）:

```
L = w_h·(1 - chroma_cos) + w_r·onset_distance + w_d·loudness_envelope_distance
    + w_t·mfcc_dtw_distance + w_m·melody_pitch_error + λ·complexity_penalty
```

重みは **小テスト（Test 1-9）で人間の主観評価と相関するようキャリブレーションする**。

---

## C. Minecraft側の制約（Java Edition 26.2 基準）

### C-1. バージョン状況（★重要な前提の変更）

- **Java Edition の最新安定版は `26.2`（2026-06-16）**
- **バージョン番号体系が変わった**: `1.21.x` の後は `1.22` ではなく **`26.1`（年ベース）**
  - 26.1: 2026-03-24 / 26.1.1: 04-01 / 26.1.2: 04-09 / 26.2: 06-16
  - 26.3 は Q3 2026 予定（スナップショット中）
- 26.2 はレンダリング最適化・安定性中心

**→ 既存ツールの対象バージョンは全て古い**: MidiMC=1.20.1、beats-to-blocks=1.20.2、hyperchoron=2025年時点の1.21系。**「最新版対応」は自分で埋める必要がある。**

### C-2. 音符ブロックの基本仕様

- **音階**: 1ブロックあたり **25段階（use count 0–24）= 2オクターブ**。再生倍率 = `2^((use_count - 12) / 12)`。use_count=12 が基準(1.0)、24 で 2.0（1オクターブ上）。25回目で0に戻る
- **楽器**: **直下のブロック**で決まる
- **発音条件**: **真上が空気（または Mob ヘッド）** であること
- **可聴距離**: **48ブロック**（通常のブロック音は16ブロック）。離れるほど小さくなる
- **音源**: ワンショットのモノラルOGG。**サステイン機構は無い**。例: `harp.ogg` は約 **0.6秒**。長い音は再発音するしかない
- 上に Mob ヘッドを置くとMobの鳴き声を再生できる（＝実質的な音色拡張）

### C-3. 楽器一覧と音域（26.1以降、トランペット追加後）

| 楽器 | 直下のブロック | 音域 | 備考 |
|---|---|---|---|
| harp（ピアノ） | 上記以外すべて（デフォルト） | F♯3–F♯5 | |
| bass | 木材系（原木・板材等） | F♯1–F♯3 | 最低音域 |
| didgeridoo | カボチャ | F♯1–F♯3 | 最低音域 |
| guitar | 羊毛 | F♯2–F♯4 | |
| iron_xylophone（ビブラフォン） | 鉄ブロック | F♯3–F♯5 | |
| bit（矩形波シンセ） | エメラルドブロック | F♯3–F♯5 | |
| banjo | 干草の俵 | F♯3–F♯5 | |
| pling（60年代エレピ） | グロウストーン | F♯3–F♯5 | |
| **trumpet** ★26.1新規 | **銅ブロック（酸化段階別に音色が変わる）** | F♯3–F♯5 | **既存ツール全て未対応** |
| flute | 粘土・ハニカムブロック | F♯4–F♯6 | |
| cow_bell | ソウルサンド | F♯4–F♯6 | |
| bell（グロッケン） | 金ブロック | F♯5–F♯7 | |
| chime | 氷塊 (packed ice) | F♯5–F♯7 | |
| xylophone | 骨ブロック | F♯5–F♯7 | 最高音域 |
| basedrum（バスドラム） | 石系 | 無音程 | |
| snare | 砂・砂利 | 無音程 | |
| hat（ハイハット） | ガラス系 | 無音程 | |

> **2026-08-21 追記 — 実機のアセットで確定した**（詳細は [02_measurements.md](02_measurements.md)）
>
> - 26.2 の `sounds.json` を解決した結果、**音符ブロック楽器は20種**。
>   トランペットは酸化段階ごとに**独立したサウンドイベント4種**
>   （`trumpet` / `trumpet_exposed` / `trumpet_weathered` / `trumpet_oxidized`）＝ **4つの別音色として使える**
> - `block.note_block.harp` の実サンプルは `note/harp.ogg` ではなく **`note/harp2.ogg`**、
>   `bass` は **`note/bassattack.ogg`**。`harp.ogg` / `bass.ogg` は未参照の旧ファイル。
>   **hyperchoron のレンダラはこの2つを取り違えている** → Phase 2 で修正
> - Mob ヘッド音6種のうち**決定論的なのは `imitate.creeper` のみ**。
>   他5種はサンプルが3〜5個ありランダム選択されるため厳密な再現に使えない
> - 全サンプルがモノラル（OpenAL 3D定位の前提と一致）。有音程で最も長く残るのは chime の
>   **0.79秒（約16 tick, -40dB）**、最短は hat の 0.026秒

**合計ピッチ音域**: F♯1 – F♯7 = **6オクターブ**（楽器を切り替えることで到達）
**同一音高を出せる楽器が複数ある** ＝ 音色選択の自由度がある（例: F♯3–F♯5 は harp / iron_xylophone / bit / banjo / pling / trumpet / guitar上端 / flute下端 / bass上端 …）
→ **これが「1音＝1ブロックを捨てる」ための最大のリソース**

**要検証**: 26.1のトランペットの酸化段階バリエーション数（銅/露出銅/風化銅/酸化銅の4種か）、錆止め(waxed)銅が有効か、音域が本当に F♯3–F♯5 か。**実機で確認する。**

### C-4. 音量・空間・強弱

- **プレイヤーとの距離が唯一の連続的な音量制御手段**（バニラでは note block 自体に音量パラメータが無い）
- 可聴上限距離 = `max(volume × 16, 16)`。音符ブロックは volume=3.0 → **48ブロック**
- 減衰は **線形（OpenAL linear clamped 想定）**、概ね `gain ≈ 1 − d/48`
  - **★要実機検証**。この式が正しければ、距離 d を選ぶことで velocity を **48段階以上の分解能**で制御できる
- **音源はモノラル** → OpenAL の3D定位が効く → **プレイヤーから見た左右配置がそのままステレオパンになる**
- `/playsound` を使う場合: volume 任意、**pitch は 0.5–2.0 にクランプ**（0.5未満は0.5扱い）
  → データパック方式なら音量を直接指定できるが、pitch は1オクターブ幅しか無いので楽器切替は依然必要

### C-5. タイミング

- **game tick = 20 Hz（50 ms）**
- レッドストーンリピーターの最小遅延 = 2 gametick（1 redstone tick）→ **素直に作ると 10 Hz しか出ない**
- hyperchoron は **ピストン+葉ブロック回路で5 gametick遅延** を作り、偶数tick制約を外して **20 Hz** を実現
- **50 ms 未満のタイミング差はバニラでは表現できない**（要件8の「数ms〜数十msの時間差」は 1 tick = 50 ms が最小粒度）
  - `/playsound` をコマンドブロックで使う場合も tick 粒度は同じ
  - → **「微小な時間差」は 1 tick（50ms）刻みでしか作れない**。これは重要な制約

### C-6. 同時発音数 — 2プロファイル方式を採る（方針決定済み）

- **バニラのサウンドチャンネル上限: 255（うち一般音 247、ムード音 8）**
- 超えると音が切れる（MC-1538 として古くから知られる）
- hyperchoron の構造上のポリフォニー予算は **87音/tick**

**方針**: サウンド上限を外す Mod の使用を前提にしたうえで、出力を2系統用意する。

| プロファイル | 同時発音上限 | 必要なもの | 用途 |
|---|---|---|---|
| `vanilla` | 247 | なし | 配布用。誰でもそのまま鳴らせる |
| `enhanced` | **最大 4095** | Raise Sound Limit Simplified | **本命**。レイヤリング・オクターブ重ね・疑似残響を積極的に使う |

**Raise Sound Limit Simplified (RSLS)** — https://modrinth.com/mod/rsls
- ライセンス **MIT**、**クライアント側のみ**、Fabric / Forge / NeoForge 対応
- **26.2 対応済み**（1.2.2、2026-07-16 リリース）。26.1.x / 1.21.x / 1.20.x も対応
- 上限を **4095** に引き上げ、静的ソースとストリーミングソースの配分を設定可能
- サウンド処理をレンダースレッドから外すため、音が多いときのFPSも改善する
- 設定は `.minecraft/config/rsls.properties`

**設計上の含意**:

1. `enhanced` では**チャンネル上限が実質的に制約でなくなる**。
   代わりに **「聴感上濁るかどうか」＝目的関数の複雑さペナルティ `λ_cost` が唯一の制約**になる。
   これは本プロジェクトの中心戦略（1音を複数ブロックで作る）と直接噛み合う。
   バニラ247のままだと、密なパッセージでレイヤリングを使う余裕がそもそも無い。
2. ただし **hyperchoron の 87音/tick は物理的な配置構造の制約**であり、
   Mod を入れても自動では外れない。`--max-distance` を広げるか、独自の配置構造が必要（Phase 4）。
3. レンダラ（`render_nbs` 改）は**両プロファイルをモデル化する**。
   `vanilla` では247超過時の音の欠落を再現し、`enhanced` では再現しない。
   ＝ 同じ編曲でも2つの音が予測でき、どちらを目的関数に使うか選べる。

### C-7. 配置方式の比較

| 方式 | 長所 | 短所 | 評価 |
|---|---|---|---|
| **.litematic**（Litematicaモッド） | 大規模構造を安定して配置。hyperchoron が対応済。プレビュー可 | クライアントMod必須 | **第一候補** |
| **.schem**（Sponge v3 / WorldEdit） | サーバ側で貼れる。仕様が公開・安定 | WorldEdit必須 | **第二候補**（`mcschematic`, Apache-2.0） |
| **.nbt**（structure block） | バニラ機能 | **サイズ上限が厳しく実用不可**（hyperchoron README も言及） | 不採用 |
| **.mcfunction / データパック** | Mod不要。`/setblock` 列挙 | `maxCommandChainLength` を上げる必要。長い曲で切れる | 補助的に採用 |
| `/playsound` データパック | **音量・位置を直接指定でき、note blockの制約を超えられる** | 「音符ブロック演奏」ではなくなる。pitch は 0.5–2.0 | **比較用の上限ベースラインとして実装価値あり** |
| **.mca / ワールド直生成** | 何でもできる | バージョン追随コストが高い（`amulet-core` 依存） | 後回し |
| サーバプラグイン (NoteBlockAPI 等) | 動的再生 | 「ワールド内に構造を作る」目的から外れる | 不採用 |

**Sponge Schematic v3**（2021-05-04）: 3Dバイオーム対応、varint明確化。modded block state も表現可。

---

## D. 再利用可能なコードとライセンス整理

| プロジェクト | ライセンス | 使い方 | 自作コードへの影響 |
|---|---|---|---|
| hyperchoron | MIT AND (Apache-2.0 OR BSD-2-Clause) | ライブラリとして import / 部分改変 | **なし**（帰属表示のみ） |
| pynbs | MIT | import | なし |
| nbswave | MIT | import（代替レンダラ） | なし |
| mcschematic | Apache-2.0 | import | なし（NOTICE要確認） |
| **litemapy** | **GPL-3.0** | import | **⚠ 配布時にGPL-3.0伝播**。hyperchoronの依存なので実質不可避 |
| nbtlib | MIT | import | なし |
| mido | MIT | import | なし |
| librosa | ISC | import | なし |
| demucs | MIT | import | なし |
| audio-separator | MIT | import | なし（**モデル重みは別途要確認**） |
| Basic Pitch | Apache-2.0 | import | なし |
| MT3 | Apache-2.0 | 参照 | なし |
| YourMT3+ | **要精査（NC条項の可能性）** | 保留 | 商用不可の可能性 |
| NoteBlockLib | LGPL-3.0 | **別プロセスCLI呼び出しのみ** | 動的利用なら影響なし |
| Note Block Studio | MIT | 仕様参照 | なし |
| beats-to-blocks | **記載なし** | **参照のみ、コピー不可** | — |
| MidiMC | クローズド | 参照のみ | — |
| MC音源 `.ogg` | **Mojang EULA** | **再配布不可。ユーザーのjarから抽出する** | ⚠ |

### ⚠ ライセンス上の重要な判断

1. **litemapy が GPL-3.0** で、hyperchoron が依存している。本プロジェクトを配布する場合、**全体が GPL-3.0 になる**。
   - 回避したい場合: litematic 出力を捨てて `mcschematic`（Apache-2.0）の `.schem` に一本化する
   - **推奨**: 個人利用・研究目的なので当面は GPL-3.0 で進め、README に明記する
2. **Minecraft の音源ファイルは再配布しない**。`%APPDATA%/.minecraft/` のバージョンjar および `assets/objects/` から実行時に抽出するスクリプトを用意する。これはライセンス的に安全で、かつ **常に最新バージョンの音源（トランペット含む）を使える**という実利もある
3. **分離モデルのチェックポイント**は UVR コミュニティ由来のものが多く、ライセンスが不明瞭。研究・個人利用に留め、成果物に重みを同梱しない

---

## E. 推奨アーキテクチャ

### E-1. 全体パイプライン

```
song.mp3 / wav / flac
    │
    ├─[1] 前処理: ラウドネス正規化, ステレオ保持, 44.1kHz
    │
    ├─[2] 音源分離 (audio-separator)
    │      DeEcho-DeReverb → BS-RoFormer(vocal/inst) → HTDemucs-ft(4stem) → DrumSep(6要素)
    │      出力: vocals, drums{kick,snare,tom,hh,ride,crash}, bass, other
    │
    ├─[3] リズム解析 (beat_this / librosa)
    │      beat grid, downbeat, BPM, 拍子
    │
    ├─[4] 採譜 (stem別に手法を変える)
    │      bass   → pYIN (単音)
    │      vocals → Basic Pitch (メロディ抽出。★除去するが「主旋律の根拠」として保持)
    │      other  → Basic Pitch + CQT + (YourMT3+)
    │      drums  → onset検出のみ
    │
    ├─[5] ★採譜補正 (新規実装)
    │      候補ノート集合を原音の CQT/chroma と照合し、
    │      オクターブ誤り・幻ノート・タイミングずれを修正
    │
    ├─[6] 楽曲理解表現 (Musical Representation) ← 中間JSON
    │      note{onset, dur, pitch, velocity, stem, role, confidence}
    │      + 和音進行, 主旋律ライン, ダイナミクス包絡, セクション構造
    │
    ├─[7] ★Minecraft編曲最適化器 (新規実装・本プロジェクトの中核)
    │      (a) tick量子化 (beat gridベース, 20Hz)
    │      (b) 各音符に対する「音符ブロック構成」候補生成
    │          基音 / オクターブ重ね / 5度 / 3度 / 別楽器レイヤ /
    │          先行音 / 遅延音 / 残響音
    │      (c) 距離配置による velocity 割当
    │      (d) ポリフォニー予算内での探索 (beam search + 局所改善)
    │            ↕ 評価ループ
    ├─[8] ★MC音響レンダラ (hyperchoron render_nbs を改造)
    │      音源18種+ / 距離減衰 / 定パワーパン / 247音制限
    │            ↓
    ├─[9] ★知覚的距離の目的関数 (新規実装)
    │      chroma / onset / loudness envelope / MFCC-DTW / melody
    │            ↓ (loop [7]→[8]→[9])
    │
    └─[10] 出力
           .nbs (中間・エコシステム互換)
           .litematic (第一候補) / .schem / データパック
           レンダリングWAV / 解析JSON / 比較レポート
```

### E-2. 「1音＝1ブロック」を捨てるための設計

各入力音符 n に対し、**Voicing候補集合 V(n)** を生成する。

```python
Voicing = list[NoteBlockEvent]
NoteBlockEvent = (tick_offset, instrument, pitch, distance, side)
```

候補テンプレート（**固定ルールではなく、コスト付き候補として扱う**）:

| テンプレート | 内容 | 主な用途 |
|---|---|---|
| `SINGLE` | 基音1個 | 密度が高い箇所、内声 |
| `OCT_UP` / `OCT_DOWN` | 基音 + オクターブ | 厚み、低音の輪郭補強 |
| `FIFTH` | 基音 + 完全5度 | 倍音の擬似再現（第3倍音） |
| `LAYER` | 同音高を別楽器で重ねる | 音色合成（例: harp + bit で持続感） |
| `ATTACK` | 短い先行音（1tick前・別楽器・小音量） | アタック感、パーカッシブな立ち上がり |
| `TAIL` | 1–3tick後に小音量で同音 | 残響感 |
| `SUSTAIN` | n tick おきに減衰しながら再発音 | 長い音符 |
| `STRUM` | 和音を1tickずつずらす | ラウドネス爆発の回避、ギター感 |
| `DETUNE` | 微妙に違う楽器で同音（サンプルが違うので位相がずれる） | コーラス感 |

**探索**: 音符列を時間順にビームサーチ。状態 = (使用中のポリフォニー予算, 直前のvoicing, 累積コスト)。コスト = 目的関数の局所寄与 + ブロック使用数ペナルティ。

**なぜビームサーチか**: 遺伝的アルゴリズムや強化学習は、評価1回あたりのレンダリングコスト（音声合成）を考えると割に合わない。時間方向にほぼマルコフ的な構造なので、**ビームサーチ + 区間ごとの局所改善**で十分。要件10の指摘（「単純なアルゴリズムで十分ならそちらを優先」）に従う。

### E-3. 強弱の実現手段（優先順）

1. **距離** — `gain ≈ 1 − d/48`。連続的で最も自然。**主力**
2. **重ね数** — 同音を2〜3個鳴らすと +3〜5 dB 相当。距離では届かない強さを出す
3. **楽器選択** — bit/bell は明るく前に出る、bass/didgeridoo は沈む。音色で相対的な前後感を作る
4. **時間差** — 1 tick ずらすとアタックが分散して柔らかくなる（＝弱く聞こえる）
5. **左右配置** — 定位を分けると各パートが聴き分けやすくなり、実効的な「明瞭さの強弱」になる

**crescendo / decrescendo** は、距離を連続的に変える（＝ブロックを階段状に配置する）ことで実現可能。
**ghost note** は最遠距離（d ≈ 45 前後）に配置。

### E-4. 目的関数

```
L(candidate) =
    w_harmony  · (1 − chroma_cosine(orig, rendered))
  + w_rhythm   · onset_envelope_distance(orig, rendered)
  + w_dynamics · loudness_envelope_L1(orig, rendered)
  + w_timbre   · mfcc_dtw(orig, rendered)
  + w_melody   · melody_pitch_error(orig_melody, rendered)
  + λ_cost     · (note_block_count / budget)
```

- 全て **短時間フレーム単位** で計算し、区間ごとに最適化する（曲全体を一括最適化しない）
- 重み `w_*` は Test 1–9 の小テストで **人間の主観評価と相関するようキャリブレーション**
- **λ_cost を入れる理由**: 音を増やせば大抵の指標は良くなるが、実際には濁って聞こえるため

### E-5. 実装スタック

- Python 3.13（確認済: `Python 3.13.14`）
- パッケージ管理: `uv`（torch 系の重い依存を扱うため）
- **注意**: 現環境の Java は **1.8.0_332**。Minecraft 26.2 の実行には **Java 21+ が必要**。実機検証フェーズまでに JDK 21/25 を導入する

---

## F. 原理的にできないこと / 妥協が必要な点

要件17-F「通常の音符ブロックでは原音を完全には再現できない部分」の明確化:

| # | 制約 | 影響 | 緩和策 |
|---|---|---|---|
| F1 | **音源がワンショット（0.6秒前後）で減衰する。サステインが無い** | 弦楽器・オルガン・パッド・ロングトーンが原理的に再現不能 | n tick ごとに再発音（トレモロ的になる）。音量を落として重ねる |
| F2 | **時間分解能が 50 ms (1 tick)** | 「数ms〜数十msの時間差」による位相・コーラス効果は作れない。速いフレーズ（32分音符 @ 180BPM = 41ms）は量子化で潰れる | 高速フレーズは音を間引くか、テンポを整数倍で近似 |
| F3 | **音量が位置で決まる ＝ プレイヤーが動くと全部変わる** | 「プレイヤーがこの1点にいる」前提でしか正しい音量にならない | 演奏中プレイヤーを固定位置に置く（トロッコ/バリア）。hyperchoron も同じ前提 |
| F4 | **音色が18種類（+Mobヘッド）に固定。EQ/フィルタ/エンベロープ加工が一切できない** | ボーカル、歪みギター、ブラスセクション等は「似た音」すら存在しない | 複数楽器レイヤで近似。**完全再現は不可能と明示する** |
| F5 | **同時発音 247（実用上87程度）** | Black MIDI 級の密度は原理的に不可能 | 重要度で間引く（既に hyperchoron が実装） |
| F6 | **無音程打楽器が3種のみ** | ドラムキットの表現力が足りない | Mobヘッド、ピッチ変更した snare/hat、他楽器の低音を流用 |
| F7 | **ステレオが「プレイヤーからの相対位置」でしか作れない** | 原曲のパンをそのまま再現すると構造が横に非常に長くなる | パン幅を圧縮する。距離（音量）とパン（左右）が同じ「位置」パラメータを奪い合う **トレードオフがある** |
| F8 | **ピッチが半音単位（バニラ）** | 微分音・ビブラート・ポルタメント・ベンドが表現できない | grace note で近似。`--microtones` 相当のコマンドブロック方式はサバイバル非合法 |
| F9 | **リバーブ/残響が無い** | 空間的な広がりが出ない | 遅延音（TAIL）で疑似残響。ただし F2 により 50ms 刻み |
| F10 | 26.2 に対応した既存ツールが **存在しない** | トランペット4種を誰も使っていない | 自分で `material_map` を拡張する（＝**他ツールに対する明確な優位点になる**） |

**結論**: 「原曲に近づける」の上限は **F1（サステインなし）と F4（音色18種）** で決まる。
したがって目的関数は **波形一致ではなく、和声・リズム・ダイナミクス・音色の「相対的な動き」の一致** を測るべき。
参考動画（真っ黒ナイト・オブ・ナイツ）が成功しているのは、**原曲がピアノ主体で減衰音であり、MC音源の特性と元々相性が良い**という側面が大きい。ピアノ系・チップチューン系・オルゴール系は相性が良く、ボーカル・ストリングス・ディストーションギターは相性が悪い。

---

## G. 実装計画

### Phase 1: 基盤構築とベースライン（既存技術の確認）

1. `uv` プロジェクト初期化、依存導入
2. **MC音源抽出スクリプト** — ユーザーの `.minecraft` から 26.2 の note block 音源を抽出（トランペット4種を含む。ここで実際に何種類あるか判明する）
3. hyperchoron を動かし、既知のMIDIで `.nbs` / `.litematic` / レンダリングWAV を生成
4. **ベースライン記録** — 「hyperchoron そのまま」の出力品質を、後の比較基準として保存

### Phase 2: Minecraft音響モデルの検証（実機）

5. JDK 21+ 導入、Minecraft 26.2 環境準備
6. **測定用ワールドを自動生成**して以下を実測:
   - 距離 d と実際の音量の関係（線形か？ `1 − d/48` か？）
   - トランペットの酸化段階バリエーション数・音域
   - 同時発音数の実効上限（何音から破綻するか）
   - 各楽器のサンプル長・減衰カーブ
   - 左右配置とステレオ定位の対応
7. 測定結果で `render_nbs` 改造版（距離減衰・247音制限込み）を較正
   → **「MCを起動せずにMCの音を予測できる」状態を作る**

### Phase 3: 評価系の構築

8. 目的関数の実装（chroma / onset / loudness / MFCC-DTW / melody）
9. **Test 1–9（小テスト）を合成音源で作成** し、指標が人間の主観と相関するか検証・重み較正
10. 比較レポート自動生成（スペクトログラム・chromagram 並置、指標スコア）

### Phase 4: 編曲最適化器（★中核）

11. Voicing 候補生成器
12. ビームサーチ + ポリフォニー予算管理
13. Phase 3 の目的関数で評価 → 反復
14. Test 1–9 で hyperchoron ベースラインと比較

### Phase 5: 音声入力パイプライン

15. 音源分離カスケード（audio-separator）
16. stem別採譜
17. **採譜補正**（原音照合によるオクターブ誤り・幻ノート除去）
18. Test 10（実際の楽曲）

### Phase 6: 出力と実機検証

19. `.litematic` / `.schem` / データパック出力（26.2対応、トランペット込み）
20. 実機で貼り付け → 演奏 → 録音 → 原音と比較
21. **予測（render_nbs改）と実測（実機録音）の乖離を測定** し、モデルを補正

### Phase 7: 統合

22. CLI 一本化（`song.mp3` → 全出力）
23. ドキュメント、ライセンス表記

### テスト計画（要件15）

| # | テスト | 検証したいこと |
|---|---|---|
| 1 | 単音 | ピッチ・楽器選択・tick整合 |
| 2 | 2音の和音 | 同時発音、strum の要否 |
| 3 | 3音の和音 | voicing 選択、濁りの発生点 |
| 4 | メロディ＋伴奏 | 主旋律と伴奏の音量差（距離配置） |
| 5 | ドラム | 3打楽器＋代替音源のマッピング |
| 6 | 強弱 | 距離による velocity 制御の精度 |
| 7 | 短い残響 | TAIL テンプレートの効果 |
| 8 | 複数楽器 | LAYER / 音色合成 |
| 9 | 高速フレーズ | 20Hz 量子化の限界、間引き戦略 |
| 10 | 実際の楽曲 | パイプライン全体 |

各テストで「元音源 / 変換結果 / MC録音 / 問題点 / 改善方法」を `tests/NN_*/` に記録する。

---

## H. 参考リンク

- hyperchoron: https://github.com/thomas-xin/hyperchoron
- Note Block Studio: https://noteblock.studio/ / https://github.com/OpenNBS/NoteBlockStudio
- NBS フォーマット仕様: https://opennbs.gitbook.io/open-note-block-studio/nbs-format
- pynbs: https://github.com/OpenNBS/pynbs
- nbswave: https://github.com/OpenNBS/nbswave
- NoteBlockLib: https://github.com/RaphiMC/NoteBlockLib
- MidiMC: https://www.midimc.com/
- beats-to-blocks: https://github.com/dustinlaa/beats-to-blocks
- litemapy: https://github.com/SmylerMC/litemapy
- mcschematic: https://github.com/Sloimayyy/mcschematic
- Sponge Schematic 仕様: https://github.com/SpongePowered/Schematic-Specification
- audio-separator: https://github.com/karaokenerds/python-audio-separator
- Minecraft Wiki 音符ブロック: https://minecraft.wiki/w/Note_Block
- Minecraft Wiki Java 26.1: https://minecraft.wiki/w/Java_Edition_26.1
- Minecraft Wiki Sound: https://minecraft.wiki/w/Sound
- YourMT3+: https://arxiv.org/abs/2407.04822
- MR-MT3: https://arxiv.org/abs/2403.10024
- 参考動画: https://www.youtube.com/watch?v=qeJ7NMr0cLk
