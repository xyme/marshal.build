import { Toaster } from "sonner";
import AppShell from "@/components/shell/AppShell";
import { ConfirmProvider } from "@/components/ui/ConfirmDialog";
import { UserProvider } from "@/components/user-context";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <UserProvider>
      <ConfirmProvider>
        <AppShell>{children}</AppShell>
        <Toaster
          theme="dark"
          position="bottom-right"
          toastOptions={{
            style: { background: "#0f172a", border: "1px solid #334155", color: "#e2e8f0" },
          }}
        />
      </ConfirmProvider>
    </UserProvider>
  );
}
