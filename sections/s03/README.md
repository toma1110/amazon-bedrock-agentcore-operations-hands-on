# s03 同じAgentをAmazon Bedrock AgentCore Runtimeで実行する

このSectionでは、s02の合成runbookを読むStrands AgentをAgentCore Runtimeへ配置し、AWS認証を使うクライアントから呼び出します。応答のinvocation IDをRuntimeのCloudWatch Logsで探し、同一sessionと新しいsessionの挙動を比べます。Runtime session IDは会話単位の実行環境を選ぶ値で、利用者の認証・認可の代わりにはなりません。

## 開始地点と完成地点

- 開始地点: `sections/s02/agent_app.py`と`runbooks.json`。
- 完成地点: `sections/s03/runtime_app.py`、`aws_workflow.py`、このREADME。
- AWS上に作るもの: 新しいS3 bucket/object、専用Runtime execution role、AgentCore Runtime（Python 3.13、東京、platform V2）、Runtime CloudWatch Logs。後続Sectionの利用が終わったら「このrunの後片付け」でstateに記録したresourceだけ削除します。

## 前提と安全

- Python 3.13、uv、AWS CLI v2の認証profileまたは有効なIAM Identity Center sessionを使います。AWS操作のAPI呼び出しは、AgentCore control/runtime APIモデルが含まれる`boto3==1.43.108`を使います（`sections/s02/pyproject.toml`に固定済み）。CLI v2のAgentCore service model有無には依存しません。
- Access key、secret、session tokenをfileやcommand引数に貼り付けません。profileは`--profile`または`AWS_PROFILE`で選択します。
- Runtime作成時、実際のSTS accountと、非表示入力する確認済みaccount IDが一致しなければ何も作成しません。処理中も書き込み直前に同じaccountを確認します。account ID、ARN等を含む`.agentcore-s03-state.json`はローカル専用で、Git管理しないでください。
- regionは東京`ap-northeast-1`です。Runtime execution roleにはこのAgentの`amazon.nova-lite-v1:0`呼び出しとRuntime Logs書込みだけを与えます。Callerには今回のbucket/object、専用role、Runtimeに限定された操作権限と、対象role ARNだけへの`iam:PassRole`が必要です。広いmanaged policyやAdministratorAccessをこの演習のために付けません。
- Runtime inbound認証はAWS IAM（SigV4）です。Caller権限とRuntime execution roleは別主体です。s02の`runbooks.json`だけがtoolのdata sourceで、実際のAWSログやシステムにはアクセスしません。
- Runtime execution roleのCloudWatch Logs権限はAWSのAgentCore実行role例に合わせます。ただし`logs:DescribeLogGroups`はCloudWatch Logs Service Authorization Referenceでresource-level authorization非対応です。Runtime roleにはこのread actionだけ`Resource: "*"`で残し、`aws:RequestedRegion=ap-northeast-1`条件を付けます。これにより東京regionのlog group一覧を参照できますが、特定log groupには絞れません。他のlog write actionとBedrock model actionは対象ARNへscopeします。ログ確認で`AccessDenied`が出た場合は実行を止め、拒否されたAPI、region、Runtime execution roleを確認して管理者に必要な権限を確認します。Resource scopeを`*`へ広げて再実行しません。
- 実際のAWS resource作成は費用を伴います。対象account、region、作成resource、見込み費用とこの節のrun単位cleanupを確認してから実行します。権限不足、衝突、曖昧な状態では停止し、既存resourceを再利用したり権限を広げたりしません。

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

同じAWS profileとregionの認証を使い、repository rootから実行します。各command内の`your-sso-profile`は例示文字列です。実行前に必ず自分の設定済みAWS profile名へ置き換えるか、事前に`AWS_PROFILE`を設定して`--profile your-sso-profile`部分を省略してください。以下の各コマンドは`sections/s02`の固定済みBoto3環境で`aws_workflow.py`を動かします。`--run-tag oct27a`はこの実演専用のRuntime、execution role、bucket、state fileを選びます。以降のstatus/invoke/logs/stopでも同じtagを指定してください。別の実演には別tagを使います。既定のtag `default`は従来のS03 stateとresource namespaceを維持します。`deploy`は新しい専用bucketとexecution roleを作り、ZIPをuploadし、V2 Runtimeを作成してMMDSv2を有効化します。作成対象名が既存と衝突した場合は作成APIが失敗するため、そこで停止します。

