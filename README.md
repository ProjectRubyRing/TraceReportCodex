# CloudWatch Transaction Search トレースレポート

CloudWatch Logs Insightsで `aws/spans` に時間指定クエリを実行し、CSVをエクスポートしてExcelテンプレートに反映します。X-Rayのトレース詳細に近い、親子階層と処理時間のタイムラインを含む静的レポートを生成します。

実行入口は **RHEL 9.8向けのBashスクリプト** `bin/trace-report.sh` です。クエリ開始・完了待ち・ページ取得・時間分割はBashとAWS CLIで処理し、CSV変換とExcelテンプレートへの反映だけを付属のPythonで処理します。Pythonは標準ライブラリのみを使用し、pip、Excel、LibreOffice、Node.jsは実行環境に不要です。

## セットアップ

必要なコマンドはBash 4.4以上、GNU coreutils、jq 1.6以上、Python 3.9以上、AWS CLI v2です。

```bash
sudo dnf install -y bash coreutils jq python3

# AWS CLI v2は組織の標準手順または下記のAWS公式手順で別途導入してください。
aws --version
python3 --version
jq --version
```

AWS CLIの導入手順: [AWS CLI v2のインストール](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)。100,000件上限またはAPIの `nextToken` を使う場合は、それらの入力に対応した新しいCLI v2が必要です。既定値は10,000件と時間分割です。

AWS側では、対象アカウント・リージョンでTransaction Searchが有効化され、`aws/spans` にスパンが蓄積されている必要があります。スクリプトは有効化、IAM設定、ログの変更を行いません。

認証には通常のAWS CLI認証チェーンを使います。EC2のインスタンスロール、環境変数、設定済みプロファイルなどを利用してください。鍵のスクリプトへの記述は不要です。必要な権限の例は `iam/read-spans-policy.json` にあります。`REGION` と `ACCOUNT_ID` を置き換えて利用してください。`--log-group` を変える場合は対象ARNも変更します。`StopQuery` はリソース単位の制限をサポートしないため、このアクションだけ `Resource: "*"` です。

## 実行

プロジェクト一式をRHELに配置し、プロジェクトのルートで実行します。任意の作業ディレクトリからスクリプトの絶対パスで起動することもできます。

配布用の `trace-report-rhel9.tar.gz` を使う場合は、同梱のチェックサムと照合して展開できます。

```bash
sha256sum -c trace-report-rhel9.tar.gz.sha256
tar -xzf trace-report-rhel9.tar.gz
cd trace-report
```

```bash
bash bin/trace-report.sh \
  --start '2026-10-26T09:00:00+09:00' \
  --end '2026-10-26T10:00:00+09:00' \
  --region ap-northeast-1 \
  --output-dir ./outputs/report-20261026-0900
```

出力ディレクトリには**存在しない新規パス**を指定します。既存パスは上書きしません。開始・終了にはタイムゾーン付きRFC3339（ミリ秒まで）または整数Unix秒を指定できます。タイムゾーンのない日時は受け付けません。

期間はログイベントの **`@timestamp` が `開始以上・終了未満`** の範囲です。`startTimeUnixNano` で検索期間を判定するものではありません。APIの秒単位の境界は外側に丸め、クエリ内のミリ秒条件で境界の重複と欠落を防ぎます。レポート上の日時はUTC、処理時間はmsです。

```bash
# プロファイルと特定トレースを指定する例
bash bin/trace-report.sh \
  --start '2026-10-26T00:00:00Z' \
  --end '2026-10-26T01:00:00Z' \
  --region ap-northeast-1 \
  --profile reporting \
  --trace-id 1-69000000-111111112222222233333333 \
  --output-dir ./outputs/one-trace
```

`--trace-id` はX-Ray形式と32桁の16進数形式を受け付け、クエリでは両方の表現を検索します。レポート内のTrace IDは32桁の小文字16進数に統一します。

主なオプション:

| オプション | 既定値 | 内容 |
| --- | --- | --- |
| `--switchback-mode` | `warn` | AWSの認証・権限エラー時に警告して終了する `warn`、または専用シェルをsourceする `auto` |
| `--switchback-script` | なし | sourceする専用Bashシェルのパス。AWS接続時の `auto` では必須 |
| `--log-group` | `aws/spans` | 取得対象のロググループ名 |
| `--template` | 付属テンプレート | 同じ配置契約を持つ `.xlsx` |
| `--query-limit` | `10000` | クエリごとの結果上限。2〜100000 |
| `--poll-interval` | `2` | 完了確認の間隔、秒。1〜60 |
| `--timeout` | `900` | 1クエリの開始・完了待ち・ページ取得の待機期限、秒 |
| `--max-queries` | `1000` | 分割前のクエリも含む発行数の上限 |
| `--from-results` | なし | AWSに接続せず保存済み応答から生成 |

