import {
  createContext,
  useCallback,
  useContext,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import {
  AssistantRuntimeProvider,
  useComposer,
  useComposerRuntime,
  useExternalStoreRuntime,
  useMessage,
  type ThreadMessageLike,
  type ToolCallMessagePartProps,
} from "@assistant-ui/react";
import {
  Composer,
  Thread as ChatThread,
  UserMessage,
  makeMarkdownText,
} from "@assistant-ui/react-ui";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { ArrowUp } from "lucide-react";
import {
  api,
  changing,
  finished,
  paused,
  type ChatMessage,
  type Delivery,
} from "@/lib/caligo";
import { useCaligoContext } from "@/providers/caligo";

const Markdown = makeMarkdownText({
  remarkPlugins: [remarkGfm, remarkMath],
  rehypePlugins: [rehypeKatex],
});
const SendContext = createContext<
  (text: string, delivery: Delivery) => Promise<void>
>(async () => {});
const convertMessage = (message: ChatMessage): ThreadMessageLike => ({
  id: message.id,
  role: message.type === "human" ? "user" : "assistant",
  content:
    message.type === "tool"
      ? [
          {
            type: "tool-call",
            toolCallId: message.id,
            toolName: message.name || "Tool output",
            args: {},
            argsText: "{}",
            result: message.content,
          },
        ]
      : [{ type: "text", text: message.content }],
  metadata: { custom: { delivery: message.status } },
});

function ToolOutput({ toolName, result }: ToolCallMessagePartProps) {
  return (
    <details className="caligo-tool">
      <summary>{toolName}</summary>
      <pre>
        {typeof result === "string" ? result : JSON.stringify(result, null, 2)}
      </pre>
    </details>
  );
}

function UserInstruction() {
  const delivery = useMessage((message) => message.metadata.custom.delivery);
  return (
    <UserMessage.Root>
      <UserMessage.Content />
      {typeof delivery === "string" && delivery && (
        <small className="caligo-delivery">{delivery}</small>
      )}
    </UserMessage.Root>
  );
}

function CaligoComposer() {
  const send = useContext(SendContext);
  const { thread, capabilities } = useCaligoContext();
  const composer = useComposerRuntime();
  const text = useComposer((s) => s.text);
  const [delivery, setDelivery] = useState<Delivery>();
  const working = thread!.state === "RUNNING";
  const canSteer = Boolean(capabilities[thread!.backend]?.steer);
  const mode =
    delivery === "steer" && !canSteer
      ? "queue"
      : delivery || (canSteer ? "steer" : "queue");
  async function sendWhileWorking() {
    if (!text.trim()) return;
    try {
      await send(text.trim(), mode);
      await composer.reset();
    } catch {
      /* The adapter displays the error and preserves the input. */
    }
  }
  if (finished(thread!.state)) return null;
  return (
    <Composer.Root
      onKeyDownCapture={(event: KeyboardEvent<HTMLFormElement>) => {
        if (
          working &&
          event.key === "Enter" &&
          !event.shiftKey &&
          !event.nativeEvent.isComposing
        ) {
          event.preventDefault();
          event.stopPropagation();
          void sendWhileWorking();
        }
      }}
    >
      <Composer.Input aria-label="Message" />
      {working && (
        <select
          className="caligo-send-mode"
          aria-label="Message timing"
          value={mode}
          onChange={(e) => setDelivery(e.target.value as Delivery)}
        >
          {canSteer && <option value="steer">Send now</option>}
          <option value="queue">After this response</option>
          <option value="interrupt">Interrupt and send</option>
        </select>
      )}
      {working ? (
        <>
          <Composer.Cancel />
          {text.trim() && (
            <button
              type="button"
              className="aui-composer-send caligo-send"
              aria-label="Send message"
              onClick={sendWhileWorking}
            >
              <ArrowUp size={19} />
            </button>
          )}
        </>
      ) : (
        <Composer.Send>
          <ArrowUp size={19} />
        </Composer.Send>
      )}
    </Composer.Root>
  );
}

export function CaligoChat() {
  const { agent, base, thread, messages, control, setError } =
    useCaligoContext();
  const pending = useRef<{
    text: string;
    delivery: Delivery;
    id: string;
  } | null>(null);
  const send = useCallback(
    async (text: string, delivery: Delivery) => {
      if (
        pending.current?.text !== text ||
        pending.current?.delivery !== delivery
      )
        pending.current = { text, delivery, id: crypto.randomUUID() };
      setError("");
      try {
        await api(
          base + (delivery === "interrupt" ? "/interrupt" : "/messages"),
          delivery === "interrupt" ? { text } : pending.current,
        );
        if (paused(thread!.state)) await control("resume");
        pending.current = null;
      } catch (e) {
        setError((e as Error).message);
        throw e;
      }
    },
    [base, thread?.state, control, setError],
  );
  const runtime = useExternalStoreRuntime<ChatMessage>({
    messages,
    convertMessage,
    isRunning: thread!.state === "RUNNING",
    isDisabled: finished(thread!.state) || changing(thread!.state),
    onNew: (message) =>
      send(
        message.content
          .filter((part) => part.type === "text")
          .map((part) => part.text)
          .join(""),
        "queue",
      ),
    onCancel: async () => {
      await control("pause");
    },
  });
  return (
    <SendContext.Provider value={send}>
      <AssistantRuntimeProvider runtime={runtime}>
        <ChatThread
          welcome={{ message: null }}
          composer={{ allowAttachments: false }}
          branchPicker={{ allowBranchPicker: false }}
          userMessage={{ allowEdit: false }}
          assistantMessage={{
            allowCopy: true,
            allowReload: false,
            allowSpeak: false,
            allowFeedbackPositive: false,
            allowFeedbackNegative: false,
            components: { Text: Markdown, ToolFallback: ToolOutput },
          }}
          strings={{
            composer: {
              input: { placeholder: `Message ${agent!.name}` },
              send: { tooltip: "Send message" },
              cancel: { tooltip: "Stop response" },
            },
          }}
          components={{
            Composer: CaligoComposer,
            UserMessage: UserInstruction,
            ThreadWelcome: () => null,
          }}
        />
      </AssistantRuntimeProvider>
    </SendContext.Provider>
  );
}
