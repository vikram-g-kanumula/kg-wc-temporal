"""
graph_queries.py
================
Centralized Cypher query library for the Streamlit app.
"""

def get_portfolio_kpis(driver, filters=None):
    """Returns top-level KPIs for Page 1."""
    query = """
    MATCH (c:Claim)
    WITH count(c) AS total_claims,
         sum(CASE WHEN c.status = 'OPEN' THEN 1 ELSE 0 END) AS open_claims,
         sum(c.current_reserve) AS total_reserves
         
    OPTIONAL MATCH (c2:Claim {status: 'OPEN'})
    WHERE c2.date_of_injury < datetime() - duration('P180D')
    WITH total_claims, open_claims, total_reserves, count(c2) AS stale_claims
    
    OPTIONAL MATCH (c3:Claim)-[:CURRENT_EVENT]->(e:ClaimEvent)
    WHERE e.stage IN ['IME_ORD', 'IME_COMP', 'QME_ORD', 'QME_COMP', 'MEDIATION', 'LITIGATION']
    WITH total_claims, open_claims, total_reserves, stale_claims, count(DISTINCT c3) AS dispute_claims
    
    RETURN total_claims, open_claims, total_reserves, stale_claims, dispute_claims
    """
    with driver.session() as s:
        result = s.run(query)
        return result.single().data() if result.peek() else {}


def get_claim_grid(driver, status_filter="All", limit=50):
    """Returns list of claims for the grid."""
    
    match_clause = "MATCH (c:Claim)"
    if status_filter != "All":
        match_clause += f" WHERE c.status = '{status_filter}'"
        
    query = f"""
    {match_clause}
    OPTIONAL MATCH (c)-[:CURRENT_EVENT]->(e:ClaimEvent)<-[:OF_STAGE]-(s:Stage)
    OPTIONAL MATCH (c)-[:INVOLVES_BODYPART]->(bp:BodyPart)
    OPTIONAL MATCH (c)-[:IN_JURISDICTION]->(j:Jurisdiction)
    OPTIONAL MATCH (c)-[:HAS_CLAIMANT]->(p:Person)
    OPTIONAL MATCH (c)-[:REPRESENTED_BY]->(a:Attorney)
    
    RETURN 
        c.claim_id AS claim_id,
        c.status AS status,
        bp.label AS body_part,
        j.code AS jurisdiction,
        s.label AS current_stage,
        c.total_paid AS total_paid,
        c.current_reserve AS current_reserve,
        CASE WHEN a IS NOT NULL THEN 'Yes' ELSE 'No' END AS attorney,
        p.name AS claimant_name,
        c.is_hero AS is_hero,
        c.demo_notes AS demo_notes
    ORDER BY c.is_hero DESC, c.claim_id ASC
    LIMIT $limit
    """
    with driver.session() as s:
        return [record.data() for record in s.run(query, limit=limit)]


def get_hero_claims(driver):
    """Returns list of hero claims."""
    query = """
    MATCH (c:Claim {is_hero: true})
    RETURN c.claim_id AS claim_id, c.demo_notes AS demo_notes, c.status AS status
    ORDER BY c.claim_id
    """
    with driver.session() as s:
        return [r.data() for r in s.run(query)]


def get_claim_trajectory(driver, claim_id):
    """Returns the ordered event chain for a claim, including durations and stages."""
    query = """
    MATCH path = (c:Claim {claim_id: $cid})-[:FIRST_EVENT]->(fe:ClaimEvent)-[:NEXT*0..]->(last:ClaimEvent)
    WHERE NOT (last)-[:NEXT]->()
    WITH nodes(path) AS events
    
    UNWIND events AS e
    MATCH (e)-[:OF_STAGE]->(s:Stage)
    RETURN 
        e.event_id AS event_id,
        s.code AS stage_code,
        s.label AS stage_label,
        s.phase AS phase,
        e.duration_days AS duration,
        e.occurred_at AS date
    """
    with driver.session() as s:
        return [r.data() for r in s.run(query, cid=claim_id)]


