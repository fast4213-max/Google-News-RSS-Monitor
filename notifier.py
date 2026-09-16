# -*- coding: utf-8 -*-
"""
Discord Webhook 通知処理。

方針:
  - 1メッセージ = 1記事。「タイトル / (改行) / リンク」の指定フォーマット。
  - Discord Webhook のレート制限(概ね 5リクエスト/2秒 程度)を避けるため、
    送信の合間に短いスリープを入れる。
  - エラー通知は通常通知と見分けやすいよう先頭に絵文字を付ける。
  - Webhook URL は環境変数 DISCORD_WEBHOOK_URL から読む(コードに直書きしない)。
"""

import json
import os
import time
import urllib.request
import urllib.error

import logger

_CONTEXT = "discord-send"
SEND_INTERVAL_SECONDS = 1.2  # レート制限回避のための送信間隔


def _webhook_url() -> str:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        raise RuntimeError(
            "環境変数 DISCORD_WEBHOOK_URL が設定されていません。"
            "GitHub Secrets に登録されているか確認してください。"
        )
    return url


def _post(content: str) -> None:
    url = _webhook_url()
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            # Discord Webhook は成功時 204 No Content
            if res.status not in (200, 204):
                raise RuntimeError(f"Discord応答が異常です status={res.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Discord送信でHTTPエラー status={e.code} body={body}") from e
    except Exception as e:
        raise RuntimeError(f"Discord送信に失敗しました: {e}") from e


def send_article(feed_name: str, title: str, link: str) -> None:
    """
    指定フォーマットで1記事を通知する。
      【<feed_name>】
      <title>
      <link>
    """
    content = f"【{feed_name}】\n{title}\n{link}"
    _post(content)
    logger.info(_CONTEXT, f"通知送信OK: {title}")


def send_articles(feed_name: str, articles: list, max_count: int) -> list:
    """
    複数記事をまとめて送信する。Discordのレート制限を避けるため間隔を空けて送信。
    max_count を超える分は送信せずそのまま返す(呼び出し元がqueueに保存する)。

    戻り値: 実際に送信できた記事のリスト
    """
    to_send = articles[:max_count]
    sent = []
    for i, article in enumerate(to_send):
        send_article(feed_name, article["title"], article["link"])
        sent.append(article)
        if i < len(to_send) - 1:
            time.sleep(SEND_INTERVAL_SECONDS)
    return sent


def send_error(context: str, message: str) -> None:
    """
    処理エラーをDiscordに通知する。
    タイトル通知と見分けやすいよう絵文字と context を先頭に出す。
    Discord送信自体が失敗した場合はActionsログにのみ残す(無限ループ防止のため再送しない)。
    """
    content = f"⚠️ **RSS通知エラー** [{context}]\n```\n{message[:1800]}\n```"
    try:
        _post(content)
    except Exception as e:
        logger.error(_CONTEXT, f"エラー通知そのものの送信にも失敗しました: {e}")