```powershell
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile --run-tag oct27a deploy --zip $zip
```

`deploy`はAWS STSのaccountと非表示入力した確認済みaccount IDが一致した場合だけ作成し、最初のwrite前に正確なresource名とZIP hashをローカルstateへ保存します。Runtimeが`READY`でMMDSv2が有効と確認できるまでinvokeしません。L04はpackage build、同じZIPの検査、deploy、read-only statusによる`READY`確認までを連続して記録します。L04ではinvokeしません。後続L05/L06ではこのRuntimeに対して次の3呼び出しを行い、その応答のinvocation IDを使って後からlogsを読みます。

`invoke`コマンドでは同じsession IDで2回、その後新しいsession IDで1回呼び出します。各呼び出しの直前に`event=request`、`step`、`session_id`、`prompt`を表示し、直後に`event=response`、同じ`step`/`session_id`、`result.invocation_id`、`result.trace`を表示します。これにより入力、使用session、応答、CloudWatchで照合するIDを同じ記録で追えます。

CLIは標準出力と標準エラーをUTF-8へ設定してから処理を始めます。出力を別コマンドやツールへパイプする場合は、受け側もUTF-8として読み取る設定にしてください。

まず、保存したRuntime IDに対する読み取り専用のstatus確認を行います。これは正確なRuntime IDだけを`GetAgentRuntime`で読み、実際のversion/status/platform/MMDSv2設定を表示します。

```powershell
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile --run-tag oct27a status
```

```powershell
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile --run-tag oct27a invoke
```

応答の各`invocation_id`はローカルstateへ保存されます。invoke後に`status`をもう一度実行してprovider側の状態を読み直し、その後`logs`で照合します。`logs`はstate内のinvocation IDだけを、今回のRuntimeのlog groupで検索します。一致する`agent_invocation`が見つからない場合は、成功扱いせず「トラブルシューティング」を参照してください。

```powershell
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile --run-tag oct27a status
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile --run-tag oct27a logs
```

`logs`の表示は保存したinvocation ID、時刻、status、tool call数、tool名だけに絞ります。Runtimeアプリ自身の`agent_invocation`ログにはprompt、model応答、tool引数を出しません。一方、AgentCoreが別途出力する標準APPLICATION_LOGSにはrequest payloadが含まれる場合があります。このコマンドはその別eventを表示しません。任意でAWS Consoleを開き同じRuntime log groupを参照しても構いませんが、基本手順はこの再現可能なSDKコマンドです。AgentCoreのlog group命名規則とDEFAULT endpointは[Runtime observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)を参照してください。

request/responseの各ペアでsession IDが対応していることを確認します。最初の2回で同じsession IDが使われ、3回目で新しいsession IDが使われていることを確認したうえで、3つの`result`と`result.trace`を見比べます。同じsessionで会話履歴が続くか、新しいsessionで直前の会話が続かないかを観察します。modelの言い回しだけで合否判定しません。実データや秘密情報をpromptへ含めないでください。

観察後、実演で作ったactive sessionを停止します。

```powershell
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile your-sso-profile --run-tag oct27a stop
```

停止対象はこの演習が記録した2つのsessionだけです。Runtimeやrole、bucket、CloudWatch Logsは後続Sectionのため残し、最終利用後に「このrunの後片付け」で削除します。

## 4. 任意: AWS Consoleでログを深掘りする

