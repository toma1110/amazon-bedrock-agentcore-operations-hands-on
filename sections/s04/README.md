# S04: S03 Runtimeの実行記録を限定して読む

S03でエージェントを実行したあと、その実行のinvocation IDと時刻を使って、statusとtool callの要約を読みます。読む処理は専用LambdaとAgentCore Gatewayを通します。別のS04 RuntimeがGateway toolを呼びます。S03 Runtimeやその設定は変更しません。

## この演習で行うこと

1. S03の実行結果から、実行に対応する`invocation_id`と`timestamp_ms`を控えます。
2. S03と同じS3 artifactを参照する新しいS04 Runtimeを作り、READY後にS04 reader artifactへ更新します。
3. S03で控えた値を使ってreaderを一度呼び、見つかったstatusとtool情報をS03の出力と照合します。
4. S04 Runtimeの専用sessionを停止し、Gatewayへ署名なし要求と、有効なS03 invocation UUIDに対する8日前timestampの署名付き時刻範囲外要求を各1回送ります。
5. 利用後に、このrunが作成したresourceと2つのpackage objectだけを削除し、残存を確認します。

## 前提と安全

- 作業はAWS CloudShellで行います。AWS CLI v2、Python 3.13、`uv`、Git、東京Region `ap-northeast-1`を使います。これらが利用できない場合は実行を止め、環境管理者へ確認してください。Python package、access key、secret、session tokenをfileやcommandへ貼り付けません。
- S03を先に完了し、S03 Runtimeが`READY`であることと、S03 READMEの実行・ログ確認手順を完了してください。S03のlocal state `.agentcore-s03-state.json`が必要です。
- S03 Runtime ID、log group、Region、local stateのaccount IDと、CloudShellのAWS caller accountが一致することを開始前に確認します。S03 log groupは`/aws/bedrock-agentcore/runtimes/<Runtime ID>-DEFAULT`です。
- このWorkflowは、既存S03 Runtimeのartifact objectをread-onlyで参照します。正確なbucket/key/hashがS03 stateと一致しなければ停止します。実行時roleに許可するS3 readはそのobjectだけです。既存S03 bucket、Runtime、role、environment、log groupは変更せず、S03 artifact objectをcleanupしません。
- `run_id`はこの実行で使う新しい1〜20文字の小文字英数字・hyphenです。AWS上で一致するnameや、そのrun prefixのS3 objectが一つでもあれば別の`run_id`にします。各AWS resource nameはこのIDから生成されます。
- Runtime、Gateway、Lambda、CloudWatch Logs、S3、Bedrock modelには利用料が発生します。実行前にAWS Pricing Calculatorで想定利用時間・回数を見積もり、呼出しを必要最小限にしてください。見積もりは費用上限ではありません。この手順はreader Runtime invokeを1回に制限し、model requestは実装上1 invocationあたり最大2回・各最大512 output tokensです。negative工程はGatewayへ署名なしinitializeと署名付き時刻範囲外tools/callを各1回、直接送ります。readerの論理tool call数とMCP wire request数は分けて記録し、観測前にwire request総数の上限を置きません。
- shared S3 bucketのversioningがEnabledまたはSuspendedの場合はこの手順を実行しません。bucket設定は変更せず、対象外object versionを残したままになります。
- 最初のwrite前に、確認済みaccount、Region、S03 Runtimeとartifact、candidate ZIP hash、run専用resource名、料金見込みを確認します。想定外の状態、権限不足、resource衝突、失敗または曖昧な応答では停止します。writeをそのまま再送しません。

## 1. 固定版を取得し、CloudShellの前提を確認する

Courseで指定された固定commitをcheckoutします。repositoryの最新`main`へ自動追従させないでください。

```bash
git clone https://github.com/toma1110/amazon-bedrock-agentcore-operations-hands-on.git
cd amazon-bedrock-agentcore-operations-hands-on
git checkout COURSE_COMMIT_SHA
python3 --version
uv --version
aws --version
```

`COURSE_COMMIT_SHA`をCourseで指定された実際のcommit SHAに置き換えてから実行してください。`uv`が使えない場合は続行せず、環境管理者へ確認します。S02の固定依存を使うため、repository rootから以下を実行します。

```bash
uv run --project sections/s02 python --version
aws sts get-caller-identity --query Account --output text
```

表示されたAWS account IDを、以後の`EXPECTED_ACCOUNT`へ設定します。`AWS_PROFILE`を使う場合は有効なprofile名を設定します。CloudShell sessionは一時的なので、コマンド結果や生成ZIPを必要な間は保持してください。

