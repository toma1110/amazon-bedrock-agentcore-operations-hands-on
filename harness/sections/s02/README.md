# S02 実習：モデルとHarnessの応答を照合する

## この実習で確かめること

同じ質問を東京リージョンのAmazon Bedrockモデルへ直接送り、次にAgentCore Harnessへ送ります。モデル単体の応答と、Harnessが許可された教材用ツールを使う実行を比べ、設定・ツール要求・合成データの返却値・最終回答を同じ実行の記録で照合します。

- **L02**：モデルへ直接送った質問と応答を記録します。応答文だけでは外部データを調べた証拠にならないことを確認します。
- **L03**：モデル、指示、専用実行ロール、個別の`allowedTools`、Memory無効を設定し、作成後の値を読み戻します。
- **L04**：同じ質問でHarnessを呼び出し、ツール要求・入力・結果と、結果を受け取った後の回答を照合します。

両方に使う質問は次のとおりです。

> 教材用の架空記録 CASE-042 を確認し、記録されているHTTP状態と次の確認項目を答えてください。記録が確認できなければ不明としてください。

ツールが読むのは`exercise.py`に含まれる1件の架空データだけです。実際のAWSリソースや障害は調べません。HTTP 503も実習用に固定した合成値です。

## 用意するもの

- 講座用AWSアカウントにサインインできること
- 東京リージョン（`ap-northeast-1`）のCloudShell
- CloudShellで使えるAWS CLI、Python 3、`unzip`、`sha256sum`
- `boto3`と`botocore`。必要なAPIモデルが不足する場合に限り、この手順で作る仮想環境へ固定版`boto3==1.43.69`を導入します
- `amazon.nova-lite-v1:0`の利用契約・利用資格と、モデル呼び出し権限
- CloudShellの呼び出し元には、モデル利用可否を読む権限、モデル単体呼び出し権限、実習専用IAMロールの作成・読み取り・削除権限、Harnessの作成・読み取り・実行・削除権限、対象ロググループの読み取り・削除権限が必要です。組織の権限境界やサービスコントロールポリシーも適用されます

権限不足、モデル利用条件の未完了、またはアカウント・呼び出し元の不一致があれば、書き込み操作へ進まず停止します。管理者権限を追加して解決しようとせず、講座用環境の管理者に不足している権限名を確認してください。

## CloudShellで配布ファイルを取得・検証する

ZIPはCloudShellの中で直接ダウンロードします。PCからのアップロードは不要です。READMEとZIPには同じ`PUBLIC_REF`を使います。初期値は`main`です。講師から固定コミットSHAの案内がある場合は、`main`をそのSHAに置き換えてください。 [ZIPを開く](./s02-harness-exercise.zip)

AWSコンソールで講座用アカウントと東京リージョンを選び、CloudShellを開いて次を実行します。

```bash
PUBLIC_REF=main
BASE_URL="https://raw.githubusercontent.com/toma1110/amazon-bedrock-agentcore-operations-hands-on/${PUBLIC_REF}/harness/sections/s02"
EXERCISE_DIR=$(mktemp -d "$HOME/s02-harness.XXXXXX")
curl -fL "${BASE_URL}/README.md" -o "$EXERCISE_DIR/s02-README.md"
curl -fL "${BASE_URL}/s02-harness-exercise.zip" -o "$EXERCISE_DIR/s02-harness-exercise.zip"
EXPECTED_ARCHIVE_SHA256=7C1502DED306EFC69BAEB8D2E7B6157D8597D27AC8F8F967CE190F638B904484
printf '%s  %s\n' "$EXPECTED_ARCHIVE_SHA256" "$EXERCISE_DIR/s02-harness-exercise.zip" | sha256sum -c -
```

チェックサムが`OK`にならない場合は展開・実行せず、CloudShell内のZIPを削除して講師に確認してください。続けて同じCloudShell端末で展開し、3ファイルのSHA-256を照合します。

```bash
mkdir "$EXERCISE_DIR/exercise"
unzip -q "$EXERCISE_DIR/s02-harness-exercise.zip" -d "$EXERCISE_DIR/exercise"
cd "$EXERCISE_DIR/exercise"
sha256sum exercise.py execution-role-trust.json execution-role-policy.json
```

