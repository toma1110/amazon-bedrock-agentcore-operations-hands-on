# s02 ローカルで調査ツールを使うエージェントを作る

## このSectionで確かめること

モデルに文章を渡すだけの呼出しと、モデルが必要なツールを要求し、その結果を受けて回答するAgentの違いを確かめます。AgentはStrands Agentsの単一エージェントです。ツールはこのdirectoryの`runbooks.json`だけを読み、AWSやネットワーク、他のfileにはアクセスしません。データは架空のサービス名を使った合成教材です。

## 開始地点と完成地点

- 開始地点: Public repositoryの`main`にある`sections/s02/`。Section実装前のREADMEのみの状態はcommit `987e36fe7b90c7b60bd4aca6d4d0fc6611aa4379`です。
- 完成地点: `sections/s02/agent_app.py`、`runbooks.json`、`pyproject.toml`、`uv.lock`、`tests/`。
- s03では完成地点の`agent_app.py`とデータをそのままRuntime用プロジェクトへ持ち込みます。Runtime、Gateway、Lambda、Memory、CloudWatch等はこのSectionでは作成しません。

## 実行条件

- Python 3.11〜3.13、uv、エディターを使えるローカルPC。
- AWSの認証済みprofileまたはAWS IAM Identity Centerの有効なsession。認証情報をfile、環境変数、コマンド引数へ貼り付けないでください。
- 実行前に自分が使う学習用accountの12桁IDを確認します。
- `ap-northeast-1`（東京）でAmazon Bedrockの`amazon.nova-lite-v1:0`を利用できること。
- 呼出し元には、このモデルの`bedrock:InvokeModel`が必要です。Account番号を表示するためのSTS `GetCallerIdentity`も実行します。このAPIはIAM許可を追加しなくても呼び出せます。既存の認証設定や権限を教材が変更することはありません。
- この教材はPython 3.11〜3.13、Strands Agents `1.57.2`、boto3 `1.43.108`に固定し、`uv.lock`で依存関係を固定します。

## 準備と実行

