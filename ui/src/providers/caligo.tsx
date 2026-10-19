"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import {
  api,
  Conversation,
  threadPath,
  type AgentEvent,
  type ChatMessage,
  type Listing,
} from "@/lib/caligo";

function useCaligo() {
  const [listing, setListing] = useState<Listing>({
    project: "",
    agents: [],
    capabilities: {},
  });
  const [selection, setSelection] = useState<{
    agent: string;
    thread?: string;
  }>();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [reconnecting, setReconnecting] = useState(false);
  const [error, setError] = useState("");
  const agent = listing.agents.find((a) => a.id === selection?.agent);
  const thread =
    agent?.threads.find((t) => t.id === selection?.thread) ||
    agent?.threads.at(-1);
  const base = agent && thread ? threadPath(agent.id, thread.id) : "";

  const refresh = useCallback(async () => {
    const next = await api<Listing>("/api/agents");
    setListing(next);
    document.title = `${next.project} · Caligo`;
    setSelection((current) => {
      if (current && next.agents.some((a) => a.id === current.agent))
        return current;
      const [id, thread] = location.hash.slice(1).split("/");
      const selected =
        next.agents.find((a) => a.id === id) || next.agents.at(-1);
      return selected ? { agent: selected.id, thread } : undefined;
    });
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh().catch((e) => {
      setError(e.message);
      setLoading(false);
    });
    const timer = setInterval(
      () => refresh().catch((e) => setError(e.message)),
      10000,
    );
    return () => clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    setMessages([]);
    setError("");
    setReconnecting(false);
    if (!base) return;
    location.hash = `${agent!.id}/${thread!.id}`;
    const conversation = new Conversation();
    const source = new EventSource(base + "/events");
    source.onopen = () => setReconnecting(false);
    source.onerror = () => setReconnecting(true);
    source.onmessage = ({ data }) => {
      const event: AgentEvent = JSON.parse(data);
      if (!conversation.accept(event)) return;
      setMessages([...conversation.messages]);
      if (event.type === "thread.updated" && event.data.state) {
        setListing((current) => ({
          ...current,
          agents: current.agents.map((a) => ({
            ...a,
            threads: a.threads.map((t) =>
              t.id === thread!.id ? { ...t, state: event.data.state } : t,
            ),
          })),
        }));
      }
      if (
        [
          "agent.error",
          "agent.blocked",
          "runtime.error",
          "command.failed",
        ].includes(event.type)
      )
        setError(event.data.error || "Agent needs attention.");
    };
    return () => source.close();
    // Reconnect only when the selected conversation changes.
  }, [base]);

  async function control(action: string, values: unknown = {}) {
    setError("");
    try {
      const result = await api(base + "/" + action, values);
      await refresh();
      return result;
    } catch (e) {
      setError((e as Error).message);
      throw e;
    }
  }

  return {
    ...listing,
    agent,
    thread,
    base,
    messages,
    loading,
    reconnecting,
    error,
    setError,
    refresh,
    control,
    select: (agent: string, thread?: string) => {
      setSelection({ agent, thread });
    },
  };
}

const CaligoContext = createContext<ReturnType<typeof useCaligo> | null>(null);
export function CaligoProvider({ children }: { children: ReactNode }) {
  const caligo = useCaligo();
  return (
    <CaligoContext.Provider value={caligo}>{children}</CaligoContext.Provider>
  );
}
export function useCaligoContext() {
  const value = useContext(CaligoContext);
  if (!value) throw new Error("CaligoProvider is required");
  return value;
}
