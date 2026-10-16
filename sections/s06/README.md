# S06：Agent の処理を trace・log・metric でたどる

## この演習で学ぶこと

「処理が遅い／失敗したとき、どこで何が起きたか」を、同じ request ID と trace ID を使って調べます。S03/S04 で作った読み取り専用 AgentCore Runtime と Gateway を再利用し、S04 の合成 `catalog-api` データだけを読みます。顧客データや本番障害は扱わず、Agent に AWS resource の変更を依頼しません。

Runtime request を親 span、その中で動く Strands の model/tool span を子 span として記録します。Runtime の `agent_invocation` JSON log にある trace ID を使って span を絞り、親子関係と所要時間を確認します。また、request ごとの latency を一つの CloudWatch metric 時系列に記録します。

この構成は AgentCore Observability のネイティブ destination/dashboard でも、CloudWatch Transaction Search でもありません。アカウント全体の Transaction Search や共有 `aws/spans` destination は有効にしません。span は Runtime の通常の CloudWatch log group に JSON log として出力します。metric は Runtime から直接送る CloudWatch custom metric です。

## 前提

- S03 Runtime と S04 Gateway/合成データが自分の AWS account の東京 region (`ap-northeast-1`) にあり、S03/S04 の演習が完了していること。準備手順は [S03](../s03/README.md) と [S04](../s04/README.md) を参照してください。
- Python 3.13、uv、`sections/s02/pyproject.toml` で固定された Boto3 が使えること。AgentCore Control/Runtime API が手元の AWS CLI にない場合も、以降の Boto3 SDK コマンドを使います。
- 自分の AWS profile がすでに利用可能であること。既存の認証設定を使います。この演習ではログイン、SSO session、permission set、credential を作成・更新しません。認証が失効・不足している場合は止め、既存の管理手順に従ってください。
- Caller には対象 Runtime の読み取り・更新、対象 S3 object の書き込み、対象 Runtime role への一時 inline policy 追加・削除、Runtime invoke、対象 log group の読み取り、CloudWatch metric 読み取りが必要です。不足時に権限を広げず、管理者へ必要な範囲を確認します。

## 1. 自分の対象を確認し、変更前状態を保存する

Repository root の PowerShell で、自分の値を設定します。profile 名の例は placeholder です。account ID、Runtime/Gateway ID、ARN、bucket、version を教材や別の人の設定からコピーしないでください。

```powershell
$Profile = "your-own-profile"
$Region = "ap-northeast-1"
$ExpectedAccount = "your-12-digit-account-id"
$RuntimeId = "your-s03-runtime-id"
$GatewayId = "your-s04-gateway-id"
```

次はAWSに読み取り専用の確認を行います。表示されたaccount IDが自分で指定した値と一致し、東京regionであることを確認してください。account違いなら止めます。

```powershell
aws sts get-caller-identity --profile $Profile
aws configure get region --profile $Profile
uv run --project sections/s02 python sections/s03/aws_workflow.py --profile $Profile status
uv run --project sections/s02 python sections/s04/aws_workflow.py --profile $Profile status
```

S03/S04 のstatusで示されたRuntime ID/Gateway IDが設定値と一致し、どちらも `READY` であることを確認します。Runtime と Gateway は別 resource です。Runtime の実行 role、S04 Gateway URL/tool名、現在の Runtime version、DEFAULT endpoint の実行中versionは、AWS Consoleの該当する resource 詳細または以下の Boto3 確認結果で照合します。

変更前の状態を自分だけが読める一時ディレクトリに保存します。出力にはAWS resource情報とRuntime環境変数の値が含まれるため、共有・公開・Git追加しないでください。環境変数にsecret/token/password/key等がある、状態がS04設定と一致しない、DEFAULT endpointが `READY` でない、または現行設定を復元できない場合は、ここで停止します。

```powershell
$env:AWS_PROFILE = $Profile
$env:S06_REGION = $Region
$env:S06_EXPECTED_ACCOUNT = $ExpectedAccount
$env:S06_RUNTIME_ID = $RuntimeId
$env:S06_GATEWAY_ID = $GatewayId
$env:S06_BACKUP = Join-Path $env:TEMP ("s06-before-" + [guid]::NewGuid().ToString('N') + ".json")
@'
import boto3, json, os, pathlib, re

region = os.environ["S06_REGION"]
runtime_id = os.environ["S06_RUNTIME_ID"]
session = boto3.Session(region_name=region)
sts = session.client("sts")
identity = sts.get_caller_identity()
if identity["Account"] != os.environ["S06_EXPECTED_ACCOUNT"]:
    raise SystemExit("Account mismatch; no changes made")
control = session.client("bedrock-agentcore-control")
endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
if endpoint.get("status") != "READY" or not endpoint.get("liveVersion"):
    raise SystemExit("DEFAULT endpoint is not READY; stop")
runtime = control.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=endpoint.get("liveVersion", ""))
env = runtime.get("environmentVariables") or {}
if runtime.get("status") != "READY" or endpoint.get("status") != "READY":
    raise SystemExit("Runtime or DEFAULT endpoint is not READY; stop")
if endpoint.get("liveVersion") != runtime.get("agentRuntimeVersion"):
    raise SystemExit("Runtime version and DEFAULT liveVersion differ; stop and inspect")
runtime_arn = runtime.get("agentRuntimeArn", "")
expected_runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{identity['Account']}:runtime/{runtime_id}"
if runtime_arn != expected_runtime_arn:
    raise SystemExit("Runtime ARN does not match the verified account, region, and Runtime ID; stop")
if not env.get("S04_GATEWAY_URL") or not env.get("S04_GATEWAY_TOOL_NAME"):
    raise SystemExit("The expected S04 Gateway values are missing; stop")
gateway = control.get_gateway(gatewayIdentifier=os.environ["S06_GATEWAY_ID"])
gateway_url = (gateway.get("gatewayUrl") or gateway.get("endpoint") or "").rstrip("/")
runtime_gateway_url = env["S04_GATEWAY_URL"].rstrip("/")
if gateway.get("gatewayId") != os.environ["S06_GATEWAY_ID"] or gateway.get("status") != "READY":
    raise SystemExit("The selected S04 Gateway is not the exact READY Gateway; stop")
if runtime_gateway_url not in {gateway_url, gateway_url + "/mcp"}:
    raise SystemExit("Runtime S04_GATEWAY_URL does not match the selected Gateway; stop")
artifact_bucket = runtime["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["bucket"]
s3 = session.client("s3")
versioning = s3.get_bucket_versioning(Bucket=artifact_bucket, ExpectedBucketOwner=identity["Account"])
if versioning.get("Status") is not None:
    raise SystemExit("Artifact bucket has versioning enabled or suspended; stop before any write")
role_arn = runtime.get("roleArn", "")
role_name = role_arn.rsplit("/", 1)[-1]
iam = session.client("iam")
role = iam.get_role(RoleName=role_name)["Role"]
inline_names = iam.list_role_policies(RoleName=role_name).get("PolicyNames", [])
inline_policies = {name: iam.get_role_policy(RoleName=role_name, PolicyName=name)["PolicyDocument"]
                   for name in inline_names}
attached_policies = iam.list_attached_role_policies(RoleName=role_name).get("AttachedPolicies", [])
if any(re.search(r"secret|token|password|private.?key|access.?key", key, re.I) for key in env):
    raise SystemExit("A secret-like Runtime environment key exists; do not print or save it")
snapshot = {
    "account_id": identity["Account"], "region": region,
    "runtime_id": runtime_id, "runtime_arn": runtime_arn,
    "gateway_id": os.environ["S06_GATEWAY_ID"],
    "runtime": {key: runtime[key] for key in (
        "agentRuntimeArtifact", "roleArn", "platformVersion", "networkConfiguration",
        "protocolConfiguration", "environmentVariables", "metadataConfiguration",
        "lifecycleConfiguration") if key in runtime},
    "runtime_role": {"role_name": role_name,
                      "assume_role_policy": role.get("AssumeRolePolicyDocument"),
                      "permissions_boundary": role.get("PermissionsBoundary"),
                      "inline_policies": inline_policies,
                      "attached_policies": attached_policies},
    "endpoint": {key: endpoint[key] for key in (
        "name", "status", "liveVersion", "targetVersion", "agentRuntimeEndpointArn") if key in endpoint},
    "baseline_version": endpoint["liveVersion"],
    "artifact_bucket_versioning": None,
}
path = pathlib.Path(os.environ["S06_BACKUP"])
path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
print(json.dumps({"account_id": identity["Account"], "region": region,
                  "runtime_id": runtime_id, "runtime_arn": runtime_arn,
                  "runtime_status": runtime["status"],
                  "baseline_version": endpoint["liveVersion"], "gateway_id": snapshot["gateway_id"],
                  "gateway_url_present": True, "gateway_tool": env["S04_GATEWAY_TOOL_NAME"],
                  "backup_file": str(path)}, indent=2))
'@ | uv run --project sections/s02 python -
```

