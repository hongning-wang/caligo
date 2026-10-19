import { useState, type FormEvent } from "react";
import { api, type Backend } from "@/lib/caligo";
import { useCaligoContext } from "@/providers/caligo";
import { Panel } from "./panel";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Textarea } from "./ui/textarea";

export function CreateAgent({
  attempt,
  onClose,
}: {
  attempt?: string;
  onClose: () => void;
}) {
  const { agents, thread, default_model, refresh, select } = useCaligoContext();
  const [attemptId, setAttemptId] = useState(attempt);
  const [backend, setBackend] = useState<Backend>(
    attempt && thread ? thread.backend : "codex",
  );
  const [model, setModel] = useState((attempt && thread?.model) || "");
  const [custom, setCustom] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const models = new Set(
    agents
      .flatMap((a) => a.threads)
      .filter((t) => t.backend === backend)
      .map((t) => t.model)
      .filter(Boolean) as string[],
  );
  if (default_model) models.add(default_model);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const name = String(data.get("name") || "").trim();
    setError("");
    setBusy(true);
    try {
      const result = await api<{ attempt: string; thread: { id: string } }>(
        "/api/agents",
        {
          attempt: attemptId,
          name,
          backend,
          model: (model === "__custom__" ? custom.trim() : model) || null,
          goal: String(data.get("goal")).trim(),
          continuous: data.has("continuous"),
        },
      );
      await refresh();
      select(result.attempt, result.thread.id);
      onClose();
    } catch (e) {
      setError((e as Error).message);
      // Attempt creation may have succeeded before the backend failed to start.
      try {
        const listing = await api<{
          agents: { id: string; name: string; threads: unknown[] }[];
        }>("/api/agents");
        const created = listing.agents.find(
          (a) => a.name === name && !a.threads.length,
        );
        if (created) setAttemptId(created.id);
      } catch {
        /* Preserve the form and its original error. */
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
      title={attempt ? "New thread" : "New agent"}
    >
      <form className="create-form" onSubmit={submit}>
        {!attempt && (
          <label>
            Name
            <Input
              name="name"
              required
              autoFocus
              autoComplete="off"
              pattern="[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}"
              title="Use letters, numbers, hyphens, or underscores."
              placeholder="e.g. regularization"
            />
          </label>
        )}
        <div className="form-row">
          <label>
            Backend
            <select
              name="backend"
              value={backend}
              onChange={(e) => {
                setBackend(e.target.value as Backend);
                setModel("");
              }}
            >
              <option value="codex">Codex</option>
              <option value="claude">Claude</option>
            </select>
          </label>
          <label>
            Model
            <select
              name="model"
              value={model}
              onChange={(e) => setModel(e.target.value)}
            >
              <option value="">
                {default_model ? `Default (${default_model})` : "Default"}
              </option>
              {[...models].map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
              <option value="__custom__">Other…</option>
            </select>
          </label>
        </div>
        {model === "__custom__" && (
          <label>
            Model ID
            <Input
              name="custom_model"
              required
              autoFocus
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              autoComplete="off"
            />
          </label>
        )}
        <label>
          Objective
          <Textarea
            name="goal"
            rows={4}
            required
            autoFocus={Boolean(attempt)}
            defaultValue={attempt ? thread?.goal : ""}
            placeholder="What should the agent work on?"
          />
        </label>
        <label className="checkbox">
          <input
            name="continuous"
            type="checkbox"
            defaultChecked={attempt ? Boolean(thread?.continuous) : true}
          />
          Work until done
        </label>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <Button type="submit" disabled={busy}>
          {busy ? "Starting…" : attempt ? "Start thread" : "Start agent"}
        </Button>
      </form>
    </Panel>
  );
}
