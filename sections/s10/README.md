# S10: 後片付けと次に学ぶこと

このSectionでは、Course演習のAWS資源を、利用状態と依存関係を確認してから片付けます。実環境や他の演習で使っている資源は対象外です。最後に、実務へ手順を持ち帰る際に再確認する条件を整理します。

## 開始地点と完成地点

- 開始地点: S02–S09で作成・利用したCourse演習資源と、それぞれのSection README・記録。
- 完成地点: 事前のexact inventoryと人間が確認したcleanup plan、dry-runで対象を再照合した結果、依存順に削除した対象と、後続照会で確認した残存理由。
- S03 RuntimeはS03/S04/S06で共有するため、利用・依存が残っていれば保持します。共有S03 artifact bucketを削除する場合は、S03とS04双方のartifact keyを含む完全なinventoryを確認します。
- AgentCoreが作成するruntime-managed bucket、他の演習・予定作業・他利用者の資源、ログイン権限・請求設定は変更しません。
- CloudWatchのcustom metricはこの手順で削除できません。発行元を止め、過去datapointは保持期限に従って残ることを記録します。

## 1. 実行前の確認

Python 3.13、AWS CLI v2、boto3、対象学習accountへの通常の認証sessionが必要です。credentialやsession tokenをplanやコマンドへ貼り付けないでください。AWSを変更しないtestはrepository rootから実行します。

```powershell
uv run --project sections/s02 pytest -q sections/s10/tests
```

AWS CLIでSTS caller accountとregion `ap-northeast-1`を確認します。次の読み取り専用コマンドで、IDと全endpoint、Gateway/target、Lambda/role、専用bucket全version、log groupを記録します。`<...>`は自分のinventoryから得た値に置き換えます。各`--output json`出力を保存し、別演習・予定作業・共有利用がないか人が確認してください。S03 Runtime/bucketはS03/S04/S06共有なので、3 Sectionすべての利用終了と依存を確認するまで対象から外します。AgentCore Runtime APIでsessionsを列挙できない環境では「listが空だからsessionなし」と推測せず、その観測限界を記録します。セッションが見えないことだけを削除可否の根拠にしません。

```powershell
aws sts get-caller-identity --region ap-northeast-1 --output json
aws bedrock-agentcore-control list-agent-runtimes --region ap-northeast-1 --output json
aws bedrock-agentcore-control list-agent-runtime-endpoints --agent-runtime-id <runtime-id> --region ap-northeast-1 --output json
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <runtime-id> --region ap-northeast-1 --output json
aws bedrock-agentcore-control list-gateways --region ap-northeast-1 --output json
aws bedrock-agentcore-control list-gateway-targets --gateway-identifier <gateway-id> --region ap-northeast-1 --output json
aws bedrock-agentcore-control get-gateway-target --gateway-identifier <gateway-id> --target-id <exact-target-id> --region ap-northeast-1 --output json
aws bedrock-agentcore-control get-gateway --gateway-identifier <gateway-id> --region ap-northeast-1 --output json
aws lambda get-function-configuration --function-name <function-name> --region ap-northeast-1 --output json
aws iam get-role --role-name <role-name> --region ap-northeast-1 --output json
aws iam list-attached-role-policies --role-name <role-name> --region ap-northeast-1 --output json
aws iam list-role-policies --role-name <role-name> --region ap-northeast-1 --output json
aws iam list-instance-profiles-for-role --role-name <role-name> --region ap-northeast-1 --output json
aws s3api list-object-versions --bucket <dedicated-course-bucket> --region ap-northeast-1 --output json
aws logs describe-log-groups --log-group-name-prefix <exact-log-group> --region ap-northeast-1 --output json
```

削除する場合は、照合済みのexact inventoryをローカルJSON planへ記録してください。下記は全Course planで使う項目を示すschema例です。各Runtimeに全endpointとrole（live version/statusを含む）、Gatewayに全targetとrole、全Lambdaと専用role、全専用bucketとその全object version/delete marker、対象ロググループ、role、および各roleの`get-role.AssumeRolePolicyDocument`を漏れなく記載します。trust JSON placeholderは実際の返却object全体に置き換えます。値は自分のaccountで取得し、実在しないSection資源は空配列にし、実resource IDやplan外の値を推測で追加しません。

