# Billing assumptions

Verified: 2026-11-03 (Phase 0). Re-verify at the start of every phase.

## Verified facts
1. Agent SDK and `claude -p` with a Claude plan login (OAuth) count against the
   normal subscription usage limits. The separate monthly "Agent SDK credit"
   planned for 2026-06-15 was paused and is not active.
   Source: https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan
   (page shows an update dated 2026-10-07).
2. Since 2026-10-07, Max, Ultra and Team plans include monthly API credits.
   They apply only to usage with an API key from a linked Console organization.
   This design does NOT use them (hard rule: no API key).
   Same source as above.
3. OAuth login is for the plan owner's ordinary use. Advertised limits "assume
   ordinary, individual usage of Claude Code and the Agent SDK". Developers who
   build products for other users must use API keys.
   Source: https://code.claude.com/docs/en/legal-and-compliance
4. Anthropic does not allow routing other people's requests through plan
   credentials. This system is personal use only.
   Sources: legal-and-compliance page above and
   https://code.claude.com/docs/en/agent-sdk/overview (note box).

## Not verified (open risks)
- No official API shows the remaining usage window. The system estimates it
  and treats a limit error from the CLI/SDK as the ground truth.
- "Ordinary individual usage" has no numeric definition. Heavy unattended
  loops may hit limits faster than expected or draw review. Keep runs sequential.
- Whether the account has "extra usage" enabled: only the owner can see this in
  claude.ai settings. The guard cannot read it. The owner must check by hand.

## Environment variables the billing guard must reject
ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL,
CLAUDE_CODE_USE_BEDROCK, CLAUDE_CODE_USE_VERTEX, CLAUDE_CODE_USE_FOUNDRY,
AWS_BEARER_TOKEN_BEDROCK, ANTHROPIC_VERTEX_PROJECT_ID, ANTHROPIC_FOUNDRY_API_KEY,
apiKeyHelper in any settings.json.
