import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Link, useNavigate } from "react-router-dom";
import { api, chatFileUrl, type ChatEvent, type ChatItem, type Job } from "../api";
import { Notice } from "./common";

const SUGGESTIONS = [
  "Summarise the main findings in three sentences.",
  "Is Krt5 lower in luminal cells? How strong is the evidence?",
  "Make a volcano plot with only these genes labelled: Krt5, Krt18, Elf5, Acta2, Myh11",
  "Which pathways come up among the genes lower in the test group?",
  "What settings and software versions were used?",
  "Make 5 slides for a lab meeting about these results.",
];
const IMAGE = /\.(png|jpe?g|gif|svg|webp)$/i;

export default function ChatTab({ job }: { job: Job }) {
  const navigate = useNavigate();
  const [status, setStatus] = useState<{ configured: boolean; model: string; detail: string } | null>(null);
  const [chats, setChats] = useState<{ id: string; title: string | null; created: string }[]>([]);
  const [convId, setConvId] = useState<string | null>(null);
  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [thinking, setThinking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api.chatStatus().then(setStatus).catch(() => undefined);
    api.chats(job.id).then((list) => {
      setChats(list);
      if (list.length) open(list[0].id);
    }).catch((e) => setError((e as Error).message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id]);

  useEffect(() => { bottom.current?.scrollIntoView({ block: "end" }); }, [items, thinking]);

  async function open(id: string) {
    const c = await api.chat(job.id, id);
    setConvId(c.id);
    setItems(c.transcript);
  }

  async function newChat() {
    const c = await api.newChat(job.id);
    setConvId(c.id);
    setItems([]);
    setChats([{ id: c.id, title: null, created: new Date().toISOString() }, ...chats]);
    return c.id;
  }

  function onEvent(e: ChatEvent) {
    if (e.type === "thinking") { setThinking(true); return; }
    setThinking(false);
    setItems((cur) => {
      const next = [...cur];
      const last = next[next.length - 1];
      if (e.type === "text") {
        if (last && last.role === "assistant" && last.pending) next[next.length - 1] = { ...last, text: (last.text ?? "") + e.text };
        else next.push({ role: "assistant", text: e.text, pending: true });
      } else if (e.type === "tool_start") {
        if (last?.pending) next[next.length - 1] = { ...last, pending: false };
        next.push({ role: "tool", id: e.id, name: e.name, title: e.title, code: e.code, language: e.language as ChatItem["language"], pending: true });
      } else if (e.type === "tool_end") {
        const i = next.findIndex((it) => it.role === "tool" && it.pending && it.id === e.id);
        const { type: _type, ...item } = e;
        void _type;
        if (i >= 0) next[i] = { ...item, pending: false };
        else next.push({ ...item, pending: false });
      } else if (e.type === "error") {
        next.push({ role: "error", text: e.text });
      } else if (e.type === "done" && last?.pending) {
        next[next.length - 1] = { ...last, pending: false };
      }
      return next;
    });
  }

  async function send(text: string) {
    const msg = text.trim();
    if (!msg || busy) return;
    setBusy(true);
    setError(null);
    setInput("");
    try {
      const id = convId ?? await newChat();
      setItems((cur) => [...cur, { role: "user", text: msg }]);
      await api.sendChat(job.id, id, msg, onEvent);
      api.chats(job.id).then(setChats).catch(() => undefined);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      setThinking(false);
      setItems((cur) => cur.map((it) => (it.pending ? { ...it, pending: false } : it)));
    }
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); }
  }

  async function startRerun(item: ChatItem) {
    if (!item.rerun) return;
    try {
      const j = await api.startRun(item.rerun.upload_id, item.rerun.settings,
        { source: "chat", model: status?.model ?? null, changed_by_user: false });
      navigate(`/runs/${j.id}`);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  if (status && !status.configured) {
    return (
      <section className="card">
        <h2>Ask about this analysis</h2>
        <Notice kind="warning">The chat needs an Anthropic API key.</Notice>
        <Link className="button" to="/settings">Add a key in Settings</Link>
      </section>
    );
  }

  return (
    <section className="card chat">
      <div className="row">
        <h2 style={{ margin: 0 }}>Ask about this analysis</h2>
        {status && <span className="source-tag api">Claude · {status.model}</span>}
        <span className="spacer" />
        {chats.length > 1 && (
          <select value={convId ?? ""} onChange={(e) => open(e.target.value)} style={{ maxWidth: 240 }} aria-label="Earlier chats">
            {chats.map((c) => <option key={c.id} value={c.id}>{c.title ?? "New chat"}</option>)}
          </select>
        )}
        <button className="secondary" disabled={busy} onClick={newChat}>New chat</button>
      </div>
      <p className="muted small" style={{ margin: "6px 0 0" }}>
        Answers come from Claude through the Anthropic API, so this run's results are sent to Anthropic: use public data
        during the trial. Results the assistant computes itself are marked as custom analyses; the tested pipeline's
        results are in the other tabs.
      </p>

      <div className="chat-log">
        {items.length === 0 && (
          <div className="suggestions">
            {SUGGESTIONS.map((s) => <button key={s} className="secondary" onClick={() => send(s)} disabled={busy}>{s}</button>)}
          </div>
        )}
        {items.map((it, i) => <Item key={i} item={it} jobId={job.id} convId={convId} onRerun={startRerun} />)}
        {busy && (thinking || items[items.length - 1]?.role === "user") && (
          <p className="muted small"><span className="spinner" /> Thinking…</p>
        )}
        <div ref={bottom} />
      </div>

      {error && <Notice kind="fail">{error}</Notice>}
      <div className="chat-input">
        <textarea value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={onKey} rows={2}
          placeholder="Ask a question, or ask for a plot, a table or slides…  (Enter to send, Shift+Enter for a new line)"
          disabled={busy} aria-label="Message" />
        <button onClick={() => send(input)} disabled={busy || !input.trim()}>{busy ? "Working…" : "Send"}</button>
      </div>
    </section>
  );
}

function Item({ item, jobId, convId, onRerun }: {
  item: ChatItem; jobId: string; convId: string | null; onRerun: (i: ChatItem) => void;
}) {
  if (item.role === "user") return <div className="msg user">{item.text}</div>;
  if (item.role === "error") return <Notice kind="fail">{item.text}</Notice>;
  if (item.role === "assistant") {
    return <div className="msg assistant md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{item.text ?? ""}</ReactMarkdown></div>;
  }
  // a tool call
  const files = item.files ?? [];
  const icon = item.pending ? <span className="spinner" /> : item.error || item.ok === false ? "✗" : "✓";
  return (
    <div className="msg tool">
      {(item.code || item.output) && !item.pending ? (
        <details>
          <summary><span className="tool-icon">{icon}</span> {label(item)}</summary>
          {item.code && <pre className="code">{item.code}</pre>}
          {item.output && <pre className="code out">{item.output}</pre>}
        </details>
      ) : (
        <div className="small muted"><span className="tool-icon">{icon}</span> {label(item)}</div>
      )}
      {convId && files.length > 0 && (
        <div className="chat-files">
          {files.map((f) => IMAGE.test(f) ? (
            <figure key={f} className="figure">
              <a href={chatFileUrl(jobId, convId, f)} target="_blank" rel="noreferrer"><img src={chatFileUrl(jobId, convId, f)} alt={f} /></a>
              <figcaption>{f.split("/").pop()} · <a href={chatFileUrl(jobId, convId, f, true)}>Download</a></figcaption>
            </figure>
          ) : (
            <a key={f} className="button secondary" href={chatFileUrl(jobId, convId, f, true)}>⬇ {f.split("/").pop()}</a>
          ))}
        </div>
      )}
      {item.rerun && (
        <div className="rerun-card">
          <strong>Suggested new run:</strong> {item.rerun.reason}
          <table><tbody>
            {Object.entries(item.rerun.changes).map(([k, v]) => <tr key={k}><th>{k}</th><td>{Array.isArray(v) ? v.join(", ") : String(v)}</td></tr>)}
          </tbody></table>
          <button onClick={() => onRerun(item)}>Start this run</button>
          <span className="muted small"> The current run stays as it is.</span>
        </div>
      )}
    </div>
  );
}

function label(item: ChatItem): string {
  const lang = item.language === "python" ? "Ran Python" : item.language === "r" ? "Ran R" : null;
  return lang ? `${lang}: ${item.title}` : item.title ?? item.name ?? "";
}
