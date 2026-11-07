import React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, ArrowUpRight, BookOpen, BrainCircuit, Check, ChevronDown, CircleHelp, Clock3, LayoutDashboard, LogOut, Radio, Settings2, ShieldCheck, TriangleAlert, Plus, Search, Copy, RotateCw, X, FileText } from "lucide-react";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

type Service = { id: string; name: string; environment: string; status: string; requests_per_minute: number; error_rate: number; p95_latency_ms: number; uptime: number; ingest_key_configured?: boolean };
type Incident = { id: string; title: string; service: string; severity: string; status: string; started_at: string };
type Dashboard = { updated_at: string; organization: { id: string; name: string }; summary: { requests_per_minute: number; error_rate: number; p95_latency_ms: number; availability: number; active_incidents: number }; traffic: { time: string; requests: number }[]; services: Service[]; incidents: Incident[] };
type Session = { user: { id: string; email: string }; organization: { id: string; name: string }; role: string };
type View = "Overview" | "Services" | "Intelligence" | "Incidents" | "Settings" | "Help & support";
type IntelligenceResult = { updated_at: string; model: string | null; model_status: string; model_metrics: Record<string, unknown>; services: { service_id: string; service: string; window_start: string; features: Record<string, number>; anomaly_score: number | null; failure_probability: number | null; latency_forecast_ms: number | null; explanations: { feature: string; contribution_log_odds: number }[] }[] };
type InvestigationReport = { id: string; incident_id: string; report: { summary: string; evidence: unknown[]; deployment_events: Record<string, unknown>[]; application_errors: Record<string, unknown>[]; correlated_clues: string[]; runbook_matches: { id: string; title: string; excerpt: string; score: number }[]; recommendation: string; human_approval_required: boolean; sources_unavailable: string[]; limitations: string } };
type AlertDestination = { id: string; name: string; provider: string; enabled: boolean; configured: boolean; created_at?: string };
type OperationsStatus = { redis: string; scheduler: string; scheduler_last_seen?: string | null; worker: string; worker_last_seen?: string | null };

async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

const getSession = () => api<Session>("/api/auth/me");
const getDashboard = (hours: number) => api<Dashboard>(`/api/dashboard?range_hours=${hours}`);
const nav: { label: View; icon: typeof LayoutDashboard }[] = [
  { label: "Overview", icon: LayoutDashboard },
  { label: "Services", icon: Radio },
  { label: "Intelligence", icon: BrainCircuit },
  { label: "Incidents", icon: TriangleAlert },
  { label: "Settings", icon: Settings2 },
  { label: "Help & support", icon: CircleHelp },
];

function App() {
  const [view, setView] = React.useState<View>("Overview");
  const [range, setRange] = React.useState(1);
  const queryClient = useQueryClient();
  const session = useQuery({ queryKey: ["session"], queryFn: getSession, retry: false });
  const dashboard = useQuery({ queryKey: ["dashboard", range], queryFn: () => getDashboard(range), enabled: !!session.data, refetchInterval: 30_000 });
  const signOut = useMutation({ mutationFn: () => api<void>("/api/auth/logout", { method: "POST" }), onSuccess: () => { queryClient.clear(); window.location.assign("/"); } });

  if (session.isLoading) return <div className="auth-loading">Loading workspace…</div>;
  if (!session.data) return <AuthScreen onAuthenticated={() => void session.refetch()} />;
  const current = session.data;
  const degraded = dashboard.data?.services.filter(service => service.status === "degraded").length ?? 0;

  return <div className="app-shell">
    <aside className="sidebar">
      <button className="brand brand-button" onClick={() => setView("Overview")}><span className="brand-mark"><Activity size={18} /></span><span>sentinel<span className="brand-period">.</span></span></button>
      <div className="workspace"><div className="workspace-icon">{current.organization.name.slice(0, 1).toUpperCase()}</div><div><strong>{current.organization.name}</strong><span>Workspace · {current.role}</span></div></div>
      <p className="nav-label">MONITOR</p>
      <nav aria-label="Main navigation">{nav.map(({ label, icon: Icon }) => <button key={label} onClick={() => setView(label)} className={`nav-item ${view === label ? "selected" : ""}`} aria-current={view === label ? "page" : undefined}><Icon size={17} /><span>{label}</span>{label === "Incidents" && dashboard.data?.summary.active_incidents ? <span className="nav-count">{dashboard.data.summary.active_incidents}</span> : null}</button>)}</nav>
      <div className="sidebar-bottom"><div className="user-card"><div className="avatar">{current.user.email.slice(0, 1).toUpperCase()}</div><div className="user-details"><strong>{current.user.email}</strong><span>{current.role}</span></div><button className="logout-button" aria-label="Sign out" title="Sign out" disabled={signOut.isPending} onClick={() => signOut.mutate()}><LogOut size={15} /></button></div></div>
    </aside>

    <main className="main-content">
      <header className="topbar"><div className="breadcrumb">{current.organization.name} <span>/</span> <strong>{view}</strong></div><div className="top-actions"><span className={`live-indicator ${degraded ? "has-issues" : ""}`}><i />{degraded ? `${degraded} service${degraded === 1 ? "" : "s"} degraded` : "Services operational"}</span><div className="top-avatar">{current.user.email.slice(0, 1).toUpperCase()}</div></div></header>
      <div className="page-wrap">
        {view === "Overview" && <Overview dashboard={dashboard.data} loading={dashboard.isLoading} error={dashboard.error} range={range} onRange={setRange} onView={setView} refreshed={dashboard.dataUpdatedAt} />}
        {view === "Services" && <ServicesPage services={dashboard.data?.services ?? []} loading={dashboard.isLoading} error={dashboard.error} onRetry={() => void dashboard.refetch()} onCreated={() => void queryClient.invalidateQueries({ queryKey: ["dashboard"] })} />}
        {view === "Intelligence" && <IntelligencePage incidents={dashboard.data?.incidents ?? []} onRefresh={() => { void queryClient.invalidateQueries({ queryKey: ["dashboard"] }); }} />}
        {view === "Incidents" && <IncidentsPage incidents={dashboard.data?.incidents ?? []} loading={dashboard.isLoading} error={dashboard.error} onRetry={() => void dashboard.refetch()} />}
        {view === "Settings" && <SettingsPage session={current} onSaved={() => { void queryClient.invalidateQueries({ queryKey: ["session"] }); void queryClient.invalidateQueries({ queryKey: ["dashboard"] }); }} />}
        {view === "Help & support" && <HelpPage />}
      </div>
    </main>
  </div>;
}

