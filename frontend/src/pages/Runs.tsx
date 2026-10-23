import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Job } from "../api";
import { Badge, Notice } from "../components/common";
import { fmtTime } from "../format";

export default function Runs() {
  const [runs, setRuns] = useState<Job[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { api.runs().then(setRuns).catch((e) => setError((e as Error).message)); }, []);

  if (error) return <Notice kind="fail">{error}</Notice>;
  if (!runs) return <p><span className="spinner" /> Loading…</p>;
  return (
    <>
      <h1>My analyses</h1>
      {runs.length === 0 ? (
        <p className="muted">No analyses yet. <Link to="/">Start one</Link>.</p>
      ) : (
        <div className="table-wrap">
          <table>
            <thead><tr><th>Comparison</th><th>Dataset</th><th>Accounting for</th><th>Started</th><th>Status</th></tr></thead>
            <tbody>
              {runs.map((j) => (
                <tr key={j.id}>
                  <td><Link to={`/runs/${j.id}`}>{j.settings.test_level} vs {j.settings.reference_level}</Link></td>
                  <td>{j.dataset}</td>
                  <td>{j.settings.covariates.join(", ") || "–"}</td>
                  <td>{fmtTime(j.created)}</td>
                  <td><Badge status={j.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
