import dayjs from 'dayjs';

export const formatDateTime = (value?: string | null) => {
  if (!value) return '-';
  return dayjs(value).format('YYYY-MM-DD HH:mm:ss');
};

export const formatDuration = (seconds?: number | null) => {
  if (!seconds && seconds !== 0) return '-';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const min = Math.floor(seconds / 60);
  const sec = Math.round(seconds % 60);
  return `${min}m ${sec}s`;
};
