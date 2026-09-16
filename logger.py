# -*- coding: utf-8 -*-
"""
共通ログ出力モジュール。

設計方針:
  - GitHub Actions のログにそのまま出力される print() ベース。
  - 「どの処理の、どの段階で」問題が起きたかを一目で追えるよう、
    context (例: "rss-fetch", "discord-send", "git-commit") を必ず付ける。
  - ERROR ログは main.py 側で拾って Discord にもエラー通知される。
"""

import sys
import traceback
from datetime import datetime, timezone


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def info(context: str, message: str) -> None:
    print(f"[INFO ] {_timestamp()} [{context}] {message}", file=sys.stdout, flush=True)


def warn(context: str, message: str) -> None:
    print(f"[WARN ] {_timestamp()} [{context}] {message}", file=sys.stdout, flush=True)


def error(context: str, message: str, exc: Exception | None = None) -> str:
    """
    エラーログを出力し、Discord通知用に整形した文字列を返す。
    exc を渡すとスタックトレースも記録する（Actionsログで原因追跡しやすくするため）。
    """
    lines = [f"[ERROR] {_timestamp()} [{context}] {message}"]
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        lines.append(tb)
    text = "\n".join(lines)
    print(text, file=sys.stderr, flush=True)
    return text
