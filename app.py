"""AutoDesq desk screen.   Run:  streamlit run app.py   (opens http://localhost:8501)"""
import pandas as pd
import streamlit as st

from autodesq import config, engine, feedback, ml, store
from autodesq.ranking import feedback_since

st.set_page_config(page_title="AutoDesq", page_icon="📈", layout="wide")


@st.cache_resource
def _tables():
    try:
        store.init_db()
    except Exception as e:
        st.warning(f"Could not create AutoDesq tables on the test DB: {e}")


_tables()
st.title("AutoDesq")
st.caption("Find the counterparties most likely to deliver a bond, send them bids, and learn from every reply.")

tab_quote, tab_replies, tab_learning = st.tabs(["Rank counterparties", "Replies", "Learning"])

# ------------------------------------------------------------------ 1. rank
with tab_quote:
    with st.form("req"):
        c1, c2, c3, c4 = st.columns([2, 1, 1, 2])
        isin = c1.text_input("ISIN", "INE0NES07352")
        inv_y = c2.number_input("Investor yield (%)", value=12.20, step=0.05, format="%.2f")
        qty = c3.number_input("Quantity (Rs Cr)", value=5.0, step=1.0, min_value=0.1)
        investor = c4.text_input("Investor", "Investor")
        explain = st.checkbox(f"Add plain-English explanations ({config.LLM_PROVIDER})",
                              value=False, disabled=config.LLM_PROVIDER == "none")
        go = st.form_submit_button("Rank counterparties", type="primary")
    if go:
        with st.spinner("Reading bond, market and counterparty data ..."):
            try:
                st.session_state.quote = engine.quote(isin.strip(), inv_y, qty, investor, explain=explain)
                st.session_state.sent = None
            except Exception as e:
                st.error(f"Could not rank: {e}")

    q = st.session_state.get("quote")
    if q is None:
        st.info("Enter a requirement and select Rank counterparties.")
    else:
        d = q.to_dict()
        b, f = d["bond"], d["fair"]
        st.subheader(f"{b['issuer_name']} {float(b['coupon']):.2f}% {b['maturity_date']}")
        m = st.columns(5)
        m[0].metric("Rating", b.get("rating") or "-")
        m[1].metric("Fair YTM", f"{f['low']:.2f}–{f['high']:.2f}%" if f else "n/a")
        m[2].metric("Our bid", f"{d['bid_yield']:.2f}%", f"+{d['markup_bps']:g} bps")
        m[3].metric("Revenue if filled", f"Rs {d['revenue_rs']:,}")
        m[4].metric("Ranked by", "Model" if d["engines"]["ranking"] != "scorecard" else "Scorecard")
        with st.expander("How the bid was set"):
            st.dataframe(pd.DataFrame(d["steps"]), hide_index=True, width="stretch")
            for fl in d["flags"]:
                st.warning(fl)
            if f:
                st.caption(f"Fair YTM source: {f['source']}, {f['n_obs']} observations. "
                           f"WhatsApp rows: {d['whatsapp_rows'].get('real', 0)} real, {d['whatsapp_rows'].get('dummy', 0)} dummy.")

        rows = [r for r in d["dealers"] if r["reasons"] != ["No recent activity in this issuer"]]
        table = pd.DataFrame([{
            "Send": i < config.TOP_N, "Rank": r["rank"], "Counterparty": r["name"],
            "Delivery chance": round(r["chance"], 2), "Bid yield": r["bid_yield"],
            "Why": "; ".join(r["reasons"]), "cpty_id": r["cpty_id"]} for i, r in enumerate(rows)])
        st.markdown(f"**Counterparties** — {len(rows)} with activity in this bond, of {len(d['dealers'])} considered. "
                    f"The top {config.TOP_N} are ticked.")
        edited = st.data_editor(
            table, hide_index=True, width="stretch", disabled=[c for c in table.columns if c != "Send"],
            column_config={"Delivery chance": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1),
                           "Bid yield": st.column_config.NumberColumn(format="%.2f%%"),
                           "Why": st.column_config.TextColumn(width="large"), "cpty_id": None},
            key=f"editor_{id(q)}")

        x = d.get("explanation")
        if x and not x.get("error"):
            st.info(x["markup_note"])
            names = {r["cpty_id"]: r["name"] for r in d["dealers"]}
            for y in x["dealers"]:
                st.write(f"**{names.get(y['cpty_id'], y['cpty_id'])}**: {y['reason']}" + (f"  ⚠️ {y['flag']}" if y["flag"] else ""))
        elif x and x.get("error"):
            st.caption(f"Explanations not available: {x['error']}")

        chosen = edited.loc[edited["Send"], "cpty_id"].tolist()
        if st.button(f"Send bids to {len(chosen)} counterparties", type="primary", disabled=not chosen):
            req_id, bid_ids = engine.send_bids(q, chosen)
            st.session_state.sent = req_id
            st.success(f"Bids sent and saved as requirement {req_id}. Record replies in the Replies tab.")
        if st.session_state.get("sent"):
            for r in store.bid_messages(st.session_state.sent).itertuples():
                with st.expander(f"Message to {r.cpty_name}"):
                    st.code(r.message, language=None)

# ------------------------------------------------------------------ 2. replies
STATUS = {"confirmed": "✅ Confirmed", "accepted": "✅ Accepted", "na": "❌ NA", "declined": "❌ Declined",
          "counter": "🔁 Counter-offer", "unclear": "❔ Unclear", "no_reply": "⏳ No reply", None: "Waiting"}

