# Security

- API Key 和 GitHub Token 只通过环境变量或用户输入提供。
- 不在日志中打印 token。
- MVP 阶段不写回 GitHub，只生成草稿。
- GitHub Token 如需持久化，必须加密保存。
