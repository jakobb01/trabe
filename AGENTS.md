# Working on trabe

At the start of a new conversation, read `docs/project-status.md` for the project
handoff. Verify the current worktree and relevant deployment evidence before
assuming a release is deployed or changes are committed. Update that note after
meaningful changes so another conversation can continue without this chat history.

Follow the release priorities in `docs/next-features.md` and the testing policy in
`CONTRIBUTING.md`. Keep changes small and deliver usable Telegram features.

- For ordinary code changes, run the relevant unit tests and lint/format checks
  for changed Python files. Preserve basic checks for money arithmetic, owner
  authorization, persistence and alert deduplication when those areas change.
- Documentation-only changes need no app tests, image build or Pi deployment.
  For CI/shell changes, check the changed configuration/shell syntax.
- Integration suites, container builds, ARM/QEMU checks, storage soaks and broad
  repeated test runs are not routine requirements. Use an extended check only
  when the user requests it or a specific change/failure makes it relevant; name
  the reason and run it once. Fixes can justify rerunning the affected check.
- Reuse passing results for unchanged code. For a deployment, build once and use
  its existing readiness/delivery checks plus one relevant Telegram walkthrough.
  Avoid duplicate local and Pi validation of the same behavior.
- Use fixtures for development. Keep only the deployed process polling the real
  Telegram bot; credentials and runtime state stay outside Git.
