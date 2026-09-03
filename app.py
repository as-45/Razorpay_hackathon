import uuid, requests, streamlit as st
from langgraph.types import Command
from agent.graph import build
from streamlit_mic_recorder import mic_recorder
MERCHANT = "http://127.0.0.1:8000"

st.set_page_config(page_title="Agentic commerce", layout="wide")
st.title("Agentic commerce — bounded AI shopping")
st.caption("A merchant an AI buyer can purchase from unattended, with "
           "spending authority the merchant enforces, not the agent.")

for k in ("graph", "cfg", "trace", "pending", "result", "mandate",
          "last_audio_id"):
    st.session_state.setdefault(k, None)

left, right = st.columns([1, 1], gap="large")

# ─────────────── left: mandate + instruction ───────────────
with left:
    st.subheader("1. Grant spending authority")

    cap  = st.number_input("Spending cap (Rs)", 100, 100000, 2000, step=100)
    cats = st.multiselect("Allowed categories",
                          ["sweets", "premium", "addons"], ["sweets"])

    if st.button("Issue mandate", use_container_width=True):
        r = requests.post(f"{MERCHANT}/mandates", json={
            "agent_id": "agt_ui", "max_amount_paise": int(cap * 100),
            "allowed_categories": cats, "valid_days": 7})
        st.session_state.mandate = r.json()["mandate_id"]
        st.session_state.result = st.session_state.pending = None

    if st.session_state.mandate:
        st.success(f"Mandate {st.session_state.mandate} — "
                   f"Rs {cap:,.0f} on {', '.join(cats)}")

    with st.expander("What this merchant sells (machine-readable catalog)"):
        if st.button("Load catalog"):
            st.json(requests.get(f"{MERCHANT}/catalog").json())

    st.subheader("2. Tell the agent what to buy")
    st.session_state.setdefault("spoken","2 boxes of kaju katli under Rs 2000")
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
            st.success(f"Order {r['order_id']} created")
            st.link_button("Complete payment", r["payment_url"],
                           use_container_width=True)
            if st.button("Check payment status", use_container_width=True):
                o = requests.get(
                    f"{MERCHANT}/orders/{r['order_id']}").json()
                st.write(f"status: **{o['status']}**")

# ─────────────── right: audit trail ───────────────
with right:
    st.subheader("Audit trail")
    if not st.session_state.trace:
        st.caption("run the agent to see the trail")
    else:
        st.caption(st.session_state.trace)
        try:
            rows = requests.get(
                f"{MERCHANT}/audit/{st.session_state.trace}").json()
        except Exception:
            rows = []
        for row in rows:
            icon = "🟢" if row["decision"] == "ok" else "🔴"
            amt  = (f" · Rs {row['amount_paise']/100:,.0f}"
                    if row["amount_paise"] else "")
            st.markdown(
                f"{icon} **{row['step']}** · `{row['actor']}`{amt}  \n"
                f"<span style='color:#888;font-size:0.85em'>"
                f"{row['reason']}</span>",
                unsafe_allow_html=True)