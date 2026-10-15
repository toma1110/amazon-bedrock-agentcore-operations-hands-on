# s03 同じAgentをAmazon Bedrock AgentCore Runtimeで実行する

このSectionでは、s02の合成runbookを読むStrands AgentをAgentCore Runtimeへ配置し、AWS認証を使うクライアントから呼び出します。応答のinvocation IDをRuntimeのCloudWatch Logsで探し、同一sessionと新しいsessionの挙動を比べます。Runtime session IDは会話単位の実行環境を選ぶ値で、利用者の認証・認可の代わりにはなりません。

## 開始地点と完成地点

- 開始地点: `sections/s02/agent_app.py`と`runbooks.json`。
- 完成地点: `sections/s03/runtime_app.py`、`aws_workflow.py`、このREADME。
- AWS上に作るもの: 新しいS3 bucket/object、専用Runtime execution role、AgentCore Runtime（Python 3.13、東京、platform V2）、Runtime CloudWatch Logs。これらは後続Sectionで使うのでs10で削除します。

## 前提と安全

- Python 3.13、uv、AWS CLI v2の認証profileまたは有効なIAM Identity Center sessionを使います。AWS操作のAPI呼び出しは、AgentCore control/runtime APIモデルが含まれる`boto3==1.43.108`を使います（`sections/s02/pyproject.toml`に固定済み）。CLI v2のAgentCore service model有無には依存しません。
- Access key、secret、session tokenをfileやcommand引数に貼り付けません。profileは`--profile`または`AWS_PROFILE`で選択します。
- Runtime作成時、実際のSTS accountと、非表示入力する確認済みaccount IDが一致しなければ何も作成しません。処理中も書き込み直前に同じaccountを確認します。account ID、ARN等を含む`.agentcore-s03-state.json`はローカル専用で、Git管理しないでください。
- regionは東京`ap-northeast-1`です。Runtime execution roleにはこのAgentの`amazon.nova-lite-v1:0`呼び出しとRuntime Logs書込みだけを与えます。Callerには今回のbucket/object、専用role、Runtimeに限定された操作権限と、対象role ARNだけへの`iam:PassRole`が必要です。広いmanaged policyやAdministratorAccessをこの演習のために付けません。
- Runtime inbound認証はAWS IAM（SigV4）です。Caller権限とRuntime execution roleは別主体です。s02の`runbooks.json`だけがtoolのdata sourceで、実際のAWSログやシステムにはアクセスしません。
- Runtime execution roleのCloudWatch Logs権限はAWSのAgentCore実行role例に合わせます。ただし`logs:DescribeLogGroups`はCloudWatch Logs Service Authorization Referenceでresource-level authorization非対応です。Runtime roleにはこのread actionだけ`Resource: "*"`で残し、`aws:RequestedRegion=ap-northeast-1`条件を付けます。これにより東京regionのlog group一覧を参照できますが、特定log groupには絞れません。他のlog write actionとBedrock model actionは対象ARNへscopeします。ログ確認で`AccessDenied`が出た場合は実行を止め、拒否されたAPI、region、Runtime execution roleを確認して管理者に必要な権限を確認します。Resource scopeを`*`へ広げて再実行しません。
- 実際のAWS resource作成は費用を伴います。対象account、region、作成resource、見込み費用とs10でのcleanupを確認してから実行します。権限不足、衝突、曖昧な状態では停止し、既存resourceを再利用したり権限を広げたりしません。

## 1. ローカルで確認する

Public repository rootから実行します。以下はAWSへ接続しないunit testです。

```powershell
uv run --project sections/s02 pytest sections/s03/tests
```

このtestはRuntime HTTP入力の検査、provider失敗時の応答、health endpoint、応答/log間のinvocation ID一致を確認します。AWS account、Bedrock、Runtimeへの接続成功を示すものではありません。

## 2. ARM64用deployment ZIPを作る

Runtime direct code deploymentにはLinux ARM64用dependency packageが必要です。repository rootのPowerShellから実行します。Windows用`.venv`と`__pycache__`はZIPへ含めません。

