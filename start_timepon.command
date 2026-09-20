#!/bin/bash
# TimePon 一括起動スクリプト（macOS / Finder でダブルクリック可）
#
# このファイルがあるフォルダを基準に動くので、フォルダごとどこへ移動しても
# 中身を書き換えずにそのまま使えます。
#
#   ＜任意のフォルダ＞/
#   ├── timepon/              index.php と data/（TimePon 本体）
#   ├── timepon_bridge.py
#   ├── bridge_config.json
#   └── start_timepon.command  ← これ
#
# 終了するときは、このウィンドウで Ctrl+C を押してください。

set -u

# --- 文字化け対策（ロケール未設定のアカウントでも日本語ログを正しく出す）---
# ロケール名は環境によって存在しないことがあるので、実在するものだけ採用する
if [ -z "${LANG:-}" ]; then
  for _l in ja_JP.UTF-8 en_US.UTF-8 C.UTF-8; do
    if locale -a 2>/dev/null | grep -qix "$(echo "$_l" | tr -d '-')\|$_l"; then export LANG="$_l"; break; fi
  done
fi
export PYTHONUTF8=1            # Python の入出力を常に UTF-8 に固定
export PYTHONIOENCODING=utf-8

cd "$(dirname "$0")" || exit 1
BASE="$PWD"

PHP_PORT="${PHP_PORT:-18080}"
TIMEPON_DIR="$BASE/timepon"
BRIDGE="$BASE/timepon_bridge.py"
PHP_LOG="$BASE/php.log"

die() { echo ""; echo "[NG] $*"; echo ""; echo "何かキーを押すと閉じます。"; read -r _; exit 1; }

# --- 実行ファイルの場所を探す（Homebrew / Apple Silicon / Intel / CLT に対応）---
find_bin() {
  for c in "$@"; do
    if [ -x "$c" ]; then echo "$c"; return 0; fi
    if command -v "$c" >/dev/null 2>&1; then command -v "$c"; return 0; fi
  done
  return 1
}
PHP_BIN="$(find_bin /opt/homebrew/bin/php /usr/local/bin/php php)" \
  || die "php が見つかりません。 brew install php を実行してください。"
PY_BIN="$(find_bin "$BASE/venv/bin/python" /opt/homebrew/bin/python3 /usr/bin/python3 python3)" \
  || die "python3 が見つかりません。"

# --- 事前チェック ---
[ -f "$TIMEPON_DIR/index.php" ] || die "$TIMEPON_DIR/index.php がありません。フォルダ構成を確認してください。"
[ -f "$BRIDGE" ] || die "$BRIDGE がありません。"
[ -r "$TIMEPON_DIR/index.php" ] || die "index.php を読み取れません。別アカウントのフォルダを参照していませんか？
     ls -l \"$TIMEPON_DIR\" で所有者と権限を確認してください。"
if ! ( : > "$TIMEPON_DIR/.write_test" ) 2>/dev/null; then
  die "$TIMEPON_DIR に書き込めません（ルーム情報を保存できません）。
     フォルダの権限か、置き場所（他ユーザーのホーム配下ではないか）を確認してください。"
fi
rm -f "$TIMEPON_DIR/.write_test"

LSOF="$(find_bin /usr/sbin/lsof /usr/bin/lsof lsof || true)"
if [ -n "${LSOF:-}" ] && "$LSOF" -nP -iTCP:"$PHP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[!] ポート $PHP_PORT はすでに使用中です。使用しているプロセス:"
  "$LSOF" -nP -iTCP:"$PHP_PORT" -sTCP:LISTEN | sed -n '1,5p'
  die "先に終了させるか、PHP_PORT=8090 ./start_timepon.command のように別ポートで起動してください。"
fi

echo "========================================================"
echo " TimePon 起動"
echo "   フォルダ : $BASE"
echo "   PHP      : $PHP_BIN ($("$PHP_BIN" -r 'echo PHP_VERSION;' 2>/dev/null))"
echo "   Python   : $PY_BIN"
echo "========================================================"

# --- スリープ抑止（このスクリプトが動いている間だけ有効）---
CAFF="$(find_bin /usr/bin/caffeinate caffeinate || true)"
if [ -n "${CAFF:-}" ]; then
  "$CAFF" -dimsu -w $$ &
  echo "スリープ抑止を有効にしました（終了すると自動で解除されます）"
fi

# ゾンビ（終了済みで未回収）を「生存」と誤判定しないための生死判定
alive() { ps -p "$1" -o state= 2>/dev/null | grep -qv '^Z'; }

start_php() {
  PHP_CLI_SERVER_WORKERS=4 "$PHP_BIN" -S 0.0.0.0:"$PHP_PORT" -t "$TIMEPON_DIR" >> "$PHP_LOG" 2>&1 &
  PHP_PID=$!
}
start_bridge() {
  "$PY_BIN" "$BRIDGE" &
  BRIDGE_PID=$!
}

STOPPING=0
cleanup() {
  STOPPING=1
  echo ""
  echo "終了します..."
  kill "${PHP_PID:-}" "${BRIDGE_PID:-}" 2>/dev/null
  wait 2>/dev/null
  exit 0
}
trap cleanup INT TERM

# --- TimePon 本体（データは index.php と同じ場所の data/ に入る）---
: > "$PHP_LOG"
start_php

# --- PHP が実際に応答するまで待つ（ここで落ちていれば理由を表示して止まる）---
printf "TimePon(PHP) の起動を確認中"
OK=0
for _ in $(seq 1 20); do
  if ! alive "$PHP_PID"; then break; fi
  if /usr/bin/curl -s -o /dev/null -m 2 "http://127.0.0.1:$PHP_PORT/"; then OK=1; break; fi
  printf "."
  sleep 0.5
done
echo ""
# 別プロセスが同じポートで応答していると curl だけでは判定を誤るので、ログも見る
if grep -qi "Failed to listen" "$PHP_LOG" 2>/dev/null; then OK=0; fi
if [ "$OK" != "1" ]; then
  echo "----- php.log -----"
  tail -20 "$PHP_LOG"
  echo "-------------------"
  die "TimePon(PHP) が起動しませんでした。上のログを確認してください。
     よくある原因: ポートの重複 / php の実行権限 / フォルダの権限"
fi
echo "TimePon(PHP) 起動OK  http://127.0.0.1:$PHP_PORT/"

# --- ブリッジ（bridge_config.json はこのフォルダのものを使う）---
start_bridge

echo ""
echo "起動しました。終了するには Ctrl+C を押してください。"
echo "（どちらかのプロセスが落ちても自動で再起動します）"

# --- 監視ループ: 落ちたら自動再起動する ---
PHP_RESTARTS=0
BRIDGE_RESTARTS=0
while [ "$STOPPING" = "0" ]; do
  sleep 2
  if ! alive "$PHP_PID"; then
    wait "$PHP_PID" 2>/dev/null
    PHP_RESTARTS=$((PHP_RESTARTS + 1))
    echo "$(date '+%Y-%m-%d %H:%M:%S') [!] TimePon(PHP) が終了しました。再起動します（通算 ${PHP_RESTARTS} 回目）"
    tail -3 "$PHP_LOG" | sed 's/^/      /'
    start_php
  fi
  if ! alive "$BRIDGE_PID"; then
    wait "$BRIDGE_PID" 2>/dev/null
    BRIDGE_RESTARTS=$((BRIDGE_RESTARTS + 1))
    echo "$(date '+%Y-%m-%d %H:%M:%S') [!] ブリッジが終了しました。再起動します（通算 ${BRIDGE_RESTARTS} 回目）"
    start_bridge
  fi
done
