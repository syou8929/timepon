#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Karabiner-Elements 設定点検スクリプト

ルールが「有効に見えるのにマッチしない」ときの原因を、設定ファイルから機械的に洗い出す。

    python3 check_karabiner.py
    python3 check_karabiner.py --key f13      # 対象キーを変える場合
"""

import argparse
import json
import os
import sys

CONFIG = os.path.expanduser("~/.config/karabiner/karabiner.json")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="f13", help="調べるキー（既定: f13）")
    ap.add_argument("--config", default=CONFIG)
    args = ap.parse_args()

    if not os.path.exists(args.config):
        print("[NG] 設定ファイルがありません: %s" % args.config)
        sys.exit(1)
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)

    profiles = cfg.get("profiles", [])
    selected = None
    print("=" * 70)
    print(" プロファイル")
    print("=" * 70)
    for p in profiles:
        mark = "★ 使用中" if p.get("selected") else "  （未使用）"
        print("  %s  %s" % (mark, p.get("name", "(no name)")))
        if p.get("selected"):
            selected = p
    if selected is None:
        print("\n[NG] 使用中のプロファイルがありません。")
        sys.exit(1)
    if len(profiles) > 1:
        print("\n  ※ プロファイルが複数あります。ルールを有効にしたのが"
              "『使用中』のプロファイルか確認してください。")

    # ---- 有効なルール ----
    rules = (selected.get("complex_modifications", {}) or {}).get("rules", []) or []
    print("\n" + "=" * 70)
    print(" 使用中プロファイルで有効なルール（%d件）" % len(rules))
    print("=" * 70)
    if not rules:
        print("  [NG] 1件もありません。Complex Modifications で Add rule → Enable が必要です。")
    hit = []
    for r in rules:
        desc = r.get("description", "(説明なし)")
        print("  ・%s" % desc)
        for m in r.get("manipulators", []):
            frm = m.get("from", {}) or {}
            if str(frm.get("key_code", "")).lower() != args.key.lower():
                continue
            hit.append((desc, m))
            conds = m.get("conditions") or []
            tos = m.get("to") or []
            kinds = []
            for t in tos:
                kinds.extend(t.keys())
            print("      → %s を捕まえる / to: %s" % (args.key, ", ".join(kinds) or "(なし)"))
            for c in conds:
                print("      ⚠ 条件つき: type=%s" % c.get("type"))
                for ident in c.get("identifiers", []):
                    print("         vendor_id=%s product_id=%s"
                          % (ident.get("vendor_id"), ident.get("product_id")))
                print("         ※ この条件に一致しないデバイスからの入力は無視されます。"
                      "vendor_id / product_id は EventViewer の Devices タブで確認した"
                      "実際の値ですか？")

    if not hit:
        print("\n  [NG] %s を捕まえるルールが、使用中プロファイルに1件もありません。" % args.key)
        print("       → Complex Modifications で目的のルールを Enable してください。")
    elif all(m.get("conditions") for _d, m in hit):
        print("\n  [!] %s のルールはすべて条件つきです。条件が合っていないと発火しません。"
              % args.key)
    else:
        print("\n  [OK] %s を無条件で捕まえるルールがあります。" % args.key)

    # ---- デバイス設定 ----
    devices = selected.get("devices", []) or []
    print("\n" + "=" * 70)
    print(" デバイス個別設定（%d件）" % len(devices))
    print("=" * 70)
    if not devices:
        print("  個別設定なし（＝すべてのデバイスが既定で対象）")
    ignored = []
    for d in devices:
        ident = d.get("identifiers", {}) or {}
        label = "vendor_id=%s product_id=%s%s%s" % (
            ident.get("vendor_id"), ident.get("product_id"),
            " keyboard" if ident.get("is_keyboard") else "",
            " pointing" if ident.get("is_pointing_device") else "")
        flags = []
        if d.get("ignore"):
            flags.append("ignore=true（★このデバイスは変更対象外）")
            ignored.append(label)
        if d.get("disable_built_in_keyboard_if_exists"):
            flags.append("disable_built_in_keyboard_if_exists=true")
        print("  ・%s%s" % (label, ("  " + " / ".join(flags)) if flags else ""))

    if ignored:
        print("\n  [!] ignore=true のデバイスがあります。フットスイッチがこの中にいる場合、")
        print("      EventViewer には見えてもルールは一切適用されません。")
        print("      Karabiner-Elements → Devices タブで、そのデバイスの")
        print("      『Modify events』にチェックを入れてください。")

    print("\n" + "=" * 70)
    print(" 次に見るところ")
    print("=" * 70)
    print("  1. Karabiner-Elements → Devices タブでフットスイッチが一覧にあり、")
    print("     Modify events が有効になっているか")
    print("  2. 上で [NG] や [!] が出た項目")
    print("  3. それでも解決しなければ Hammerspoon 方式（方式D）へ切り替え")


if __name__ == "__main__":
    main()
