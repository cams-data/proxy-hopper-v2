import {
  cacheExchange,
  createClient,
  fetchExchange,
  type Client,
} from "urql";
import { getAdminUrl, getAuthHeaders } from "./auth";

let _client: Client | null = null;
let _clientUrl = "";

export function getClient(): Client {
  const url = `${getAdminUrl()}/graphql`;
  if (_client && _clientUrl === url) return _client;

  _client = createClient({
    url,
    exchanges: [cacheExchange, fetchExchange],
    fetchOptions: () => ({
      headers: {
        "Content-Type": "application/json",
        ...getAuthHeaders(),
      },
    }),
  });
  _clientUrl = url;
  return _client;
}

export function resetClient(): void {
  _client = null;
  _clientUrl = "";
}
