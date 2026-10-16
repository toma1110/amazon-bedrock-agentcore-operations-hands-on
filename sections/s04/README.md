# s04 Gateway toolで合成CloudWatchデータを読む

このSectionでは、s03で作成したAgentCore Runtimeから、AWS IAM認証のAgentCore Gatewayを経由してLambda toolを呼び出します。Lambdaは教材用の合成CloudWatch Logsとmetricだけを読みます。実システム、顧客データ、本番環境へは接続しません。

Gateway targetの名前は`udemy-agentcore-s04-cw-target`、Runtimeから見えるtool名は`udemy-agentcore-s04-cw-target___read_synthetic_cloudwatch`です。Lambda roleとGateway roleのinline policy名は、それぞれ`S04CloudWatchRead`と`S04InvokeReaderLambda`です。inline policy名は管理画面上の識別子であり、権限はpolicy documentのstatementで決まります。policy名を理由にstatementやresource scopeを広げたり、既存policyを重複作成したりしません。

## 何を確認するか

呼び出し元からRuntimeへの認証、Runtime execution roleからGatewayへの許可、Gateway roleからLambdaへの許可、LambdaからCloudWatchへの読み取り許可は、別々の境界です。GatewayのMCP routing metadataは接続先を照合する情報であり、認証の代わりにはなりません。

Lambdaは`service=catalog-api`、UTCの直近7日以内、最大60分、最大50件だけを受け付けます。Logsは`/udemy/agentcore/s04/synthetic/catalog-api`に限定します。Classic CloudWatch `MetricStat`による`GetMetricData`はIAMでnamespace、metric name、dimension別に制限できないため、実行roleではresource `*`と`aws:RequestedRegion=ap-northeast-1`を使います。metric namespace/name/dimension、期間、件数はLambdaコードで固定します。この制限はIAM境界ではありません。

## 前提

- s03を完了し、既存RuntimeがREADYであること。s03のローカルstate file `.agentcore-s03-state.json`も保持します。
- package作成にはrepository rootでBashと`uv`を使います。CloudShellのworkflow実行にはPython 3.13と、lock fileからexportしたhash付きrequirementsを使う専用virtual environmentを用意します。AWS操作はAWS ConsoleのCloudShell（region `ap-northeast-1`、Bash）から実行します。CloudShellのaccountを開始前に照合してください。
- AWS named profileを使うローカル実行では各commandのsubcommandより前に`--profile PROFILE_NAME`を付けます。CloudShellではConsoleの既定credentialを使い、profile nameやcredential値を手入力しません。
- 演習用AWS accountと東京region `ap-northeast-1`を使います。credentialやMFA情報をコード、ZIP、環境変数へ貼り付けません。
- resource名がすでに存在する、account/regionが違う、state fileの状態が分からない場合は続行しません。既存resourceを上書き・再利用しません。

## 1. ローカル検査とpackage作成

repository rootから次を実行します。最初の2行はAWS APIを呼びません。package作成はlock fileのdependencyをPython 3.13 ARM64向けに解決し、ZIPサイズとSHA-256を表示します。

```bash
uv lock --locked --project sections/s02
uv run --project sections/s02 pytest sections/s03/tests sections/s04/tests -q
uv run --project sections/s02 python sections/s04/package_artifacts.py --output sections/s04/build
uv export --locked --project sections/s02 --no-dev --format requirements.txt --output-file sections/s04/build/requirements-cloudshell.txt
sha256sum sections/s04/build/s04-lambda.zip sections/s04/build/s04-runtime-arm64.zip
sha256sum sections/s04/build/requirements-cloudshell.txt
```

`s04-lambda.zip`には`cloudwatch_reader.py`だけを含み、handlerは`cloudwatch_reader.lambda_handler`です。`s04-runtime-arm64.zip`にはlock済dependency、S02の`agent_app.py`と`runbooks.json`、S03の`runtime_app.py`と`gateway_tools.py`を含みます。ZIPを展開してcredentialや無関係なfileがないことも確認してください。packageのhashを実行記録に残します。テスト成功はAWS上のresource、権限、実動作の確認ではありません。

## CloudShellへ安全に配置する

AWS ConsoleのCloudShellを東京regionで開き、Actionsの「Upload file」から次のファイルをCloudShell homeへアップロードします。これは自分のAWSアカウントのCloudShellへの転送です。state fileやZIPをGitへ追加しません。

