import streamlit as st
import pandas as pd
import plotly.express as px
from app import get_driver
from graph_queries import get_hero_claims, get_similar_claims_dynamic, get_claim_trajectory

st.set_page_config(page_title="Similarity Workbench", layout="wide")
driver = get_driver()

st.title("Similarity Workbench")
st.markdown("Discover functionally similar claims using the decomposable temporal graph distance metric.")

# Left / Right split
col_cfg, col_res = st.columns([1, 3])

with col_cfg:
    st.header("Configuration")
    
    # Hero selection
    heroes = get_hero_claims(driver)
    hero_options = ["---"] + [f"{h['claim_id']} ({h['status']})" for h in heroes]
    selected_hero = st.selectbox("Quick Select (Hero Scenarios)", hero_options)
    
    if selected_hero != "---":
        cid = selected_hero.split(" ")[0]
        h_data = next(h for h in heroes if h['claim_id'] == cid)
        st.info(f"**Demo Script:**\n{h_data['demo_notes']}")
        anchor_id = cid
    else:
        anchor_id = st.text_input("Or enter Anchor Claim ID", value="CLM_A1_00001")
        
    st.divider()
    st.subheader("Similarity Weights")
    
    # Presets
    preset = st.radio("Weight Preset", ["Balanced", "Shape-led (Recommended)", "Demographic-led", "Custom"])
    
    if preset == "Balanced":
        w_demo, w_shape, w_pace, w_graph = 25, 25, 25, 25
    elif preset == "Shape-led (Recommended)":
        w_demo, w_shape, w_pace, w_graph = 20, 45, 15, 20
    elif preset == "Demographic-led":
        w_demo, w_shape, w_pace, w_graph = 50, 20, 10, 20
    else:
        w_demo = st.slider("Demographics", 0, 100, 25)
        w_shape = st.slider("Trajectory Shape", 0, 100, 35)
        w_pace = st.slider("Pacing", 0, 100, 20)
        w_graph = st.slider("Graph Neighborhood", 0, 100, 20)
        
    k = st.slider("Neighbors (k)", 1, 20, 5)
    
    weights = {"demo": w_demo, "shape": w_shape, "pace": w_pace, "graph": w_graph}

with col_res:
    st.header("Results")
    
    if anchor_id:
        with st.spinner("Re-weighting and querying Neo4j..."):
            neighbors = get_similar_claims_dynamic(driver, anchor_id, k, weights)
            
        if not neighbors:
            st.warning("No similarities found. Did you run the similarity engine in Admin?")
        else:
            df_n = pd.DataFrame(neighbors)
            # Format scores
            for col in ["composite_score", "demo_score", "shape_score", "pace_score", "graph_score"]:
                df_n[col] = (df_n[col] * 100).round(1)
                
            st.subheader("Top Similar Claims")
            
            st.dataframe(
                df_n[["target_id", "status", "composite_score", "demo_score", "shape_score", "pace_score", "graph_score", "total_paid", "current_reserve"]],
                use_container_width=True,
                column_config={
                    "composite_score": st.column_config.ProgressColumn("Match %", min_value=0, max_value=100, format="%.1f%%"),
                    "total_paid": st.column_config.NumberColumn(format="$%d"),
                    "current_reserve": st.column_config.NumberColumn(format="$%d")
                }
            )
            
            st.divider()
            st.subheader("Trajectory Alignment Comparison")
            
            compare_id = st.selectbox("Select neighbor to align with Anchor", df_n["target_id"].tolist())
            
            if compare_id:
                t_anchor = get_claim_trajectory(driver, anchor_id)
                t_compare = get_claim_trajectory(driver, compare_id)
                
                # We'll plot them on a shared synthetic timeline (Days from DOI)
                def make_gantt_df(traj, label):
                    rows = []
                    cum_days = 0
                    for e in traj:
                        rows.append({
                            "Claim": label,
                            "Stage": e["stage_label"],
                            "Phase": e["phase"],
                            "StartDay": cum_days,
                            "EndDay": cum_days + e["duration"],
                            "Duration": e["duration"]
                        })
                        cum_days += e["duration"]
                    return rows
                    
                df_anchor = pd.DataFrame(make_gantt_df(t_anchor, f"Anchor ({anchor_id})"))
                df_compare = pd.DataFrame(make_gantt_df(t_compare, f"Neighbor ({compare_id})"))
                
                df_combined = pd.concat([df_anchor, df_compare])
                
                fig = px.timeline(df_combined, x_start="StartDay", x_end="EndDay", y="Claim", color="Phase", hover_name="Stage")
                # Plotly px.timeline expects datetime. A common trick for relative days is treating them as ms from epoch or simple bar charts.
                # Since px.timeline requires datetime, let's just use px.bar with base
                fig2 = px.bar(df_combined, base="StartDay", x="Duration", y="Claim", color="Phase", hover_name="Stage", orientation='h')
                
                st.plotly_chart(fig2, use_container_width=True)

