# S07: エージェントの失敗を調べて復旧する（草案）

S07では、エージェントのツール、モデル設定、API応答に起きた問題を識別し、安全な範囲で原因を調べて復旧する方法を学ぶ予定です。現時点では教材の実装草案とローカルの合成APIスタブがある段階で、AWS上のハンズオンは未実証です。このREADMEやコードをAWSの実行手順として使用しないでください。

## ローカルテスト

AWS APIを呼び出さずに、固定した失敗ケースと入力制限を確認できます。教材リポジトリのrootで実行してください。

```text
uv run --project sections/s02 --with pytest pytest sections/s07/tests
```

## ローカル実行成果物の生成

Runtime用ZIPを作る場合は、repository rootから未使用の一意な出力directoryを指定します。出力先がすでに存在するときは、既存成果物を上書きせず停止します。

```text
uv run --project sections/s02 python sections/s07/package_artifacts.py --output sections/s07/build-s07-run-01
```

依存packageの配置とZIP検査はrepository外の一時directoryで行い、検査済みZIPだけを指定した出力先へコピーします。同じ出力先は再利用しないでください。

## AWSハンズオン

AWS環境での実行、ログ・トレースの確認、復旧、後片付けの手順は、動作確認が完了するまで提供していません。