- `sections/s04/aws_workflow.py`
- `sections/s04/cloudwatch_reader.py`
- `sections/s04/emit_synthetic_data.py`
- `sections/s03/.agentcore-s03-state.json`
- `sections/s04/build/s04-lambda.zip`
- `sections/s04/build/s04-runtime-arm64.zip`
- `sections/s04/build/requirements-cloudshell.txt`

CloudShell内で専用作業directoryを新規作成し、重複時には停止します。`sha256sum`のCloudShell側出力を、package作成時に記録したhashと照合してください。1文字でも違えば実行しません。

```bash
WORK="$HOME/agentcore-s04"
test ! -e "$WORK" || { echo "既存directoryがあります。別名を選びます。"; exit 1; }
mkdir -p "$WORK/sections/s03" "$WORK/sections/s04/build"
cp -n "$HOME/aws_workflow.py" "$WORK/sections/s04/aws_workflow.py"
cp -n "$HOME/cloudwatch_reader.py" "$WORK/sections/s04/cloudwatch_reader.py"
cp -n "$HOME/emit_synthetic_data.py" "$WORK/sections/s04/emit_synthetic_data.py"
cp -n "$HOME/.agentcore-s03-state.json" "$WORK/sections/s03/.agentcore-s03-state.json"
cp -n "$HOME/s04-lambda.zip" "$WORK/sections/s04/build/s04-lambda.zip"
cp -n "$HOME/s04-runtime-arm64.zip" "$WORK/sections/s04/build/s04-runtime-arm64.zip"
cd "$WORK"
sha256sum sections/s04/build/s04-lambda.zip sections/s04/build/s04-runtime-arm64.zip
sha256sum "$HOME/requirements-cloudshell.txt"
sha256sum sections/s04/aws_workflow.py sections/s04/cloudwatch_reader.py sections/s04/emit_synthetic_data.py
```

上記の`$HOME/`直下のファイル名はCloudShellにアップロードした名前です。既存ファイルを上書きせず、転送ごとに固有名を付けてください。以下のAWS workflow例はCloudShell上で`$WORK`をcurrent directoryとして実行します。

CloudShellのsystem Pythonやglobal site-packagesは変更しません。Python 3.13であることを確認し、`$WORK/.venv`が存在しない場合だけ専用virtual environmentを作成します。requirements fileはrepositoryの`uv.lock`から`--locked --no-dev`でexportしたものです。ローカルで記録したそのfileのSHA-256とCloudShellの出力が一致することを確認してから、`--require-hashes`でlock済の全dependencyをinstallします。ARM64 Runtime ZIP内のdependencyはx86_64 CloudShellでは使いません。

```bash
python3 --version
test ! -e "$WORK/.venv" || { echo "既存virtual environmentがあります。別名を選びます。"; exit 1; }
python3 -m venv "$WORK/.venv"
"$WORK/.venv/bin/python" -m pip install --disable-pip-version-check --require-hashes -r "$HOME/requirements-cloudshell.txt"
"$WORK/.venv/bin/python" -c 'import boto3; from importlib.metadata import version; from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client; from strands.tools.mcp.mcp_client import MCPClient; print("boto3=" + boto3.__version__); print("mcp=" + version("mcp")); print("strands-agents=" + version("strands-agents")); print("mcp-proxy-for-aws=" + version("mcp-proxy-for-aws")); print("aws_iam_streamablehttp_client=imported"); print("MCPClient=imported")'
```

最後の出力で`boto3=1.43.108`、`mcp=1.30.0`、`strands-agents=1.57.2`、`mcp-proxy-for-aws=1.7.0`と両import成功を確認します。失敗時はAWS操作へ進まず、原因を調査します。

## 2. AWSの事前確認と反映