AWS account / region / resource IDs / role / Runtime version / endpoint mapping / S04 Gateway URLとtool名 / environment / lifecycle / network / protocol / artifact locationを記録しました。`$env:S06_BACKUP` を控え、fileを安全な場所に保持します。S04のGateway IDとRuntime環境変数のURLが同じGatewayを指していることもConsoleで照合してください。違う、または他のCourse用resourceだった場合は変更しません。

## 2. packageを作る

初めてまたはコードを変更した場合はlocked dependencyからpackageを作成します。ZIPは新しい一意な作業directoryに出し、既存ZIPやS3 objectを上書きしません。

```powershell
$BuildDir = Join-Path $PWD ("sections/s06/build/learner-" + [guid]::NewGuid().ToString('N'))
uv lock --locked --project sections/s02
uv run --project sections/s02 python sections/s06/package_artifacts.py --output $BuildDir
$ZipPath = Join-Path $BuildDir "s06-runtime-arm64.zip"
Get-Item $ZipPath | Select-Object FullName,Length
Get-FileHash $ZipPath -Algorithm SHA256
```

SHA-256、size、package pathを変更前の保存記録に加えます。`sections/s06/build/` はGit対象外です。S3 keyは既存S03/S04 artifactを避け、SHA-256と一意なrun IDを含む新しいkeyにします。AWS操作の結果が曖昧な場合はZIPやkeyを変えて再実行せず、現在状態を確認します。

## 3. S06を反映しDEFAULT endpointを更新する

この段階で費用の発生するAWS変更が始まります。2時間の単一Runtime session、最大3 Runtime request、最大6 model request、3 Gateway call、log 1 MiB、Logs Insights scan 10 MiB、1 metric series、US$2の手動stop/reassessしきい値を確認します。これはAWSのbilling capではありません。実行前にTokyoの料金と既存利用を見直し、$2を超える可能性、曖昧な状態、違うaccount/resourceなら止めます。

変更前のsnapshotを読み、account/region/runtime/endpoint/baseline versionが一致すること、S06 policyがまだないこと、package/S3 keyが一意であることを再確認します。続く一つのBoto3 commandはIAM policyを追加し、S06 ZIPを新規S3 keyへuploadし、Runtime versionを更新してDEFAULT endpointをcandidateへ進めます。AWS書き込みは一度だけです。errorや接続断など応答が曖昧なら再実行せず、read-onlyで各状態を照合して止まります。

PowerShellで変数を設定します。`$ArtifactBucket` は保存snapshotの `runtime.agentRuntimeArtifact.codeConfiguration.code.s3.bucket` にある自分の値です。S3 keyはrunごとに新規UUIDを使います。

```powershell
$ZipPath = (Resolve-Path $ZipPath).Path
$ArtifactBucket = "your-existing-s03-s04-artifact-bucket"
$CandidateSha256 = (Get-FileHash $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
$CandidateKey = "s06/$([guid]::NewGuid().ToString('N'))/$CandidateSha256/s06-runtime-arm64.zip"
$env:S06_ZIP_PATH = $ZipPath
$env:S06_ARTIFACT_BUCKET = $ArtifactBucket
$env:S06_CANDIDATE_KEY = $CandidateKey
```

次のcommandは書き込みを行うため、AWS対象をもう一度確認した後、一度だけ実行します。policy名が既にあれば停止します。Runtime update・endpoint update・uploadのいずれかが不明瞭に終わった場合は再実行しません。

