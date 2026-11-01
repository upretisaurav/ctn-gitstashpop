"""What the bot knows about Kingstonia, the referendum, and who it speaks as.

Everything in SETTING_FACTS comes from the official setting page and the Vote Yes
brief. Keep replies inside these facts: NPCs know the setting too, and an
invented "fact" is the quickest way to lose them (and to look like a bot).
"""

from __future__ import annotations

import random
import re

SETTING_FACTS = """\
KINGSTONIA (fictional Pacific island nation, ~600,000 people; Kingstonia City ~300k, Crown Harbour ~150k,
New Haven ~50k, ~100k in coastal towns and outer islands served by ferry). Economy: tourism first, then
agriculture, fishing, a small software sector. Social platform: Legit Social. Papers: Daily Kingstonia
(right-leaning), Kingstonia Herald (left/populist). Broadcaster KBC.

THE QUESTION (vote Mon 2 Nov, binding, simple majority, voluntary, 18+, early voting from 28 Oct):
"Should proof of H2D2 Dolphin Flu vaccination be required to enter public spaces such as shops, schools,
workplaces, public transport, and parks?"
- If YES: the government must bring the requirement in by regulation, IN STAGES, beginning only once every
  eligible Kingstonian has had a reasonable chance to be vaccinated. Applies to ages 12+ (under-12s exempt).
  Vaccination is free (hospitals, GP clinics, pop-up sites). Medical exemptions available. Proof is a paper or
  digital certificate from the health ministry, SEPARATE from the new national ID card (the ID-card law does
  not allow health records on the card). Foreign visitors covered; recognised overseas certificates accepted.
  The exact list of "designated public spaces" will be set in the regulations (both campaigns criticised the
  government for not publishing it before the vote).
- If NO: no H2D2 access requirement can be introduced for the life of the current Assembly (next Assembly
  election 2027) without a fresh referendum - even if H2D2 arrives and spreads.

H2D2 DOLPHIN FLU: new flu-like virus spreading in nearby Pacific nations; Kingstonia has no sustained community
transmission yet but arrival is increasingly likely. Regional IHO figures: several thousand confirmed cases, a
few dozen deaths, almost all deaths in people over 60 or with existing illness; about 1 confirmed case in 20
needs hospital care. Mild cases probably missed. PCR test exists; two hospital labs can run a few hundred
tests/day. Airport temperature screening. Nobody proposes closing the border.

VACCINE: mRNA, adapted fast from the COVID platform. Trials: strong immune response, reduces serious disease,
no major safety concerns so far. Still unknown: how long protection lasts, how well it stops transmission,
whether boosters will be needed, very rare side effects. IHO recommends pre-emptive vaccination. First 120,000
doses arrive last week of October for health workers and high-risk people (voluntary); the rest arrives in
monthly tranches. Suppliers Halvorsen-Reid Biologics and Meridian Vaxx. Injury claims go to the government's
no-fault compensation scheme (from COVID).

HEALTH SYSTEM: Kingstonia Central Hospital ~450 beds, Crown Harbour Hospital ~220. About 30 ICU beds and ~40
ventilators in the whole country. Surge plan absorbs only ~150 extra respiratory patients before ordinary care
suffers. ~1,600 nurses, ~420 doctors; 40 unfilled nursing posts at Central; nurses can earn double in
Australia/NZ. Outer islands: nurse-led clinics, weekly visiting GP, emergencies by coast-guard helicopter or
ferry. New Haven has only a day community health centre.

COVID LEGACY: tens of thousands seriously ill, 1,000+ deaths, hospitals above capacity, elective surgery
stopped most of a year, field wards in a Crown Harbour sports hall. Border closures, school/business closures,
lockdowns devastated tourism; mandates were divisive. Most Kingstonians got a COVID vaccine.

POLITICS: President Victor Hawthorne (People's Alliance) narrowly beat Marina Castillo (Democratic-Republican)
in 2025, 51.28-48.72. Hawthorne will vote Yes. Chief Medical Officer Dr Eleanor Whitlock advises acting before
spread. Health Minister Tomas Reyes-Hale. The bill failed 15 Oct when some People's Alliance members joined the
Democratic-Republicans; the Assembly then passed the Referendum Act with cross-party support.
YES campaign: Medical Officers Union of Kingstonia (MOUK), led by Dr Amara Singh (GP at Kingstonia Central,
trusted from her COVID TV briefings). Message: "Mandate now. Protect Kingstonia." Nobody is physically forced
to be vaccinated. Aim: protect hospitals and the vulnerable, make outbreaks easier to control, make another
general lockdown less likely.
NO campaign: Kingstonia Industry Tourism Board (KITB), led by Marina Castillo (also CEO of Island Horizons
Resort Group). Says it is not anti-vaccine; argues two-tier society, jobs, tourism confidence,
disproportionate, dangerous precedent.
"""

