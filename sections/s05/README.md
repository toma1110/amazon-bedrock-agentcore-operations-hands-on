# S05：利用者の好みを保存し、分離と削除を確認する

この演習では、S02と同じStrandsエージェントをローカルのPythonプロセスとして起動します。S03 invocation summaryを返す読み取り専用Gatewayツールと、任意で利用するAgentCore Memoryを組み合わせ、MemoryのOFF/ONで利用者の好みと選択したS03 invocation summaryがどう違うかを確認します。S03 Runtimeは呼び出さず、S03 RuntimeやS04 Gatewayの設定も変更しません。

ここで2つのLearner roleが表すのは、AWS IAM上の2つの認可主体です。別々の人間であることを証明するものではありません。両方のroleを引き受けられる人はroleを切り替えられます。アプリはSTS `GetCallerIdentity`が返す署名済みのrole ARNからactorを決め、コマンド引数やHTTP headerのactor値は受け付けません。分離の根拠はアプリ内の判定だけでなく、AWS IAMの条件付き許可です。

## この演習で確認すること

- 各Learner roleが、合成した画面表示の好みを短期eventとして保存できる。
- `USER_PREFERENCE`戦略が長期Memory recordを抽出し、`/actors/<actor>/`から後で検索できる。
- 自分のeventとrecordは読めるが、他actorのevent/namespaceを直接指定した要求はAWS IAMに拒否される。
- Memory OFFではMemoryツールが存在せず呼ばれない。Memory ONでは自分のnamespaceを使う1回の検索traceがあり、期待するrecord IDと内容が出た場合だけ、回答がMemory由来と判断する。
- 選択したS03 invocation summaryは毎回Gatewayから取得し、Memoryには保存しない。S09の最新インシデント実ログとは別のデータです。
- Exercise-adminのみが、指定した2つのeventとrecordを別々に削除する。Learner roleにはevent/recordの削除、record ID直接取得、actor一覧の権限を与えない。

好みは合成データだけを使ってください。氏名、credential、顧客情報、実際のインシデントは保存しないでください。

## 進め方と出力の読み方

1. 既存のbootstrap認証経路を確認し、3つのS05 role/profileと1つのMemoryを準備します。
2. A/Bそれぞれのroleで自分のevent・recordを保存/読取し、相互のevent/namespace要求がAWS IAMに拒否されることを確認します。
3. A/Bそれぞれで同じ質問をMemory OFF/ON各1回実行します。OFFにはMemory toolがなく、ONは自分のnamespaceだけを検索します。
4. Exercise-adminでeventとrecordを個別削除し、各Learner roleで削除後の不存在を確かめてからMemoryとS05 role/profileをcleanupします。

`ask`の出力には、回答文、`tools`の呼出しtrace、`token_usage`が含まれます。期待するtraceは質問内容によって異なります。好みだけを尋ねる場合、Gatewayは通常呼ばれず、OFFにはMemory traceがなく、ONには自分のnamespaceを使うMemory traceが期待されます。障害情報と保存した好みを同時に尋ねる場合は、Gateway traceに加え、ONでは自分のnamespaceを使うMemory traceが期待されます。Memory ONでrecordを使ったと判断するには、traceのnamespaceが自分の`/actors/<actor>/`であること、`record_count`が1以上で期待record ID/内容を含むこと、回答がその内容を反映していることを合わせて確認します。回答だけからMemoryの効果を推定しません。

## 構成と境界

```text
Learner A/B profile → ローカルPython/Strands → AWS SigV4
    ├─ STS GetCallerIdentity → 固定されたactor対応付け
    ├─ AgentCore Memory：自分のevent/namespaceだけ
    ├─ 既存Gateway → reader → 指定したS03 invocation summary（Memoryとは別の情報源）
    └─ S02と同じNova Liteモデル
Exercise-admin profile → S05 Memoryの作成・限定削除・最終cleanup
S03 Runtime、S04 Gateway/Lambdaの設定は変更しない
```

Runtimeの実行roleを受講者本人として扱いません。このS05演習は、Memory APIを呼ぶローカルプロセス自体をLearner roleで動かし、そのAWS principalを確認するため、Runtimeを経由しません。後のホスト型アプリがRuntime経由でclient identityを渡す方法を示すものではありません。

## 作成するAWS resourceとIAM

対象は学習用AWS accountのTokyo region `ap-northeast-1`だけです。作成するMemoryは1つ、名前は`udemy_agentcore_s05_memory`です。CreateMemoryでは短期eventの保持を3日にし、`S05UserPreferences`という`USER_PREFERENCE`戦略を1つ作ります。namespace templateは`/actors/{actorId}/`です。Memory IDとstrategy IDはAWSが生成する値を必ず使ってください。

この演習で固定しているboto3/botocore 1.43.108では、CreateMemory/UpdateMemoryの戦略入力にrecordExpirySpecificationがありません。そのため、このSDK演習ではrecord TTLを設定せず、自動失効を前提にしません。これはAgentCore service全般にTTL機能がないという意味ではありません。演習後はeventとrecordを個別に削除し、最後にS05 Memory resource全体を削除して、削除完了を確認します。

作るIAM roleは次の3つです。既存roleとの衝突がないことを先に確認し、root-pathで作成してください。S05 inline policyの名前はlearner A=`S05LearnerA`、learner B=`S05LearnerB`、admin cleanup=`S05AdminCleanup`、一時setup=`S05TemporarySetup`に固定します。異なる名前のpolicyが見つかった場合は推測で削除せず、実行計画とpolicy documentを照合できるまで停止します。