```powershell
@'
import boto3, copy, hashlib, json, os, pathlib, time, uuid
from botocore.exceptions import ClientError

region, account = os.environ["S06_REGION"], os.environ["S06_EXPECTED_ACCOUNT"]
runtime_id, gateway_id = os.environ["S06_RUNTIME_ID"], os.environ["S06_GATEWAY_ID"]
backup_path = pathlib.Path(os.environ["S06_BACKUP"])
snapshot = json.loads(backup_path.read_text(encoding="utf-8"))
session = boto3.Session(region_name=region)
sts, control = session.client("sts"), session.client("bedrock-agentcore-control")
s3, iam = session.client("s3"), session.client("iam")
if sts.get_caller_identity()["Account"] != account or region != "ap-northeast-1":
    raise SystemExit("Account/region mismatch; no changes made")
if snapshot.get("account_id") != account or snapshot.get("runtime_id") != runtime_id or snapshot.get("gateway_id") != gateway_id:
    raise SystemExit("Backup target mismatch; no changes made")
baseline = snapshot["runtime"]
endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
if endpoint.get("status") != "READY" or endpoint.get("liveVersion") != snapshot.get("baseline_version"):
    raise SystemExit("DEFAULT changed since backup; no changes made")
live = control.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=snapshot["baseline_version"])
for key in ("agentRuntimeArtifact", "roleArn", "platformVersion", "networkConfiguration",
            "protocolConfiguration", "environmentVariables", "metadataConfiguration", "lifecycleConfiguration"):
    if live.get(key) != baseline.get(key):
        raise SystemExit(f"Runtime config changed since backup ({key}); no changes made")
if live.get("status") != "READY":
    raise SystemExit("Baseline Runtime is not READY; no changes made")
env = dict(baseline.get("environmentVariables") or {})
if env.get("S06_TRACE_ENABLED") or env.get("S06_METRICS_ENABLED"):
    raise SystemExit("S06 opt-in already exists; no changes made")
gateway = control.get_gateway(gatewayIdentifier=gateway_id)
if gateway.get("status") != "READY":
    raise SystemExit("Selected S04 Gateway is not READY; no changes made")
expected_url = (gateway.get("gatewayUrl") or gateway.get("endpoint") or "").rstrip("/")
if env.get("S04_GATEWAY_URL", "").rstrip("/") not in {expected_url, expected_url + "/mcp"}:
    raise SystemExit("Runtime points to a different S04 Gateway; no changes made")
role_arn = baseline["roleArn"]
role_name = role_arn.rsplit("/", 1)[-1]
policy_name = "S06CourseMetrics"
try:
    iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
except ClientError as exc:
    if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
        raise
else:
    raise SystemExit("S06CourseMetrics already exists; do not overwrite it")
zip_path = pathlib.Path(os.environ["S06_ZIP_PATH"])
digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
if digest != os.environ["S06_CANDIDATE_KEY"].split("/")[2]:
    raise SystemExit("ZIP SHA-256 and unique S3 key differ; no changes made")
bucket, key = os.environ["S06_ARTIFACT_BUCKET"], os.environ["S06_CANDIDATE_KEY"]
artifact_bucket = baseline["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["bucket"]
if bucket != artifact_bucket:
    raise SystemExit("Bucket differs from the saved Runtime artifact bucket; no changes made")
versioning = s3.get_bucket_versioning(Bucket=bucket, ExpectedBucketOwner=account)
if versioning.get("Status") is not None or snapshot.get("artifact_bucket_versioning") is not None:
    raise SystemExit("Artifact bucket is not confirmed unversioned; no changes made")
try:
    s3.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
except ClientError as exc:
    if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
        raise
else:
    raise SystemExit("Candidate object key already exists; no overwrite or adoption")

# Save the exact intended candidate identifiers before the first AWS write.
snapshot.update({"candidate_bucket": bucket, "candidate_key": key,
                 "candidate_sha256": digest, "candidate_size": zip_path.stat().st_size,
                 "metric_policy_name": policy_name, "phase": "policy_write_requested"})
backup_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
    "Action": "cloudwatch:PutMetricData", "Resource": "*",
    "Condition": {"StringEquals": {"cloudwatch:namespace": "Course/S06",
                                    "aws:RequestedRegion": region}}}]}
iam.put_role_policy(RoleName=role_name, PolicyName=policy_name, PolicyDocument=json.dumps(policy))
snapshot["phase"] = "policy_created"
backup_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
snapshot["phase"] = "s3_write_requested"
backup_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
with zip_path.open("rb") as body:
    s3.put_object(Bucket=bucket, Key=key, Body=body, ExpectedBucketOwner=account,
                  Metadata={"sha256": digest})
snapshot["phase"] = "s3_uploaded"
backup_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
uploaded = s3.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
if uploaded.get("ContentLength") != zip_path.stat().st_size or uploaded.get("Metadata", {}).get("sha256") != digest:
    raise SystemExit("Uploaded ZIP does not match; stop without Runtime update")
artifact = copy.deepcopy(baseline["agentRuntimeArtifact"])
artifact["codeConfiguration"]["code"]["s3"] = {"bucket": bucket, "prefix": key}
env.update({"S06_TRACE_ENABLED": "true", "S06_METRICS_ENABLED": "true"})
request = {"agentRuntimeId": runtime_id, "agentRuntimeArtifact": artifact,
           "roleArn": role_arn, "platformVersion": baseline["platformVersion"],
           "networkConfiguration": baseline["networkConfiguration"],
           "protocolConfiguration": baseline["protocolConfiguration"],
           "environmentVariables": env,
           "clientToken": str(uuid.uuid4())}
for optional in ("metadataConfiguration", "lifecycleConfiguration"):
    if optional in baseline:
        request[optional] = baseline[optional]
snapshot["phase"] = "runtime_update_requested"
backup_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
updated = control.update_agent_runtime(**request)
version = updated["agentRuntimeVersion"]
snapshot.update({"candidate_version": version, "phase": "runtime_update_submitted"})
backup_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
deadline = time.monotonic() + 1200
while time.monotonic() < deadline:
    candidate = control.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=version)
    if candidate.get("status") == "READY":
        break
    if candidate.get("status") not in {"CREATING", "UPDATING"}:
        raise SystemExit("Candidate Runtime failed; stop and inspect without retrying")
    time.sleep(10)
else:
    raise SystemExit("Candidate Runtime readiness timed out; stop and inspect")
endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
deadline = time.monotonic() + 1200
while endpoint.get("status") in {"CREATING", "UPDATING"} and time.monotonic() < deadline:
    time.sleep(10)
    endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
if endpoint.get("status") != "READY":
    raise SystemExit("DEFAULT endpoint is not READY; stop and inspect")
if endpoint.get("liveVersion") != version:
    raise SystemExit("DEFAULT did not automatically move to the new latest version; stop and inspect")
snapshot["phase"] = "ready"
backup_path.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
print(json.dumps({"runtime_id": runtime_id, "baseline_version": snapshot["baseline_version"],
                  "candidate_version": version, "endpoint_live_version": endpoint["liveVersion"],
                  "status": endpoint["status"], "s3_bucket": bucket, "s3_key": key,
                  "size": zip_path.stat().st_size, "sha256": digest,
                  "backup_file": str(backup_path)}, indent=2))
'@ | uv run --project sections/s02 python -
```

### デプロイが途中で止まった場合

上のcommandは、書込み前・各書込み完了後にsnapshotの `phase` を更新します。エラー、timeout、接続断、size/hash不一致があったら、最初にAWS操作を止めてください。IAM policy・S3 object・Runtime updateを再送しないでください。特に `runtime_update_requested` はAPI応答を受け取る前にRuntime versionが作成された可能性を示すため、そのまま候補ZIPやpolicyを削除してはいけません。

