# S04: S03 invocation logをUUIDで限定して読む

このレッスンでは、S03 Runtime invocation logをUUIDと時刻で絞って確認する方法を扱います。以前のsynthetic CloudWatch演習は[保存されたS04演習README](legacy-synthetic-README.md)にあります。S06のsynthetic後続演習はそちらを参照してください。

## 旧S04 READMEリンクの互換入口

既存の[S04 Gatewayと合成データ](../s04/README.md)リンクから来た場合、S06のsynthetic演習に必要なセットアップ・データ作成・実行確認・停止/復旧手順は[保存されたS04演習README](legacy-synthetic-README.md)を参照してください。

## 目的と境界

既存S03 Runtime invocationのlogsに、`target_invocation_id`と`target_timestamp_ms`が表示されている場合、そのUUIDと時刻だけを使って対応する`agent_invocation`ログ要約を読みます。S03 invocation、S04 reader Runtime invocation、reader Lambda executionは別ID・別処理です。log検索結果は実行結果の観測であり、runtime healthや原因、復旧を保証しません。

この演習のreaderはS03のRuntime、Gateway、Lambda、execution role、environmentを変更しません。新しいS04 Runtime、専用Gateway/target、専用reader Lambdaと、それらの専用role/log groupを使います。読取対象は既存S03 log groupの該当 invocation summaryのみです。

## 現在の候補コードと固定契約

- `s03_invocation_reader.py`: 東京`ap-northeast-1`のS03 log group `/aws/bedrock-agentcore/runtimes/udemy_agentcore_s03-RsrJEUHMQ7-DEFAULT`だけを`FilterLogEvents`で読む。UUID完全一致、7日以内、target timestampの5分窓、最大3ページ、最大5 records。
- 結果は`event`, target UUID/timestamp, status, tool-call count, bounded tool namesだけ。raw log message、prompt、answer、argumentsは返さない。最大5 names / 各128 UTF-8 bytes、損失は`tool_names_truncated=true`で明示。`data` JSON最大4,096 bytes。
- `s04_gateway_tools.py`: `read_s03_invocation` toolを公開。1 Runtime invocationにつきGateway wire request最大1回。引数をlocalでvalidateし、不正/期限外ならAWS call前にreject。Gateway応答は8,192 bytesを超えたらfail closed。
- `s04_runtime_app.py`: S04専用HTTP entrypoint/prompt。既存lock済S02 dependency、`amazon.nova-lite-v1:0`、Tokyo model設定を利用。tool name等のログ由来テキストはuntrusted dataとして扱う。Agentは最大2 turns、512 output tokens/request、実装上の `total_tokens=2,048` limit（request-byte guardとは別）。モデル送信は1 logical testにつき最大2 request / 各32,768 bytes、Botocore retryなし。毎回request開始時に両budgetをresetする。
- Runtime log recordは`invocation_id`をS04側で発行し、reader outputは別の`reader_execution.lambda_request_id`を返す。どちらもtarget S03 UUIDとは混同しない。

## Testとhash package

repository rootからS03/S04のunit testsを実行します。これはAWSの実権限やlive動作を検証するものではありません。

```bash
uv run --project sections/s02 pytest sections/s03/tests sections/s04/tests -q
```

Reader packageはfresh run IDごとに、既存directoryがないことを確認して作ります。Directoryは上書きされず、artifact名にSHA-256 prefix、builder出力にfull SHA-256を表示します。S03 reader Lambda ZIPには`s03_invocation_reader.py`だけ、Runtime ZIPにはlock済S02 depsと下記を含めます。

```bash
uv run --project sections/s02 python sections/s04/package_artifacts.py \
  --profile s03-reader --run-id FRESH_RUN_ID
```

Runtime artifactのexact module inputs: `s04_runtime_app.py` as `runtime_app.py`, `s04_gateway_tools.py` as `s04_gateway_tools.py`, `s03_invocation_reader.py`, S02 `agent_app.py`, S02 `runbooks.json`, and the locked dependency set from S02. The Lambda package contains only `s03_invocation_reader.py`; its handler is `s03_invocation_reader.lambda_handler`, runtime `python3.13`, architecture `x86_64`. Confirm ZIP integrity, archive member names, and record both full SHA-256 values before any upload.

### Action-time request plan (does not execute AWS calls)

