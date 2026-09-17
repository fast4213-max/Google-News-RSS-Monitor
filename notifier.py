# -*- coding: utf-8 -*-
"""
Discord Webhook 通知処理。

方針:
  - 1メッセージ = 1記事。「タイトル / (改行) / リンク」の指定フォーマット。
  - Discord Webhook のレート制限(概ね 5リクエスト/2秒 程度)を避けるため、
    送信の合間に短いスリープを入れる。
  - エラー通知は通常通知と見分けやすいよう先頭に絵文字を付ける。
  - Webhook URL はフィードごとに別チャンネルへ送れるよう、呼び出し元から
    「環境変数名」を受け取ってその都度読み出す方式にしている(コードに直書きしない)。
  - フィードを特定できない/共通のエラー(feeds.json自体が壊れている等)は、
    共通のシステム通知用Webhook (DISCORD_WEBHOOK_URL_SYSTEM) に送る。
"""

import json
import os
import time
import urllib.request
import urllib.error

import logger

_CONTEXT = "discord-send"
SEND_INTERVAL_SECONDS = 1.2  # レート制限回避のための送信間隔
SYSTEM_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL_SYSTEM"  # フィード特定不能な異常時の送り先

# Discord Webhookはcloudflareの背後にあり、urllibのデフォルトUser-Agent
# ("Python-urllib/3.12") だとbot判定で403 (Cloudflareエラー1010) になることがあるため、
# ブラウザ相当のUser-Agentを明示的に付ける。rss.pyと同じ値で統一。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _resolve_webhook_url(webhook_env: str) -> str:
    """
    環境変数名(webhook_env)からWebhook URLの実体を取り出す。
    feeds.json 側では実URLを書かず「環境変数の名前」だけを持たせる設計。
    """
    url = os.environ.get(webhook_env, "").strip()
    if not url:
        raise RuntimeError(
            f"環境変数 {webhook_env} が設定されていません。"
            "GitHub Secrets に登録されているか、feeds.json の webhook_env の綴りが"
            "Secrets の名前と一致しているか確認してください。"
        )
    return url


def _post(webhook_env: str, content: str) -> None:
    url = _resolve_webhook_url(webhook_env)
    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
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


def send_article(webhook_env: str, feed_name: str, title: str, link: str) -> None:
    """
    指定フォーマットで1記事を、指定のWebhook(=そのフィード専用チャンネル)に通知する。
      【<feed_name>】
      <title>
      <link>
    """
    content = f"【{feed_name}】\n{title}\n{link}"
    _post(webhook_env, content)
    logger.info(_CONTEXT, f"通知送信OK ({webhook_env}): {title}")


def send_articles(webhook_env: str, feed_name: str, articles: list, max_count: int) -> list:
    """
    複数記事をまとめて、指定のWebhook(フィード専用チャンネル)に送信する。
    Discordのレート制限を避けるため間隔を空けて送信。
    max_count を超える分は送信せずそのまま返す(呼び出し元がqueueに保存する)。

    戻り値: 実際に送信できた記事のリスト
    """
    to_send = articles[:max_count]
    sent = []
    for i, article in enumerate(to_send):
        send_article(webhook_env, feed_name, article["title"], article["link"])
        sent.append(article)
        if i < len(to_send) - 1:
            time.sleep(SEND_INTERVAL_SECONDS)
    return sent


def send_error(context: str, message: str, webhook_env: str | None = None) -> None:
    """
    処理エラーをDiscordに通知する。
    タイトル通知と見分けやすいよう絵文字と context を先頭に出す。

    webhook_env を指定すれば「そのフィード専用チャンネル」にエラーを送る
    (例: 大東市フィードのRSS取得失敗 → 大東市チャンネルにエラーが出る)。
    webhook_env を指定しない(=フィードを特定できない/フィード横断のエラー)場合は、
    共通のシステム通知用Webhook (DISCORD_WEBHOOK_URL_SYSTEM) に送る。

    Discord送信自体が失敗した場合はActionsログにのみ残す(無限ループ防止のため再送しない)。
    """
    target_env = webhook_env or SYSTEM_WEBHOOK_ENV
    content = f"⚠️ **RSS通知エラー** [{context}]\n```\n{message[:1800]}\n```"
    try:
        _post(target_env, content)
    except Exception as e:
        logger.error(
            _CONTEXT,
            f"エラー通知そのものの送信にも失敗しました (webhook_env={target_env}): {e}",
        )
