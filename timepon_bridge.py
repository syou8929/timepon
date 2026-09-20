#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TimePon Bridge - TimePon(index.php) を一切改造せずに外部から制御するサイドカー

役割:
  1. UDP / TCP で外部ソフトからの信号を受け取り、指定秒数でリセット＆スタート
  2. 司会用の「リセット＆スタート」1ボタンページ (HTTP) を配信
  3. TimePon 本体の管理画面・演壇画面はそのまま併用可能

依存: Python 3.9+ の標準ライブラリのみ

使い方:
  python timepon_bridge.py --create-room   # ルームを新規作成して設定に保存
  python timepon_bridge.py                 # 常駐起動
"""

import argparse
import json
import os
import platform
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("TIMEPON_BRIDGE_CONFIG") or os.path.join(APP_DIR, "bridge_config.json")

DEFAULT_CONFIG = {
    "timepon_base": "http://127.0.0.1:8080/",
    "timepon_public_base": "",
    "proxy_prefix": "/tp",
    "room_id": "",
    "admin_key": "",
    "http_host": "0.0.0.0",
    "http_port": 8081,
    "udp_host": "0.0.0.0",
    "udp_port": 5010,
    "tcp_host": "0.0.0.0",
    "tcp_port": 5011,
    "trigger_token": "",
    "cors_allow_origin": "*",
    "cooldown_sec": 3.0,
    "command_cooldown_sec": 0.8,
    "idempotency_ttl_sec": 120,
    "clear_message_on_trigger": True,
    "default_profile_udp": "net",
    "default_profile_tcp": "net",
    "default_profile_http": "mc",
    "profiles": {
        "mc": {"label": "司会スタート", "durationSec": 300, "warn1Sec": 60, "warn2Sec": 30},
        "net": {"label": "外部ソフト", "durationSec": 420, "warn1Sec": 60, "warn2Sec": 30},
    },
    "footswitch": {
        "enabled": False,
        "keys": ["f13"],
        "action": "trigger",
        "profile": "mc",
        "min_interval_sec": 1.0
    },
    "log_file": "bridge.log",
}

_log_lock = threading.Lock()
CONFIG = dict(DEFAULT_CONFIG)


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _log_lock:
        print(line, flush=True)
        path = CONFIG.get("log_file")
        if path:
            if not os.path.isabs(path):
                path = os.path.join(APP_DIR, path)
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass


_throttle_state = {}


def log_throttled(key, msg, interval=15.0):
    """同じ種類のエラーが連発するときに、ログを間引いて出す"""
    now = time.monotonic()
    last, skipped = _throttle_state.get(key, (-1e9, 0))
    if now - last < interval:
        _throttle_state[key] = (last, skipped + 1)
        return
    if skipped:
        msg += "（同種のログ %d 件を省略）" % skipped
    _throttle_state[key] = (now, 0)
    log(msg)


def upstream_down_note(err):
    if "refused" in str(err).lower():
        return "  ← TimePon(PHP) が起動していません。php.log を確認してください"
    return ""


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("設定ファイルを作成しました: %s" % CONFIG_PATH)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def remaining_ms(st, now_ms):
    """TimePon の state から残りミリ秒を計算する"""
    dur = int(st.get("durationSec") or 0) * 1000
    if st.get("state") == "running":
        return dur - (now_ms - int(st.get("startedAtMs") or 0) - int(st.get("pausedAccumMs") or 0))
    if st.get("state") == "paused":
        return dur - (int(st.get("pausedAtMs") or 0) - int(st.get("startedAtMs") or 0)
                      - int(st.get("pausedAccumMs") or 0))
    return dur


class TimeponClient:
    """index.php の公開 API を、ブラウザの代わりに叩くクライアント"""

    def __init__(self, base, room_id, admin_key):
        self.base = base if base.endswith("/") else base + "/"
        parts = urllib.parse.urlsplit(self.base)
        # index.php の same_origin_ok() は Origin と Host の一致を見るので、
        # 接続先と同じ値を自分で付ける（PHP 側の改造は不要）
        self.origin = "%s://%s" % (parts.scheme, parts.netloc)
        self.room_id = room_id
        self.admin_key = admin_key

    def _request(self, act, fields=None, method="POST"):
        url = self.base + "?act=" + urllib.parse.quote(act)
        data = None
        if fields:
            if method == "POST":
                data = urllib.parse.urlencode(fields).encode("utf-8")
            else:
                url += "&" + urllib.parse.urlencode(fields)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("Origin", self.origin)
        req.add_header("Referer", self.base)
        req.add_header("Cache-Control", "no-store")
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=6) as res:
            body = res.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            raise RuntimeError("TimePon から JSON 以外の応答: %s" % body[:200])

    def create_room(self):
        j = self._request("create")
        if not j.get("ok"):
            raise RuntimeError("ルーム作成に失敗: %s" % j)
        return j["id"], j["adminKey"]

    def get_state(self):
        return self._request("get", {"id": self.room_id, "t": int(time.time() * 1000)}, method="GET")

    def _set(self, cmd, extra=None):
        fields = {"id": self.room_id, "k": self.admin_key, "cmd": cmd}
        if extra:
            fields.update(extra)
        j = self._request("set", fields)
        if not j.get("ok"):
            raise RuntimeError("cmd=%s が拒否されました: %s" % (cmd, j))
        return j

    def apply_settings(self, duration_sec, warn1=None, warn2=None):
        # setSettings の durSec は 1..720 に丸められる仕様なので、
        # 警告秒だけをここで設定し、実際の持ち時間は start 側で渡す
        dur_for_settings = max(1, min(720, int(duration_sec)))
        fields = {"id": self.room_id, "k": self.admin_key, "durSec": dur_for_settings}
        if warn1 is not None:
            fields["warn1Sec"] = max(0, min(dur_for_settings, int(warn1)))
        if warn2 is not None:
            fields["warn2Sec"] = max(0, min(dur_for_settings, int(warn2)))
        j = self._request("setSettings", fields)
        if not j.get("ok"):
            raise RuntimeError("setSettings が拒否されました: %s" % j)

    def brief_state(self):
        j = self.get_state()
        st = j.get("state", {}) or {}
        now = int(j.get("serverNowMs") or time.time() * 1000)
        return {"state": st.get("state", "idle"),
                "durationSec": int(st.get("durationSec") or 0),
                "remainingMs": remaining_ms(st, now)}

    def stop_and_reset(self, clear_message=True, duration_sec=None, warn1=None, warn2=None):
        """
        停止して idle に戻す。duration_sec を渡すと持ち時間もその値に戻す（スタートはしない）。
        setSettings の durSec は 720 秒までなので、それを超える場合だけ
        reset → start → pause で「その秒数のまま待機」させる。
        """
        if clear_message:
            try:
                self._set("message", {"text": ""})
            except Exception as e:
                log("WARN カンペ消去に失敗: %s" % e)
        self._set("reset")
        note = ""
        if duration_sec:
            d = max(5, min(86400, int(duration_sec)))
            if d <= 720:
                self.apply_settings(d, warn1, warn2)
            else:
                if warn1 is not None or warn2 is not None:
                    self.apply_settings(d, warn1, warn2)     # 警告秒だけ先に反映
                self._set("start", {"durationSec": d})
                self._set("pause")
                note = "（12分超のため一時停止状態で待機）"
        return self.brief_state(), note

    def start_timer(self, duration_sec=None, warn1=None, warn2=None):
        """idle なら指定秒数で開始、paused なら再開、running なら何もしない"""
        before = self.brief_state()
        if before["state"] == "idle" and duration_sec:  # 秒数指定つきの開始
            duration_sec = max(5, min(86400, int(duration_sec)))
            if warn1 is not None or warn2 is not None:
                self.apply_settings(duration_sec, warn1, warn2)
            self._set("start", {"durationSec": duration_sec})
        else:
            self._set("start")
        return before["state"], self.brief_state()

    def pause_timer(self):
        before = self.brief_state()
        self._set("pause")
        return before["state"], self.brief_state()

    def reset_and_start(self, duration_sec, warn1=None, warn2=None, clear_message=True):
        duration_sec = max(5, min(86400, int(duration_sec)))
        if warn1 is not None or warn2 is not None:
            self.apply_settings(duration_sec, warn1, warn2)
        if clear_message:
            try:
                self._set("message", {"text": ""})
            except Exception as e:  # カンペ消去の失敗は致命ではない
                log("WARN カンペ消去に失敗: %s" % e)
        self._set("reset")                                   # state -> idle
        self._set("start", {"durationSec": duration_sec})    # idle のときだけ秒数を渡せる
        return duration_sec


ACTIONS = ("trigger", "reset", "start", "pause")
ACTION_ALIASES = {
    "go": "trigger", "restart": "trigger", "resetstart": "trigger", "reset_start": "trigger",
    "stop": "reset", "stopreset": "reset", "stop_reset": "reset",
    "resume": "start", "unpause": "start",
}
ACTION_LABELS = {"trigger": "リセット＆スタート", "reset": "ストップ＆リセット",
                 "start": "スタート", "pause": "一時停止"}


def normalize_action(name):
    n = str(name or "").strip().lower().replace("-", "_")
    n = ACTION_ALIASES.get(n.replace("_", ""), ACTION_ALIASES.get(n, n))
    return n


class Controller:
    """クールダウン＋冪等キー付きで TimePon を操作する"""

    def __init__(self, client, cfg):
        self.client = client
        self.cfg = cfg
        self.lock = threading.Lock()
        self.last_fire = {}        # action -> monotonic
        self.last_result = {"at": 0, "profile": "", "ok": None, "detail": ""}
        self.history = []          # 直近の実行結果（新しい順・最大20件）
        self._recent = {}          # req_id -> (monotonic, result)  再送検出用

    def _result(self, ok, code, message, action="", profile="", duration=None,
                duplicate=False, state=None):
        return {
            "ok": bool(ok),
            "code": code,          # done / duplicate / cooldown / unknown_profile / unknown_action / error
            "message": message,
            "action": action,
            "profile": profile,
            "durationSec": duration,
            "duplicate": bool(duplicate),
            "state": state,        # 実行後の TimePon 側の状態
            "atMs": int(time.time() * 1000),
        }

    def _purge(self, now):
        ttl = float(self.cfg.get("idempotency_ttl_sec", 120))
        for k, (ts, _r) in list(self._recent.items()):
            if now - ts > ttl:
                del self._recent[k]

    def execute(self, action, profile_name, source, duration_override=None, req_id=None,
                profile_explicit=None):
        now = time.monotonic()
        if profile_explicit is None:
            profile_explicit = bool(profile_name)
        action = normalize_action(action) or "trigger"
        req_id = (req_id or "").strip()[:120]
        label = ACTION_LABELS.get(action, action)

        if action not in ACTIONS:
            log("NG  未対応の操作 '%s' (%s)" % (action, source))
            return self._result(False, "unknown_action", "未対応の操作です: %s" % action, action)

        # 1. 再送（同じリクエストID）なら実行せず前回の結果を返す
        if req_id:
            with self.lock:
                self._purge(now)
                hit = self._recent.get(req_id)
            if hit:
                res = dict(hit[1])
                res.update({"duplicate": True, "code": "duplicate"})
                res["message"] = "再送を検出しました（前回の結果を返します）: " + res["message"]
                log("DUP %s (%s) req=%s 前回結果を返却" % (action, source, req_id))
                return res

        # 2. プロファイル解決
        if action == "trigger" and not profile_name:
            profile_name = self.cfg.get("default_profile_http", "mc")
        prof = self.cfg["profiles"].get(profile_name) or {}
        if action in ("trigger", "reset", "start") and profile_name and not prof:
            log("NG  未定義のプロファイル '%s' (%s)" % (profile_name, source))
            return self._result(False, "unknown_profile",
                                "プロファイルが未定義です: %s" % profile_name, action, profile_name)

        # 3. クールダウン（操作の種類ごとに独立。reset の直後に start を送れる）
        limit = float(self.cfg.get("cooldown_sec", 3.0) if action == "trigger"
                      else self.cfg.get("command_cooldown_sec", 0.8))
        with self.lock:
            wait = limit - (now - self.last_fire.get(action, -1e9))
            if wait > 0:
                log("SKIP %s (%s) クールダウン中 残り%.1fs" % (action, source, wait))
                return self._result(False, "cooldown",
                                    "クールダウン中です（残り %.1f 秒）" % wait, action, profile_name)
            self.last_fire[action] = now

        clear_msg = bool(self.cfg.get("clear_message_on_trigger", True))
        dur = None
        try:
            if action == "trigger":
                dur = int(duration_override or prof.get("durationSec", 300))
                dur = self.client.reset_and_start(dur, prof.get("warn1Sec"), prof.get("warn2Sec"), clear_msg)
                after = self.client.brief_state()
                msg = "%d 秒でリセット＆スタートしました" % dur

            elif action == "reset":
                # プロファイルか秒数が明示されたときだけ「その秒数に戻す」
                dur = (int(duration_override) if duration_override
                       else (prof.get("durationSec") if profile_explicit else None))
                warns = (prof.get("warn1Sec"), prof.get("warn2Sec")) if (dur and prof) else (None, None)
                after, note = self.client.stop_and_reset(clear_msg, dur, warns[0], warns[1])
                msg = ("%d 秒にリセットしました%s" % (after.get("durationSec") or dur, note)
                       if dur else "ストップ＆リセットしました")

            elif action == "start":
                # プロファイル未指定なら今の持ち時間のままスタート（リセット後の再開に使う）
                dur = (int(duration_override) if duration_override
                       else (prof.get("durationSec") if profile_explicit else None))
                before, after = self.client.start_timer(dur, prof.get("warn1Sec"), prof.get("warn2Sec"))
                if before == "idle":
                    msg = "%d 秒でスタートしました" % (after.get("durationSec") or 0)
                elif before == "paused":
                    msg = "一時停止から再開しました"
                else:
                    msg = "すでに進行中です（変更なし）"
                dur = after.get("durationSec")

            else:  # pause
                before, after = self.client.pause_timer()
                msg = "一時停止しました" if before == "running" else "進行中ではありません（変更なし）"

            log("OK  %s (%s)%s → %s" % (action, source, (" req=%s" % req_id) if req_id else "", msg))
            res = self._result(True, "done", msg, action, profile_name, dur, state=after)
        except Exception as e:
            log("NG  %s (%s) 失敗: %s" % (action, source, e))
            res = self._result(False, "error", "TimePon に送れませんでした: %s" % e,
                               action, profile_name)

        with self.lock:
            self.last_result = {"at": res["atMs"], "profile": profile_name,
                                "ok": res["ok"], "detail": res["message"]}
            self.history.insert(0, {"atMs": res["atMs"], "action": action, "label": label,
                                    "profile": profile_name, "ok": res["ok"], "code": res["code"],
                                    "durationSec": res["durationSec"], "source": source})
            del self.history[20:]
            if req_id and res["ok"]:      # 成功だけ記録（失敗は再送でリトライさせる）
                self._recent[req_id] = (now, res)
        return res

    # 旧インタフェース（リセット＆スタート）
    def fire(self, profile_name, source, duration_override=None, req_id=None):
        return self.execute("trigger", profile_name, source, duration_override, req_id)


def parse_payload(text, default_profile, token):
    """
    受理する書式（大文字小文字は不問・順不同）:
      START / GO / TRIG            -> リセット＆スタート（既定）
      RESET / STOP                 -> ストップ＆リセット
      RESUME                       -> スタート（idle なら開始・paused なら再開）
      PAUSE                        -> 一時停止
      action=start                 -> 操作を明示指定
      START mc                     -> プロファイル指定
      START net 600                -> 秒数を上書き
      key=SECRET START mc          -> トークン付き
      START mc req=abc123          -> リクエストID付き（再送しても二重発火しない）
    空パケットは既定プロファイルのリセット＆スタートとして扱う
    """
    words = text.replace("\r", " ").replace("\n", " ").split()
    given_token = ""
    action = "trigger"
    profile = None
    duration = None
    req_id = ""
    word_actions = {"start": "trigger", "go": "trigger", "trig": "trigger", "trigger": "trigger",
                    "reset": "reset", "stop": "reset",
                    "resume": "start", "unpause": "start", "pause": "pause"}
    for w in words:
        low = w.lower()
        if low.startswith("key=") or low.startswith("token="):
            given_token = w.split("=", 1)[1]
        elif low.startswith("req=") or low.startswith("id="):
            req_id = w.split("=", 1)[1]
        elif low.startswith("action=") or low.startswith("cmd="):
            action = normalize_action(w.split("=", 1)[1])
        elif low in word_actions:
            action = word_actions[low]
        elif w.isdigit():
            duration = int(w)
        else:
            profile = w
    if token and given_token != token:
        return None, None, None, None, "bad token"
    return action, profile, duration, req_id, None


def start_udp_listener(controller, cfg):
    host, port = cfg["udp_host"], int(cfg["udp_port"])
    if not port:
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as e:
        log("UDP  待受を開始できません %s:%d (%s) — UDP 経路のみ無効にして続行します" % (host, port, e))
        return

    def loop():
        log("UDP  待受 %s:%d" % (host, port))
        while True:
            try:
                data, addr = sock.recvfrom(1024)
            except OSError:
                break
            text = data.decode("utf-8", "replace").strip()
            action, profile, dur, req_id, err = parse_payload(
                text, cfg["default_profile_udp"], cfg.get("trigger_token", ""))
            if action == "trigger" and not profile:
                profile = cfg["default_profile_udp"]
            src = "UDP %s:%d %r" % (addr[0], addr[1], text[:40])
            if err:
                log("NG  %s → %s" % (src, err))
                continue
            res = controller.execute(action, profile, src, dur, req_id)
            try:
                sock.sendto((("OK " if res["ok"] else "NG ") + res["code"] + " " +
                             res["message"] + "\n").encode(), addr)
            except OSError:
                pass

    threading.Thread(target=loop, daemon=True).start()


def start_tcp_listener(controller, cfg):
    host, port = cfg["tcp_host"], int(cfg["tcp_port"])
    if not port:
        return

    class Handler(socketserver.StreamRequestHandler):
        timeout = 10

        def handle(self):
            try:
                line = self.rfile.readline(1024)
            except OSError:
                return
            text = (line or b"").decode("utf-8", "replace").strip()
            action, profile, dur, req_id, err = parse_payload(
                text, cfg["default_profile_tcp"], cfg.get("trigger_token", ""))
            if action == "trigger" and not profile:
                profile = cfg["default_profile_tcp"]
            src = "TCP %s %r" % (self.client_address[0], text[:40])
            if err:
                log("NG  %s → %s" % (src, err))
                self.wfile.write(b"NG bad_token\n")
                return
            res = controller.execute(action, profile, src, dur, req_id)
            self.wfile.write((("OK " if res["ok"] else "NG ") + res["code"] + " " +
                              res["message"] + "\n").encode())

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    try:
        srv = Server((host, port), Handler)
    except OSError as e:
        log("TCP  待受を開始できません %s:%d (%s) — TCP 経路のみ無効にして続行します" % (host, port, e))
        return
    log("TCP  待受 %s:%d" % (host, port))
    threading.Thread(target=srv.serve_forever, daemon=True).start()


MC_PAGE = """<!doctype html>
<html lang="ja">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>進行操作</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; background: #14181d; color: #e8ecf1;
    font-family: "Segoe UI", "Yu Gothic UI", system-ui, sans-serif;
    display: flex; flex-direction: column; gap: 16px; padding: 20px;
    -webkit-user-select: none; user-select: none; -webkit-tap-highlight-color: transparent;
  }
  header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
  .room { font-size: 15px; color: #8b96a3; letter-spacing: .04em; }
  .clock { font-variant-numeric: tabular-nums; font-size: clamp(44px, 13vw, 92px); font-weight: 300; line-height: 1; }
  .clock small { display: block; font-size: 14px; font-weight: 400; color: #8b96a3; margin-top: 6px; letter-spacing: .04em; }
  .clock.running { color: #7fd8a8; }
  .clock.over { color: #ff8a80; }
  button.go {
    flex: 1 1 auto; min-height: 46vh; border: none; border-radius: 18px;
    background: #2f7d5a; color: #fff; font-size: clamp(28px, 7vw, 48px); font-weight: 600;
    letter-spacing: .06em; cursor: pointer; transition: background .12s ease, transform .08s ease;
  }
  button.go:active { transform: scale(.985); background: #276b4d; }
  button.go:focus-visible { outline: 3px solid #9fe3c0; outline-offset: 4px; }
  button.go:disabled { background: #39424c; color: #8b96a3; cursor: default; }
  button.go .sub { display: block; margin-top: 10px; font-size: 16px; font-weight: 400; opacity: .85; letter-spacing: .02em; }
  .status { min-height: 22px; font-size: 15px; color: #8b96a3; }
  .status.ok { color: #7fd8a8; }
  .status.ng { color: #ff8a80; }
  @media (prefers-reduced-motion: reduce) { button.go { transition: none; } }
</style>
<header>
  <div class="clock" id="clock">--:--<small id="stateLabel">接続中</small></div>
  <div class="room" id="room"></div>
</header>
<button class="go" id="go">リセット＆スタート<span class="sub" id="goSub"></span></button>
<div class="status" id="status"></div>
<script>
const $ = (id) => document.getElementById(id);
let remainMs = null, lastSync = 0, running = false, busy = false;

function fmt(ms) {
  const over = ms < 0; const t = Math.floor(Math.abs(ms) / 1000);
  const m = String(Math.floor(t / 60)).padStart(2, '0'), s = String(t % 60).padStart(2, '0');
  return (over ? '-' : '') + m + ':' + s;
}
function tick() {
  if (remainMs !== null) {
    const shown = running ? remainMs - (Date.now() - lastSync) : remainMs;
    $('clock').firstChild.nodeValue = fmt(shown);
    $('clock').className = 'clock' + (shown < 0 ? ' over' : (running ? ' running' : ''));
  }
  requestAnimationFrame(tick);
}
async function poll() {
  try {
    const r = await fetch('/state?t=' + Date.now(), { cache: 'no-store' });
    const j = await r.json();
    if (j.ok) {
      remainMs = j.remainingMs; lastSync = Date.now(); running = (j.state === 'running');
      $('stateLabel').textContent = { idle: '待機中', running: '進行中', paused: '一時停止' }[j.state] || j.state;
      $('room').textContent = 'ROOM ' + j.roomId;
      $('goSub').textContent = j.profileLabel + ' / ' + Math.floor(j.profileDurationSec / 60) + '分' +
        (j.profileDurationSec % 60 ? String(j.profileDurationSec % 60) + '秒' : '');
    }
  } catch (e) { $('stateLabel').textContent = '通信できません'; }
  setTimeout(poll, 1000);
}
$('go').addEventListener('click', async () => {
  if (busy) return;
  busy = true; $('go').disabled = true; $('status').className = 'status'; $('status').textContent = '送信中';
  try {
    const r = await fetch('/trigger', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'trigger', profile: 'mc', req: 'mc-' + Date.now() }) });
    const j = await r.json();
    $('status').className = 'status ' + (j.ok ? 'ok' : 'ng');
    $('status').textContent = j.ok ? 'スタートしました' : ('送れませんでした: ' + j.message);
  } catch (e) {
    $('status').className = 'status ng'; $('status').textContent = '送れませんでした: ' + e;
  }
  setTimeout(() => { busy = false; $('go').disabled = false; }, 1500);
});
// USBフットスイッチがキーボードとして刺さっている場合、このページを最前面にしておけば
// OS の入力監視権限なしでも押下を拾える（Plan B）
const HOTKEYS = __KEYS__;
window.addEventListener('keydown', (e) => {
  if (e.repeat) return;
  const k = (e.key || '').toLowerCase(), c = (e.code || '').toLowerCase();
  if (HOTKEYS.includes(k) || HOTKEYS.includes(c)) { e.preventDefault(); $('go').click(); }
});
tick(); poll();
</script>
</html>
"""


KEYTEST_PAGE = """<!doctype html>
<html lang="ja">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>キー確認</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; background:#14181d; color:#e8ecf1; padding:24px;
         font-family:"Segoe UI","Yu Gothic UI",system-ui,sans-serif; }
  h1 { font-size:18px; font-weight:500; margin:0 0 6px; }
  p  { font-size:14px; color:#8b96a3; margin:0 0 18px; }
  .big { font-size:clamp(28px,7vw,52px); font-variant-numeric:tabular-nums;
         padding:28px; border:1px dashed #39424c; border-radius:14px; text-align:center;
         color:#7fd8a8; min-height:1.2em; }
  .big.none { color:#8b96a3; font-size:18px; }
  table { width:100%; border-collapse:collapse; font-size:13px; margin-top:20px; color:#b9c2cc; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid #232a31; }
  th { color:#8b96a3; font-weight:500; }
  code { background:#1d232a; padding:2px 6px; border-radius:5px; color:#c9d3de; }
</style>
<h1>フットスイッチ キー確認</h1>
<p>このページを開いたまま、フットスイッチを踏んでください。ブラウザが受け取ったキーを表示します。
   OS の権限設定も追加インストールも不要です。ここで反応があれば、ペダルとケーブルは正常です。</p>
<div class="big none" id="last">まだ何も検出していません</div>
<table><thead><tr><th>時刻</th><th>event.key</th><th>event.code</th><th>keyCode</th>
<th>設定に書く値</th></tr></thead><tbody id="log"></tbody></table>
<script>
const $ = (id) => document.getElementById(id);
window.addEventListener('keydown', (e) => {
  e.preventDefault();
  const key = e.key, code = e.code;
  $('last').className = 'big';
  $('last').textContent = key === ' ' ? '(space)' : key;
  const guess = (key.length === 1 ? key.toLowerCase() : (code || key).toLowerCase()
                 .replace(/^key|^digit/, ''));
  const tr = document.createElement('tr');
  tr.innerHTML = `<td>${new Date().toLocaleTimeString()}</td><td><code>${key}</code></td>` +
                 `<td><code>${code}</code></td><td>${e.keyCode}</td><td><code>${guess}</code></td>`;
  $('log').prepend(tr);
  while ($('log').children.length > 15) $('log').lastChild.remove();
});
</script>
</html>
"""


PANEL_PAGE = """<!doctype html>
<html lang="ja">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TimePon トリガーパネル</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; min-height:100vh; background:#14181d; color:#e8ecf1; padding:20px;
         font-family:"Segoe UI","Yu Gothic UI",system-ui,sans-serif; }
  header { display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:18px; }
  .clock { font-variant-numeric:tabular-nums; font-size:clamp(36px,9vw,64px); font-weight:300; line-height:1; }
  .clock small { display:block; font-size:13px; color:#8b96a3; margin-top:6px; }
  .clock.running { color:#7fd8a8; } .clock.over { color:#ff8a80; }
  .room { font-size:14px; color:#8b96a3; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }
  button.go { border:none; border-radius:14px; background:#2f7d5a; color:#fff; padding:26px 18px;
              font-size:20px; font-weight:600; cursor:pointer; text-align:left; }
  button.go:active { background:#276b4d; }
  button.go:disabled { background:#39424c; color:#8b96a3; cursor:default; }
  button.go .sub { display:block; margin-top:8px; font-size:14px; font-weight:400; opacity:.85; }
  .card { display:flex; flex-direction:column; gap:8px; }
  .card button.go { width:100%; }
  .subrow { display:flex; gap:8px; }
  .subrow button.ctl { flex:1 1 0; padding:10px 8px; font-size:14px; }
  .ctlrow { display:flex; flex-wrap:wrap; gap:10px; margin-top:16px;
            border-top:1px solid #232a31; padding-top:16px; }
  button.ctl { flex:1 1 140px; border:1px solid #39424c; border-radius:10px; background:#1d232a;
               color:#e8ecf1; padding:14px 12px; font-size:15px; cursor:pointer; }
  button.ctl:active { background:#252d36; }
  button.ctl:disabled { color:#8b96a3; cursor:default; }
  .status { margin:16px 0; min-height:22px; font-size:15px; color:#8b96a3; }
  .status.ok { color:#7fd8a8; } .status.ng { color:#ff8a80; }
  table { width:100%; border-collapse:collapse; font-size:13px; color:#b9c2cc; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid #232a31; white-space:nowrap; }
  th { color:#8b96a3; font-weight:500; }
  td.ng { color:#ff8a80; }
  h2 { font-size:14px; color:#8b96a3; font-weight:500; margin:24px 0 6px; }
  .links { display:flex; flex-wrap:wrap; gap:10px; }
  .links a.lnk, .links a { display:inline-block; margin-right:8px; padding:10px 14px; border:1px solid #39424c; border-radius:10px;
             background:#1d232a; color:#e8ecf1; text-decoration:none; font-size:14px; }
  code { background:#1d232a; padding:2px 6px; border-radius:5px; color:#c9d3de; }
</style>
<header>
  <div class="clock" id="clock">--:--<small id="stateLabel">接続中</small></div>
  <div class="room" id="room"></div>
</header>
<div class="grid" id="buttons">__BUTTONS__</div>
<div class="ctlrow">__CONTROLS__</div>
<div class="status" id="status"></div>
<h2>直近の受信</h2>
<table><thead><tr><th>時刻</th><th>プロファイル</th><th>操作</th><th>送信元</th></tr></thead>
<tbody id="hist"><tr><td colspan="4">-</td></tr></tbody></table>
<h2>TimePon 本体のURL</h2>
<div style="font-size:13px;color:#b9c2cc;line-height:1.9">__LINKS__</div>
<h2>外部アプリ用URL</h2>
<p style="font-size:13px;color:#8b96a3">
  <code id="sampleUrl"></code> をGETするだけでトリガーします。
  再送で二重発火させたくない場合は <code>&amp;req=一意なID</code> を付けてください。
</p>
<script>
const $ = (id) => document.getElementById(id);
let remainMs = null, lastSync = 0, running = false, busy = false;
$('sampleUrl').textContent = location.origin + '/trigger?profile=net';
function fmt(ms){ const over=ms<0; const t=Math.floor(Math.abs(ms)/1000);
  return (over?'-':'')+String(Math.floor(t/60)).padStart(2,'0')+':'+String(t%60).padStart(2,'0'); }
function tick(){
  if (remainMs !== null) {
    const shown = running ? remainMs - (Date.now()-lastSync) : remainMs;
    $('clock').firstChild.nodeValue = fmt(shown);
    $('clock').className = 'clock' + (shown<0?' over':(running?' running':''));
  }
  requestAnimationFrame(tick);
}
async function poll(){
  try {
    const j = await (await fetch('/state?t='+Date.now(),{cache:'no-store'})).json();
    if (j.ok) {
      remainMs = j.remainingMs; lastSync = Date.now(); running = (j.state === 'running');
      $('stateLabel').textContent = {idle:'待機中',running:'進行中',paused:'一時停止'}[j.state] || j.state;
      $('room').textContent = 'ROOM ' + j.roomId;
      const rows = (j.history||[]).map(h =>
        `<tr><td>${new Date(h.atMs).toLocaleTimeString()}</td><td>${h.profile}</td>` +
        `<td class="${h.ok?'':'ng'}">${h.ok ? h.label : h.code}</td>` +
        `<td>${(h.source||'').replace(/[<>&]/g,'')}</td></tr>`).join('');
      $('hist').innerHTML = rows || '<tr><td colspan="4">-</td></tr>';
    }
  } catch(e) { $('stateLabel').textContent = '通信できません'; }
  setTimeout(poll, 1000);
}
const allBtns = () => document.querySelectorAll('button.go, button.ctl');
allBtns().forEach(b => b.addEventListener('click', async () => {
  if (busy) return;
  busy = true; allBtns().forEach(x=>x.disabled=true);
  $('status').className='status'; $('status').textContent='送信中';
  try {
    const r = await fetch('/cmd/' + b.dataset.action, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ profile: b.dataset.profile || undefined, req: 'panel-' + Date.now() }) });
    const j = await r.json();
    $('status').className = 'status ' + (j.ok ? 'ok' : 'ng');
    $('status').textContent = j.message;
  } catch(e) { $('status').className='status ng'; $('status').textContent='送れませんでした: '+e; }
  setTimeout(()=>{ busy=false; allBtns().forEach(x=>x.disabled=false); }, 900);
}));
tick(); poll();
</script>
</html>
"""

RESULT_PAGE = """<!doctype html>
<html lang="ja">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; background:#14181d; color:#e8ecf1; display:flex;
         flex-direction:column; align-items:center; justify-content:center; gap:14px; padding:24px;
         font-family:"Segoe UI","Yu Gothic UI",system-ui,sans-serif; text-align:center; }
  .mark { font-size:64px; line-height:1; color:__COLOR__; }
  .msg { font-size:20px; }
  .meta { font-size:13px; color:#8b96a3; }
  a { color:#7fd8a8; font-size:14px; }
</style>
<div class="mark">__MARK__</div>
<div class="msg">__MSG__</div>
<div class="meta">__META__</div>
<a href="/panel">パネルを開く</a>
</html>
"""

ONE_PX_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00"
              b"\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class BridgeHTTPHandler(BaseHTTPRequestHandler):
    server_version = "TimeponBridge/1.1"
    protocol_version = "HTTP/1.1"
    controller = None
    client = None
    cfg = None

    def log_message(self, fmt, *args):  # アクセスログは抑制
        pass

    # ---------- 送信ヘルパ ----------

    def _cors(self):
        origin = self.cfg.get("cors_allow_origin", "*")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, url):
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    # ---------- ルーティング ----------

    def do_GET(self):
        self._guard(self._do_get)

    def do_POST(self):
        self._guard(self._do_post)

    def _guard(self, fn):
        try:
            fn()
        except Exception as e:
            log("HTTP 例外: %s: %s" % (type(e).__name__, e))
            try:
                self._json(500, {"ok": False, "code": "internal_error", "message": str(e)})
            except Exception:
                pass

    def _do_get(self):
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        prefix = self.cfg.get("proxy_prefix", "/tp")
        if prefix and (parts.path == prefix or parts.path.startswith(prefix + "/")):
            self._proxy(parts.path)
        elif path in ("/", "/mc"):
            self._send(200, self._mc(), "text/html; charset=utf-8")
        elif path == "/keytest":
            self._send(200, KEYTEST_PAGE, "text/html; charset=utf-8")
        elif path == "/panel":
            self._send(200, self._panel(), "text/html; charset=utf-8")
        elif path == "/state":
            self._json(200, self._state())
        elif path == "/profiles":
            self._json(200, {"ok": True, "roomId": self.cfg["room_id"],
                             "profiles": self.cfg["profiles"],
                             "actions": list(ACTIONS)})
        elif path in ("/admin", "/stage", "/urls"):
            token = self.cfg.get("trigger_token", "")
            if token and params.get("key") != token and not self.client_address[0].startswith("127."):
                self._json(403, {"ok": False, "code": "bad_token", "message": "トークンが違います"})
            elif path == "/admin":
                self._redirect(self._timepon_urls()["admin"])
            elif path == "/stage":
                self._redirect(self._timepon_urls()["stage"])
            elif path == "/direct":
                self._redirect(self._timepon_urls()["adminDirect"])
            else:
                self._json(200, dict(self._timepon_urls(), ok=True, roomId=self.cfg["room_id"]))
        elif path == "/healthz":
            self._json(200, {"ok": True, "roomId": self.cfg["room_id"],
                             "platform": platform.platform()})
        else:
            self._route_action(path, params)

    def _do_post(self):
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        prefix = self.cfg.get("proxy_prefix", "/tp")
        if prefix and (parts.path == prefix or parts.path.startswith(prefix + "/")):
            self._proxy(parts.path, self.rfile.read(length) if length else None)
            return
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        try:
            if raw.strip().startswith("{"):
                body = json.loads(raw)
                if isinstance(body, dict):
                    params.update({k: v for k, v in body.items() if v is not None})
            elif raw:
                params.update({k: v[0] for k, v in urllib.parse.parse_qs(raw).items()})
        except ValueError:
            pass
        self._route_action(path, params)

    def _route_action(self, path, params):
        """
        /trigger  /trigger/<profile>      リセット＆スタート
        /reset    /stop                   ストップ＆リセット
        /start    /start/<profile>        スタート（idle→開始 / paused→再開）
        /pause                            一時停止
        /cmd/<action>[/<profile>]         明示指定
        """
        seg = [x for x in path.split("/") if x]
        action = None
        if seg and seg[0] == "cmd":
            action = normalize_action(seg[1]) if len(seg) > 1 else None
            if len(seg) > 2:
                params.setdefault("profile", seg[2])
        elif seg and normalize_action(seg[0]) in ACTIONS:
            action = normalize_action(seg[0])
            if len(seg) > 1:
                params.setdefault("profile", seg[1])
        if action is None:
            self._json(404, {"ok": False, "code": "not_found", "message": "not found"})
            return
        if params.get("action") or params.get("cmd"):
            action = normalize_action(params.get("action") or params.get("cmd"))
        self._act(action, params)

    # ---------- 実行 ----------

    def _act(self, action, params):
        fmt = str(params.get("format") or "").lower()
        if not fmt:
            accept = (self.headers.get("Accept") or "").lower()
            fmt = "html" if ("text/html" in accept and "application/json" not in accept) else "json"
        redirect = params.get("redirect")
        remote = self.client_address[0]

        token = self.cfg.get("trigger_token", "")
        if token and str(params.get("key", "")) != token and not remote.startswith("127."):
            res = {"ok": False, "code": "bad_token", "message": "トークンが違います",
                   "action": action, "profile": params.get("profile", ""), "durationSec": None,
                   "duplicate": False, "state": None, "atMs": int(time.time() * 1000)}
            self._respond(res, 403, fmt, redirect)
            return

        profile_raw = params.get("profile")
        profile_explicit = profile_raw not in (None, "")
        profile = str(profile_raw) if profile_explicit else None
        sec = params.get("sec") or params.get("durationSec")
        try:
            sec = int(sec) if sec not in (None, "") else None
        except (TypeError, ValueError):
            sec = None
        req_id = str(params.get("req") or params.get("reqId") or params.get("id") or "")

        res = self.controller.execute(action, profile, "HTTP %s" % remote, sec, req_id,
                                      profile_explicit=profile_explicit)
        code = {"done": 200, "duplicate": 200, "cooldown": 429, "unknown_profile": 400,
                "unknown_action": 400, "error": 502}.get(res["code"], 200)
        self._respond(res, code, fmt, redirect)

    def _respond(self, res, code, fmt, redirect):
        if redirect and (redirect.startswith("/") or redirect.startswith("http://")
                         or redirect.startswith("https://")):
            self._redirect(redirect)
            return
        if fmt == "gif":                    # 画像しか読めないアプリ向けのビーコン
            self._send(200, ONE_PX_GIF, "image/gif")
            return
        if fmt in ("text", "txt", "plain"):
            self._send(code, ("OK " if res["ok"] else "NG ") + res["code"] + " " + res["message"] + "\n",
                       "text/plain; charset=utf-8")
            return
        if fmt == "html":
            page = (RESULT_PAGE
                    .replace("__TITLE__", "OK" if res["ok"] else "NG")
                    .replace("__COLOR__", "#7fd8a8" if res["ok"] else "#ff8a80")
                    .replace("__MARK__", "OK" if res["ok"] else "NG")
                    .replace("__MSG__", esc(res["message"]))
                    .replace("__META__", esc("%s / %s" % (
                        ACTION_LABELS.get(res.get("action"), res.get("action", "")), res["code"]))))
            self._send(code, page, "text/html; charset=utf-8")
            return
        self._json(code, res)

    # ---------- TimePon 本体への入口 ----------

    HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                  "te", "trailers", "transfer-encoding", "upgrade", "content-length",
                  "content-encoding", "strict-transport-security"}

    def _proxy(self, path, body=None):
        """
        TimePon を同一オリジンで中継する。目的は CSP の upgrade-insecure-requests を外すこと。
        これが付いたままだと、http://<LAN IP>:8080 で開いたページからの通信が
        すべて https に強制され、TLS 待受のない TimePon に届かなくなる。
        （localhost だけは仕様上アップグレードされないので、ホストPCでは問題が出ない）
        """
        prefix = self.cfg.get("proxy_prefix", "/tp")
        rest = path[len(prefix):].lstrip("/")
        query = urllib.parse.urlsplit(self.path).query
        upstream = self.client.base + rest + (("?" + query) if query else "")

        req = urllib.request.Request(upstream, data=body,
                                     method=self.command)
        for h in ("Content-Type", "Accept", "Accept-Language", "User-Agent", "Cache-Control"):
            v = self.headers.get(h)
            if v:
                req.add_header(h, v)
        # PHP 側の同一オリジン判定は Origin と Host の一致を見るので、上流に合わせて付け直す
        req.add_header("Origin", self.client.origin)
        req.add_header("Referer", self.client.base)

        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                status, headers, data = res.getcode(), res.headers, res.read()
        except urllib.error.HTTPError as e:
            status, headers, data = e.code, e.headers, e.read()
        except Exception as e:
            note = upstream_down_note(e)
            log_throttled("proxy_error", "PROXY 失敗 %s: %s%s" % (upstream, e, note))
            accept = (self.headers.get("Accept") or "").lower()
            if "text/html" in accept:
                page = (RESULT_PAGE.replace("__TITLE__", "TimePon に接続できません")
                        .replace("__COLOR__", "#ff8a80").replace("__MARK__", "NG")
                        .replace("__MSG__", "TimePon 本体に接続できません")
                        .replace("__META__", esc("接続先: %s / %s" % (self.client.base, e))))
                self._send(502, page, "text/html; charset=utf-8")
            else:
                self._json(502, {"ok": False, "code": "proxy_error", "message": str(e),
                                 "upstream": self.client.base})
            return

        self.send_response(status)
        for k, v in headers.items():
            if k.lower() in self.HOP_BY_HOP:
                continue
            if k.lower() == "content-security-policy":
                v = strip_upgrade_insecure(v)
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _timepon_urls(self):
        # 既定はプロキシ経由の相対URL。ホスト名の食い違いも CSP の問題も起きない
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        direct = timepon_urls(self.cfg, host or None)
        prefix = self.cfg.get("proxy_prefix", "/tp")
        if not prefix:
            return dict(direct, adminDirect=direct["admin"], stageDirect=direct["stage"])
        return {
            "base": prefix + "/",
            "admin": "%s/?op=1&id=%s#k=%s" % (prefix, self.cfg["room_id"], self.cfg["admin_key"]),
            "stage": "%s/?id=%s" % (prefix, self.cfg["room_id"]),
            "adminDirect": direct["admin"],
            "stageDirect": direct["stage"],
        }

    # ---------- 表示用 ----------

    def _state(self):
        prof = self.cfg["profiles"].get(self.cfg.get("default_profile_http", "mc"), {})
        base = {"roomId": self.cfg["room_id"],
                "profileLabel": prof.get("label", "mc"),
                "profileDurationSec": int(prof.get("durationSec", 300)),
                "last": self.controller.last_result,
                "history": self.controller.history,
                "serverNowMs": int(time.time() * 1000)}
        try:
            j = self.client.get_state()
            st = j.get("state", {}) or {}
            now = int(j.get("serverNowMs") or time.time() * 1000)
            base.update({"ok": True, "state": st.get("state", "idle"),
                         "remainingMs": remaining_ms(st, now),
                         "durationSec": int(st.get("durationSec") or 0)})
        except Exception as e:
            base.update({"ok": False, "message": str(e)})
        return base

    def _mc(self):
        keys = [str(k).lower() for k in (self.cfg.get("footswitch", {}).get("keys") or [])]
        return MC_PAGE.replace("__KEYS__", json.dumps(keys + ["enter", " "]))

    def _panel(self):
        btns = []
        for name, prof in self.cfg["profiles"].items():
            dur = int(prof.get("durationSec", 300))
            label = "%d分%s" % (dur // 60, ("%d秒" % (dur % 60)) if dur % 60 else "")
            btns.append(
                '<div class="card">'
                '<button class="go" data-action="trigger" data-profile="%s">%s'
                '<span class="sub">リセット＆スタート / %s</span></button>'
                '<div class="subrow">'
                '<button class="ctl" data-action="reset" data-profile="%s">リセット</button>'
                '<button class="ctl" data-action="start" data-profile="%s">スタート</button>'
                '</div></div>'
                % (esc(name), esc(prof.get("label", name)), esc(label), esc(name), esc(name)))
        ctl = []
        for act, lbl in (("reset", "ストップ＆リセット"), ("start", "そのままスタート"),
                         ("pause", "一時停止")):
            ctl.append('<button class="ctl" data-action="%s">%s</button>' % (act, esc(lbl)))
        urls = self._timepon_urls()
        local = self.client_address[0] in ("127.0.0.1", "::1")
        links = ['ROOM <code>%s</code>' % esc(self.cfg["room_id"]),
                 '<a class="lnk" href="/admin" target="_blank">管理画面を開く</a>'
                 '<a class="lnk" href="/stage" target="_blank">演壇画面を開く</a>',
                 '<span style="color:#8b96a3">管理画面は必ずこのリンクから開いてください'
                 '（adminKey が付いた URL に転送されます）。'
                 'ID や #k= が違う URL を直接開くとボタンが効きません。</span>']
        if local:
            links.append('<span style="color:#8b96a3;word-break:break-all">管理URL: <code>%s</code></span>'
                         % esc(urls["admin"]))
        return (PANEL_PAGE.replace("__BUTTONS__", "\n".join(btns))
                          .replace("__CONTROLS__", "\n".join(ctl))
                          .replace("__LINKS__", "<br>".join(links)))


def pynput_key_names(key):
    """pynput のキーオブジェクトから、設定に書ける名前の候補を取り出す"""
    names = set()
    name = getattr(key, "name", None)
    if name:
        names.add(name.lower())
    ch = getattr(key, "char", None)
    if ch:
        names.add(ch.lower())
    vk = getattr(key, "vk", None)
    if vk is not None:
        names.add("vk%d" % vk)
    return names


def watch_keys():
    """--keys: 押されたキーの名前を表示する診断モード"""
    print("=" * 68)
    print(" キー確認モード: フットスイッチを踏んでください（Ctrl+C で終了）")
    print("=" * 68)
    try:
        from pynput import keyboard
    except ImportError:
        print("\n[NG] pynput がインストールされていません。")
        print("     python3 -m pip install --user pynput")
        print("\n     ※ このモードが使えなくても、ブラウザで http://127.0.0.1:<port>/keytest")
        print("       を開けば、権限も追加インストールも無しでキー名を確認できます。")
        return

    seen = {"n": 0}

    def hint():
        time.sleep(8)
        if seen["n"] == 0:
            print("\n[!] 8秒間、キーを1つも検出できていません。考えられる原因:")
            if sys.platform == "darwin":
                print("    1. 入力監視の許可がない")
                print("       システム設定 → プライバシーとセキュリティ → 入力監視 で")
                print("       このプログラムを起動したアプリ（ターミナル.app など）を追加して ON")
                print("       ※ 許可の変更後はターミナルを再起動してから試し直してください")
                print("    2. パスワード欄にフォーカスがある（macOS のセキュア入力中は検出できません）")
            print("    3. ペダルがキーボードとして認識されていない")
            print("       （マウスクリック送出型・専用ドライバ必須型の可能性）")
            print("       → システム情報.app の USB でデバイスが見えるか確認")
    threading.Thread(target=hint, daemon=True).start()

    def on_press(key):
        seen["n"] += 1
        names = sorted(pynput_key_names(key))
        print("  検出: %-22s → footswitch.keys に書く値: %s"
              % (repr(key), json.dumps(names, ensure_ascii=False)))

    try:
        with keyboard.Listener(on_press=on_press) as listener:
            listener.join()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("[NG] リスナーを起動できません: %s" % e)


def start_footswitch_listener(controller, cfg):
    """
    USBフットスイッチ（キーボードとして認識されるタイプ）を拾う。
    macOS / Windows / Linux 共通で pynput を使う（未インストールなら無効化して続行）。
      macOS : システム設定 → プライバシーとセキュリティ → 入力監視 で
              実行元（ターミナル.app など）を許可する必要がある
      Windows : 追加設定なしで動く
    """
    fs = cfg.get("footswitch") or {}
    if not fs.get("enabled"):
        return
    try:
        from pynput import keyboard
    except ImportError:
        log("FOOT 無効: pynput が見つかりません（pip install pynput）。"
            "司会ページをブラウザで開いておく方式なら追加インストール不要です。")
        return

    wanted = {str(k).strip().lower() for k in (fs.get("keys") or ["f13"])}
    action = normalize_action(fs.get("action") or "trigger")
    profile = fs.get("profile") or "mc"
    min_interval = float(fs.get("min_interval_sec", 1.0))
    last = [0.0]

    def on_press(key):
        names = pynput_key_names(key)
        if not (names & wanted):
            return
        now = time.monotonic()
        if now - last[0] < min_interval:
            return
        last[0] = now
        threading.Thread(
            target=controller.execute,
            args=(action, profile, "FOOT %s" % ",".join(sorted(names))),
            kwargs={"req_id": "foot-%d" % int(now * 1000)},
            daemon=True).start()

    try:
        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()
    except Exception as e:
        log("FOOT 起動できません: %s" % e)
        return
    log("FOOT 待受 キー=%s → %s (%s)" % (",".join(sorted(wanted)),
                                        ACTION_LABELS.get(action, action), profile))
    if sys.platform == "darwin":
        log("     macOS では『入力監視』の許可が必要です（未許可だと無反応になります）")


def strip_upgrade_insecure(csp):
    """CSP から upgrade-insecure-requests だけを取り除く（他のディレクティブは残す）"""
    parts = [d.strip() for d in csp.split(";")]
    parts = [d for d in parts if d and d.lower() != "upgrade-insecure-requests"]
    return "; ".join(parts) + ";"


def timepon_urls(cfg, host=None):
    """
    TimePon 本体の URL を組み立てる。host を渡すと、そのホスト名で組み直す
    （別マシンのブラウザに 127.0.0.1 の URL を渡さないため）。
    """
    base = cfg.get("timepon_public_base") or cfg["timepon_base"]
    base = base if base.endswith("/") else base + "/"
    if host and not cfg.get("timepon_public_base"):
        parts = urllib.parse.urlsplit(base)
        port = parts.port
        netloc = host if not port or port == 80 else "%s:%d" % (host, port)
        base = "%s://%s%s" % (parts.scheme, netloc, parts.path or "/")
    return {
        "base": base,
        "stage": "%s?id=%s" % (base, cfg["room_id"]),
        "admin": "%s?op=1&id=%s#k=%s" % (base, cfg["room_id"], cfg["admin_key"]),
    }


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def print_urls(cfg):
    """別マシンから開くURLをまとめて表示する（管理URLは #k= 付き）"""
    base = cfg["timepon_base"]
    parts = urllib.parse.urlsplit(base if base.endswith("/") else base + "/")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path
    ips = local_ips()
    if not ips:
        print("LAN の IP アドレスが取得できませんでした。ネットワーク接続を確認してください。")
        return
    print("ROOM %s" % cfg["room_id"])
    print("※ TimePon は 0.0.0.0 で待ち受けている必要があります"
          "（確認: lsof -nP -iTCP:%d -sTCP:LISTEN）" % port)
    for ip in ips:
        host = "%s://%s:%d%s" % (parts.scheme, ip, port, path)
        print("")
        print("  [%s]" % ip)
        print("  演壇画面   %s?id=%s" % (host, cfg["room_id"]))
        print("  管理画面   %s?op=1&id=%s#k=%s" % (host, cfg["room_id"], cfg["admin_key"]))
        print("  司会ボタン http://%s:%d/" % (ip, cfg["http_port"]))
        print("  操作パネル http://%s:%d/panel" % (ip, cfg["http_port"]))
    print("")
    print("管理画面のURLは #k= まで含めて開いてください（キーはブラウザごとに保存されます）")


def main():
    global CONFIG
    ap = argparse.ArgumentParser(description="TimePon Bridge")
    ap.add_argument("--create-room", action="store_true", help="ルームを新規作成して設定に保存する")
    ap.add_argument("--urls", action="store_true",
                    help="別マシンから開くためのURL一覧（管理キー付き）を表示して終了する")
    ap.add_argument("--keys", action="store_true",
                    help="フットスイッチ診断: 押されたキーの名前を表示して終了する")
    args = ap.parse_args()

    if args.keys:
        watch_keys()
        return

    CONFIG = load_config()
    client = TimeponClient(CONFIG["timepon_base"], CONFIG["room_id"], CONFIG["admin_key"])

    if args.urls:
        if not CONFIG["room_id"]:
            print("room_id が未設定です。--create-room を先に実行してください。")
            sys.exit(1)
        u = timepon_urls(CONFIG)
        print("ROOM    : %s" % CONFIG["room_id"])
        print("管理URL : %s" % u["admin"])
        print("演壇URL : %s" % u["stage"])
        return

    if args.create_room or not CONFIG["room_id"] or not CONFIG["admin_key"]:
        if not args.create_room:
            print("room_id / admin_key が未設定です。--create-room で作成するか、"
                  "既存の管理URL (?op=1&id=XXXXXX#k=...) から設定ファイルに書き写してください。")
            sys.exit(1)
        rid, key = client.create_room()
        CONFIG["room_id"], CONFIG["admin_key"] = rid, key
        save_config(CONFIG)
        client.room_id, client.admin_key = rid, key
        base = CONFIG["timepon_base"]
        print("ルームを作成しました")
        print("  管理URL : %s?op=1&id=%s#k=%s" % (base, rid, key))
        print("  演壇URL : %s?id=%s" % (base, rid))
        if args.create_room:
            return

    if args.urls:
        print_urls(CONFIG)
        return

    controller = Controller(client, CONFIG)
    BridgeHTTPHandler.controller = controller
    BridgeHTTPHandler.client = client
    BridgeHTTPHandler.cfg = CONFIG

    try:
        client.get_state()
        log("CHECK TimePon 応答OK: %s" % CONFIG["timepon_base"])
    except Exception as e:
        log("CHECK TimePon に接続できません: %s (%s)" % (CONFIG["timepon_base"], e))
        log("      PHP が起動しているか確認してください（php.log / ポートの重複）")

    start_udp_listener(controller, CONFIG)
    start_tcp_listener(controller, CONFIG)
    start_footswitch_listener(controller, CONFIG)

    try:
        httpd = ThreadingHTTPServer((CONFIG["http_host"], int(CONFIG["http_port"])), BridgeHTTPHandler)
    except OSError as e:
        log("HTTP ポート %s:%s を開けません (%s)。bridge_config.json の http_port を変えてください。"
            % (CONFIG["http_host"], CONFIG["http_port"], e))
        sys.exit(1)
    log("HTTP 待受 %s:%d" % (CONFIG["http_host"], CONFIG["http_port"]))
    log("ROOM %s  TimePon: %s" % (CONFIG["room_id"], CONFIG["timepon_base"]))
    log("     演壇URL       %s" % timepon_urls(CONFIG)["stage"])
    log("     管理URL       %s" % timepon_urls(CONFIG)["admin"])
    log("HOST %s / Python %s" % (platform.platform(), platform.python_version()))
    for ip in local_ips() or ["127.0.0.1"]:
        log("     司会ボタン      http://%s:%d/" % (ip, CONFIG["http_port"]))
        log("     操作パネル      http://%s:%d/panel" % (ip, CONFIG["http_port"]))
        log("     管理画面へ転送  http://%s:%d/admin   ← 別マシンからはこれを開く" % (ip, CONFIG["http_port"]))
        log("     演壇画面へ転送  http://%s:%d/stage" % (ip, CONFIG["http_port"]))
        log("     キー確認        http://%s:%d/keytest" % (ip, CONFIG["http_port"]))
        log("     ストップ＆RST   http://%s:%d/reset" % (ip, CONFIG["http_port"]))
        log("     スタート        http://%s:%d/start?profile=net" % (ip, CONFIG["http_port"]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("停止しました")


if __name__ == "__main__":
    main()