After packages exist, generate a read-only review plan with the exact account, a fresh run ID, existing artifact bucket, and both local ZIP paths. The IAM identity running the upload and `CreateAgentRuntime` needs `s3:PutObject` for the unique new keys and `s3:GetObject` for the exact artifact objects; the latter is a caller permission, not a Runtime execution-role permission. Verify these pre-existing permissions before setup; do not broaden or edit the caller role.

```bash
uv run --project sections/s02 python sections/s04/s03_reader_deployment_plan.py \
  --account 123456789012 --run-id FRESH_RUN \
  --bucket EXISTING_VERIFIED_ARTIFACT_BUCKET \
  --lambda-zip sections/s04/build/s03-reader-FRESH_RUN/lambda-FULLSHA.zip \
  --runtime-zip sections/s04/build/s03-reader-FRESH_RUN/runtime-FULLSHA.zip
```

Replace every example value with the exact values verified for your AWS account and Region. This tool only hashes local artifacts and prints JSON; it has no AWS client and performs no network/write operation. Its `read_only_preflight` entries describe checks for the caller account/Region, current S03 and legacy S04 state, exact-name availability, and unique S3 keys. The `writes` list contains ordered request data with returned-ID placeholders; save every successful response ID/ARN and substitute only that returned value into the next request. Verify role policies, targets, endpoints, log groups, object hashes, and Runtime state before any model invocation. Do not use the legacy synthetic deployment workflow for this separate reader. Review the plan, expected cost, and rollback path with the course owner before applying it. Never retry a write with an uncertain outcome; reconcile by exact name/ID using read-only APIs, and stop if the result is not uniquely attributable to this run.

## AWS resource setup

Before setup, confirm the AWS account and Tokyo Region, inspect the existing S03 Runtime/log group and legacy S04 resources, verify the artifact bucket and exact resource-name availability, and review current prices and rollback steps. Do not reuse an existing role or target.

The request plan names a dedicated Runtime, Gateway, Lambda, and separate execution roles. Use only names shown by the reviewed plan, and stop if a name is already in use.

The request plan orders the minimum resource changes:

1. Upload each ZIP with `s3:PutObject` to a unique run prefix and include the conditional `If-None-Match: *` header (`IfNoneMatch="*"` in the SDK request plan). This prevents an existing object from being replaced even if the preflight check races with another writer. Preserve the source SHA in the run record.
2. Create only the new Lambda log group with 7-day retention, and create the Lambda using Python 3.13, handler `s03_invocation_reader.lambda_handler`, x86_64, 512 MB, 30-second timeout. Its execution role trust is Lambda only. Its policy allows `logs:FilterLogEvents` on the exact S03 log group ARN ending `:*`, plus write-only log stream actions on the new Lambda log group; no S03 writes, no other log groups, no `DescribeLogGroups`, no metrics, and no secrets.
3. Create a dedicated Gateway role trusted only by `bedrock-agentcore.amazonaws.com` and the current account. Create the new Gateway with `protocolType="MCP"`, `authorizerType="AWS_IAM"`, and that role. After AWS returns its generated ARN, tighten the role trust `aws:SourceArn` to the exact new Gateway ARN before adding the target. Its identity policy allows `lambda:InvokeFunction` on only the new Lambda ARN; same-account resource policy on Lambda is not required.
4. Create a single Lambda target with `create_gateway_target` using `credentialProviderType="GATEWAY_IAM_ROLE"`, and an inline schema containing only `read_s03_invocation` with exactly `target_invocation_id` (string), `target_timestamp_ms` (integer), and `max_results` (integer 1–5). Confirm the resulting exposed tool name/endpoint from Get/List output; place those exact values in the new Runtime environment. Do not update the existing S04 Gateway or its target.
5. Create a separate Runtime role. Trust is AgentCore Runtime with account condition and only the new Runtime ARN/name prefix. Application permissions are `bedrock-agentcore:InvokeGateway` for the exact new Gateway ARN; `bedrock:InvokeModel` for `arn:aws:bedrock:ap-northeast-1::foundation-model/amazon.nova-lite-v1:0`; and standard stream logging actions scoped to the new Runtime log-group prefix. `logs:CreateLogGroup` and `logs:DescribeLogStreams` are likewise scoped only to that prefix. The Runtime ID is generated by AWS, so its exact log group can only be checked/precreated after `create_agent_runtime`; first read it back, create it only if absent, then set 7-day retention. Do not copy the broad example role or grant `logs:PutResourcePolicy` / account-wide logging access: those actions are not used by this application and could affect shared logging. Review the current [Runtime execution-role documentation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html) at action time; if the platform requires permissions broader than this new prefix, pause for course-owner review instead of expanding them.
6. Upload the dedicated Runtime artifact; call Boto3 `bedrock-agentcore-control.create_agent_runtime` using name above, `platformVersion="V2"`, `networkConfiguration={"networkMode":"PUBLIC"}`, `protocolConfiguration={"serverProtocol":"HTTP"}`, code `runtime="PYTHON_3_13"`, `entryPoint=["runtime_app.py"]`, the exact new S3 object key, and environment variables `S04_GATEWAY_URL` and `S04_GATEWAY_TOOL_NAME`. Set explicit `lifecycleConfiguration={"idleRuntimeSessionTimeout":900,"maxLifetime":28800}` (15-minute idle termination, 8-hour max microVM lifetime). Save the returned Runtime ID/ARN, tighten trust to its exact ARN, discover its exact log-group name, create that group only if absent, and set 7-day retention. Verify READY and role/Gateway/Lambda wiring read-only before any model invocation.