まず次のread-only確認を一度実行します。実行時にAWS resourceは変更しません。Account/region/Runtime ARNがsnapshotと合わない、readが失敗する、versions一覧を最後まで取得できない場合はそこで止めます。

```powershell
@'
import boto3, json, os, pathlib
from botocore.exceptions import ClientError

snapshot = json.loads(pathlib.Path(os.environ["S06_BACKUP"]).read_text(encoding="utf-8"))
session = boto3.Session(region_name=snapshot["region"])
sts, control = session.client("sts"), session.client("bedrock-agentcore-control")
iam, s3 = session.client("iam"), session.client("s3")
if sts.get_caller_identity()["Account"] != snapshot["account_id"]:
    raise SystemExit("Account mismatch; read-only disposition stopped")
expected_runtime_arn = f"arn:aws:bedrock-agentcore:{snapshot['region']}:{snapshot['account_id']}:runtime/{snapshot['runtime_id']}"
if snapshot.get("runtime_arn") != expected_runtime_arn:
    raise SystemExit("Runtime ARN/ID/account/region mismatch; read-only disposition stopped")
endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=snapshot["runtime_id"], endpointName="DEFAULT")
if endpoint.get("status") not in {"READY", "CREATING", "UPDATING"}:
    raise SystemExit("Unexpected DEFAULT status; stop and ask the course administrator")

all_versions, token = [], None
while True:
    request = {"agentRuntimeId": snapshot["runtime_id"], "maxResults": 100}
    if token:
        request["nextToken"] = token
    page = control.list_agent_runtime_versions(**request)
    all_versions.extend(page.get("agentRuntimes", []))
    token = page.get("nextToken")
    if not token:
        break
key = snapshot.get("candidate_key")
bucket = snapshot.get("candidate_bucket")
references = []
for item in all_versions:
    version = item["agentRuntimeVersion"]
    runtime = control.get_agent_runtime(agentRuntimeId=snapshot["runtime_id"], agentRuntimeVersion=version)
    code = (((runtime.get("agentRuntimeArtifact") or {}).get("codeConfiguration") or {}).get("code") or {}).get("s3") or {}
    if key and code.get("bucket") == bucket and code.get("prefix") == key:
        references.append({"version": version, "status": runtime.get("status"),
                           "s06_trace": (runtime.get("environmentVariables") or {}).get("S06_TRACE_ENABLED"),
                           "s06_metrics": (runtime.get("environmentVariables") or {}).get("S06_METRICS_ENABLED")})

role_name = snapshot.get("runtime_role", {}).get("role_name")
policy_name = snapshot.get("metric_policy_name", "S06CourseMetrics")
try:
    policy = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)["PolicyDocument"]
    expected_policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
        "Action": "cloudwatch:PutMetricData", "Resource": "*",
        "Condition": {"StringEquals": {"cloudwatch:namespace": "Course/S06",
                                        "aws:RequestedRegion": snapshot["region"]}}}]}
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
    policy_state = {"present": True, "matches_expected": canonical(policy) == canonical(expected_policy)}
except ClientError as exc:
    if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
        raise
    policy_state = "absent"

object_state = "not identified"
if bucket and key:
    try:
        obj = s3.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=snapshot["account_id"])
        object_state = {"present": True, "size": obj.get("ContentLength"),
                        "sha256_metadata": obj.get("Metadata", {}).get("sha256"),
                        "matches_expected": obj.get("ContentLength") == snapshot.get("candidate_size")
                            and obj.get("Metadata", {}).get("sha256") == snapshot.get("candidate_sha256")}
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
            raise
        object_state = {"present": False}

print(json.dumps({"phase": snapshot.get("phase"), "baseline_version": snapshot.get("baseline_version"),
                  "candidate_version_saved": snapshot.get("candidate_version"),
                  "candidate_bucket": bucket, "candidate_key": key,
                  "default_status": endpoint.get("status"), "default_live_version": endpoint.get("liveVersion"),
                  "versions_seen": len(all_versions), "latest_version_seen": max(
                      (v["agentRuntimeVersion"] for v in all_versions), key=int, default=None),
                  "candidate_artifact_references": references,
                  "metric_policy": policy_state, "candidate_object": object_state,
                  "session_id_saved": bool(snapshot.get("session_id"))}, indent=2))
'@ | uv run --project sections/s02 python -
```

- If `candidate_version` is saved, the DEFAULT version must equal it and the read-only result must show that exact READY version references this run's bucket/key and S06 flags. Continue to the normal cleanup only after verifying the candidate's complete runtime configuration against the saved baseline plus the S06 artifact/flags. Normal cleanup never deletes the active version; it first creates a new restore version from the saved V4 artifact/config, waits until DEFAULT serves it, and only then considers the exact S06 policy/object.
- If no candidate version is saved and phase is `policy_created` or `s3_uploaded`, partial cleanup is permitted only when all of these hold: DEFAULT is READY at the saved baseline version; latest listed version is still that baseline; no version references the candidate S3 key; `metric_policy.matches_expected=true` if the policy is present; `candidate_object.matches_expected=true` if the object is present; `session_id_saved=false`; and no invoke was run. Then run the Step 6 cleanup command below once; its no-candidate branch repeats these checks and removes only the exact `S06CourseMetrics` policy and candidate S3 object. Re-read both to confirm absence. If any check differs, do not delete anything; retain the files/resources and ask the course administrator to inspect them.
- If phase is `policy_write_requested` or `s3_write_requested`, the corresponding request may have an uncertain outcome. Do not run cleanup or repeat the write yet. Use the read-only disposition output, wait for any in-flight request to settle, and ask the course administrator if exact absence/presence and version references remain unclear.
- If phase is `runtime_update_requested` with no saved `candidate_version`, or version listing shows a possible candidate that is not yet READY/current, do not run normal or partial cleanup and do not retry `UpdateAgentRuntime`. Preserve the policy and object. Recheck read-only after the Runtime operation has settled. If exactly one READY version references this run's candidate key and S06 flags, record its version in the local snapshot and use normal cleanup only after DEFAULT/configuration match it. If the outcome remains ambiguous, leave resources intact and ask the course administrator; do not infer that a missing version or delayed endpoint means the write failed.
- If the backup phase is missing/unknown, the backup cannot be read, or AWS state does not match one of these cases, stop. Never delete an active candidate, never delete by prefix, and never repeat a write just because its response was lost.

### 非stream方式を使う場合の診断上の注意

