# FASTEXPR cheatsheet

An expression returns one number per instrument per day. BRAIN turns those numbers into portfolio
weights, so only their cross-sectional ordering and relative size matter. Call `list_operators` for
the exact set your account can use, and `list_datafields` for the fields.

## Common price and volume fields

`open`, `high`, `low`, `close`, `volume`, `vwap`, `returns`, `cap`, `adv20`

Grouping fields: `market`, `sector`, `industry`, `subindustry`

## Common operators

| Kind | Operators |
|---|---|
| Cross-sectional | `rank(x)`, `zscore(x)`, `scale(x)`, `winsorize(x, std=4)` |
| Time series | `ts_rank(x, d)`, `ts_zscore(x, d)`, `ts_mean(x, d)`, `ts_std_dev(x, d)`, `ts_delta(x, d)`, `ts_delay(x, d)`, `ts_sum(x, d)`, `ts_corr(x, y, d)`, `ts_regression(y, x, d)`, `ts_backfill(x, d)`, `ts_decay_linear(x, d)` |
| Group | `group_rank(x, g)`, `group_neutralize(x, g)`, `group_zscore(x, g)`, `group_mean(x, w, g)` |
| Arithmetic | `abs`, `log`, `sign`, `power(x, y)`, `max`, `min`, `+ - * /` |
| Logic | `if_else(c, a, b)`, `trade_when(trigger, alpha, exit)`, `is_nan(x)`, `< > == && \|\|` |

## Examples

Short-term reversal: `rank(-ts_delta(close, 5))`

Value within industry: `group_rank(ts_backfill(fundamental_field, 60) / cap, industry)`

Volume-confirmed move: `rank(ts_corr(rank(close), rank(volume), 10)) * -1`

## Habits that save quota

- Sparse fundamental and analyst fields usually need `ts_backfill` before `rank`.
- `VECTOR` fields need a vector operator such as `vec_avg` before they can be used as a signal.
- Smooth a high-turnover signal with `ts_mean` or a larger `decay`.
- Division by a near-zero field produces outliers. Rank or winsorize the result.