with tab_replies:
    feedback.expire_stale()
    try:
        reqs = store.recent_requirements()
    except Exception as e:
        reqs = pd.DataFrame()
        st.error(f"Could not read AutoDesq records on the test DB: {e}")
    if reqs.empty:
        st.info("No bids sent yet. Rank counterparties and send bids first.")
    else:
        labels = {r.id: f"#{r.id}  {r.isin}  {r.investor_name}  {r.qty_cr:g} Cr at {r.investor_yield:.2f}%  "
                        f"({pd.Timestamp(r.created_at):%d %b %H:%M})" for r in reqs.itertuples()}
        req_id = st.selectbox("Requirement", list(labels), format_func=labels.get,
                              index=list(labels).index(st.session_state["sent"]) if st.session_state.get("sent") in labels else 0)
        status = store.requirement_status(req_id)
        req = store.requirement(req_id)
        filled = float(status.loc[status["status"].isin(["confirmed", "accepted"]), "reply_qty_cr"].fillna(0).sum())
        st.progress(min(filled / float(req["qty_cr"]), 1.0), text=f"Filled Rs {filled:g} of {float(req['qty_cr']):g} Cr")
        for r in status.itertuples():
            with st.container(border=True):
                c1, c2 = st.columns([3, 2])
                c1.markdown(f"**{r.cpty_name}**  \nbid {r.bid_yield:.2f}% for Rs {r.qty_cr:g} Cr · "
                            f"chance at send {r.delivery_chance:.0%}")
                st_txt = STATUS.get(r.status if isinstance(r.status, str) else None, r.status)
                extra = (f" at {r.reply_yield:.2f}%" if pd.notna(r.reply_yield) and r.status == "counter" else "") + \
                        (f" for Rs {r.reply_qty_cr:g} Cr" if pd.notna(r.reply_qty_cr) and r.status in ("counter", "confirmed") else "")
                c2.markdown(f"{st_txt}{extra}" + (f"  \n_“{r.raw_text}”_" if isinstance(r.raw_text, str) and r.raw_text else ""))
                with st.form(f"reply_{r.bid_id}"):
                    t1, t2, t3 = st.columns([4, 2, 1])
                    text = t1.text_input("Dealer's reply", placeholder="e.g. done / NA bhai / best 12.45 for 3cr",
                                         label_visibility="collapsed")
                    corr = t2.selectbox("Status", ["Read automatically", "confirmed", "na", "counter", "accepted", "declined"],
                                        label_visibility="collapsed")
                    if t3.form_submit_button("Save"):
                        out = engine.record_reply(int(r.bid_id), text,
                                                  None if corr == "Read automatically" else corr)
                        st.toast(f"{r.cpty_name}: {STATUS.get(out['status'], out['status'])}")
                        st.rerun()
        st.caption("Every reply updates this counterparty's ranking in the next quote. Bids without a reply after "
                   f"{feedback.NO_REPLY_HOURS} hours count as no reply.")

# ------------------------------------------------------------------ 3. learning
with tab_learning:
    rep = ml.report().get("rank", {})
    use_model = ml.engine_wanted(config.RANKING_ENGINE, "rank")
    st.subheader("Ranking model")
    if not rep:
        st.info("No model trained yet: the scorecard ranks counterparties. Train the model below.")
    else:
        c = st.columns(3)
        c[0].metric("Status", "In use" if use_model else "Not in use")
        c[1].metric("Trained on", rep.get("trained_on", "-"))
        c[2].metric("Bid cases learned from", rep.get("autodesq_bid_cases", 0))
        for name, key in (("All suppliers", "test_all_suppliers"), ("Leaving out the dominant supplier", "test_excluding_dominant")):
            if key in rep:
                t = pd.DataFrame(rep[key]).T.rename(index={"model": "LightGBM model", "busiest": "Busiest seller",
                                                           "simple": "Simple activity count", "issuer_history": "Past sales of issuer"})
                st.markdown(f"**{name}**: tested on recent history the model never saw")
                t = t[["mrr", "top1", "top3"]].rename(columns={"mrr": "Ranking score (1 = supplier always first)",
                                                              "top1": "Supplier ranked first", "top3": "Supplier in top 3"})
                st.dataframe(t, width="stretch", column_config={
                    "Ranking score (1 = supplier always first)": st.column_config.NumberColumn(format="%.2f"),
                    "Supplier ranked first": st.column_config.NumberColumn(format="percent"),
                    "Supplier in top 3": st.column_config.NumberColumn(format="percent")})
        if rep.get("importance"):
            from autodesq.rank_model import LABELS
            imp = pd.Series(rep["importance"]).rename(index=LABELS)
            imp = (imp[imp > 0] / imp.sum()).sort_values(ascending=False)
            st.markdown("**What the model relies on**")
            st.dataframe(imp.rename("Share of the model's decisions").to_frame(), width="stretch", column_config={
                "Share of the model's decisions": st.column_config.ProgressColumn(format="percent", min_value=0,
                                                                                    max_value=float(imp.max()))})
        st.caption(f"Checks: {rep.get('checks')}. {rep.get('note', '')}")

    st.subheader("Replies since the last training")
    fb = feedback.summary(feedback_since(use_model))
    if fb.empty:
        st.info("No replies yet. They adjust the ranking straight away and become training data at the next retrain.")
    else:
        st.dataframe(fb, width="stretch")

    if st.button("Retrain ranking model"):
        from autodesq import history
        with st.spinner("Retraining on platform history and dealers' replies ..."):
            r = history.train_ranking()
        if r.get("note"):
            st.warning("The new model was not better, so the current model stays in use.")
        elif r.get("approved"):
            st.success("New model approved and in use.")
        else:
            st.warning(f"Model not approved: {r.get('checks', r.get('reason'))}. The scorecard stays in use.")