```json
{
  "account_id": "123456789012",
  "region": "ap-northeast-1",
  "runtimes": [
    {"id": "udemy_agentcore_s08-<generated-id>", "endpoints": [{"name": "DEFAULT", "live_version": "2", "status": "READY"}], "role_name": "udemy-agentcore-s08-runtime"}
  ],
  "gateways": [
    {"id": "udemy-agentcore-s04-gateway-<generated-id>", "targets": [{"id": "<exact-target-id>", "name": "udemy-agentcore-s04-cw-target", "target_type": "LAMBDA", "lambda_arn": "arn:aws:lambda:ap-northeast-1:123456789012:function:udemy-agentcore-s04-cw-reader"}], "role_name": "udemy-agentcore-s04-gateway-role"}
  ],
  "lambda_functions": ["udemy-agentcore-s04-cw-reader"],
  "lambda_role_name": "udemy-agentcore-s04-cw-reader-role",
  "buckets": ["udemy-agentcore-s08-apne1-123456789012-<suffix>"],
  "bucket_objects": {
    "udemy-agentcore-s08-apne1-123456789012-<suffix>": [
      {"key": "s08/<exact-key>", "version_id": "<exact-version-id>", "kind": "version", "size": 1234, "etag": "<exact-etag>"},
      {"key": "s08/<exact-key>", "version_id": "<exact-delete-marker-id>", "kind": "delete-marker", "size": 0, "etag": ""}
    ]
  },
  "log_groups": ["/aws/bedrock-agentcore/runtimes/udemy_agentcore_s08-<generated-id>-DEFAULT"],
  "roles": ["udemy-agentcore-s08-runtime", "udemy-agentcore-s04-gateway-role", "udemy-agentcore-s04-cw-reader-role"],
  "role_trust_policies": {
    "udemy-agentcore-s08-runtime": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": "123456789012"}, "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:runtime/udemy_agentcore_s08-<generated-id>"}}}]},
    "udemy-agentcore-s04-gateway-role": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": "123456789012"}, "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:ap-northeast-1:123456789012:gateway/udemy-agentcore-s04-gateway-<generated-id>"}}}]},
    "udemy-agentcore-s04-cw-reader-role": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
  }
}
```

`bucket_objects`は実行前に取得した全object key、全version/delete-marker、`VersionId`、size、ETagを列挙します。`kind`は`version`または`delete-marker`です。空bucketは空listを指定します。ライブ一覧が一件でも追加・欠落・変化していれば最初の削除より前に停止します。S07ではpackageが作成した64桁小文字hexのroot folder markerだけsize=0の通常Versionとして認めます。それも完全一致snapshotに含め、人が確認します。S03 bucketのplanは他Section分も共有しうるため、単にS03 Runtimeを消す目的で入れてはいけません。S03/S04/S06の利用終了を確認した後でも、完全一致しないobject-version一覧ならcleanup scriptは停止します。Lambdaを含める場合は、そのFunctionの専用execution roleもrolesへ追加します。bucket ownerも期待accountと一致しない場合は操作を拒否します。削除直前に外部の書き込みが並行しないよう、発行元を止めてから実行してください。

Gatewayのtarget inventoryでは、`list-gateway-targets`が返す各targetの正確なIDを使って`get-gateway-target`を実行します。各応答の`targetConfiguration.mcp.lambda.lambdaArn`を対応するtargetの`lambda_arn`としてplanへ記録してください。target IDとLambda ARNの対応を推測で補わないでください。

## 2. Dry-runでexact対象を確認する

既定はdry-runです。`--expected-account`と`--region`は毎回明示し、出力された対象と順序をinventoryと突合します。dry-runはAWSを変更しません。

```powershell
uv run --project sections/s02 python sections/s10/cleanup_resources.py `
  --plan .\cleanup-plan.local.json `
  --expected-account 123456789012 `
  --region ap-northeast-1
