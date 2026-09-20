# TimePon 外部連携ブリッジ（index.php 無改造 / macOS・Windows 両対応）

TimePon の `index.php` に一切手を加えず、外部プロセス（サイドカー）だけで次を実現します。

| 誰が | どこから | 何をする |
| --- | --- | --- |
| 別ツール | HTTP リクエスト | **タイマーストップ＆リセット** / **タイマースタート**（別々に送れる） |
| 壇上司会 | USBフットスイッチ（USBエクステンダー経由） | **リセット＆スタート**（ワンアクション） |
| ホスト管理者 | TimePon の管理画面 | 緊急対応（そのまま温存） |

ブリッジは Python 3.9+ の標準ライブラリのみで動くので、**M4 Mac mini でも Windows でも同じ
ファイル・同じ設定で動きます**（フットスイッチ対応だけ任意ライブラリを1つ使います）。

---

## 1. 全体構成

```
   ┌──────────────── ホストPC（M4 Mac mini / Windows 11）────────────────┐
   │                                                                    │
別ツール ──HTTP──→  timepon_bridge.py  ──HTTP(localhost)──→  TimePon     │
(/reset, /start)  │      :8081                ?act=set 等     index.php  │
                  │        ↑                                    :8080    │
USBフットスイッチ ─┼→ キー入力(F13)                                ↑      │
   ↑ USBエクステンダー(LAN)                                        │      │
   │              │   管理者ブラウザ（管理画面 ?op=1&id=…#k=…）─────┘      │
   │              └────────────────────┬───────────────────────────────┘
   │                                   │ HDMI（既存の映像出力）
 演壇                                  ↓
                              演壇ディスプレイ ×2（登壇者用／司会用）
```

フットスイッチは **USBエクステンダーでホストPCに直結** されるので、演壇側にネットワーク機器は
不要になりました。ホストPCから見ると「キーボードが1台つながっていて、踏むと F13 が押される」
だけの状態です。ブリッジがそのキーを拾ってリセット＆スタートを実行します。

---

## 2. なぜ無改造で動くのか（TimePon 側 API の実測）

| 項目 | 内容 |
| --- | --- |
| リセット | `POST ?act=set` に `id`, `k`(adminKey), `cmd=reset` → `state` が `idle` に戻る（持ち時間は保持） |
| スタート | `cmd=start` … `idle` なら `durationSec` を指定して開始、`paused` なら再開、`running` なら無反応 |
| 一時停止 | `cmd=pause` … `running` のときのみ有効 |
| 秒数の範囲 | `start` 経由は 5〜86400 秒。`setSettings` の `durSec` は 1〜720 秒に丸められるので、長い持ち時間は必ず `start` で渡す |
| 警告秒 | `POST ?act=setSettings` の `warn1Sec` / `warn2Sec` |
| CSRF 対策 | `Origin`（無ければ `Referer`）が `Host` と一致すること → ブリッジが自分で正しい `Origin` を付ければ通る |
| レート制限 | 書き込み 120回/分・IP、`get`/`hb` 1200回/分・IP |

管理画面は `?act=get` を1秒ごとにポーリングしているので、**ブリッジが外から変更した状態も
管理画面にそのまま反映されます**。管理者はいつでも割り込めます。

---

## 3. セットアップ

### 3-1. TimePon 本体（PHP）

**macOS（M4 Mac mini）**

macOS には PHP が同梱されなくなっているので Homebrew で入れます。

```bash
brew install php
cd ~/timepon                       # index.php を置いたフォルダ
PHP_CLI_SERVER_WORKERS=4 php -S 0.0.0.0:8080
```

`PHP_CLI_SERVER_WORKERS` は PHP 内蔵サーバをマルチプロセス化する環境変数で、macOS/Linux では
有効です（Windows では効きません）。演壇2画面＋管理画面＋ブリッジが同時にポーリングしても
詰まらなくなるので、Mac ならこれで十分実用になります。Docker Desktop for Mac でも
`docker compose up -d` は Apple Silicon で問題なく動きます（`php:8.3-fpm` はマルチアーキ対応）。

**Windows 11**

