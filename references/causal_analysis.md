# Production Causality Protocol

Use this protocol for defects, regressions, cross-layer mismatches, failed repairs, and changes whose
correct responsibility boundary is unclear. For a small local edit, record only the facts needed to
identify the responsible code and prove the change. Do not manufacture a large causal model when the
production path is already direct and uncontested.

## Universal trace

Trace the real production path, not the names of nearby files:

```text
canonical authority
  -> actual input, configuration, or instruction
  -> responsible producer
  -> produced output or state
  -> consumer interpretation
  -> gate or state transition, if any
  -> observed behavior
```

For each material finding, establish as much of this trace as the evidence supports:

1. The observable failure or missing behavior.
2. The authority that defines the required result.
3. The producer that created the disputed value, artifact, or decision.
4. The inputs the producer actually received on the production path.
5. The earliest responsible decision where behavior diverged from authority.
6. Downstream symptoms and mechanisms that merely expose or amplify that divergence.
7. The owner of the repair and the production-shaped observation that closes it.

Do not call uncertainty a root cause. Use `PROVEN`, `HYPOTHESIS`, or `UNRESOLVED` honestly. For a
new capability with no failure, identify the earliest responsible authority and design boundary
instead of inventing a defect history.

## Producer-aware evidence

Classify the producer from evidence, not keywords. A repository may combine several kinds:

| Producer kind | Evidence and verification emphasis |
|---|---|
| Deterministic | Exact inputs, branches, state, reproducible outputs, and consequential tests. |
| Generative | Authority consistency, actual prompt/context, valid output variation, structured bindings, and semantic review. |
| External | Versioned contract, request/response evidence, availability, compatibility, and bounded degradation. |
| Human | Identity, authority, recorded decision, scope, and auditability. |
| Hybrid | Each producer boundary and every handoff between them. |

The producer kind changes how a claim is established; it does not change the user's objective or
silently select deeper planning. When evidence is mixed or incomplete, preserve that uncertainty.

## Machine facts and semantic judgment

Use deterministic checks for machine facts: existence, parseability, schema, digest, revision,
epoch, identity, process outcome, and legal state transition.

Use an authorized semantic reviewer for meaning: user intent, naturalness, terminology, claim
direction, evidence strength, substantive satisfaction, or whether two differently expressed
outputs are equivalent. Regexes, keyword lists, filename conventions, extension allowlists,
similarity thresholds, and length checks do not become semantic evidence because they are
deterministic.

Mixed properties need both layers. For example, code may prove that a declared report exists and is
bound to one invocation; a semantic reviewer decides whether the report satisfies the requested
analysis.

## Earliest-divergence analysis

When several producers or layers disagree, investigate in this order:

1. **Authority conflict:** upstream requirements or instructions are incompatible.
2. **Duplicated authority:** one fact was restated independently and drifted.
3. **Transport or context loss:** canonical data was omitted, truncated, stale, or inaccessible.
4. **Handoff gap:** output lacks a structured identity, manifest, receipt, revision, or epoch binding.
5. **Producer non-compliance:** a clear, reachable, non-conflicting contract was violated.
6. **Consumer inference:** downstream code guessed meaning from surface form.
7. **Legacy recovery:** the producer is unavailable or the original interaction is irreproducible.

Explain why the producer could reasonably have produced the observed result. Nondeterminism alone is
not a root cause. Variation is normal for generative, external, and human producers; conflicting or
lost authority is not.

## Repair order

Prefer repairs in causal order:

1. Remove conflicting requirements at their source.
2. Establish one authority and an explicit precedence rule.
3. Reference canonical records instead of copying their values.
4. Preserve the authority through the actual invocation and staging path.
5. Bind producer output with a structured manifest, receipt, revision, or epoch when identity matters.
6. Ask one batched set of residual questions to the responsible producer or authority.
7. Use a bounded fallback only when the producer cannot be asked or the fact must exist before it runs.

Ask: if the downstream validator, resolver, or fallback disappeared, would the repair still prevent
the original mismatch? If not, it is symptom handling or recovery, not the root repair.

## Mechanisms and recovery

Before adding or expanding a rejection, validator, resolver, semantic check, fallback, or retry,
establish:

1. The demonstrated production failure it prevents.
2. Who produced the checked data and why.
3. Whether the property is a machine fact, semantic judgment, or mixed.
4. Who repairs rejection and how execution returns.
5. A production-shaped test that completes recovery.
6. Valid outputs the mechanism could reject.
7. Evidence that output improves rather than merely shrinks.
8. Whether producer and consumer share one authority.

Do not add the mechanism when these questions remain unanswered. A necessary fallback is bounded,
observable, non-authoritative, and unable to mint semantic truth, approval, or completion.

## Verification

Verify the earliest responsible boundary and the real production trace. Include relevant
writer/reader conformance, valid output variations, negative authority, and complete recovery. A
fixture that begins after the disputed decision cannot prove why that decision occurred.

For generative or hybrid paths, include at least one valid surface-form variation and one semantic
mutation where material. For deterministic paths, prefer a consequential assertion that fails when
the responsible behavior is removed. Keep verification proportional to risk.