`123456789012`を演習先の12桁account IDへ置き換えます。`S03_RUNTIME_ID_VALUE`は`s03`の`.agentcore-s03-state.json`に記録された`runtime_id`の値へ置き換えます。どちらもplaceholderなので、実際の値を入力してください。`preflight`はSTS identity、指定account/region、既存s03 RuntimeのREADY状態とlifecycle設定、`bedrock-agentcore:StopRuntimeSession`のexact Runtime ARNに対するIAM policy simulationを読み取り専用で確認します。今回の事前確認で観測したlifecycle値は`idleRuntimeSessionTimeout=900秒`、`maxLifetime=28,800秒`でした。workflowが許容するのは両方が正の整数で、idle timeoutがmaxLifetime以下かつ900秒以下の構成です。`preflight`はその時点の値を表示しますが、lifecycle snapshotを保存してdeploy時と一致することまでは保証しません。さらに、Lambda・専用role・Gateway・log groupのexact name inventoryとmetricの衝突を調べ、確認結果を`.agentcore-s04-preflight.json`へ保存します。このinventory確認は実際に照会した名前の結果であり、AWS上のあらゆるS04関連resourceが存在しないことを保証しません。CloudWatch `ListMetrics`は直近2週間にデータがないmetric seriesを列挙しません。このため、検出できるのは同一dimensionのうち最近観測可能なseriesに限られ、より古い履歴が存在しないことや、列挙されたseriesのデータが直近7日間にあることは証明しません。同一seriesが列挙された場合は、期間を厳密に区別せず保守的に衝突として止めます。すべて一致しなければ止め、policyやaccount assignmentを変更してはいけません。

```bash
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py preflight --expected-account 123456789012 --expected-region ap-northeast-1 --expected-runtime-id S03_RUNTIME_ID_VALUE
```

事前確認がGOの場合のみ、指定された2つのZIPを渡して反映します。`deploy`は専用Lambda/Gateway/roleと演習用log groupを作成し、既存s03 RuntimeへGateway tool接続を追加します。Runtime設定を読み取りますが、初回deploy時に`environmentVariables`が空でない、または形式が不正な場合は、値を表示・stateへ保存せず、AWSを変更する前に停止します。既存Runtimeではこの項目は未設定でした。変更前のRuntime artifact/configを必要なallowlist項目に限定してS04 stateへ記録し、DEFAULT endpointを更新します。deployはRuntimeを再読し、idle timeoutが正の整数でmaxLifetime以下かつ900秒以下、maxLifetimeが正の整数である場合に、その時点のlifecycle設定を変更せず使います。preflightで観測した値との連続一致は確認しません。Runtimeは新しいversionを作り、DEFAULTが新versionへ進みます。`deploy`が途中で曖昧なエラーを返したら、state fileを残して現在状態を調査します。同じcommandを再実行したり、stateやZIPを置き換えたりしません。

```bash
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py deploy --expected-account 123456789012 --expected-region ap-northeast-1 --expected-runtime-id S03_RUNTIME_ID_VALUE --lambda-zip sections/s04/build/s04-lambda.zip --runtime-zip sections/s04/build/s04-runtime-arm64.zip
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py status
```

`status`でRuntimeがREADYであることを確認します。各`invoke`の直前にもRuntimeのREADY状態とlifecycle設定を読み取り専用で確認し、deploy時にstateへ保存した値から変化していれば自動修正せず停止します。expected toolが一つだけかは、最初のRuntime invocationでMCP clientがlistし照合します。一致しない場合はtool call前に失敗するので、再実行せず調査します。seed stateはCloudWatchの7日保持期間内で再利用でき、各queryはseed時刻のmetric minuteに固定した最大3分のUTC範囲を使います。S04 state fileとpackage ZIPは公開せず、保護された場所で保管してください。

## 2.1 Runtime artifactだけを更新する場合

初回`deploy`後にRuntimeコードだけを差し替える場合は、同じ`deploy`を再実行せず、別の新規build directoryにRuntime ZIPを作ります。以下はrepository rootから実行します。directoryが既に存在する場合は中身を上書きせず、別の未使用名を選びます。既存の`sections/s04/build/s04-runtime-arm64.zip`は保持します。

```bash
CANDIDATE_DIR=sections/s04/build/runtime-artifact-v2
test ! -e "$CANDIDATE_DIR" || { echo "既存build directoryです。別の名前を選んでください。"; exit 1; }
uv run --project sections/s02 python sections/s04/package_artifacts.py --output "$CANDIDATE_DIR"
test ! -e "$CANDIDATE_DIR/s04-runtime-arm64-v2.zip" || { echo "転送用ZIP名が既にあります。停止します。"; exit 1; }
cp -n "$CANDIDATE_DIR/s04-runtime-arm64.zip" "$CANDIDATE_DIR/s04-runtime-arm64-v2.zip"
sha256sum "$CANDIDATE_DIR/s04-runtime-arm64-v2.zip"
```