内蔵サーバがシングルスレッドで詰まるため、XAMPP を推奨します。`C:\xampp\htdocs\timepon\` に
`index.php` を置いて Apache を起動するだけです（前回お伝えした手順のまま）。

### 3-1b. フォルダの置き場所と移設

どのファイルも「自分がある場所」を基準に動くので、**フォルダごと移動するだけで引っ越せます**。

| ファイル | 基準 | 移設時 |
| --- | --- | --- |
| `index.php` のデータ (`data/`) | `index.php` と同じ階層（`__DIR__`） | フォルダごと移動すればそのまま |
| `bridge_config.json` / `bridge.log` | `timepon_bridge.py` と同じ階層 | 同上 |
| `~/.config/karabiner/` | Karabiner-Elements の固定位置 | **移動不可・移動不要**（ペダル設定のみ） |

推奨する構成は次のとおりです。

```
＜任意のフォルダ＞/
├── timepon/                  index.php と data/
│   ├── index.php
│   └── data/                 ルーム情報（消すと adminKey も消えます）
├── timepon_bridge.py
├── bridge_config.json
└── start_timepon.command     ダブルクリックで両方起動
```

**移設手順**

1. 起動中の PHP とブリッジを終了する（`start_timepon.command` なら Ctrl+C）
2. フォルダごと新しい場所へ移動する。**`data/` を一緒に移せば、ルームID・adminKey・
   持ち時間の設定がそのまま引き継がれます**（`--create-room` のやり直しは不要）
3. 新しい場所の `start_timepon.command` をダブルクリック

`bridge_config.json` の `timepon_base` は `http://127.0.0.1:8080/` のままで構いません
（URL なのでフォルダ位置とは無関係です）。

**別アカウントで運用する場合**は、次の3点も新しいアカウント側で用意が必要です。

- フォルダの読み書き権限（他ユーザーのホーム配下だとアクセスできません。
  `/Users/Shared/` 以下に置くのが確実です）
- Hammerspoon / Karabiner の設定と権限（どちらもユーザーごとの設定・許可です）
- ロケール環境変数（未設定だとログが文字化けします。`start_timepon.command` が
  UTF-8 を固定するので、これを経由して起動すれば解決します）

**macOS の置き場所に関する注意**

`~/Documents` `~/Desktop` `~/Downloads` は macOS のプライバシー保護対象フォルダです。
ターミナルから手で起動する分には初回に許可ダイアログが出るだけですが、launchd から
自動起動する場合に**許可が下りず黙って失敗する**ことがあります。イベント用の常設なら
`~/timepon-event/` や `/Users/Shared/timepon-event/` のような保護対象外の場所を勧めます。

データだけ別の場所（外部ディスクなど）に置きたい場合は、PHP 起動時に環境変数を渡します。

```bash
TIMEPON_DATA_DIR=/Volumes/SSD/timepon-data php -S 0.0.0.0:8080 -t ./timepon
```

ブリッジの設定ファイルを別の場所に置きたい場合は `TIMEPON_BRIDGE_CONFIG` を使います。

**launchd で自動起動している場合**は、plist 内の `ProgramArguments` と
`WorkingDirectory` のパスを新しい場所に書き換えてから読み込み直してください。

```bash
launchctl unload ~/Library/LaunchAgents/local.timepon.bridge.plist
# plist を編集
launchctl load -w ~/Library/LaunchAgents/local.timepon.bridge.plist
```

### 3-1c. 起動スクリプトの動作

`start_timepon.command` は、単に2つを起動するだけでなく次を行います。

- **スリープ抑止**（`caffeinate`）。スクリプトが動いている間だけ有効で、終了すると自動解除
- 起動前チェック（ポートの使用状況・フォルダの読み書き権限・php/python の所在）
- PHP が**実際に応答するまで待ってから**ブリッジを起動。失敗時は `php.log` の末尾を表示して停止
- **プロセス監視**。PHP かブリッジが落ちたら 2 秒以内に検出して自動再起動し、
  画面に時刻と通算回数を表示

```
2026-09-06 19:12:23 [!] TimePon(PHP) が終了しました。再起動します（通算 1 回目）
```

この行が出ていたら、落ちた事実が記録されているということです。頻発する場合は
`php.log` と `pmset -g log` を突き合わせて原因を追ってください。

長時間の無人運用では、macOS 側でもスリープを止めておくと確実です。

```bash
sudo pmset -a sleep 0 displaysleep 0
```

### 3-2. Python とブリッジ

```bash
# macOS
python3 --version                  # 3.9 以上。無ければ brew install python
python3 timepon_bridge.py          # 初回: bridge_config.json を生成して終了
```

```powershell
# Windows
python timepon_bridge.py
```

`bridge_config.json` の `timepon_base` を実環境に合わせてから、ルームを作ります。

```bash
python3 timepon_bridge.py --create-room     # room_id / admin_key を保存し、URLを表示
python3 timepon_bridge.py                   # 常駐起動
```

設定ファイルの場所は環境変数 `TIMEPON_BRIDGE_CONFIG` で変更できます（OSごとに配置を分けたい場合）。

### 3-3. フットスイッチ対応（任意ライブラリ）

キー入力を拾うために `pynput` を使います。**入れなくてもブリッジは動きます**（後述の Plan B を使う場合は不要）。

```bash
# macOS（Homebrew python は PEP 668 対策で venv 推奨）
python3 -m venv ~/timepon-bridge/venv
~/timepon-bridge/venv/bin/pip install pynput
~/timepon-bridge/venv/bin/python timepon_bridge.py
```

```powershell
# Windows
pip install pynput
```