`S06_TRACE_ENABLED=true` のRuntimeでは、必要なときだけ `S06_MODEL_STREAMING` に厳密な小文字の `true` または `false` を設定できます。未設定または `true` は従来のstreaming設定を変更せず、`false` は同じ `BedrockModel` の非stream `Converse` 経路を選びます。この設定はS06の限定診断用であり、通常の演習では既定のstreaming動作を使います。`S06_TRACE_ENABLED` が有効で値が不正な場合は、model/clientを作成したりinvokeしたりする前にRuntimeが拒否します。

非stream経路で成功しても、以前のstreaming失敗の原因が確定したことにはならず、streaming経路の再現・修正・検証にもなりません。結果を一般化せず、演習の成功証拠として扱わないでください。どちらの方式でも、既存のtimeout・失敗時停止・同一run内cleanup手順を省略したり緩めたりしません。

## 4. 同じ質問2回と入力検証失敗1回を実行する

CloudWatchに送るpromptは、下記の固定質問を両方の成功requestに同じ文面で使います。対象範囲はS04の合成データに限定します。

> 合成データの catalog-api について、2026-10-15 23:50:00 UTC から 23:53:00 UTC の CloudWatch Logs と metrics を読み、遅い処理と5xxの時刻・観測値を要約してください。sourceから確認できることだけを述べ、これは現在または実顧客の障害ではないことも明記してください。

Python/Boto3で、3 requestを一つのRuntime session ID（36文字以上）に送り、各開始を60秒以上離します。profile/region/runtime ARN/session IDは上の自分の値を使います。次のsnippetは `InvokeAgentRuntime` APIを使い、各 response body、status、service trace ID、Runtime invocation ID、tool traceを表示します。promptは固定した合成データ範囲だけです。`invoke_agent_runtime` はmodel/Gateway実行費用を生じます。失敗した場合、追加実行せず停止してください。

```powershell
$env:AWS_PROFILE = $Profile
$env:AWS_DEFAULT_REGION = $Region
$env:S06_SESSION_ID = "s06-" + [guid]::NewGuid().ToString()
@'
import boto3, json, os, pathlib, time

snapshot = json.loads(pathlib.Path(os.environ["S06_BACKUP"]).read_text(encoding="utf-8"))
region, expected_account = snapshot["region"], snapshot["account_id"]
if os.environ["AWS_DEFAULT_REGION"] != region:
    raise SystemExit("AWS region differs from the saved pre-change snapshot; stop")
session = boto3.Session(region_name=region)
if session.client("sts").get_caller_identity()["Account"] != expected_account:
    raise SystemExit("AWS account differs from the saved pre-change snapshot; stop")
runtime_arn = snapshot["runtime_arn"]
if runtime_arn.rsplit("/", 1)[-1] != snapshot["runtime_id"]:
    raise SystemExit("Saved Runtime ARN and Runtime ID differ; stop")
client = session.client("bedrock-agentcore")
session_id = os.environ["S06_SESSION_ID"]
snapshot["phase"] = "invoke_started"
snapshot["session_id"] = session_id
pathlib.Path(os.environ["S06_BACKUP"]).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
prompt = ("合成データの catalog-api について、2026-10-15 23:50:00 UTC から "
          "23:53:00 UTC の CloudWatch Logs と metrics を読み、遅い処理と5xxの "
          "時刻・観測値を要約してください。sourceから確認できることだけを述べ、"
          "これは現在または実顧客の障害ではないことも明記してください。")

def invoke(payload):
    return client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn, qualifier="DEFAULT", runtimeSessionId=session_id,
        contentType="application/json", accept="application/json",
        payload=json.dumps(payload, ensure_ascii=False).encode("utf-8"))

print("session_id:", session_id)
for step in (1, 2):
    result = invoke({"prompt": prompt})
    body = result["response"].read().decode("utf-8")
    print(json.dumps({"step": step, "statusCode": result.get("statusCode"),
                      "runtimeSessionId": result.get("runtimeSessionId"),
                      "serviceTraceId": result.get("traceId"), "body": json.loads(body)},
                     ensure_ascii=False))
    if step == 1:
        time.sleep(60)

time.sleep(60)
try:
    result = invoke({"request": "catalog-api"})
    print(json.dumps({"step": 3, "statusCode": result.get("statusCode"),
                      "serviceTraceId": result.get("traceId"),
                      "body": result["response"].read().decode("utf-8")}, ensure_ascii=False))
except Exception as exc:
    # Some SDK/service combinations surface the Runtime HTTP 400 as an exception.
    # Do not retry. Find the matching status=400 event in the exact Runtime log group.
    response = getattr(exc, "response", {})
    print(json.dumps({"step": 3, "client_error_type": type(exc).__name__,
                      "error_code": response.get("Error", {}).get("Code"),
                      "http_status": response.get("ResponseMetadata", {}).get("HTTPStatusCode")},
                     ensure_ascii=False))
snapshot["phase"] = "invoke_complete"
pathlib.Path(os.environ["S06_BACKUP"]).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
'@ | uv run --project sections/s02 python -
```

最初の2回はHTTP/application status 200でAgentの回答が返ります。Runtime応答の `trace` には選ばれたGateway toolとその実行結果が含まれます。`read_synthetic_cloudwatch` のtool responseをsource recordとして確認し、source record ID、時刻範囲、各回答の主張を「支持／支持しない／判断できない」に分けます。合成503を実際の障害と表現しないでください。

3回目は必要な `prompt` がないJSON `{"request":"catalog-api"}` をRuntimeへ送り、modelを呼ぶ前の入力検証でHTTP 400になる想定です。これはRuntime入力拒否であり、backend/model/tool障害でもS04合成503でもありません。正常なら Runtime の親request spanは `ERROR`、`agent_invocation` log は `status=400`、`error_type=RequestValidationError`、同じrequestの `trace_id`、`latency_ms`、`model_request_attempts=0`、tool call数0を示します。3件目にAgent/model/tool spanがないのは入力validationで実行を止めたためです。「spanがないなら未実行」と一般化せず、status/error/attempt数/tool数と同じtrace IDの親spanを合わせて確認します。

成功2回は2 request合計で最大4 model API送信試行/最大2 Gateway呼び出しです。3回目のinput validation requestはmodel/toolを呼ばないので、演習全体は最大3 Runtime request、最大6 model送信、最大3 Gateway呼び出しの上限内です。実行前に固定した上限は広げません。失敗したmodel/API requestや曖昧な応答を成功として数え直さず、再実行しません。

## 5. CloudWatchでmetric・log・spanを結ぶ

対象Runtimeの既存log groupは次の形です。自分のRuntime IDで一意に置き換え、他Runtimeや全groupを検索しないでください。

```text
/aws/bedrock-agentcore/runtimes/<your-runtime-id>-DEFAULT
```