3ファイルのSHA-256がすべて次と一致し、ZIPにこの3ファイル以外が含まれないことを確認してください。

| ファイル | SHA-256 |
| --- | --- |
| `exercise.py` | `F8F0E1F8A4D1FBCE82AB4096227F7344A8FAD5927CD645F1746AD65EA7A3007D` |
| `execution-role-trust.json` | `57B29E00444B97980066CFDB60995AD8F9EC63AEA0188E07802DF4AB57BE8138` |
| `execution-role-policy.json` | `E2FA41B6EFB4C79A7836731755ED4CE5CE0A3504D769EA9C04975F9202461F29` |

不一致、ファイル不足、予期しないファイルがあれば続行しません。

## PythonとSDKを確認する

```bash
aws --version
python3 --version
python3 -c 'import boto3, botocore; print("boto3", boto3.__version__); print("botocore", botocore.__version__)'
```

`boto3`または`botocore`の読み込みに失敗する場合は後続のAWS操作へ進みません。CloudShell標準のSDKで後述の`preflight`が必要なAPIモデルを確認します。不足している場合だけ、作業フォルダー内に仮想環境を作り、固定版を導入します。システムPythonは変更しません。

```bash
python3 -m venv "$EXERCISE_DIR/.venv"
source "$EXERCISE_DIR/.venv/bin/activate"
python -m pip install 'boto3==1.43.69'
python3 -c 'import boto3, botocore; print("boto3", boto3.__version__); print("botocore", botocore.__version__)'
```

以降も仮想環境を有効にした同じ端末で実行します。固定版でも`preflight`に必要なAPIモデルがなければ、SDKをさらに更新せず停止してください。

## 作成するものと料金

この演習は、専用Harness、専用IAM実行ロールとインラインポリシー、Harness Runtimeのロググループを使います。実行ロールの権限とHarnessの`allowedTools`は別の境界です。

- **Harness起動・AWS認証**：CloudShellのログイン済みセッションからCLIを実行します。Harness用ロールの信頼ポリシーはAgentCoreサービスからの引き受けを、このアカウントと東京リージョンのAgentCoreリソースに制限します。Harness実行時のECR Public認証トークンとWorkload Identity token取得もこの用途です。
- **モデル呼び出し**：専用ロールは東京の`amazon.nova-lite-v1:0`に対するモデル呼び出しだけを許可します。
- **記録・テレメトリー**：専用ロールは今回のHarness Runtimeに対応するロググループへの記録、AgentCoreのトレース送信と`bedrock-agentcore`名前空間のメトリクス送信を許可します。
- **教材用ツール**：`lookup_example_record`はローカルの合成データを読むPython関数です。AWS API、ネットワーク、ファイルを呼び出さず、IAM権限も必要としません。`allowedTools`はこのツール1つだけです。

この実習ではモデル呼び出しと、Harnessの短時間稼働・ログ・テレメトリーにAWS利用料が発生します。見込みは少額（数セント程度）ですが、処理時間やトークン、記録量により変動し、上限を保証しません。モデル単体の呼び出しは1回、Harnessはツール要求と結果返送の一連の呼び出しです。

## 1. アカウント・リージョン・呼び出し元を確認する

画面上で選択したAWSアカウントとリージョンが講座用のものと一致するか確認します。次のコマンドはアカウントIDと呼び出し元ARNだけを端末内で読み取り、画面には表示しません。認証情報や環境変数一覧を表示・共有しないでください。

```bash
read -rs -p '確認済みの講座用AWSアカウントID: ' EXPECTED_ACCOUNT
printf '\n'
read -r CURRENT_ACCOUNT CURRENT_CALLER_ARN < <(aws sts get-caller-identity --query '[Account,Arn]' --output text)
if [ "$EXPECTED_ACCOUNT" != "$CURRENT_ACCOUNT" ]; then
  printf '%s\n' 'アカウントが一致しません。実習を中止してください。'
else
  printf '%s\n' 'アカウントが一致しました。'
fi
```

不一致なら中止します。一致したら現在の呼び出し元を後続コマンドに渡します。

```bash
COMMON_ARGS=(--expected-account "$EXPECTED_ACCOUNT" --expected-caller-arn "$CURRENT_CALLER_ARN")
```