def get_reserve_history(driver, claim_id):
    """Returns chronological reserve snapshots."""
    query = """
    MATCH path = (c:Claim {claim_id: $cid})-[:FIRST_EVENT]->(fe:ClaimEvent)-[:NEXT*0..]->(last:ClaimEvent)
    WHERE NOT (last)-[:NEXT]->()
    WITH nodes(path) AS events
    
    UNWIND events AS e
    MATCH (e)-[:RESET_RESERVE]->(rs:ReserveSnapshot)
    MATCH (e)-[:OF_STAGE]->(s:Stage)
    RETURN rs.date AS date, rs.amount AS amount, s.label AS trigger_stage
    ORDER BY date ASC
    """
    with driver.session() as s:
        return [r.data() for r in s.run(query, cid=claim_id)]


def get_entity_neighborhood(driver, claim_id):
    """Returns edges for the local graph neighborhood to feed streamlit-agraph."""
    query = """
    MATCH (c:Claim {claim_id: $cid})
    
    OPTIONAL MATCH (c)-[:HAS_CLAIMANT]->(p:Person)
    OPTIONAL MATCH (c)-[:ASSIGNED_TO]->(adj:Person)
    OPTIONAL MATCH (c)-[:OCCURRED_AT_EMPLOYER]->(emp:Employer)
    OPTIONAL MATCH (c)-[:REPRESENTED_BY]->(att:Attorney)
    
    // Providers linked from events
    OPTIONAL MATCH (c)-[:FIRST_EVENT]->()-[:NEXT*0..]->(e:ClaimEvent)-[:TREATED_BY]->(prov:Provider)
    
    RETURN 
        p.name AS claimant,
        adj.name AS adjuster,
        emp.name AS employer,
        att.name AS attorney,
        collect(DISTINCT prov.name) AS providers
    """
    with driver.session() as s:
        res = s.run(query, cid=claim_id)
        return res.single().data() if res.peek() else {}


def get_similar_claims_dynamic(driver, claim_id, k, weights):
    """
    Queries the pre-computed :SIMILAR_TO edges and re-weights the components
    on the fly using the supplied slider weights.
    weights = {'demo': 0.25, 'shape': 0.35, 'pace': 0.20, 'graph': 0.20}
    """
    # Normalize weights so they sum to 1.0 just in case
    total = sum(weights.values())
    if total == 0:
        w_demo, w_shape, w_pace, w_graph = 0.25, 0.25, 0.25, 0.25
    else:
        w_demo  = weights.get('demo', 0) / total
        w_shape = weights.get('shape', 0) / total
        w_pace  = weights.get('pace', 0) / total
        w_graph = weights.get('graph', 0) / total

    query = """
    MATCH (src:Claim {claim_id: $cid})-[r:SIMILAR_TO]->(tgt:Claim)
    
    // Dynamic composite calculation
    WITH tgt, r,
         ($w_demo * r.demo_score) + 
         ($w_shape * r.shape_score) + 
         ($w_pace * r.pace_score) + 
         ($w_graph * r.graph_score) AS composite_score
         
    // Fetch tgt details
    OPTIONAL MATCH (tgt)-[:INVOLVES_BODYPART]->(bp:BodyPart)
    OPTIONAL MATCH (tgt)-[:IN_JURISDICTION]->(j:Jurisdiction)
    OPTIONAL MATCH (tgt)-[:CURRENT_EVENT]->(e:ClaimEvent)<-[:OF_STAGE]-(s:Stage)
    
    RETURN 
        tgt.claim_id AS target_id,
        tgt.status AS status,
        bp.label AS body_part,
        j.code AS jurisdiction,
        s.label AS current_stage,
        tgt.total_paid AS total_paid,
        tgt.current_reserve AS current_reserve,
        tgt.demo_notes AS demo_notes,
        tgt.is_hero AS is_hero,
        composite_score,
        r.demo_score AS demo_score,
        r.shape_score AS shape_score,
        r.pace_score AS pace_score,
        r.graph_score AS graph_score
    ORDER BY composite_score DESC
    LIMIT $k
    """
    
    with driver.session() as s:
        return [r.data() for r in s.run(query, 
                                        cid=claim_id, k=k,
                                        w_demo=w_demo, w_shape=w_shape, 
                                        w_pace=w_pace, w_graph=w_graph)]