```powershell
$work = Join-Path $env:TEMP ("agentcore-s03-" + [guid]::NewGuid().ToString('N'))
$build = Join-Path $work 'package'
$requirements = Join-Path $work 'requirements.txt'
New-Item -ItemType Directory -Path $build | Out-Null
uv export --locked --project sections/s02 --no-dev --format requirements-txt --output-file $requirements
uv pip install --python-platform aarch64-manylinux2014 --python-version 3.13 --target $build --only-binary=:all: -r $requirements
Copy-Item sections/s02/agent_app.py $build
Copy-Item sections/s02/runbooks.json $build
Copy-Item sections/s03/runtime_app.py $build
$zip = Join-Path (Join-Path $PWD 'sections/s03') ("s03-runtime-" + [guid]::NewGuid().ToString('N') + '.zip')
Compress-Archive -Path (Join-Path $build '*') -DestinationPath $zip
Get-FileHash $zip -Algorithm SHA256
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::OpenRead($zip).Entries | Select-Object -ExpandProperty FullName
```

ZIP内に`agent_app.py`、`runbooks.json`、`runtime_app.py`と依存packageがあり、`.venv`や`__pycache__`がないことを確認します。ZIPが250 MB以下であることも確認します。毎回新しい作業directoryとZIP名を使い、既存成果物を上書きしません。`$work`はARM64 package build中だけ必要で、`$zip`はdeployのZIP uploadが成功しRuntimeがREADYになるまで保管します。成功を確認したら`Remove-Item -LiteralPath $work -Recurse`と`Remove-Item -LiteralPath $zip`で削除します。失敗時は再実行や削除をせず、次節のstate recovery手順で判断します。生成ZIPはdeploy時の一回upload用で、後続Sectionでは再利用しません。

## 3. SDKで作成してRuntimeを呼び出す

同じAWS profileとregionの認証を使い、repository rootから実行します。各command内の`your-sso-profile`は例示文字列です。実行前に必ず自分の設定済みAWS profile名へ置き換えるか、事前に`AWS_PROFILE`を設定して`--profile your-sso-profile`部分を省略してください。以下の各コマンドは`sections/s02`の固定済みBoto3環境で`aws_workflow.py`を動かします。`deploy`は新しい専用bucketとexecution roleを作り、ZIPをuploadし、V2 Runtimeを作成してMMDSv2を有効化します。作成対象名が既存と衝突した場合は作成APIが失敗するため、そこで停止します。

```powershell
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile deploy --zip $zip
```

`deploy`はAWS STSからaccountを読み、学習用account IDを非表示で尋ねます。一致しなければ書き込みません。最初のAWS write前にbucket/role/Runtimeの正確な名前、ZIP hash、role trust/policy、Runtime作成requestとclient tokenをローカルstateへ保存し、その後の各write前にもSTS account guardを実行します。IAM roleはこのrun固有のDescriptionとtrust policyが一致した場合だけ作成応答喪失後の回復対象にします。Runtimeのcreate/update応答が失われた場合は、stateに保存された同じAPI requestとclient tokenで`deploy`を再実行して回復します。S3 bucket作成はAWS APIにclient tokenがないため、stateが`create_requested`で止まった場合は自動再試行しません。state内のexact bucket nameを確認し、account/regionでその名前だけを調査してください。存在するempty bucketが今回の作成物だと確認できた場合に限ってそのbucketをcleanupし、存在しないことを確認できた場合に限ってstateを削除して新しいdeployを開始します。所有者・内容・状態が曖昧なら停止し、別bucketや既存resourceを流用しません。deploy再実行は保存済ZIP path/hashが一致する必要があります。Runtimeが`READY`で、取得した`metadataConfiguration.requireMMDSV2`が`true`になるまでinvokeしません。

`invoke`コマンドでは同じsession IDで2回、その後新しいsession IDで1回呼び出します。各呼び出しの直前に`event=request`、`step`、`session_id`、`prompt`を表示し、直後に`event=response`、同じ`step`/`session_id`、`result.invocation_id`、`result.trace`を表示します。これにより入力、使用session、応答、CloudWatchで照合するIDを同じ記録で追えます。

