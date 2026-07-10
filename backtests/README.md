# Backtests

Walkthrough backtests over local Sierra `.scid` tick data. These are research
scripts, not a framework: each one reconstructs per-session context with no
lookahead (previous value area, ON range, wVWAP as known at RTH open) and
walks 1-minute bars to outcomes.

Run with the sierra-mcp venv (scripts import `sierra_mcp.scid_reader`):

```powershell
D:\inversion\sierra-mcp\.venv\Scripts\python.exe -X utf8 backtests\backtest_mes_bpb.py
```

## Scripts

- `backtest_mes_bpb.py` — mechanized proxy of the draft BPB setup (K#3):
  break of the previous value-area edge after RTH open, entry on the pullback
  to the level, fixed 6-pt stop, target at the next known level. Results
  2026-07-10 over 18 sessions: no edge for the naive skeleton (journal
  observation #12).
- `backtest_mes_bpb_acceptance.py` — grid over acceptance definitions
  (A bars closing beyond the level + E pts extension before the retouch).
  Finding: A=1 filters instant-failure breaks and keeps fast winners; A>=2
  blocks the best trades because good MES pullbacks arrive within 1-2 minutes
  (journal observation #13). Grid-mined on 7 setups — hypothesis, NOT a
  validated rule. Re-run out-of-sample once ~20-30 new sessions accumulate.

## Conventions

- Value profiles are full-Globex-session (22:00 UTC anchor), matching
  `indicator_engine`. If calibration against Sierra visual studies
  (`compare_indicator_levels`) shows donAdri uses RTH-only profiles, add an
  RTH-profile variant before drawing further conclusions.
- Intra-bar ambiguity resolves stop-first (conservative).
- No slippage/commissions. Point results are directional evidence for
  playbook validation, not PnL forecasts.
- Log every experiment's method + result to the journal (`log_observation`,
  source `backtest:scid`) so conclusions stay auditable.
