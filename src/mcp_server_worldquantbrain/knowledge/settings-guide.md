# Simulation settings

Call `get_simulation_options` for the combinations your account can use.

| Setting | Meaning | Practical notes |
|---|---|---|
| `region` | Market | `USA` has the longest history and most fields |
| `universe` | Tradable set | Smaller universes such as `TOP200` are harder and more liquid |
| `delay` | Days between data and trade | `1` is standard. `0` trades on same-day data and needs higher Sharpe |
| `decay` | Linear smoothing over N days | Raising it lowers turnover and adds lag |
| `neutralization` | Removes exposure to a group | `SUBINDUSTRY` is strictest. `MARKET` removes only market beta |
| `truncation` | Maximum weight per stock | `0.08` is the usual starting point |
| `pasteurization` | Removes values for instruments outside the universe | Leave `ON` |
| `nan_handling` | Replaces NaN weights with group means when `ON` | Can raise coverage |
| `unit_handling` | Checks that field units are compatible | `VERIFY` |
| `max_trade` | Limits trade size to liquidity | Asian regions require `ON`; the server sets this for you |
| `test_period` | Holds back recent data, as an ISO duration | `P2Y` holds out two years |

Tuning order that usually works: fix the idea and data, pick neutralization, then adjust decay for
turnover, then truncation for concentration.
