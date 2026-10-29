# SOW-AR Milestones

> **Status: DRAFT — under review.** This document is a working draft and is subject to change. Please provide comments and suggestions.

M1 & M2 Target Date: Sept 1st, 2026

Calendar mapping: M1 = July 2026 · M2 = August 2026 · M3 = September 2026 · M4 = October 2026 · M5 = November 2026 · M6 = December 2026

## August MVP — Team Expectations

The three expectations the MVP must meet by end of August, on a Dev-only, manually-triggered happy path:

1. **Onboard features via YAML** — DS team can onboard new features, even if the pipeline doesn't run automatically: list those features in YAML and run ingestion manually.
2. **Store model in registry + score new data** — DS team can store a model in the registry and score new data on the model.
3. **View performance + backtesting + metrics** — DS team can view model performance and generate backtesting and model performance metrics.

---

> **August MVP path.** Milestones are ordered into two buckets:
> - **Milestone 1.6 — Build now (S1–S2, required regardless):** Foundation (accounts, IaC/CDK, networking, IAM/IdC, KMS, spikes), M1 Studio + profiles, M2 Feature Store with YAML onboarding, and M6 model registry (Dev). Delivers **Expectation 1** and the "store model in registry" half of **Expectation 2**.
> - **Milestone 2.6 — Net-new MVP gap (S3–S4, the gap to close):** M4-minimal cohort/label + point-in-time join, M5-MVP manual/Dev training pipeline (train + register), and a registered-model **endpoint + Feature Store access** so the Customer DS team runs **their own evaluation/backtest scripts manually** (and scores new data via the endpoint). The full AR-built evaluation/backtest report and the productised model-scoring solution are **deferred — post August-MVP**. Completes **Expectation 2** and delivers **Expectation 3** (Customer-run).
>
> Everything else (automated scheduling, full Neptune KG discovery, MCP/Gateway serving, drift monitoring, full training automation, HITL governance, production hardening) is **deferred — post August-MVP** and tagged inline. Per-milestone trade-off tables record what moved or was added versus the initial plan and the business value it targets. A dedicated **"August MVP — Opportunity cost"** table (directly below the MVP milestones) frames these deferrals through the leadership lens: **what the original plan would have delivered earlier that is now pushed out to protect the August date**, and why that trade is acceptable.

---

## Business Summary — What's In Place, By Month

| Milestone | Month | What's delivered (business terms) | Real business value (DS / ops impact) |
|---|---|---|---|
| M1 | July 2026 — Foundation + Registry | The AWS development environment is stood up — networking, security groups, encryption (KMS), and an automated build/deploy (IaC/CDK + CI/CD) pipeline for dev. Read connectivity to the customer's Snowflake Gold layer is verified and an S3 landing zone is ready. SageMaker Unified Studio is available to the data science team (user profiles, Snowflake and Feature Store connectivity). Cross-account access roles and data-access rules (Snowflake permissions, encryption at rest/in transit) are configured. The **Feature Store is live** with the first feature group loaded from Snowflake, and the DS team can **onboard new features via a YAML form and run ingestion manually** (**Expectation 1**). A **SageMaker Model Registry (Dev)** is in place to store models with model cards (**half of Expectation 2**). Initial KDA-model discovery is documented and the feature-store design (LLD) is approved. | **DS team is productive on a shared platform from day one: features onboarded in days (YAML) instead of weeks of bespoke code, and models catalogued in one discoverable registry instead of scattered notebooks/S3. Managed services keep infra maintenance low.** |
| M2 | August 2026 — August MVP Complete | The first real predictive model is built end to end. A **point-in-time-correct labeled dataset** (no target leakage) is produced for the first KDA model (Cosentyx HS) and versioned to S3. The DS team can **train the model and register it** (with a model card) by running the pipeline manually — extract → features → train → evaluate → register (**Expectation 2 — train + register**). The registered model is **deployed to a SageMaker endpoint** and, with Feature Store access, the **Customer DS team runs their own evaluation/backtesting scripts manually** to generate a backtest report (**Expectation 3**) and to **score new data** through the endpoint (**Expectation 2 — score new data**). The full AR-built evaluation report (SHAP/PDP, Clarify bias, ≥75% gate) and the productised model-scoring solution are **deferred — post August-MVP**. The completed MVP is demonstrated to customer stakeholders. **All three August expectations are met (evaluation and scoring are Customer-run/manual in August).** | **A real, trustworthy model that produces predictions — reproducible, auditable metrics give the business a confident go/no-go, and a repeatable train→register→score path replaces one-off notebooks with consistent, comparable outputs.** |
| M3 | September 2026 — Full KDA Scope | The single-model MVP expands toward the full KDA scope. Full discovery is documented (feature catalog, business rules, success criteria, integration requirements) and source SQL for **all four feature groups** is drafted and validated against live Snowflake data. Four feature groups are defined, the **first two loaded with live data**, and a **weekly-refresh skeleton** runs on schedule. A **reusable ML pipeline template** runs a training job end to end without manual steps. **At least three algorithms** are compared to select the best model. Operational visibility (centralized logs, alarms, SNS alerting, pharma-compliant log retention) is in place, and a **staging environment** is stood up for pre-production validation. | **A discoverable, reusable feature library plus a consistent pipeline template cut per-model effort; onboarding gates keep feature quality consistent, and staging + observability make releases safer with fewer incidents.** |
| M4 | October 2026 — Feature Store Complete + KG Started | The Feature Store is completed — **all four KDA feature groups** are loaded (online + offline) and **refresh automatically every week with data-freshness checks**. Model and feature information starts flowing into the **knowledge graph (Neptune)**: feature nodes and model nodes are written on registration, are queryable via SPARQL, and are linked to the registry model package for lineage and discovery. | **Automated weekly refresh removes recurring manual data-prep toil (ongoing DS hours saved) and keeps models on fresh data; graph lineage makes features and models discoverable and enables impact analysis ("what breaks if this feature changes?").** |
| M5 | November 2026 — Automation, Serving, Monitoring, Governance | The platform becomes automated and governed. The **training pipeline runs fully automatically on a schedule (90–120 day cadence) and only when data is fresh**, with conditional (quality-gated) registration and automatic sync to Neptune and the Collate catalog. The model is served as an **auto-scaling SageMaker endpoint** and exposed as an agent-callable **MCP tool** (`predict_territory_drivers`). **Drift monitoring** runs daily with a CloudWatch dashboard and an automatic retraining trigger. The knowledge graph is completed with **feature, model, and KDA insight nodes** (traversal from a territory to its KDA insight works). **Weekly batch scoring** runs across territories with results written back to Snowflake Gold and Neptune. **Human-review (HITL) workflows** — entity resolution, model approval, feature validation — are operational with UIs and notifications, and the first model is approved and promoted to the production model group. | **The platform runs itself with oversight: hands-off scheduled retraining saves major recurring DS time, drift detection sustains accuracy, on-demand serving makes predictions usable by apps/agents, and governed promotion enforces consistent, compliant quality — reducing production maintenance.** |
| M6 | December 2026 — Production + KDA Live | The system is production-hardened and handed over. The **production environment** is deployed with monitoring, alerting, and cost tracking. Drift monitoring runs cleanly for **at least seven consecutive days** with dashboards accessible to the customer. **Batch scoring covers 100% of territories for two consecutive weeks**, with results queryable in Snowflake Gold and Neptune. **All HITL review points are production-ready** (approve/reject demonstrated end to end with audit logging). The **final demo** shows territory drivers queryable in natural-language chat with **live KDA predictions**. Documentation, operational runbooks, data-model docs, and knowledge-transfer sessions are delivered so the customer can operate independently, and **KDA insight nodes are integrated with the Field Orchestration application** (territory-level queries return insights and recommended actions). | **Reliable full-scale predictions in production with monitoring and cost control; the customer can operate the system independently, and KDA insights reach field teams inside their own application — turning models into day-to-day decisions.** |

