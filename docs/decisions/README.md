# Architecture decisions

Six decisions that the rest of the code is downstream of. Each records what was
being traded off, what was chosen, what it cost, and what would change the
answer.

They are here because most of these decisions look arbitrary from the outside
and a few look actively wrong until you know what the alternative did. The
policy engine refusing to act on a model's output looks like distrust of the
model; it is actually what makes the model safe to use at all. Reporting
recovery as incremental rather than gross makes every headline number smaller;
it is the only version of the number that means anything.

| | decision | in one line |
|---|---|---|
| [001](ADR-001-model-has-no-authority.md) | The model proposes, the policy engine disposes | A classifier cannot reach a side effect |
| [002](ADR-002-incremental-not-gross.md) | Recovery is incremental, never gross | Subtract what would have arrived anyway |
| [003](ADR-003-policy-as-cited-clauses.md) | Every decision cites the clauses that produced it | An unexplainable action is a bug |
| [004](ADR-004-synthetic-world-committed-fixtures.md) | The world is seeded and the model answers are committed | Anyone can reproduce the figures with no key |
| [005](ADR-005-human-attention-is-finite.md) | Human review is a budget, not an escape hatch | Otherwise "escalate everything" wins |
| [006](ADR-006-compliance-is-not-a-tunable.md) | Regulatory clauses are not parameters | They cost ₹95,568 and they stay |
