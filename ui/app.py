"""
Seedance Lead Qualification Engine — UI
Run with: streamlit run ui/app.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
OUTPUT_DIR = ROOT / "output"
ARTIFACTS_DIR = OUTPUT_DIR / "artifacts"

QUALIFIED_CSV = OUTPUT_DIR / "qualified_leads.csv"
REVIEW_CSV = OUTPUT_DIR / "review_leads.csv"
DISQUALIFIED_CSV = OUTPUT_DIR / "disqualified_leads.csv"

# ── Colour palette ─────────────────────────────────────────────────────────────
COLORS = {
    "qualified": "#22c55e",
    "review": "#f59e0b",
    "disqualified": "#ef4444",
    "bg": "#0f172a",
    "card": "#1e293b",
    "text": "#f1f5f9",
    "muted": "#94a3b8",
}

DECISION_COLOR_MAP = {
    "qualified": COLORS["qualified"],
    "review": COLORS["review"],
    "disqualified": COLORS["disqualified"],
}

SEGMENT_LABELS = {
    "film_production": "Film Production",
    "short_drama": "Short Drama",
    "ai_tools": "AI Tools",
    "creative_agency": "Creative Agency",
    "content_creator": "Content Creator",
    "advertising": "Advertising",
    "other": "Other",
}

# ── Data loading ───────────────────────────────────────────────────────────────

@st.cache_data(ttl=10)
def load_all_leads() -> pd.DataFrame:
    frames = []
    for path in [QUALIFIED_CSV, REVIEW_CSV, DISQUALIFIED_CSV]:
        if path.exists():
            df = pd.read_csv(path)
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    combined["score"] = pd.to_numeric(combined["score"], errors="coerce")
    return combined


def load_artifact(lead_id: str, kind: str) -> Optional[dict]:
    path = ARTIFACTS_DIR / f"{lead_id}_{kind}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def get_upload_suffix(filename: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".csv", ".json"}:
        return suffix
    raise ValueError("Unsupported file type. Upload a .csv or .json file.")


def preview_uploaded_file(uploaded) -> tuple[pd.DataFrame, str]:
    suffix = get_upload_suffix(uploaded.name)
    if suffix == ".csv":
        return pd.read_csv(uploaded), "csv"

    data = json.load(uploaded)
    if not isinstance(data, list):
        raise ValueError("JSON upload must contain an array of lead objects.")
    return pd.json_normalize(data), "json"


# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Seedance Lead Engine",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* Dark sidebar */
    [data-testid="stSidebar"] {
        background-color: #0f172a;
    }
    /* Metric cards */
    [data-testid="metric-container"] {
        background-color: #1e293b;
        border-radius: 12px;
        padding: 16px 20px;
        border: 1px solid #334155;
    }
    /* Tab styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
    }
    .stTabs [data-baseweb="tab"] {
        background-color: #1e293b;
        border-radius: 8px;
        border: 1px solid #334155;
        color: #94a3b8;
        padding: 8px 20px;
    }
    .stTabs [aria-selected="true"] {
        background-color: #3b82f6 !important;
        color: #ffffff !important;
        border-color: #3b82f6 !important;
    }
    /* Score bar */
    .score-bar-wrap { background: #334155; border-radius: 6px; height: 8px; }
    .score-bar-fill { border-radius: 6px; height: 8px; }
    /* Decision badge */
    .badge {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 999px;
        font-size: 12px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    .badge-qualified  { background: #14532d; color: #22c55e; }
    .badge-review     { background: #451a03; color: #f59e0b; }
    .badge-disqualified { background: #450a0a; color: #ef4444; }
    /* Dim divider */
    hr { border-color: #334155 !important; }
    /* Fix plotly bg */
    .js-plotly-plot { border-radius: 12px; }
</style>
""", unsafe_allow_html=True)


# ── Sidebar navigation ─────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🎬 Seedance")
    st.markdown("**Lead Qualification Engine**")
    st.markdown("---")
    page = st.radio(
        "Navigation",
        ["Dashboard", "Leads", "Run Pipeline"],
        label_visibility="collapsed",
    )
    st.markdown("---")
    st.markdown(f"<span style='color:{COLORS['muted']};font-size:12px;'>Output dir: `{OUTPUT_DIR}`</span>", unsafe_allow_html=True)


# ── Helpers ────────────────────────────────────────────────────────────────────

def score_color(score: float) -> str:
    if score >= 70:
        return COLORS["qualified"]
    if score >= 60:
        return COLORS["review"]
    return COLORS["disqualified"]