CLIは標準出力と標準エラーをUTF-8へ設定してから処理を始めます。出力を別コマンドやツールへパイプする場合は、受け側もUTF-8として読み取る設定にしてください。

まず、保存したRuntime IDに対する読み取り専用のstatus確認を行います。これは正確なRuntime IDだけを`GetAgentRuntime`で読み、実際のversion/status/platform/MMDSv2設定を表示します。

```powershell
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile status
```

```powershell
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile invoke
```

応答の各`invocation_id`はローカルstateへ保存されます。invoke後に`status`をもう一度実行してprovider側の状態を読み直し、その後`logs`で照合します。`logs`は保存済みIDだけを使い、Runtimeの正確なlog group `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`に対して`FilterLogEvents`を呼びます。空pageでも`nextToken`があれば続けて読み、各検索巡回でIDごとに最大50ページまで確認します。tokenが拒否または期限切れ、同じtokenの反復、ページ上限の場合は安全に停止するため、後で`logs`を再実行してください。[FilterLogEvents API](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html)のpagination仕様に従います。Log groupがまだ作成・反映されていない場合は、約120秒を目安に読み取り専用で再確認します。ただし、一巡のページ確認やAPI応答待ちで超過する場合があります。それでも一致する`agent_invocation`が見つからなければ失敗として扱います。全log groupの一覧や他のRuntimeのログは検索しません。

```powershell
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile status
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile logs
```

`logs`の表示は保存したinvocation ID、時刻、status、tool call数、tool名だけに絞ります。Runtimeアプリ自身の`agent_invocation`ログにはprompt、model応答、tool引数を出しません。一方、AgentCoreが別途出力する標準APPLICATION_LOGSにはrequest payloadが含まれる場合があります。このコマンドはその別eventを表示しません。任意でAWS Consoleを開き同じRuntime log groupを参照しても構いませんが、基本手順はこの再現可能なSDKコマンドです。AgentCoreのlog group命名規則とDEFAULT endpointは[Runtime observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)を参照してください。

request/responseの各ペアでsession IDが対応していることを確認します。最初の2回で同じsession IDが使われ、3回目で新しいsession IDが使われていることを確認したうえで、3つの`result`と`result.trace`を見比べます。同じsessionで会話履歴が続くか、新しいsessionで直前の会話が続かないかを観察します。modelの言い回しだけで合否判定しません。実データや秘密情報をpromptへ含めないでください。

観察後、実演で作ったactive sessionを停止します。

```powershell
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile stop
```

停止対象はこの演習が記録した2つのsessionだけです。Runtimeやrole、bucket、CloudWatch Logsは後続Sectionのため残し、s10で削除します。

## 4. 任意: AWS Consoleでログを深掘りする

基本のtrace/log照合は前節のSDK `logs`コマンドで完了します。この任意のConsole確認は必須ではありません。さらに詳しく見る場合のみ、AWS ConsoleのCloudWatch Logsで対象Runtimeのlog group/streamを開き、SDK出力と同じinvocation IDの`agent_invocation` eventを確認します。このeventにはstatus、tool call数、tool名が含まれます。prompt、model応答、tool引数はこのeventには記録しませんが、別の標準APPLICATION_LOGSにrequest payloadが含まれる場合があるため、そこは混同しないでください。toolの引数とtool resultは応答`trace`で確認します。ログ反映にはprovider側の処理時間がかかることがあります。

`GET /ping`のHealthyはHTTP applicationの起動を示すだけで、Bedrock接続やmodel invokeの成功を示しません。Agentと会話履歴は同じactive session内のprocess stateです。Runtime sessionはidle timeoutや最大有効期間で終了し、同じIDを再利用しても終了済み環境の会話が残る保証はありません。会話履歴はこの短い実演中のプロセス内に限られ、永続Memoryには自動保存されません。

