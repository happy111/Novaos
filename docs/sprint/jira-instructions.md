# Jira Ticket Creation — NTU Project Standards

Standing field template for creating new stories/tasks in the **NTU** (NovaOS Transformation US) Jira project, modeled on [NTU-50](https://novartisdevelop.atlassian.net/browse/NTU-50). Apply this template whenever creating new NTU stories/tasks, unless told otherwise.

## Fixed field defaults

| Field (display name) | Field ID | Value |
|---|---|---|
| Team | `customfield_10112` | Analytics_Rewiring (AR). **Subtasks only:** do NOT set this field — Jira rejects it with "Issue is a subtask, and inherits the team assignment from its parent." Only set Team on Stories/Epics. |
| Specification Category | `customfield_11704` | Technical |
| Implementation Method | `customfield_11657` | Configured |
| GxP Process Criticality | `customfield_11655` | GxP-None |
| Test Approach | `customfield_11702` | Adhoc |
| Requirement Category | `customfield_11703` | Others (default value) |
| Labels | `labels` | `["ARFoundation"]` — apply to every Story, Epic, and Subtask created for AR sprint work (confirm with the user if a ticket falls outside AR sprint scope). Set at creation via `additional_fields='{"labels": ["ARFoundation"]}'` or after creation via `jira_update_issue(fields='{"labels": ["ARFoundation"]}')`. |

## Fields to look up fresh each time

| Field (display name) | Field ID | How to resolve |
|---|---|---|
| Sprint | `customfield_10115` | Look up the current numbered "Ar Sprint #N" sprint via `jira_get_sprints_from_board` on board `44546` ("NovaOS AR - Analytics Rewiring"). Do not hardcode a sprint number — it changes over time. As of 2026-08-28, the active sprint is **"Ar Sprint #5"** (sprint id `95167`, active, 2026-08-26 to 2026-09-07). **Sprint 6 onward:** do not set this field / do not assign the ticket to a sprint at creation — leave new tickets in the backlog (use `jira_move_issues_to_backlog` if one lands in a sprint by accident). This is a deliberate change from the Sprint 5 behavior above; only Sprint 5 tickets were sprint-assigned. |

## Field format gotchas

- **Acceptance Criteria** (`customfield_11401`) is a **required** field on both Epic and Story create screens, and it takes **ADF**, not plain text. Passing a plain string throws `The field value is not valid Atlassian Document Format (ADF) content.` Pass it as:
  ```json
  {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "..."}]}]}
  ```
  Combine with the CRITICAL Confluence-pointer rule below: the AC text itself should be a pointer sentence, not restated criteria.
- **Team** (`customfield_10112`) takes a **bare UUID string** for this project's AR team, not `{"value": ...}` or `{"id": ...}`— e.g. `"de74f9d9-8434-4630-a554-38f98770a254"`. Read the value off an existing AR story (e.g. via `jira_get_issue(fields="*all")`) rather than guessing/fabricating a UUID.
- **Epics do not carry a Team value** in this project (existing AR epics have `customfield_10112` = null). Only set Team on Stories, matching the Fixed field defaults note above.
- **Priority cannot be left blank.** The NTU project applies a default of "Low" automatically when priority is omitted at creation, and `jira_update_issue` rejects an explicit null (`Specify the Priority (id or name) in the string format`). If the source markdown/Confluence leaves Priority blank, the closest faithful outcome in Jira is the default "Low" — flag this to the user rather than silently treating it as a match.
- **Issue type name at creation vs. readback**: pass `issue_type="Subtask"` when creating; Jira reports the created issue's type back as `"Sub-task"`. Both refer to the same type.
- **Jira's text search index splits on hyphens.** `summary ~ "NOVAOS-AR-19"` (or even `summary ~ "NOVAOS-AR"`) can return zero results even when matching issues exist — this is a false-absence signal, not proof the ticket doesn't exist. To check whether an Epic/Story already exists, query structurally instead, e.g. `project = NTU AND issuetype = Epic AND parent = NTU-56`.

## Fields to ask the user for, per ticket

These vary per ticket and must **not** be defaulted — ask before creating the issue:

- **Story Points** (`customfield_10117`) — for Subtasks, leave empty unless the user specifies a value
- **Priority** (standard field) — for Subtasks, default to the parent Story's priority (confirm with the user); for Stories, always ask
- **Parent** — see Parent linkage below; always ask which parent to use, never assume

## Parent linkage

Every Story and Subtask must be linked to its parent. The mechanism differs by issue type:

| Issue type | Parent relationship | How to set it |
|---|---|---|
| **Story** | Parent **Epic** (e.g. NTU-50's parent was NTU-45, "AR - Model Registry and Approval") | Set the Epic Link field (`customfield_10005`) at creation via `jira_create_issue(..., additional_fields='{"epicKey": "NTU-45"}')`, or after creation via `jira_link_to_epic(issue_key=..., epic_key=...)`. |
| **Subtask** | Parent **Story** | Set the native `parent` field at creation via `jira_create_issue(issue_type="Subtask", ..., additional_fields='{"parent": "NTU-123"}')`. This is a structural parent-child relationship, not an issue link. |

Always ask the user which Epic (for a Story) or Story (for a Subtask) to link to — never assume or guess a parent.

## Subtask assignee

Subtasks default to the parent Story's assignee (use `jira_get_issue` on the parent to look it up, then `jira_assign_issue` on the new subtask — assignee is not settable at creation time reliably, so assign as a follow-up step). Confirm with the user if this default doesn't apply.

**Exception — named-reviewer subtasks.** When a subtask's own title/summary names a specific person (e.g. a per-lead approval subtask like "AR-15-i-1 - feature-intake YAML Template review and approval - Ankit"), assign it to **that named person**, not the parent Story's assignee. The parent-Story default only applies when the subtask has no named owner of its own. Getting this backwards was a real mistake caught after the fact — always re-check the subtask's own summary for a name before defaulting to the parent.

**Resolving the assignee account.** Assignee is unreliable if you guess an email — an unresolvable address fails silently and the ticket comes back Unassigned with no error. Always resolve the real account first with `jira_search_assignable_users(query="<first name>", project_key="NTU")`, then pass the returned `account_id` to `jira_assign_issue`. Do the same for subtasks that inherit the parent's assignee — assignee is not reliably settable at issue-creation time, so it always needs this follow-up call regardless of which rule (parent-default or named-reviewer) applies.

## CRITICAL: Link to Confluence, don't duplicate its content

When creating a Story/Subtask whose content originates from a Confluence sprint planning page (e.g. AR-AWS-Sprint_N), do **not** copy the Description/Acceptance Criteria/Demo Steps text into Jira. Confluence is the single source of truth for that content. Instead:

- Set the Jira **Description** to a short pointer: the item's title/summary plus a direct link to the relevant Confluence section (e.g. "Details, Acceptance Criteria, and Demo Steps: [AR-AWS-Sprint_1 § AR-00](confluence-url)").
- Leave the Jira **Acceptance Criteria** field pointing to the same Confluence link rather than re-stating the criteria.
- **Never summarize, rewrite, or reframe** the Confluence content into the Jira fields — link to it instead.
- If work requires something **beyond** what's currently in Confluence, **update the Confluence page** with that requirement — don't add it as new/different text in Jira. Jira and Confluence must not drift apart into two separate descriptions of the same story.
- Any **gaps identified while working the ticket**, and how they were closed, get logged as **comments on the Jira subtask** (not in the Description/AC fields) — this preserves a timestamped audit trail of what was found and fixed, separate from the stable Confluence spec.

## Summary naming convention

Use the story's/subtask's **full markdown heading** as the Jira summary, including the `AR-xx` prefix (e.g. `AR-12-b - SageMaker Unified Studio Enablement and Architecture Validation - Part 2`), not just the title text after the prefix. This matches how existing tickets in this project are titled and keeps the Jira summary searchable by AR ID.

## Summary field takes raw text

The `summary` parameter is stored verbatim — do not HTML-escape it. Passing `&amp;` (instead of a literal `&`) stores the literal string `&amp;` in the ticket title, which then needs a follow-up `jira_update_issue` to fix. Write summaries with plain characters (`&`, not `&amp;`).

## Verify every write by readback

Success messages from `jira_create_issue`/`jira_update_issue` confirm the call succeeded, not that the stored value is what was intended (see the ADF, Team UUID, Priority-default, and summary-escaping gotchas above — every one of them returned a "success" response with a wrong or unexpected value). Re-query changed/created issues afterward (`jira_search` or `jira_get_issue`) and compare against the source markdown/Confluence before considering the ticket done.

## Reference past Jira tickets when relevant

When a new story/subtask is derived from or reviews prior work (e.g. a "Sprint N review / gap closure" item that revisits an earlier sprint's story), check whether the original Jira ticket(s) for that prior work already exist (search by summary/labels via `jira_search`, or check the "Jira" column on the relevant `AR-AWS-Sprint_N` Confluence page). If they exist, list them in the new ticket's description as reference links (key + summary + status), so reviewers can jump straight to the original implementation, its comments, and its resolution history. Do not skip this check even if the Confluence planning page shows "none linked yet" for the new item itself — the *prior* ticket being referenced may still exist and just not be cross-linked on that page.

## Board/project reference

- Project key: `NTU`
- Relevant Agile board: `44546` — "NovaOS AR - Analytics Rewiring" (scrum)
- Other NTU boards exist (Grid Cross-Layer View, ESL Semantic Layer, ESL Ontology & SDP, Agentic Layer) — use board 44546 specifically for AR team work.

## Origin

Captured 2026-08-26 by pulling the full field set (`*all` fields) from NTU-50 via the Atlassian MCP `jira_get_issue` tool and cross-referencing sprint data via `jira_get_agile_boards` / `jira_get_sprints_from_board`. Reproduce with:

```
jira_get_issue(issue_key="NTU-50", fields="*all", use_display_names=true)
jira_get_agile_boards(project_key="NTU")
jira_get_sprints_from_board(board_id="44546", limit=50)
```
