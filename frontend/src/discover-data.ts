/** Lightweight Discover data client. Keeps YouTube credentials on the server. */
export interface DiscoverTrack {
  id: string;
  provider: string;
  provider_id?: string;
  title: string;
  artist: string;
  thumbnail?: string | null;
  duration?: number | null;
  published_at?: string | null;
  url?: string;
}

export interface DiscoverPayload {
  configured: boolean;
  source?: string;
  channel_id?: string;
  count?: number;
  tracks: DiscoverTrack[];
  recent: DiscoverTrack[];
  recommendations: DiscoverTrack[];
  artists: Array<{ name: string; count: number }>;
}

export async function loadDiscover(signal?: AbortSignal): Promise<DiscoverPayload | null> {
  try {
    const response = await fetch('/api/discover', {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
      signal,
    });
    if (!response.ok) return null;
    const payload = await response.json() as DiscoverPayload;
    return {
      configured: Boolean(payload?.configured),
      source: payload?.source,
      channel_id: payload?.channel_id,
      count: Number(payload?.count || payload?.tracks?.length || 0),
      tracks: Array.isArray(payload?.tracks) ? payload.tracks : [],
      recent: Array.isArray(payload?.recent) ? payload.recent : [],
      recommendations: Array.isArray(payload?.recommendations) ? payload.recommendations : [],
      artists: Array.isArray(payload?.artists) ? payload.artists : [],
    };
  } catch {
    return null;
  }
}

export function formatTrackDuration(seconds?: number | null): string {
  if (!Number.isFinite(seconds) || !seconds || seconds < 0) return '';
  const total = Math.floor(seconds);
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, '0')}`;
}