macOS では初回起動時に **システム設定 → プライバシーとセキュリティ → 入力監視** で、実行元
（ターミナル.app、または venv の python 実行ファイル）を許可する必要があります。許可しないと
エラーは出ずに無反応になるので、リハーサルで必ず1回踏んで確認してください。
Windows は追加設定なしで動きます。

### 3-4. 自動起動

**macOS（launchd）** — `~/Library/LaunchAgents/local.timepon.bridge.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.timepon.bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/USERNAME/timepon-bridge/venv/bin/python</string>
    <string>/Users/USERNAME/timepon-bridge/timepon_bridge.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/USERNAME/timepon-bridge</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/USERNAME/timepon-bridge/launchd.log</string>
  <key>StandardErrorPath</key><string>/Users/USERNAME/timepon-bridge/launchd.log</string>
</dict></plist>
```

```bash
launchctl load -w ~/Library/LaunchAgents/local.timepon.bridge.plist
```

PHP 側も同じ要領でもう1つ plist を作れば、電源を入れるだけで両方立ち上がります。

**注意**: launchd から起動したプロセスへの「入力監視」許可は通り方が分かりにくいことがあります。
フットスイッチを pynput 方式で使うなら、**当日はターミナルから手で起動する**か、許可済みの
python 実行ファイルのフルパスを plist に書いて挙動を事前確認してください。

**Windows** — `start_bridge.bat` を作ってタスクスケジューラの「ログオン時」に登録。

```bat
@echo off
cd /d C:\timepon-bridge
python timepon_bridge.py >> bridge.log 2>&1
```

### 3-5. ファイアウォール

- **macOS**: 初回起動時に「ネットワーク受信を許可しますか」のダイアログが出るので許可。
  出ない場合はシステム設定 → ネットワーク → ファイアウォール → オプションで python を許可。
- **Windows**: 管理者 PowerShell で以下（ポートは設定に合わせて調整）。

```powershell
netsh advfirewall firewall add rule name="TimePon Web"    dir=in action=allow protocol=TCP localport=8080
netsh advfirewall firewall add rule name="TimePon Bridge" dir=in action=allow protocol=TCP localport=8081
```

別ツールが同じ Mac 上で動くなら、そもそも外部からの受信を許可する必要はありません。

---

## 4. 設定ファイル（bridge_config.json）

```json
{
  "timepon_base": "http://127.0.0.1:8080/",
  "room_id": "123456",
  "admin_key": "（32桁以上のhex）",

  "http_host": "0.0.0.0",
  "http_port": 8081,
  "udp_port": 5010,
  "tcp_port": 5011,

  "trigger_token": "",
  "cors_allow_origin": "*",
  "cooldown_sec": 3.0,
  "command_cooldown_sec": 0.8,
  "idempotency_ttl_sec": 120,
  "clear_message_on_trigger": true,

  "default_profile_udp": "net",
  "default_profile_tcp": "net",
  "default_profile_http": "mc",

  "profiles": {
    "mc":  { "label": "司会スタート", "durationSec": 300, "warn1Sec": 60, "warn2Sec": 30 },
    "net": { "label": "外部ツール",   "durationSec": 420, "warn1Sec": 60, "warn2Sec": 30 }
  },

  "footswitch": {
    "enabled": true,
    "keys": ["f13"],
    "action": "trigger",
    "profile": "mc",
    "min_interval_sec": 1.0
  },

  "log_file": "bridge.log"
}
```

- `cooldown_sec` は「リセット＆スタート」の連打抑止。`command_cooldown_sec` は個別操作用で、
  **操作の種類ごとに独立してカウント**されます。だから「ストップ＆リセット」の直後に
  「スタート」を送っても弾かれません。
- `footswitch.keys` は pynput のキー名（`f13` `f19` `space` など）か1文字（`b` など）。複数指定可。
- `footswitch.action` を `reset` などに変えれば、ペダルの役割を変更できます。
- UDP / TCP は使わないなら `udp_port` / `tcp_port` を `0` にすれば待受を止められます。

---

## 5. 別ツールからの HTTP リクエスト

### エンドポイント

| メソッド / パス | 操作 |
| --- | --- |
| `GET/POST /reset?profile=net` | **リセット**（停止して、そのプロファイルの秒数に戻して待機） |
| `GET/POST /reset?sec=600` | **リセット**（秒数を直接指定して戻す） |
| `GET/POST /reset` （`/stop` も可） | **ストップ＆リセット**（停止して `idle` に戻す。秒数は現状維持） |
| `GET/POST /start?profile=net` | **スタート**（そのプロファイルの秒数で開始 / `paused`なら再開） |
| `GET/POST /start` | **そのままスタート**（今表示されている秒数で開始） |
| `GET/POST /trigger?profile=mc` | **リセット＆スタート**（フットスイッチと同じ動作） |
| `GET/POST /pause` | 一時停止 |
| `GET/POST /cmd/<action>[/<profile>]` | 明示指定（`action` = `trigger` / `reset` / `start` / `pause`） |
| `GET /state` | 残り時間・状態・直近20件の受信履歴 |
| `GET /profiles` `GET /healthz` | プロファイル一覧 / 生存確認 |
| `GET /panel` `GET /` | 操作パネル / 司会用ワンボタンページ |
| `GET /admin` | **TimePon の管理画面へ転送**（`/tp/` 経由・adminKey 付きURLに 303） |
| `GET /stage` | 演壇画面へ転送（`/tp/` 経由） |
| `ALL /tp/…` | TimePon 本体の中継（CSP の `upgrade-insecure-requests` を除去） |
| `GET /urls` | 管理URL・演壇URLを JSON で返す |

