import { useEffect, useState } from "react";
import { Download, FlaskConical } from "lucide-react";
import {
  api,
  changing,
  finished,
  labels,
  paused,
  type Run,
} from "@/lib/caligo";
import { useCaligoContext } from "@/providers/caligo";
import { Button } from "./ui/button";
import { Panel } from "./panel";

function Experiment({
  run,
  base,
  refresh,
}: {
  run: Run;
  base: string;
  refresh: () => void;
}) {
  const [logs, setLogs] = useState<string>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function showLogs() {
    if (logs !== undefined) {
      setLogs(undefined);
      return;
    }
    try {
      setLogs(
        (await api<{ logs: string }>(`${base}/runs/${run.id}/logs`)).logs ||
          "No output yet.",
      );
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function cancel() {
    setBusy(true);
    try {
      await api(`${base}/runs/${run.id}/cancel`, {});
      refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <article className="experiment">
      <header>
        <span className="run-state">
          {labels[run.state] || run.state.toLowerCase().replaceAll("_", " ")}
        </span>
        <time dateTime={run.created_at}>
          {new Date(run.created_at).toLocaleString(undefined, {
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
          })}
        </time>
      </header>
      <p className="run-id">{run.id}</p>
      <div className="run-actions">
        <Button variant="outline" size="sm" onClick={showLogs}>
          {logs === undefined ? "Logs" : "Hide logs"}
        </Button>
        <Button variant="outline" size="sm" asChild>
          <a href={`${base}/runs/${run.id}/download`} download>
            <Download />
            Download
          </a>
        </Button>
        {[
          "PENDING",
          "RUNNING",
          "SUBMITTED",
          "REQUEUED",
          "CONFIGURING",
        ].includes(run.state) && (
          <Button variant="ghost" size="sm" disabled={busy} onClick={cancel}>
            Cancel
          </Button>
        )}
      </div>
      {logs !== undefined && <pre className="run-logs">{logs}</pre>}
      {(error || run.poll_error) && (
        <p className="error" role="alert">
          {error || run.poll_error}
        </p>
      )}
    </article>
  );
}

export function Experiments({ onClose }: { onClose: () => void }) {
  const { base, thread, control } = useCaligoContext();
  const [runs, setRuns] = useState<Run[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    const refresh = () =>
      api<{ runs: Run[] }>(base + "/runs", undefined, abort.signal)
        .then((result) => {
          setRuns(result.runs.toReversed());
          setError("");
        })
        .catch((e) => {
          if (!abort.signal.aborted) setError(e.message);
        });
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => {
      abort.abort();
      clearInterval(timer);
    };
  }, [base, refreshKey]);
  async function run() {
    setBusy(true);
    try {
      await control("run");
      setRefreshKey((k) => k + 1);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Panel
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title="Experiments"
      side="right"
    >
      <div className="experiments-content">
        <Button
          onClick={run}
          disabled={
            busy ||
            !thread ||
            finished(thread.state) ||
            paused(thread.state) ||
            changing(thread.state)
          }
        >
          <FlaskConical />
          {busy ? "Submitting…" : "Run experiment"}
        </Button>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        {!runs.length && !error && (
          <p className="muted empty-runs">No experiments yet.</p>
        )}
        {runs.map((run) => (
          <Experiment
            key={run.id}
            run={run}
            base={base}
            refresh={() => setRefreshKey((k) => k + 1)}
          />
        ))}
      </div>
    </Panel>
  );
}
