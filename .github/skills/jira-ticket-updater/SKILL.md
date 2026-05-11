---
name: jira-ticket-updater
description: "Update Jira tickets with implementation details after code changes. Use when: user has finished implementing a feature/fix and wants to update the Jira ticket with summary of changes, files modified, testing notes, and transition the ticket status. Also use to add comments, update description, or log work on a Jira issue."
argument-hint: "Provide the Jira ticket key (e.g., PROJ-123) and optionally the cloud ID"
---

# Jira Ticket Updater

Update a Jira ticket with complete implementation details after code changes are made. Adds structured comments, updates fields, and transitions the ticket status.

## When to Use

- User has finished implementing changes for a Jira ticket
- User says "update the ticket" or "update PROJ-123 with the changes"
- User wants to add implementation notes to a Jira issue
- User wants to transition a ticket (e.g., move to "In Review", "Done")
- After using `/jira-ticket-analyzer` and completing implementation

## Procedure

### Step 1 — Gather Implementation Context

1. Identify the Jira issue key from the user's message or conversation history.
2. If no cloud ID is available, use `mcp_com_atlassian_getAccessibleAtlassianResources` to discover available sites.
3. Review what was done in the current session:
   - Files created or modified
   - What features/fixes were implemented
   - Any deviations from the original ticket requirements
   - Known limitations or follow-up items

### Step 2 — Fetch Current Ticket State

1. Use `mcp_com_atlassian_getJiraIssue` to get the current ticket details:
   - `issueIdOrKey`: the ticket key
   - `cloudId`: the site cloud ID
   - `responseContentFormat`: `"markdown"`
2. Note the current status, description, and existing comments to avoid duplicating information.

### Step 3 — Build the Update

Prepare a structured implementation summary with these sections:

```
## Implementation Summary

### Changes Made
- [List each change with the file path and what was done]
- e.g., `src/api/users.ts` — Added new endpoint for user profile updates

### Files Modified
- [Bulleted list of all files created, modified, or deleted]

### Acceptance Criteria Status
- [x] Criteria 1 — Met (brief explanation)
- [x] Criteria 2 — Met
- [ ] Criteria 3 — Not applicable / Deferred (reason)

### Testing Notes
- [What was tested and how]
- [Any manual testing steps for reviewers]

### Edge Cases Handled
- [List edge cases that were addressed]

### Known Limitations / Follow-ups
- [Any items deferred or out of scope]
- [Suggested follow-up tickets if needed]
```

### Step 4 — Update the Ticket

Perform updates in this order:

#### 4a. Add Implementation Comment
Use `mcp_com_atlassian_addCommentToJiraIssue`:
- `issueIdOrKey`: the ticket key
- `cloudId`: the site cloud ID
- `commentBody`: the structured implementation summary from Step 3
- `contentFormat`: `"markdown"`

#### 4b. Update Ticket Fields (if needed)
Use `mcp_com_atlassian_editJiraIssue` to update fields if appropriate:
- Update the description to add implementation details (append, don't overwrite)
- Update labels (e.g., add "implemented", "needs-review")
- Update any custom fields relevant to the workflow

#### 4c. Transition the Ticket Status (if requested)
If the user wants to move the ticket to a new status:
1. Use `mcp_com_atlassian_getTransitionsForJiraIssue` to get available transitions.
2. Present the available transitions to the user and ask which one to apply.
3. Use `mcp_com_atlassian_transitionJiraIssue` with the selected transition ID.

### Step 5 — Confirm Updates

1. Summarize all updates made to the ticket.
2. Provide the ticket key for easy reference.
3. If any updates failed, report the error and suggest manual steps.

## Guidelines

- **Append, don't overwrite**: When updating descriptions, preserve existing content and append new sections.
- **Be concise but complete**: Include enough detail for reviewers to understand the changes without reading every line of code.
- **Use markdown formatting**: Structure comments with headers, lists, and code blocks for readability.
- **Ask before transitioning**: Always confirm with the user before changing ticket status.
- **Include file paths**: Reference specific files so reviewers can find the changes easily.
- **Flag deviations**: If the implementation differs from the ticket requirements, explain why.