## 2. S03の値と今回のrun IDを準備する

S03のlocal stateからRuntime ID、artifact bucket/key、SHA-256を読みます。このfileにはresource情報が含まれるため、内容全体を公開したりIssueへ貼り付けたりしません。

```bash
export RUN_ID=s04-reader-01
export EXPECTED_ACCOUNT=123456789012
export EXPECTED_REGION=ap-northeast-1
export S03_RUNTIME_ID="$(python3 -c 'import json; print(json.load(open("sections/s03/.agentcore-s03-state.json"))["runtime_id"])')"
export S03_LOG_GROUP="/aws/bedrock-agentcore/runtimes/${S03_RUNTIME_ID}-DEFAULT"
export S03_BUCKET="$(python3 -c 'import json; print(json.load(open("sections/s03/.agentcore-s03-state.json"))["bucket"])')"
export S03_ARTIFACT_KEY="$(python3 -c 'import json; print(json.load(open("sections/s03/.agentcore-s03-state.json"))["object_key"])')"
export S03_ARTIFACT_SHA256="$(python3 -c 'import json; print(json.load(open("sections/s03/.agentcore-s03-state.json"))["zip_sha256"])')"
```

`RUN_ID`と`EXPECTED_ACCOUNT`を自分の値に置き換えます。値を確認します。

```bash
printf 'run=%s account=%s region=%s s03=%s\n' "$RUN_ID" "$EXPECTED_ACCOUNT" "$EXPECTED_REGION" "$S03_RUNTIME_ID"
aws sts get-caller-identity --query Account --output text
```

両方のaccount IDが一致し、S03の`logs`出力に同一runの`invocation_id`と`timestamp_ms`があることを確認します。値を推測したり別runから組み合わせたりしません。readerは最大7日以内の記録を検索します。

## 3. codeとpackageを確認する

以下はAWSへ接続しないunit testです。合格してもAWSの権限・resource状態・配備成功は証明しません。

```bash
uv run --project sections/s02 pytest sections/s03/tests sections/s04/tests -q
```

今回のrun用に新しいreader Lambda/Runtime ZIPを作ります。既存出力を上書きしません。

```bash
uv run --project sections/s02 python sections/s04/package_artifacts.py \
  --profile s03-reader --run-id "$RUN_ID"
export PACKAGE_DIR="sections/s04/build/s03-reader-${RUN_ID}"
export LAMBDA_ZIP="$(find "$PACKAGE_DIR" -maxdepth 1 -type f -name 'lambda-*.zip' -print -quit)"
export READER_RUNTIME_ZIP="$(find "$PACKAGE_DIR" -maxdepth 1 -type f -name 'runtime-*.zip' -print -quit)"
test -n "$LAMBDA_ZIP" && test -n "$READER_RUNTIME_ZIP"
sha256sum "$LAMBDA_ZIP" "$READER_RUNTIME_ZIP"
unzip -l "$LAMBDA_ZIP"
unzip -l "$READER_RUNTIME_ZIP"
```

ZIP内fileとhashを確認し、READMEの想定と異なる場合は先へ進みません。ZIPはdeployとcleanup確認が終わるまで保持します。

## 4. read-only preflightとplan previewを確認する

PreflightはCaller account、S03 RuntimeのREADY/version/configuration、S03 artifact object key/hash、Region、run-specific resource name、S3 prefixをread-onlyで確認し、run別stateを保存します。既存nameやprefixと衝突した場合は何も作成せず停止します。

```bash
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py preflight \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION" \
  --s03-log-group "$S03_LOG_GROUP"
```

必要ならJSON plan previewも読み合わせます。これはAWS clientを作らず、AWSへ接続・書込みしません。

```bash
uv run --project sections/s02 python sections/s04/s03_reader_deployment_plan.py \
  --account "$EXPECTED_ACCOUNT" --run-id "$RUN_ID" --bucket "$S03_BUCKET" \
  --s03-log-group "$S03_LOG_GROUP" --s03-artifact-key "$S03_ARTIFACT_KEY" \
  --s03-artifact-sha256 "$S03_ARTIFACT_SHA256" \
  --lambda-zip "$LAMBDA_ZIP" --runtime-zip "$READER_RUNTIME_ZIP"
```

JSONのborrowed S03 artifact、package SHA-256、resource名、S3 prefix、2段階Runtime作成・更新、cleanup対象を確認します。previewやunit testだけをAWS実行成功として扱いません。

## 5. reader resourceを作成し、状態を確認する