まず[公開教材repo](https://github.com/toma1110/amazon-bedrock-agentcore-operations-hands-on)をcloneします。PowerShellまたはターミナルで、cloneしたrepo rootからSectionのdirectoryへ移動して実行します。

```powershell
git clone https://github.com/toma1110/amazon-bedrock-agentcore-operations-hands-on.git
cd amazon-bedrock-agentcore-operations-hands-on
cd sections/s02
uv sync --locked
uv run pytest
```

unit testはローカルの固定データを確認します。AWSやモデルを呼ばないため、実Bedrock呼出しの成功を示すものではありません。

### 1. 実行先と接続を確認する

次の`<学習用account ID>`を今回使う予定の12桁IDへ置き換えます。実装は、まず指定したaccount IDが12桁の数字かを確認し、続けてregionが東京、model IDが固定値かを検証します。その後STSからaccountを取得して指定値と照合し、違う場合はBedrock clientを作る前に停止します。一致した場合だけ、最大16 tokenの短い`Converse`リクエストを1回送ります。この確認ではAgentも教材toolも実行しません。

```powershell
uv run python agent_app.py --preflight --expected-account <学習用account ID>
```

成功時はaccount/regionの照合結果、`amazon.nova-lite-v1:0`、`Bedrock connectivity: response received`を表示します。モデルの返答文ではなく、API応答を受け取れたことを確認します。この1回もモデル推論のため利用料が発生します。`AWS_DEFAULT_REGION`を指定しない場合は`ap-northeast-1`（東京）を使います。このSectionでは東京以外への切替えを扱いません。

### 2. Agentを実行する

事前確認が成功した後、同じexpected account IDを付けて質問を試します。Agent実行の直前にもSTS accountを再確認し、一致しなければモデル呼出し前に停止します。

```powershell
uv run python agent_app.py --question "catalog-api の5xxを調べる順序を教えてください。" --expected-account <学習用account ID>
uv run python agent_app.py --question "エラーが出ています。どの手順で調べればよいですか？" --expected-account <学習用account ID>
```

1つ目は`service=catalog-api`と`topic=5xx`が分かるため、教材読取りツールの要求、実際の引数、取得元と結果、Agentの回答が出ることを確認します。2つ目では対象サービスと問題の種類が分かりません。ツールを呼ばずに不足情報を尋ねるかを確認します。`--demo`を使うと同じ2ケースを続けて実行できます。追加実行のたびにモデル利用料が発生します。

### 3. コードの処理を追う

`agent_app.py`では、次の順に実装しながらデータと結果の対応を確認します。

1. `@tool`の`lookup_runbook(service, topic)`で入力とtoolの説明を定義する。
2. `find_runbook`が`runbooks.json`からservice/topicの完全一致を探し、不一致なら`found: false`、source、メッセージを返す。
3. `lookup_runbook`が要求引数と返却レコードをtraceへ記録し、JSON文字列を返す。
4. `_make_agent`で`SYSTEM_PROMPT`、モデル、`tools=[lookup_runbook]`をAgentへ登録する。
5. `run_question`が質問をAgentへ渡し、Agentのtool要求・関数実行・結果返却を経て回答とtraceを表示する。

既存の固定データを1件追加・変更する練習では、先に`runbooks.json`のservice/topicと返却内容を決め、`find_runbook`の完全一致検索と不一致結果を確かめます。次に`lookup_runbook`の入力・説明・返却値を合わせて、`_make_agent`へ登録し、固定入力のunit testを実行します。最後に質問をAgentへ渡し、表示された要求引数、trace、回答をデータと照合します。

既存例では`service=catalog-api`と`topic=5xx`が一致結果につながります。確認項目はエラー増加と直前のデプロイ時刻、同時間帯の依存先応答時間と5xx、request IDに対応するログ行です。不一致では手順を作らず該当なしを返します。unit testは固定入力に対する関数の動作を確かめますが、モデルがtoolを要求するかどうかの実証とは別です。

## 結果の読み方

- `--preflight`はSTS accountをexpected accountと照合してからBedrockへ接続確認を送ります。不一致ならBedrock clientを作らず停止します。Agent実行でも直前にaccountを再確認します。
- 各質問の完了後にStrandsが返すAgent invocation単位のinput/output/total token usageを表示します。モデル応答のusageが欠ける場合は値が`None`になり得るため、請求額はAWS Billingの明細を正としてください。
- 十分な質問では、tool traceに`service`と`topic`の引数、`sections/s02/runbooks.json`の一致結果が表示されます。最後の回答に、ツール結果を使った確認順序が含まれるか読みます。
- 情報不足の質問では`(no tool call)`となり、回答で必要な情報を聞き返すことを確認します。モデルの応答は常に同じ文章になるとは限らないため、実際のtool traceで呼出しの有無・`service`/`topic`引数・返却データを質問と照合します。toolが呼ばれた場合、または不足情報を尋ねない場合は課題の完了とせず、`agent_app.py`の`SYSTEM_PROMPT`で両方の識別子が明示された場合だけtoolを使う指示になっているか確認し、同じ不足質問を再実行してtraceを比較します。教材toolは`runbooks.json`にあるservice/topicの組み合わせだけを検索し、他の組み合わせは不一致として返します。ただしモデルが質問の情報不足を正しく判断すること自体をコードで保証するものではありません。
- `Agent answer`には回答本文だけが表示され、モデルが応答に含めた`<thinking>...</thinking>`区間は画面表示から除外されます。tool traceと回答本文を照合し、モデルが付加した推論用表示を事実の証拠として扱わないでください。
- runbookが示すのは確認順序だけです。確認の結果や実際の原因は示さず、手順の提示は原因特定・復旧・適切な対応を保証しません。回答が教材データを越えて断定していないか確認してください。
- 見つかった手順は架空の教材データです。実AWS環境の状態、原因、復旧方法を示すものではありません。

## 費用

外部リソースは作成しません。費用が発生するのはBedrockのモデル推論で、入出力token量に応じた従量課金です。現在の東京リージョンの単価は[Amazon Bedrock料金表](https://aws.amazon.com/bedrock/pricing/)で実行前に確認してください。`--preflight`は最大16 tokenの接続確認を1回行います。これに加え、`--demo`は2つのAgent質問を実行します。1質問のAgentは最大4回のmodel turn、合計6,000 token・出力2,048 tokenまで、1回の応答は512 tokenまでに制限されます。質問文は2,000文字までです。費用は現行単価と実際のtoken数によるため、実行前にAWSの料金表を確認し、請求明細を正としてください。テストだけならモデル費用はかかりません。

## 想定と異なる場合

- `Unable to locate credentials`など: profileの選択とIdentity Center sessionの有効期限を確認します。教材は認証を作成・更新しません。
- `AccessDeniedException`: `bedrock:InvokeModel`が利用者または実行roleに許可されているか、対象account/regionを管理者と確認します。この教材のために権限を広げないでください。
- `ValidationException`またはmodel not available: regionが`ap-northeast-1`であること、モデルIDが`amazon.nova-lite-v1:0`であること、Bedrockの現在のモデル提供状況を確認します。別モデルへ自動切替しません。
- account mismatch: 表示されたaccount IDとexpected account IDを照合し、意図した学習用profileまたはIdentity Center sessionを選び直します。誤ったaccountのまま続けません。
- Bedrock connectivityで失敗: 認証エラーならprofile/session、account不一致ならexpected ID、regionなら`AWS_DEFAULT_REGION`、続いてmodel ID・モデル利用可否・`bedrock:InvokeModel`許可を確認します。原因確認前に別regionや別modelへ切り替えません。

## 後片付け

Agentはプロセス内でローカルJSONを読むだけなので、AWS resourceの削除はありません。継続課金するresourceもありません。作業を終えるときはプロセスを終了し、必要に応じてこのSectionの`.venv/`を削除してください。`uv.lock`とsourceはs03で再利用するので残します。費用の記録はBilling and Cost Managementで確認できます。モデル呼出し履歴が直ちに費用明細へ反映されない場合があります。