function PageHeading({ eyebrow, title, subtitle, children }: { eyebrow: string; title: string; subtitle: string; children?: React.ReactNode }) {
  return <section className="page-heading"><div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1><p className="subtitle">{subtitle}</p></div>{children}</section>;
}

function Overview({ dashboard, loading, error, range, onRange, onView, refreshed }: { dashboard?: Dashboard; loading: boolean; error: Error | null; range: number; onRange: (hours: number) => void; onView: (view: View) => void; refreshed: number }) {
  return <>
    <PageHeading eyebrow={new Date().toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" }).toUpperCase()} title="API overview" subtitle="Current health and traffic across monitored services.">
      <label className="range-select"><Clock3 size={15} /><span className="sr-only">Dashboard time range</span><select value={range} onChange={event => onRange(Number(event.target.value))}><option value={1}>Last hour</option><option value={6}>Last 6 hours</option><option value={24}>Last 24 hours</option></select><ChevronDown size={14} /></label>
    </PageHeading>
    {loading && <div className="notice">Loading dashboard…</div>}{error && <div className="notice error" role="alert">{error.message}</div>}
    {dashboard && <>
      <section className="metrics-grid" aria-label="Key metrics"><Metric title="Requests / min" value={dashboard.summary.requests_per_minute.toLocaleString()} icon={<Activity size={16} />} /><Metric title="Error rate" value={`${dashboard.summary.error_rate}%`} icon={<TriangleAlert size={16} />} /><Metric title="P95 latency" value={`${dashboard.summary.p95_latency_ms} ms`} icon={<Clock3 size={16} />} /><Metric title="Availability" value={`${dashboard.summary.availability}%`} icon={<ShieldCheck size={16} />} /></section>
      <section className="panel traffic-panel"><div className="panel-heading"><div><h2>Request volume</h2><p>Incoming requests · last {range === 1 ? "hour" : `${range} hours`}</p></div><div className="chart-legend"><i /> Requests</div></div><div className="chart-area"><ResponsiveContainer width="100%" height="100%"><AreaChart data={dashboard.traffic} margin={{ top: 8, right: 4, left: -18, bottom: 0 }}><defs><linearGradient id="trafficFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#48c7a5" stopOpacity={0.2} /><stop offset="95%" stopColor="#48c7a5" stopOpacity={0} /></linearGradient></defs><CartesianGrid stroke="#20262d" vertical={false} /><XAxis dataKey="time" axisLine={false} tickLine={false} tick={{ fill: "#a0aab3", fontSize: 11 }} dy={10} /><YAxis axisLine={false} tickLine={false} tick={{ fill: "#a0aab3", fontSize: 11 }} /><Tooltip contentStyle={{ background: "#151a20", border: "1px solid #293139", borderRadius: 8, color: "#eef2f5" }} /><Area type="monotone" dataKey="requests" stroke="#48c7a5" strokeWidth={2} fill="url(#trafficFill)" /></AreaChart></ResponsiveContainer></div></section>
      <div className="lower-grid"><section className="panel services-panel"><div className="panel-heading"><div><h2>Services</h2><p>{dashboard.services.length} monitored services</p></div><button className="text-link" onClick={() => onView("Services")}>View all <ArrowUpRight size={14} /></button></div><ServiceTable services={dashboard.services.slice(0, 5)} /></section>
      <section className="panel incidents-panel"><div className="panel-heading"><div><h2>Open incidents</h2><p>{dashboard.incidents.length} need attention</p></div><button className="text-link" onClick={() => onView("Incidents")}>View all <ArrowUpRight size={14} /></button></div><IncidentList incidents={dashboard.incidents.slice(0, 3)} /></section></div>
      <footer className="page-footer"><span>Refreshes every 30 seconds</span><span>Updated {new Date(refreshed).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span></footer>
    </>}
  </>;
}

function Metric({ title, value, icon }: { title: string; value: string; icon: React.ReactNode }) {
  return <article className="metric-card"><div className="metric-top"><span>{title}</span><span className="metric-icon">{icon}</span></div><div className="metric-value">{value}</div><div className="metric-foot">Based on the selected period</div></article>;
}

function ServiceTable({ services, onRotate }: { services: Service[]; onRotate?: (service: Service) => void }) {
  if (!services.length) return <EmptyState title="No services yet" detail="Add a service to start collecting API telemetry." />;
  return <div className="table-scroll"><table><thead><tr><th>Service</th><th>Status</th><th>Requests/min</th><th>Error rate</th><th>P95 latency</th>{onRotate && <th>Ingestion key</th>}</tr></thead><tbody>{services.map(service => <tr key={service.id}><td><div className="service-name"><span className="service-icon"><Activity size={15} /></span><span><strong>{service.name}</strong><small>{service.environment}</small></span></div></td><td><span className={`status ${service.status.replace(" ", "-")}`}><i />{service.status}</span></td><td>{service.requests_per_minute.toLocaleString()}</td><td className={service.error_rate > 1 ? "warn-text" : ""}>{service.error_rate.toFixed(2)}%</td><td>{service.p95_latency_ms} ms</td>{onRotate && <td><button className="key-action" onClick={() => onRotate(service)}><RotateCw size={13} />{service.ingest_key_configured ? "Rotate" : "Create key"}</button></td>}</tr>)}</tbody></table></div>;
}

function IncidentList({ incidents, onStatus }: { incidents: Incident[]; onStatus?: (incident: Incident, status: string) => void }) {
  if (!incidents.length) return <EmptyState title="No open incidents" detail="Incidents will appear here when detected." />;
  return <div className="incident-list">{incidents.map(incident => <article className="incident" key={incident.id}><span className={`incident-mark ${incident.severity}`}><TriangleAlert size={15} /></span><div className="incident-copy"><div className="incident-title"><strong>{incident.title}</strong><span className={`severity ${incident.severity}`}>{incident.severity}</span></div><p>{incident.service} <span>·</span> {incident.id}</p><small>{incident.status} · {new Date(incident.started_at).toLocaleString()}</small></div>{onStatus && <div className="incident-actions"><button className="key-action" onClick={() => onStatus(incident, incident.status === "investigating" ? "monitoring" : "investigating")}>{incident.status === "investigating" ? "Monitor" : "Reopen"}</button><button className="key-action" onClick={() => onStatus(incident, "resolved")}>Resolve</button></div>}</article>)}</div>;
}

function EmptyState({ title, detail }: { title: string; detail: string }) { return <div className="empty-state"><span>{title}</span><p>{detail}</p></div>; }

function ServicesPage({ services, loading, error: loadError, onRetry, onCreated }: { services: Service[]; loading: boolean; error: Error | null; onRetry: () => void; onCreated: () => void }) {
  const [search, setSearch] = React.useState("");
  const [showForm, setShowForm] = React.useState(false);
  const [name, setName] = React.useState("");
  const [environment, setEnvironment] = React.useState("production");
  const [error, setError] = React.useState("");
  const [issuedKey, setIssuedKey] = React.useState<{ key: string; service: string } | null>(null);
  const [copied, setCopied] = React.useState(false);
  const create = useMutation({ mutationFn: () => api<{ ingest_key: string }>("/api/services", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, environment }) }), onSuccess: result => { setIssuedKey({ key: result.ingest_key, service: name }); setName(""); setShowForm(false); setError(""); onCreated(); }, onError: cause => setError(cause instanceof Error ? cause.message : "Could not create service") });
  const rotate = useMutation({ mutationFn: (service: Service) => api<{ ingest_key: string }>(`/api/services/${service.id}/ingest-key`, { method: "POST" }), onSuccess: (result, service) => setIssuedKey({ key: result.ingest_key, service: service.name }), onError: cause => setError(cause instanceof Error ? cause.message : "Could not rotate ingestion key") });
  const filtered = services.filter(service => `${service.name} ${service.environment} ${service.status}`.toLowerCase().includes(search.toLowerCase()));
  return <><PageHeading eyebrow="MONITOR" title="Services" subtitle="Manage monitored APIs and inspect their current health."><button className="primary-button" onClick={() => { setShowForm(!showForm); setError(""); }}><Plus size={15} /> Add service</button></PageHeading>
    {error && !showForm && <p className="form-error" role="alert">{error}</p>}
    {issuedKey && <KeyReveal service={issuedKey.service} value={issuedKey.key} copied={copied} onCopy={async () => { await navigator.clipboard.writeText(issuedKey.key); setCopied(true); window.setTimeout(() => setCopied(false), 1800); }} onClose={() => { setIssuedKey(null); setCopied(false); }} />}
    {showForm && <form className="panel inline-form" onSubmit={event => { event.preventDefault(); setError(""); create.mutate(); }}><label>Service name<input required minLength={2} maxLength={120} value={name} onChange={event => setName(event.target.value)} placeholder="Payments API" /></label><label>Environment<select value={environment} onChange={event => setEnvironment(event.target.value)}><option>production</option><option>staging</option><option>development</option></select></label><button className="primary-button" disabled={create.isPending}>{create.isPending ? "Adding…" : "Save service"}</button><button type="button" className="secondary-button" onClick={() => setShowForm(false)}>Cancel</button>{error && <p className="form-error" role="alert">{error}</p>}</form>}
    {loading ? <div className="notice">Loading services…</div> : loadError ? <div className="notice error" role="alert">{loadError.message} <button className="retry-button" onClick={onRetry}>Retry</button></div> : <section className="panel full-panel"><div className="list-toolbar"><span>{filtered.length} {filtered.length === 1 ? "service" : "services"}</span><label className="search-box"><Search size={15} /><input aria-label="Search services" placeholder="Search services" value={search} onChange={event => setSearch(event.target.value)} /></label></div><ServiceTable services={filtered} onRotate={service => rotate.mutate(service)} /></section>}
  </>;
}