---

## Milestone 1.6 (July 2026) Deliverables and Acceptance Criteria — Build now (Foundation + registry)

**1.6.1 — Infrastructure provisioned to build (Code):**

VPC, subnets, security groups, development environments, IaC/CDK templates, CI/CD skeleton, KMS.

Acceptance Criteria:
  1. IaC templates successfully deploying into development
  2. CI/CD executing a sample deployment without errors confirmed

**1.6.2 — Data foundation (Technical Solution):**

Snowflake Gold Layer read connectivity developed and verified, and Amazon S3 landing zone configured.

Acceptance Criteria:
  1. Test query to Snowflake Gold returning results confirmed
  2. Amazon S3 landing zone accepting uploads

**1.6.3 — SageMaker Unified Studio provisioned (Technical Solution):**

SageMaker Unified Studio provisioned with user profiles, kernels, Snowflake connectivity, and feature store connectivity.

Acceptance Criteria:
  1. SageMaker Unified Studio accessible to at least two (2) user profiles developed
  2. Feature store connectivity confirmed via a sample feature read

**1.6.4 — Identity and access setup (Technical Solution):**

Cross-account IAM role structure established and documented.

Acceptance Criteria:
  1. All cross-account IAM service roles created and policy-attached

**1.6.5 — Data access policies configured (Technical Solution):**

Snowflake roles, VPC security groups, encryption enabled.

Acceptance Criteria:
  1. Snowflake roles granting appropriate access confirmed
  2. Encryption confirmed at rest and in transit

**1.6.6 — Feature Store provisioned with YAML onboarding (Code):**

Feature Store provisioned; first Feature Group ingested from Snowflake Gold. YAML intake-form schema + compiler and the ingestion Processing job in place; features onboarded via YAML and ingested by running the job manually (no automated schedule required for the MVP). Formal onboarding gates (schema/data-quality validation + onboarding approval) are deferred to 3.6.2.

Acceptance Criteria:
  1. First feature group populated and documented
  2. Feature onboarded via YAML intake form and ingestion job run manually, populating online and offline stores
  3. Online store returning feature values

**1.6.7 — Model registry deployed, Dev / workload-local (Technical Solution):**

SageMaker Model Registry deployed as the Dev (workload-local) model package group with a model card template.

Note: Model registry changed from MLflow (on Fargate) to SageMaker Model Registry. The model-approval workflow/gates (PendingApproval / Approved / Rejected transitions, HITL review) are **deferred — post August-MVP** (see 5.6.7 / 5.6.8); the MVP stores and catalogues models without an approval gate.

