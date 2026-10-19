import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { TooltipIconButton } from "./thread/tooltip-icon-button";

export function Panel({
  open,
  onOpenChange,
  title,
  side,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  side?: "left" | "right";
  children: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="panel-overlay" />
        <Dialog.Content
          className={`panel ${side ? `panel-${side}` : "panel-center"}`}
          aria-describedby={undefined}
        >
          <header className="panel-header">
            <Dialog.Title>{title}</Dialog.Title>
            <Dialog.Close asChild>
              <TooltipIconButton tooltip={`Close ${title.toLowerCase()}`}>
                <X />
              </TooltipIconButton>
            </Dialog.Close>
          </header>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
