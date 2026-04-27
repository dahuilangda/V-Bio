import { useEffect, useState } from 'react';
import { getCopilotConfig } from '../api/copilotApi';

export function useCopilotAvailability(): boolean {
  const [available, setAvailable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void getCopilotConfig()
      .then((config) => {
        if (!cancelled) setAvailable(config.enabled);
      })
      .catch(() => {
        if (!cancelled) setAvailable(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return available;
}
