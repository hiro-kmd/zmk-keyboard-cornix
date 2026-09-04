# studio-backup — ZMK Studio キーマップのバックアップ / 復元

ZMK Studio で編集したキーマップは **キーボード本体のフラッシュ** にだけ保存され、
Studio には書き出し機能がありません。このフォルダのツール `zmkbak` は、Studio が
内部で使っている RPC (USB シリアル) でキーボードから直接キーマップを読み書きし、
`snapshots/` に保存・復元します。

> **キーマップは PC ではなくキーボードに入っています。** 別の PC に挿し替えても配列は
> そのまま使えます。このツールが必要になるのは次の場面です:
> 設定リセット (`cornix_reset.uf2`)・Studio の「Restore Stock Settings」・ファーム
> ウェア更新で配列が消えたときの復元、以前の配列への巻き戻し、2 台目の Cornix への複製。

## セットアップ (初回のみ、各 PC で)

Python 3.9 以上が必要です (Windows / macOS / Linux x86_64 ・ macOS arm64 対応)。

```
Windows : setup.cmd をダブルクリック
macOS/Linux : sh setup.sh
```

## GUI (おすすめ)

`CornixKeymapBackup.cmd` をダブルクリック (macOS/Linux: `./zmkbak.sh` の代わりに
`.venv/bin/python zmkbak_gui.py`)。

- 上部に **キーボードの接続状態** を常時表示。ZMK Studio が接続中 (ポート使用中) だと赤で
  「Studio で Save → Disconnect してください」と出て、操作ボタンが無効になります。
  緑「接続可能」になればワンクリックで操作できます。
- **バックアップ** (メモ付き) / **選択したものを復元** (確認ダイアログあり、復元前に自動バックアップ) /
  **ドライラン** / **キーボードと比較** (違うキーが赤く表示) / **キーボードの今の配列を表示**
- 左の一覧でスナップショットを選ぶと、右にレイヤーごとのタブでキーボード配置図が出ます (★ = 最新)。
- **GitHub に保存** ボタンで `snapshots/` を commit & push。

## コマンドライン

**キーボードを USB で接続し、ZMK Studio は Disconnect (またはタブを閉じる)** してから
実行します。Studio が接続中だとポートが使えず「アクセスが拒否されました」になります。

| コマンド | 内容 |
|---|---|
| `zmkbak doctor` | 接続診断 (ポート、ロック状態、レイヤー数、ビヘイビア一覧) |
| `zmkbak backup [--note "メモ"]` | 今のキーマップを `snapshots/<日時>_<機種>/` に保存 |
| `zmkbak list` | 保存済み一覧 |
| `zmkbak show --latest` / `show <名前>` / `show --device` | レイヤーごとの一覧表示 |
| `zmkbak diff <A> [<B>]` | 2 つのスナップショットを比較 (B 省略または `--device` でキーボードと比較。A に `latest` も可) |
| `zmkbak verify --latest` | キーボードが最新スナップショットと一致しているか |
| `zmkbak restore --latest --dry-run` | 復元のリハーサル (RAM に書いて読み戻し、保存せず破棄) |
| `zmkbak restore --latest` | 復元 (確認プロンプトあり。`--yes` で省略) |

Windows では `zmkbak.cmd backup` のように、macOS/Linux では `./zmkbak.sh backup` のように呼びます。

### Studio で編集したら

```
(Studio で Save → Disconnect)
zmkbak.cmd backup --note "親指に ! を追加"
git add studio-backup/snapshots && git commit -m "keymap snapshot" && git push
```

### 別の PC / 消えた後に復元

```
git clone https://github.com/hiro-kmd/zmk-keyboard-cornix.git
cd zmk-keyboard-cornix/studio-backup
setup.cmd            (初回のみ)
zmkbak.cmd doctor
zmkbak.cmd restore --latest --dry-run
zmkbak.cmd restore --latest
```

## 復元の安全設計

- **復元前に必ず自動バックアップ** を `snapshots/<日時>_<機種>_pre-restore/` に取ります
  (`--dry-run` のときは取りません)。
- Studio に未保存の変更がある状態で `backup` すると、作業中の内容を `_unsaved` 付きで保存し、
  `LATEST` は更新しません (確定した配列だけが「最新」になるようにするため)。
- 書き込みは RAM 上の作業コピーにだけ行い、**全キーを読み戻して一致を確認してから**
  `save_changes` でフラッシュに保存します。途中でエラーや不一致があれば `discard_changes`
  で取り消すので、フラッシュの内容は変わりません。
- Studio に**未保存の変更**が残っているときは復元を拒否します (`--discard-unsaved` で上書き)。
- `behavior_id` はファームウェア固有で、設定リセットや別個体では変わることがあります。
  スナップショットには表示名 (`Key Press`, `hm_l` など) も保存し、復元時は**表示名で
  再解決**します。`&mo N` などが参照するレイヤーも、レイヤーの並び順で付け直します。
- キー数やレイヤー数が違う場合は拒否します (`--force` で重なる範囲のみ)。
  Python API からはレイヤーの追加・削除・改名ができないため、レイヤー数は ZMK Studio で
  先に揃えてください。レイヤー名は復元されません (配列には影響なし)。
- このツールは `reset_settings` を一切呼びません。

## スナップショットの中身

```
snapshots/20260904-223000_Cornix/
  snapshot.json   構造化データ (機種・シリアル・物理レイアウト・全レイヤーの全キー・ビヘイビア定義)
  keymap.txt      人が読む用。物理配置に並べた表と、位置ごとの &kp / &mo ... 表記
  raw/*.bin       RPC が返した生の protobuf (将来の互換用)
snapshots/LATEST  最新スナップショットのフォルダ名
```

`keymap.txt` の表記について: `&kp N1` は数字の 1、`&kp EXCL` は Shift+1 (= `!`) です。
Studio の一覧に出る「Keyboard 1 and Bang」は前者 (`N1`) を指します。

## 終了コード

0 成功 / 1 使い方 / 2 デバイス未検出 / 3 ポート使用中 (Studio 接続中) / 4 ロック中 /
5 未保存の変更あり / 6 互換性なし (キー数・レイヤー数) / 7 ビヘイビア未解決 /
8 検証不一致 (取り消し済み) / 9 保存失敗 / 10 通信エラー

## 制限

- 通信は USB シリアルのみ (PyPI の wheel は BLE 無効)。左手側 (central) かドングルに接続してください。
  `cornix_left_for_dongle` ファームの左手側は RPC を持たないため、その構成ではドングルに接続します。
- Linux arm64 (Raspberry Pi など) 向け wheel は無く、Rust ツールチェーンが必要です。
- 開発用テスト: `python test_offline.py` (キーボード不要)。