- `udemy-agentcore-s05-learner-a`
- `udemy-agentcore-s05-learner-b`
- `udemy-agentcore-s05-exercise-admin`

3 roleの信頼先は、作業環境の認証済みbootstrap sessionからIAM `GetRole`で確認したbootstrap role ARN 1つだけです。実際に確認したARNをtrust policyへ使います。account全体、wildcard、推測したARNを信頼先にしません。ログイン設定やIdentity Centerのsession・permission setは変更しません。

## 事前準備：profileと非公開state

最初に環境に合う分岐を一つ選び、その分岐だけを実行してください。2つの`BOOTSTRAP_ARGS`設定を両方実行しません。

### 既存の認証済みbootstrap profileを使う場合

通常環境では、以降の同じBash shellで使う既存の認証済みprofile名を指定します。CloudShellでも既存の名前付きbootstrap profileを使う場合はこちらです。SSO profileでは、既存の認証手順でlogin済みであることを確認します。

```bash
BOOTSTRAP_ARGS=(--profile "<既存bootstrap-profile>")
```

### CloudShellに既存の名前付きbootstrap profileがない場合

この分岐では上のprofile指定行を実行しません。まず`aws configure list`のcredential providerが`container-role`であることを確認します。続けてprofile未指定のSTS identityが想定accountとbootstrap主体であることを照合し、両方確認できた場合だけ同じshellで空配列を設定します。

```bash
aws configure list
aws sts get-caller-identity --output json --no-cli-pager
BOOTSTRAP_ARGS=()
```

providerが`container-role`でない、account/roleが想定外、またはidentityを確認できない場合は停止します。CloudShellのcredentialをコピー、保存したり、認証設定を変更したりしません。通常環境では、3つのS05 profileがbootstrap profileを`source_profile`として各roleを引き受けます。CloudShellの上記条件を満たす場合だけ、3つのS05 profileの`source_profile`行を`credential_source = EcsContainer`に置き換えます。AWS CLIのこの設定はECS container roleをrole profileのsourceとして使うものです。CloudShellではprovider表示を確認した場合に限って使用します。

`~/.aws/config`に次の3つのS05 profileを設定します。account IDと、通常環境の`<既存bootstrap-profile>`を置き換えます。CloudShell条件を使う場合は3つの`source_profile`行だけを`credential_source = EcsContainer`へ置き換えます。一時credentialは書きません。

```ini
[profile s05-learner-a]
role_arn = arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a
source_profile = <既存bootstrap-profile>
region = ap-northeast-1

[profile s05-learner-b]
role_arn = arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b
source_profile = <既存bootstrap-profile>
region = ap-northeast-1

[profile s05-operator]
role_arn = arn:aws:iam::<account>:role/udemy-agentcore-s05-exercise-admin
source_profile = <既存bootstrap-profile>
region = ap-northeast-1
```

role作成後、`aws sts get-caller-identity --profile s05-learner-a`、`... s05-learner-b`、`... s05-operator`で、出力ARNに期待したrole名が含まれることを確認します。profile名だけを本人確認とは扱いません。

CloudShellで`container-role`条件を使う場合も、既存の他設定を保持し、3つのS05 profile sectionだけを追加してください。bootstrap identity確認とS05 profileのSTS結果でaccountが想定値と一致し、Arnが各exact S05 roleのsessionであることを確認してから次へ進みます。後続のbootstrap用STS/IAM例では、通常環境は`BOOTSTRAP_ARGS`に既存profileを指定し、CloudShell条件では空配列のまま同じ配列表記を使います。Learner/operatorのコマンドは引き続きそれぞれ`--profile s05-learner-a`、`--profile s05-learner-b`、`--profile s05-operator`を使用します。

演習の識別子を保存するファイルは、repository内の`.local/s05-state.json`だけです。このrepositoryの`.gitignore`で`.local/`全体を除外しています。`.local` directoryを作り、次の形で保存してください。認証情報、access key、session token、credentialファイルの内容は絶対に保存しません。作成した後も`git status`で追跡対象になっていないことを確認します。

```json
{
  "account_id": "<12-digit-account>",
  "region": "ap-northeast-1",
  "bootstrap_role_arn": "<GetRoleで確認したARN>",
  "learner_a_role_arn": "<exact ARN>",
  "learner_b_role_arn": "<exact ARN>",
  "admin_role_arn": "<exact ARN>",
  "memory_id": "<CreateMemory result>",
  "memory_arn": "<CreateMemory result>",
  "strategy_id": "<GetMemory result>",
  "gateway_id": "<既存S04 stateの値>",
  "gateway_arn": "<既存S04 stateの値>",
  "gateway_endpoint": "<既存S04 endpoint>/mcp",
  "gateway_tool_name": "udemy-agentcore-s04-cw-target___read_s03_invocation",
  "event_ids": {"learner-a": "<exact ID>", "learner-b": "<exact ID>"},
  "record_ids": {"learner-a": "<exact ID>", "learner-b": "<exact ID>"}
}
```

S04 state fileは非公開の既存ファイルです。直接コピー・編集せず、読み取り専用でexact endpoint、Gateway ARN/ID、tool nameを確認して上記stateへ記録します。確認できない値は推測せず停止してください。

## IAM policyの設定

