// Caligo's conversations use the familiar ChatGPT sidebar and composer layout.
import { useRef, useState } from "react";
import {
  FlaskConical,
  MoreHorizontal,
  PanelLeft,
  Pause,
  Play,
  Search,
  Square,
  SquarePen,
  X,
} from "lucide-react";
import { changing, finished, labels, paused } from "@/lib/caligo";
import { useCaligoContext } from "@/providers/caligo";
import { Button } from "../ui/button";
import { CreateAgent } from "../create-agent";
import { Experiments } from "../experiments";
import { Panel } from "../panel";
import { TooltipIconButton } from "./tooltip-icon-button";
import { CaligoChat } from "./caligo-chat";

function Sidebar({
  onCreate,
  onCollapse,
  onSelect,
}: {
  onCreate: () => void;
  onCollapse?: () => void;
  onSelect?: () => void;
}) {
  const { project, agents, agent, select } = useCaligoContext();
  const [search, setSearch] = useState(false);
  const [query, setQuery] = useState("");
  return (
    <>
      <div className="sidebar-heading">
        <span className="brand">Caligo</span>
        <div>
          <TooltipIconButton
            tooltip="Search agents"
            onClick={() => setSearch(!search)}
          >
            <Search />
          </TooltipIconButton>
          {onCollapse && (
            <TooltipIconButton tooltip="Close sidebar" onClick={onCollapse}>
              <PanelLeft />
            </TooltipIconButton>
          )}
        </div>
      </div>
      <Button variant="ghost" className="new-agent" onClick={onCreate}>
        <SquarePen />
        New agent
      </Button>
      {search && (
        <div className="sidebar-search">
          <Search size={16} />
          <input
            aria-label="Search agents"
            autoFocus
            placeholder="Search agents"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                setSearch(false);
                setQuery("");
              }
            }}
          />
        </div>
      )}
      <div className="sidebar-label">Agents</div>
      <nav aria-label="Agents">
        {agents
          .filter(
            (item) =>
              !search || item.name.toLowerCase().includes(query.toLowerCase()),
          )
          .map((item) => (
            <Button
              key={item.id}
              variant="ghost"
              className="agent-item"
              aria-current={item.id === agent?.id}
              onClick={() => {
                select(item.id);
                onSelect?.();
              }}
            >
              <span className="agent-name">{item.name}</span>
              {["RUNNING", "STARTING"].includes(
                item.threads.at(-1)?.state || "",
              ) && <span className="status-dot" />}
            </Button>
          ))}
      </nav>
      <div className="workspace">
        <span className="workspace-avatar">
          {(project || "C").slice(0, 1).toUpperCase()}
        </span>
        <span>{project}</span>
      </div>
    </>
  );
}