Acceptance Criteria:
  1. SageMaker Model Registry accessible with the Dev model package group created
  2. Model card template populating on registration

**1.6.8 — KDA Discovery commenced (Document):**

Initial outputs document produced: existing model documentation, data source inventory, integration requirements documented.

Acceptance Criteria:
  1. KDA discovery document covering existing model algorithm, target variables, data sources, and integration requirements — delivered and reviewed by the Customer data science team

**1.6.9 — Architecture finalization document (Document):**

LLD for feature store schema.

Acceptance Criteria:
  1. LLD (Low-Level Design) covering feature store schema — reviewed and approved by the Customer

### Milestone 1.6 — MVP trade-offs vs. initial plan

| Item | Change vs. initial plan | Business value targeted | Real business value (DS / ops impact) |
|---|---|---|---|
| 1.6.6 Feature Store + YAML onboarding | Onboarding acceptance consolidated into Milestone 1 (intake-form schema + compiler + ingestion job); **onboarding gates deferred to 3.6.2** | DS team can onboard features via YAML and run ingestion without waiting for automated scheduling — meets **Expectation 1** | **Feature time-to-onboard drops from weeks of bespoke pipeline code to a YAML form (days); a shared store makes features discoverable and reusable across models, eliminating duplicate engineering and train/serve skew** |
| 1.6.7 Model registry (Dev) | **Moved earlier** from 2.6.1 | Delivers the "store a model in the registry" half of **Expectation 2** in the build-now bucket | **One catalogued home for every model version with lineage — models become discoverable and comparable across the team, replacing scattered notebooks/S3 artifacts and giving audit-ready traceability** |
| 1.6.1 Staging environment | **Not in the MVP** — dev env only in scope; **reintroduced** at 3.6.7 (Sept). | Dev-only deployment is sufficient for the three August expectations; a pre-prod tier is restored post-MVP | **Dev-only keeps a second environment's infra cost and maintenance off the books during the MVP; the pre-prod safety tier that catches issues before production is restored in Sept without holding up August** |

---

## Milestone 2.6 (Aug 2026) Deliverables and Acceptance Criteria — Net-new MVP gap (Expectations 2 & 3)

**2.6.1 — Cohort and label engineering, minimal (Code):**  *(M4-minimal — hidden prerequisite)*

Point-in-time labeled dataset produced for the first KDA model (Cosentyx HS): cohort/label intake plus point-in-time feature join, versioned to Central S3. Just enough to produce one labeled dataset.

Acceptance Criteria:
  1. One versioned labeled dataset produced with a point-in-time-correct feature join (no target leakage)
  2. Labeled dataset available to the training pipeline

**2.6.2 — KDA training pipeline: train and register, MVP (Code):**  *(M5-MVP; moved earlier)*

Manual/Dev training pipeline that trains the first KDA model on the labeled dataset and registers the candidate to the Dev registry: extract → features → train → evaluate → register, run manually (full automation and conditional-registration logic deferred to 5.6).

Acceptance Criteria:
  1. Training pipeline executing extract → features → train → evaluate → register on the labeled dataset
  2. Trained KDA model registered in the registry with a model card

**2.6.3 — KDA model evaluation and backtest, Customer-run (Document):**  *(Reduced scope; full AR report deferred)*

Using the registered model (deployed to an endpoint in 2.6.4) and the Feature Store features, the **Customer DS team runs their own evaluation/backtesting scripts manually** to generate the backtest report (out-of-time / holdout KPIs). AR provides the model, endpoint, and feature access; AR does not build bespoke evaluation logic.

Note: The standard backtest uses **only the evaluation/backtesting scripts shared by the Customer DS team**. The full AR-built + automated evaluation report — side-by-side metric comparison, SHAP/PDP explainability, SageMaker Clarify bias assessment, and the ≥75% accuracy gate — is **deferred — post August-MVP**: automated reporting arrives via the pipeline (template in 3.6.3 → fully automated in 5.6.5).

Acceptance Criteria:
  1. Customer DS team able to read Feature Store features and invoke the endpoint from their own evaluation scripts
  2. Backtest report (out-of-time / holdout KPIs) generated by the Customer DS team using their own scripts

**2.6.4 — Registered-model endpoint + feature access, MVP (Technical Solution):**  *(Reduced scope; full Model scoring deferred; MCP/Gateway deferred)*

Deploy the model registered to the Dev model package group in 2.6.2 to a SageMaker endpoint (from the registered model package, `ModelPackageName` = model package ARN) and provide Feature Store access, so the Customer DS team can invoke it for evaluation and ad-hoc new-data scoring.

Note: Deployment sources the model from the Dev registry (1.6.7 / 2.6.2) rather than a raw S3 artifact, so the deployed model carries version and lineage. The full **Model scoring solution** — scoring new data at scale via Batch Transform and scheduled/automated scoring — is **deferred — post August-MVP** (5.6.2 serving, 5.6.6 batch scoring). MCP tool / AgentCore Gateway registration and Neptune discovery are also deferred (see 5.6).

Acceptance Criteria:
  1. Registered model package deployed to a SageMaker endpoint, created from the model package ARN
  2. Customer able to invoke the endpoint and receive predictions (new-data scoring for evaluation)

**2.6.5 — Milestone 1 Demo (Document):**  *(Moved earlier from 3.6)*

