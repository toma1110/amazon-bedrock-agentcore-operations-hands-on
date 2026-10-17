# S07: エージェントの失敗を調べて復旧する（草案）

S07は、失敗した箇所を記録から調べる講座の草案です。下記のローカル演習は合成データと`127.0.0.1`上の一時APIスタブだけを使います。AWSで限られた確認を行いましたが、通常の一回の送信が常に一件のアプリ処理になることや、失敗処理を安全に再実行できることは確認できていません。このREADMEやコードをAWSの実行手順として使用しないでください。

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

### 限定的な実証メモ（完成した実行手順ではありません）

- 一回の正常なSDK要求（SDKの`RetryAttempts`は0）では、アプリ要求1件、モデルの`chat`記録2件、ローカルの合成runbook呼出し1件を観測しました。runbookはGatewayではありません。この一回の観測は、以後の要求も同じ多重度になる保証ではありません。
- 以前の一時的な明示Deny試験では、Test UIの一操作に対し8件の異なるRuntimeアプリ要求記録が残り、内訳は`AccessDeniedException` 7件と`EventLoopException` 1件でした。UIの429表示やこの混在したログから、単一のIAM拒否だけが起きた、または一操作が一要求だったとは説明できません。
- その後の固定した無効モデル設定での一回のSDK送信も、SDK再試行0でしたが、ログにはアプリ要求8件・モデル`chat`記録8件・ツール記録0件が残りました。Bedrockの`ValidationException` / HTTP 400、アプリ例外処理が返すコンテナHTTP 502、Runtime APIのHTTP 424 `RuntimeClientError`とその内側で報告されたRuntime 429は、別の層の結果です。コンテナの502は確認したコード経路による値で、保存済み呼出し本文から直接採取した値ではありません。HTTP契約上の424はコンテナが4xx/5xxを返した場合のRuntimeエラーですが、内部要求数や増殖原因を説明するものではありません。
- SDKの`RetryAttempts=0`はSDK自動再試行が記録されなかったという意味に限られ、Runtime内部やアプリ内の再配信・処理数の上限を示しません。実装の最大8回カウンターはプロセス内の値で、全体のhard capではありません。失敗探索の追加送信・再実行は停止しています。
- 無効モデル設定を除いた後、元の2つのGateway環境変数へ戻した新しいV5 / DEFAULTがREADYになりましたが、復元後の正常な機能確認はしていません。READYは正常応答の証拠ではありません。費用やセッション終了・課金ゼロも確認済みではありません。

したがってS07全体、AWSハンズオン、復元後の正常確認、SectionのUdemy反映は未完了です。講義の事実と手順を再整合するまでは、これを完成した調査手順や教材上の合格例に使わないでください。Runtime V2のsnapshot資料は乱数の状態が復元後のinstanceで同じ系列になり得る一般的な注意を説明していますが、今回の識別子反復の原因を確定する証拠ではありません。原因や未確認の処理数を推測せず、未実装・未実証の修正案も実施済みとして扱いません。

参照するAWS一次資料: [Runtime HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)（HTTP応答の層を区別するため）、[Runtime V2 optimization](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html)（snapshot復元時の乱数に関する一般的な注意）。いずれも今回の内部処理数・原因・費用を保証する資料ではありません。

AWS環境での実行、ログ・トレースの完全な確認、復元後の機能確認、復旧手順、後片付けは未完了です。