### パラメータ

| 名前 | 説明 |
| --- | --- |
| `profile` | プロファイル名。秒数と警告秒がこれで決まる。**付けるか付けないかで挙動が変わる**（下記） |
| `sec` | 秒数の上書き |
| `req` | **リクエストID**。同じ ID の再送は実行されず、前回の結果が返る（既定120秒記憶） |
| `key` | `trigger_token` を設定した場合に必要 |
| `format` | `json`（既定） / `html` / `text` / `gif` |
| `redirect` | 実行後に 303 で指定 URL へ |

### `profile` の有無で挙動が変わる

`reset` と `start` は、`profile`（または `sec`）を**明示したときだけ持ち時間に触ります**。
「戻す」と「そのまま動かす」を1つのエンドポイントで使い分けられます。

| リクエスト | 動作 |
| --- | --- |
| `/reset?profile=net` | 停止して **420秒（netの持ち時間）に戻して待機**。次は `/start` を押すだけ |
| `/reset` | 停止するだけ。表示されている秒数は変えない |
| `/start?profile=net` | 420秒で開始（`paused` からなら再開） |
| `/start` | **今表示されている秒数のまま開始**。`/reset?profile=…` の直後はこれを使う |

そのため、別ツールからの標準的な流れは次の2手になります。

```
1. /reset?profile=net    → 停止して 07:00 表示で待機
2. /start                → 07:00 からカウント開始
```

秒数を毎回変えたい場合は `1. /reset?sec=540` → `2. /start` としてください。

### 応答

実行後の TimePon 側の状態が `state` に入るので、別ツール側で結果を確認できます。

```json
{
  "ok": true,
  "code": "done",
  "message": "420 秒でスタートしました",
  "action": "start",
  "profile": "net",
  "durationSec": 420,
  "duplicate": false,
  "state": { "state": "running", "durationSec": 420, "remainingMs": 420000 },
  "atMs": 1788320444871
}
```

| `code` | HTTP status | 別ツール側の推奨動作 |
| --- | --- | --- |
| `done` | 200 | 完了 |
| `duplicate` | 200 | 同じ `req` の再送。完了扱い |
| `cooldown` | 429 | 直前に同じ操作を実行済み。再送しない |
| `unknown_profile` / `unknown_action` | 400 | 設定ミス |
| `bad_token` | 403 | 設定ミス |
| `error` | 502 | **再送してよい**（成功以外は冪等記録に残さない） |

### 使い方の例

```bash
# 進行の区切りで止めて、次の持ち時間に戻して待機
curl "http://127.0.0.1:8081/reset?profile=net&req=$(uuidgen)"

# 表示されている秒数からスタート
curl "http://127.0.0.1:8081/start?req=$(uuidgen)"

# 秒数をその場で指定して戻す／開始する
curl "http://127.0.0.1:8081/reset?sec=540&req=$(uuidgen)"
curl "http://127.0.0.1:8081/start?sec=540&req=$(uuidgen)"

# 停止だけしたい（秒数は触らない）
curl "http://127.0.0.1:8081/stop?req=$(uuidgen)"
```

```javascript
async function timepon(action, opts = {}) {
  const req = crypto.randomUUID();                   // 操作ごとに1つ
  const qs = new URLSearchParams({ ...opts, req });
  for (let i = 0; i < 5; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:8081/${action}?${qs}`, { cache: 'no-store' });
      const j = await r.json();
      if (j.ok) return j;                            // done でも duplicate でも成功
      if (j.code === 'cooldown') return j;
    } catch (e) { /* 通信断。リトライへ */ }
    await new Promise(res => setTimeout(res, 400));
  }
  throw new Error('TimePon 操作に失敗しました');
}

