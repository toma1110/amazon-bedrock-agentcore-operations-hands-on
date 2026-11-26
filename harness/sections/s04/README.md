# 第4章 Gateway・Identity・Policyでツールへのアクセスを管理する

## この章で学ぶこと

ログ取得APIをMCPツールとして登録し、Gatewayの呼び出し元が使う権限と、Gatewayが接続先APIを呼ぶ権限を確認します。2つの演習用IAMロールで同じツールを呼び出し、`LOG_ONLY`と`ENFORCE`でPolicyの判定と実際の結果を比べます。IdentityのOAuth例で、利用者が同意する範囲を確かめます。

## このページで案内する講義

- `s04-l03`　ログ取得APIをMCPツールとして登録する
- `s04-l04`　MCPクライアントとHarnessからツールを呼び出す
- `s04-l06`　Cedar Policyでツール呼び出しの許可・拒否を確かめる

## 次の章へ

Harness、Gateway、ツール、呼び出し権限の対応を控えます。第5章ではHarnessにMemoryを加え、保存・分離・削除を確かめます。

実行手順と配布ファイルは準備中です。[章一覧に戻る](../../../README.md)
