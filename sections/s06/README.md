# S06：Agentのモデル・tool処理をCloudWatchで追う

この演習ではAgentCore Runtime requestを親span、Strandsのmodelと`lookup_runbook`呼出しを子spanとして記録し、JSON logのtrace ID、回答のinvocation ID・trace ID・tool実行結果、CloudWatch custom metricを照合します。`lookup_runbook`は`sections/s02/runbooks.json`だけを読むローカルtoolです。runbookは合成教材データで、AWS上の実障害ではありません。CloudWatch実データ、S04 Gateway、Memory、顧客システムは読みません。

## 作成・変更するもの

- 自分のS03 artifact bucketに新しいkey `s06/<run-id>/<zip-sha256>/s06-runtime-arm64.zip`を一つ作ります。bucketと既存objectは変更しません。
- 専用Runtime `udemy_s06_obs_20261105`、role `udemy_s06_obs_20261105_runtime`、限定inline policy、専用log groupを作ります。Runtime V2、PUBLIC network、HTTP protocol、Python 3.13。idle timeout 300秒、最大lifetime 900秒。
- CloudWatch custom metricは`Course/S06 / AgentInvocationLatencyMs`、dimension `Section=S06, RunId=<run>`の一時系列だけです。S03/S04のRuntime、endpoint、role、policy、Gateway、environment、保護対象object、bucket自体は変更しません。

## 前提と環境

S03を完了し、自分のaccountにartifact bucketがあること。S04 Gatewayは使いません。標準経路はS02で使ったローカルPCのPython/uv/editorとPowerShellです。S02の既存checkoutを使い、新しいOS、shell、credential設定は導入しません。AWS CloudShellは任意経路で、ローカルclone、uv環境、private stateは共有されません。必要なPython、uv、AWS CLI、locked dependencies、認証済みcredential providerが使えない場合は停止し、認証設定を変更しません。

`sections/s06/.agentcore-s06-state.json`、`.agentcore-s06-state.tmp`、`artifacts/s06-runtime-private.json`はprompt、answer、tool arguments/resultsを含むprivate記録です。共有・公開・Git追加しません。stateが既にあれば上書きせず停止します。

## 1. source・環境・account/Regionを確認

以降は選んだ一方の環境だけで行います。Console上部で自分の学習accountとTokyo (`ap-northeast-1`)を確認します。

### 標準：ローカルPC PowerShell

S02の既存public checkoutのrepository rootで実行します。

```powershell
Get-Command python, uv, aws, git
python --version
uv --version
aws --version
git status --short --branch
uv sync --locked --project sections/s02
uv run --project sections/s02 python --version
```

Python 3.11〜3.13とcommandの利用可能性を確認し、未確認のGit変更があれば停止します。locked dependencyを同期できなければAWS操作をしません。

S03を既存のnamed profileで実行した場合は、以降のAWS CLIとS06 Python helperでも同じ既存profileを使います。このPowerShell session内で次のように選ぶと、AWS CLIとBoto3の両方に適用されます。実際の既存profile名に置き換えてください。

```powershell
$env:AWS_PROFILE = "既存profile名"
```

S03でdefault profileを使った場合は、`$env:AWS_PROFILE`が未設定であることを確認し、別profileが設定されていればこのPowerShell sessionだけで解除します。

```powershell
Remove-Item Env:AWS_PROFILE -ErrorAction SilentlyContinue
```

既存profileの選択・設定が分からない場合は停止してください。profileやcredentialの新規作成、SSO変更、login情報のcopyは不要です。CloudShellはConsoleから提供される既存sessionを使うため、ローカルprofileを持ち込みません。

### 任意：AWS CloudShell Bash

CloudShellはlocal S02 stateを持たないため、未使用の一意なdirectoryに公開cloneを取得し、独立してlocked環境を準備します。directoryを流用・削除・resetしません。

```bash
for command in python3 uv aws git; do
  if ! command -v "$command" >/dev/null 2>&1; then echo "Required command $command unavailable; stop." >&2; exit 1; fi
done
python3 --version
uv --version
aws --version
python3 -c 'import sys; assert (3, 11) <= sys.version_info[:2] < (3, 14)'
WORK_DIR="$HOME/agentcore-s06-hands-on-$(date +%Y%m%d-%H%M%S)-$$"
if [ -e "$WORK_DIR" ]; then echo "Choose a new directory; stop." >&2; exit 1; fi
git clone https://github.com/toma1110/amazon-bedrock-agentcore-operations-hands-on.git "$WORK_DIR"
cd "$WORK_DIR"
git rev-parse --show-toplevel
git log -1 --oneline
git status --short --branch
uv sync --locked --project sections/s02
uv run --project sections/s02 python --version
```

