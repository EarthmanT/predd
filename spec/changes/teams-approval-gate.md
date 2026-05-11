# MCP Server: Teams Approval Gate

## Overview
Local MCP server running on developer laptop that routes agent approval requests to Microsoft Teams, collects human approval or denial via interactive message buttons, and returns the decision back to the requesting agent.

## Goals
- Unblock agents waiting for approval when developer is away from desk
- Enable approval decisions via Teams mobile app from anywhere
- Maintain single point of control for all agent approval flows
- Zero external infrastructure or company security review overhead

## Non-Goals
- Batching or queuing approval requests
- Audit logging or compliance reporting
- Multi-user approval workflows
- Integration with other approval systems

## Constraints
- Runs only on developer's laptop
- Only accessible to agents running on the same machine
- Requires Teams webhook URL configured locally
- Approval responses must arrive within configurable timeout window
- No persistent storage of approval history

## Architecture

### Components
1. **MCP Server** — Local process exposing approval tool to Claude Code and Windsurf
2. **Teams Bot Integration** — Sends approval requests to Teams channel or bot DM via webhook
3. **Request Tracker** — In-memory map of pending approval requests with IDs and timestamps
4. **Response Handler** — Webhook receiver that catches approval or denial responses from Teams

### Data Flow
1. Agent calls `request_approval` tool with request ID, description, and risk level
2. MCP server generates unique request ID, stores in tracker with timestamp
3. Server posts formatted message to Teams with approve/deny buttons
4. Human receives notification on Teams mobile app
5. Human taps approve or deny button
6. Teams bot webhook fires response back to server
7. Server updates tracker, returns decision to agent
8. Agent proceeds or halts based on response
9. Request cleaned from tracker after timeout or response

### Interfaces

#### Tool: `request_approval`
**Input:**
- `request_id` (string): Unique identifier for this request
- `description` (string): What the agent wants to do
- `risk_level` (enum): `low`, `medium`, `high`

**Output:**
- `approved` (boolean): True if human approved, false if denied
- `timestamp` (ISO 8601): When decision was made
- `error` (string, optional): Timeout or system error

**Timeout:** 5 minutes by default, configurable

#### Webhook: Teams Bot Response
**Endpoint:** `http://localhost:{port}/approval-response`
**Method:** POST
**Payload:**
```json
{
  "request_id": "...",
  "decision": "approve" | "deny",
  "timestamp": "<ISO 8601>"
}
```

## Implementation Phases

### Phase 1: Core MCP Server + In-Memory Tracker
- Create MCP server exposing `request_approval` tool
- Build in-memory request tracker with timeout mechanism
- Implement basic request/response matching

### Phase 2: Teams Integration
- Configure Teams bot webhook
- Build message formatter with approve/deny buttons
- Implement Teams webhook receiver
- Test end-to-end approval flow

### Phase 3: Polish + Tooling
- Add request ID generation
- Implement timeout handling
- Add logging for debugging
- Document Teams bot setup for local testing

## Security Assumptions
- Server runs only on developer's trusted machine
- Agents running on same machine are trusted
- Teams webhook URL stored in local env file, not committed to repo
- No external network exposure
- Human is sole approver for all requests

## Configuration
- Teams webhook URL (environment variable)
- MCP server port (default: 3001)
- Approval timeout in seconds (default: 300)
- Risk level thresholds (optional, for future filtering)

## Success Criteria
1. Agent can call `request_approval` and receive approval within 30 seconds of human response
2. Approval message appears in Teams with working buttons
3. Timeout correctly rejects requests after 5 minutes
4. Server handles multiple concurrent approval requests
5. Human can approve from Teams mobile app