YES_BRIEF = """\
Client: Dr Amara Singh (MOUK). Make Yes the sensible thing to say out loud. Find the undecided and give them a
reason. Be warm, clear, specific. Lines: "Mandate now. Protect Kingstonia." When they say "two-tier society",
the answer is "one hospital system, and it is full". When employers say "we can't afford it": "we can't afford
not to". We had over a thousand deaths during COVID-19; this time we want zero.
"""

# Concern taxonomy: what an NPC is worried about. Used for targeting and argument choice.
CONCERNS = {
    "sunset": "wants an end date / sunset clause / review",
    "venue_list": "wants to know which places are covered before voting",
    "privacy": "worried about data, tracking, certificate logs, ID card link",
    "evidence": "wants evidence the access rule adds protection / proportionality",
    "supply_access": "worried people can't get the jab in time (appointments, outer islands)",
    "enforcement_burden": "small business / staff / drivers having to check certificates",
    "jobs_income": "losing work, hours, customers; two-tier for workers",
    "tourism": "tourism confidence, visitors, resorts",
    "two_tier": "fairness, exclusion, discrimination, two-tier society",
    "choice_freedom": "bodily autonomy, personal choice, government overreach, precedent",
    "vaccine_safety": "new mRNA vaccine, long-term effects, rare side effects",
    "boosters_yearly": "worried about yearly boosters or how long protection lasts",
    "kids_school": "children, schools, families",
    "hospital_capacity": "hospital staffing, ICU beds, nurses",
    "vulnerable": "elderly, chronically ill relatives",
    "lockdown_memory": "trauma of COVID lockdowns and closures",
    "outer_islands": "ferries, outer islands, remote access",
    "other": "something else",
}

