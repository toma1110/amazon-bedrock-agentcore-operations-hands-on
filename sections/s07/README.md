# S07: エージェントの失敗を調べて復旧する

S07では、実行記録から失敗箇所を調べ、確認できた原因だけを修正し、同じ業務入力で結果を確認する方法を学びます。ローカル演習は合成データと`127.0.0.1`上の一時APIスタブを使います。AWSで行うのは、利用を許可された既存Runtimeへの正常な呼び出しと読み取り専用のログ確認です。AWSの失敗例は記録から観察し、IAM Denyや無効なモデル設定を受講者が再現する手順は含みません。

## ローカル環境

リポジトリrootで実行します。`sections/s02`のロック済みuv環境（Python 3.12）を演習とテストに使います。Runtime用のパッケージ成果物は別途Python 3.13/Linux ARM64向けであり、ローカル環境の代用ではありません。

ローカルテストを実行します。AWS APIや認証情報は使いません。

```powershell
uv run --project sections/s02 --with pytest pytest sections/s07/tests
```

## ローカル故障演習

次のPowerShellブロックは既存のS07関数を呼び出す、決定的な再現用デモです。Bedrockモデル、AgentCore Runtime、S04 Gatewayへ接続しません。コマンドを実行すると、入力検証エラー、ツール内部エラー、HTTP 429・503、タイムアウト、最後の復旧結果を端末に表示します。L06の診断行は原因を開示するため、初見の診断問題ではありません。初見演習では、その行と復旧コマンドを見せる前に、学習者がタイムアウト症状から原因と確認方法を記録します。その後、講師が下記デモを実行して答え合わせと同一リクエストの復旧を確認します。

```powershell
$demo = @'
import json, sys, time
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
root = Path.cwd()
sys.path.insert(0, str(root / "sections" / "s07"))
sys.path.insert(0, str(root / "sections" / "s02"))
import agent_app, fault_injection as fi

valid = {"service": "catalog-api", "topic": "5xx"}
invalid = {"service": "catalog-api", "topic": "unknown"}

def show(label, operation):
    try:
        print(json.dumps({"case": label, "result": operation()}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"case": label, "error_type": type(exc).__name__, "message": str(exc),
                          "status": getattr(exc, "status", None),
                          "attempts": getattr(exc, "attempts", None)}, ensure_ascii=False))

# L03: reject invalid input; inject a separate implementation failure; resend the same valid input.
show("L03-invalid-input", lambda: fi.call_checked_tool(invalid, agent_app.find_runbook))
show("L03-internal-error", lambda: fi.call_checked_tool(valid, agent_app.find_runbook, inject_internal_error=True))
show("L03-same-valid-input-recovered", lambda: fi.call_checked_tool(valid, agent_app.find_runbook))

# L05: the business request is always GET /sample with a 250 ms timeout.
# scenario is only the local fault-control setting; the stub accepts no external URL.
for scenario in ("429", "5xx", "delay", "success"):
    show("L05-" + scenario, lambda scenario=scenario: fi.call_local_api_stub(scenario, timeout_ms=250))
    if scenario == "delay":
        time.sleep(0.85)  # the local handler's fixed 750 ms delay may outlast the client timeout

# L06 reproduction: this deterministic script intentionally shows the cause after the timeout.
# For a first-look diagnostic exercise, pause before this block and ask learners to record
# their hypothesis and evidence to collect; only then run it as the reveal/recovery step.
show("L06-reproduction-timeout", lambda: fi.call_local_api_stub("delay", timeout_ms=250))
print(json.dumps({"L06-diagnosis": "localhost stub delay is fixed at 750 ms; client timeout is 250 ms",
                  "business_request": {"method": "GET", "path": "/sample", "timeout_ms": 250}}, ensure_ascii=False))
time.sleep(0.85)
show("L06-same-request-after-fault-cleared", lambda: fi.call_local_api_stub("success", timeout_ms=250))
'@
uv run --project sections/s02 python -c $demo
```

期待される結果は次のとおりです。