表示したhashを記録し、`s04-runtime-arm64-v2.zip`をCloudShellへuploadします。CloudShellで既存作業directoryを使い、hashが一致した場合だけ別のcandidate directoryへコピーします。

```bash
test ! -e "$WORK/sections/s04/build/runtime-artifact-v2" || { echo "既存candidate directoryです。停止します。"; exit 1; }
mkdir -p "$WORK/sections/s04/build/runtime-artifact-v2"
cp -n "$HOME/s04-runtime-arm64-v2.zip" "$WORK/sections/s04/build/runtime-artifact-v2/s04-runtime-arm64.zip"
cd "$WORK"
sha256sum sections/s04/build/runtime-artifact-v2/s04-runtime-arm64.zip
```

CloudShellで表示したhashが記録値と完全一致することを確認してから、次のcommandを一度だけ実行します。commandはaccount、region、Runtime ID、READY状態、version、artifact、role、network/protocol、environment、metadata、lifecycleと保存済みstateの一致を読み取り専用で確認し、全sessionのstop responseが最新attemptでHTTP status 200、status code 200、request IDありであることも確認します。`positive_attempts`に`requested`の記録が残っている場合は、先に下の「例外時のrecovery」を完了し、該当Runtime log eventとstop responseが一意に整合して`reconcile-invocation`が成功した後で再開します。元のattempt記録を変更・削除しません。新しいSHA256を含むS3 keyが未使用であることを確認してから一度だけuploadし、保存済みV3設定のartifact参照だけを新しいkeyへ変更したrequestでRuntimeを一度だけ更新します。古いRuntime update requestとS03の変更前snapshotは履歴として保持します。

```bash
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py update-runtime-artifact --expected-account 123456789012 --expected-region ap-northeast-1 --expected-runtime-id S03_RUNTIME_ID_VALUE --runtime-zip sections/s04/build/runtime-artifact-v2/s04-runtime-arm64.zip
```

このcommandのstateに更新intentが保存された後は、再実行してもS3 uploadやRuntime updateを繰り返しません。曖昧な結果では読み取り専用の状態照合だけを行い、対象object、Runtime version、artifact、READY状態が一致しなければ停止します。新しいDEFAULT versionがREADYになった後の`invoke`は、そのactive versionとartifactも一致する場合だけ進みます。更新の見込み費用を事前に既存の承認済みbudgetへ含め、上限到達前に追加呼出しを止めてください。

## 3. 合成データを作る

以下はCloudWatch Logsとmetricへ合成データを一度だけ書き込みます。`--expected-account`は実際に確認したaccountへ置き換えます。`.s04-seed-state.json`を同じ場所に保持してください。書込み中の接続断では自動replayしません。状態が`*_requested`で止まった場合はstate fileを削除せず、該当stream/event/metricを調査します。過去のwrite有無が確定できなければ先へ進みません。

```bash
"$WORK/.venv/bin/python" sections/s04/emit_synthetic_data.py --expected-account 123456789012 --state-file sections/s04/.s04-seed-state.json
```

正常終了時にはaccount、region、log group、metric識別子が表示されます。作成されるのは`service=catalog-api`、`status_code=503`、識別用`request_id`を含む合成log eventと`Udemy/AgentCoreS04`の`Http5xxCount` datapointです。値は本番や実障害を表しません。

## 4. 動作、境界、結果の確認

### 通常の確認

次の`invoke`を1回実行すると、Runtimeを同じsessionで2回呼び出します。1回目はCloudWatch Logs、2回目はmetricを順番に確認し、それぞれのinvocation IDとresponseを表示します。commandは両方の結果が返るまで終了しません。正常終了後にsessionを停止し、Runtime Logsを読みます。どちらかが失敗・未確認ならcommandを再実行せず、下の「例外時のrecovery」を確認してください。

```bash
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py invoke --expected-account 123456789012 --expected-region ap-northeast-1 --expected-runtime-id S03_RUNTIME_ID_VALUE
```

2回目は、最初の呼び出し開始から5分以内に限って送られます。最初の呼び出しが実行中のまま5分を過ぎた場合、2回目は開始されません。別のCloudShell terminal tabからstateに表示された同じsessionを`stop`し、元のcommandが終了するまで追加の`invoke`を行わないでください。別tabでは最初に専用directoryとvirtual environmentを参照する変数を設定し、既存environmentがあることを確認します。

