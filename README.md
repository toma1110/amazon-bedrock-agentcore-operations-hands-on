# Amazon Bedrock AgentCore Operations Hands-on

Build the confidence to investigate an AI agent's behavior and make sound operational decisions. Starting without prior Amazon Bedrock experience, you will trace a model request through tool use, run the agent locally and on Amazon Bedrock AgentCore Runtime, and use evidence to distinguish failures, permissions, and changes. By the end, you will be able to explain what the agent did, what the available evidence supports, and what to check before making an operational decision.

Materials are added by section as they are prepared. A section is ready to run only when its instructions and required files are present.

- [s02: Build a local read-only agent](sections/s02/README.md)
- [s03: Run the same agent on AgentCore Runtime](sections/s03/README.md)
- [s04: Compare an S03 Runtime invocation with records read through an AgentCore Gateway](sections/s04/README.md)
- [s05: Save and isolate non-sensitive user preferences](sections/s05/README.md)
- [s06: Follow model and tool execution with trace-correlated logs and metrics](sections/s06/README.md)
- [s07: Investigate agent failures with local fault examples, one allowed AWS invocation, and read-only log inspection](sections/s07/README.md)
- [s08 更新の影響と費用を確認する](sections/s08/README.md)
- [s09: AWS運用調査エージェント](sections/s09/README.md)
- [s10: 後片付けと次に学ぶこと](sections/s10/README.md)

For S09, use the shared [sample app README](https://github.com/toma1110/aws-observability-sample-app/blob/v1.0.2/README.md) at v1.0.2 (`3a3599121537bb83a9b7afd42162a8635aeee59b`). Follow it to run the sample app, then investigate its real CloudWatch logs in the S09 exercise. The sample app's code and infrastructure are maintained in that repository and are not duplicated here.

## Safety and cost

- Never commit AWS credentials, API keys, tokens, personal data, or other secrets. Use your normal AWS credential provider; do not paste credentials into code or configuration files.
- Hands-on exercises may create billable AWS resources or incur model, logging, and storage charges. Review each section's resource list and current AWS pricing before running it, use a learning account, and complete its cleanup steps.
- Run a section only when its complete instructions and required files are available.
