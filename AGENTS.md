# SupportGuard Operating Constitution

## Purpose

SupportGuard is an interview-focused AI SaaS support Agent for AI application and Agent engineering roles.
The repository must remain safe, evidence-backed, understandable by its author, and honest about what is
production-shaped versus actually production-integrated.

## Current Authority

1. `docs/interview-edition-simplification-v2.0.md` is the approved and frozen authority for product display
   scope, simplification, Phase 0～7, complexity budgets, verification, Archive, and author handoff.
2. This file is the project-level operating constitution for development, Git, safety, validation, and
   Confirmation Gates.
3. Historical v1.2～v1.7 specifications, Addenda, Baselines, Receipts, Matrixes and Manifests are read-only
   evidence. They do not authorize new work or historical Gate / Parity reruns.
4. The pre-simplification Archive baseline is exactly
   `6255c8c0eb0dcedd877bfbf16a9695dad2a0c9eb`.
5. The user explicitly authorized v2.0 Phase 0～7 execution on 2026-08-11. That Prompt authorizes the exact
   Archive annotated Tag, later in-scope historical-file / Migration migration after their prerequisites,
   verified Phase commits, and pushes to the existing `origin/main`.
6. The execution Prompt does not authorize an extra remote Branch, repository creation / rename / deletion,
   visibility changes, protected Evaluation access, or making Interview Edition the default before final
   Human Acceptance.
7. Current Phase, exact Candidate, local receipt, and Hosted CI disposition are recorded in
   `docs/release-verification.md`. Phase 6 Candidate `30254587585fa2169cab071a926c501e06dac9a6`
   completed controlled pruning and the Authority Transition; the current phase is Phase 7, blocked before
   protected execution by Hosted CI's external zero-step account condition. This constitution never upgrades
   historical or local evidence into a Hosted CI or final Definition of Done claim.
8. On 2026-08-12 the user explicitly authorized the history-free, MIT-licensed public mirror
   `Teddy-creator/SupportGuard-Interview`. This authorization does not publish or modify the private canonical
   repository, its Archive Tags, historical Actions records, protected Evaluation inputs, or private evidence.

When rules conflict, apply: explicit v2.0 clause → inherited safety invariants below → this operating
constitution → historical evidence as read-only facts.

## Inherited Safety Invariants

- Use one explicit bounded LangGraph Agent, not a multi-Agent Runtime.
- Preserve `Decision → Read Tool → Observation → Replan / Stop`; a one-shot ReadPlan is not Tool Calling.
- The LLM may classify, select read tools, propose answers, or form typed action candidates. It never grants
  authorization.
- Read MCP is read-only. Proposal MCP creates inert proposals only when invoked by deterministic Policy.
- Refund, API key revocation, and entitlement execution remain Runtime-only after independent approval.
- RAG documents, Memory, Prompt text, Provider output and MCP observations never receive Mutation authority.
- PostgreSQL is authoritative state. Redis is asynchronous delivery, not business truth.
- Preserve trusted identity, tenant scope, RLS, approval scope, transaction-time revalidation, idempotency,
  Lease / Fence, effect-once, Checkpoint / Resume, and immutable audit lineage.
- Memory is non-authoritative history. Current business facts must come from fresh scoped observations.
- Customer and interview surfaces must not expose Secret, raw Provider payload, Prompt, private
  Chain-of-Thought, or another tenant's resource existence.
- Missing Secret, Provider initialization failure, native Tool Calling failure, and dependency failure must
  fail closed; never silently switch to Fake in real mode.

## v2.0 Scope Boundaries

- Keep the three primary demos: 429 diagnosis, duplicate-charge refund with HITL, and cross-tenant denial.
- Keep refund, API key revocation, and entitlement change on one shared ActionSpec / Approval / RuntimeEffect
  pipeline.
- Keep PostgreSQL, pgvector, Redis Streams, two stdio MCP servers, RAG citations, scoped Memory and Trace.
- Do not add Operator Inbox, SLA, full IAM, external payment / key / quota integrations, multi-Agent,
  Milvus, PDF / OCR / VLM, a second queue, dynamic model routing, or default online Cross-Encoder.
- Do not access Evaluation v6 Holdout, run Cross-Encoder, historical Gate / Parity, or invocation eight.
- Do not add Case-ID, exact-demo-text, evaluation-keyword, or fixed-output branches.

## Historical Evidence Discipline

- Never rewrite a historical Candidate SHA, score, failure, Receipt, cost, Hash, Matrix, Manifest, Prompt, or
  corpus to make it appear successful.