Demo delivered per agreed demo script; demo summary document produced. Demonstrates the completed August MVP: onboard features and run manually, train + register a model, deploy an endpoint, and the Customer runs their own scripts to score new data and view performance/backtest metrics.

Acceptance Criteria:
  1. Milestone 1 demo conducted with Customer stakeholders
  2. Demo summary document covering component status, integration path, and risks delivered

### Milestone 2.6 — MVP trade-offs vs. initial plan

| Item | Change vs. initial plan | Business value targeted | Real business value (DS / ops impact) |
|---|---|---|---|
| 2.6.1 Cohort & label engineering (minimal) | **Added** — not in initial plan | Produces the point-in-time labeled dataset that **Expectations 2 & 3 depend on**; without it there is nothing to train or backtest (hidden prerequisite) | **A versioned, leakage-free dataset makes model metrics trustworthy and reproducible (auditable) — preventing the silent over-optimism that would otherwise derail go-live decisions and cause rework** |
| 2.6.2 Train + register (MVP) | **Moved earlier** from initial 4.6 | DS team can train and store a real model by end of August — **Expectation 2** (train + register) | **A repeatable extract→train→register path standardises how every model is produced and stored — cutting per-model setup effort and giving consistent, comparable, catalogued outputs instead of one-off notebooks** |
| 2.6.3 Evaluation + backtest, Customer-run | **Reduced scope, partly earlier.** Customer runs their own eval/backtest scripts against the endpoint + features; the full AR-built + automated evaluation report (from initial 4.6.4) is **deferred to 5.6.5** | Customer team can view model performance + backtesting (out-of-time holdout) now — **Expectation 3** (Customer-run) | **Customer gets a backtest report immediately using their own trusted scripts; the standardised automated AR report (SHAP/PDP, Clarify, ≥75% gate) that puts every model on one yardstick follows with the pipeline** |
| 2.6.4 Registered-model endpoint (enablement) | **Reduced scope.** Endpoint + Feature Store access so the Customer can score/evaluate; the productised **Model scoring** solution (Batch Transform at scale, scheduled — from initial 5.6) is **deferred to 5.6.2 / 5.6.6** | Customer can score new data via the endpoint — **Expectation 2** (score new data), version/lineage carried through | **The customer can turn the stored model into predictions now (endpoint, sourced from the registry); the productised at-scale/scheduled scoring that removes manual runs follows post-MVP** |
| 2.6.5 Milestone 1 Demo | **Moved earlier** from 3.6 | Demonstrates the completed end-of-August MVP to Customer stakeholders | **Early proof builds stakeholder confidence and surfaces course-corrections before the expensive automation phase — protecting the investment and momentum** |
| KDA Discovery document (full, 4 groups) | **Moved later** from initial 2.6.3 → now 3.6.1 | Full four-group discovery is beyond the single-model MVP; sequenced post-MVP to protect the August date | **The complete design blueprint that prevents costly downstream rework — high value, but sequenced to Sept so it doesn't block the August date** |
| MLflow / model registry | **Moved earlier** from initial 2.6.1 → now 1.6.7 (also switched MLflow → SageMaker Model Registry) | Registry is a build-now foundation item; delivers the "store model in registry" half of **Expectation 2** | **Managed SageMaker registry removes the maintenance burden of a self-hosted MLflow/Fargate stack while giving discoverable, version-controlled models — less ops overhead, more consistency** |
| Feature Store expansion (2nd group + weekly-refresh skeleton) | **Moved later** from initial 2.6.2 → now 3.6.2 | The MVP needs only one feature group (1.6.6); expansion + scheduled refresh sequenced post-MVP | **Broader, discoverable, reusable feature coverage plus reduced manual-refresh toil — real recurring value, but not required to prove the single-model MVP** |

### August MVP — Opportunity cost: business value deferred to protect the August date

*Read this as the mirror image of the two tables above. Those show what was pulled **into** the MVP; this shows what the original plan would have delivered in the same window that is now **pushed out** to accommodate it — the lens for a go/no-go on scope vs. date.*

*Net effect: the reprioritization pulls the highest-value, DS-requested capabilities (train, register, and a registered-model endpoint the Customer uses to run their own evaluation/scoring) **earlier**; the full AR-built evaluation/backtest report and the productised model-scoring solution remain post-MVP (in August the Customer runs their own scripts). What moves out is predominantly **feature breadth, refresh automation, full-scope design, training automation, and production governance** — not direct end-user analytical value. Slips are ~1–2 months (feature breadth, automated refresh) and ~1 month (full training automation, conditional registration); the registry itself moves *earlier* (to July), with only its production-promotion step landing later.*