- L03: 不正な`topic`は`ToolInputError`、別途注入した実装失敗は`ToolExecutionError`として区別されます。その後、同じ有効入力`{"service":"catalog-api","topic":"5xx"}`は合成runbookを返します。
- L05: 同じ`GET /sample`業務リクエストに対し、429と503は初回と最大1回の再試行の後にエラー、750 msの遅延は250 msでタイムアウト、faultなしでは`status: 200`です。
- L06: まずタイムアウトだけを観察し、その後でスタブの750 ms遅延とクライアントの250 ms timeoutを原因として確認します。故障を解除した同じ`GET /sample`は`status: 200`、`attempts: 1`で返ります。

429と503の試行は、初回と再試行1回の合計2回までです。実サービス障害やAWSの状態を表す結果ではありません。演習コードは各呼び出しの`finally`でlocalhost listener/socketを閉じます。遅延ケースのサーバーハンドラーが終了する時間を確保するため、コード内で850 ms待機します。外部URL、AWS資格情報、任意の宛先は入力として受け付けません。

## ローカル実行成果物の生成

Runtime用ZIPを作る場合は、repository rootから未使用の一意な出力directoryを指定します。出力先がすでに存在するときは、既存成果物を上書きせず停止します。

```powershell
uv run --project sections/s02 python sections/s07/package_artifacts.py --output sections/s07/build-s07-run-01
```

依存packageの配置とZIP検査はrepository外の一時directoryで行い、検査済みZIPだけを指定した出力先へコピーします。同じ出力先は再利用しないでください。

## AWS: 正常な要求を1回送り、結果をログで確かめる

この演習は、すでに用意され、利用を許可されたAgentCore Runtimeを使います。学習者アカウント、Runtime ARN、リージョンは自分の環境の値へ置き換えてください。IAM Denyや無効なモデル名を作る失敗注入、IAM policyの変更、Runtimeの作成・更新は行いません。AWSでの失敗例は、講義内の記録から「主体・操作・対象」を読み取り、原因と確認できた修正箇所を考えます。

記録されたIAMの例では、主体はS07専用Runtime実行ロール、操作は`bedrock:InvokeModel`、対象は東京リージョンのNova Liteモデルです。記録の担当者は一時Denyを追加して拒否記録を得た後、そのDenyを除去しました。その後、別のモデル設定ミスの試験と設定復元を経て、失敗時と同じ業務入力で正常応答を確認しています。これはDeny解除だけの効果を独立に再試験した結果ではありません。既に行われた操作の記録であり、受講者がIAM policyを追加・除去・変更する指示ではありません。記録には`AccessDeniedException`と`EventLoopException`が混在しているため、単一拒否や複数記録の原因を推測しません。受講者はこの失敗を再現せず、記録された主体・操作・対象を読み解いてください。

### 1. 呼び出し先と費用を確認する

AWS Consoleで、担当者から指定された学習者アカウント（`<LEARNER_ACCOUNT_ID>`）、リージョン（`<REGION>`）、Runtime ARN（`<RUNTIME_ARN>`）、`DEFAULT` qualifierと使用許可を確認します。Runtime ARN内のアカウントIDと、Consoleで選択している学習者アカウントが一致していることも確かめます。Runtimeの準備完了表示だけでは、モデルへの接続や正常な応答までは分かりません。確認できない値があれば実行せず、環境の担当者に問い合わせてください。

開始前に、現在のAWS料金表と自分のアカウントの料金・予算アラートを確認します。1回の呼び出しでもRuntimeの稼働、モデルtoken、ログの取り込み・保存・検索などに費用が発生することがあります。コードの再試行設定は費用上限ではありません。承認された費用条件が分からない場合は実行しません。以下は、既に許可された学習者用AWS identityで実行し、コードやコマンドへcredentialを埋め込みません。

### 2. 正常要求を1回だけ送信する

AWS SDK for Python (Boto3)を使う例です。`RUNTIME_ARN`と`REGION`を自身の環境用に設定します。session IDはこの1回のためにUUIDとして新しく生成します。接続失敗などで結果が分からないときは同じ要求を再送せず、SDKの応答とログを調べてから担当者に相談してください。