export function Thread() {
  const {
    agents,
    agent,
    thread,
    loading,
    reconnecting,
    error,
    setError,
    select,
    control,
  } = useCaligoContext();
  const [create, setCreate] = useState<{ attempt?: string }>();
  const [experiments, setExperiments] = useState(false);
  const [navigation, setNavigation] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const menu = useRef<HTMLDetailsElement>(null);
  async function action(name: string) {
    if (menu.current) menu.current.open = false;
    try {
      await control(name);
    } catch {
      /* The provider displays the error. */
    }
  }
  const closed = thread && finished(thread.state);
  return (
    <div className={`app-shell ${collapsed ? "sidebar-collapsed" : ""}`}>
      <aside className="sidebar">
        <Sidebar
          onCreate={() => setCreate({})}
          onCollapse={() => setCollapsed(true)}
        />
      </aside>
      {navigation && (
        <Panel open onOpenChange={setNavigation} side="left" title="Caligo">
          <Sidebar
            onCreate={() => {
              setNavigation(false);
              setCreate({});
            }}
            onSelect={() => setNavigation(false)}
          />
        </Panel>
      )}
      <main className="conversation">
        <header className="conversation-header">
          <div className="heading">
            <TooltipIconButton
              tooltip="Open sidebar"
              className="open-sidebar"
              onClick={() =>
                window.matchMedia("(max-width: 760px)").matches
                  ? setNavigation(true)
                  : setCollapsed(false)
              }
            >
              <PanelLeft />
            </TooltipIconButton>
            <h1>{agent?.name || "Caligo"}</h1>
            {thread && (
              <span className="state">
                {thread.state === "RUNNING" && <span className="status-dot" />}
                {labels[thread.state] || thread.state}
              </span>
            )}
          </div>
          {thread && (
            <div className="header-actions">
              {paused(thread.state) && (
                <Button
                  variant="outline"
                  className="resume-agent"
                  onClick={() => action("resume")}
                >
                  <Play size={14} />
                  Resume
                </Button>
              )}
              <Button
                variant="outline"
                className="experiments-button"
                aria-label="Experiments"
                onClick={() => setExperiments(true)}
              >
                <FlaskConical />
                <span>Experiments</span>
              </Button>
              {closed && (
                <Button
                  variant="outline"
                  className="new-thread"
                  onClick={() =>
                    agent!.threads.at(-1)!.id === thread.id
                      ? setCreate({ attempt: agent!.id })
                      : select(agent!.id)
                  }
                >
                  {agent!.threads.at(-1)!.id === thread.id
                    ? "New thread"
                    : "Latest thread"}
                </Button>
              )}
              <details className="thread-menu" ref={menu}>
                <summary aria-label="Conversation options">
                  <MoreHorizontal size={21} />
                </summary>
                <div className="menu-popup">
                  <div className="model-info">
                    {thread.backend === "codex" ? "Codex" : "Claude"}
                    {thread.model && <span>{thread.model}</span>}
                  </div>
                  {agent!.threads.length > 1 && (
                    <label className="history-label">
                      Thread
                      <select
                        aria-label="Thread history"
                        value={thread.id}
                        onChange={(e) => {
                          select(agent!.id, e.target.value);
                          if (menu.current) menu.current.open = false;
                        }}
                      >
                        {agent!.threads.map((t, i) => (
                          <option key={t.id} value={t.id}>
                            Thread {i + 1}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {!closed && (
                    <>
                      <Button
                        variant="ghost"
                        disabled={changing(thread.state)}
                        onClick={() =>
                          action(paused(thread.state) ? "resume" : "pause")
                        }
                      >
                        {paused(thread.state) ? <Play /> : <Pause />}
                        {paused(thread.state) ? "Resume agent" : "Pause agent"}
                      </Button>
                      <Button
                        variant="ghost"
                        disabled={changing(thread.state)}
                        onClick={() => action("stop")}
                      >
                        <Square />
                        Stop agent
                      </Button>
                    </>
                  )}
                </div>
              </details>
            </div>
          )}
        </header>
        {reconnecting && (
          <p className="connection" role="status">
            Reconnecting…
          </p>
        )}
        {error && (
          <div className="error-banner" role="alert">
            <p>{error}</p>
            <TooltipIconButton
              tooltip="Dismiss error"
              onClick={() => setError("")}
            >
              <X />
            </TooltipIconButton>
          </div>
        )}
        {thread ? (
          <div className="chat" key={thread.id}>
            <CaligoChat />
          </div>
        ) : (
          <div className="empty-state">
            <h2>{loading ? "Loading…" : "What are we working on?"}</h2>
            {!loading && (
              <>
                {agent?.error && <p className="error">{agent.error}</p>}
                <Button
                  onClick={() => setCreate(agent ? { attempt: agent.id } : {})}
                >
                  <SquarePen />
                  {agent ? "Start agent" : "New agent"}
                </Button>
                {agents.length > 0 && !agent && (
                  <p>Choose an agent from the sidebar.</p>
                )}
              </>
            )}
          </div>
        )}
      </main>
      {create && (
        <CreateAgent
          key={create.attempt || "new"}
          attempt={create.attempt}
          onClose={() => setCreate(undefined)}
        />
      )}
      {experiments && thread && (
        <Experiments key={thread.id} onClose={() => setExperiments(false)} />
      )}
    </div>
  );
}