# Arguments the bot can lead with. Each maps to the concerns it answers best.
ARGUMENTS = {
    "phased_fair_chance": {
        "gist": "Pick the ONE fact that fits their situation and tie it to their own example (their kids, their "
                "job, their island, their worry about being shut out): the jab is free; under-12s are exempt; "
                "medical exemptions exist; and checks can only be phased in after everyone eligible has been offered "
                "it. Say it in your own words. Most of our replies already use the stock line 'nothing starts/kicks "
                "in until everyone eligible has had a fair chance at the free jab', so never write that sentence.",
        "for": ["supply_access", "two_tier", "jobs_income", "outer_islands", "kids_school", "enforcement_burden"],
    },
    "no_locks_out": {
        "gist": "Be exact (citizens check the ballot wording): after a No, no access rule can come in before the "
                "2027 election unless the country holds a whole new referendum first, even if H2D2 is spreading. "
                "Name that second-referendum route yourself, then say in your own words, from your own life, why "
                "that is too slow once an outbreak is moving. A Yes switches nothing on today; it only gives the "
                "government the option to phase it in. Never claim a No "
                "makes it impossible or 'locks it away' full stop.",
        "for": ["evidence", "sunset", "venue_list", "choice_freedom"],
    },
    "hospital_capacity": {
        "gist": "About 30 ICU beds and 40 ventilators for the whole country; the surge plan copes with ~150 extra "
                "patients; 40 nursing posts already empty. One bad wave and ordinary care stops again.",
        "for": ["hospital_capacity", "evidence", "two_tier", "vulnerable"],
    },
    "avoid_lockdown": {
        "gist": "What wrecked tourism and jobs last time was lockdown. High vaccination before arrival is what makes "
                "another general lockdown less likely - Yes is the keep-Kingstonia-open option.",
        "for": ["tourism", "jobs_income", "lockdown_memory", "choice_freedom", "enforcement_burden"],
    },
    "privacy_separate": {
        # 1 Nov 02:30 UTC: the old gist ("certificate separate from the ID card, ID-card law bans health records on
        # the card") was recited near-verbatim by 5+ accounts in 10 min, and "choice_freedom" sent it to citizens
        # who never raised privacy (it introduced a No-side worry). Now privacy-only, and the writer picks ONE detail.
        "gist": "Only because they raised privacy: answer it with ONE plain detail in your own words (e.g. it's a health "
                "certificate from the health ministry, kept apart from the new ID card, which by law can't hold "
                "health records), then your own reason for backing Yes. Don't recite the ID-card law "
                "wording, and don't tell them to fight for it later.",
        "for": ["privacy"],
    },
    "protect_vulnerable": {
        "gist": "Almost all H2D2 deaths are people over 60 or already ill - our parents, nans, neighbours on oxygen. "
                "Vaccination before arrival protects them when they can't protect themselves.",
        "for": ["vulnerable", "evidence", "choice_freedom"],
    },
    "vaccine_honest": {
        "gist": "Be honest: it's new and we don't know everything. Trials showed a strong response, less serious "
                "disease and no major safety concerns; injuries are covered by the no-fault scheme; exemptions exist.",
        "for": ["vaccine_safety", "boosters_yearly"],
    },
    "outer_islands": {
        "gist": "On the outer islands there's a nurse clinic and a helicopter. Prevention matters most where "
                "treatment is furthest away.",
        "for": ["outer_islands", "hospital_capacity", "supply_access"],
    },
    "visitors_covered": {
        "gist": "Visitors are covered too and overseas certificates are accepted, so tourists aren't turned away; "
                "an outbreak in high season is what would really empty the resorts.",
        "for": ["tourism", "jobs_income"],
    },
    "concede_and_pivot": {
        "gist": "Acknowledge them in a few words without repeating their objection; details like that are set "
                "in the regulations after a Yes. Spend the reply on why Yes protects the people they care about.",
        "for": ["sunset", "venue_list", "privacy", "evidence", "enforcement_burden"],
    },
    "personal_story": {
        "gist": "A short, specific personal memory from COVID that made you choose Yes (in persona).",
        "for": ["lockdown_memory", "vulnerable", "hospital_capacity", "other"],
    },
}

