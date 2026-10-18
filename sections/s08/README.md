# s08 更新の影響と費用を確認する

固定の質問と合成runbookを使い、AgentCore Runtimeの更新前後で回答・応答時間・token使用量を比較します。続けて、新しい版から前の版へendpointを切り替え、同じ質問にもう一度回答させます。実AWSログや本番システムは参照・変更しません。

## 開始地点と完成地点

- 開始地点: `sections/s02/agent_app.py`、`sections/s02/runbooks.json`、このSectionのRuntime source。
- 完成地点: S08専用Runtimeの不変なV1・V2と、V1/V2へ切替可能な`s08_training` endpoint。L02/L03をそれぞれ同じCLI runで実行し、4回の実応答を画面で比較します。後続Sectionで利用するため保持し、削除はSection s10の担当です。
- 既存S07 Runtime、role、bucket、log groupは対象外です。変更・削除しません。

## 実行前に確認する

Python 3.13、`uv`、AWS CLI v2と、通常利用する承認済みAWS profile / IAM Identity Center sessionを使います。credential、session tokenをfileやcommand引数へ貼り付けません。利用するtraining account IDを自分のAWS環境で確認し、12桁の値を後述の`$expectedAccount`へ設定します。regionは東京`ap-northeast-1`です。実行直前にSTSでaccountを確認し、一致しなければ停止します。

AWSへ接続しないunit test:

```powershell
uv run --project sections/s02 pytest sections/s08/tests
```

必要であればローカルで教材データを確認できます。手元の`runbooks.json`から1件を読みますが、AWS Runtimeやモデルには接続せず、L02/L03の実行結果を示すものではありません。

```powershell
uv run --project sections/s02 python sections/s08/run_exercise.py --exercise offline-probe
```

Deployment ZIPには`sections/s02/agent_app.py`、`sections/s02/runbooks.json`、`sections/s08/runtime_app.py`と`s02/pyproject.toml`から出力した依存を入れます。Linux ARM64 / Python 3.13向けのZIP作成手順は[s03 README](../s03/README.md)を参照し、sourceと出力先だけをS08へ置き換えます。ZIPのSHA-256とS3 object versionを記録し、S07のartifactは上書きしません。

S08だけに使うresource名は次の通りです。作成前に同名resourceが既にないことを確認し、既存resourceがあれば流用せず停止します。

- Runtime name: `udemy_agentcore_s08`（作成後のRuntime ID/ARNはAWSの応答値を記録）
- Runtime execution role: `udemy-agentcore-s08-runtime`
- artifact bucket: `udemy-agentcore-s08-apne1-<account-id>-<new-6-character-suffix>`
- artifact key: `s08/<zip-sha256>/s08-runtime-arm64.zip`
- comparison endpoint: `s08_training`

RuntimeにはAgentCoreのserverless Runtime V2 platformを選びます。これは「AgentCore Runtimeのplatform version V2」です。比較するagent runtime versionsは別の番号で、初回作成がV1、環境変数`S08_PROMPT_VARIANT`の更新で不変のV2を作成します。名前が似ていますが同じversion番号ではありません。

Runtime execution roleはCallerとは別に作り、S07 roleを共有しません。必要な権限は東京Nova Liteの`bedrock:InvokeModel`とAgentCore Runtime log書込みに限定します。`logs:DescribeLogGroups`はresource-level scope非対応の場合に限り`Resource: "*"`と`aws:RequestedRegion=ap-northeast-1`条件を使い、他のlog書込みはS08 Runtime log-group prefixへ絞ります。Runtime roleへGateway、S3、IAMなどの権限は付けません。Callerの`iam:PassRole`はS08 role ARNのみ、`iam:PassedToService=bedrock-agentcore.amazonaws.com`条件付きです。

## S08専用resourceを作る

以下はこのSectionに限る新規resource作成です。実行前に現在の作業範囲と承認状態、自分のtraining account ID、region `ap-northeast-1`、Caller IAM policy、ZIP hash/内容、上記費用仮定を確認します。以降は確認した12桁IDを使います。既定profileは明示せず、利用中の通常profileを`--profile`で渡してください。credentialをfileや引数に書きません。

### 1. ARM64 ZIPを作成する

```powershell
$work = Join-Path $env:TEMP ("agentcore-s08-" + [guid]::NewGuid().ToString('N'))
$build = Join-Path $work 'package'
$requirements = Join-Path $work 'requirements.txt'
New-Item -ItemType Directory -Path $build | Out-Null
uv export --locked --project sections/s02 --no-dev --format requirements-txt --output-file $requirements
uv pip install --python-platform aarch64-manylinux2014 --python-version 3.13 --target $build --only-binary=:all: -r $requirements
Copy-Item sections/s02/agent_app.py $build
Copy-Item sections/s02/runbooks.json $build
Copy-Item sections/s08/runtime_app.py $build
$zip = Join-Path (Join-Path $PWD 'sections/s08') ("s08-runtime-" + [guid]::NewGuid().ToString('N') + '.zip')
Compress-Archive -Path (Join-Path $build '*') -DestinationPath $zip
Get-FileHash $zip -Algorithm SHA256
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::OpenRead($zip).Entries | Select-Object -ExpandProperty FullName
```