await timepon('reset', { profile: 'net' });           // リセット（420秒に戻して待機）
await timepon('start');                              // スタート（表示のまま開始）
await timepon('trigger', { profile: 'mc' });         // リセット＆スタート（1発）
```

HTML しか書けない場合は、リンク・画像ビーコン・フォーム送信のどれでも叩けます。

```html
<a href="http://127.0.0.1:8081/reset?format=html" target="_blank">ストップ＆リセット</a>
<img src="http://127.0.0.1:8081/start?profile=net&format=gif&req=evt-042" width="1" height="1" alt="">
<iframe src="http://127.0.0.1:8081/panel" style="width:100%;height:560px;border:0"></iframe>
```

別オリジンの Web アプリから `fetch` できるよう CORS は許可済みです（`cors_allow_origin`）。

### UDP / TCP（予備経路）

HTTP が主経路になりましたが、UDP(5010) / TCP(5011) も残してあります。

```
START / GO      → リセット＆スタート        RESET net    → リセット（netの秒数に戻す）
RESUME          → スタート                  RESET / STOP → 停止のみ
PAUSE           → 一時停止                  START net 600 req=evt-042 （書式は共通）
```

---

## 6. USBフットスイッチ

### 6-1. ペダルの設定

プログラマブルなペダルを選び、**F13 を送る**ように設定するのが安全です。F13 は通常のアプリが
使わないので、他の操作と衝突しません。設定ユーティリティが Windows 専用の製品が多いので、
**設定だけ Windows 機で済ませて**から Mac につないでください（設定はペダル本体に保存されます）。

ペダルが送るキーを変えた場合は `footswitch.keys` を合わせてください（`space`、`b` など）。

### 6-2. USBエクステンダーの選び方

- **ハードウェア式（Cat5e/6 で延長するタイプ）を推奨**。ドライバ不要で、OS からは普通の USB
  キーボードに見えます。HID は低速なので距離による問題も起きにくいです。
- **USB over IP（ネットワーク経由でUSBを共有するタイプ）は要注意**。クライアントソフトを
  ホストに入れる必要があり、Apple Silicon 対応の macOS クライアントが提供されていない製品が
  あります。この方式を使うなら、購入前に **arm64 macOS 対応版があるか**を必ず確認してください。
- リハーサルでは、ペダルを踏んで**テキストエディタに F13 相当の反応があるか**（あるいは
  後述の司会ページで反応するか）を先に確認すると切り分けが早いです。

### 6-3. キーの拾い方は2通り

**方式A: ブリッジが直接拾う** — `footswitch.enabled: true` にして pynput を入れる方式。
どのアプリが前面にいても踏めば動きます。macOS では「入力監視」の許可が必要です。

**方式B: 司会ページで拾う（権限・追加インストール不要）** — 司会用ディスプレイのブラウザで
`http://ホストIP:8081/` を全画面表示しておくと、そのページがキー入力を拾って同じ動作をします。
`footswitch.keys` に設定したキーに加えて Enter / Space でも発火します。ページを最前面に
保つ必要がありますが、**残り時間が同時に見える**という利点があります。キオスクモード
（`open -a "Google Chrome" --args --kiosk http://...`）にしておけば安定します。

**方式C: Karabiner-Elements から HTTP リクエストを飛ばす（他ソフト併用時の推奨）** —
ペダルのキーを OS レベルで捕まえて `curl` を実行する方式。フォーカスに一切依存せず、
ブリッジ側は `pynput` も入力監視の許可も不要になります（`footswitch.enabled` は `false` のままでOK）。

