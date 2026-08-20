# 実測記録 — Minecraft 26.2 の音符ブロック音源

**測定日**: 2026-08-21
**対象**: Minecraft Java Edition 26.2（アセットindex 32）
**再現方法**:

```bash
uv run python -m mcnb.mcassets --out assets/mc
uv run python -m mcnb.samples
```

---

## 1. 楽器一覧（sounds.json を解決した結果）

`block.note_block.*` イベントを sounds.json から辿った、**26.2 における確定情報**。

### 音符ブロック楽器：20種

従来の16種 + **26.1 で追加された trumpet 4種**。

| 楽器 | 実サンプル | 直下のブロック |
|---|---|---|
| harp | **`note/harp2`** | 上記以外すべて（既定） |
| bass | **`note/bassattack`** | 木材系 |
| basedrum | `note/bd` | 石系 |
| snare | `note/snare` | 砂・砂利 |
| hat | `note/hat` | ガラス系 |
| guitar | `note/guitar` | 羊毛 |
| flute | `note/flute` | 粘土・ハニカム |
| bell | `note/bell` | 金ブロック |
| chime | `note/icechime` | 氷塊 |
| xylophone | `note/xylobone` | 骨ブロック |
| iron_xylophone | `note/iron_xylophone` | 鉄ブロック |
| cow_bell | `note/cow_bell` | ソウルサンド |
| didgeridoo | `note/didgeridoo` | カボチャ |
| bit | `note/bit` | エメラルドブロック |
| banjo | `note/banjo` | 干草の俵 |
| pling | `note/pling` | グロウストーン |
| **trumpet** | `note/trumpet` | 銅ブロック |
| **trumpet_exposed** | `note/trumpet_exposed` | 露出した銅 |
| **trumpet_weathered** | `note/trumpet_weathered` | 風化した銅 |
| **trumpet_oxidized** | `note/trumpet_oxidized` | 酸化した銅 |

### ⚠ harp と bass のサンプル名が名前と一致しない

```
block.note_block.harp  →  note/harp2.ogg        (note/harp.ogg ではない)
block.note_block.bass  →  note/bassattack.ogg   (note/bass.ogg ではない)
```

`harp.ogg` と `bass.ogg` はアセットには存在するが**どのイベントからも参照されていない旧ファイル**。
アセットindex 26（1.21系）の時点で既にこうなっている。

**hyperchoron の `render_nbs()` は `nbs_raws` で `"harp"` / `"bass"` を指定しており、
旧ファイルの方をレンダリングしている。** Phase 2 でレンダラを改造する際に修正する。

### Mob ヘッド音：6種

音符ブロックの上に Mob ヘッドを置いたときに鳴る音。別イベントへの参照になっている。

| 楽器 | 参照先 | variant数 | 補正 |
|---|---|---|---|
| imitate.creeper | `random/fuse` | **1（決定論的）** | pitch ×0.5 |
| imitate.skeleton | `mob/skeleton/say1-3` | 3（ランダム） | — |
| imitate.wither_skeleton | `mob/wither_skeleton/idle1-3` | 3（ランダム） | — |
| imitate.zombie | `mob/zombie/say1-3` | 3（ランダム） | — |
| imitate.piglin | `mob/piglin/idle1-5` | 5（ランダム） | volume ×0.66 |
| imitate.ender_dragon | `mob/enderdragon/growl1-4` | 4（ランダム） | — |

**編曲上の含意**: variant が複数あるものは **再生のたびにサンプルがランダムに選ばれる**ため、
オフラインレンダラの予測と実機の音が一致しない。厳密な再現に使えるのは
**`imitate.creeper` だけ**（hyperchoron が Drumset クラスを creeper に割り当てているのは妥当）。
他はドラムの色付けなど、再現性を要求しない用途に限る。

---

## 2. サンプルの長さと減衰時間

全サンプルが **モノラル**（OpenAL の3D定位が効く前提と一致）。
`-20dB` / `-40dB` はピークからそこまで落ちきる時刻。移動平均5msで包絡を取った値。