- Never rerun a consumed historical Candidate or use v2.0 public regression authority to replay a historical
  Formal Gate.
- Historical `37/37` belongs only to its recorded Candidate. Current HEAD must have its own verification
  statement.
- Archive and pruning must follow v2.0 Authority Transition, SHA-256 Manifest, annotated-tag binding, restore
  dry-run, Test Disposition Manifest, and Schema Equivalence requirements.

## Development Workflow

Before work:

1. Read this file and the complete v2.0 specification.
2. Check worktree, branch, HEAD, `origin/main`, user modifications, and current Phase evidence.
3. Build a Phase plan. Treat existing code and tests as baseline, not automatic proof of v2.0 completion.

For every authorized Phase:

1. Implement only the frozen scope.
2. Add or update deterministic tests at the owning public contract.
3. Run applicable Unit, Contract, Integration, PostgreSQL/RLS, MCP, two-worker, Frontend, Lint, Mypy,
   Security, wheel, Compose and documentation checks.
4. Inspect Diff, dependency direction, safety invariants, user changes and cleanup ownership.
5. Fix failures and rerun affected validation.
6. Re-read every Phase requirement. If any item remains incomplete, continue in the same Phase; a commit does
   not prove Phase completion.
7. Create one explainable, verified Commit and push `origin/main` only after checks pass.

Phase 1 may register Hosted CI account failure as the v2.0-defined Phase 7 Release Blocker. No other unmet
Phase item may be silently deferred.

## Provider and Evaluation Discipline

- Deterministic tests use Fake Provider and deterministic embedding fixtures.
- Real mode uses the frozen DeepSeek configuration and native Tool Calling; never log or commit API keys.
- Freeze IE-P16, IE-F06, IE-J12 and RAG Dev30 before executing them.
- IE-P16 and IE-F06 have separate denominators and claims. Fault injection is not Provider-quality evidence.
- One complete IE-P16 is allowed per Candidate. Preserve all attempts, usage, failures and cleanup.
- Any IE-P16 failure stops at a Confirmation Gate. A replacement Candidate requires explicit user approval
  and again stops if it fails.
- Do not tune against Evaluation v6 Holdout. Public Dev regression is not independent generalization proof.
- Pause before external API spend is expected to exceed CNY 30.

## Git, Files and Dependencies

- Preserve user modifications. Never use destructive Git operations or rewrite shared history without explicit
  approval.
- Use `apply_patch` for manual file edits. Use formatters only for bounded mechanical rewrites.
- Keep commits single-purpose and auditable. Do not claim unrun commands or tests passed.
- Do not commit Secret, local `.env`, raw external payload, private trace, or Chain-of-Thought.
- Runtime and Validation packaging must remain installable from clean environments.
- Historical test removal requires a Requirement ID → replacement test mapping and simultaneous green old /
  new safety coverage before pruning.

## Docker and Process Hygiene

- Use a named project and owned builder for validation. Do not run global Docker prune.
- Record resources before and after validation; clean only invocation-owned containers, images, builders,
  volumes, temporary files and MCP children.
- Shared-daemon cache is not owned-clean evidence. Report it as unverifiable rather than claiming clean.
- Verify MCP initialization, discovery, Schema Hash, allowlist, reconnect, close, and zero orphan processes.

## Confirmation Gates

Pause and ask the user when:

- Phase 0～7 has not been explicitly authorized;
- creating or deleting Archive Tag / Branch, migrating files off the default branch, or replacing Migration
  history is required but not explicitly authorized by the execution Prompt;
- changing the three demos, three action types, product policy, state-machine meaning, database grants,
  MCP / Runtime isolation, or another inherited safety invariant is required;
- creating, renaming, deleting, publishing, or changing visibility of a remote repository is required;
- a required Secret, account, permission, or external service is unavailable;
- Hosted CI is zero-step or blocked by Billing, Spending Limit, Actions permission, or Runner quota;
- expected external API spend exceeds CNY 30;
- any complete IE-P16 fails or a replacement Candidate would be needed;
- Evaluation v6 Holdout, Cross-Encoder, historical Gate / Parity, or invocation eight would be accessed;
- existing user work cannot be preserved safely;
- an irreversible external action is required;
- all engineering DoD is complete and the user's author-ownership Human Acceptance is required.

## Communication

Progress updates should state current Phase, verified result, next action and real blocker. Final reports must
separate actual results, historical evidence, unexecuted evaluation, known limitations, cleanup, and the final
Commit ID.