アカウントID、ロールARN、Harness IDなど環境固有値を、README・Issue・共有ログへ貼らないでください。画面出力を共有する場合は、環境固有値を伏せます。

## 2. 利用条件を読み、モデル単体で質問する（L02）

```bash
aws bedrock get-foundation-model-availability --region ap-northeast-1 --model-id amazon.nova-lite-v1:0
```

利用契約と権限が`AVAILABLE` / `AUTHORIZED`、利用資格とリージョンが`AVAILABLE`であることを確認します。値が異なる、または権限エラーの場合はモデルを呼び出さず停止してください。

```bash
python3 exercise.py preflight "${COMMON_ARGS[@]}"
python3 exercise.py baseline "${COMMON_ARGS[@]}"
```

`preflight`はアカウント、呼び出し元、リージョン、SDKの対応を確認します。リソース名の競合とモデル利用条件は確認しません。`baseline`は同一質問をモデルへ直接1回送ります。回答の文章は生成ごとに変わるため、決まった文面を期待しません。回答だけで合成記録を読んだとは判断しないでください。

## 3. 専用ロールとHarnessを作る（L03）

実行ロールには用途別の権限を設定します。アカウントIDはテンプレート内のプレースホルダーから、この実行で確認した値に置き換えます。Harnessの設定では`allowedTools`に`@lookup_example_record`だけを指定し、Memoryを明示的に無効にします。

```bash
python3 exercise.py prepare-role "${COMMON_ARGS[@]}"
python3 exercise.py create "${COMMON_ARGS[@]}"
```

コマンドは作成前に予約名の既存リソースを読み取ります。この演習の所有物と確認できない同名リソースがある場合は変更せず停止します。作成後の読み戻しで次を確認します。

- モデルが`amazon.nova-lite-v1:0`、API形式が`converse_stream`である。
- 指示が合成データの範囲を守り、記録にある事実だけを返す。
- 専用実行ロールと信頼ポリシー、インラインポリシーが配布ファイルの内容に一致する。
- `allowedTools`が`@lookup_example_record`の1つだけである。
- Memoryが省略でなく明示的に無効である。

設定が一致しない、読み戻せない、Harnessが`READY`にならない場合は呼び出しへ進みません。指示は応答方針、`allowedTools`はHarnessが使えるツール、IAMロールはAWS APIの認可を定めます。それぞれ別に確認します。

## 4. Harnessの実行とツール結果を照合する（L04）

```bash
python3 exercise.py invoke "${COMMON_ARGS[@]}"
```

出力を同一実行の記録として読みます。

1. 質問がL02と同じである。
2. ツール名が`lookup_example_record`で、入力が`CASE-042`だけである。
3. ツール結果が合成データ`http_status=503`と次の確認項目である。
4. 同じセッションで結果を返した後の最終回答が、その結果に基づいている。

結果は教材内に固定した架空データで、実際のAWSや障害の状態を示しません。ツール要求・入力・結果・最終回答が一つの実行に結び付かない場合や、想定外のツールが現れた場合は成功と扱わず停止します。

## 5. エラー時は再送せず状態を確認する

通信エラーやタイムアウトでは、AWS側で処理を受け付けたか分からないことがあります。`baseline`や`invoke`をすぐに再実行しないでください。プログラムは開始済みの呼び出しをローカル状態ファイルへ記録し、重複実行を止めます。

Harness作成の応答が不明な場合は、まずCLIの読み取りで完全一致するHarness名と状態を確認します。対象が見つかった場合は削除・再作成・呼び出しをせず、所有確認を担当者に相談してください。対象がなく、同名Harnessが存在しないと確認でき、作成前の状態記録も残っているときだけ、保存済みtokenで安全に再開できます。

```bash
python3 exercise.py create "${COMMON_ARGS[@]}" --resume-idempotent
```

作成状態が不明なまま片付ける場合、Harnessがないことを読み取りで確認した後に限り、次の確認フラグを使えます。

```bash
python3 exercise.py cleanup "${COMMON_ARGS[@]}" --confirm-no-harness
```

削除応答が不明な場合は、状態記録と読み戻しで今回所有するHarness IDを確認した場合だけ、同じ削除の再開フラグを使います。

```bash
python3 exercise.py cleanup "${COMMON_ARGS[@]}" --resume-delete
```