```bash
WORK="$HOME/agentcore-s04"
cd "$WORK"
test -x "$WORK/.venv/bin/python" || { echo "専用virtual environmentがありません。停止し調査します。"; exit 1; }
"$WORK/.venv/bin/python" -c 'import json,datetime; s=json.load(open("sections/s04/.agentcore-s04-state.json")); print(datetime.datetime.fromtimestamp(s["first_invoke_started_epoch"],datetime.timezone.utc).astimezone())'
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py stop --expected-account 123456789012 --expected-region ap-northeast-1 --expected-runtime-id S03_RUNTIME_ID_VALUE
```

`invoke`が正常に終了したら、同じsessionを`stop`し、Runtime Logsを確認します。`stop`はstateに記録されたS04 sessionだけを対象にします。

```bash
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py stop --expected-account 123456789012 --expected-region ap-northeast-1 --expected-runtime-id S03_RUNTIME_ID_VALUE
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py logs
```

それぞれのtraceで`read_synthetic_cloudwatch`、`service=catalog-api`、保存済みseedのmetric minuteを含む最大3分のUTC範囲、`max_results=50`、`ok=true`を確認します。Logs応答では合成イベントの`request_id`と`status_code=503`を確認します。metric応答では`Http5xxCount`の`datapoints`にseedのmetric minuteと値があることを確認します（実行時刻・応答文は変わります）。例：

```json
{
  "name": "read_synthetic_cloudwatch",
  "arguments": {
    "data_type": "metrics",
    "service": "catalog-api",
    "start_time": "2026-10-15T23:50:00Z",
    "end_time": "2026-10-15T23:53:00Z",
    "max_results": 50
  },
  "result": {
    "ok": true,
    "data": {
      "source": {"namespace": "Udemy/AgentCoreS04", "metric_name": "Http5xxCount", "statistic": "Sum", "period_seconds": 60},
      "datapoints": [{"timestamp": "2026-10-15T23:51:00Z", "value": 1.0}],
      "status": "Complete",
      "truncated": false
    }
  }
}
```

### 例外時のrecovery（通常経路では使わない）

失敗した`invoke`を同じcommandで再実行しません。まずCloudWatch Runtime log groupでinvocation IDに一致するeventと、同じsessionの`stop`応答を確認します。recoveryは、Runtime eventがHTTP 500 / `RuntimeError` / tool call 0回、かつstopがHTTP status 200 / status code 200の組み合わせを一意に示す場合だけ、次のcommandでstateに追記できます。曖昧・不一致・複数eventなら停止して調査します。

```bash
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py reconcile-invocation --expected-account 123456789012 --expected-region ap-northeast-1 --expected-runtime-id S03_RUNTIME_ID_VALUE --invocation-id EXACT_RUNTIME_INVOCATION_ID
```

成功したreconciliationは失敗attemptを成功へ書き換えず、一度だけ新しいsessionで通常の2回確認を許可します。そこで再び失敗した場合は再recoveryせず停止します。合計最大4回のinvocation、1回あたり最大4回のGateway tool request、Gateway上限50 request、Agent上限4 turn / 6,000 total tokens / 2,048 output tokensは変わりません。

次に、Runtime modelを呼ばずに2種類の拒否境界を確認します。1つ目はAWS署名なしGateway request、2つ目は正しいAWS署名を使った`service`不一致requestです。

```bash
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py negative --expected-account 123456789012 --expected-region ap-northeast-1 --expected-runtime-id S03_RUNTIME_ID_VALUE
```

未認証requestが拒否され、`service`不一致requestがLambdaの固定条件で拒否されることを出力で確認します。署名付きのservice不一致結果は、MCP呼び出し自体が`success`で、内容が`{"ok":false,"error":"service is outside the S04 synthetic dataset"}`と一致する場合だけ、境界拒否として扱います。HTTP/MCP error、別のエラー、JSON以外の返却は拒否の証拠ではありません。結果が曖昧または不一致なら追加requestを送らず停止します。入力validationだけではIAMやGateway認証の代わりになりません。

Negative checkの試行は実行前にstate fileへ記録されます。応答が欠落するなど結果が曖昧でもstateを消して再送せず、現在状態を調査します。

## 時間・費用の上限