## AWS操作権限不足時のスイッチバック

本ツールはCodeCommit操作を行いません。CodeCommit用の認証状態などで実行し、CloudWatch Logsのクエリ開始・完了確認・ページ取得で認証または権限エラーが返ると、スイッチバックを案内します。追加の権限確認APIは呼び出さず、実際に必要な操作のエラーで判定します。

既定の `--switchback-mode warn` は、標準エラーに警告を表示し、失敗したAWS CLIの終了コードで終了します。別チームが用意した専用シェルを手動で `source` し、同じコマンドを再実行してください。`--switchback-script FILE` も指定すると、警告にそのファイルをsourceするコマンドを表示します。

自動でスイッチバックして続行する場合は、`--switchback-mode auto` と専用シェルの配置場所を指定します。

```bash
bash bin/trace-report.sh \
  --start '2026-10-26T09:00:00+09:00' \
  --end '2026-10-26T10:00:00+09:00' \
  --region ap-northeast-1 \
  --switchback-mode auto \
  --switchback-script '/opt/team/aws roles/switchback.sh' \
  --output-dir ./outputs/report-with-switchback
```

専用シェルのインターフェースは次のとおりです。

- Bashから引数なしで `source` できるファイルを指定します。相対パスは起動時の作業ディレクトリを基準に解決します。読み取り権限が必要です。
- シェルは必要な認証環境変数を `export` / `unset` するなどして更新し、成功時は0、失敗時は非0を返してください。非対話で制御を戻し、`exit` / `exec` や呼び出し側のトラップの置き換えは行わない契約です。
- 同じBashプロセス内でsourceするため、更新した認証環境は再試行、後続のAWS操作、終了時のクエリ停止に引き継がれます。認証環境の変更範囲はレポート実行中のBashプロセスです。シェルの標準出力は標準エラーに流し、作業ディレクトリはsource前の場所へ戻します。

自動sourceは実行全体で最大1回です。成功後に失敗したAWS操作を同じ引数で1回再試行します。source失敗時はその終了コードで終了し、再試行後や後続操作で再び認証・権限エラーになった場合も終了します。API通信のリトライ設定は引き続きAWS CLIが管理し、sourceの所要時間もクエリの `--timeout` に含めます。終了処理中の `StopQuery` では新たなスイッチバックを始めません。

対象は `AccessDenied` / `AccessDeniedException`、`ExpiredToken` などの認証・権限エラー、認証情報の未設定・不完全な設定、SSOセッションの期限切れです。通信障害、スロットリング、対象ロググループの不存在、引数エラーではスイッチバックを実行しません。`--from-results` では専用シェルの存在確認もsourceも行いません。

`--profile` を指定した場合、その値はスイッチバック後も維持します。専用シェルが `AWS_PROFILE` を切り替える方式なら、`--profile` を省略して起動してください。AWS CLIの `--profile` は `AWS_PROFILE` より優先されます（[AWS公式の環境変数と優先順位](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-envvars.html)）。`--region` で指定した検索先も維持します。

## 出力とExcelの見方

| ファイル | 内容 |
| --- | --- |
| `report.xlsx` | テンプレートに取得結果を反映したExcelレポート |
| `spans.csv` | UTF-8 BOM付きCSV。1行1スパン。元のJSONも保持 |
| `raw-results.jsonl` | `@timestamp`、元の `@message`、`@ptr` を保持した取得データ |
| `query-manifest.json` | 検索条件、クエリID、分割範囲、ページ数、取得数、スキャン量、注意事項の件数 |

Excelは次の4シートです。

全シートは **Meiryo UI** フォント、白・黒・グレーを使ったモノトーンの書式です。見出しはチャコール、トレースの区切りは薄いグレー、タイムラインの状態はグレーの濃淡で区別します。日時・数値・IDの表示形式、列幅・行高、見出しの折り返し、ウィンドウ枠の固定を設定しています。Meiryo UIを利用できるExcel環境で開いてください。RHEL側へのフォント導入は生成処理に不要です。

- **Traces**: トレースごとの開始時刻、取得分の経過時間、スパン数、異常数、親未取得数。Trace IDをクリックすると該当するタイムラインに移動します。
- **Timeline**: 親スパンを先にした階層表示とウォーターフォール。各トレースの取得分の経過時間を40区間に分けて表示します。正確な開始差・処理時間は数値列で確認できます。右側のAU〜AZ列にID、Kind、深さ、注意事項があります。
- **Dependencies**: 取得した親子スパンのサービスが異なる接続を集計した表です。呼び出し数、異常数、呼び出し先スパン時間の平均・p95・最大を表示します。p95はnearest-rank法です。`aws.remote.service` だけを根拠に未取得の接続を補完しません。
- **Spans**: HTTP、ステータス、リソース属性、スパン属性、イベント、リンク、ナノ秒時刻などの詳細です。

