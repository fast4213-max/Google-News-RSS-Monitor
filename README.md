# 大東市 Googleニュース Discord通知Bot

Googleニュースの「大東市」検索RSSを1時間ごとにチェックし、新着記事をDiscordに通知します。

- GitHubの `schedule:`（cron）は**使いません**。起動は **cron-job.org からの外部HTTPリクエスト** のみです。
- 実行は **GitHub Actions**（Pythonが動きます）。
- 既読管理・通知しきれなかった記事の一時保存は **JSON** で行い、GitHub Actionsが自動でリポジトリにコミットします。
- 1回の実行で **最大10件まで** しか通知しません（Discord Webhookのレート制限対策）。10件を超えた分は**捨てずに**次回に持ち越します。

---

## 1. 全体の仕組み（図）

```
[cron-job.org]  ---1時間ごとにPOST--->  [GitHub API (repository_dispatch)]
                                                    |
                                                    v
                                        [GitHub Actions が起動]
                                                    |
                                                    v
                                    main.py が RSS取得 → 差分抽出 → Discord通知
                                                    |
                                                    v
                                    state/*.json を更新して自動コミット
```

なぜこの構成か:
- GitHubリポジトリは「コードを置く場所」であって、URLを叩いても中のPythonは実行されません。
- そこで cron-job.org には **GitHubの「repository_dispatch」という仕組みを呼び出すAPIリクエスト** を叩かせます。これを合図にGitHub Actionsが起動し、その中でPythonが実行されます。
- 結果として「GitHub純正のcron機能は使わない」「外部(cron-job.org)から起動する」「Pythonが動く」の3つを同時に満たせます。

---

## 2. ファイル構成

```
.
├── .github/workflows/dispatch.yml   # GitHub Actionsの設定（起動条件はrepository_dispatchのみ）
├── feeds.json                       # 監視するRSSフィードの一覧（ここに追加すればフィードを増やせる）
├── main.py                          # 【通常実行】差分を検出してDiscordに通知するメイン処理
├── init_read.py                     # 【初回用】現在のRSSを全部「既読」にする（通知なし）
├── test_notify.py                   # 【テスト用】各フィードの最新1件だけ試しに通知する
├── rss.py                           # RSSの取得とXML解析
├── notifier.py                      # Discordへの送信処理
├── state_manager.py                 # 既読リスト・持ち越しキューの読み書き
├── logger.py                        # ログ出力（GitHub Actionsのログに残る）
├── test_logic.py                    # 開発用の動作確認スクリプト（本番運用には無関係、削除してもOK）
├── requirements.txt                 # 依存パッケージ（標準ライブラリのみなので基本空）
└── state/
    ├── read_daito.json              # 「大東市」フィードの既読記事ID一覧
    └── queue_daito.json             # 通知しきれず持ち越し中の記事一覧
```

---

## 3. セットアップ手順（順番にやってください）

### ステップ1: GitHubリポジトリを作る

1. GitHubで新しいリポジトリを作成します（Public / Private どちらでも可）。
2. このフォルダの中身を丸ごとそのリポジトリにアップロード（push）します。

```bash
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/【あなたのユーザー名】/【リポジトリ名】.git
git push -u origin main
```

### ステップ2: Discord Webhook URLを作る

1. 通知したいDiscordチャンネルの設定 → 「連携サービス」→「ウェブフック」→「新しいウェブフック」を作成。
2. 「ウェブフックURLをコピー」する。これが `DISCORD_WEBHOOK_URL` になります。

### ステップ3: GitHub SecretsにWebhook URLを登録する

**ここが「よく忘れる」ポイントです。必ずやってください。**

1. GitHubのリポジトリページを開く
2. 上部タブの **Settings**
3. 左メニューの **Secrets and variables** → **Actions**
4. **New repository secret** をクリック
5. 以下の通り入力:
   - Name: `DISCORD_WEBHOOK_URL`
   - Secret: （ステップ2でコピーしたWebhook URL）
6. **Add secret** をクリックして保存

> これを忘れると `main.py` が「環境変数 DISCORD_WEBHOOK_URL が設定されていません」というエラーを出して止まります。

### ステップ4: GitHub PAT（Personal Access Token）を作る

これは **cron-job.orgがGitHubのActionsを起動するため専用のトークン** です（Discord Webhookとは別物）。

