# Amazon Bedrock AgentCore Operations Hands-on

This repository contains hands-on materials for a course that starts with Amazon Bedrock and builds toward operating an AI agent with Amazon Bedrock AgentCore. Learners first make a model-only request and inspect Bedrock's request and response. They then run the model-to-tool-to-answer flow locally, where a Python application executes the model's tool request and returns the result. After checking that behavior locally, they build an agent and move it to AgentCore Runtime. The course then covers Gateway, Memory, observability and answer evaluation, troubleshooting, updates, costs, and cleanup. Finally, learners investigate a synthetic 5xx incident and separate observed facts, possible causes, and follow-up checks.

You do not need previous experience with Amazon Bedrock or another AI course. The course assumes familiarity with general API usage and AWS authentication, as well as AWS operations such as IAM, the AWS CLI, and CloudWatch Logs. You should be able to read short Python examples with functions, dictionaries, and exceptions; the course provides support for the Python needed in its exercises. Bedrock-specific request and response formats and AgentCore-specific authentication paths are introduced in the course. Bring an AWS account and working CLI credentials; general AWS authentication instructions are outside the course scope.

Materials are added by section as they are prepared. A section is ready to run only when its instructions and required files are present.

The course design for S04 is to use an AgentCore Gateway read tool to inspect a bounded record from an S03 Runtime execution, then compare that evidence with the agent's answer. The S04 materials linked below have not yet been updated to that design and still describe synthetic CloudWatch data. Follow the linked section only when its complete instructions and required files are available; the course design does not mean that its implementation is complete.

- [s02 Bedrockの基本から、ローカルでツールを使うエージェントを作る](sections/s02/README.md)
- [s03 AgentCore Runtimeへデプロイする](sections/s03/README.md)
- [s04 AgentCore Gatewayでログ取得ツールをエージェントにつなぐ (course design; linked materials not yet updated)](sections/s04/README.md)
- [s05 AgentCore Memoryで利用者の好みを次の会話へ引き継ぐ](sections/s05/README.md)
- [s06 エージェントの動作を観測し、回答の品質を評価する](sections/s06/README.md)
- [s07 エージェントのエラーを再現し、原因を調べて直す (local fault examples; AWS failures shown from records)](sections/s07/README.md)
- [s08 エージェントの更新・ロールバック・費用管理](sections/s08/README.md)
- [s09 総合演習：5xxエラーを調べるAWS運用調査エージェント](sections/s09/README.md)
- [s10 おわりに：演習環境の後片付けと実務への応用](sections/s10/README.md)

## Safety and cost

- Never commit AWS credentials, API keys, tokens, personal data, or other secrets. Use your normal AWS credential provider; do not paste credentials into code or configuration files.
- Hands-on exercises may create billable AWS resources or incur model, logging, and storage charges. Review each section's resource list and current AWS pricing before running it, use a learning account, and complete its cleanup steps.
- Run a section only when its complete instructions and required files are available.