以下は実際にAWSへresourceを作成・更新するコマンドです。実行前にaccount、region、料金、requestの対象、cleanup手順をもう一度確認します。最初の新RuntimeはS03の既存artifact objectを参照して作り、READYを確認してから、同じ新Runtimeをreader ZIPと専用Gateway URL/tool nameへ更新します。Gatewayの外部tool名は`<target-name>___read_s03_invocation`です。Runtime内のStrands tool名は`read_s03_invocation`で、この外部名を使ってGateway toolを呼びます。S03 Runtimeそのものには更新APIを送りません。

```bash
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py deploy \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION" \
  --reader-lambda-zip "$LAMBDA_ZIP" --reader-runtime-zip "$READER_RUNTIME_ZIP"
```

Workflowは各writeのrequestを送る前にlocal stateへ記録し、boto3 write retryを1回に制限します。S3 package object、専用Lambda/Gateway/Runtime、3つの専用roleは`run_id`から生成され、ownership tagとresource ARNで追跡します。Runtime更新時はnetwork、protocol、role、MMDSv2、lifecycleを現在値から引き継ぎ、更新後にREADYとreadbackを確認します。

Runtime実行roleはAgentCoreのlog setupに必要な`logs:PutResourcePolicy`を持ちます。ResourceはこのrunのRuntime名に続く`/aws/bedrock-agentcore/runtimes/<run-runtime-name>-*`のlog-group ARN prefixだけです。account全体のresource policy変更権限にはしません。AWS公式のRuntime実行role例も、`PutResourcePolicy`をRuntime名prefixのlog group ARNへ限定しています。

Runtime実行roleには`logs:DescribeLogGroups`もあります。Resourceは`*`ですが、`aws:RequestedRegion`を`ap-northeast-1`に限定します。この権限でlog groupの一覧を確認できますが、log eventの読取権限は付与しません。

状態は次で読み戻します。`status`は専用resource、Lambda log group、package objectの現在状態とhash、bucket versioningを読み取ります。Runtimeが作る`DEFAULT` log groupと、そのexact log group ARNへscopeされたCloudWatch Logs resource policyも一覧に含めます。Enabled/Suspendedなら過去versionの残存は未確認として表示されます。

```bash
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py status \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION"
```

write応答が不確定になった場合、そのwriteを再送しません。`resume`はrequest ID/name/package hash/tagをprovider stateとread-only照合し、確実に一致した結果だけstateへ保存します。識別できなければstateとresourceを保持して停止し、講師へ状況を共有してください。

```bash
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py resume \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION"
```

初回のLambda作成前には、専用execution roleの作成から最低10秒経過するまで待ち、role ARN・ownership tag・trust・policyを再確認します。IAMの伝播完了を保証する待ち時間ではありません。Lambda作成が`InvalidParameterValueException`で拒否され、CloudTrailに「The role defined for the function cannot be assumed by Lambda.」と記録された場合だけ、`recover-lambda-create`で拒否を確認できます。この読み取り専用コマンドは、指定したrequest IDと時刻の前後2分だけを検索し、CloudTrail eventを保存済みwrite時刻の前後2分以内に限定したうえで、account・region・function名・role ARN・errorを照合します。stateにSDK request IDがあればそれも一致させます。さらにLambdaが存在しないことと、専用roleのARN・ownership tag・trustを再確認します。CloudTrail結果が200件を超える場合、該当eventが一意でない場合、または照合項目が一つでも異なる場合はstateを変えません。

CloudTrail証跡のrequest IDとevent時刻を確認してから実行します。実行者にはCloudTrailの`LookupEvents` read権限が必要です。

```bash
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py recover-lambda-create \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION" \
  --request-id CLOUDTRAIL_REQUEST_ID --event-time '2026-01-02T03:04:05Z'
```

確認が完了した後に同じ`deploy`を一度実行すると、保存済みZIPとS3 object hash・size、Lambda不在、role ARN・ownership tag・trustを再確認し、短い待機後に同じ項目を再確認してLambda作成を一度だけ再試行します。retry結果が不明なら再試行しません。別のerror、CloudTrail証拠がないstate、再試行済みのstateはこの経路の対象外です。原因不明の作成失敗を自動再送しないでください。

Gatewayの作成が終わる前にtargetを作ると拒否されることがあります。deployはGatewayが`READY`になり、設定と専用roleのtrustが一致することを確かめてからtargetを作ります。