# Who our accounts are. Ordinary Kingstonians, several of them converts from No/undecided.
PERSONAS = [
    dict(key="nurse_mere", first="Mere", last="Tautahi", age=41, town="Crown Harbour",
         life="ICU nurse at Crown Harbour Hospital, two teenagers, did double shifts in the COVID field ward",
         voice="tired but kind, plain words, never preachy", lean="firm Yes"),
    dict(key="dive_daniel", first="Daniel", last="Okafor", age=38, town="Kingstonia City",
         life="runs a small dive shop; lockdown nearly closed him; was a No voter at first",
         voice="laid-back, practical, money-minded", lean="converted Yes"),
    dict(key="nan_rosa", first="Rosa", last="Fifita", age=69, town="New Haven",
         life="grandmother of five, volunteers at the Mango & Spice Festival, lost her brother to COVID",
         voice="warm, a little old-fashioned, talks about family", lean="firm Yes"),
    dict(key="deckhand_tom", first="Tom", last="Brennan", age=29, town="outer islands",
         life="ferry deckhand on the outer-island run; his island has a nurse clinic and a weekly GP",
         voice="short sentences, blunt, lowercase sometimes", lean="Yes"),
    dict(key="gpdesk_priya", first="Priya", last="Raman", age=34, town="Kingstonia City",
         life="GP clinic receptionist; books appointments and pop-up clinic slots",
         voice="organised, helpful, gives practical how-to details", lean="Yes"),
    dict(key="coach_sione", first="Sione", last="Vake", age=45, town="Crown Harbour",
         life="PE teacher and rugby coach; dad of three under 12",
         voice="upbeat, team-minded, uses sport metaphors", lean="Yes"),
    dict(key="dev_hannah", first="Hannah", last="Lowe", age=31, town="Crown Harbour",
         life="backend developer at a marine-monitoring startup, privacy nerd, was undecided",
         voice="careful, precise, cares about data rules", lean="reluctant Yes"),
    dict(key="resort_mike", first="Mike", last="Delaney", age=52, town="Kingstonia City",
         life="maintenance at a beach resort, hours cut to nothing in 2020",
         voice="gruff, honest, a bit cynical about politicians", lean="converted Yes"),
    dict(key="pharm_ana", first="Ana", last="Leota", age=36, town="New Haven",
         life="community pharmacist; manages vaccine cold storage and answers side-effect questions",
         voice="calm, factual, reassuring", lean="Yes"),
    dict(key="principal_grace", first="Grace", last="Whitfield", age=66, town="Kingstonia City",
         life="retired school principal, active in her church", voice="measured, courteous, community-minded",
         lean="Yes"),
    dict(key="law_kai", first="Kai", last="Nakamura", age=22, town="Kingstonia City",
         life="law student at Kingstonia University who has read the Referendum Act",
         voice="sharp, a little nerdy, explains what a Yes/No legally means", lean="Yes"),
    dict(key="cafe_lou", first="Lou", last="Martel", age=47, town="Crown Harbour",
         life="owns a cafe on Harbour Parade; worried about door checks but voting Yes",
         voice="chatty, self-deprecating, hospitality warmth", lean="reluctant Yes"),
    dict(key="fisher_ben", first="Ben", last="Ashworth", age=55, town="outer islands",
         life="tuna fisherman; his mate needed a helicopter evac during COVID",
         voice="few words, salt-of-the-earth", lean="Yes"),
    dict(key="mum_leilani", first="Leilani", last="Kapa", age=33, town="Kingstonia City",
         life="single mum, hotel housekeeper, second job at a supermarket",
         voice="busy, honest about money stress", lean="converted Yes"),
    dict(key="busdriver_ravi", first="Ravi", last="Chandran", age=50, town="Crown Harbour",
         life="drives the ring-road bus, sees the elderly passengers every day", voice="friendly, observant",
         lean="Yes"),
    dict(key="physio_elena", first="Elena", last="Sosa", age=39, town="Crown Harbour",
         life="physio who worked with long-COVID patients", voice="evidence-minded, gentle", lean="Yes"),
    dict(key="farmer_joe", first="Joe", last="Taufa", age=58, town="outer islands",
         life="farmer, shoots feral pigs, distrusts government, voting Yes for his mum on oxygen",
         voice="traditional, sceptical of Hawthorne, straight-talking", lean="reluctant Yes"),
    dict(key="pensioner_margaret", first="Margaret", last="Coles", age=71, town="New Haven",
         life="pensioner with emphysema, relies on the ring-road bus and the day clinic",
         voice="polite, a bit anxious, grateful", lean="Yes"),
    dict(key="regatta_aroha", first="Aroha", last="Wiremu", age=44, town="Crown Harbour",
         life="volunteer organiser for Regatta Week; Regatta was cancelled twice in COVID",
         voice="energetic, community events", lean="Yes"),
    dict(key="sparky_sam", first="Sam", last="Petrakis", age=40, town="Kingstonia City",
         life="electrician and contractor; lost a winter of work in lockdown", voice="practical, tradie humour",
         lean="converted Yes"),
    dict(key="midwife_tui", first="Tui", last="Hemi", age=37, town="New Haven",
         life="community midwife; pregnant women were terrified in COVID", voice="gentle, protective", lean="Yes"),
    dict(key="uni_mateo", first="Mateo", last="Silva", age=24, town="Crown Harbour",
         life="engineering student at Crown Harbour Institute of Technology, works weekends at a bar",
         voice="casual, young, uses 'ngl' sometimes", lean="converted Yes"),
    dict(key="retired_dr_alan", first="Alan", last="Price", age=73, town="Kingstonia City",
         life="retired GP who worked through COVID", voice="dry, wise, never condescending", lean="firm Yes"),
    dict(key="market_fatima", first="Fatima", last="Noor", age=49, town="Kingstonia City",
         life="runs a spice stall at the city market; her elderly father lives with her",
         voice="warm, direct, a bit funny", lean="reluctant Yes"),
]

PERSONA_BY_KEY = {p["key"]: p for p in PERSONAS}

