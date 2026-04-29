"""
similarity_engine.py
====================
Computes the 4-component decomposable similarity score between claims:
1. Demographics (Weighted Jaccard over categorical features)
2. Trajectory Shape (Longest Common Subsequence over stage chains)
3. Pacing (Dynamic Time Warping over stage duration sequences)
4. Graph Context (Neighborhood overlap of providers, adjusters, attorneys)

Results are persisted as :SIMILAR_TO edges in Neo4j.
"""

import time
import os
import random
import numpy as np
from rapidfuzz import fuzz
from dtaidistance import dtw
from collections import defaultdict
from neo4j import GraphDatabase

try:
    import streamlit as st
    HAS_ST = True
except ImportError:
    HAS_ST = False


class SimilarityEngine:
    def __init__(self, uri=None, user=None, password=None):
        if uri and user and password:
            pass
        elif HAS_ST and hasattr(st, "secrets"):
            uri      = st.secrets["neo4j"]["uri"]
            user     = st.secrets["neo4j"]["user"]
            password = st.secrets["neo4j"]["password"]
        else:
            uri      = os.getenv("NEO4J_URI",      "neo4j://localhost:7687")
            user     = os.getenv("NEO4J_USERNAME",  "neo4j")
            password = os.getenv("NEO4J_PASSWORD",  "password")

        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.claims_data = {}  # In-memory cache for fast pairwise computation

    def close(self):
        self.driver.close()

    def load_data(self, log=print):
        """Loads all claims, event chains, and 1-hop neighborhoods into memory."""
        log("Loading data from Neo4j for similarity computation...")
        
        query = """
        MATCH (c:Claim)
        // 1. Demographics
        OPTIONAL MATCH (c)-[:HAS_CLAIMANT]->(p:Person)
        OPTIONAL MATCH (c)-[:OCCURRED_AT_EMPLOYER]->(e:Employer)
        OPTIONAL MATCH (c)-[:INVOLVES_BODYPART]->(bp:BodyPart)
        OPTIONAL MATCH (c)-[:CAUSED_BY]->(ic:InjuryCause)
        
        // 2 & 3. Event Chain
        OPTIONAL MATCH path = (c)-[:FIRST_EVENT]->(fe:ClaimEvent)-[:NEXT*0..]->(last:ClaimEvent)
        WHERE NOT (last)-[:NEXT]->()
        WITH c, p, e, bp, ic, nodes(path) AS longest_chain
        
        // 4. Graph Neighborhood
        OPTIONAL MATCH (c)-[:ASSIGNED_TO]->(adj:Person)
        OPTIONAL MATCH (c)-[:REPRESENTED_BY]->(att:Attorney)
        OPTIONAL MATCH (c)-[:FIRST_EVENT]->()-[:NEXT*0..]->(cev:ClaimEvent)-[:TREATED_BY]->(prov:Provider)
        
        RETURN 
            c.claim_id AS claim_id,
            c.status AS status,
            {
                age_band: p.age_band,
                wage_band: p.wage_band,
                industry: e.industry,
                body_part: bp.code,
                cause: ic.code
            } AS demographics,
            [x IN longest_chain | x.stage] AS stage_chain,
            [x IN longest_chain | toFloat(x.duration_days)] AS durations,
            {
                adjuster: adj.person_id,
                attorney: att.attorney_id,
                providers: collect(DISTINCT prov.provider_id)
            } AS network
        """
        
        with self.driver.session() as session:
            result = session.run(query)
            for record in result:
                cid = record["claim_id"]
                # Default empty lists for missing chains
                chain = record["stage_chain"] or []
                durs = record["durations"] or []
                
                self.claims_data[cid] = {
                    "status": record["status"],
                    "demographics": record["demographics"],
                    "stage_chain": "".join([s.ljust(10) for s in chain]), # RapidFuzz works on strings, pad to 10 chars
                    "durations": np.array(durs, dtype=np.double),
                    "network": record["network"]
                }
                
        log(f"Loaded {len(self.claims_data)} claims into memory.")

    def _compute_demographic_sim(self, d1, d2):
        """Jaccard similarity over categorical features, manually weighted."""
        score = 0.0
        total_weight = 5.0
        
        if d1["body_part"] == d2["body_part"]: score += 1.5
        if d1["cause"] == d2["cause"]: score += 1.0
        if d1["industry"] == d2["industry"]: score += 1.0
        if d1["age_band"] == d2["age_band"]: score += 0.5
        if d1["wage_band"] == d2["wage_band"]: score += 1.0
        
        return score / total_weight

    def _compute_shape_sim(self, c1, c2, s1, s2):
        """Longest Common Subsequence ratio.
        If comparing OPEN to CLOSED (prefix matching), truncate the closed claim.
        """
        if not c1 or not c2: return 0.0
        
        str1, str2 = c1, c2
        if s1 == "OPEN" and s2 == "CLOSED":
            # truncate str2 to length of str1
            str2 = str2[:len(str1)]
        elif s2 == "OPEN" and s1 == "CLOSED":
            str1 = str1[:len(str2)]
            
        return fuzz.ratio(str1, str2) / 100.0

    def _compute_pacing_sim(self, d1, d2, s1, s2):
        """Dynamic Time Warping over duration vectors."""
        if len(d1) == 0 or len(d2) == 0: return 0.0
        
        v1, v2 = d1, d2
        if s1 == "OPEN" and s2 == "CLOSED":
            v2 = v2[:len(v1)]
        elif s2 == "OPEN" and s1 == "CLOSED":
            v1 = v1[:len(v2)]
            
        if len(v1) == 0 or len(v2) == 0: return 0.0
            
        distance = dtw.distance_fast(v1, v2)
        # Normalize distance to [0,1] similarity
        # 100 days of DTW distance = 0.5 similarity
        return 1.0 / (1.0 + (distance / 100.0))

    def _compute_graph_sim(self, n1, n2):
        """Entity neighborhood overlap."""
        score = 0.0
        max_score = 3.0
        
        if n1["adjuster"] and n1["adjuster"] == n2["adjuster"]: score += 1.0
        if n1["attorney"] and n1["attorney"] == n2["attorney"]: score += 1.0
        
        p1 = set(n1["providers"])
        p2 = set(n2["providers"])
        if p1 and p2:
            jaccard = len(p1.intersection(p2)) / len(p1.union(p2))
            score += jaccard * 1.0
            
        return score / max_score

    def compute_all_pairs(self, log=print):
        if not self.claims_data:
            self.load_data(log)
            
        cids = list(self.claims_data.keys())
        n = len(cids)
        
        log(f"Computing pairs for {n} claims ({n*(n-1)//2} comparisons)...")
        
        # We will keep top 10 for each claim
        top_k = {cid: [] for cid in cids}
        
        start_time = time.time()
        
        for i in range(n):
            cid_a = cids[i]
            data_a = self.claims_data[cid_a]
            
            # Temporary list for this claim's neighbors
            neighbors = []
            
            for j in range(n):
                if i == j: continue
                
                cid_b = cids[j]
                data_b = self.claims_data[cid_b]
                
                # Compute components
                demo_sim = self._compute_demographic_sim(data_a["demographics"], data_b["demographics"])
                shape_sim = self._compute_shape_sim(data_a["stage_chain"], data_b["stage_chain"], data_a["status"], data_b["status"])
                pace_sim = self._compute_pacing_sim(data_a["durations"], data_b["durations"], data_a["status"], data_b["status"])
                graph_sim = self._compute_graph_sim(data_a["network"], data_b["network"])
                
                # Default baseline weights: Demo=0.25, Shape=0.35, Pace=0.20, Graph=0.20
                composite = (0.25 * demo_sim) + (0.35 * shape_sim) + (0.20 * pace_sim) + (0.20 * graph_sim)
                
                neighbors.append({
                    "target": cid_b,
                    "score": composite,
                    "demo_score": demo_sim,
                    "shape_score": shape_sim,
                    "pace_score": pace_sim,
                    "graph_score": graph_sim
                })
                
            # Sort and keep top 15
            neighbors.sort(key=lambda x: x["score"], reverse=True)
            top_k[cid_a] = neighbors[:15]
            
            if (i+1) % 50 == 0:
                log(f"  ... processed {i+1}/{n} claims")
                
        elapsed = time.time() - start_time
        log(f"Pairwise computation finished in {elapsed:.2f} seconds.")
        
        self._write_edges(top_k, log)

    def _write_edges(self, top_k, log):
        log("Writing :SIMILAR_TO edges back to Neo4j...")
        
        # Clear existing
        with self.driver.session() as s:
            s.run("MATCH ()-[r:SIMILAR_TO]->() DELETE r")
            
        # Batch write
        edges = []
        for src, neighbors in top_k.items():
            for n in neighbors:
                edges.append({
                    "src": src,
                    "tgt": n["target"],
                    "score": n["score"],
                    "demo": n["demo_score"],
                    "shape": n["shape_score"],
                    "pace": n["pace_score"],
                    "graph": n["graph_score"]
                })
                
        query = """
        UNWIND $edges AS edge
        MATCH (a:Claim {claim_id: edge.src})
        MATCH (b:Claim {claim_id: edge.tgt})
        MERGE (a)-[r:SIMILAR_TO]->(b)
        SET r.score = edge.score,
            r.demo_score = edge.demo,
            r.shape_score = edge.shape,
            r.pace_score = edge.pace,
            r.graph_score = edge.graph,
            r.computed_at = datetime()
        """
        
        with self.driver.session() as s:
            # Chunking to avoid massive memory use in AuraDB Free
            chunk_size = 1000
            for i in range(0, len(edges), chunk_size):
                chunk = edges[i:i+chunk_size]
                s.run(query, edges=chunk)
                
        log(f"Successfully wrote {len(edges)} :SIMILAR_TO edges.")

if __name__ == "__main__":
    engine = SimilarityEngine()
    engine.compute_all_pairs()
    engine.close()