基本のtrace/log照合は前節のSDK `logs`コマンドで完了します。この任意のConsole確認は必須ではありません。さらに詳しく見る場合のみ、AWS ConsoleのCloudWatch Logsで対象Runtimeのlog group/streamを開き、SDK出力と同じinvocation IDの`agent_invocation` eventを確認します。このeventにはstatus、tool call数、tool名が含まれます。prompt、model応答、tool引数はこのeventには記録しませんが、別の標準APPLICATION_LOGSにrequest payloadが含まれる場合があるため、そこは混同しないでください。toolの引数とtool resultは応答`trace`で確認します。ログ反映にはprovider側の処理時間がかかることがあります。

`GET /ping`のHealthyはHTTP applicationの起動を示すだけで、Bedrock接続やmodel invokeの成功を示しません。Agentと会話履歴は同じactive session内のprocess stateです。Runtime sessionはidle timeoutや最大有効期間で終了し、同じIDを再利用しても終了済み環境の会話が残る保証はありません。会話履歴はこの短い実演中のプロセス内に限られ、永続Memoryには自動保存されません。

## 費用の目安

2026-10-27に確認したAWS公式pricing pagesの東京list ratesで試算します。AgentCore Runtime V2はvCPU-hour US$0.1276、memory GB-hour US$0.0169（2026-10-06適用）；Bedrock Nova Lite input/outputは1K tokensあたりUS$0.000072/US$0.000288（2026-10-01適用）；CloudWatch Logs Standard ingestion/storageはUS$0.76/GB・US$0.033/GB-month；S3 Standard storageはUS$0.025/GB-monthです。出典: [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)、[Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)、[CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/)、[S3 pricing](https://aws.amazon.com/s3/pricing/)。

次の条件を置いた参考シナリオでは、L04 deployと後続3回のinvokeを合わせて約US$0.032〜0.036（3〜4米セント）です。3回は同じsessionを2回、新しいsessionを1回使うため、計算上は2 sessionsとします。各sessionのactive時間を600秒、active中の平均CPUを0.5 vCPU、memoryを1 GBと仮定します。idle中のCPU消費は0、active終了後は各sessionで最大120秒同じ1 GBが残る条件です。このときRuntime分はCPU約US$0.02127とmemory約US$0.00676、計US$0.02803です。model APIを6〜12件、各input 5K/output 512 tokensと置くと約US$0.00304〜0.00609です。CloudWatch Logsを合計300 KiB、7日保持すると約US$0.00024、約40.2 MBのZIPをS3 Standardに7〜30日置くと約US$0.00023〜0.00101です。memory reclaim/CPU使用量、実際のmodel API request数、処理時間には変動があり、この試算は実測値や請求額の上限ではありません。idle CPUゼロは説明用仮定です。memoryのreclaimは[session idle timeout](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html)とは別で、session idle timeoutの既定値は900秒です。

金額はbytesをdecimal GB（1 GB = 1,000,000,000 bytes）へ換算し、US dollarで丸めています。L04自体にはinvokeを含めず、3回のinvoke費用はL05/L06の説明用条件です。

これらは消費量を置いた説明例で、固定割当、実測値、実際の利用予測、請求額の上限ではありません。Bedrock model API request数はRuntime invocation数やsession数と同じではありません。service API request fee、tax、為替は含みません。実際の期間・usageと現在価格は実行前にAWS Pricing Calculatorで再計算してください。想定を超える場合は実行を止め、人間が範囲と費用を再判断します。

## このrunの後片付け

この節のコマンドは、`--run-tag oct27a`で作成した今回のRuntime一式を、後続Sectionでの最終利用が終わった後に削除します。s10の現在のcleanup allowlistはこのtagを認識しません。`default`や旧S03 Runtime version 16、他Sectionのresourceは対象外です。aws_workflowが作るbucketはversioningを有効化せず、保存するobjectもこのZIP1つだけです。

最初に同じAWS profileと東京regionの`sections/s02` Python環境を使い、次のPowerShell blockでstateのscopeとaccountを確認します。STS account、tag、region、resource namespaceが一致しない場合は停止します。

```powershell
@'
import boto3, json
from pathlib import Path
s = json.loads(Path(r"sections/s03/.agentcore-s03-state-oct27a.json").read_text(encoding="utf-8"))
assert s["run_tag"] == "oct27a" and s["region"] == "ap-northeast-1"
assert s["runtime_name"] == "udemy_s03_oct27a"
assert s["role_name"] == "udemy-s03-oct27a-runtime"
assert s["object_key"] == "s03/oct27a/runtime.zip"
session = boto3.Session(profile_name="your-sso-profile", region_name=s["region"])
assert session.client("sts").get_caller_identity()["Account"] == s["account_id"]
print(json.dumps({k: s.get(k) for k in ("run_tag", "run_id", "account_id", "region", "runtime_id", "runtime_arn", "bucket", "object_key", "role_name")}, indent=2))
'@ | uv run --project sections/s02 python -
```

After reviewing those exact IDs, run the following block. It stops only sessions recorded in this state, verifies the Runtime identity and endpoints, then deletes this Runtime with no version qualifier (all versions). The prompt requires typing the exact Runtime ID.

```powershell
@'
import boto3, json, time
import re
from pathlib import Path
from botocore.exceptions import ClientError
s = json.loads(Path(r"sections/s03/.agentcore-s03-state-oct27a.json").read_text(encoding="utf-8"))
assert s["run_tag"] == "oct27a" and s["region"] == "ap-northeast-1"
assert s["runtime_name"] == "udemy_s03_oct27a" and s["runtime_id"].startswith("udemy_s03_oct27a-")
assert s["role_name"] == "udemy-s03-oct27a-runtime" and s["object_key"] == "s03/oct27a/runtime.zip"
assert s["runtime_arn"] == f"arn:aws:bedrock-agentcore:{s['region']}:{s['account_id']}:runtime/{s['runtime_id']}"
assert s["runtime_create_request"]["roleArn"] == f"arn:aws:iam::{s['account_id']}:role/udemy-s03-oct27a-runtime"
assert re.fullmatch(r"udemy-s03-oct27a-apne1-[0-9a-f]{32}", s["bucket"])
session = boto3.Session(profile_name="your-sso-profile", region_name=s["region"])
assert session.client("sts").get_caller_identity()["Account"] == s["account_id"]
runtime = session.client("bedrock-agentcore")
control = session.client("bedrock-agentcore-control")
for sid in s.get("sessions", []):
    runtime.stop_runtime_session(agentRuntimeArn=s["runtime_arn"], runtimeSessionId=sid)
current = control.get_agent_runtime(agentRuntimeId=s["runtime_id"])
assert current["agentRuntimeId"] == s["runtime_id"]
assert current["agentRuntimeName"] == s["runtime_name"]
assert current["roleArn"] == s["runtime_create_request"]["roleArn"]
endpoints = control.list_agent_runtime_endpoints(agentRuntimeId=s["runtime_id"]).get("runtimeEndpoints", [])
assert all(e["endpointName"] == "DEFAULT" for e in endpoints), "Unexpected endpoint; stop and inspect"
assert input(f"Type {s['runtime_id']} to delete this Runtime and all its versions: ") == s["runtime_id"]
control.delete_agent_runtime(agentRuntimeId=s["runtime_id"])
deadline = time.monotonic() + 20 * 60
while True:
    try:
        control.get_agent_runtime(agentRuntimeId=s["runtime_id"])
        if time.monotonic() >= deadline: raise TimeoutError("Runtime deletion timed out; keep state and inspect")
        time.sleep(10)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException": break
        raise
'@ | uv run --project sections/s02 python -
```

For S3, inspect that the only object is the exact ZIP recorded in state, with matching size/hash. Type the exact bucket name to confirm deletion; the bucket is removed only after its sole object is deleted.

```powershell
@'
import boto3, json
from pathlib import Path
import re
s = json.loads(Path(r"sections/s03/.agentcore-s03-state-oct27a.json").read_text(encoding="utf-8"))
assert s["run_tag"] == "oct27a" and s["region"] == "ap-northeast-1"
assert s["runtime_name"] == "udemy_s03_oct27a" and s["runtime_id"].startswith("udemy_s03_oct27a-")
assert s["role_name"] == "udemy-s03-oct27a-runtime" and s["object_key"] == "s03/oct27a/runtime.zip"
assert re.fullmatch(r"udemy-s03-oct27a-apne1-[0-9a-f]{32}", s["bucket"])
session = boto3.Session(profile_name="your-sso-profile", region_name=s["region"])
assert session.client("sts").get_caller_identity()["Account"] == s["account_id"]
s3 = session.client("s3")
v = s3.get_bucket_versioning(Bucket=s["bucket"])
assert v.get("Status") not in ("Enabled", "Suspended"), "Bucket versioning is unexpected; stop"
objects = s3.list_objects_v2(Bucket=s["bucket"]).get("Contents", [])
assert len(objects) == 1 and objects[0]["Key"] == s["object_key"], "Unexpected bucket contents; stop"
head = s3.head_object(Bucket=s["bucket"], Key=s["object_key"], ExpectedBucketOwner=s["account_id"])
assert head["ContentLength"] == s["zip_size"] and head.get("Metadata", {}).get("sha256") == s["zip_sha256"]
assert input(f"Type {s['bucket']} to delete its recorded ZIP and empty bucket: ") == s["bucket"]
s3.delete_object(Bucket=s["bucket"], Key=s["object_key"])
s3.delete_bucket(Bucket=s["bucket"])
'@ | uv run --project sections/s02 python -
```

The final block deletes only the log group derived from this Runtime ID and the role whose Description/trust policy match the saved run. It stops if extra policies are attached. Verify each resource is absent before deleting the local state file.

```powershell
@'
import boto3, json
import re
from pathlib import Path
from botocore.exceptions import ClientError
s = json.loads(Path(r"sections/s03/.agentcore-s03-state-oct27a.json").read_text(encoding="utf-8"))
assert s["run_tag"] == "oct27a" and s["region"] == "ap-northeast-1"
assert s["runtime_name"] == "udemy_s03_oct27a" and s["runtime_id"].startswith("udemy_s03_oct27a-")
assert s["role_name"] == "udemy-s03-oct27a-runtime" and s["object_key"] == "s03/oct27a/runtime.zip"
assert s["runtime_arn"] == f"arn:aws:bedrock-agentcore:{s['region']}:{s['account_id']}:runtime/{s['runtime_id']}"
assert re.fullmatch(r"udemy-s03-oct27a-apne1-[0-9a-f]{32}", s["bucket"])
session = boto3.Session(profile_name="your-sso-profile", region_name=s["region"])
assert session.client("sts").get_caller_identity()["Account"] == s["account_id"]
logs = session.client("logs")
group = f"/aws/bedrock-agentcore/runtimes/{s['runtime_id']}-DEFAULT"
matches = [x["logGroupName"] for x in logs.describe_log_groups(logGroupNamePrefix=group).get("logGroups", []) if x["logGroupName"] == group]
if matches: logs.delete_log_group(logGroupName=group)
iam = session.client("iam")
role = iam.get_role(RoleName=s["role_name"])["Role"]
assert role["Description"] == s["role_description"]
assert role["AssumeRolePolicyDocument"] == s["role_trust"]
assert not iam.list_attached_role_policies(RoleName=s["role_name"]).get("AttachedPolicies", [])
assert iam.list_role_policies(RoleName=s["role_name"]).get("PolicyNames", []) == ["S03RuntimeExecution"]
assert input(f"Type {s['role_name']} to delete this run's role: ") == s["role_name"]
iam.delete_role_policy(RoleName=s["role_name"], PolicyName="S03RuntimeExecution")
iam.delete_role(RoleName=s["role_name"])
'@ | uv run --project sections/s02 python -
```

Read-only verification and state removal follow. Run the first block; only if all four checks return `absent` should the last command remove this exact local state file.

```powershell
@'
import boto3, json
import re
from pathlib import Path
from botocore.exceptions import ClientError
s = json.loads(Path(r"sections/s03/.agentcore-s03-state-oct27a.json").read_text(encoding="utf-8"))
assert s["run_tag"] == "oct27a" and s["region"] == "ap-northeast-1"
assert s["runtime_name"] == "udemy_s03_oct27a" and s["runtime_id"].startswith("udemy_s03_oct27a-")
assert s["role_name"] == "udemy-s03-oct27a-runtime" and s["object_key"] == "s03/oct27a/runtime.zip"
assert s["runtime_arn"] == f"arn:aws:bedrock-agentcore:{s['region']}:{s['account_id']}:runtime/{s['runtime_id']}"
assert re.fullmatch(r"udemy-s03-oct27a-apne1-[0-9a-f]{32}", s["bucket"])
session = boto3.Session(profile_name="your-sso-profile", region_name=s["region"])
assert session.client("sts").get_caller_identity()["Account"] == s["account_id"]
checks = {}
control = session.client("bedrock-agentcore-control")
try: control.get_agent_runtime(agentRuntimeId=s["runtime_id"])
except ClientError as e:
    if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException": raise
    checks["runtime"] = "absent"
else: raise RuntimeError("Runtime still exists; preserve state and inspect")
s3 = session.client("s3")
try: s3.head_bucket(Bucket=s["bucket"], ExpectedBucketOwner=s["account_id"])
except ClientError as e:
    if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket"): raise
    checks["bucket"] = "absent"
else: raise RuntimeError("Bucket still exists; preserve state and inspect")
group = f"/aws/bedrock-agentcore/runtimes/{s['runtime_id']}-DEFAULT"
logs = session.client("logs")
matches = [x["logGroupName"] for x in logs.describe_log_groups(logGroupNamePrefix=group).get("logGroups", []) if x["logGroupName"] == group]
if matches: raise RuntimeError("Log group still exists; preserve state and inspect")
checks["log_group"] = "absent"
iam = session.client("iam")
try: iam.get_role(RoleName=s["role_name"])
except ClientError as e:
    if e.response.get("Error", {}).get("Code") != "NoSuchEntity": raise
    checks["role"] = "absent"
else: raise RuntimeError("Role still exists; preserve state and inspect")
print(json.dumps(checks, sort_keys=True))
'@ | uv run --project sections/s02 python -
```

```powershell
Remove-Item -LiteralPath 'sections/s03/.agentcore-s03-state-oct27a.json'
```

## トラブルシューティング

- デプロイが中断した場合は`sections/s03/.agentcore-s03-state-oct27a.json`と同じZIPを保管し、別のtagやZIPでやり直しません。再開時は保存したZIP path/hashと一致する同じZIPを使い、account guardと名前衝突を再確認します。
- Bucket作成のstateが`create_requested`で止まった場合、このAWS APIにはclient tokenがないため自動retryしません。stateに記録されたbucket名だけを同じaccount/regionで調べます。今回作成された空bucketだと確認できた場合のみ個別cleanupし、存在しないことが確認できた場合のみstateを削除して再開します。所有者や中身が不明なら停止します。
- IAM roleはstateのDescriptionとtrust policyが完全一致する場合に限り、作成応答喪失後の再開対象にします。相違やAccessDeniedがあれば既存roleや権限を流用せず停止します。
- RuntimeのCreate/Update応答が失われた場合はstateに保存されたrequest/client tokenで同じZIPの`deploy`を再実行します。異なるRuntime、bucket、roleを作らず、read-only statusで`READY`とMMDSv2を確認するまでinvokeしません。
- `logs`は保存済みinvocation IDごとに最大50ページを読みます。空pageに`nextToken`があれば続きを取得し、token拒否/期限切れ、同tokenの反復、上限到達時は安全に停止します。Log groupの作成反映を約120秒待っても一致する`agent_invocation`がない場合は、stateのID、region、Runtime statusを確認して後でread-only `logs`を再実行します。全log groupや他Runtimeのログは検索しません。[FilterLogEvents API](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html)のpagination仕様に従います。

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