この既知の拒否が起きた場合は、表示されたCloudTrail request IDと時刻で次の読み取り専用確認を行います。元のrequestとの照合、Gatewayの所有・設定・`READY`、target一覧の全ページが空であることを確認できた場合だけ、再開できます。

```bash
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py recover-gateway-target \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION" \
  --request-id CLOUDTRAIL_REQUEST_ID --event-time '2026-01-02T03:04:05Z'
```

CloudTrail確認後は、同じ`deploy`を一度実行します。元のtarget作成記録を残したまま、保存した同一requestで一度だけ試します。結果が不明、またはattemptを記録した後は再送せず、`status`で状態を確認して担当者へ共有してください。targetが既にある、照合情報が合わない場合も、resourceやstateを変更せずに停止します。

CreateAgentRuntimeは`metadataConfiguration`を受け付けず、UpdateAgentRuntimeは受け付けます。作成時にSDKが`metadataConfiguration`を拒否した場合、その呼び出しはHTTPへ送信されていないため、CloudTrail request IDはありません。`recover-runtime-create-validation`は保存requestでbotocoreの同じローカル検証エラーを再現し、`metadataConfiguration`だけを除いたrequestがCreate APIモデルで有効なこと、既存のLambda/Gateway targetとroleが一致すること、全ページのRuntime一覧に完全一致する名前がないことを読み取り専用で確認します。続く`deploy`はGateway target/Lambda作成を再実行せず、保存済みの元requestと同じclient tokenを保ったまま、metadata項目だけを除いたCreateを1回だけ送信します。MMDSv2はRuntime READY後、UpdateAgentRuntimeの更新requestで指定します。不明な応答やattempt記録後は再送せず停止します。

```bash
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py recover-runtime-create-validation \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION"
```

確認後、section 5の同じ`deploy`を1回実行します。既存のLambda ZIP、Runtime ZIPと保存済みpackage hashを使います。

## 6. S03ログを一度読み、専用sessionを停止する

S03実行の正確な`invocation_id`と`timestamp_ms`をS03のログ出力からコピーします。`invoke`は指定したrunのreader Runtimeを一度だけ呼び、戻ったJSONをlocal記録へ保存します。`found=false`は記録が見つからなかった意味だけで、Runtime healthや実行不存在の証明ではありません。不完全検索では不在と結論しません。

```bash
export TARGET_INVOCATION_ID="PASTE_S03_INVOCATION_ID"
export TARGET_TIMESTAMP_MS="PASTE_S03_TIMESTAMP_MS"
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py invoke \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION" \
  --invocation-id "$TARGET_INVOCATION_ID" --timestamp-ms "$TARGET_TIMESTAMP_MS"
```

`PASTE_S03_INVOCATION_ID`と`PASTE_S03_TIMESTAMP_MS`は、S03の同じ実行出力にある実値へ置き換えてから実行してください。角括弧のplaceholderをBashへそのまま貼り付けないでください。

出力の`status`、`tool_call_count`、`tool_names`を同じS03 runと照合します。S04 Runtime invocation ID、Lambda request ID、対象S03 invocation IDは別の値です。結果を保存してから、このrunのS04 Runtime専用sessionだけを停止します。

```bash
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py stop \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION"
```

StopRuntimeSessionのHTTP応答statusが200で返ったことを確認します。停止結果が不明なら同じsessionへのstopを再送しません。

## 7. Gateway境界を2回だけ確認する

`negative`はS04 Runtime/modelを呼ばず、専用Gatewayへ以下を各1回送信します。実応答statusとtool errorをそのまま保存・確認します。特定のHTTP statusを事前の成功条件に決めず、応答が不明なら再送しません。

- 署名なしMCP initialize要求
- AWS IAM SigV4署名付き`tools/call`で、`<target-name>___read_s03_invocation`へ正例と同じ有効な実行IDと、現在UTCより8日前の時刻を1回送信（取得上限1件）

2件目はreaderの「直近7日以内」という取得範囲を外す試験です。8日前の時刻はこの負例のために作った入力で、S03の実際の実行時刻ではありません。Workflowは元の正例の時刻と負例の送信内容を別々に保存します。実行IDの形式を壊す試験とは区別してください。

`target-name`は今回のrunのGateway target名です。Workflowはこの外部tool名をRuntime設定と直接Gateway要求へ使います。Runtime側のStrands wrapper名`read_s03_invocation`とは異なります。

```bash
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py negative \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION"
```

