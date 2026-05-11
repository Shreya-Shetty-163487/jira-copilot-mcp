---
name: jira-ticket-analyzer
description: "Analyze Jira tickets for completeness before implementing code changes. Use when: user asks to implement a feature, fix a bug, or make updates based on a Jira ticket. Checks acceptance criteria, requirements clarity, design specs, and edge cases. Asks follow-up questions if ticket is incomplete, then proceeds with implementation."
argument-hint: "Provide the Jira ticket key (e.g., PROJ-123) and optionally the cloud ID"
---

# Jira Ticket Analyzer & Implementation Gate

Analyze a Jira ticket for completeness and clarity before starting any code changes. If the ticket has sufficient detail, proceed with implementation. If not, ask targeted follow-up questions to fill gaps.

## When to Use

- User asks to implement a feature or story from a Jira ticket
- User asks to fix a bug referenced by a Jira issue key
- User says "implement PROJ-123" or "work on this ticket"
- User provides a Jira issue key and asks for code changes

## Procedure

### Step 1 — Extract Ticket Information

1. Identify the Jira issue key from the user's message (e.g., `PROJ-123`).
2. If no cloud ID is provided, use `mcp_com_atlassian_getAccessibleAtlassianResources` to discover available sites and ask the user to confirm which one.
3. Fetch the full ticket using `mcp_com_atlassian_getJiraIssue` with these parameters:
   - `issueIdOrKey`: the extracted key
   - `cloudId`: the site cloud ID
   - `responseContentFormat`: `"markdown"`
   - Request all relevant fields: summary, description, status, issuetype, priority, acceptance criteria, labels, components, attachments, comments, linked issues.

### Step 2 — Analyze Ticket Completeness

Evaluate the ticket against this **completeness checklist**:

| Criteria | Required? | What to Check |
|----------|-----------|---------------|
| **Summary** | Yes | Clear, specific title describing the work |
| **Description** | Yes | Detailed explanation of what needs to be done |
| **Acceptance Criteria** | Yes | Measurable conditions for "done" (may be in description or a custom field) |
| **Issue Type** | Yes | Correctly categorized (Bug, Story, Task, etc.) |
| **Priority** | Recommended | Set appropriately |
| **Affected Components** | Recommended | Which parts of the codebase are involved |
| **Edge Cases** | Recommended | Error scenarios, boundary conditions mentioned |
| **Dependencies** | If applicable | Linked issues, blockers, or prerequisites identified |
| **UI/UX Specs** | If UI work | Mockups, wireframes, or design references |
| **API Contract** | If API work | Request/response formats, endpoints, status codes |
| **Data Changes** | If DB work | Schema changes, migrations, data transformations |

### Step 3 — Decision Gate

**If the ticket is COMPLETE (all required criteria met + enough context to implement):**

1. Summarize what you understand the ticket requires.
2. Present your implementation plan to the user:
   - Files to create or modify
   - High-level approach
   - Any assumptions you're making
3. Ask for a quick confirmation before proceeding.
4. Begin implementation.

**If the ticket is INCOMPLETE or AMBIGUOUS:**

1. List exactly what information is missing or unclear.
2. Ask the user targeted follow-up questions. Examples:
   - "The acceptance criteria don't specify behavior when [X] is empty. Should it show an error or a default value?"
   - "The ticket mentions a new API endpoint but doesn't define the request/response format. What fields should be included?"
   - "There are no edge cases described. Should I handle [specific scenario]?"
3. Do NOT start coding until the gaps are resolved.
4. Once the user answers, re-evaluate completeness and proceed if satisfied.

### Step 4 — Explore the Codebase

Before writing code:
1. Use the explore_subagent or search tools to understand the existing codebase structure.
2. Identify relevant files, patterns, and conventions already in use.
3. Check for existing similar implementations to follow the same patterns.

### Step 5 — Implement

1. Follow the project's existing code conventions and patterns.
2. Implement the changes as described in the ticket and confirmed by the user.
3. Write code that satisfies all acceptance criteria.
4. Handle edge cases identified in the ticket or during analysis.

### Step 6 — Hand Off to Ticket Updater

After implementation is complete, remind the user they can use `/jira-ticket-updater` to update the Jira ticket with implementation details.

## Quality Gates

- Never start implementation with ambiguous requirements
- Always confirm your understanding with the user before coding
- If the ticket contradicts the existing codebase, flag it
- If implementation reveals new edge cases not in the ticket, mention them