ZIPは250 MB以下で、3つのsourceとLinux ARM64 dependenciesを含み、`.venv`/`__pycache__`を含まないことを確認します。ここで得た実hashがartifact identityになります。setupは新規bucket名のcollision確認、versioning有効化、hash-keyへのuploadとS3 `VersionId`記録、専用role、V1 Runtime、`s08_training` endpointの順で一度だけ行います。エラー時はlocal stateを保全し、`aws_workflow.py status`と実AWSのRuntime/endpoint status・failure reasonをread-onlyで確認します。原因と部分作成resourceを特定するまでsetupを再実行したり、別名で作り直したりしません。

### 2. Caller権限とsetupを実行する

先に今回のCaller policy案を出力し、管理者が現行のCaller権限と照合します。既存権限を置換・変更するコマンドはありません。

```powershell
$expectedAccount = '123456789012' # 例示値。自分のtraining account IDへ置き換える
uv run --project sections/s02 python sections/s08/aws_workflow.py print-caller-policy --expected-account $expectedAccount
```

Callerには出力policyに加え、実行profileが既に持つ認証権限が必要です。policy案はS08専用bucket prefix、専用role、tag付きRuntimeとS08 Runtime/endpoint操作へ絞っています。CloudWatch Logsの`PutResourcePolicy`は含みません。このAPIはregion内の他log groupのresource policyを変更できるためです。Runtime作成やログ書込みで拒否が出た場合は停止し、呼出主体・拒否API・既存S07の設定をread-onlyで照合してから権限を再検討します。scopeを広げて再試行しません。

```powershell
uv run --project sections/s02 python sections/s08/aws_workflow.py setup --zip $zip --expected-account $expectedAccount --profile your-sso-profile
uv run --project sections/s02 python sections/s08/aws_workflow.py status
```

setupが作成するartifact bucket名はS08用の新規suffixです。ZIP、account/region、実際のRuntime ID/ARN、execution role、S3 object key/VersionId、endpoint ARN、READY/liveVersion、要求と結果のstateはローカル`.agentcore-s08-state.json`に保存され、Gitへ追加しません。Runtime roleにはS3権限を付与しません。AWS APIのretryは無効で、曖昧な応答・失敗時に状態を記録して停止します。ZIPはRuntime/endpointがREADYになった後もS10 cleanupとの照合まで保持し、作業用依存build directoryだけを確認後に削除します。

## L02: 同じ質問でV1とV2を比べる

1. setupで作成したRuntimeの保存stateを確認し、Runtime V1が`READY`であることをread-onlyで確認します。
2. setupで作成済みの`s08_training` endpointをread-onlyで確認し、`READY` / `liveVersion=1`であることを確認します。再作成しません。
3. 同じCLI runでL02を実行します。V1への実呼出し・回答表示の後、8秒の確認時間を経てRuntimeを更新し、endpointをV2へ移して同じ質問をV2へ送ります。実行中に前面化やキー入力は不要です。

```powershell
uv run --project sections/s02 python sections/s08/run_exercise.py --exercise compare --expected-account $expectedAccount --runtime-arn <S08_RUNTIME_ARN>
```

`update_agent_runtime`の前にappは現在のRuntime設定を読み、artifact、role、network、protocol、lifecycle、tagsなどを維持し、`S08_PROMPT_VARIANT`だけを`candidate`へ変えます。最初の更新でV2以外が返された場合は、追加更新をせず停止します。AgentCoreはRuntime更新ごとに不変versionを作るため、既存V1の中身を書き換えません。

### L03: 新しい版を使い、前の版へ戻す

L03を始める前に、L02の観測結果とL01で決めた正確性・許容応答時間・利用量の条件から、採用版1または2を選びます。正確性の例は、`catalog-api`の合成runbookにある3確認（エラー増加とデプロイ時刻の比較、依存先応答時間と5xxの比較、request IDによる該当ログ確認）を順番に漏らさず挙げ、実システムの原因と断定せず、合成データの限界を伝えることです。

比較前に1回あたりの許容往復時間とtoken利用量の範囲も決めます。安全上の上限（SDK retryなし、model request最大2回、output最大512 tokens）は品質基準ではなく、超過時に停止して調べるための上限です。L02では同じ質問・同じdatasetでV1/V2の値を記録し、結果を見た後で基準を都合よく変えません。正確性を満たし、事前に決めた許容時間・利用量の範囲内なら2を候補にし、誤り・重要な抜け・許容範囲超過ならbaselineである1を候補にします。4回の単一例だけから一般的な性能改善や低コストを断言せず、必要なら追加評価を計画します。以下ではL01/L02の判断を確定した後、`$adoptVersion`を`1`または`2`へ設定します。

