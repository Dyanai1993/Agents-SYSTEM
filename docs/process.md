# Process: what "a phase is finished" means

Every phase ends with these steps, in this order. Claude Code (the builder) makes all changes. A separate reviewer only reviews.

1. All unit tests pass (`python -m unittest discover -s tests -t .`) and the eval selfcheck passes.
2. Docs are updated (`README.md`, `docs/architecture.md`, `docs/review-response.md` when a review changed something).
3. **Self-review prompt**: write `docs/reviews/phase<N>.json` (claims, files, known weak points, how to verify, specific questions),
   then run `python scripts/gen_review_prompt.py <N>`. It writes `docs/reviews/phase<N>-review-prompt.md` with direct links to the
   repository, the branch, the pull request and each file. The weak points must be honest: list what was never run live.
4. The branch is pushed and a PR is opened. The user passes the prompt to the reviewer. The reviewer must not change files.
5. The builder reads the report, fixes what is real, records the answer in `docs/review-response.md`, and regenerates the prompt.

Repository: https://github.com/Dyanai1993/Agents-SYSTEM