| 楽器 | 長さ(s) | -20dB(s) | -40dB(s) | -40dB(tick) | sr |
|---|---:|---:|---:|---:|---:|
| chime | 1.103 | 0.283 | **0.793** | 15.9 | 48000 |
| pling | 0.646 | 0.309 | 0.468 | 9.4 | 44100 |
| harp | 0.573 | 0.275 | 0.454 | 9.1 | 48000 |
| guitar | 0.567 | 0.192 | 0.448 | 9.0 | 48000 |
| bell | 0.436 | 0.239 | 0.375 | 7.5 | 48000 |
| bass | 0.465 | 0.158 | 0.313 | 6.3 | 44100 |
| flute | 0.615 | 0.233 | 0.302 | 6.0 | 48000 |
| iron_xylophone | 0.584 | 0.169 | 0.287 | 5.7 | 48000 |
| didgeridoo | 0.428 | 0.177 | 0.274 | 5.5 | 48000 |
| bit | 0.380 | 0.172 | 0.225 | 4.5 | 48000 |
| banjo | 0.464 | 0.065 | 0.216 | 4.3 | 48000 |
| trumpet_oxidized | 0.313 | 0.167 | 0.212 | 4.2 | 48000 |
| trumpet_exposed | 0.328 | 0.184 | 0.208 | 4.2 | 48000 |
| trumpet_weathered | 0.280 | 0.144 | 0.166 | 3.3 | 48000 |
| trumpet | 0.277 | 0.139 | 0.160 | 3.2 | 48000 |
| xylophone | 0.138 | 0.029 | 0.104 | 2.1 | 48000 |
| cow_bell | 0.170 | 0.044 | 0.095 | 1.9 | 48000 |
| basedrum | 0.122 | 0.064 | 0.094 | 1.9 | 44100 |
| snare | 0.057 | 0.013 | 0.055 | 1.1 | 44100 |
| hat | 0.060 | 0.005 | 0.026 | 0.5 | 44100 |

### 読み取れること

1. **最も長く残る有音程楽器でも 0.79 秒（約16 tick）**。
   これが制約 F1（サステインなし）の定量的な中身。
   全音符どころか、120BPM の2分音符（1.0秒）すら1発では持たない。

2. **SUSTAIN テンプレートの再発音間隔の上限が決まった**。
   楽器ごとに `-20dB` 到達時刻（＝明確に減ったと分かる点）が
   `hat: 0.005s` から `pling: 0.309s` まで60倍以上開いている。
   **一律の再発音間隔ではなく、楽器ごとに変えるべき。**

3. **トランペット4種は短い**（-40dB まで 0.16〜0.21秒）。
   名前から想像される持続的なブラスではなく、**アタック主体の短い音**。
   ロングトーン用途ではなく、リズミカルなブラス・スタブや
   `ATTACK` テンプレート（先行音）の素材として使うのが妥当。

4. **打楽器は極端に短い**（hat 0.026s = 0.5 tick）。
   1 tick(50ms) より短いので、**tick 単位のタイミング制御で完全に分離できる**。
   打楽器の高速連打は音の重なりを気にせず打てる。

5. サンプルレートが 44100 と 48000 で混在している。
   レンダラ側で統一リサンプリングが必要（hyperchoron は 36000 Hz に揃えている）。

6. **chime だけ突出して長い（1.1秒）** ＝ 疑似残響（`TAIL` テンプレート）や
   持続感の演出に使える唯一の楽器に近い。音域が F♯5–F♯7 と高いのが難点。

---

## 3. 未測定（Phase 2 で実機測定する）

| 項目 | 現状の仮定 | 測定方法 |
|---|---|---|
| 距離による音量減衰 | `gain ≈ 1 − d/48`（線形） | 一定距離ごとに音符ブロックを置き、録音してRMSを比較 |
| trumpet の音域 | F♯3–F♯5（他の中音域楽器と同じ） | 実際に24回叩いて録音・ピッチ解析 |
| 錆止め(waxed)銅で trumpet が鳴るか | 鳴ると推定 | 設置して確認 |
| 同時発音の実効上限 | vanilla 247 / RSLS 4095 | 同時発音数を増やしながら欠落を検出 |
| 左右配置とステレオ定位の対応 | 定パワー則 | 左右に振って録音、L/R振幅比を測定 |
| 25段階ピッチの実周波数 | `2^((n-12)/12)` | 全24半音を録音してピッチ解析 |
