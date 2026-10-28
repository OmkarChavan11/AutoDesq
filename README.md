# AutoDesq prototype

Prices an investor's bond requirement, ranks the counterparties most likely to deliver, drafts the bids,
reads the replies and records everything for training. Phase 1: rule-based markup and a scorecard ranking,
as in the *Technology Flow, Models and Training Plan* doc.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env              # fill in RDS_URL (read-only user) and TEST_DB_URL
python -m autodesq.cli init-db    # creates autodesq_* tables on the TEST DB
```

## Desk screen (web)

```bash
uvicorn autodesq.api:app --port 8000      # then open http://localhost:8000
```
The Offer Finder screen: 1 investor requirement (ISIN search and bond details), 2 fair yield and markup (rule table,
slider to change it), 3 who can deliver (ranked counterparties with reasons; yellow tags = feedback from replies),
4 bid message, 5 responses (paste replies, Simulate replies for demos), 6 confirmed deals, 7 ranking model test
results, replies since the last retrain and a Retrain button. Recording a reply re-ranks the open requirement at once.

## Desk screen (Streamlit, simpler alternative)

```bash
streamlit run app.py          # opens http://localhost:8501
```
Three tabs: **Rank counterparties** (enter ISIN, investor yield and quantity; tick who to bid to; send bids and copy
the messages), **Replies** (type each dealer's reply; it is read automatically, or set the status yourself), and
**Learning** (model test results against simple baselines, replies since the last retrain, and a Retrain button).

## Use

```bash
python -m autodesq.cli quote INE0NES07352 12.20 5            # price + rank, nothing saved
python -m autodesq.cli quote INE0NES07352 12.20 5 --send     # save requirement, bid the top 5
python -m autodesq.cli reply 12 "best 12.45 for 3cr"         # read a dealer's reply
python -m autodesq.cli reply 12 "x" --correct accepted --yield 12.45 --qty 3   # trader accepts a counter
python -m autodesq.cli status 3                              # all replies for requirement 3
uvicorn autodesq.api:app --reload                            # same flow as an HTTP API for a dashboard
```

## AI models (LightGBM + LLM)

| Step | Without training | After `python -m autodesq.cli train` |
| --- | --- | --- |
| Markup | Rule table (default `MARKUP_ENGINE=rules`) | Optional, `MARKUP_ENGINE=auto`: 1) LightGBM fill model if approved (fill chance x revenue); else 2) LightGBM markup model: the markup INRBonds achieved on similar executed deals (indication_of_interest.revenues cost_price vs client price), shown as a 25-75% range; else 3) rule table. Offer rule, cheap-offer bids and limits apply in every case |
| Ranking | Scorecard | LightGBM supply model: chance each counterparty can supply this issuer in the next 30 days, learned from its sales, email offers, sell orders and WhatsApp activity in the ISIN, issuer, sector and rating band. Approved only if it ranks real suppliers higher than every simple baseline, including when the dominant seller is left out |
| Explanations | Template reasons | `quote --explain`: Gemini (or any `LLM_PROVIDER`) writes plain-English reasons and flags |
| Reading replies | Rules, LLM when unsure | Same |

`train` builds the datasets from platform history (buy orders/RFQs and their outcome; executed deals and who
sold), point-in-time, tests on the most recent 20% it never saw, approves a model only if it beats simple baselines, and saves `models/markup_model.joblib`, `models/fill_model.joblib`,
`models/rank_model.joblib`, `models/report.json` (scores, feature importance, fill curve) and the datasets as CSV.
`MARKUP_ENGINE` / `RANKING_ENGINE` = `auto` (model if trained), `rules`/`scorecard`, or `lightgbm`.
`diagnose` shows whether there is enough history. The model never sees WhatsApp data (dummy or real), and the LLM
never changes the bid or the ranking. Each saved bid records which engines produced it.

## Markup: most profit that still fills

`MARKUP_ENGINE=optimise` (default): for each markup the limits allow (5 bps steps), AutoDesq works out the revenue if
filled and the chance that at least one of the top 5 counterparties confirms (from the ranking at that bid). It picks
the markup with the highest expected revenue (revenue x fill chance) whose fill chance is at least `MIN_FILL_CHANCE`
(default 0.6); if none reaches it, the markup most likely to fill. Rule-table limits and cheap-offer bids still apply.
`MARKUP_ENGINE=rules` uses the rule table alone. The screen shows the full table in section 2; click a row to use it.

## Demo replies

`python -m autodesq.cli demo-replies INE0NES07352 12.20 5` (or **Add demo replies** in section 7) creates 12 labelled dummy
requirements with replies that follow each counterparty's current chance, so the ranking shows feedback before real
replies exist. They are tagged "(dummy replies)", hidden from Responses, never used for training, and removed with
`demo-replies --clear` (or **Clear demo replies**).

## Ranking feedback loop

1. **Every reply counts at once.** `reply` saves the dealer's answer; bids with no answer after 2 hours are
   recorded as "no reply". The next quote updates each counterparty's chance from its replies since the model
   was trained (confirmed = success, counter = half, NA / no reply = miss; same issuer counts double; an NA
   to the same bond in the last 3 days pushes it down hard). The reason is shown: "Feedback: 2 confirmed, 1 NA ...".
2. **Retraining learns from them.** `train` adds reply rates and confirm rates as model inputs, and turns every
   answered AutoDesq requirement into a training case (dealers bid to = candidates, those who confirmed = answer).
3. **A worse model never goes live.** The new model must be at least as good as every baseline (and better on one test) and at least as good as the
   current model on the same recent period; otherwise the current one stays. Old models go to `models/archive/`.
4. **Weekly retrain (Windows):**
   `schtasks /create /sc weekly /d SUN /st 22:00 /tn AutoDesqRetrain /tr "cmd /c cd /d <folder> && python -m autodesq.cli train >> train.log 2>&1"`

`python -m autodesq.cli feedback` shows replies per counterparty since the last retrain.

## Where each table is used

| Table | Database | Used for |
| --- | --- | --- |
| bond_issuances, issuers, issuer_rating_history | RDS | Bond terms, rating, issuer |
| cashflow_master | RDS | Exact cash flows for pricing (amortising bonds included); falls back to coupon + maturity |
| trade_spread_decomposition | RDS | Fair YTM = G-sec yield + fair_value_bps, band from fair_value_band90_bps |
| nse_trade_data, bse_trade_data | RDS | Liquidity (trades, volume, days since last trade); fair YTM fallback |
| email_offers | RDS | Live offers (markup snaps to them) and dealer activity |
| counterparty, employees, customers | RDS | Dealer list; matching email senders and orders to cpty_id |
| order_tran, rfq_tran | RDS | Dealers' sell orders / sell RFQs in this issuer |
| indication_of_interest | RDS | Executed deals: who delivered this ISIN or issuer (sell side) |
| bond_intelligence | TEST | WhatsApp offers, mentions and response rates |
| autodesq_requirements, autodesq_bids, autodesq_bid_replies | TEST | AutoDesq's own log: every bid with its snapshot and outcome |

AutoDesq never writes to the RDS; `db.execute` refuses it.

## Dummy WhatsApp data (prototype)

The real WhatsApp intelligence is incomplete, so `WA_DUMMY=on` adds made-up rows, for the counterparties really active in the issuer, on top of it.
They are labelled "(dummy)" in every reason, counted separately in each quote and bid snapshot (`wa_dummy_rows`),
and dummy offers never set the markup or a bid level (`WA_DUMMY_SETS_MARKUP=off`). Set `WA_DUMMY=off` for real use.

## Check these assumptions against real data

1. **Fair YTM units.** `fair_value_bps` is treated as the fair spread over `gsec_yield`, and `fair_value_band90_bps`
   as the half-width of the 90% band. Yields below 1 are read as decimals (0.0685 = 6.85%). Fair values are taken
   from trades in the last 30 days, without moving them for G-sec changes since the trade date.
2. **email_offers.user_name.** Matched to a cpty_id by exact email, email domain, contact name, then firm name.
   Unmatched senders still count as live offers but give no dealer credit. Add fixes to `config.ALIASES`.
3. **Counterparties.** By default (`DEALER_SCOPE=all`) every counterparty except INRBonds' own ids is ranked and can
   be bid to; `EXCLUDE_CLIENTS=on` drops those flagged `is_client`. `DEALER_SCOPE=list` limits it to `config.DEALERS`.
   cpty_ids with the same legal name (OXYZO and OXYVENTPLTD) count as one counterparty.
4. **bond_intelligence columns.** Uses cpty_id, isin, issuer_id, side, response_status, quoted_ytm; finds the time
   and quantity columns by name. Offers are rows whose `side` is offer/sell/ask.
5. **Quantities.** Requirements are in Rs crore of face value; bonds = crore x 1e7 / current face value.
   Set `EXCHANGE_VOLUME_TO_CR` if exchange volume is in lakh.
6. **IOI status.** Deliveries are IOIs with `status = 'executed'`, seller taken from the sell-side order or RFQ.
7. **INRBonds' own cpty_id** goes in `OWN_CPTY_IDS` so it is never ranked as a dealer.

## LLM for reading replies

The reply reader uses rules first and only asks the LLM when unsure. Set `LLM_PROVIDER` in `.env`:

| Provider | Setting | Cost | Client data |
| --- | --- | --- | --- |
| Qwen3 on Ollama | `ollama` (`ollama pull qwen3:8b`) | Free | Stays on your machine |
| Gemini | `gemini` + `GEMINI_API_KEY` from Google AI Studio | Free tier, or pay per use | Free tier: Google may use it to improve its products. Use a billed project for real replies |
| Claude | `anthropic` + `ANTHROPIC_API_KEY` | Pay per use | Not used for training |
| None | `none` | | Unclear replies wait for the trader |

A Gemini app subscription (Google AI Pro) does not include API access; the key comes from Google AI Studio.
`GEMINI_MODEL` defaults to the latest Flash model; any model name listed in AI Studio works.
Check the connection with `python -m autodesq.cli test-llm "best 12.45 for 3cr"`.

## Tests

```bash
python -m pytest tests/test_core.py          # no database needed
# end to end on LOCAL copies only (refuses non-localhost URLs):
python tests/seed_local.py && python -m autodesq.cli quote INE0NES07352 12.20 5
```

## Next (phase 2)

Once ~500 requirements have outcomes, train LightGBM on `autodesq_bids.features` -> reply status to predict fill
chance per markup and per dealer; the `alternatives` table in each quote already shows the expected-revenue view
the model will optimise.