reader codeはUUID形式と時刻の範囲をLambda内のLogs client生成より先に検査します。AWS側でどの境界が拒否したかは、実際のGateway応答と観測できる情報から判断します。Lambda logやCloudWatch logに記録がないことだけで`FilterLogEvents`未呼出しと断定しません。署名なし要求の拒否と、認証済み要求に対するreaderの時刻範囲の検査は、別の境界です。

## 8. cleanupと残存確認

学習が終わり、このrun専用resourceが他のSectionで不要になったことを確認したら、保存stateからownershipを確認してcleanupします。cleanup前にsaved S04 sessionの停止を確認し、Runtime/Gateway/LambdaのARNやtag、package hashがrunの保存値に一致することを確認します。shared S3 bucketのversioningも再確認します。

Runtimeのexact IDから`/aws/bedrock-agentcore/runtimes/<Runtime ID>-DEFAULT`とそのlog-group ARNを作り、`DescribeResourcePolicies(policyScope=RESOURCE, resourceArn=<exact ARN>)`でpolicyを照合します。policyが見つかったらname、revision ID、document、resource ARNをstateへ保存します。複数件、scope/ARN不一致、またはname/revision/documentの欠落・保存後の変更があれば、削除前に停止します。

削除は次の順で行い、各項目が不在になったことをread-onlyで確認します。

1. run所有のS04 Runtimeを削除し、Runtime IDが不在になったことを確認します。
2. stateへ保存したexact resource-scoped policyだけを`expectedRevisionId`付きで削除し、同じpolicy nameとlog-group ARNの組み合わせが不在になったことを確認します。
3. exact `/aws/bedrock-agentcore/runtimes/<Runtime ID>-DEFAULT` log groupを削除し、不在になったことを確認します。

Runtimeを削除するとAgentCoreが自動作成したWorkload identityも削除されるため、identityへ別のdelete requestは送りません。policyまたはlog groupが既に不在なら、read-only照合して不在を記録し、delete APIを送りません。

その他のcleanup対象はGateway target/Gateway、Lambda、専用roles/policies、Lambda log group、今回uploadした2つのS3 package objectだけです。S03 Runtime/role/log group、S03 artifact object、shared bucket自体、account scopeのLogs policy、他のrunは削除しません。

```bash
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py cleanup \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION"
uv run --project sections/s02 python sections/s04/s03_reader_workflow.py status \
  --run-id "$RUN_ID" --expected-account "$EXPECTED_ACCOUNT" --expected-region "$EXPECTED_REGION"
```

Provider deletionが非同期の場合は削除後のread-only確認が終わるまで待ちます。statusが専用Runtime/Gateway/Lambda/log group/package objectと保存済みresource-scoped policyを不在と示すことを確認します。S3のhead確認はcurrent objectだけを観測します。bucket versioningがEnabled/Suspendedへ変化した場合は過去versionが残る可能性を記録し、S3 cleanup完了とは扱わず、bucket設定は変更しません。state、ZIP、失敗resourceを確認前に削除しません。S03や別Sectionのresourceをこのrunのcleanupへ追加しません。

## 想定と異なる場合

- Account/Region/Runtime/log group/hash/name/prefixが一致しない: write前に止め、正しいprofile/state/run IDを確認します。
- `AccessDenied`: Caller、Lambda、Gateway、Runtimeのどの主体で拒否されたかを分け、actionとexact resourceを確認します。権限を広げません。
- Lambda/Gateway/RuntimeがREADYにならない: status、failure reason、request IDと観測できたlogを保存します。generic errorだけで根因を断定しません。
- `found=false`: region、7日以内の時刻、invocation ID、検索完了状態を確認します。S03 Runtime healthを推測しません。
- write/Invoke/Stop/negativeの応答が不明: local stateを保持し、同じ要求を再送せず`resume`とread-only確認を行います。
- cleanupでownership tag/hash/ARNが一致しない: すべての削除を停止し、対象resourceを個別に確認します。
- S3 bucket versioningがEnabled/Suspendedへ変わった: objectを削除せず停止します。statusは過去versionの残存を未確認として示します。

## AWS公式資料

- [AgentCore Gateway Lambda target](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-api-target-config.html)
- [CreateAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateAgentRuntime.html)
- [UpdateAgentRuntime API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_UpdateAgentRuntime.html)
- [Runtime lifecycle settings](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html)
- [Runtime execution role permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
- [CloudWatch Logs DescribeResourcePolicies API](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_DescribeResourcePolicies.html)
- [CloudWatch Logs DeleteResourcePolicy API](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_DeleteResourcePolicy.html)
- [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [Lambda pricing](https://aws.amazon.com/lambda/pricing/)
- [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)
