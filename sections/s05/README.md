# S05：利用者の好みを保存し、分離と削除を確認する

この演習では、S02と同じStrandsエージェントをローカルのPythonプロセスとして起動します。S04の読み取り専用Gatewayツールと、任意で利用するAgentCore Memoryを組み合わせ、MemoryのOFF/ONで利用者の好みと最新のインシデント情報がどう違うかを確認します。S03 Runtimeは呼び出さず、S03 RuntimeやS04 Gatewayの設定も変更しません。

ここで2つのLearner roleが表すのは、AWS IAM上の2つの認可主体です。別々の人間であることを証明するものではありません。両方のroleを引き受けられる人はroleを切り替えられます。アプリはSTS `GetCallerIdentity`が返す署名済みのrole ARNからactorを決め、コマンド引数やHTTP headerのactor値は受け付けません。分離の根拠はアプリ内の判定だけでなく、AWS IAMの条件付き許可です。

## この演習で確認すること

- 各Learner roleが、合成した画面表示の好みを短期eventとして保存できる。
- `USER_PREFERENCE`戦略が長期Memory recordを抽出し、`/actors/<actor>/`から後で検索できる。
- 自分のeventとrecordは読めるが、他actorのevent/namespaceを直接指定した要求はAWS IAMに拒否される。
- Memory OFFではMemoryツールが存在せず呼ばれない。Memory ONでは自分のnamespaceを使う1回の検索traceがあり、期待するrecord IDと内容が出た場合だけ、回答がMemory由来と判断する。
- 現在のインシデントは毎回S04の合成CloudWatchデータから取得し、Memoryには保存しない。
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
    ├─ 既存S04 Gateway → 既存Lambda → 合成CloudWatchデータ（最新の情報源）
    └─ S02と同じNova Liteモデル
