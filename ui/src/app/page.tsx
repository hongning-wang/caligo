"use client";

import { CaligoProvider } from "@/providers/caligo";
import { Thread } from "@/components/thread";

export default function Page() {
  return (
    <CaligoProvider>
      <Thread />
    </CaligoProvider>
  );
}