内部のデータの流れは `GetQueryResults → raw-results.jsonl → spans.csv → Excelテンプレート → report.xlsx` です。CSVの `raw_message_json` 列を元にExcelの入力を復元するため、CSVの数式評価防止用の接頭辞がExcelの表示名に入りません。CSVの他の列だけを手で変更する更新方式には対応しません。

`status.code=ERROR` はError、HTTP 5xxはFault、429はThrottle、それ以外のHTTP 4xxはErrorとしてグレーの濃淡と状態名で区別します。HTTPによる分類を優先します。これはスパンの情報から行う分類で、X-Rayコンソール固有の判定の完全な再現ではありません。

## 件数上限と失敗時の動作

クエリ例は以下です。`--trace-id` を指定するとIDのフィルターが追加されます。

```text
fields @timestamp, @message, @ptr
| filter toMillis(@timestamp) >= <開始ミリ秒> and toMillis(@timestamp) < <終了ミリ秒>
| sort @timestamp asc
```

1. `StartQuery` で検索し、`Scheduled` / `Running` の間は待機します。部分結果は使用しません。
2. `Complete` の結果を取得します。`nextToken` があれば全ページを取得します。
3. 取得数が `--query-limit` 以上、または `statistics.recordsMatched` が取得数を上回るときは、時間範囲を二分割して再取得します。切り詰められた元の結果はCSVに混ぜません。ちょうど上限と同件数の場合も、完全性を優先して分割します。
4. 1msの範囲でも上限に達する場合は、不完全なレポートを出力せず終了します。対応するCLIで `--query-limit` を上げるか、`--trace-id` で対象を絞ってください。

クエリ失敗、権限不足、タイムアウト、循環するページトークン、不正なスパン、異なる内容の同一Trace ID/Span IDなども成功として扱いません。正常に完了した出力だけを新規ディレクトリとして公開します。空の検索結果は件数0のレポートとして正常終了します。

処理中のクエリはエラー終了、SIGINT、SIGTERMの際に `StopQuery` で停止を試みます。停止できなかった場合はクエリIDを標準エラーに表示します。SIGKILLやマシン停止では後処理できないため、CloudWatch側で実行状況を確認してください。AWS CLIの通信リトライは既定でstandardモード、最大5試行です。`AWS_RETRY_MODE` / `AWS_MAX_ATTEMPTS` が設定済みならその設定を使用します。待機期限はAPI呼び出し間で確認するため、通信中・リトライ中は指定した秒数を超える場合があります。

検索の分割は追加スキャンを伴います。`query-manifest.json` のスキャン量には分割前のスキャンも含めます。アクセス権限はディレクトリ・ファイルとも所有者に限定する設定で生成します。元の属性・イベントには業務データが含まれる可能性があるため、出力先は既存のログ取扱方針に従ってください。

## トレースの範囲と精度

- 指定したログ時刻範囲に含まれるスパンだけを取得します。同じTrace IDの前後のスパンを別途取得する処理はありません。完全なトレースが必要なときは十分に広い期間と `--trace-id` を指定します。取り込み遅延や未送信のスパンは復元できません。
- 親スパンが未取得、ルートが未取得、複数ルート、親子関係が循環、終了時刻がない場合を明示します。注意事項が0件でも、未取得の子スパンが存在しないことを保証するものではありません。
- トレースの経過時間は `取得スパンの最大終了時刻 − 最小開始時刻` です。スパン時間の合計ではありません。終了時刻がないスパンは時間を空欄にし、タイムラインには開始点を示します。
- Trace ID、Span ID、ナノ秒時刻はExcelで文字列として保持します。CSVとJSONLにもナノ秒整数を保持します。Excelの日時列は数値の日付でミリ秒表示、バーは40区間の近似表示です。
- CSVはExcelで数式として解釈され得る文字列に `'` を付けます。CSVを直接開くとIDや長い整数がExcelの自動変換対象になるため、通常は `report.xlsx` を利用してください。CSVを取り込む際はID・ナノ秒列を文字列として指定します。
- 1セルの文字数上限を超える内容はExcel上で省略表示し、そのセルとmanifestで明示します。CSV・JSONLには全文を保持します。Excelの行数上限に達する場合はエラーにします。
- 同じTrace ID/Span IDかつ同じJSON内容の重複は除去します。内容が違う重複は、更新版を推測で選ばずエラーにします。
- X-Rayの対話的なサービスマップ、画面内検索、ログへのドリルダウンなどのUI機能は含みません。サービス間の接続はDependencies表で確認できます。

