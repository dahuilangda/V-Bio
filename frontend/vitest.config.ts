import { defineConfig } from 'vitest/config';

// Dedicated vitest config (takes precedence over vite.config.ts) so the test runner doesn't pull in
// the app's dev-server proxy settings. Tests run in node — the suites under test are pure logic
// (no React/DOM); add @testing-library if component tests are needed later.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
