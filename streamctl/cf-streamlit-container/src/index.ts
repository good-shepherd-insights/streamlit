// Streamlit inside a Cloudflare Container: the Worker holds the DO class that
// boots the instance and proxies every request to it (getStarted helpers).
import { Container, getContainer } from "@cloudflare/containers";

export class StreamlitContainer extends Container {
  defaultPort = 8501;
  // Boot on first request; idle-shutdown after 5 min to keep the free tier lean.
  sleepAfter = 300_000;
  envVars = {
    STREAMLIT_SERVER_PORT: "8501",
    STREAMLIT_SERVER_ADDRESS: "0.0.0.0",
    STREAMLIT_SERVER_HEADLESS: "true",
  };

  async onStart() {
    console.log("streamlit container started");
  }
}

export default {
  async fetch(request: Request, env: { MY_CONTAINER: DurableObjectNamespace }, ctx: ExecutionContext): Promise<Response> {
    return getContainer(env.MY_CONTAINER, "streamlit").fetch(request);
  },
};