1. [Karabiner-Elements](https://karabiner-elements.pqrs.org/) をインストールし、
   起動時に出る「ドライバ機能拡張の許可」と「入力監視の許可」をウィザードに従って進める
2. 同梱の `timepon_footswitch.json` を次の場所にコピー

   ```bash
   mkdir -p ~/.config/karabiner/assets/complex_modifications
   cp timepon_footswitch.json ~/.config/karabiner/assets/complex_modifications/
   ```

3. Karabiner-Elements → Complex Modifications → Add rule →
   「F13 → TimePon リセット＆スタート」を Enable

これで、どのアプリを使っていてもペダルを踏むと
`http://127.0.0.1:8081/trigger?profile=mc` が飛びます。F13 は Karabiner が飲み込むので、
他のソフトに漏れる心配もありません。

ペダル以外のキーボードの F13 まで反応させたくない場合は、同梱JSONの3番目のルール
（`device_if` で `vendor_id` / `product_id` を指定する例）を使ってください。IDは
Karabiner-EventViewer の Devices タブで確認できます。

**方式D: Hammerspoon から HTTP リクエストを飛ばす（Karabiner が動かないとき）** —
Karabiner はドライバ機能拡張の承認が必要で、macOS 15 以降で承認が通らない不具合が
報告されています。Hammerspoon は**アクセシビリティ権限だけ**で動き、機能拡張は不要です。

1. インストール: `brew install --cask hammerspoon`（または公式サイトから）
2. 同梱の `timepon_hammerspoon_init.lua` を `~/.hammerspoon/init.lua` に置く
   （既存の `init.lua` がある場合は中身を末尾に追記）
3. Hammerspoon を起動し、アクセシビリティの許可を出す
4. メニューバーアイコン → Reload Config

「TimePon フットスイッチ 有効 (F13)」と表示されれば設定完了です。F13 でリセット＆スタート、
F14 でリセット、`Cmd+Alt+Ctrl+T` でブリッジの生死確認ができます。
`BRIDGE` の行のポート番号は `bridge_config.json` の `http_port` に合わせてください。

### 6-5. Karabiner が反応しないときの切り分け

1. **Karabiner-EventViewer** を開いてペダルを踏む
   - Main タブに `f13` が出る → 手順2へ
   - 出ない → System Extensions タブで
     `org.pqrs.Karabiner-DriverKit-VirtualHIDDevice` が `[activated enabled]` か確認。
     違えば システム設定 → 一般 → ログイン項目と機能拡張 → ドライバ機能拡張 で承認。
     承認済みに見えるのに動かない既知の不具合があるので、**一度オフ→オン**も試す
2. Complex Modifications の有効な一覧にルールが並んでいるか（置くだけでは有効になりません）
3. 同梱JSONの「【切り分け用】」ルールを有効にして踏み、`/tmp/timepon_foot.log` を見る

   ```bash
   tail -f /tmp/timepon_foot.log
   ```

   - 時刻と `{"ok": true …}` が出る → 正常。本番ルールに戻す
   - **ファイルが1行もできない** → `shell_command` が実行されていない（下の「4」へ）
4. `curl -s 'http://127.0.0.1:8081/trigger?profile=mc'` を手で実行して応答を確認
   （同梱JSONは 8081 決め打ち。`http_port` が違うなら書き換える）
5. **ルールがマッチしているかを1発で判定する** — Karabiner のルールを有効にしたまま
   `/keytest` を開いてペダルを踏む。`to` が `shell_command` だけのルールは F13 を
   飲み込むので、**マッチしていれば keytest には何も出ないのが正解**。
   フォーカス時に F13 が出るなら、ルールは素通りしている＝マッチしていない。
   その場合は同梱の `check_karabiner.py` で設定を点検する。

   ```bash
   python3 check_karabiner.py
   ```

   よくある原因は3つ。
   - **Devices タブでそのデバイスの Modify events がオフ**（`ignore=true`）。
     EventViewer には見えるのにルールが適用されない典型。後から挿したデバイスで起きる
   - 有効にしたのが `device_if` 付きの応用ルール（同梱JSONの vendor_id / product_id は
     仮の値なので絶対にマッチしない）
   - ルールを有効にしたプロファイルが「使用中」ではない
6. **キーは見えるのに `shell_command` だけ動かない場合** — Karabiner 16.0.0 には、
   スリープ復帰後に内部の認証が壊れ、再マッピングは動くのに `shell_command` だけが
   黙って失敗する既知の不具合があります。ログとリセットは次のとおり。

   ```bash
   tail -30 ~/.local/share/karabiner/log/console_user_server.log
   launchctl kickstart -k gui/$(id -u)/org.pqrs.karabiner.karabiner_console_user_server
   ```

   `invalid shared secret` が出ていればこの不具合です。kickstart で復旧しますが、
   **スリープのたびに再発します**。イベント用途なら、Mac をスリープさせない設定にするか、
   方式D（Hammerspoon）へ切り替えてください。

当日は方式Cか方式Dを本命に、方式Bを保険にする構成が最も堅いと思います。
どの方式も最終的に同じ処理を呼ぶので、TimePon 側の挙動は変わりません。

### 6-4. 動かないときの切り分け

上から順に見ていくと、どの層で止まっているかが確実に分かります。

**手順1: ペダルが「キーを送っているか」** — ブラウザで `http://127.0.0.1:8081/keytest` を開き、
ペダルを踏む。権限も追加インストールも不要です。

- ここで反応する → ペダル・ケーブル・macOS の認識はすべて正常。手順2へ
- **ここで無反応** → ペダルがキーボードとして動いていない。以下を確認
  - システム情報.app → USB にデバイスが見えるか（見えなければケーブル／ハブ／ペダル本体）
  - マウスクリック送出モードになっていないか（ペダル側の設定を切り替える）
  - 専用ドライバ前提の製品ではないか（macOS 版ドライバの有無を確認）

**手順2: 送っているキーの名前を確認** — `/keytest` の表の「設定に書く値」列、または
`python3 timepon_bridge.py --keys` の出力を `bridge_config.json` の `footswitch.keys` に
そのまま書く。F13 のつもりでも実際は別のキーだった、というのが一番多い原因です。

```bash
python3 timepon_bridge.py --keys      # 踏むとキー名が出る。Ctrl+C で終了
```

**手順3: pynput が入っているか** — ブリッジ起動時のログを見る。

- `FOOT 待受 キー=f13 → …` が出ている → 導入済み。手順4へ
- `FOOT 無効: pynput が見つかりません` → `python3 -m pip install --user pynput`
- 行そのものが無い → `footswitch.enabled` が `false`

**手順4: macOS の入力監視の許可** — `--keys` が「8秒間、キーを1つも検出できていません」と
出す場合はこれです。

- システム設定 → プライバシーとセキュリティ → 入力監視 に、**ブリッジを起動したアプリ**
  （ターミナル.app、iTerm、VS Code など）を追加して ON
- **許可を変えたらターミナルを再起動**してから試す（起動済みプロセスには反映されません）
- launchd から自動起動している場合は許可が通りにくいので、切り分け中は手動起動で試す
- パスワード入力欄にフォーカスがあると macOS がセキュア入力モードになり、
  どのアプリもキーを受け取れません

**手順5: 踏んでも TimePon が動かない** — ログに `FOOT` 付きの行が出ているかを見る。

- `OK trigger (FOOT …)` が出ているのに画面が変わらない → TimePon 側の問題（`/panel` の
  受信履歴と `/state` を確認）
- `SKIP … クールダウン中` → 連続で踏んでいる。`min_interval_sec` / `cooldown_sec` を調整

---

### 管理URL・演壇URLを確認する

ルームを作り直すと ID が変わり、**以前の管理URLはボタンが効かなくなります**（管理キーは
URL の `#k=` にあり、ブラウザは ID ごとに localStorage へ保存するため）。現在の設定に対応する
URL はいつでもこれで取り出せます。

```bash
python3 timepon_bridge.py --urls
```

`/panel` をホストPC上（localhost）で開いた場合にも、同じリンクが表示されます。

### 起動後の疎通確認（1コマンド）

差し替え後や当日の立ち上げ時は、これで全ルートを一度に叩けます。

```bash
for u in / /panel /state /profiles /healthz /reset /start /trigger /pause; do
  printf "%-12s " "$u"; curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8081$u"; sleep 1
done
```

すべて 200（連続実行のため一部 429 が出るのは正常）であれば問題ありません。

---

## 7. 当日の運用

1. TimePon（8080）とブリッジ（8081）を起動。
2. 管理者ブラウザで管理画面 `?op=1&id=XXXXXX#k=…` を開く（緊急対応用）。
3. 演壇向け2画面に `?id=XXXXXX` を全画面表示。
4. フットスイッチを1回踏み、管理画面の残り時間が変わることを確認。
5. 別ツールから `/reset` と `/start` を1回ずつ投げて疎通確認。
6. 本番。トラブル時は管理者が管理画面から一時停止・リセット・カンペ送出で介入。

`/panel` を開いておくと、残り時間・全プロファイルのボタン・個別操作ボタン・**直近20件の
受信履歴**が1画面で見えます。「今の信号が届いたか」を目視確認できるので、リハーサル時の
切り分けに便利です。

---

### 別マシンからアクセスするときは `/tp/` 経由で

TimePon は CSP に `upgrade-insecure-requests` を付けて返します。これは
「このページから出る通信をすべて https に上げる」という指示です。

- ホストPCで `http://localhost:8080/` を開く → localhost は仕様上「安全なオリジン」なので
  アップグレードされない → **正常に動く**
- 別マシンから `http://192.168.0.x:8080/` を開く → ページ内の `?act=get` / `?act=set` が
  すべて `https://192.168.0.x:8080/` に強制変換される → TLS で待ち受けていないので接続不能
  → **画面は出るがボタンも時計も動かない**

ブラウザ側にこれを止める設定はなく、PHP を触らずに直すにはヘッダを書き換えるしかないので、
ブリッジにリバースプロキシを内蔵しました。`/tp/` 以下は TimePon をそのまま中継し、
CSP から `upgrade-insecure-requests` だけを取り除いて返します（他のディレクティブは維持）。

| 用途 | URL |
| --- | --- |
| 管理画面（別マシン可） | `http://＜ホストIP＞:8081/admin` |
| 演壇画面（別マシン可） | `http://＜ホストIP＞:8081/stage` |
| 直接指定したい場合 | `http://＜ホストIP＞:8081/tp/?op=1&id=XXXXXX#k=…` |
| ホストPC上での直アクセス | `http://localhost:8080/?op=1&id=XXXXXX#k=…`（従来どおり） |

`proxy_prefix` を `""` にすると中継を無効化できます。中継経由の通信は TimePon から見ると
すべて同じ IP（127.0.0.1）に見えるため、レート制限（読み取り1200回/分・書き込み120回/分）を
全クライアントで共有します。演壇画面は0.5秒間隔・管理画面は1秒間隔のポーリングなので、
画面が10枚を超えるような規模になる場合だけ注意してください。

### 別マシンから管理画面を開くとき

TimePon の管理画面は、URL の `#k=…`（ハッシュ部分）から adminKey を読み取り、その端末の
localStorage に保存する作りです。**`#k=` が付いていない URL で開くと、ページは表示されるのに
ボタンが一切効きません**（サーバ側は `forbidden` を返します）。

そのため、ホストPC以外から管理画面を開くときは、ブリッジの転送用URLを使ってください。

```
http://＜ホストPCのIP＞:8081/admin    → adminKey 付きの管理画面へ転送
http://＜ホストPCのIP＞:8081/stage    → 演壇画面へ転送
```

`/panel` にも同じリンクを置いてあります。adminKey の生の文字列は、**ホストPC自身から
`/panel` を開いたときだけ**表示されます（LAN 上の他端末には見せません）。
`trigger_token` を設定している場合、`/admin` `/stage` `/urls` にも `?key=…` が必要になります。

なお、これらの転送URLは**アクセスしてきたホスト名を使って組み立てる**ので、
`timepon_base` が `127.0.0.1` のままでも別マシンから正しく開けます。DNS 名や
リバースプロキシ経由で固定したい場合は `timepon_public_base` に完全なURLを書いてください。

---

## 8. 注意点

- **ルームの寿命**: 最終更新から7日で自動削除。長期間空けたら `--create-room` で作り直し。
- **`start` の秒数**: `idle` のときだけ `durationSec` が効きます。`paused` からの `start` は
  再開なので、秒数を変えたい場合は `reset` → `start` の順にしてください。
- **12分を超えるリセット**: TimePon は `setSettings` の持ち時間が720秒までなので、それを超える
  秒数への「リセット」は内部的に `reset → start → pause` を使い、**一時停止状態で待機**します
  （表示上は指定秒数のまま止まって見え、`/start` で再開します）。720秒以下なら通常の `idle`
  状態に戻ります。
- **警告秒の上書き**: プロファイルに `warn1Sec` / `warn2Sec` を書くと実行のたびに上書きされます。
  管理画面での手動設定を維持したいならキーごと削除してください。
- **持ち時間720秒超**: `setSettings` 側は720秒に丸められますが、実際の持ち時間は `start` の
  `durationSec` で決まるため12分超でも正しく動きます（1800秒で検証済み）。
- **時刻**: 残り時間はサーバ時刻基準です。ホストPCの時刻同期を確認しておいてください。

---

## 9. トラブルシュート

| 症状 | 確認 |
| --- | --- |
| ペダルを踏んでも無反応 | 6-4 の切り分け手順へ。まず `/keytest` を開いて踏む |
| 特定の画面を選んでいないと効かない | 方式B で動いている状態。方式C（Karabiner）か方式A（入力監視の許可）に切り替える |
| `--keys` で何も出ない | macOS の「入力監視」未許可か、ペダルがキーを送っていない |
| `FOOT 無効: pynput が見つかりません` | `pip install pynput`。または方式B（司会ページ）に切り替える |
| `bad_origin` エラー | `timepon_base` のホスト名／ポートがブラウザで開く URL と一致しているか |
| `forbidden` エラー | `admin_key` が一致しているか。ルームが7日で消えていないか |
| 別マシンで管理画面のボタンが効かない | `http://ホストIP:8081/admin` から開き直す。DevTools の Request URL が `https://` になっていたら CSP のアップグレードが原因 |
| Request URL が `https://` になっている | 直接 8080 を開いている。`/tp/` 経由（= `/admin`）に切り替える |
| 別マシンで開いた管理画面のIDが違う | `--create-room` でルームを作り直すとIDが変わる。古いブックマークを開いていないか |
| **標準の管理画面のボタンが効かない**（表示は動く） | 開いている URL の `id` が `bridge_config.json` の `room_id` と一致しているか。`--urls` で出た管理URL（`#k=` 付き）から開き直す |
| 標準の管理画面で操作しても演壇画面が変わらない | 管理画面と演壇画面で `id` が違っている。両方 `--urls` の URL から開き直す |
| `cooldown` が返る | 同じ操作を短時間に繰り返している。`command_cooldown_sec` を調整 |
| リセットしても秒数が戻らない | `profile` か `sec` を付けているか（付けないと停止のみ） |
| スタートしたら秒数が変わった | `/start` に `profile` を付けている。表示のまま開始したいなら付けない |
| 同じイベントで2回動く | `req` を付けているか。イベントごとに違う値になっているか |
| ブラウザから `fetch` で CORS エラー | `cors_allow_origin` が空になっていないか。HTTPS ページから HTTP を叩いていないか |
| `PROXY 失敗 … Connection refused` | TimePon(PHP) が起動していない。`php.log` の**最終行の時刻**を見る。エラー発生時刻より前で止まっていれば、その時点で PHP が終了している（スリープ・クラッシュ）。最新の `start_timepon.command` なら自動復帰します |
| ログが `PROXY å¤±æ` のように化ける | ロケール未設定。最新の `start_timepon.command` を使う（UTF-8 を固定します） |
| 起動時に `CHECK TimePon に接続できません` | 同上。PHP 側の起動失敗 |
| `AttributeError` などの例外がログに出る | v1.3 で例外ガードを追加済み。古い版なら差し替える |
| 演壇画面が固まる（Mac） | `PHP_CLI_SERVER_WORKERS=4` を付けて起動しているか |
| 演壇画面が固まる（Windows） | PHP 内蔵サーバのシングルスレッド問題。XAMPP へ移行 |
