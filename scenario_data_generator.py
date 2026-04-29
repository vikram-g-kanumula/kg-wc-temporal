"""
scenario_data_generator.py  —  Phase 1 of 2
Catalog nodes + entity pools only.
Run generate_all() from the Streamlit Admin page.
"""

import random
import time
from datetime import datetime, timedelta
from neo4j import GraphDatabase, exceptions

try:
    import streamlit as st
    HAS_ST = True
except ImportError:
    HAS_ST = False
    import os

from cohort_definitions import (
    STAGE_CATALOG, BODY_PARTS, INJURY_CAUSES, EMPLOYER_GROUPS,
    SUB_TRAJECTORIES, COHORT_COUNTS, HERO_CLAIMS,
    RESERVE_TRIGGERS, DOCUMENT_TRIGGERS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _rand_duration(mean, std, lo, hi):
    """Gaussian-sampled duration, clamped to [lo, hi]."""
    v = int(random.gauss(mean, std))
    return _clamp(v, lo, hi)


FIRST_NAMES = [
    "James","Mary","John","Patricia","Robert","Jennifer","Michael","Linda",
    "David","Sarah","William","Elizabeth","Richard","Barbara","Joseph","Susan",
    "Thomas","Jessica","Charles","Karen","Kevin","Ashley","Brian","Kimberly",
    "Ryan","Amanda","Jason","Emily","Anthony","Melissa","Mark","Stephanie",
    "Donald","Rebecca","George","Laura","Kenneth","Cynthia","Steven","Amy",
]
LAST_NAMES = [
    "Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis",
    "Rodriguez","Martinez","Anderson","Taylor","Thomas","Hernandez","Moore",
    "Martin","Jackson","Thompson","White","Lopez","Lee","Gonzalez","Harris",
    "Clark","Lewis","Robinson","Walker","Perez","Hall","Young","Allen",
    "Sanchez","Wright","King","Scott","Green","Baker","Adams","Nelson","Carter",
]

CA_CITIES = [
    ("Los Angeles","CA","90001"),("San Diego","CA","92101"),
    ("San Jose","CA","95101"),("Fresno","CA","93701"),
    ("Sacramento","CA","95814"),("Long Beach","CA","90802"),
    ("Oakland","CA","94601"),("Bakersfield","CA","93301"),
    ("Riverside","CA","92501"),("Anaheim","CA","92801"),
]


def _rand_name():
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def _rand_city():
    return random.choice(CA_CITIES)


def _rand_npi():
    return str(random.randint(1_000_000_000, 9_999_999_999))


def _rand_bar():
    return f"CA-{random.randint(2000,2023)}-{random.randint(10000,99999)}"


def _rand_phone():
    return f"({random.randint(200,999)}) {random.randint(200,999)}-{random.randint(1000,9999)}"


def _rand_date(days_ago_start=730, days_ago_end=7):
    days = random.randint(days_ago_end, days_ago_start)
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Generator class
# ---------------------------------------------------------------------------

class WCDataGenerator:
    """
    Generates Workers' Compensation temporal claims data in Neo4j.
    Phase 1: catalog + entity pools
    Phase 2: cohort claims + heroes
    Phase 3: similarity edges (delegated to similarity_engine.py)
    """

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

        self.driver = GraphDatabase.driver(
            uri, auth=(user, password), max_connection_lifetime=200
        )
        self.driver.verify_connectivity()

        # pools filled during phase 1 ─ used during phase 2
        self.adjuster_ids:  list[str] = []
        self.provider_ids:  dict      = {}   # specialty -> [ids]
        self.attorney_ids:  list[str] = []
        self.employer_ids:  dict      = {}   # group -> [ids]
        self._counters:     dict      = {}

    def close(self):
        self.driver.close()

    # ── low-level helpers ────────────────────────────────────────────────────

    def _run(self, query, **params):
        max_retry = 3
        for attempt in range(max_retry):
            try:
                with self.driver.session() as s:
                    s.run(query, **params)
                return
            except exceptions.ServiceUnavailable:
                if attempt < max_retry - 1:
                    time.sleep(1)
                else:
                    raise
            except Exception as e:
                raise RuntimeError(f"Query failed: {e}\n{query}") from e

    def _query(self, query, **params):
        with self.driver.session() as s:
            return list(s.run(query, **params))

    def _next_id(self, prefix):
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}_{self._counters[prefix]:05d}"

    # ── schema / reset ───────────────────────────────────────────────────────

    def apply_schema(self):
        """Apply uniqueness constraints and indexes from schema.cypher."""
        stmts = [
            "CREATE CONSTRAINT claim_id   IF NOT EXISTS FOR (c:Claim)      REQUIRE c.claim_id IS UNIQUE",
            "CREATE CONSTRAINT person_id  IF NOT EXISTS FOR (p:Person)     REQUIRE p.person_id IS UNIQUE",
            "CREATE CONSTRAINT prov_id    IF NOT EXISTS FOR (p:Provider)   REQUIRE p.provider_id IS UNIQUE",
            "CREATE CONSTRAINT emp_id     IF NOT EXISTS FOR (e:Employer)   REQUIRE e.employer_id IS UNIQUE",
            "CREATE CONSTRAINT att_id     IF NOT EXISTS FOR (a:Attorney)   REQUIRE a.attorney_id IS UNIQUE",
            "CREATE CONSTRAINT pol_id     IF NOT EXISTS FOR (p:Policy)     REQUIRE p.policy_id IS UNIQUE",
            "CREATE CONSTRAINT ins_id     IF NOT EXISTS FOR (i:Insurer)    REQUIRE i.insurer_id IS UNIQUE",
            "CREATE CONSTRAINT stage_code IF NOT EXISTS FOR (s:Stage)      REQUIRE s.code IS UNIQUE",
            "CREATE CONSTRAINT bp_code    IF NOT EXISTS FOR (b:BodyPart)   REQUIRE b.code IS UNIQUE",
            "CREATE CONSTRAINT ic_code    IF NOT EXISTS FOR (i:InjuryCause)REQUIRE i.code IS UNIQUE",
            "CREATE CONSTRAINT ev_id      IF NOT EXISTS FOR (e:ClaimEvent) REQUIRE e.event_id IS UNIQUE",
            "CREATE INDEX claim_status    IF NOT EXISTS FOR (c:Claim)      ON (c.status)",
            "CREATE INDEX claim_doi       IF NOT EXISTS FOR (c:Claim)      ON (c.date_of_injury)",
            "CREATE INDEX ev_stage        IF NOT EXISTS FOR (e:ClaimEvent) ON (e.stage)",
        ]
        with self.driver.session() as s:
            for stmt in stmts:
                try:
                    s.run(stmt)
                except Exception:
                    pass

    def clear_database(self):
        self._run("MATCH (n) DETACH DELETE n")

    # ── Phase 1 ──────────────────────────────────────────────────────────────

    def create_catalog(self):
        """Stage, BodyPart, InjuryCause, Jurisdiction catalog nodes."""
        # Stages
        for code, info in STAGE_CATALOG.items():
            d = info["duration"]
            self._run(
                """MERGE (s:Stage {code:$code})
                   SET s.label=$label, s.phase=$phase, s.mtc=$mtc,
                       s.dur_mean=$mean, s.dur_std=$std,
                       s.dur_min=$dmin, s.dur_max=$dmax""",
                code=code, label=info["label"], phase=info["phase"],
                mtc=info.get("mtc"), mean=d[0], std=d[1], dmin=d[2], dmax=d[3],
            )
        # Body parts
        for code, info in BODY_PARTS.items():
            self._run(
                "MERGE (b:BodyPart {code:$code}) SET b.label=$label, b.region=$region",
                code=code, label=info["label"], region=info["region"],
            )
        # Injury causes
        for code, info in INJURY_CAUSES.items():
            self._run(
                "MERGE (i:InjuryCause {code:$code}) SET i.label=$label",
                code=code, label=info["label"],
            )
        # Single jurisdiction (California)
        self._run(
            "MERGE (j:Jurisdiction {code:'CA'}) SET j.name='California', j.ttd_weeks=104",
        )

    def create_insurer(self):
        self._run(
            """MERGE (i:Insurer {insurer_id:'INS_001'})
               SET i.name='Pacific Claims Mutual', i.state='CA'"""
        )

    def create_adjusters(self, count=12):
        """10 adjusters with varying experience levels."""
        exp_levels = ["Junior","Mid-level","Senior","Senior","Senior",
                      "Senior","Mid-level","Mid-level","Junior","Junior",
                      "Senior","Mid-level"]
        for i in range(count):
            pid = f"ADJ_{i+1:03d}"
            self.adjuster_ids.append(pid)
            city, state, zip_ = _rand_city()
            self._run(
                """MERGE (p:Person {person_id:$pid})
                   SET p.name=$name, p.role='ADJUSTER',
                       p.experience=$exp, p.caseload=$caseload""",
                pid=pid, name=_rand_name(),
                exp=exp_levels[i % len(exp_levels)],
                caseload=random.randint(120, 280),
            )

    def create_providers(self):
        """30 providers across specialties used by the cohorts."""
        specs = [
            ("Orthopedics",      8, "Orthopedic Associates of CA"),
            ("Physical Therapy", 8, "CA Rehab & PT"),
            ("Pain Management",  5, "Pacific Pain Specialists"),
            ("Chiropractor",     4, "Spine & Wellness CA"),
            ("IME Specialist",   3, "Independent Medical Evaluators Inc"),
            ("Surgery Center",   2, "CA Surgical Partners"),
        ]
        for specialty, n, firm_prefix in specs:
            self.provider_ids[specialty] = []
            for i in range(n):
                pid = self._next_id(f"PROV_{specialty[:3].upper()}")
                self.provider_ids[specialty].append(pid)
                city, state, zip_ = _rand_city()
                # Give IME specialists a lower "quality" score to drive demo insight
                score = (round(random.uniform(0.55, 0.80), 2)
                         if specialty == "IME Specialist"
                         else round(random.uniform(0.72, 0.98), 2))
                self._run(
                    """MERGE (p:Provider {provider_id:$pid})
                       SET p.name=$name, p.specialty=$spec,
                           p.npi=$npi, p.city=$city, p.state='CA',
                           p.performance_score=$score""",
                    pid=pid,
                    name=f"{firm_prefix} — {_rand_city()[0]}",
                    spec=specialty, npi=_rand_npi(),
                    city=city, score=score,
                )

    def create_attorneys(self):
        """15 attorneys — plaintiff and defense side."""
        for i in range(10):   # plaintiff
            aid = f"ATT_PLF_{i+1:03d}"
            self.attorney_ids.append(aid)
            self._run(
                """MERGE (a:Attorney {attorney_id:$aid})
                   SET a.name=$name, a.side='Plaintiff',
                       a.bar_number=$bar, a.state='CA'""",
                aid=aid, name=f"Law Office of {_rand_name()}",
                bar=_rand_bar(),
            )
        for i in range(5):   # defense
            aid = f"ATT_DEF_{i+1:03d}"
            self.attorney_ids.append(aid)
            self._run(
                """MERGE (a:Attorney {attorney_id:$aid})
                   SET a.name=$name, a.side='Defense',
                       a.bar_number=$bar, a.state='CA'""",
                aid=aid, name=f"{_rand_name()} Defense Group",
                bar=_rand_bar(),
            )

    def create_employers(self):
        """50 employers spread across NAICS groups."""
        for group_code, info in EMPLOYER_GROUPS.items():
            self.employer_ids[group_code] = []
            n = 7 if group_code in ("WAREHOUSE","RETAIL","CONSTRUCTION") else 4
            for i in range(n):
                eid = self._next_id(f"EMP_{group_code[:3].upper()}")
                self.employer_ids[group_code].append(eid)
                city, state, zip_ = _rand_city()
                self._run(
                    """MERGE (e:Employer {employer_id:$eid})
                       SET e.name=$name, e.naics=$naics,
                           e.industry=$industry, e.city=$city, e.state='CA',
                           e.rtw_program=$rtw""",
                    eid=eid,
                    name=f"{city} {info['label']} Inc.",
                    naics=info["naics"], industry=info["label"],
                    city=city,
                    rtw=random.choice([True, True, False]),
                )

    def run_phase1(self, log=print):
        log("▶ Phase 1: Catalog & entity pools")
        self.apply_schema();    log("  ✓ Schema constraints applied")
        self.create_catalog();  log("  ✓ Catalog nodes (stages, body parts, causes, jurisdiction)")
        self.create_insurer();  log("  ✓ Insurer node")
        self.create_adjusters();log("  ✓ Adjusters (12)")
        self.create_providers();log("  ✓ Providers (30)")
        self.create_attorneys();log("  ✓ Attorneys (15)")
        self.create_employers();log("  ✓ Employers (50)")
        log("✅ Phase 1 complete")

    # ── Phase 2 & 3: Claim Generation ────────────────────────────────────────

    def _generate_claim_chain(self, claim_id, sub_traj_code, is_hero=False, hero_spec=None):
        traj = SUB_TRAJECTORIES[sub_traj_code]
        
        status = "CLOSED"
        truncate_stage = None
        days_elapsed = None
        current_reserve = 0
        
        if is_hero and hero_spec:
            status = hero_spec["status"]
            truncate_stage = hero_spec["truncate_at_stage"]
            days_elapsed = hero_spec["days_elapsed"]
            current_reserve = hero_spec.get("current_reserve", 0)
        else:
            if random.random() < 0.35:
                status = "CLOSED"
            else:
                status = "OPEN"
                if len(traj.stages) > 1:
                    truncate_stage = random.choice(traj.stages[:-1])
                else:
                    truncate_stage = traj.stages[0]
        
        min_c, max_c = traj.cost_range
        total_incurred = random.randint(min_c, max_c)
        
        if status == "CLOSED":
            total_paid = total_incurred
            current_reserve = 0
        else:
            if not is_hero:
                split = random.uniform(0.1, 0.8)
                total_paid = int(total_incurred * split)
                current_reserve = total_incurred - total_paid
            else:
                total_paid = max(0, total_incurred - current_reserve)
        
        if days_elapsed is None:
            days_elapsed = random.randint(10, 800)
        
        doi = (datetime.now() - timedelta(days=days_elapsed)).strftime("%Y-%m-%d")
        
        # Base Claim Node
        person_id = f"CLM_PER_{claim_id}"
        self._run(
            """MERGE (p:Person {person_id:$pid})
               SET p.name=$name, p.role='CLAIMANT', p.age_band=$age, p.wage_band=$wage""",
            pid=person_id, name=_rand_name(), 
            age=random.choice(["18-25", "26-35", "36-45", "46-55", "56-65"]),
            wage=random.choice(["$30k-$50k", "$50k-$75k", "$75k-$100k", "$100k+"])
        )
        
        emp_id = random.choice(self.employer_ids.get(traj.employer_group, self.employer_ids["WAREHOUSE"]))
        adj_id = random.choice(self.adjuster_ids)
        
        has_attorney = random.random() < traj.attorney_probability
        att_id = random.choice(self.attorney_ids) if has_attorney else None
        
        self._run(
            """MERGE (c:Claim {claim_id:$cid})
               SET c.cohort=$cohort, c.sub_trajectory=$sub_traj,
                   c.status=$status, c.date_of_injury=$doi,
                   c.total_paid=$paid, c.current_reserve=$reserve,
                   c.is_hero=$is_hero
               """,
            cid=claim_id, cohort=traj.cohort, sub_traj=sub_traj_code,
            status=status, doi=doi, paid=total_paid, reserve=current_reserve,
            is_hero=is_hero
        )
        
        if is_hero and hero_spec:
            self._run("MATCH (c:Claim {claim_id:$cid}) SET c.demo_notes=$notes",
                      cid=claim_id, notes=hero_spec["talk_track"])
            
        self._run(
            """MATCH (c:Claim {claim_id:$cid}), (p:Person {person_id:$pid}), 
                     (e:Employer {employer_id:$emp_id}), (a:Person {person_id:$adj_id}),
                     (i:Insurer {insurer_id:'INS_001'}), (j:Jurisdiction {code:'CA'}),
                     (bp:BodyPart {code:$bp_code}), (ic:InjuryCause {code:$ic_code})
               MERGE (c)-[:HAS_CLAIMANT]->(p)
               MERGE (c)-[:OCCURRED_AT_EMPLOYER]->(e)
               MERGE (c)-[:ASSIGNED_TO]->(a)
               MERGE (c)-[:UNDER_POLICY]->(i)
               MERGE (c)-[:IN_JURISDICTION]->(j)
               MERGE (c)-[:INVOLVES_BODYPART]->(bp)
               MERGE (c)-[:CAUSED_BY]->(ic)
            """,
            cid=claim_id, pid=person_id, emp_id=emp_id, adj_id=adj_id,
            bp_code=traj.body_part, ic_code=traj.injury_cause
        )
        
        if att_id:
            self._run(
                "MATCH (c:Claim {claim_id:$cid}), (att:Attorney {attorney_id:$att_id}) MERGE (c)-[:REPRESENTED_BY]->(att)",
                cid=claim_id, att_id=att_id
            )
            
        stages_to_generate = traj.stages
        if status == "OPEN" and truncate_stage in traj.stages:
            idx = traj.stages.index(truncate_stage)
            stages_to_generate = traj.stages[:idx+1]
            
        prev_event_id = None
        current_date = datetime.strptime(doi, "%Y-%m-%d")
        
        running_reserve = 0
        if "ACCEPTED" in stages_to_generate:
            running_reserve = int(total_incurred * 0.8)
        
        first_event_id = None
        current_event_id = None
        
        for i, stage_code in enumerate(stages_to_generate):
            event_id = f"EV_{claim_id}_{i:03d}"
            
            if first_event_id is None:
                first_event_id = event_id
            current_event_id = event_id
            
            stage_info = STAGE_CATALOG[stage_code]
            mean, std, dmin, dmax = stage_info["duration"]
            dur_days = _rand_duration(mean, std, dmin, dmax)
            
            if i > 0:
                current_date += timedelta(days=dur_days)
                
            self._run(
                """MATCH (c:Claim {claim_id:$cid}), (s:Stage {code:$scode})
                   MERGE (e:ClaimEvent {event_id:$eid})
                   SET e.stage=$scode, e.occurred_at=$date, e.duration_days=$dur
                   MERGE (e)-[:OF_STAGE]->(s)
                """,
                cid=claim_id, scode=stage_code, eid=event_id, 
                date=current_date.strftime("%Y-%m-%d"), dur=dur_days
            )
            
            if prev_event_id:
                self._run(
                    "MATCH (prev:ClaimEvent {event_id:$prev_id}), (curr:ClaimEvent {event_id:$eid}) MERGE (prev)-[:NEXT]->(curr)",
                    prev_id=prev_event_id, eid=event_id
                )
                
            if stage_info["phase"] in ["Active Treatment", "Investigation"] and stage_code not in ["FROI_00", "TRIAGE", "ACCEPTED", "DENIED", "DELAY_NTC"]:
                spec = "Orthopedics"
                if "PT" in stage_code or "MOD" in stage_code: spec = "Physical Therapy"
                elif "IME" in stage_code or "QME" in stage_code: spec = "IME Specialist"
                elif "SURG" in stage_code: spec = "Surgery Center"
                
                if spec in self.provider_ids and self.provider_ids[spec]:
                    prov_id = random.choice(self.provider_ids[spec])
                    self._run(
                        "MATCH (e:ClaimEvent {event_id:$eid}), (p:Provider {provider_id:$pid}) MERGE (e)-[:TREATED_BY]->(p)",
                        eid=event_id, pid=prov_id
                    )
            
            if stage_code in DOCUMENT_TRIGGERS:
                doc_title, doc_desc = DOCUMENT_TRIGGERS[stage_code]
                doc_id = f"DOC_{event_id}"
                self._run(
                    """MATCH (e:ClaimEvent {event_id:$eid})
                       MERGE (d:Document {document_id:$doc_id})
                       SET d.title=$title, d.description=$desc
                       MERGE (e)-[:REFERENCES_DOC]->(d)
                    """,
                    eid=event_id, doc_id=doc_id, title=doc_title, desc=doc_desc
                )
                
            if stage_code in RESERVE_TRIGGERS:
                mult, _ = RESERVE_TRIGGERS[stage_code]
                running_reserve = int(running_reserve * mult)
                if stage_code in ["SETTLE", "STIP", "CLOSED_FN", "CLOSED_DN"]:
                    running_reserve = 0
                    
                rs_id = f"RS_{event_id}"
                self._run(
                    """MATCH (e:ClaimEvent {event_id:$eid})
                       MERGE (rs:ReserveSnapshot {snapshot_id:$rs_id})
                       SET rs.amount=$amount, rs.date=$date
                       MERGE (e)-[:RESET_RESERVE]->(rs)
                    """,
                    eid=event_id, rs_id=rs_id, amount=running_reserve, date=current_date.strftime("%Y-%m-%d")
                )
                
            prev_event_id = event_id
            
        if first_event_id:
            self._run(
                "MATCH (c:Claim {claim_id:$cid}), (e:ClaimEvent {event_id:$first_id}) MERGE (c)-[:FIRST_EVENT]->(e)",
                cid=claim_id, first_id=first_event_id
            )
        if current_event_id:
            self._run(
                "MATCH (c:Claim {claim_id:$cid}), (e:ClaimEvent {event_id:$curr_id}) MERGE (c)-[:CURRENT_EVENT]->(e)",
                cid=claim_id, curr_id=current_event_id
            )

    def create_cohort_claims(self, log=print):
        log("▶ Phase 2: Cohort Claims Generation")
        total = sum(COHORT_COUNTS.values())
        count = 0
        for sub_traj_code, num_claims in COHORT_COUNTS.items():
            for i in range(num_claims):
                claim_id = self._next_id(f"CLM_{sub_traj_code}")
                self._generate_claim_chain(claim_id, sub_traj_code)
                count += 1
                if count % 50 == 0:
                    log(f"  ... generated {count}/{total} cohort claims")
        log(f"  ✓ Total {count} cohort claims generated")
        
    def create_hero_claims(self, log=print):
        log("▶ Phase 3: Hero Claims Generation")
        for hero in HERO_CLAIMS:
            self._generate_claim_chain(hero["hero_id"], hero["sub_trajectory"], is_hero=True, hero_spec=hero)
            log(f"  ✓ Hero claim generated: {hero['hero_id']} ({hero['sub_trajectory']})")
            
    def run_all(self, log=print):
        self.run_phase1(log)
        self.create_cohort_claims(log)
        self.create_hero_claims(log)
        log("✅ Data generation complete.")

if __name__ == "__main__":
    generator = WCDataGenerator()
    print("Clearing database...")
    generator.clear_database()
    generator.run_all()
    generator.close()

