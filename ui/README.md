# Caligo UI

The chat view uses [assistant-ui's prebuilt Thread and Composer](https://github.com/assistant-ui/assistant-ui). The library owns message rendering, scrolling, input behavior, copy actions, and Markdown. `caligo-chat.tsx` connects it to Caligo's existing events and send/steer/pause operations. Its component bundle, runtime, and internal state package are pinned together for compatibility.

Caligo supplies the agent sidebar, creation form, and experiment drawer. The shared button, input, and tooltip primitives were adapted from LangChain Agent Chat UI, revision `088908b8187293163eea3ace92724aac47ff5d59` (MIT; see [LICENSE](LICENSE)).

Build with Node 22+ and pnpm:

```sh
pnpm --dir ui install --frozen-lockfile
pnpm --dir ui build
caligo ui
```

The Python gateway serves `out/`; the built application does not require Node or a LangSmith account. Build before creating a Python wheel so the frontend assets are included.

For frontend development, run `caligo ui --port 8765 --no-open` and `pnpm --dir ui dev`. Set `CALIGO_API_URL` if the gateway uses another address.

`pnpm --dir ui check` checks TypeScript. `pnpm --dir ui test` checks event replay, delivery status, and streamed message conversion.