| Deferred to accommodate the MVP | Original plan | Now lands | Business value delayed (the opportunity cost) | Why the trade is acceptable | Real business value (once delivered) |
|---|---|---|---|---|---|
| **Additional feature groups** (beyond the first group) | Aug (2.6.2) | Sept (3.6.2 / 3.6.4) → all four Oct (4.6.1) | Models through August run on a **single feature group**, so broader multi-domain coverage and any model-quality lift from richer features is delayed ~1–2 months | The MVP model (Cosentyx HS) needs only the one group; breadth improves models but isn't required to prove train→score→evaluate | **Feature discoverability + reuse across models; richer signals lift model accuracy and, in turn, field-decision quality** |
| **Automated / scheduled feature refresh + first data-freshness check** | Aug (2.6.2) | Sept skeleton (3.6.2) → Oct full (4.6.1) | Data refresh stays **manual through August**; reduced operational toil and lower staleness risk arrive ~1–2 months later | MVP is a manual/Dev happy path — refresh cadence isn't needed to meet the three expectations | **Removes recurring manual data-prep toil (weekly DS hours saved) and guards against decisions made on stale data** |
| **Full four-group KDA discovery document** | Aug (2.6.3) | Sept (3.6.1) | The **complete design blueprint** downstream planning depends on lands ~1 month later; only single-model discovery (1.6.8) exists in the MVP window | Single-model discovery is enough to build and train the MVP; full-scope design isn't on the MVP critical path | **Complete blueprint that cuts downstream rework and model-maintenance surprises** |
| **Fully-automated ("no manual intervention") training pipeline** | Oct (4.6.2 / 4.6.5) | Nov (5.6.5); generic unattended template Sept (3.6.3) | Hands-off, scheduled retraining slips ~1 month past the original October; through the MVP the pipeline is **run manually** (a manual train→register version is delivered *earlier*, Aug 2.6.2) | The MVP proves the flow manually first; scheduling/automation is toil-reduction, not required for the three expectations | **Hands-off scheduled retraining — major recurring DS time saving, consistent repeatable runs, and less production maintenance** |
| **Conditional (quality-gated) registration** | Oct (4.6.5) | Nov (5.6.1) | The auto-gate that registers a model **only** if it clears thresholds slips ~1 month; the MVP registers on manual judgement | Manual judgement is fine for a Dev-only happy path; the gate matters once registration is automated | **Automated quality gate stops sub-par models reaching prod — a consistent quality bar and less firefighting** |
| **Production-state promotion** (registry lifecycle) | Aug (2.6.1) | Nov (5.6.7) | Registry *infrastructure* actually moves **earlier** (July, 1.6.7), but the "promote to the production model package group" step lands in Nov | The MVP is Dev/workload-local by design; production promotion depends on the HITL approval flow, itself post-MVP | **A governed, auditable path to production for safe, compliant releases** |

> **On staging (not a loss vs. the original plan):** V1 stands up a staging environment in Sept (3.6.7). The **initial plan had already cut staging** ("only dev env is in scope"), so V1 does not defer staging — it **restores** a pre-prod tier that was previously removed. Dev-only remains the posture through the MVP.

**Bottom line for LT:** nothing that directly delivers analytical outcomes to end users is sacrificed to hit August — every deferral is **breadth, automation, full-scope design, or governance**, all off the MVP critical path. The largest single slips are ~1–2 months (feature breadth, automated refresh) and ~1 month (full training automation, conditional registration); the registry itself moves *earlier* (to July), with only its production-promotion step landing in Nov. If LT weights any of these above the August date (e.g., broader feature coverage, automated refresh, or a quality-gated pipeline sooner), it can be pulled back into the MVP window — the explicit cost being a later MVP or reduced model/scoring scope in August.

---

## Milestone 3.6 (Sept 2026) Deliverables and Acceptance Criteria — Post-MVP: full feature groups and experiments

*Post August-MVP: expands the single-model MVP to the full KDA scope.*

> **In business terms — what's in place after this milestone:** The platform moves beyond the single-model MVP to the full KDA scope. The complete data foundation is designed and documented — feature catalog, business rules, success criteria, and validated source SQL for all four feature groups. The Feature Store now holds the first two feature groups with live data and refreshes them on a scheduled (skeleton) basis. A reusable, hands-off training-pipeline template runs end to end, and at least three algorithms have been compared to select the strongest model. The team also gains operational visibility (centralized logs, alarms, alerting with pharma-grade log retention) and a dedicated staging environment for safer pre-production validation.

**3.6.1 — KDA Discovery document, full (Document):**  *(Moved from 2.6.3)*

All outputs documented including feature catalog, business rules, success criteria, and integration requirements; source SQL drafted for all four (4) Feature Groups.

Acceptance Criteria:
  1. KDA discovery completion document reviewed and accepted by the Customer data science team
  2. Source SQL for all four (4) Feature Groups drafted and validated against live Snowflake data

**3.6.2 — Feature Store: four KDA Feature Groups (Code):**

Define and document four KDA Feature Groups; first two (2) ingested; weekly refresh pipeline skeleton running. Feature-onboarding gates (schema/data-quality validation, required metadata/documentation, and onboarding approval) added to the YAML intake path (deferred from 1.6.6).

Acceptance Criteria:
  1. Four (4) KDA Feature Group schemas defined and registered in feature store; 2 groups populated with live data
  2. Weekly refresh executing on schedule demonstrated
  3. Onboarding gates enforced — a feature failing schema/data-quality validation or missing required metadata is blocked from onboarding

**3.6.3 — ML Pipeline template and deploy (Code):**

SageMaker Pipelines and Step Functions skeleton running.

Acceptance Criteria:
  1. ML Pipeline template executing a sample training job end-to-end in SageMaker Pipelines without manual intervention

**3.6.4 — KDA features ingested (Code):**

Source SQL validated against live Snowflake data; first two (2) Feature Groups ingested to feature store.