## 費用の目安

Runtime V2は実測CPU secondsとpeak memory per secondで費用が変わります。東京のlist rate（2026-10-06適用）はvCPU-hour US$0.1276、memory GB-hour US$0.0169です。Bedrock Nova Lite東京のinput/output料金（2026-10-01適用）は1K tokensあたりUS$0.000072/US$0.000288です。CloudWatch Logs Standard ingestionはUS$0.76/GB、S3 Standard Tokyo storageはUS$0.025/GB-monthです。出典: [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)、[Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)、[CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/)、[S3 pricing](https://aws.amazon.com/s3/pricing/)。

条件を置いた試算例は約US$0.03〜0.05です（単位換算後に丸めた概算）。この試算例は、利用者1人、異なるRuntime session 3つ、Bedrock model API request 6〜12件（1件あたりinput 5K/output 512 tokens）、active 810〜1,530秒とsession idle 1,800秒、CPU 0.5 vCPU/activeと0.02 vCPU/idle、memory 1 GB、合計300 KiB logsを7日保持、ZIP 29,306,783 bytesを7〜30日保持する条件を置いています。

この試算例の条件は、下記の`invoke`コマンドで受講者が行う呼出し回数とは異なります。CLI手順ではInvokeAgentRuntimeを3回呼び出し、同じRuntime session IDを2回使ってから新しいIDを1回使うため、異なるRuntime sessionは2つです。1回のRuntime呼び出しでエージェントからモデルへの呼び出し（agent turn）は最大4回、3回のRuntime呼び出しでは最大12回です。Bedrock model API request数はRuntime invocation数やsession数と同じではありません。

これは記載した条件に基づく参考値で、実測値や請求額の上限ではありません。service API request fee、tax、為替は含みません。実際の期間・usageと現在価格は実行前にAWS Pricing Calculatorで再計算してください。想定を超える場合は実行を止め、人間が範囲と費用を再判断します。

## s10での後片付け

Runtimeとactive sessionの状態を確認してRuntimeを削除します。その後、専用bucketの`s03/runtime.zip`だけを削除し、bucketに他objectがない場合のみbucketを削除します。対象RuntimeのCloudWatch log groupと専用execution role（inline policyを含む）を削除します。Runtime・object・log group・roleが残っていないことを個別に確認します。他のCourse resourceやobjectを削除しません。Section 2で生成した`$work`と`$zip`が残っている場合は、このcleanup時に削除します。ローカルの`.agentcore-s03-state.json`はAWS resource cleanupと照合後に削除できます。

## 想定と異なる場合

- `AccessDenied`: Caller側とRuntime execution role側で主体・操作・resource scopeを個別に確認します。Administrator権限を追加しません。
- `CREATE_FAILED`またはreadiness timeout: status/failure reason、ZIP内entrypoint、Python 3.13、ARM64 dependency、role trust/policy、S3 object位置を確認します。別account/regionで作り直しません。
- prompt validation error: JSON objectの`prompt`が文字列で1〜2,000文字かを確認します。
- 応答またはlogが見つからない: invocation ID、対象Runtime、region、session、READY statusを照合し、同じrunの出力とlogが結び付くまで成功扱いしません。

## AWS公式資料

- [Python direct code deploymentとARM64 package](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-code-deploy-python.html)
- [RuntimeのPython runtime support](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-code-deploy-supported-runtimes.html)
- [Runtime service contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-service-contract.html)
- [Runtime invoke API、payload、session ID](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-invoke-agent.html)
- [Runtime execution roleとcaller permission](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
- [CloudWatch Logs IAM resource support](https://docs.aws.amazon.com/service-authorization/latest/reference/list_logs.html)
- [Runtime MMDSv2](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-troubleshooting.html)
- [CreateAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateAgentRuntime.html)
- [UpdateAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_UpdateAgentRuntime.html)
- [GetAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_GetAgentRuntime.html)
- [Runtime versioningとDEFAULT endpoint](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html)
- [StopRuntimeSession API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_StopRuntimeSession.html)
