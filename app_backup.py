import uuid, requests, streamlit as st
from langgraph.types import Command
from agent.graph import build
from streamlit_mic_recorder import mic_recorder

MERCHANT = "http://127.0.0.1:8000"

st.set_page_config(page_title="Sharma Sweets — agentic commerce",
                   page_icon="🍬", layout="wide")
st.markdown("""
<style>
  .stApp { background:#0f1115; color:#d8dee9; }
  h1 { font-size:2.1rem !important; font-weight:650 !important;
       letter-spacing:-0.02em; color:#f0f3f8 !important;text-align:center !important; }
  h3 { font-size:0.78rem !important; font-weight:600 !important;
       text-transform:uppercase; letter-spacing:0.1em;
       color:#7aa2f7 !important; margin-top:1.6rem !important; }
  section[data-testid="stSidebar"] { display:none; }

  div[data-testid="stTextInput"] input,
  div[data-testid="stNumberInput"] input,
  div[data-testid="stMultiSelect"] div[data-baseweb="select"] > div {
      background:#161a21 !important; border:1px solid #2a303b !important;
      color:#d8dee9 !important; border-radius:6px !important; }

  .stButton > button {
      background:#1b1f27; border:1px solid #333a45; color:#d8dee9;
      border-radius:6px; font-weight:500; transition:all .15s; }
  .stButton > button:hover { border-color:#7aa2f7; color:#fff; }
  .stButton > button[kind="primary"] {
      background:#2f6f4f; border-color:#3d8a63; color:#fff; }
  .stButton > button[kind="primary"]:hover { background:#3d8a63; }

  div[data-testid="stExpander"] {
      background:#141821; border:1px solid #232a35; border-radius:8px; }

  .trail-row { border-left:3px solid #3b4252; background:#141821;
      padding:10px 14px; margin:7px 0; border-radius:0 6px 6px 0; }
  .trail-ok      { border-left-color:#5fa87a; }
  .trail-refused { border-left-color:#c96a6a; background:#20161a; }
  .trail-head { font-size:0.9rem; font-weight:600; color:#e6ebf2; }
  .trail-actor { font-family:ui-monospace,Consolas,monospace;
      font-size:0.7rem; color:#7aa2f7; background:#1b2130;
      padding:1px 7px; border-radius:4px; margin-left:8px; }
  .trail-amt { float:right; color:#e0c07a; font-size:0.85rem; }
  .trail-reason { font-size:0.8rem; color:#8b95a3; margin-top:3px; }

  /* clear the white bars behind embedded components */
  header[data-testid="stHeader"] { background:transparent !important; }
  div[data-testid="stToolbar"] { background:transparent !important; }
  .block-container { padding-top:2rem !important; }

  /* the mic component renders in its own iframe with a white body.
     CSS cannot reach inside it, so clip the iframe to button width. */
  .stApp iframe { background:transparent !important; color-scheme:dark; }
  .stApp div[data-testid="stCustomComponentV1"],
  .stApp div[data-testid="stIFrame"] {
      background:transparent !important;
      width:200px !important; }
  .stApp div[data-testid="stCustomComponentV1"] iframe,
  .stApp div[data-testid="stIFrame"] iframe {
      width:200px !important; border-radius:6px; }
  div[data-testid="element-container"]:has(iframe) {
      background:transparent !important;
      width:fit-content !important; }

  /* field labels in amber, matching the amounts */
  div[data-testid="stWidgetLabel"] p,
  label p {
      color:#e0c07a !important; font-weight:600 !important;
      font-size:0.82rem !important; text-transform:uppercase;
      letter-spacing:0.06em; }
</style>
""", unsafe_allow_html=True)

st.title("Welcome to Agentic commerce — bounded AI shopping")
# st.caption("A merchant, an AI buyer can purchase from unattended, with "
#            "spending authority the merchant enforces, not the agent.")

for k in ("graph", "cfg", "trace", "pending", "result", "mandate",
          "last_audio_id", "paid_order"):
    st.session_state.setdefault(k, None)
st.session_state.setdefault("spoken", "Add any sweet you want")

left, right = st.columns([1, 1], gap="large")