CloudWatch Metricsで region `ap-northeast-1`、namespace `Course/S06`、metric `AgentInvocationLatencyMs`、dimension `Section=S06` を選び、1分 periodで3回のUTC範囲だけを表示します。追加dimensionを作りません。各1分bucketの `SampleCount` が1であることを確認し、metricの latency value が、そのbucket内で唯一の `agent_invocation.latency_ms` と一致することを照合します。そうであれば、その一意なlog recordの `trace_id` でspanを絞ります。1 requestを60秒以上離してbucketを分けていますが、bucket内に複数request/log、missing metric/log、または値の不一致があれば相関したと結論せず停止します。時刻だけが近い別recordを対応付けません。

CloudWatch Logs Insightsは実行UTC時刻を含む短い範囲だけを選択して、次のqueryを一度だけ実行します。QueryはCloudWatch Logsの読み取りで、スキャン量に応じた費用がかかります。Start前に対象groupのstored bytesと期間を確認し、10 MiBを超える見込みやscopeの曖昧さがあればStartせず止めます。完了後 `statistics.bytesScanned` が10 MiB以下であることを確認します。超過や異常な結果の後にqueryを繰り返しません。

```text
fields @timestamp, event, invocation_id, trace_id, span_id, parent_span_id, name,
       latency_ms, duration_ms, status, error_type, model_request_attempts
| filter s06_trace = true or event = "otel_span"
| sort @timestamp asc
```

最初の2成功requestでは、`agent_invocation` と同じtrace IDの `runtime.request`、Strands `chat`、`execute_tool` spanを探し、`parent_span_id`をたどってmodel/toolの処理時間を確認します。2つの成功traceから比較的長いspanを一つ選び、そのrequestを一意のIDで特定します。3件目の400では、`status=400`/`RequestValidationError`/zero model attemptsのlogと同じtrace IDのERROR親spanを照合します。入力validation requestに子spanがないことだけを障害原因の証拠にしません。成功requestで期待spanが欠けていた場合も、400の特例で説明せず追加invoke前に停止・調査します。

invokeが失敗した場合は、`agent_invocation` の `error_type` と、記録されていれば `error_code`、`provider_event_type`、`http_status`、`original_status_code` を確認します。`provider_event_type` はSDK例外が提供する例外型の識別子（metadata）であり、response headerを調査・保存する指示ではありません。raw header、header全体、provider message、response body、promptは記録しません。情報がなければ推測せず、失敗後のinvokeを止めて確認します。usageが観測できない失敗では、費用も確定できないものとして扱います。

必要ならS04が既に用意した `sections/s04/aws_workflow.py logs` の読み取り手順も参照できます。ただし、このS06演習では上記の正確なRuntime group、3 requestの時間帯、同一IDを使い、他のRuntime logを検索しません。

## 上限と費用の見積もり

Runtime processあたり最大3 request、各request間は60秒以上です。コードは4回目、1分以内のrequest、5xx後のrequestを拒否します。これはprocess-local guardなので、process restartや別sessionで上限を回避せず、どの時点か不明な場合は停止して照合します。

- 各Runtime invocationは最大2 model send attempt、Converse bodyは最大32,768 bytes、各model requestの`maxTokens`は512、Gateway callは最大1回です。SDK/Strands retryはそれぞれ1 attemptに制限します。
- Strands `turns=2`、`output_tokens=512`、`total_tokens=2048`はloop controlであり、厳密な課金上限ではありません。model usageが結果から観測できる場合は記録します。失敗時にusageが分からない場合は不明として止めます。
- 合計log outputは1 MiB以内、Logs Insights scanは10 MiB以内、一時系列のみ、単一Runtime sessionで2時間以内です。失敗時の課金、API fees、税、為替に予備を確保し、US$2に達する・不確実なら人が停止します。これはbilling capではありません。

概算の計画値（Tokyo、USD。AWS実行前に公式料金を再確認）: Runtime V2を1 vCPU + 2.5 GiBで2時間フル使用と仮定して約$0.34、Nova Liteは最大6 model sends/各32,768 body bytesをtoken概算値とする仮定で約$0.02、Logs 1 MiBと最大10 MiB Insights scanに約$0.011、1 custom metric seriesの月額全額で約$0.30、S3 Standardにpackage 1か月保管で約$0.001、request fee/失敗時課金/税為替等の予備$1.00です。合計は約$1.67の計画値ですが、token概算や価格変動があるため保証ではありません。最終ZIP sizeと実行時の公式価格で再計算し、$2を超える可能性があれば始めません。Metric seriesはすぐ削除できず月額全額を計上します。

## 6. DEFAULTを元に戻し、演習の後片付けをする

Runtime versionはimmutableで、`DEFAULT` はRuntimeの最新versionを自動的に指します。そのためV4へendpointを直接戻す手順にはしません。S06終了後は、変更前snapshotのV4 artifactと全Runtime設定を使い、S06環境変数を含まない新しいRuntime versionを作成します。この復元versionはV4そのものではなく、内容がV4相当の新しいversion（通常はV6）です。Runtime更新後、`DEFAULT` が復元versionを指すことを `GetAgentRuntimeEndpoint` で確認します。AWS公式資料ではDEFAULTのlatest追従が説明されていますが、UpdateAgentRuntimeEndpointの例はcustom endpoint向けであり、DEFAULTをV4へ直接付け替える前提にしません。

以下のBoto3 cleanup例は保存snapshotで識別したRuntime sessionを停止し、V4の保存済みartifactと設定を使った新しい復元versionを作成してDEFAULTをそちらへ進め、今回作成したinline policyとS3 objectを対象確認後に削除します。AWS writeを行うため、自分の `$env:S06_BACKUP` と、invoke実施後なら `$env:S06_SESSION_ID` が今回の値であることを確認して一度だけ実行してください。invoke前でsession IDがまだなければsession停止は省略します。DEFAULTが今回のcandidate versionでなければ停止します。応答が曖昧な場合は再実行せず、後段のread-only確認に進んでください。