L03は両版の実応答と切戻しを確認した後、事前選択した版へendpointを戻す演習です。V1を選ぶことはbaselineへ戻す判断、V2を選ぶことはcandidateを次のSectionで使う判断です。Runtime更新後の`DEFAULT`はV2のままで、S08専用endpointだけが選択版になります。L03中に追加の問題が見つかった場合は自動再実行せず、statusと原因を確認して版選択を再評価します。

```powershell
$adoptVersion = '1' # L01/L02で決めた値: '1' または '2'
uv run --project sections/s02 python sections/s08/run_exercise.py --exercise rollback --expected-account $expectedAccount --runtime-arn <S08_RUNTIME_ARN> --adopt-version $adoptVersion
```

このrunではprovider処理の待ち、`READY`/`liveVersion`の確認、再実行と回答の読取りを行います。Runtime/endpointの状態pollは最大255秒、最後のAPI読取は最大45秒なので1 wait全体は最大300秒です。管理API/STSはconnect 5秒/read 45秒、retryをせず、timeoutを超えた後に同じ操作を自動再送しません。CLIは状態と実経過秒数を表示します。受講者が回答を比較する値は、回答本文、測定した往復時間、input/output/total token、実際の`model_requests`、SDK retry回数です。回答差があるだけで新しい版を良いと判定しません。

AWSのRuntime updateは新しい不変versionを作り、`DEFAULT`を最新versionへ自動で移します。ここでは管理`DEFAULT`ではなく、明示的に版を指すS08専用の`s08_training` endpointだけをV1/V2間で切替えます。L03後の期待状態は`DEFAULT=2`、`s08_training=<L01/L02で選んだimmutable version 1または2>`です。出典: [Runtime versioning and endpoints](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html)、[CreateAgentRuntimeEndpoint API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateAgentRuntimeEndpoint.html)、[UpdateAgentRuntimeEndpoint API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_UpdateAgentRuntimeEndpoint.html)。

## 回数・費用・停止条件

L02がV1/V2の2回、L03がV2/V1の2回で、計画上はRuntime invocation合計4回です。各SDK callは`total_max_attempts=1`、Runtime内は1 invocationあたり最大2 model request、1 request最大256 output tokens、agent全体は最大4,096 tokensで、Strands retry strategyを無効にしています。Runtime process/sessionごとのapplication上限は4 invocationです。これらはproviderの処理時間や課金を完全に止める上限ではありません。read timeout 45秒に達してもprovider処理が終了した証明にはなりません。失敗、SDK retryが0以外、model requestが2を超えた兆候、予想外の処理継続があれば、次の送信を止めてRuntime状態と同じ時間帯のlogを確認します。

AWS Price List APIで確認した東京の現行list rate（Runtime V2: 2026-10-06適用、Nova Lite: 2026-10-01適用）はRuntime vCPU **US$0.1276/vCPU-hour**、memory **US$0.0169/GB-hour**、Nova Lite input **US$0.000072/1K tokens**、output **US$0.000288/1K tokens**です。説明用に4 sessionそれぞれの継続時間を60秒、平均CPU使用量を1 vCPU、ピーク時のmemory使用量を1GBと置きます。RuntimeはCPU使用時間とピークmemory使用量・時間から試算し、純粋なI/O待ちでbackground CPU処理がなければ、その待ち時間分のCPU使用料は加算しません。この仮定でRuntime部分は約**US$0.00963**です。4回の全てで4,096 total token上限と512 output token上限を使い切る仮定ならmodel部分は約**US$0.00162**、Runtime+modelの試算は約**US$0.0113**です。各項目は表示時に丸めていますが、小計と合計は丸める前の値から計算しています。Logs/S3、税、為替、CPUやmemoryの追加消費は別です。これは仮定に基づく試算で、実測額、請求額、硬い上限ではありません。実行後のobserved billingは確認できるまで未確認と記録し、試算と混同しません。出典: [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)、[Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)。

## 後続Sectionへ渡す状態と削除

L03後はV1/V2を保持し、`DEFAULT=2`のままにします。`s08_training`は`--adopt-version`で事前に選んだimmutable version 1または2を指し、S09ではこの判断済みendpoint versionを通常状態として使います。S08のRuntime、endpoint、role、bucket、artifact、log groupはこのSectionでは削除しません。Section s10でexact account/region/Runtime IDを再確認し、選択中endpoint versionとactive sessionを確認・停止した後、S08 Runtime/endpoints、versioned ZIP objectと空になったS08 bucket、S08 log group、S08専用role/policyだけを削除して残存を確認します。S07および共有S02 dataは削除しません。エラー時はstatusとfailure reasonを確認し、別名での再作成や権限拡大を行いません。

## この比較からは分からないこと

結果は1つの合成質問と合成データによる4回の実行例です。この単一例から一般的な品質・性能改善や料金低下、継続利用費を推定できません。version rollbackは以前の設定へ戻す操作であり、既に発生した利用料を取り消しません。