### Runtime failure and verification boundary

Do not invoke the Runtime or send a model request until `get_agent_runtime` reports `READY`. A separately signed Gateway-to-Lambda call can verify the target and reader against one exact S03 event, but it does not verify Runtime startup, its model access, or its user-facing response. If Runtime creation or update reaches a failed state, preserve its exact status, failure reason, request ID/time, and available Runtime logs. A generic service error with empty Runtime logs does not establish a root cause; do not infer one, add permissions, or repeat the write. Escalate the request ID for AWS-side diagnosis. Keep the deployment outcome marked incomplete until Runtime readiness and an end-to-end Runtime invocation are verified.

For the Lambda target, AWS Boto3 request shape is documented in [AgentCore Gateway Lambda target docs](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-add-target-api-target-config.html). Runtime code and lifecycle fields are described in [CreateAgentRuntime Boto3 API](https://docs.aws.amazon.com/boto3/latest/reference/services/bedrock-agentcore-control/client/create_agent_runtime.html) and [Runtime lifecycle settings](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html).

### Cost assumptions (estimate, not cap)

For planning, one reader workflow is estimated at about US$0.10 assuming 15 minutes at full 2-vCPU/8-GB allocation: Runtime $0.0976 ($0.1276/vCPU-hour + $0.0169/GB-hour), Lambda $0.000250 compute ($0.0000166667/GB-second) + $0.0000002 request ($0.20/million), Gateway $0.000005 for one tool API request ($0.005/1,000; 20-request planning allowance is $0.0001), and Nova Lite $0.000486 for two requests combined totaling 4,000 input / 1,024 output tokens ($0.06/million input, $0.24/million output). A conservative example of 30 minutes at full 2-vCPU/8-GB Runtime allocation is about $0.1952 Runtime compute and about $0.196 total with the same Lambda/Gateway/model assumptions, rounded to about US$0.20. Under those assumptions the estimated variable cost is below US$1, but this is not a guaranteed cap. Rates are Tokyo planning rates checked against the linked AWS pricing pages; confirm current rates and actual billing dimensions before setup. The Runtime idle timeout is 900 seconds (15 minutes without activity) and maximum microVM lifetime is 28,800 seconds (8 hours); the 15/30-minute calculations assume full listed CPU/memory usage for each active minute, not the maximum lifetime. This code has no explicit CPU/memory sizing parameter in CreateAgentRuntime, so 2-vCPU/8-GB is a conservative billing assumption, not a guaranteed allocation. Runtime permits at most two model submissions; the combined token counts are estimate assumptions, not enforced caps. Actual account charges and measured consumption are unconfirmed. Excludes taxes/FX, storage, logs, data transfer, free tier, and price changes. Pricing references: [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/), [Lambda pricing](https://aws.amazon.com/lambda/pricing/), [Nova Lite Bedrock pricing](https://aws.amazon.com/bedrock/pricing/).

### Rollback boundary

Stop only the new Runtime's active sessions, then delete only this run's new Runtime, target, Gateway, Lambda, three roles/policies, dedicated log groups, and its unique S3 prefix after preserving the run record. Leave every pre-existing S03/S04 synthetic resource, shared S03 Runtime role/environment, and all other section resources unchanged.