function KeyReveal({ service, value, copied, onCopy, onClose }: { service: string; value: string; copied: boolean; onCopy: () => void; onClose: () => void }) {
  const snippet = `import os\n\napp.add_middleware(\n    SentinelMiddleware,\n    service_key=os.environ["API_SENTINEL_KEY"],\n    collector_url="http://127.0.0.1:8000/api/telemetry/batch",\n)`;
  return <section className="key-reveal panel"><div className="key-reveal-heading"><div><h2>Ingestion key for {service}</h2><p>Copy it now. This key is shown only once; rotating it revokes the previous key.</p></div><button className="icon-button" aria-label="Close key details" onClick={onClose}><X size={17} /></button></div><div className="key-value"><code>{value}</code><button className="secondary-button" onClick={onCopy}><Copy size={14} />{copied ? "Copied" : "Copy key"}</button></div><pre><code>{snippet}</code></pre><p className="muted-note">Set <code>API_SENTINEL_KEY</code> to the key above in the monitored API’s environment. Never commit it to source control.</p></section>;
}

function IncidentsPage({ incidents, loading, error, onRetry }: { incidents: Incident[]; loading: boolean; error: Error | null; onRetry: () => void }) {
  const queryClient = useQueryClient();
  const [filter, setFilter] = React.useState("all");
  const update = useMutation({ mutationFn: ({ incident, status }: { incident: Incident; status: string }) => api(`/api/incidents/${incident.id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status }) }), onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["dashboard"] }) });
  const visible = incidents.filter(item => filter === "all" || item.status === filter);
  return <><PageHeading eyebrow="RELIABILITY" title="Incidents" subtitle="Review and track service disruptions."><label className="filter-select"><span className="sr-only">Filter incidents</span><select value={filter} onChange={event => setFilter(event.target.value)}><option value="all">All incidents</option><option value="investigating">Investigating</option><option value="monitoring">Monitoring</option></select><ChevronDown size={14} /></label></PageHeading>{loading ? <div className="notice">Loading incidents…</div> : error ? <div className="notice error" role="alert">{error.message} <button className="retry-button" onClick={onRetry}>Retry</button></div> : <section className="panel full-panel"><div className="list-toolbar"><span>{visible.length} incidents</span></div><IncidentList incidents={visible} onStatus={(incident, status) => update.mutate({ incident, status })} /></section>}</>;
}

function IntelligencePage({ incidents, onRefresh }: { incidents: Incident[]; onRefresh: () => void }) {
  const queryClient = useQueryClient();
  const [runbookTitle, setRunbookTitle] = React.useState("");
  const [runbookBody, setRunbookBody] = React.useState("");
  const [report, setReport] = React.useState<InvestigationReport | null>(null);
  const [notice, setNotice] = React.useState("");
  const intelligence = useQuery({ queryKey: ["intelligence"], queryFn: () => api<IntelligenceResult>("/api/intelligence") });
  const runbooks = useQuery({ queryKey: ["runbooks"], queryFn: () => api<{ id: string; title: string; body: string }[]>("/api/runbooks") });
  const analyze = useMutation({ mutationFn: () => api<{ feature_windows: number; message: string }>("/api/intelligence/analyze", { method: "POST" }), onSuccess: result => { setNotice(result.message); void queryClient.invalidateQueries({ queryKey: ["intelligence"] }); onRefresh(); }, onError: e => setNotice(e instanceof Error ? e.message : "Analysis could not run") });
  const train = useMutation({ mutationFn: () => api<Record<string, unknown>>("/api/intelligence/train", { method: "POST" }), onSuccess: result => { setNotice(result.version_status === "active" ? "Model passed holdout checks against the baseline and is active." : result.version_status === "candidate" ? `Candidate saved; it is not active (${String(result.quality_status ?? result.status)}).` : `Model not trained: ${String(result.status)} (${String(result.labeled_windows ?? 0)} labeled windows).`); void queryClient.invalidateQueries({ queryKey: ["intelligence"] }); }, onError: e => setNotice(e instanceof Error ? e.message : "Training could not run") });
  const saveRunbook = useMutation({ mutationFn: () => api("/api/runbooks", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: runbookTitle, body: runbookBody }) }), onSuccess: () => { setRunbookTitle(""); setRunbookBody(""); setNotice("Runbook saved to this workspace."); void queryClient.invalidateQueries({ queryKey: ["runbooks"] }); }, onError: e => setNotice(e instanceof Error ? e.message : "Runbook could not be saved") });
  const investigate = useMutation({ mutationFn: (id: string) => api<InvestigationReport>(`/api/incidents/${id}/investigate`, { method: "POST" }), onSuccess: setReport, onError: e => setNotice(e instanceof Error ? e.message : "Investigation could not run") });
  const modelReady = Boolean(intelligence.data?.model);
  return <>
    <PageHeading eyebrow="PREDICTIVE OBSERVABILITY" title="Intelligence" subtitle="Inspect telemetry features, anomaly signals, failure risk, and evidence-backed incident reports.">
      <div className="intelligence-actions"><button className="secondary-button" disabled={analyze.isPending} onClick={() => analyze.mutate()}>{analyze.isPending ? "Analyzing…" : "Analyze last 24 hours"}</button><button className="primary-button" disabled={train.isPending} onClick={() => train.mutate()}>{train.isPending ? "Training…" : "Train failure model"}</button></div>
    </PageHeading>
    {notice && <div className="notice" role="status">{notice}</div>}
    <section className="panel intelligence-note"><BrainCircuit size={17} /><div><strong>{modelReady ? intelligence.data?.model : intelligence.data?.model_status === "candidate" ? "Model candidate needs more validation" : "Predictor not trained"}</strong><p>{modelReady ? `Validated holdout model. ${String(intelligence.data?.model_metrics.labeled_windows ?? 0)} labeled windows.` : intelligence.data?.model_status === "candidate" ? `Candidate status: ${String(intelligence.data.model_metrics.quality_status ?? "pending evaluation")}. Risk scores remain inactive until the model beats the prevalence/Brier baseline on a sufficiently large purged holdout.` : "Failure risk stays unavailable until enough labeled telemetry windows exist. Analyze persists feature windows; training evaluates a purged, time-ordered holdout against a baseline."}</p>{intelligence.data?.model_metrics.holdout_brier !== undefined && <div className="quality-metrics"><span>Holdout Brier <strong>{Number(intelligence.data.model_metrics.holdout_brier).toFixed(3)}</strong></span><span>Baseline Brier <strong>{Number(intelligence.data.model_metrics.baseline_brier).toFixed(3)}</strong></span><span>PR-AUC <strong>{Number(intelligence.data.model_metrics.holdout_pr_auc).toFixed(3)}</strong></span><span>ROC-AUC <strong>{intelligence.data.model_metrics.holdout_roc_auc == null ? "—" : Number(intelligence.data.model_metrics.holdout_roc_auc).toFixed(3)}</strong></span><span>Latency MAE <strong>{intelligence.data.model_metrics.latency_holdout_mae_ms == null ? "—" : `${Number(intelligence.data.model_metrics.latency_holdout_mae_ms).toFixed(0)} ms`}</strong></span></div>}</div></section>
    {intelligence.isLoading ? <div className="notice">Loading intelligence…</div> : intelligence.error ? <div className="notice error" role="alert">{intelligence.error.message}</div> : <section className="intel-grid">{intelligence.data?.services.length ? intelligence.data.services.map(item => <article className="panel intel-card" key={item.service_id}><div className="panel-heading"><div><h2>{item.service}</h2><p>Window · {new Date(item.window_start).toLocaleString()}</p></div><span className={`severity ${item.failure_probability !== null && item.failure_probability >= .7 ? "high" : "low"}`}>{item.failure_probability === null ? "No model" : `${Math.round(item.failure_probability * 100)}% risk`}</span></div><div className="intel-metrics"><span>Anomaly score <strong>{item.anomaly_score?.toFixed(3) ?? "—"}</strong></span><span>5xx rate <strong>{((item.features.error_5xx_rate ?? 0) * 100).toFixed(2)}%</strong></span><span>Rolling p95 <strong>{Math.round(item.features.rolling_5m_p95 ?? item.features.latency_p95 ?? 0)} ms</strong></span><span>Forecast p95 / 10m <strong>{item.latency_forecast_ms === null ? "—" : `${item.latency_forecast_ms} ms`}</strong></span></div>{item.explanations.length > 0 && <div className="contributions"><strong>Top SHAP contributions (log-odds)</strong>{item.explanations.map(reason => <div key={reason.feature}><span>{reason.feature.replace(/_/g, " ")}</span><code>{reason.contribution_log_odds > 0 ? "+" : ""}{reason.contribution_log_odds.toFixed(3)}</code></div>)}</div>}</article>) : <div className="panel"><EmptyState title="No feature windows yet" detail="Add the SDK to a service, collect requests, then analyze telemetry." /></div>}</section>}
    <section className="panel investigation-panel"><div className="panel-heading"><div><h2>Incident investigation</h2><p>LangGraph workflow retrieves tenant runbooks and drafts a cited report. No automated remediation runs.</p></div><FileText size={17} /></div>{incidents.length ? <div className="investigation-list">{incidents.map(item => <div key={item.id}><span><strong>{item.title}</strong><small>{item.service} · {item.status}</small></span><button className="secondary-button" disabled={investigate.isPending} onClick={() => investigate.mutate(item.id)}>Investigate</button></div>)}</div> : <p className="muted-note">No open incidents. Analyze telemetry to evaluate current windows and create qualifying incidents.</p>}
      {report && <article className="investigation-report"><div className="panel-heading"><h3>{report.report.summary}</h3><button className="icon-button" aria-label="Close report" onClick={() => setReport(null)}><X size={15} /></button></div><p><strong>Recommendation:</strong> {report.report.recommendation}</p>{report.report.correlated_clues.map((clue, index) => <p key={index}>{clue}</p>)}<p><strong>Telemetry evidence:</strong> {report.report.evidence.map((entry, index) => <code key={index}>{JSON.stringify(entry)}</code>)}</p>{report.report.deployment_events.map((event, index) => <p key={`deploy-${index}`}><strong>Deployment:</strong> {JSON.stringify(event)}</p>)}{report.report.application_errors.map((event, index) => <p key={`log-${index}`}><strong>Application error:</strong> {JSON.stringify(event)}</p>)}{report.report.runbook_matches.map(match => <p key={match.id}><strong>Runbook · {match.title}</strong> ({Math.round(match.score * 100)}% keyword overlap): {match.excerpt}</p>)}<small>{report.report.limitations} Sources unavailable: {report.report.sources_unavailable.join(", ")}.</small></article>}
    </section>
    <section className="panel runbook-panel"><div className="panel-heading"><div><h2>Workspace runbooks</h2><p>Stored privately in this organization and searched during investigations.</p></div><BookOpen size={17} /></div><form className="runbook-form" onSubmit={event => { event.preventDefault(); setNotice(""); saveRunbook.mutate(); }}><label>Title<input required minLength={2} maxLength={200} value={runbookTitle} onChange={event => setRunbookTitle(event.target.value)} placeholder="Database connection pool exhaustion" /></label><label>Recovery steps<textarea required minLength={10} maxLength={20000} rows={4} value={runbookBody} onChange={event => setRunbookBody(event.target.value)} placeholder="Symptoms, checks, and approved recovery steps…" /></label><button className="primary-button" disabled={saveRunbook.isPending}>{saveRunbook.isPending ? "Saving…" : "Save runbook"}</button></form><div className="runbook-list">{runbooks.data?.map(book => <article key={book.id}><strong>{book.title}</strong><p>{book.body}</p></article>)}</div></section>
  </>;
}

function SettingsPage({ session, onSaved }: { session: Session; onSaved: () => void }) {
  const [name, setName] = React.useState(session.organization.name);
  const [notice, setNotice] = React.useState("");
  const [error, setError] = React.useState("");
  const [webhookName, setWebhookName] = React.useState("Slack");
  const [webhookUrl, setWebhookUrl] = React.useState("");
  const queryClient = useQueryClient();
  const alerts = useQuery({ queryKey: ["alerts"], queryFn: () => api<AlertDestination[]>("/api/alerts") });
  const operations = useQuery({ queryKey: ["operations-status"], queryFn: () => api<OperationsStatus>("/api/operations/status"), refetchInterval: 15_000 });
  const save = useMutation({ mutationFn: () => api("/api/organizations/current", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }), onSuccess: () => { setNotice("Workspace settings saved."); setError(""); onSaved(); }, onError: cause => setError(cause instanceof Error ? cause.message : "Could not save settings") });
  const addAlert = useMutation({ mutationFn: () => api<AlertDestination>("/api/alerts/slack", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: webhookName, webhook_url: webhookUrl }) }), onSuccess: () => { setWebhookUrl(""); setNotice("Slack alert destination added."); setError(""); void queryClient.invalidateQueries({ queryKey: ["alerts"] }); }, onError: cause => setError(cause instanceof Error ? cause.message : "Could not add Slack alerts") });
  const testAlert = useMutation({ mutationFn: (id: string) => api(`/api/alerts/${id}/test`, { method: "POST" }), onSuccess: () => setNotice("Test alert delivered to Slack."), onError: cause => setError(cause instanceof Error ? cause.message : "Slack test delivery failed") });
  const toggleAlert = useMutation({ mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => api(`/api/alerts/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) }), onSuccess: (_result, variables) => { setNotice(variables.enabled ? "Slack alerts enabled." : "Slack alerts disabled."); void queryClient.invalidateQueries({ queryKey: ["alerts"] }); }, onError: cause => setError(cause instanceof Error ? cause.message : "Could not update alert destination") });
  return <><PageHeading eyebrow="WORKSPACE" title="Settings" subtitle="Manage workspace access and incident notifications." /><section className="panel settings-panel"><h2>Workspace details</h2><p>Changes apply to everyone in this workspace.</p><form className="settings-form" onSubmit={event => { event.preventDefault(); setNotice(""); setError(""); save.mutate(); }}><label>Workspace name<input required minLength={2} maxLength={120} value={name} onChange={event => setName(event.target.value)} disabled={session.role !== "owner"} /></label><label>Signed-in account<input value={session.user.email} readOnly /></label><label>Role<input value={session.role} readOnly /></label>{session.role === "owner" ? <button className="primary-button" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save changes"}</button> : <p className="muted-note">Only workspace owners can change workspace settings.</p>}</form></section><section className="panel settings-panel"><h2>Background processing</h2><p>Feature analysis runs each minute; model evaluation runs daily when Redis, the scheduler, and worker are running.</p><div className="ops-status"><span>Redis <strong>{operations.data?.redis ?? "checking…"}</strong></span><span>Scheduler <strong>{operations.data?.scheduler ?? "checking…"}</strong></span><span>Worker <strong>{operations.data?.worker ?? "checking…"}</strong></span></div></section><section className="panel settings-panel alert-settings"><h2>Slack incident alerts</h2><p>Webhook URLs are encrypted in the database. Delivery retries run through the background worker.</p>{alerts.error && <p className="form-error" role="alert">{alerts.error.message}</p>}{alerts.data?.map(destination => <div className="alert-destination" key={destination.id}><span><strong>{destination.name}</strong><small>{destination.provider} · {destination.enabled ? "enabled" : "disabled"}</small></span><div>{destination.enabled && <button className="secondary-button" disabled={testAlert.isPending} onClick={() => testAlert.mutate(destination.id)}>Send test</button>}{session.role === "owner" && <button className="key-action" onClick={() => toggleAlert.mutate({ id: destination.id, enabled: !destination.enabled })}>{destination.enabled ? "Disable" : "Enable"}</button>}</div></div>)}{session.role === "owner" && !alerts.data?.length && <form className="runbook-form" onSubmit={event => { event.preventDefault(); setNotice(""); setError(""); addAlert.mutate(); }}><label>Destination name<input required minLength={2} maxLength={120} value={webhookName} onChange={event => setWebhookName(event.target.value)} /></label><label>Slack incoming webhook URL<input required type="url" value={webhookUrl} onChange={event => setWebhookUrl(event.target.value)} placeholder="https://hooks.slack.com/services/…" autoComplete="off" /></label><button className="primary-button" disabled={addAlert.isPending}>{addAlert.isPending ? "Saving…" : "Connect Slack"}</button></form>}{notice && <p className="success-message"><Check size={15} />{notice}</p>}{error && <p className="form-error" role="alert">{error}</p>}</section></>;
}

