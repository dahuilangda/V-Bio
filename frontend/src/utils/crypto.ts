export async function sha256Hex(input: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle || typeof subtle.digest !== 'function') {
    throw new Error('crypto.subtle is unavailable; a secure context (HTTPS or localhost) is required.');
  }
  const data = new TextEncoder().encode(input);
  const digest = await subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

export async function hashPassword(username: string, password: string): Promise<string> {
  const normalized = username.trim().toLowerCase();
  return sha256Hex(`${normalized}::${password}`);
}