このblockはBash専用です。PowerShellへ貼り付けません。CloudShellで必要なcommand/providerを使えない場合、installや認証変更をせず停止します。

12桁の自分の学習account IDを入力し、STS応答、Console account、入力値を照合します。placeholderのまま進めません。

PowerShell:
```powershell
$EXPECTED_ACCOUNT = Read-Host "Expected 12-digit AWS account ID"
if ($EXPECTED_ACCOUNT -notmatch '^\d{12}$') { throw "Enter exactly 12 digits; stop." }
aws sts get-caller-identity --region ap-northeast-1
"AWS_REGION=$env:AWS_REGION AWS_DEFAULT_REGION=$env:AWS_DEFAULT_REGION"
aws configure get region
aws configure list
```

CloudShell Bash:
```bash
read -r -p "Expected 12-digit AWS account ID: " EXPECTED_ACCOUNT
if [[ ! "$EXPECTED_ACCOUNT" =~ ^[0-9]{12}$ ]]; then echo "Enter exactly 12 digits; stop." >&2; exit 1; fi
aws sts get-caller-identity --region ap-northeast-1
printf 'AWS_REGION=%s\nAWS_DEFAULT_REGION=%s\n' "${AWS_REGION:-<unset>}" "${AWS_DEFAULT_REGION:-<unset>}"
aws configure get region
aws configure list
```

空の`aws configure get region`だけで未ログインとは判断しません。Tokyoが確認でき、env/configに別Regionとの矛盾がなく、S06 helperがTokyoを明示利用する場合は続行できます。account/region不一致や曖昧さがあれば作成前に停止します。credential情報を公開せず、global設定やSSOを変えません。

### 自分のS03 Runtimeとbucket

自分のRuntime IDを特定し、read-only照会でname/status/bucket/keyを確認します。別用途、見覚えのないRuntime、空bucket、別Regionなら停止し、他人の値やbucketに切り替えません。

ローカルではS03と同じrun tag（defaultなら`default`）のstatusを確認できます。stateがない場合は自分のRuntime IDをS03/Consoleから確認して下の照会を使います。

PowerShell:
```powershell
$S03_RUN_TAG = "default" # S03で別tagを使った場合は正確なtagへ置換
uv run --project sections/s02 python sections/s03/aws_workflow.py --run-tag $S03_RUN_TAG status
$S03_RUNTIME_ID = Read-Host "Your S03 AgentCore Runtime ID"
if ($S03_RUNTIME_ID -notmatch '^[a-zA-Z][a-zA-Z0-9_]{0,99}-[a-zA-Z0-9]{10}$') { throw "Invalid Runtime ID; stop." }
$S03_INFO = aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id $S03_RUNTIME_ID --region ap-northeast-1 --query '{name:agentRuntimeName,status:status,bucket:agentRuntimeArtifact.codeConfiguration.code.s3.bucket,key:agentRuntimeArtifact.codeConfiguration.code.s3.prefix}' --output json --no-cli-pager
if ($LASTEXITCODE -ne 0) { throw "S03 query failed; stop." }
$S03_INFO
$ARTIFACT_BUCKET = aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id $S03_RUNTIME_ID --region ap-northeast-1 --query 'agentRuntimeArtifact.codeConfiguration.code.s3.bucket' --output text --no-cli-pager
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($ARTIFACT_BUCKET) -or $ARTIFACT_BUCKET -eq "None") { throw "No artifact bucket; stop." }
```

Fresh CloudShell cloneにはS03 private stateがないためstatusは実行しません。自分のRuntime IDをS03/Consoleから確認します。

CloudShell Bash:
```bash
read -r -p "Your S03 AgentCore Runtime ID: " S03_RUNTIME_ID
if [[ ! "$S03_RUNTIME_ID" =~ ^[a-zA-Z][a-zA-Z0-9_]{0,99}-[a-zA-Z0-9]{10}$ ]]; then echo "Invalid Runtime ID; stop." >&2; exit 1; fi
S03_INFO="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$S03_RUNTIME_ID" --region ap-northeast-1 --query '{name:agentRuntimeName,status:status,bucket:agentRuntimeArtifact.codeConfiguration.code.s3.bucket,key:agentRuntimeArtifact.codeConfiguration.code.s3.prefix}' --output json --no-cli-pager)" || exit 1
printf '%s\n' "$S03_INFO"
ARTIFACT_BUCKET="$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$S03_RUNTIME_ID" --region ap-northeast-1 --query 'agentRuntimeArtifact.codeConfiguration.code.s3.bucket' --output text --no-cli-pager)" || exit 1
if [[ -z "$ARTIFACT_BUCKET" || "$ARTIFACT_BUCKET" == "None" ]]; then echo "No artifact bucket; stop." >&2; exit 1; fi
```