function HelpPageOld() {
  const [copied, setCopied] = React.useState(false);
  const example = `curl -X POST http://localhost:8000/api/telemetry \\\n+  -H 'Content-Type: application/json' \\\n+  --cookie 'sentinel_session=YOUR_SESSION_COOKIE' \\\n+  -d '{"service_id":"YOUR_SERVICE_ID","endpoint":"/health","method":"GET","status_code":200,"latency_ms":42}'`;
  const copy = async () => { await navigator.clipboard.writeText(example); setCopied(true); window.setTimeout(() => setCopied(false), 1800); };
  return <><PageHeading eyebrow="DOCUMENTATION" title="Help & support" subtitle="Instrument an API and stream request telemetry." /><section className="panel help-panel"><div className="help-heading"><BookOpen size={18} /><div><h2>Instrument a FastAPI service</h2><p>Create a service to issue its ingestion key. The key is shown only once; rotate it if it is lost.</p></div></div><div className="code-toolbar"><span>Install the local SDK</span><button className="secondary-button" onClick={() => void copy()}><Copy size={14} />{copied ? "Copied" : "Copy example"}</button></div><pre><code>{example}</code></pre><p className="muted-note">The SDK records route templates, method, status, latency, content lengths, validated request/trace IDs, and exception type. It never reads request or response bodies or authorization headers. The bounded queue sends batches asynchronously so telemetry delivery does not hold up the API response.</p></section><section className="panel help-panel"><h2>Key safety</h2><p>Store the ingestion key in an environment variable. The database stores only its SHA-256 hash. Rotating a key immediately invalidates its previous value.</p></section></>;
}

