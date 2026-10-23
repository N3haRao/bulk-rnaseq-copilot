import { useState, type ReactNode } from "react";
import type { Check, Figure as FigureT } from "../api";
import { fileUrl } from "../api";

const STEPS = ["Upload", "Check & set up", "Run", "Results"];

export function Stepper({ current }: { current: number }) {
  return (
    <ol className="stepper">
      {STEPS.map((label, i) => (
        <li key={label} className={i < current ? "done" : i === current ? "current" : ""}>
          <span className="dot">{i < current ? "✓" : i + 1}</span>
          {label}
        </li>
      ))}
    </ol>
  );
}

export function Notice({ kind, children }: { kind: "pass" | "warning" | "fail" | "info"; children: ReactNode }) {
  return <div className={`notice ${kind}`} role={kind === "fail" ? "alert" : undefined}>{children}</div>;
}

export function Badge({ status, label }: { status: string; label?: string }) {
  const text = label ?? status.replace(/_/g, " ");
  return <span className={`badge ${status}`}>{text}</span>;
}

export type Explanations = Record<string, { explanation: string; how_to_fix: string }>;

export function CheckList({ checks, explain }: { checks: Check[]; explain?: Explanations }) {
  const problems = checks.filter((c) => c.status !== "pass");
  const passed = checks.filter((c) => c.status === "pass");
  return (
    <div>
      {problems.length > 0 && (
        <ul className="check-list">
          {problems.map((c) => (
            <li key={c.id}>
              <Badge status={c.status} label={c.status === "fail" ? "Problem" : "Check"} />
              <div>
                <div>{c.message}</div>
                {explain?.[c.id] && (
                  <div className="assist small">
                    <div>{explain[c.id].explanation}</div>
                    <div><strong>How to fix:</strong> {explain[c.id].how_to_fix}</div>
                  </div>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {passed.length > 0 && (
        <details>
          <summary className="muted small">{passed.length} checks passed</summary>
          <ul className="check-list small">
            {passed.map((c) => (
              <li key={c.id}>
                <Badge status="pass" label="OK" />
                <span>{c.message}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function Figures({ jobId, figures }: { jobId: string; figures: FigureT[] }) {
  const [zoom, setZoom] = useState<string | null>(null);
  if (!figures.length) return <p className="muted">No figures for this section.</p>;
  return (
    <>
      <div className="figures">
        {figures.map((f) => (
          <figure className="figure" key={f.path}>
            <img src={fileUrl(jobId, f.path)} alt={f.title} loading="lazy" onClick={() => setZoom(f.path)} />
            <figcaption>
              <strong>{f.title}</strong>
              {f.caption}{" "}
              <a href={fileUrl(jobId, f.path, true)}>Download</a>
            </figcaption>
          </figure>
        ))}
      </div>
      {zoom && (
        <div className="lightbox" onClick={() => setZoom(null)} role="dialog" aria-label="Enlarged figure">
          <img src={fileUrl(jobId, zoom)} alt="" />
        </div>
      )}
    </>
  );
}

// Who made a suggestion: the open-weight assistant or the built-in rules
export function SourceTag({ source, model }: { source: string; model: string | null }) {
  return source === "model"
    ? <span className="source-tag model" title="Suggested by the open-weight assistant running on this computer">Assistant · {model?.replace(/^ollama:/, "")}</span>
    : <span className="source-tag rules" title="Suggested by built-in rules">Built-in rules</span>;
}

export function Tabs<T extends string>({ tabs, active, onChange }: {
  tabs: { id: T; label: string }[];
  active: T;
  onChange: (id: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <button key={t.id} role="tab" aria-selected={active === t.id} className={active === t.id ? "active" : ""}
          onClick={() => onChange(t.id)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

// Minimal Markdown for methods.md: headings, paragraphs, bullet lists, *italic* and **bold**.
export function Markdown({ text }: { text: string }) {
  const blocks = text.trim().split(/\n\s*\n/);
  return (
    <div className="methods">
      {blocks.map((b, i) => {
        const lines = b.split("\n");
        if (/^#{1,3} /.test(b)) {
          const level = b.match(/^#+/)![0].length;
          const content = inline(b.replace(/^#+ /, ""));
          return level === 1 ? <h2 key={i}>{content}</h2> : <h3 key={i}>{content}</h3>;
        }
        if (lines.every((l) => l.startsWith("- "))) {
          return <ul key={i}>{lines.map((l, j) => <li key={j}>{inline(l.slice(2))}</li>)}</ul>;
        }
        return <p key={i}>{inline(lines.join(" "))}</p>;
      })}
    </div>
  );
}

function inline(s: string): ReactNode[] {
  return s.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g).map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) return <em key={i}>{part.slice(1, -1)}</em>;
    return part;
  });
}
