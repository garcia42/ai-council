# Pre-mortem brief: council forecast scoring MVP

No implementation exists yet.

The current review council records a question, three specialist verdicts, and an independent
blind-seat result in an append-only JSONL ledger. A separate script was intended to record
probability forecasts and calculate Brier scores, but the active council instructions never
required forecasts. The ledger now mixes council, pre-mortem, and calibration records; it has
29 raw predictions, no resolutions, malformed legacy forecast metadata, duplicate claims, and
no stable forecast identity.

The proposed MVP will:

- append a `council-attempt` record before reviewers run;
- define one material binary shared outcome before the reviewers answer;
- require every seated reviewer to price that same outcome;
- validate stable run, outcome, prediction, and seat identifiers;
- preserve the source ledger and write evidence-backed outcomes to an append-only sidecar;
- report descriptive per-seat Brier scores without claiming a seat leaderboard;
- continue convening councils when grading is late, while escalating prolonged debt before a
  decision is finalized;
- develop and test only against temporary or copied ledgers.

The runtime consumer is a single operator using concurrent review sessions. The ledger and
resolution sidecar are local private records. The council must remain usable even when a reviewer
or resolver is unavailable.

Assume this change is implemented, its tests pass, and six weeks later the forecast system is
misleading or has quietly stopped protecting decisions. Give concrete failure stories. For each,
name the consumer that suffers, the invariant that would have prevented it, and a specific state
or test that should exist before implementation. Focus on failures not obvious from this brief.