## 入力スキーマとテンプレート

`@message` はTransaction SearchのJSONスパン1件で、`traceId`、`spanId`、`startTimeUnixNano` が必要です。`endTimeUnixNano` または `durationNano` から終了・時間を求めます。どちらもなければ未終了として表示します。`parentSpanId`、`resource.attributes`、`attributes`、`kind`、`status`、`events`、`links` を参照します。属性はAWSのオブジェクト形式とOTLPのkey/value配列形式を受け付けます。旧X-Rayセグメントドキュメントを直接入力する形式には対応しません。

サービス名は `attributes["aws.local.service"]`、`resource.attributes["aws.local.service"]`、`resource.attributes["service.name"]`、`attributes["service.name"]` の順に取得します。HTTP属性は新旧のOpenTelemetry命名の両方に対応します。

付属の `templates/trace-report.xlsx` はそのまま使用できます。色、フォント、列幅などを変更したコピーは `--template` で指定できます。シート名、見出し、以下の配置は維持してください。

| シート | 見出し行 | データ書式のひな型行 | データ領域 |
| --- | --- | --- | --- |
| Traces | 10 | 11 | A11:K最終行 |
| Timeline | 8 | 9=トレース見出し、10=スパン | A9:AZ最終行 |
| Dependencies | 8 | 9 | A9:I最終行 |
| Spans | 8 | 9 | A9:X最終行 |

TimelineのB5〜E5は正常・Error・Fault・Throttleの色のひな型です。期間の入力位置はTracesのB4と他シートのA4、集計欄はTracesのB5/D5/B6/D6/F6/H6/J6です。Excelの1900日付システムを使います。データ領域は実行時に置き換えるため、ここに独自の数式・テーブル・グラフを追加する使い方には対応しません。

## AWSに接続しない動作確認

付属データは3トレース・9スパンの合成データです。個人情報、認証情報、実アカウントのデータは含みません。

```bash
bash bin/trace-report.sh \
  --start '2026-10-26T09:00:00+09:00' \
  --end '2026-10-26T10:00:00+09:00' \
  --region ap-northeast-1 \
  --from-results examples/insights-results.json \
  --output-dir ./outputs/example

bash tests/run.sh
```

オフライン入力は、`status=Complete`、`nextToken` がない、件数が `--query-limit` 未満、`recordsMatched` が返却件数以下のGetQueryResults応答だけを受け付けます。スナップショットから欠落ページを補完できないためです。

テストには日時境界、ナノ秒精度、ステータス、親欠落・循環・深い階層、CSV、Excelの値と型、重複・不正入力の検証があります。Linuxのシェル結合テストではローカルの偽AWS CLIを使用し、完了待ち、ページ取得、上限での分割、空結果、タイムアウト、失敗時の停止に加え、スイッチバックの警告・自動source・認証環境の引き継ぎ・再試行回数の上限を確認します。AWS認証情報やネットワーク接続は使いません。

Windows環境では、Pythonテスト21件とShellCheck 0.11.0が成功しました。Linuxのシェル結合テスト27件は環境条件によりスキップしています。RHEL 9.8実機、実AWSへの接続、別チーム提供の実際のスイッチバックシェル、Microsoft Excel本体での表示は未検証です。RHELで上記の `bash tests/run.sh` とオフライン実行を確認した後、実際のスパンで検索してください。

## 開発時のテンプレート再生成

通常の利用には不要です。テンプレートのソースは `tools/build_template.mjs` です。開発環境で `@oai/artifact-tool` を解決できるNode.jsから実行すると、テンプレートと `.work/previews/` の確認画像を生成します。`tools/verify_workbook.mjs` は既存レポートを読み取り、値の検査とプレビュー生成を行います。

```bash
node tools/build_template.mjs
node tools/verify_workbook.mjs outputs/example/report.xlsx
python3 tools/package_release.py
```

## 参照したAWS公式仕様

- [Transaction Searchのスパン用ロググループ](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search-ingesting-span-log-groups.html)
- [StartQuery: 時間範囲・件数上限](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_StartQuery.html)
- [GetQueryResults: 完了状態・ページ取得・統計](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_GetQueryResults.html)
- [CloudWatchのスパンのクエリ例](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/GettingSetup.html)
- [CloudWatch Logsのアクションと権限範囲](https://docs.aws.amazon.com/service-authorization/latest/reference/list_logs.html)
- [AWS CLIの終了コード](https://docs.aws.amazon.com/cli/latest/topic/return-codes.html)
- [AWS CLIの認証・権限エラー](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-troubleshooting.html)