Exercise-admin profile → S05 Memoryの作成・限定削除・最終cleanup
S03 Runtime、S04 Gateway/Lambdaの設定は変更しない
```

Runtimeの実行roleを受講者本人として扱いません。このS05演習は、Memory APIを呼ぶローカルプロセス自体をLearner roleで動かし、そのAWS principalを確認するため、Runtimeを経由しません。後のホスト型アプリがRuntime経由でclient identityを渡す方法を示すものではありません。

## 作成するAWS resourceとIAM

対象は学習用AWS accountのTokyo region `ap-northeast-1`だけです。作成するMemoryは1つ、名前は`udemy_agentcore_s05_memory`です。CreateMemoryでは短期eventの保持を3日にし、`S05UserPreferences`という`USER_PREFERENCE`戦略を1つ作ります。namespace templateは`/actors/{actorId}/`です。Memory IDとstrategy IDはAWSが生成する値を必ず使ってください。

長期Memory recordのTTLはこの演習では設定しません。使用中の固定SDKがrecord TTLを設定する入力・応答schemaを持たず、TTLを含むAPI要求は送信前にSDK検証で失敗するためです。したがってrecordが自動で31日後に消えるとは説明しません。演習後はeventとrecordを個別に削除し、最後にS05 Memory resource全体を削除して、削除完了を確認します。

作るIAM roleは次の3つです。既存roleとの衝突がないことを先に確認し、root-pathで作成してください。S05 inline policyの名前はlearner A=`S05LearnerA`、learner B=`S05LearnerB`、admin cleanup=`S05AdminCleanup`、一時setup=`S05TemporarySetup`に固定します。異なる名前のpolicyが見つかった場合は推測で削除せず、実行計画とpolicy documentを照合できるまで停止します。

- `udemy-agentcore-s05-learner-a`
- `udemy-agentcore-s05-learner-b`
- `udemy-agentcore-s05-exercise-admin`

3 roleの信頼先は、作業環境の認証済みbootstrap sessionからIAM `GetRole`で確認したbootstrap role ARN 1つだけです。実際に確認したARNをtrust policyへ使います。account全体、wildcard、推測したARNを信頼先にしません。ログイン設定やIdentity Centerのsession・permission setは変更しません。

## 事前準備：profileと非公開state

AWS CLIの既存認証済みbootstrap profileを1つ用意してください。3つのS05 profileは、そのbootstrap profileを`source_profile`として、各roleを引き受けます。`source_profile`はPCに実在する認証済みprofile名へ置き換えてください。SSO profileを使う環境では、まず既存の認証手順でlogin済みであることを確認します。profile設定ができない、または想定したroleを引き受けられない場合はAWS操作へ進まず停止してください。

`~/.aws/config`に次のprofileを設定します（`<既存bootstrap-profile>`とaccount IDを置き換えます）。ここに一時credentialを書きません。

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

AWS CloudShellで既存の名前付きbootstrap profileがなく、`aws configure list`のcredential providerが`container-role`と表示される場合は、`source_profile`の代わりに各S05 profileで`credential_source = EcsContainer`を使えます。たとえば上記3 profileの各`source_profile`行を`credential_source = EcsContainer`に置き換えます。これはCloudShellが提供する一時的なcontainer role credentialsをSTSのsourceとして利用し、`~/.aws/credentials`へのcredentialコピーやログイン設定変更をしません。既存の他設定を保持し、3つのS05 profile sectionだけを追加してください。CloudShell外の環境では、実在する認証済みbootstrap profileを使う上記`source_profile`方式を使用します。どちらの方式でも、各profileのSTS結果でaccountが想定値と一致し、Arnが各exact S05 roleのsessionであることを確認してから次へ進みます。

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
  "gateway_tool_name": "udemy-agentcore-s04-cw-target___read_synthetic_cloudwatch",
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

3つのroleを作成し、trustはすべて確認済みのbootstrap IAM role ARN 1つだけにします。例は実行前に完全なJSONを確認してください。

```bash
aws iam create-role --role-name udemy-agentcore-s05-learner-a --assume-role-policy-document file://s05-bootstrap-trust.json
aws iam create-role --role-name udemy-agentcore-s05-learner-b --assume-role-policy-document file://s05-bootstrap-trust.json
aws iam create-role --role-name udemy-agentcore-s05-exercise-admin --assume-role-policy-document file://s05-bootstrap-trust.json
```

実行順は、Learner/Admin用roleとprofileの準備、adminの一時setup policyによるMemory 1つの作成、setup policy削除、exact-resource admin cleanup policyとLearner A/B policyの設定、Learner A/B各自のevent・record保存/読取/分離probe、OFF/ON比較、event/recordの個別削除、Memory削除、最後にIAM/profile cleanupです。Learnerには自分の範囲のdata操作だけを許可し、AdminはMemory作成と対象event/recordの削除に使います。名前衝突や作成の不明な結果がある場合、再作成せず既存stateを調べます。成功時のexact `memory_id`/`memory_arn`、およびGetMemoryで確認した`strategy_id`を、credentialを含めず`.local/s05-state.json`へ記録します。学習者はこのprivate stateからIDを使います。roleのARN、Memory ID、Gateway識別情報はpublic fileに書きません。

```bash
aws iam put-role-policy --role-name udemy-agentcore-s05-exercise-admin --policy-name S05TemporarySetup --policy-document file://s05-admin-setup.json
uv run --project sections/s02 python sections/s05/memory_lab.py create-memory --profile s05-operator --expected-account <account> --admin-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-exercise-admin --confirm-create
aws iam delete-role-policy --role-name udemy-agentcore-s05-exercise-admin --policy-name S05TemporarySetup
```

Create結果とGetMemoryで状態が`ACTIVE`であることを確認後、exact-resource admin policyを付け、Learner A/B policyを付けます。bootstrap roleには権限を追加しません。

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

## Memory OFF/ON比較と現在情報の再取得

各`ask`は新しいStrands agentとして作成され、会話履歴はmode間で共有されません。Memory OFFではMemory retrieval toolをagentへ登録しません。Memory ONでは自分のactor namespaceだけを検索するtoolを1つ登録します。

最初に同じ好みの質問をOFF/ONそれぞれ1回ずつ実行します。OFFにはMemory toolを登録しませんが、S04 Gateway toolは利用可能です。質問が好みだけなら通常Gatewayも呼ばず`tools`が空になります。ONでMemory由来と判断する条件は、`AgentCore Memory` traceが1件、namespaceが自分の`/actors/learner-a/`、record_countが1以上、record_idsに先ほどのexact IDがあり、回答がその内容を使っていることです。traceがない、recordが空/異なる、または回答が合わない場合、Memoryの効果として説明しません。回答とtoken usageを記録します。

```bash
uv run --project sections/s02 python sections/s05/memory_lab.py ask --profile s05-learner-a --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b --memory-id <exact-memory-id> --strategy-id <exact-strategy-id> --gateway-endpoint <exact-S04-endpoint/mcp> --memory-mode off --question "What dashboard preference did I ask you to remember?"
uv run --project sections/s02 python sections/s05/memory_lab.py ask --profile s05-learner-a --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b --memory-id <exact-memory-id> --strategy-id <exact-strategy-id> --gateway-endpoint <exact-S04-endpoint/mcp> --memory-mode on --question "What dashboard preference did I ask you to remember?"
```

まずLearner A/Bそれぞれのprofileで、同じ質問・UTC範囲をMemory OFF/ON各1回ずつ実行します。各runではS04 Gatewayを1回だけ読み、ONだけがMemory toolを登録して自分のnamespaceを検索します。`tools`出力でMemoryの有無、namespace、record IDと、Gatewayの入力・出力を確認します。long-term Memory recordの本文はtoolへ返るため、本人の画面で内容も確認できます。S04のsynthetic logは演習用の履歴データで、実際の現在障害を示しません。使うUTC範囲はS04 exerciseで確認した範囲を質問へそのまま転記し、source eventがあることを確認します。Gateway traceや範囲が見えない、`ok`でない、範囲外のevent、または期待データがなければ回答をsource evidenceとして扱わず停止します。実データや顧客情報を質問へ入れません。

```bash
uv run --project sections/s02 python sections/s05/memory_lab.py ask --profile s05-learner-a --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b --memory-id <exact-memory-id> --strategy-id <exact-strategy-id> --gateway-endpoint <exact-S04-endpoint/mcp> --memory-mode off --question "Use data_type=logs. For catalog-api, summarize the synthetic 5xx log events between 2026-10-15T23:50:00Z and 2026-10-15T23:53:00Z UTC, and state how I should display timestamps. Also recall my saved dashboard display preference."
uv run --project sections/s02 python sections/s05/memory_lab.py ask --profile s05-learner-a --expected-account <account> --actor-a-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-a --actor-b-role-arn arn:aws:iam::<account>:role/udemy-agentcore-s05-learner-b --memory-id <exact-memory-id> --strategy-id <exact-strategy-id> --gateway-endpoint <exact-S04-endpoint/mcp> --memory-mode on --question "Use data_type=logs. For catalog-api, summarize the synthetic 5xx log events between 2026-10-15T23:50:00Z and 2026-10-15T23:53:00Z UTC, and state how I should display timestamps. Also recall my saved dashboard display preference."
```

このUTC範囲はS04の合成catalog-apiデータで確認した履歴例で、実際の現在障害ではありません。別の演習データを使う場合は、S04で確認したUTC範囲をそのまま指定します。S02環境を既に準備している場合は`uv run --project sections/s02 ...`を使います。CloudShellに`uv`がなく、S02 `.venv`がある場合は`.venv/bin/python sections/s05/memory_lab.py ...`を使います。Learner A/BごとにMemory OFF/ON各1回を行い、S04 Gatewayは最大4回、Memory検索は最大2回です。CLI出力では回答文の下に、`tools`で各Gateway/Memory呼出し（Memory namespace・record ID、Gateway入力・結果）、`token_usage`で入力/出力/total token usageが表示されます。ONの回答が表示preferenceを反映し、同時にGateway traceがこの履歴イベントを示すか確認し、回答・trace・token usageを受講者ノートへ記録します。credentialをコマンド・state・共有画面に表示しないでください。

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

次にIAM cleanupを行います。`<既存bootstrap-profile>`を実際の認証済みprofileへ置き換え、cleanup直前にSTS callerを確認します。Accountが期待した学習用accountと一致し、Arnに保存済み`.local/s05-state.json`の`bootstrap_role_arn`で記録したroleが含まれることを確認します。実環境のbootstrap role名を教材例へ固定しません。3 roleそれぞれについて`GetRole`、`list-role-policies`、managed policy/instance profile一覧を確認し、Learner A=`S05LearnerA`、Learner B=`S05LearnerB`、admin=`S05AdminCleanup`（setup policy `S05TemporarySetup`が残っている場合のみ追加）と照合します。名前やpolicy documentが想定外なら停止します。S05用と確認したinline policyだけをexact名で削除し、各roleのinline policy一覧が空であることを再確認してからroleを削除します。各削除の直前にもSTS callerを確認します。

以下はexact targetの読み取り例です。`<既存bootstrap-profile>`は実際に使用する既存認証済みbootstrap profile名へ置き換えます。

```bash
aws iam get-role --role-name udemy-agentcore-s05-learner-a --profile <既存bootstrap-profile> --output json --no-cli-pager
aws iam get-role --role-name udemy-agentcore-s05-learner-b --profile <既存bootstrap-profile> --output json --no-cli-pager
aws iam get-role --role-name udemy-agentcore-s05-exercise-admin --profile <既存bootstrap-profile> --output json --no-cli-pager
aws iam list-role-policies --role-name udemy-agentcore-s05-learner-a --profile <既存bootstrap-profile> --query 'PolicyNames' --output json --no-cli-pager
aws iam list-role-policies --role-name udemy-agentcore-s05-learner-b --profile <既存bootstrap-profile> --query 'PolicyNames' --output json --no-cli-pager
aws iam list-role-policies --role-name udemy-agentcore-s05-exercise-admin --profile <既存bootstrap-profile> --query 'PolicyNames' --output json --no-cli-pager
aws iam list-attached-role-policies --role-name <exact-S05-role-name> --profile <既存bootstrap-profile> --output json --no-cli-pager
aws iam list-instance-profiles-for-role --role-name <exact-S05-role-name> --profile <既存bootstrap-profile> --output json --no-cli-pager
aws iam get-role-policy --role-name udemy-agentcore-s05-learner-a --policy-name S05LearnerA --profile <既存bootstrap-profile> --output json --no-cli-pager
aws iam get-role-policy --role-name udemy-agentcore-s05-learner-b --policy-name S05LearnerB --profile <既存bootstrap-profile> --output json --no-cli-pager
aws iam get-role-policy --role-name udemy-agentcore-s05-exercise-admin --policy-name S05AdminCleanup --profile <既存bootstrap-profile> --output json --no-cli-pager
```

`S05TemporarySetup`削除は次の条件付き手順です。admin roleのinventoryにこのexact nameが残っている場合に限りpolicy documentを読み、S05一時setup policyと確認できた場合だけ実行します。見つからない場合は削除を省略します。別の名前や内容なら停止します。

```bash
aws iam get-role-policy --role-name udemy-agentcore-s05-exercise-admin --policy-name S05TemporarySetup --profile <既存bootstrap-profile> --output json --no-cli-pager
```

上記のdocumentがS05一時setup policyと照合できた場合のみ:

```bash
aws sts get-caller-identity --profile <既存bootstrap-profile> --output json --no-cli-pager
aws iam delete-role-policy --role-name udemy-agentcore-s05-exercise-admin --policy-name S05TemporarySetup --profile <既存bootstrap-profile> --no-cli-pager
```

次に、inventoryで実在し、S05専用と照合済みの各policyに限り、各削除の直前に同じSTS確認をしてから exact `DeleteRolePolicy` を実行します（policy名がlist結果にない場合はskipし、別名があれば停止）。例:

```bash
aws sts get-caller-identity --profile <既存bootstrap-profile> --output json --no-cli-pager
aws iam delete-role-policy --role-name udemy-agentcore-s05-learner-a --policy-name S05LearnerA --profile <既存bootstrap-profile> --no-cli-pager
```

各roleでinline policy一覧が空になった後、各`DeleteRole`の直前にもSTSを確認してroleを削除します。

```bash
aws iam list-role-policies --role-name <exact-S05-role-name> --profile <既存bootstrap-profile> --query 'PolicyNames' --output json --no-cli-pager
aws sts get-caller-identity --profile <既存bootstrap-profile> --output json --no-cli-pager
aws iam delete-role --role-name udemy-agentcore-s05-learner-a --profile <既存bootstrap-profile> --no-cli-pager
```

最後に3つのexact role名が`GetRole`で`NoSuchEntity`となり、既存S03 Runtime/S04 Gateway/Lambdaとそれらのroleが前後のread-only inventoryで不変なことを確認します。削除要求の結果が曖昧なら盲目的に再実行せず、状態を照合してから判断してください。S03 Runtime、S04 Gateway/Lambdaやそれらのroleは削除・変更しません。

3つのexact roleがすべて不存在と確認できた後、この演習で追加した`~/.aws/config`内の`[profile s05-learner-a]`、`[profile s05-learner-b]`、`[profile s05-operator]` sectionだけを削除します。元から存在したsectionは削除せず、他のprofile、default/bootstrap/SSO設定、認証設定は保持してください。config全体や`~/.aws/credentials`ファイルを削除しないでください。

## 費用と所要時間

上限計算の作業量は、Memory 1つ、合成event 2件、短期event retrieval 2回、長期record retrieval 6回（手動read-records 2回とMemory ONのask 4回）、agent呼び出し8回、既存S04 Gateway/Lambda read 8回です。

2026-10-16時点で公開されているAgentCore料金では、短期Memoryはingestion `$1/GB`、retrieval `$0.20/GB`、storage `$0.10/GB-month`です。ingestion/retrievalはeventごとの最小12KB、最大64KB、storageは実event sizeです。長期built-in strategyのstorageは`$0.75/1000 records-month`、retrievalは`$0.50/1000 requests`です。したがって2つの小さいeventのingestion約`$0.000025`、2回の短期event retrieval約`$0.000005`、record 2つを約1か月置くstorage約`$0.0015`、長期retrieval 6回は`$0.003`です。Eventの3日storage費はevent本体が1KB未満なら1セントのさらに小さい桁です。

S02 agentには1 invocationあたり6,000 total tokens、うち出力2,048 tokens、最大4 turnの意図したbudgetを渡します。ただしStrandsの実行でこの値がprovider-enforced hard capになる保証はなく、実際に報告されるusageがtotal 6,000を超える場合があります。毎回`token_usage`の実測値を記録し、上限とみなして費用を計算しないでください。AWS Price List APIで2026-10-01 effectiveのTokyo Nova Lite on-demand単価は入力`$0.072/1M tokens`、出力`$0.288/1M tokens`でした。8回すべてが意図したbudgetに収まる場合、入力31,616、出力16,384 tokenとして約`$0.00698`です。これはbudgetどおりだった場合の直接model invocation費用の計画値であり、実費の上限やdiscountを保証するものではありません。実測token usageを単価に掛けた金額を更新し、請求前に実際のregion、model ID、契約条件を再確認します。

既存Gatewayの8 invokeはAgentCore Gateway `$0.005/1000 invocation`で約`$0.00004`です。既存LambdaはS04の30秒timeout、256MB設定を保守的に8回全て使う仮定でも約60 GB-secondです。Lambda通常のrequest/compute課金を適用した理論値は約`$0.001`（request分はさらに小さい）です。実行時間が短ければ低くなります。CloudWatch Logsの取り込み・保存はLambdaが出すログ量、保存期間、accountの既存使用量に応じて変動するため、正確な配賦額はこの手順だけでは確定できません。実行前後にaccountの利用量を確認してください。

上記の数値を合算した直接サービス費は約`$0.01257`、安全側に丸めて`$0.013`です。内訳のCloudWatch LogsはTokyoのingestion `$0.76/GB`、Standard storage `$0.033/GB-month`で試算します。Lambda 8回につきAWS platform logを保守的に各2KiBと仮定すると、ingestionは約`$0.000012`、1か月storageでも約`$0.000001`未満です。これは出力量の計画仮定で、provider-enforced上限ではありません。実際のログ量・retention/account既存利用量を実行前後に確認し、増分は見積もりに加えます。`FilterLogEvents` read API自体にはCloudWatch Logs Insightsのdata-scanned料金を適用しません。Built-in `USER_PREFERENCE`の抽出・統合はAgentCore管理の組み込み処理です。override/self-managed strategyのように自分のaccountへ追加のBedrock model usageを課金する構成ではありません。これらの料金、model ID、free tier、既存CloudWatch利用量は変わり得るため、実行前にTokyo価格と現在の請求条件を確認してください。`$2`はprovider-enforced上限ではなく、追加実行前に中止して再評価する運用閾値です。見積りが近づく前に利用状況を確認し、想定外の課金や抽出遅延があればイベント・agent追加実行を止めます。1時間は作業時間の目安で、請求反映の保証ではありません。

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