Acceptance Criteria:
  1. Source SQL for four (4) KDA Feature Groups validated against live Snowflake data with no data type or join errors
  2. Two groups in feature store with freshness check passing

**3.6.5 — KDA model initial experiments (Technical Solution):**

Develop initial experiments running in SageMaker; algorithm comparison underway.

Acceptance Criteria:
  1. At least three (3) algorithm candidates trained in SageMaker with evaluation metrics (AUC, precision, recall) logged to SageMaker Experiments / Model Registry; comparison summary produced

**3.6.6 — Observability backbone (Code):**  *(Added — supporting; off the MVP critical path)*

Logging, SNS alerting, alarms, and log archival with pharma log retention.

Acceptance Criteria:
  1. Centralized logs, alarms, and SNS alerting operational across provisioned services

**3.6.7 — Staging environment provisioned (Code):**  *(Placeholder — deferred from 1.6.1)*

Staging environment stood up via IaC/CDK, mirroring the dev environment (VPC, subnets, security groups, CI/CD promotion path). Deferred out of 1.6.1, which is dev-only for the MVP.

Acceptance Criteria:
  1. Staging environment IaC templates successfully deploying
  2. CI/CD promoting a sample deployment dev → staging without errors

### Milestone 3.6 — trade-offs vs. initial plan

| Item | Change vs. initial plan | Business value targeted | Real business value (DS / ops impact) |
|---|---|---|---|
| 3.6.1 Discovery document (full, 4 groups) | **Moved later** from 2.6.3 | Full four-group discovery is beyond the single-model MVP; sequencing it here protects the August date without dropping scope | **Complete blueprint that cuts downstream rework and rework-driven maintenance** |
| 3.6.2 / 3.6.4 Four feature groups | **Deferred** from initial 2.6 (was "Feature Store expansion": second feature group + weekly-refresh skeleton on schedule + freshness check). Refresh skeleton on schedule here → all 4 groups + freshness checks in 4.6.1 → **freshness gating in 5.6.1**. **Onboarding gates moved here from 1.6.6.** | Expands from the MVP's single labeled dataset to the full KDA feature set; the MVP itself needs only one feature group (1.6.6) | **A discoverable, reusable feature library — future models are assembled from existing features instead of re-engineered, and richer signals improve accuracy; onboarding gates keep feature quality consistent** |
| 3.6.3 ML Pipeline template (no manual intervention) | Kept post-MVP. Template runs unattended here → **fully automated + freshness-gated in 5.6.5** (conditional registration in 5.6.1) | Begins the training-pipeline automation deferred from the manual MVP pipeline (2.6.2) | **A reusable pipeline template gives every future model a consistent, repeatable build path and saves setup time per model** |
| 3.6.5 Algorithm comparison (≥3 candidates) | Kept post-MVP | Model-quality improvement beyond the MVP's single trained model | **A consistent selection method yields a better-performing model — better predictions, better field decisions** |
| 3.6.6 Observability backbone | **Added** — not in initial plan (supporting, off critical path) | Operational visibility and pharma-compliant log retention; not on the MVP critical path, so sequenced after the August MVP | **Faster detection and resolution of issues (less downtime) and pharma-compliant log retention for audit** |
| 3.6.7 Staging environment (placeholder) | **Deferred** from 1.6.1 (dev-only for the MVP) | Adds a staging tier for pre-prod validation once the MVP is proven; not required for the three August expectations | **A pre-prod safety tier that catches issues before they hit production — fewer incidents, safer releases** |

---

## Milestone 4.6 (Oct 2026) Deliverables and Acceptance Criteria — Post-MVP: feature store completion and KG integration

> **In business terms — what's in place after this milestone:** The Feature Store is complete — all four KDA feature groups are loaded and refresh automatically every week with data-freshness checks, removing manual data-prep effort. The knowledge graph (Neptune) comes online for discovery: every feature and model is automatically recorded as a linked, queryable node tied back to its registry entry, giving traceable lineage from data to model.

**4.6.1 — Feature store: ingest all four KDA Feature Groups (Code):**  *(Deferred — post August-MVP)*

All four (4) KDA Feature Groups ingested as defined; weekly refresh pipeline with data-freshness checks.

Note: Automated weekly scheduling is post August-MVP — the MVP runs ingestion manually (see 1.6.6).

Acceptance Criteria:
  1. All four (4) KDA Feature Groups in feature store with online and offline stores
  2. Weekly refresh executing without error
  3. Data-freshness checks passing

**4.6.2 — KG Integration: feature and model nodes written to Neptune (Technical Solution):**  *(Deferred — post August-MVP)*

Feature nodes and model nodes written to Neptune on registration.

Note: Dependency on both SL and AR workstreams. Not on the MVP critical path — training, registration, scoring, and evaluation do not require Neptune graph discovery.

Acceptance Criteria:
  1. Feature nodes and model nodes queryable in Neptune via SPARQL
  2. Model node linked to corresponding SageMaker Model Registry model package ARN

### Milestone 4.6 — trade-offs vs. initial plan