1. GitHubの右上アイコン → **Settings**（個人設定の方）
2. 左メニュー一番下 **Developer settings**
3. **Personal access tokens** → **Tokens (classic)**
4. **Generate new token (classic)**
5. 設定:
   - Note: 好きな名前（例: `cron-job-rss-notifier`）
   - Expiration: お好みで（`No expiration` にすると期限切れの心配がなくなります）
   - スコープ（権限）: **`repo`** にチェックを入れる（＝「全権限のやつ」。これで対象のリポジトリへのdispatch送信ができます）
6. **Generate token** をクリックし、表示されたトークン文字列を **その場でコピーして保存**（二度と表示されません）。

> このPATは **cron-job.org側の設定に使う**ものであり、GitHub Secretsには入れません。

### ステップ5: Actionsの書き込み権限をONにする（★これも忘れがちなので注意★）

`state/*.json`（既読・キュー）を自動コミットするには、**リポジトリ全体の設定で「ActionsにWrite権限を与える」**必要があります。`.github/workflows/dispatch.yml` に `permissions: contents: write` と書いてあるだけでは不十分で、リポジトリ側の大元のスイッチがOFFだと `git push` が **403エラー** で失敗します。

特に2023年2月以降に作成したリポジトリは、デフォルトで「読み取り専用」になっているため、ほぼ必ず変更が必要です。

1. リポジトリの **Settings**
2. 左メニュー **Actions** → **General**
3. 一番下までスクロールして **Workflow permissions** の項目を探す
4. **Read and write permissions** を選択する（デフォルトは "Read repository contents permission" になっている）
5. **Save** をクリック

> これを忘れると、通知自体は正常に届くのに「既読状態が保存されない」→ **次回また同じ記事が再通知される** という、原因の分かりにくい不具合になります。何か変（同じニュースが繰り返し来る）と思ったら、まずここを疑ってください。

### ステップ6: 初回既読化を実行する

いきなり通常実行すると、今RSSに載っている記事が全部「新着」として大量通知されてしまいます。それを防ぐため、最初に1回だけ「全部既読化」を実行します。

**方法A: GitHubのWeb画面から手動実行（一番簡単）**

1. リポジトリの **Actions** タブを開く
2. 左側の **RSS Discord Notifier (external trigger only)** をクリック
3. 右側の **Run workflow** ボタンを押す
4. `mode` のプルダウンで **init-read** を選んで実行

これで通知は送られず、現在のRSS記事がすべて既読になります。

### ステップ7: テスト通知で疎通確認する

同じくActionsタブの **Run workflow** から、今度は `mode` を **test-notify** にして実行してください。Discordチャンネルに `[TEST] ...` という通知が1件届けば成功です。

### ステップ8: cron-job.org を設定する（本番の定期起動）

1. https://cron-job.org にログイン（アカウントがなければ作成）
2. **Create cronjob** をクリック
3. 以下のように設定します:

| 項目 | 設定値 |
|---|---|
| Title | 大東市ニュース通知 |
| Address (URL) | `https://api.github.com/repos/【ユーザー名】/【リポジトリ名】/dispatches` |
| Request method | **POST** |
| Schedule | **Every hour**（1時間ごと） |

4. **Advanced（詳細設定）** を開き、以下を追加:

**Headers（ヘッダー）**
| Key | Value |
|---|---|
| `Authorization` | `Bearer 【ステップ4で作ったPAT】` |
| `Accept` | `application/vnd.github+json` |
| `Content-Type` | `application/json` |

**Body（リクエストボディ）**
```json
{"event_type": "run-notify"}
```

5. 保存すれば、1時間ごとにこのジョブがGitHub Actionsを起動するようになります。

> **テスト用・初期化用のジョブを別途作ってもOKです。** Bodyの `event_type` を `test-notify` や `init-read` に変えた別のcronジョブを作れば、cron-job.orgの画面からいつでもテストや再初期化を手動実行できます（普段はOFFにしておいて、必要な時だけ実行）。

---

## 4. 各関数（スクリプト）の役割まとめ

| ファイル | 起動方法 | やること |
|---|---|---|
| `main.py` | `event_type: run-notify`（1時間毎の本番） | 差分検出→最大10件通知→既読化・残りはqueueへ |
| `init_read.py` | `event_type: init-read`（最初の1回だけ） | 今あるRSS記事を全部既読化。通知はしない |
| `test_notify.py` | `event_type: test-notify`（動作確認したい時） | 各フィードの最新1件だけ試しに通知。既読状態は変更しない |

