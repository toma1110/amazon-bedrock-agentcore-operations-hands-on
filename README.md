# Amazon Bedrock AgentCore Operations Hands-on

This repository will provide the hands-on materials for a course on operating AI agents with Amazon Bedrock AgentCore. The course focuses on deploying and operating a read-oriented agent, understanding identity and permissions, investigating failures with evidence, checking changes and costs, and cleaning up learning resources.

Materials are added by section as they are prepared. A section is ready to run only when its instructions and required files are present.

- [s02: Build a local read-only agent](sections/s02/README.md)
- [s03: Run the same agent on AgentCore Runtime](sections/s03/README.md)
- [s04: Read synthetic CloudWatch data through an AgentCore Gateway](sections/s04/README.md)
- [s05: Save and isolate non-sensitive user preferences](sections/s05/README.md)
- [s06: Follow model and tool execution with trace-correlated logs and metrics](sections/s06/README.md)
- [s07 草案・AWS未実証: エージェントの失敗を調べて復旧する](sections/s07/README.md)
- [s08 更新の影響と費用を確認する](sections/s08/README.md)

## Safety and cost

- Never commit AWS credentials, API keys, tokens, personal data, or other secrets. Use your normal AWS credential provider; do not paste credentials into code or configuration files.
- Hands-on exercises may create billable AWS resources or incur model, logging, and storage charges. Review each section's resource list and current AWS pricing before running it, use a learning account, and complete its cleanup steps.
- Run a section only when its complete instructions and required files are available.