| Item | Change vs. initial plan | Business value targeted | Real business value (DS / ops impact) |
|---|---|---|---|
| 4.6.1 Automated weekly refresh (all 4 groups) | **Automated scheduling deferred** from 1.6.6 (MVP runs ingestion manually). Skeleton on schedule in 3.6.2 → all 4 groups + freshness checks here (4.6.1) → **freshness gating completes in 5.6.1** | Removes operational toil once the MVP is proven; not required for the three August expectations | **Eliminates recurring manual refresh work (ongoing DS time saved) and keeps features fresh so models always score on current data** |
| 4.6.2 Neptune KG integration | **Deferred — post August-MVP.** Feature/model nodes on registration here (4.6.2) → **insight nodes + traversal complete in 5.6.4** | Graph discovery is not needed to train/score/evaluate; deferring protects the August date and de-risks the SL-workstream dependency | **Feature and model discoverability with end-to-end lineage — enables reuse, impact analysis ("what breaks if this feature changes?"), and governance** |

---

## Milestone 5.6 (Nov 2026) Deliverables and Acceptance Criteria — Post-MVP: automation, serving, monitoring, governance

> **In business terms — what's in place after this milestone:** The platform now runs itself with oversight. Model training is fully automated on a schedule and only runs when data is fresh, with quality-gated registration. The model is served as an auto-scaling endpoint and exposed as an agent-callable tool, and is monitored daily for drift with an automatic retraining trigger. The knowledge graph is complete (feature, model, and insight nodes, with territory-to-insight traversal), weekly batch scoring writes results back to Snowflake and Neptune, and human-review workflows (entity resolution, model approval, feature validation) are operational — with the first model approved and promoted toward production.

**5.6.1 — ML pipelines complete (Code):**  *(Deferred — post August-MVP)*

Conditional registration, Neptune sync, Collate sync, data-freshness gating operational.

Acceptance Criteria:
  1. ML pipeline executing all steps including conditional registration
  2. Neptune and Collate sync confirmed after each successful run
  3. Data-freshness gate blocking the pipeline when data is stale

**5.6.2 — Model serving with MCP integration (Technical Solution):**  *(Productised serving deferred — post August-MVP; August provides only a registered-model endpoint for Customer-run eval in 2.6.4)*

Deploy KDA model to SageMaker endpoint; registered as MCP tool predict_territory_drivers; auto-scaling configured.

Acceptance Criteria:
  1. KDA SageMaker endpoint returning predictions at p95 under ten (10) concurrent requests; MCP tool registration confirmed; auto-scaling adding capacity when concurrent requests exceed threshold

**5.6.3 — Model monitoring (Technical Solution):**  *(Deferred — post August-MVP)*

Set model monitor baseline; daily drift detection running; CloudWatch dashboard live; drift-to-retraining trigger wired.

Acceptance Criteria:
  1. Model monitor baseline established; daily drift detection executing on schedule; CloudWatch dashboard displaying data drift and model quality metrics; retraining trigger firing on a simulated drift event

**5.6.4 — KG Integration completed (Technical Solution):**  *(Deferred — post August-MVP)*

Feature, model, and KDA insight nodes in Neptune with SPARQL discoverability.

Note: Dependency on both SL and AR workstreams

Acceptance Criteria:
  1. Feature, model, and KDA insight nodes all queryable in Neptune via SPARQL
  2. Traversal from territory node to KDA insight node returning correct results documented

**5.6.5 — KDA training pipeline fully automated (Code):**  *(Deferred — post August-MVP; MVP training is manual/Dev per 2.6.2)*

Fully automated and data-freshness-gated.

Acceptance Criteria:
  1. KDA training pipeline executing automatically on schedule (90–120 day cadence) and when triggered by the data-freshness gate

**5.6.6 — KDA Batch Scoring (Code):**  *(Deferred — post August-MVP)*

All territories scored; results in Snowflake Gold Layer and Neptune; weekly schedule running.

Acceptance Criteria:
  1. At least 50% of territories scored in a batch scoring run
  2. Results written to Snowflake Gold Layer and Neptune
  3. Weekly schedule confirmed via Step Functions

**5.6.7 — KDA Model Approval (Technical Solution):**  *(Deferred — post August-MVP; Dev-only happy path for the MVP)*

HITL workflow tested; first KDA model approved and deployed through the approval process.

Acceptance Criteria:
  1. First KDA model approved through the HITL workflow end-to-end
  2. Approval recorded in SageMaker Model Registry with reviewer identity and timestamp completed
  3. Model package status set to Approved after approval, and promoted to the production model package group

**5.6.8 — HITL Framework (Technical Solution):**  *(Deferred — post August-MVP; Dev-only happy path for the MVP)*

Entity resolution, model approval, and feature validation UIs operational; notifications configured.

Acceptance Criteria:
  1. Entity resolution, model approval, and feature validation UIs accessible and functional for at least two (2) test users
  2. Email/Slack notifications triggering on HITL queue events

### Milestone 5.6 — trade-offs vs. initial plan