GitHub Actions画面から手動実行したい場合は、Actionsタブ→ワークフロー選択→**Run workflow**→`mode`のプルダウンで選択、でも同じことができます（cron-job.orgを介さないデバッグ用）。

---

## 5. フィード（監視対象）を増やしたい時

`feeds.json` に追記するだけです。例えば「野崎まいり」というキーワードも追加したい場合:

```json
[
  {
    "id": "daito",
    "name": "大東市",
    "url": "https://news.google.com/rss/search?q=%22%E5%A4%A7%E6%9D%B1%E5%B8%82%22&hl=ja&gl=JP&ceid=JP:ja"
  },
  {
    "id": "nozaki",
    "name": "野崎まいり",
    "url": "https://news.google.com/rss/search?q=%22%E9%87%8E%E5%B4%8E%E3%81%BE%E3%81%84%E3%82%8A%22&hl=ja&gl=JP&ceid=JP:ja"
  }
]
```

- `id`: 半角英数字。state/read_<id>.json のファイル名になります。他と重複しないユニークな値にしてください。
- `name`: Discord通知の見出し（【○○】の部分）に使われます。
- `url`: Google ニュースの検索RSS。キーワードをダブルクォートで囲むと完全一致検索になり、誤爆が減ります。

キーワードをURLエンコードする際は以下のようなツールを使うと楽です:
- ブラウザのアドレスバーで `https://news.google.com/rss/search?q="キーワード"&hl=ja&gl=JP&ceid=JP:ja` と入力してEnterを押すと、自動的にエンコードされたURLになります。

追加後は普通に `main.py`（run-notify）が新しいフィードも自動で拾って処理します。追加した直後は、そのフィードだけ既読が空なので大量通知される可能性があります。気になる場合は `init_read.py` をもう一度実行してください（既存フィードの既読は保持されたままです）。

---

## 6. エラー時の挙動

以下のようなケースでは、Discordチャンネルに **⚠️ RSS通知エラー** という形式でエラー内容が通知されます。

- RSSの取得に失敗した（ネットワークエラー、HTTPエラーなど）
- RSSのXML構造が想定と異なっていた（例: GoogleニュースがRSSの形式を変更した場合など）
- Discordへの送信自体に失敗した

エラー通知には「どの処理のどの段階で失敗したか」を示す `context`（例: `feed:daito`）が含まれるので、原因を特定しやすくなっています。またGitHub Actionsの実行ログ（Actionsタブ→該当の実行→ログ）にはさらに詳しいスタックトレースが残ります。

**構造が崩れた（RSS形式が変わった）場合の直し方の目安:**
1. エラー通知の `context` を見て、どのフィードで起きたか確認
2. GitHub Actionsのログで、`rss.py` の `RssParseError` のメッセージを確認（どの要素が見つからなかったかが書いてあります）
3. `rss.py` の `parse_items()` 関数内の要素名（`title`, `link`, `guid`, `pubDate`）を、実際に返ってきているXMLの構造に合わせて修正

---

## 7. よくある質問・詰まりポイント

**Q. cron-job.orgの実行が失敗する（401 Unauthorized）**
A. PATの権限不足か期限切れの可能性があります。ステップ4を見直し、`repo` スコープにチェックが入った新しいPATを作り直して、cron-job.orgのヘッダーを更新してください。

**Q. Discordに通知が来ない**
A. まずGitHub Secretsに `DISCORD_WEBHOOK_URL` が正しく登録されているか確認（ステップ3）。次にActionsタブでワークフローが実際に起動・成功しているか確認してください。

**Q. 同じ記事が何度も通知される**
A. ほぼ確実に **ステップ5（Workflow permissionsをRead and writeにする設定）が未実施**です。Actionsの実行ログを開き、「状態ファイルをコミット&プッシュ」のステップが **赤字で403エラー** になっていないか確認してください。なっていたら Settings → Actions → General → Workflow permissions を **Read and write permissions** に変更して、もう一度実行してください。

**Q. 通知が来た記事数が10件より少ない/多い日がある**
A. 仕様通りです。1回の実行につき最大10件までしか送信しません。未読が10件を超えていた場合、残りは `state/queue_<id>.json` に保存され、次回の実行で優先的に送信されます（消えることはありません）。

**Q. GitHub Actionsのcronは本当に使ってない？**
A. `.github/workflows/dispatch.yml` の `on:` セクションを見てください。`repository_dispatch` と手動デバッグ用の `workflow_dispatch` のみで、`schedule:` は記述していません。