返ったRuntime name/statusとbucket/keyが自分のS03の値であることを確認します。これは読み取りだけです。[AWS CLI reference](https://docs.aws.amazon.com/cli/latest/reference/bedrock-agentcore-control/get-agent-runtime.html)

## 2. packageし、AWS preflightで対象を記録

repository rootで、locked S02 dependenciesからARM64/Python 3.13向けZIPを作ります。output directory `sections/s06/build/`はgitignoredです。

```text
uv run --project sections/s02 python sections/s06/package_artifacts.py --output sections/s06/build
```

表示されたZIP path、size、SHA-256を確認します。packageが成功してからpreflightします。preflightはaccount、bucket owner/versioning、専用roleとRuntime nameの未使用状態を照合し、ZIP hash/sizeと新しいS3 keyをprivate local stateへ記録します。AWS Runtime等をまだ作成・変更しません。既存state/role/Runtime、bucket versioningの問題があれば上書き・採用せず停止します。

PowerShell:
```powershell
uv run --project sections/s02 python sections/s06/aws_workflow.py --expected-account $EXPECTED_ACCOUNT preflight --artifact-bucket $ARTIFACT_BUCKET --zip sections/s06/build/s06-runtime-arm64.zip
```

CloudShell Bash:
```bash
uv run --project sections/s02 python sections/s06/aws_workflow.py --expected-account "$EXPECTED_ACCOUNT" preflight --artifact-bucket "$ARTIFACT_BUCKET" --zip sections/s06/build/s06-runtime-arm64.zip
```

`preflight_complete`、account、bucket、key、hash/sizeが想定どおりの場合だけ進みます。

## 3. 専用Runtimeをdeploy

```text
uv run --project sections/s02 python sections/s06/aws_workflow.py --expected-account "$EXPECTED_ACCOUNT" deploy
```

PowerShellでもCloudShellでも同じCLIを使います。deployはZIP identityを再確認し、専用role/policy、新規object、V2 RuntimeとMMDSv2設定を作成し、DEFAULT endpointが対象versionでREADYになるのを待ちます。成功時のRuntime ID/ARN、role、bucket/key、hash/size、log group、version、endpoint statusを確認します。error、timeout、接続断など結果が不明なら繰り返さず、復旧手順へ進みます。

## 4. 同じ質問2回とpromptなし1回をinvoke

一つのRuntime sessionを使い、`catalog-api の5xxを調べる順序を教えてください。`を2回、61秒間隔で送り、最後に`{}`を送ります。

```text
uv run --project sections/s02 python sections/s06/aws_workflow.py --expected-account "$EXPECTED_ACCOUNT" invoke
```

上限はRuntime request 3回、HTTP request body最大16,384 bytes、valid invocationごとmodel request最大2回、出力最大512 tokens、model request body 32,768 bytes、local `lookup_runbook`最大1回（全体model最大4回/tool最大2回）です。promptなしrequestはmodel/toolを呼びません。modelがtoolを呼ぶ保証はありません。traceにtool resultがなければ推測しません。

model error、timeout、outer response不明、1回目/2回目の失敗時は追加requestを送りません。3回目はapplication `status=400`, `RequestValidationError`, model/tool attempt 0を確認します。outer HTTP 424の場合があり、outer statusとapplication statusは別です。invokeが`requested`または`uncertain`のままなら再invokeせず、下記の一度だけのreadbackを使えるか確認します。

## 5. 専用CloudWatch Logsとmetricを読み戻す

```text
uv run --project sections/s02 python sections/s06/aws_workflow.py --expected-account "$EXPECTED_ACCOUNT" readback
```

通常の完了時readbackはstate記録済みRuntime IDから生成した一つのlog groupと3 requestのUTC窓だけを対象にし、最大100 events・1 MiB、専用groupだけ1日retentionを設定します。invokeが`invoke_[1-3]_(requested|uncertain)`のphaseで止まった場合は、保存済みsend intentとprivate RAWの同じRuntime/sessionに属する連続した送受信記録が、正確な有限の時間窓を示す場合に限り、保存済みRAW subsetに基づくreadbackを一度だけ実行できます。この不確かな経路ではretentionを変更せず、AWS Logs/Metricsへのread-only照会だけを行います。送受信recordの欠落、順序不一致、timestamp不明/重複などで境界が確定しなければAPI照会をせず停止します。全group検索、Logs Insights、Transaction Search、共有`aws/spans`、広い時間窓への拡張はしません。

この一度だけのreadbackは最大100 events・1 MiB、exact専用log group、一つのRunId metric seriesから最大3 pointsに制限されます。readback開始phaseはAPI前にprivate stateへ保存されるため、成功・失敗にかかわらずreadbackを繰り返せません。private recordでrequest/responseまたは安全なouter error、回答のinvocation/trace ID、実際に含まれる場合のtool arguments/resultを確認します。`agent_invocation` logsにprompt/answer/tool arguments/resultsを出しません。JSON `otel_span`はIDs、timing、model/tool labels等の安全なfieldだけです。

metricは専用RunId dimensionの1時系列から最大3 pointsを読取ります。時刻だけではrequestと対応付けず、1分bucket内のlog、latency、trace IDが一意に合わなければ相関不成立です。欠落を埋める再invokeはしません。回答品質は自動採点せず、tool resultのsource・実textを同responseの主張と照合し、未確認範囲を記録します。

## 6. cleanup

readbackと独立review後、同じaccountでcleanupします。

```text
uv run --project sections/s02 python sections/s06/aws_workflow.py --expected-account "$EXPECTED_ACCOUNT" cleanup
```

保存済みexact Runtime ID、専用role/policy、ZIP hash/sizeとbucket/key、専用log groupを照合し、S06 Runtime/object/policy/role/log groupだけを削除してabsenceを確認します。S03/S04、bucket自体、shared retention、他Runtime/group、account-wide設定は変えません。metric series/datapointsはcleanupで即時削除できず残存し得ます。

## 7. 曖昧な結果・中断時の復旧

書込み/応答が不明なら同じ`deploy`、`invoke`、`readback`、`cleanup`を再実行しません。private stateを保ち、次のread-only reconciliationを行います。

不確かなinvoke (`requested`/`uncertain`)で正確な時間窓がprivate RAWから証明できる場合は、先に上記の一度だけのreadbackを行い、観測結果を独立reviewへ**未確認**として渡します。requestが実行済みか、application 400か、model/tool callが0かを推測しません。窓を証明できない、readback開始後に失敗した、または観測が不足する場合は再readback/reinvokeせず、保存stateのまま下の`reconcile`で正確な状態を照合してcleanup判断へ進みます。

```text
uv run --project sections/s02 python sections/s06/aws_workflow.py --expected-account "$EXPECTED_ACCOUNT" reconcile
```

これはsaved stateと専用role/policy、object identity、Runtime configuration/tags、log groupを照合します。deploy中、invoke request後/応答不明、readback中断、cleanup途中のいずれも、phaseと各identityを読みます。競合・重複・不一致・想定外resourceがあれば停止し削除しません。出力が保存した新しい`partial_reconciled` stateで今回のrunに一意一致した場合だけ、`cleanup`を実行して残存resourceを再照合しcleanupします。`cleanup`はこの新しいreconcile結果がある場合だけpartial cleanupを許します。

Runtimeが`DELETING`ならread-onlyで不在を待ち、deleteを再送しません。Runtime不在確認後にだけ依存resourceへ進みます。削除結果不明のresourceを盲目的に削除再試行しません。reconcile/cleanup自体が失敗したら推測で再実行せず停止し、metricの残存可能性を記録します。

## 実行上限と料金

一つのsession、最大900秒lifetime/300秒idle、3 Runtime request、最大4 model request（valid invocation各最大2回）、最大2 tool call、1 custom metric series、最大1 MiB readbackに限ります。retryの許可や請求capではありません。

実費は未観測です。公開Runtime V2単価CPU `$0.1276/vCPU-hour`、memory `$0.0169/GB-hour`、最大2 vCPU/8 GBを900秒使い続ける仮定は約`$0.0976`ですが、Tokyo固有の確定額でもhard capでもありません。model費用は最大4 request・512出力token/回・32,768 byte guardと実token usageから見積もります。system prompt、tool schema、historyもinput tokenです。token usage未観測ならmodel費用未確定です。[Amazon Bedrock Pricing](https://aws.amazon.com/bedrock/pricing/)で実行時にregion/model単価を確認します。metric datapointsはservice retentionまで残り課金が続く場合があります。Tokyo料金は[CloudWatch Pricing](https://aws.amazon.com/cloudwatch/pricing/)で確認します。`$2`は人為的stop/reassess thresholdで請求capではありません。

## AWS資料

- [CreateAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateAgentRuntime.html)
- [UpdateAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_UpdateAgentRuntime.html)
- [Runtime session lifecycle](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html)
- [AgentCore Runtime billing](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html)
- [Amazon Bedrock AgentCore Pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [Amazon Nova Lite model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-lite.html)