# ─────────────── left: mandate + instruction ───────────────
with left:
    st.subheader("1. Grant spending authority")

    cap  = st.number_input("Spending cap (Rs)", 100, 100000, 2000, step=100)
    cats = st.multiselect("Allowed categories",["sweets", "premium", "milk", "new", "addons"],["sweets"])

    if st.button("Issue mandate", use_container_width=True):
        r = requests.post(f"{MERCHANT}/mandates", json={
            "agent_id": "agt_ui", "max_amount_paise": int(cap * 100),
            "allowed_categories": cats, "valid_days": 7})
        st.session_state.mandate = r.json()["mandate_id"]
        st.session_state.result = None
        st.session_state.pending = None
        st.session_state.paid_order = None

    if st.session_state.mandate:
        st.success(f"Mandate {st.session_state.mandate} — "
                   f"Rs {cap:,.0f} on {', '.join(cats)}")

    with st.expander("What this merchant sells (machine-readable catalog)"):
        if st.button("Load catalog"):
            st.json(requests.get(f"{MERCHANT}/catalog").json())

    st.subheader("2. Tell the agent what to buy")

    with st.container():
        audio = mic_recorder(start_prompt="🎤 Speak instead",stop_prompt="⏹ Stop", key="voice")

    if audio and audio.get("bytes") and \
       audio.get("id") != st.session_state.get("last_audio_id"):
        st.session_state.last_audio_id = audio.get("id")
        with st.spinner("transcribing"):
            if "whisper" not in st.session_state:
                from faster_whisper import WhisperModel
                st.session_state.whisper = WhisperModel(
                    "base", device="cpu", compute_type="int8")
            with open("temp_voice.wav", "wb") as f:
                f.write(audio["bytes"])
            segs, _ = st.session_state.whisper.transcribe(
                "temp_voice.wav", language="en")
            heard = " ".join(s.text for s in segs).strip()
        if heard:
            st.session_state.spoken = heard
            st.rerun()

    text = st.text_input("Instruction", st.session_state.spoken,
                         label_visibility="collapsed",
                         help="Type it, or use the mic above. "
                              "You can edit what was heard.")

    if st.button("Run agent", type="primary", use_container_width=True,
                 disabled=not st.session_state.mandate):
        if not text.strip():
            st.warning("Give the agent an instruction first.")
        else:
            st.session_state.trace = f"agt_{uuid.uuid4().hex[:8]}"
            st.session_state.graph = build()
            st.session_state.paid_order = None
            st.session_state.cfg = {"configurable":
                                    {"thread_id": st.session_state.trace}}
            with st.spinner("agent working — local model, this takes ~30s"):
                res = st.session_state.graph.invoke({
                    "instruction": text,
                    "trace_id":    st.session_state.trace,
                    "mandate_id":  st.session_state.mandate,
                    "cap_paise":   int(cap * 100),
                    "notes": [], "status": "running"}, st.session_state.cfg)
            st.session_state.pending = res.get("__interrupt__")
            st.session_state.result  = res
            st.rerun()

    # ─────────── the gate ───────────
    if st.session_state.pending:
        ask = st.session_state.pending[0].value
        st.divider()
        clicked = None

        if ask.get("kind") == "budget_scope":
            st.warning(ask["message"])
            c1, c2 = st.columns(2)
            if c1.button("Whole order", type="primary",
                         use_container_width=True):
                clicked = "total"
            if c2.button("Per item", use_container_width=True):
                clicked = "per item"

        elif ask.get("kind") == "over_budget":
            st.warning(ask["message"])
            c1, c2 = st.columns(2)
            if c1.button("Continue", type="primary",
                         use_container_width=True):
                clicked = "y"
            if c2.button("Stop", use_container_width=True):
                clicked = "n"

        elif ask.get("kind") == "not_affordable":
            st.warning(ask["message"])
            c1, c2 = st.columns(2)
            if c1.button("Show me that instead", type="primary",
                         use_container_width=True):
                clicked = "show"
            if c2.button("Cancel", use_container_width=True):
                clicked = "n"

        else:
            st.info(f"**{ask.get('summary')}**\n\n"
                    f"Total: Rs {ask.get('total_paise', 0)/100:,.0f}")
            c1, c2 = st.columns(2)
            if c1.button("Approve", type="primary",
                         use_container_width=True):
                clicked = "y"
            if c2.button("Decline", use_container_width=True):
                clicked = "n"

        if clicked:
            with st.spinner("resuming"):
                res = st.session_state.graph.invoke(
                    Command(resume=clicked), st.session_state.cfg)
            st.session_state.pending = res.get("__interrupt__")
            st.session_state.result  = res
            st.rerun()

    # ─────────── outcome ───────────
    r = st.session_state.result
    if r and not st.session_state.pending:
        st.divider()
        if r.get("status") == "refused":
            st.error(f"Refused — {r.get('refusal_reason')}")

        elif r.get("payment_url"):
            oid = r["order_id"]

            if not st.session_state.paid_order:
                o = requests.get(f"{MERCHANT}/orders/{oid}").json()
                if o["status"] == "paid":
                    st.session_state.paid_order = o
                    st.balloons()
                    st.rerun()

            if st.session_state.paid_order:
                o = st.session_state.paid_order
                st.success(f"Payment complete — "
                           f"Rs {o['total_paise']/100:,.0f}")
                st.caption("Receipt is on the right.")
                if st.button("New order", use_container_width=True):
                    for k in ("result", "pending", "paid_order", "trace"):
                        st.session_state[k] = None
                    st.rerun()
            else:
                st.success(f"Order {oid} created")
                st.link_button("Complete payment", r["payment_url"],
                               use_container_width=True)
                st.caption("Pay in the tab that opens, then come back here.")
                if st.button("I've paid — check now", type="primary",
                             use_container_width=True):
                    st.rerun()

