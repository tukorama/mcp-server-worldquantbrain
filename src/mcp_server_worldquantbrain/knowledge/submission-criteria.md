# Submission criteria

BRAIN decides with its own checks. `check_alpha_submission` returns them with the limits that apply
to your alpha, and those values always win over this page.

Typical limits for a delay-1 regular alpha:

| Check | Requirement |
|---|---|
| `LOW_SHARPE` | Sharpe at least 1.25 |
| `LOW_FITNESS` | Fitness at least 1.0 |
| `LOW_TURNOVER` | Turnover at least 1% |
| `HIGH_TURNOVER` | Turnover at most 70% |
| `CONCENTRATED_WEIGHT` | No excessive weight in single stocks |
| `LOW_SUB_UNIVERSE_SHARPE` | Holds up in a more liquid sub-universe |
| `SELF_CORRELATION` | Below 0.7 against your submitted alphas, unless clearly better |

Delay-0 alphas and other regions can have different limits.

Fitness is `sharpe * sqrt(abs(returns) / max(turnover, 0.125))`, so cutting turnover can raise it
even when Sharpe stays flat.

If an alpha fails on self-correlation, change the data or the idea rather than the parameters. A
parameter tweak almost always stays correlated with the original.
