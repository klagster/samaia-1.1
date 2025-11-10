```mermaid
flowchart TD
    %% === INPUTS ===
    A[Supabase: Campaign + Target Account Records] --> B[Pipeline Input Builder]

    %% === CORE PIPELINE ===
    B --> C[Step 1: Data Acquisition<br/>→ Load signals + company context]
    C --> D[Step 2: Hypothesis Generation<br/>→ AI builds issue statements]
    D --> E[Step 3: Taxonomy Alignment<br/>→ Map to challenge taxonomy]
    E --> F[Step 4: Compelling Event Construction<br/>→ Generate sales narratives]
    F --> G[Step 5: Output Assembly<br/>→ Write to Supabase JSON columns]

    %% === VERTEX & AI INTEGRATION ===
    subgraph Vertex_AI_and_ADK
        H[Gemini 2.0 Flash<br/>Search + Reasoning Model]
        I[Grounding + Schema Validation]
        J[Evidence Enrichment<br/>Auto-Collect Web Data]
        K[Regex / JSON Fallback Parser]
        H --> I --> J --> K
    end

    %% === LOCAL EXECUTION ===
    subgraph Local_Tools
        L[preview_queries.py<br/>Generate and Preview Web Queries]
        M[run.py<br/>Full Pipeline Executor]
        N[enrich_signals.py<br/>Attach Evidence + Merge Signals]
    end

    %% === FLOW CONNECTIONS ===
    B --> H
    D --> J
    J --> N
    N --> F
    K --> F
    F --> G

    %% === OUTPUTS ===
    subgraph JSON_Outputs
        G --> O[step2_json<br/>Hypotheses + Evidence]
        G --> P[step3_json<br/>Aligned Challenges]
        G --> Q[step4_json<br/>Compelling Events]
    end

    %% === STYLES ===
    classDef phase fill:#ffefff,stroke:#333,stroke-width:1px;
    classDef vertex fill:#e0ffe0,stroke:#333,stroke-width:1px;
    classDef local fill:#fff2cc,stroke:#333,stroke-width:1px;
    classDef output fill:#e0f0ff,stroke:#333,stroke-width:1px;

    class C,D,E,F phase
    class H,I,J,K vertex
    class L,M,N local
    class O,P,Q output