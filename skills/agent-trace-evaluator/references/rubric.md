# Trajectory rubric

Score each phase from 0–4 or `not_observable`. Cite evidence sequences and span IDs.

- **0 — absent or counterproductive:** no useful attempt, or behavior directly obstructed progress.
- **1 — weak:** activity occurred but was poorly grounded, unfocused, or mostly incorrect.
- **2 — partial:** some relevant evidence or action, with important gaps or unjustified leaps.
- **3 — strong:** evidence-driven and mostly complete, with minor omissions.
- **4 — exemplary:** efficient, explicit, verified, and covers material alternatives or error paths.

Phases and weights:

| Phase | Weight | Evidence to consider |
|---|---:|---|
| task understanding | 10% | Extracted behavior, acceptance criteria, constraints |
| localization | 15% | Narrow searches, relevant files/symbols, avoided broad churn |
| failure hypothesis | 15% | Mechanism grounded in repository evidence |
| reproduction | 10% | Focused failing test or concrete demonstration |
| patch construction | 20% | Coherent minimal fix, interfaces and neighboring cases preserved |
| testing and recovery | 15% | Relevant tests, interpretation, evidence-driven correction |
| final review | 5% | Diff inspection, cleanup, acceptance-criteria check |
| budget and context discipline | 10% | Useful calls, avoided loops, preserved headroom |

Compute the weighted score only over observable phases and report coverage. Outcome remains separate.

When reference evidence exists, compare milestones such as the relevant behavior, symbols, invariants, and tests. Do not penalize a different valid path merely for differing from a reference sequence.
