const AVATAR_KEY = 'vbio_avatar_overrides_v1';

function readStore(): Record<string, string> {
  try {
    const raw = localStorage.getItem(AVATAR_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, string>;
    if (!parsed || typeof parsed !== 'object') return {};
    return parsed;
  } catch {
    return {};
  }
}

function writeStore(next: Record<string, string>): void {
  localStorage.setItem(AVATAR_KEY, JSON.stringify(next));
}

export function getAvatarOverride(userId: string): string {
  const store = readStore();
  return store[userId] || '';
}

export function setAvatarOverride(userId: string, avatarUrl: string): void {
  const store = readStore();
  if (!avatarUrl.trim()) {
    delete store[userId];
  } else {
    store[userId] = avatarUrl.trim();
  }
  writeStore(store);
}
