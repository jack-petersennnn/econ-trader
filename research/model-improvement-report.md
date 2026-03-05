# Model Improvement Research Report

**Date:** 2026-02-19  
**Models:** CPI (41.7% WR, -$217.50) and Fed Funds Rate (62.5% WR, -$265.00)  
**Objective:** Identify why these models lose money and propose specific improvements

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Breakeven Analysis](#breakeven-analysis)
3. [CPI Model Diagnosis](#cpi-model-diagnosis)
4. [CPI Model Improvements](#cpi-model-improvements)
5. [Fed Model Diagnosis](#fed-model-diagnosis)
6. [Fed Model Improvements](#fed-model-improvements)
7. [New FRED Series to Add](#new-fred-series)
8. [Proposed Architecture Changes](#proposed-architecture-changes)
9. [Implementation Priority](#implementation-priority)

---

## Executive Summary

Both the CPI and Fed models lose money despite the Fed model having a >50% win rate. The root causes are different:

- **CPI Model (41.7% WR):** Uses stale lagging indicators to predict a forward-looking number. The backtester's "market line" proxy is unrealistic, but the underlying model genuinely can't predict CPI direction because it's essentially using last month's CPI to predict this month's CPI — which is exactly what the market already prices in.

- **Fed Model (62.5% WR, -$265):** Wins often but loses money because the backtest P&L structure is asymmetric — wins pay only $3.50 while losses cost $50.00. This means the model correctly predicts the obvious (holds) but gets destroyed on the rare pivots (cuts/hikes), which is where the actual edge would be.

**Key insight:** To be profitable on Kalshi, you need different win rates depending on your contract price and the fee structure. The models need to stop trading obvious consensus outcomes and focus on identifying when the market is *wrong*.

---

## Breakeven Analysis

### Kalshi Fee Structure

Kalshi charges a fee of `fee_rate × price × (1 - price)`, which is maximized at 50¢ contracts. With our configured 7% fee rate:

| Contract Price | Fee per side | Total round-trip cost | Breakeven WR needed |
|---|---|---|---|
| 10¢ (long shot) | 0.63¢ | 1.26¢ | ~51.4% |
| 25¢ | 1.31¢ | 2.63¢ | ~52.6% |
| 50¢ (coin flip) | 1.75¢ | 3.50¢ | ~53.5% |
| 75¢ (favorite) | 1.31¢ | 2.63¢ | ~52.6% |
| 90¢ (heavy fav) | 0.63¢ | 1.26¢ | ~51.4% |

**However**, the real breakeven depends on payout structure:
- Buying YES at 50¢: win $0.50, lose $0.50 → need >53.5% WR
- Buying YES at 80¢: win $0.20, lose $0.80 → need >80% WR just to break even (before fees ~82%)
- Buying YES at 20¢: win $0.80, lose $0.20 → need >20% WR (before fees ~22%)

**This is why the Fed model loses money at 62.5% WR:** It's buying high-probability "hold" contracts at ~90¢+ (pays $3.50 on wins = ~93¢ contracts) but losing the full $50 when wrong. At 93¢ contracts, you need ~93% accuracy to break even, not 62.5%.

### Required Win Rates by Strategy

| Strategy | Typical Contract Price | Needed WR (inc. fees) |
|---|---|---|
| Bet on consensus (hold/obvious) | 85-95¢ | 87-96% |
| Bet on moderate favorites | 60-75¢ | 63-78% |
| Bet on coin flips | 45-55¢ | 53-56% |
| Bet against consensus | 5-20¢ | 7-22% |
| **Sweet spot: slight edge** | **35-65¢** | **~55-58%** |

---

## CPI Model Diagnosis

### What's Wrong

1. **Circular prediction problem:** The model uses CPI components (shelter, food, energy, medical, core) to predict CPI. But these ARE CPI — they're released simultaneously. The FRED data available before a CPI release is the *previous* month's CPI components, which is exactly what consensus already uses.

2. **No truly leading indicators:** The model's "leading" indicators (PPI, breakevens, import prices) have weak short-term CPI prediction power:
   - **PPI→CPI pass-through** takes 1-3 months and is only ~0.3-0.4 correlation
   - **Breakeven inflation** reflects long-run expectations (5Y/10Y), not next month's print
   - **Import prices** affect only ~15% of CPI basket with variable lag

3. **Missing the #1 predictor — shelter/OER lag structure:** CPI shelter (36% of CPI) lags market rents by **8-14 months** (NBER, William Blair research). The Zillow Observed Rent Index (ZORI) from 10-12 months ago is the single best predictor of today's shelter CPI. The model uses shelter's own momentum instead.

4. **Missing high-frequency real-time data:** Professional CPI traders use:
   - Gasoline prices (daily, via EIA — not just weekly GASREGW)
   - Used car wholesale prices (Manheim Index, released mid-month, leads CPI used cars by 1-2 months)
   - Airline fares (real-time from Google Flights / BTS)
   - Cleveland Fed Nowcast (daily updates, but our scraper often fails)

5. **Backtester uses unrealistic "market line":** `market_line = (prev_yoy + actual_yoy) / 2` — this uses the ACTUAL value to set the line, which is look-ahead bias. Real Kalshi markets have specific brackets.

6. **std_dev = 0.25% for bracket matching is too tight:** Historical CPI surprise distribution has a std dev of ~0.15-0.20% for MoM and ~0.3-0.4% for YoY. The model's 0.25% for YoY is reasonable but the confidence calibration is off.

### Why It Loses: The Fundamental Issue

CPI is one of the most efficiently priced macro indicators. The Bloomberg consensus forecast has an MAE of only ~0.1% for MoM CPI. Our model, built from the same FRED data everyone has, cannot consistently beat consensus. When it disagrees with consensus, it's usually wrong.

---

## CPI Model Improvements

### Priority 1: Shelter Nowcasting via Rent Lag (HIGH IMPACT)

The single biggest improvement. Shelter is 36% of CPI and highly predictable due to the BLS methodology lag.

**Approach:** Use Zillow ZORI (or FRED rent series) from 10-12 months prior to predict current shelter CPI.

**FRED Series:**
- `CUSR0000SEHA` — CPI Rent of Primary Residence
- `CUSR0000SEHC` — CPI Owners' Equivalent Rent (OER)
- No direct FRED series for ZORI — need Zillow API or scrape

**Implementation:**
```
shelter_predicted = f(ZORI_10mo_ago, ZORI_11mo_ago, ZORI_12mo_ago, shelter_momentum)
```

Research shows this approach has R² > 0.85 for predicting shelter CPI direction. Since shelter is 36% of CPI, getting shelter right alone could flip the model.

### Priority 2: Cleveland Fed Nowcast Integration (HIGH IMPACT)

The Cleveland Fed Nowcast updates daily and beats the Bloomberg consensus and SPF forecasts by 0.25-0.39 percentage points on average. It should be the **primary** signal, not a 20% weight cross-check.

**Current problem:** Our scraper (`_fetch_cleveland_nowcast`) is fragile and often returns None. We need:
1. More robust API parsing (they update their API format)
2. Fallback to their data download page
3. Cache the last known value

**Proposed weight:** 35-40% of ensemble (up from 20%)

### Priority 3: Real-Time Energy Price Tracking (MEDIUM IMPACT)

Energy is only 7% of CPI but causes most of the surprises. Our model uses weekly GASREGW but:

**Better approach:**
- Use daily gasoline prices: `GASDESW` (diesel), `GASREGW` (regular) — but get daily from EIA API
- Track the *within-month* price path, not just 4-week-ago comparison
- The BLS uses prices from the entire reference month, so the monthly average matters

**FRED Series:**
- `DCOILWTICO` — Daily WTI Crude Oil (proxy for energy CPI)
- `GASREGCOVW` — Weekly regular gas, conventional
- `DEXUSEU` — EUR/USD (affects import prices)

### Priority 4: Manheim Used Vehicle Index (MEDIUM IMPACT)

Used cars (4% of CPI) are volatile and cause frequent surprises. The Manheim Used Vehicle Value Index leads CPI used cars by 1-2 months with ~0.7 correlation.

**Not on FRED** — needs web scraping from Manheim or Cox Automotive. Consider adding as a data source.

### Priority 5: Use Consensus as Anchor (CRITICAL STRATEGIC CHANGE)

**Stop trying to predict CPI from scratch.** Instead:

1. Get the Bloomberg/Reuters consensus estimate (scrape from TradingEconomics, Investing.com, or similar)
2. Model the *surprise* — will CPI come in above or below consensus?
3. Only trade when our model predicts a surprise with high confidence

This is how professional traders approach it. The edge isn't in predicting CPI better than 50 PhD economists — it's in identifying the rare months where consensus is systematically wrong.

**Signals that predict upside CPI surprise:**
- Shelter CPI accelerating (from our lag model) while consensus hasn't adjusted
- Energy prices spiked after consensus was locked in (gas prices rose in last 2 weeks of reference month)
- Seasonal adjustment quirks (January seasonal factors are notoriously volatile)

**Signals that predict downside CPI surprise:**
- Used car prices falling faster than consensus models (Manheim → CPI lag)
- Airfare data showing weakness
- Dollar strength → import price deflation with lag

### Priority 6: Seasonal Pattern Analysis (MEDIUM IMPACT)

CPI has known seasonal patterns in specific components:
- **January:** Large seasonal adjustment factor changes, historically volatile
- **Shelter:** Tends to accelerate in Q1 due to lease renewal patterns
- **Apparel:** Seasonal clearance → deflation in Jan, inflation in Feb-Mar
- **Medical:** Annual insurance premium resets in January

**FRED Series for seasonal analysis:**
- `CUSR0000SAA` — Apparel CPI
- `CPIMEDSL` — Medical Care CPI
- `CUSR0000SETB01` — New Vehicles CPI
- `CUSR0000SETA01` — Used Cars & Trucks CPI

---

## Fed Model Diagnosis

### What's Wrong

1. **Asymmetric P&L is the killer:** The model buys "hold" at ~93¢ and wins $3.50 (10 wins × $3.50 = $35) but loses $50 on each miss (6 losses × $50 = $300). Net: -$265. Even at 62.5% WR, you lose money buying heavy favorites.

2. **The CME→Kalshi "arb" doesn't exist in practice:**
   - CME FedWatch derives from Fed Funds futures (institutional, liquid, tight spreads)
   - Kalshi is a retail prediction market (wider spreads, fees, different contract structure)
   - Divergences reflect different market structures, not mispricings (per the Polymarket/FedWatch analysis from Dec 2025)
   - The apparent arb disappears after accounting for Kalshi fees

3. **Momentum model is too simple:** The backtester uses `trend = rate 3 months ago vs now` which:
   - Gets every hold correct (trivial — most meetings are holds)
   - Misses every pivot (when the Fed actually changes course)
   - Pivots are exactly when there's money to be made

4. **Macro indicators are used as qualitative "consensus/hold/cut/hike" votes** rather than quantitative probability adjustments. The ±3% adjustment from macro context is too small to matter.

5. **Dot plot is hardcoded and stale.** Updated only at SEP meetings (4x/year). Between updates it provides no edge.

### Why It Loses: The Fundamental Issue

Fed decisions within 2 weeks of the meeting are >95% predictable by Fed Funds futures. The market already knows. The only edge is in:
1. Predicting decisions far in advance (3-6 months out) when futures are less certain
2. Correctly calling the rare surprises (emergency cuts, unexpected pivots)
3. Trading the *number of cuts in a year* rather than individual meetings

---

## Fed Model Improvements

### Priority 1: Stop Trading Near-Term Obvious Outcomes (CRITICAL)

If CME FedWatch shows >85% probability for any outcome, **don't trade that meeting**. The Kalshi price will already reflect this. There's no edge in buying 90¢+ contracts.

**Instead, focus on:**
- Meetings 2-4 months out where uncertainty is higher (contract prices 30-70¢)
- "Total cuts this year" contracts where our macro model could have genuine edge
- The specific rate level at year-end (dot plot implied)

### Priority 2: Fed Speaker Sentiment Tracking (HIGH IMPACT)

Between meetings, Fed speakers telegraph their intentions. Professional traders track:
- Hawkish vs dovish language in speeches
- Voting vs non-voting member distinction
- Shift in rhetoric from previous statements

**Data sources:**
- Fed calendar: track upcoming speakers
- NLP sentiment on Fed speeches (scrape from federalreserve.gov)
- Media: Reuters/Bloomberg Fed speaker trackers

This could provide edge 2-4 weeks before a meeting when futures haven't fully adjusted.

### Priority 3: Financial Conditions as Rate Predictor (MEDIUM IMPACT)

**Better FRED series to use:**

- `NFCI` — National Financial Conditions Index (already used, keep)
- `ANFCI` — Adjusted NFCI (removes business cycle effects — better for rate prediction)
- `STLFSI4` — St. Louis Fed Financial Stress Index (alternative view)
- `GSFCI` — Goldman Sachs FCI (not on FRED, but widely tracked)
- `BAMLH0A0HYM2` — ICE BofA High Yield OAS (credit spreads → distress signal)
- `BAMLC0A0CM` — ICE BofA Corporate Bond OAS

When financial conditions tighten rapidly, the Fed is more likely to cut (or pause hikes). A rapid tightening of credit spreads preceded the Sept 2024 cut.

### Priority 4: Labor Market Slack Composite (MEDIUM IMPACT)

The Fed's dual mandate means labor data drives rate decisions. Build a composite:

**FRED Series:**
- `UNRATE` — Unemployment Rate (already used)
- `U6RATE` — U-6 Underemployment Rate (broader measure)
- `JTSJOL` — Job Openings (JOLTS) — leading indicator of labor slack
- `JTSHIL` — Hires Level
- `JTSQUL` — Quits Level (workers' confidence in finding new jobs)
- `CES0500000003` — Average Hourly Earnings (wage inflation → rate pressure)
- `AWHAETP` — Average Weekly Hours (leading — hours cut before layoffs)
- `LNS13025703` — Part-time for Economic Reasons

**Composite approach:** When job openings fall, quits drop, and weekly hours decline — this precedes rate cuts by 3-6 months, even if unemployment hasn't risen yet.

### Priority 5: Yield Curve as Rate Path Predictor (MEDIUM IMPACT)

The yield curve already encodes the market's rate expectations. Use it more precisely:

**FRED Series:**
- `T10Y2Y` — 10Y-2Y spread (already used)
- `T10Y3M` — 10Y-3M spread (better recession predictor per NY Fed)
- `DGS2` — 2Y Treasury (daily — embeds next 2 years of rate expectations)
- `DGS1` — 1Y Treasury
- `DGS3MO` — 3M Treasury (proxy for current Fed funds)

**Key signal:** `DGS2 - DGS3MO` change over the past month. If the 2Y yield drops 20bp+ while 3M is stable, the market is pricing in cuts. If this diverges from Kalshi pricing, there may be edge.

### Priority 6: Inflation Expectations as Rate Anchor (LOW-MEDIUM IMPACT)

**FRED Series:**
- `MICH` — University of Michigan 1Y Inflation Expectations
- `EXPINF1YR` — 1Y Expected Inflation (Cleveland Fed)
- `EXPINF10YR` — 10Y Expected Inflation
- `T5YIFR` — 5Y Forward Inflation Expectation Rate (5Y5Y forward — the Fed's preferred measure)

When `T5YIFR` rises above 2.5%, the Fed becomes hawkish. When it drops below 2.0%, dovish. This is a medium-term signal (months, not days).

---

## New FRED Series

### For CPI Model

| Series ID | Description | Use | Priority |
|---|---|---|---|
| `CUSR0000SEHA` | CPI Rent of Primary Residence | Shelter nowcast | HIGH |
| `CUSR0000SEHC` | CPI Owners' Equivalent Rent | Shelter nowcast | HIGH |
| `DCOILWTICO` | Daily WTI Crude | Real-time energy | HIGH |
| `CUSR0000SAA` | CPI Apparel | Seasonal patterns | MED |
| `CUSR0000SETB01` | CPI New Vehicles | Vehicle prices | MED |
| `CPIMEDSL` | CPI Medical Care | Medical component | MED |
| `CUSR0000SAF11` | CPI Food at Home | Detailed food | LOW |
| `CUSR0000SEFV` | CPI Food Away from Home | Services inflation | LOW |
| `PCU4841114841111` | BLS Airline Fares PPI | Travel proxy | LOW |
| `DTWEXBGS` | Trade-Weighted Dollar Index | Import price pressure | MED |

### For Fed Model

| Series ID | Description | Use | Priority |
|---|---|---|---|
| `ANFCI` | Adjusted NFCI | Financial conditions (better) | HIGH |
| `BAMLH0A0HYM2` | High Yield OAS | Credit stress | HIGH |
| `U6RATE` | U-6 Underemployment | Broader labor slack | HIGH |
| `JTSJOL` | JOLTS Job Openings | Labor demand leading | HIGH |
| `JTSQUL` | JOLTS Quits Level | Worker confidence | MED |
| `CES0500000003` | Avg Hourly Earnings | Wage inflation | HIGH |
| `AWHAETP` | Avg Weekly Hours | Early labor weakness | MED |
| `T10Y3M` | 10Y-3M Spread | Recession probability | HIGH |
| `T5YIFR` | 5Y5Y Forward Inflation | Fed's preferred measure | MED |
| `STLFSI4` | StL Fed Financial Stress | Alt. stress measure | LOW |
| `BAMLC0A0CM` | Corporate Bond OAS | Corporate credit | LOW |
| `MICH` | Michigan 1Y Inflation Exp | Consumer expectations | MED |

---

## Proposed Architecture Changes

### CPI Model v2: Surprise-Based Architecture

```
OLD: Our estimate → bracket probability → trade vs Kalshi price
NEW: Consensus estimate → our surprise prediction → trade only when expecting surprise
```

**Pipeline:**
1. Scrape consensus CPI estimate (TradingEconomics, Investing.com)
2. Run shelter lag model → does our shelter forecast differ from consensus?
3. Check real-time energy data → did gas prices move after consensus was set?
4. Check Cleveland Fed Nowcast → does it diverge from consensus?
5. Score surprise probability: P(CPI > consensus) or P(CPI < consensus)
6. Only trade when surprise probability > 60% with identifiable catalyst
7. Target contracts near the consensus boundary (40-60¢ range)

**Expected improvement:** Even a modest 58% accuracy on surprise calls at 50¢ contracts would be profitable.

### Fed Model v2: Longer-Horizon + Conditions-Based

```
OLD: CME prob for next meeting → compare to Kalshi → trade divergence
NEW: Macro conditions composite → rate path forecast → trade longer-dated contracts
```

**Pipeline:**
1. Build labor market slack composite (JOLTS + earnings + hours + U6)
2. Track financial conditions trajectory (ANFCI + credit spreads)
3. Monitor inflation expectations (T5YIFR + Michigan)
4. Score: "Where will rates be in 3-6 months?" 
5. Compare to Kalshi contracts for meetings 2-4 months out
6. Only trade when conditions diverge from market pricing AND contract is in 30-70¢ range
7. Avoid buying contracts above 80¢ or below 20¢

### Backtester v2 Improvements

The current backtester has serious flaws that must be fixed to get reliable signals:

1. **CPI backtester uses look-ahead bias** in market line calculation → fix by using actual Kalshi bracket levels or reasonable proxies
2. **Fed backtester has asymmetric P&L** that doesn't model contract prices → fix by simulating realistic contract prices based on futures-implied probabilities
3. **Neither backtester models bid-ask spread** → add 2-3¢ spread assumption
4. **Add position sizing** based on Kelly criterion and contract price
5. **Add time-series cross-validation** instead of full-sample backtest

---

## Implementation Priority

| # | Change | Expected Impact | Effort |
|---|---|---|---|
| 1 | **Fix backtester P&L modeling** | Foundation — everything depends on this | Medium |
| 2 | **Fed: Stop trading >80¢ contracts** | Immediately stops bleeding on Fed | Trivial |
| 3 | **CPI: Add shelter lag model** (ZORI/rent data) | Biggest single CPI improvement | Medium |
| 4 | **CPI: Weight Cleveland Fed Nowcast higher** + fix scraper | Easy win if scraper works | Low |
| 5 | **Both: Add consensus scraping** | Required for surprise-based architecture | Medium |
| 6 | **Fed: Add JOLTS + credit spreads + ANFCI** | Better macro composite | Low |
| 7 | **Fed: Focus on 2-4 month out contracts** | Where edge actually exists | Medium |
| 8 | **CPI: Add real-time energy tracking** | Catches late-month price moves | Low |
| 9 | **Fed: Add speaker sentiment** | High value but hard to implement | High |
| 10 | **Both: Implement proper Kelly sizing by contract price** | Risk management improvement | Medium |

### Minimum Viable Improvement

If we do only items 1-4, we should expect:
- **CPI:** Move from 41.7% to ~52-58% WR by getting shelter right and anchoring to nowcast
- **Fed:** Move from -$265 to breakeven or slight positive by simply not trading obvious outcomes

### Target Performance

| Model | Current WR | Current P&L | Target WR | Target P&L |
|---|---|---|---|---|
| CPI | 41.7% | -$217.50 | 55-60% | +$50-150 |
| Fed | 62.5% | -$265.00 | 55-60% (at 50¢ contracts) | +$25-75 |
| NFP | 84.0% | +$813.25 | 80%+ (don't break it) | +$700+ |

---

## Appendix: Key Research Sources

1. **Cleveland Fed Inflation Nowcasting** — Outperforms consensus by 0.25-0.39pp (Cleveland Fed WP 2024)
2. **Shelter/OER Lag** — 8-14 month lag from market rents to CPI shelter (NBER, William Blair, SF Fed)
3. **Zillow CPI Shelter Forecast** — Monthly forecast with methodology details (zillow.com/research)
4. **FedWatch vs Prediction Markets** — Methodological differences explain apparent arb (Polymarket Now, Dec 2025)
5. **Kalshi Market Microstructure** — Low-price contracts win less than breakeven, high-price contracts win more (UCD WP 2025)
6. **Manheim Used Vehicle Index** — Leads CPI used cars by 1-2 months, ~0.7 correlation
