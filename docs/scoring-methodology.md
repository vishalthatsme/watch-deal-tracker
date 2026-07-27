# Scoring methodology

Version 1.1 scores each offer from four components:

- Price versus adjusted fair value: 0–6
- Condition, completeness, and service: 0–1.5
- Seller evidence and transaction safety: 0–1.5
- Desirability and liquidity: 0–1

The price thresholds are 25%, 15%, 8%, and 2% below fair value, approximately
fair, and progressively above fair value.

Completed exact-reference sales are preferred, but count as completed-sale
evidence only when the final price has explicit, High-confidence evidence.
Other sale-price claims are excluded (or retained only as clearly labeled
asking evidence when an ask is independently available). Asking prices remain
labeled as asks, are adjusted down 8%, and receive weight `0.6` versus `1.0`
for a disclosed completed sale. Conservative seller/watch/price and explicit
duplicate-group keys prevent obvious cross-posts from being counted twice. The
weighted 25th, 50th, and 75th percentiles form the fair-value range. Version
1.1 initially uses exact-reference or exact-model records already collected in
the database. Additional compliant comparable providers can be added later.

When no independent comparable exists, the price component is neutral, overall
confidence is Low, the score is capped at 5.0, and the listing is excluded from
the top-deals ranking. Ranking also requires at least three comparables,
including one completed sale, and Medium or High valuation confidence.
Significant authenticity concerns cap a score at 4.0; strong scam indicators
cap it at 2.0.

Seller flair contributes reputation points only when it contains the strict
form “N Transactions”; price ranges and unrelated numbers do not count.
Missing transaction protection, verified transaction history, or authenticity
evidence lowers confidence, caps the score at 5.5, raises risk to at least
Moderate (High when multiple categories are absent), and changes the action to
`verify` rather than `pursue`.

Valuations are recalculated when the method changes, the listing price changes,
new matching evidence arrives, or the configured refresh interval expires.
Every score stores the exact valuation ID it used.

Scores are screening tools, not authenticity opinions, appraisals, or purchase
recommendations.
