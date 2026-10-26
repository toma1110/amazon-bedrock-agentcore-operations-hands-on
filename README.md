# Amazon Bedrock AgentCore Operations Hands-on

This repository contains hands-on materials for a course that starts with Amazon Bedrock and builds toward operating an AI agent with Amazon Bedrock AgentCore. Learners first make a model-only request, then follow a model's tool request as a Python application runs the tool and returns its result. They then build an agent and study Runtime, Gateway, Memory, observability, troubleshooting, updates, costs, and cleanup. Finally, they apply these skills to a synthetic 5xx incident and separate observed facts, possible causes, and follow-up checks.

You do not need previous experience with Amazon Bedrock, its APIs, or another AI course. The course assumes general AWS operations knowledge such as IAM, the AWS CLI, and CloudWatch Logs. You should be able to read short Python examples with functions, dictionaries, and exceptions; the course provides support for the Python needed in its exercises.

Materials are added by section as they are prepared. A section is ready to run only when its instructions and required files are present.

The course design for S04 is to use an AgentCore Gateway read tool to inspect a bounded record from an S03 Runtime execution, then compare that evidence with the agent's answer. The S04 materials linked below have not yet been updated to that design and still describe synthetic CloudWatch data. Follow the linked section only when its complete instructions and required files are available; the course design does not mean that its implementation is complete.

- [s02: Build a local read-only agent](sections/s02/README.md)
- [s03: Run the same agent on AgentCore Runtime](sections/s03/README.md)
- [s04: Read an S03 Runtime execution record through an AgentCore Gateway (course design; linked materials not yet updated)](sections/s04/README.md)
- [s05: Save and isolate non-sensitive user preferences](sections/s05/README.md)
- [s06: Follow model and tool execution with trace-correlated logs and metrics](sections/s06/README.md)
- [s07: Troubleshoot agent failures (local fault examples; AWS failures shown from records)](sections/s07/README.md)
- [s08 更新の影響と費用を確認する](sections/s08/README.md)
- [s09: AWS運用調査エージェント](sections/s09/README.md)
- [s10: 後片付けと次に学ぶこと](sections/s10/README.md)

## Safety and cost

- Never commit AWS credentials, API keys, tokens, personal data, or other secrets. Use your normal AWS credential provider; do not paste credentials into code or configuration files.
- Hands-on exercises may create billable AWS resources or incur model, logging, and storage charges. Review each section's resource list and current AWS pricing before running it, use a learning account, and complete its cleanup steps.
- Run a section only when its complete instructions and required files are available.
