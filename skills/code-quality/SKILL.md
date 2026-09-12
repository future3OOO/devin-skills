---
name: code-quality
description: Compact quality rubric: the seven principles for judging a change. Use when reviewing or challenging a diff, as an advisor or review rubric, or when another skill needs the quality-rule vocabulary; for implementing production changes use production-code.
---

# Code Quality Rubric

This skill owns the seven quality principles below. `production-code` extends
them with execution procedure; it does not redefine them.

Judge the changed surface, not unrelated legacy debt. Cite the diff, contract,
and concrete proof. The hard invariants remain owned by `AGENTS.md`.

## Seven principles

### 1. Minimal code

- Is this the smallest correct change?
- Did it remove code it made obsolete?
- Did it avoid one-off wrappers, pass-through modules, and speculative options?

### 2. No duplicated behavior

- Does equivalent behavior already have an owner to reuse or extend?
- Is each behavior implemented once?
- Is apparent consolidation genuinely shared rather than merely similar?

### 3. Direct data and control flow

- Are I/O, state transitions, and control flow explicit and traceable?
- Did the change add avoidable hops, transforms, retries, or orchestration?
- Are inputs validated once at their trust boundary?

### 4. No fake-green escape

- Does every claimed proof satisfy the canonical mock ban?
- Did the change suppress, swallow, disable, or bypass a real failure?
- Are failures corrected at their source rather than muted?

### 5. Cleanup discipline

- Are temporary artifacts, leaked state, obsolete branches, and
  change-created dead code removed?
- Is cleanup deterministic where the change creates external or temporary state?
- If an Interface promises cleanup, rollback, or atomicity around caller-controlled work, is it proved for success, ordinary failure, and supported interruption/cancellation paths?
- Are no placeholders, broad catch/pass paths, or blanket suppressions left?

### 6. Consequence coverage

- Were callers, callees, adjacent consumers, no-change surfaces, and persisted
  contracts traced where the change affects them?
- For stateful edits, does proof cross the combined behavior surface?
- Are required coupled updates included in the same change?

### 7. Simple implementation

- Is the code readable and direct rather than ceremonial?
- Are comments limited to non-obvious contracts and decisions?
- Does proof use a high-signal workflow check plus sharp invariant checks?
- Does the code use the authority that owns a semantic rule instead of reconstructing it from names, syntax, or regexes?

## Language checks

For Python, retain useful public-boundary types, validate external input at the
boundary, and report broad exception swallowing or unjustified type ignores.

For TypeScript/JavaScript, narrow untrusted values, reject unsafe `any`, broad
casts and suppressions, and make exhaustive state handling fail closed.

## Review output

For each finding report the principle and severity, location, violated contract
or demonstrated consequence, smallest correction, and proof required. If there
are no findings, name any evidence surface that was unavailable. Syntax-only
checks do not prove consequence coverage.