AgentCore Runtimeは使用したCPU/memoryと稼働時間で課金されます。保守的に最大割当の2 vCPU/8 GBを使い、Tokyoの単価を`$0.1276/vCPU-hour`と`$0.0169/GB-hour`とすると、上限の時間単価は`2×0.1276 + 8×0.0169 = $0.3904/hour`です。事前確認で観測した既存Runtimeのlifecycle値は`idleRuntimeSessionTimeout=900秒`、`maxLifetime=28,800秒`でした。deployは許容条件内のlifecycle設定をそのまま引き継ぎ、各invoke前にdeploy時の設定から変化していないことを確認します。料金見積りではworkflowが許容する最大900秒のidle timeoutを使います。2回の正常invokeは同じsessionを使い、2回目は最初の開始から5分以内だけ許可します。各同期invokeは最大15分です。最初のinvoke開始から2回目開始までを最大5分、2回目を最大15分、最後のrequest後のidle timeoutを最大15分として、合計最大35分で計算します（`2 vCPU + 8 GB × 35分 = $0.22774`）。最初の呼出しが5分を超えて2回目を開始しない場合は、1回のinvoke最大15分とidle timeout最大15分で、最大30分です。停止操作が成功しても失敗しても、この保守的な上限を適用します。session停止の手動確認を行ってもRuntime lifecycle値は変更しません。

| 項目 | 上限見積り |
| --- | ---: |
| Runtime 2 vCPU + 8 GB、35分 | $0.22774 |
| Nova Lite、2回×最大6,000 input / 2,048 output tokens | $0.00205 |
| Gateway MCP/API（最大50 request）+ 1 tool indexing | $0.00045 |
| Lambda、合成Logs、S3 artifact、Logs保管など | $0.00273以内の予備 |
| Classic custom metric 1本（1か月分を全額計上） | $0.30000 |
| **合計（概算）** | **$0.533以下** |

上記は料金の保証ではありません。価格、free tier、実使用量により変わります。開始前に東京regionの最新料金を確認します。$0.80に達する見込みなら新しい呼び出しを停止し、stateに記録したsessionを直ちに停止します。停止APIに失敗しても再invokeせず、AWS Consoleで同じRuntime/sessionを停止してください。既存lifecycle設定を維持する前提で、35分の最悪条件（Runtime約$0.228 + その他約$0.305 ≈ $0.533）は$0.80未満で、$1.00の課金上限に対し約$0.467の余裕を残す想定です。実際の請求額はBillingで後から確認してください。

## 状態、停止、復旧

S04/S03 stateと2つのpackage ZIPは保管し、state fileは保護されたローカル場所に置きます。`logs`と`status`は読み取り専用です。`stop`は記録済みのS04 Runtime ARN/session IDだけに要求し、AWSの応答情報をstateへ保存します。停止が確認できない場合は再invokeせず、同じsessionの状態を調査してください。

通常の演習後はS04 Runtime/Gatewayを後続Sectionで再利用します。S03設定へ戻す必要がある場合だけ、保存済みの変更前artifact/configを使う`restore`を行います。これはDEFAULTを新しい復元versionへ進める操作で、resource削除や古いversionへのrollbackはしません。専用resourceの撤去とCourse全体のcleanupは別手順です。

```bash
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py restore --expected-account 123456789012 --expected-region ap-northeast-1 --expected-runtime-id S03_RUNTIME_ID_VALUE
"$WORK/.venv/bin/python" sections/s04/aws_workflow.py status
```

通常の演習完了後は、後続Sectionで利用できるようS04設定のRuntimeとGateway等をそのまま使います。このworkflowはAWS resourceを削除せず、`restore`は保存済みの変更前Runtime設定へDEFAULTを戻す操作だけです。S04専用resourceの限定撤去とCourse全体の最終cleanupはこのworkflowに含まれず、それぞれ実行時点のlive resource inventoryに基づく別手順で行います。限定撤去では共有resourceを対象にせず、Course全体のcleanupではs10のexact inventoryに共有resourceが含まれる場合があります。CloudWatch Logs retentionは7日で、metricは送信を止めても即時削除できず、保持期間に沿ってexpireします。

## AWS資料

- [AgentCore Gateway MCP toolの呼び出し](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using-mcp-call.html)
- [Boto3でLambda targetをGatewayに追加](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-api-target-config.html)
- [Runtime versioningとDEFAULT endpoint](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html)
- [StopRuntimeSession API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_StopRuntimeSession.html)
- [AgentCore IAM actions, resources, condition keys](https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html)
- [AgentCore料金](https://aws.amazon.com/bedrock/agentcore/pricing/)