| Item | Change vs. initial plan | Business value targeted | Real business value (DS / ops impact) |
|---|---|---|---|
| 5.6.1 Pipelines complete (conditional registration, sync, gating) | **Deferred** from 2.6.2 (MVP pipeline is manual). Completes the automation begun as a template in 3.6.3 | Full automation/governance of the pipeline, delivered after the MVP proves the flow | **Automated quality gate + catalog sync — consistent model quality, always-current lineage, and far less manual pipeline maintenance** |
| 5.6.2 MCP tool / AgentCore Gateway serving | **Productised serving deferred** — post August-MVP. August provides only a registered-model endpoint for Customer-run eval/scoring (2.6.4); this adds the MCP tool, auto-scaling, and hardened serving | Agent-invocable, auto-scaling serving adds value later; the Aug endpoint already lets the Customer score new data for their evaluation | **Predictions become usable on demand by apps and agents; auto-scaling sustains responsiveness without manual capacity management** |
| 5.6.3 Model monitoring / drift | **Deferred** — post August-MVP. Baseline + daily drift + retrain trigger here (5.6.3) → **fully operational (7-day run) in 6.6.2** | Drift automation is not required for the three expectations | **Automatically catches model decay — sustained accuracy, less manual monitoring, and lower risk of silent failures in production** |
| 5.6.4 Neptune KG integration completed | **Deferred** — post August-MVP. Continues from 4.6.2 (feature/model nodes) → adds insight nodes + traversal here | Discovery/catalog querying is not on the MVP critical path; deferring protects the date | **Territory insights become discoverable and traversable — field teams find the "why" behind a number, and insights are reusable across domains** |
| 5.6.5 Fully automated training | **Deferred** from 2.6.2 (MVP training is manual/Dev). Continues from the no-manual-intervention template in 3.6.3 → scheduled + freshness-gated here | Scheduling/automation is toil-reduction, delivered after the MVP proves the pipeline | **Hands-off scheduled retraining — large recurring DS time saving, consistent repeatable retrains, and reduced production maintenance** |
| 5.6.6 Automated batch scoring | **Deferred** — post August-MVP. August provides only an endpoint for Customer-run eval/scoring (2.6.4); the productised at-scale batch scoring is delivered here — 50% on weekly schedule (5.6.6) → **100% over 2 consecutive weeks in 6.6.3** | Full-population scheduled scoring, delivered after the MVP proves single-run scoring | **Predictions delivered to Snowflake/Neptune on schedule at full scale — usability across all territories without anyone running jobs by hand** |
| 5.6.7 / 5.6.8 HITL approval + framework | **Deferred** — post August-MVP (Dev-only happy path). Approval + promotion (5.6.7) and HITL UIs (5.6.8) → **all review points production-ready in 6.6.4** | Governance gates add production control; the August MVP is Dev-only | **Governed, auditable model promotion — compliance and controlled quality for models entering production** |

---

## Milestone 6.6 (Dec 2026) Deliverables and Acceptance Criteria — Production hardening

*Post August-MVP: production hardening and full-scale operations. No MVP reordering applied to this milestone.*

> **In business terms — what's in place after this milestone:** The solution is production-ready and handed over. The production environment is deployed with monitoring, alerting, and cost tracking; drift detection has run cleanly for a week; and batch scoring reliably covers 100% of territories on a weekly schedule. Human-review governance is fully production-ready, the final demo shows live KDA predictions answered in natural language, and the customer receives full documentation, runbooks, and training to operate independently — including integration of KDA insights into the Field Orchestration application.

**6.6.1 — Infrastructure complete (Technical Solution):**

Deploy production environment, monitoring and alerting active, cost tracking configured.

Acceptance Criteria:
  1. Production environment infrastructure deployed
  2. CloudWatch alarms configured
  3. Cost tracking tags applied
  4. Alerting triggering on a simulated threshold breach

**6.6.2 — Completed Model Monitoring (Technical Solution):**

Daily drift detection, CloudWatch dashboards, retraining trigger fully operational.

Acceptance Criteria:
  1. Daily drift detection running without error for at least seven (7) consecutive days
  2. Retraining trigger demonstrated on a simulated drift event
  3. Dashboards accessible to Customer team

**6.6.3 — KDA Batch Scoring (Code):**

All territories scored weekly; results in Snowflake Gold Layer and Neptune.

Acceptance Criteria:
  1. 100% of territories scored in at least two (2) consecutive weekly batch scoring runs
  2. Results queryable in Snowflake Gold Layer and Neptune
  3. Scheduling confirmed via Step Functions execution logs

**6.6.4 — HITL Framework: all review points operational (Technical Solution):**

Entity resolution, model approval, feature validation workflows production-ready.

Acceptance Criteria:
  1. All HITL review points demonstrated end-to-end with at least one (1) approved and one rejected item per workflow type
  2. Notifications delivered and reviewer decisions recorded in the audit log

**6.6.5 — Milestone 2 Demo (Technical Solution):**

Delivered per agreed demo script, demonstrating territory drivers queryable in natural language chat with live KDA predictions.

Acceptance Criteria:
  1. Milestone 2 Demo conducted with Customer stakeholders demonstrating all key demo items
  2. Demo recorded and summary document delivered

**6.6.6 — Handover documentation package (Technical Solution):**

Architecture documentation, operational runbooks, data model documentation, knowledge transfer sessions completed.

Acceptance Criteria:
  1. Architecture documentation, operational runbooks, and data model documentation delivered; at least two (2) knowledge transfer sessions conducted
  2. Customer team able to perform a sample operational task independently

**6.6.7 — Field Orchestration Integration (Technical Solution):**

KDA insight nodes queryable by Field Orchestration application; traversal working.

Acceptance Criteria:
  1. At least three (3) territory-level queries via the Field Orchestration application returning KDA insight nodes and recommended actions via SPARQL traversal
  2. Integration confirmed operational