# ─────────────── right: receipt + audit trail ───────────────
with right:
    open_by_default = bool(st.session_state.trace)

    if st.session_state.paid_order:
        o = st.session_state.paid_order
        lines = "\n".join(
            f"  {l['qty']} x {l['name']:<22} "
            f"Rs {l['subtotal_paise']/100:>8,.0f}"
            for l in o["items"])
        sep_eq, sep_dash = "=" * 46, "-" * 46
        receipt = (
            f"SHARMA SWEETS\n{sep_eq}\n"
            f"Order    {o['order_id']}\n"
            f"Trace    {st.session_state.trace}\n"
            f"Status   {o['status'].upper()}\n\n"
            f"{lines}\n{sep_dash}\n"
            f"TOTAL                          Rs "
            f"{o['total_paise']/100:>8,.0f}\n{sep_eq}\n"
            f"Paid through Razorpay test mode.\n"
            f"Purchased by an AI agent under a mandate the merchant\n"
            f"verified before any payable order existed.\n")

        st.subheader("Receipt")
        st.code(receipt, language=None)
        st.download_button("Download receipt", receipt,
                           file_name=f"receipt_{o['order_id']}.txt",
                           use_container_width=True)
        st.divider()

    with st.expander("AUDIT TRAIL", expanded=open_by_default):
        st.caption(
            "Every decision either side made, in order. The agent's steps "
            "and the merchant's checks share one trace id, so a whole "
            "purchase reads as a single story. Refusals are recorded as "
            "carefully as successes — red means something was stopped, "
            "and nothing after it happened.")

        if not st.session_state.trace:
            st.caption("Run the agent to see a trail.")
        else:
            st.caption(f"trace {st.session_state.trace}")
            try:
                rows = requests.get(
                    f"{MERCHANT}/audit/{st.session_state.trace}").json()
            except Exception:
                rows = []
            for row in rows:
                cls = ("trail-ok" if row["decision"] == "ok"
                       else "trail-refused")
                amt = (f'<span class="trail-amt">Rs '
                       f'{row["amount_paise"]/100:,.0f}</span>'
                       if row["amount_paise"] else "")
                st.markdown(
                    f'<div class="trail-row {cls}">'
                    f'{amt}'
                    f'<span class="trail-head">{row["step"]}</span>'
                    f'<span class="trail-actor">{row["actor"]}</span>'
                    f'<div class="trail-reason">{row["reason"]}</div>'
                    f'</div>', unsafe_allow_html=True)