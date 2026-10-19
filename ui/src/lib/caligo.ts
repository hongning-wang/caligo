export type Backend = "codex" | "claude";
export type Delivery = "queue" | "steer" | "interrupt";
export type AgentThread = {
  id: string;
  attempt: string;
  state: string;
  backend: Backend;
  model: string | null;
  goal: string;
  continuous: boolean;
};
export type Agent = {
  id: string;
  name: string;
  threads: AgentThread[];
  error?: string;
};
export type Listing = {
  project: string;
  agents: Agent[];
  default_model?: string;
  capabilities: Partial<Record<Backend, { steer: boolean }>>;
};
export type Run = {
  id: string;
  state: string;
  created_at: string;
  exit_code?: number;
  poll_error?: string;
};
export type ChatMessage = {
  id: string;
  type: "human" | "ai" | "tool";
  content: string;
  name?: string;
  status?: string;
};
export type AgentEvent = {
  seq: number;
  type: string;
  data: Record<string, any>;
};

export const labels: Record<string, string> = {
  STARTING: "Starting",
  RUNNING: "Working",
  WAITING: "Ready",
  PAUSING: "Pausing",
  PAUSED: "Paused",
  RETRYING: "Reconnecting",
  BLOCKED: "Needs input",
  FAILED: "Failed",
  STOPPING: "Stopping",
  STOPPED: "Stopped",
  COMPLETING: "Finishing",
  COMPLETED: "Completed",
};
export const finished = (state: string) =>
  ["STOPPED", "COMPLETED"].includes(state);
export const paused = (state: string) =>
  ["PAUSED", "FAILED", "BLOCKED"].includes(state);
export const changing = (state: string) =>
  ["STARTING", "RETRYING", "PAUSING", "STOPPING", "COMPLETING"].includes(state);
export const threadPath = (agent: string, thread: string) =>
  `/api/agents/${agent}/threads/${thread}`;

export async function api<T>(
  path: string,
  values?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(path, {
    ...(values === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(values),
        }),
    signal,
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Connection failed");
  return result;
}

/** Translate Caligo's durable events into the Agent Chat UI message model. */
export class Conversation {
  messages: ChatMessage[] = [];
  cursor = 0;
  private group: string | null = null;
  private decoders = new Map<string, TextDecoder>();

  private append(
    event: AgentEvent,
    type: ChatMessage["type"],
    text: string,
    name?: string,
  ) {
    const last = this.messages.at(-1);
    if (this.group === type && last?.type === type) {
      this.messages[this.messages.length - 1] = {
        ...last,
        content: last.content + text,
      };
    } else {
      this.messages.push({ id: String(event.seq), type, content: text, name });
    }
    this.group = type;
  }

  accept(event: AgentEvent) {
    if (event.seq <= this.cursor) return false;
    this.cursor = event.seq;
    const { type, data } = event;
    if (
      type === "message.accepted" &&
      !["caligo", "scheduler"].includes(data.author)
    ) {
      this.messages.push({
        id: data.id,
        type: "human",
        content: data.text,
        status: "Queued",
      });
      this.group = null;
    } else if (type.startsWith("message.") && type !== "message.accepted") {
      const index = this.messages.findIndex((m) => m.id === data.id);
      if (index >= 0)
        this.messages[index] = {
          ...this.messages[index],
          status: (
            {
              delivered: "",
              sending: "Sending",
              failed: "Failed",
              delivery_unknown: "Delivery unconfirmed",
            } as Record<string, string>
          )[type.slice(8)],
        };
    } else if (type === "agent.text") {
      this.append(event, "ai", data.text || data.delta || "");
    } else if (type === "turn.started" || type === "turn.completed") {
      this.group = null;
    } else if (["tool.output", "dev.output", "runtime.stderr"].includes(type)) {
      let text = data.text || data.delta || "";
      if (type === "dev.output") {
        if (!this.decoders.has(data.channel))
          this.decoders.set(data.channel, new TextDecoder());
        text = this.decoders.get(data.channel)!.decode(
          Uint8Array.from(atob(data.data), (c) => c.charCodeAt(0)),
          { stream: true },
        );
      }
      this.append(
        event,
        "tool",
        text,
        type === "dev.output" ? "Development output" : "Tool output",
      );
    } else if (type === "backend.event") {
      const item = data.params?.item;
      if (data.method === "item/started" && item?.type === "commandExecution") {
        this.group = null;
        this.append(event, "tool", (item.command || "") + "\n", "Command");
      }
      for (const block of data.message?.content || []) {
        if (block.name && block.input) {
          this.group = null;
          this.append(
            event,
            "tool",
            JSON.stringify(block.input, null, 2) + "\n",
            block.name,
          );
        } else if (block.tool_use_id) {
          this.append(
            event,
            "tool",
            typeof block.content === "string"
              ? block.content
              : JSON.stringify(block.content, null, 2),
            "Tool result",
          );
        }
      }
    } else if (type === "goal.completed" || type === "goal.blocked") {
      this.group = null;
      this.append(event, "ai", data.summary);
    }
    return true;
  }
}