```powershell
@'
import boto3, copy, json, os, pathlib, time, uuid
from botocore.exceptions import ClientError

snapshot = json.loads(pathlib.Path(os.environ["S06_BACKUP"]).read_text(encoding="utf-8"))
account, region = snapshot["account_id"], snapshot["region"]
runtime_id = snapshot["runtime_id"]
session = boto3.Session(region_name=region)
sts = session.client("sts")
control = session.client("bedrock-agentcore-control")
runtime = session.client("bedrock-agentcore")
iam, s3 = session.client("iam"), session.client("s3")
if sts.get_caller_identity()["Account"] != account:
    raise SystemExit("Account mismatch; cleanup stopped")
expected_runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"
if snapshot.get("runtime_arn") != expected_runtime_arn:
    raise SystemExit("Saved Runtime ARN does not match the account/region/Runtime ID; cleanup stopped")

phase = snapshot.get("phase")
if phase == "restore_update_requested":
    raise SystemExit("Restore update outcome is ambiguous; read-only inspect only; do not repeat the update")
if phase in {"invoke_started", "invoke_complete"} and not snapshot.get("session_id"):
    raise SystemExit("Invoke phase has no saved session ID; stop before cleanup writes and inspect read-only")

# Stop only the session ID printed by this run's invoke command. Before the
# invoke step, no session exists and this call is intentionally skipped.
session_id = snapshot.get("session_id")
if session_id:
    try:
        stopped = runtime.stop_runtime_session(agentRuntimeArn=snapshot["runtime_arn"],
                                               runtimeSessionId=session_id)
        print(json.dumps({"session_stop_status": stopped.get("statusCode"),
                          "session_id": session_id}))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"ResourceNotFoundException", "ResourceNotFound"}:
            raise
        print(json.dumps({"session_stop_status": "already ended or not found",
                          "session_id": session_id}))
else:
    print(json.dumps({"session_stop_status": "skipped; invoke step was not run"}))

baseline_version = snapshot["baseline_version"]
candidate_version = snapshot.get("candidate_version")
current_endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
baseline = control.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=baseline_version)
if baseline.get("status") != "READY":
    raise SystemExit("Saved baseline Runtime version is not READY; keep resources and inspect")
saved_runtime = snapshot["runtime"]
resume_cleanup_phases = {"restore_update_submitted", "restore_ready",
                         "cleanup_policy_removed", "cleanup_object_removed"}
if candidate_version and snapshot.get("phase") in resume_cleanup_phases:
    restore_version = snapshot.get("restore_version")
    if not restore_version or current_endpoint.get("status") != "READY" or current_endpoint.get("liveVersion") != restore_version:
        raise SystemExit("Restore outcome is not confirmed by the saved version and DEFAULT; read-only inspect only")
    restored_runtime = control.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=restore_version)
    if restored_runtime.get("status") != "READY":
        raise SystemExit("Saved restore version is not READY; keep policy/object and inspect")
    endpoint = current_endpoint
elif candidate_version:
    if current_endpoint.get("status") != "READY" or current_endpoint.get("liveVersion") != candidate_version:
        raise SystemExit("DEFAULT is not serving this run's candidate; stop and inspect before restoring")
    candidate = control.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=candidate_version)
    expected_artifact = copy.deepcopy(saved_runtime["agentRuntimeArtifact"])
    expected_artifact["codeConfiguration"]["code"]["s3"] = {
        "bucket": snapshot["candidate_bucket"], "prefix": snapshot["candidate_key"]}
    expected_environment = dict(saved_runtime.get("environmentVariables") or {})
    expected_environment.update({"S06_TRACE_ENABLED": "true", "S06_METRICS_ENABLED": "true"})
    for name, expected in (("agentRuntimeArtifact", expected_artifact),
                           ("roleArn", saved_runtime["roleArn"]),
                           ("platformVersion", saved_runtime["platformVersion"]),
                           ("networkConfiguration", saved_runtime["networkConfiguration"]),
                           ("protocolConfiguration", saved_runtime["protocolConfiguration"]),
                           ("environmentVariables", expected_environment)):
        if candidate.get(name) != expected:
            raise SystemExit(f"Active candidate differs from this run's saved baseline + S06 ({name}); keep resources")
    for optional in ("metadataConfiguration", "lifecycleConfiguration"):
        if candidate.get(optional) != saved_runtime.get(optional):
            raise SystemExit(f"Active candidate differs from saved baseline ({optional}); keep resources")
    if candidate.get("status") != "READY":
        raise SystemExit("Candidate is not READY; keep policy/object and inspect")
    restore_request = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArtifact": saved_runtime["agentRuntimeArtifact"],
        "roleArn": saved_runtime["roleArn"],
        "platformVersion": saved_runtime["platformVersion"],
        "networkConfiguration": saved_runtime["networkConfiguration"],
        "protocolConfiguration": saved_runtime["protocolConfiguration"],
        "environmentVariables": saved_runtime.get("environmentVariables") or {},
        "clientToken": str(uuid.uuid4()),
    }
    for optional in ("metadataConfiguration", "lifecycleConfiguration"):
        if optional in saved_runtime:
            restore_request[optional] = saved_runtime[optional]
    snapshot["phase"] = "restore_update_requested"
    pathlib.Path(os.environ["S06_BACKUP"]).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    restored = control.update_agent_runtime(**restore_request)
    restore_version = restored["agentRuntimeVersion"]
    snapshot.update({"restore_version": restore_version, "phase": "restore_update_submitted"})
    pathlib.Path(os.environ["S06_BACKUP"]).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        restored_runtime = control.get_agent_runtime(agentRuntimeId=runtime_id, agentRuntimeVersion=restore_version)
        if restored_runtime.get("status") == "READY":
            break
        if restored_runtime.get("status") not in {"CREATING", "UPDATING"}:
            raise SystemExit("Restored Runtime version failed; keep policy/object and inspect")
        time.sleep(10)
    else:
        raise SystemExit("Restored Runtime timed out; keep policy/object and inspect")
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
        if endpoint.get("status") == "READY" and endpoint.get("liveVersion") == restore_version:
            break
        if endpoint.get("status") not in {"CREATING", "UPDATING"}:
            raise SystemExit("DEFAULT did not move to restored version; keep policy/object and inspect")
        time.sleep(10)
    else:
        raise SystemExit("DEFAULT restore timed out; keep policy/object and inspect")
    snapshot["phase"] = "restore_ready"
    pathlib.Path(os.environ["S06_BACKUP"]).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
else:
    if snapshot.get("phase") not in {"policy_created", "s3_uploaded", "cleanup_policy_removed", "cleanup_object_removed"} or session_id:
        raise SystemExit("No candidate version is saved but deployment/invoke phase is ambiguous; read-only inspect only")
    if current_endpoint.get("status") != "READY" or current_endpoint.get("liveVersion") != baseline_version:
        raise SystemExit("DEFAULT differs from saved baseline; do not partial-clean resources")
    versions, token = [], None
    while True:
        page_request = {"agentRuntimeId": runtime_id, "maxResults": 100}
        if token:
            page_request["nextToken"] = token
        page = control.list_agent_runtime_versions(**page_request)
        versions.extend(page.get("agentRuntimes", []))
        token = page.get("nextToken")
        if not token:
            break
    if not versions or max((v["agentRuntimeVersion"] for v in versions), key=int) != baseline_version:
        raise SystemExit("Runtime version list is not unchanged from baseline; do not partial-clean")
    candidate_key = snapshot.get("candidate_key")
    candidate_bucket = snapshot.get("candidate_bucket")
    for item in versions:
        version_runtime = control.get_agent_runtime(agentRuntimeId=runtime_id,
                                                    agentRuntimeVersion=item["agentRuntimeVersion"])
        code = (((version_runtime.get("agentRuntimeArtifact") or {}).get("codeConfiguration") or {}).get("code") or {}).get("s3") or {}
        if candidate_key and code.get("bucket") == candidate_bucket and code.get("prefix") == candidate_key:
            raise SystemExit("A Runtime version references the S06 candidate; do not partial-clean")
    for name in ("agentRuntimeArtifact", "roleArn", "platformVersion", "networkConfiguration",
                 "protocolConfiguration", "environmentVariables", "metadataConfiguration", "lifecycleConfiguration"):
        if baseline.get(name) != saved_runtime.get(name):
            raise SystemExit(f"Current DEFAULT baseline differs from saved state ({name}); do not partial-clean")
    restored_runtime, restore_version, endpoint = baseline, baseline_version, current_endpoint
if (restored_runtime.get("environmentVariables") or {}) != (saved_runtime.get("environmentVariables") or {}):
    raise SystemExit("Restored Runtime environment differs from the saved baseline; keep resources")
for name in ("agentRuntimeArtifact", "roleArn", "platformVersion", "networkConfiguration",
             "protocolConfiguration", "metadataConfiguration", "lifecycleConfiguration"):
    if restored_runtime.get(name) != saved_runtime.get(name):
        raise SystemExit(f"Restored Runtime differs from saved baseline ({name}); keep resources")

role_name = saved_runtime["roleArn"].rsplit("/", 1)[-1]
policy_name = snapshot.get("metric_policy_name", "S06CourseMetrics")
expected_policy = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow",
    "Action": "cloudwatch:PutMetricData", "Resource": "*",
    "Condition": {"StringEquals": {"cloudwatch:namespace": "Course/S06",
                                    "aws:RequestedRegion": region}}}]}
current_policy = None
try:
    current_policy = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)["PolicyDocument"]
except ClientError as exc:
    if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
        raise
else:
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))
    if canonical(current_policy) != canonical(expected_policy):
        raise SystemExit("S06 policy differs from this exercise; do not delete it")
bucket, key = snapshot["candidate_bucket"], snapshot["candidate_key"]
versioning = s3.get_bucket_versioning(Bucket=bucket, ExpectedBucketOwner=account)
if versioning.get("Status") is not None:
    raise SystemExit("Artifact bucket versioning changed; do not delete or claim all versions are removed")
expected_bucket = saved_runtime["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]["bucket"]
if bucket != expected_bucket or not key.startswith("s06/"):
    raise SystemExit("S06 object target differs from the saved target; do not delete")
obj = None
try:
    obj = s3.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
except ClientError as exc:
    if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
        raise
else:
    if obj.get("Metadata", {}).get("sha256") != snapshot["candidate_sha256"] or obj.get("ContentLength") != snapshot["candidate_size"]:
        raise SystemExit("S3 object differs from this exact ZIP; do not delete it")
if current_policy is not None:
    iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
    snapshot["phase"] = "cleanup_policy_removed"
    pathlib.Path(os.environ["S06_BACKUP"]).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
if obj is not None:
    s3.delete_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
    snapshot["phase"] = "cleanup_object_removed"
    pathlib.Path(os.environ["S06_BACKUP"]).write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

# Read-only residual checks.
endpoint = control.get_agent_runtime_endpoint(agentRuntimeId=runtime_id, endpointName="DEFAULT")
if endpoint.get("status") != "READY" or endpoint.get("liveVersion") != restore_version:
    raise SystemExit("DEFAULT residual check failed")
try:
    iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
except ClientError as exc:
    if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
        raise
else:
    raise SystemExit("S06 inline policy remains; inspect")
try:
    s3.head_object(Bucket=bucket, Key=key, ExpectedBucketOwner=account)
except ClientError as exc:
    if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
        raise
else:
    raise SystemExit("S06 S3 object remains; inspect")
print(json.dumps({"cleanup_verified": True, "runtime_id": runtime_id,
                  "original_baseline_version": baseline_version,
                  "restored_version": restore_version,
                  "default_live_version": endpoint["liveVersion"],
                  "metric_series_remains_until_service_expiry": True,
                  "shared_log_group_and_retention_preserved": True}, indent=2))
'@ | uv run --project sections/s02 python -
```

