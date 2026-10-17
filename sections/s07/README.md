# S07: エージェントの失敗を調べて復旧する（草案）

S07では、エージェントのツール、モデル設定、API応答に起きた問題を識別し、安全な範囲で原因を調べて復旧する方法を学ぶ予定です。下記のローカル演習は合成データと`127.0.0.1`上の一時APIスタブだけを使います。AWS上のハンズオンは未実証なので、このREADMEやコードをAWSの実行手順として使用しないでください。

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

## AWSハンズオン

AWS環境での実行、ログ・トレースの確認、復旧、後片付けの手順は、動作確認が完了するまで提供していません。
