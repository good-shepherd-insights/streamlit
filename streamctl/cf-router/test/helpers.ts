// Mock fetch helper: records every CF API call and answers from
// scripted routes. Responders may be static CF bodies {success, errors,
// result} or stateful functions receiving per-call state.
export interface RecordedCall {
  url: string;
  method: string;
  body?: unknown;
}

export interface MockResponse {
  /** CF API success flag; when false, errors[0].message is surfaced verbatim. */
  ok?: boolean;
  errors?: { code: number; message: string }[];
  result?: unknown;
}

interface MockRoute {
  test: (url: string, method: string) => boolean;
  respond: MockResponse | ((callsSoFar: number, call: RecordedCall) => MockResponse);
}

export function recordingFetch() {
  const calls: RecordedCall[] = [];
  const routes: MockRoute[] = [];

  const fetchMock = Object.assign(
    (url: string, init?: RequestInit): Promise<Response> => {
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url, method, body: init?.body });
      const route = routes.find((r) => r.test(url, method));
      if (!route) return Promise.resolve(new Response(JSON.stringify({ result: null }), { status: 200 }));
      const priorCalls = calls.slice(0, -1).filter((c) => route.test(c.url, c.method)).length;
      const resp =
        typeof route.respond === "function" ? route.respond(priorCalls, { url, method, body: init?.body }) : route.respond;
      return Promise.resolve(
        new Response(JSON.stringify(resp), {
          status: resp.ok === false ? 400 : 200,
          headers: { "content-type": "application/json" },
        }),
      );
    },
    {
      calls,
      respondTo(
        test: (url: string, method: string) => boolean,
        respond: MockResponse | ((callsSoFar: number, call: RecordedCall) => MockResponse),
      ) {
        routes.push({ test, respond });
      },
      reset() {
        calls.length = 0;
        routes.length = 0;
      },
    },
  ) as {
    (url: string, init?: RequestInit): Promise<Response>;
    calls: RecordedCall[];
    respondTo: (
      test: (url: string, method: string) => boolean,
      respond: MockResponse | ((callsSoFar: number, call: RecordedCall) => MockResponse),
    ) => void;
    reset: () => void;
  };

  return fetchMock;
}