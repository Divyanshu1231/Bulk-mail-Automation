import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "https://bulk-mail-automation.onrender.com").replace(/\/$/, "");
const API = `${API_BASE}/api`;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// Render free instances sleep when idle; retry GET requests while the backend wakes up (~30-60s).
async function request(path, options = {}, retries = 0) {
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(API + path, options);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const err = new Error(data.detail || `Request failed (${res.status})`);
        err.status = res.status;
        throw err;
      }
      return data;
    } catch (e) {
      lastErr = e;
      const retryable = !e.status || e.status === 502 || e.status === 503 || e.status === 504;
      if (!retryable || attempt === retries) break;
      await sleep(4000);
    }
  }
  throw lastErr;
}
const defaultInstruction = "Write a concise, professional recruitment outreach email. Do not invent facts, claims, or promises. Use {{name}}, {{company}}, and {{role}} placeholders where suitable.";

// Convert API values to renderable text; React cannot render objects such as {name: "..."} directly.
function safeText(value) {
  if (value == null) return "";
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return value.map(safeText).filter(Boolean).join(", ");
  if (typeof value === "object") {
    if (value.name != null) return safeText(value.name);
    if (value.email != null) return safeText(value.email);
    return "";
  }
  return "";
}

function App() {
  const [health, setHealth] = useState(null);
  const [contacts, setContacts] = useState([]);
  const [stats, setStats] = useState({ contacts: 0, consented_contacts: 0, drafts: {} });
  const [instruction, setInstruction] = useState(defaultInstruction);
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [progress, setProgress] = useState(null);

  async function refresh() {
    try {
      const [h, c, s] = await Promise.all([request("/health", {}, 12), request("/contacts", {}, 12), request("/stats", {}, 12)]);
      setHealth(h); setContacts(Array.isArray(c) ? c : []); setStats(s && typeof s === "object" ? s : { contacts: 0, consented_contacts: 0, drafts: {} });
    } catch (e) { setError("Cannot reach backend (Render may still be waking up, retry in a minute). " + e.message); }
  }
  useEffect(() => { refresh(); }, []);
  function showError(e) { setError(e.message || String(e)); setMessage(""); }

  async function upload(e) {
    const file = e.target.files?.[0]; if (!file) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const form = new FormData(); form.append("file", file);
      const result = await request("/contacts/upload", { method: "POST", body: form });
      setMessage(`CSV imported: ${result.added} added, ${result.updated || 0} updated, ${result.skipped} skipped.`);
      await refresh();
    } catch (err) { showError(err); } finally { setBusy(false); e.target.value = ""; }
  }
  async function setConsent(contact, approved) {
    setBusy(true); setError(""); setMessage("");
    try {
      await request(`/contacts/${contact.id}/consent`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approved }) });
      setMessage(approved ? `Marked ${safeText(contact.email)} as authorized.` : `Removed authorization for ${safeText(contact.email)}.`);
      await refresh();
    } catch (err) { showError(err); } finally { setBusy(false); }
  }
  async function generateTemplate() {
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await request("/template/generate", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instruction }) });
      setSubject(result.subject); setBody(result.body);
      setMessage("One reusable template generated. Review and edit it before sending.");
    } catch (err) { showError(err); } finally { setBusy(false); }
  }
  async function sendCampaign() {
    if (!subject.trim() || !body.trim()) { setError("Generate or enter a subject and email body first."); return; }
    if (!stats.consented_contacts) { setError("No authorized contacts. Import contacts and mark permission only when valid consent exists."); return; }
    const confirmed = window.confirm(`Start one bulk campaign for ${stats.consented_contacts} authorized contact(s)?\n\nGemini will NOT generate a separate draft per contact. The same template will be personalized with {{name}}, {{company}}, {{role}} and sent in batches.\n\nContinue only if these recipients are authorized and your Brevo plan supports the volume.`);
    if (!confirmed) return;
    setBusy(true); setError(""); setMessage(""); setProgress({ sent: 0, failed: 0, pending: stats.consented_contacts, total: stats.consented_contacts });
    try {
      const campaign = await request("/campaigns", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ subject, body }) });
      let latest = { sent: 0, failed: 0, pending: campaign.total, total: campaign.total };
      setProgress(latest);
      // Process one small batch per request so a single long HTTP request doesn't handle all 5,000 recipients.
      while (true) {
        const batch = await request(`/campaigns/${campaign.campaign_id}/process`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ limit: campaign.batch_size || 20 }) });
        latest = { sent: batch.sent || 0, failed: batch.failed || 0, pending: batch.pending || 0, total: campaign.total };
        setProgress(latest);
        if (batch.done) break;
        if (!batch.processed && latest.pending > 0) throw new Error("Campaign made no progress. Check Render logs and retry after resolving the error.");
      }
      setMessage(`Campaign finished. Accepted by Brevo: ${latest.sent}; failed: ${latest.failed}. Check the campaign results before retrying failed recipients.`);
      await refresh();
    } catch (err) { showError(err); }
    finally { setBusy(false); }
  }

  const consented = contacts.filter(c => c.consent).length;
  const statusClass = s => "status " + (s || "draft").replace(/\s/g, "-");
  return <div className="app">
    <header>
      <div><div className="eyebrow">AI + EMAIL WORKFLOW</div><h1>AI Email Automation</h1><p>Generate one reusable email template and send it to authorized contacts in batches.</p></div>
      <button className="secondary" onClick={refresh} disabled={busy}>Refresh</button>
    </header>
    {error && <div className="alert error"><b>Problem:</b> {error}<button onClick={() => setError("")}>×</button></div>}
    {message && <div className="alert success"><span style={{whiteSpace:"pre-wrap"}}>{message}</span><button onClick={() => setMessage("")}>×</button></div>}
    <section className="cards">
      <div className="metric"><span>Contacts</span><strong>{stats.contacts}</strong></div>
      <div className="metric"><span>Authorized contacts</span><strong>{consented}</strong></div>
      <div className="metric"><span>Existing drafts</span><strong>{Object.values(stats.drafts || {}).reduce((a, b) => a + b, 0)}</strong></div>
      <div className="metric"><span>Previously sent drafts</span><strong>{stats.drafts?.sent || 0}</strong></div>
    </section>
    <section className="panel">
      <div className="section-head"><div><h2>1. Import contacts</h2><p>CSV headers: email, consent, name, company, role, context</p></div><label className="upload">Choose CSV<input type="file" accept=".csv,text/csv" onChange={upload} disabled={busy}/></label></div>
      <div className="notice">Only mark permission for contacts you are genuinely authorized to email. Uploading a contact alone is not consent. The bulk campaign sends only to contacts marked as authorized.</div>
      <div className="table-wrap"><table><thead><tr><th>Email</th><th>Name</th><th>Company</th><th>Permission</th><th>Authorize</th></tr></thead><tbody>
        {contacts.map(c => <tr key={safeText(c.id)}><td>{safeText(c.email)}</td><td>{safeText(c.name)}</td><td>{safeText(c.company)}</td><td><span className={statusClass(c.consent ? "consented" : "not-consented")}>{c.consent ? "yes" : "no"}</span></td><td><button className={c.consent ? "secondary" : ""} disabled={busy} onClick={() => setConsent(c, !c.consent)}>{c.consent ? "Remove" : "I have permission"}</button></td></tr>)}
        {!contacts.length && <tr><td colSpan="5" className="empty">No contacts yet. Upload a CSV to get started.</td></tr>}
      </tbody></table></div>
    </section>
    <section className="panel">
      <h2>2. Create one email template</h2>
      <p>Gemini is called once to create a reusable template, not once per contact. You can edit the subject and body before sending.</p>
      <label className="field-label">Instructions to AI</label><textarea rows="3" value={instruction} onChange={e => setInstruction(e.target.value)} disabled={busy}/>
      <div className="action-row"><span>Placeholders: <code>{"{{name}}"}</code> <code>{"{{company}}"}</code> <code>{"{{role}}"}</code></span><button onClick={generateTemplate} disabled={busy}>{busy ? "Working…" : "Generate one template"}</button></div>
      <label className="field-label">Email subject</label><input value={subject} onChange={e => setSubject(e.target.value)} maxLength="250" disabled={busy}/>
      <label className="field-label">Email body</label><textarea rows="10" value={body} onChange={e => setBody(e.target.value)} maxLength="12000" disabled={busy} placeholder="Your reusable email template will appear here…"/>
      <div className="notice">Example: “Hi {"{{name}}"}” will be personalized for each recipient. Do not add claims or facts that you cannot verify.</div>
    </section>
    <section className="panel">
      <h2>3. Send campaign in batches</h2>
      <p>Only authorized contacts are queued. Each request processes up to {Math.min(health?.max_send_per_request || 20, 20)} emails to avoid one giant request. Sending 5,000 emails still depends on your provider's daily quota and account limits.</p>
      <button onClick={sendCampaign} disabled={busy || !subject.trim() || !body.trim() || !consented}>{busy ? "Campaign processing…" : `Send to ${consented} authorized contact(s)`}</button>
      {progress && <div style={{marginTop:16}}><strong>Campaign progress</strong><p>Sent: {progress.sent} · Failed: {progress.failed} · Pending: {progress.pending} / {progress.total}</p><div style={{height:10,background:"#e5e7eb",borderRadius:99,overflow:"hidden"}}><div style={{height:"100%",width:`${progress.total ? ((progress.sent + progress.failed) / progress.total) * 100 : 0}%`,background:"#2563eb",transition:"width .2s"}}/></div></div>}
      <div className="notice">Failed emails are recorded in the campaign queue and are not automatically retried, to avoid duplicate sends. “Accepted by Brevo” does not guarantee inbox delivery.</div>
    </section>
    <footer><span>Backend: {health ? "connected" : "checking…"}</span><span>Gemini: {health?.gemini_configured ? "configured" : "not configured"}</span><span>Brevo: {health?.brevo_configured ? "configured" : "not configured"}</span></footer>
  </div>;
}
createRoot(document.getElementById("root")).render(<App/>);