```

このtoolは、S03/S07/S08/S09用Runtime、S04 Gateway/target/reader Lambda、指定された専用bucket・log group・専用roleだけを許可します。account・regionの不一致、別名、plan外のEndpoint、unexpected S3 key、managed bucket、追加されたIAM policy/boundary/profileを検出した場合は停止し、広い対象へ自動拡張しません。

## 3. 依存順に削除し、同じ実行でabsenceを確かめる

dry-runの内容を人が照合し、対象利用を止めた後に限り、明示的な`--execute`で実行します。APIの状態遷移は待機し、各対象の不在を確認できなければ後続resourceへ進みません。

```powershell
uv run --project sections/s02 python sections/s10/cleanup_resources.py `
  --plan .\cleanup-plan.local.json `
  --expected-account 123456789012 `
  --region ap-northeast-1 `
  --execute
```

順序はRuntime endpoint→Runtime全version、Gateway target→Gateway、reader Lambda、全object versionとdelete marker→専用bucket、log group、最後に専用IAM roleです。Roleにpermissions boundary、attached managed policy、instance profile、想定外inline policyがあれば削除しません。S3 versioning bucketではcurrent keyだけでなく、全ページのすべてのversion/delete markerを明示したVersionIdで削除し、空になったことを照会してからbucketを削除します。現在のplan・resource状態が実行直前に変わっていた場合は、実行を止めて一覧を更新し、別の担当者に対象と依存関係を再確認してもらってください。

このtoolはinvoke、seed、metric送信、AWSログイン基盤の変更、custom metric削除をしません。削除結果が曖昧、別resourceへの依存が判明、timeout、API拒否があれば再試行せず停止し、AWSの現在状態をread-onlyで確認します。削除は同じRuntime ID・endpoint ID・session履歴の復元を保証しません。事前に保存したartifact ZIPは再構築入力であり、Runtime設定、ログ履歴、ID、sessionsのbackupではありません。

## 4. 残存理由を分類して記録する

実行後は次の照会を対象ごとに行い、Runtime/Gateway/Lambdaは`ResourceNotFound`、endpoint/targetは空配列、IAM roleは`NoSuchEntity`を確認します。S3では`Versions`と`DeleteMarkers`が空であり、`list-buckets`にも対象bucketがないこと、Logsでは対象groupが返らないことを確認します。

```powershell
aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id <runtime-id> --region ap-northeast-1 --output json
aws bedrock-agentcore-control list-agent-runtime-endpoints --agent-runtime-id <runtime-id> --region ap-northeast-1 --output json
aws bedrock-agentcore-control get-gateway --gateway-identifier <gateway-id> --region ap-northeast-1 --output json
aws bedrock-agentcore-control list-gateway-targets --gateway-identifier <gateway-id> --region ap-northeast-1 --output json
aws lambda get-function-configuration --function-name <function-name> --region ap-northeast-1 --output json
aws iam get-role --role-name <role-name> --region ap-northeast-1 --output json
aws s3api list-object-versions --bucket <bucket> --region ap-northeast-1 --output json
aws s3api list-buckets --query "Buckets[?Name=='<bucket>'].Name" --region ap-northeast-1 --output json
aws logs describe-log-groups --log-group-name-prefix <exact-log-group> --region ap-northeast-1 --output json
```

残存があれば、失敗/timeoutを含むCLI応答と現在状態を保存し、他資源を追加削除せず原因を調査してください。削除できないcustom metricは再発行源を停止した状態と、datapointのretention見込みを区別して記録します。料金の報告では、請求で観測した額、仮定した見積もり、追加実行の見込みを混ぜません。正確な個別resource savingは、taggableな現在請求根拠がない限り断定しません。

料金は使用量・region・保管期間・無料枠残量で変わります。AgentCore RuntimeはsessionのCPU/memory消費に応じて、GatewayはAPI呼び出し回数に応じて課金されます。公開料金を実行日・regionで確認してください: [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/), [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/), [S3 pricing](https://aws.amazon.com/s3/pricing/)。

## 5. 次に学ぶこと

次のテーマは仕事で解きたい課題から選びます。実行と接続ならRuntime・認証、障害調査ならLogs・metrics・traces、運用化なら権限・保持・費用・復元手順が候補です。演習で使ったID、権限、データ、結果をそのまま実務環境の事実として扱わず、対象と時間範囲を改めて確認します。
