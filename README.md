# TimePon

カンファレンスタイマー TIME-PON と、HTTP・UDP・TCP・USBフットスイッチから操作する Python ブリッジです。

- [TIME-PON 本体の使い方・ライセンス](timepon/README.md)
- [外部連携ブリッジのセットアップ・操作方法](README_timepon_bridge.md)
- `start_timepon.command`: macOS 用の一括起動スクリプト
- `timepon_footswitch.json`: Karabiner-Elements 用の設定例

## ローカル設定

Python 3.9 以上と PHP 8 以上を用意し、設定例をコピーしてください。

```bash
cp bridge_config.example.json bridge_config.json
```

設定例の `timepon_base` は、一括起動スクリプトの既定ポート `18080` に合わせています。Docker や手動起動でポート `8080` を使う場合は、`http://127.0.0.1:8080/` に変更してください。

ルームの作成・接続設定は[ブリッジのセットアップ手順](README_timepon_bridge.md#3-セットアップ)を参照してください。

管理キーを含む `bridge_config.json`、`timepon/data/` 内の実行時データ、バックアップ、ログ、Python 仮想環境は Git 管理対象外です。各環境で作成・設定してください。
