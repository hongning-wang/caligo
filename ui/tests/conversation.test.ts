import { test } from "node:test";
import assert from "node:assert/strict";
import { Conversation } from "../src/lib/caligo.ts";

test("replay ignores duplicate events and preserves separate turns", () => {
  const chat = new Conversation();
  chat.accept({ seq: 1, type: "agent.text", data: { text: "First " } });
  chat.accept({ seq: 2, type: "agent.text", data: { delta: "answer" } });
  assert.equal(
    chat.accept({ seq: 2, type: "agent.text", data: { delta: "answer" } }),
    false,
  );
  chat.accept({ seq: 3, type: "turn.completed", data: {} });
  chat.accept({ seq: 4, type: "agent.text", data: { text: "Second answer" } });
  assert.deepEqual(
    chat.messages.map((m) => m.content),
    ["First answer", "Second answer"],
  );
});

test("queued instructions and delivery updates do not alter agent text", () => {
  const chat = new Conversation();
  chat.accept({ seq: 1, type: "agent.text", data: { text: "Working" } });
  chat.accept({
    seq: 2,
    type: "message.accepted",
    data: { id: "queued", text: "Check this", author: "operator" },
  });
  chat.accept({
    seq: 3,
    type: "message.delivery_unknown",
    data: { id: "queued" },
  });
  chat.accept({ seq: 4, type: "agent.text", data: { text: "Continued" } });
  assert.ok(chat.messages[1].status);
  assert.deepEqual(
    chat.messages.map((m) => m.content),
    ["Working", "Check this", "Continued"],
  );
});

test("tool output combines chunks and decodes split UTF-8", () => {
  const chat = new Conversation();
  chat.accept({
    seq: 1,
    type: "backend.event",
    data: {
      method: "item/started",
      params: {
        item: { type: "commandExecution", command: "python check.py" },
      },
    },
  });
  chat.accept({ seq: 2, type: "tool.output", data: { delta: "passed\n" } });
  chat.accept({ seq: 3, type: "turn.completed", data: {} });
  const bytes = Buffer.from("accuracy ✓");
  chat.accept({
    seq: 4,
    type: "dev.output",
    data: { channel: "stdout", data: bytes.subarray(0, -1).toString("base64") },
  });
  chat.accept({
    seq: 5,
    type: "dev.output",
    data: { channel: "stdout", data: bytes.subarray(-1).toString("base64") },
  });
  assert.ok(chat.messages[0].content.includes("python check.py"));
  assert.ok(chat.messages[0].content.includes("passed"));
  assert.equal(chat.messages[1].content, "accuracy ✓");
});

test("scheduler reminders stay hidden and completion evidence is visible", () => {
  const chat = new Conversation();
  chat.accept({
    seq: 1,
    type: "message.accepted",
    data: { id: "auto", author: "caligo", text: "Review objective" },
  });
  chat.accept({
    seq: 2,
    type: "message.accepted",
    data: { id: "result", author: "scheduler", text: "Run finished" },
  });
  chat.accept({
    seq: 3,
    type: "goal.completed",
    data: { summary: "Verified accuracy is 91%." },
  });
  assert.equal(chat.messages.length, 1);
  assert.equal(chat.messages[0].content, "Verified accuracy is 91%.");
});
