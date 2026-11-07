from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from app.intelligence import retrieve_runbooks


class InvestigationState(TypedDict):
    title: str
    kind: str
    service: str
    evidence: list
    runbooks: list[dict]
    logs: list[dict]
    deployments: list[dict]
    incident_started_at: str
    matches: list[dict]
    report: dict


def _retrieve(state: InvestigationState):
    query = " ".join([state["title"], state["kind"], *(str(item) for item in state["evidence"])])
    return {"matches": retrieve_runbooks(query, state["runbooks"])}


def _draft(state: InvestigationState):
    matches = state["matches"]
    incident_time = state["incident_started_at"]
    recent_deployments = [item for item in state["deployments"]
                          if item["deployed_at"] <= incident_time and item["minutes_before"] <= 15]
    strongest_log = state["logs"][0] if state["logs"] else None
    clues = []
    if recent_deployments:
        deployment = recent_deployments[0]
        clues.append(f"Deployment {deployment['version']} ({deployment['commit_sha'] or 'no commit id'}) occurred {deployment['minutes_before']} minutes before the incident. This is a time correlation, not proof of cause.")
    if strongest_log:
        clues.append(f"Recent {strongest_log['level']} log {strongest_log['error_type'] or ''}: {strongest_log['message']}")
    report = {
        "summary": f"{state['title']} for {state['service']}.",
        "evidence": state["evidence"],
        "deployment_events": state["deployments"],
        "application_errors": state["logs"],
        "correlated_clues": clues,
        "runbook_matches": matches,
        "sources_checked": ["recent telemetry", "sanitized application logs", "service deployment events", "workspace runbooks"],
        "sources_unavailable": ["automatic CI/CD provider sync", "external log provider sync"],
        "recommendation": matches[0]["excerpt"] if matches else (clues[0] if clues else "Review the cited telemetry and add a matching runbook with service-specific recovery steps."),
        "human_approval_required": True,
        "limitations": "Evidence is ingested through service-key endpoints. Runbook retrieval uses tenant-scoped keyword matching. Deployment timing and log matches are clues, not causal findings; no LLM is configured.",
    }
    return {"report": report}


_builder = StateGraph(InvestigationState)
_builder.add_node("retrieve_runbooks", _retrieve)
_builder.add_node("draft_evidence_report", _draft)
_builder.add_edge(START, "retrieve_runbooks")
_builder.add_edge("retrieve_runbooks", "draft_evidence_report")
_builder.add_edge("draft_evidence_report", END)
investigation_graph = _builder.compile()


def investigate(*, title: str, kind: str, service: str, evidence: list, runbooks: list[dict],
                logs: list[dict] | None = None, deployments: list[dict] | None = None,
                incident_started_at: str = "") -> dict:
    state = investigation_graph.invoke({"title": title, "kind": kind, "service": service,
                                        "evidence": evidence, "runbooks": runbooks,
                                        "logs": logs or [], "deployments": deployments or [],
                                        "incident_started_at": incident_started_at,
                                        "matches": [], "report": {}})
    return state["report"]