```python
import json
import time
import uuid

import boto3
from botocore.config import Config

runtime_arn = "<RUNTIME_ARN>"
region = "<REGION>"
session_id = str(uuid.uuid4())  # この実行用に一度だけ生成する
payload = {
    "prompt": "Find the synthetic runbook for catalog-api 5xx and summarize its three checks."
}

client = boto3.client(
    "bedrock-agentcore",
    region_name=region,
    config=Config(
        connect_timeout=5,
        read_timeout=30,
        retries={"mode": "standard", "total_max_attempts": 1},
    ),
)

start_ms = int(time.time() * 1000)
result = client.invoke_agent_runtime(
    agentRuntimeArn=runtime_arn,
    qualifier="DEFAULT",
    runtimeSessionId=session_id,
    contentType="application/json",
    accept="application/json",
    payload=json.dumps(payload).encode("utf-8"),
)
body = result["response"].read()
end_ms = int(time.time() * 1000)
print("HTTP:", result["ResponseMetadata"]["HTTPStatusCode"])
print("SDK retries:", result["ResponseMetadata"].get("RetryAttempts"))
print("Runtime session:", result.get("runtimeSessionId"))
print("Body:", body.decode("utf-8"))
print("CloudWatch window (epoch ms):", start_ms, end_ms)
```

`total_max_attempts=1`はBoto3/botocore SDKの再試行を無効にする設定で、Runtime内部の処理回数やモデル呼び出し数を制限するものではありません。送信成功時はHTTP 200とJSON本文が返り、本文には回答`response`とアプリケーションの`request_id`が含まれます。出力されたCloudWatch time windowはUTC epoch millisecondsです。環境・モデル・ログ内容が異なるため、文章や各層の記録数は実行ごとに確認します。

### 3. CloudWatchで同じ時間帯の記録を確認する

呼び出し前後のUTC時刻を記録します。AWS CLIから、Runtime用ロググループ全体を時間窓で検索します。`RUNTIME_ID`、`REGION`、`START_MS`、`END_MS`は、実行対象とUTC時刻から得た値に置き換えます。ミリ秒はUnix epochからの値です。

```sh
aws logs filter-log-events \
  --region <REGION> \
  --log-group-name "/aws/bedrock-agentcore/runtimes/<RUNTIME_ID>-DEFAULT" \
  --start-time <START_MS> \
  --end-time <END_MS> \
  --filter-pattern '"s07_invocation"' \
  --output json
```

結果の`message`内にあるアプリケーション`request_id`、`status`、`time`をSDK応答と照合します。成功時は`status`が`ok`の`s07_invocation`記録を確認します。ログが見つからない場合は時間窓、リージョン、Runtime ID、qualifier、閲覧権限を確認し、見つからないことだけから「呼び出しがなかった」と決めつけません。CloudWatch Logsの検索は複数streamを対象にします。モデルspanやtool記録も調べる場合は、同じ時間窓とtrace/request情報で別々に確認し、SDK retry数と混同しないでください。

この演習の参考実測では、HTTP 200の回答と1件の`s07_invocation`、2件の`chat` span、1件のローカル`inspect_synthetic_runbook`記録を確認しました。このtoolはRuntime内の合成runbookを読み、S04 Gatewayへ問い合わせません。これは一つの限定された実測例であり、別の実行で同じ記録数になる保証ではありません。以前の失敗記録では複数のapp/model記録がありましたが、その原因は特定されていません。

### 4. 結果が不明な場合と後片付け

応答が不明、エラー、予想外の記録になった場合は、追加送信・設定変更をせず停止します。まず送信時刻、Runtime ARN/region/qualifier、session ID、HTTP status、SDK `RetryAttempts`、response bodyまたはSDK error、CloudWatchの時間窓を保全して確認します。SDKの再試行が0でもRuntimeやアプリ内部の処理数を示すとは限りません。

この観察演習はAWS資源やIAM設定を変更しないため、削除作業はありません。共有または授業用Runtime・ログを自分で削除したり、session停止操作をしたりせず、管理者から案内された保持・cleanup手順に従ってください。ログの保持期間と費用はアカウント設定に依存します。セッションの時間制限を、要求数や料金の上限とみなさないでください。

## 参考資料

- [Boto3 `invoke_agent_runtime` API](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore/client/invoke_agent_runtime.html)
- [CloudWatch Logs `filter-log-events` CLI reference](https://docs.aws.amazon.com/cli/latest/reference/logs/filter-log-events.html)
- [Amazon Bedrock AgentCore Runtime HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)
