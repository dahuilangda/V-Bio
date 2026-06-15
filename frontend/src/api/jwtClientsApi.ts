import { requestManagement } from './backendClient';

export interface JwtClientRecord {
  client_id: string;
  name: string;
  issuer: string;
  audience: string;
  max_ttl_seconds: number;
  active: boolean;
  created_at: string;
  updated_at: string;
}

export interface JwtClientSecretResponse {
  client: JwtClientRecord;
  secret: string;
}

function adminHeaders(managementToken: string): Record<string, string> {
  return {
    'Content-Type': 'application/json',
    Accept: 'application/json',
    'X-VBio-Session': managementToken
  };
}

async function readPayload<T>(res: Response): Promise<T> {
  const payload = (await res.json().catch(() => ({}))) as T & { error?: string };
  if (!res.ok) {
    throw new Error(payload.error || `JWT client request failed with HTTP ${res.status}.`);
  }
  return payload;
}

export async function listJwtClients(managementToken: string): Promise<JwtClientRecord[]> {
  const res = await requestManagement('/vbio-api/admin/jwt-clients', {
    method: 'GET',
    headers: adminHeaders(managementToken)
  });
  const payload = await readPayload<{ clients?: JwtClientRecord[] }>(res);
  return Array.isArray(payload.clients) ? payload.clients : [];
}

export async function createJwtClient(
  managementToken: string,
  input: { name: string; issuer: string; audience: string; max_ttl_seconds: number }
): Promise<JwtClientSecretResponse> {
  const res = await requestManagement('/vbio-api/admin/jwt-clients', {
    method: 'POST',
    headers: adminHeaders(managementToken),
    body: JSON.stringify(input)
  });
  return readPayload<JwtClientSecretResponse>(res);
}

export async function updateJwtClient(
  managementToken: string,
  clientId: string,
  patch: Partial<Pick<JwtClientRecord, 'name' | 'issuer' | 'audience' | 'max_ttl_seconds' | 'active'>>
): Promise<JwtClientRecord> {
  const res = await requestManagement(`/vbio-api/admin/jwt-clients/${encodeURIComponent(clientId)}`, {
    method: 'PATCH',
    headers: adminHeaders(managementToken),
    body: JSON.stringify(patch)
  });
  const payload = await readPayload<{ client: JwtClientRecord }>(res);
  return payload.client;
}

export async function rotateJwtClient(managementToken: string, clientId: string): Promise<JwtClientSecretResponse> {
  const res = await requestManagement(`/vbio-api/admin/jwt-clients/${encodeURIComponent(clientId)}/rotate`, {
    method: 'POST',
    headers: adminHeaders(managementToken)
  });
  return readPayload<JwtClientSecretResponse>(res);
}

export async function deleteJwtClient(managementToken: string, clientId: string): Promise<void> {
  const res = await requestManagement(`/vbio-api/admin/jwt-clients/${encodeURIComponent(clientId)}`, {
    method: 'DELETE',
    headers: adminHeaders(managementToken)
  });
  await readPayload<{ ok?: boolean }>(res);
}