状態・所有・作成時刻を確認できないときは、`exercise-state.json`を消さず担当者へ相談してください。別の演習や利用者のリソースを流用・変更・削除しません。

よくある停止理由：

- **利用資格や権限のエラー**：モデルの利用条件とCloudShell呼び出し元の権限を管理者に確認します。
- **SDKのAPIモデル不足**：上記の固定版を仮想環境へ入れます。さらに新しい版へ更新しません。
- **名前の競合、予期しないポリシー**：所有者が今回の演習と確認できない限り停止します。
- **Harnessが5分以内に準備・削除できない**：再作成や権限拡張をせず、状態を保って担当者に相談します。
- **ロググループが残る**：Harness Runtime IDと作成時刻で今回の実行分と確認できるものだけを扱います。名前の接頭辞一致だけで削除しません。

## 6. 作成したリソースを片付け、残存を確認する

実行結果の照合後、まずHarnessから管理されたRuntimeを読み取りで確認します。通常は、この演習のHarness名に対応するRuntimeが1件表示されます。`list-agent-runtimes`は一覧を読むだけです。このコマンドではリソースを変更・削除しません。

```bash
aws bedrock-agentcore-control list-agent-runtimes --region ap-northeast-1 --query "agentRuntimes[?starts_with(agentRuntimeId, 'harness_UdemySZeroTwoIssueFourNineThree-')].[agentRuntimeName,status]" --output table
```

表示がちょうど1件ならRuntimeの名前と状態を確認し、通常の片付けへ進みます。0件または複数件の場合は、作成が完了しなかった場合や既に片付け済みの場合もあり得ます。空の一覧だけで「リソースがない」と決めつけず、`exercise-state.json`、Harness一覧、作成結果と照合してください。所有と状態を確認できなければ、状態ファイルを保持して担当者に相談し、`create`、`invoke`、削除を繰り返しません。既にCleanupが完了している場合は再度Cleanupせず、以下の残存確認へ進みます。

```bash
python3 exercise.py cleanup "${COMMON_ARGS[@]}"
```

Cleanup後に次の読み取りを行います。Harness一覧・Runtime一覧・ロググループ一覧は空、IAMの`get-role`は`NoSuchEntity`であることを確認します。Runtime IDの接頭辞は絞り込みにだけ使い、一覧ではRuntime名と状態だけを表示します。接頭辞だけを根拠に削除しません。出力に含まれるIDやARNを共有しないでください。

```bash
aws bedrock-agentcore-control list-harnesses --region ap-northeast-1 --query "harnesses[?harnessName=='UdemySZeroTwoIssueFourNineThree'].[harnessName,status]" --output table
aws iam get-role --role-name UdemySZeroTwoIssueFourNineThreeRole --query 'Role.RoleName' --output text
aws bedrock-agentcore-control list-agent-runtimes --region ap-northeast-1 --query "agentRuntimes[?starts_with(agentRuntimeId, 'harness_UdemySZeroTwoIssueFourNineThree-')].[agentRuntimeName,status]" --output table
aws logs describe-log-groups --log-group-name-prefix "/aws/bedrock-agentcore/runtimes/harness_UdemySZeroTwoIssueFourNineThree-" --query 'logGroups[].{name:logGroupName,created:creationTime}' --output table --region ap-northeast-1
```

Cleanup後に1件でも表示される、IAMが`NoSuchEntity`でない、または所有・状態が不明な場合は成功と扱わず、IDやARNを共有せず担当者に相談してください。既にCleanup済みで一覧が空なら、その状態を確認結果として記録します。

## ローカル状態と情報の扱い

`exercise-state.json`と`.harness-*-token`は作業フォルダー内に一時作成され、アカウントID、Harness ID、ロールARN、実行状態などを含むことがあります。共有したり、配布物へ含めたりしません。Cleanupと不在確認が終わったらCloudShellの作業フォルダーを閉じてください。認証情報、個人情報、実環境の記録を教材や共有ログに貼り付けないでください。

## 公式資料

- [Harnessのモデルと指示](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-models.html)
- [Harnessのツールと`allowedTools`](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-tools.html)
- [Harnessの提供リージョン](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html)
- [Harnessの実行、記録、料金](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html)
