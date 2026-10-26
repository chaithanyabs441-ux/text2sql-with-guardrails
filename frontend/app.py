"""Streamlit interface for the guarded Text-to-SQL API."""

import os

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Trustworthy Text-to-SQL", page_icon="🛡️", layout="wide")
st.title("Trustworthy Text-to-SQL")
st.caption("Natural-language SQL with policy checks, read-only execution, and confidence signals.")

api_base_url = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
question = st.text_input(
    "Ask a question",
    "What is the total net revenue for enterprise customers in Europe?",
)
strategy = st.selectbox(
    "Generation strategy",
    ("schema_glossary", "zero_shot", "schema_glossary_selfcorrect"),
)

if st.button("Run query", type="primary"):
    with st.spinner("Generating candidates, checking policy, and verifying results…"):
        try:
            response = requests.post(
                f"{api_base_url}/v1/query",
                json={"question": question, "strategy": strategy},
                timeout=240,
            )
            if response.status_code != 200:
                st.error(f"API error {response.status_code}: {response.json().get('detail')}")
            else:
                result = response.json()
                decision = result["decision"]
                score = result["confidence_score"]
                st.subheader(f"Decision: {decision.replace('_', ' ').title()}")
                st.metric(
                    "Confidence score",
                    f"{score:.3f}",
                    help=(
                        "Calibrated score when a fitted report is available; otherwise "
                        "uses the model's self-report."
                    ),
                )
                if not result["calibration_ready"]:
                    st.info(
                        "Calibration report not loaded. The API is using fallback "
                        "confidence thresholds."
                    )

                if result["sql"]:
                    st.subheader("Validated SQL")
                    st.code(result["sql"], language="sql")
                    st.write(result["explanation"])
                if result["data"]:
                    st.subheader("Query results")
                    st.dataframe(pd.DataFrame(result["data"]), use_container_width=True)
                elif decision != "abstain_with_alternatives":
                    st.info("The query returned no rows.")

                signals_col, verification_col = st.columns(2)
                with signals_col:
                    st.subheader("Confidence signals")
                    for name, value in result["confidence_signals"].items():
                        st.write(f"**{name.replace('_', ' ').title()}:** {value:.3f}")
                with verification_col:
                    st.subheader("Back-translation")
                    st.write(result["back_translated_question"])

                if result["warnings"]:
                    st.warning("Sanity warnings")
                    for warning in result["warnings"]:
                        st.write(f"- {warning}")
                if result["alternatives"]:
                    st.subheader("Safe alternative candidates")
                    for alternative in result["alternatives"]:
                        st.code(alternative, language="sql")
        except requests.RequestException as exc:
            st.error(f"Could not reach the API at {api_base_url}: {exc}")
