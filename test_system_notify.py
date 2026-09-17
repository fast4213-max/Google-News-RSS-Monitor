# -*- coding: utf-8 -*-
"""
システム通知用Webhook (DISCORD_WEBHOOK_URL_SYSTEM) の疎通確認用スクリプト。

feeds.json やフィードには一切触れず、共通のシステム通知チャンネルへ
テストメッセージを1件送るだけ。state/*.jsonも変更しない。

使い方: cron-job.org を介さず、GitHubのUIから workflow_dispatch で
       mode=system-test を選んで手動実行する。
"""

import logger
import notifier

_CONTEXT = "system-test"


def main() -> None:
    logger.info(_CONTEXT, "===== システム通知テスト 開始 =====")
    notifier.send_error(
        _CONTEXT,
        "これはテスト送信です。このメッセージが見えていれば "
        "DISCORD_WEBHOOK_URL_SYSTEM の設定は正しく機能しています。",
    )
    logger.info(_CONTEXT, "===== システム通知テスト 終了 =====")


if __name__ == "__main__":
    main()