def badge(decision: str) -> str:
    cls = f"badge-{decision}"
    label = decision.upper()
    return f'<span class="badge {cls}">{label}</span>'


def score_bar(score: float, max_score: float, color: str) -> str:
    pct = min(100, (score / max_score) * 100)
    return (
        f'<div class="score-bar-wrap">'
        f'<div class="score-bar-fill" style="width:{pct}%;background:{color};"></div>'
        f'</div>'
    )


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════
if page == "Dashboard":
    df = load_all_leads()

    st.title("Dashboard")

    if df.empty:
        st.info("No leads processed yet. Go to **Run Pipeline** to get started.")
        st.stop()

    total = len(df)
    n_qual = len(df[df["decision"] == "qualified"])
    n_rev = len(df[df["decision"] == "review"])
    n_dis = len(df[df["decision"] == "disqualified"])
    avg_score = df["score"].mean()

    # ── KPI row ────────────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Leads", total)
    c2.metric("Qualified", n_qual, delta=f"{n_qual/total*100:.0f}%")
    c3.metric("Review", n_rev, delta=f"{n_rev/total*100:.0f}%")
    c4.metric("Disqualified", n_dis, delta=f"{n_dis/total*100:.0f}%")
    c5.metric("Avg Score", f"{avg_score:.1f}")

    st.markdown("---")

    # ── Charts row ─────────────────────────────────────────────────────────────
    col_l, col_r = st.columns([3, 2])

    with col_l:
        st.subheader("Score Distribution")
        fig_hist = px.histogram(
            df, x="score", color="decision",
            nbins=20,
            color_discrete_map=DECISION_COLOR_MAP,
            labels={"score": "Score", "count": "Leads"},
        )
        fig_hist.add_vline(x=70, line_dash="dash", line_color="#22c55e", annotation_text="Qualified ≥70")
        fig_hist.add_vline(x=60, line_dash="dash", line_color="#f59e0b", annotation_text="Review ≥60")
        fig_hist.update_layout(
            paper_bgcolor="#1e293b", plot_bgcolor="#1e293b",
            font_color="#f1f5f9", margin=dict(t=20, b=20),
            legend=dict(bgcolor="#1e293b"),
            bargap=0.1,
        )
        st.plotly_chart(fig_hist, use_container_width=True)

    with col_r:
        st.subheader("Decision Breakdown")
        fig_donut = go.Figure(go.Pie(
            labels=["Qualified", "Review", "Disqualified"],
            values=[n_qual, n_rev, n_dis],
            hole=0.6,
            marker_colors=[COLORS["qualified"], COLORS["review"], COLORS["disqualified"]],
            textinfo="label+percent",
            textfont_size=13,
        ))
        fig_donut.update_layout(
            paper_bgcolor="#1e293b",
            font_color="#f1f5f9",
            margin=dict(t=20, b=20, l=0, r=0),
            showlegend=False,
        )
        st.plotly_chart(fig_donut, use_container_width=True)

    # ── Second row ─────────────────────────────────────────────────────────────
    col_a, col_b = st.columns(2)

    with col_a:
        if "segment" in df.columns:
            st.subheader("Segment Breakdown")
            seg_counts = (
                df[df["decision"] == "qualified"]["segment"]
                .value_counts()
                .reset_index()
                .rename(columns={"index": "segment", "count": "count"})
            )
            seg_counts["segment"] = seg_counts["segment"].map(
                lambda s: SEGMENT_LABELS.get(s, s)
            )
            fig_seg = px.bar(
                seg_counts, x="count", y="segment", orientation="h",
                color="count", color_continuous_scale="Blues",
                labels={"count": "Qualified Leads", "segment": ""},
            )
            fig_seg.update_layout(
                paper_bgcolor="#1e293b", plot_bgcolor="#1e293b",
                font_color="#f1f5f9", margin=dict(t=20, b=20),
                coloraxis_showscale=False,
            )
            st.plotly_chart(fig_seg, use_container_width=True)

    with col_b:
        st.subheader("Score by Dimension (Qualified Leads)")
        qual_df = df[df["decision"].isin(["qualified", "review"])].copy()
        dim_cols = ["segment_fit", "buyer_fit", "pain_fit", "workflow_fit", "commercial_fit"]
        # Try loading from artifacts if not in CSV
        if not any(c in qual_df.columns for c in dim_cols):
            rows = []
            for lid in qual_df["lead_id"].head(20):
                art = load_artifact(lid, "decision")
                if art:
                    rows.append({c: art.get(c, 0) for c in dim_cols})
            if rows:
                dim_avgs = pd.DataFrame(rows).mean()
            else:
                dim_avgs = None
        else:
            dim_avgs = qual_df[dim_cols].mean()

        if dim_avgs is not None:
            max_vals = {"segment_fit": 30, "buyer_fit": 25, "pain_fit": 20, "workflow_fit": 15, "commercial_fit": 10}
            labels = ["Segment", "Buyer", "Pain", "Workflow", "Commercial"]
            avgs = [dim_avgs.get(c, 0) if hasattr(dim_avgs, "get") else dim_avgs[c] for c in dim_cols]
            maxes = [max_vals[c] for c in dim_cols]
            pcts = [a / m * 100 for a, m in zip(avgs, maxes)]
            fig_radar = go.Figure(go.Bar(
                x=labels, y=pcts,
                marker_color=[COLORS["qualified"] if p >= 70 else COLORS["review"] if p >= 50 else COLORS["disqualified"] for p in pcts],
                text=[f"{a:.1f}/{m}" for a, m in zip(avgs, maxes)],
                textposition="outside",
            ))
            fig_radar.update_layout(
                paper_bgcolor="#1e293b", plot_bgcolor="#1e293b",
                font_color="#f1f5f9", margin=dict(t=30, b=20),
                yaxis=dict(range=[0, 110], ticksuffix="%"),
            )
            st.plotly_chart(fig_radar, use_container_width=True)

    # ── Confidence breakdown ───────────────────────────────────────────────────
    if "confidence" in df.columns:
        st.subheader("Confidence Distribution")
        conf_df = df.groupby(["confidence", "decision"]).size().reset_index(name="count")
        fig_conf = px.bar(
            conf_df, x="confidence", y="count", color="decision",
            color_discrete_map=DECISION_COLOR_MAP,
            barmode="stack",
            labels={"confidence": "Confidence", "count": "Leads"},
        )
        fig_conf.update_layout(
            paper_bgcolor="#1e293b", plot_bgcolor="#1e293b",
            font_color="#f1f5f9", margin=dict(t=20, b=20),
            legend=dict(bgcolor="#1e293b"),
        )
        st.plotly_chart(fig_conf, use_container_width=True)


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: LEADS
# ══════════════════════════════════════════════════════════════════════════════
elif page == "Leads":
    df = load_all_leads()

    st.title("Leads")

    if df.empty:
        st.info("No leads found. Run the pipeline first.")
        st.stop()

    # ── Filters ────────────────────────────────────────────────────────────────
    with st.expander("Filters", expanded=True):
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            decision_filter = st.multiselect(
                "Decision",
                ["qualified", "review", "disqualified"],
                default=["qualified", "review", "disqualified"],
            )
        with fc2:
            score_range = st.slider("Score range", 0, 100, (0, 100))
        with fc3:
            search = st.text_input("Search name / company", "")

    filtered = df[df["decision"].isin(decision_filter)]
    filtered = filtered[
        (filtered["score"] >= score_range[0]) & (filtered["score"] <= score_range[1])
    ]
    if search:
        mask = (
            filtered["person_name"].str.contains(search, case=False, na=False)
            | filtered["company_name"].str.contains(search, case=False, na=False)
        )
        filtered = filtered[mask]

    st.markdown(f"**{len(filtered)}** leads matching filters")

    # ── Table ──────────────────────────────────────────────────────────────────
    if filtered.empty:
        st.info("No leads match the current filters.")
        st.stop()

    # Render as styled HTML table
    table_rows = ""
    for _, row in filtered.sort_values("score", ascending=False).iterrows():
        color = score_color(row["score"])
        b = badge(row["decision"])
        table_rows += f"""
        <tr style="cursor:pointer;" onclick="">
            <td style="padding:10px 8px;">{row.get('person_name','—')}</td>
            <td style="padding:10px 8px;color:{COLORS['muted']};font-size:13px;">{row.get('title','—')}</td>
            <td style="padding:10px 8px;">{row.get('company_name','—')}</td>
            <td style="padding:10px 8px;text-align:center;">{b}</td>
            <td style="padding:10px 8px;text-align:center;">
                <span style="font-size:18px;font-weight:700;color:{color};">{int(row['score'])}</span>
            </td>
            <td style="padding:10px 8px;color:{COLORS['muted']};font-size:13px;">{row.get('confidence','—')}</td>
        </tr>
        """

    st.markdown(f"""
    <table style="width:100%;border-collapse:collapse;background:#1e293b;border-radius:12px;overflow:hidden;">
        <thead>
            <tr style="background:#0f172a;color:{COLORS['muted']};font-size:12px;text-transform:uppercase;letter-spacing:0.5px;">
                <th style="padding:12px 8px;text-align:left;">Name</th>
                <th style="padding:12px 8px;text-align:left;">Title</th>
                <th style="padding:12px 8px;text-align:left;">Company</th>
                <th style="padding:12px 8px;text-align:center;">Decision</th>
                <th style="padding:12px 8px;text-align:center;">Score</th>
                <th style="padding:12px 8px;text-align:left;">Confidence</th>
            </tr>
        </thead>
        <tbody>{table_rows}</tbody>
    </table>
    """, unsafe_allow_html=True)

    st.markdown("---")

    # ── Lead detail panel ──────────────────────────────────────────────────────
    st.subheader("Lead Detail")
    sorted_leads = filtered.sort_values("score", ascending=False)
    lead_options = [
        f"{row['person_name']} — {row['company_name']} ({int(row['score'])})"
        for _, row in sorted_leads.iterrows()
    ]
    lead_ids = sorted_leads["lead_id"].tolist()

    if lead_options:
        selected_label = st.selectbox("Select a lead to inspect", lead_options)
        idx = lead_options.index(selected_label)
        selected_id = lead_ids[idx]
        row = sorted_leads.iloc[idx]

        evidence = load_artifact(selected_id, "evidence")
        decision = load_artifact(selected_id, "decision")

        color = score_color(row["score"])

        dc1, dc2 = st.columns([2, 3])

        with dc1:
            st.markdown(f"### {row['person_name']}")
            st.markdown(f"**{row.get('title', '—')}** at **{row.get('company_name', '—')}**")
            st.markdown(badge(row["decision"]), unsafe_allow_html=True)
            st.markdown(f"<br><span style='font-size:48px;font-weight:800;color:{color};'>{int(row['score'])}</span><span style='color:{COLORS['muted']};'>/100</span>", unsafe_allow_html=True)
            st.markdown(f"Confidence: **{row.get('confidence', '—')}**")

            if decision:
                st.markdown("#### Score Breakdown")
                dims = [
                    ("Segment Fit", "segment_fit", 30),
                    ("Buyer Fit", "buyer_fit", 25),
                    ("Pain Fit", "pain_fit", 20),
                    ("Workflow Fit", "workflow_fit", 15),
                    ("Commercial Fit", "commercial_fit", 10),
                ]
                for label, key, max_val in dims:
                    val = decision.get(key, 0)
                    pct = val / max_val * 100
                    bar_color = score_color(pct)
                    st.markdown(
                        f"<div style='margin-bottom:8px;'>"
                        f"<div style='display:flex;justify-content:space-between;margin-bottom:4px;'>"
                        f"<span style='font-size:13px;color:{COLORS['text']};'>{label}</span>"
                        f"<span style='font-size:13px;font-weight:600;color:{bar_color};'>{val}/{max_val}</span>"
                        f"</div>"
                        + score_bar(val, max_val, bar_color) +
                        f"</div>",
                        unsafe_allow_html=True,
                    )

        with dc2:
            tab1, tab2, tab3 = st.tabs(["Reasoning", "Evidence", "Raw JSON"])

            with tab1:
                if decision:
                    st.markdown("**Summary**")
                    st.markdown(decision.get("reasoning_summary", "—"))

                    if decision.get("qualification_reasons"):
                        st.markdown("**Why qualified:**")
                        for r in decision["qualification_reasons"]:
                            st.markdown(f"- ✅ {r}")

                    if decision.get("disqualification_reasons"):
                        st.markdown("**Why disqualified:**")
                        for r in decision["disqualification_reasons"]:
                            st.markdown(f"- ❌ {r}")

                    if decision.get("suggested_use_case"):
                        st.markdown("**Suggested use case:**")
                        st.info(decision["suggested_use_case"])

            with tab2:
                if evidence:
                    st.markdown(f"**Signal strength:** `{evidence.get('signal_strength', '—')}`")
                    st.markdown(f"**Segment:** `{evidence.get('segment', '—')}`")

                    if evidence.get("observed_evidence"):
                        st.markdown("**Observed facts:**")
                        for e in evidence["observed_evidence"]:
                            st.markdown(f"- {e}")

                    if evidence.get("inferred_evidence"):
                        st.markdown("**Inferences:**")
                        for e in evidence["inferred_evidence"]:
                            st.markdown(f"- _{e}_")

                    if evidence.get("unknowns"):
                        st.markdown("**Unknowns:**")
                        for u in evidence["unknowns"]:
                            st.markdown(f"- ❓ {u}")

                    if evidence.get("hard_disqualifiers"):
                        st.markdown("**Hard disqualifiers:**")
                        for d in evidence["hard_disqualifiers"]:
                            st.markdown(f"- 🚫 {d}")
                else:
                    st.info("No evidence artifact found.")

            with tab3:
                col_j1, col_j2 = st.columns(2)
                with col_j1:
                    st.markdown("**Evidence (Stage 1)**")
                    st.json(evidence or {})
                with col_j2:
                    st.markdown("**Decision (Stage 2)**")
                    st.json(decision or {})