Learner A/Bのpolicyは、CreateEvent/ListEventsのactor IDとsession ID、およびRetrieveMemoryRecordsのnamespaceとstrategy IDをAWS IAMで絞ります。role Aはactor `learner-a`、namespace `/actors/learner-a/`、role Bは`learner-b`側に置換します。Resourceは作成後に得た1つのMemory ARNだけです。S04 GatewayとNova Liteについても、それぞれ既存の正確なARNだけ許可します。

role Aのpolicy例（`<account>`、`<memory-id>`、`<strategy-id>`、`<gateway-id>`を確認済みの値へ置換）：

`bedrock:InvokeModelWithResponseStream`は、このStrands agent呼出しがstreaming APIを使うために必要です。次の例のように`InvokeModel`とともに正確なNova Lite model ARNにだけ許可します。streaming actionを外すとmodel呼出しが`AccessDeniedException`になることがあります。

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect":"Allow","Action":["bedrock-agentcore:CreateEvent","bedrock-agentcore:ListEvents"],"Resource":"arn:aws:bedrock-agentcore:ap-northeast-1:<account>:memory/<memory-id>","Condition":{"StringEquals":{"bedrock-agentcore:actorId":"learner-a","bedrock-agentcore:sessionId":"s05-course-demo"}}},
    {"Effect":"Allow","Action":"bedrock-agentcore:RetrieveMemoryRecords","Resource":"arn:aws:bedrock-agentcore:ap-northeast-1:<account>:memory/<memory-id>","Condition":{"StringEquals":{"bedrock-agentcore:namespace":"/actors/learner-a/","bedrock-agentcore:strategyId":"<strategy-id>"}}},
    {"Effect":"Allow","Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],"Resource":"arn:aws:bedrock:ap-northeast-1::foundation-model/amazon.nova-lite-v1:0"},
    {"Effect":"Allow","Action":"bedrock-agentcore:InvokeGateway","Resource":"arn:aws:bedrock-agentcore:ap-northeast-1:<account>:gateway/<gateway-id>"}
  ]
}
```

role Bは`actorId`とnamespaceのみを`learner-b`へ変更します。Learner policyに`GetMemoryRecord`、`DeleteMemoryRecord`、`BatchDeleteMemoryRecords`、`DeleteEvent`、`ListActors`、Memoryのcontrol-plane変更、wildcard actionを足しません。

Exercise-adminには一時的に作成用policyを付けます。CreateMemoryはID発行前なのでresource指定できませんが、request tagを必須にします。AgentCoreがCreateMemory時に別途TagResourceを要求する場合は、TagResourceだけをMemory名prefixのARNと同じCourse/TagKeys条件へ追加します。Inventory actionは名前衝突とresource数確認の読取専用です。Service Quotasにresource上限が表示されるとは限りません。作成後はsetup policyをすぐ外してください。

```json
{
  "Version":"2012-10-17",
  "Statement":[
    {"Effect":"Allow","Action":"bedrock-agentcore:CreateMemory","Resource":"*","Condition":{"StringEquals":{"aws:RequestTag/Course":"udemy-agentcore-s05"},"ForAllValues:StringEquals":{"aws:TagKeys":["Course","Owner"]}}},
    {"Effect":"Allow","Action":"bedrock-agentcore:TagResource","Resource":"arn:aws:bedrock-agentcore:ap-northeast-1:<account>:memory/udemy_agentcore_s05_memory-*","Condition":{"StringEquals":{"aws:RequestTag/Course":"udemy-agentcore-s05"},"ForAllValues:StringEquals":{"aws:TagKeys":["Course","Owner"]}}},
    {"Effect":"Allow","Action":"bedrock-agentcore:ListMemories","Resource":"*"},
    {"Effect":"Allow","Action":"bedrock-agentcore:GetMemory","Resource":"arn:aws:bedrock-agentcore:ap-northeast-1:<account>:memory/*"}
  ]
}
```

作成後のadmin policyは、同じMemory ARNと`Course` tagだけに限定します。短期eventの削除はIAM条件でも2つのactorと固定sessionに制限します。長期record削除権限は同じMemory内に限られますが、record namespace condition keyの対応が確認できないため、CLIは固定された2 namespaceのexact record IDだけを要求します。操作時はIDとownerを照合します。

```json
{
  "Version":"2012-10-17",
  "Statement":[
    {"Effect":"Allow","Action":["bedrock-agentcore:GetMemory","bedrock-agentcore:DeleteMemory","bedrock-agentcore:DeleteMemoryRecord"],"Resource":"arn:aws:bedrock-agentcore:ap-northeast-1:<account>:memory/<memory-id>","Condition":{"StringEquals":{"aws:ResourceTag/Course":"udemy-agentcore-s05"}}},
    {"Effect":"Allow","Action":"bedrock-agentcore:DeleteEvent","Resource":"arn:aws:bedrock-agentcore:ap-northeast-1:<account>:memory/<memory-id>","Condition":{"StringEquals":{"aws:ResourceTag/Course":"udemy-agentcore-s05","bedrock-agentcore:actorId":["learner-a","learner-b"],"bedrock-agentcore:sessionId":"s05-course-demo"}}}
  ]
}
```

作成前にIAM Service Authorization referenceで各actionとcondition keyの対応を再確認します。実アカウントでは自分の要求が成功し、別actorのListEventsと別namespaceのRetrieveMemoryRecordsがAWS IAMにより`AccessDeniedException`となることを確認するまで、分離が成功したと扱いません。Client側で拒否した結果や、resource不存在のエラーはIAM拒否の証拠ではありません。

## 作成前の確認とMemory作成

次を読み取り専用で確認します。

- account/regionが想定通りである。
- bootstrap roleの完全なARNと既存のS04 Gateway ARN、endpoint、tool name。
- 3つのS05 role名/パスが未使用である。
- AWS公式quota資料にあるdefault 150 resourceの上限と、現在のMemory resource数。Service Quotas API/UIにaccountで適用中の値が表示される場合はその値も確認します。
- `ListMemories`で既存の`udemy_agentcore_s05_memory`がないこと。

Inventoryは対象account/regionの現在の状態を示します。AWS公式資料のdefault quota 150は参考値で、現在の適用quotaとは区別します。現在数が150以上、同名Memoryの存在、S04識別子不明、想定外accountなら作成せず停止してください。現在数が150未満の場合は、CreateMemoryのAWS側quota判定に従います。`ServiceQuotaExceededException`などのquota/resource-limit拒否が返ったら、再試行やquota増加申請をせず停止します。quota結果がAPIで表示されないことだけを理由に追加の承認条件を設けず、APIが拒否した操作を迂回しません。

`sections/s05/`を含むpublic hands-on repositoryのrootで、既存の依存関係を同期してmock testを実行します。これらのtestはAWS resourceを作らず、Bedrockも呼びません。

```bash
uv sync --locked --project sections/s02
uv run --project sections/s02 pytest sections/s02/tests sections/s03/tests sections/s04/tests sections/s05/tests -q
```

設定した`BOOTSTRAP_ARGS`を使い、対象accountと引受元roleを再確認します。CloudShell条件では配列が空のため、profile未指定で現在のCloudShell認証providerを使います。AWS CLIはrepository rootから実行してください。出力は公開fileへ書かず、accountとARNをIssueやログへ貼らないでください。

```bash
aws sts get-caller-identity "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam get-role --role-name <exact-bootstrap-role-name> --query Role.Arn --output text "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
```

STSのaccountとassumed-role名が想定どおりであることを確認し、`GetRole`で返った完全なIAM role ARNを控えます。account/role名が想定と異なる、または確認できない場合は停止します。

次に、作成予定の3つのS05 role名が未使用か確認します。`NoSuchEntity`だけを不存在として扱います。roleが存在する場合は作成せず停止し、`AccessDenied`など他のエラーや曖昧な応答でも先へ進みません。

```bash
aws iam get-role --role-name udemy-agentcore-s05-learner-a "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam get-role --role-name udemy-agentcore-s05-learner-b "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam get-role --role-name udemy-agentcore-s05-exercise-admin "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
```

3つすべてで`NoSuchEntity`を確認できた後、公開テンプレートを変更せず`.local/`内にtrust JSONの作業用copyを作成します。`Principal`を先ほど確認した完全なbootstrap role ARNへ置き換えて読み返し、placeholderが残っていないこと、ARNが確認値と完全一致することを確かめます。

```bash
mkdir -p .local
cp sections/s05/policies/s05-bootstrap-trust.json .local/s05-bootstrap-trust.json
# .local/s05-bootstrap-trust.json のPrincipalを確認済みの完全なbootstrap role ARNへ置換する
# 読み返してplaceholderが残っていないこと、ARNが確認値と一致することを確認する
```

確認済みの`.local/s05-bootstrap-trust.json`を使い、3つのroleを作成します。コマンドはすべて同じ`BOOTSTRAP_ARGS`を使います。

```bash
aws iam create-role --role-name udemy-agentcore-s05-learner-a --assume-role-policy-document file://.local/s05-bootstrap-trust.json "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
aws iam create-role --role-name udemy-agentcore-s05-learner-b --assume-role-policy-document file://.local/s05-bootstrap-trust.json "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
aws iam create-role --role-name udemy-agentcore-s05-exercise-admin --assume-role-policy-document file://.local/s05-bootstrap-trust.json "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
```

`.local/s05-state.json`はrepository rootに作成します。内容はこのREADMEの上記JSON形に従い、account、3つのrole ARN、確認済みbootstrap ARN、Memory/Gateway情報を自分の確認値で埋めます。秘密credentialは含めません。S03の照合値は同じS03実行から取得します。各placeholderは同じaccount/regionで確認した値へ置換し、Git statusで`.local/`が追跡対象外であることを確認してください。

```bash
mkdir -p .local
cat > .local/s05-state.json <<'JSON'
{
  "account_id": "<12-digit-account>",
  "region": "ap-northeast-1",
  "bootstrap_role_arn": "<verified-bootstrap-role-arn>",
  "learner_a_role_arn": "arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a",
  "learner_b_role_arn": "arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b",
  "admin_role_arn": "arn:aws:iam::<account>:role/udemy-agentcore-s05-exercise-admin",
  "memory_id": "<created-memory-id>",
  "memory_arn": "<created-memory-arn>",
  "strategy_id": "<confirmed-strategy-id>",
  "gateway_id": "<read-from-private-S04-state>",
  "gateway_arn": "<read-from-private-S04-state>",
  "gateway_endpoint": "<exact-private-S04-endpoint>/mcp",
  "gateway_tool_name": "<exact-configured-reader-tool-name>",
  "event_ids": {"learner-a": "<exact-event-id>", "learner-b": "<exact-event-id>"},
  "record_ids": {"learner-a": "<exact-record-id>", "learner-b": "<exact-record-id>"}
}
JSON
git status --short -- .local/s05-state.json
```

```bash
# S03をこの環境で実行済みなら、該当するtag/profileでread-only logsを表示
uv run --project sections/s02 python sections/s03/aws_workflow.py logs --profile <S03-profile> --run-tag <S03-run-tag>
```

出力の同じJSON行から`invocation_id`と整数の`timestamp`（Unix millisecond）を一緒に控えます。S03実行がまだなら、S03 READMEの手順で一度実行し、その出力を`logs`で照合してください。別実行のIDと時刻を組み合わせません。S03ログ出力に該当行がない場合は、別値を推測せず停止します。Gateway情報は既存の非公開S04 state（`sections/s04/.agentcore-s04-state.json`）と現在の設定を読み取り専用で照合します。endpoint、Gateway ARN/ID、tool nameが一致しなければ先へ進みません。現在のmainにあるS04教材が旧reader契約のままなら、S04で定めた`read_s03_invocation` readerが実装・配備され、既存Gatewayの設定と一致するまでこの手順は実行できません。

実行順は、Learner/Admin用roleとprofileの準備、adminの一時setup policyによるMemory 1つの作成、setup policy削除、exact-resource admin cleanup policyとLearner A/B policyの設定、Learner A/B各自のevent・record保存/読取/分離probe、OFF/ON比較、event/recordの個別削除、Memory削除、最後にIAM/profile cleanupです。Learnerには自分の範囲のdata操作だけを許可し、AdminはMemory作成と対象event/recordの削除に使います。名前衝突や作成の不明な結果がある場合、再作成せず既存stateを調べます。成功時のexact `memory_id`/`memory_arn`、およびGetMemoryで確認した`strategy_id`を、credentialを含めず`.local/s05-state.json`へ記録します。学習者はこのprivate stateからIDを使います。roleのARN、Memory ID、Gateway識別情報はpublic fileに書きません。

```bash
cp sections/s05/policies/s05-admin-setup.json .local/s05-admin-setup.json
# .local/s05-admin-setup.json 内の全<account>を確認済みaccount IDへ置換し、placeholderが残っていないこととARNを読み返して確認する
aws iam put-role-policy --role-name udemy-agentcore-s05-exercise-admin --policy-name S05TemporarySetup --policy-document file://.local/s05-admin-setup.json "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
uv run --project sections/s02 python sections/s05/memory_lab.py create-memory --profile s05-operator --expected-account <account> --admin-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-exercise-admin --confirm-create
aws iam delete-role-policy --role-name udemy-agentcore-s05-exercise-admin --policy-name S05TemporarySetup "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
```

Create結果とGetMemoryで状態が`ACTIVE`であることを確認後、`S05TemporarySetup`は直ちに削除します（上の3番目のcommand）。次にREADMEのLearner A/B policy JSONとexact-resource admin cleanup JSONを、それぞれ`.local/s05-learner-a.json`、`.local/s05-learner-b.json`、`.local/s05-admin-cleanup.json`へ保存します。各file内の`<account>`、`<memory-id>`、`<strategy-id>`、`<gateway-id>`を`.local/s05-state.json`の確認済み値に置換します。モデルARN、actor/session/namespace、Course tagは変更しません。3 fileすべてを読み返し、placeholderが残っていないこと、対象ARNがS05のexact resourceであることを確認してから適用します。`git status --short -- .local`で作業fileが追跡対象外であることも確認します。

```bash
aws iam put-role-policy --role-name udemy-agentcore-s05-learner-a --policy-name S05LearnerA --policy-document file://.local/s05-learner-a.json "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
aws iam put-role-policy --role-name udemy-agentcore-s05-learner-b --policy-name S05LearnerB --policy-document file://.local/s05-learner-b.json "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
aws iam put-role-policy --role-name udemy-agentcore-s05-exercise-admin --policy-name S05AdminCleanup --policy-document file://.local/s05-admin-cleanup.json "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
```

bootstrap roleには権限を追加しません。作成直後から運用中まで、trustのprincipalは確認済みbootstrap IAM role ARN 1つだけです。

## actorごとの保存と分離確認

まずrole A/BそれぞれのSTS identityとactor対応を確認します。role Aなら`learner-a`、role Bなら`learner-b`でなければ、Memory要求の前に停止します。

```bash
uv run --project sections/s02 python sections/s05/memory_lab.py identity --profile s05-learner-a --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b
uv run --project sections/s02 python sections/s05/memory_lab.py identity --profile s05-learner-b --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b
```

各actorで合成した好みを1 eventだけ保存し、返されたexact event IDをprivate stateへ記録します。eventは非同期に長期recordへ抽出されます。`read-records`で期待recordが表示されたらexact record IDも記録してください。3分待ってrecordがなければ、それ以上eventを追加せず、Memory status/event/strategyを調査します。

```bash
uv run --project sections/s02 python sections/s05/memory_lab.py save-event --profile s05-learner-a --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b --memory-id <exact-memory-id>
uv run --project sections/s02 python sections/s05/memory_lab.py read-events --profile s05-learner-a --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b --memory-id <exact-memory-id>
uv run --project sections/s02 python sections/s05/memory_lab.py read-records --profile s05-learner-a --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b --memory-id <exact-memory-id> --strategy-id <exact-strategy-id>
```

Learner Bも同じ3コマンドで実行し、actor Bが自身のnamespaceだけから取得することを確認します。次に各learnerで`probe-other`を1回ずつ実行します。これは他actorのeventとnamespaceを1回ずつ要求します。両方がAWS IAMによるAccessDeniedになることを確認してください。成功した要求が1つでもあれば停止し、分離が成功したと記録しません。

```bash
uv run --project sections/s02 python sections/s05/memory_lab.py probe-other --profile s05-learner-a --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b --memory-id <exact-memory-id> --strategy-id <exact-strategy-id>
```

## S03 invocation summaryを取得し、Memory OFF/ONを比べる

S03のlogsから同じinvocation UUIDと13桁Unix millisecond timestampをコピーします。両方が直近7日以内で、Gateway endpointとtool nameは実際に確認したexact値を使います。値が欠けている・形式が違う場合は、AWS identity確認、Memory retrieval、Gateway requestより前にCLIが拒否します。モデルには引数なし`read_s03_invocation()`だけを渡し、対象UUID・時刻・`max_results=1`はCLIで固定します。モデルが対象を変更・選択できません。

Memory OFF/ONは同じ質問と同じUUID/timestampで実行します。OFFはMemory取得0回、ONは自分のnamespaceから最大1回です。Gatewayのdata取得元は常に指定されたS03 invocation summaryです。Memoryの好みは回答の表現だけに影響し、運用事実の取得元にはしません。readerの`ok` / `reader_execution.lambda_request_id` / `data` envelopeを確認した後、`data.found`、一致したS03 eventの`target_invocation_id`・`target_timestamp_ms`・status・tool_call_count・tool_names、切詰状態だけをモデルとtraceへ渡します。reader request IDは検索対象のS03 invocation IDと別の値です。`note`や未知field、raw/full logs、prompt、answer、元のtool argumentsは渡しません。`found=false`、eventなし、`truncated=true`、tool_names切詰、malformed/unsupported応答は不確定です。追加読取や再試行をせず停止し、取得できた範囲からRuntime health、障害、業務影響、原因を断定しません。

```bash
uv run --project sections/s02 python sections/s05/memory_lab.py ask --profile s05-learner-a --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b --memory-id <exact-memory-id> --strategy-id <exact-strategy-id> --gateway-endpoint <exact-Gateway-endpoint/mcp> --gateway-tool-name <exact-reader-tool-name> --target-invocation-id <UUID-copied-from-S03-logs> --target-timestamp-ms <13-digit-Unix-milliseconds-copied-from-S03-logs> --memory-mode off --question "Summarize this selected S03 invocation and tell me how to present timestamps."
uv run --project sections/s02 python sections/s05/memory_lab.py ask --profile s05-learner-a --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b --memory-id <exact-memory-id> --strategy-id <exact-strategy-id> --gateway-endpoint <exact-Gateway-endpoint/mcp> --gateway-tool-name <exact-reader-tool-name> --target-invocation-id <UUID-copied-from-S03-logs> --target-timestamp-ms <13-digit-Unix-milliseconds-copied-from-S03-logs> --memory-mode on --question "Summarize this selected S03 invocation and tell me how to present timestamps."
```

Learner Bでも同様に実行できます。CLI出力のtraceでGatewayの固定入力とallowlist結果を確認し、Memory ONではnamespaceとrecord IDも確認します。S03 invocation summaryはS09が都度読む最新incident実ログではありません。summaryが見つからない/切り詰められている場合は追加AWS readをせず停止し、同一S03 runのID・時刻と契約済みreaderの設定を確認してください。実運用のprompt、answer、個人・顧客情報は使わず、共有しないでください。

## eventとrecordを別々に削除し、確認する

Learner roleには削除権限がありません。Exercise-adminを使って、コードがprivate `.local/s05-state.json`のaccount、region、Memory ID、owner別IDと一致した場合だけ削除します。stateがない、壊れている、またはID/ownerが一致しない場合はAWS要求を送らず停止します。各actorのeventとrecordを1つずつ削除し、Learner roleで再読して対象が消えたことを確認します。抽出・削除が遅れている場合はstatusとIDを調べ、前の操作結果が確定する前に破壊操作を繰り返しません。

```bash
aws sts get-caller-identity --profile s05-operator --output json --no-cli-pager
uv run --project sections/s02 python sections/s05/memory_lab.py admin-delete-event --profile s05-operator --expected-account <account> --admin-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-exercise-admin --memory-id <exact-memory-id> --owner a --event-id <exact-event-id>
aws sts get-caller-identity --profile s05-operator --output json --no-cli-pager
uv run --project sections/s02 python sections/s05/memory_lab.py admin-delete-record --profile s05-operator --expected-account <account> --admin-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-exercise-admin --memory-id <exact-memory-id> --owner a --record-id <exact-record-id>
```

Bのexact IDでも繰り返します。各削除CLIの直前にSTS結果を確認し、Accountが期待した学習用accountで、Arnが`udemy-agentcore-s05-exercise-admin` roleのsessionである場合だけ続けます。両actorの再読でevent/recordが消えたことを確認してから、Memory cleanupします。private `.local/s05-state.json`に保存したaccount、region、Memory IDとARNが期待値に一致することを、read-onlyの`get-memory`が返すexact ID、ARN、名前`udemy_agentcore_s05_memory`とも照合します。GetMemory応答にresource tagが含まれるとは仮定しません。tagはこの応答確認に使わず、IAM policyに設定したtag conditionとは区別します。state項目が欠落・不一致、STS principal/account違い、またはGetMemory応答との不一致・曖昧さがあれば削除しません。

```bash
aws sts get-caller-identity --profile s05-operator --output json --no-cli-pager
aws bedrock-agentcore-control get-memory --memory-id <exact-memory-id> --profile s05-operator --region ap-northeast-1 --output json --no-cli-pager
```

STS Accountが期待した学習用accountで、Arnが`udemy-agentcore-s05-exercise-admin` roleのsessionであることを確認したうえで、保存済みstate値とGetMemoryのexact ID/ARN/nameが全て一致する場合だけ削除します。

```bash
aws sts get-caller-identity --profile s05-operator --output json --no-cli-pager
aws bedrock-agentcore-control delete-memory --memory-id <exact-memory-id> --profile s05-operator --region ap-northeast-1 --output json --no-cli-pager
```

`DELETING`は完了ではありません。削除要求を再送せず、同じIDへのread-only `get-memory`で`DELETED`または`ResourceNotFoundException`になるまで状態を確認します。他の応答・失敗・対象不一致では止めて調査します。

```bash
aws bedrock-agentcore-control get-memory --memory-id <exact-memory-id> --profile s05-operator --region ap-northeast-1 --output json --no-cli-pager
```

次にIAM cleanupを行います。事前に選んだ同じ`BOOTSTRAP_ARGS`を使い、cleanup直前にSTS callerを確認します。Accountが期待した学習用accountと一致し、Arnに保存済み`.local/s05-state.json`の`bootstrap_role_arn`で記録したroleが含まれることを確認します。CloudShell条件では空配列のためprofile未指定で実行します。実環境のbootstrap role名を教材例へ固定しません。3 roleそれぞれについて`GetRole`、`list-role-policies`、managed policy/instance profile一覧を確認し、Learner A=`S05LearnerA`、Learner B=`S05LearnerB`、admin=`S05AdminCleanup`（setup policy `S05TemporarySetup`が残っている場合のみ追加）と照合します。名前やpolicy documentが想定外なら停止します。S05用と確認したinline policyだけをexact名で削除し、各roleのinline policy一覧が空であることを再確認してからroleを削除します。各削除の直前にもSTS callerを確認します。

以下はexact targetの読み取り例です。各コマンドで`BOOTSTRAP_ARGS`を同じBash shellのまま使用します。

```bash
aws iam get-role --role-name udemy-agentcore-s05-learner-a "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam get-role --role-name udemy-agentcore-s05-learner-b "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam get-role --role-name udemy-agentcore-s05-exercise-admin "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam list-role-policies --role-name udemy-agentcore-s05-learner-a "${BOOTSTRAP_ARGS[@]}" --query 'PolicyNames' --output json --no-cli-pager
aws iam list-role-policies --role-name udemy-agentcore-s05-learner-b "${BOOTSTRAP_ARGS[@]}" --query 'PolicyNames' --output json --no-cli-pager
aws iam list-role-policies --role-name udemy-agentcore-s05-exercise-admin "${BOOTSTRAP_ARGS[@]}" --query 'PolicyNames' --output json --no-cli-pager
aws iam list-attached-role-policies --role-name <exact-S05-role-name> "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam list-instance-profiles-for-role --role-name <exact-S05-role-name> "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam get-role-policy --role-name udemy-agentcore-s05-learner-a --policy-name S05LearnerA "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam get-role-policy --role-name udemy-agentcore-s05-learner-b --policy-name S05LearnerB "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam get-role-policy --role-name udemy-agentcore-s05-exercise-admin --policy-name S05AdminCleanup "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
```

`S05TemporarySetup`削除は次の条件付き手順です。admin roleのinventoryにこのexact nameが残っている場合に限りpolicy documentを読み、S05一時setup policyと確認できた場合だけ実行します。見つからない場合は削除を省略します。別の名前や内容なら停止します。

```bash
aws iam get-role-policy --role-name udemy-agentcore-s05-exercise-admin --policy-name S05TemporarySetup "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
```

上記のdocumentがS05一時setup policyと照合できた場合のみ:

```bash
aws sts get-caller-identity "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam delete-role-policy --role-name udemy-agentcore-s05-exercise-admin --policy-name S05TemporarySetup "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
```

次に、inventoryで実在し、S05専用と照合済みの各policyに限り、各削除の直前に同じSTS確認をしてから exact `DeleteRolePolicy` を実行します（policy名がlist結果にない場合はskipし、別名があれば停止）。例:

```bash
aws sts get-caller-identity "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam delete-role-policy --role-name udemy-agentcore-s05-learner-a --policy-name S05LearnerA "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
```

各roleでinline policy一覧が空になった後、各`DeleteRole`の直前にもSTSを確認してroleを削除します。

```bash
aws iam list-role-policies --role-name <exact-S05-role-name> "${BOOTSTRAP_ARGS[@]}" --query 'PolicyNames' --output json --no-cli-pager
aws sts get-caller-identity "${BOOTSTRAP_ARGS[@]}" --output json --no-cli-pager
aws iam delete-role --role-name udemy-agentcore-s05-learner-a "${BOOTSTRAP_ARGS[@]}" --no-cli-pager
```

最後に3つのexact role名が`GetRole`で`NoSuchEntity`となり、既存S03 Runtime/S04 Gateway/Lambdaとそれらのroleが前後のread-only inventoryで不変なことを確認します。削除要求の結果が曖昧なら盲目的に再実行せず、状態を照合してから判断してください。S03 Runtime、S04 Gateway/Lambdaやそれらのroleは削除・変更しません。

3つのexact roleがすべて不存在と確認できた後、この演習で追加した`~/.aws/config`内の`[profile s05-learner-a]`、`[profile s05-learner-b]`、`[profile s05-operator]` sectionだけを削除します。元から存在したsectionは削除せず、他のprofile、default/bootstrap/SSO設定、認証設定は保持してください。config全体や`~/.aws/credentials`ファイルを削除しないでください。

## 費用と所要時間

上限計算の作業量は、Memory 1つ、event 2件、短期event retrieval 2回、長期record retrieval 4回（手動read-records 2回とMemory ONのask 2回）、agent呼び出し4回、既存Gateway/Lambda read 4回です。

2026-10-16時点で確認したAgentCore料金では、短期Memoryはingestion `$1/GB`、retrieval `$0.20/GB`、storage `$0.10/GB-month`です。ingestion/retrievalはeventごとの最小12KB、最大64KB、storageは実event sizeです。長期built-in strategyのstorageは`$0.75/1000 records-month`、retrievalは`$0.50/1000 requests`です。したがって2つの小さいeventのingestion約`$0.000025`、2回の短期event retrieval約`$0.000005`、record 2つを約1か月置くstorage約`$0.0015`、長期retrieval 4回は`$0.002`です。Eventの3日storage費はevent本体が1KB未満なら1セントのさらに小さい桁です。これは2026-10-16時点の価格確認記録であり、現在価格の確認ではありません。実行前に現在のTokyo料金を再確認してください。

S02 agentには1 invocationあたり3,952 input / 2,048 output tokens、最大4 turnの意図したbudgetを渡します。4回分は15,808 input / 8,192 output tokensです。ただしStrandsの実行でこの値がprovider-enforced hard capになる保証はありません。毎回`token_usage`の実測値を記録し、budget超過を検出したら追加呼出しを止めて原因と費用を再評価します。AWS Price List APIで2026-10-01 effectiveとして確認したTokyo Nova Lite on-demand単価は入力`$0.072/1M tokens`、出力`$0.288/1M tokens`でした。このbudgetどおりなら4回のmodel費用は`15,808×0.072/1,000,000 + 8,192×0.288/1,000,000 = $0.003497472`です。これは歴史時点の単価による見積りで、現在価格・実費・provider上限を示しません。

既存Gatewayの4 invokeはAgentCore Gateway `$0.005/1000 invocation`で約`$0.00002`です。既存LambdaはS04の30秒timeout、256MB設定を保守的に4回全て使う仮定でも約30 GB-secondです。Lambda通常のrequest/compute課金を適用した理論値は約`$0.0005`（request分はさらに小さい）です。実行時間が短ければ低くなります。CloudWatch Logsの取り込み・保存はLambdaが出すログ量、保存期間、accountの既存使用量に応じて変動するため、正確な配賦額はこの手順だけでは確定できません。実行前後にaccountの利用量を確認してください。

この前提での合算は、Memoryのevent/record処理・約1か月のrecord保管約`$0.00353`、4回のGateway約`$0.00002`、Lambda約`$0.00050`、model約`$0.003497472`、CloudWatch Logs約`$0.000013`で、合計約`$0.00756`です（event storageや一部のrequest費は丸め誤差未満）。内訳のCloudWatch LogsはTokyoのingestion `$0.76/GB`、Standard storage `$0.033/GB-month`で試算します。Lambda 4回につきAWS platform logを保守的に各2KiBと仮定すると、ingestionは約`$0.000012`、1か月storageでも約`$0.000001`未満です。これは出力量の計画仮定で、provider-enforced上限ではありません。実際のログ量・retention/account既存利用量を実行前後に確認し、増分は見積もりに加えます。`FilterLogEvents` read API自体にはCloudWatch Logs Insightsのdata-scanned料金を適用しません。Built-in `USER_PREFERENCE`の抽出・統合はAgentCore管理の組み込み処理です。override/self-managed strategyのように自分のaccountへ追加のBedrock model usageを課金する構成ではありません。これらの料金、model ID、free tier、既存CloudWatch利用量は変わり得るため、実行前にTokyo価格と現在の請求条件を確認してください。`$2`はprovider-enforced上限ではなく、追加実行前に中止して再評価する運用閾値です。見積りが近づく前に利用状況を確認し、想定外の課金や抽出遅延があればイベント・agent追加実行を止めます。1時間は作業時間の目安で、請求反映の保証ではありません。

## 困ったとき

- 自分の要求が`AccessDeniedException`になる：選択profileのSTS identity、account、Memory ID、固定actor/session、strategy ID、IAM condition値を確認します。権限をwildcardへ広げません。
- 他actorへのprobeが成功する：その場で停止します。実際のSTS roleへ正しいpolicyが付いているか、actionがcondition keyをサポートするか調べます。
- 3分後も長期recordがない：eventとstrategy statusを読み取りで確認します。重複eventは作らず、課金と原因を再評価します。
- 削除後もevent/recordが見える：exact ID、Memory ID、サービス状態を確認します。前の結果が確定するまで再削除しません。
- account/region/roleが異なる：AWS操作前に止め、正しい学習環境を選び直します。

## AWS公式資料

- [AgentCore MemoryのIAM actionとcondition key](https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html)
- [AgentCore Memory quotas](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/quotas.html)
- [CreateMemory API](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateMemory.html)
- [Memory namespaceとIAM条件](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/specify-long-term-memory-organization.html)
- [AgentCore料金](https://aws.amazon.com/bedrock/agentcore/pricing/)
- [Amazon Bedrock料金](https://aws.amazon.com/bedrock/pricing/)