cleanup commandの最後にpolicyとS3 objectの不在、DEFAULT liveVersionを再確認します。通常終了ではDEFAULT liveVersionが新しい復元versionになり、これはV4そのものではなくV4 artifact/設定から作った新しいversionです。途中デプロイでRuntime update前に止まった場合はbaseline versionがそのままです。どちらの場合もS06 candidate versionはimmutable履歴として残ることがあり、そのversionやRuntimeを削除しません。

cleanup後、S04 environment values、既存Gateway/role policy、log group retentionが維持されていることを読み取り確認します。`Course/S06 / AgentInvocationLatencyMs / Section=S06`への送信を止めます。CloudWatch custom metricは個別に即時削除できないため系列は残ることがあります。既存S03/S04 log groupや7日retentionは共有なので削除・変更しません。ログは既存retentionに従ってexpireします。package ZIPとsnapshotは復元・policy/object・請求/費用記録が揃うまで保持し、その後、自分が作った一時ファイルだけを削除できます。不明なresource・objectは削除せず止めて確認します。

Cleanup完了時にRuntime ID、S04 Gateway ID、baseline/S06 versions、DEFAULT liveVersion、変更したIAM policy名、candidate keyとその削除確認、metric namespace/name/dimensionと残存、log group/retention、request・model・tool回数、log/query bytes、usageと費用見込みを自分の作業記録に残します。account-wide設定、別Lecture/Runtime、既存S04 resourceは変更しません。

## AWS資料

- [S03 Runtimeの作成・invoke・status・logs](../s03/README.md)
- [S04 Gatewayと合成データ](../s04/README.md)
- [AgentCore Runtime Invoke API (Boto3)](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_agent_runtime.html)
- [GetAgentRuntimeEndpoint API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_GetAgentRuntimeEndpoint.html)
- [UpdateAgentRuntimeEndpoint API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_UpdateAgentRuntimeEndpoint.html)
- [UpdateAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_UpdateAgentRuntime.html)
- [AgentCore Runtime versioning and endpoints](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html)
- [AgentCore observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html)
- [Amazon CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/)
- [Amazon Bedrock AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
