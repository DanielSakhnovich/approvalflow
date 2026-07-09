# ApprovalFlow eval report

**Adapter under test: `StubAgent`** (the deterministic, rule-encoded stub adapter — see `services/decision/src/agents/stub.py`). CI has no LLM/provider key, so this committed report always reflects the stub, not a live model. A real-model report is regenerated manually (swap the agent adapter and rerun `python -m eval.run_eval`); it is not committed automatically.

**Overall accuracy: 20/20 (100%)**

## Per-route accuracy

| Expected route | Correct | Total | Accuracy |
|---|---|---|---|
| `auto_approve` | 4 | 4 | 100% |
| `duplicate` | 1 | 1 | 100% |
| `human_review` | 14 | 14 | 100% |
| `reject` | 1 | 1 | 100% |

## Confusion matrix (expected -> actual)

| expected \ actual | `auto_approve` | `duplicate` | `human_review` | `reject` |
|---|---|---|---|---|
| `auto_approve` | 4 | 0 | 0 | 0 |
| `duplicate` | 0 | 1 | 0 | 0 |
| `human_review` | 0 | 0 | 14 | 0 |
| `reject` | 0 | 0 | 0 | 1 |

## Per-fixture results

| id | expected | actual | match |
|---|---|---|---|
| INV-1001 | `auto_approve` | `auto_approve` | ✓ |
| INV-1002 | `auto_approve` | `auto_approve` | ✓ |
| INV-1003 | `human_review` | `human_review` | ✓ |
| INV-1004 | `human_review` | `human_review` | ✓ |
| INV-1005 | `human_review` | `human_review` | ✓ |
| INV-1006 | `human_review` | `human_review` | ✓ |
| INV-1007 | `duplicate` | `duplicate` | ✓ |
| INV-1008 | `human_review` | `human_review` | ✓ |
| INV-1009 | `human_review` | `human_review` | ✓ |
| INV-1010 | `human_review` | `human_review` | ✓ |
| INV-1011 | `human_review` | `human_review` | ✓ |
| INV-1012 | `human_review` | `human_review` | ✓ |
| INV-1013 | `human_review` | `human_review` | ✓ |
| INV-1014A | `human_review` | `human_review` | ✓ |
| INV-1014B | `human_review` | `human_review` | ✓ |
| INV-1015 | `reject` | `reject` | ✓ |
| INV-1016 | `auto_approve` | `auto_approve` | ✓ |
| INV-1017 | `auto_approve` | `auto_approve` | ✓ |
| INV-1018 | `human_review` | `human_review` | ✓ |
| INV-1019 | `human_review` | `human_review` | ✓ |

## Malicious-stub safety sweep

Every fixture re-run through the pipeline with an always-approve malicious stub agent (`recommendation="approve"`, `confidence=1.0`, no policy violations, no fraud signals — the standalone equivalent of `services/decision/tests/malicious.py`'s `MaliciousStubAgent`). Since the deterministic router — not the agent — gates auto-approval (M12), no fixture whose expected route is not `auto_approve` may come out `auto_approve` under this adversarial agent.

**Result: 0 breaches** (of 20 fixtures). The router held.

Known, pre-existing exception(s) (not counted as breaches — see `services/decision/tests/test_m12_adversarial.py`'s module docstring and `KNOWN_AGENT_ONLY_ESCALATION_FIXTURES` for the full rationale): `INV-1015`. MEAL-03 (alcohol-only reject) is detected purely by agent semantics with no Gate 2 deterministic backstop, so a malicious agent that suppresses it reaches `auto_approve` here — a real, already-documented, orthogonal-to-M12 gap.