function HelpPage() {
  const [copied, setCopied] = React.useState(false);
  const snippet = `from fastapi import FastAPI\nfrom api_sentinel import SentinelMiddleware\nimport os\n\napp = FastAPI()\napp.add_middleware(\n    SentinelMiddleware,\n    service_key=os.environ["API_SENTINEL_KEY"],\n    collector_url="http://127.0.0.1:8000/api/telemetry/batch",\n)`;
  const copy = async () => { await navigator.clipboard.writeText(snippet); setCopied(true); window.setTimeout(() => setCopied(false), 1800); };
  return <><PageHeading eyebrow="DOCUMENTATION" title="Help & support" subtitle="Instrument an API and stream request telemetry." /><section className="panel help-panel"><div className="help-heading"><BookOpen size={18} /><div><h2>Instrument a FastAPI service</h2><p>Create a service to issue its ingestion key. The key is shown only once; rotate it if it is lost.</p></div></div><p className="muted-note">Install the SDK with <code>pip install -e ./sdk-python</code>, set the service key in <code>API_SENTINEL_KEY</code>, then add this middleware:</p><div className="code-toolbar"><span>FastAPI integration</span><button className="secondary-button" onClick={() => void copy()}><Copy size={14} />{copied ? "Copied" : "Copy example"}</button></div><pre><code>{snippet}</code></pre><p className="muted-note">The SDK records route templates, method, status, latency, content lengths, validated request/trace IDs, and exception type. It never reads request or response bodies or authorization headers. Its bounded in-memory queue batches delivery without blocking API responses.</p></section><section className="panel help-panel"><h2>Key safety</h2><p>Store the ingestion key in an environment variable. The database stores only its SHA-256 hash. Rotating a key immediately invalidates its previous value.</p></section></>;
}

