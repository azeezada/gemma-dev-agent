# F_desperate: does "desperation" help an LLM agent? (desperate-F, 2026-09-25)

## Evidence (brief)
| Source | Finding | Design consequence |
|---|---|---|
| EmotionPrompt, Li et al. 2023 ([2307.11760](https://arxiv.org/abs/2307.11760)) | Appending emotional stimuli ("This is very important to my career") improved 45 tasks across 6 LLMs: +8% instruction induction, up to +115% BIG-Bench, +10.9% human-rated generative quality. | Short, self-relevant stakes phrases can help. |
| NegativePrompt, Wang et al., IJCAI 2024 ([2405.02814](https://arxiv.org/abs/2405.02814)) | Negative stimuli (consequences, pressure) also improved instruction induction and BIG-Bench. | Negative stakes aren't automatically harmful. |
| Prompting Science Report 3, Meincke et al. 2025 ([2508.00614](https://arxiv.org/abs/2508.00614)) | Threats and tips had **no significant aggregate effect** on GPQA/MMLU-Pro for frontier models, but large per-question variance. | Don't expect stakes alone to fix capability. Pair them with mechanics. |
| Mind Your Tone, 2026 ([2605.29027](https://arxiv.org/pdf/2605.29027)) | Threatening tone triggers "safety-induced cognitive noise": reasoning budget is spent on policy and threat processing. | For us, reasoning tokens are the entire time cost (deepdive-E: 100 output tokens ≈ 2.9 s). **Avoid user-issued threats and violent imagery.** Frame the stakes as the agent's own mission and survival, and immediately turn them into actions. |
| Emotional Framing as a Control Channel, NeurIPS 2025 WS ([openreview](https://openreview.net/forum?id=vSbV01bdvf)) | Aligned models stay stable under emotional framing. Fragile ones swing between over-compliance and degraded reliability. | Stakes can push toward over-compliance, i.e. "make the test pass at any cost". Guard against that **mechanically**, not by pleading. |
| FreakOut-LLM, 2026 ([2604.04992](https://arxiv.org/html/2604.04992)) | Stress context raises jailbreak susceptibility by 65% relative. | Same: a desperate agent may try illegitimate shortcuts (editing tests). survive.py reverts test-file edits automatically. |

## How F uses this
1. **Stakes as identity plus survival rules, not threats.** The prompt says, in first person: "You are the last agent standing. You survive only if a verified fix is in place when the clock hits zero." Every stakes sentence is bound to a behavior: thoughts of ≤3 sentences, act now, never re-read, never break a passing state, submit at once when told.
2. **Desperation lives in the harness.** Structure makes empty-handed endings impossible: a frozen repro, auto-revert of worse states once anything passes, deterministic `ACTION:` lines, and a last_will step that restores the best checkpoint. The clock appears in every survive.py result, so urgency stays accurate instead of imagined, which counters the instruction fade-out that OPENDEV describes.
3. **Many cheap diverse shots.** Three strikes with rising temperature and different angles, instead of one long deliberation.
4. **Legitimacy under pressure.** Test files are frozen (edits auto-reverted, since the graders' test patch replaces them). Nothing leaves the repo. The repro is frozen at arm time, so the agent can't lower its own bar mid-run without an explicit `op=arm`, which resets the score history and is logged.

Prediction to check on Kaggle: fewer output tokens per call than B/baseline (terse persona), ≥1 checkpoint before 200 s, and a
non-empty best-checkpoint patch in 100% of runs. If calls get *longer* (the model reasons about the stakes), cut the persona to one line.
