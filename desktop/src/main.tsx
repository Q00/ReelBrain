import { Component, StrictMode, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

class AppErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ReelBrain UI failed", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <main className="fatal-error">
          <strong>ReelBrain could not open this workspace.</strong>
          <p>{this.state.error.message}</p>
          <button onClick={() => window.location.reload()}>Reload ReelBrain</button>
        </main>
      );
    }
    return this.props.children;
  }
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppErrorBoundary><App /></AppErrorBoundary>
  </StrictMode>,
);