function AuthScreen({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [register, setRegister] = React.useState(true);
  const [error, setError] = React.useState("");
  const [form, setForm] = React.useState({ email: "", password: "", organization_name: "" });
  const submit = useMutation({ mutationFn: async () => api(`/api/auth/${register ? "register" : "login"}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(register ? form : { email: form.email, password: form.password }) }), onSuccess: onAuthenticated, onError: cause => setError(cause instanceof Error ? cause.message : "Could not sign in") });
  return <main className="auth-page"><section className="auth-card"><div className="brand auth-brand"><span className="brand-mark"><Activity size={18} /></span><span>sentinel<span className="brand-period">.</span></span></div><p className="eyebrow">API OBSERVABILITY</p><h1>{register ? "Create your workspace" : "Welcome back"}</h1><p className="subtitle">{register ? "Start monitoring your APIs with a new account." : "Sign in to view your workspace."}</p><form onSubmit={event => { event.preventDefault(); setError(""); submit.mutate(); }}>{register && <label>Workspace name<input required minLength={2} maxLength={120} value={form.organization_name} onChange={event => setForm({ ...form, organization_name: event.target.value })} autoComplete="organization" /> </label>}<label>Email address<input required type="email" autoComplete="email" value={form.email} onChange={event => setForm({ ...form, email: event.target.value })} /></label><label>Password<input required type="password" minLength={register ? 12 : 1} maxLength={128} autoComplete={register ? "new-password" : "current-password"} value={form.password} onChange={event => setForm({ ...form, password: event.target.value })} />{register && <small>Use at least 12 characters.</small>}</label>{error && <p className="auth-error" role="alert">{error}</p>}<button className="auth-submit" disabled={submit.isPending}>{submit.isPending ? "Please wait…" : register ? "Create workspace" : "Sign in"}</button></form><p className="auth-toggle">{register ? "Already have an account?" : "New to API Sentinel?"}<button onClick={() => { setRegister(!register); setError(""); }}>{register ? "Sign in" : "Create a workspace"}</button></p></section></main>;
}

export default App;