# ══════════════════════════════════════════════════════════════════════════════
# PAGE: RUN PIPELINE
# ══════════════════════════════════════════════════════════════════════════════
elif page == "Run Pipeline":
    st.title("Run Pipeline")

    col_left, col_right = st.columns([2, 1])

    with col_left:
        st.subheader("Upload a leads CSV or JSON")
        uploaded = st.file_uploader(
            "Drag and drop a Trigify CSV export or normalized JSON file",
            type=["csv", "json"],
            help="Upload a Trigify CSV export or a JSON array of normalized lead objects.",
        )

        if uploaded:
            try:
                preview_df, file_kind = preview_uploaded_file(uploaded)
                uploaded.seek(0)
                st.success(f"Loaded: **{uploaded.name}** ({uploaded.size / 1024:.1f} KB)")
                st.markdown(f"**{len(preview_df)} rows**, {len(preview_df.columns)} columns · `{file_kind}`")
                st.dataframe(preview_df.head(5), use_container_width=True)
            except Exception as exc:
                uploaded.seek(0)
                st.error(str(exc))
                uploaded = None

    with col_right:
        st.subheader("Settings")
        concurrency = st.slider("Concurrency", 1, 20, 10, help="Parallel leads processed at once")
        verbose = st.checkbox("Verbose logging", value=True)
        output_dir = st.text_input("Output directory", value=str(OUTPUT_DIR))

    st.markdown("---")

    run_col, _ = st.columns([1, 3])
    with run_col:
        run_btn = st.button(
            "▶  Run Pipeline",
            type="primary",
            disabled=uploaded is None,
            use_container_width=True,
        )

    if run_btn and uploaded:
        suffix = get_upload_suffix(uploaded.name)
        # Save uploaded file to a temp location
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, mode="wb") as tmp:
            tmp.write(uploaded.read())
            tmp_path = tmp.name

        cmd = [
            sys.executable, str(ROOT / "main.py"),
            "--input", tmp_path,
            "--output-dir", output_dir,
            "--concurrency", str(concurrency),
        ]
        if verbose:
            cmd.append("--verbose")

        st.markdown("### Pipeline output")
        log_box = st.empty()
        lines = []

        with st.spinner("Running..."):
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(ROOT),
            )
            for line in proc.stdout:
                lines.append(line.rstrip())
                log_box.code("\n".join(lines[-60:]), language="text")
            proc.wait()

        os.unlink(tmp_path)

        if proc.returncode == 0:
            st.success("Pipeline completed successfully!")
            st.cache_data.clear()
            st.balloons()
        else:
            st.error(f"Pipeline exited with code {proc.returncode}")

        # ── Results summary ────────────────────────────────────────────────────
        df_new = load_all_leads()
        if not df_new.empty:
            st.markdown("### Results")
            r1, r2, r3 = st.columns(3)
            r1.metric("Qualified", len(df_new[df_new["decision"] == "qualified"]))
            r2.metric("Review", len(df_new[df_new["decision"] == "review"]))
            r3.metric("Disqualified", len(df_new[df_new["decision"] == "disqualified"]))

            st.markdown("**Download results**")
            dl1, dl2, dl3 = st.columns(3)
            for col, path, label in [
                (dl1, QUALIFIED_CSV, "qualified_leads.csv"),
                (dl2, REVIEW_CSV, "review_leads.csv"),
                (dl3, DISQUALIFIED_CSV, "disqualified_leads.csv"),
            ]:
                if path.exists():
                    with col:
                        st.download_button(
                            label=f"⬇ {label}",
                            data=path.read_bytes(),
                            file_name=label,
                            mime="text/csv",
                        )