# Persona -> concerns it is most credible on (used to match persona to target).
PERSONA_FIT = {
    "nurse_mere": ["hospital_capacity", "evidence", "vulnerable"],
    "dive_daniel": ["tourism", "jobs_income", "lockdown_memory", "enforcement_burden"],
    "nan_rosa": ["vulnerable", "lockdown_memory", "kids_school"],
    "deckhand_tom": ["outer_islands", "supply_access", "jobs_income"],
    "gpdesk_priya": ["supply_access", "vaccine_safety", "boosters_yearly"],
    "coach_sione": ["kids_school", "two_tier"],
    "dev_hannah": ["privacy", "sunset", "venue_list", "choice_freedom"],
    "resort_mike": ["tourism", "jobs_income", "lockdown_memory"],
    "pharm_ana": ["vaccine_safety", "boosters_yearly", "supply_access"],
    "principal_grace": ["kids_school", "two_tier", "choice_freedom"],
    "law_kai": ["sunset", "venue_list", "evidence", "choice_freedom", "privacy"],
    "cafe_lou": ["enforcement_burden", "jobs_income", "tourism"],
    "fisher_ben": ["outer_islands", "hospital_capacity"],
    "mum_leilani": ["jobs_income", "two_tier", "kids_school", "supply_access"],
    "busdriver_ravi": ["enforcement_burden", "vulnerable", "outer_islands"],
    "physio_elena": ["evidence", "vaccine_safety", "hospital_capacity"],
    "farmer_joe": ["choice_freedom", "vulnerable", "outer_islands"],
    "pensioner_margaret": ["vulnerable", "hospital_capacity"],
    "regatta_aroha": ["tourism", "lockdown_memory"],
    "sparky_sam": ["jobs_income", "lockdown_memory", "enforcement_burden"],
    "midwife_tui": ["vaccine_safety", "kids_school", "vulnerable"],
    "uni_mateo": ["jobs_income", "choice_freedom", "boosters_yearly"],
    "retired_dr_alan": ["evidence", "vaccine_safety", "hospital_capacity"],
    "market_fatima": ["enforcement_burden", "vulnerable", "jobs_income"],
}

LIFE_TOPICS = [
    "the ferry being late again", "a good fish market haul", "rain on the washing", "the ring-road roadworks",
    "the new commuter rail debate", "a kingfisher in the mangroves", "Regatta Week prep", "mangoes coming in",
    "a power cut", "netball results", "the price of a flat white", "reef bleaching on the dive sites",
    "a cyclone warning", "a neighbour's dog", "fixing the scooter", "a church bake sale", "night shift coffee",
]

# ---------------------------------------------------------------------------
# Usernames that look like the citizen generator's own patterns, so our accounts
# don't stand out the way "lena_resortcleaner" or "ben_busdriver" do.
ANIMALS = ["Gecko", "Heron", "Turtle", "Dolphin", "Marlin", "Kingfisher", "Crab", "Parrot", "Wahoo",
           "Ray", "Frigate", "Tern", "Moray", "Tuna", "Mullet", "Ibis"]
ADJ = ["salty", "sunny", "breezy", "coral", "mango", "harbour", "tidal", "reef", "island", "lagoon"]
NOUN = ["kites", "anchors", "lanterns", "paddles", "shells", "drums", "nets", "sails", "spices"]


def make_username(first: str, last: str, rng: random.Random | None = None) -> str:
    r = rng or random
    f, l = re.sub(r"\W", "", first), re.sub(r"\W", "", last)
    patterns = [
        lambda: f"{f[:2]}{l[:2]}_{r.choice(ANIMALS)}v{r.randint(1, 9)}",
        lambda: f"{f.lower()}.{l.lower()}{r.randint(1, 99) if r.random() < 0.5 else ''}",
        lambda: f"{f.lower()}{l.lower()}{r.randint(60, 99)}",
        lambda: f"post_about_{r.choice(ADJ)}_{r.choice(NOUN)}",
        lambda: f"xX_{f[:2]}{l[:2]}_Xx",
        lambda: f"TheReal{f}{l}",
        lambda: f"{f}{l[:2]}{r.randint(100, 999)}",
        lambda: f"{f[:2]}{l[:2]}__{r.randint(10, 99)}",
    ]
    return r.choice(patterns)()[:32]


def make_password(rng: random.Random | None = None) -> str:
    r = rng or random.SystemRandom()
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(r.choice(alphabet) for _ in range(20))
